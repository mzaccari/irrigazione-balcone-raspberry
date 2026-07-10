"""Demone di irrigazione: UNICO proprietario delle pompe/GPIO.

Fa girare lo scheduling anche a browser chiuso. L'interfaccia Streamlit non
comanda i GPIO: scrive comandi nella coda (runtime/commands/) e legge lo stato
da runtime/state.json, che questo demone aggiorna ad ogni tick.

Il loop e cooperativo e a singolo thread: comandi manuali e avvii programmati
passano tutti dallo stesso `current_run`, quindi non possono correre in
parallelo e `allow_multiple=false` e rispettato per costruzione. La logica di un
singolo giro e isolata in `tick(now)` (con l'ora iniettata) cosi da poterla
testare senza hardware e senza attese reali (vedi tests/test_daemon_sim.py).

Sicurezza: stato sicuro (all_off) all'avvio e alla chiusura; ogni run ha un tetto
`max_run_seconds` indipendente dalla durata programmata; STOP interrompe subito;
i comandi manuali hanno precedenza su un avvio programmato in corso.
"""

from __future__ import annotations

import atexit
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import decision
import notify
import paths
import power
import scheduler
import store
import weather
from clock import Clock
from pump_controller import PumpController, load_config
from scheduler import get_option, occurrence_key
from sensors import FloatDebounce, FloatSwitchBank, MoistureSensorBank, moisture_percent


DEFAULT_TANK_LITERS = 25.0
DEFAULT_FLOW_LPH = 600.0
MAX_WARNINGS = 25
DEFAULT_TICK_SECONDS = 0.5
TREND_MINUTES = 15                       # campionamento sensori per il trend
LIVE_SECONDS_MAX = 300                   # tetto della lettura live (calibrazione)
WEATHER_RETRY_MINUTES = 30               # attesa minima tra tentativi di fetch falliti
SENSORS_LOG_MAX_BYTES = 5 * 1024 * 1024  # guardia dimensione runtime/sensors.jsonl
SENSORS_LOG_KEEP_LINES = 20_000


@dataclass
class RunState:
    pump_id: str
    source: str  # "scheduled" | "manual" | "pulse"
    started_at: datetime
    ends_at: datetime | None       # None = manuale a tempo indeterminato
    duration_s: int | None
    program_id: str | None
    last_serviced: datetime        # ultimo istante gia conteggiato per l'acqua


class Daemon:
    def __init__(
        self,
        controller: PumpController,
        clock: Clock,
        *,
        programs_path: Path = paths.PROGRAMS_JSON,
        state_path: Path = paths.STATE_JSON,
        commands_dir: Path = paths.COMMANDS_DIR,
        rejected_dir: Path = paths.REJECTED_DIR,
        events_path: Path = paths.EVENTS_JSONL,
        weather_path: Path = paths.WEATHER_JSON,
        sensors_log_path: Path = paths.SENSORS_JSONL,
        float_bank: FloatSwitchBank | None = None,
        moisture_bank: MoistureSensorBank | None = None,
        power_monitor: Any = None,
        notifier: notify.Notifier | None = None,
        weather_refresh: Callable[[dict, Path, datetime, str], None] | None = None,
    ) -> None:
        self.controller = controller
        self.clock = clock
        self.programs_path = Path(programs_path)
        self.state_path = Path(state_path)
        self.commands_dir = Path(commands_dir)
        self.rejected_dir = Path(rejected_dir)
        self.events_path = Path(events_path)
        self.weather_path = Path(weather_path)
        self.sensors_log_path = Path(sensors_log_path)

        self.current_run: RunState | None = None
        self.last_fired: dict[str, str] = {}
        self.water: dict[str, float] = {}
        self.tanks: dict[str, dict[str, float]] = {}
        self.warnings: list[dict[str, Any]] = []
        self.config: dict[str, Any] = {}
        self._config_mtime: float | None = None
        self._stop = False

        # Sensori & servizi (tutto spento finche la config non li abilita).
        # I banchi di default senza config sono inerti: nessun GPIO/I2C toccato.
        self.float_bank = float_bank if float_bank is not None else FloatSwitchBank()
        self.moisture_bank = (
            moisture_bank if moisture_bank is not None else MoistureSensorBank()
        )
        self.power_monitor = power_monitor
        self._power_injected = power_monitor is not None
        self.notifier: notify.Notifier = notifier if notifier is not None else notify.NullNotifier()
        self._notifier_injected = notifier is not None
        self._weather_refresh = weather_refresh or self._spawn_weather_refresh

        self.sensor_cfg: dict[str, decision.PumpSensorsCfg] = {}
        self.weather_cfg: dict[str, Any] = dict(decision.DEFAULT_WEATHER_CFG)
        self.notify_cfg: dict[str, Any] = {"enabled": False}
        self.power_cfg: dict[str, Any] = dict(decision.DEFAULT_POWER_CFG)
        self.config_warnings: list[str] = []
        self._logged_warnings: list[str] = []
        self.float_debounce: dict[str, FloatDebounce] = {}
        self.tank_empty: dict[str, bool] = {}
        self.battery_tracker: power.BatteryStateTracker | None = None
        self._battery_params: tuple[float, float, float] | None = None
        self.battery_state: str | None = None
        self.last_power: power.PowerStatus | None = None
        self.last_moisture: dict[str, list[dict[str, Any]]] = {}
        self.last_decision: dict[str, dict[str, Any]] = {}
        self.last_heartbeat_date: str | None = None
        self.live_until: datetime | None = None
        self._last_trend: datetime | None = None
        self._last_weather_attempt: datetime | None = None
        self._weather_thread: threading.Thread | None = None
        self._sensors_log_guard_date: str | None = None

        # Stato sicuro immediato, poi carica config e stato persistito.
        self.controller.all_off()
        self._reload_config(force=True)
        self._restore_state()

    # --- Config e stato persistito -----------------------------------------

    def _pump_ids(self) -> list[str]:
        return [pump.id for pump in self.controller.config.pumps]

    def _reload_config(self, force: bool = False) -> None:
        try:
            mtime = self.programs_path.stat().st_mtime
        except OSError:
            mtime = None
        if not force and mtime == self._config_mtime:
            return
        self._config_mtime = mtime
        self.config = store.read_json_or(self.programs_path, {"options": {}, "pumps": {}})

        pumps_cfg = self.config.get("pumps", {}) if isinstance(self.config, dict) else {}
        for pump_id in self._pump_ids():
            entry = pumps_cfg.get(pump_id, {}) if isinstance(pumps_cfg, dict) else {}
            capacity = float(entry.get("tank_liters", DEFAULT_TANK_LITERS))
            flow = float(entry.get("flow_lph", DEFAULT_FLOW_LPH))
            self.tanks[pump_id] = {"capacity": capacity, "flow": flow}
            if pump_id not in self.water:
                self.water[pump_id] = capacity  # prima accensione: serbatoio pieno
            else:
                self.water[pump_id] = scheduler.clamp_level(self.water[pump_id], capacity)

        self._reload_sensor_services()

    def _reload_sensor_services(self) -> None:
        """Applica le sezioni sensors/weather/notify/power dopo un reload config.

        Diff-aware: tocca solo cio che e' cambiato; latch e debounce dei
        galleggianti sopravvivono al reload (chiave = pump_id).
        """
        cfg = self.config if isinstance(self.config, dict) else {}
        self.sensor_cfg = decision.parse_sensor_config(cfg)
        self.weather_cfg = decision.weather_config(cfg)
        self.power_cfg = decision.power_config(cfg)
        new_notify_cfg = decision.notify_config(cfg)

        # Validazione rumorosa: logga solo quando gli avvisi cambiano.
        self.config_warnings = decision.validate_config(
            cfg,
            set(self._pump_ids()),
            {pump.gpio for pump in self.controller.config.pumps},
        )
        if self.config_warnings and self.config_warnings != self._logged_warnings:
            self._log_event(self.clock.now(), "config_avviso",
                            warnings=list(self.config_warnings))
        self._logged_warnings = list(self.config_warnings)

        # Galleggianti: ricostruzione input e debounce solo per cio che cambia.
        float_map = {
            pump_id: pump_cfg.float_gpio
            for pump_id, pump_cfg in self.sensor_cfg.items()
            if pump_cfg.float_gpio is not None and pump_id in self._pump_ids()
        }
        try:
            self.float_bank.reconfigure(float_map)
        except Exception as exc:
            self._warn(self.clock.now(), "sistema", f"galleggianti non inizializzati: {exc}")
        for pump_id, pump_cfg in self.sensor_cfg.items():
            if pump_id not in float_map:
                continue
            existing = self.float_debounce.get(pump_id)
            if existing is None or existing.debounce_s != pump_cfg.float_debounce_s:
                self.float_debounce[pump_id] = FloatDebounce(pump_cfg.float_debounce_s)
        for pump_id in list(self.float_debounce):
            if pump_id not in float_map:
                del self.float_debounce[pump_id]

        # Batteria: tracker ricreato solo se cambiano le soglie.
        if self.power_cfg.get("enabled"):
            params = (
                float(self.power_cfg.get("v_low", 12.6)),
                float(self.power_cfg.get("v_critical", 12.3)),
                float(self.power_cfg.get("hold_minutes", 10.0)) * 60.0,
            )
            if self.battery_tracker is None or params != self._battery_params:
                self.battery_tracker = power.BatteryStateTracker(
                    params[0], params[1], hold_s=params[2]
                )
                self._battery_params = params
        else:
            self.battery_tracker = None
            self._battery_params = None
            self.battery_state = None
            self.last_power = None

        # Monitor VE.Direct: gestito dal demone se non iniettato dai test.
        if not self._power_injected:
            enabled = bool(self.power_cfg.get("enabled"))
            port = str(self.power_cfg.get("serial_port", "/dev/ttyUSB0"))
            if enabled and self.power_monitor is None:
                try:
                    self.power_monitor = power.build_monitor(self.power_cfg, mock=self.float_bank.mock)
                except Exception as exc:
                    self._warn(self.clock.now(), "sistema", f"monitor VE.Direct non avviato: {exc}")
            elif self.power_monitor is not None and (
                not enabled or getattr(self.power_monitor, "port", port) != port
            ):
                self.power_monitor.close()
                self.power_monitor = None

        # Notifier: ricostruito solo se la sezione notify cambia.
        if not self._notifier_injected and new_notify_cfg != self.notify_cfg:
            self.notifier.close()
            self.notifier = notify.build_notifier(new_notify_cfg)
        self.notify_cfg = new_notify_cfg

    def _restore_state(self) -> None:
        data = store.read_json_or(self.state_path, None)
        if not isinstance(data, dict):
            return
        saved_last_fired = data.get("last_fired")
        if isinstance(saved_last_fired, dict):
            self.last_fired = {str(k): str(v) for k, v in saved_last_fired.items()}
        saved_pumps = data.get("pumps", {})
        if isinstance(saved_pumps, dict):
            for pump_id, info in saved_pumps.items():
                if pump_id not in self.tanks or not isinstance(info, dict):
                    continue
                if "water_liters" in info:
                    capacity = self.tanks[pump_id]["capacity"]
                    self.water[pump_id] = scheduler.clamp_level(info["water_liters"], capacity)
                # Il latch "serbatoio vuoto" DEVE sopravvivere al riavvio
                # (Restart=always non deve sbloccare un serbatoio vuoto).
                float_info = info.get("float")
                if isinstance(float_info, dict) and float_info.get("empty_latched"):
                    self.tank_empty[pump_id] = True
        notify_info = data.get("notify")
        if isinstance(notify_info, dict) and notify_info.get("last_heartbeat_date"):
            self.last_heartbeat_date = str(notify_info["last_heartbeat_date"])
        # current_run NON viene ripristinato: dopo un riavvio le pompe sono spente.

    def _flow(self, pump_id: str) -> float:
        return self.tanks.get(pump_id, {}).get("flow", DEFAULT_FLOW_LPH)

    def _capacity(self, pump_id: str) -> float:
        return self.tanks.get(pump_id, {}).get("capacity", DEFAULT_TANK_LITERS)

    # --- Tick ---------------------------------------------------------------

    def tick(self, now: datetime) -> None:
        """Un singolo giro del loop. `now` iniettato per i test."""
        self._reload_config()
        commands = store.drain_commands(self.commands_dir, self.rejected_dir)
        self._apply_commands(commands, now)

        self._service_floats(now)  # prima del run: puo fermare la pompa

        if self.current_run is not None:
            self._service_current_run(now)

        if self.current_run is None:
            self._start_due_if_any(now)

        self._service_trend(now)
        self._service_weather(now)
        self._service_power(now)
        self._service_heartbeat(now)
        self._sweep_missed(now)
        self._write_state(now)

    # --- Comandi ------------------------------------------------------------

    def _apply_commands(self, commands: list[dict[str, Any]], now: datetime) -> None:
        # stop_all sempre per primo (sicurezza)
        ordered = sorted(commands, key=lambda c: 0 if c.get("type") == "stop_all" else 1)
        for command in ordered:
            ctype = command.get("type")
            pump = command.get("pump")
            if ctype == "stop_all":
                self._cmd_stop_all(now)
            elif ctype == "on" and self._valid_pump(pump):
                self._cmd_manual_on(now, pump)
            elif ctype == "off" and self._valid_pump(pump):
                self._cmd_off(now, pump)
            elif ctype == "pulse" and self._valid_pump(pump):
                self._cmd_pulse(now, pump, command.get("seconds"))
            elif ctype == "refill" and self._valid_pump(pump):
                self._cmd_refill(now, pump, command.get("liters"))
            elif ctype == "sensor_live":
                self._cmd_sensor_live(now, command.get("seconds"))
            elif ctype == "reload":
                self._reload_config(force=True)
            else:
                self._log_event(now, "comando_ignoto", command=command)

    def _valid_pump(self, pump_id: Any) -> bool:
        return isinstance(pump_id, str) and pump_id in set(self._pump_ids())

    def _cmd_stop_all(self, now: datetime) -> None:
        if self.current_run is not None:
            self._end_run(now, "stop_manuale")
        self.controller.all_off()
        self._log_event(now, "stop_all")

    def _cmd_manual_on(self, now: datetime, pump_id: str) -> None:
        self._preempt_for_manual(now)
        self._warn_if_low(now, pump_id, duration_s=None)
        self._warn_if_latched(now, pump_id)
        self.controller.on(pump_id)
        self.current_run = RunState(pump_id, "manual", now, None, None, None, now)
        self._log_event(now, "manuale_on", pump_id=pump_id)

    def _cmd_off(self, now: datetime, pump_id: str) -> None:
        if self.current_run is not None and self.current_run.pump_id == pump_id:
            self._end_run(now, "off_manuale")
        else:
            self.controller.off(pump_id)
            self._log_event(now, "manuale_off", pump_id=pump_id)

    def _cmd_pulse(self, now: datetime, pump_id: str, seconds: Any) -> None:
        try:
            secs = int(float(seconds))
        except (TypeError, ValueError):
            self._log_event(now, "pulse_invalido", pump_id=pump_id, seconds=seconds)
            return
        if secs <= 0:
            return
        self._preempt_for_manual(now)
        self._warn_if_low(now, pump_id, duration_s=secs)
        self._warn_if_latched(now, pump_id)
        self.controller.on(pump_id)
        self.current_run = RunState(
            pump_id, "pulse", now, now + timedelta(seconds=secs), secs, None, now
        )
        self._log_event(now, "impulso_on", pump_id=pump_id, seconds=secs)

    def _cmd_refill(self, now: datetime, pump_id: str, liters: Any) -> None:
        capacity = self._capacity(pump_id)
        if liters is None:
            level = capacity
        else:
            try:
                level = scheduler.clamp_level(float(liters), capacity)
            except (TypeError, ValueError):
                level = capacity
        self.water[pump_id] = level
        # Il riempimento sgancia il latch del galleggiante e riarma il debounce:
        # se il serbatoio e' ancora vuoto, ri-scatta debounce_s dopo (corretto).
        self.tank_empty[pump_id] = False
        cfg = self.sensor_cfg.get(pump_id)
        if cfg is not None and cfg.float_gpio is not None:
            self.float_debounce[pump_id] = FloatDebounce(cfg.float_debounce_s)
        self._log_event(now, "riempito", pump_id=pump_id, water=level)

    def _cmd_sensor_live(self, now: datetime, seconds: Any) -> None:
        """Arma la lettura live dei sensori (calibrazione dalla UI)."""
        try:
            secs = int(float(seconds))
        except (TypeError, ValueError):
            secs = 60
        secs = max(1, min(secs, LIVE_SECONDS_MAX))
        self.live_until = now + timedelta(seconds=secs)
        self._log_event(now, "lettura_live", seconds=secs)

    def _warn_if_latched(self, now: datetime, pump_id: str) -> None:
        """Avvio manuale con serbatoio vuoto: procede con avviso (filosofia
        esistente) ma il debounce viene riarmato, cosi se il serbatoio e'
        davvero vuoto il galleggiante ferma la pompa debounce_s secondi dopo."""
        if not self.tank_empty.get(pump_id, False):
            return
        self._warn(now, pump_id,
                   "serbatoio vuoto (galleggiante): avvio manuale eseguito comunque")
        cfg = self.sensor_cfg.get(pump_id)
        if cfg is not None and cfg.float_gpio is not None:
            self.float_debounce[pump_id] = FloatDebounce(cfg.float_debounce_s)

    def _preempt_for_manual(self, now: datetime) -> None:
        if self.current_run is not None:
            self._end_run(now, "interrotto_da_manuale")

    # --- Sensori e servizi periodici -----------------------------------------

    def _service_floats(self, now: datetime) -> None:
        """Legge i galleggianti (solo GPIO, microsecondi) e gestisce il latch.

        Allo scatto (debounce superato): ferma un'eventuale pompa in funzione
        su quel serbatoio, attiva il latch persistente e riconcilia la stima
        d'acqua al valore di riserva. Il latch si sgancia solo col refill.
        """
        for pump_id, debounce in self.float_debounce.items():
            present = self.float_bank.water_present(pump_id)
            if not debounce.update(present, now):
                continue
            if self.current_run is not None and self.current_run.pump_id == pump_id:
                self._end_run(now, "stop_galleggiante")
            if not self.tank_empty.get(pump_id, False):
                self.tank_empty[pump_id] = True
                cfg = self.sensor_cfg.get(pump_id)
                reserve = cfg.reserve_liters if cfg is not None else decision.DEFAULT_RESERVE_LITERS
                self.water[pump_id] = min(self.water.get(pump_id, 0.0), reserve)
                self._warn(now, pump_id, "galleggiante: serbatoio vuoto")
                self._log_event(now, "serbatoio_vuoto", pump_id=pump_id,
                                water=round(self.water[pump_id], 2))

    def _read_moisture(self, pump_id: str, now: datetime) -> list[decision.MoistureIndex]:
        """Legge e normalizza i sensori di umidita di una pompa (mai solleva)."""
        cfg = self.sensor_cfg.get(pump_id)
        if cfg is None or not cfg.moisture:
            return []
        out: list[decision.MoistureIndex] = []
        for sensor in cfg.moisture:
            raw = self.moisture_bank.read_oversampled(sensor.adc_addr, sensor.adc_channel)
            percent = moisture_percent(raw, sensor.raw_dry, sensor.raw_wet)
            note = None
            if raw is None:
                note = "lettura fallita"
            elif percent is None:
                note = "fuori banda o non calibrato"
            out.append(decision.MoistureIndex(sensor.sensor_id, raw, percent, note))
        self.last_moisture[pump_id] = [
            {"id": m.sensor_id, "raw": m.raw,
             "percent": round(m.percent, 1) if m.percent is not None else None,
             "note": m.note, "at": now.isoformat()}
            for m in out
        ]
        return out

    def _decide_duration(self, now: datetime, occ: Any) -> tuple[int, decision.DoseDecision | None]:
        """Durata effettiva del programma dopo umidita/meteo/batteria.

        Se per la pompa non e' configurato nulla, il percorso e' identico a
        prima (nessuna lettura, nessun evento): i test storici non cambiano.
        """
        pump_cfg = self.sensor_cfg.get(occ.pump_id)
        has_moisture = pump_cfg is not None and bool(pump_cfg.moisture)
        weather_enabled = bool(self.weather_cfg.get("enabled"))
        power_enabled = bool(self.power_cfg.get("enabled"))
        if not (has_moisture or weather_enabled or power_enabled):
            return occ.duration_s, None

        moisture = self._read_moisture(occ.pump_id, now) if has_moisture else []
        winfo = (
            weather.load_cached(self.weather_path, now,
                                float(self.weather_cfg.get("max_age_hours", 30.0)))
            if weather_enabled else None
        )
        battery_state = self.battery_state if power_enabled else None

        dose = decision.compute_dose_multiplier(
            moisture, winfo, battery_state, pump_cfg, self.weather_cfg
        )
        max_run = int(float(get_option(self.config, "max_run_seconds")))
        effective_s = min(int(round(occ.duration_s * dose.multiplier)), max_run)

        self.last_decision[occ.pump_id] = {
            "at": now.isoformat(),
            "multiplier": round(dose.multiplier, 3),
            "base_s": occ.duration_s,
            "effective_s": effective_s,
            "reasons": list(dose.reasons),
        }
        self._log_event(
            now, "decisione_dose", pump_id=occ.pump_id, time=occ.time_str,
            base_s=occ.duration_s, effective_s=effective_s,
            multiplier=round(dose.multiplier, 3),
            moisture=[{"id": m.sensor_id, "raw": m.raw, "percent": m.percent}
                      for m in moisture],
            weather=({"et0_mm": winfo.et0_mm, "rain_mm": winfo.rain_mm,
                      "rain_prob": winfo.rain_prob} if winfo is not None else None),
            battery=battery_state,
            reasons=list(dose.reasons),
        )
        return effective_s, dose

    def _service_trend(self, now: datetime) -> None:
        """Campionamento periodico per trend/UI. MAI mentre una pompa gira
        (protegge il tick e evita letture disturbate dal carico sul solare)."""
        if self.current_run is not None:
            return
        if self.live_until is not None and now >= self.live_until:
            self.live_until = None

        pumps_with_moisture = [p for p, c in self.sensor_cfg.items() if c.moisture]
        if self.live_until is not None:
            # modalita calibrazione: campiona ogni tick, solo stato (niente log)
            for pump_id in pumps_with_moisture:
                self._read_moisture(pump_id, now)
            return

        anything = bool(pumps_with_moisture or self.float_debounce
                        or self.power_cfg.get("enabled"))
        if not anything:
            return
        if (self._last_trend is not None
                and (now - self._last_trend).total_seconds() < TREND_MINUTES * 60):
            return
        self._last_trend = now

        entry: dict[str, Any] = {"at": now.isoformat()}
        moisture: dict[str, Any] = {}
        for pump_id in pumps_with_moisture:
            for m in self._read_moisture(pump_id, now):
                moisture[m.sensor_id] = {"raw": m.raw, "percent": m.percent}
        if moisture:
            entry["moisture"] = moisture
        if self.float_debounce:
            entry["floats"] = {
                pump_id: self.float_bank.water_present(pump_id)
                for pump_id in self.float_debounce
            }
        if self.last_power is not None or self.battery_state is not None:
            entry["battery"] = {
                "v": self.last_power.battery_v if self.last_power else None,
                "panel_w": self.last_power.panel_w if self.last_power else None,
                "state": self.battery_state,
            }
        entry["water"] = {p: round(self.water.get(p, 0.0), 2) for p in self._pump_ids()}
        try:
            store.append_event(self.sensors_log_path, entry)
        except OSError:
            pass

        today = now.date().isoformat()
        if self._sensors_log_guard_date != today:
            self._sensors_log_guard_date = today
            store.trim_jsonl(self.sensors_log_path,
                             SENSORS_LOG_MAX_BYTES, SENSORS_LOG_KEEP_LINES)

    def _service_weather(self, now: datetime) -> None:
        """Avvia (in thread) il fetch meteo quando serve. Il tick non fa MAI rete."""
        if not self.weather_cfg.get("enabled"):
            return
        if self.weather_cfg.get("latitude") is None or self.weather_cfg.get("longitude") is None:
            return
        cache = store.read_json_or(self.weather_path, None)
        if not weather.refresh_due(cache, now, str(self.weather_cfg.get("fetch_hour", "05:30"))):
            return
        if self._weather_thread is not None and self._weather_thread.is_alive():
            return
        if (self._last_weather_attempt is not None
                and (now - self._last_weather_attempt).total_seconds()
                < WEATHER_RETRY_MINUTES * 60):
            return
        self._last_weather_attempt = now
        self._weather_refresh(dict(self.weather_cfg), self.weather_path, now,
                              self.clock.tz_name)

    def _spawn_weather_refresh(self, cfg: dict, path: Path, now: datetime,
                               tz_name: str) -> None:
        thread = threading.Thread(
            target=weather.refresh_cache, args=(cfg, path, now, tz_name),
            name="weather", daemon=True,
        )
        self._weather_thread = thread
        thread.start()

    def _service_power(self, now: datetime) -> None:
        """Aggiorna lo stato batteria dall'ultimo frame VE.Direct (mai seriale qui)."""
        if not self.power_cfg.get("enabled") or self.power_monitor is None \
                or self.battery_tracker is None:
            return
        status = power.fresh_status(
            getattr(self.power_monitor, "latest", None), now,
            float(self.power_cfg.get("stale_seconds", 120.0)),
        )
        self.last_power = status
        new_state = self.battery_tracker.update(
            status.battery_v if status is not None else None, now
        )
        if new_state == self.battery_state:
            return
        old_state = self.battery_state
        self.battery_state = new_state
        severity = {"ok": 0, "sconosciuto": 0, "bassa": 1, "critica": 2}
        if severity.get(new_state, 0) > severity.get(old_state or "ok", 0):
            voltage = status.battery_v if status is not None else None
            self._warn(now, "sistema",
                       f"batteria {new_state}" + (f" ({voltage:.2f} V)" if voltage else ""))
            self._log_event(now, "batteria_bassa", state=new_state, voltage=voltage)

    def _service_heartbeat(self, now: datetime) -> None:
        if not self.notify_cfg.get("enabled"):
            return
        heartbeat_time = self.notify_cfg.get("heartbeat_time")
        if not heartbeat_time:
            return
        if not notify.heartbeat_due(now, self.last_heartbeat_date, heartbeat_time):
            return
        self.last_heartbeat_date = now.date().isoformat()
        title, message, priority = notify.format_heartbeat(
            self._heartbeat_snapshot(now), now
        )
        self.notifier.send(title, message, priority)

    def _heartbeat_snapshot(self, now: datetime) -> dict[str, Any]:
        names = self._pump_names()
        pumps_cfg = self.config.get("pumps", {}) if isinstance(self.config, dict) else {}
        pumps: dict[str, Any] = {}
        for pump_id in self._pump_ids():
            water_liters = self.water.get(pump_id, 0.0)
            daily = decision.estimated_daily_liters(
                pumps_cfg.get(pump_id, {}) if isinstance(pumps_cfg, dict) else {},
                now.date(),
            )
            moisture_values = [
                m["percent"] for m in self.last_moisture.get(pump_id, [])
                if m.get("percent") is not None
            ]
            pumps[pump_id] = {
                "name": names.get(pump_id, pump_id),
                "water_liters": round(water_liters, 1),
                "days_left": (water_liters / daily) if daily > 0 else None,
                "empty_latched": self.tank_empty.get(pump_id, False),
                "moisture_pct": min(moisture_values) if moisture_values else None,
            }
        snapshot: dict[str, Any] = {
            "pumps": pumps,
            "warnings_count": len(self.warnings),
        }
        if self.battery_state is not None:
            snapshot["battery"] = {
                "battery_v": self.last_power.battery_v if self.last_power else None,
                "panel_w": self.last_power.panel_w if self.last_power else None,
                "state": self.battery_state,
            }
        winfo = weather.load_cached(self.weather_path, now,
                                    float(self.weather_cfg.get("max_age_hours", 30.0)))
        if winfo is not None:
            snapshot["weather_line"] = (
                f"Meteo: ET0 {winfo.et0_mm} mm, pioggia {winfo.rain_mm} mm"
            )
        return snapshot

    def _pump_names(self) -> dict[str, str]:
        return {pump.id: pump.name for pump in self.controller.config.pumps}

    def close(self) -> None:
        """Chiude i servizi posseduti dal demone (banche sensori, notifier, VE.Direct)."""
        self.float_bank.close()
        self.moisture_bank.close()
        if self.power_monitor is not None:
            self.power_monitor.close()
        self.notifier.close()

    # --- Avvii programmati --------------------------------------------------

    def _start_due_if_any(self, now: datetime) -> None:
        catch_up = get_option(self.config, "catch_up_minutes")
        due = scheduler.due_occurrences(self.config, now, self.last_fired, catch_up)
        if not due:
            return
        occ = due[0]
        key = occurrence_key(occ.pump_id, occ.program_id, occ.time_str)
        today = now.date().isoformat()

        # 1) Latch galleggiante: serbatoio vuoto conclamato -> non ritentare oggi.
        if self.tank_empty.get(occ.pump_id, False):
            self.last_fired[key] = today
            self._warn(now, occ.pump_id,
                       f"programma {occ.time_str} saltato: serbatoio vuoto (galleggiante)")
            self._log_event(now, "saltato_serbatoio_vuoto", pump_id=occ.pump_id,
                            time=occ.time_str)
            return

        # 2) Umidita x meteo x batteria -> durata effettiva (neutro se non configurati).
        effective_s, dose = self._decide_duration(now, occ)
        if effective_s <= 0 and dose is not None:
            self.last_fired[key] = today
            event_type = dose.skip_event() or "saltato_umidita"
            self._warn(now, occ.pump_id,
                       f"programma {occ.time_str} saltato: {'; '.join(dose.reasons)}")
            self._log_event(now, event_type, pump_id=occ.pump_id, time=occ.time_str,
                            reasons=list(dose.reasons))
            return

        # 3) Stima acqua sulla durata effettiva (una dose ridotta puo starci).
        if not scheduler.has_enough_water(self.water[occ.pump_id], self._flow(occ.pump_id), effective_s):
            self.last_fired[key] = today  # non ritentare oggi
            self._warn(now, occ.pump_id,
                       f"programma {occ.time_str} saltato: acqua stimata insufficiente")
            self._log_event(now, "saltato_acqua", pump_id=occ.pump_id,
                            time=occ.time_str, water=round(self.water[occ.pump_id], 2))
            return

        self.last_fired[key] = today  # marca subito: niente doppio avvio durante il run
        self.controller.on(occ.pump_id)
        self.current_run = RunState(
            occ.pump_id, "scheduled", now,
            now + timedelta(seconds=effective_s), effective_s, occ.program_id, now,
        )
        extra = {"base_s": occ.duration_s} if effective_s != occ.duration_s else {}
        self._log_event(now, "programma_on", pump_id=occ.pump_id,
                        time=occ.time_str, seconds=effective_s, **extra)

    def _sweep_missed(self, now: datetime) -> None:
        catch_up = get_option(self.config, "catch_up_minutes")
        today = now.date().isoformat()
        for occ in scheduler.missed_occurrences(self.config, now, self.last_fired, catch_up):
            key = occurrence_key(occ.pump_id, occ.program_id, occ.time_str)
            self.last_fired[key] = today
            self._log_event(now, "saltato_fuori_finestra", pump_id=occ.pump_id, time=occ.time_str)

    # --- Gestione del run in corso ------------------------------------------

    def _service_current_run(self, now: datetime) -> None:
        run = self.current_run
        assert run is not None
        self._account_water(run, now)

        if run.ends_at is not None and now >= run.ends_at:
            self._end_run(now, "completato")
            return
        max_run = float(get_option(self.config, "max_run_seconds"))
        if (now - run.started_at).total_seconds() > max_run:
            self._warn(now, run.pump_id, "durata massima superata: pompa spenta per sicurezza")
            self._end_run(now, "max_run")

    def _account_water(self, run: RunState, now: datetime) -> None:
        # Conta il consumo fino a min(now, ends_at): non oltre la fine prevista.
        seg_end = run.ends_at if (run.ends_at is not None and now > run.ends_at) else now
        elapsed = (seg_end - run.last_serviced).total_seconds()
        if elapsed > 0:
            self.water[run.pump_id] = scheduler.apply_consumption(
                self.water[run.pump_id], self._flow(run.pump_id), elapsed
            )
            run.last_serviced = seg_end

    def _end_run(self, now: datetime, reason: str) -> None:
        run = self.current_run
        assert run is not None
        self._account_water(run, now)
        self.controller.off(run.pump_id)
        self._log_event(now, "run_off", pump_id=run.pump_id, source=run.source,
                        reason=reason, water=round(self.water[run.pump_id], 2))
        self.current_run = None

    # --- Avvisi e log -------------------------------------------------------

    def _warn_if_low(self, now: datetime, pump_id: str, duration_s: int | None) -> None:
        if duration_s is None:
            return
        if not scheduler.has_enough_water(self.water[pump_id], self._flow(pump_id), duration_s):
            self._warn(now, pump_id, "acqua stimata bassa: avvio manuale eseguito comunque")

    def _warn(self, now: datetime, pump_id: str, message: str) -> None:
        self.warnings.append({"at": now.isoformat(), "pump_id": pump_id, "message": message})
        self.warnings = self.warnings[-MAX_WARNINGS:]

    def _log_event(self, now: datetime, event_type: str, **fields: Any) -> None:
        event = {"at": now.isoformat(), "type": event_type}
        event.update(fields)
        try:
            store.append_event(self.events_path, event)
        except OSError:
            pass
        # Notifica push (accodamento istantaneo, mai bloccante per il tick).
        notify_key = event_type
        if event_type == "run_off" and fields.get("reason") == "max_run":
            notify_key = "max_run"
        if notify.should_notify(self.notify_cfg, notify_key):
            title, message, priority = notify.format_event_message(
                event, self._pump_names()
            )
            self.notifier.send(title, message, priority)

    # --- Scrittura stato ----------------------------------------------------

    def _write_state(self, now: datetime) -> None:
        next_runs = scheduler.next_run_per_pump(self.config, now)
        snapshot = {ps.id: ps for ps in self.controller.snapshot()}

        pumps_state: dict[str, Any] = {}
        for pump_id in self._pump_ids():
            ps = snapshot.get(pump_id)
            nxt = next_runs.get(pump_id)
            pumps_state[pump_id] = {
                "name": ps.name if ps else pump_id,
                "gpio": ps.gpio if ps else None,
                "physical_pin": ps.physical_pin if ps else None,
                "active": bool(ps.active) if ps else False,
                "tank_liters": self._capacity(pump_id),
                "flow_lph": self._flow(pump_id),
                "water_liters": round(self.water.get(pump_id, 0.0), 2),
                "next_run": nxt.isoformat() if nxt else None,
            }
            pump_cfg = self.sensor_cfg.get(pump_id)
            if pump_cfg is not None and pump_cfg.float_gpio is not None:
                debounce = self.float_debounce.get(pump_id)
                pumps_state[pump_id]["float"] = {
                    "configured": True,
                    "gpio": pump_cfg.float_gpio,
                    "water_present": self.float_bank.water_present(pump_id),
                    "empty_latched": self.tank_empty.get(pump_id, False),
                    "empty_since": (
                        debounce.empty_since.isoformat()
                        if debounce is not None and debounce.empty_since is not None
                        else None
                    ),
                }
            if pump_id in self.last_moisture:
                pumps_state[pump_id]["moisture"] = self.last_moisture[pump_id]
            if pump_id in self.last_decision:
                pumps_state[pump_id]["last_decision"] = self.last_decision[pump_id]

        state = {
            "updated_at": now.isoformat(),
            "mock": self.controller.mock,
            "sensor_mock": self.float_bank.mock,
            "timezone": self.clock.tz_name,
            "pumps": pumps_state,
            "current_run": self._current_run_dict(),
            "last_fired": self.last_fired,
            "warnings": self.warnings,
            "config_warnings": self.config_warnings,
            "live_sampling_until": (
                self.live_until.isoformat() if self.live_until is not None else None
            ),
        }
        if self.weather_cfg.get("enabled"):
            state["weather"] = store.read_json_or(self.weather_path, None)
        if self.power_cfg.get("enabled"):
            state["power"] = {
                "state": self.battery_state,
                "battery_v": self.last_power.battery_v if self.last_power else None,
                "battery_a": self.last_power.battery_a if self.last_power else None,
                "panel_w": self.last_power.panel_w if self.last_power else None,
                "charge_state": self.last_power.charge_state if self.last_power else None,
                "at": self.last_power.at.isoformat() if self.last_power else None,
            }
        if self.notify_cfg.get("enabled") or self.last_heartbeat_date:
            state["notify"] = {
                "enabled": bool(self.notify_cfg.get("enabled")),
                "backend": self.notify_cfg.get("backend", "ntfy"),
                "last_heartbeat_date": self.last_heartbeat_date,
                **self.notifier.stats(),
            }
        try:
            store.write_json_atomic(self.state_path, state)
        except OSError:
            pass

    def _current_run_dict(self) -> dict[str, Any] | None:
        run = self.current_run
        if run is None:
            return None
        return {
            "pump_id": run.pump_id,
            "source": run.source,
            "started_at": run.started_at.isoformat(),
            "ends_at": run.ends_at.isoformat() if run.ends_at else None,
            "duration_s": run.duration_s,
            "program_id": run.program_id,
        }

    # --- Loop ---------------------------------------------------------------

    def request_stop(self, *_args: Any) -> None:
        self._stop = True

    def run_forever(self, tick_seconds: float = DEFAULT_TICK_SECONDS) -> None:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        atexit.register(self.controller.all_off)
        try:
            while not self._stop:
                self.tick(self.clock.now())
                time.sleep(tick_seconds)
        finally:
            self.controller.all_off()
            self._write_state(self.clock.now())


def main() -> None:
    config = load_config(str(paths.PUMPS_JSON))
    programs = store.read_json_or(paths.PROGRAMS_JSON, {"options": {}})
    tz = get_option(programs, "timezone")
    clock = Clock(tz)
    controller = PumpController(config)
    daemon = Daemon(controller, clock)
    mode = "SIMULAZIONE (mock)" if controller.mock else "GPIO reale"
    sensor_mode = "mock" if daemon.float_bank.mock else "reali"
    print(f"[daemon] avvio - {mode} - sensori {sensor_mode} - tz {tz}")
    try:
        daemon.run_forever()
    finally:
        daemon.close()      # sensori, notifier, VE.Direct
        controller.close()  # per ultimo: i rele restano in stato sicuro
    print("[daemon] uscita pulita")


if __name__ == "__main__":
    main()
