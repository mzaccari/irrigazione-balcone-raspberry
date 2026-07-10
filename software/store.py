"""Persistenza a file per il coordinamento demone <-> interfaccia.

Regole di robustezza (vedi piano):
- Un solo scrittore per file. Scrittura ATOMICA: file temporaneo nella STESSA
  cartella del target + os.replace (atomico su Windows e Linux se sullo stesso
  volume). Retry sullo scrittore perche su Windows os.replace puo dare
  PermissionError se un lettore tiene il file aperto.
- I lettori tollerano file assenti o a meta scrittura (JSONDecodeError):
  restituiscono un default e ritentano al giro dopo.
- Coda comandi: una cartella con un file per comando, nome ordinabile per
  timestamp. Chi scrive usa scrittura atomica (il temp ha suffisso .tmp e non
  viene raccolto dal glob *.json), chi consuma legge, applica e cancella.
- events.jsonl: log append-only (un evento per riga), scritto solo dal demone.
"""

from __future__ import annotations

import itertools
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


_WRITE_RETRIES = 4
_WRITE_BACKOFF = 0.02  # secondi, cresce linearmente ad ogni tentativo

# Contatore monotono per rompere i pareggi di time_ns (su Windows la risoluzione
# puo essere grossolana): garantisce ordine stabile dei comandi accodati dallo
# stesso processo.
_command_counter = itertools.count()


def _remove_quiet(path: str | Path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def write_json_atomic(path: str | Path, data: Any) -> None:
    """Scrive `data` come JSON in modo atomico, con retry su PermissionError."""
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False))


def write_text_atomic(path: str | Path, payload: str) -> None:
    """Scrive testo in modo atomico (stesso schema temp+replace del JSON)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    last_exc: Exception | None = None
    for attempt in range(_WRITE_RETRIES):
        fd, tmp = tempfile.mkstemp(
            dir=str(target.parent), prefix=target.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
            return
        except PermissionError as exc:  # Windows: un lettore tiene il file aperto
            last_exc = exc
            _remove_quiet(tmp)
            time.sleep(_WRITE_BACKOFF * (attempt + 1))
        except Exception:
            _remove_quiet(tmp)
            raise

    assert last_exc is not None
    raise last_exc


def read_json(path: str | Path) -> Any:
    """Legge JSON; propaga FileNotFoundError / JSONDecodeError al chiamante."""
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_json_or(path: str | Path, default: Any) -> Any:
    """Come read_json ma restituisce `default` se il file manca o e corrotto."""
    try:
        return read_json(path)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


# --- Coda comandi -----------------------------------------------------------

def enqueue_command(commands_dir: str | Path, command: dict[str, Any]) -> Path:
    """Accoda un comando come file JSON con nome ordinabile per timestamp."""
    commands_dir = Path(commands_dir)
    name = f"{time.time_ns():020d}_{next(_command_counter):06d}_{uuid.uuid4().hex[:6]}.json"
    dest = commands_dir / name
    write_json_atomic(dest, command)
    return dest


def drain_commands(
    commands_dir: str | Path, rejected_dir: str | Path | None = None
) -> list[dict[str, Any]]:
    """Legge e rimuove tutti i comandi in coda, in ordine di arrivo.

    I file illeggibili/corrotti vengono spostati in `rejected_dir` (se dato) o
    cancellati, senza mai sollevare: il loop del demone non deve morire per un
    file malformato.
    """
    commands_dir = Path(commands_dir)
    if not commands_dir.exists():
        return []

    out: list[dict[str, Any]] = []
    for entry in sorted(commands_dir.glob("*.json")):
        try:
            with entry.open("r", encoding="utf-8") as handle:
                command = json.load(handle)
            if not isinstance(command, dict):
                raise ValueError("comando non e un oggetto JSON")
        except (json.JSONDecodeError, OSError, ValueError):
            _reject(entry, rejected_dir)
            continue
        out.append(command)
        _remove_quiet(entry)
    return out


def _reject(entry: Path, rejected_dir: str | Path | None) -> None:
    if rejected_dir is None:
        _remove_quiet(entry)
        return
    rejected_dir = Path(rejected_dir)
    try:
        rejected_dir.mkdir(parents=True, exist_ok=True)
        os.replace(entry, rejected_dir / entry.name)
    except OSError:
        _remove_quiet(entry)


# --- Log eventi -------------------------------------------------------------

def append_event(events_path: str | Path, event: dict[str, Any]) -> None:
    """Aggiunge un evento (una riga JSON) al log. Solo il demone scrive qui."""
    events_path = Path(events_path)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False)
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def trim_jsonl(path: str | Path, max_bytes: int, keep_lines: int) -> bool:
    """Se il file JSONL supera `max_bytes`, tiene solo le ultime `keep_lines` righe.

    Riscrittura atomica; qualunque errore -> False senza sollevare (e' una
    manutenzione di cortesia, non deve mai fermare il demone).
    """
    target = Path(path)
    try:
        if not target.exists() or target.stat().st_size <= max_bytes:
            return False
        lines = target.read_text(encoding="utf-8").splitlines()
        tail = lines[-max(1, int(keep_lines)):]
        write_text_atomic(target, "\n".join(tail) + "\n")
        return True
    except OSError:
        return False


def read_events(events_path: str | Path, limit: int = 200) -> list[dict[str, Any]]:
    """Ritorna gli ultimi `limit` eventi del log (piu recenti in fondo)."""
    events_path = Path(events_path)
    if not events_path.exists():
        return []
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    out: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
