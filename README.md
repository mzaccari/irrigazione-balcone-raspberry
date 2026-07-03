# Irrigazione balcone Raspberry

Progetto per controllare tre pompe 12 V tramite Raspberry Pi 4 e modulo relè 8 canali.

Il repository contiene:

- schema elettrico del cablaggio a 12 V;
- lista materiali per montaggio e manutenzione;
- primo software Python/Streamlit per test manuali delle pompe dalla rete locale.

## Revisione hardware: da MOSFET a relè

Il cablaggio iniziale usava driver MOSFET (Adafruit 5648) con potenza passante dai cavetti JST a 3 pin: sotto il carico delle pompe la corrente causava una caduta di tensione (V+/GND crollava da 12 V a 3-7 V sui canali attivi) e rendeva l'impianto inaffidabile.

Fix adottato: sostituzione dei driver MOSFET con un **modulo relè 8 canali 5V optoisolato**, commutazione sul positivo (bus 12V+ -> COM -> NO -> pompa+, pompa- diretta al bus GND), diodo di ricircolo 1N5819 in antiparallelo su ogni pompa. Vedi `software/README.md` per il dettaglio del cablaggio segnale e la logica active-low.

## Revisione hardware: da Pi Zero 2 W a Pi 4, alimentazione su due buck distinti

Al primo tentativo di accensione della revisione a relè, il buck 12V->5V (non testato prima con un multimetro) ha danneggiato in modo permanente il Raspberry Pi Zero 2 W: acceso, spento dopo un istante, poi non più funzionante e solo caldo al tatto. Causa più probabile: sovratensione (transitorio o guasto del buck) sul rail 5V, dato che un buck singolo alimentava sia il Pi che, tramite i suoi pin, le bobine della scheda relè.

Fix adottato:

- **Raspberry Pi 4** al posto dello Zero 2 W (stesso GPIO a 40 pin, nessuna modifica al software: `gpiozero` e la numerazione BCM sono identiche su tutta la gamma Pi).
- **Due buck 12V->5V distinti**: Buck 1 alimenta solo il Pi 4 (5A, margine sopra il requisito ufficiale di 5V/3A); Buck 2 alimenta solo JD-VCC della scheda relè (bobine, circa 210 mA, ampiamente sovradimensionato a 3A). Le bobine del relè non passano più dal rail 5V del Pi.
- **VCC (lato optoisolatore) resta dal 3.3V del Pi** (pin 1): serve a far combaciare il livello logico con i GPIO, il prelievo di corrente e minimo e non e stato spostato sul secondo buck.

Vedi la sezione Sicurezza qui sotto per la procedura di verifica obbligatoria prima di ridare tensione.

## Stato attuale

Hardware in fase di rimontaggio con Raspberry Pi 4 e doppio buck, dopo il guasto del Pi Zero 2 W descritto sopra. Non ridare tensione senza aver eseguito la checklist di accensione nella sezione Sicurezza.

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

Cablaggio scheda relè: togliere il jumper VCC-JD-VCC. VCC (lato optoisolatore) al 3.3V del Pi (pin 1), JD-VCC (bobine) al **Buck 2 dedicato** (non al Pi), GND comune al bus. Con il jumper montato VCC e JD-VCC vanno in corto e il lato logico si ritrova a una tensione diversa da 3.3V.

### Checklist di accensione (obbligatoria dopo il guasto del Pi Zero 2 W)

Non collegare Pi o scheda relè prima di aver completato questi passaggi, nell'ordine:

1. **Testa ENTRAMBI i buck isolati, senza Pi ne relè collegati.** Batteria -> buck, multimetro sull'uscita: deve dare 5.0-5.1V stabili.
2. **Ritesta ciascun buck sotto un carico fittizio** (una resistenza o un vecchio carica-USB da qualche centinaio di mA), collegando e scollegando la batteria un paio di volte: verifica che non ci siano picchi ne cali anomali.
3. **Accendi solo il Buck 1 e collega solo il Pi 4.** Verifica che si avvii correttamente prima di procedere.
4. **Solo con il Pi gia acceso e stabile, collega la scheda relè** (segnale + Buck 2 per JD-VCC + 3.3V del Pi per VCC). Verifica il jumper VCC-JD-VCC rimosso prima di questo passaggio.
5. **Ricontrolla fisicamente ogni filo sulla morsettiera del relè**: IN1/IN2/IN3 solo verso i rispettivi GPIO, nessun contatto accidentale tra i morsetti COM/NO (12V) e le linee di segnale. Un filo di potenza finito su un GPIO e sufficiente a bruciare il Pi indipendentemente dai buck.

## File principali

- `software/streamlit_app.py`: interfaccia web minimale.
- `software/pump_controller.py`: controllo pompe con GPIO reali o mock.
- `software/pumps.json`: configurazione GPIO e durata massima impulso.
- `software/README.md`: istruzioni operative dettagliate.
- `outputs/`: schema e lista spesa esportati.
