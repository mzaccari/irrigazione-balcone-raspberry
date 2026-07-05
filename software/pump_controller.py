from __future__ import annotations

import json
import os
import platform
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TRUE_VALUES = {"1", "true", "yes", "y", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "off"}


@dataclass(frozen=True)
class PumpConfig:
    id: str
    name: str
    gpio: int
    physical_pin: int
    active_high: bool = False


@dataclass(frozen=True)
class ControllerConfig:
    pumps: tuple[PumpConfig, ...]
    allow_multiple: bool = False
    max_manual_seconds: float = 10.0


@dataclass(frozen=True)
class PumpState:
    id: str
    name: str
    gpio: int
    physical_pin: int
    active: bool


DEFAULT_CONFIG = ControllerConfig(
    pumps=(
        PumpConfig(
            id="pompa_1",
            name="Pompa 1 - zona acqua alta",
            gpio=17,
            physical_pin=11,
            active_high=False,
        ),
        PumpConfig(
            id="pompa_2",
            name="Pompa 2 - zona media",
            gpio=27,
            physical_pin=13,
            active_high=False,
        ),
        PumpConfig(
            id="pompa_3",
            name="Pompa 3 - zona secca",
            gpio=22,
            physical_pin=15,
            active_high=False,
        ),
    )
)


class MockOutput:
    def __init__(self, pin: int, active_high: bool = False) -> None:
        self.pin = pin
        self.active_high = active_high
        self._active = False
        self._closed = False

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def closed(self) -> bool:
        return self._closed

    def on(self) -> None:
        # Mima gpiozero: agire su un device chiuso solleva.
        if self._closed:
            raise RuntimeError("MockOutput chiuso: on() non consentito")
        self._active = True

    def off(self) -> None:
        if self._closed:
            raise RuntimeError("MockOutput chiuso: off() non consentito")
        self._active = False

    def close(self) -> None:
        self._active = False
        self._closed = True


class PumpController:
    def __init__(self, config: ControllerConfig, mock: bool | None = None) -> None:
        self.config = config
        self.mock = resolve_mock_mode(mock)
        self._lock = threading.RLock()
        self._pumps = {pump.id: pump for pump in config.pumps}
        self._outputs = {
            pump.id: self._build_output(pump.gpio, pump.active_high)
            for pump in config.pumps
        }
        self.all_off()

    def on(self, pump_id: str) -> None:
        pump = self._get_pump(pump_id)
        with self._lock:
            if not self.config.allow_multiple:
                self._all_off_locked()
            self._outputs[pump.id].on()

    def off(self, pump_id: str) -> None:
        pump = self._get_pump(pump_id)
        with self._lock:
            self._outputs[pump.id].off()

    def all_off(self) -> None:
        with self._lock:
            self._all_off_locked()

    def pulse(self, pump_id: str, seconds: float) -> None:
        seconds = float(seconds)
        if seconds <= 0:
            raise ValueError("La durata deve essere maggiore di zero.")
        if seconds > self.config.max_manual_seconds:
            raise ValueError(
                f"La durata massima manuale e {self.config.max_manual_seconds:g} secondi."
            )

        self.on(pump_id)
        try:
            time.sleep(seconds)
        finally:
            self.off(pump_id)

    def snapshot(self) -> tuple[PumpState, ...]:
        with self._lock:
            return tuple(
                PumpState(
                    id=pump.id,
                    name=pump.name,
                    gpio=pump.gpio,
                    physical_pin=pump.physical_pin,
                    active=bool(self._outputs[pump.id].is_active),
                )
                for pump in self.config.pumps
            )

    def close(self) -> None:
        with self._lock:
            self._all_off_locked()
            for output in self._outputs.values():
                output.close()

    def _all_off_locked(self) -> None:
        # Idempotente: salta i device gia chiusi cosi la rete di sicurezza
        # (atexit del demone) non solleva se lo spegnimento e gia avvenuto.
        for output in self._outputs.values():
            if getattr(output, "closed", False):
                continue
            output.off()

    def _get_pump(self, pump_id: str) -> PumpConfig:
        try:
            return self._pumps[pump_id]
        except KeyError as exc:
            valid = ", ".join(self._pumps)
            raise KeyError(f"Pompa non configurata: {pump_id}. Valide: {valid}") from exc

    def _build_output(self, pin: int, active_high: bool) -> Any:
        if self.mock:
            return MockOutput(pin, active_high=active_high)

        try:
            from gpiozero import OutputDevice
        except ImportError as exc:
            raise RuntimeError(
                "gpiozero non e installato. Installa i requisiti sul Raspberry oppure "
                "avvia con PUMP_MOCK=1 per la simulazione."
            ) from exc

        return OutputDevice(pin, active_high=active_high, initial_value=False)


def load_config(path: str | Path | None = None) -> ControllerConfig:
    if path is None:
        return DEFAULT_CONFIG

    config_path = Path(path)
    if not config_path.exists():
        return DEFAULT_CONFIG

    with config_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    pumps = tuple(_parse_pump(item) for item in data.get("pumps", []))
    if not pumps:
        pumps = DEFAULT_CONFIG.pumps

    config = ControllerConfig(
        pumps=pumps,
        allow_multiple=bool(data.get("allow_multiple", DEFAULT_CONFIG.allow_multiple)),
        max_manual_seconds=float(
            data.get("max_manual_seconds", DEFAULT_CONFIG.max_manual_seconds)
        ),
    )
    _validate_config(config)
    return config


def resolve_mock_mode(explicit_mock: bool | None = None) -> bool:
    if explicit_mock is not None:
        return explicit_mock

    env_value = os.getenv("PUMP_MOCK")
    if env_value is not None:
        return parse_bool(env_value)

    return platform.system() != "Linux"


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ValueError(f"Valore booleano non valido: {value!r}")


def _parse_pump(data: dict[str, Any]) -> PumpConfig:
    return PumpConfig(
        id=str(data["id"]),
        name=str(data["name"]),
        gpio=int(data["gpio"]),
        physical_pin=int(data["physical_pin"]),
        active_high=bool(data.get("active_high", False)),
    )


def _validate_config(config: ControllerConfig) -> None:
    ids = [pump.id for pump in config.pumps]
    gpios = [pump.gpio for pump in config.pumps]
    if len(ids) != len(set(ids)):
        raise ValueError("ID pompa duplicati nella configurazione.")
    if len(gpios) != len(set(gpios)):
        raise ValueError("GPIO duplicati nella configurazione.")
    if config.max_manual_seconds <= 0:
        raise ValueError("max_manual_seconds deve essere maggiore di zero.")
