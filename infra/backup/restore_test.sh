#!/usr/bin/env bash
# Monthly restore test — restores the latest backup into a scratch DB.
# A backup that cannot be restored is not a backup.
#
# 0.8.1 additions (per feature spec §1.1): the test now also verifies
#   - the restored alembic_version matches the .alembic manifest,
#   - per-table row counts are >= 99% of the .counts manifest (tables > 100 rows),
#   - every medical_records.source_image_path in the restored DB exists as a
#     file in the untarred files archive.
# Exits non-zero on any failure.
set -euo pipefail

BACKUP_DIR="${DRHIRO_BACKUP_DIR:-/var/backups/drhiro}"
TEST_DB="drhiro_restore_test"
DB_USER="${POSTGRES_USER:-drhiro}"
FILES_TMP="/tmp/restore-files"
TOL=0.99   # restored counts must be >= 99% of recorded (tables with > 100 rows)

fail() { echo "[restore-test] FAILED: $*" >&2; exit 1; }

# Latest backup set: pick the newest sql dump, then match its STAMP across
# the file tar + manifests. A dump without its manifests is a partial backup.
LATEST_SQL="$(ls -1t "$BACKUP_DIR"/drhiro-*.sql.gz* 2>/dev/null | head -1)"
[ -n "$LATEST_SQL" ] || fail "no drhiro-*.sql.gz* found in ${BACKUP_DIR}"
STAMP="$(basename "$LATEST_SQL" | sed -E 's/^drhiro-([0-9]{8}-[0-9]{6}).*/\1/')"
LATEST_ALEMBIC="$BACKUP_DIR/drhiro-${STAMP}.alembic"
LATEST_COUNTS="$BACKUP_DIR/drhiro-${STAMP}.counts"
LATEST_FILES="$(ls -1t "$BACKUP_DIR"/drhiro-files-${STAMP}.tar.gz* 2>/dev/null | head -1)"
[ -f "$LATEST_ALEMBIC" ] || fail "missing manifest drhiro-${STAMP}.alembic (partial backup?)"
[ -f "$LATEST_COUNTS" ] || fail "missing manifest drhiro-${STAMP}.counts (partial backup?)"
[ -n "$LATEST_FILES" ] || fail "missing files archive drhiro-files-${STAMP}.tar.gz* (partial backup?)"

echo "[restore-test] using ${LATEST_SQL}"

case "$LATEST_SQL" in
  *.gpg)
    echo "[restore-test] decrypting..."
    gpg --batch --yes --decrypt "$LATEST_SQL" | gunzip > /tmp/drhiro-restore.sql
    rm -rf "$FILES_TMP"; mkdir -p "$FILES_TMP"
    gpg --batch --yes --decrypt "$LATEST_FILES" | tar -xzf - -C "$FILES_TMP"
    ;;
  *.gz)
    gunzip -c "$LATEST_SQL" > /tmp/drhiro-restore.sql
    rm -rf "$FILES_TMP"; mkdir -p "$FILES_TMP"
    tar -xzf "$LATEST_FILES" -C "$FILES_TMP"
    ;;
esac

echo "[restore-test] dropping/creating ${TEST_DB}"
dropdb --if-exists -U "$DB_USER" "$TEST_DB"
createdb -U "$DB_USER" "$TEST_DB"
psql -U "$DB_USER" -d "$TEST_DB" -f /tmp/drhiro-restore.sql > /tmp/drhiro-restore.log 2>&1

echo "[restore-test] verifying tables"
psql -U "$DB_USER" -d "$TEST_DB" -tAc "SELECT count(*) FROM pg_tables WHERE schemaname='public';" | xargs echo "tables restored:"

# --- alembic revision check -------------------------------------------------
RECORDED_REV="$(tr -d '[:space:]' < "$LATEST_ALEMBIC")"
RESTORED_REV="$(psql -U "$DB_USER" -d "$TEST_DB" -tAc "SELECT version_num FROM alembic_version" | tr -d '[:space:]')"
[ "$RESTORED_REV" = "$RECORDED_REV" ] || fail "alembic mismatch: restored ${RESTORED_REV} != recorded ${RECORDED_REV}"
echo "[restore-test] alembic revision OK: ${RESTORED_REV}"

# --- per-table row-count check ----------------------------------------------
# Manifest lines: relname=n_live_tup (pg_stat estimate). Compare each table
# with > 100 recorded rows: restored count(*) must be >= 99% of recorded.
FAILED_COUNTS=0
while IFS='=' read -r rel recorded; do
  [ -n "$rel" ] || continue
  case "$recorded" in ''|*[!0-9]*) continue ;; esac
  [ "$recorded" -gt 100 ] || continue
  # Table may be absent in the restored schema -> count query errors -> 0.
  restored="$(psql -U "$DB_USER" -d "$TEST_DB" -tAc "SELECT count(*) FROM \"${rel}\"" 2>/dev/null || echo 0)"
  case "$restored" in ''|*[!0-9]*) restored=0 ;; esac
  min_required="$(awk -v r="$recorded" -v t="$TOL" 'BEGIN { printf "%d", r * t }')"
  if [ "$restored" -lt "$min_required" ]; then
    echo "[restore-test] COUNT MISMATCH: ${rel} restored=${restored} recorded=${recorded}"
    FAILED_COUNTS=$((FAILED_COUNTS + 1))
  fi
done < "$LATEST_COUNTS"
[ "$FAILED_COUNTS" -eq 0 ] || fail "${FAILED_COUNTS} table(s) below ${TOL} of recorded counts"
echo "[restore-test] row counts OK"

# --- scan files check --------------------------------------------------------
MISSING=0
while IFS=$'\t' read -r p; do
  [ -n "$p" ] || continue
  if [ ! -f "$FILES_TMP/medical_scans/$p" ]; then
    echo "[restore-test] MISSING SCAN FILE: medical_scans/$p"
    MISSING=$((MISSING + 1))
  fi
done < <(psql -U "$DB_USER" -d "$TEST_DB" -tAc \
  "SELECT source_image_path FROM medical_records WHERE source_image_path IS NOT NULL")
[ "$MISSING" -eq 0 ] || fail "${MISSING} scan file(s) referenced by medical_records not found in files archive"
echo "[restore-test] scan files OK"

echo "[restore-test] PASSED"
rm -f /tmp/drhiro-restore.sql
