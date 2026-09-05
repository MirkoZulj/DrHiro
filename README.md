# drHiro

**Self-hosted, privacy-first health assistant over Telegram with human approvals.**

drHiro is a single-user, self-hosted personal health information assistant. It runs on
[TrueForge](https://trueforge.dev) (the open-source, MIT-licensed agent harness), which owns
the entire agent execution loop — model calls, tool orchestration, human approvals, context
management, and session state. Telegram is a pure transport; the agent decides, and you
approve.

> **What drHiro does:** records what you want to discuss with your own clinician, retrieves a
> bundled synthetic demo case, and produces a structured, non-diagnostic care-preparation
> brief.
>
> **What drHiro does NOT do:** it does **not** diagnose, prescribe, provide emergency triage,
> act as a medical device, or replace a healthcare professional. All data in this package is
> **synthetic and explicitly labelled as such**.

## Features

- **Privacy-first, self-hosted.** Runs on your own Ubuntu host. No cloud dependency, no
  third-party health storage, no public data plane.
- **Human approvals.** Exporting a visit brief requires your explicit Allow/Deny in Telegram.
  The agent asks before any approved action.
- **Single-user, authorized by username.** Only the configured Telegram username reaches the
  agent.
- **Long polling by default.** No public domain, no HTTPS webhook, no DNS — you need only a
  bot token. The installer detects an existing webhook and requires explicit confirmation
  before removing it, and never runs polling and webhook together.
- **Structured output validation.** The agent's replies are validated against a JSON schema.
- **Synthetic data only.** Zero real health data ships in or out of this package.
- **APK distribution via your bot.** The signed drHiro Bridge Android app is served by your
  own Telegram bot (`/apk`), with checksum verification and persisted `file_id` — no public
  APK host. See [docs/APK_DISTRIBUTION.md](docs/APK_DISTRIBUTION.md).
- **Secure Bridge pairing.** A short-lived, single-use pairing token bound to your Telegram
  user links the Android Bridge to your server via a `drhiro://pair` deep link. No server
  credentials or permanent tokens are embedded in the APK. See
  [docs/BRIDGE_PAIRING.md](docs/BRIDGE_PAIRING.md).

## Architecture

```
Telegram
   │  long-polling transport (authorized-user gate)
   ▼
Pairing / approval bridge      short-lived single-use pairing tokens,
   │                           Allow/Deny approval prompts, command handling
   ▼
TrueForge runtime              agent execution loop: model calls, tool
   │                           orchestration, approvals, context, sessions
   ├─ tf-shim                  OpenAI-compatible adapter for OpenClaw
   ▼
MCP tools                      the tools the agent calls
   ▼
Model backend                  any OpenAI-compatible endpoint (cloud or local)
```

- **TrueForge runs the agent loop.** It owns the model calls, reasoning, tool
  orchestration, human-approval pauses, context compaction, and persistent per-user
  sessions. The bridge talks to it over its `/api/v1/*` API (sessions, turns, SSE
  events, tool approvals).
- **Two real model paths into TrueForge:**
  - **telegram-bridge → TrueForge `/api/v1/sessions`** — agent turns streamed over SSE,
    with `tool.approval_required` pauses surfaced as Allow/Deny prompts.
  - **OpenClaw → tf-shim → TrueForge** — an OpenAI-compatible adapter
    (`/v1/chat/completions`) that maps each request to a persistent TrueForge session.
- **The pairing/approval bridge** is the thin layer between Telegram and TrueForge: it
  enforces the authorized-user gate, mints one-use pairing tokens, and surfaces Allow/Deny
  prompts.
- **The agent calls MCP tools** over the MCP surface (SSE).
- **The model backend** is any OpenAI-compatible endpoint — cloud or a local server.

The agent spec lives in [`agent/drhiro.agent.json`](agent/drhiro.agent.json).

> This package now ships the **complete drHiro application** (health tracking, meals,
> weight, blood pressure, reminders, Android Bridge) on the same stack, with synthetic
> fixtures only. The submission-shell synthetic tools remain for evaluation.


## Quick start

```bash
# 1. Clone this repository onto Ubuntu 22.04 or 24.04.
# 2. Run the installer as root (or with sudo):
sudo ./install.sh
# 3. Answer the five prompts (bot token, username, model backend URL, key, model).
# 4. The stack builds, starts, health-checks, and provisions TrueForge automatically.
```

See [docs/INSTALL.md](docs/INSTALL.md) for the full step-by-step guide.

### Pairing flow

To link the Android Bridge to your server, the server must be reachable from the device over a
public address. Set `DRHIRO_PUBLIC_URL` (e.g. `https://bridge.example.com`) either when the
installer prompts, or later in `.env`. With it set, the bot mints a short-lived, single-use
pairing link via `/pair` (or `/apk`). The Android Bridge opens the `drhiro://pair` deep link,
exchanges a one-time token bound to your Telegram user, and is linked — no server credentials
ever reach the device.

If `DRHIRO_PUBLIC_URL` is unset, pairing commands refuse to mint an unreachable link and you
get a clear warning.

## Configuration variables

All values are provided interactively by `install.sh` and written to a protected `.env`
(mode 600). They can also be set directly. See [`.env.example`](.env.example).

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram bot token (from @BotFather). Never exposed. |
| `TELEGRAM_ALLOWED_USERNAME` | Yes | Authorized Telegram username (no `@`). Only this user may talk to the bot. |
| `AI_BACKEND_BASE_URL` | Yes | OpenAI-compatible AI backend base URL (cloud or local). |
| `AI_API_KEY` | Yes | AI backend API key, or any placeholder for a local model that ignores auth. |
| `AI_MODEL` | Yes | Model name advertised by the backend. |
| `DRHIRO_PUBLIC_URL` | No | Public URL the Android device reaches the server at for pairing. Enables `/pair` and `/apk`. |
| `TRUEFORGE_PORT` | No | TrueForge HTTP port (default `8790`). |
| `TF_POSTGRES_PORT` | No | TrueForge Postgres host port (default `5433`). |
| `TF_REDIS_PORT` | No | TrueForge Redis host port (default `6380`). |
| `EXPORT_DIR` | No | Where saved visit briefs are exported (default `/data/exports`). |
| `APK_DIR` | No | Host directory holding the signed Bridge APK + sidecar (default `./apk`). |
| `DRHIRO_DEBUG` | No | `true` enables debug logs (default `false`). |

## Roadmap

- **Production MCP servers as pinned artifacts.** This package ships synthetic example tools so
  the full stack can be evaluated end-to-end with no private data. The roadmap replaces those
  with **real MCP servers published as pinned, installable artifacts** — versioned, checksum
  pinned, and fetched by the installer by artifact reference rather than by mutable clone — so
  a production deployment wires its own tool servers the same way it wires its own model
  backend. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the current design.
- **Demo walkthrough.** The full flow is: run `sudo ./install.sh`, answer the five
  prompts, then message the bot — it answers the authorized user's questions, and any
  gated action (e.g. saving/exporting) pauses with an Allow/Deny prompt in Telegram. On the
  Android side, `/apk` sends the signed Bridge APK and a `drhiro://pair` deep link; opening
  it pairs the device with a short-lived, single-use token.
- **Qodo code-review evidence** on the merged release branch (see
  [docs/PUBLIC_RELEASE_AUDIT.md](docs/PUBLIC_RELEASE_AUDIT.md)).

## Scope of this package
This repository ships with synthetic example tools so the full stack — install, Telegram pairing, human approvals, agent runtime — can be evaluated end-to-end with no private data or credentials. A production deployment connects its own MCP servers via the tool configuration; no real health data, tokens, or secrets are required or included.

## Safety & privacy boundaries

- **No real health data.** The tools operate only on bundled synthetic fixtures, every one
  labelled `SYNTHETIC`.
- **No secrets in logs.** The bot token, API key, and message bodies are never logged.
- **No public webhook.** Long polling only; webhook conflicts require explicit confirmation.
- **Single-user, authorized by username.** Only the configured username reaches the agent.
- **Not a medical device.** drHiro does not diagnose, prescribe, triage, or replace a
  clinician. See [docs/SECURITY_AND_PRIVACY.md](docs/SECURITY_AND_PRIVACY.md).

## Bot commands

The bot answers commands from the authorized user (plus agent conversation for anything else):

| Command | What it does |
|---|---|
| `/start` | Welcome message |
| `/apk` | Send the current signed drHiro Bridge Android APK + a pairing link |
| `/apkinfo` | Show version, Android requirement, size, SHA-256 |
| `/pair` | Generate a fresh pairing link without resending the APK |
| `/devices` | List the authorized user's linked devices |
| `/revoke <device>` | Revoke a linked device (after confirmation) |
| `/status` | Non-sensitive server + APK status |
| `/help` | Explain commands and Android installation |

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system design and component responsibilities
- [docs/INSTALL.md](docs/INSTALL.md) — step-by-step installation on Ubuntu 22.04/24.04
- [docs/SECURITY_AND_PRIVACY.md](docs/SECURITY_AND_PRIVACY.md) — safety model, threat model
- [docs/VALIDATION_REPORT.md](docs/VALIDATION_REPORT.md) — automated test results
- [docs/PUBLIC_RELEASE_AUDIT.md](docs/PUBLIC_RELEASE_AUDIT.md) — what was audited before release
- [docs/DECISIONS.md](docs/DECISIONS.md) — design decisions log
- [docs/APK_DISTRIBUTION.md](docs/APK_DISTRIBUTION.md) — how the Bridge APK is distributed
- [docs/BRIDGE_PAIRING.md](docs/BRIDGE_PAIRING.md) — secure Android Bridge linking
- [docs/INSTALL_ANDROID_BRIDGE.md](docs/INSTALL_ANDROID_BRIDGE.md) — installing the Android app

## License

[MIT](LICENSE). TrueForge itself is MIT-licensed (© TrueFoundry).
