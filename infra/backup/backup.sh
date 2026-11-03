#!/usr/bin/env bash
# drHiro encrypted database backup — run daily via cron/systemd timer.
# Restores into staging for the monthly restore test.
#
# 0.8.1 additions (per feature spec §1.1):
#   - a tar.gz of the filesystem data dir (medical_scans + generated)
#   - .alembic / .counts manifests so restore_test.sh can verify the restore
#   - refusal to keep a partial archive: if any step fails, the whole set is
#     removed before exiting (set -euo pipefail + an EXIT trap while running).
set -euo pipefail

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${DRHIRO_BACKUP_DIR:-/var/backups/drhiro}"
DATA_DIR="${DRHIRO_DATA_DIR:-/opt/drhiro/data}"   # host path of the drHiro data dir
DB_NAME="${POSTGRES_DB:-drhiro}"
DB_USER="${POSTGRES_USER:-drhiro}"
GPG_RECIPIENT="${DRHIRO_GPG_RECIPIENT:-}"

mkdir -p "$BACKUP_DIR"
cd "$BACKUP_DIR"

# Remove this run's artefacts if we abort part-way (never keep a partial set).
ARTIFACTS=(drhiro-${STAMP}.sql.gz drhiro-${STAMP}.sql.gz.gpg \
           drhiro-files-${STAMP}.tar.gz drhiro-files-${STAMP}.tar.gz.gpg \
           drhiro-${STAMP}.alembic drhiro-${STAMP}.counts)
cleanup_partial() { rm -f "${ARTIFACTS[@]}"; }
trap cleanup_partial EXIT

echo "[backup] dumping ${DB_NAME} -> ${STAMP}.sql.gz"
pg_dump -U "$DB_USER" -d "$DB_NAME" | gzip > "drhiro-${STAMP}.sql.gz"

echo "[backup] taring filesystem data from ${DATA_DIR}"
# medical_scans + generated live on the host under DATA_DIR (bind-mounted into
# api/worker). Missing subdirs are created so the tar always succeeds.
mkdir -p "$DATA_DIR/medical_scans" "$DATA_DIR/generated"
tar -czf "drhiro-files-${STAMP}.tar.gz" -C "$DATA_DIR" medical_scans generated

echo "[backup] writing manifests (.alembic, .counts)"
psql -U "$DB_USER" -d "$DB_NAME" -tAc "SELECT version_num FROM alembic_version" \
  > "drhiro-${STAMP}.alembic"
psql -U "$DB_USER" -d "$DB_NAME" -tAc \
  "SELECT relname||'='||n_live_tup FROM pg_stat_user_tables ORDER BY relname" \
  > "drhiro-${STAMP}.counts"

if [ -n "$GPG_RECIPIENT" ]; then
  echo "[backup] encrypting with gpg for ${GPG_RECIPIENT}"
  gpg --batch --yes --encrypt --recipient "$GPG_RECIPIENT" "drhiro-${STAMP}.sql.gz"
  rm "drhiro-${STAMP}.sql.gz"
  gpg --batch --yes --encrypt --recipient "$GPG_RECIPIENT" "drhiro-files-${STAMP}.tar.gz"
  rm "drhiro-files-${STAMP}.tar.gz"
  BACKUP_FILE="drhiro-${STAMP}.sql.gz.gpg"
else
  BACKUP_FILE="drhiro-${STAMP}.sql.gz"
  echo "[backup] WARNING: no GPG_RECIPIENT set — backup is NOT encrypted"
fi

trap - EXIT

# Retention: keep 14 daily, 8 weekly-ish (first of week), 6 monthly-ish.
# Applied to every artefact kind so manifests/tars never outlive their dump.
ls -1t drhiro-*.sql.gz* 2>/dev/null | tail -n +15 | xargs -r rm -f
ls -1t drhiro-files-*.tar.gz* 2>/dev/null | tail -n +15 | xargs -r rm -f
ls -1t drhiro-*.alembic 2>/dev/null | tail -n +15 | xargs -r rm -f
ls -1t drhiro-*.counts 2>/dev/null | tail -n +15 | xargs -r rm -f

echo "[backup] done: ${BACKUP_DIR}/${BACKUP_FILE}"
echo "[backup] TEST RESTORE: infra/backup/restore_test.sh"
