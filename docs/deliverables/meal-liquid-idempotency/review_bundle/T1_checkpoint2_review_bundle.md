# drHiro Meal+Liquid Idempotency — Review Bundle (Checkpoint 2)

**Branch:** `feature/meal-liquid-idempotency` (isolated)
**Commit pin:** `ec6b538d64330a93f7d7667521aa95f8c73a6af0`
**Date:** 2026-09-09
**Status:** Evidence submitted for review. **Acceptance pending.** No production
change, push, deployment, or historical cleanup was performed.

All file checksums below are SHA-256 truncated to 16 hex chars, computed from
the committed state at the pin above (`git checkout ec6b538` → `sha256sum`).

---

## 0. Scope of this checkpoint

The prior checkpoint delivered the T1 vertical slice (design + prototype).
This checkpoint closes the review's outstanding items:

1. Recovery of unfinished operations after crashes, without Telegram redelivery.
2. Read-only reconciliation of the production ingress.
3. Route-by-route writer-gate coverage + transition tests.
4. Explicit clarification/failure for unsupported inputs (LLM proposer outside
   acceptance).
5. R3 reconciliation regression, remaining R4 migration evidence, R6 cutover/rollback.

---

## 1. Commits on the branch (T1 range)

```
ec6b538  feat(t1): checkpoint - crash recovery, ingress reconciliation, writer-gate coverage, R3/R4/R6 evidence
477bc1b  feat(t1): trusted ingress vertical slice on an Alembic-built database
394537c  docs(t1): trusted ingress design + writer-ownership plan
dfc976e  docs(r2): transport rebound - TrueForge cannot carry trusted per-run context (STOP)
```

(Only the T1 range `394537c..ec6b538` is new this checkpoint; `dfc976e` is the
prior R2 transport-rebound finding.)

---

## 2. Checksums (committed state @ `ec6b538`)

### Source — bridge (the prototype trusted ingress; not the production seam)
```
b6d95a7489bdbb5b  services/telegram-bridge/src/drhiro_bridge/ingress.py
3ee8c83a1d731907  services/telegram-bridge/src/drhiro_bridge/config.py
f7f5a97859bf908c  services/telegram-bridge/src/drhiro_bridge/main.py
```

### Source — API (trust boundary + durable authority)
```
2709f0533654c219  apps/api/src/drhiro_api/services/telegram_ingress.py
201ed84abca0687e  apps/api/src/drhiro_api/routers/telegram_ingress.py
98dce09548f18295  apps/api/src/drhiro_api/routers/openclaw_tools.py
1902eed55dd89c20  apps/api/src/drhiro_api/routers/meals.py
1907eb820031c8ba  apps/api/src/drhiro_api/routers/ingest.py
3352ce234189f0ff  apps/api/src/drhiro_api/config.py
27f8bc7878868a2a  apps/api/src/drhiro_api/main.py
```

### Source — MCP (decisive model-side writer gate)
```
8244cd8bb2582187  packages/drhiro-mcp/src/drhiro_mcp/sse_server.py
```

### Tests
```
beedee4100f48968  tests/test_t1_trusted_ingress_vertical_slice.py   (24)
d413956f45dd1d76  tests/test_t1_ingress_endpoint.py                 (10)
ca34087b3b826937  tests/test_t1_bridge_wiring.py                    (10)
c7376157271e8e99  tests/test_t1_writer_gate_routes.py               (27)
820e6c5cb319e225  tests/test_t1_mcp_writer_gate.py                  (10)
a21a4b83cd569cd6  tests/test_b3_identity_propagation.py
f186152c80347bf4  tests/test_legacy_new_water_coexistence.py
```

### Docs
```
0a16ba957a453eb7  T1_ingress_design_and_writer_ownership.md
790c25334b9415e5  T1_vertical_slice_evidence.md
4b31a5e7bd091d0c  T1_production_ingress_reconciliation.md
35dcf551418b76d6  R4_remaining_evidence.md
2b52d73df1e8de02  R6_t1_cutover_rollback_plan.md
```

---

## 3. Test results — the full matrix

### Default suite (single clean run)
```
331 passed, 45 skipped, 0 errors
```
The 45 skips are the env-gated Alembic suites listed below (they run with their
gate env var set).

### Gated T1 suites (Alembic-built DB `drhiro_t1_alembic`, head `9a1b2c3d4e5f`)
| Suite | Count | Result |
|---|---|---|
| `test_t1_trusted_ingress_vertical_slice.py` | 24 | **pass** (incl. crash recovery, Redis-loss, concurrency, edits, callbacks) |
| `test_t1_ingress_endpoint.py` | 10 | **pass** (fail-closed HTTP + writer gate) |
| `test_t1_bridge_wiring.py` | 10 | **pass** (bridge owns consumption; getMe verify) |
| `test_t1_writer_gate_routes.py` | 27 | **pass** (route-by-route + transitions) |
| `test_t1_mcp_writer_gate.py` | 10 | **pass** (real MCP subprocess; decisive model gate) |

### Gated R1/R4 (second Alembic-built DB `drhiro_r1r4_alembic`)
| Suite | Count | Result |
|---|---|---|
| `test_r1_nutrition_resolution.py` | 3 | **pass** |
| `test_r4_orm_alembic_parity.py` | 8 | **pass** |
| `test_r4_migration_chain.py` | 6 | **pass** |

### R3 reconciliation regression
| Suite | Count | Result |
|---|---|---|
| `test_legacy_new_water_coexistence.py` | 10 | **pass** |

---

## 4. Skipped-test reasons (exact)

The default suite skips 45 tests across four files. Every skip is deliberate and
documented by its own `pytestmark`:

| Suite | Tests | Gate | Reason |
|---|---|---|---|
| `test_r1_nutrition_resolution.py` | 3 | `DRHIRO_R1R2_ALEMBIC_DB != "1"` | Requires an **Alembic-built** DB + Redis (no `create_all`); the default suite builds via `create_all` so a passing default would not prove the R1 path on the canonical schema. |
| `test_r4_orm_alembic_parity.py` | 8 | `DRHIRO_R4_ALEMBIC_DB != "1"` | ORM↔Alembic parity can only be asserted against an Alembic-built DB; the default `create_all` suite cannot detect schema drift. |
| `test_r4_migration_chain.py` | 6 | `DRHIRO_R4_ALEMBIC_DB != "1"` | Revision-graph / chain assertions need an Alembic-built DB; running on a `create_all` DB would be meaningless. |
| `test_t1_trusted_ingress_vertical_slice.py` | 24 | `DRHIRO_T1_ALEMBIC_DB != "1"` | The decisive T1 tests must run on an **Alembic-built** DB (the suite asserts `alembic_version` is populated); excluded from the default suite so it is reported separately. |
| `test_t1_ingress_endpoint.py` | 10 | `DRHIRO_T1_ALEMBIC_DB != "1"` | Same: real ASGI app against an Alembic-built DB, reported separately. |

**Rationale:** the default suite must stay green against the shared
`create_all` test DB (fast, no external Alembic build) while the decisive
schema-sensitive suites run explicitly against Alembic-built databases and are
reported separately. No skip hides a failure: each gated suite is green when its
gate is set (shown in §3).

---

## 5. What this checkpoint demonstrates

- **Recovery without Telegram redelivery:** `recover_incomplete_operations()`
  re-drives `pending`/`processing` ops from PostgreSQL. Proven: crash after
  receipt → recovery completes one 813-kcal meal; completed/clarification ops
  untouched. Complete Redis loss does not affect replay.
- **Production ingress reconciled (read-only):** the actual production ingress is
  OpenClaw's Telegram channel (`botId 8677922871` in the durable offset store),
  **not** the repo `telegram-bridge`. The T1 minting site must therefore be on
  OpenClaw's channel in production; the bridge remains the prototype.
- **Writer ownership, corrected and covered:** the model reaches `/ingest` with a
  user JWT, so the API cannot gate it; the **decisive** control is the MCP tool
  layer (`DRHIRO_TRUSTED_INGRESS_WRITERS_DISABLED`). The `/meals` + `/tools`
  service-token writers are API-gated. 37 route/transition/tool tests cover the
  full surface.
- **Clarification/failure:** unsupported input never silently logs; clarification
  leaves no artifacts and records `ok:false`. The untested LLM proposer is **not**
  part of acceptance — all tests use deterministic proposers.
- **R3/R4/R6:** reconciliation regression green; R4 evidence adds the exact
  revision graph + production state + `activities` gap; R6 cutover/rollback plan
  written (flag-only rollback, emergency-only destructive downgrade).

---

## 6. Honest open items (acceptance gates — NOT closed)

1. **`activities` provisioning (R4-5):** confirmed missing from the Alembic chain;
   a fresh Alembic DB lacks it. A follow-on migration is required. **Open.**
2. **Production-mirror value/relationship preservation (R4-3):** not yet built.
   **Open.**
3. **No live Telegram round trip:** no deployment authorized; ingress proven
   through real bridge extraction + real ASGI app + real Postgres, not Telegram's
   servers. The LLM proposer path is unexercised (deliberately outside
   acceptance).
4. **Writer gate not activated:** all three flags default OFF; behaviour unchanged
   until cutover (which itself is gated on this review).
5. **Production seam:** the minting component must be built on OpenClaw's channel
   (per the reconciliation) before any real cutover; the bridge prototype does
   not map 1:1 to the production path.

Acceptance is therefore **not** declared. The work is committed, checksummed, and
green on the documented matrix; it awaits your review.
