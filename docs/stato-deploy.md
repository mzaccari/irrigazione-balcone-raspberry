# Stato deploy — irrigatore (handoff tra dispositivi)

> File di riferimento per riprendere il lavoro da un altro PC. Aggiornato al 2026-07-10.
> Leggi anche [checklist-accensione.md](checklist-accensione.md), [upgrade-sensori.md](upgrade-sensori.md)
> e [../software/README.md](../software/README.md).

## In una riga

Controller irrigazione a 3 pompe 12 V su **Raspberry Pi 4**, modulo relè 8 canali
**active-low**. Software = **demone headless** (unico proprietario dei GPIO) +
**Streamlit** (editor/monitor via file, non tocca i GPIO). Branch di lavoro:
`feature/scheduler-pompe` (pushato su `origin`, parallelo a `main`).

## Accesso al Pi

| | |
|---|---|
| Host | `pi4` — IP `192.168.178.39` |
| Utente | `mzaccari` |
| SSH | `ssh mzaccari@192.168.178.39` |
| Chiave | login senza password dal PC fisso (NucBox). **Da un altro PC**: usa la password, oppure autorizza la sua chiave (`~/.ssh/id_ed25519.pub` → append in `~/.ssh/authorized_keys` sul Pi) |
| sudo | senza password |
| OS | Raspberry Pi OS / Debian 13 (Trixie), Python 3.13, aarch64 |

## Layout sul Pi

- Codice in **`/home/mzaccari/irrigatore`** (deployato via `git archive`, non è un clone git).
- venv in `.venv` creato con `--system-site-packages`.
- Dipendenze: **GPIO da apt** (`python3-gpiozero`, `python3-lgpio`), **Streamlit + pytest da pip** nel venv.
- Pin factory gpiozero: **lgpio** (impostato nel service).
- Runtime (stato, coda comandi, eventi) in `software/runtime/` (non versionato).

## Servizi systemd (attivi ed `enabled` al boot)

| Servizio | Fa | Note |
|---|---|---|
| `irrigatore-daemon.service` | demone pompe, unico owner GPIO | `PUMP_MOCK=0`, `GPIOZERO_PIN_FACTORY=lgpio` |
| `irrigatore-web.service` | UI Streamlit | su `0.0.0.0:8501`, dopo il demone |

Interfaccia web: **http://192.168.178.39:8501**

## Comandi utili

```bash
systemctl is-active irrigatore-daemon irrigatore-web   # stato
journalctl -u irrigatore-daemon -f                     # log demone dal vivo
sudo systemctl restart irrigatore-daemon               # riavvia demone
sudo reboot                                            # riavvia il Pi (servizi ripartono da soli)
sudo poweroff                                          # spegni (poi serve staccare/riattaccare corrente o pulsante pin5-6)
PUMP_MOCK=1 .venv/bin/python -m pytest software -q      # test (dalla dir ~/irrigatore)
```

Ridistribuire il codice dopo modifiche locali (dal PC di sviluppo):
```bash
git archive --format=tar <branch> | ssh mzaccari@192.168.178.39 'tar -xf - -C ~/irrigatore'
sudo systemctl restart irrigatore-daemon irrigatore-web   # sul Pi, dopo il redeploy
```

> ⚠ **Il deploy via `git archive` SOVRASCRIVE anche `software/programs.json` sul Pi.**
> Se sul Pi la config è stata modificata dalla UI (programmi, portate, livelli...),
> PRIMA del deploy ricattura la config live nel repo:
> `scp mzaccari@192.168.178.39:~/irrigatore/software/programs.json software/` → commit → poi deploy.
> (`runtime/` invece è al sicuro: non è versionato e l'archivio non lo contiene.)

## Hardware — mappa GPIO

| Pompa | GPIO BCM | Pin fisico | Serbatoio |
|---|---:|---:|---:|
| Pompa 1 "Piante Grandi" | 17 | 11 | 25 L |
| Pompa 2 "Piante aromatiche" | 27 | 13 | 25 L |
| Pompa 3 "oleandri e ibisco" | 22 | 15 | 30 L |

Relè **active-low**: GPIO basso = relè chiuso = pompa accesa. Portate **calibrate coi
gocciolatori: ~10–12 L/h** (i 600 nominali valgono solo a tubo aperto).

Pin riservati per la Fase 6+ (vedi [upgrade-sensori.md](upgrade-sensori.md)):
GPIO **23/24/25** galleggianti serbatoi, GPIO **2/3 (SDA/SCL)** ADS1115 umidità,
una USB per il cavo VE.Direct dal Victron.

## Protezione al boot (applicata)

In `/boot/firmware/config.txt`: `gpio=17,27,22=op,dh` → i pin dei relè nascono
ALTI (pompe spente) fin dall'avvio, evitando la finestra di ~2-3 s in cui i
pull-down di default li terrebbero bassi (= pompe accese). Backup:
`config.txt.bak-20260705-213704`. Attiva dal prossimo reboot.

## Stato lavori

**Fatto**
- Fasi 0-3 (store, scheduler, demone, UI) — logica pura, test pytest verdi.
- Fase 4 completa: hardware montato, **sistema in esercizio** con schedule reale
  (P1 06:30/20:00 ×480 s, P2 06:45/20:15 ×300 s, P3 07:00 ×360 s), portate calibrate,
  net-watchdog e log temperatura installati, config reale catturata nel repo (`8939df4`).
- Fix crash-safety spegnimento (`cfce715`), protezione boot config.txt, checklist e questo doc.
- **Fase 5 (branch `feature/sensori`)**: software sensori/meteo/batteria/notifiche
  completo e testato in mock (suite: 140 test verdi) — vedi
  [upgrade-sensori.md](upgrade-sensori.md). Tutte le novità nascono **disabilitate**
  in `programs.json`: il deploy del branch NON cambia il comportamento in esercizio.

**Da fare al rientro dalle vacanze (Fasi 6-7, hardware)** — dettagli in
[upgrade-sensori.md](upgrade-sensori.md) §9:
1. Acquisti (~75 €): 3 galleggianti, ADS1115, 3× DFRobot SEN0308, USB-UART 3,3 V per VE.Direct.
2. Fase 6: galleggianti su GPIO 23/24/25, ntfy (drop-in con `NOTIFY_NTFY_TOPIC`),
   meteo (coordinate in `programs.json`), cavo VE.Direct.
3. Fase 7: I2C on, ADS1115, sensore pilota su pompa 2 (aromatiche), taratura, poi 3 zone.
4. Test crash-safety relè ancora in sospeso: `sudo systemctl kill -s KILL irrigatore-daemon`
   → pompa spenta entro ~2-3 s (Restart=always). Se resta accesa → **watchdog hardware** (`/dev/watchdog`).
5. (Opzionale) pulsante on/off pulito: `dtoverlay=gpio-shutdown,gpio_pin=26` —
   ⚠ **NON su GPIO3/pin 5 come indicato in passato: GPIO3 è l'SCL dell'I2C** e
   collide con l'ADS1115. Su GPIO26 si perde il "wake da pulsante" (per riaccendere
   da spento: stacca/riattacca corrente), lo spegnimento pulito resta.
