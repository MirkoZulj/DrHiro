# Installation — drHiro on TrueForge

This guide covers a clean install on **Ubuntu 22.04 or 24.04** with Docker Compose.

## Prerequisites

- An Ubuntu 22.04/24.04 host (x86_64 recommended) with internet access.
- A **Telegram bot token** from [@BotFather](https://t.me/BotFather).
- The **authorized Telegram username** that may talk to the bot.
- An **OpenAI-compatible AI backend** reachable from the host: its base URL, an API key (or
  a local placeholder key), and a model name.
- `sudo` access. The installer will install Docker if it is not present.

> No public domain, DNS, or HTTPS certificate is required. The bot uses **long polling**.

## Step 1 — Get the repository

```bash
git clone <your-drhiro-trueforge-repo-url> drhiro-trueforge
cd drhiro-trueforge
```

## Step 2 — Run the installer

```bash
sudo ./install.sh
```

The installer will:

1. **Validate the OS** (Ubuntu 22.04/24.04) and privileges.
2. **Install/check Docker Engine + Compose** if missing (official convenience script).
3. **Create a protected `.env`** (mode 600) and prompt for the **five** inputs:
   - Telegram bot token (not echoed)
   - Authorized Telegram username
   - OpenAI-compatible AI backend base URL
   - AI backend API key (or `local` placeholder, not echoed)
   - Model name
4. **Validate the Telegram token** via `getMe` *without printing it*.
5. **Detect webhook conflicts.** If a webhook is set on this token, it asks you to type
   `CONFIRM` before deleting it — long polling and a webhook are never run together.
6. **Validate the AI backend** (`GET /models`) and model availability (best-effort).
7. **Clone TrueForge** (MIT, pinned tag) and **build + start** the full stack.
8. **Health-check** every service.
9. **Provision TrueForge** (model provider, tools MCP server, `drhiro` agent) and print safe
   operational commands.

First build can take several minutes (TrueForge is built from source once).

## Step 3 — Confirm it is running

```bash
./scripts/health-check.sh          # ALL CHECKS PASSED
docker compose ps                  # all services Up
```

Open the TrueForge admin UI at `http://<host>:8790` (or `TRUEFORGE_PORT`) to see the
`drhiro` agent.

## Step 4 — Use the bot

Message the bot from the **authorized username**. Example flow:

1. Ask for a demo case: *"get me a demo case"*
2. Ask for a care-preparation brief: *"create a visit brief about sleep and activity"*
3. Ask to save it: *"save the brief"* — the bot will present **Allow / Deny**; choose Allow
   to export it, or Deny to discard.

## Configuration reference

See [.env.example](../.env.example) and the README's configuration table. All five required
values are collected by the installer.

## Upgrade

```bash
./scripts/update.sh
```

## Backup / restore

```bash
./scripts/backup.sh                # writes ./backups/drhiro-trueforge-<stamp>.tar.gz
```

## Uninstall

```bash
./scripts/uninstall.sh             # destructive; requires typing UNINSTALL
```

## Troubleshooting

| Symptom | Check |
|---|---|
| `Telegram token rejected` | Token invalid; recreate via @BotFather. Value is never printed. |
| `Webhook conflict` | A webhook is set on the token; run install again and confirm removal. |
| `AI backend unreachable` | Verify `AI_BACKEND_BASE_URL` and that the host can reach it. |
| `Model not found` | Verify `AI_MODEL` matches an id in `GET {base}/models`. |
| `drhiro-tools not reachable` | `docker compose logs drhiro-tools`; confirm it is on the internal network. |
| Health check fails after install | `docker compose logs -f --tail=200 telegram-bridge trueforge` |

## Demo without a real bot

The test suite runs entirely offline against **mock Telegram + mock TrueForge** servers, so
installation behaviour can be validated with no real bot, no real AI backend, and no
TrueForge instance. See [docs/VALIDATION_REPORT.md](VALIDATION_REPORT.md) and the `tests/`
directory.
