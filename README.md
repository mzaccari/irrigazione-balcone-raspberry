# Irrigazione balcone Raspberry

Progetto per controllare tre pompe 12 V tramite Raspberry Pi Zero 2 e moduli MOSFET.

Il repository contiene:

- schema elettrico del cablaggio a 12 V;
- lista materiali per montaggio e manutenzione;
- primo software Python/Streamlit per test manuali delle pompe dalla rete locale.

## Stato attuale

Hardware montato e alimentato:

- LED Raspberry acceso;
- LED moduli MOSFET accesi;
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

I MOSFET sono comandati in active high: GPIO alto = pompa accesa.

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

## File principali

- `software/streamlit_app.py`: interfaccia web minimale.
- `software/pump_controller.py`: controllo pompe con GPIO reali o mock.
- `software/pumps.json`: configurazione GPIO e durata massima impulso.
- `software/README.md`: istruzioni operative dettagliate.
- `outputs/`: schema e lista spesa esportati.
