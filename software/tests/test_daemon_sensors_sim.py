"""Simulazioni del demone CON sensori: galleggianti, umidita, meteo, batteria.

Stesso stile di test_daemon_sim.py: clock iniettato, tick(now) a mano, mock
per tutto l'hardware, ispezione di state.json / events.jsonl. In piu qui si
iniettano banchi sensori mock, un monitor batteria finto, un notifier
sincrono registratore e un weather_refresh finto (mai thread, mai rete).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import notify
import power
import store
from clock import FixedClock
from daemon import Daemon
from power import PowerStatus
from pump_controller import DEFAULT_CONFIG, PumpController
from sensors import FloatSwitchBank, MoistureSensorBank

TZ = ZoneInfo("Europe/Rome")

# raw per il sensore calibrato 26000 (secco) -> 10500 (bagnato)
RAW_DRY, RAW_WET = 26000, 10500


def raw_for(percent: float) -> int:
    return int(RAW_DRY - percent / 100.0 * (RAW_DRY - RAW_WET))


def at(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=TZ)


class RecordingNotifier(notify.Notifier):
    """Notifier sincrono per i test: registra e basta."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    def send(self, title: str, message: str, priority: str = "default") -> None:
        self.messages.append((title, message, priority))

    def stats(self) -> dict[str, int]:
        return {"sent": len(self.messages), "failed": 0, "dropped": 0}


def prog(pid="p1", start="2026-07-01", times=None, duration=60):
    return {"id": pid, "enabled": True, "start_date": start, "end_date": None,
            "times": times or ["07:00"], "duration_s": duration}


def base_programs(pump1_sensors=None, pump1_duration=60, options=None, **top):
    pumps = {
        "pompa_1": {"tank_liters": 25, "flow_lph": 600,
                    "programs": [prog(duration=pump1_duration)]},
        "pompa_2": {"tank_liters": 25, "flow_lph": 600, "programs": []},
        "pompa_3": {"tank_liters": 20, "flow_lph": 600, "programs": []},
    }
    if pump1_sensors is not None:
        pumps["pompa_1"]["sensors"] = pump1_sensors
    data = {"options": options or {}, "pumps": pumps}
    data.update(top)
    return data


FLOAT_CFG = {"float": {"gpio": 23, "debounce_s": 10, "reserve_liters": 1.0}}
MOISTURE_CFG = {
    "moisture": [{"id": "p1_m1", "adc": {"addr": "0x48", "channel": 0},
                  "raw_dry": RAW_DRY, "raw_wet": RAW_WET}],
    "thresholds": {"skip_above": 80, "reduce_above": 65, "boost_below": 25},
}


def make_daemon(tmp_path, programs, *, power_monitor=None, notifier=None,
                weather_refresh=None, start=None):
    controller = PumpController(DEFAULT_CONFIG, mock=True)
    clock = FixedClock(start or at(2026, 7, 5, 0, 0, 0))
    store.write_json_atomic(tmp_path / "programs.json", programs)
    daemon = Daemon(
        controller,
        clock,
        programs_path=tmp_path / "programs.json",
        state_path=tmp_path / "state.json",
        commands_dir=tmp_path / "commands",
        rejected_dir=tmp_path / "commands" / "rejected",
        events_path=tmp_path / "events.jsonl",
        weather_path=tmp_path / "weather.json",
        sensors_log_path=tmp_path / "sensors.jsonl",
        float_bank=FloatSwitchBank(mock=True),
        moisture_bank=MoistureSensorBank(mock=True),
        power_monitor=power_monitor,
        notifier=notifier if notifier is not None else RecordingNotifier(),
        weather_refresh=weather_refresh or (lambda cfg, path, now, tz: None),
    )
    return daemon, controller


def active(controller, pump_id):
    return {ps.id: ps.active for ps in controller.snapshot()}[pump_id]


def events(tmp_path, etype=None):
    all_events = store.read_events(tmp_path / "events.jsonl")
    return [e for e in all_events if etype is None or e["type"] == etype]


def read_state(tmp_path):
    return store.read_json(tmp_path / "state.json")


# --- Galleggiante -------------------------------------------------------------

def test_float_trip_stops_run_and_latches(tmp_path):
    d, ctrl = make_daemon(tmp_path, base_programs(FLOAT_CFG, pump1_duration=120))
    d.tick(at(2026, 7, 5, 7, 0, 0))
    assert active(ctrl, "pompa_1") is True

    d.float_bank.mock_input("pompa_1").set_water_present(False)
    d.tick(at(2026, 7, 5, 7, 0, 20))   # vuoto da 0 s: debounce parte
    assert active(ctrl, "pompa_1") is True
    d.tick(at(2026, 7, 5, 7, 0, 30))   # 10 s consecutivi: scatta
    assert active(ctrl, "pompa_1") is False
    assert d.current_run is None
    assert d.tank_empty["pompa_1"] is True
    assert d.water["pompa_1"] == 1.0   # riconciliata alla riserva

    run_off = events(tmp_path, "run_off")[-1]
    assert run_off["reason"] == "stop_galleggiante"
    assert events(tmp_path, "serbatoio_vuoto")
    state = read_state(tmp_path)
    assert state["pumps"]["pompa_1"]["float"]["empty_latched"] is True


def test_latch_blocks_scheduled_start_once_per_day(tmp_path):
    d, ctrl = make_daemon(tmp_path, base_programs(FLOAT_CFG))
    d.float_bank.mock_input("pompa_1").set_water_present(False)
    d.tick(at(2026, 7, 5, 6, 0, 0))
    d.tick(at(2026, 7, 5, 6, 0, 10))   # latch attivo prima del programma
    assert d.tank_empty["pompa_1"] is True

    d.tick(at(2026, 7, 5, 7, 0, 10))
    assert active(ctrl, "pompa_1") is False
    assert len(events(tmp_path, "saltato_serbatoio_vuoto")) == 1
    d.tick(at(2026, 7, 5, 7, 0, 30))   # non ritenta oggi
    assert len(events(tmp_path, "saltato_serbatoio_vuoto")) == 1


def test_manual_on_while_latched_runs_then_float_stops_it(tmp_path):
    d, ctrl = make_daemon(tmp_path, base_programs(FLOAT_CFG))
    d.float_bank.mock_input("pompa_1").set_water_present(False)
    d.tick(at(2026, 7, 5, 6, 0, 0))
    d.tick(at(2026, 7, 5, 6, 0, 10))
    assert d.tank_empty["pompa_1"] is True

    store.enqueue_command(tmp_path / "commands", {"type": "on", "pump": "pompa_1"})
    d.tick(at(2026, 7, 5, 6, 1, 0))
    assert active(ctrl, "pompa_1") is True  # filosofia esistente: procede con avviso
    assert any("galleggiante" in w["message"] for w in d.warnings)

    # ...ma il debounce riarmato lo ferma debounce_s dopo (protezione pompa)
    d.tick(at(2026, 7, 5, 6, 1, 5))
    assert active(ctrl, "pompa_1") is True
    d.tick(at(2026, 7, 5, 6, 1, 11))
    assert active(ctrl, "pompa_1") is False
    assert events(tmp_path, "run_off")[-1]["reason"] == "stop_galleggiante"


def test_refill_clears_latch_and_still_empty_relatches(tmp_path):
    d, ctrl = make_daemon(tmp_path, base_programs(FLOAT_CFG))
    d.float_bank.mock_input("pompa_1").set_water_present(False)
    d.tick(at(2026, 7, 5, 6, 0, 0))
    d.tick(at(2026, 7, 5, 6, 0, 10))
    assert d.tank_empty["pompa_1"] is True

    # riempimento vero: acqua torna, latch via, il programma delle 7 parte
    d.float_bank.mock_input("pompa_1").set_water_present(True)
    store.enqueue_command(tmp_path / "commands", {"type": "refill", "pump": "pompa_1"})
    d.tick(at(2026, 7, 5, 6, 30, 0))
    assert d.tank_empty["pompa_1"] is False
    d.tick(at(2026, 7, 5, 7, 0, 10))
    assert active(ctrl, "pompa_1") is True

    # refill "sbadato" (serbatoio ancora vuoto): ri-scatta dopo il debounce
    d2, _ = make_daemon(tmp_path, base_programs(FLOAT_CFG))
    d2.float_bank.mock_input("pompa_1").set_water_present(False)
    d2.tick(at(2026, 7, 5, 8, 0, 0))
    d2.tick(at(2026, 7, 5, 8, 0, 10))
    assert d2.tank_empty["pompa_1"] is True
    store.enqueue_command(tmp_path / "commands", {"type": "refill", "pump": "pompa_1"})
    d2.tick(at(2026, 7, 5, 8, 1, 0))
    assert d2.tank_empty["pompa_1"] is False
    d2.tick(at(2026, 7, 5, 8, 1, 5))
    d2.tick(at(2026, 7, 5, 8, 1, 11))
    assert d2.tank_empty["pompa_1"] is True


def test_latch_survives_daemon_restart(tmp_path):
    d1, _ = make_daemon(tmp_path, base_programs(FLOAT_CFG))
    d1.float_bank.mock_input("pompa_1").set_water_present(False)
    d1.tick(at(2026, 7, 5, 6, 0, 0))
    d1.tick(at(2026, 7, 5, 6, 0, 10))
    d1.tick(at(2026, 7, 5, 6, 0, 11))  # scrive lo stato col latch
    assert d1.tank_empty["pompa_1"] is True

    d2, ctrl2 = make_daemon(tmp_path, base_programs(FLOAT_CFG))
    assert d2.tank_empty.get("pompa_1") is True  # Restart=always non sblocca
    d2.tick(at(2026, 7, 5, 7, 0, 10))
    assert active(ctrl2, "pompa_1") is False
    assert events(tmp_path, "saltato_serbatoio_vuoto")


# --- Umidita --------------------------------------------------------------------

def test_moisture_skip_leaves_water_untouched(tmp_path):
    d, ctrl = make_daemon(tmp_path, base_programs(MOISTURE_CFG))
    d.moisture_bank.mock_adc.set_raw(0x48, 0, raw_for(90))
    d.tick(at(2026, 7, 5, 7, 0, 10))
    assert active(ctrl, "pompa_1") is False
    assert d.water["pompa_1"] == 25.0
    assert events(tmp_path, "saltato_umidita")
    dose = events(tmp_path, "decisione_dose")[-1]
    assert dose["multiplier"] == 0.0 and dose["moisture"][0]["percent"] >= 80


def test_moisture_reduces_duration_and_water_math(tmp_path):
    d, ctrl = make_daemon(tmp_path, base_programs(MOISTURE_CFG, pump1_duration=60))
    d.moisture_bank.mock_adc.set_raw(0x48, 0, raw_for(70))
    d.tick(at(2026, 7, 5, 7, 0, 0))
    assert active(ctrl, "pompa_1") is True
    start_event = events(tmp_path, "programma_on")[-1]
    assert start_event["seconds"] == 30 and start_event["base_s"] == 60

    d.tick(at(2026, 7, 5, 7, 0, 30))  # meta dose: 30 s a 600 L/h = 5 L
    assert active(ctrl, "pompa_1") is False
    assert abs(d.water["pompa_1"] - 20.0) < 0.01


def test_moisture_boost_clamped_by_max_run(tmp_path):
    programs = base_programs(MOISTURE_CFG, pump1_duration=480,
                             options={"max_run_seconds": 500})
    programs["pumps"]["pompa_1"]["flow_lph"] = 10  # portata reale: 500 s ~ 1.4 L
    d, ctrl = make_daemon(tmp_path, programs)
    d.moisture_bank.mock_adc.set_raw(0x48, 0, raw_for(10))
    d.tick(at(2026, 7, 5, 7, 0, 0))
    start_event = events(tmp_path, "programma_on")[-1]
    assert start_event["seconds"] == 500  # 480*1.25=600 -> clamp al tetto
    d.tick(at(2026, 7, 5, 7, 8, 20))      # 500 s dopo: completato, non max_run
    assert active(ctrl, "pompa_1") is False
    assert events(tmp_path, "run_off")[-1]["reason"] == "completato"
    assert not any("massima" in w["message"] for w in d.warnings)


def test_dead_sensor_gives_full_dose(tmp_path):
    d, ctrl = make_daemon(tmp_path, base_programs(MOISTURE_CFG))
    # nessun raw impostato nel mock: lettura None
    d.tick(at(2026, 7, 5, 7, 0, 0))
    assert active(ctrl, "pompa_1") is True
    dose = events(tmp_path, "decisione_dose")[-1]
    assert dose["effective_s"] == 60
    assert any("nessun sensore" in r for r in dose["reasons"])


def test_no_sensors_configured_no_decision_event(tmp_path):
    d, ctrl = make_daemon(tmp_path, base_programs(None))
    d.tick(at(2026, 7, 5, 7, 0, 0))
    assert active(ctrl, "pompa_1") is True
    assert events(tmp_path, "decisione_dose") == []


# --- Meteo ------------------------------------------------------------------------

WEATHER_TOP = {"weather": {"enabled": True, "latitude": 45.46, "longitude": 9.19,
                           "rain_skip_mm": 5.0, "et0_low": 2.0, "et0_high": 5.0,
                           "fetch_hour": "05:30", "max_age_hours": 30}}


def write_weather(tmp_path, now, rain_mm=0.0, et0=3.0, prob=10.0):
    store.write_json_atomic(tmp_path / "weather.json", {
        "fetched_at": now.isoformat(), "date": now.date().isoformat(),
        "et0_mm": et0, "rain_mm": rain_mm, "rain_prob": prob, "tmax_c": 30.0,
    })


def test_rain_forecast_skips_exposed_zone(tmp_path):
    programs = base_programs({"rain_exposed": True, **MOISTURE_CFG}, **WEATHER_TOP)
    d, ctrl = make_daemon(tmp_path, programs)
    d.moisture_bank.mock_adc.set_raw(0x48, 0, raw_for(50))
    write_weather(tmp_path, at(2026, 7, 5, 5, 31), rain_mm=9.0)
    d.tick(at(2026, 7, 5, 7, 0, 10))
    assert active(ctrl, "pompa_1") is False
    assert events(tmp_path, "saltato_meteo")


def test_stale_weather_is_neutral(tmp_path):
    programs = base_programs({"rain_exposed": True}, **WEATHER_TOP)
    d, ctrl = make_daemon(tmp_path, programs)
    write_weather(tmp_path, at(2026, 7, 3, 5, 31), rain_mm=9.0)  # 2 giorni fa
    d.tick(at(2026, 7, 5, 7, 0, 10))
    assert active(ctrl, "pompa_1") is True  # cache stantia -> neutro, si irriga
    dose = events(tmp_path, "decisione_dose")[-1]
    assert any("non disponibile" in r for r in dose["reasons"])


def test_weather_refresh_spawned_once_with_backoff(tmp_path):
    calls: list[datetime] = []
    programs = base_programs(None, **WEATHER_TOP)
    d, _ = make_daemon(tmp_path, programs,
                       weather_refresh=lambda cfg, path, now, tz: calls.append(now))
    d.tick(at(2026, 7, 5, 5, 29, 0))
    assert calls == []                       # prima dell'ora di fetch
    d.tick(at(2026, 7, 5, 5, 30, 30))
    assert len(calls) == 1                   # scatta
    d.tick(at(2026, 7, 5, 5, 45, 0))
    assert len(calls) == 1                   # backoff 30 min (fetch fallito)
    d.tick(at(2026, 7, 5, 6, 5, 0))
    assert len(calls) == 2                   # ritenta
    write_weather(tmp_path, at(2026, 7, 5, 6, 6, 0))
    d.tick(at(2026, 7, 5, 6, 40, 0))
    assert len(calls) == 2                   # cache di oggi presente: basta


# --- Batteria ------------------------------------------------------------------------

POWER_TOP = {"power": {"enabled": True, "v_low": 12.6, "v_critical": 12.3,
                       "hold_minutes": 0, "stale_seconds": 120}}


def status(v, when):
    return PowerStatus(battery_v=v, battery_a=0.0, panel_v=None, panel_w=20.0,
                       charge_state="bulk", at=when)


def test_critical_battery_skips_and_notifies(tmp_path):
    monitor = power.MockPowerMonitor()
    rec = RecordingNotifier()
    programs = base_programs(
        None, **POWER_TOP,
        notify={"enabled": True, "events": ["saltato_batteria", "batteria_bassa"]},
    )
    d, ctrl = make_daemon(tmp_path, programs, power_monitor=monitor, notifier=rec)
    monitor.set_status(status(12.0, at(2026, 7, 5, 6, 59, 50)))
    d.tick(at(2026, 7, 5, 6, 59, 55))        # prima del programma: stato adottato
    assert d.battery_state == "critica"
    assert any("batteria" in t.lower() for t, _, _ in rec.messages)

    monitor.set_status(status(12.0, at(2026, 7, 5, 7, 0, 5)))
    d.tick(at(2026, 7, 5, 7, 0, 10))
    assert active(ctrl, "pompa_1") is False
    assert events(tmp_path, "saltato_batteria")
    assert any("saltata" in t.lower() for t, _, _ in rec.messages)


def test_low_battery_halves_dose(tmp_path):
    monitor = power.MockPowerMonitor()
    d, ctrl = make_daemon(tmp_path, base_programs(None, **POWER_TOP),
                          power_monitor=monitor)
    monitor.set_status(status(12.5, at(2026, 7, 5, 6, 59, 50)))
    d.tick(at(2026, 7, 5, 6, 59, 55))
    assert d.battery_state == "bassa"
    monitor.set_status(status(12.5, at(2026, 7, 5, 7, 0, 5)))
    d.tick(at(2026, 7, 5, 7, 0, 10))
    assert active(ctrl, "pompa_1") is True
    assert events(tmp_path, "programma_on")[-1]["seconds"] == 30  # 60 x 0.5


def test_stale_battery_data_is_neutral(tmp_path):
    monitor = power.MockPowerMonitor()
    d, ctrl = make_daemon(tmp_path, base_programs(None, **POWER_TOP),
                          power_monitor=monitor)
    monitor.set_status(status(12.0, at(2026, 7, 5, 5, 0, 0)))  # vecchio di 2 ore
    d.tick(at(2026, 7, 5, 6, 59, 55))
    assert d.battery_state == "sconosciuto"
    d.tick(at(2026, 7, 5, 7, 0, 10))
    assert active(ctrl, "pompa_1") is True   # cavo staccato non ferma l'irrigazione
    assert events(tmp_path, "programma_on")[-1]["seconds"] == 60


# --- Lettura live, trend, config ---------------------------------------------------------

def test_sensor_live_command_populates_state_and_expires(tmp_path):
    d, _ = make_daemon(tmp_path, base_programs(MOISTURE_CFG))
    d.moisture_bank.mock_adc.set_raw(0x48, 0, raw_for(40))
    store.enqueue_command(tmp_path / "commands", {"type": "sensor_live", "seconds": 30})
    d.tick(at(2026, 7, 5, 10, 0, 0))
    state = read_state(tmp_path)
    assert state["live_sampling_until"] is not None
    assert state["pumps"]["pompa_1"]["moisture"][0]["raw"] == raw_for(40)

    d.tick(at(2026, 7, 5, 10, 0, 31))
    assert read_state(tmp_path)["live_sampling_until"] is None


def test_trend_sampling_every_15_minutes_only_idle(tmp_path):
    d, _ = make_daemon(tmp_path, base_programs(MOISTURE_CFG, pump1_duration=120))
    d.moisture_bank.mock_adc.set_raw(0x48, 0, raw_for(50))
    d.tick(at(2026, 7, 5, 6, 0, 0))
    assert len(store.read_events(tmp_path / "sensors.jsonl")) == 1
    d.tick(at(2026, 7, 5, 6, 10, 0))
    assert len(store.read_events(tmp_path / "sensors.jsonl")) == 1  # < 15 min
    d.tick(at(2026, 7, 5, 6, 16, 0))
    assert len(store.read_events(tmp_path / "sensors.jsonl")) == 2

    d.tick(at(2026, 7, 5, 7, 0, 0))          # parte il programma (120 s)
    d.tick(at(2026, 7, 5, 7, 0, 40))         # pompa accesa: niente campioni
    assert len(store.read_events(tmp_path / "sensors.jsonl")) == 2


def test_config_warning_logged_once_and_cleared_on_fix(tmp_path):
    bad = base_programs({"float": {"gpio": 17}})  # collide col rele di pompa_1
    d, _ = make_daemon(tmp_path, bad)
    d.tick(at(2026, 7, 5, 9, 0, 0))
    assert len(events(tmp_path, "config_avviso")) == 1
    assert read_state(tmp_path)["config_warnings"]

    store.enqueue_command(tmp_path / "commands", {"type": "reload"})
    d.tick(at(2026, 7, 5, 9, 0, 5))
    assert len(events(tmp_path, "config_avviso")) == 1  # niente spam a parita di avvisi

    store.write_json_atomic(tmp_path / "programs.json", base_programs(FLOAT_CFG))
    store.enqueue_command(tmp_path / "commands", {"type": "reload"})
    d.tick(at(2026, 7, 5, 9, 0, 10))
    assert read_state(tmp_path)["config_warnings"] == []


# --- Notifiche e heartbeat -----------------------------------------------------------------

def test_event_notification_respects_allowlist(tmp_path):
    rec = RecordingNotifier()
    programs = base_programs(
        FLOAT_CFG, notify={"enabled": True, "events": ["serbatoio_vuoto"]},
    )
    d, _ = make_daemon(tmp_path, programs, notifier=rec)
    d.tick(at(2026, 7, 5, 7, 0, 0))          # programma_on: NON in allowlist
    assert rec.messages == []
    d.float_bank.mock_input("pompa_1").set_water_present(False)
    d.tick(at(2026, 7, 5, 7, 0, 20))
    d.tick(at(2026, 7, 5, 7, 0, 30))          # scatta il galleggiante
    assert len(rec.messages) == 1
    assert "VUOTO" in rec.messages[0][0]


def test_heartbeat_once_per_day_and_survives_restart(tmp_path):
    rec = RecordingNotifier()
    programs = base_programs(
        None, notify={"enabled": True, "events": [], "heartbeat_time": "20:30"},
    )
    d, _ = make_daemon(tmp_path, programs, notifier=rec)
    d.tick(at(2026, 7, 5, 20, 29, 0))
    assert rec.messages == []
    d.tick(at(2026, 7, 5, 20, 30, 5))
    assert len(rec.messages) == 1
    assert "ok" in rec.messages[0][0]
    d.tick(at(2026, 7, 5, 21, 0, 0))
    assert len(rec.messages) == 1             # una volta al giorno

    # riavvio nello stesso giorno: last_heartbeat_date ripristinato da state.json
    rec2 = RecordingNotifier()
    d2, _ = make_daemon(tmp_path, programs, notifier=rec2)
    d2.tick(at(2026, 7, 5, 21, 30, 0))
    assert rec2.messages == []
    d2.tick(at(2026, 7, 6, 20, 30, 5))        # il giorno dopo si
    assert len(rec2.messages) == 1
