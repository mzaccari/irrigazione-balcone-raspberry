"""Astrazione dell'orologio, iniettabile per rendere testabile la logica a tempo.

Tutto il resto del software chiede l'ora tramite un oggetto Clock invece di
chiamare datetime.now() direttamente: cosi nei test possiamo usare un
FixedClock e controllare esattamente lo scorrere del tempo (nessun timer reale,
nessuna dipendenza dall'ora della macchina).

L'ora e sempre "local wall-clock" nel fuso configurato (default Europe/Rome):
e cio che intende l'utente quando scrive "07:00" per un programma.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


DEFAULT_TZ = "Europe/Rome"


class Clock:
    """Orologio reale: restituisce l'ora corrente nel fuso indicato."""

    def __init__(self, tz: str = DEFAULT_TZ) -> None:
        self._tz_name = tz
        self._tz = ZoneInfo(tz)

    @property
    def tz(self) -> ZoneInfo:
        return self._tz

    @property
    def tz_name(self) -> str:
        return self._tz_name

    def now(self) -> datetime:
        return datetime.now(self._tz)


class FixedClock(Clock):
    """Orologio controllabile per i test: istante fisso, avanzabile a mano."""

    def __init__(self, current: datetime, tz: str = DEFAULT_TZ) -> None:
        super().__init__(tz)
        self._current = self._localize(current)

    def _localize(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=self._tz)
        return value.astimezone(self._tz)

    def now(self) -> datetime:
        return self._current

    def set(self, current: datetime) -> None:
        self._current = self._localize(current)

    def advance(self, seconds: float) -> None:
        self._current = self._current + timedelta(seconds=seconds)
