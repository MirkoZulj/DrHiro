#!/usr/bin/env node
/**
 * drhiro-medical-ingest — OpenClaw plugin (message_received hook).
 *
 * WHY THIS EXISTS: on multi-photo medical batches the local model reads the
 * images but often never calls add_medical_encounter/add_medical_record — it
 * fabricates "Done" or ends with a bare "tool run finished". Storage must not
 * depend on LLM tool discipline.
 *
 * WHAT IT DOES (deterministically, BEFORE the LLM turn):
 *   1. On every inbound message, scan the gateway inbound-media dir for
 *      unconsumed photos (same consumed-marker file the MCP bridge uses).
 *   2. DEBOUNCE: buffer photos and wait for a quiet window
 *      (DRHIRO_MEDICAL_DEBOUNCE_MS, default 3000ms — aligned with the
 *      gateway collect debounce) before uploading, so one Telegram album
 *      resend that arrives as several message_received fragments stages
 *      as ONE batch, not one placeholder per fragment. Each new fragment
 *      resets the timer; buffered paths are only marked consumed after a
 *      successful upload, so a crash before flush loses nothing and a
 *      retry re-derives the same stable batch_key (idempotent replay via
 *      IngestBatch.batch_key on the API survives).
 *   3. Upload ALL buffered photos to POST /api/v1/tools/ingest_medical_batch,
 *      which atomically creates ONE encounter + ONE record with every scan
 *      bound (placeholder content).
 *   4. Write the staged ids to drhiro-medical-batch.json (shared with the MCP
 *      bridge) and inject them into the turn context via agent_turn_prepare,
 *      so the model knows a record already exists and must enrich THAT record
 *      with update_medical_record + verify before claiming success.
 *
 * Grouping rule: all photos buffered within one debounce window = ONE batch
 * = ONE record. If the user later says separate messages were one batch (or
 * vice versa), the model re-groups via update_medical_record (encounter_id,
 * merge_record_ids) — the plugin never splits a batch on its own.
 *
 * Failure mode: fail-open. Any error is logged and the turn proceeds; the
 * MCP bridge's ensureBatchIngested safety net stages the batch on the first
 * tool call as a second deterministic chance.
 *
 * Config via env (same names as the MCP bridge):
 *   DRHIRO_MCP_API / OPENCLAW_DRHIRO_API, DRHIRO_OPENCLAW_SERVICE_TOKEN,
 *   DRHIRO_TELEGRAM_ID, DRHIRO_MEDIA_DIR, DRHIRO_MEDIA_CONSUMED_FILE,
 *   DRHIRO_MEDICAL_BATCH_FILE, DRHIRO_MEDICAL_DEBOUNCE_MS.
 */
"use strict";

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const API_BASE = (process.env.DRHIRO_MCP_API || process.env.OPENCLAW_DRHIRO_API || "http://api:8000/api/v1").replace(/\/$/, "");
const SERVICE_TOKEN = process.env.DRHIRO_OPENCLAW_SERVICE_TOKEN || "";
const TELEGRAM_ID = process.env.DRHIRO_TELEGRAM_ID || "";
const MEDIA_DIR = process.env.DRHIRO_MEDIA_DIR || "/home/node/.openclaw/media/inbound";
const CONSUMED_FILE = process.env.DRHIRO_MEDIA_CONSUMED_FILE || "/home/node/.openclaw/tmp/drhiro-media-consumed.json";
const BATCH_FILE = process.env.DRHIRO_MEDICAL_BATCH_FILE || "/home/node/.openclaw/tmp/drhiro-medical-batch.json";
const DEBOUNCE_MS = Math.max(0, parseInt(process.env.DRHIRO_MEDICAL_DEBOUNCE_MS || "3000", 10) || 0);
const MEDIA_TTL_MS = 30 * 60 * 1000;
const MEDIA_MAX_BYTES = 25 * 1024 * 1024;
const MEDIA_MIME = { ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".heic": "image/heic" };

let lastStage = null; // in-memory copy for agent_turn_prepare context injection
let pending = []; // buffered media entries awaiting the debounce flush
let flushTimer = null;
let flushing = false;

function warn(msg) {
  try { console.warn("[drhiro-medical-ingest] " + msg); } catch (_) {}
}

function readConsumedSet() {
  try {
    const j = JSON.parse(fs.readFileSync(CONSUMED_FILE, "utf8"));
    if (j && Array.isArray(j.consumed)) return j;
  } catch (_) {}
  return { consumed: [], at: Date.now() };
}

function writeConsumedSet(set) {
  try {
    fs.mkdirSync(path.dirname(CONSUMED_FILE), { recursive: true });
    fs.writeFileSync(CONSUMED_FILE, JSON.stringify(set));
  } catch (e) { warn("consumed write failed: " + e); }
}

function markConsumed(entries) {
  if (!entries.length) return;
  const set = readConsumedSet();
  const consumed = new Set(set.consumed);
  for (const e of entries) consumed.add(e.path);
  writeConsumedSet({ consumed: Array.from(consumed).slice(-500), at: Date.now() });
}

function peekInboundMedia() {
  let entries = [];
  try {
    entries = fs.readdirSync(MEDIA_DIR).map((name) => {
      const p = path.join(MEDIA_DIR, name);
      const st = fs.statSync(p);
      return { path: p, name, mtime: st.mtimeMs, size: st.size };
    });
  } catch (_) { return []; }
  entries = entries.filter((e) => e.mtime > Date.now() - MEDIA_TTL_MS && e.size > 0 && e.size <= MEDIA_MAX_BYTES);
  entries.sort((a, b) => a.mtime - b.mtime);
  const consumed = new Set(readConsumedSet().consumed);
  const buffered = new Set(pending.map((e) => e.path));
  return entries.filter((e) => !consumed.has(e.path) && !buffered.has(e.path));
}

function takeAllInboundMedia() {
  // Back-compat name: peeks without consuming; consumption happens only
  // after a successful upload (see flushPendingBatch).
  return peekInboundMedia();
}

async function uploadBatch(filePaths) {
  const boundary = "----drhiroBatch" + Date.now();
  const hash = crypto.createHash("sha256");
  const parts = [];
  const stats = filePaths.map((p) => { const st = fs.statSync(p); return { p, size: st.size, mtime: st.mtimeMs }; });
  // Stable batch_key: sorted by path so a retry of the same file set
  // re-derives the identical key and the API deduplicates via batch_key.
  const ordered = [...stats].sort((a, b) => a.p.localeCompare(b.p));
  for (const s of ordered) hash.update(s.p + ":" + s.size + ":" + Math.round(s.mtime));
  const batchKey = "medbatch-" + hash.digest("hex").slice(0, 32);
  parts.push(Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="batch_key"\r\n\r\n${batchKey}\r\n`));
  for (const filePath of filePaths) {
    const bytes = fs.readFileSync(filePath);
    const extMatch = filePath.match(/\.([a-z0-9]+)$/i);
    const ext = extMatch ? "." + extMatch[1].toLowerCase() : "";
    const mime = MEDIA_MIME[ext] || "application/octet-stream";
    parts.push(Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="files"; filename="scan${ext}"\r\nContent-Type: ${mime}\r\n\r\n`));
    parts.push(bytes);
    parts.push(Buffer.from("\r\n"));
  }
  parts.push(Buffer.from(`\r\n--${boundary}--\r\n`));
  const body = Buffer.concat(parts);
  const res = await fetch(API_BASE + "/tools/ingest_medical_batch", {
    method: "POST",
    headers: {
      "X-Service-Token": SERVICE_TOKEN,
      "X-Telegram-Id": TELEGRAM_ID,
      "Content-Type": `multipart/form-data; boundary=${boundary}`,
      "Content-Length": String(body.length),
    },
    body,
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`API ${res.status}: ${text.slice(0, 300)}`);
  return JSON.parse(text);
}

function writeBatchStage(stage) {
  lastStage = stage;
  try {
    fs.mkdirSync(path.dirname(BATCH_FILE), { recursive: true });
    fs.writeFileSync(BATCH_FILE, JSON.stringify({ ...stage, at: Date.now() }));
  } catch (e) { warn("batch stage write failed: " + e); }
}

async function flushPendingBatch() {
  flushTimer = null;
  if (flushing) return null;
  if (!pending.length) return null;
  if (!SERVICE_TOKEN || !TELEGRAM_ID) { warn("missing service token / telegram id — skipping deterministic ingest"); return null; }
  flushing = true;
  const batch = pending;
  pending = [];
  try {
    // Only upload files that still exist (a file could vanish mid-burst).
    const existing = batch.filter((e) => { try { fs.statSync(e.path); return true; } catch (_) { return false; } });
    if (!existing.length) return null;
    const r = await uploadBatch(existing.map((m) => m.path));
    if (r && r.ok === true && r.data) {
      markConsumed(existing);
      writeBatchStage(r.data);
      return r.data;
    }
    // Not-ok: leave un-consumed so the MCP safety net / next event retries
    // with the same batch_key (idempotent).
    pending = batch.concat(pending);
    warn("ingest returned not-ok: " + JSON.stringify(r).slice(0, 200));
    return null;
  } catch (e) {
    pending = batch.concat(pending);
    warn("deterministic ingest failed (MCP bridge safety net remains): " + e);
    return null;
  } finally {
    flushing = false;
  }
}

function scheduleFlush() {
  if (flushTimer) clearTimeout(flushTimer);
  if (DEBOUNCE_MS <= 0) {
    // Zero debounce: flush on the next tick (still one upload per event loop).
    flushTimer = setTimeout(() => { flushPendingBatch().catch(() => {}); }, 0);
  } else {
    flushTimer = setTimeout(() => { flushPendingBatch().catch(() => {}); }, DEBOUNCE_MS);
  }
  if (flushTimer && typeof flushTimer.unref === "function") flushTimer.unref();
}

async function stagePendingBatch() {
  // Buffer newly arrived media and (re)start the debounce timer. Returns
  // the staged batch only when called after a flush (tests) — in the live
  // hook path the flush happens asynchronously after the quiet window.
  const media = peekInboundMedia();
  if (media.length) {
    pending = pending.concat(media);
    scheduleFlush();
  }
  return null;
}

function stageContextText() {
  if (!lastStage) {
    try {
      const j = JSON.parse(fs.readFileSync(BATCH_FILE, "utf8"));
      if (j && j.record_id && Date.now() - (j.at || 0) < MEDIA_TTL_MS) lastStage = j;
    } catch (_) {}
  }
  if (!lastStage) return null;
  return (
    `[DRHIRO_MEDICAL_BATCH] A medical photo batch was ALREADY staged deterministically before this turn: ` +
    `encounter_id=${lastStage.encounter_id} record_id=${lastStage.record_id} photo_count=${lastStage.photo_count || "?"} ` +
    `(status: ${lastStage.status || "staged_transcription_pending"}). The record exists — do NOT create a new record for these photos. ` +
    `Transcribe faithfully, then enrich THAT record via update_medical_record (record_id above, merge_record_ids for any same-burst placeholder fragments), then VERIFY with get_medical_encounter ` +
    `before telling the user it is done. Never claim "Done" without the verification payload.`
  );
}

async function onMessageReceived(_event) {
  await stagePendingBatch();
}

function onAgentTurnPrepare() {
  const text = stageContextText();
  if (!text) return undefined;
  return { prependContext: text, appendContext: text, context: text, text };
}

const plugin = {
  id: "drhiro-medical-ingest",
  name: "drhiro-medical-ingest",
  hooks: {
    message_received: onMessageReceived,
    agent_turn_prepare: onAgentTurnPrepare,
    before_prompt_build: onAgentTurnPrepare,
  },
  activate(api) {
    if (api && typeof api.on === "function") {
      api.on("message_received", onMessageReceived);
      api.on("agent_turn_prepare", onAgentTurnPrepare);
      api.on("before_prompt_build", onAgentTurnPrepare);
    }
  },
  // Direct-test helpers (used by the throwaway-stack verification script).
  _stagePendingBatch: stagePendingBatch,
  _flushPendingBatch: flushPendingBatch,
  _stageContextText: stageContextText,
  _takeAllInboundMedia: takeAllInboundMedia,
  _peekInboundMedia: peekInboundMedia,
  _pendingCount: () => pending.length,
  _debounceMs: DEBOUNCE_MS,
};

if (typeof module !== "undefined" && module.exports) module.exports = plugin;
module.exports.default = plugin;
