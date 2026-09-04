# FULL PUBLISH AUDIT — drHiro complete application

**Scope:** publishing the complete drHiro application (all real agent logic) to
`MirkoZulj/DrHiro`, extending the hackathon shell. This audit inventories the
private codebase at `/opt/drhiro` on the VPS and categorises every file as
**safe-to-copy / needs-review / never-copy** before anything is migrated.

**Audit date:** 2026-09-04
**Auditor:** Jeeves (read-only; nothing copied)
**Private source audited:** `/opt/drhiro` (VPS 144.91.107.8), ~156 MB incl.
artifacts; source (excluding node_modules/dist/venv) ≈ 62 `.py`, 22 `.tsx`, 9
`.kt`, plus markdown/shell/config.

---

## 1. Architecture (authoritative)

- **OpenClaw** = Telegram communications layer (message transport, bot interaction).
- **TrueForge** = agent runtime responsible for logging and data processing.
- **drHiro app logic** = the API / worker / MCP / rules / health-schema /
  nutrition code under `/opt/drhiro/apps` and `/opt/drhiro/packages`.

The hackathon shell was the **packaging/deployment layer** around this same
stack — **not** a competing runtime. It must not be removed or migrated away.

---

## 2. Audit findings (high-signal, from greps)

### 2.1 Credentials / secrets — NEVER-copy unless env-driven

| File | Finding | Verdict |
|---|---|---|
| `apps/api/src/drhiro_api/services/intelligent_food_search.py:99-102` | **Hardcoded** `sshpass -p Mile0001` + `mirko@100.75.194.51` (Pi SSH). Real password + host. | **never-copy as-is** → must become env-driven or replaced (this is the deprecated Pi-SSH food path). |
| `openclaw/openclaw.json` | Contains live `apiKey` for `trueforge` and `qwen-local` providers. | **never-copy** → ship as `.example` with placeholders only. |
| `infra/.env`, `.env.prod`, `.env.prod.bak-*`, `settings.json` | Live secrets (JWT, DB URL, USDA key, service token, bot token). | **never-copy** (all are gitignored runtime state). |
| `infra/.env.example` | Safe template (placeholders). | **safe-to-copy** (already public in hackathon shell). |

### 2.2 Personal identifiers — must be scrubbed / parametrised

| File | Finding | Verdict |
|---|---|---|
| `apps/android-bridge/.../MainActivity.kt:215,231` | `"Linked as Kresimir!"` / `"Device linked (Kresimir)"` — real user name hardcoded in UI. | **needs-review** → replace with dynamic user display name. |
| `apps/android-bridge/.../net/DeviceLinker.kt:39` | `device_name: String = "Kresimir"` hardcoded default. | **needs-review** → env/config-driven. |
| `openclaw/skills/skill-drhiro/SKILL.md` + `scripts/drhiro_api.sh` | Telegram user id `984523234` hardcoded in example calls. | **needs-review** → replace with `<TELEGRAM_USER_ID>` placeholder. |
| `apps/api/src/drhiro_api/routers/openclaw_tools.py:452` | Hardcoded `https://vmi3413468.contaboserver.net/drhiro-app`. | **needs-review** → env-driven base URL. |
| `apps/android-bridge/.../net/ApiClient.kt:26` | Hardcoded `baseUrl = "https://vmi3413468.contaboserver.net/drhiro/api/v1"`. | **needs-review** → env-driven (already has a setter; make the default a placeholder). |
| `xiaomi_csv.py` / `test_import_csv.py` | `ACTIVITY_1234567890.csv` etc. and test BP rows are **synthetic fixtures** (fake IDs, fake timestamps). | **safe-to-copy** (already synthetic). |

### 2.3 Device / infra names

- `apps/android-bridge/.../MainActivity.kt` mentions "Windows Qwen box" only in
  the workspace prose (anonymised). No Tailscale name in source other than the
  Pi IP above. Grep for `100.119.202.68` found **no** source hits outside the
  `.env` files → the Windows box IP is not in tracked logic. Good.

### 2.4 OpenClaw persona/workspace — already anonymised

- `openclaw/workspace/drhiro/USER.md` uses "User A and User B", "household of
  two in Zagreb", "Xiaomi Mi Smart Band 9 ×2", "OMRON BP5350". No names/IDs.
- `IDENTITY.md` describes the drHiro persona (Japanese-calm voice). No personal
  data.
- **Verdict:** safe-to-copy, with a note that "Zagreb" + "household of two" is a
  mild footprint; acceptable for a public self-hostable project but flag in
  SECURITY_AND_PRIVACY that USER.md is an *example* a deployer edits.

### 2.5 Health / chat data stores, logs, exports

- `find` for logs/exports/data/raw/uploads under `/opt/drhiro` returned only
  `infra/backup` and `openclaw/workspace`. **No production health rows, chat
  logs, or session exports live inside the source tree.**
- Postgres data (drhiro DB), Redis state, and MinIO objects live in **containers
  / volumes**, not the repo — excluded by construction. Confirm they are not
  bind-mounted into any copied path (see Risks).
- APK files `drhiro-bridge-0.1.*.apk` are **built artifacts** → safe-to-copy only
  if they contain no baked-in real endpoint (they carry `ApiClient` default, so
  the signed APK with the real URL must be treated as **never-copy**; the repo
  should build its own from env). No `.jks`/`.keystore`/`.p12` under
  `android-bridge` → signing material is external (good).

### 2.6 Third-party code / licenses

- **Python:** FastAPI, SQLAlchemy, Pydantic, Alembic, httpx, requests, BeautifulSoup,
  PyJWT, psycopg2, uvicorn, pytest. **Node:** React, Vite, TypeScript.
  **Android:** Jetpack Compose, Health Connect client. **Runtime:** OpenClaw,
  TrueForge (TrueForge is MIT, pinned in installer).
- Licenses are declared in each app/package `pyproject.toml` /
  `package.json`; no vendored third-party source copies were found in the source
  tree (dependencies are installed, not committed). Confirm no LICENSE file
  omissions before merge.

---

## 3. Categorisation summary

### Safe-to-copy (after placeholder scrub)
- `apps/api/src/drhiro_api/` — routers, models, schemas, services **except**
  `intelligent_food_search.py` SSH block; alembic migrations; tests (synthetic).
- `apps/worker/src/`, `apps/web/src/` (React source; build from env), `infra/docker/`
  Dockerfiles, `infra/docker-compose*.yml` (env-var references), `infra/openclaw/`
  as `.example`.
- `packages/*` source: `health-schema`, `nutrition-core`, `rule-engine`,
  `drhiro-mcp`, `api-client-ts`, `telegram-ui`.
- `openclaw/skills/skill-drhiro/` (scrub the numeric user id to placeholder).
- `openclaw/workspace/drhiro/*.md` (anonymised; mark USER.md as example).
- Docs `docs/*.md`, `.github/workflows/ci.yml`, AGENTS.md, README, .gitignore.

### Needs-review (before copy)
- The 4 hardcoded-identifier files in §2.2 (name, user id, two base URLs) →
  convert to env-driven / placeholders.
- `intelligent_food_search.py` — decide whether the DDG-via-host path (already
  built and working) fully supersedes the deprecated Pi-SSH Google path; if so,
  copy the DDG version and exclude the SSH block.
- `apps/web/dist/`, `node_modules/` — **exclude from git** (build artifacts); add
  to `.gitignore` if not already.

### Never-copy (exclude from branch, do not import history)
- `infra/.env`, `.env.dev`, `.env.prod`, `.env.prod.bak-*`, `settings.json`
  (live secrets).
- `openclaw/openclaw.json` (live API keys) — provide `.example` only.
- `apps/android-bridge/drhiro-bridge-0.1.*.apk` (baked real endpoint) — build
  fresh, do not ship binaries.
- Any `*.egg-info`, `__pycache__`, `.pytest_cache`, `dist/`, `node_modules/`.
- The original project's `.git` history (never import).

---

## 4. Risks & mitigations

1. **Real Pi password in source** (`intelligent_food_search.py`). Must be removed
   before any merge; the file must read credentials from env or be deleted if the
   DDG path supersedes it. **Blocker for merge.**
2. **Real user name "Kresimir"** baked into the Android bridge. A published APK
   built from the repo would carry it unless scrubbed. Fix before public release.
3. **Health data hygiene:** confirm no Postgres/Redis/MinIO volume is bind-mounted
   under a repo path that would get committed. Production data lives in container
   volumes; keep it that way. Add data-volume paths to `.gitignore`.
4. **OpenClaw provider keys** in `openclaw.json`. Ship schema/example only.
5. **The hackathon shell already on GitHub** (`MirkoZulj/DrHiro`, tag
   `hackathon-submission`) must stay reproducible — the new full-logic work goes
   on a **feature branch** (Phase 3), never rewriting the frozen history.
6. **TrueForge/OpenClaw version pinning** — the installer already pins TrueForge;
   ensure OpenClaw + drHiro package versions are declared so the published app is
   reproducible.
7. **"Zagreb / household of two" footprint** in USER.md — acceptable but document
   that it is example persona data a deployer replaces.

---

## 5. What is NOT yet scanned (deferred to Phase 4)

- Full **history** scan (the private repo has no history to import; the public
  repo history is the hackathon shell — verify no stray secrets were ever pushed).
- Live **container volumes** (Postgres/Redis/MinIO) — out of scope for repo copy;
  verify no bind-mount overlaps a repo dir.
- **Signed-APK / pairing pipeline** re-test (Phase 4).
- Full **license** review of every transitive dependency (package manifests
  present; deep license audit deferred).

---

*End of audit. Nothing has been copied or changed in the private codebase.
Awaiting review before Phase 2 (integration plan).*
