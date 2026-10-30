# drHiro — Architecture

**Status:** Phase 1 implementation (secure multi-user core), following the
approved blueprint. Version 0.1.0.

## 1. Boundary decision

drHiro is a **health platform with OpenClaw as the conversational
orchestrator** — not a database and not a sole decision-maker. The
user-facing assistant is a **standalone OpenClaw agent** (the `drhiro`
agent), not a Hermes subagent. OpenClaw runs its own gateway, workspace,
and skill; Hermes is not in the runtime path.

- **drHiro Core** owns users, consent, measurements, meals, reminders,
  goals, rule evaluation, audit history, and APIs.
- **Android Bridge** reads user-approved Health Connect records and
  uploads normalized data.
- **OpenClaw** receives Telegram messages, calls narrowly scoped drHiro
  tools, explains results, asks follow-up questions.
- **Deterministic rule engine** handles safety flags and reminder logic.
  The LLM may explain a flag but must never invent, suppress, or change it.
- **Telegram bot, Mini App, web app** are clients of the same backend API.

## 2. System context

```
Xiaomi Mi Band ──> Mi Fitness ──> Health Connect ──> Android Bridge ─┐
OMRON device   ──> OMRON Connect ─ (if supported) ─> Health Connect ─┤
                                                                    v
Telegram Bot ──> OpenClaw Gateway ──> drHiro Tools ──> drHiro Core API
Telegram Mini App ────────────────┘                      │
Web App ─────────────────────────┘                       ├─> PostgreSQL 16
                                                         ├─> Redis (jobs)
                                                         └─> MinIO (photos)
```

## 3. Repository layout

```
apps/api        FastAPI core (models, routers, security, services)
apps/worker     RQ worker + scheduler (reminders, alerts, aggregates)
apps/web        React/Vite web + Telegram Mini App
apps/android-bridge  Kotlin Health Connect app
packages/health-schema   canonical metric types + value schemas
packages/rule-engine      deterministic calculations + versioned rules
packages/nutrition-core   Open Food Facts / USDA / private catalog
openclaw/                 OpenClaw agent: openclaw.json + skills/skill-drhiro
infra/           docker-compose, Caddyfile, backup, monitoring
docs/            this + data-dictionary + rule-governance + runbook
```

## 4. Core API

- FastAPI, SQLAlchemy 2.0, Alembic migrations, PostgreSQL 16.
- Base path `/api/v1`; OpenAPI at `/docs`.
- Auth: JWT access (15 min) + rotating refresh; Telegram Mini App initData
  validated server-side with HMAC; Android device-code exchange;
  OpenClaw signed service token + `X-Telegram-Id`.
- Tenant isolation: every tenant table carries `user_id`; all queries are
  scoped by the authenticated user.

## 5. Safety architecture

Three layers:

1. **Deterministic calculations** — rolling averages, robust weight trend,
   BP context averages, sleep consistency, coverage score.
2. **Reviewed rule engine** — versioned, jurisdiction-aware rules in pure
   Python (`packages/rule-engine`). The LLM never creates thresholds.
3. **LLM explanation/coaching** — OpenClaw explains rule output, asks
   high-impact questions, generates low-risk suggestions with explicit
   uncertainty.

Photo-derived BP/weight/meals are drafts until confirmed. Missing
wearable data is "missing", never zero. No medication changes. Fixed
escalation templates for urgent values.

## 6. Deployment

Docker Compose on the VPS; Caddy TLS at the edge; only 80/443 exposed.
Postgres/Redis/MinIO/workers on a private network. See
`infra/docker-compose.yml`, `infra/Caddyfile`, `infra/.env.example`.

## 7. MVP roadmap (from blueprint)

| Phase | Scope | Status |
|-------|-------|--------|
| 0 | Discovery (band/OMRON models, Health Connect matrix) | open |
| 1 | Secure multi-user core (users, auth, ingestion, meals, audit) | built in 0.1.0 |
| 2 | Android Health Connect bridge | scaffold |
| 3 | Reminders + trend engine | built (worker + rules) |
| 4 | Nutrition MVP (OFF/USDA, recipes) | built (core) |
| 5 | Food photos (vision worker) | endpoint stub |
| 6 | Hardening (threat model, restore test, monitoring) | partial |

## 8. Open decisions (blueprint §21)

1. Exact Xiaomi Mi Band models
2. Exact OMRON model + region
3. Android vs iPhone per user
4. Does OMRON write BP to Health Connect?
5. Cloud vs self-hosted vision model for food photos
6. VPS provider/domain/Docker current state
7. Public vs household-only
8. Diet objective(s)
9. Bot/UI languages
