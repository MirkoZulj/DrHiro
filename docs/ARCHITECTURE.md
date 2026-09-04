# Architecture — drHiro on TrueForge

## Overview

drHiro is a privacy-first personal health information agent. A user talks to a Telegram
bot; the conversation is carried by TrueForge, which runs the whole agent execution loop;
and the agent reaches a small set of tools that operate only on synthetic data.

The guiding principle: **TrueForge owns the intelligence, drHiro owns the narrow,
consent-scoped tool surface.** No reasoning, tool orchestration, approvals, or session
state lives in the transport layer.

```
┌────────────┐   long polling    ┌──────────────────┐
│  Telegram  │ ─────────────────▶│  telegram-bridge │  (transport only)
│  (user)    │ ◀─────────────────│  authorized user │
└────────────┘     messages      └────────┬─────────┘
                                          │ HTTP/SSE (sessions, turns, approvals)
                                          ▼
                                  ┌──────────────────┐
                                  │     TrueForge     │  (agent execution loop)
                                  │ model · tools ·   │
                                  │ approvals ·       │
                                  │ context · sessions│
                                  └────────┬─────────┘
                                           │ MCP (tools)
                                           ▼
                                  ┌──────────────────┐
                                  │  drhiro-tools     │  (4 tools, synthetic)
                                  │  :3100 (SSE)      │
                                  └──────────────────┘
```

## Components

### 1. `telegram-bridge` (services/telegram-bridge)

A dependency-free Python service (stdlib only) that:

- **Long polls** the Telegram Bot API. It never uses a webhook.
- **Enforces webhook/polling exclusivity** before polling starts: it calls
  `getWebhookInfo`; if a webhook URL is set, it raises a `WebhookConflictError` and
  requires explicit operator confirmation before `deleteWebhook`.
- **Gates by username (and later by resolved user id).** Only the configured
  `TELEGRAM_ALLOWED_USERNAME` reaches the agent; everyone else is told they are
  unauthorized. After a successful verification the sender's numeric id is also trusted.
- **Serves the drHiro Bridge APK.** `/apk`, `/apkinfo`, `/status`, and `/help` distribute
  the signed Android companion APK. Delivery validates the SHA-256 and size, uploads once,
  persists the Telegram `file_id` (mode 600), and resends by `file_id` on later requests.
  `/apk` and `/apkinfo` are restricted to the authorized user.
- **Secure Bridge pairing.** `/apk` and `/pair` create a single-use, time-limited pairing
  token bound to the authorized user's Telegram id, and post a "Connect drHiro Bridge"
  inline button carrying a `drhiro://pair` deep link. A background HTTP server
  (`pairing_http`, port `PAIRING_HTTP_PORT`) handles `/pair/exchange`, `/pair/verify`,
  `/pair/devices`, and `/pair/revoke`. `/devices` and `/revoke` manage the user's linked
  devices. HTTPS is required for remote endpoints; HTTP is allowed only for trusted-LAN
  dev mode with a visible warning.
- **Maps conversations to persistent TrueForge sessions** (one session per Telegram chat),
  streams turns over SSE, and relays the reply.
- **Surfaces approvals.** When TrueForge emits `tool.approval_required` (the gated
  `save_visit_brief`), the bridge posts an inline Allow/Deny keyboard and, on the user's
  choice, resumes with `user.tool_approval`.

**Secret-safety:** the bot token, API key, and message bodies are never logged. Errors are
returned as generic messages.

### 2. `trueforge` (agent execution loop)

The open-source, MIT-licensed [TrueForge](https://trueforge.dev) harness, deployed in
**hosted mode** (server + Postgres + Redis) via Docker Compose — its official quickstart
path. It:

- runs the model calls (the configured OpenAI-compatible backend),
- orchestrates the MCP tools,
- pauses for human approval on gated tools (`require_approval_for_tools`),
- compacts context and manages persistent sessions,
- validates the final response against the declared `response_format` schema.

The agent is defined in [`agent/drhiro.agent.json`](../agent/drhiro.agent.json).

### 3. `drhiro-tools` (services/drhiro-tools)

An MCP server (SSE transport on :3100) exposing exactly four tools over synthetic data:

| Tool | Read/Write | Notes |
|---|---|---|
| `get_demo_case` | read-only | Retrieves a bundled SYNTHETIC demo case. |
| `create_visit_brief` | read-only | Builds a structured, NON-diagnostic care-preparation brief. |
| `save_visit_brief` | **write/export** | Persists a brief to the exports volume. Gated: TrueForge pauses for approval. |
| `get_service_status` | read-only | Non-sensitive service status (version, uptime, export count). |

`save_visit_brief` refuses to persist a brief that is not explicitly marked `SYNTHETIC`,
and writes only inside the `EXPORT_DIR` volume.

### 4. Secure Bridge pairing (pairing service + HTTP API)

`services/telegram-bridge/src/drhiro_bridge/pairing.py` (`PairingManager`) is the server-side
pairing service. It mints **cryptographically random, short-lived (default 10 min),
single-use** pairing tokens, each **bound to the requesting Telegram user id and server URL**,
and keeps a device registry (only SHA-256 hashes of device secrets). A background
`pairing_http` server exposes the device-facing API:

- `POST /pair/exchange` — exchange a one-time token for a device-specific credential.
- `POST /pair/verify` — verify a stored device credential.
- `GET /pair/devices?user=<id>` — list a user's devices.
- `POST /pair/revoke` — revoke a device.

URL policy: **HTTPS required** for remote endpoints; **HTTP allowed only** for trusted-LAN
dev mode (localhost / private ranges / `.local`) with a visible warning. Token creation and
exchange attempts are rate-limited.

### 5. Installer & scripts

- `install.sh` — validates OS/privileges, installs Docker, prompts for five inputs, writes
  a protected `.env`, validates the Telegram token (without exposing it), detects webhook
  conflicts, validates the AI backend/model, builds and starts the stack, health-checks,
  provisions TrueForge, and prints safe commands.
- `scripts/configure.sh` — registers the model provider, the MCP server, and the `drhiro`
  agent with TrueForge (idempotent).
- `scripts/health-check.sh` — verifies containers, TrueForge health, tools reachability,
  Telegram token validity, polling-safety, and AI backend reachability.
- `scripts/update.sh` — refresh TrueForge source, rebuild, re-check.
- `scripts/backup.sh` — export a redacted env-key reference and a TrueForge DB dump.
- `scripts/uninstall.sh` — destructive teardown with explicit confirmation.
- `scripts/create-pairing-token.sh` / `list-paired-devices.sh` / `revoke-device.sh` /
  `regenerate-pairing-link.sh` — pairing-token and device management.
- `scripts/apk-verify.sh` / `apk-register.sh` / `apk-info.sh` — APK distribution.

## Data & state

- **Synthetic only.** The tools read from an in-code synthetic fixture store. No real
  health data, database, or user data ships.
- **Exports** go to a named Docker volume (`exports_data`) mounted at `EXPORT_DIR`.
- **Pairing state** (tokens + device registry) lives in the `pairing_state` volume.
- **TrueForge state** lives in the `tf_pgdata` Postgres volume and its Redis.
- **Secrets** live only in `.env` (mode 600), never in the repository.

## Network boundaries

- Only TrueForge's admin UI port (`TRUEFORGE_PORT`, default 8790) is published to the host.
- Postgres, Redis, the tools server, and the bridge are on an internal Docker network.
- The Telegram bridge talks outbound to `api.telegram.org` (long polling only).
- The TrueForge server talks outbound to the configured AI backend.

## Failure modes handled

- **Unreachable AI backend** → `TrueForgeError` surfaces as a clean "please try again"
  message; health check reports it.
- **Unavailable model** → the turn fails without hanging; a bounded timeout guards it.
- **Webhook conflict** → polling refuses to start until the operator explicitly removes it.
- **Unauthorized user** → rejected before any session/turn is created.

## See also

- [docs/INSTALL.md](INSTALL.md) — installation
- [docs/SECURITY_AND_PRIVACY.md](SECURITY_AND_PRIVACY.md) — security & threat model
- [docs/VALIDATION_REPORT.md](VALIDATION_REPORT.md) — automated tests
- [docs/DECISIONS.md](DECISIONS.md) — design decisions

---

## Full application topology (single merged stack)

The repo now ships the **complete drHiro application**, not just the submission
shell. One `docker-compose.yml` at the root brings up every service:

| Service | Role | Reads runtime settings via |
|---|---|---|
| `postgres` | Shared Postgres 16: drHiro DB + isolated TrueForge DB (created by `infra/postgres-init`) | — |
| `redis` | Shared Redis (drHiro `/0`, TrueForge `/1`) | — |
| `minio` | Object storage (food/meal photos, exports) | — |
| `api` | drHiro Core FastAPI (models, routers, services) | **live** — reads AI model/url/key from the settings store at call time |
| `worker`, `scheduler` | RQ worker + scheduler (reminders, alerts, aggregates) | **live** via api |
| `web` | React/Vite web + Telegram Mini App (incl. Settings screen) | reads/writes the settings store via the api |
| `reverse-proxy` | Caddy TLS edge | — |
| `trueforge` | Agent runtime (MIT, cloned by install.sh) | **env + re-provision** via `scripts/configure.sh` |
| `drhiro-tools` | MCP tools server (submission shell) | — |
| `telegram-bridge` | Telegram comms (long polling) | **restart-applies** (see below) |
| `openclaw-gateway` | OpenClaw Telegram comms layer | **restart-applies** (see below) |

### Configuration model — bootstrap vs runtime

- **`.env` is BOOTSTRAP ONLY.** `install.sh` writes it from the five installer
  inputs plus auto-generated service secrets (mode 600). It is not the live
  settings surface.
- **The runtime settings store** is a single `app_settings` row in Postgres
  (table `app_settings`, id `singleton`). First boot seeds it from `.env`;
  thereafter it is the source of truth for the fields editable from the web
  **Settings** screen:
  - AI backend URL, model name, AI API key
  - Telegram bot token, authorized Telegram username
- **Secret values are write-only** through the API: reads return a masked
  set/not-set indicator; the full secret is never returned to the frontend or
  logged. Audit records field names only.

### How a settings change is applied

Settings that a service reads **live** (the api LLM client) take effect at the
next call — no restart. Settings that a service **binds at start** follow the
restart-apply model:

1. The Settings screen `PUT`s the change to `/api/v1/settings` (authorized
   user only).
2. The api writes an **empty flag file** named `<service>.flag` into
   `RESTART_FLAGS_DIR` (a host path bind-mounted into the api container) for
   each affected service. The file carries **only the service name** — never a
   value.
3. A **host-side watcher** (root cron, `scripts/drhiro-settings-watcher.sh`,
   runs every minute under `flock`) reads pending flags, and for each:
   - reads the current settings **directly from Postgres** via
     `docker compose exec -T postgres psql` (the DB is never published to the
     host for this);
   - atomically regenerates `.env` (temp file → chmod 600 root → `mv`);
   - for `trueforge`: runs `scripts/configure.sh` (re-provisions the agent +
     model provider) then `docker compose up -d --force-recreate trueforge`;
   - for `telegram-bridge` / `openclaw-gateway`: regenerates `.env` then
     `docker compose up -d --force-recreate <service>`;
   - logs timestamp / service / success-or-failure / exit code to
     `/var/log/drhiro-settings-watcher.log` — **field names only, never values**;
   - on any failure, leaves a `failed.flag` that `health-check.sh` detects so a
     half-applied config is visible, not silent.
4. The Settings screen tells the user what will restart (~10s bridge, ~30s
   TrueForge re-provision).

**Why no container mounts the Docker socket:** applying a settings change
requires restarting sibling containers, which a container cannot do by itself,
and mounting `/var/run/docker.sock` into a container would hand it full host
control — a severe blast-radius expansion. The watcher therefore runs **on the
host as root** (the same trust level as `install.sh`), not inside any
container. No container ever mounts `docker.sock` and no app code shells out to
`docker`.

### Division of labour (authoritative)

- **OpenClaw** = Telegram communications layer (message transport, bot
  interaction).
- **TrueForge** = agent runtime responsible for logging and data processing
  (the model loop, tools, approvals, sessions).
- **drHiro app logic** (api / worker / rules / health schema / nutrition /
  Android Bridge / web) runs on the same stack. The submission shell was the
  packaging/deployment layer around this stack — not a competing runtime.
