"""Motore decisionale PURO della dose di irrigazione (nessun I/O, nessun hardware).

Come scheduler.py: qui non si legge nessun sensore e non si accende nulla; si
calcola soltanto il MOLTIPLICATORE della durata programmata a partire da input
gia' raccolti dal demone. Regole (vedi docs/upgrade-sensori.md):

- i sensori MODULANO lo schedule, non lo sostituiscono;
- aggregazione umidita' di zona = MIN dei sensori validi (proteggi la pianta
  piu' a rischio, il "vaso indicatore");
- rischio asimmetrico -> ogni input mancante/guasto/stantio vale 1.0 (neutro):
  un sensore morto non deve mai far morire le piante;
- composizione moltiplicativa umidita' x meteo x batteria, clamp [0, 1.5];
- ogni ramo non-neutro produce un motivo leggibile (evento `decisione_dose`).

Qui vive anche il parsing/validazione della nuova sezione `sensors`/`weather`/
`notify`/`power` di programs.json: parsing DIFENSIVO (righe rotte ignorate) ma
validazione RUMOROSA (lista di avvisi da mostrare in UI e nel log eventi).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

import scheduler


CLAMP_MAX = 1.5
MOISTURE_REDUCE_MULT = 0.5
MOISTURE_BOOST_MULT = 1.25
ET0_COOL_MULT = 0.8
ET0_HOT_MULT = 1.2
BATTERY_LOW_MULT = 0.5

# GPIO gia' impegnati dal sistema o riservati (I2C per l'ADS1115).
I2C_GPIOS = {2, 3}

DEFAULT_WEATHER_CFG: dict[str, Any] = {
    "enabled": False,
    "latitude": None,
    "longitude": None,
    "rain_skip_mm": 5.0,
    "rain_prob_min": 80.0,
    "et0_low": 2.0,
    "et0_high": 5.0,
    "fetch_hour": "05:30",
    "max_age_hours": 30.0,
}

DEFAULT_POWER_CFG: dict[str, Any] = {
    "enabled": False,
    "serial_port": "/dev/ttyUSB0",
    "v_low": 12.6,
    "v_critical": 12.3,
    "hold_minutes": 10.0,
    "stale_seconds": 120.0,
}

DEFAULT_FLOAT_DEBOUNCE_S = 10.0
DEFAULT_RESERVE_LITERS = 1.0


# --- Tipi ---------------------------------------------------------------------

@dataclass(frozen=True)
class MoistureIndex:
    """Lettura normalizzata di un sensore di umidita' (percent=None se inutilizzabile)."""

    sensor_id: str
    raw: int | None
    percent: float | None
    note: str | None = None


@dataclass(frozen=True)
class MoistureSensorCfg:
    sensor_id: str
    adc_addr: int
    adc_channel: int
    raw_dry: int
    raw_wet: int


@dataclass(frozen=True)
class PumpSensorsCfg:
    """Sezione `sensors` di una pompa, gia' parsata e con i default applicati."""

    float_gpio: int | None = None
    float_debounce_s: float = DEFAULT_FLOAT_DEBOUNCE_S
    reserve_liters: float = DEFAULT_RESERVE_LITERS
    moisture: tuple[MoistureSensorCfg, ...] = ()
    thresholds: Mapping[str, float] | None = None
    rain_exposed: bool = False

    def any_configured(self) -> bool:
        return self.float_gpio is not None or bool(self.moisture)


@dataclass(frozen=True)
class DoseDecision:
    multiplier: float
    moisture_mult: float
    weather_mult: float
    battery_mult: float
    reasons: tuple[str, ...]

    def skip_event(self) -> str | None:
        """Tipo di evento 'saltato_*' se la dose e' zero, altrimenti None."""
        if self.multiplier > 0:
            return None
        if self.moisture_mult == 0.0:
            return "saltato_umidita"
        if self.weather_mult == 0.0:
            return "saltato_meteo"
        return "saltato_batteria"


# --- Motore -------------------------------------------------------------------

def compute_dose_multiplier(
    moisture: Sequence[MoistureIndex],
    weather: Any | None,
    battery_state: str | None,
    pump_cfg: PumpSensorsCfg | None,
    weather_cfg: Mapping[str, Any] | None,
) -> DoseDecision:
    """Calcola il moltiplicatore di dose per una pompa.

    `weather` e' un weather.WeatherInfo (o None se cache assente/stantia);
    `battery_state` e' lo stato gia' risolto da power.BatteryStateTracker
    ("ok"/"bassa"/"critica"/"sconosciuto") o None se il monitor e' disattivo.
    """
    reasons: list[str] = []

    moisture_mult = _moisture_multiplier(moisture, pump_cfg, reasons)
    weather_mult = _weather_multiplier(weather, pump_cfg, weather_cfg, reasons)
    battery_mult = _battery_multiplier(battery_state, reasons)

    multiplier = max(0.0, min(CLAMP_MAX, moisture_mult * weather_mult * battery_mult))
    return DoseDecision(
        multiplier=multiplier,
        moisture_mult=moisture_mult,
        weather_mult=weather_mult,
        battery_mult=battery_mult,
        reasons=tuple(reasons),
    )


def _moisture_multiplier(
    moisture: Sequence[MoistureIndex],
    pump_cfg: PumpSensorsCfg | None,
    reasons: list[str],
) -> float:
    if pump_cfg is None or not pump_cfg.moisture:
        return 1.0  # nessun sensore configurato: silenziosamente neutro
    valid = [m.percent for m in moisture if m.percent is not None]
    if not valid:
        reasons.append("nessun sensore umidita valido: neutro")
        return 1.0
    thresholds = pump_cfg.thresholds
    min_pct = min(valid)
    if thresholds is None:
        reasons.append(f"umidita {min_pct:.0f}%: solo osservazione (soglie non impostate)")
        return 1.0
    skip_above = _as_float(thresholds.get("skip_above"), 101.0)
    reduce_above = _as_float(thresholds.get("reduce_above"), 101.0)
    boost_below = _as_float(thresholds.get("boost_below"), -1.0)
    if min_pct >= skip_above:
        reasons.append(f"umidita {min_pct:.0f}% >= {skip_above:g}: salto")
        return 0.0
    if min_pct >= reduce_above:
        reasons.append(f"umidita {min_pct:.0f}% >= {reduce_above:g}: dose ridotta")
        return MOISTURE_REDUCE_MULT
    if min_pct <= boost_below:
        reasons.append(f"umidita {min_pct:.0f}% <= {boost_below:g}: dose aumentata")
        return MOISTURE_BOOST_MULT
    return 1.0


def _weather_multiplier(
    weather: Any | None,
    pump_cfg: PumpSensorsCfg | None,
    weather_cfg: Mapping[str, Any] | None,
    reasons: list[str],
) -> float:
    if not weather_cfg or not weather_cfg.get("enabled"):
        return 1.0
    if weather is None:
        reasons.append("meteo non disponibile: neutro")
        return 1.0

    rain_exposed = bool(pump_cfg.rain_exposed) if pump_cfg is not None else False
    rain_mm = getattr(weather, "rain_mm", None)
    rain_prob = getattr(weather, "rain_prob", None)
    if rain_exposed:
        rain_skip_mm = _as_float(weather_cfg.get("rain_skip_mm"), 5.0)
        rain_prob_min = _as_float(weather_cfg.get("rain_prob_min"), 80.0)
        heavy_rain = rain_mm is not None and rain_mm >= rain_skip_mm
        likely_rain = rain_prob is not None and rain_prob >= rain_prob_min
        if heavy_rain or likely_rain:
            reasons.append(
                f"pioggia prevista {rain_mm if rain_mm is not None else '?'} mm "
                f"(prob {rain_prob if rain_prob is not None else '?'}%): salto"
            )
            return 0.0

    et0 = getattr(weather, "et0_mm", None)
    if et0 is None:
        return 1.0
    et0_low = _as_float(weather_cfg.get("et0_low"), 2.0)
    et0_high = _as_float(weather_cfg.get("et0_high"), 5.0)
    if et0 < et0_low:
        reasons.append(f"ET0 {et0:.1f} mm: giornata fresca, dose ridotta")
        return ET0_COOL_MULT
    if et0 > et0_high:
        reasons.append(f"ET0 {et0:.1f} mm: giornata torrida, dose aumentata")
        return ET0_HOT_MULT
    return 1.0


def _battery_multiplier(battery_state: str | None, reasons: list[str]) -> float:
    if battery_state is None:
        return 1.0  # monitor disattivo: silenziosamente neutro
    if battery_state == "critica":
        reasons.append("batteria critica: salto")
        return 0.0
    if battery_state == "bassa":
        reasons.append("batteria bassa: dose ridotta")
        return BATTERY_LOW_MULT
    if battery_state == "sconosciuto":
        reasons.append("stato batteria sconosciuto: neutro")
    return 1.0


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --- Stima consumo giornaliero (per l'heartbeat) -------------------------------

def estimated_daily_liters(pump_entry: Mapping[str, Any], day: date) -> float:
    """Litri/giorno previsti dallo schedule di una pompa nella data `day`.

    Serve per l'autonomia stimata nell'heartbeat ("~N giorni residui").
    Parsing difensivo come scheduler._iter_occurrences_on.
    """
    if not isinstance(pump_entry, Mapping):
        return 0.0
    try:
        flow = float(pump_entry.get("flow_lph", 0.0))
    except (TypeError, ValueError):
        return 0.0
    total = 0.0
    programs = pump_entry.get("programs", [])
    for prog in programs if isinstance(programs, list) else []:
        if not isinstance(prog, dict) or not prog.get("enabled", True):
            continue
        try:
            start = date.fromisoformat(str(prog["start_date"]))
        except (KeyError, ValueError, TypeError):
            continue
        end_raw = prog.get("end_date")
        try:
            end = date.fromisoformat(str(end_raw)) if end_raw else None
        except (ValueError, TypeError):
            continue
        if day < start or (end is not None and day > end):
            continue
        try:
            duration = int(prog.get("duration_s", 0))
        except (ValueError, TypeError):
            continue
        if duration <= 0:
            continue
        times = prog.get("times", [])
        n_times = len(times) if isinstance(times, list) else 0
        total += scheduler.liters_for_duration(flow, duration) * n_times
    return total


# --- Parsing configurazione ----------------------------------------------------

def parse_adc_addr(value: Any) -> int:
    """Indirizzo I2C come int (72) o stringa esadecimale ("0x48")."""
    if isinstance(value, bool):
        raise ValueError(f"indirizzo I2C non valido: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value.strip(), 0)  # accetta "0x48" e "72"
    raise ValueError(f"indirizzo I2C non valido: {value!r}")


def weather_config(programs: Mapping[str, Any]) -> dict[str, Any]:
    return _merged(DEFAULT_WEATHER_CFG, programs.get("weather"))


def power_config(programs: Mapping[str, Any]) -> dict[str, Any]:
    return _merged(DEFAULT_POWER_CFG, programs.get("power"))


def notify_config(programs: Mapping[str, Any]) -> dict[str, Any]:
    raw = programs.get("notify")
    return dict(raw) if isinstance(raw, Mapping) else {"enabled": False}


def _merged(defaults: Mapping[str, Any], raw: Any) -> dict[str, Any]:
    merged = dict(defaults)
    if isinstance(raw, Mapping):
        merged.update(raw)
    return merged


def parse_sensor_config(programs: Mapping[str, Any]) -> dict[str, PumpSensorsCfg]:
    """Sezione `sensors` per pompa, con default. Righe malformate -> ignorate
    (la segnalazione rumorosa e' compito di validate_config)."""
    out: dict[str, PumpSensorsCfg] = {}
    pumps = programs.get("pumps", {})
    if not isinstance(pumps, Mapping):
        return out
    for pump_id, entry in pumps.items():
        raw = entry.get("sensors") if isinstance(entry, Mapping) else None
        if not isinstance(raw, Mapping):
            continue

        float_gpio: int | None = None
        debounce_s = DEFAULT_FLOAT_DEBOUNCE_S
        reserve = DEFAULT_RESERVE_LITERS
        float_raw = raw.get("float")
        if isinstance(float_raw, Mapping):
            try:
                float_gpio = int(float_raw["gpio"])
            except (KeyError, TypeError, ValueError):
                float_gpio = None
            debounce_s = _as_float(float_raw.get("debounce_s"), DEFAULT_FLOAT_DEBOUNCE_S)
            reserve = _as_float(float_raw.get("reserve_liters"), DEFAULT_RESERVE_LITERS)

        moisture: list[MoistureSensorCfg] = []
        moisture_raw = raw.get("moisture", [])
        for idx, item in enumerate(moisture_raw if isinstance(moisture_raw, list) else []):
            if not isinstance(item, Mapping):
                continue
            adc = item.get("adc")
            if not isinstance(adc, Mapping):
                continue
            try:
                moisture.append(
                    MoistureSensorCfg(
                        sensor_id=str(item.get("id") or f"{pump_id}_m{idx}"),
                        adc_addr=parse_adc_addr(adc.get("addr")),
                        adc_channel=int(adc.get("channel")),
                        raw_dry=int(item.get("raw_dry", 0)),
                        raw_wet=int(item.get("raw_wet", 0)),
                    )
                )
            except (TypeError, ValueError):
                continue

        thresholds_raw = raw.get("thresholds")
        thresholds = dict(thresholds_raw) if isinstance(thresholds_raw, Mapping) else None

        out[pump_id] = PumpSensorsCfg(
            float_gpio=float_gpio,
            float_debounce_s=debounce_s,
            reserve_liters=reserve,
            moisture=tuple(moisture),
            thresholds=thresholds,
            rain_exposed=bool(raw.get("rain_exposed", False)),
        )
    return out


# --- Validazione rumorosa -------------------------------------------------------

def validate_config(
    programs: Mapping[str, Any],
    pump_ids: set[str],
    pump_gpios: set[int],
) -> list[str]:
    """Avvisi (in italiano) su configurazioni sospette o in conflitto.

    Non blocca nulla: il demone continua col parsing difensivo, ma gli avvisi
    finiscono nell'evento `config_avviso` e nella UI.
    """
    warnings: list[str] = []
    pumps = programs.get("pumps", {})
    pumps = pumps if isinstance(pumps, Mapping) else {}

    seen_float_gpios: dict[int, str] = {}
    seen_channels: dict[tuple[int, int], str] = {}

    for pump_id, entry in pumps.items():
        raw = entry.get("sensors") if isinstance(entry, Mapping) else None
        if raw is None:
            continue
        if pump_id not in pump_ids:
            warnings.append(f"sensors: pompa sconosciuta '{pump_id}' (ignorata)")
            continue
        if not isinstance(raw, Mapping):
            warnings.append(f"{pump_id}: sezione sensors non valida (ignorata)")
            continue

        float_raw = raw.get("float")
        if isinstance(float_raw, Mapping):
            try:
                gpio = int(float_raw["gpio"])
            except (KeyError, TypeError, ValueError):
                warnings.append(f"{pump_id}: galleggiante senza gpio valido (ignorato)")
                gpio = None
            if gpio is not None:
                if gpio in pump_gpios:
                    warnings.append(
                        f"{pump_id}: gpio galleggiante {gpio} in conflitto con un rele pompa"
                    )
                if gpio in I2C_GPIOS:
                    warnings.append(
                        f"{pump_id}: gpio galleggiante {gpio} in conflitto con I2C (SDA/SCL)"
                    )
                if gpio in seen_float_gpios:
                    warnings.append(
                        f"{pump_id}: gpio galleggiante {gpio} gia usato da {seen_float_gpios[gpio]}"
                    )
                seen_float_gpios.setdefault(gpio, pump_id)

        moisture_raw = raw.get("moisture", [])
        for idx, item in enumerate(moisture_raw if isinstance(moisture_raw, list) else []):
            label = f"{pump_id} sensore #{idx + 1}"
            if not isinstance(item, Mapping) or not isinstance(item.get("adc"), Mapping):
                warnings.append(f"{label}: voce umidita senza blocco adc (ignorata)")
                continue
            adc = item["adc"]
            try:
                addr = parse_adc_addr(adc.get("addr"))
            except ValueError:
                warnings.append(f"{label}: indirizzo adc non valido: {adc.get('addr')!r}")
                continue
            try:
                channel = int(adc.get("channel"))
            except (TypeError, ValueError):
                warnings.append(f"{label}: canale adc non valido: {adc.get('channel')!r}")
                continue
            if not 0 <= channel <= 3:
                warnings.append(f"{label}: canale adc fuori range 0-3: {channel}")
            key = (addr, channel)
            if key in seen_channels:
                warnings.append(
                    f"{label}: canale adc {hex(addr)}/{channel} gia usato da {seen_channels[key]}"
                )
            seen_channels.setdefault(key, label)

        thresholds = raw.get("thresholds")
        if isinstance(thresholds, Mapping):
            skip_above = _as_float(thresholds.get("skip_above"), 101.0)
            reduce_above = _as_float(thresholds.get("reduce_above"), 101.0)
            boost_below = _as_float(thresholds.get("boost_below"), -1.0)
            if reduce_above >= skip_above:
                warnings.append(
                    f"{pump_id}: soglie invertite (reduce_above {reduce_above:g} >= "
                    f"skip_above {skip_above:g})"
                )
            if boost_below >= reduce_above:
                warnings.append(
                    f"{pump_id}: soglie invertite (boost_below {boost_below:g} >= "
                    f"reduce_above {reduce_above:g})"
                )

    wcfg = weather_config(programs)
    if wcfg.get("enabled"):
        if wcfg.get("latitude") is None or wcfg.get("longitude") is None:
            warnings.append("weather abilitato ma latitude/longitude mancanti")
        try:
            hh, mm = str(wcfg.get("fetch_hour", "05:30")).split(":")
            valid_hour = 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59
        except (ValueError, AttributeError):
            valid_hour = False
        if not valid_hour:
            warnings.append(f"weather: fetch_hour non valida: {wcfg.get('fetch_hour')!r}")

    ncfg = notify_config(programs)
    if ncfg.get("enabled") and ncfg.get("backend", "ntfy") not in ("ntfy",):
        warnings.append(f"notify: backend non supportato: {ncfg.get('backend')!r}")

    pcfg = power_config(programs)
    if pcfg.get("enabled"):
        v_low = _as_float(pcfg.get("v_low"), 12.6)
        v_critical = _as_float(pcfg.get("v_critical"), 12.3)
        if v_critical >= v_low:
            warnings.append(
                f"power: v_critical {v_critical:g} >= v_low {v_low:g} (soglie invertite)"
            )

    return warnings
