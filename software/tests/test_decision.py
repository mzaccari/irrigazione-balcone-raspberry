"""Test del motore decisionale puro e del parsing/validazione config sensori."""

from __future__ import annotations

from datetime import date

import pytest

import decision
from decision import (
    MoistureIndex,
    PumpSensorsCfg,
    compute_dose_multiplier,
    estimated_daily_liters,
    parse_adc_addr,
    parse_sensor_config,
    validate_config,
)
from weather import WeatherInfo


PUMP_IDS = {"pompa_1", "pompa_2", "pompa_3"}
PUMP_GPIOS = {17, 27, 22}

THRESHOLDS = {"skip_above": 80, "reduce_above": 65, "boost_below": 25}


def m(percent, sensor_id="s1", raw=1000):
    return MoistureIndex(sensor_id=sensor_id, raw=raw, percent=percent)


def cfg(thresholds=THRESHOLDS, rain_exposed=False, with_moisture=True):
    moisture = (
        (decision.MoistureSensorCfg("s1", 0x48, 0, 26000, 10500),) if with_moisture else ()
    )
    return PumpSensorsCfg(moisture=moisture, thresholds=thresholds, rain_exposed=rain_exposed)


def winfo(et0=3.0, rain=0.0, prob=0.0):
    return WeatherInfo(
        et0_mm=et0, rain_mm=rain, rain_prob=prob, tmax_c=30.0,
        fetched_at="2026-07-21T05:30:00+02:00", date="2026-07-21",
    )


WEATHER_ON = {"enabled": True, "rain_skip_mm": 5.0, "rain_prob_min": 80,
              "et0_low": 2.0, "et0_high": 5.0}


# --- Umidita ---------------------------------------------------------------------

def test_moisture_tiers():
    assert compute_dose_multiplier([m(85)], None, None, cfg(), None).multiplier == 0.0
    assert compute_dose_multiplier([m(80)], None, None, cfg(), None).multiplier == 0.0   # bordo
    assert compute_dose_multiplier([m(70)], None, None, cfg(), None).multiplier == 0.5
    assert compute_dose_multiplier([m(50)], None, None, cfg(), None).multiplier == 1.0
    assert compute_dose_multiplier([m(25)], None, None, cfg(), None).multiplier == 1.25  # bordo
    assert compute_dose_multiplier([m(10)], None, None, cfg(), None).multiplier == 1.25


def test_moisture_min_aggregation_protects_driest():
    d = compute_dose_multiplier([m(80, "s1"), m(20, "s2")], None, None, cfg(), None)
    assert d.multiplier == 1.25  # comanda il piu secco, non la media


def test_dead_sensor_never_starves_plants():
    # un sensore rotto (None) + uno secco: si irriga col boost
    d = compute_dose_multiplier([m(None, "s1"), m(20, "s2")], None, None, cfg(), None)
    assert d.multiplier == 1.25
    # tutti rotti: neutro con motivo, MAI salto
    d = compute_dose_multiplier([m(None), m(None)], None, None, cfg(), None)
    assert d.multiplier == 1.0
    assert any("nessun sensore" in r for r in d.reasons)


def test_moisture_observation_mode_without_thresholds():
    d = compute_dose_multiplier([m(90)], None, None, cfg(thresholds=None), None)
    assert d.multiplier == 1.0
    assert any("osservazione" in r for r in d.reasons)


def test_no_sensors_configured_is_silently_neutral():
    d = compute_dose_multiplier([], None, None, None, None)
    assert d.multiplier == 1.0
    assert d.reasons == ()


# --- Meteo -----------------------------------------------------------------------

def test_weather_disabled_or_missing():
    assert compute_dose_multiplier([], winfo(et0=9), None, None, None).multiplier == 1.0
    d = compute_dose_multiplier([], None, None, None, WEATHER_ON)
    assert d.multiplier == 1.0
    assert any("meteo non disponibile" in r for r in d.reasons)


def test_rain_skips_only_exposed_zones():
    rainy = winfo(rain=8.0)
    covered = compute_dose_multiplier([], rainy, None, cfg(rain_exposed=False), WEATHER_ON)
    assert covered.multiplier == 1.0
    exposed = compute_dose_multiplier([], rainy, None, cfg(rain_exposed=True), WEATHER_ON)
    assert exposed.multiplier == 0.0
    assert exposed.skip_event() == "saltato_meteo"


def test_rain_probability_alone_can_skip():
    likely = winfo(rain=2.0, prob=90.0)
    d = compute_dose_multiplier([], likely, None, cfg(rain_exposed=True), WEATHER_ON)
    assert d.multiplier == 0.0


def test_et0_tiers():
    assert compute_dose_multiplier([], winfo(et0=1.0), None, None, WEATHER_ON).multiplier == 0.8
    assert compute_dose_multiplier([], winfo(et0=3.0), None, None, WEATHER_ON).multiplier == 1.0
    assert compute_dose_multiplier([], winfo(et0=6.0), None, None, WEATHER_ON).multiplier == 1.2


# --- Batteria ----------------------------------------------------------------------

def test_battery_states():
    assert compute_dose_multiplier([], None, "ok", None, None).multiplier == 1.0
    assert compute_dose_multiplier([], None, "bassa", None, None).multiplier == 0.5
    d = compute_dose_multiplier([], None, "critica", None, None)
    assert d.multiplier == 0.0
    assert d.skip_event() == "saltato_batteria"
    unknown = compute_dose_multiplier([], None, "sconosciuto", None, None)
    assert unknown.multiplier == 1.0
    assert any("sconosciuto" in r for r in unknown.reasons)


def test_battery_saving_beats_drought_boost():
    d = compute_dose_multiplier([m(10)], None, "bassa", cfg(), None)
    assert d.multiplier == 1.25 * 0.5  # 0.625: il boost non scavalca il risparmio


# --- Composizione ---------------------------------------------------------------------

def test_compose_and_clamp():
    d = compute_dose_multiplier([m(10)], winfo(et0=6.0), None, cfg(), WEATHER_ON)
    assert d.multiplier == 1.5  # 1.25 * 1.2 = 1.5 (al clamp)
    assert d.moisture_mult == 1.25 and d.weather_mult == 1.2


def test_skip_event_priority_moisture_first():
    d = compute_dose_multiplier(
        [m(90)], winfo(rain=9.0), "critica", cfg(rain_exposed=True), WEATHER_ON
    )
    assert d.multiplier == 0.0
    assert d.skip_event() == "saltato_umidita"


def test_every_non_neutral_branch_has_a_reason():
    d = compute_dose_multiplier([m(70)], winfo(et0=6.0), "bassa", cfg(), WEATHER_ON)
    assert len(d.reasons) == 3


# --- estimated_daily_liters -------------------------------------------------------------

def test_estimated_daily_liters_real_schedule():
    entry = {"flow_lph": 10, "programs": [
        {"id": "estate", "start_date": "2026-07-07", "duration_s": 480,
         "times": ["06:30", "20:00"]},
    ]}
    liters = estimated_daily_liters(entry, date(2026, 7, 10))
    assert abs(liters - 2.667) < 0.01  # 480 s x 2 a 10 L/h


def test_estimated_daily_liters_respects_dates_and_enabled():
    entry = {"flow_lph": 10, "programs": [
        {"id": "p", "start_date": "2026-08-01", "duration_s": 480, "times": ["06:30"]},
        {"id": "q", "enabled": False, "start_date": "2026-07-01", "duration_s": 480,
         "times": ["06:30"]},
        {"rotto": True},
    ]}
    assert estimated_daily_liters(entry, date(2026, 7, 10)) == 0.0


# --- Parsing config ----------------------------------------------------------------------

def test_parse_adc_addr_forms():
    assert parse_adc_addr("0x48") == 0x48
    assert parse_adc_addr(72) == 72
    assert parse_adc_addr("72") == 72
    with pytest.raises(ValueError):
        parse_adc_addr("boh")
    with pytest.raises(ValueError):
        parse_adc_addr(None)


def test_parse_sensor_config_full_and_defaults():
    programs = {"pumps": {"pompa_1": {"sensors": {
        "rain_exposed": True,
        "float": {"gpio": 23, "debounce_s": 5, "reserve_liters": 2.0},
        "moisture": [
            {"id": "p1_m1", "adc": {"addr": "0x48", "channel": 0},
             "raw_dry": 26000, "raw_wet": 10500},
            {"rotto": True},
        ],
        "thresholds": {"skip_above": 80, "reduce_above": 65, "boost_below": 25},
    }}, "pompa_2": {}}}
    parsed = parse_sensor_config(programs)
    p1 = parsed["pompa_1"]
    assert p1.float_gpio == 23 and p1.float_debounce_s == 5 and p1.reserve_liters == 2.0
    assert p1.rain_exposed is True
    assert len(p1.moisture) == 1  # la voce rotta e ignorata
    assert p1.moisture[0].adc_addr == 0x48 and p1.moisture[0].raw_wet == 10500
    assert p1.any_configured()
    assert "pompa_2" not in parsed  # nessuna sezione sensors


# --- Validazione rumorosa ------------------------------------------------------------------

def _programs_with(sensors_p1):
    return {"pumps": {"pompa_1": {"sensors": sensors_p1}}}


def test_validate_gpio_collisions():
    warnings = validate_config(
        _programs_with({"float": {"gpio": 17}}), PUMP_IDS, PUMP_GPIOS
    )
    assert any("conflitto con un rele" in w for w in warnings)
    warnings = validate_config(
        _programs_with({"float": {"gpio": 2}}), PUMP_IDS, PUMP_GPIOS
    )
    assert any("I2C" in w for w in warnings)


def test_validate_duplicate_float_gpio():
    programs = {"pumps": {
        "pompa_1": {"sensors": {"float": {"gpio": 23}}},
        "pompa_2": {"sensors": {"float": {"gpio": 23}}},
    }}
    warnings = validate_config(programs, PUMP_IDS, PUMP_GPIOS)
    assert any("gia usato" in w for w in warnings)


def test_validate_inverted_thresholds_and_bad_channel():
    warnings = validate_config(_programs_with({
        "moisture": [{"adc": {"addr": "0x48", "channel": 7}}],
        "thresholds": {"skip_above": 60, "reduce_above": 65, "boost_below": 70},
    }), PUMP_IDS, PUMP_GPIOS)
    assert any("fuori range" in w for w in warnings)
    assert any("reduce_above" in w for w in warnings)
    assert any("boost_below" in w for w in warnings)


def test_validate_weather_power_notify():
    programs = {
        "weather": {"enabled": True, "fetch_hour": "boh"},
        "notify": {"enabled": True, "backend": "piccione"},
        "power": {"enabled": True, "v_low": 12.0, "v_critical": 12.5},
        "pumps": {},
    }
    warnings = validate_config(programs, PUMP_IDS, PUMP_GPIOS)
    assert any("latitude" in w for w in warnings)
    assert any("fetch_hour" in w for w in warnings)
    assert any("backend" in w for w in warnings)
    assert any("v_critical" in w for w in warnings)


def test_validate_unknown_pump_and_clean_config():
    programs = {"pumps": {"pompa_9": {"sensors": {"float": {"gpio": 23}}}}}
    warnings = validate_config(programs, PUMP_IDS, PUMP_GPIOS)
    assert any("sconosciuta" in w for w in warnings)

    clean = {"pumps": {"pompa_1": {"sensors": {
        "float": {"gpio": 23},
        "moisture": [{"adc": {"addr": "0x48", "channel": 0}}],
        "thresholds": {"skip_above": 80, "reduce_above": 65, "boost_below": 25},
    }}}}
    assert validate_config(clean, PUMP_IDS, PUMP_GPIOS) == []
