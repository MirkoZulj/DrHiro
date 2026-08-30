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

### 4. Installer & scripts

`install.sh` + `scripts/` provide a safe, repeatable install and operations surface:

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

## Data & state

- **Synthetic only.** The tools read from an in-code synthetic fixture store. No real
  health data, database, or user data ships.
- **Exports** go to a named Docker volume (`exports_data`) mounted at `EXPORT_DIR`.
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
