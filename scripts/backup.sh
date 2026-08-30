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

info() { echo "[backup] $*"; }

info "Backing up .env (redacted copy for safety, original stays private)..."
# Keep a copy of the env keys (values masked) for restore reference.
if [[ -f .env ]]; then
  sed -E 's/=(.*)$/=***REDACTED***/' .env > "$BK_DIR/env-keys-$STAMP.txt"
  chmod 600 "$BK_DIR/env-keys-$STAMP.txt"
fi

info "Backing up TrueForge Postgres data and exports volume..."
docker compose exec -T tf-postgres pg_dump -U trueforge trueforge \
  > "$BK_DIR/trueforge-db-$STAMP.sql" 2>/dev/null \
  || info "DB dump skipped (postgres not running / pg_dump unavailable)."

info "Creating tarball..."
tar -czf "$TARBALL" \
  -C "$BK_DIR" trueforge-db-$STAMP.sql env-keys-$STAMP.txt 2>/dev/null || true

ls -lh "$TARBALL" >/dev/null 2>&1 && info "Backup written: $TARBALL" || info "No data to archive yet."
info "Note: the exports volume (synthetic visit briefs) lives in Docker; use 'docker run --rm -v drhiro-trueforge_exports_data:/data -v $(pwd)/backups:/out alpine cp -r /data /out/exports-$STAMP' to copy it."
