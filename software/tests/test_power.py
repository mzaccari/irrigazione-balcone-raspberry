"""Test di power.py: parser VE.Direct su frame di fixture, staleness, isteresi."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import power
from power import BatteryStateTracker, PowerStatus, VeDirectParser, checksum_frame

TZ = ZoneInfo("Europe/Rome")


def at(h, mi=0, s=0):
    return datetime(2026, 7, 21, h, mi, s, tzinfo=TZ)


FIELDS = [
    ("PID", "0xA053"),
    ("V", "13120"),      # mV
    ("I", "1500"),       # mA
    ("VPV", "18500"),
    ("PPV", "55"),
    ("CS", "3"),
]


# --- Parser -----------------------------------------------------------------------

def test_parser_roundtrip_single_frame():
    frames = VeDirectParser().feed(checksum_frame(FIELDS))
    assert len(frames) == 1
    assert frames[0]["V"] == "13120" and frames[0]["CS"] == "3"


def test_parser_discards_partial_first_frame_mid_stream():
    parser = VeDirectParser()
    full = checksum_frame(FIELDS)
    # aggancio a meta flusso: coda di un frame precedente + frame completo
    frames = parser.feed(full[10:] + full)
    assert len(frames) == 1  # il moncone iniziale fallisce il checksum


def test_parser_rejects_corrupted_frame():
    corrupted = bytearray(checksum_frame(FIELDS))
    corrupted[5] ^= 0xFF
    assert VeDirectParser().feed(bytes(corrupted)) == []


def test_parser_handles_byte_by_byte_feed():
    parser = VeDirectParser()
    frames = []
    for byte in checksum_frame(FIELDS):
        frames.extend(parser.feed(bytes([byte])))
    assert len(frames) == 1


def test_parse_status_units_and_missing_fields():
    status = power.parse_status(
        {"V": "13120", "I": "-800", "VPV": "18500", "PPV": "55", "CS": "5"}, at(12)
    )
    assert status.battery_v == 13.12 and status.battery_a == -0.8
    assert status.panel_v == 18.5 and status.panel_w == 55.0
    assert status.charge_state == "float"

    empty = power.parse_status({}, at(12))
    assert empty.battery_v is None and empty.charge_state is None


# --- Staleness -----------------------------------------------------------------------

def test_fresh_status_staleness():
    status = PowerStatus(13.1, 0.5, 18.0, 40.0, "bulk", at=at(12, 0, 0))
    assert power.fresh_status(status, at(12, 1, 0), stale_seconds=120) is status
    assert power.fresh_status(status, at(12, 3, 0), stale_seconds=120) is None  # stantio
    assert power.fresh_status(status, at(11, 59, 0), stale_seconds=120) is None  # futuro
    assert power.fresh_status(None, at(12), stale_seconds=120) is None


# --- BatteryStateTracker ----------------------------------------------------------------

def make_tracker(hold_s=600):
    return BatteryStateTracker(v_low=12.6, v_critical=12.3, hold_s=hold_s)


def test_tracker_adopts_first_reading_immediately():
    t = make_tracker()
    assert t.state == "sconosciuto"
    assert t.update(12.5, at(8, 0)) == "bassa"  # primo dato: subito


def test_tracker_requires_persistence_before_changing():
    t = make_tracker(hold_s=600)
    t.update(13.2, at(8, 0))
    assert t.state == "ok"
    # flessione da avvio pompa: 5 minuti sotto soglia NON bastano
    assert t.update(12.5, at(8, 1)) == "ok"
    assert t.update(12.5, at(8, 6)) == "ok"
    # rientra: candidato azzerato
    assert t.update(13.1, at(8, 7)) == "ok"
    assert t.update(12.5, at(8, 8)) == "ok"
    # stavolta persiste 10 minuti -> bassa
    assert t.update(12.5, at(8, 18)) == "bassa"


def test_tracker_hysteresis_on_recovery():
    t = make_tracker(hold_s=0)  # hold nullo per isolare l'isteresi
    t.update(12.5, at(9, 0))
    assert t.state == "bassa"
    assert t.update(12.65, at(9, 1)) == "bassa"   # sotto 12.6+0.15: resta bassa
    assert t.update(12.80, at(9, 2)) == "ok"      # sopra la soglia + isteresi


def test_tracker_critical_and_unknown():
    t = make_tracker(hold_s=0)
    t.update(12.2, at(10, 0))
    assert t.state == "critica"
    assert t.update(12.4, at(10, 1)) == "critica"  # dentro l'isteresi (< 12.45): resta
    assert t.update(12.5, at(10, 1)) == "bassa"    # sopra 12.3+0.15 ma sotto v_low
    assert t.update(None, at(10, 2)) == "sconosciuto"  # dati persi: subito
    assert t.update(13.3, at(10, 3)) == "ok"      # dati tornati: adottato subito


def test_mock_monitor_and_build():
    cfg_off = {"enabled": False}
    assert power.build_monitor(cfg_off, mock=True) is None
    monitor = power.build_monitor({"enabled": True}, mock=True)
    assert isinstance(monitor, power.MockPowerMonitor)
    status = PowerStatus(13.0, None, None, None, "bulk", at=at(11))
    monitor.set_status(status)
    assert power.fresh_status(monitor.latest, at(11, 1), 120) is status
    monitor.close()
