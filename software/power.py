"""Telemetria energia dal Victron SmartSolar via VE.Direct (seriale USB).

Il regolatore trasmette DA SOLO, ~1 frame al secondo a 19200 baud: righe
"\r\nETICHETTA\tVALORE" chiuse da un campo "Checksum" il cui byte rende la
somma dell'intero frame 0 mod 256. Qui ci sono:

- `VeDirectParser`: parser incrementale PURO (byte -> frame validati), testabile
  su fixture senza hardware;
- `BatteryStateTracker`: macchina a stati PURA ok/bassa/critica con isteresi e
  persistenza temporale (la curva LiFePO4 e' piatta e l'avvio pompa fa flettere
  la tensione: niente decisioni su letture istantanee);
- `PowerMonitor`: UNICO punto che tocca la seriale (pyserial), in un thread
  dedicato che aggiorna solo `latest` — il tick del demone legge l'ultimo
  frame e basta, mai la porta. `MockPowerMonitor` per test/PC.

Fail-safe: dati assenti o stantii -> stato "sconosciuto" -> il motore
decisionale resta NEUTRO (un cavo staccato non deve fermare l'irrigazione);
la protezione attiva scatta solo su letture fresche e persistenti.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


VEDIRECT_BAUD = 19200
_CHECKSUM_LABEL = b"Checksum\t"
_MAX_BUFFER = 4096

# Codici CS (stato di carica) del protocollo VE.Direct per gli MPPT.
CHARGE_STATES = {
    0: "off",
    2: "guasto",
    3: "bulk",
    4: "absorption",
    5: "float",
    7: "equalize",
    245: "starting",
    247: "equalize",
    252: "ext_control",
}


@dataclass(frozen=True)
class PowerStatus:
    battery_v: float | None   # Volt
    battery_a: float | None   # Ampere (positiva = carica)
    panel_v: float | None     # Volt pannello
    panel_w: float | None     # Watt pannello
    charge_state: str | None  # bulk/absorption/float/...
    at: datetime              # istante (aware) dell'ultimo frame valido


# --- Parser (puro) ---------------------------------------------------------------

class VeDirectParser:
    """Accumula byte e restituisce i frame con checksum valido.

    Il primo frame dopo l'aggancio a meta' flusso fallisce il checksum e viene
    scartato: e' il comportamento voluto (si riparte dal frame successivo).
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[dict[str, str]]:
        frames: list[dict[str, str]] = []
        self._buf.extend(data)
        while True:
            idx = self._buf.find(_CHECKSUM_LABEL)
            if idx < 0 or len(self._buf) < idx + len(_CHECKSUM_LABEL) + 1:
                break
            end = idx + len(_CHECKSUM_LABEL) + 1  # incluso il byte di checksum
            block = bytes(self._buf[:end])
            del self._buf[:end]
            if sum(block) % 256 == 0:
                frames.append(_parse_fields(block[:idx]))
        if len(self._buf) > _MAX_BUFFER:  # flusso senza checksum: non crescere all'infinito
            del self._buf[: -len(_CHECKSUM_LABEL)]
        return frames


def _parse_fields(data: bytes) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in data.split(b"\r\n"):
        if not line or line.startswith(b":"):  # righe del protocollo HEX: ignorate
            continue
        label, _, value = line.partition(b"\t")
        if label:
            fields[label.decode("latin-1")] = value.decode("latin-1")
    return fields


def parse_status(fields: dict[str, str], at: datetime) -> PowerStatus:
    """Campi VE.Direct -> PowerStatus (unita' SI; campi assenti -> None)."""

    def as_int(label: str) -> int | None:
        try:
            return int(fields[label])
        except (KeyError, ValueError):
            return None

    mv = as_int("V")
    ma = as_int("I")
    panel_mv = as_int("VPV")
    panel_w = as_int("PPV")
    cs = as_int("CS")
    return PowerStatus(
        battery_v=mv / 1000.0 if mv is not None else None,
        battery_a=ma / 1000.0 if ma is not None else None,
        panel_v=panel_mv / 1000.0 if panel_mv is not None else None,
        panel_w=float(panel_w) if panel_w is not None else None,
        charge_state=CHARGE_STATES.get(cs, f"cs_{cs}") if cs is not None else None,
        at=at,
    )


def fresh_status(
    latest: PowerStatus | None, now: datetime, stale_seconds: float
) -> PowerStatus | None:
    """L'ultimo stato solo se abbastanza fresco, altrimenti None (-> neutro)."""
    if latest is None:
        return None
    age = (now - latest.at).total_seconds()
    if not 0 <= age <= float(stale_seconds):
        return None
    return latest


# --- Stato batteria (puro) ---------------------------------------------------------

class BatteryStateTracker:
    """ok / bassa / critica / sconosciuto, con isteresi e persistenza.

    Un cambio di stato richiede che la nuova condizione persista `hold_s`
    secondi (l'avvio di una pompa fa flettere la tensione per qualche istante);
    per USCIRE da bassa/critica la tensione deve superare la soglia di
    `hysteresis_v`. Tensione None -> subito "sconosciuto" (mai bloccante);
    il primo dato valido dopo "sconosciuto" viene adottato subito.
    """

    def __init__(
        self,
        v_low: float,
        v_critical: float,
        hold_s: float = 600.0,
        hysteresis_v: float = 0.15,
    ) -> None:
        self.v_low = float(v_low)
        self.v_critical = float(v_critical)
        self.hold_s = float(hold_s)
        self.hysteresis_v = float(hysteresis_v)
        self.state = "sconosciuto"
        self._candidate: str | None = None
        self._candidate_since: datetime | None = None

    def update(self, voltage: float | None, now: datetime) -> str:
        target = self._target(voltage)
        if target == "sconosciuto":
            self.state = "sconosciuto"
            self._candidate = None
            return self.state
        if self.state == "sconosciuto":
            self.state = target  # primo dato valido: adottato subito
            self._candidate = None
            return self.state
        if target == self.state:
            self._candidate = None
            return self.state
        if target != self._candidate or self._candidate_since is None:
            self._candidate = target
            self._candidate_since = now
        if (now - self._candidate_since).total_seconds() >= self.hold_s:
            self.state = target
            self._candidate = None
        return self.state

    def _target(self, voltage: float | None) -> str:
        if voltage is None:
            return "sconosciuto"
        # Per uscire da uno stato basso serve superare la soglia + isteresi.
        crit_th = self.v_critical + (self.hysteresis_v if self.state == "critica" else 0.0)
        low_th = self.v_low + (
            self.hysteresis_v if self.state in ("bassa", "critica") else 0.0
        )
        if voltage <= crit_th:
            return "critica"
        if voltage <= low_th:
            return "bassa"
        return "ok"


# --- Monitor (I/O in thread, mai nel tick) -------------------------------------------

class MockPowerMonitor:
    """Gemello mock: i test impostano direttamente `latest`."""

    def __init__(self) -> None:
        self.latest: PowerStatus | None = None

    def set_status(self, status: PowerStatus | None) -> None:
        self.latest = status

    def close(self) -> None:
        pass


class PowerMonitor:
    """Legge la porta VE.Direct in un thread e tiene solo l'ultimo frame.

    L'assegnazione di `latest` e' atomica (GIL): nessun lock necessario.
    Errori seriale -> pausa e riapertura; il thread non muore mai da solo.
    """

    def __init__(self, port: str, baud: int = VEDIRECT_BAUD) -> None:
        self.port = port
        self.baud = baud
        self.latest: PowerStatus | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="vedirect", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3.0)

    def _run(self) -> None:
        parser = VeDirectParser()
        ser: Any = None
        while not self._stop.is_set():
            try:
                if ser is None:
                    ser = self._open()
                data = ser.read(256)  # timeout breve impostato in _open
                if data:
                    for fields in parser.feed(data):
                        status = parse_status(
                            fields, datetime.now(timezone.utc).astimezone()
                        )
                        if status.battery_v is not None:
                            self.latest = status
            except Exception:
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass
                    ser = None
                parser = VeDirectParser()
                self._stop.wait(5.0)  # respiro prima di riaprire la porta
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

    def _open(self) -> Any:
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError(
                "pyserial non e' installato. `pip install pyserial` sul Raspberry "
                "oppure disabilita `power.enabled` in programs.json."
            ) from exc
        return serial.Serial(self.port, self.baud, timeout=2)


def build_monitor(
    power_cfg: dict[str, Any], mock: bool
) -> PowerMonitor | MockPowerMonitor | None:
    """Monitor dalla config: None se disabilitato, mock fuori dal Pi."""
    if not power_cfg.get("enabled"):
        return None
    if mock:
        return MockPowerMonitor()
    return PowerMonitor(str(power_cfg.get("serial_port", "/dev/ttyUSB0")))


def checksum_frame(fields: Iterable[tuple[str, str]]) -> bytes:
    """Costruisce un frame VE.Direct valido (per test e simulazioni)."""
    body = b"".join(
        b"\r\n" + label.encode("latin-1") + b"\t" + value.encode("latin-1")
        for label, value in fields
    )
    body += b"\r\n" + b"Checksum\t"
    check = (256 - sum(body) % 256) % 256
    return body + bytes([check])
