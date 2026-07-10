"""Interfaccia Irrigazione: editor dei programmi + monitor dello stato.

Questa app NON comanda i GPIO (su Raspberry un solo processo puo possederli).
Il proprietario delle pompe e il demone (daemon.py). Qui:
- si leggono lo stato live (runtime/state.json) e lo storico (runtime/events.jsonl);
- si inviano comandi manuali accodandoli in runtime/commands/;
- si modificano i programmi e i serbatoi scrivendo programs.json.

Avvio (due processi, in simulazione su PC):
    PUMP_MOCK=1 python software/daemon.py
    PUMP_MOCK=1 streamlit run software/streamlit_app.py
"""

from __future__ import annotations

import time
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

import streamlit as st

import paths
import scheduler
import store
from pump_controller import load_config


DAEMON_STALE_SECONDS = 6.0
DEFAULT_TANK_LITERS = 25.0
DEFAULT_FLOW_LPH = 600.0


# --- Accesso a stato / comandi / configurazione -----------------------------

def read_state() -> dict | None:
    return store.read_json_or(paths.STATE_JSON, None)


def send_command(command: dict) -> None:
    store.enqueue_command(paths.COMMANDS_DIR, command)


def send_and_refresh(command: dict, toast: str | None = None) -> None:
    send_command(command)
    if toast:
        st.toast(toast)
    time.sleep(0.7)  # lascia al demone il tempo di elaborare (~1 tick)
    st.rerun()


def load_programs() -> dict:
    data = store.read_json_or(paths.PROGRAMS_JSON, {"options": {}, "pumps": {}})
    data.setdefault("options", {})
    data.setdefault("pumps", {})
    return data


def save_programs(data: dict) -> None:
    store.write_json_atomic(paths.PROGRAMS_JSON, data)
    send_command({"type": "reload"})


def hardware_pumps():
    return load_config(str(paths.PUMPS_JSON)).pumps


def pump_entry(programs: dict, pump_id: str) -> dict:
    entry = programs["pumps"].setdefault(pump_id, {})
    entry.setdefault("tank_liters", DEFAULT_TANK_LITERS)
    entry.setdefault("flow_lph", DEFAULT_FLOW_LPH)
    entry.setdefault("programs", [])
    return entry


def daemon_status(state: dict | None) -> tuple[bool, float | None]:
    if not state or "updated_at" not in state:
        return (False, None)
    try:
        updated = datetime.fromisoformat(state["updated_at"])
    except (ValueError, TypeError):
        return (False, None)
    tz = updated.tzinfo or ZoneInfo("Europe/Rome")
    age = (datetime.now(tz) - updated).total_seconds()
    return (age <= DAEMON_STALE_SECONDS, age)


# --- Formattazione ----------------------------------------------------------

def fmt_dt(iso: str | None, with_date: bool = False) -> str:
    if not iso:
        return "-"
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return str(iso)
    return dt.strftime("%d/%m %H:%M" if with_date else "%H:%M:%S")


def parse_times(text: str) -> list[str]:
    out: list[str] = []
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        parts = token.split(":")
        if len(parts) != 2:
            raise ValueError(token)
        hh, mm = int(parts[0]), int(parts[1])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError(token)
        out.append(f"{hh:02d}:{mm:02d}")
    if not out:
        raise ValueError("nessun orario valido")
    return sorted(set(out))


def program_summary(prg: dict, flow_lph: float) -> str:
    start = prg.get("start_date", "?")
    end = prg.get("end_date")
    fine = f"al {end}" if end else "per sempre"
    times = ", ".join(prg.get("times", []))
    dur = int(prg.get("duration_s", 0))
    liters = scheduler.liters_for_duration(flow_lph, dur)
    return f"Dal {start} {fine} · ore **{times}** · {dur}s (~{liters:.1f} L a erogazione)"


def show_status_badge(active: bool) -> None:
    label = "ON" if active else "OFF"
    color = "#b91c1c" if active else "#166534"
    background = "#fee2e2" if active else "#dcfce7"
    st.markdown(
        f"""
        <div style="width:96px;height:38px;display:flex;align-items:center;
        justify-content:center;border-radius:6px;color:{color};
        background:{background};font-weight:700;
        border:1px solid rgba(0,0,0,0.08);">{label}</div>
        """,
        unsafe_allow_html=True,
    )


# --- Sezioni ----------------------------------------------------------------

def render_header(state: dict | None, alive: bool, age: float | None) -> None:
    cols = st.columns([3, 1])
    with cols[0]:
        st.title("Irrigazione balcone")
    with cols[1]:
        if st.button("🔄 Aggiorna", use_container_width=True):
            st.rerun()

    if state is None:
        st.error(
            "Demone non ancora avviato: nessuno stato disponibile. "
            "Avvialo con `python software/daemon.py` (o `PUMP_MOCK=1 ...` in simulazione)."
        )
        return

    mode = "SIMULAZIONE (mock)" if state.get("mock") else "GPIO reale"
    if not alive:
        eta = f"{age:.0f}s fa" if age is not None else "mai"
        st.error(
            f"**DEMONE NON ATTIVO** (ultimo aggiornamento {eta}). "
            "I comandi verranno accodati ma non eseguiti finche il demone non riparte."
        )
    else:
        st.caption(f"Modo: {mode} · demone attivo (aggiornato {age:.0f}s fa) · tz {state.get('timezone', '?')}")

    warnings = state.get("warnings") or []
    if warnings:
        last = warnings[-1]
        st.warning(f"Ultimo avviso: {last.get('message', '')} ({fmt_dt(last.get('at'))})")


def render_manual(state: dict | None, alive: bool) -> None:
    pumps = hardware_pumps()
    pstate = (state or {}).get("pumps", {})
    current = (state or {}).get("current_run")

    top = st.columns([3, 1])
    with top[0]:
        if current:
            end_txt = f" fino alle {fmt_dt(current.get('ends_at'))}" if current.get("ends_at") else ""
            st.info(f"In corso: **{current.get('pump_id')}** ({current.get('source')}){end_txt}")
        else:
            st.caption("Nessuna erogazione in corso.")
    with top[1]:
        if st.button("STOP TUTTO", type="primary", use_container_width=True):
            send_and_refresh({"type": "stop_all"}, "Stop inviato")

    if not alive:
        st.info("Demone non attivo: i pulsanti sono disabilitati (i comandi non verrebbero eseguiti).")

    for pump in pumps:
        live = pstate.get(pump.id, {})
        active = bool(live.get("active", False))
        with st.container(border=True):
            cols = st.columns([2.4, 0.8, 1, 1, 1.6])
            with cols[0]:
                st.subheader(pump.name)
                st.caption(f"GPIO{pump.gpio} · pin {pump.physical_pin}")
            with cols[1]:
                show_status_badge(active)
            with cols[2]:
                if st.button("Accendi", key=f"on-{pump.id}", disabled=active or not alive,
                             use_container_width=True):
                    send_and_refresh({"type": "on", "pump": pump.id}, f"{pump.name}: accendi")
            with cols[3]:
                if st.button("Spegni", key=f"off-{pump.id}", disabled=not active or not alive,
                             use_container_width=True):
                    send_and_refresh({"type": "off", "pump": pump.id}, f"{pump.name}: spegni")
            with cols[4]:
                secs = st.number_input("Secondi", min_value=1, max_value=120, value=3,
                                       step=1, key=f"secs-{pump.id}")
                if st.button("Impulso", key=f"pulse-{pump.id}", disabled=not alive,
                             use_container_width=True):
                    send_and_refresh({"type": "pulse", "pump": pump.id, "seconds": int(secs)},
                                     f"{pump.name}: impulso {secs}s")


def render_programs(state: dict | None) -> None:
    programs = load_programs()
    st.caption(
        "Ogni programma vale **ogni giorno** tra la data d'inizio e la fine "
        "(o per sempre), agli orari indicati, per la durata scelta."
    )

    for pump in hardware_pumps():
        entry = pump_entry(programs, pump.id)
        flow = float(entry.get("flow_lph", DEFAULT_FLOW_LPH))
        st.subheader(pump.name)

        progs = entry.get("programs", [])
        if not progs:
            st.caption("Nessun programma.")
        for i, prg in enumerate(list(progs)):
            cols = st.columns([0.9, 4, 0.9])
            with cols[0]:
                enabled = st.toggle("Attivo", value=bool(prg.get("enabled", True)),
                                    key=f"en-{pump.id}-{i}")
                if enabled != bool(prg.get("enabled", True)):
                    prg["enabled"] = enabled
                    save_programs(programs)
                    st.rerun()
            with cols[1]:
                mark = "" if prg.get("enabled", True) else " _(disattivato)_"
                st.markdown(program_summary(prg, flow) + mark)
            with cols[2]:
                if st.button("Elimina", key=f"del-{pump.id}-{i}"):
                    progs.pop(i)
                    save_programs(programs)
                    st.rerun()

        with st.expander(f"➕ Aggiungi programma a {pump.name}"):
            _render_add_program_form(programs, entry, pump.id, flow)
        st.divider()

    with st.expander("⚙️ Impostazioni avanzate"):
        _render_options_form(programs)


def _render_add_program_form(programs: dict, entry: dict, pump_id: str, flow: float) -> None:
    with st.form(f"add-{pump_id}", clear_on_submit=True):
        c1, c2 = st.columns(2)
        start = c1.date_input("Data inizio", value=date.today(), key=f"start-{pump_id}")
        forever = c2.checkbox("Per sempre", value=True, key=f"forever-{pump_id}")
        end = c2.date_input("Data fine", value=date.today(), disabled=forever, key=f"end-{pump_id}")
        times_text = st.text_input("Orari (HH:MM, separati da virgola)", value="07:00",
                                   key=f"times-{pump_id}")
        duration = st.number_input("Durata erogazione (secondi)", min_value=1, max_value=1200,
                                   value=30, step=5, key=f"dur-{pump_id}")
        st.caption(f"~{scheduler.liters_for_duration(flow, int(duration)):.1f} L per erogazione "
                   f"(portata {flow:.0f} L/h).")
        submitted = st.form_submit_button("Aggiungi programma")

    if not submitted:
        return
    try:
        times = parse_times(times_text)
    except ValueError as exc:
        st.error(f"Orario non valido: {exc}")
        return
    if not forever and end < start:
        st.error("La data di fine e precedente all'inizio.")
        return

    entry["programs"].append({
        "id": uuid.uuid4().hex[:8],
        "enabled": True,
        "start_date": start.isoformat(),
        "end_date": None if forever else end.isoformat(),
        "times": times,
        "duration_s": int(duration),
    })
    save_programs(programs)
    st.success("Programma aggiunto.")
    st.rerun()


def _render_options_form(programs: dict) -> None:
    opts = programs.setdefault("options", {})
    with st.form("options-form"):
        catch_up = st.number_input(
            "Finestra di recupero (minuti)", min_value=0, max_value=180,
            value=int(opts.get("catch_up_minutes", 5)),
            help="Se il demone era spento all'orario previsto, avvia comunque se il ritardo e entro questi minuti.",
        )
        max_run = st.number_input(
            "Durata massima per erogazione (secondi)", min_value=10, max_value=7200,
            value=int(opts.get("max_run_seconds", 1200)),
            help="Tetto di sicurezza: nessuna pompa resta accesa oltre questo tempo.",
        )
        tz = st.text_input("Fuso orario", value=str(opts.get("timezone", "Europe/Rome")))
        if st.form_submit_button("Salva impostazioni"):
            opts["catch_up_minutes"] = int(catch_up)
            opts["max_run_seconds"] = int(max_run)
            opts["timezone"] = tz.strip() or "Europe/Rome"
            save_programs(programs)
            st.success("Impostazioni salvate.")
            st.rerun()


def render_tanks(state: dict | None, alive: bool) -> None:
    programs = load_programs()
    pstate = (state or {}).get("pumps", {})
    st.caption("I livelli sono una **stima** (portata × tempo), non una misura reale. "
               "Segnala il riempimento quando riempi il serbatoio.")

    for pump in hardware_pumps():
        entry = pump_entry(programs, pump.id)
        live = pstate.get(pump.id, {})
        capacity = float(entry.get("tank_liters", DEFAULT_TANK_LITERS))
        water = live.get("water_liters")
        next_run = live.get("next_run")

        st.subheader(pump.name)
        cols = st.columns([2, 1, 1.2])
        with cols[0]:
            float_info = live.get("float") or {}
            if float_info.get("empty_latched"):
                st.error("**VUOTO (galleggiante)** — la pompa e bloccata finche non "
                         "segnali il riempimento.")
            if water is not None and capacity > 0:
                frac = max(0.0, min(1.0, water / capacity))
                st.progress(frac, text=f"{water:.1f} / {capacity:.0f} L ({frac * 100:.0f}%)")
            else:
                st.caption("Nessuna stima disponibile (demone non avviato).")
            st.caption(f"Prossimo avvio: {fmt_dt(next_run, with_date=True)}")
        with cols[1]:
            if st.button("Serbatoio riempito", key=f"fill-{pump.id}", disabled=not alive,
                         use_container_width=True):
                send_and_refresh({"type": "refill", "pump": pump.id}, f"{pump.name}: pieno")
            level = st.number_input("Imposta livello (L)", min_value=0.0, max_value=float(capacity),
                                    value=float(water if water is not None else capacity),
                                    step=1.0, key=f"lvl-{pump.id}")
            if st.button("Imposta livello", key=f"setlvl-{pump.id}", disabled=not alive,
                         use_container_width=True):
                send_and_refresh({"type": "refill", "pump": pump.id, "liters": float(level)})
        with cols[2]:
            new_cap = st.number_input("Capacita serbatoio (L)", min_value=1.0, max_value=1000.0,
                                      value=capacity, step=1.0, key=f"cap-{pump.id}")
            new_flow = st.number_input("Portata (L/h)", min_value=1.0, max_value=5000.0,
                                       value=float(entry.get("flow_lph", DEFAULT_FLOW_LPH)),
                                       step=10.0, key=f"flow-{pump.id}")
            st.caption("Calibrazione: misura i litri erogati in 30 s e moltiplica ×120 per i L/h.")
            if st.button("Salva serbatoio", key=f"savetank-{pump.id}", use_container_width=True):
                entry["tank_liters"] = float(new_cap)
                entry["flow_lph"] = float(new_flow)
                save_programs(programs)
                st.success("Configurazione serbatoio salvata.")
                st.rerun()
        st.divider()


_EVENT_LABELS = {
    "programma_on": "Avvio programmato",
    "impulso_on": "Impulso manuale",
    "manuale_on": "Accensione manuale",
    "manuale_off": "Spegnimento manuale",
    "run_off": "Erogazione conclusa",
    "stop_all": "STOP TUTTO",
    "saltato_acqua": "Saltato: acqua insufficiente",
    "saltato_fuori_finestra": "Saltato: fuori finestra",
    "riempito": "Serbatoio riempito",
    "comando_ignoto": "Comando ignoto",
    "pulse_invalido": "Impulso non valido",
    "decisione_dose": "Decisione dose",
    "saltato_umidita": "Saltato: terreno gia umido",
    "saltato_meteo": "Saltato: pioggia prevista",
    "saltato_batteria": "Saltato: batteria critica",
    "saltato_serbatoio_vuoto": "Saltato: serbatoio vuoto",
    "serbatoio_vuoto": "Galleggiante: serbatoio VUOTO",
    "batteria_bassa": "Batteria bassa",
    "config_avviso": "Avviso configurazione",
    "lettura_live": "Lettura live sensori",
}


def render_sensors(state: dict | None, alive: bool) -> None:
    """Pannello read-only di sensori/meteo/batteria + helper di calibrazione.

    La UI non tocca MAI GPIO/I2C/seriale: il demone campiona e scrive
    state.json, qui si legge e al massimo si accodano comandi (sensor_live)
    o si salvano i punti di calibrazione in programs.json.
    """
    programs = load_programs()
    pstate = (state or {}).get("pumps", {})

    for warning in (state or {}).get("config_warnings") or []:
        st.warning(f"Config: {warning}")

    # --- Meteo e batteria (globali) ---
    cols = st.columns(2)
    with cols[0]:
        st.markdown("**Meteo (Open-Meteo)**")
        weather_state = (state or {}).get("weather")
        if isinstance(weather_state, dict):
            st.caption(
                f"{weather_state.get('date', '?')} · ET0 {weather_state.get('et0_mm', '?')} mm · "
                f"pioggia {weather_state.get('rain_mm', '?')} mm "
                f"(prob {weather_state.get('rain_prob', '?')}%) · "
                f"Tmax {weather_state.get('tmax_c', '?')} °C"
            )
        else:
            st.caption("Disabilitato o nessun dato (vedi `weather` in programs.json).")
    with cols[1]:
        st.markdown("**Batteria / pannello (VE.Direct)**")
        power_state = (state or {}).get("power")
        if isinstance(power_state, dict):
            volt = power_state.get("battery_v")
            panel = power_state.get("panel_w")
            batt_state = power_state.get("state", "?")
            volt_txt = f"{volt:.2f} V" if isinstance(volt, (int, float)) else "nessun dato"
            panel_txt = f" · pannello {panel:.0f} W" if isinstance(panel, (int, float)) else ""
            charge = power_state.get("charge_state")
            charge_txt = f" · {charge}" if charge else ""
            line = f"{volt_txt}{panel_txt}{charge_txt} · stato: **{batt_state}**"
            if batt_state in ("bassa", "critica"):
                st.error(line)
            else:
                st.caption(line)
        else:
            st.caption("Disabilitato (vedi `power` in programs.json).")

    # --- Lettura live per calibrazione ---
    live_until = (state or {}).get("live_sampling_until")
    live_on = False
    if live_until:
        try:
            until = datetime.fromisoformat(live_until)
            live_on = datetime.now(until.tzinfo) < until
        except (ValueError, TypeError):
            live_on = False
    cols = st.columns([1.4, 3])
    with cols[0]:
        if st.button("Avvia lettura live (60 s)", disabled=not alive,
                     use_container_width=True):
            send_and_refresh({"type": "sensor_live", "seconds": 60},
                             "Lettura live avviata")
    with cols[1]:
        if live_on:
            st.info(f"Lettura live attiva fino alle {fmt_dt(live_until)}: i valori raw "
                    "si aggiornano ad ogni tick, premi 🔄 Aggiorna per rileggerli.")
        else:
            st.caption("Per calibrare: avvia la lettura live, metti la sonda in aria "
                       "(secco) o in un bicchiere d'acqua (bagnato) e cattura il raw.")

    st.divider()

    # --- Per pompa: galleggiante, umidita, ultima decisione ---
    for pump in hardware_pumps():
        live = pstate.get(pump.id, {})
        entry = pump_entry(programs, pump.id)
        sensors_cfg = entry.setdefault("sensors", {})
        st.subheader(pump.name)

        info_cols = st.columns([1.4, 1])
        with info_cols[0]:
            float_info = live.get("float")
            if isinstance(float_info, dict):
                if float_info.get("empty_latched"):
                    st.error("Galleggiante: **serbatoio VUOTO** (latch attivo — si sblocca "
                             "con 'Serbatoio riempito' nel tab Serbatoi)")
                elif float_info.get("water_present") is False:
                    st.warning("Galleggiante: livello basso (debounce in corso...)")
                elif float_info.get("water_present") is None:
                    st.warning("Galleggiante configurato ma lettura non disponibile")
                else:
                    st.success(f"Galleggiante ok (GPIO{float_info.get('gpio')}): acqua presente")
            else:
                st.caption("Nessun galleggiante configurato (`sensors.float` in programs.json).")
        with info_cols[1]:
            exposed = st.toggle(
                "Zona esposta alla pioggia", value=bool(sensors_cfg.get("rain_exposed", False)),
                key=f"rain-{pump.id}",
                help="Se attiva, il meteo puo saltare l'irrigazione quando e prevista pioggia.",
            )
            if exposed != bool(sensors_cfg.get("rain_exposed", False)):
                sensors_cfg["rain_exposed"] = exposed
                save_programs(programs)
                st.rerun()

        moisture_live = live.get("moisture") or []
        moisture_cfg = sensors_cfg.get("moisture") or []
        if not moisture_cfg and not moisture_live:
            st.caption("Nessun sensore di umidita configurato (`sensors.moisture`).")
        for reading in moisture_live:
            sensor_id = reading.get("id", "?")
            raw = reading.get("raw")
            percent = reading.get("percent")
            note = reading.get("note")
            row = st.columns([2, 1, 1, 1])
            with row[0]:
                if percent is not None:
                    st.progress(max(0.0, min(1.0, percent / 100.0)),
                                text=f"{sensor_id}: {percent:.0f}% (raw {raw})")
                else:
                    detail = f" — {note}" if note else ""
                    st.warning(f"{sensor_id}: non utilizzabile (raw {raw}){detail}")
                st.caption(f"Ultima lettura: {fmt_dt(reading.get('at'))}")
            cfg_item = next((c for c in moisture_cfg
                             if isinstance(c, dict) and c.get("id") == sensor_id), None)
            with row[1]:
                if st.button("Usa come SECCO", key=f"dry-{pump.id}-{sensor_id}",
                             disabled=raw is None or cfg_item is None,
                             use_container_width=True):
                    cfg_item["raw_dry"] = int(raw)
                    save_programs(programs)
                    st.success(f"{sensor_id}: raw_dry = {raw}")
                    st.rerun()
            with row[2]:
                if st.button("Usa come BAGNATO", key=f"wet-{pump.id}-{sensor_id}",
                             disabled=raw is None or cfg_item is None,
                             use_container_width=True):
                    cfg_item["raw_wet"] = int(raw)
                    save_programs(programs)
                    st.success(f"{sensor_id}: raw_wet = {raw}")
                    st.rerun()
            with row[3]:
                if cfg_item is not None:
                    st.caption(f"secco {cfg_item.get('raw_dry', 0)} · "
                               f"bagnato {cfg_item.get('raw_wet', 0)}")

        last_decision = live.get("last_decision")
        if isinstance(last_decision, dict):
            reasons = "; ".join(last_decision.get("reasons") or []) or "dose piena"
            st.caption(
                f"Ultima decisione ({fmt_dt(last_decision.get('at'), with_date=True)}): "
                f"×{last_decision.get('multiplier')} → "
                f"{last_decision.get('effective_s')}s su {last_decision.get('base_s')}s — {reasons}"
            )
        st.divider()


def render_history() -> None:
    events = store.read_events(paths.EVENTS_JSONL, limit=300)
    if not events:
        st.caption("Nessun evento registrato.")
        return
    rows = []
    for ev in reversed(events):
        extra = {k: v for k, v in ev.items() if k not in ("at", "type", "pump_id")}
        rows.append({
            "Ora": fmt_dt(ev.get("at"), with_date=True),
            "Evento": _EVENT_LABELS.get(ev.get("type", ""), ev.get("type", "")),
            "Pompa": ev.get("pump_id", ""),
            "Dettagli": ", ".join(f"{k}={v}" for k, v in extra.items()),
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)


# --- Entry point ------------------------------------------------------------

st.set_page_config(page_title="Irrigazione balcone", layout="wide")

state = read_state()
alive, age = daemon_status(state)

render_header(state, alive, age)

tab_manuale, tab_programmi, tab_serbatoi, tab_sensori, tab_storico = st.tabs(
    ["Manuale", "Programmi", "Serbatoi", "Sensori", "Storico"]
)
with tab_manuale:
    render_manual(state, alive)
with tab_programmi:
    render_programs(state)
with tab_serbatoi:
    render_tanks(state, alive)
with tab_sensori:
    render_sensors(state, alive)
with tab_storico:
    render_history()
