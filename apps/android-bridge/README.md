# drHiro Android Bridge (Phase 2 scaffold)

Small native Android app that reads user-approved Health Connect records
and uploads normalized data to drHiro Core. A PWA or Mini App **cannot**
reliably read Health Connect — a native app is required (blueprint §3.3).

## Why this exists

- Mi Fitness writes Xiaomi Mi Band data to Health Connect on Android.
- OMRON Connect may write BP to Health Connect depending on region/app.
- drHiro Core runs on the VPS; Health Connect lives on the phone. The
  bridge is the only authorized path between them.

## Status

Scaffold (Phase 2 in the MVP plan). Implements:
- `HealthConnectReader.kt` — incremental reads for steps, weight, BP,
  heart rate, sleep, exercise; maps to the drHiro batch contract.
- `SyncWorker.kt` — WorkManager periodic sync with idempotent batches.
- `ApiClient.kt` — batch upload to `/api/v1/ingest/health-connect/batch`.

Still to build:
- Device-code linking UI (POST /auth/android/device-code + exchange).
- Health Connect permission screen (steps, distance, calories, heart
  rate, resting HR, sleep, SpO2, weight, BP).
- Room-based encrypted local retry queue (Android Keystore).
- Sync diagnostics screen (last_device_sync_at / last_hc_record_at /
  last_server_upload_at shown separately).
- Bounded historical import (~30 days) on first link.

## Build

```bash
cd apps/android-bridge
# Requires Android SDK 35; set ANDROID_HOME
./gradlew assembleDebug
```

## Permission notes

- Health Connect requires API 28+ (Android 9).
- Background and historical reads need additional permissions.
- Sync only newly created/changed records in bounded batches per
  Health Connect guidance.
- Never interpret missing records as zero activity — the API surfaces
  missing as missing.
