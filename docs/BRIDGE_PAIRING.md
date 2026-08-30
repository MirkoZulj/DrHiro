# Secure Android Bridge Pairing

The drHiro Bridge Android app links to **your** self-hosted drHiro server using a
short-lived, single-use pairing token issued by your Telegram bot. No server
credentials, bot token, AI key, TrueForge key, root credential, or permanent user
token is ever embedded in the APK.

## The flow

1. You install your self-hosted drHiro server and configure your Telegram bot.
2. You send `/apk` to your bot.
3. The bot sends the generic signed drHiro Bridge APK **and** creates a single-use
   pairing token bound to your Telegram user id.
4. The bot posts an inline **"Connect drHiro Bridge"** button carrying a deep link:
   `drhiro://pair?server=...&token=...`.
5. After installing the APK, you tap that button.
6. Android opens drHiro Bridge through the custom deep link.
7. The Bridge displays the target server and asks you to confirm the connection.
8. The Bridge calls the server's `/pair/exchange` endpoint with the one-time token.
9. The server verifies the token (not expired, not reused, bound to your user id,
   matching server), invalidates it, and returns a **device-specific credential**.
10. The Bridge stores only that device credential in Android secure storage and is
    then linked to that specific drHiro server.

## Deep link

Scheme: `drhiro://pair`

Query parameters:
- `server` — the drHiro server base URL (required)
- `token` — the one-time pairing token (required)
- `version` — protocol version (optional, default `1`)
- `expiration` — token expiration timestamp, ISO-8601 (optional)

The Bridge must **not** trust the `server` or `token` from the link until it verifies
them against the server at `/pair/exchange`.

## Server URL policy

- **HTTPS is required** for non-local (remote) endpoints.
- **HTTP is allowed only** for explicit trusted-LAN development mode (localhost /
  private ranges / `.local`), and every such exchange is flagged `insecure` so the
  Bridge shows a **visible warning**.

## Server-side pairing service

`services/telegram-bridge/src/drhiro_bridge/pairing.py` (`PairingManager`):

- Tokens are **cryptographically random** (`secrets.token_urlsafe(32)`), **short-lived**
  (default 10 minutes), and **single-use**.
- Each token is **bound to the Telegram user id** that requested it, and to the server URL.
- Tokens are **invalidated after successful use**; a reused token is rejected.
- **Rate limiting** bounds token creation and pairing attempts per user.
- Only the resulting **device-specific credential** is issued; only its SHA-256 hash is
  stored. The plaintext secret is returned to the Bridge exactly once.
- State persists to `PAIRING_STATE_DIR` so restarts keep paired devices.

Device-facing HTTP API (internal network, port `PAIRING_HTTP_PORT`):
- `POST /pair/exchange` — exchange a one-time token for a device credential.
- `POST /pair/verify` — verify a stored device credential.
- `GET /pair/devices?user=<id>` — list a user's devices.
- `POST /pair/revoke` — revoke a device.

## Telegram commands

| Command | What it does |
|---|---|
| `/apk` | Sends the signed APK and creates a pairing link + Connect button |
| `/pair` | Generates a fresh pairing link without resending the APK |
| `/devices` | Lists the requesting authorized user's linked devices |
| `/revoke <device>` | Revokes a linked device after confirmation (`CONFIRM`) |

`/apk`, `/pair`, `/devices`, and `/revoke` are restricted to the authorized user.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/create-pairing-token.sh <user-id>` | Create a single-use pairing token + deep link |
| `scripts/list-paired-devices.sh <user-id>` | List a user's paired devices |
| `scripts/revoke-device.sh <device-id> <user-id>` | Revoke a device |
| `scripts/regenerate-pairing-link.sh <user-id>` | Fresh pairing link without resending the APK |

## Manual fallback

The Bridge also offers a **manual fallback**: "Enter server address and pairing code".
This lets a user link without the deep link button — the same one-time token from
`/pair` (or the scripts) is entered manually. The same HTTPS/LAN validation applies.

## Security properties

- No secrets embedded in the APK.
- Token bound to (user, server); wrong user/server rejected.
- Single-use + time-limited; reuse and expiry rejected.
- Rate-limited creation and attempts.
- Only a device-specific credential is stored (Android secure storage); only its hash
  is kept server-side.
- Device access can be revoked at any time.

See also [docs/INSTALL_ANDROID_BRIDGE.md](INSTALL_ANDROID_BRIDGE.md) and
[docs/SECURITY_AND_PRIVACY.md](SECURITY_AND_PRIVACY.md).
