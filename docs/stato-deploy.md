# Stato deploy — irrigatore (handoff tra dispositivi)

> File di riferimento per riprendere il lavoro da un altro PC. Aggiornato al 2026-07-05.
> Leggi anche [checklist-accensione.md](checklist-accensione.md) e [../software/README.md](../software/README.md).

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
git archive --format=tar feature/scheduler-pompe | ssh mzaccari@192.168.178.39 'tar -xf - -C ~/irrigatore'
sudo systemctl restart irrigatore-daemon irrigatore-web   # sul Pi, dopo il redeploy
```

## Hardware — mappa GPIO

| Pompa | GPIO BCM | Pin fisico | Serbatoio |
|---|---:|---:|---:|
| Pompa 1 | 17 | 11 | 25 L |
| Pompa 2 | 27 | 13 | 25 L |
| Pompa 3 | 22 | 15 | 20 L |

Relè **active-low**: GPIO basso = relè chiuso = pompa accesa. Portata 600 L/h **da calibrare**.

## Protezione al boot (applicata)

In `/boot/firmware/config.txt`: `gpio=17,27,22=op,dh` → i pin dei relè nascono
ALTI (pompe spente) fin dall'avvio, evitando la finestra di ~2-3 s in cui i
pull-down di default li terrebbero bassi (= pompe accese). Backup:
`config.txt.bak-20260705-213704`. Attiva dal prossimo reboot.

## Stato lavori

**Fatto**
- Fasi 0-3 (store, scheduler, demone, UI) — logica pura, 55 test pytest verdi.
- Fase 4 software: deploy sul Pi, dipendenze, servizi systemd, demone verificato su GPIO reali (pompe spente, nessun errore).
- Fix crash-safety spegnimento (`cfce715`), protezione boot config.txt, checklist e questo doc.

**Da fare (hardware, con l'utente sull'alimentazione)** — segui [checklist-accensione.md](checklist-accensione.md):
1. Montaggio: test doppio buck → solo Pi → scheda relè (senza 12 V) → 12 V pompe.
2. Primo impulso reale 0,5–1 s, una pompa alla volta.
3. Test crash-safety relè: `sudo systemctl kill -s KILL irrigatore-daemon` → pompa deve spegnersi entro ~2-3 s (Restart=always). Se resta accesa → **watchdog hardware** (`/dev/watchdog`).
4. Calibrazione portata reale.
5. (Opzionale) pulsante on/off pulito: `dtoverlay=gpio-shutdown` + pulsante momentaneo tra pin 5 (GPIO3) e pin 6 (GND).
