"""Percorsi standard dei file usati dal demone e dall'interfaccia.

I file di configurazione (versionati) stanno in software/.
Lo stato a runtime (non versionato, vedi .gitignore) sta in software/runtime/.
"""

from __future__ import annotations

from pathlib import Path


SOFTWARE_DIR = Path(__file__).resolve().parent

# Configurazione
PUMPS_JSON = SOFTWARE_DIR / "pumps.json"        # hardware (invariato)
PROGRAMS_JSON = SOFTWARE_DIR / "programs.json"  # programmi + serbatoi, editati dalla UI

# Runtime (scritto dal demone / coda comandi dalla UI)
RUNTIME_DIR = SOFTWARE_DIR / "runtime"
STATE_JSON = RUNTIME_DIR / "state.json"
COMMANDS_DIR = RUNTIME_DIR / "commands"
REJECTED_DIR = COMMANDS_DIR / "rejected"
EVENTS_JSONL = RUNTIME_DIR / "events.jsonl"
WEATHER_JSON = RUNTIME_DIR / "weather.json"    # cache meteo (scritta dal thread meteo)
SENSORS_JSONL = RUNTIME_DIR / "sensors.jsonl"  # trend sensori (umidita/galleggianti/batteria)
