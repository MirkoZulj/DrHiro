# VALIDATION RUNBOOK — drHiro single merged stack (live-server run)

This runbook is for execution on a **real server** with Docker, network access
(for the TrueForge clone), and a real Telegram bot token. It is the acceptance
gate for the merged single-stack build. Run it top-to-bottom on a clean host.

Offline, everything statically provable has already been validated (unit tests,
`tsc`, `docker compose config`, secret scan) — see the commit report in the PR.

---

## 0. Prerequisites (clean host)

```bash
# Ubuntu 22.04/24.04, root or sudo
sudo apt-get update
sudo apt-get install -y git curl ca-certificates
```

## 1. Clean clone

```bash
git clone https://github.com/MirkoZulj/DrHiro.git drhiro
cd drhiro
git checkout feature/full-app-logic   # the branch under review
git log --oneline -1                  # note the commit you are validating
```

## 2. Install (single entry point)

```bash
sudo ./install.sh
```

`install.sh` prompts for **exactly five inputs** (Telegram bot token,
authorized username, AI backend URL, AI API key, model name), writes a
protected `.env` (mode 600) with auto-generated service secrets, builds and
starts the full 12-service stack, runs `scripts/health-check.sh`, provisions
TrueForge via `scripts/configure.sh`, and installs the host-side settings
watcher (`/etc/cron.d/drhiro-settings-watcher`).

> First build clones TrueForge from source (pinned tag) and takes several
> minutes. The bot token must be real and match the authorized username.

## 3. Health check — full stack healthy

```bash
./scripts/health-check.sh
# Expect: ALL CHECKS PASSED (containers up, TrueForge healthy, tools reachable,
# Telegram token valid, no webhook, AI backend reachable, settings watcher
# installed, no failed.flag)
```

## 4. Per-service build checklist

Each of these must build cleanly (they are also built by `install.sh`'s
`docker compose up -d --build`, but confirm individually):

```bash
docker compose build api
docker compose build worker
docker compose build scheduler
docker compose build web
docker compose build drhiro-tools
docker compose build telegram-bridge
docker compose build trueforge          # builds from ./trueforge-src (cloned)
docker compose build openclaw-gateway   # pulls ghcr.io/openclaw image
# postgres/redis/minio/reverse-proxy pull public images (no local build)
```

Report build success per service. `docker compose config --quiet` should exit 0
(already validated offline).

## 5. Confirm no docker.sock mount in any service

```bash
docker compose config | grep -i 'docker.sock' && echo "FOUND (FAIL)" || echo "no docker.sock in any service (PASS)"
docker ps --format '{{.Names}}' | while read c; do
  docker inspect "$c" --format '{{.Name}} {{range .HostConfig.Binds}}{{.}} {{end}}' 2>/dev/null
done | grep -i docker.sock && echo "FOUND in running container (FAIL)" || echo "no running container mounts docker.sock (PASS)"
```

## 6. Settings round-trip — MODEL (live, NO restart)

The api LLM client reads the model live from the settings store.

1. Open the web app at the configured URL → Settings → **System settings**.
2. Change the **Model name** to a value the backend advertises. Save.
3. Confirm the save notice says **"Applied immediately"** (model change needs no
   restart for the api).
4. Confirm the api now uses the new model:
   ```bash
   # The api's LLM path resolves the model from the store. Trigger a call that
   # uses chat_complete (e.g. a meal-item correction) and check the api log
   # shows the new model id WITHOUT any secret, OR run a direct store check:
   docker compose exec -T postgres psql -U drhiro -d drhiro \
     -tAc "SELECT model_name FROM app_settings WHERE id='singleton';"
   # -> the new model name
   ```
5. Confirm it survives a restart:
   ```bash
   docker compose restart api
   docker compose exec -T postgres psql -U drhiro -d drhiro \
     -tAc "SELECT model_name FROM app_settings WHERE id='singleton';"
   # -> still the new model name (store is authoritative, not .env)
   ```

## 7. Settings round-trip — BOT TOKEN or USERNAME (flag → watcher → restart)

1. Open Settings → **System settings**.
2. Change the **authorized username** (or bot token). Save.
3. Confirm the notice says the **Telegram bridge is restarting ~10 seconds**.
4. Within ~60s, confirm the flag was written and the watcher acted:
   ```bash
   ls -la /var/lib/drhiro/restart-flags/          # should be empty after apply
   tail -20 /var/log/drhiro-settings-watcher.log   # shows telegram-bridge applied OK
   ```
5. Confirm the new value is in effect:
   ```bash
   docker compose exec -T postgres psql -U drhiro -d drhiro \
     -tAc "SELECT telegram_allowed_username FROM app_settings WHERE id='singleton';"
   # -> new username
   docker compose exec telegram-bridge sh -c 'echo $TELEGRAM_ALLOWED_USERNAME'
   # -> new username (bridge was recreated with regenerated env)
   ```
6. Confirm it survives a restart:
   ```bash
   docker compose restart telegram-bridge
   docker compose exec telegram-bridge sh -c 'echo $TELEGRAM_ALLOWED_USERNAME'
   ```

## 8. Settings round-trip — MODEL (trueforge flag → configure.sh → recreate)

1. Change the **Model name** again (or AI URL) in System settings. Save.
2. Confirm the notice says **TrueForge re-provisioning ~30 seconds**.
3. Watch the watcher:
   ```bash
   tail -30 /var/log/drhiro-settings-watcher.log
   # expect: apply trueforge: OK (configure.sh + recreate)
   ```
4. Confirm the new model is in effect in TrueForge:
   ```bash
   ./scripts/health-check.sh          # all healthy
   # Query the TrueForge agent spec (or the drhiro agent) to confirm the model:
   curl -s http://localhost:8790/api/v1/agents | python3 -m json.tool \
     | grep -i '"model"'               # new model id, no secrets printed
   ```
5. **Failure case — unreachable TrueForge is DETECTED, not silent:**
   stop TrueForge, change the model, save, then confirm the watcher leaves a
   `failed.flag` and health-check reports it:
   ```bash
   docker compose stop trueforge
   # change model in UI, save
   sleep 70   # let the watcher run
   ls /var/lib/drhiro/restart-flags/failed.flag && echo "failed.flag present (FAIL state visible — PASS)"
   ./scripts/health-check.sh    # must report the failed settings-apply marker
   docker compose start trueforge
   ```

## 9. No secrets in watcher logs (grep)

```bash
# The watcher log must contain NO secret values — only service names, field
# names (in the api audit), and exit codes.
grep -nE 'token|api.?key|Bearer|TELEGRAM_BOT_TOKEN=.|AI_API_KEY=.' /var/log/drhiro-settings-watcher.log \
  && echo "SECRET FOUND (FAIL)" || echo "no secret values in watcher log (PASS)"
# Also confirm the api never returned secrets: masked-read check via the UI
# (AI API key field shows '(set)' only, never the value).
```

## 10. Backup contains secrets — handle accordingly

```bash
./scripts/backup.sh
# The resulting dump is secret-bearing (it holds app_settings incl. bot token +
# AI key). Store it 600 / encrypted. Confirm it is NOT in the repo or world-readable.
```

## 11. Uninstall (removes watcher too)

```bash
./scripts/uninstall.sh    # type UNINSTALL; removes containers, volumes, .env,
                          # and the settings watcher (/etc/cron.d entry, flags, log)
ls /etc/cron.d/drhiro-settings-watcher 2>&1   # should be gone
```

---

## Honest list of what remains unproven until this live run

- Clean-clone `install.sh` end-to-end on a fresh host (TrueForge clone + full
  12-service build + health).
- The two settings round-trips (model live; bridge restart) against a real bot.
- The trueforge configure.sh + recreate path and its failure detection.
- Watcher cron actually firing on a real host (offline we only validated the
  script syntax + logic; the cron entry install is exercised at install).
- Secret-free watcher log over a real apply (unit tests + grep cover the static
  paths; a live apply is the real proof).
- Android APK distribution + pairing pipeline against the merged stack.
