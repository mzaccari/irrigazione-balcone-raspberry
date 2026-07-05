"""Test di simulazione del demone: loop a clock iniettato, mock, file temporanei.

Nessun hardware, nessuna attesa reale: chiamiamo tick(now) passando l'ora e
avanzando a mano, e ispezioniamo state.json / events.jsonl.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import store
from clock import FixedClock
from daemon import Daemon
from pump_controller import DEFAULT_CONFIG, PumpController

TZ = ZoneInfo("Europe/Rome")


def at(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=TZ)


def prog(pid="p1", start="2026-07-01", end=None, times=None, duration=60, enabled=True):
    return {
        "id": pid,
        "enabled": enabled,
        "start_date": start,
        "end_date": end,
        "times": times if times is not None else ["07:00"],
        "duration_s": duration,
    }


def programs_cfg(pump_programs, options=None):
    caps = {"pompa_1": 25, "pompa_2": 25, "pompa_3": 20}
    pumps = {
        pid: {"tank_liters": caps[pid], "flow_lph": 600, "programs": pump_programs.get(pid, [])}
        for pid in ("pompa_1", "pompa_2", "pompa_3")
    }
    return {"options": options or {}, "pumps": pumps}


def make_daemon(tmp_path, programs):
    controller = PumpController(DEFAULT_CONFIG, mock=True)
    clock = FixedClock(at(2026, 7, 5, 0, 0, 0))
    programs_path = tmp_path / "programs.json"
    store.write_json_atomic(programs_path, programs)
    daemon = Daemon(
        controller,
        clock,
        programs_path=programs_path,
        state_path=tmp_path / "state.json",
        commands_dir=tmp_path / "commands",
        rejected_dir=tmp_path / "commands" / "rejected",
        events_path=tmp_path / "events.jsonl",
    )
    return daemon, controller


def active(controller, pump_id):
    return {ps.id: ps.active for ps in controller.snapshot()}[pump_id]


def read_state(tmp_path):
    return store.read_json(tmp_path / "state.json")


def event_types(tmp_path):
    return [e["type"] for e in store.read_events(tmp_path / "events.jsonl")]


# --- Ciclo programmato ------------------------------------------------------

def test_scheduled_run_starts_and_stops(tmp_path):
    d, ctrl = make_daemon(tmp_path, programs_cfg({"pompa_1": [prog(duration=60)]}))

    d.tick(at(2026, 7, 5, 7, 0, 10))
    assert active(ctrl, "pompa_1") is True
    assert d.current_run is not None and d.current_run.source == "scheduled"

    # fine erogazione (start 07:00:10 + 60s)
    d.tick(at(2026, 7, 5, 7, 1, 10))
    assert active(ctrl, "pompa_1") is False
    assert d.current_run is None
    # 60 s a 600 L/h = 10 L consumati: 25 -> 15
    assert abs(d.water["pompa_1"] - 15.0) < 0.01
    assert "programma_on" in event_types(tmp_path)
    assert "run_off" in event_types(tmp_path)


def test_water_decrements_gradually_during_run(tmp_path):
    d, ctrl = make_daemon(tmp_path, programs_cfg({"pompa_1": [prog(duration=120)]}))
    d.tick(at(2026, 7, 5, 7, 0, 0))       # avvio
    d.tick(at(2026, 7, 5, 7, 0, 30))      # dopo 30 s -> 5 L
    assert abs(d.water["pompa_1"] - 20.0) < 0.01
    assert active(ctrl, "pompa_1") is True


def test_scheduled_run_skipped_when_water_low(tmp_path):
    d, ctrl = make_daemon(tmp_path, programs_cfg({"pompa_1": [prog(duration=60)]}))
    d.water["pompa_1"] = 5.0  # servono 10 L
    d.tick(at(2026, 7, 5, 7, 0, 10))
    assert active(ctrl, "pompa_1") is False
    assert d.current_run is None
    assert "saltato_acqua" in event_types(tmp_path)
    assert d.warnings and "insufficiente" in d.warnings[-1]["message"]


def test_missed_occurrence_logged_and_not_run(tmp_path):
    d, ctrl = make_daemon(tmp_path, programs_cfg({"pompa_1": [prog(duration=60)]},
                                                 options={"catch_up_minutes": 5}))
    d.tick(at(2026, 7, 5, 7, 10, 0))  # 10 min dopo, fuori finestra
    assert active(ctrl, "pompa_1") is False
    assert "saltato_fuori_finestra" in event_types(tmp_path)


# --- Comandi manuali --------------------------------------------------------

def test_stop_all_interrupts_scheduled_run(tmp_path):
    d, ctrl = make_daemon(tmp_path, programs_cfg({"pompa_1": [prog(duration=120)]}))
    d.tick(at(2026, 7, 5, 7, 0, 0))
    assert active(ctrl, "pompa_1") is True

    store.enqueue_command(tmp_path / "commands", {"type": "stop_all"})
    d.tick(at(2026, 7, 5, 7, 0, 20))
    assert active(ctrl, "pompa_1") is False
    assert d.current_run is None
    assert "stop_all" in event_types(tmp_path)


def test_manual_on_preempts_scheduled(tmp_path):
    d, ctrl = make_daemon(tmp_path, programs_cfg({"pompa_1": [prog(duration=120)]}))
    d.tick(at(2026, 7, 5, 7, 0, 0))
    assert active(ctrl, "pompa_1") is True

    store.enqueue_command(tmp_path / "commands", {"type": "on", "pump": "pompa_2"})
    d.tick(at(2026, 7, 5, 7, 0, 20))
    assert active(ctrl, "pompa_1") is False
    assert active(ctrl, "pompa_2") is True
    assert d.current_run is not None
    assert d.current_run.source == "manual" and d.current_run.pump_id == "pompa_2"


def test_manual_pulse_runs_for_seconds(tmp_path):
    d, ctrl = make_daemon(tmp_path, programs_cfg({}))
    store.enqueue_command(tmp_path / "commands", {"type": "pulse", "pump": "pompa_3", "seconds": 6})
    d.tick(at(2026, 7, 5, 8, 0, 0))
    assert active(ctrl, "pompa_3") is True

    d.tick(at(2026, 7, 5, 8, 0, 6))
    assert active(ctrl, "pompa_3") is False
    assert d.current_run is None
    # 6 s a 600 L/h = 1 L: 20 -> 19
    assert abs(d.water["pompa_3"] - 19.0) < 0.05


def test_refill_full_and_partial(tmp_path):
    d, _ = make_daemon(tmp_path, programs_cfg({}))
    d.water["pompa_1"] = 3.0

    store.enqueue_command(tmp_path / "commands", {"type": "refill", "pump": "pompa_1"})
    d.tick(at(2026, 7, 5, 9, 0, 0))
    assert d.water["pompa_1"] == 25.0  # pieno (capacita)

    store.enqueue_command(tmp_path / "commands", {"type": "refill", "pump": "pompa_1", "liters": 10})
    d.tick(at(2026, 7, 5, 9, 0, 1))
    assert d.water["pompa_1"] == 10.0

    # oltre capacita -> clamp
    store.enqueue_command(tmp_path / "commands", {"type": "refill", "pump": "pompa_1", "liters": 999})
    d.tick(at(2026, 7, 5, 9, 0, 2))
    assert d.water["pompa_1"] == 25.0


def test_max_run_safety_cap(tmp_path):
    # manuale "on" indeterminato: deve spegnersi al tetto max_run_seconds
    d, ctrl = make_daemon(tmp_path, programs_cfg({}, options={"max_run_seconds": 30}))
    store.enqueue_command(tmp_path / "commands", {"type": "on", "pump": "pompa_1"})
    d.tick(at(2026, 7, 5, 10, 0, 0))
    assert active(ctrl, "pompa_1") is True

    d.tick(at(2026, 7, 5, 10, 0, 31))  # oltre i 30 s
    assert active(ctrl, "pompa_1") is False
    assert d.current_run is None
    assert any("massima" in w["message"] for w in d.warnings)


# --- Persistenza / stato ----------------------------------------------------

def test_last_fired_persisted_across_restart(tmp_path):
    programs = programs_cfg({"pompa_1": [prog(pid="p1", duration=60)]})
    d1, _ = make_daemon(tmp_path, programs)
    d1.tick(at(2026, 7, 5, 7, 0, 10))   # avvia
    d1.tick(at(2026, 7, 5, 7, 0, 20))   # scrive stato con last_fired

    # "riavvio": nuovo demone, stessi file, controller nuovo (tutto spento)
    controller2 = PumpController(DEFAULT_CONFIG, mock=True)
    d2 = Daemon(
        controller2,
        FixedClock(at(2026, 7, 5, 0, 0, 0)),
        programs_path=tmp_path / "programs.json",
        state_path=tmp_path / "state.json",
        commands_dir=tmp_path / "commands",
        rejected_dir=tmp_path / "commands" / "rejected",
        events_path=tmp_path / "events.jsonl",
    )
    assert d2.last_fired  # ripristinato
    d2.tick(at(2026, 7, 5, 7, 0, 30))   # stesso orario, stesso giorno
    assert active(controller2, "pompa_1") is False  # niente doppio avvio
    assert d2.current_run is None


def test_state_has_heartbeat_and_next_run(tmp_path):
    d, _ = make_daemon(tmp_path, programs_cfg({"pompa_1": [prog(times=["07:00", "19:00"])]}))
    now = at(2026, 7, 5, 8, 0, 0)
    d.tick(now)
    state = read_state(tmp_path)
    assert state["updated_at"] == now.isoformat()
    assert state["mock"] is True
    assert state["pumps"]["pompa_1"]["next_run"] == at(2026, 7, 5, 19, 0, 0).isoformat()


def test_unknown_command_does_not_crash(tmp_path):
    d, _ = make_daemon(tmp_path, programs_cfg({}))
    store.enqueue_command(tmp_path / "commands", {"type": "boh", "pump": "pompa_1"})
    d.tick(at(2026, 7, 5, 11, 0, 0))
    assert "comando_ignoto" in event_types(tmp_path)


def test_water_never_negative(tmp_path):
    d, ctrl = make_daemon(tmp_path, programs_cfg({}))
    d.water["pompa_1"] = 1.0
    store.enqueue_command(tmp_path / "commands", {"type": "on", "pump": "pompa_1"})
    d.tick(at(2026, 7, 5, 12, 0, 0))
    d.tick(at(2026, 7, 5, 12, 5, 0))  # 5 min: consumerebbe 50 L
    assert d.water["pompa_1"] == 0.0
