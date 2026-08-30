# Installing the drHiro Bridge Android app

The **drHiro Bridge** is the official Android companion app. It is distributed by
**your own Telegram bot** after the drHiro server is installed — there is no public
APK host and no QR-code pairing requirement.

## Prerequisites

- Your drHiro server is installed and running (`./scripts/health-check.sh` → ALL CHECKS PASSED).
- The **signed** `drhiro-bridge.apk` has been placed in the `./apk` directory on the server,
  with an `apk.json` sidecar (see [docs/APK_DISTRIBUTION.md](APK_DISTRIBUTION.md)).
- You are using the **authorized Telegram username** configured during install.

## Request the app from your bot

Open a chat with your drHiro bot and send:

```
/apk
```

Your bot replies with the signed APK attached as a document, plus a caption:

```
drHiro Bridge v0.1.0
This is the official signed Android companion APK from your self-hosted drHiro server.
SHA-256: <checksum>
Install:
Download the attached APK.
Open it from Telegram or your Downloads folder.
If Android asks, allow installation from this source.
Open drHiro Bridge.
Only install APK files sent by your own authorized drHiro bot.
```

Only the authorized user can download the APK. Anyone else is denied.

## Verify the checksum

On your computer or phone, compare the SHA-256 in the bot's caption with the file you
downloaded:

```bash
sha256sum ~/Download/drhiro-bridge.apk
# compare with the SHA-256 shown by the bot / /apkinfo
```

On the server you can also run `./scripts/apk-verify.sh` to confirm the stored artifact
has not been tampered with.

## Android "install unknown apps" permission

1. Download the APK from Telegram.
2. Open it (from the chat or your Downloads folder).
3. Android may warn that the file is from an unknown source.
4. Tap **Settings / More details → Allow from this source** (the exact wording varies by
   Android version and OEM), then go back.
5. Tap **Install**.

> **Security rule:** only install APK files sent by **your own authorized drHiro bot**.
> Never install a drHiro Bridge APK from an unofficial source, and always check the SHA-256
> matches what your bot reports.

## Open drHiro Bridge

After install, open the app. On first launch it connects to your self-hosted drHiro server
(the connection target is configured on your server — it is never hardcoded to a third-party
host). Follow any on-screen steps to authorize Health Connect data.

## Upgrade

To upgrade, just send `/apk` again to your bot. You will receive the newest signed APK.
Repeat the install steps (the app upgrades in place; your data is preserved).

## Useful commands

| Command | What it does |
|---|---|
| `/apk` | Send the current signed APK to you |
| `/apkinfo` | Show version, Android requirement, file size, and SHA-256 |
| `/status` | Show non-sensitive server + APK status |
| `/help` | Explain commands and Android installation |

## Troubleshooting

| Symptom | What to check |
|---|---|
| `/apk` says the APK is not ready | Ensure `./apk/drhiro-bridge.apk` + `apk.json` exist on the server; run `./scripts/apk-info.sh`. |
| Checksum mismatch | The artifact may be corrupted/tampered. Run `./scripts/apk-verify.sh`; re-copy the signed APK. |
| Android won't install | You must allow installation from the source (see above). |
| Bot says "not authorized" | You are not the configured authorized username. |
