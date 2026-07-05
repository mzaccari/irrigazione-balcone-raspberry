"""Test della logica pura di scheduling e del modello acqua."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import scheduler
from scheduler import occurrence_key

TZ = ZoneInfo("Europe/Rome")


def at(year, month, day, hour=0, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=TZ)


def prog(pid="p1", start="2026-07-01", end=None, times=None, duration=45, enabled=True):
    return {
        "id": pid,
        "enabled": enabled,
        "start_date": start,
        "end_date": end,
        "times": times if times is not None else ["07:00"],
        "duration_s": duration,
    }


def cfg(programs, options=None, tank=25, flow=600):
    return {
        "options": options or {},
        "pumps": {"pompa_1": {"tank_liters": tank, "flow_lph": flow, "programs": programs}},
    }


def cfg_multi(pumps, options=None):
    return {"options": options or {}, "pumps": pumps}


def times(occs):
    return sorted(o.time_str for o in occs)


# --- Range date -------------------------------------------------------------

def test_due_within_range_and_window():
    c = cfg([prog(times=["07:00"])])
    due = scheduler.due_occurrences(c, at(2026, 7, 5, 7, 0, 10), {}, 5)
    assert len(due) == 1
    assert due[0].pump_id == "pompa_1"
    assert due[0].time_str == "07:00"
    assert due[0].duration_s == 45


def test_not_due_before_start():
    c = cfg([prog(start="2026-07-01", times=["07:00"])])
    now = at(2026, 6, 30, 7, 0, 10)
    assert scheduler.due_occurrences(c, now, {}, 5) == []
    assert scheduler.missed_occurrences(c, now, {}, 5) == []


def test_not_due_after_end():
    c = cfg([prog(start="2026-07-01", end="2026-07-04", times=["07:00"])])
    now = at(2026, 7, 5, 7, 0, 10)
    assert scheduler.due_occurrences(c, now, {}, 5) == []


def test_forever_end_none_still_due_far_future():
    c = cfg([prog(end=None, times=["07:00"])])
    due = scheduler.due_occurrences(c, at(2028, 1, 1, 7, 0, 10), {}, 5)
    assert len(due) == 1


def test_last_day_of_range_is_included():
    c = cfg([prog(start="2026-07-01", end="2026-07-05", times=["07:00"])])
    due = scheduler.due_occurrences(c, at(2026, 7, 5, 7, 0, 10), {}, 5)
    assert len(due) == 1


# --- Finestra di recupero / futuro ------------------------------------------

def test_future_time_not_due_nor_missed():
    c = cfg([prog(times=["07:00"])])
    now = at(2026, 7, 5, 6, 59, 0)
    assert scheduler.due_occurrences(c, now, {}, 5) == []
    assert scheduler.missed_occurrences(c, now, {}, 5) == []


def test_missed_outside_window():
    c = cfg([prog(times=["07:00"])])
    now = at(2026, 7, 5, 7, 10, 0)  # 10 min dopo, finestra 5 min
    assert scheduler.due_occurrences(c, now, {}, 5) == []
    missed = scheduler.missed_occurrences(c, now, {}, 5)
    assert len(missed) == 1


def test_window_edge_inclusive():
    c = cfg([prog(times=["07:00"])])
    # esattamente al bordo (5 min) -> ancora dovuta
    assert len(scheduler.due_occurrences(c, at(2026, 7, 5, 7, 5, 0), {}, 5)) == 1
    # un secondo oltre -> saltata
    assert scheduler.due_occurrences(c, at(2026, 7, 5, 7, 5, 1), {}, 5) == []
    assert len(scheduler.missed_occurrences(c, at(2026, 7, 5, 7, 5, 1), {}, 5)) == 1


# --- Orari e programmi multipli ---------------------------------------------

def test_multiple_times_per_day():
    c = cfg([prog(times=["07:00", "19:00"])])
    due = scheduler.due_occurrences(c, at(2026, 7, 5, 19, 0, 20), {}, 5)
    assert times(due) == ["19:00"]  # le 07:00 sono fuori finestra


def test_multiple_programs_same_pump_same_time():
    c = cfg([prog(pid="p1", times=["07:00"], duration=30),
             prog(pid="p2", times=["07:00"], duration=60)])
    due = scheduler.due_occurrences(c, at(2026, 7, 5, 7, 0, 10), {}, 5)
    assert len(due) == 2
    assert sorted(o.duration_s for o in due) == [30, 60]


def test_sorting_earliest_scheduled_first():
    c = cfg_multi({
        "pompa_1": {"tank_liters": 25, "flow_lph": 600, "programs": [prog(times=["07:00"])]},
        "pompa_2": {"tank_liters": 25, "flow_lph": 600, "programs": [prog(times=["06:00"])]},
    })
    # finestra ampia perche entrambe siano "dovute" alle 07:05
    due = scheduler.due_occurrences(c, at(2026, 7, 5, 7, 5, 0), {}, 120)
    assert [o.pump_id for o in due] == ["pompa_2", "pompa_1"]


# --- Deduplica --------------------------------------------------------------

def test_dedup_same_day_not_refired():
    c = cfg([prog(pid="p1", times=["07:00"])])
    key = occurrence_key("pompa_1", "p1", "07:00")
    last_fired = {key: "2026-07-05"}
    assert scheduler.due_occurrences(c, at(2026, 7, 5, 7, 0, 30), last_fired, 5) == []


def test_dedup_fires_again_next_day():
    c = cfg([prog(pid="p1", times=["07:00"])])
    key = occurrence_key("pompa_1", "p1", "07:00")
    last_fired = {key: "2026-07-05"}
    due = scheduler.due_occurrences(c, at(2026, 7, 6, 7, 0, 30), last_fired, 5)
    assert len(due) == 1


# --- Robustezza al file malformato ------------------------------------------

def test_disabled_program_never_due():
    c = cfg([prog(enabled=False, times=["07:00"])])
    assert scheduler.due_occurrences(c, at(2026, 7, 5, 7, 0, 10), {}, 5) == []


def test_zero_and_negative_duration_ignored():
    c = cfg([prog(times=["07:00"], duration=0)])
    assert scheduler.due_occurrences(c, at(2026, 7, 5, 7, 0, 10), {}, 5) == []


def test_bad_time_and_date_entries_ignored():
    c = cfg([
        {"id": "bad_date", "start_date": "non-una-data", "times": ["07:00"], "duration_s": 30},
        {"id": "ok", "start_date": "2026-07-01", "times": ["nonvalido", "07:00"], "duration_s": 30},
    ])
    due = scheduler.due_occurrences(c, at(2026, 7, 5, 7, 0, 10), {}, 5)
    assert len(due) == 1
    assert due[0].program_id == "ok"


# --- Ora legale (DST) Europe/Rome -------------------------------------------

def test_no_hour_drift_on_fall_back_day():
    # 2026-10-25: si torna indietro di un'ora nella notte. Un programma alle 07:00
    # deve restare alle 07:00 locali (nessun offset di un'ora).
    c = cfg([prog(times=["07:00"])])
    now = at(2026, 10, 25, 7, 3, 0)
    due = scheduler.due_occurrences(c, now, {}, 5)
    assert len(due) == 1
    # delta ~180s: se ci fosse un bug UTC/DST sarebbe ~3780s (fuori finestra)
    assert 150 <= due[0].delta_seconds <= 210


def test_no_hour_drift_on_spring_forward_day():
    # 2026-03-29: si va avanti di un'ora. Le 07:00 restano le 07:00 locali.
    c = cfg([prog(start="2026-01-01", times=["07:00"])])
    now = at(2026, 3, 29, 7, 3, 0)
    due = scheduler.due_occurrences(c, now, {}, 5)
    assert len(due) == 1
    assert 150 <= due[0].delta_seconds <= 210


def test_fall_back_day_fires_once_with_dedup():
    # Simula il passaggio del tempo attorno alle 07:00 del giorno di fall-back:
    # con la deduplica per data l'occorrenza scatta una sola volta.
    c = cfg([prog(pid="p1", times=["07:00"])])
    key = occurrence_key("pompa_1", "p1", "07:00")
    last_fired: dict[str, str] = {}
    fires = 0
    for minute in range(0, 6):  # 07:00 .. 07:05
        now = at(2026, 10, 25, 7, minute, 0)
        due = scheduler.due_occurrences(c, now, last_fired, 5)
        if due:
            fires += 1
            last_fired[key] = now.date().isoformat()
    assert fires == 1


# --- Prossimo avvio (per la UI) ---------------------------------------------

def test_next_run_same_day_later_time():
    c = cfg([prog(times=["07:00", "19:00"])])
    nxt = scheduler.next_run_per_pump(c, at(2026, 7, 5, 8, 0, 0))
    assert nxt["pompa_1"] == at(2026, 7, 5, 19, 0, 0)


def test_next_run_rolls_to_tomorrow():
    c = cfg([prog(times=["07:00", "19:00"])])
    nxt = scheduler.next_run_per_pump(c, at(2026, 7, 5, 20, 0, 0))
    assert nxt["pompa_1"] == at(2026, 7, 6, 7, 0, 0)


def test_next_run_absent_when_no_programs():
    c = cfg([])
    assert scheduler.next_run_per_pump(c, at(2026, 7, 5, 8, 0, 0)) == {}


# --- Modello acqua ----------------------------------------------------------

def test_liters_for_duration():
    assert scheduler.liters_for_duration(600, 60) == 10.0
    assert scheduler.liters_for_duration(600, 30) == 5.0
    assert scheduler.liters_for_duration(600, 0) == 0.0


def test_has_enough_water_boundary():
    assert scheduler.has_enough_water(10.0, 600, 60) is True
    assert scheduler.has_enough_water(9.99, 600, 60) is False


def test_apply_consumption_and_clamp():
    assert scheduler.apply_consumption(25, 600, 60) == 15.0
    assert scheduler.apply_consumption(3, 600, 60) == 0.0  # non va sotto zero


def test_clamp_level():
    assert scheduler.clamp_level(30, 25) == 25.0
    assert scheduler.clamp_level(-5, 25) == 0.0
    assert scheduler.clamp_level(10, 25) == 10.0


def test_get_option_defaults():
    assert scheduler.get_option({}, "catch_up_minutes") == 5
    assert scheduler.get_option({"options": {"catch_up_minutes": 12}}, "catch_up_minutes") == 12
