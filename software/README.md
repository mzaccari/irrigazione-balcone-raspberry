# Software test pompe

Primo controllo manuale per le tre pompe dell'irrigazione balcone.

## Pin usati

| Pompa | GPIO BCM | Pin fisico | Nota |
| --- | ---: | ---: | --- |
| Pompa 1 | 17 | 11 | Zona acqua alta |
| Pompa 2 | 27 | 13 | Zona media |
| Pompa 3 | 22 | 15 | Zona secca |

Il comando del modulo relè e attivo basso: GPIO basso = relè chiuso = pompa accesa. `pumps.json` ha `active_high: false` per tutte e tre le pompe; `gpiozero` applica l'inversione (`OutputDevice(active_high=False, initial_value=False)`), quindi `on()`/`off()` restano invariati nel codice e nell'app.

Cablaggio scheda relè (8 canali, 3 usati): bus 12V+ -> COM -> NO -> pompa+, pompa- diretta al bus GND, diodo 1N5819 in antiparallelo su ogni pompa. Sul lato segnale: togli il jumper VCC-JD-VCC, VCC (opto) al 3.3V del Pi, JD-VCC (bobine) a un **secondo buck 12V->5V dedicato** (non al Pi, per non far passare la corrente delle bobine dal suo rail), GND comune, IN1/IN2/IN3 su GPIO17/27/22.

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

0. Testa entrambi i buck isolati e sotto carico (multimetro, 5.0-5.1V stabili) prima di collegare qualunque scheda: vedi checklist di accensione nel README principale. Poi verifica il cablaggio scheda relè a Pi spento: jumper VCC-JD-VCC rimosso, VCC dal 3.3V del Pi, JD-VCC dal secondo buck dedicato, IN1/2/3 su GPIO17/27/22. Accendi il Pi e controlla che tutti e tre i relè restino aperti (led IN spenti): e lo stato sicuro atteso all'avvio.
1. Avvia l'interfaccia in simulazione e verifica che i pulsanti cambino stato.
2. Avvia in modalita GPIO reale con i serbatoi pieni o le pompe pronte a girare a secco solo per tempi brevissimi.
3. Usa prima `Impulso` a 0.5-1 secondo su una pompa alla volta.
4. Se qualcosa non torna, premi `STOP TUTTO` e togli alimentazione 12 V alle pompe.

La configurazione tiene `allow_multiple=false`, quindi un'accensione spegne automaticamente le altre pompe.
