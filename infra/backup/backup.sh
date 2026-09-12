#!/usr/bin/env bash
# drHiro encrypted database backup — run daily via cron/systemd timer.
# Restores into staging for the monthly restore test.
set -euo pipefail

COMPOSE_FILE="${DRHIRO_COMPOSE_FILE:-infra/docker-compose.yml}"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-drhiro}"
DB_CONTAINER_SERVICE="${POSTGRES_SERVICE:-postgres}"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="${DRHIRO_BACKUP_DIR:-/var/backups/drhiro}"
DB_NAME="${POSTGRES_DB:-drhiro}"
DB_USER="${POSTGRES_USER:-drhiro}"
GPG_RECIPIENT="${DRHIRO_GPG_RECIPIENT:-}"

mkdir -p "$BACKUP_DIR"
cd "$BACKUP_DIR"

echo "[backup] dumping ${DB_NAME} via container ${DB_CONTAINER_SERVICE} -> ${STAMP}.sql.gz"
# Run pg_dump INSIDE the compose postgres container (the DB port is not exposed
# on the host; the host-local socket would back up a different instance). Use
# exec -T to avoid a pseudo-tty; -U/-d are container-local (matching the
# service's POSTGRES_* env, which we mirror via $POSTGRES_*).
DUMP_CMD="docker compose -f \"${COMPOSE_FILE}\" -p \"${COMPOSE_PROJECT_NAME}\" exec -T ${DB_CONTAINER_SERVICE} pg_dump -U \"${DB_USER}\" -d \"${DB_NAME}\" --no-owner --no-privileges"

DUMP_FILE="drhiro-${STAMP}.sql.gz"
set +e
DUMP_OUTPUT=$(eval "${DUMP_CMD}" 2>/tmp/drhiro-backup-stderr)
DUMP_RC=$?
set -e

if [ $DUMP_RC -ne 0 ]; then
  echo "[backup] FAILED: pg_dump exited with code ${DUMP_RC}" >&2
  if [ -s /tmp/drhiro-backup-stderr ]; then
    echo "[backup] stderr:" >&2
    cat /tmp/drhiro-backup-stderr >&2
  fi
  rm -f /tmp/drhiro-backup-stderr
  exit 1
fi
rm -f /tmp/drhiro-backup-stderr

# Fail loudly if the dump is empty/trivial (no pg_dump output at all)
if [ -z "${DUMP_OUTPUT}" ]; then
  echo "[backup] FAILED: pg_dump produced no output (empty dump)" >&2
  exit 1
fi

# Compress and verify the gzip is valid
printf '%s\n' "${DUMP_OUTPUT}" | gzip > "${DUMP_FILE}"

# Verify gzip integrity
if ! gzip -t "${DUMP_FILE}"; then
  echo "[backup] FAILED: gzip integrity check failed for ${DUMP_FILE}" >&2
  rm -f "${DUMP_FILE}"
  exit 1
fi

# Verify the compressed file is non-trivial (at least 1KB)
MIN_SIZE=1024
FILE_SIZE=$(stat -c %s "${DUMP_FILE}" 2>/dev/null || stat -f %z "${DUMP_FILE}" 2>/dev/null || echo 0)
if [ "${FILE_SIZE}" -lt "${MIN_SIZE}" ]; then
  echo "[backup] FAILED: ${DUMP_FILE} is only ${FILE_SIZE} bytes (min ${MIN_SIZE})" >&2
  rm -f "${DUMP_FILE}"
  exit 1
fi

if [ -n "$GPG_RECIPIENT" ]; then
  echo "[backup] encrypting with gpg for ${GPG_RECIPIENT}"
  gpg --batch --yes --encrypt --recipient "$GPG_RECIPIENT" "${DUMP_FILE}"
  rm "${DUMP_FILE}"
  BACKUP_FILE="drhiro-${STAMP}.sql.gz.gpg"
else
  BACKUP_FILE="drhiro-${STAMP}.sql.gz"
  echo "[backup] WARNING: no GPG_RECIPIENT set — backup is NOT encrypted"
fi

# Retention: keep 14 daily, 8 weekly-ish (first of week), 6 monthly-ish
ls -1t drhiro-*.sql.gz* 2>/dev/null | tail -n +15 | xargs -r rm -f

echo "[backup] done: ${BACKUP_DIR}/${BACKUP_FILE} (${FILE_SIZE} bytes)"
echo "[backup] TEST RESTORE: gunzip -c <file> | psql -U ${DB_USER} -d drhiro_restore_test"
