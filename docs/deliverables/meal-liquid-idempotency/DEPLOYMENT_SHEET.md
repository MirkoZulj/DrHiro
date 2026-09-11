# Deployment sheet — meal+liquid idempotency release

**Status: NOT READY for deployment.** Reasons and the exact conditions that would change
this are in §9. Nothing here has been executed. No production change, push, deployment,
credential rotation, gate activation, or historical cleanup has been performed.

---

## 1. Release identity

- **Release commit (functional):** `50bec02db48358ae9ef32f1640e23e347b8e9057`
  - *Exact final commit of the release-candidate line. The packaging/docs commit that
    carries this sheet is recorded in the bundle's `BASE_AND_HEAD.md` — a file cannot
    cite the hash of the commit that contains it.*
- **Base of the reviewed increment:** `74ca71f8dec9368c3274b2166a5d9b19d3b94a2b`
  (fully-reviewed round-10 state).
- **Frozen candidate (UNCHANGED, do not repackage):**
  `7e2cf6915dbd7478e8a558817d4d51aa63879e60`; frozen archive
  `review_bundle/rebuilt.tar.gz` = `4d2bf6851c869670308ad28961d32626a12cf2b2a2af389fc3e1f6c8fe59668d`.
- **New release artifact (new name, new checksum):**
  `drhiro_release_2026-09-11_review12.tar.gz`. Its SHA-256 is reported alongside this
  artifact in the release message — it cannot be recorded inside the artifact itself,
  because the checksum changes if any file in it changes. It is NOT the frozen archive
  and does not replace it.
- **Branch:** `feature/meal-liquid-idempotency` (isolated; not merged, not pushed).

### 1b. Exactly what would reach production (shippable code delta)

Diffed against the frozen candidate `7e2cf69` over `apps/ packages/ services/ infra/
docker-compose.yml` — the paths that build into images — the entire code delta is
**three ADDED files**:

| File | New? | Loaded by production runtime? |
|---|---|---|
| `apps/api/src/drhiro_api/schema_activities.py` | added | **No** — imported only by the new Alembic revision |
| `apps/api/alembic/versions/b7c8d9e0f1a2_activities_table.py` | added | **No** — runs only if `alembic upgrade` is invoked |
| `apps/api/src/drhiro_api/services/ingress_keys.py` | added | **No** — imported only by `tests/test_r2_trusted_key_set.py` |

`packages/`, `services/`, `infra/` and `docker-compose.yml` are **unchanged**. In
particular the legacy consumption writers, the writer gate, the ingress endpoint/router
and `main.py` were **already present in the frozen candidate** — this release adds no
new route and changes no existing one. Every added file is inert at runtime unless a
migration is explicitly run.

### 1a. Image digests

**Release images do not exist yet.** No image has been built from `50bec02`; building and
tagging is part of the approved deploy step, and the resulting digests must be recorded
before rollout. Claiming digests now would be fabrication.

**Current production images — the rollback target (read-only capture, 2026-09-11):**

| Service | Image | Image ID (short) |
|---|---|---|
| drhiro-api-1 | `ghcr.io/your-org/drhiro-api:0.1.0` | `608ab330a8d6` |
| drhiro-worker-1 | `ghcr.io/your-org/drhiro-worker:0.1.0` | `e636bccd929c` |
| drhiro-scheduler-1 | `ghcr.io/your-org/drhiro-scheduler:0.1.0` | `fca9aa9b548e` |
| drhiro-web-1 | `ghcr.io/your-org/drhiro-web:0.1.0` | `6bfbf1ea0185` |
| drhiro-mcp | `drhiro-mcp:latest` | `898e25d712ad` |
| drhiro-postgres-1 | `postgres:16` | `95206741a5b2` |

Note the `your-org` placeholder in three tags: these images are **built locally**, not
pulled from a registry, so there is no upstream digest to pin. A release must therefore
record the built image IDs, not a registry digest.

---

## 2. Features — enabled / disabled in this release

| Feature | State | Mechanism |
|---|---|---|
| Trusted Telegram ingress `POST /api/v1/ingest/telegram/event` | **DISABLED (inert)** | returns `503 ingress_secret_not_configured` while `telegram_ingress_secret`/`telegram_bot_id` are unset |
| `telegram_ingress_enabled` | **false** | config default |
| `legacy_consumption_writers_enabled` | **true** | config default — existing writers unchanged |
| Sender identity (B7) | **NOT IMPLEMENTED** | — |
| Edit / content-conflict handling (B7) | **NOT IMPLEMENTED** | — |
| Disposable-stack receipt/lease machinery | **not deployed to production** | lives only in `deploy/disposable/` |

### 2a. Demonstrated: startup does not activate the feature

Verified in this checkpoint, not asserted:

- **No auto-activation.** With production defaults, `POST /api/v1/ingest/telegram/event`
  returns **503 `ingress_secret_not_configured`**. `require_model_writer_allowed` returns
  **ALLOWED**, i.e. existing writers behave exactly as before.
- **No auto-migration.** The production API image `CMD` is plain
  `uvicorn drhiro_api.main:app`; `Dockerfile.api` and `docker-compose.yml` contain **no**
  `alembic upgrade`. A repo-wide search finds `alembic upgrade head` in a compose file
  **only** in `deploy/disposable/docker-compose.isolated.yml` (the disposable stack's
  `migrate` service) — never in the production path.
- **No startup hook.** `drhiro_api.main` has no startup/`lifespan` handler, no
  `create_all`, no migration call.
- **No routing change.** The production `docker-compose.yml` has **zero** references to
  `ingress`, `telegram_receipts`, `trusted-spool`, `fake_telegram`, `consumer_offset`, or
  `disposable`. The existing `telegram-bridge` service is untouched.
- **No production-behaviour change.** All four gating config keys are **absent** from the
  production `.env` (`/opt/drhiro/.env`, key-name check only; no values read), so every
  default above applies.

**Caveat that must be respected:** the route is *registered* unconditionally in
`main.py`, and activation requires only that an operator set the two config values. The
gate is configuration, not code.

---

## 3. Migrations and their effects

- **Production `alembic_version` = `d5e6f7a8b9c0`** (recipes revision), captured
  read-only. `consumption_operations` does not exist; the `activities` table exists,
  created out-of-band rather than by the Alembic chain.
- **This release adds `b7c8d9e0f1a2_activities_table.py`, placed at Alembic CHAIN HEAD**
  (`down_revision = 9a1b2c3d4e5f`). Verified: the chain has a **single head**,
  `b7c8d9e0f1a2`, over 10 revisions. It is a production migration path.
- **It is NOT applied by this release.** Migrations are never automatic here.
- **⚠ If anyone runs `alembic upgrade head`,** this migration *will* be applied and will
  attempt to adopt/validate the pre-existing production `activities` table. That is an
  irreversible-ish schema action against live data.
- **Therefore: this release must be deployed with migrations explicitly EXCLUDED**
  (§7, trigger R-MIG). The migration and the trusted-ingress feature are deferred
  together (§9).

---

## 4. Backup / restore point

**NONE EXISTS. This is a hard blocker.**

Read-only verification on the VPS found:
- no `/opt/drhiro/backups` directory (or any drhiro backup directory / `*.dump`) on the
  host;
- **no** `pg_dump` or backup entry in `crontab -l`;
- production data volume `drhiro_pgdata` present, with no verified restorable artifact
  derived from it.

A restore point must be created **and restore-verified** (dump restored into a scratch
database and row-count/spot-checked) before any deploy that could touch production data.
A backup that has never been restored is not a backup.

---

## 5. Smoke tests (to run after an approved deploy)

Read-only / non-mutating first:

1. `GET /health` on the API returns 200.
2. `POST /api/v1/ingest/telegram/event` returns **503 `ingress_secret_not_configured`** —
   proves the feature is still inert post-deploy.
3. An existing meal-logging path (authenticated, e.g. `POST /api/v1/meals/from-text` via a
   normal user JWT) still succeeds — proves the writer gate did not close.
4. `alembic current` still reports `d5e6f7a8b9c0` — proves no migration ran.
5. Container health: all `drhiro-*` services `Up (healthy)` where healthchecks exist;
   restart counts unchanged from the pre-deploy capture.

Any failure ⇒ execute the rollback in §6.

---

## 6. Rollback

- **Commands:** re-deploy the previous image IDs from §1a
  (`docker compose up -d` pinned to those IDs, or `docker tag` the retained images back
  and recreate the affected services), then re-run smoke tests 1, 3, 4.
- **Triggers:**
  - any smoke test fails;
  - API or worker crash-loops, or healthcheck failures appear;
  - error rate on existing meal/liquid endpoints rises above the pre-deploy baseline;
  - any unexpected write appears in `consumption_operations` or a new Alembic revision
    appears in `alembic_version`;
  - the ingress endpoint stops reporting 503 (i.e. it became active unexpectedly).
- **Schema rollback:** not required for this release because no migration is applied. If
  `b7c8d9e0f1a2` is ever applied, the rollback is
  `docs/deliverables/meal-liquid-idempotency/B_schema_migration_rollback.sql` — which has
  **never been executed against production** and is unverified for this path (§9).
- **Data rollback:** impossible without §4. There is no restore point today.

---

## 7. Deployment procedure (for the approved, feature-disabled path only)

1. Create and restore-verify the backup (§4). **Do not proceed without it.**
2. Build images from `50bec02`; record the resulting image IDs.
3. `docker compose up -d` **without** any migration step. Do **not** run
   `alembic upgrade head`.
4. Leave `DRHIRO_TELEGRAM_INGRESS_SECRET`, `DRHIRO_TELEGRAM_BOT_ID`,
   `DRHIRO_TELEGRAM_INGRESS_ENABLED` and `DRHIRO_LEGACY_CONSUMPTION_WRITERS_ENABLED`
   **unset**.
5. Run §5 smoke tests; capture output.
6. Record the deployed image IDs and the post-deploy `alembic current`.

---

## 8. Remaining limitations

- **B7 OPEN.** Sender identity and edit/content-conflict handling are NOT implemented. A
  legitimate bot identity does not authenticate the sender.
- The trusted ingestion path is **unproven in production**; disposable-stack passes are
  evidence, not acceptance.
- **No verified backup/restore point exists** (§4).
- `activities`-table adoption against a **production mirror** has never been executed
  (§9); the migration is therefore untested against real production shape and values.
- Evidence for this checkpoint remains **agent-captured plus an in-process API
  demonstration**; no independent rerun was performed by a second party.
- Production uptime evidence supports only "no observed restart" — it does not prove
  configuration, credentials, or data were unchanged.

---

## 9. Production prerequisites — status (item 3)

| # | Prerequisite | Status | Evidence |
|---|---|---|---|
| 1 | Real OpenClaw/MCP compatibility with the reduced trust boundary | **NOT SATISFIED** | `T1_production_ingress_reconciliation.md`: production ingress is OpenClaw's Telegram channel; the minting component must sit there via a channel plugin or gateway shim, and **that component is not implemented**. "the gap … is unchanged"; MCP drhiro server and skill-drhiro are `enabled: false`. B7 is blocked on this. |
| 2 | Remove unnecessary credentials from the model-accessible runtime + **approved rotation plan** | **PARTIAL / NOT SATISFIED** | `CREDENTIAL_FOLLOW_UP.md`: touched paths cleaned (inline JWT minting and hard-coded API URL removed). Still open: CORS `allow_origins=["*"]` (medium), backend rejection of an **empty** `SERVICE_TOKEN` unverified (medium), hard-coded model path (low), test DB URL (low). **No approved rotation plan is on record** — the document is a backlog, not an approved plan. |
| 3 | User-JWT boundary enforcement | **PARTIAL — not independently verified** | `/manual/*` paths use the standard `get_current_user` dependency and the new `/manual/liquid` mints no JWTs. But there is no verified evidence that empty/forged service tokens are rejected across the boundary, and `DRHIRO_JWT_SECRET` empty-key fallback is only lint-recommended. |
| 4 | Production-mirror migration + value-preservation verification | **NOT SATISFIED** | Production state was inspected **read-only** (`alembic_version = d5e6f7a8b9c0`). No production **mirror** has been migrated, and no row-count/value-preservation comparison exists — in particular for the out-of-band `activities` table this release would adopt. |
| 5 | Cutover and rollback procedure | **DOCUMENTED PLAN, NOT EXECUTED** | `R6_t1_cutover_rollback_plan.md` states "plan, NOT executed"; tested only on disposable databases. `B_schema_migration_rollback.sql` exists but is unverified against production. |

**Disposition:** prerequisites 1, 3, 4, 5 are not satisfied and 2's rotation plan is
absent. Per instruction, the **affected feature is explicitly DEFERRED**: neither the
trusted Telegram ingestion nor the `activities` migration is proposed for activation in
this release. The reason no prerequisite was "closed" in this checkpoint is that closing
any of them requires production work (config changes, credential rotation, mirror
migration, cutover) which was explicitly out of scope.

---

## 10. Verdict

**NOT READY.**

Specific reason: a verified backup/restore point does not exist on the production VPS
(no `pg_dump` job, no backup artifact), and four of the five recorded production
prerequisites are unsatisfied — so neither full activation nor a safely hedged deploy is
currently possible.

What would change this, in order:

1. Create and **restore-verify** a production backup → removes the §4 blocker.
2. Approve the deploy with migrations explicitly excluded and the four config keys unset
   → **READY — feature disabled with existing behaviour preserved** (the inertness is
   already demonstrated in §2a, and would be re-verified by smoke tests 2 and 4).
3. To reach **READY — full activation prerequisites satisfied**, additionally: implement
   the OpenClaw-side minting component (prereq 1), an approved rotation plan (2), verified
   service-token rejection (3), a real production-mirror migration with value-preservation
   evidence (4), and an executed cutover/rollback rehearsal (5) — plus B7.

**Awaiting explicit deployment approval. No deployment has been performed.**
