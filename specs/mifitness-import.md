# Mi Fitness export import — gap-filling importer (spec)

**Status:** approved by Kresimir 2026-10-05 ("Health Connect leads, Mi Fitness export is here to fill the gaps… ideally we would create an uploader in dashboard and have the skill/tool for bot to log this in the future, without overrunning already existing data.")
**Owner:** Jeeves (spec, verification) → Jerry (implementation)

---

## 1. Goal

Import the Xiaomi Mi Fitness manual export into drHiro so that two years of device history fills the gaps
Health Connect never delivered — without ever overwriting or disturbing data Health Connect already has.

## 2. Principles (user-decided, non-negotiable)

1. **Health Connect leads.** The export fills gaps only. It never replaces a value we already hold.
2. **Never overwrite.** No existing row is modified, retagged or deleted. Additive only.
3. **Idempotent.** Re-running an import is a no-op; a partially-failed import can be re-run safely.
4. **Heart rate is the exception — keep ALL of it.** The user used HR data to diagnose and treat sleep
   apnoea and explicitly does not want any HR input disregarded. HR is a *sample* metric: union, not gap-fill.
5. **Display is aggregated; storage is complete.** Raw samples stay queryable, but what the dashboard
   shows is aggregated (the user's words: "Aggregated data is welcome as raw data is too much for the user").
6. **Nothing else from the export is a display priority** — steps/calories/sleep/stress/stand are wanted,
   the remaining device scores (vitality etc.) are not.

## 3. The archive (verified facts)

- Path on the VPS: `/opt/drhiro/imports/incoming/20261001_1587352415_MiFitness_ams1_data_copy.zip`
- Size 23,007,290 bytes, md5 `23c5a07822542ec58779068a85c7ab7f`
- Encrypted (ZipCrypto; every entry). Password is supplied per import run and **must never be stored** in
  config, DB, logs or image.
- Contents: raw stream 394 MB / **3,692,842 rows**; daily aggregates **8,820 rows**; member/fitness profile;
  data-source list; `sport_record` **empty** (no workouts); routes **empty**; guide PDF.
- Raw columns: `Uid,Sid,Key,Time,Value,UpdateTime` — `Time` = unix seconds UTC, `Value` = JSON.
- Daily columns: `Uid,Sid,Tag,Key,Time,Value,UpdateTime` — `Time` = day anchor at 00:00 UTC, which is
  01:00/02:00 local, so **the UTC date equals the local date**; `local_date` can be derived safely.

## 4. Raw stream — decision per key (measured counts)

| Key | Rows | Span | Decision |
|---|---|---|---|
| `light_sensitivity_value` | 1,056,232 | 2024-09-21..2026-10-01 | **SKIP** — sensor telemetry |
| `body_momentum` | 1,055,540 | 2024-09-21..2026-10-01 | **SKIP** — sensor telemetry |
| `heart_rate` | 780,239 | 2024-09-21..2026-10-01 | **IMPORT ALL** → `measurements.heart_rate` |
| `calories` | 478,360 | 2024-09-21..2026-10-01 | skip per-sample; daily aggregate carries the day |
| `steps` | 198,293 | 2024-09-21..2026-10-01 | skip per-sample; daily aggregate carries steps+distance+calories |
| `stress` | 87,506 | 2024-09-22..2026-10-01 | skip per-sample; daily aggregate carries avg/max/min |
| `intensity` | 26,413 | 2024-09-21..2026-10-01 | skip per-sample; daily aggregate carries duration |
| `valid_stand` | 8,262 | 2024-09-22..2026-09-30 | skip per-sample; daily aggregate carries the count |
| `sleep` | 790 | 2024-09-22..2026-10-01 | **IMPORT** → `measurements.sleep` (rich session) |
| `vitality` | 581 | 2024-09-22..2026-10-01 | **SKIP** (Xiaomi score, not used) |
| `resting_heart_rate` | 501 | 2024-09-22..2026-10-01 | **IMPORT** → `measurements.resting_heart_rate` |
| `single_heart_rate` | 61 | 2025-04-21..2026-04-10 | **IMPORT** → `measurements.heart_rate`, `recording_method='manual'` |
| `spo2` | 52 | 2024-09-22..**2025-05-04** | **IMPORT** → `measurements.spo2` (thin — band stopped) |
| `weight` | 10 | 2024-09-22..2026-09-10 | **IMPORT** → `measurements.weight` (with `bmi`) |
| `single_spo2` | 1 | 2025-05-04 | **IMPORT** → `measurements.spo2` |

Telemetry skipped: **2,111,772 rows of 3,692,842 (57%)**. Imported raw: 781,653 rows.

Raw JSON shapes (verified samples):
- `heart_rate`: `{"time":…,"bpm":70,"type":0}` (type 0 = continuous; manual = `single_heart_rate` `{"bpm":74}`)
- `resting_heart_rate`: `{"bpm":61,"date_time":…}`
- `sleep`: `{"avg_breath":11,"avg_hr":63,"bedtime":…,"sleep_deep_duration":151,"sleep_light_duration":127,
  "sleep_rem_duration":103,"duration":381,"awake_count":…,"sleep_awake_duration":…,"max_hr":97,"min_hr":53,
  "wake_up_time":…,"timezone":…,"items":[{"start_time":…,"end_time":…,"state":2|3|4}, …]}`
- `spo2`: `{"time":…,"spo2":97}` · `weight`: `{"bmi":29.736746,"weight":98.5,"sid":"xiaomiwear_app_manually"}`

## 5. Daily aggregates — mapping to `daily_aggregates`

`daily_aggregates` exists, is **empty, and nothing writes it today** (verified: only `models.py:126` references
it — no code path inserts). It is free for this purpose.

| Export key | Rows | → `metric_type` | `value_json` |
|---|---|---|---|
| `steps` | 1,409 | `steps` | `{"steps":N,"distance_m":N,"calories":N}` |
| `calories` | 1,409 | `active_calories` | `{"kcal":N}` |
| `sleep` | 1,273 | `sleep` | `{"duration_min":N,"deep_min":N,"light_min":N,"score":N,"avg_hr":N,"awake_min":N}` |
| `intensity` | 1,107 | `active_minutes` | `{"minutes":N}` |
| `heart_rate` | 1,068 | `heart_rate` | `{"avg_bpm":N,"max_bpm":N,"min_bpm":N,"zones":{…},"abnormal_count":N}` |
| `stress` | 1,031 | `stress` | `{"avg":N,"max":N,"min":N,"scale":{…}}` |
| `valid_stand` | 768 | `stand_hours` | `{"count":N}` |
| `goal` | 741 | `goal_achievement` | `{"items":[{"field":N,"achieved":N,"target":N}]}` |
| `weight` | 8 | **SKIP** | `{"has_data":true}` — carries no value |
| `spo2` | 5 | `spo2` | `{"avg":N}` |

`local_date` = the UTC date of `Time`, formatted `YYYY-MM-DD` (the column is `character varying`).

**New metric types required** in `drhiro_schema.metrics.MetricType`: `stress`, `active_minutes`,
`stand_hours`, `goal_achievement`. (`distance`, `active_calories`, `resting_heart_rate` already exist with
zero rows — no schema change needed there.) `metric_type` in the DB is `varchar`, so **no migration is needed
for the values themselves**.

**Schema change required:** `daily_aggregates` has **no provenance column**. Add `source_provider varchar`
(+ `source_record_id varchar`) so a row's origin is explicit and "Health Connect leads" is enforceable in
data rather than by convention. Alembic migration; backfill existing rows as `computed` (there are none).

## 6. Gap-fill rules — by metric class

**Daily metrics** (`steps`, `active_calories`, `sleep`, `stress`, `active_minutes`, `stand_hours`):
insert the export row **only if no row exists** for `(user_id, local_date, metric_type)` from any other
source. Health Connect's day wins; the export fills the empty days. On the overlap window (HC data begins
2026-08-09) this means the export contributes history *before* 9 August and leaves the recent days alone.

**Sample metrics** (`heart_rate`, `resting_heart_rate`, `spo2`, `weight`, `blood_pressure`):
**union.** Import every export sample. Two reasons: a handful of HC samples cannot "cover" a day of
continuous HR, and the user's explicit requirement is that no HR input is discarded. Dedupe is guaranteed
by `uq_measurement_source (user_id, source_provider, source_record_id)`.

**Provenance:** every imported row is `source_provider='mifitness_export'`,
`recording_method='automatic'` (or `'manual'` for `single_heart_rate` / weight), and
`source_record_id='mf:<Sid>:<Key>:<Time>'` — deterministic, so re-imports are no-ops.

## 7. Deliverables, phased

**P1 — importer (this dispatch).** `services/mifitness_import.py`: ZIP/CSV reader, key→metric mapper,
gap-fill logic, `--dry-run`, reconciliation report. CLI entry point. `POST /import/mifitness`
(multipart: file + password + dry_run) extending the existing `routers/import_csv.py` importer-registry
pattern; the route's size cap raised for this importer only (accept the 23 MB ZIP, decompress server-side).
Report persisted into `ingest_batches` (`batch_id='mifitness-<timestamp>'`, `result_json`=report) — no new table.
Alembic migration for `daily_aggregates.source_provider`.

**P2 — dashboard uploader.** Settings → Import in the web app: file + password, dry-run first, then a
progress/result view listing imported/skipped/duplicate counts and the days filled. Web bundle bump.

**P3 — bot tool + skill.** `import_mifitness_export` tool for the OpenClaw gateway. Because a Telegram bot
cannot download files over 20 MB (which is exactly why the user's upload failed), the large-file path is the
VPS inbox: the user drops the export in `/opt/drhiro/imports/incoming/`, then tells the bot to import it by
filename; files ≤ 20 MB may also arrive as a Telegram `file_id`. Skill rule documents both paths.

**P4 — dashboard tiles.** Aggregated views for the newly available metrics: stress, active minutes, stand
hours, distance, active calories, resting HR, sleep score/stages. Display aggregates, not raw samples.

## 8. Acceptance (P1)

1. **Dry run reconciles exactly:** rows read 3,692,842 = imported 781,653 + skipped-telemetry 2,111,772 +
   skipped-per-sample 799,417; daily rows read 8,820 = mapped 8,812 + skipped 8.
2. **Real run on the dev DB** (never production first): HR rows +780,239 with `source_provider='mifitness_export'`;
   daily rows ~8,812; no existing row's `updated_at` changes.
3. **Idempotent:** second run inserts 0 rows and reports 0 errors.
4. **Gap-fill proof:** a day already covered by Health Connect for a *daily* metric receives **no** export row;
   an HC-covered day's HR samples **are** imported (union rule). Demonstrated with fixtures on the dev DB.
5. **No data loss:** `measurements` row count per provider before/after is unchanged for every non-export
   provider; HC row count identical.
6. **Password hygiene:** the password appears in no file, log line, image layer or DB row.

## 9. Risks / open items

- **Volume:** 780k HR rows ≈ 100× the current `measurements` size (~3.8 MB → ~0.4 GB with indexes).
  VPS has 50 GB free — acceptable, but the import must be batched and must not lock the table.
- **`activities` table is dead** (4 rows, newest 2026-09-11); device exercise lives in `measurements`. Not
  part of this spec, but two competing sources should be reconciled eventually.
- **Workouts carry no calories or distance** (Bridge sends `{"exercise_type":"79","duration_min":N}`), so the
  activity panel reports 0 kcal burned. This export cannot fill that either (its workout file is empty).
- **Weight is thin on both sides** (export 10 manual entries; our DB 5 rows, last 2026-09-10) — the Mi scale
  is not syncing to either path. Separate investigation.
- **SpO2 is effectively dead** (52 samples, none after 2025-05-04).
- Importing is additive but large; take a verified `pg_dump` before the production run and keep
  `source_provider='mifitness_export'` as the rollback handle.
