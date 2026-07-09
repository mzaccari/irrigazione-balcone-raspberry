#!/bin/bash
LOG=/home/mzaccari/pi-temp.csv
if [ ! -f "$LOG" ]; then echo "timestamp,temp_c,throttled,load1" > "$LOG"; fi
TS=$(date -Iseconds)
TEMP=$(vcgencmd measure_temp | grep -oE '[0-9.]+')
THR=$(vcgencmd get_throttled | cut -d= -f2)
LOAD=$(cut -d' ' -f1 /proc/loadavg)
echo "$TS,$TEMP,$THR,$LOAD" >> "$LOG"