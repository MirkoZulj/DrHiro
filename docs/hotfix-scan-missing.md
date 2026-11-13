# Hotfix: missing scan file for record d0a6583a (Eye exam — cataract note)

Date: 2026-11-06. Prod 0.11.0, https://144.91.107.8/drhiro-app/
Status: **read-only investigation — no prod data restored, created, or deleted.**

## Symptom

- Record `d0a6583a-dfeb-4471-bc28-510e37d66cd2` ("Eye exam — cataract (zelenjak) note"):
  `GET /api/v1/medical/scan/<id>?index=0` → 404.
- DB: `source_image_path=scans/0bfad360-9938-4216-8abd-b44d69e2003f/fb3cba1b-5ce8-4c1b-a7d0-d9bbab0732a9.jpg`,
  `source_image_paths_json=NULL`. File **not on disk** in prod (45 other jpgs in the
  user scan dir, this one missing; `find /opt` returns nothing). Text/content intact in DB.
- Served error before this hotfix was indistinguishable in the UI from a generic
  failure; the backend already returned `Scan image missing` (404) when the record
  existed but `resolve_scan_file` failed, vs `Record not found` for unknown ids.

## Deletion paths audited (code, local tree)

Only three `os.unlink`/`os.remove` sites touch scan files; none is a bulk/cleanup sweep:

1. `openclaw_tools.py` `tool_dedupe_record_scans` — **bytes mode** (~L1589):
   deletes a file only when its sha256 was already seen *for the same record*,
   keeps the first path per hash, rewrites `source_image_paths_json`/`source_image_path`
   to the kept list, audits `tools.dedupe_record_scans` with `before/after/missing`.
   **Perceptual mode** (~L1555): deletes only clustered duplicates (Hamming ≤ threshold,
   default 4), never unhashed files; missing files are reported in `missing` and *skipped*,
   not deleted. Both modes therefore leave a DB pointing at a deleted duplicate only if
   the kept twin is itself later lost — dedupe alone cannot orphan a *sole* scan:
   with `source_image_paths_json=NULL` and a single legacy path, there is no cluster
   partner, so neither mode would unlink the only file.
2. `routers/files.py` `delete_file` — GeneratedFile artefacts only
   (`generated_files_dir`), not medical scans.
3. Record/encounter deletion (`tool_delete_medical_record`, `tool_delete_medical_encounter`,
   `_merge_placeholder_siblings`) deletes **DB rows only** — it does not unlink scan
   files at all. That is an orphan-file leak, the opposite failure mode; it cannot
   explain a missing file with a surviving DB row.

No `shutil.rmtree`, retention sweeper, or cron cleanup for `medical_scans_dir`
was found in `apps/api/src` (grep: `os.unlink|os.remove|shutil.rmtree`).

## Docker logs

Local dev stack (`drhiro-dev-api-1`, image `drhiro-api:0.8.0-int`) logs show only
test-suite traffic (no `dedupe`, `unlink`, or the prod record/file ids) — this is
the **dev** container, not prod. Prod logs on the VPS (144.91.107.8) were not
accessible from this worktree, so no prod-side deletion event could be confirmed
or ruled out here. The audit trail that *would* answer it is the `audit` table
(`tools.dedupe_record_scans` rows carry `missing`/`removed_paths` metadata) — a
read-only prod query for that action, owner-approved, is the recommended next step;
it was not run against prod from here.

## Hypothesis (unconfirmed)

Most plausible, in order:

1. **Manual/operational removal or a partial restore/backup cycle on the VPS**
   (e.g. scans dir restored from an older tarball, or a single file deleted by
   hand) — consistent with a sole legacy-path file vanishing while the DB row
   survives and neighbouring files remain. `merge_placeholder_siblings` /
   dedupe cannot produce this shape (single path, NULL list).
2. **A dedupe run where this file was clustered as a duplicate of another scan
   that was subsequently also lost** — possible only if the record once had
   `source_image_paths_json` with ≥2 entries; current NULL contradicts the
   post-dedupe rewrite (dedupe writes the kept list, never NULL). Low probability.
3. **Upload never flushed to the prod volume** (failed write / wrong bind mount
   on the ingest host) with the DB row committed anyway — `store_scan` writes the
   file *before* the record commit, so this requires a volume/mount mismatch
   at ingest time. Possible, indistinguishable from (1) without prod logs.

## Recoverability

- **Not recoverable from the API/DB**: no scan bytes are stored in the database;
  the transcription text is intact, only the image is gone.
- **Possibly recoverable from Telegram**: the original photo lives in the owner's
  Telegram chat (the bot received it there). Owner can resend the photo, which
  re-ingests it as a new scan via the normal `ingest_medical_batch` flow.
  It cannot be reconstructed from the transcription.
- **Server-side, owner-approved checks only** (not done here): prod filesystem
  trash/snapshots, scans tarball backups (`data-dir bind mounts + backup hardening`
  — see commit `4a4264b feat(0.8.1): data-dir bind mounts + backup hardening (scans tar, manifests, …)`),
  and the audit-table query above. Do not fabricate/recreate the image.

## Hotfix behaviour (this branch)

- Backend: `scan_count`/`has_scans` (list, detail, record) count only scans that
  `resolve_scan_file` confirms on disk; `serve_scan` distinguishes
  `Record not found` vs `Scan image missing`. Valid scans unaffected.
- Frontend: no gallery when `has_scans` is false; `ScanThumb` shows a muted
  `Scan unavailable` placeholder on fetch failure; lightbox shows
  `Scan image missing` for a 404 instead of a generic error.
- Text view: `white-space: pre-wrap` on record/conclusion/encounter-notes
  paragraphs so single-`\n` OCR lines render as lines (edit `<textarea>` unchanged).

## Prod audit 2026-11-06 (owner question: how many lost)
- 29 records have scans in DB (64 referenced files: 29 single-path + 35 in 10 multi-path records)
- 45 files on disk, 19 missing files
- All 19 missing are single-path records (10 Nov 2 -pageN PDF batch + 9 Nov 2-5 plain). All 10 multi-path records intact (35 files).
- Pattern: missing = older ingests (Nov 2 14:16-14:30 batch + Nov 2 18:27 + Nov 5 09:54), present singles = Nov 5 19:43 / Nov 6 batches.

## Restoration 2026-11-06 (images-only, owner-approved)
- All 19 missing files recovered from owner Telegram resends (local Pi folders
  `drhiro-{nose,eye,affidea,batch-20261106-extra}-*`), re-attached to the
  EXISTING prod records — no records/encounters created, no DB writes.
  Files were copied to `/opt/drhiro/data/medical_scans/scans/0bfad360-…/`
  under the exact filenames already in `source_image_path` (45 → 64 files).
- Pre-write backup: `/opt/drhiro/backups/drphiro-pre-scanrestore-20261106T2240Z.dump`.
- PDF (Povijest bolesti 03.02.2026) converted with `pdftoppm` to JPG
  (serve_scan/store_scan only accept jpg/png/webp/heic).
- Ordering proof: DB content carries `Dok N/10` for the Affidea batch; local
  tesseract OCR (vision was down) confirmed pages 1–7, 9 (GE MAC2000), 10
  (6:05:22). Page 8 (UZV snimke) is by elimination/darkness only — see
  `/home/mirko/drhiro-staged-19-manifest.txt` for the full per-record map.
- Verified: `/health` 0.11.0; encounters nose 3/3, Affidea 10/10, Merkur 1/1,
  eyes 1/1 each; `GET /medical/scan/<id>` 200 for all 19. No GitHub push.
