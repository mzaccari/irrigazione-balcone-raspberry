"""Test di sensors.py: normalizzazione, debounce galleggiante, banchi mock."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import sensors
from sensors import FloatDebounce, FloatSwitchBank, MoistureSensorBank, moisture_percent

TZ = ZoneInfo("Europe/Rome")


def at(h=0, mi=0, s=0):
    return datetime(2026, 7, 5, h, mi, s, tzinfo=TZ)


# --- moisture_percent ---------------------------------------------------------

def test_moisture_percent_capacitive_polarity():
    # capacitivo: secco = raw alto, bagnato = raw basso
    assert moisture_percent(26000, raw_dry=26000, raw_wet=10500) == 0.0
    assert moisture_percent(10500, raw_dry=26000, raw_wet=10500) == 100.0
    mid = moisture_percent(18250, raw_dry=26000, raw_wet=10500)
    assert abs(mid - 50.0) < 0.1


def test_moisture_percent_inverse_polarity():
    assert moisture_percent(0, raw_dry=0, raw_wet=1000) == 0.0
    assert moisture_percent(1000, raw_dry=0, raw_wet=1000) == 100.0


def test_moisture_percent_clamps_inside_plausibility_band():
    # poco oltre il punto bagnato: plausibile, clampato a 100
    assert moisture_percent(10000, raw_dry=26000, raw_wet=10500) == 100.0
    # poco oltre il punto secco: clampato a 0
    assert moisture_percent(26500, raw_dry=26000, raw_wet=10500) == 0.0


def test_moisture_percent_implausible_or_missing_is_none():
    assert moisture_percent(None, 26000, 10500) is None
    assert moisture_percent(1000, 26000, 10500) is None    # molto sotto la banda
    assert moisture_percent(32000, 26000, 10500) is None   # molto sopra la banda
    assert moisture_percent(5000, 0, 0) is None            # non calibrato (0/0)


# --- FloatDebounce --------------------------------------------------------------

def test_float_debounce_ignores_sloshing():
    fd = FloatDebounce(10)
    assert fd.update(False, at(7, 0, 0)) is False
    assert fd.update(False, at(7, 0, 3)) is False
    assert fd.update(True, at(7, 0, 4)) is False   # acqua tornata: riarmo
    assert fd.update(False, at(7, 0, 5)) is False
    assert fd.update(False, at(7, 0, 14)) is False  # solo 9 s consecutivi
    assert fd.update(False, at(7, 0, 15)) is True   # 10 s consecutivi


def test_float_debounce_trips_once():
    fd = FloatDebounce(10)
    fd.update(False, at(8, 0, 0))
    assert fd.update(False, at(8, 0, 10)) is True
    assert fd.update(False, at(8, 0, 20)) is False  # gia scattato
    fd.update(True, at(8, 0, 30))                   # acqua tornata
    fd.update(False, at(8, 0, 40))
    assert fd.update(False, at(8, 0, 50)) is True   # nuovo ciclo, nuovo scatto


def test_float_debounce_none_counts_as_empty():
    fd = FloatDebounce(10)
    assert fd.update(None, at(9, 0, 0)) is False
    assert fd.update(None, at(9, 0, 10)) is True


# --- resolve_sensor_mock ---------------------------------------------------------

def test_resolve_sensor_mock_precedence(monkeypatch):
    monkeypatch.setenv("SENSOR_MOCK", "0")
    assert sensors.resolve_sensor_mock(True) is True      # esplicito vince
    assert sensors.resolve_sensor_mock(None) is False     # poi la env
    monkeypatch.setenv("SENSOR_MOCK", "1")
    assert sensors.resolve_sensor_mock(None) is True
    monkeypatch.delenv("SENSOR_MOCK")
    import platform
    expected = platform.system() != "Linux"                # infine la piattaforma
    assert sensors.resolve_sensor_mock(None) is expected


# --- FloatSwitchBank (mock) ------------------------------------------------------

def test_float_bank_reads_and_reconfigures():
    bank = FloatSwitchBank({"pompa_1": 23, "pompa_2": 24}, mock=True)
    assert bank.water_present("pompa_1") is True   # default: acqua presente
    bank.mock_input("pompa_1").set_water_present(False)
    assert bank.water_present("pompa_1") is False
    assert bank.water_present("pompa_3") is None   # non configurata

    kept = bank.mock_input("pompa_2")
    bank.reconfigure({"pompa_2": 24, "pompa_3": 25})  # p1 via, p2 intatta, p3 nuova
    assert bank.water_present("pompa_1") is None
    assert bank.mock_input("pompa_2") is kept
    assert bank.water_present("pompa_3") is True


def test_float_bank_read_error_is_none():
    bank = FloatSwitchBank({"pompa_1": 23}, mock=True)
    bank.mock_input("pompa_1").close()  # lettura su device chiuso solleva nel mock
    assert bank.water_present("pompa_1") is None


# --- MoistureSensorBank (mock) -----------------------------------------------------

def test_moisture_bank_median_and_failure():
    bank = MoistureSensorBank(mock=True)
    bank.mock_adc.set_raw(0x48, 0, 15200)
    assert bank.read_oversampled(0x48, 0, samples=8) == 15200
    assert bank.read_oversampled(0x48, 1) is None  # canale mai impostato
    bank.mock_adc.fail = True
    assert bank.read_oversampled(0x48, 0) is None  # bus guasto -> None


def test_ads1115_rejects_bad_channel():
    adc = sensors.Ads1115()
    with pytest.raises(ValueError):
        adc.read_single(0x48, 4)
