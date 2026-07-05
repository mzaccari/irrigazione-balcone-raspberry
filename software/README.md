# Software irrigazione balcone

Controllo delle tre pompe con **programmazione oraria** e **stima dell'acqua nei
serbatoi**, gestibile da interfaccia web.

## Architettura (due processi)

Su Raspberry un solo processo puo possedere i GPIO, quindi il sistema e diviso:

- **`daemon.py`** — il demone: unico proprietario delle pompe. Fa girare lo
  scheduling anche a browser chiuso, stima l'acqua, protegge dalla marcia a
  secco. Loop a singolo thread: comandi manuali e avvii programmati passano tutti
  dallo stesso stato, quindi non si sovrappongono.
- **`streamlit_app.py`** — l'interfaccia web: NON comanda i GPIO. Modifica i
  programmi, mostra lo stato live e invia comandi manuali al demone.

I due processi si coordinano tramite file (nessun database, tutto ispezionabile):

| File | Chi scrive | Contenuto |
| --- | --- | --- |
| `pumps.json` | (fisso) | Hardware: id pompa, GPIO, pin fisico |
| `programs.json` | interfaccia | Programmi, capacita/portata serbatoi, opzioni |
| `runtime/state.json` | demone | Stato live: pompe on/off, acqua, prossimi avvii, heartbeat |
| `runtime/commands/` | interfaccia | Coda comandi manuali (un file per comando) |
| `runtime/events.jsonl` | demone | Storico avvii/spegnimenti/avvisi |

La cartella `runtime/` non e versionata (vedi `.gitignore`).

Moduli di supporto: `scheduler.py` (logica pura di scheduling + acqua, con clock
iniettabile), `store.py` (I/O atomico + coda), `clock.py`, `paths.py`.

## Pin usati

| Pompa | GPIO BCM | Pin fisico | Serbatoio | Nota |
| --- | ---: | ---: | ---: | --- |
| Pompa 1 | 17 | 11 | 25 L | Zona acqua alta |
| Pompa 2 | 27 | 13 | 25 L | Zona media |
| Pompa 3 | 22 | 15 | 20 L | Zona secca |

Comando relè **attivo basso**: GPIO basso = relè chiuso = pompa accesa.
`pumps.json` ha `active_high: false` per tutte e tre; `gpiozero` applica
l'inversione, quindi `on()`/`off()` restano invariati. Cablaggio scheda relè e
alimentazione: vedi il README principale.

## Installazione

```bash
cd /percorso/del/progetto
python -m venv .venv
source .venv/bin/activate          # su Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Avvio in simulazione (PC, senza GPIO)

Servono **due terminali** (mock automatico su Windows/non-Linux):

```bash
# terminale 1: il demone
PUMP_MOCK=1 python software/daemon.py

# terminale 2: l'interfaccia
PUMP_MOCK=1 streamlit run software/streamlit_app.py
```

## Avvio sul Raspberry (GPIO reali)

```bash
# demone (comanda le pompe)
PUMP_MOCK=0 python software/daemon.py

# interfaccia, raggiungibile dalla rete
PUMP_MOCK=0 streamlit run software/streamlit_app.py --server.address 0.0.0.0 --server.port 8501
```

Da un altro dispositivo: `http://IP_DEL_RASPBERRY:8501` (l'IP con `hostname -I`).

Per l'avvio automatico all'accensione usa i servizi systemd
`irrigatore-daemon.service` e `irrigatore-web.service` (istruzioni dentro i file).

## Uso dell'interfaccia

- **Manuale**: accendi/spegni/impulso per prova; `STOP TUTTO` ferma subito.
- **Programmi**: per ogni pompa, aggiungi programmi con data d'inizio, fine (o
  "per sempre"), uno o piu orari e durata. Valgono ogni giorno nell'intervallo.
- **Serbatoi**: livello stimato, "Serbatoio riempito" per azzerare il conteggio,
  configurazione di capacita e **portata** (con nota di calibrazione).
- **Storico**: ultimi eventi (avvii, spegnimenti, salti per acqua bassa).

### Nota sull'acqua

600 L/h = 10 L/min: un serbatoio da 25 L si svuota in ~2,5 minuti a portata
nominale. La stima e **portata × tempo**, non una misura reale. Con teste basse
(~1,5 m) e tubo sottile la portata reale e verosimilmente piu bassa: **calibra**
(misura i litri erogati in 30 s e moltiplica ×120 per i L/h) e inserisci il
valore nella tab Serbatoi. Se l'acqua stimata non basta, l'avvio programmato
viene **saltato e segnalato** (gli avvii manuali procedono con avviso).

## Test

Logica pura e simulazione del demone, tutto su PC senza hardware:

```bash
python -m pytest software/tests -q
```

## Sicurezza

- Stato sicuro (tutte spente) all'avvio e alla chiusura del demone.
- Tetto `max_run_seconds`: nessuna pompa resta accesa oltre il limite.
- `STOP TUTTO` interrompe subito; un comando manuale ha precedenza su un avvio
  programmato in corso.

### Checklist quando arriva il Raspberry (verifiche hardware)

1. Segui la sequenza di accensione del README principale (buck sotto carico, ecc.).
2. Avvia solo il demone in `PUMP_MOCK=0` e prova un `Impulso` di 0,5-1 s su una
   pompa alla volta con i serbatoi pieni.
3. **Crash-safety del relè**: con una pompa accesa, termina il demone
   bruscamente (`kill -9`) e verifica che la pompa si **spenga**. Se il tuo
   modulo relè resta attivo con la linea GPIO rilasciata, aggiungi un **watchdog
   hardware** (`/dev/watchdog`) che riavvia il Pi se il loop si blocca.
4. Verifica che, con il demone attivo, l'interfaccia NON provi a possedere i GPIO
   (qui non lo fa: usa solo la coda comandi e lo stato).
