#!/usr/bin/env bash
# Capture review evidence for the incremental artifact (disposable stack).
#
# Sanitised by construction: it records image NAMES/DIGESTS, migration revisions,
# table/column shapes, counts and measured outputs. It never prints credential
# values - only key names - and the stack's own credentials are throwaways.
#
# Usage: bash deploy/disposable/capture_evidence.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:-$HERE/../../docs/deliverables/meal-liquid-idempotency/review_bundle/evidence}"
DC=(docker compose -f "$HERE/docker-compose.isolated.yml" -p drhiro-iso)
mkdir -p "$OUT"

{
  echo "=== disposable stack evidence ($(date -u +%Y-%m-%dT%H:%M:%SZ)) ==="
  echo
  echo "--- image versions and digests (RepoTags / RepoDigests / Image ID) ---"
  "${DC[@]}" images 2>/dev/null || true
  for svc in postgres redis fake-telegram ingress openclaw mcp; do
    cid="$("${DC[@]}" ps -q "$svc" 2>/dev/null | head -1)"
    [ -n "$cid" ] || continue
    img="$(docker inspect -f '{{.Config.Image}}' "$cid" 2>/dev/null)"
    id="$(docker inspect -f '{{.Image}}' "$cid" 2>/dev/null)"
    printf '%s: image=%s image_id=%s\n' "$svc" "$img" "$id"
  done
  echo
  echo "--- container image digests (immutable references) ---"
  for svc in postgres redis fake-telegram ingress openclaw mcp; do
    cid="$("${DC[@]}" ps -q "$svc" 2>/dev/null | head -1)"
    [ -n "$cid" ] || continue
    printf '%s:\n' "$svc"
    docker image inspect --format '  RepoDigests={{.RepoDigests}} Id={{.Id}}' \
      "$(docker inspect -f '{{.Image}}' "$cid")" 2>/dev/null || true
  done
  echo
  echo "--- postgres server version ---"
  "${DC[@]}" exec -T postgres psql -U drhiro -d drhiro_t1 -tAc "select version();" 2>/dev/null
  echo
  echo "--- alembic migration revision (the REAL chain) ---"
  "${DC[@]}" exec -T postgres psql -U drhiro -d drhiro_t1 -tAc \
    "select version_num from alembic_version;" 2>/dev/null
  echo
  echo "--- trusted + T1 table inventory (names only) ---"
  "${DC[@]}" exec -T postgres psql -U drhiro -d drhiro_t1 -tAc \
    "select table_name from information_schema.tables where table_schema='public' order by 1;" 2>/dev/null
  echo
  echo "--- database assertions: real consumption output ---"
  "${DC[@]}" exec -T postgres psql -U drhiro -d drhiro_t1 -c \
    "select count(*) as meals from meals;
     select count(*) as meal_items from meal_items;
     select count(*) as measurements from measurements where source_provider='consumption';
     select count(*) as beverage_measurements from beverage_measurements;
     select count(*) as consumption_operations from consumption_operations;
     select totals_json from meals order by created_at desc limit 3;" 2>/dev/null
  echo
  echo "--- database assertions: trusted bookkeeping ---"
  "${DC[@]}" exec -T postgres psql -U drhiro -d drhiro_t1 -c \
    "select status, count(*) from telegram_receipts group by status;
     select reply_state, count(*) from reply_outbox group by reply_state;
     select action, from_state, to_state, count(*) from reply_audit group by 1,2,3;" 2>/dev/null
  echo
  echo "--- migration-created activities table shape (R4) ---"
  "${DC[@]}" exec -T postgres psql -U drhiro -d drhiro_t1 -tAc \
    "select column_name||':'||data_type||':'||is_nullable||':'||coalesce(column_default,'-')
       from information_schema.columns where table_name='activities' order by ordinal_position;" 2>/dev/null
  echo
  echo "--- ingress events (sanitised: no values, no bodies) ---"
  "${DC[@]}" logs ingress 2>&1 | grep -oE '"event": "[a-z_]+"' | sort | uniq -c | sort -rn
  echo
  echo "--- credential key NAMES visible to each container (never values) ---"
  for svc in ingress openclaw mcp; do
    cid="$("${DC[@]}" ps -q "$svc" 2>/dev/null | head -1)"
    [ -n "$cid" ] || continue
    echo "$svc:"
    docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$cid" 2>/dev/null \
      | cut -d= -f1 | sort | tr '\n' ' '
    echo
  done
} > "$OUT/incremental_evidence.txt" 2>&1

echo "written: $OUT/incremental_evidence.txt"
wc -l "$OUT/incremental_evidence.txt"
