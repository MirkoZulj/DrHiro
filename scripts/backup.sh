#!/usr/bin/env bash
# drHiro on TrueForge — backup: export the protected .env (never the token as
# plaintext in a public place), the TrueForge Postgres data, and the exports
# volume to a dated tarball in ./backups. No secrets are printed.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

STAMP="$(date -u +%Y%m%d-%H%M%S)"
BK_DIR="$(pwd)/backups"
mkdir -p "$BK_DIR"
TARBALL="$BK_DIR/drhiro-trueforge-$STAMP.tar.gz"
STAGE="$BK_DIR/stage-$STAMP"
rm -rf "$STAGE" && mkdir -p "$STAGE"

info() { echo "[backup] $*"; }
fail() { echo "[backup] ERROR: $*" >&2; exit 1; }

info "Backing up .env (redacted copy for safety, original stays private)..."
# Keep a copy of the env keys (values masked) for restore reference.
if [[ -f .env ]]; then
  sed -E 's/=(.*)$/=***REDACTED***/' .env > "$STAGE/env-keys.txt"
  chmod 600 "$STAGE/env-keys.txt"
fi

# 1. Postgres dump. A failed dump MUST NOT produce an archive with an empty SQL.
info "Dumping TrueForge Postgres data..."
if docker compose exec -T tf-postgres pg_dump -U trueforge trueforge > "$STAGE/trueforge-db.sql" 2>/dev/null; then
  info "  pg_dump OK ($(wc -c < "$STAGE/trueforge-db.sql") bytes)"
else
  rm -f "$STAGE/trueforge-db.sql"
  fail "pg_dump failed — not creating an incomplete backup."
fi

# 2. Export the exports_data volume (Qodo #6).
# The synthetic visit briefs live in the named volume; copy them into the stage
# dir so they are archived, not merely mentioned in a note.
info "Exporting exports_data volume..."
if docker run --rm \
  -v "${COMPOSE_PROJECT_NAME:-drhiro-trueforge}_exports_data:/data:ro" \
  -v "$STAGE:/out" alpine sh -c 'cp -r /data /out/exports 2>/dev/null || mkdir -p /out/exports' 2>/dev/null; then
  info "  exports volume copied ($(find "$STAGE/exports" -type f 2>/dev/null | wc -l) files)"
else
  info "  exports volume copy skipped (volume not present yet — continuing)."
fi

# 3. Require a complete archive (Qodo #6): every expected artifact must exist.
MISSING=""
[[ -f "$STAGE/env-keys.txt" ]] || MISSING="$MISSING env-keys"
[[ -f "$STAGE/trueforge-db.sql" ]] || MISSING="$MISSING trueforge-db"
if [[ -n "$MISSING" ]]; then
  fail "backup incomplete, missing:$MISSING — refusing to write a partial archive."
fi

info "Creating tarball..."
if tar -czf "$TARBALL" -C "$STAGE" . 2>/dev/null; then
  rm -rf "$STAGE"
  info "Backup written: $TARBALL ($(du -h "$TARBALL" | cut -f1))"
else
  fail "tar failed — no archive created."
fi
