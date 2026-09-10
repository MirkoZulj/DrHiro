# Incremental review artifact — trusted-ingress vertical slice (R2)

**Status:** review artifact for the incremental checkpoint. The frozen release
candidate archive is **unchanged** (`rebuilt.tar.gz`, SHA-256
`4d2bf6851c869670308ad28961d32626a12cf2b2a2af389fc3e1f6c8fe59668d`).

**No production changes, push, deployment, gate activation, credential rotation or
historical cleanup.** The disposable stack only.

This document answers the five review questions directly, separating what ran from
what is still a proposal.

---

## 1. What actually ran — real, modified, or substitute

| Component | Classification | What that means |
|---|---|---|
| **PostgreSQL** | **REAL** | Real PostgreSQL container. Schema built by the **real Alembic chain** (`migrate` service runs `alembic upgrade head`), not a lookalike. |
| **T1 API domain** | **REAL, UNMODIFIED** | The frozen candidate's `drhiro_api` source is used as-is: `get_or_create_operation`, `parse_consumption_text`, `resolve_item_nutrition`, `write_consumption`. No forks, no shims. |
| **ingress** | **REAL application code, NEW** | `deploy/disposable/app/ingress.py` is newly written trusted-side code. It is *not* the production component and is *not deployed*. It is real code exercising real domain services. |
| **OpenClaw** | **TEST SUBSTITUTE** | `openclaw_stub.py`, ~60 lines of Python. **It is not OpenClaw.** It has no Telegram channel, no spool, no model runtime. |
| **MCP** | **TEST SUBSTITUTE** | `mcp_stub.py`, a placeholder HTTP surface. Not the real `drhiro-mcp`. |
| **Telegram** | **TEST SUBSTITUTE** | `fake_telegram.py`, a local Bot API stand-in. No Telegram network traffic, no real bot, no real chat. |

### Concede the limitation explicitly

Isolation assertions were taken **from inside a placeholder container**. That proves
the *network and mount topology* denies a container with those properties access to
trusted state. It does **not** prove the real OpenClaw can run without the bot token,
the JWT secret and its spool mount. The real OpenClaw is a Node application with its
own configuration and startup requirements; demonstrating that it still functions
after credential removal is a separate piece of work, still **PENDING**, and is
correctly listed as such. The stack establishes the boundary is *enforceable*; it does
not establish *application compatibility* with the boundary.

### Confirmation on credentials and isolation from production

- The stack uses **only** the fake Telegram API (`TELEGRAM_API_URL=http://fake-telegram:8081`)
  and **throwaway** credentials (`111111:disposable-bot-token`, `disposable-only`,
  `disposable-admin-token`). None is a production value, and none was read from the
  production host.
- **No production bot polling:** the real bot token appears nowhere in this stack;
  `getUpdates` is served entirely by the local fake.
- **No shared production storage:** the stack has its own Docker volumes and its own
  database (`drhiro_t1` inside the `drhiro-iso` project). The production
  `drhiro_openclaw_state` volume and production database were never mounted or
  connected. The diagnostic that read production only ever ran `docker inspect`
  (read-only) and printed key **names**.

---

## 2. What was persisted — real service output, not just an operation row

The earlier slice wrote a `ConsumptionOperation` row and fabricated
`result_json={"items": 1}`. That was a stub, and this question correctly caught it.
The slice now calls the genuine path:

```
parse_consumption_text   -> real parser (quantities, units, beverage classification)
get_or_create_operation  -> trusted Telegram identity, fail-closed, atomic ON CONFLICT
resolve_item_nutrition   -> real DB-first resolution over a seeded catalog (offline)
write_consumption        -> THE single write path
```

`write_consumption` is what creates meal rows, `meal_items`, the liquid projection
(`Measurement` + `BeverageMeasurement`) and the 6-nutrient totals.

### Observed output for `200 g steak and 0.5 l beer`

```json
{"meals": [{"meal_type": "snack",
            "totals_json": {"kcal": 757.0, "protein_g": 54.5, "carbs_g": 18.0,
                            "fat_g": 36.0, "fiber_g": 0.0, "sodium_mg": 130.0}}],
 "meal_items": [{"display_name": "steak", "grams": 200.0, "volume_ml": null},
                {"display_name": "beer",  "grams": 500.0, "volume_ml": 500.0,
                 "beverage_category": "beer"}],
 "measurements": [{"metric_type": "water", "unit": "ml",
                   "value_json": {"amount_ml": 500.0, "category": "beer"}}],
 "beverage_measurements": 1}
```

Arithmetic check: steak 200 g → 542.0 kcal / 52.0 p / 36.0 f, beer 500 ml → 215.0
kcal / 2.5 p / 18.0 c; totals 757.0 / 54.5 / 18.0 / 36.0 / 0.0 / 130.0. Correct.

**Terminology, stated precisely:** in this codebase a "projection" is the linked
liquid projection — the `Measurement` row plus the `BeverageMeasurement` 1:1 link that
makes a drink contribute to the liquid tile while remaining a meal item. That is what
is asserted. There is no separate `daily_aggregates` writer in the logging path, and
none is claimed.

### Duplicate and concurrency assertions over those outputs

- **Duplicate delivery** (same `message_id` delivered twice): exactly 1 meal, 2 meal
  items, 1 measurement, 1 beverage link, byte-identical `meal_items`, totals unchanged.
- **Concurrent writers** (4 processes racing on one Telegram identity at the
  persistence layer): `creations: 1`, `duplicates_reported: 3`, `errors: []`,
  and final state exactly 1 consumption / 1 meal / 2 items / 1 measurement with
  correct totals.
  This deliberately does **not** start a second Telegram poller — the architecture
  keeps exactly one consumer, so a second `getUpdates` loop would test something we
  do not intend to support. It tests what actually happens when two writers race on
  one identity (a stolen claim after lease expiry, or a restarted worker).

---

## 3. Which credentials were rotated — and which were not

**Blunt answer: the Ed25519 ingress-envelope keys are not production credentials at
all, and their rotation proves nothing about the exposed JWT secret.** The earlier
"rotation test" wording invited exactly that confusion.

| Credential | Type | Rotated / tested in this stack? | Notes |
|---|---|---|---|
| ingress envelope key | **Ed25519, NEW, PROPOSAL** | **Mechanism tested** (retired kid rejected even with a valid signature; kid abuse, `alg=none`, HMAC confusion, `jwk`/`jku` refused) | Does not exist in production. Tested against the *disposable* key set. |
| `DRHIRO_JWT_SECRET` | **HS256 symmetric, PRODUCTION, EXPOSED** | **NOT rotated. NOT tested.** | Present in the model-accessible gateway *and* MCP. Symmetric: any holder can mint a valid user token. The API **cannot** distinguish who minted an otherwise valid token under this key. |
| `DRHIRO_SERVICE_TOKEN`, `DRHIRO_OPENCLAW_SERVICE_TOKEN`, `OPENCLAW_GATEWAY_TOKEN` | service tokens, PRODUCTION | **NOT rotated** | Service-to-service auth; also in the model-accessible runtime. |
| `DRHIRO_TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_TOKEN` | bot token, PRODUCTION | **NOT rotated** | In the model-accessible gateway. Grants full control of the bot. |
| `POSTGRES_PASSWORD` | database, PRODUCTION | **NOT rotated** | In the model-accessible gateway. |
| `MINIO_ROOT_PASSWORD` | object storage, PRODUCTION | **NOT rotated** | **Root** object-storage credential in the model-accessible runtime. |
| `NOUS_API_KEY`, `DRHIRO_LLM_API_KEY` | provider API keys, PRODUCTION | **NOT rotated** | Billable provider credentials. |
| `DRHIRO_TELEGRAM_ID` | identifier, PRODUCTION | n/a | Present in MCP; a bare identifier, lower severity but still unnecessary. |

### Expanded inventory — what each component legitimately needs

Classification: **R** = required, **M** = must be *moved into* the trusted side, **X** = should be removed from that component.

| Credential | drhiro-ingress (trusted, proposed) | OpenClaw gateway | MCP | T1 API | Postgres | MinIO |
|---|---|---|---|---|---|---|
| Telegram bot token | **R** (sole poller) | **X** | **X** | X | – | – |
| `DRHIRO_JWT_SECRET` | X | **X** | **X** | **R** (verify) | – | – |
| ingress envelope **private** key | **R** | **X** | **X** | X | – | – |
| ingress envelope **public** key | X | – | – | **R** (verify) | – | – |
| service tokens | **M** (its own) | **M** | **M** | **R** | – | – |
| `POSTGRES_PASSWORD` | **R** | **X** | **X** | **R** | **R** | – |
| `MINIO_ROOT_PASSWORD` | X | **X** | **X** | – | – | **R** |
| `NOUS_API_KEY` / `DRHIRO_LLM_API_KEY` | X | **M** (its own LLM key only) | **X** | **R** (if it calls the model) | – | – |
| `OPENCLAW_GATEWAY_TOKEN` | X | **R** | – | X | – | – |
| `DRHIRO_TELEGRAM_ID` | R | X | **X** | R | – | – |

### Dependency-aware removal / rotation plan (NOT authorized, not executed)

Ordering matters: rotating in the wrong order breaks the running system, and leaving
the old value alive makes the rotation cosmetic.

1. **Freeze the exposure** — stop the model-accessible runtime from *reading* new
   values before any new value is minted. Remove the env entries and any mounts that
   carry them from `openclaw` (and MCP), then recreate those containers.
2. **Rotate `DRHIRO_JWT_SECRET`** — mint a new key, deploy to the issuing/verifying
   API and legitimately authorized callers **together**, and **reject the retired key
   with no dual-accept window**. Reissue replacement credentials for every legitimate
   caller. Deleting an environment variable alone invalidates nothing: previously
   minted tokens and copies of the old key stay valid until the key is retired.
3. **Rotate the bot token** via BotFather, and hand the new value **only** to the
   trusted ingress. Stop OpenClaw's polling in the same change, so exactly one
   consumer exists at every instant.
4. **Rotate `POSTGRES_PASSWORD`** and `MINIO_ROOT_PASSWORD`; scope a non-root
   object-storage user for whatever actually needs it.
5. **Rotate provider keys** (`NOUS_API_KEY`, `DRHIRO_LLM_API_KEY`) — these are billable
   and should be rotated on exposure even though nothing suggests misuse.
6. **Rotate service tokens**, one service at a time, updating both ends.
7. **Verify** retirement: tokens signed with the retired key must be **rejected**, and
   unauthorized scope/audience claims refused. Then re-run the read-only diagnostic to
   confirm the credentials are gone from the model-accessible runtime.

Exposure warrants remediation planning. It is **not** evidence of abuse: presence in
a runtime establishes an exposure, nothing more. No secret values appear in this
document or in any artifact; the diagnostic prints names and mount modes only.

---

## 4. Where trusted spool ownership ends up

The earlier phrasing "real OpenClaw spool wiring pending" is **reconciled and
withdrawn** as written, because it implied keeping OpenClaw in the trusted path.

**Architecture decision (unchanged in direction, now stated unambiguously):**

- Trusted polling, spool ownership and signing belong to **`drhiro-ingress`** — a
  separate trusted component. It holds the bot token, the private signing key and the
  trusted spool. It performs `getMe`/`getUpdates` and is the **single** Telegram
  consumer.
- **OpenClaw keeps none of it.** No bot token, no signing key, no shared trusted mount.
  It becomes a model-accessible component that receives an already-authenticated turn.
- **Reusing spool *code* is fine; reusing the spool *mount and token* is not.** The
  durable-receipt/claim logic in this slice is precisely the code worth porting into
  the ingress. What must not be restored to OpenClaw is access to the spool volume and
  the bot credential.

The concrete work still **PENDING**: porting the receipt/claim/lease logic into the
real ingress deployment, wiring it to whichever spool mechanism the ingress owns,
and the upgrade fail-safe (build-hashed module + version whitelist + fail-closed) so a
container upgrade cannot silently re-grant access.

---

## 5. How `unknown` replies are resolved

`unknown` means a send was attempted and we cannot know whether it arrived. The
resolution action is implemented and tested against the disposable stack.

**Endpoint:** `POST /admin/reply/resolve` on the trusted ingress admin surface.

**Authenticated:** `Authorization: Bearer <INGRESS_ADMIN_TOKEN>`, compared with
`hmac.compare_digest`. The token exists only in the trusted ingress and is never
mounted into a model-accessible container. **An unset token disables resolution
entirely (fail closed)** rather than leaving it open. Missing/incorrect token → `401`.

**Ownership-checked:** the reply is bound to a chat via `reply_owners`. The caller's
claimed chat must match the binding, else `403 not_owner`. A caller cannot resolve or
resend another user's reply.

**Actions:**

- `acknowledge` — records that the ambiguity is accepted and no resend will happen.
  Terminal state `resolved_acknowledged`. Sends nothing; consumption untouched.
- `resend` — an **explicit** new delivery attempt. Requires
  `duplicate_risk_ack: true`; without it → `400 duplicate_risk_not_acknowledged`,
  because **delivery may already have occurred**. The audit row states this verbatim:
  *"explicit resend after ambiguous delivery; delivery may already have occurred"*.

**Audit trail:** every resolution writes immutable rows to `reply_audit`
(`actor`, `action`, `from_state`, `to_state`, `delivery_attempt`, `detail`,
`created_at`). A resend creates **two** rows — one when the attempt is made, one when
its outcome is known — so an interrupted resend is itself traceable.

**Never recreates the consumption:** resolution touches only `reply_outbox` and
`reply_audit`. It does not call the write path. Asserted directly: after acknowledge
*and* after resend, consumption count stays 1 and meals/items/measurements are
byte-identical.

**Concurrency:** the outbox row is `SELECT ... FOR UPDATE` with the state re-checked
under the lock, so two simultaneous resolutions cannot both act. A racing pair yields
exactly one success and one `409 state_changed`; if the acknowledgement wins, the
losing resend sends nothing (asserted). Resolving a reply that is **not** in a
resolvable state (already `sent`) → `409 state_changed`.

**A real defect this test found (and fixed).** The first implementation had a
time-of-check/time-of-use race: the resend released its row lock *before* recording
the new state, because the network send happens outside the lock. A resolver racing
in that window still saw `unknown`, passed its own state check, and both actions
succeeded. The concurrency test caught exactly that — two successes instead of one.
The fix transitions the row **out of the resolvable set** (`in_flight`) inside the
locked transaction, *before* the lock is released, and stamps `in_flight_at` so a
crashed resend is later converted to `unknown` by the normal recovery pass rather than
stranding in `in_flight` forever. Locking alone was not sufficient; the state claim
had to happen while the lock was held.

**Still PENDING:** exposing this through a real user-facing surface (the stack exposes
the trusted admin endpoint only), and operator tooling/runbook for the `unknown`
queue.

---

## 6. Executed vs pending

**EXECUTED and passing (all on the disposable stack or disposable databases):**

| Suite | Result | Gate |
|---|---|---|
| `tests/test_t1_isolated_ingress_stack.py` | **28 passed** | `DRHIRO_ISOLATED_STACK=1` |
| `tests/test_r1_stack_isolation.py` | **14 passed** | in the default suite |
| `tests/test_r2_trusted_key_set.py` | **17 passed** | default |
| `tests/test_r4_activities_migration.py` | **11 passed** | `DRHIRO_ACTIVITIES_MIGRATION_DB=1` |
| default suite | **363 passed / 85 skipped / 0 errors** | — |

The stack suite includes the real-output tests, duplicate-delivery and concurrent-writer
assertions, and the seven resolution tests (authentication, ownership, acknowledge,
resend refusal without acknowledgement, traceable resend that never recreates the
consumption, concurrent resolution, and refusal on a non-resolvable state).

Recorded environment: PostgreSQL **16.15** (Alpine, disposable) — the local test
databases are 16.14; both are 16.x, consistent with the catalog finding in §3 of the
prior revision. Alembic revision at head in the stack: **`b7c8d9e0f1a2`**, produced by
the real chain. Image tags and immutable digests are captured in
`evidence/incremental_evidence.txt`.

**PENDING (not implemented — do not read as accepted):**

- Production container split; `drhiro-ingress` is a role in a disposable stack.
- Production rotation cutover for **every** credential in §3, including
  `DRHIRO_JWT_SECRET`.
- Porting receipt/claim logic into the real ingress; wiring its real spool; upgrade
  fail-safe.
- Real OpenClaw compatibility with the boundary (application, not topology).
- User-facing surface for `unknown` resolution (the stack exposes the trusted admin
  endpoint only).
- Production-mirror value preservation (still open).
- R6 cutover / rollback plan (still open).
- R1 partial · R2 production rotation open · R4 production-mirror preservation open.

## 7. Open items (unchanged)

- **Production-mirror value preservation** — still open; not run.
- **R6 cutover / rollback plan** — still open.
- R1 partial · R2 production rotation open · R4 production-mirror preservation open.
- Real OpenClaw compatibility with the boundary (application, not topology).
- Porting receipt/claim logic into the real ingress + upgrade fail-safe.

**The passing disposable-stack tests are useful evidence, not production acceptance.**
