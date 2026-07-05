# Checklist di accensione hardware — irrigatore

**Regola d'oro:** il **bus 12 V delle pompe si collega per ULTIMO** e deve restare
sempre facile da staccare durante tutti i primi test. Un filo di potenza (COM/NO,
12 V) che tocca un GPIO **brucia il Pi all'istante**, indipendentemente dai buck.

Contesto: il Pi 4 gira headless (`ssh mzaccari@192.168.178.39`, hostname `pi4`).
Demone e web UI sono servizi systemd **attivi e `enabled` al boot** — dopo ogni
riavvio ripartono da soli in stato sicuro (pompe spente). UI:
<http://192.168.178.39:8501>.

---

## 0. Protezione al boot (già applicata)

Al boot i GPIO **17/27/22 partono BASSI** (pull-down di default) per ~2-3 s prima
che il demone li alzi. Con relè **active-low**, "basso = pompa accesa": col 12 V
collegato, le pompe partirebbero per qualche secondo a ogni riavvio.

**Fix applicato** in `/boot/firmware/config.txt` (attivo dal prossimo reboot):

```
gpio=17,27,22=op,dh
```

→ i pin nascono **alti** (relè aperti) fin dal primo istante di boot.
Backup del file originale: `config.txt.bak-20260705-213704`.

*(Opzionale, da valutare con pulsante fisico: `dtoverlay=gpio-shutdown` per
spegnimento/accensione pulita da un pulsantino tra pin 5 (GPIO3) e pin 6 (GND).)*

## A. A freddo — tutto scollegato

- [ ] 12 V staccato
- [ ] jumper **VCC–JD-VCC rimosso** dalla scheda relè
- [ ] multimetro pronto e stacco 12 V a portata di mano

## B. Test dei due buck (SENZA Pi né relè)

- [ ] Buck 1 (Pi) a vuoto: uscita **5.0–5.1 V** stabile
- [ ] Buck 2 (JD-VCC) a vuoto: uscita **5.0–5.1 V** stabile
- [ ] ripeti **sotto carico fittizio** (resistenza / vecchio USB), staccando e
      riattaccando il 12 V un paio di volte: nessun picco né calo anomalo

> È un buck non testato che ha bruciato il Pi Zero 2 W: non saltare questo passo.

## C. Solo Pi (Buck 1)

- [ ] collega **solo il Pi** al Buck 1, dai 12 V
- [ ] `ssh mzaccari@192.168.178.39` risponde
- [ ] `systemctl is-active irrigatore-daemon irrigatore-web` → `active active`

## D. Scheda relè (SENZA 12 V pompe) — a Pi acceso

Il demone tiene già i pin 17/27/22 alti (relè off): puoi collegare i segnali a caldo.

- [ ] GND comune (Pi ↔ relè ↔ buck)
- [ ] VCC (lato opto) ← **3.3 V del Pi (pin 1)**
- [ ] JD-VCC ← **Buck 2** (NON dal Pi)
- [ ] IN1 ← GPIO17 (pin 11) · IN2 ← GPIO27 (pin 13) · IN3 ← GPIO22 (pin 15)
- [ ] a riposo **tutti i LED relè spenti** (relè aperti). Se uno è acceso →
      pin/segnale sbagliato: **fermati**
- [ ] prova software (ancora senza pompe/12 V): impulso 1 s su pompa 1 dalla UI →
      relè 1 fa *click* e LED per 1 s, poi si spegne. Ripeti per pompa 2 e 3

## E. Pompe + 12 V (l'ultimo passo)

- [ ] **ricontrolla ogni filo**: IN solo verso i GPIO; nessun contatto tra COM/NO
      (12 V) e le linee di segnale
- [ ] potenza: `12V+ bus → COM → NO → pompa+`, `pompa- → GND bus`
- [ ] diodo **1N5819** in antiparallelo su ogni pompa (banda verso il +)
- [ ] mano sullo stacco 12 V, poi dai tensione
- [ ] **primo impulso reale 0,5–1 s, una pompa alla volta** dalla UI, pronto a staccare

## F. Test crash-safety (con relè + pompa)

- [ ] avvia una pompa, poi da SSH: `sudo systemctl kill -s KILL irrigatore-daemon`
- [ ] entro ~2-3 s la pompa **deve spegnersi**: systemd riavvia il demone
      (`Restart=always`) che riparte in stato sicuro
- [ ] se la pompa **resta accesa** oltre → serve **watchdog hardware**
      (`/dev/watchdog`): configurarlo prima di lasciare l'impianto autonomo

---

## Note di gestione

- **Riavvio:** `sudo reboot` (i servizi ripartono da soli). Non serve toccare
  l'alimentazione.
- **Spegnere:** `sudo poweroff`, attendi che il **LED verde** smetta di
  lampeggiare, *poi* stacca. Il Pi non ha pulsante: per riaccenderlo si stacca e
  riattacca la corrente (oppure pulsantino su pin 5–6).
- **Mai strappare la corrente** a Pi acceso: rischio di corruzione della microSD.
- **Log dal vivo:** `journalctl -u irrigatore-daemon -f`
