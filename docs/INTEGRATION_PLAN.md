# INTEGRATION PLAN — publishing the full drHiro application

**Phase 2 deliverable.** This document is the integration plan that layers the
full drHiro app logic onto the existing published stack. It describes the real
architecture, the end-to-end data flow, storage/retention/wipe behaviour, the
TrueForge logging difficulties and their workarounds, the environment-driven
config contract, and the proposed repo layout.

**Status:** PLAN ONLY — nothing copied to a branch yet. Awaiting review.

---

## 1. Authoritative architecture (supersedes prior docs)

The private `/opt/drhiro/docs/architecture.md` predates the TrueForge migration
and described OpenClaw as the conversational orchestrator. The **current,
authoritative** division of labour is:

| Layer | Component | Responsibility |
|---|---|---|
| **Comms** | **OpenClaw** | Telegram message transport + bot interaction (the `drhiro` agent shell), shim that relays user turns |
| **Agent runtime** | **TrueForge** | The agent loop that runs the model (Qwen), calls tools, and processes/logs each turn's data |
| **App logic** | **drHiro Core** | FastAPI core + worker + rules + health schema + nutrition + Android Bridge; owns users, consent, measurements, meals, reminders, goals, audit, APIs |

The published **hackathon shell** (already on GitHub, tag `hackathon-submission`)
is the packaging/deployment layer around this same stack. It is **not** a
competing runtime. Phase 3 will extend that shell with the real app logic; it
will **not** remove OpenClaw or TrueForge.

### Where the real logic lives today (private, VPS `/opt/drhiro`)

```
apps/api              FastAPI core (models, routers, security, services) — drHiro Core
apps/worker           RQ worker + scheduler (reminders, alerts, aggregates)
apps/web              React/Vite web + Telegram Mini App
apps/android-bridge   Kotlin Health Connect app
packages/health-schema   canonical metric types + value schemas
packages/rule-engine      deterministic calculations + versioned rules
packages/nutrition-core   USDA / private catalog / DuckDuckGo-fallback food data
packages/drhiro-mcp       MCP server the agent uses to call drHiro tools
openclaw/             OpenClaw agent definition + skill-drhiro (Telegram facing)
infra/                docker-compose, Caddy, backup, monitoring, .env(.prod/.example)
docs/                 architecture, data-dictionary, runbook, threat-model, rule-governance
```

---

## 2. End-to-end data flow (Telegram → ... → Telegram)

```
 User in Telegram
   │  text / command / photo
   ▼
 OpenClaw gateway (Telegram transport)
   │  → tf-shim relays the user turn (writes tfshim:last_user_text to Redis for recovery)
   ▼
 TrueForge agent runtime
   │  model (Qwen) runs the agent loop:
   │    - decides which drHiro tool to call (log_meal, log_water, list_meals, ...)
   ▼
 MCP (packages/drhiro-mcp)  ── narrow, preloaded tool schemas
   │  inline-JWT → POST http://<api>/api/v1/...  (tools/data processing)
   ▼
 drHiro Core API (FastAPI)  ── auth, validation, tenant isolation
   │  ├─ PostgreSQL 16   (users, consent, measurements, meals, reminders, goals, audit)
   │  ├─ Redis          (jobs, drafts, shim-recovery keys)
   │  └─ MinIO          (food/meal photos, exports)
   ▼
 result / totals / generated answer
   │  model composes the human reply from tool output
   ▼
 OpenClaw gateway
   ▼
 User in Telegram
```

**Deterministic boundaries:** the rule engine and calculations are pure Python
in `packages/rule-engine`; the LLM may explain a rule result but never creates
thresholds or alters stored values. Photo-derived data is a draft until
confirmed. The agent is told repeatedly (see §5) to **verify against the DB
before claiming a value was logged** — fabricated totals are a known failure
class (see §6).

---

## 3. Storage, retention, wipe

| Store | Contents | Retention / wipe |
|---|---|---|
| PostgreSQL `drhiro` | health measurements, meals, users, consent, reminders, goals, audit log | daily encrypted `pg_dump`; monthly restore test. A self-hoster wipes by dropping the DB or restoring a pre-cleanup dump. Per-user delete paths exist in the API. |
| Redis | job queues, meal drafts, shim-recovery keys (`tfshim:last_user_text`, 15-min TTL) | transient; `FLUSHDB` clears. |
| MinIO | food/meal photos, exports | object versioning; wipe by removing the bucket. |
| OpenClaw workspace | persona/example markdown only (USER.md is an *example* a deployer edits) | stateless w.r.t. health data. |

**Guarantee to document:** because the app is self-hosted, **all** health data
stays on the deployer's own Postgres/Redis/MinIO volumes. No third party holds
it. Wiping one's data = dropping/restoring the local DB + clearing Redis/MinIO.
Nothing in the repo or the git history ever contains real health rows (Phase 1
audit §2.5 confirmed production data lives in container volumes, not the tree).

---

## 4. Environment-driven configuration — preserve the installer contract

All runtime configuration must be **environment-driven**. No hardcoded
endpoints, models, paths, or credentials in logic. The installer contract stays
**exactly five inputs** and nothing more:

1. Telegram bot token
2. Authorized Telegram username
3. AI backend base URL
4. AI backend API key (or local placeholder)
5. Model name

Everything else derives from those + optional overrides in `.env` (DB URL, JWT
secret, MinIO keys, MCP URL, food-service URL). The Phase 1 audit found several
hardcoded identifiers that must be converted to env before migration
(`intelligent_food_search.py` Pi SSH creds, `openclaw_tools.py` base URL,
`ApiClient.kt` base URL, `MainActivity.kt`/`DeviceLinker.kt` "Kresimir", the
`984523234` user id in skill examples). None of these ship as-is.

---

## 5. Agent / logging discipline (standing rules the full app encodes)

- The bot **must do the work itself** and **never claim something was logged
  without a DB check** (verified-means-verified).
- **`list_meals` / `list_data_points`** exist so the agent can read its own
  logs and self-correct instead of guessing.
- Correcting a value = the **generic data-point / meal-item edit tools**, not
  delete-and-re-log (which trips the shim-recovery trap).
- External sources (USDA, DuckDuckGo fallback) return candidates; the agent
  logs real matches or an honest 0/unmatched, never an invented number.

---

## 6. TrueForge / logging difficulties this week + workarounds (honest record)

These are recorded so a future maintainer does not re-derive them, and so the
published "known limitations" section is truthful.

1. **Schema-echo tool arguments.** The Qwen model via TrueForge repeatedly
   emitted the tool's own input schema as its argument values
   (`{"title": {"__type":"string"}}`). Root causes included one malformed MCP
   tool schema poisoning the whole tool-definition pass (`tool_definitions: 0`)
   and a genuine small-model tool-calling ceiling. **Workarounds:** fixed the
   malformed schema; added `_unwrap_schema_echo` → `_rescue` → Redis
   shim-recovery to every tool handler; added `_deep_str` for double-nested
   args; verified `tool_definitions` token count when every call fabricates args.
2. **Model idle timeout.** TrueForge/OpenClaw's 120s idle watchdog aborted slow
   Qwen calls. **Workaround:** raised `timeoutSeconds` on both providers and set
   `agents.defaults.timeoutSeconds` (OpenClaw config hot-reloads).
3. **Bot "could not log" when the write actually succeeded.** Direct API writes
   persisted correctly while the tool-call path failed — a model-capability
   limit, not a server bug. **Workaround:** deterministic parsers for
   high-frequency ops (water/activity/meal) and direct-API writes as the
   reliable stopgap; always confirm a 200 with a fresh DB `SELECT`.
4. **Shim-Redis recovery fragility.** `tfshim:last_user_text` is a single key
   (15-min TTL) overwritten by the latest turn, so recovery only helps the
   first attempt. **Workaround:** design deterministic parsers around intent,
   not re-reading that key; applied an `_extract_weight_correction` /
   `_looks_like_edit_instruction` guard so a directive is never recovered as a
   blank meal.
5. **Stale / drifted containers.** Hand-run containers (`tf-shim`) drifted from
   build source (a missing `import re` crashed every reply). **Workaround:**
   treat the running file as canonical, save it back to build source before
   patching + rebuilding; recreate with the original env verbatim.
6. **Redis DB mismatch.** `tf-shim` writes recovery keys to db 3; a reader on
   db 0 never fires. **Workaround:** a Redis DB number in a URL is part of the
   contract — verify both sides match.
7. **Fabricated totals over empty logs** (the dinner incident). The bot narrated
   a confident calorie total over items that had logged as 0-kcal unmatched
   placeholders. **Workarounds:** fixed the meal-text parser to strip the shim
   `[date]` prefix so the directive clause isn't swallowed as one item; made
   `list_meals` available; corrected the affected DB row with real matched
   values and recomputed totals.

**Net:** the published app must document these as known limitations and encode
the workarounds (recovery chains, DB-verified claims, deterministic parsers,
env-driven config) as the operating contract, so a fresh self-hoster hits fewer
surprises and a maintainer has the honest engineering record.

---

## 7. Proposed repo layout (for review)

Extend the existing published repo (hackathon shell) rather than restructure it.
Proposed layout alongside the current `services/`, `agent/`, `infra/`, `docs/`:

```
MirkoZulj/DrHiro
├─ services/
│  ├─ drhiro-tools/        (existing — MCP tool server)
│  ├─ telegram-bridge/     (existing)
│  └─ drhiro-core/         (NEW — migrated from private apps/api + apps/worker)
│     ├─ api/              (FastAPI core + alembic migrations)
│     ├─ worker/           (RQ worker + scheduler)
│     └─ packages/         (health-schema, rule-engine, nutrition-core) [or top-level]
├─ apps/
│  ├─ android-bridge/      (NEW — Kotlin Health Connect app source; no baked URL)
│  └─ web/                 (NEW — React/Vite web + Mini App source; build from env)
├─ agent/
│  └─ openclaw/            (NEW — openclaw.json.example + skill-drhiro + workspace examples)
├─ infra/                  (existing docker-compose + .env.example; extend services)
├─ docs/                   (existing + FULL_PUBLISH_AUDIT + INTEGRATION_PLAN)
└─ (root) README, ARCHITECTURE, SECURITY_AND_PRIVACY, DECISIONS, .gitignore
```

Key decisions embedded:
- **`openclaw.json` ships only as `openclaw.json.example`** — the real config
  has live API keys (Phase 1 audit).
- **`.env` files never ship** — `.env.example` documents the full set.
- **APK binaries not shipped** — `apps/android-bridge` is source; a deployer
  builds + signs their own APK. No keystore/signing material in repo.
- **No `dist/`, `node_modules/`, `*.egg-info`, `__pycache__`** — extended
  `.gitignore`.
- **`intelligent_food_search.py` Pi-SSH path** — excluded; the DDG-via-host
  fallback (USDA primary → DuckDuckGo on the VPS) replaces it and is
  env-configured.
- **Persona/workspace markdown ships as examples** with placeholders
  (`<TELEGRAM_USER_ID>`), not the real Telegram id or a hardcoded name.

---

## 8. Open questions for review (before any Phase 3 copy)

1. Confirm the repo layout in §7 (naming, whether `packages/` nests under
   `services/drhiro-core/` or sits at top level).
2. Confirm the Pi-SSH food path is fully superseded by the DDG-via-host
   fallback (I recommend yes — the DDG path is built + verified).
3. Confirm we exclude the built APK binaries and publish only Android source.
4. Confirm the OpenClaw persona/workspace markdown ships as deployer-editable
   examples (with placeholders) rather than omitted.

---

*End of integration plan. Nothing copied to any branch. Awaiting review before
Phase 3 (migration onto a feature branch).*
