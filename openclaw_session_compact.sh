#!/usr/bin/env bash
# Daily Linguastic/drHiro agent session compaction — keep last 5 user turns, clean up.
# Installed as root crontab "0 6 * * *".
set -u
LOG=/var/log/openclaw-session-compact.log
TS=$(date '+%Y-%m-%d %H:%M:%S')
echo "[$TS] starting daily session compaction" >> "$LOG"

docker exec drhiro-openclaw-gateway-1 sh -lc 'openclaw sessions compact agent:main:main --max-lines 40' >> "$LOG" 2>&1
docker exec drhiro-openclaw-gateway-1 sh -lc 'openclaw sessions cleanup --enforce --agent main' >> "$LOG" 2>&1

echo "[$TS] done" >> "$LOG"