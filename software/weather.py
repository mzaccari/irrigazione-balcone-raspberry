"""Meteo giornaliero da Open-Meteo (gratuito, senza chiave) con cache su file.

Separazione rigida per proteggere il tick del demone:
- le funzioni PURE (build_url, parse_daily, refresh_due, load_cached) non fanno
  rete e sono testabili con fixture;
- `fetch` usa urllib (stdlib, nessuna dipendenza nuova) e viene chiamata SOLO
  da `refresh_cache`, che il demone esegue in un thread usa-e-getta: l'unico
  effetto e' la scrittura atomica di runtime/weather.json (un solo scrittore).
- il tick legge esclusivamente la cache; cache assente/corrotta/stantia ->
  None -> il motore decisionale resta neutro.

Il dato chiave e' l'ET0 FAO-56 (evapotraspirazione di riferimento): e' il
numero "scientifico" con cui modulare la dose, senza taratura locale.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

import store


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
DAILY_VARS = (
    "et0_fao_evapotranspiration,precipitation_sum,"
    "precipitation_probability_max,temperature_2m_max"
)
DEFAULT_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class WeatherInfo:
    et0_mm: float | None
    rain_mm: float | None
    rain_prob: float | None
    tmax_c: float | None
    fetched_at: str  # ISO, riportato nell'evento decisione_dose
    date: str        # data locale a cui si riferiscono i valori


# --- Pure ---------------------------------------------------------------------

def build_url(latitude: float, longitude: float, timezone: str) -> str:
    params = {
        "latitude": f"{float(latitude):.4f}",
        "longitude": f"{float(longitude):.4f}",
        "daily": DAILY_VARS,
        "timezone": timezone,
        "forecast_days": "2",
    }
    return OPEN_METEO_URL + "?" + urllib.parse.urlencode(params)


def parse_daily(payload: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """Estrae i valori di OGGI dal payload Open-Meteo -> dict per la cache."""
    daily = payload.get("daily", {})
    daily = daily if isinstance(daily, Mapping) else {}
    times = daily.get("time", [])
    times = times if isinstance(times, list) else []
    today = now.date().isoformat()
    idx = times.index(today) if today in times else 0

    def pick(key: str) -> float | None:
        values = daily.get(key, [])
        if not isinstance(values, list) or idx >= len(values):
            return None
        value = values[idx]
        return float(value) if isinstance(value, (int, float)) else None

    return {
        "fetched_at": now.isoformat(),
        "date": today,
        "et0_mm": pick("et0_fao_evapotranspiration"),
        "rain_mm": pick("precipitation_sum"),
        "rain_prob": pick("precipitation_probability_max"),
        "tmax_c": pick("temperature_2m_max"),
    }


def refresh_due(cache: Mapping[str, Any] | None, now: datetime, fetch_hour: str) -> bool:
    """True se manca la cache di OGGI e l'ora locale ha superato fetch_hour.

    Chiave per DATA locale, come last_fired: robusto al cambio ora legale.
    """
    try:
        hh_str, mm_str = str(fetch_hour).split(":")
        hh, mm = int(hh_str), int(mm_str)
    except (ValueError, AttributeError):
        hh, mm = 5, 30
    if (now.hour, now.minute) < (hh, mm):
        return False
    if isinstance(cache, Mapping) and cache.get("date") == now.date().isoformat():
        return False
    return True


def load_cached(
    cache_path: str | Path, now: datetime, max_age_hours: float = 30.0
) -> WeatherInfo | None:
    """Cache -> WeatherInfo, oppure None se assente/corrotta/stantia."""
    data = store.read_json_or(cache_path, None)
    if not isinstance(data, dict):
        return None
    try:
        fetched = datetime.fromisoformat(str(data.get("fetched_at")))
    except (ValueError, TypeError):
        return None
    if fetched.tzinfo is None:
        return None  # scritta sempre aware; naive = file manipolato
    age_hours = (now - fetched).total_seconds() / 3600.0
    if not 0 <= age_hours <= float(max_age_hours):
        return None

    def as_float(key: str) -> float | None:
        value = data.get(key)
        return float(value) if isinstance(value, (int, float)) else None

    return WeatherInfo(
        et0_mm=as_float("et0_mm"),
        rain_mm=as_float("rain_mm"),
        rain_prob=as_float("rain_prob"),
        tmax_c=as_float("tmax_c"),
        fetched_at=str(data.get("fetched_at")),
        date=str(data.get("date", "")),
    )


# --- Rete (mai nel tick) --------------------------------------------------------

def fetch(url: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """GET del payload JSON; propaga ogni errore al chiamante."""
    request = urllib.request.Request(url, headers={"User-Agent": "irrigatore-balcone"})
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("risposta Open-Meteo non valida")
    return payload


def refresh_cache(
    weather_cfg: Mapping[str, Any],
    cache_path: str | Path,
    now: datetime,
    tz_name: str,
    fetcher: Callable[[str], dict[str, Any]] = fetch,
) -> bool:
    """Scarica e scrive la cache (atomica). Non solleva MAI: False su errore."""
    try:
        url = build_url(
            float(weather_cfg["latitude"]),
            float(weather_cfg["longitude"]),
            tz_name,
        )
        cache = parse_daily(fetcher(url), now)
        store.write_json_atomic(cache_path, cache)
        return True
    except Exception:
        return False
