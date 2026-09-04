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

## Optional — food / nutrition lookup (USDA + Camoufox)

The nutrition pipeline that powers food logging uses two optional lookups behind the local
food database. Both are configured through **environment variables**; no code change is
required.

### Primary: USDA FoodData Central (recommended)

When the local food DB has no match, drHiro queries the **USDA FoodData Central** API
first. It is authoritative, free, and needs no browser.

1. Register at <https://fdc.nal.usda.gov/api-key-signup.html> — the key is emailed to you.
2. Export it as an environment variable on the host running the meal/food service:

   ```bash
   export USDA_API_KEY="your-32-char-key"     # NOT the public DEMO_KEY
   ```

3. Restart the food service so it picks up the variable.

The service falls back to the demo key if unset, but the **demo key is rate-limited
(~30 req/hr)** and frequently returns empty — set a real key for reliable lookups.

### Fallback: DuckDuckGo via a Camoufox browser behind a SOCKS5 proxy

For foods USDA still does not list, drHiro can scrape **DuckDuckGo** with a headless
**Camoufox** browser. Two important, tested facts:

- **Google is deliberately not used.** Both plain HTTP clients and headless browsers from
  a datacenter IP are blocked by Google regardless of the IP used — do not build a Google
  scraper.
- **DuckDuckGo HTML** works with a real browser, but the browser must egress through a
  **residential IP**. A datacenter/VPS IP is blocked by DuckDuckGo too. Camoufox also needs
  a SOCKS5 proxy with **remote DNS** so DNS resolves on the residential side.

Layout (everything on the VPS host; the browser never runs inside a container):

| Component | Purpose |
|---|---|
| `home-socks.service` | Persistent `ssh -D 127.0.0.1:1080` dynamic SOCKS5 tunnel to a residential host (the Pi) |
| `ddg-http.service` | Tiny HTTP wrapper that runs Camoufox through the tunnel and returns parsed nutrition JSON |
| food service | On DB/USDA miss, `POST /lookup` to the wrapper's HTTP endpoint |

Environment / settings used by the food service:

```bash
# Where the DDG wrapper listens (the docker bridge gateway, reachable from containers)
DDG_HTTP_URL="http://172.20.0.1:8098"
# The wrapper egresses through the tunnel to the residential IP
HOME_SOCKS_PROXY="socks5://127.0.0.1:1080"
```

The wrapper and tunnel are described only generically here because they are deployment
specific. If you operate drHiro from a VPS and want this fallback, the durable lesson from
production testing is: keep the browser **on the residential network** (or route it there
through the tunnel) and use **DuckDuckGo, not Google**. Do not install browsers or scraping
daemons on a shared/worker host that must stay lean.

### Setting order (priority)

1. Local food database
2. USDA FoodData Central (real `USDA_API_KEY`)
3. DuckDuckGo via Camoufox behind the SOCKS5 tunnel (`DDG_HTTP_URL` / `HOME_SOCKS_PROXY`)

### Privacy note

Food queries contain the foods a user eats. The USDA call sends only the food-name query
to the USDA API; the DDG fallback sends it to DuckDuckGo. Configure the proxy and data
sources according to your privacy requirements.

