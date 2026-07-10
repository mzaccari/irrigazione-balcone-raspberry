"""Test di weather.py: nessuna rete, tutto su fixture e file temporanei."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import store
import weather

TZ = ZoneInfo("Europe/Rome")


def at(y=2026, mo=7, d=21, h=6, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ)


PAYLOAD = {
    "daily": {
        "time": ["2026-07-21", "2026-07-22"],
        "et0_fao_evapotranspiration": [4.8, 5.1],
        "precipitation_sum": [0.0, 12.5],
        "precipitation_probability_max": [10, 85],
        "temperature_2m_max": [31.2, 24.0],
    }
}


def test_build_url_contains_coordinates_and_variables():
    url = weather.build_url(45.4642, 9.19, "Europe/Rome")
    assert "latitude=45.4642" in url
    assert "et0_fao_evapotranspiration" in url
    assert "precipitation_probability_max" in url
    assert "timezone=Europe%2FRome" in url


def test_parse_daily_picks_today():
    cache = weather.parse_daily(PAYLOAD, at(d=21))
    assert cache["date"] == "2026-07-21"
    assert cache["et0_mm"] == 4.8 and cache["rain_mm"] == 0.0

    # domani: prende la riga giusta
    cache = weather.parse_daily(PAYLOAD, at(d=22))
    assert cache["rain_mm"] == 12.5 and cache["rain_prob"] == 85.0


def test_parse_daily_tolerates_missing_fields():
    cache = weather.parse_daily({"daily": {"time": ["2026-07-21"]}}, at())
    assert cache["et0_mm"] is None and cache["rain_mm"] is None


def test_refresh_due_once_per_day_after_fetch_hour():
    assert weather.refresh_due(None, at(h=5, mi=29), "05:30") is False
    assert weather.refresh_due(None, at(h=5, mi=30), "05:30") is True
    today_cache = {"date": "2026-07-21"}
    assert weather.refresh_due(today_cache, at(h=6), "05:30") is False
    yesterday_cache = {"date": "2026-07-20"}
    assert weather.refresh_due(yesterday_cache, at(h=6), "05:30") is True
    # fetch_hour rotta -> default 05:30
    assert weather.refresh_due(None, at(h=4), "boh") is False
    assert weather.refresh_due(None, at(h=6), "boh") is True


def test_load_cached_happy_path(tmp_path):
    path = tmp_path / "weather.json"
    store.write_json_atomic(path, weather.parse_daily(PAYLOAD, at(h=5, mi=31)))
    info = weather.load_cached(path, at(h=6), max_age_hours=30)
    assert info is not None
    assert info.et0_mm == 4.8 and info.date == "2026-07-21"


def test_load_cached_missing_corrupt_stale(tmp_path):
    path = tmp_path / "weather.json"
    assert weather.load_cached(path, at()) is None            # assente

    path.write_text("{rotto", encoding="utf-8")
    assert weather.load_cached(path, at()) is None            # corrotta

    store.write_json_atomic(path, weather.parse_daily(PAYLOAD, at(d=19, h=5)))
    assert weather.load_cached(path, at(d=21, h=6), max_age_hours=30) is None  # stantia

    # fetched_at nel futuro (orologio impazzito) -> None
    store.write_json_atomic(path, weather.parse_daily(PAYLOAD, at(d=22, h=5)))
    assert weather.load_cached(path, at(d=21, h=6)) is None


def test_refresh_cache_writes_atomically_and_never_raises(tmp_path):
    path = tmp_path / "weather.json"
    cfg = {"latitude": 45.46, "longitude": 9.19}

    ok = weather.refresh_cache(cfg, path, at(h=5, mi=31), "Europe/Rome",
                               fetcher=lambda url: PAYLOAD)
    assert ok is True
    assert store.read_json(path)["et0_mm"] == 4.8

    def boom(url):
        raise OSError("rete giu")

    assert weather.refresh_cache(cfg, path, at(h=6), "Europe/Rome", fetcher=boom) is False
    assert store.read_json(path)["et0_mm"] == 4.8  # la cache buona resta

    # config senza coordinate -> False, niente crash
    assert weather.refresh_cache({}, path, at(), "Europe/Rome",
                                 fetcher=lambda url: PAYLOAD) is False
