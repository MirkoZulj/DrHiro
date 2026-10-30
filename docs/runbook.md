# drHiro — Runbook

Operational procedures for the drHiro stack on the VPS.

## 0. Live deployment (2026-08-10)

- **Host:** VPS <vps-host>, Ubuntu 24.04, Docker 29.6.1, Compose v5.3.0
- **Path:** /opt/drhiro (repo copy, excluding .git/.venv)
- **URL:** https://vmi3413468.contaboserver.net/drhiro/ (nginx proxy → 127.0.0.1:8010)
- **Services:** postgres 16, redis 7, minio, api, worker, scheduler (compose project `drhiro`)
- **Secrets:** /opt/drhiro/infra/.env.prod (chmod 600; `.env` is a symlink to it)
- **Verified:** full E2E over HTTPS (auth, ingestion idempotency, meals, dashboard,
  OpenClaw tool, tenant isolation, export)
- **Placeholder:** DRHIRO_TELEGRAM_BOT_TOKEN is a test token — swap for the real
  BotFather token before enabling the OpenClaw gateway / Mini App production auth.
- **Deferred:** OpenClaw gateway + web behind compose profiles (`openclaw`, `web`)

## 1. Bring up the stack

```bash
cd /opt/drhiro/infra
docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d
docker compose -f docker-compose.yml -f docker-compose.vps.yml ps   # all healthy
```

## 2. Migrations

```bash
# From a machine with the venv + DB access:
cd apps/api
source ../../.venv/bin/activate
export DRHIRO_DATABASE_URL="postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@localhost:5435/${POSTGRES_DB}"
alembic upgrade head
# Or on the VPS (one-off container):
cd /opt/drhiro/infra
docker compose -f docker-compose.yml -f docker-compose.vps.yml run --rm --no-deps api alembic upgrade head
# Generate a new migration after model changes:
alembic revision --autogenerate -m "describe change"
```

## 3. Health checks

- `curl http://<vps>/health` — API liveness.
- `docker compose ps` — all services healthy.
- `docker compose logs -f api worker scheduler` — structured logs.
- `infra/monitoring/healthcheck.sh` — disk/DB/API alerting (systemd timer).

## 4. Backups

Daily encrypted pg_dump + object storage versioning:
```bash
# install cron/systemd timer:
0 3 * * * /opt/drhiro/infra/backup/backup.sh
```
Monthly restore test (a backup that cannot be restored is not a backup):
```bash
/opt/drhiro/infra/backup/restore_test.sh
```
Restore manually:
```bash
gunzip -c /var/backups/drhiro/drhiro-<stamp>.sql.gz | psql -U drhiro -d drhiro
```

## 5. Rollback

- **API/worker code**: `docker compose down && docker compose up -d` with
  the previous pinned image digest.
- **Schema**: `alembic downgrade -1` (only when the new migration has not
  shipped data you must keep; otherwise restore from backup).
- **OpenClaw**: gateway is stateless w.r.t. drHiro; restart it.

## 6. Secret rotation

- `DRHIRO_JWT_SECRET` — rotate; old tokens die at their TTL (15 min access).
- `TELEGRAM_BOT_TOKEN` — BotFather; restart gateway.
- Postgres password — update .env, restart postgres + api/worker.
- MinIO keys — update .env, restart minio.

## 7. Incident: API down

1. `docker compose logs api --tail 100`
2. Check DB: `docker compose exec postgres pg_isready`
3. Check disk: `df -h`; alert at >85% via healthcheck.sh
4. Restart: `docker compose restart api`

## 8. Incident: worker/scheduler stalled

1. `docker compose logs worker --tail 100`
2. `docker compose exec redis redis-cli LLEN drhiro` — queue depth
3. Restart: `docker compose restart worker scheduler`

## 9. Onboarding a new user (Phase 1 flow)

1. User messages the bot -> OpenClaw calls telegram-link/start, shows code.
2. User opens Mini App/web, completes link with the code.
3. Web/Mini App calls device-code; user enters it in the Android app.
4. Bridge exchanges code, requests Health Connect permissions.
5. Bounded 30-day historical import runs; sync diagnostics show status.

## 10. Testing

```bash
# API acceptance tests (needs dev postgres+redis up):
docker compose --env-file infra/.env.dev -f infra/docker-compose.yml -f infra/docker-compose.dev.yml up -d postgres redis
cd apps/api && python -m pytest tests/ -q
cd packages/rule-engine && python -m pytest tests/ -q
cd packages/nutrition-core && python -m pytest tests/ -q
```
