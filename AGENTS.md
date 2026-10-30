# drHiro — Team Guide

## Roles

| Agent | Role |
|-------|------|
| Jeeves | Manager / Coordinator |
| Alex | First-principles architect |
| Jerry | Engineer — API, workers, packages |
| Miranda | Designer — web/Mini App |
| Kim | QA — acceptance tests, edge cases |

## Conventions

- API-first: all clients (bot, Mini App, web, Android) consume the same
  `/api/v1` surface.
- Deterministic safety rules live in `packages/rule-engine`; the LLM never
  creates or suppresses alerts.
- Every tenant query is scoped by `user_id` from the authenticated session.
- Photo-derived values are drafts until confirmed.
- Missing wearable data is missing, never zero.
- Migrations via Alembic; never edit applied migrations.
- Secrets in `.env` (never committed); non-secret settings in code/config.
- Version bump on every release (repo root + pyproject files).

## Verification

- API acceptance tests: `apps/api/tests/` (real Postgres).
- Package tests: `packages/*/tests/`.
- Restore test monthly: `infra/backup/restore_test.sh`.
