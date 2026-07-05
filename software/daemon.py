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
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import paths
import scheduler
import store
from clock import Clock
from pump_controller import PumpController, load_config
from scheduler import get_option, occurrence_key


DEFAULT_TANK_LITERS = 25.0
DEFAULT_FLOW_LPH = 600.0
MAX_WARNINGS = 25
DEFAULT_TICK_SECONDS = 0.5


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
    ) -> None:
        self.controller = controller
        self.clock = clock
        self.programs_path = Path(programs_path)
        self.state_path = Path(state_path)
        self.commands_dir = Path(commands_dir)
        self.rejected_dir = Path(rejected_dir)
        self.events_path = Path(events_path)

        self.current_run: RunState | None = None
        self.last_fired: dict[str, str] = {}
        self.water: dict[str, float] = {}
        self.tanks: dict[str, dict[str, float]] = {}
        self.warnings: list[dict[str, Any]] = []
        self.config: dict[str, Any] = {}
        self._config_mtime: float | None = None
        self._stop = False

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

        if self.current_run is not None:
            self._service_current_run(now)

        if self.current_run is None:
            self._start_due_if_any(now)

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
        self._log_event(now, "riempito", pump_id=pump_id, water=level)

    def _preempt_for_manual(self, now: datetime) -> None:
        if self.current_run is not None:
            self._end_run(now, "interrotto_da_manuale")

    # --- Avvii programmati --------------------------------------------------

    def _start_due_if_any(self, now: datetime) -> None:
        catch_up = get_option(self.config, "catch_up_minutes")
        due = scheduler.due_occurrences(self.config, now, self.last_fired, catch_up)
        if not due:
            return
        occ = due[0]
        key = occurrence_key(occ.pump_id, occ.program_id, occ.time_str)
        today = now.date().isoformat()

        if not scheduler.has_enough_water(self.water[occ.pump_id], self._flow(occ.pump_id), occ.duration_s):
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
            now + timedelta(seconds=occ.duration_s), occ.duration_s, occ.program_id, now,
        )
        self._log_event(now, "programma_on", pump_id=occ.pump_id,
                        time=occ.time_str, seconds=occ.duration_s)

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

        state = {
            "updated_at": now.isoformat(),
            "mock": self.controller.mock,
            "timezone": self.clock.tz_name,
            "pumps": pumps_state,
            "current_run": self._current_run_dict(),
            "last_fired": self.last_fired,
            "warnings": self.warnings,
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
    print(f"[daemon] avvio - {mode} - tz {tz}")
    try:
        daemon.run_forever()
    finally:
        controller.close()
    print("[daemon] uscita pulita")


if __name__ == "__main__":
    main()
