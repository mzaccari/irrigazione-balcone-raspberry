# Upgrade sensori, meteo e batteria — progetto e razionale

Stato: progettato il 2026-07-10 (sistema base in esercizio, partenza per le vacanze).
Software sviluppato in mock sul branch `feature/sensori` (Fase 5). Hardware al rientro (Fasi 6–8).
**Niente viene deployato sul Pi prima del rientro: il sistema in funzione non si tocca.**

Obiettivo: aggiungere al sistema attuale (schedule orario fisso + stima acqua open-loop)
tre ingressi reali — **galleggianti nei serbatoi, sensori di umidità del terreno, meteo** —
più un quarto emerso in corso d'opera: **stato batteria/pannello via VE.Direct**.
Il tutto con un vincolo esplicito: sostenibilità economica (~70–80 € totali).

---

## 1. Il problema dell'umidità: perché NON inseguire i valori "scientifici"

La preoccupazione di partenza era: *"bisognerebbe tradurre il dato di umidità in un dato
confrontabile con le evidenze scientifiche disponibili per la cura delle piante"*.
La risposta onesta è: **per vasi da balcone è la strada sbagliata**, per tre motivi.

1. **I sensori capacitivi economici non misurano un'unità fisica.** Restituiscono una
   tensione correlata alla costante dielettrica del mezzo attorno alla sonda. Nessuna
   calibrazione di fabbrica, variabilità da esemplare a esemplare, sensibilità alla
   densità del terriccio, alla temperatura e a come è stata infilata la sonda.
2. **La letteratura agronomica parla un'altra lingua.** Le soglie scientifiche sono in
   VWC (contenuto idrico volumetrico, %) o in tensione matriciale (kPa), calibrate per
   *quel* suolo. Il terriccio universale di ogni vaso è diverso, e cambia pure
   invecchiando.
3. **Alla pianta non serve il numero assoluto.** Serve che l'acqua arrivi prima dello
   stress. Per deciderlo bastano riproducibilità e trend, non la comparabilità con un
   paper.

### La soluzione: scala relativa per-sensore

Ogni sensore viene calibrato in situ con due punti registrati in config:

- `raw_dry`: lettura ADC con sonda **in aria** (o nel vaso al punto "va innaffiato");
- `raw_wet`: lettura con sonda nel vaso **appena innaffiato a fondo** (o in un bicchiere
  d'acqua per il primo setup).

Il software mappa linearmente in un **indice di umidità 0–100 %** relativo a quel
sensore in quel vaso. Le soglie decisionali si tarano poi *osservando*: "quando il vaso
del basilico legge sotto il 35 % inizia ad afflosciarsi" → soglia 40. In due o tre
settimane di log (`decisione_dose` + trend in `runtime/sensors.jsonl`) le soglie
convergono. È la trasformazione di un problema agronomico in un problema di controllo.

### Il ponte scientifico, per chi lo vuole (opzionale)

Se un giorno si vogliono soglie "da letteratura", esiste la via amatoriale del metodo
gravimetrico, con una bilancia da cucina:

1. pesare un vaso di riferimento con terriccio **asciutto in stufa/sole** (`P_secco`);
2. saturarlo d'acqua, lasciarlo drenare 30–60 min, ripesare (`P_cc` = capacità di campo);
3. `VWC ≈ (P − P_secco) / V_vaso` ad ogni pesata intermedia, da correlare alla lettura raw.

Con 3–4 punti (aria secca, metà, capacità di campo, saturazione) si costruisce la curva
raw→VWC e i concetti FAO-56 diventano usabili: capacità di campo del terriccio da vaso
tipicamente 35–45 % VWC, irrigare quando si è consumato il 30–50 % dell'acqua
disponibile (MAD, *management allowed depletion*: ~30 % per aromatiche assetate, ~50 %
per mediterranee). Non è richiesto per far funzionare il sistema: è documentato qui per
curiosità e futuri raffinamenti.

**Il dato scientifico vero entra da un'altra porta, gratis: l'evapotraspirazione di
riferimento (ET0) calcolata con FAO-56 Penman-Monteith**, fornita da Open-Meteo per le
nostre coordinate. È il numero che l'agronomia usa davvero per dimensionare
l'irrigazione, e non richiede alcuna taratura locale (vedi §4).

---

## 2. Il "male minore": 3 pompe, ≥18 piante, 1–2 sensori per zona

Un sensore campiona UN vaso; la pompa ne bagna 6+. La domanda era: media pesata?
Scegliere il male minore? La risposta è in tre principi.

### 2.1 Il rischio è asimmetrico → si sbaglia verso il bagnato

D'estate, in vaso: la siccità uccide in **giorni**, l'eccesso d'acqua (con vasi drenanti)
danneggia in **settimane**. Quindi ogni ambiguità si risolve a favore dell'erogazione.
Questo è il "male minore" formalizzato: la funzione di perdita non è simmetrica.

### 2.2 Vaso indicatore + aggregazione MIN

- Il sensore va nel **vaso indicatore** della zona: quello che asciuga prima
  (il più esposto, il più piccolo, la pianta più assetata). È il canarino nella miniera:
  se lui sta bene, gli altri stanno meglio.
- Con 2+ sensori per zona si aggrega col **MIN** (il più secco comanda), NON con una
  media pesata: la media diluisce proprio il segnale della pianta a rischio. Il secondo
  sensore serve anche da sanity-check (se i due divergono stabilmente → ricalibrare).

### 2.3 I sensori MODULANO lo schedule, non lo sostituiscono

Lo schedule attuale (orari + durate calibrate) funziona ed è collaudato: resta la spina
dorsale. Il motore decisionale calcola solo un **moltiplicatore della durata
programmata**:

| Indice umidità zona (MIN) | Moltiplicatore | Significato |
|---|---|---|
| ≥ `skip_above` | 0.0 | Molto umido: salta (evento `saltato_umidita`) |
| ≥ `reduce_above` | 0.5 | Umido: mezza dose |
| normale | 1.0 | Dose piena programmata |
| ≤ `boost_below` | 1.25 | Molto secco: dose aumentata |

Il moltiplicatore finale (umidità × meteo × batteria) è comunque limitato a **[0, 1.5]**
e la durata effettiva non supera mai `max_run_seconds`. L'eccesso d'acqua sulle piante
non-monitorate è quindi limitato per costruzione: al massimo ricevono la dose che già
ricevono oggi, +25 % nei giorni estremi.

### 2.4 Fail-safe: un sensore morto non deve MAI far morire le piante

- Lettura mancante, fuori dalla banda di plausibilità o stantia → quel contributo vale
  **1.0 (neutro)** con motivo registrato. Sensori tutti guasti = si torna esattamente al
  comportamento attuale.
- Meteo irraggiungibile o cache vecchia → neutro.
- Galleggiante cablato **chiuso-verso-GND = acqua presente**: filo rotto o connettore
  staccato si legge come "vuoto" → blocco pompa + notifica. Il guasto è visibile, non
  silenzioso.
- Ogni decisione emette l'evento `decisione_dose` con TUTTI gli input (raw, %, ET0,
  pioggia, tensione batteria, motivi in italiano): si può sempre rispondere a
  "perché ieri la zona 2 ha saltato?".

### Soglie iniziali proposte (da tarare osservando)

| Zona | Piante | skip_above | reduce_above | boost_below | Note |
|---|---|---|---|---|---|
| pompa_1 | Piante Grandi | 80 | 65 | 25 | |
| pompa_2 | Aromatiche (basilico, menta…) | 85 | 70 | 35 | assetate: soglie più caute |
| pompa_3 | Oleandri e ibisco | 75 | 55 | 15 | mediterranee: tollerano il secco |

---

## 3. Architettura software (Fase 5, già implementata in mock)

Stessa filosofia delle Fasi 0–3: **logica pura testabile + I/O isolato + mock ovunque**,
demone unico proprietario dell'hardware, UI che legge solo `state.json`.

```
                    ┌────────────────────────────────────────────┐
                    │                daemon.py (tick 0.5 s)       │
 galleggianti ────▶ │ _service_floats   ──▶ latch "serbatoio      │
 (GPIO 23/24/25)    │                        vuoto" + stop pompa  │
                    │ _start_due_if_any:                          │
 ADS1115 (I2C) ───▶ │   umidità (MIN) ─┐                          │
                    │   meteo (cache) ─┼─▶ decision.py ──▶ durata │
 weather.json ────▶ │   batteria ──────┘   (moltiplicatore)  eff. │
                    │ _service_weather ──▶ thread fetch 1×/giorno │
 VE.Direct (USB) ─▶ │ _service_power   ──▶ stato batteria         │
                    │ _service_trend   ──▶ sensors.jsonl (15 min) │
                    │ _service_heartbeat ─▶ ntfy 1×/giorno        │
                    └────────────────────────────────────────────┘
```

Moduli nuovi:

| Modulo | Ruolo | Rete/HW nel tick? |
|---|---|---|
| `sensors.py` | Galleggianti (gpiozero input), ADS1115 (driver smbus2 minimale), debounce puro, normalizzazione | GPIO/I2C locali, letture ~0.2 s solo a pompe ferme |
| `decision.py` | Motore decisionale PURO + parsing/validazione config sensori | no |
| `weather.py` | Open-Meteo (ET0, pioggia, Tmax) via urllib, cache `runtime/weather.json` | fetch SOLO in thread separato |
| `notify.py` | ntfy.sh con coda bounded + worker thread, heartbeat giornaliero | POST SOLO nel worker |
| `power.py` | Parser VE.Direct puro + lettore seriale in thread + stato batteria con isteresi | seriale SOLO nel thread |

Regole invariate e difese:
- il tick non fa MAI rete/seriale; le letture I2C avvengono solo a pompe ferme;
- un solo scrittore per file (il thread meteo scrive solo `weather.json`, atomico);
- config retro-compatibile: sezioni assenti o `enabled=false` = comportamento odierno
  identico bit-per-bit;
- i latch "serbatoio vuoto" sopravvivono al riavvio del demone (`Restart=always`);
- validazione config RUMOROSA: GPIO in conflitto, soglie invertite, canali ADC errati →
  evento `config_avviso` + banner nella UI (niente più errori silenziosi).

Eventi nuovi in `events.jsonl`: `decisione_dose`, `saltato_umidita`, `saltato_meteo`,
`saltato_batteria`, `saltato_serbatoio_vuoto`, `serbatoio_vuoto`, `batteria_bassa`,
`config_avviso`; reason `stop_galleggiante` nei `run_off`.

### Config (`programs.json`, tutte le novità nascono DISABILITATE)

```jsonc
{
  "options": { "...": "invariato" },
  "weather": {
    "enabled": false,
    "latitude": 45.46, "longitude": 9.19,      // impostare le proprie!
    "rain_skip_mm": 5.0, "rain_prob_min": 80,
    "et0_low": 2.0, "et0_high": 5.0,
    "fetch_hour": "05:30", "max_age_hours": 30
  },
  "notify": {
    "enabled": false, "backend": "ntfy",
    // topic ntfy: NON qui (file versionato!) ma in env NOTIFY_NTFY_TOPIC
    "events": ["serbatoio_vuoto", "saltato_serbatoio_vuoto", "saltato_acqua",
                "saltato_batteria", "batteria_bassa", "config_avviso"],
    "heartbeat_time": "20:30"
  },
  "power": {
    "enabled": false, "serial_port": "/dev/ttyUSB0",
    "v_low": 12.6, "v_critical": 12.3,
    "hold_minutes": 10, "stale_seconds": 120
  },
  "pumps": {
    "pompa_1": {
      "tank_liters": 25, "flow_lph": 10, "programs": ["...invariato..."],
      "sensors": {
        "rain_exposed": false,                  // per-zona (balcone misto!)
        "float": { "gpio": 23, "debounce_s": 10, "reserve_liters": 1.0 },
        "moisture": [
          { "id": "p1_m1", "adc": { "addr": "0x48", "channel": 0 },
            "raw_dry": 0, "raw_wet": 0 }        // 0/0 = non calibrato = ignorato
        ],
        "thresholds": { "skip_above": 80, "reduce_above": 65, "boost_below": 25 }
      }
    }
  }
}
```

---

## 4. Meteo: Open-Meteo, gratuito e senza chiave

Una chiamata al giorno (ora configurabile, default 05:30, prima del run delle 06:30) a
`api.open-meteo.com` per: **ET0 FAO-56** (mm), pioggia prevista oggi (mm e probabilità),
T max. Cache atomica in `runtime/weather.json`; se la cache è più vecchia di
`max_age_hours` → neutro.

Effetto sul moltiplicatore:
- `ET0 < et0_low` (giornata fresca/umida) → ×0.8
- `ET0 > et0_high` (caldo torrido) → ×1.2
- pioggia prevista ≥ `rain_skip_mm` (o probabilità ≥ `rain_prob_min`) **e la zona ha
  `rain_exposed: true`** → salta. Il balcone è misto: il flag è per-zona, le zone
  coperte non saltano mai per pioggia (salterebbero l'acqua restando asciutte).

Riferimento estivo per Milano: ET0 ≈ 4–6 mm/giorno nelle settimane calde, 2–3 in quelle
fresche. Le soglie 2.0/5.0 sono un buon punto di partenza.

---

## 5. Galleggianti: la verità sul fondo del serbatoio

La stima attuale (portata × tempo) è open-loop: parte da "pieno" e deriva nel tempo.
Il galleggiante dà la **verità al punto basso**:

- interruttore a galleggiante (tipo ZP4510 o simile) montato a ~2–3 cm dal fondo, alla
  quota della **riserva** che si vuole proteggere (default 1 L, configurabile);
- cablaggio: un filo al GPIO (23/24/25), l'altro a GND, **contatto chiuso = acqua
  presente** (pull-up interno del Pi). Rottura filo = "vuoto" = fail-safe;
- debounce di 10 s contro lo sciabordio (la pompa in funzione muove l'acqua): il
  serbatoio è "vuoto" solo dopo 10 s CONSECUTIVI di lettura vuota;
- allo scatto: **latch** persistente "serbatoio vuoto" → la pompa in funzione si ferma
  subito (`stop_galleggiante`), gli avvii programmati vengono saltati
  (`saltato_serbatoio_vuoto`), parte la notifica, e la stima `water_liters` viene
  riconciliata al valore di riserva (il drift accumulato si azzera);
- il latch si sgancia SOLO col comando "Serbatoio riempito" dalla UI (se il serbatoio è
  ancora vuoto, il galleggiante ri-scatta 10 s dopo). Un avvio manuale con latch attivo
  procede con warning (filosofia attuale): serve come via di test dopo un riempimento
  fisico.

Bonus futuro (Fase 8): tra un `riempito` e il `serbatoio_vuoto` successivo il sistema
conosce i litri reali consumati (capacità − riserva) e i secondi di pompa accumulati →
può **ricalibrare da solo `flow_lph`**.

---

## 6. Batteria e pannello: VE.Direct dal Victron MPPT 75/10

Idea aggiunta in fase di progetto: l'MPPT ha la porta **VE.Direct** (seriale TTL 3,3 V,
19200 baud, protocollo testuale pubblicato da Victron) che trasmette da sola, una volta
al secondo: tensione batteria (`V`, mV), corrente (`I`, mA), potenza pannello (`PPV`, W),
stato di carica (`CS`: bulk/absorption/float). Collegandola a una USB del Pi si ottiene
il monitoraggio energia **in sola lettura, senza toccare l'impianto di potenza**.

Cavo: adattatore USB-UART a **3,3 V** (CP2102/FT232) + connettore JST-PH 2.0 mm 4 pin.
Bastano **2 fili: GND e TX(Victron)→RX(adattatore)** — non si trasmette nulla verso
l'MPPT, rischio zero. (Alternativa pigra: cavo ufficiale VE.Direct-USB, ~30 €.)
⚠ Non collegare MAI il pin di alimentazione del connettore VE.Direct all'adattatore.

Uso nel motore decisionale (protezione del Pi da hard switch-off del BMS, che
corromperebbe la SD):

| Stato batteria (con isteresi + persistenza `hold_minutes`) | Moltiplicatore |
|---|---|
| ok (V > 12.6) | 1.0 |
| **bassa** (V ≤ 12.6) | 0.5 — modalità risparmio |
| **critica** (V ≤ 12.3) | 0.0 — salta (`saltato_batteria`) + notifica |
| dati assenti/stantii | 1.0 neutro + warning (il cavo staccato non ferma l'irrigazione) |

Le soglie sono in config: la curva LiFePO4 è piatta al centro, quindi conta soprattutto
la persistenza (10 min) e l'isteresi (+0.15 V per rientrare). Nota: il boost siccità
non scavalca mai il risparmio batteria (composizione moltiplicativa: 1.25 × 0.5 < 1).
La telemetria (V, W pannello, stato) entra nel trend, nello `state.json` (UI) e
nell'heartbeat serale.

---

## 7. Notifiche: ntfy.sh

Scelto ntfy.sh: zero registrazione, app gratuita iOS/Android, si sceglie un
**topic segreto** (es. `irrigatore-<stringa-casuale>`) e il demone pubblica con una
POST JSON. Il topic sta SOLO in una variabile d'ambiente sul Pi (drop-in systemd non
versionato):

```bash
sudo mkdir -p /etc/systemd/system/irrigatore-daemon.service.d
sudo tee /etc/systemd/system/irrigatore-daemon.service.d/notify.conf <<'EOF'
[Service]
Environment=NOTIFY_NTFY_TOPIC=irrigatore-CAMBIAMI-stringa-lunga-casuale
EOF
sudo systemctl daemon-reload && sudo systemctl restart irrigatore-daemon
```

Arrivano: gli eventi in allowlist (serbatoio vuoto, salti, batteria, config errata) e un
**heartbeat giornaliero** ("tutto ok: livelli serbatoi + giorni residui stimati, ultima
irrigazione, umidità zone, batteria/pannello"). Il mancato arrivo dell'heartbeat è esso
stesso un segnale (Pi spento o senza rete → il net-watchdog già installato ci prova da
solo). L'invio è su thread con coda bounded: non blocca e non fa mai crashare il tick.

---

## 8. Hardware: BOM e cablaggio

| Voce | Q.tà | ~€ | Note |
|---|---|---|---|
| Galleggiante NC/NO (es. ZP4510) | 3 | 12 | GPIO 23/24/25 + GND, pull-up interno |
| ADS1115 breakout I2C 16 bit | 1 | 5 | addr 0x48; un 2° a 0x49 per futuri 2 sensori/zona |
| DFRobot SEN0308 waterproof | 3 | 40 | out max ~2,9 V < 3,3 V → diretto in ADS1115, PGA 4.096 |
| USB-UART 3,3 V + JST-PH 2.0 | 1 | 6–8 | VE.Direct, 2 fili, sola lettura |
| Cavetteria, JST, passacavi IP | — | 10 | |
| **Totale** | | **~73–75** | |

Opzionali Fase 8: INA219 (~3 €, corrente pompe → rileva pompa morta/a secco),
BME280 (~5 €, T/UR locale), webcam USB (foto giornaliera su ntfy).

Assegnazione pin aggiornata:

| Pin BCM | Uso |
|---|---|
| 17 / 27 / 22 | Relè pompe 1/2/3 (invariato) |
| 23 / 24 / 25 | Galleggianti serbatoi 1/2/3 (input, pull-up) |
| 2 (SDA) / 3 (SCL) | I2C → ADS1115 |
| ~~3~~ → **26** | ⚠ eventuale pulsante shutdown: NON su GPIO3 (è SCL!) → `dtoverlay=gpio-shutdown,gpio_pin=26` (si perde il wake-da-pulsante, si riaccende togliendo/ridando corrente) |

Note elettriche:
- sensori SEN0308 alimentati a 3,3 V; cavi analogici corti e lontani dai cavi pompa;
  il rumore residuo è assorbito dall'oversampling (mediana di 16 letture);
- I2C: ADS1115 vicino al Pi (è il segnale analogico che può viaggiare, non l'I2C);
- brownout I2C su solare: ogni lettura è protetta (errore bus → neutro + warning,
  riapertura bus alla lettura successiva).

---

## 9. Fasi operative al rientro

**Fase 6 — galleggianti + rete (≈ mezz'ora + spesa)**
1. ⚠ Prima di ogni deploy: il flusso `git archive` SOVRASCRIVE `programs.json` sul Pi
   → ricatturare prima la config live nel repo (vedi avviso in `stato-deploy.md`).
2. Deploy del branch, `pip install -r requirements.txt` nel venv, restart servizi.
3. Cablare i 3 galleggianti (GPIO 23/24/25 + GND), configurare `sensors.float` per pompa.
4. Test: sollevare a mano il galleggiante 1 → dopo 10 s evento `serbatoio_vuoto`,
   pompa bloccata, notifica ntfy; "Serbatoio riempito" dalla UI → sblocco.
5. Attivare `weather.enabled` (con le proprie coordinate) e `notify.enabled` + drop-in
   env col topic. Verificare l'heartbeat serale.
6. Collegare il cavo VE.Direct, `power.enabled: true`, verificare V/W in UI.

**Fase 7 — umidità (pilota su pompa_2, poi le altre)**
1. `sudo raspi-config nonint do_i2c 0`, `sudo apt install i2c-tools`,
   `i2cdetect -y 1` → deve comparire `48`.
2. Un SEN0308 nel vaso indicatore delle aromatiche, canale 0.
3. Taratura dalla UI (tab Sensori → "Lettura live"): cattura `raw_dry` (sonda in aria)
   e `raw_wet` (sonda in bicchiere d'acqua o vaso appena bagnato).
4. Qualche giorno in osservazione: solo eventi `decisione_dose` nel log, soglie
   prudenti. Poi stringere le soglie e replicare sulle altre due zone.

**Fase 8 — opzionali**: ricalibrazione automatica `flow_lph`, INA219, BME280, foto
giornaliera, posticipo intelligente dei run quando la batteria è bassa ma il pannello
sta caricando.

---

## 10. Idee aggiuntive emerse (oltre galleggiante/umidità/meteo)

- **Tailscale sul Pi** (gratis): accesso SSH + UI Streamlit da fuori casa, senza aprire
  porte sul router. Utilissimo in vacanza. 10 minuti di setup, zero manutenzione.
- **Previsione svuotamento** nell'heartbeat: `litri residui / consumo giornaliero da
  schedule` → "pompa 1: ~6 giorni di autonomia". Già implementata nell'heartbeat.
- **Ricalibrazione automatica della portata** dai cicli riempito→vuoto (Fase 8).
- **INA219** sul ramo 12 V pompe: una pompa che non assorbe corrente è morta o a secco,
  una che assorbe troppo è bloccata — l'unico guasto che oggi resta invisibile.
- **Foto giornaliera** (webcam USB → ntfy come allegato): il sensore definitivo di
  "stanno bene" è guardarle.
- Con i dati attuali (10–12 L/h reali): autonomia serbatoi ≈ 9 gg (p1), 12 gg (p2),
  30 gg (p3). Da tenere a mente per la durata delle assenze.
