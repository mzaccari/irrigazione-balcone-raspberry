"""Notifiche push (ntfy.sh) senza mai toccare il tick del demone.

Architettura: `send()` accoda soltanto (put_nowait su coda bounded, drop del
piu' vecchio se piena) e un worker thread dedicato fa la POST con timeout.
Ogni eccezione di trasporto viene inghiottita e contata: una notifica persa
non deve MAI fermare l'irrigazione.

Il topic ntfy e' l'unico segreto: viene letto dall'ambiente
(NOTIFY_NTFY_TOPIC, impostato via drop-in systemd sul Pi) e NON va salvato in
programs.json, che e' versionato su GitHub. Pubblicazione in modalita' JSON
(POST alla radice del server): i titoli con accenti viaggiano in UTF-8 senza
problemi di header.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import urllib.request
from datetime import datetime
from typing import Any, Mapping


NTFY_DEFAULT_SERVER = "https://ntfy.sh"
QUEUE_MAX = 20
_PRIORITIES = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}

ENV_NTFY_TOPIC = "NOTIFY_NTFY_TOPIC"


class Notifier:
    """Interfaccia: send() non blocca e non solleva mai."""

    def send(self, title: str, message: str, priority: str = "default") -> None:
        raise NotImplementedError

    def stats(self) -> dict[str, int]:
        return {"sent": 0, "failed": 0, "dropped": 0}

    def close(self) -> None:
        pass


class NullNotifier(Notifier):
    """Default quando le notifiche sono disabilitate o mal configurate."""

    def send(self, title: str, message: str, priority: str = "default") -> None:
        pass


class _QueueNotifier(Notifier):
    """Base comune: coda bounded + worker thread; il trasporto e' _post()."""

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[str, str, str] | None] = queue.Queue(maxsize=QUEUE_MAX)
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self._thread = threading.Thread(
            target=self._worker, name="notifier", daemon=True
        )
        self._thread.start()

    def send(self, title: str, message: str, priority: str = "default") -> None:
        item = (str(title), str(message), str(priority))
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            # coda piena: scarta il piu' vecchio e riprova (mai bloccare il tick)
            try:
                self._queue.get_nowait()
                self.dropped += 1
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                self.dropped += 1

    def stats(self) -> dict[str, int]:
        return {"sent": self.sent, "failed": self.failed, "dropped": self.dropped}

    def close(self) -> None:
        try:
            self._queue.put_nowait(None)  # sentinella di uscita
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(None)
            except (queue.Empty, queue.Full):
                pass
        self._thread.join(timeout=2.0)

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self._post(*item)
                self.sent += 1
            except Exception:
                self.failed += 1  # trasporto fallito: contato, mai propagato

    def _post(self, title: str, message: str, priority: str) -> None:
        raise NotImplementedError


class NtfyNotifier(_QueueNotifier):
    def __init__(
        self,
        topic: str,
        server: str = NTFY_DEFAULT_SERVER,
        timeout_s: float = 5.0,
    ) -> None:
        self.topic = topic
        self.server = server.rstrip("/")
        self.timeout_s = timeout_s
        super().__init__()

    def _post(self, title: str, message: str, priority: str) -> None:
        payload = json.dumps(
            {
                "topic": self.topic,
                "title": title,
                "message": message,
                "priority": _PRIORITIES.get(priority, 3),
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.server,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            response.read()


def build_notifier(
    notify_cfg: Mapping[str, Any] | None,
    env: Mapping[str, str] | None = None,
) -> Notifier:
    """Notifier dalla config; qualunque problema -> NullNotifier (mai crash)."""
    env = os.environ if env is None else env
    if not notify_cfg or not notify_cfg.get("enabled"):
        return NullNotifier()
    backend = str(notify_cfg.get("backend", "ntfy"))
    if backend == "ntfy":
        topic = env.get(ENV_NTFY_TOPIC) or str(notify_cfg.get("topic") or "")
        if not topic:
            return NullNotifier()
        server = str(notify_cfg.get("server") or NTFY_DEFAULT_SERVER)
        return NtfyNotifier(topic=topic, server=server)
    return NullNotifier()


# --- Regole pure -----------------------------------------------------------------

def should_notify(notify_cfg: Mapping[str, Any] | None, event_type: str) -> bool:
    if not notify_cfg or not notify_cfg.get("enabled"):
        return False
    events = notify_cfg.get("events", [])
    return isinstance(events, list) and event_type in events


def heartbeat_due(now: datetime, last_sent_date: str | None, at_hhmm: Any) -> bool:
    """Una volta al giorno, non prima dell'orario indicato (chiave per data
    locale: un riavvio del demone nello stesso giorno non lo rimanda)."""
    try:
        hh_str, mm_str = str(at_hhmm).split(":")
        hh, mm = int(hh_str), int(mm_str)
    except (ValueError, AttributeError):
        return False
    if (now.hour, now.minute) < (hh, mm):
        return False
    return now.date().isoformat() != last_sent_date


# --- Formattazione messaggi (pura) ------------------------------------------------

_EVENT_TITLES = {
    "serbatoio_vuoto": ("Serbatoio VUOTO", "high"),
    "saltato_serbatoio_vuoto": ("Irrigazione saltata: serbatoio vuoto", "high"),
    "saltato_acqua": ("Irrigazione saltata: acqua stimata insufficiente", "high"),
    "saltato_umidita": ("Irrigazione saltata: terreno gia umido", "default"),
    "saltato_meteo": ("Irrigazione saltata: pioggia prevista", "default"),
    "saltato_batteria": ("Irrigazione saltata: batteria critica", "high"),
    "batteria_bassa": ("Batteria bassa", "high"),
    "saltato_fuori_finestra": ("Irrigazione persa (fuori finestra)", "default"),
    "config_avviso": ("Configurazione da controllare", "default"),
    "max_run": ("Pompa fermata per durata massima", "high"),
}


def format_event_message(
    event: Mapping[str, Any],
    pump_names: Mapping[str, str] | None = None,
) -> tuple[str, str, str]:
    """(titolo, messaggio, priorita') per un evento del log."""
    event_type = str(event.get("type", "evento"))
    title, priority = _EVENT_TITLES.get(event_type, (f"Irrigatore: {event_type}", "default"))
    pump_id = event.get("pump_id")
    name = (pump_names or {}).get(pump_id, pump_id) if pump_id else None

    parts: list[str] = []
    if name:
        parts.append(str(name))
    if event.get("time"):
        parts.append(f"programma {event['time']}")
    if event.get("water") is not None:
        parts.append(f"acqua stimata {event['water']} L")
    if event.get("reasons"):
        parts.extend(str(r) for r in event["reasons"])
    if event.get("warnings"):
        parts.extend(str(w) for w in event["warnings"])
    message = " - ".join(parts) if parts else str(event.get("at", ""))
    return title, message, priority


def format_heartbeat(snapshot: Mapping[str, Any], now: datetime) -> tuple[str, str, str]:
    """Riepilogo giornaliero "tutto ok" da un piccolo snapshot del demone."""
    lines: list[str] = []
    pumps = snapshot.get("pumps", {})
    for pump_id, info in (pumps if isinstance(pumps, Mapping) else {}).items():
        name = info.get("name", pump_id)
        water = info.get("water_liters")
        days = info.get("days_left")
        line = f"{name}: {water:.1f} L" if isinstance(water, (int, float)) else f"{name}: ?"
        if isinstance(days, (int, float)):
            line += f" (~{days:.0f} gg)"
        if info.get("empty_latched"):
            line += " [VUOTO!]"
        moisture = info.get("moisture_pct")
        if isinstance(moisture, (int, float)):
            line += f", umidita {moisture:.0f}%"
        lines.append(line)

    battery = snapshot.get("battery")
    if isinstance(battery, Mapping):
        volt = battery.get("battery_v")
        panel = battery.get("panel_w")
        state = battery.get("state")
        line = "Batteria: "
        line += f"{volt:.2f} V" if isinstance(volt, (int, float)) else "?"
        if isinstance(panel, (int, float)):
            line += f", pannello {panel:.0f} W"
        if state:
            line += f" ({state})"
        lines.append(line)

    weather_line = snapshot.get("weather_line")
    if weather_line:
        lines.append(str(weather_line))

    n_warnings = snapshot.get("warnings_count", 0)
    if n_warnings:
        lines.append(f"Avvisi attivi: {n_warnings}")

    title = f"Irrigatore ok - {now.strftime('%d/%m')}"
    return title, "\n".join(lines) if lines else "Sistema attivo.", "default"
