# APK Distribution — drHiro Bridge via your Telegram bot

This document explains how the signed drHiro Bridge Android APK is built, stored,
verified, and distributed by your own Telegram bot after the drHiro server is installed.
There is **no QR-code pairing and no separate public APK-hosting platform** in this flow.

## Architecture

```
drHiro server (Ubuntu)
  └─ ./apk/  (protected application directory)
       ├─ drhiro-bridge.apk   signed release APK
       └─ apk.json            version · sha256 · file_id (added after first upload)
             │
Telegram bot (long polling)
  ├─ /apk      → sends the signed APK as a Telegram document
  ├─ /apkinfo  → version, Android requirement, size, SHA-256
  ├─ /status   → non-sensitive server + APK status
  └─ /help     → commands + Android install help
```

## The signed APK artifact

- The APK is a **signed release build** of the drHiro Bridge Android app.
- It is kept **under 45 MB** where possible. The release workflow **fails before
  Telegram upload** if the APK cannot safely fit within the Telegram Bot API upload limit
  (hard cap 50 MB).
- A **SHA-256 checksum is generated and published** alongside the APK (in `apk.json` and
  shown by `/apkinfo` and the send caption).
- **Nothing sensitive is compiled into the APK**: no Telegram bot token, AI credential,
  TrueForge credential, personal server URL, user data, health data, signing key, signing
  password, or private configuration. The app's connection target is configured at runtime
  on the server, never hardcoded to a third-party host.

## Where the APK lives

The APK artifact is stored on the **installed drHiro server** in `./apk/` (a protected
application directory, gitignored). It is obtained either by **retrieving it during
installation from a verified, versioned project release**, or by the **operator placing it
manually**. `apk.json` (mode 600) carries the version, checksum, and — after first upload —
the Telegram `file_id`.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/apk-verify.sh` | Verify the APK checksum, version, and size; refuse tampered or oversized artifacts. |
| `scripts/apk-register.sh` | Upload/register the APK with Telegram on first setup, persist the returned `file_id` (mode 600), resend by `file_id` on later runs. |
| `scripts/apk-info.sh` | Report version, size, SHA-256, and registration status — without revealing secrets. |

All scripts are secret-safe: the bot token and the stored `file_id` value are never printed.

## Delivery flow

1. **First setup.** The operator places the signed APK + `apk.json` in `./apk/`. They run
   `scripts/apk-register.sh` (or answer "y" to the installer's approval-gated prompt).
   The script verifies checksum/size, uploads the APK to Telegram as a document, reads the
   returned `file_id`, and persists it in `apk.json` (mode 600). A **failed upload never
   persists a file_id**.
2. **Later `/apk` requests.** The bot sends the APK by the **stored `file_id`** — no
   re-upload — with the current version/checksum caption. If the stored `file_id` has gone
   stale on Telegram, the bot re-uploads and refreshes it.
3. **Status.** `/status` and `scripts/apk-info.sh` report delivery readiness without
   revealing the token or file_id.

## Security

- **Authorization:** `/apk` and `/apkinfo` are restricted to `ALLOWED_TELEGRAM_USERNAME`
  and, after first successful verification, the resolved `ALLOWED_TELEGRAM_USER_ID`.
- **Integrity:** every serve path validates the APK's SHA-256 against the recorded checksum
  and enforces the size limit. A tampered or oversized artifact is refused before any upload.
- **Secrets:** the APK never contains secrets; the bot token and file_id are never logged or
  shown.
- **Human gate:** uploading the APK to Telegram, publishing a release, or sending the APK to
  any real user requires **explicit human approval**.

## Testing

The full delivery pipeline is covered by offline tests using a **mocked Telegram API**
(`tests/test_apk_distribution.py`):

1. authorized `/apk` request sends the expected `file_id`
2. unauthorized `/apk` request is denied
3. missing APK file fails safely
4. checksum mismatch blocks upload
5. APK over the configured size limit blocks upload
6. failed first upload does not save a false `file_id`
7. later `/apk` calls reuse the stored `file_id`

See also [docs/INSTALL_ANDROID_BRIDGE.md](INSTALL_ANDROID_BRIDGE.md) for the end-user flow.

