# drHiro

Multi-user personal health platform. Telegram bot + Mini App + web app +
Android Health Connect bridge, orchestrated by a standalone **OpenClaw
agent** (drhiro) over a deterministic, consent-scoped core API.

> drHiro's conversational layer is an OpenClaw agent, not a Hermes
> subagent. The OpenClaw gateway owns the Telegram channel, the drhiro
> agent, its workspace, and its skill (`openclaw/skills/skill-drhiro`).
> Hermes agents (Jeeves et al.) build and operate the platform; they do
> not run the user-facing assistant.

## Quick start (dev)

```bash
# 1. Bring up Postgres + Redis
docker compose --env-file infra/.env.dev -f infra/docker-compose.yml -f infra/docker-compose.dev.yml up -d postgres redis

# 2. Install + migrate
python3 -m venv .venv && source .venv/bin/activate
pip install -e packages/health-schema -e packages/rule-engine -e packages/nutrition-core -e "apps/api[dev]" -e apps/worker
cd apps/api
export DRHIRO_DATABASE_URL="postgresql+psycopg://drhiro:drhiro@localhost:5435/drhiro"
alembic upgrade head

# 3. Run API
uvicorn drhiro_api.main:app --reload --port 8000
# Docs: http://localhost:8000/docs
```

## Tests

```bash
cd apps/api && python -m pytest tests/ -q          # 15 acceptance tests
cd packages/rule-engine && python -m pytest tests/ -q
cd packages/nutrition-core && python -m pytest tests/ -q
```

## Docs

- `docs/architecture.md` — system design
- `docs/data-dictionary.md` — canonical schema
- `docs/rule-governance.md` — safety rule lifecycle
- `docs/runbook.md` — operations
- `openclaw/openclaw.json` — OpenClaw agent config (drhiro)
- `openclaw/skills/skill-drhiro/SKILL.md` — OpenClaw agent instructions + tool contract

## Layout

```
apps/api             FastAPI core (Phase 1 — built, tested)
apps/worker          RQ worker + scheduler
apps/web             React/Vite web + Telegram Mini App (scaffold)
apps/android-bridge  Kotlin Health Connect app (scaffold)
packages/            health-schema, rule-engine, nutrition-core
openclaw/            OpenClaw agent: openclaw.json + skills/skill-drhiro
infra/               docker-compose, Caddy, backups, monitoring
```

## Legacy prototype

The original hybrid Telegram bot lives at
`/home/mirko/projects/DrHiro` (unchanged) and is reference material only.
