"""Logica PURA di scheduling e stima acqua (nessun I/O, nessun hardware).

E il cuore della correttezza del sistema: qui non si accende nessuna pompa, si
decide soltanto *cosa* andrebbe fatto dato l'istante corrente e lo stato dei
"gia scattati oggi". Tutto e testabile con un clock finto (vedi clock.FixedClock).

Modello di ricorrenza (confermato con l'utente): ogni programma vale OGNI GIORNO
tra una data d'inizio e una data di fine (o "per sempre" se end_date e null), a
uno o piu orari, per una certa durata. Piu programmi per pompa.

Deduplica: la chiave (pompa|programma|HH:MM) memorizza l'ultima DATA LOCALE in
cui quell'occorrenza e stata gestita (avviata o saltata). Confrontare per data
locale rende corretto anche il cambio di ora legale (nessun doppio avvio).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterator


DEFAULT_OPTIONS: dict[str, Any] = {
    "catch_up_minutes": 5,
    "max_run_seconds": 1200,
    "timezone": "Europe/Rome",
}


@dataclass(frozen=True)
class Occurrence:
    """Un singolo avvio programmato in una data/ora specifica."""

    pump_id: str
    program_id: str
    time_str: str
    scheduled: datetime
    duration_s: int
    delta_seconds: float  # now - scheduled: >=0 se l'orario e gia passato


def get_option(config: dict[str, Any], key: str) -> Any:
    return config.get("options", {}).get(key, DEFAULT_OPTIONS[key])


def occurrence_key(pump_id: str, program_id: str, time_str: str) -> str:
    return f"{pump_id}|{program_id}|{time_str}"


# --- Parsing difensivo ------------------------------------------------------

def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_hhmm(value: str) -> tuple[int, int]:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"orario non valido: {value!r}")
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"orario fuori range: {value!r}")
    return hh, mm


def _in_date_range(day: date, start: date, end: date | None) -> bool:
    if day < start:
        return False
    if end is not None and day > end:
        return False
    return True


def _iter_occurrences_on(
    config: dict[str, Any], now: datetime, day: date
) -> Iterator[Occurrence]:
    """Genera le occorrenze di TUTTE le pompe previste per la data `day`.

    Le righe malformate (date/orari/durate non validi) vengono ignorate invece
    di far esplodere il demone: il file puo essere anche modificato a mano.
    """
    tz = now.tzinfo
    pumps = config.get("pumps", {})
    for pump_id, pump in pumps.items():
        programs = pump.get("programs", []) if isinstance(pump, dict) else []
        for idx, prog in enumerate(programs):
            if not isinstance(prog, dict):
                continue
            if not prog.get("enabled", True):
                continue
            try:
                start = _parse_date(prog["start_date"])
            except (KeyError, ValueError, TypeError):
                continue
            end_raw = prog.get("end_date")
            try:
                end = _parse_date(end_raw) if end_raw else None
            except (ValueError, TypeError):
                continue
            if not _in_date_range(day, start, end):
                continue
            try:
                duration = int(prog.get("duration_s", 0))
            except (ValueError, TypeError):
                continue
            if duration <= 0:
                continue
            program_id = str(prog.get("id") or f"prog{idx}")
            for time_str in prog.get("times", []):
                try:
                    hh, mm = _parse_hhmm(str(time_str))
                except (ValueError, TypeError):
                    continue
                scheduled = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
                yield Occurrence(
                    pump_id=pump_id,
                    program_id=program_id,
                    time_str=f"{hh:02d}:{mm:02d}",
                    scheduled=scheduled,
                    duration_s=duration,
                    delta_seconds=(now - scheduled).total_seconds(),
                )


def _already_handled(last_fired: dict[str, str], occ: Occurrence, day: date) -> bool:
    key = occurrence_key(occ.pump_id, occ.program_id, occ.time_str)
    return last_fired.get(key) == day.isoformat()


# --- API principale ---------------------------------------------------------

def due_occurrences(
    config: dict[str, Any],
    now: datetime,
    last_fired: dict[str, str],
    catch_up_minutes: float,
) -> list[Occurrence]:
    """Occorrenze da avviare ORA: orario gia passato ma entro la finestra di
    recupero, e non ancora gestite oggi. Ordinate dalla piu vecchia.
    """
    window = catch_up_minutes * 60
    today = now.date()
    result = [
        occ
        for occ in _iter_occurrences_on(config, now, today)
        if not _already_handled(last_fired, occ, today)
        and 0 <= occ.delta_seconds <= window
    ]
    result.sort(key=lambda o: (o.scheduled, o.pump_id, o.program_id))
    return result


def missed_occurrences(
    config: dict[str, Any],
    now: datetime,
    last_fired: dict[str, str],
    catch_up_minutes: float,
) -> list[Occurrence]:
    """Occorrenze di oggi ormai fuori dalla finestra di recupero e mai gestite:
    vanno marcate come gestite e registrate come "saltate" nel log.
    """
    window = catch_up_minutes * 60
    today = now.date()
    return [
        occ
        for occ in _iter_occurrences_on(config, now, today)
        if not _already_handled(last_fired, occ, today) and occ.delta_seconds > window
    ]


def next_run_per_pump(
    config: dict[str, Any], now: datetime, horizon_days: int = 14
) -> dict[str, datetime]:
    """Prossimo avvio programmato (futuro) per ogni pompa, per la UI."""
    result: dict[str, datetime] = {}
    base = now.date()
    for offset in range(horizon_days + 1):
        day = base + timedelta(days=offset)
        for occ in _iter_occurrences_on(config, now, day):
            if occ.scheduled <= now:
                continue
            current = result.get(occ.pump_id)
            if current is None or occ.scheduled < current:
                result[occ.pump_id] = occ.scheduled
    return result


# --- Modello acqua ----------------------------------------------------------

def liters_for_duration(flow_lph: float, seconds: float) -> float:
    """Litri erogati stimati per `seconds` di funzionamento alla portata data."""
    return flow_lph / 3600.0 * max(0.0, seconds)


def has_enough_water(residual_liters: float, flow_lph: float, duration_s: float) -> bool:
    return residual_liters >= liters_for_duration(flow_lph, duration_s)


def apply_consumption(residual_liters: float, flow_lph: float, seconds: float) -> float:
    """Livello dopo aver erogato per `seconds`, mai sotto zero."""
    return max(0.0, residual_liters - liters_for_duration(flow_lph, seconds))


def clamp_level(liters: float, capacity: float) -> float:
    """Livello valido: tra 0 e la capacita del serbatoio (per il refill)."""
    return max(0.0, min(float(liters), float(capacity)))
