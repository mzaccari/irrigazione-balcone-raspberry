#!/bin/bash
# Network liveness watchdog per irrigatore: riavvia il Pi se la rete sparisce a lungo.
MAX_FAILS=4
GENTLE_AT=2
INTERVAL=300
FAILS=0
while true; do
  GW=$(ip route 2>/dev/null | awk '/default/{print $3; exit}')
  if [ -n "$GW" ] && ping -c 2 -W 5 "$GW" >/dev/null 2>&1; then
    FAILS=0
  else
    FAILS=$((FAILS+1))
    logger -t net-watchdog "rete assente ($FAILS/$MAX_FAILS) gw=${GW:-none}"
    if [ "$FAILS" -eq "$GENTLE_AT" ]; then
      logger -t net-watchdog "restart NetworkManager"
      systemctl restart NetworkManager 2>/dev/null || systemctl restart networking 2>/dev/null
    fi
    if [ "$FAILS" -ge "$MAX_FAILS" ]; then
      logger -t net-watchdog "REBOOT per perdita rete prolungata"
      sync
      systemctl reboot
    fi
  fi
  sleep "$INTERVAL"
done