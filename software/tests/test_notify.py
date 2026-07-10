"""Test di notify.py: coda non bloccante, worker che non muore, regole pure."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import notify

TZ = ZoneInfo("Europe/Rome")


def at(h, mi=0):
    return datetime(2026, 7, 21, h, mi, tzinfo=TZ)


def wait_until(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class RecordingNotifier(notify._QueueNotifier):
    """Trasporto finto: registra i messaggi; opzionalmente blocca o fallisce."""

    def __init__(self, hold: threading.Event | None = None, fail_first: int = 0):
        self.messages: list[tuple[str, str, str]] = []
        self._hold = hold
        self._fail_left = fail_first
        super().__init__()

    def _post(self, title, message, priority):
        if self._hold is not None:
            self._hold.wait(5.0)
        if self._fail_left > 0:
            self._fail_left -= 1
            raise OSError("trasporto giu")
        self.messages.append((title, message, priority))


# --- Coda e worker ---------------------------------------------------------------

def test_send_delivers_via_worker():
    n = RecordingNotifier()
    n.send("Titolo", "Messaggio", "high")
    assert wait_until(lambda: len(n.messages) == 1)
    assert n.messages[0] == ("Titolo", "Messaggio", "high")
    assert n.stats()["sent"] == 1
    n.close()


def test_full_queue_drops_oldest_never_blocks():
    hold = threading.Event()  # worker bloccato: la coda si riempie
    n = RecordingNotifier(hold=hold)
    for i in range(notify.QUEUE_MAX + 8):
        n.send("t", f"m{i}")   # non deve MAI bloccare
    assert n.stats()["dropped"] > 0
    hold.set()
    assert wait_until(lambda: n.stats()["sent"] > 0)
    n.close()


def test_worker_survives_transport_errors():
    n = RecordingNotifier(fail_first=2)
    n.send("a", "1")
    n.send("b", "2")
    n.send("c", "3")
    assert wait_until(lambda: n.stats()["failed"] == 2 and len(n.messages) == 1)
    assert n.messages[0][0] == "c"  # il worker e vivo e consegna il terzo
    n.close()


# --- build_notifier -----------------------------------------------------------------

def test_build_notifier_disabled_or_misconfigured_is_null():
    assert isinstance(notify.build_notifier(None, env={}), notify.NullNotifier)
    assert isinstance(notify.build_notifier({"enabled": False}, env={}), notify.NullNotifier)
    # abilitato ma senza topic (ne env ne config) -> Null, mai crash
    assert isinstance(
        notify.build_notifier({"enabled": True, "backend": "ntfy"}, env={}),
        notify.NullNotifier,
    )
    assert isinstance(
        notify.build_notifier({"enabled": True, "backend": "piccione"}, env={}),
        notify.NullNotifier,
    )


def test_build_notifier_reads_topic_from_env_first():
    n = notify.build_notifier(
        {"enabled": True, "backend": "ntfy", "topic": "dal-file"},
        env={"NOTIFY_NTFY_TOPIC": "dalla-env"},
    )
    assert isinstance(n, notify.NtfyNotifier)
    assert n.topic == "dalla-env"
    n.close()

    n = notify.build_notifier({"enabled": True, "topic": "dal-file"}, env={})
    assert isinstance(n, notify.NtfyNotifier)
    assert n.topic == "dal-file"
    n.close()


# --- Regole pure ----------------------------------------------------------------------

def test_should_notify_allowlist():
    cfg = {"enabled": True, "events": ["serbatoio_vuoto"]}
    assert notify.should_notify(cfg, "serbatoio_vuoto") is True
    assert notify.should_notify(cfg, "programma_on") is False
    assert notify.should_notify({"enabled": False, "events": ["x"]}, "x") is False
    assert notify.should_notify(None, "x") is False


def test_heartbeat_due_once_per_day():
    assert notify.heartbeat_due(at(20, 29), None, "20:30") is False
    assert notify.heartbeat_due(at(20, 30), None, "20:30") is True
    assert notify.heartbeat_due(at(21, 0), "2026-07-21", "20:30") is False  # gia inviato
    assert notify.heartbeat_due(at(21, 0), "2026-07-20", "20:30") is True   # ieri
    assert notify.heartbeat_due(at(21, 0), None, None) is False             # orario rotto


# --- Formattazione ----------------------------------------------------------------------

def test_format_event_message():
    title, message, priority = notify.format_event_message(
        {"type": "serbatoio_vuoto", "pump_id": "pompa_1", "water": 1.0},
        pump_names={"pompa_1": "Piante Grandi"},
    )
    assert "VUOTO" in title and priority == "high"
    assert "Piante Grandi" in message and "1.0 L" in message

    title, _, _ = notify.format_event_message({"type": "tipo_nuovo"})
    assert "tipo_nuovo" in title  # fallback generico


def test_format_heartbeat():
    snapshot = {
        "pumps": {
            "pompa_1": {"name": "Piante Grandi", "water_liters": 12.4,
                        "days_left": 4.6, "moisture_pct": 41.0},
            "pompa_3": {"name": "oleandri", "water_liters": 3.0, "empty_latched": True},
        },
        "battery": {"battery_v": 13.12, "panel_w": 55.0, "state": "ok"},
        "weather_line": "ET0 4.8 mm, pioggia 0 mm",
        "warnings_count": 2,
    }
    title, message, _ = notify.format_heartbeat(snapshot, at(20, 30))
    assert "21/07" in title
    assert "12.4 L" in message and "~5 gg" in message and "41%" in message
    assert "[VUOTO!]" in message
    assert "13.12 V" in message and "55 W" in message
    assert "Avvisi attivi: 2" in message
