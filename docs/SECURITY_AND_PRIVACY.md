# Security & Privacy — drHiro on TrueForge

This document describes the security model, privacy boundaries, and threat model for the
drHiro on TrueForge package.

## Privacy position

- **Synthetic data only.** Every fixture, demo case, and visit brief in this package is
  explicitly labelled `SYNTHETIC`. The tools never read real health data, a real database,
  or any user data beyond the structured tool arguments.
- **Nothing leaves the host except Telegram traffic and model calls.** The only outbound
  connections are long-polling to `api.telegram.org` and the TrueForge server's calls to the
  configured AI backend.
- **No webhook, no public surface.** Long polling means there is no HTTPS ingress, no public
  URL, and no cert/DNS requirement. There is no externally reachable HTTP port except the
  optional TrueForge admin UI.

## Secret handling

| Secret | How it is protected |
|---|---|
| Telegram bot token | Stored only in `.env` (mode 600); validated via `getMe` without being printed; never logged or committed. |
| AI API key | Stored in `.env` (mode 600); not echoed at input; sent only to the AI backend. |
| `.env` | Created by `install.sh` with `chmod 600`; gitignored; not tracked. |

**Logging rule:** no token, API key, password, or message body is ever logged. The bridge
logs usernames and chat IDs only for operational clarity, and errors are surfaced as generic
messages. This is enforced by tests (`tests/test_secret_safe_logs.py`).

## Access control

- **Single authorized user.** Only the configured `TELEGRAM_ALLOWED_USERNAME` can reach the
  agent. Everyone else receives "you are not authorized". This is enforced *before* any
  TrueForge session or turn is created.
- **Approval gate on writes.** The only write/export action, `save_visit_brief`, is gated by
  TrueForge (`require_approval_for_tools`). TrueForge pauses with `tool.approval_required`;
  the bridge presents **Allow / Deny** to the user; the action only runs after explicit
  approval. Denial never persists.
- **Tool-level guard.** `save_visit_brief` independently refuses to persist any brief not
  marked `SYNTHETIC`.

## Secure Bridge pairing

The Android Bridge links to the server via **short-lived, single-use pairing tokens** bound
to the authorized Telegram user id:

- Tokens are cryptographically random, valid by default **10 minutes**, and invalidated
  after a single successful exchange (reuse/expiry rejected).
- The token is bound to **(user, server)** — a wrong user or wrong server is rejected.
- Only a **device-specific credential** is issued; the server stores only its SHA-256 hash.
- **HTTPS is required** for remote server endpoints; **HTTP is allowed only** for
  trusted-LAN dev mode (localhost / private ranges / `.local`) and is flagged `insecure` so
  the Bridge shows a visible warning.
- Token creation and pairing attempts are **rate-limited**.
- Device access can be **revoked** at any time (`/devices`, `/revoke`, or the scripts).
- **No secrets in the APK**: no bot token, AI key, TrueForge key, root credential, server
  URL, or permanent user token is ever compiled into the Bridge.

See [docs/BRIDGE_PAIRING.md](BRIDGE_PAIRING.md) for the full pairing design.

## Webhook / polling exclusivity

- The bridge uses long polling only.
- Before polling starts, it checks `getWebhookInfo`. If a webhook is configured, it raises a
  conflict and requires the operator to type `CONFIRM` before calling `deleteWebhook`.
- Long polling and a webhook are **never** active at the same time. `health-check.sh`
  verifies no webhook is set on the token.

## Network boundaries

```
Telegram (api.telegram.org, outbound polling)        ┐
                                                     │ only outbound
AI backend (configured base URL, outbound model)     ┘
Host published port: TRUEFORGE_PORT (admin UI, optional)
All other services: internal Docker network only
```

## Threat model (summary)

| Threat | Mitigation |
|---|---|
| Malicious Telegram user | Username allowlist; rejected before any agent work. |
| Token/key leak via logs or repo | Secret-safe logging; `.env` gitignored and mode 600; token validated without printing. |
| Webhook + polling conflict / hijack | Exclusivity enforcement; explicit-confirmation deletion. |
| Unauthorized data write/export | Approval gate at TrueForge + tool-level synthetic guard. |
| Pairing-token theft / reuse | Single-use, time-limited tokens bound to (user, server); reuse/expiry rejected; rate-limited. |
| Rogue device linking | HTTPS required for remote; device credential issued once, only its hash stored; revocable. |
| Exposure of real health data | None exists — synthetic only, explicitly labelled. |
| Model abuse / prompt injection into a real backend | Agent is single-user, synthetic-only, non-diagnostic; the backend is the operator's own. |
| DoS / long turns | Bounded timeouts on turns; best-effort typing keepalive; failed turns return a generic message. |

## Health safety

drHiro is **not** a medical device. It does not diagnose, prescribe, provide emergency
triage, or replace a healthcare professional. The agent prompt, the tool disclaimers, and the
docs all state this. If any component suggests otherwise, treat it as a bug and report it.

## Operational notes

- Keep `.env` private and mode 600.
- Run `scripts/health-check.sh` after any change.
- Back up with `scripts/backup.sh`.
- If you expose the TrueForge admin UI beyond localhost, protect it with an authentication
  proxy — TrueForge hosted mode has no login by default.
