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
}


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

tab_manuale, tab_programmi, tab_serbatoi, tab_storico = st.tabs(
    ["Manuale", "Programmi", "Serbatoi", "Storico"]
)
with tab_manuale:
    render_manual(state, alive)
with tab_programmi:
    render_programs(state)
with tab_serbatoi:
    render_tanks(state, alive)
with tab_storico:
    render_history()
