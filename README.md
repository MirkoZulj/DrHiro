# drHiro — Privacy-first Health Information Agent on TrueForge

drHiro is a **privacy-first personal health information agent**. It helps one person
organize what they want to discuss with their own clinician before an upcoming visit —
preparing a care-preparation brief, not a diagnosis. It is built on
[TrueForge](https://trueforge.dev) (the open-source, MIT-licensed agent harness),
which runs the entire agent execution loop: model calls, tool orchestration, human
approvals, context management, and session state.

> **What drHiro does:** records what a user wants to talk about, retrieves a bundled
> synthetic demo case, and produces a structured, non-diagnostic care-preparation brief.
>
> **What drHiro does NOT do:** it does **not** diagnose, prescribe, provide emergency
> triage, act as a medical device, or replace a healthcare professional. All data in
> this package is **synthetic and explicitly labelled as such**.

## Highlights

- **TrueForge manages the agent loop.** Telegram is a pure transport; TrueForge owns the
  model, the tools, approvals, context, and session state.
- **Long polling by default.** No public domain, no HTTPS webhook, no DNS — a user needs
  only a bot token. The installer detects an existing webhook and requires explicit
  confirmation before removing it, and never runs polling and webhook together.
- **Five inputs only.** Telegram bot token, authorized Telegram username, OpenAI-compatible
  AI backend base URL, API key (or placeholder), and model name.
- **Approval-gated export.** Saving a visit brief requires the user's explicit Allow/Deny.
- **Structured output validation.** The agent's replies are validated against a JSON schema.
- **Synthetic data only.** Zero real health data ships in or out of this package.
- **APK distribution via your bot.** The signed drHiro Bridge Android app is served by your
  own Telegram bot (`/apk`), with checksum verification and persisted `file_id` — no public
  APK host or QR pairing. See [docs/APK_DISTRIBUTION.md](docs/APK_DISTRIBUTION.md).

## How TrueForge is used

```
Telegram user
  → telegram-bridge      long-polling transport (authorized-user gate)
  → TrueForge            agent execution loop (model, tools, approvals, context, sessions)
  → drhiro-tools         MCP server — the four tools (synthetic data)
```

- **TrueForge runs the agent loop.** It owns the model calls, the reasoning, the tool
  orchestration, human-approval pauses, context compaction, and persistent per-user sessions.
- **The agent calls real tools** exposed as an MCP server (`drhiro-tools`):
  - `get_demo_case` — read-only synthetic fixture retrieval
  - `create_visit_brief` — structured non-diagnostic care-preparation output
  - `save_visit_brief` — **approval-gated** persistent/export action
  - `get_service_status` — non-sensitive status for authorized users
- **Approvals.** `save_visit_brief` is declared in `require_approval_for_tools`, so TrueForge
  pauses with `tool.approval_required`; the bridge asks the user Allow/Deny in Telegram and
  resumes with `user.tool_approval`.
- **Structured output.** The agent spec declares `response_format: json_schema`.
- **Session context safely.** One persistent TrueForge session per conversation, scoped to
  the authorized user, operating only on synthetic data.

The agent spec lives in [`agent/drhiro.agent.json`](agent/drhiro.agent.json).

## Quick start

```bash
# 1. Download / clone this repository onto Ubuntu 22.04 or 24.04.
# 2. Run the installer as root (or with sudo):
sudo ./install.sh
# 3. Answer the five prompts.
# 4. The stack builds, starts, health-checks, and provisions TrueForge automatically.
```

See [docs/INSTALL.md](docs/INSTALL.md) for the full step-by-step guide.

## Configuration variables

All values are provided interactively by `install.sh` and written to a protected `.env`
(mode 600). They can also be set directly. See [`.env.example`](.env.example).

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram bot token (from @BotFather). Never exposed. |
| `TELEGRAM_ALLOWED_USERNAME` | Yes | Authorized Telegram username (no `@`). Only this user may talk to the bot. |
| `AI_BACKEND_BASE_URL` | Yes | OpenAI-compatible AI backend base URL (e.g. `https://api.openai.com/v1` or a local `http://host:8000/v1`). |
| `AI_API_KEY` | Yes | AI backend API key, or any placeholder (`local`) for a local model that ignores auth. |
| `AI_MODEL` | Yes | Model name advertised by the backend. |
| `TRUEFORGE_PORT` | No | TrueForge admin UI port (default `8790`). |
| `TRUEFORGE_AGENT` | No | TrueForge agent name (default `drhiro`). |
| `DRHIRO_DEBUG` | No | `true` enables debug logs (default `false`). |

## Safety & privacy boundaries

- **No real health data.** The tools operate only on bundled synthetic fixtures, every one
  labelled `SYNTHETIC`.
- **No secrets in logs.** The bot token, API key, and message bodies are never logged.
- **No public webhook.** Long polling only; webhook conflicts require explicit confirmation.
- **Single-user, authorized by username.** Only the configured username reaches the agent.
- **Not a medical device.** drHiro does not diagnose, prescribe, triage, or replace a
  clinician. See [docs/SECURITY_AND_PRIVACY.md](docs/SECURITY_AND_PRIVACY.md).

## Bot commands

The bot answers commands from the authorized user (plus agent conversation for anything
else):

| Command | What it does |
|---|---|
| `/start` | Welcome message |
| `/apk` | Send the current signed drHiro Bridge Android APK |
| `/apkinfo` | Show version, Android requirement, size, SHA-256 |
| `/status` | Non-sensitive server + APK status |
| `/help` | Explain commands and Android installation |

## Demo video

> 🎬 **Demo video placeholder.** A recorded walkthrough of installing the package on a clean
> Ubuntu 22.04 host, running a full conversation (including an approval), and downloading the
> drHiro Bridge APK via `/apk` will be linked here once recorded.

## Qodo Code Review Evidence

> **Placeholder.** A single substantive feature branch was prepared and opened for review
> through the Qodo (formerly CodiumAI) code-review workflow. Once a Qodo-reviewed PR is
> merged, the actual PR URL and a factual summary of the review findings and remediations
> will replace this placeholder. No backdated or fabricated review evidence is included.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system design and component responsibilities
- [docs/INSTALL.md](docs/INSTALL.md) — step-by-step installation on Ubuntu 22.04/24.04
- [docs/SECURITY_AND_PRIVACY.md](docs/SECURITY_AND_PRIVACY.md) — safety model, threat model
- [docs/VALIDATION_REPORT.md](docs/VALIDATION_REPORT.md) — automated test results
- [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) — demo recording script
- [docs/PUBLIC_RELEASE_AUDIT.md](docs/PUBLIC_RELEASE_AUDIT.md) — what was audited before release
- [docs/DECISIONS.md](docs/DECISIONS.md) — design decisions log
- [docs/APK_DISTRIBUTION.md](docs/APK_DISTRIBUTION.md) — how the Bridge APK is distributed
- [docs/INSTALL_ANDROID_BRIDGE.md](docs/INSTALL_ANDROID_BRIDGE.md) — installing the Android app

## License

[MIT](LICENSE). TrueForge itself is MIT-licensed (© TrueFoundry).
