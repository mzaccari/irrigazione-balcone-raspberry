# Software test pompe

Primo controllo manuale per le tre pompe dell'irrigazione balcone.

## Pin usati

| Pompa | GPIO BCM | Pin fisico | Nota |
| --- | ---: | ---: | --- |
| Pompa 1 | 17 | 11 | Zona acqua alta |
| Pompa 2 | 27 | 13 | Zona media |
| Pompa 3 | 22 | 15 | Zona secca |

Il comando dei MOSFET e attivo alto: GPIO alto = pompa accesa.

## Installazione sul Raspberry

```bash
cd /percorso/del/progetto
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Avvio

Simulazione, utile su PC o prima prova senza GPIO:

```bash
PUMP_MOCK=1 streamlit run software/streamlit_app.py
```

GPIO reali sul Raspberry:

```bash
PUMP_MOCK=0 streamlit run software/streamlit_app.py --server.address 0.0.0.0 --server.port 8501
```

Da un altro dispositivo sulla stessa rete apri:

```text
http://IP_DEL_RASPBERRY:8501
```

Per trovare l'IP sul Raspberry:

```bash
hostname -I
```

## Prima sequenza di test

1. Avvia l'interfaccia in simulazione e verifica che i pulsanti cambino stato.
2. Avvia in modalita GPIO reale con i serbatoi pieni o le pompe pronte a girare a secco solo per tempi brevissimi.
3. Usa prima `Impulso` a 0.5-1 secondo su una pompa alla volta.
4. Se qualcosa non torna, premi `STOP TUTTO` e togli alimentazione 12 V alle pompe.

La configurazione tiene `allow_multiple=false`, quindi un'accensione spegne automaticamente le altre pompe.
