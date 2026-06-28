from __future__ import annotations

import atexit
from datetime import datetime
from pathlib import Path

import streamlit as st

from pump_controller import PumpController, load_config


CONFIG_PATH = Path(__file__).with_name("pumps.json")


@st.cache_resource
def get_controller(config_path: str, config_mtime: float) -> PumpController:
    del config_mtime
    controller = PumpController(load_config(config_path))
    atexit.register(controller.close)
    return controller


def config_mtime(path: Path) -> float:
    return path.stat().st_mtime if path.exists() else 0.0


def mark_event(message: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    st.session_state["last_event"] = f"{now} - {message}"


def show_status_badge(active: bool) -> None:
    label = "ON" if active else "OFF"
    color = "#b91c1c" if active else "#166534"
    background = "#fee2e2" if active else "#dcfce7"
    st.markdown(
        f"""
        <div style="
            width: 96px;
            height: 38px;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 6px;
            color: {color};
            background: {background};
            font-weight: 700;
            border: 1px solid rgba(0, 0, 0, 0.08);
        ">{label}</div>
        """,
        unsafe_allow_html=True,
    )


st.set_page_config(
    page_title="Irrigazione - Test pompe",
    page_icon=None,
    layout="wide",
)

controller = get_controller(str(CONFIG_PATH), config_mtime(CONFIG_PATH))

st.title("Irrigazione - test pompe")

top_cols = st.columns([2, 1])
with top_cols[0]:
    mode = "SIMULAZIONE" if controller.mock else "GPIO reale"
    st.caption(f"Modo: {mode} | GPIO in numerazione BCM")
    if "last_event" in st.session_state:
        st.caption(st.session_state["last_event"])

with top_cols[1]:
    if st.button("STOP TUTTO", type="primary", use_container_width=True):
        controller.all_off()
        mark_event("tutte le pompe spente")
        st.rerun()

if controller.mock:
    st.info("SIMULAZIONE: nessun GPIO reale viene comandato.")

for pump in controller.snapshot():
    with st.container(border=True):
        cols = st.columns([2.4, 0.7, 1, 1, 1.4])

        with cols[0]:
            st.subheader(pump.name)
            st.caption(f"GPIO{pump.gpio} | pin fisico {pump.physical_pin}")

        with cols[1]:
            show_status_badge(pump.active)

        with cols[2]:
            if st.button(
                "Accendi",
                key=f"on-{pump.id}",
                disabled=pump.active,
                use_container_width=True,
            ):
                controller.on(pump.id)
                mark_event(f"{pump.name} accesa")
                st.rerun()

        with cols[3]:
            if st.button(
                "Spegni",
                key=f"off-{pump.id}",
                disabled=not pump.active,
                use_container_width=True,
            ):
                controller.off(pump.id)
                mark_event(f"{pump.name} spenta")
                st.rerun()

        with cols[4]:
            seconds = st.number_input(
                "Secondi",
                min_value=0.1,
                max_value=float(controller.config.max_manual_seconds),
                value=1.0,
                step=0.5,
                key=f"seconds-{pump.id}",
            )
            if st.button("Impulso", key=f"pulse-{pump.id}", use_container_width=True):
                with st.spinner(f"{pump.name} per {seconds:g}s"):
                    controller.pulse(pump.id, seconds)
                mark_event(f"{pump.name} impulso {seconds:g}s")
                st.rerun()
