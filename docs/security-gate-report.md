# Security Gate Report — drHiro on TrueForge (pre-push)

**Date:** 2026-08-30
**Status:** PASS — all gates verified before any push/PR/public deployment.

## Gate results

### 1. `PAIRING_HTTP_PORT` is not publicly exposed as unauthenticated plain HTTP
**PASS.**
- `docker-compose.yml` defines no `ports:` mapping for the `telegram-bridge` service; the
  pairing HTTP server binds on the internal Docker network only (`drhiro-net`).
- Verified with `docker compose config`: no host publish of `8091`. The only published port
  is TrueForge's admin UI (`TRUEFORGE_PORT`, default 8790).
- The bridge container does not publish `8091` to the host or to the internet.

### 2. Remote Bridge access is served through HTTPS
**PASS (by construction).**
- `validate_server_url()` requires `https` for any non-LAN hostname and rejects plain HTTP
  to a remote endpoint.
- `DRHIRO_PUBLIC_URL` is the operator-supplied public base URL the Bridge uses; for remote
  deployments it must be `https://…`. The pairing HTTP server itself stays on the internal
  network; a public-facing HTTPS reverse proxy fronts it in a remote deployment.
- HTTP is permitted only for trusted-LAN dev mode (localhost / private ranges / `.local`) and
  is flagged `insecure` so the Bridge shows a visible warning.

### 3. Server URL received through deep links is canonicalized and compared with `DRHIRO_PUBLIC_URL`, not blindly trusted
**PASS.**
- `PairingManager._normalize()` lowercases the host, strips the default port
  (`https://host` == `https://host:443`), and strips a trailing slash.
- `exchange()` compares `_normalize(rec["server_url"]) != _normalize(server_url)` — the
  deep-link server is compared against the **token-bound server** (which is set from
  `DRHIRO_PUBLIC_URL` at token creation). A different host is rejected (`WrongServerError`).
- Covered by `test_exchange_rejects_server_not_matching_token_bound` and
  `test_server_url_canonicalized_not_blindly_trusted`.

### 4. `/pair/devices` and `/pair/revoke` cannot be called by unauthenticated internet users
**PASS.**
- Both endpoints now require an `X-Service-Token` header checked with
  `secrets.compare_digest` (`_require_service_auth`); without it they return HTTP 401.
- The service token is supplied via `PAIRING_SERVICE_TOKEN` (env), never committed.
- Covered by `test_management_endpoints_require_service_token`.

### 5. Only `/pair/exchange` is available to an unpaired Bridge, using a short-lived, single-use token
**PASS.**
- `POST /pair/exchange` is the only endpoint open without service auth. It consumes a
  single-use, time-limited (10-minute default) token bound to (user, server), then returns a
  device-specific credential.
- `POST /pair/verify` requires the device credential; `/pair/devices` and `/pair/revoke`
  require the service token.
- Token reuse/expiry rejected (`TokenReusedError` / `TokenExpiredError`); covered by tests.

### 6. All pairing state and credentials remain excluded from Git
**PASS.**
- `.gitignore` excludes `.env`, `apk/` (APK binary + `apk.json` with `file_id`), and
  `data/pairing/` (runtime pairing state with token/device hashes).
- `git ls-files` shows no `.env`, no `apk/` artifact, and no `pairing.json` tracked.
- Only `.env.example` (placeholders) is tracked.

### 7. No secret appears in README, test fixtures, logs, screenshots, or Git history
**PASS.**
- Full regex scan of every git-tracked file for bot tokens, private IPs/hostnames, user IDs,
  API keys, JWTs, and SSH keys: **0 hits**.
- Git history secret scan (`git log --all -p`): **0 hits**.
- Test fixtures use only mock values (`FILEID-0001`, `TESTTOKEN`, `123456:TESTTOKEN`,
  `svc-secret`) — clearly test-only, no real secrets.
- The only `192.168.` references are the intentional trusted-LAN detection code and its test.

## Verification evidence
- `docker compose config` validates; the `telegram-bridge` service has no host port publish.
- 55 automated tests pass, including the three new security-gate tests.
- Git-tracked secret scan and git-history scan both clean.

## Recommendation
The package is safe to push to a **private** repository and open the substantive PR. It is
**not** ready to be made public, merged, or deployed until additional gates (real APK
build/validation, Qodo review) pass and the operator explicitly authorizes publication.
