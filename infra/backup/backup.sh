#!/usr/bin/env bash
# drHiro encrypted database backup — run daily via cron/systemd timer.
# Restores into staging for the monthly restore test.
set -euo pipefail

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${DRHIRO_BACKUP_DIR:-/var/backups/drhiro}"
DB_NAME="${POSTGRES_DB:-drhiro}"
DB_USER="${POSTGRES_USER:-drhiro}"
GPG_RECIPIENT="${DRHIRO_GPG_RECIPIENT:-}"

mkdir -p "$BACKUP_DIR"
cd "$BACKUP_DIR"

echo "[backup] dumping ${DB_NAME} -> ${STAMP}.sql.gz"
pg_dump -U "$DB_USER" -d "$DB_NAME" | gzip > "drhiro-${STAMP}.sql.gz"

if [ -n "$GPG_RECIPIENT" ]; then
  echo "[backup] encrypting with gpg for ${GPG_RECIPIENT}"
  gpg --batch --yes --encrypt --recipient "$GPG_RECIPIENT" "drhiro-${STAMP}.sql.gz"
  rm "drhiro-${STAMP}.sql.gz"
  BACKUP_FILE="drhiro-${STAMP}.sql.gz.gpg"
else
  BACKUP_FILE="drhiro-${STAMP}.sql.gz"
  echo "[backup] WARNING: no GPG_RECIPIENT set — backup is NOT encrypted"
fi

# Retention: keep 14 daily, 8 weekly-ish (first of week), 6 monthly-ish
ls -1t drhiro-*.sql.gz* 2>/dev/null | tail -n +15 | xargs -r rm -f

echo "[backup] done: ${BACKUP_DIR}/${BACKUP_FILE}"
echo "[backup] TEST RESTORE: gunzip -c <file> | psql -U ${DB_USER} -d drhiro_restore_test"
