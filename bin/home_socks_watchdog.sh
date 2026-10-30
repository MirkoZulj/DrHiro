#!/usr/bin/env bash
# home-socks watchdog: verify the tunnel egress is NOT the datacenter IP.
# Compares against the VPS's own public IP so an ISP IP rotation doesn't
# cause a permanent restart loop.
set -uo pipefail
EXPECTED_IP="89.172.48.184"   # historical home IP (informational only)

SELF_IP=$(timeout 10 curl -s -m 8 https://api.ipify.org 2>/dev/null | tr -d '[:space:]')
OUT=$(timeout 15 curl -s -m 12 --socks5-hostname 127.0.0.1:1080 https://api.ipify.org 2>/dev/null | tr -d '[:space:]')

if [ -z "$OUT" ]; then
  logger -t home-socks "watchdog: tunnel dead (no egress) — restarting"
  systemctl restart home-socks
  exit 1
fi

if [ -n "$SELF_IP" ] && [ "$OUT" = "$SELF_IP" ]; then
  logger -t home-socks "watchdog: egress '$OUT' is the datacenter IP, not home — restarting"
  systemctl restart home-socks
  exit 1
fi

logger -t home-socks "watchdog: ok (egress=$OUT, dc=$SELF_IP, historic_home=$EXPECTED_IP)"
exit 0
