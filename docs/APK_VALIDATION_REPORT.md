# Android Bridge APK Build & Validation Report

**Date:** 2026-08-30
**Build host:** remote Windows box (JDK 17, Android SDK 36, AGP 8.9.1, Gradle 8.11.1)
**Status:** RELEASE APK built and validated. On-device install test pending a physical device.

## APK identity

| Item | Value |
|---|---|
| Package ID (applicationId) | `app.drhiro.bridge` |
| Version name | `0.1.12` |
| Version code | `12` |
| minSdk / targetSdk / compileSdk | 28 / 35 / 36 |

## Signing

| Item | Result |
|---|---|
| Debug- or release-signed | **Release-signed** (not debug) |
| Signing scheme | APK Signature Scheme **v2** (verified true) |
| Signer certificate DN | `CN=drHiro Bridge, O=drHiro, C=US` |
| Certificate SHA-256 | `852c5a0b2f7c752b595348008da85599b518c34289d6d5117d49427dd5d88881` |
| Key algorithm / size | RSA / 2048 |
| apksigner verify exit code | `0` (PASS) |

The APK is signed with a **new private release keystore** (`drhiro-release.jks`) generated for
this release. It is NOT the Android debug keystore.

## File metrics

| Item | Value |
|---|---|
| APK file size | **3,015,752 bytes (~2.9 MB)** |
| SHA-256 checksum | `8c45352902f059918d2a0ce873dd8ac21c078ada3bdb1bfd0432301370fb6e91` |
| `file` type | `Android package (APK), with APK Signing Block` |

## Telegram size-limit validation

| Check | Result |
|---|---|
| Under 45 MB (recommended) | **PASS** (2.9 MB) |
| Under 50 MB (Bot API hard cap) | **PASS** |

## Signature verification result

- `apksigner verify --verbose --print-certs` → **Verifies** (v2 scheme), exit 0.
- Cert belongs to the drHiro release key, not debug.

## Android installation test

**PENDING — no physical device connected.** The Windows build box has no emulator and no
ADB-connected device (confirmed). Per the operator's decision, the build is fully validated
(signature/checksum/size) and the on-device installation test is reported as pending a
physical device being connected for `adb install`.

## Deep-link & pairing integration (verified in merged manifest)

- `drhiro://pair` custom scheme intent-filter is present in the built APK's merged manifest
  (verified via `aapt dump xmltree` → `android:scheme="drhiro"`).
- The app removes the previous hardcoded production URL. Server URL + device credential come
  from pairing (`/pair/exchange`) and are stored in Android secure storage
  (EncryptedSharedPreferences). HTTPS required for remote; HTTP only for trusted-LAN dev
  with a visible warning. Manual "enter server address and pairing code" fallback included.

## Private signing files excluded from Git

The following are **private** and excluded from the repository (workspace `apk-build/` is
gitignored):

- `apk-build/drhiro-release.jks` — the release signing keystore (also at `C:\drhiro-release.jks` on the build host).
- `apk-build/KEYSTORE_PASS.txt` — the keystore/key password.

No keystore or password appears in any tracked file or git history.

## Secret scan of the built APK

- `strings` over the APK's `classes.dex`: **0 hits** for the old production URL, private IPs,
  user IDs, bot tokens, or AI keys.
- No bot token, AI key, TrueForge key, root credential, server URL, or permanent user token
  is compiled into the APK.

## Artifact location

- `/home/mirko/projects/drhiro-trueforge/apk-build/drhiro-bridge-release.apk`
- Build host: `C:\bridge-src\app\build\outputs\apk\release\app-release.apk`

## Boundary

This APK has **not** been uploaded to any Telegram bot or sent to any real user. No real
pairing token has been created. External delivery requires explicit operator approval.
