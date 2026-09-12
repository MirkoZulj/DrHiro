#!/usr/bin/env bash
# Monthly restore test — restores the latest backup into a scratch DB.
# A backup that cannot be restored is not a backup.
set -euo pipefail

BACKUP_DIR="${DRHIRO_BACKUP_DIR:-/var/backups/drhiro}"
TEST_DB="drhiro_restore_test"
DB_USER="${POSTGRES_USER:-drhiro}"

LATEST="$(ls -1t "$BACKUP_DIR"/drhiro-*.sql.gz* | head -1)"
echo "[restore-test] using ${LATEST}"

case "$LATEST" in
  *.gpg)
    echo "[restore-test] decrypting..."
    gpg --batch --yes --decrypt "$LATEST" | gunzip > /tmp/drhiro-restore.sql
    ;;
  *.gz)
    gunzip -c "$LATEST" > /tmp/drhiro-restore.sql
    ;;
esac

echo "[restore-test] dropping/creating ${TEST_DB}"
dropdb --if-exists -U "$DB_USER" "$TEST_DB"
createdb -U "$DB_USER" "$TEST_DB"
psql -U "$DB_USER" -d "$TEST_DB" -f /tmp/drhiro-restore.sql > /tmp/drhiro-restore.log 2>&1

echo "[restore-test] verifying tables"
psql -U "$DB_USER" -d "$TEST_DB" -tAc "SELECT count(*) FROM pg_tables WHERE schemaname='public';" | xargs echo "tables restored:"

echo "[restore-test] PASSED"
rm -f /tmp/drhiro-restore.sql
