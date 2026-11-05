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
 *   2. If photos are pending, upload ALL of them to
 *      POST /api/v1/tools/ingest_medical_batch, which atomically creates ONE
 *      encounter + ONE record with every scan bound (placeholder content).
 *   3. Write the staged ids to drhiro-medical-batch.json (shared with the MCP
 *      bridge) and inject them into the turn context via agent_turn_prepare,
 *      so the model knows a record already exists and must enrich THAT record
 *      with update_medical_record + verify before claiming success.
 *
 * Grouping rule: all photos pending in one message_received event = ONE
 * batch = ONE record. If the user later says separate messages were one
 * batch (or vice versa), the model re-groups via update_medical_record
 * (encounter_id) — the plugin never splits a batch on its own.
 *
 * Failure mode: fail-open. Any error is logged and the turn proceeds; the
 * MCP bridge's ensureBatchIngested safety net stages the batch on the first
 * tool call as a second deterministic chance.
 *
 * Config via env (same names as the MCP bridge):
 *   DRHIRO_MCP_API / OPENCLAW_DRHIRO_API, DRHIRO_OPENCLAW_SERVICE_TOKEN,
 *   DRHIRO_TELEGRAM_ID, DRHIRO_MEDIA_DIR, DRHIRO_MEDIA_CONSUMED_FILE,
 *   DRHIRO_MEDICAL_BATCH_FILE.
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
const MEDIA_TTL_MS = 30 * 60 * 1000;
const MEDIA_MAX_BYTES = 25 * 1024 * 1024;
const MEDIA_MIME = { ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".heic": "image/heic" };

let lastStage = null; // in-memory copy for agent_turn_prepare context injection

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

function takeAllInboundMedia() {
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
  const fresh = entries.filter((e) => !consumed.has(e.path));
  for (const e of fresh) consumed.add(e.path);
  if (fresh.length) writeConsumedSet({ consumed: Array.from(consumed).slice(-200), at: Date.now() });
  return fresh;
}

async function uploadBatch(filePaths) {
  const boundary = "----drhiroBatch" + Date.now();
  const hash = crypto.createHash("sha256");
  const parts = [];
  // batch_key computed after reading stats below; placeholder fields first.
  const stats = filePaths.map((p) => { const st = fs.statSync(p); return { p, size: st.size, mtime: st.mtimeMs }; });
  for (const s of stats) hash.update(s.p + ":" + s.size + ":" + Math.round(s.mtime));
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

async function stagePendingBatch() {
  if (!SERVICE_TOKEN || !TELEGRAM_ID) { warn("missing service token / telegram id — skipping deterministic ingest"); return null; }
  const media = takeAllInboundMedia();
  if (!media.length) return null;
  try {
    const r = await uploadBatch(media.map((m) => m.path));
    if (r && r.ok === true && r.data) {
      writeBatchStage(r.data);
      return r.data;
    }
    warn("ingest returned not-ok: " + JSON.stringify(r).slice(0, 200));
    return null;
  } catch (e) {
    warn("deterministic ingest failed (MCP bridge safety net remains): " + e);
    return null;
  }
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
    `Transcribe faithfully, then enrich THAT record via update_medical_record (record_id above), then VERIFY with get_medical_encounter ` +
    `before telling the user it is done. Never claim "Done" without the verification payload.`
  );
}

async function onMessageReceived(_event) {
  await stagePendingBatch();
}

function onAgentTurnPrepare() {
  const text = stageContextText();
  if (!text) return undefined;
  // OpenClaw context-injection shapes vary by version; return text-bearing
  // objects defensively — unknown fields are ignored by older hosts.
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
  // Callbacks-style hosts: api.on('message_received', fn)
  activate(api) {
    if (api && typeof api.on === "function") {
      api.on("message_received", onMessageReceived);
      api.on("agent_turn_prepare", onAgentTurnPrepare);
      api.on("before_prompt_build", onAgentTurnPrepare);
    }
  },
  // Direct-test helpers (used by the throwaway-stack verification script).
  _stagePendingBatch: stagePendingBatch,
  _stageContextText: stageContextText,
  _takeAllInboundMedia: takeAllInboundMedia,
};

if (typeof module !== "undefined" && module.exports) module.exports = plugin;
module.exports.default = plugin;
