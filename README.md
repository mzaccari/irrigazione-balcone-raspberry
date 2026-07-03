# Irrigazione balcone Raspberry

Progetto per controllare tre pompe 12 V tramite Raspberry Pi Zero 2 e modulo relè 8 canali.

Il repository contiene:

- schema elettrico del cablaggio a 12 V;
- lista materiali per montaggio e manutenzione;
- primo software Python/Streamlit per test manuali delle pompe dalla rete locale.

## Revisione hardware: da MOSFET a relè

Il cablaggio iniziale usava driver MOSFET (Adafruit 5648) con potenza passante dai cavetti JST a 3 pin: sotto il carico delle pompe la corrente causava una caduta di tensione (V+/GND crollava da 12 V a 3-7 V sui canali attivi) e rendeva l'impianto inaffidabile.

Fix adottato: sostituzione dei driver MOSFET con un **modulo relè 8 canali 5V optoisolato**, commutazione sul positivo (bus 12V+ -> COM -> NO -> pompa+, pompa- diretta al bus GND), diodo di ricircolo 1N5819 in antiparallelo su ogni pompa. Vedi `software/README.md` per il dettaglio del cablaggio segnale e la logica active-low.

## Stato attuale

Hardware montato e alimentato (revisione MOSFET, in fase di sostituzione con la scheda relè):

- LED Raspberry acceso;
- LED moduli driver accesi;
- pompe spente a riposo, comportamento atteso.

Il software iniziale permette di:

- accendere e spegnere una pompa a comando;
- inviare impulsi brevi temporizzati;
- spegnere tutto con un comando globale;
- usare una UI Streamlit accessibile dalla LAN.

## Mappa GPIO

| Pompa | GPIO BCM | Pin fisico | Zona |
| --- | ---: | ---: | --- |
| Pompa 1 | 17 | 11 | Acqua alta |
| Pompa 2 | 27 | 13 | Media |
| Pompa 3 | 22 | 15 | Secca |

Il modulo relè e comandato in active low: GPIO basso = relè chiuso = pompa accesa. L'inversione e gestita in `pumps.json` (`active_high: false`) e applicata da `gpiozero` (`OutputDevice(..., active_high=False, initial_value=False)`), quindi i metodi `on()`/`off()` nel codice restano invariati e allo stato logico "off" il pin fisico e HIGH (relè aperto) fin dall'avvio.

## Avvio rapido sul Raspberry

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
PUMP_MOCK=0 streamlit run software/streamlit_app.py --server.address 0.0.0.0 --server.port 8501
```

Poi aprire dalla stessa rete:

```text
http://IP_DEL_RASPBERRY:8501
```

Per trovare l'IP:

```bash
hostname -I
```

## Sicurezza

Questa UI comanda hardware reale. Usarla solo in rete locale, con alimentazione 12 V facilmente scollegabile durante i primi test.

Per il primo test usare impulsi da 0.5-1 secondo e una pompa alla volta.

Cablaggio scheda relè: togliere il jumper VCC-JD-VCC. VCC (lato optoisolatore) al 3.3V del Pi, JD-VCC (bobine) al 5V del Pi, GND comune. Con il jumper montato VCC e JD-VCC vanno in corto e il lato logico si ritrova a 5V invece di 3.3V.

## File principali

- `software/streamlit_app.py`: interfaccia web minimale.
- `software/pump_controller.py`: controllo pompe con GPIO reali o mock.
- `software/pumps.json`: configurazione GPIO e durata massima impulso.
- `software/README.md`: istruzioni operative dettagliate.
- `outputs/`: schema e lista spesa esportati.
