# Identity model

The drHiro gateway is **single-tenant**: each instance serves exactly **one paired Telegram identity** set via `DRHIRO_TELEGRAM_ID`.

## Why the bridge forces the configured identity

The OpenClaw MCP bridge (`openclaw/mcp/drhiro-mcp-server.js`) forwards **only** the configured `DRHIRO_TELEGRAM_ID` to the API as the Telegram identity. A model-supplied `telegram_id` that diverges is silently discarded — a confused or manipulated model can never act as a different paired user. When `DRHIRO_TELEGRAM_ID` is unset, calls fail closed (no impersonation risk).

This is a deliberate, documented design decision. It is not a bug.

## Multi-user?

Supporting multiple users on a single gateway would require **per-session bound identities** — a separate identity-resolution path that binds each verified chat/session to its own stored identity. That is out of scope for the single-tenant deployment model.

To serve multiple users, deploy **separate gateway instances**, each with its own `DRHIRO_TELEGRAM_ID`.
