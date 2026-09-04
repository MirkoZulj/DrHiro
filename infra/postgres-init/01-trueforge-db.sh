#!/bin/bash
# Bootstrap: create the TrueForge database + role on the shared Postgres
# instance. Runs once on first Postgres boot (docker-entrypoint-initdb.d).
# drHiro's own database/role are created by the POSTGRES_USER/PASSWORD/DB env.
set -euo pipefail

TF_USER="${TF_POSTGRES_USER:-trueforge}"
TF_PASSWORD="${TF_POSTGRES_PASSWORD:-trueforge}"
TF_DB="${TF_POSTGRES_DB:-trueforge}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  DO \$\$
  BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${TF_USER}') THEN
      CREATE ROLE ${TF_USER} LOGIN PASSWORD '${TF_PASSWORD}';
    END IF;
  END
  \$\$;
EOSQL

# Create the TrueForge DB owned by its role (idempotent).
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -tAc "SELECT 1 FROM pg_database WHERE datname='${TF_DB}'" | grep -q 1 || \
  createdb --username "$POSTGRES_USER" -O "$TF_USER" "$TF_DB"

echo "[postgres-init] TrueForge database '${TF_DB}' ready (role '${TF_USER}')"
