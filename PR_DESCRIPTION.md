# PR Description — drHiro: Privacy-first Health Information Agent on TrueForge

**Branch:** `feature/drhiro-trueforge-release`
**Base:** `main` (initial empty commit)

## Summary

Adds the complete drHiro on TrueForge package: a clean, public-safe, installable Ubuntu
(Docker Compose) deployment of a privacy-first health information agent. TrueForge (MIT,
open source) runs the agent execution loop; drHiro contributes a narrow MCP tool surface
over synthetic data and a long-polling Telegram bridge.

## What it does / does not do

- **Does:** help a person prepare for a visit by producing a structured, non-diagnostic
  care-preparation brief from synthetic demo cases.
- **Does NOT:** diagnose, prescribe, provide emergency triage, act as a medical device, or
  replace a healthcare professional. All data is synthetic and explicitly labelled.

## Key elements

- **TrueForge manages the agent loop.** Agent spec in `agent/drhiro.agent.json`: model,
  instructions, MCP tools, approval gate, structured-output schema.
- **Four tools** (`services/drhiro-tools`, MCP SSE server):
  `get_demo_case`, `create_visit_brief`, `save_visit_brief` (approval-gated),
  `get_service_status`.
- **Long-polling Telegram bridge** (`services/telegram-bridge`, stdlib-only): authorized-user
  gate (username + resolved user id), webhook/polling exclusivity, approval Allow/Deny,
  secret-safe logs.
- **APK distribution.** `/apk`, `/apkinfo`, `/status`, `/help` serve the signed drHiro
  Bridge Android APK from a protected `./apk/` directory: checksum/size validation, one-time
  upload with persisted `file_id`, resend by `file_id`, and `/apk`+`/apkinfo` restricted to
  the authorized user. Scripts `apk-verify.sh`, `apk-register.sh`, `apk-info.sh`.
- **Secure Bridge pairing.** `/apk`/`/pair` mint a single-use, time-limited, cryptographically
  random pairing token bound to the authorized user id + server URL, delivered as a
  `drhiro://pair` deep link with a "Connect drHiro Bridge" button (plus a manual code-entry
  fallback). A device-facing HTTP API (`/pair/exchange`, `/pair/verify`, `/pair/devices`,
  `/pair/revoke`) issues a device-specific credential; only its SHA-256 hash is stored.
  HTTPS required for remote; HTTP allowed only for trusted-LAN dev with a visible warning.
  `/pair`, `/devices`, `/revoke` added. Scripts `create-pairing-token.sh`,
  `list-paired-devices.sh`, `revoke-device.sh`, `regenerate-pairing-link.sh`.
- **Installer** `install.sh` + `scripts/` (configure, health-check, update, backup,
  uninstall). Prompts for exactly five inputs; validates token without exposing it; detects
  webhook conflicts; validates AI backend/model.
- **Docker Compose** deployment of TrueForge hosted mode (server + Postgres + Redis) plus
  the two drHiro services.
- **Offline tests:** 52 passing tests against mock Telegram + mock TrueForge (no real bot
  required), including 11 APK-distribution scenarios and 10 secure-pairing scenarios.

## Safety & compliance

- No real health data; synthetic fixtures only, explicitly labelled.
- No secrets committed or logged; `.env` is gitignored and mode 600.
- No private infrastructure references (redacted audit in `docs/PUBLIC_RELEASE_AUDIT.md`).
- No medical claims anywhere.
- No backdated or fabricated review evidence.

## Tests

`./run_tests.sh` → **52 passed**.

## Documentation

README, CONTRIBUTING, SECURITY, and `docs/` (ARCHITECTURE, INSTALL, SECURITY_AND_PRIVACY,
VALIDATION_REPORT, DEMO_SCRIPT, PUBLIC_RELEASE_AUDIT, DECISIONS).

## Reviewer notes

This is the initial substantive feature branch. Requesting review via the Qodo workflow
before merge. The README "Qodo Code Review Evidence" section is a placeholder until a
Qodo-reviewed PR is merged.
