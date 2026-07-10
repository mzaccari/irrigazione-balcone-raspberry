"""Sensori di campo: galleggianti serbatoio e umidita terreno via ADS1115.

Stessa filosofia di pump_controller: classi hardware sottili con gemello mock,
piu funzioni PURE (normalizzazione, debounce) testabili senza hardware.

Fail-safe by design:
- galleggiante cablato chiuso-verso-GND = acqua presente (pull-up interno):
  filo rotto o connettore staccato si leggono come "vuoto";
- ogni errore di lettura restituisce None e il chiamante degrada (il debounce
  tratta None come vuoto -> guasto visibile; l'umidita None -> dose neutra).

Il mock si attiva come per le pompe: esplicito > env SENSOR_MOCK > auto fuori Linux.
"""

from __future__ import annotations

import os
import platform
import statistics
import time
from datetime import datetime
from typing import Any

from pump_controller import parse_bool


ADS1115_DEFAULT_ADDR = 0x48
_ADS_REG_CONVERSION = 0x00
_ADS_REG_CONFIG = 0x01
_ADS_SPS = 128            # sample rate configurato (DR=100)
PLAUSIBILITY_MARGIN = 0.15  # banda extra ammessa fuori da [raw_dry, raw_wet]


def resolve_sensor_mock(explicit_mock: bool | None = None) -> bool:
    """Mock dei sensori: esplicito > env SENSOR_MOCK > auto-mock fuori Linux."""
    if explicit_mock is not None:
        return explicit_mock
    env_value = os.getenv("SENSOR_MOCK")
    if env_value is not None:
        return parse_bool(env_value)
    return platform.system() != "Linux"


# --- Normalizzazione umidita (pura) ------------------------------------------

def moisture_percent(raw: int | None, raw_dry: int, raw_wet: int) -> float | None:
    """Mappa una lettura ADC grezza in indice di umidita 0-100 (relativo).

    Ritorna None se il sensore non e calibrato (raw_dry == raw_wet, incluso il
    default 0/0), se la lettura manca o se cade fuori dalla banda di
    plausibilita (guasto/scollegato): il chiamante deve degradare a neutro.
    Funziona con entrambe le polarita (capacitivi: raw_dry > raw_wet).
    """
    if raw is None or raw_dry == raw_wet:
        return None
    lo, hi = min(raw_dry, raw_wet), max(raw_dry, raw_wet)
    margin = (hi - lo) * PLAUSIBILITY_MARGIN
    if raw < lo - margin or raw > hi + margin:
        return None
    percent = (raw_dry - raw) / (raw_dry - raw_wet) * 100.0
    return max(0.0, min(100.0, percent))


# --- Debounce galleggiante (puro) ---------------------------------------------

class FloatDebounce:
    """Anti-sciabordio: "vuoto" solo dopo debounce_s SECONDI CONSECUTIVI.

    Basato sull'orologio iniettato (non sui tick), quindi identico nei test e
    sul Pi. `update` ritorna True UNA volta sola allo scatto; se l'acqua
    ricompare lo stato si riarma. None (errore di lettura) conta come vuoto.
    """

    def __init__(self, debounce_s: float) -> None:
        self.debounce_s = float(debounce_s)
        self.empty_since: datetime | None = None
        self._tripped = False

    def update(self, water_present: bool | None, now: datetime) -> bool:
        if water_present:
            self.empty_since = None
            self._tripped = False
            return False
        if self.empty_since is None:
            self.empty_since = now
            return False
        if not self._tripped and (now - self.empty_since).total_seconds() >= self.debounce_s:
            self._tripped = True
            return True
        return False


# --- Galleggianti (I/O) -------------------------------------------------------

class MockFloatInput:
    """Gemello mock di gpiozero.DigitalInputDevice per i galleggianti."""

    def __init__(self, pin: int) -> None:
        self.pin = pin
        self._active = True  # default: acqua presente (contatto chiuso a GND)
        self._closed = False

    @property
    def is_active(self) -> bool:
        if self._closed:
            raise RuntimeError("MockFloatInput chiuso: lettura non consentita")
        return self._active

    def set_water_present(self, present: bool) -> None:
        self._active = bool(present)

    def close(self) -> None:
        self._closed = True


class FloatSwitchBank:
    """Possiede gli input GPIO dei galleggianti (uno per pompa, opzionale).

    Contratto elettrico: contatto chiuso verso GND = acqua presente ->
    is_active True (pull-up interno). Circuito aperto = vuoto o filo rotto.
    """

    def __init__(self, gpio_by_pump: dict[str, int] | None = None,
                 mock: bool | None = None) -> None:
        self.mock = resolve_sensor_mock(mock)
        self._inputs: dict[str, Any] = {}
        self._gpio_by_pump: dict[str, int] = {}
        if gpio_by_pump:
            self.reconfigure(gpio_by_pump)

    def reconfigure(self, gpio_by_pump: dict[str, int]) -> None:
        """Applica una nuova mappa pompa->GPIO toccando solo cio che cambia."""
        for pump_id in list(self._inputs):
            if gpio_by_pump.get(pump_id) != self._gpio_by_pump.get(pump_id):
                try:
                    self._inputs[pump_id].close()
                except Exception:
                    pass
                del self._inputs[pump_id]
        for pump_id, gpio in gpio_by_pump.items():
            if pump_id not in self._inputs:
                self._inputs[pump_id] = self._build_input(gpio)
        self._gpio_by_pump = dict(gpio_by_pump)

    def water_present(self, pump_id: str) -> bool | None:
        """True/False dal galleggiante; None = non configurato o errore."""
        device = self._inputs.get(pump_id)
        if device is None:
            return None
        try:
            return bool(device.is_active)
        except Exception:
            return None  # guasto lettura: il debounce lo trattera' come vuoto

    def mock_input(self, pump_id: str) -> MockFloatInput:
        """Accesso al mock nei test."""
        device = self._inputs[pump_id]
        assert isinstance(device, MockFloatInput)
        return device

    def close(self) -> None:
        for device in self._inputs.values():
            try:
                device.close()
            except Exception:
                pass
        self._inputs.clear()

    def _build_input(self, pin: int) -> Any:
        if self.mock:
            return MockFloatInput(pin)
        try:
            from gpiozero import DigitalInputDevice
        except ImportError as exc:
            raise RuntimeError(
                "gpiozero non e installato. Installa i requisiti sul Raspberry "
                "oppure avvia con SENSOR_MOCK=1 per la simulazione."
            ) from exc
        # pull_up=True: stato attivo = pin a massa = galleggiante chiuso = acqua OK
        return DigitalInputDevice(pin, pull_up=True)


# --- ADS1115 (I2C) ------------------------------------------------------------

class MockAds1115:
    """ADC finto: valori raw impostabili per (addr, canale) nei test."""

    def __init__(self) -> None:
        self._values: dict[tuple[int, int], int] = {}
        self.fail = False  # simula bus I2C guasto

    def set_raw(self, addr: int, channel: int, raw: int | None) -> None:
        key = (addr, channel)
        if raw is None:
            self._values.pop(key, None)
        else:
            self._values[key] = int(raw)

    def read_single(self, addr: int, channel: int) -> int:
        if self.fail or (addr, channel) not in self._values:
            raise OSError("mock: canale ADC non disponibile")
        return self._values[(addr, channel)]

    def close(self) -> None:
        pass


class Ads1115:
    """Driver minimale ADS1115 su smbus2: letture single-shot, single-ended.

    PGA fisso a 4.096 V (i SEN0308 escono max ~2.9 V a 3.3 V di alimentazione),
    128 SPS. In caso di errore il bus viene chiuso e riaperto alla lettura
    successiva (i brownout su alimentazione solare possono incantare l'I2C).
    """

    def __init__(self, bus_number: int = 1) -> None:
        self.bus_number = bus_number
        self._bus: Any = None

    def _ensure_bus(self) -> Any:
        if self._bus is None:
            try:
                from smbus2 import SMBus
            except ImportError as exc:
                raise RuntimeError(
                    "smbus2 non e installato. `pip install smbus2` sul Raspberry "
                    "oppure avvia con SENSOR_MOCK=1 per la simulazione."
                ) from exc
            self._bus = SMBus(self.bus_number)
        return self._bus

    def read_single(self, addr: int, channel: int) -> int:
        if not 0 <= int(channel) <= 3:
            raise ValueError(f"canale ADS1115 non valido: {channel}")
        # OS=1 (avvia), MUX=100+ch (single-ended AINx), PGA=001 (4.096 V),
        # MODE=1 (single-shot), DR=100 (128 SPS), comparatore disabilitato.
        config = (
            (1 << 15)
            | ((0b100 + int(channel)) << 12)
            | (0b001 << 9)
            | (1 << 8)
            | (0b100 << 5)
            | 0b11
        )
        try:
            bus = self._ensure_bus()
            bus.write_i2c_block_data(addr, _ADS_REG_CONFIG, [config >> 8, config & 0xFF])
            time.sleep(1.0 / _ADS_SPS + 0.002)
            hi, lo = bus.read_i2c_block_data(addr, _ADS_REG_CONVERSION, 2)
        except Exception:
            self.close()  # riapre al prossimo giro
            raise
        raw = (hi << 8) | lo
        if raw > 0x7FFF:
            raw -= 0x10000
        return raw

    def close(self) -> None:
        if self._bus is not None:
            try:
                self._bus.close()
            except Exception:
                pass
            self._bus = None


class MoistureSensorBank:
    """Letture di umidita robuste: mediana di N campioni, None su errore."""

    def __init__(self, mock: bool | None = None) -> None:
        self.mock = resolve_sensor_mock(mock)
        self._adc: MockAds1115 | Ads1115 = MockAds1115() if self.mock else Ads1115()

    @property
    def mock_adc(self) -> MockAds1115:
        """Accesso all'ADC finto nei test."""
        assert isinstance(self._adc, MockAds1115)
        return self._adc

    def read_oversampled(self, addr: int, channel: int, samples: int = 16) -> int | None:
        values: list[int] = []
        for _ in range(max(1, int(samples))):
            try:
                values.append(self._adc.read_single(addr, channel))
            except Exception:
                return None  # bus/canale guasto: il chiamante degrada a neutro
        return int(statistics.median(values))

    def close(self) -> None:
        self._adc.close()
