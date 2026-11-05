#!/usr/bin/env node
/**
 * drHiro MCP server (dependency-free Node implementation).
 *
 * Speaks MCP (JSON-RPC over stdio) with zero npm dependencies: node has
 * fetch and readline built in. OpenClaw launches this via the mcpServers
 * config; the agent sees the real drHiro tool schemas and calls them.
 *
 * Tools forward to the drHiro Core API with the signed service token and
 * the deployment's configured Telegram identity (NEVER a model-supplied one).
 */

"use strict";

const fs = require("fs");
const readline = require("readline");

const API_BASE = process.env.DRHIRO_MCP_API || "http://api:8000/api/v1";
const SERVICE_TOKEN = process.env.DRHIRO_OPENCLAW_SERVICE_TOKEN || "";
// The deployment's bound Telegram identity. The bridge forwards ONLY this
// identity to the API as X-Telegram-Id. A model-supplied telegram_id that
// differs is silently dropped — a confused/manipulated model can never act
// as a different paired user. When unset, no tools can be called (fail-closed).
const CONFIGURED_TELEGRAM_ID = process.env.DRHIRO_TELEGRAM_ID || "";
// Inbound message id shared by the drhiro-msgid plugin (message_received hook).
// The plugin writes the real Telegram message id of the latest inbound message;
// we read it here so a meal log always carries a trustworthy idempotency key
// even when the model omits or guesses telegram_message_id.
const INBOUND_ID_FILE = process.env.DRHIRO_INBOUND_ID_FILE || "/home/node/.openclaw/tmp/drhiro-msgid.json";
const INBOUND_ID_TTL_MS = 30 * 60 * 1000;
function readInboundMessageId() {
  try {
    const raw = fs.readFileSync(INBOUND_ID_FILE, "utf8");
    const j = JSON.parse(raw);
    if (j && j.id && Number.isFinite(j.at) && Date.now() - j.at < INBOUND_ID_TTL_MS) return String(j.id);
  } catch (_) {}
  return null;
}

// ---- Inbound media relay (medical scans) ----
// The gateway saves each inbound Telegram photo to media/inbound/<uuid>.jpg in
// the shared state volume. The model can never relay binary itself, so the
// medical-record tools read the NEWEST unconsumed inbound media file
// server-side and upload the bytes to the API (multipart). A consumed-marker
// state file prevents re-attaching an older photo on a later turn.
const MEDIA_DIR = process.env.DRHIRO_MEDIA_DIR || "/home/node/.openclaw/media/inbound";
const MEDIA_CONSUMED_FILE = process.env.DRHIRO_MEDIA_CONSUMED_FILE || "/home/node/.openclaw/tmp/drhiro-media-consumed.json";
const MEDIA_TTL_MS = 30 * 60 * 1000;
const MEDIA_MAX_BYTES = 25 * 1024 * 1024;

function readConsumedSet() {
  try {
    const j = JSON.parse(fs.readFileSync(MEDIA_CONSUMED_FILE, "utf8"));
    if (j && Array.isArray(j.consumed)) return j;
  } catch (_) {}
  return { consumed: [], at: Date.now() };
}

function writeConsumedSet(set) {
  try {
    fs.mkdirSync(require("path").dirname(MEDIA_CONSUMED_FILE), { recursive: true });
    fs.writeFileSync(MEDIA_CONSUMED_FILE, JSON.stringify(set));
  } catch (e) { try { console.warn("[drhiro-mcp] media consumed write failed: " + e); } catch (_) {} }
}

/** Newest unconsumed inbound media file within TTL; marks it consumed. */
function takeLatestInboundMedia() {
  const all = takeAllInboundMedia();
  return all.length ? all[all.length - 1] : null;
}

/** ALL unconsumed inbound media files within TTL (oldest first); marks them consumed. */
function takeAllInboundMedia() {
  let entries = [];
  try {
    entries = fs.readdirSync(MEDIA_DIR).map((name) => {
      const p = require("path").join(MEDIA_DIR, name);
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

function peekUnconsumedMediaCount() {
  try {
    let entries = [];
    try {
      entries = fs.readdirSync(MEDIA_DIR).map((name) => {
        const p = require("path").join(MEDIA_DIR, name);
        const st = fs.statSync(p);
        return { path: p, mtime: st.mtimeMs, size: st.size };
      });
    } catch (_) { return 0; }
    const consumed = new Set(readConsumedSet().consumed);
    return entries.filter((e) => e.mtime > Date.now() - MEDIA_TTL_MS && e.size > 0 && e.size <= MEDIA_MAX_BYTES && !consumed.has(e.path)).length;
  } catch (_) { return 0; }
}

const BATCH_FILE = process.env.DRHIRO_MEDICAL_BATCH_FILE || "/home/node/.openclaw/tmp/drhiro-medical-batch.json";
function writeBatchStage(stage) {
  try {
    fs.mkdirSync(require("path").dirname(BATCH_FILE), { recursive: true });
    fs.writeFileSync(BATCH_FILE, JSON.stringify({ ...stage, at: Date.now() }));
  } catch (e) { try { console.warn("[drhiro-mcp] batch stage write failed: " + e); } catch (_) {} }
}
function readBatchStage() {
  try {
    const j = JSON.parse(fs.readFileSync(BATCH_FILE, "utf8"));
    if (j && j.record_id && j.encounter_id && Number.isFinite(j.at) && Date.now() - j.at < BATCH_TTL_MS) return j;
  } catch (_) {}
  return null;
}
const BATCH_TTL_MS = 30 * 60 * 1000;

/** Burst coalescing: wait until inbound media has been quiet for the
 *  debounce window (aligned with the gateway collect debounce ~3s), so an
 *  album arriving as several fragments stages as ONE batch. Bounded so a
 *  never-quiet burst cannot block a tool call indefinitely. */
async function waitForBurstQuiet(maxWaitMs) {
  const debounceMs = Math.max(0, parseInt(process.env.DRHIRO_MEDICAL_DEBOUNCE_MS || "3000", 10) || 0);
  if (debounceMs <= 0) return;
  const deadline = Date.now() + (maxWaitMs || 8000);
  while (Date.now() < deadline) {
    let newest = 0;
    try {
      const consumed = new Set(readConsumedSet().consumed);
      for (const name of fs.readdirSync(MEDIA_DIR)) {
        const p = require("path").join(MEDIA_DIR, name);
        if (consumed.has(p)) continue;
        try { newest = Math.max(newest, fs.statSync(p).mtimeMs); } catch (_) {}
      }
    } catch (_) { return; }
    if (!newest || Date.now() - newest >= debounceMs) return;
    await new Promise((r) => setTimeout(r, 250));
  }
}

/** Deterministic batch staging: upload ALL pending photos as ONE encounter + ONE record. */
async function ingestPendingBatch(args, extra) {
  await waitForBurstQuiet();
  const media = takeAllInboundMedia();
  if (!media.length) return { ok: false, error: "No pending inbound medical photos to ingest." };
  const crypto = require("crypto");
  const hash = crypto.createHash("sha256");
  const ordered = [...media].sort((a, b) => a.path.localeCompare(b.path));
  for (const m of ordered) hash.update(m.path + ":" + m.size + ":" + Math.round(m.mtime));
  const batchKey = "medbatch-" + hash.digest("hex").slice(0, 32);
  const up = await uploadBatchScans(args, media.map((m) => m.path), {
    encounter_date: (extra && extra.encounter_date) || undefined,
    facility: (extra && extra.facility) || undefined,
    title: (extra && extra.title) || undefined,
    notes: (extra && extra.notes) || undefined,
    caption: (extra && extra.caption) || undefined,
    batch_key: batchKey,
  });
  if (up && up.ok === true && up.data) writeBatchStage(up.data);
  return up;
}

/**
 * Safety net: before ANY tool runs (except deletes / the ingest itself),
 * deterministically stage any pending medical photos. This makes storage
 * independent of the model choosing the right tool — a record always exists
 * before the LLM can claim success. Failures never block the real tool call.
 */
async function ensureBatchIngested(toolName) {
  const SKIP = new Set(["ingest_medical_batch", "delete_medical_record", "delete_medical_encounter", "undo_last_user_action"]);
  if (SKIP.has(toolName)) return null;
  if (peekUnconsumedMediaCount() === 0) return null;
  try {
    const r = await ingestPendingBatch({ telegram_id: CONFIGURED_TELEGRAM_ID }, {});
    return (r && r.ok === true && r.data) ? r.data : null;
  } catch (_) { return null; }
}

const MEDIA_MIME = { ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".heic": "image/heic" };

/** Multipart upload of a media file to the API scan route. */
async function uploadScan(args, filePath, recordId) {
  if (!SERVICE_TOKEN) return { ok: false, error: "DRHIRO_OPENCLAW_SERVICE_TOKEN not set" };
  const telegramId = resolveTelegramId(args && args.telegram_id);
  if (!telegramId) return { ok: false, error: "No configured Telegram identity (DRHIRO_TELEGRAM_ID not set)" };
  let bytes;
  try { bytes = fs.readFileSync(filePath); } catch (e) { return { ok: false, error: "Cannot read media file: " + e }; }
  const extMatch = filePath.match(/\.([a-z0-9]+)$/i);
  const ext = extMatch ? "." + extMatch[1].toLowerCase() : "";
  const mime = MEDIA_MIME[ext] || "application/octet-stream";
  const boundary = "----drhiroScan" + Date.now();
  const parts = [];
  if (recordId) {
    parts.push(Buffer.from(
      `--${boundary}\r\nContent-Disposition: form-data; name="record_id"\r\n\r\n${recordId}\r\n`));
  }
  parts.push(Buffer.from(
    `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="scan${ext}"\r\nContent-Type: ${mime}\r\n\r\n`));
  parts.push(bytes);
  parts.push(Buffer.from(`\r\n--${boundary}--\r\n`));
  const body = Buffer.concat(parts);
  try {
    const res = await fetch(API_BASE + "/tools/upload_medical_scan", {
      method: "POST",
      headers: {
        "X-Service-Token": SERVICE_TOKEN,
        "X-Telegram-Id": telegramId,
        "Content-Type": `multipart/form-data; boundary=${boundary}`,
        "Content-Length": String(body.length),
      },
      body,
    });
    const text = await res.text();
    if (!res.ok) return { ok: false, error: `API ${res.status}: ${text.slice(0, 300)}` };
    try { return JSON.parse(text); } catch { return { ok: true, raw: text }; }
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}


/** Multipart upload of MULTIPLE media files as one batch. */
async function uploadBatchScans(args, filePaths, fields) {
  if (!SERVICE_TOKEN) return { ok: false, error: "DRHIRO_OPENCLAW_SERVICE_TOKEN not set" };
  const telegramId = resolveTelegramId(args && args.telegram_id);
  if (!telegramId) return { ok: false, error: "No configured Telegram identity (DRHIRO_TELEGRAM_ID not set)" };
  const boundary = "----drhiroBatch" + Date.now();
  const parts = [];
  for (const [k, v] of Object.entries(fields || {})) {
    if (v !== undefined && v !== null && String(v) !== "") {
      parts.push(Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="${k}"\r\n\r\n${v}\r\n`));
    }
  }
  for (const filePath of filePaths) {
    let bytes;
    try { bytes = fs.readFileSync(filePath); } catch (e) { return { ok: false, error: "Cannot read media file: " + e }; }
    const extMatch = filePath.match(/\.([a-z0-9]+)$/i);
    const ext = extMatch ? "." + extMatch[1].toLowerCase() : "";
    const mime = MEDIA_MIME[ext] || "application/octet-stream";
    parts.push(Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="files"; filename="scan${ext}"\r\nContent-Type: ${mime}\r\n\r\n`));
    parts.push(bytes);
    parts.push(Buffer.from("\r\n"));
  }
  parts.push(Buffer.from(`\r\n--${boundary}--\r\n`));
  const body = Buffer.concat(parts);
  try {
    const res = await fetch(API_BASE + "/tools/ingest_medical_batch", {
      method: "POST",
      headers: {
        "X-Service-Token": SERVICE_TOKEN,
        "X-Telegram-Id": telegramId,
        "Content-Type": `multipart/form-data; boundary=${boundary}`,
        "Content-Length": String(body.length),
      },
      body,
    });
    const text = await res.text();
    if (!res.ok) return { ok: false, error: `API ${res.status}: ${text.slice(0, 300)}` };
    try { return JSON.parse(text); } catch { return { ok: true, raw: text }; }
  } catch (e) {
    return { ok: false, error: String(e && e.message || e) };
  }
}

function tool(name, description, inputSchema, handler) {
  return { name, description, inputSchema, handler };
}

const TOOLS = [
  tool("get_my_today_summary", "Get the current user's today summary (steps, BP, weight, water, meals).",
    { type: "object", properties: { telegram_id: { type: "string" } }, required: ["telegram_id"] },
    (a) => call("GET", "/tools/get_my_today_summary", a)),
  tool("get_my_metric_trend", "Get a trend for a metric (weight, steps, blood_pressure, sleep).",
    { type: "object", properties: { telegram_id: { type: "string" }, metric: { type: "string" }, period: { type: "string" } },
      required: ["telegram_id", "metric"] },
    (a) => call("POST", "/tools/get_my_metric_trend", a, { metric: a.metric, period: a.period || "30d" })),
  tool("create_manual_weight", "Log a manual weight in kg.",
    { type: "object", properties: { telegram_id: { type: "string" }, value: { type: "number" }, measured_at: { type: "string" } },
      required: ["telegram_id", "value"] },
    (a) => call("POST", "/tools/create_manual_weight", a, { value: a.value, measured_at: a.measured_at })),
  tool("create_manual_bp", "Log a manual blood pressure reading (systolic/diastolic mmHg, optional pulse).",
    { type: "object", properties: { telegram_id: { type: "string" }, systolic: { type: "integer" }, diastolic: { type: "integer" }, pulse: { type: "integer" }, measured_at: { type: "string" }, context: { type: "string" } },
      required: ["telegram_id", "systolic", "diastolic"] },
    (a) => call("POST", "/tools/create_manual_bp", a, { systolic: a.systolic, diastolic: a.diastolic, pulse: a.pulse, measured_at: a.measured_at, context: a.context })),
  tool("create_meal_from_text", "Log a meal from a free-text description. The Telegram message id is injected server-side from the current inbound message, so you do NOT need to supply telegram_message_id — any value you pass is ignored and replaced with the real inbound id. The chat id is pinned server-side to the deployment identity. Call this EXACTLY ONCE per user message — after it returns, continue with get_pending_meal and confirm_meal for the returned meal_id, and do NOT call it a second time for the same message. If a response reports duplicate_suppressed, that means this same message was already logged by your earlier call; confirm the meal from your first response and do NOT tell the user it was 'already logged'.",
    { type: "object", properties: { telegram_id: { type: "string" }, text: { type: "string" }, meal_type: { type: "string" }, eaten_at: { type: "string" }, telegram_message_id: { type: "string", description: "REQUIRED. The Telegram message id of the user message this log came from." } },
      required: ["telegram_id", "text"] },
    (a) => {
      // Phantom-meal guard: a meal write must carry the originating message id.
      // The chat id is pinned to the configured identity (never model-supplied);
      // the message id comes from the model. Together they feed the API
      // idempotency key, so a forged/repeated message id collides and the
      // duplicate write is rejected server-side.
      const inboundId = readInboundMessageId();
      const messageId = inboundId || a.telegram_message_id;
      if (!messageId) {
        throw new Error("create_meal_from_text requires telegram_message_id (the id of the user message this log came from)");
      }
      return call("POST", "/tools/create_meal_from_text", a, {
        text: a.text,
        meal_type: a.meal_type,
        eaten_at: a.eaten_at,
        telegram_chat_id: resolveTelegramId(a.telegram_id),
        telegram_message_id: String(messageId),
      });
    }),

  tool("get_pending_meal", "Get the parsed items of a needs_review meal (status=needs_review) so you can verify each match before confirming. Returns item_id for each item, which you pass to update_meal_item.",
    { type: "object", properties: { telegram_id: { type: "string" }, meal_id: { type: "string" } }, required: ["telegram_id", "meal_id"] },
    (a) => call("POST", "/tools/get_pending_meal", a, { meal_id: a.meal_id })),
  tool("update_meal_item", "Correct ONE item of ANY meal the user owns — a needs_review draft OR a meal that is already confirmed. There is NO status guard: a confirmed meal is patched in place and the parent meal totals are rewritten. Get item_id from get_pending_meal (draft) or list_my_meals (anything already logged), then pass the fields to fix (display_name, grams, unit, quantity, and/or *_per_100g values). The API re-resolves nutrition from the corrected name. Never tell the user a confirmed meal cannot be edited.",
    { type: "object", properties: { telegram_id: { type: "string" }, meal_id: { type: "string" }, item_id: { type: "string" }, display_name: { type: "string" }, grams: { type: "number" }, unit: { type: "string" }, quantity: { type: "number" } }, required: ["telegram_id", "meal_id", "item_id"] },
    (a) => call("POST", "/tools/update_meal_item", a, { meal_id: a.meal_id, item_id: a.item_id, patch: { display_name: a.display_name, grams: a.grams, unit: a.unit, quantity: a.quantity } })),
  tool("confirm_meal", "Confirm a needs_review meal once you have verified (and if needed corrected) all its items. This commits the meal to the log.",
    { type: "object", properties: { telegram_id: { type: "string" }, meal_id: { type: "string" } }, required: ["telegram_id", "meal_id"] },
    (a) => call("POST", "/tools/confirm_meal", a, { meal_id: a.meal_id })),
  tool("list_my_meals", "List the user's meals for one local day (default today), oldest first, with each meal's meal_id and every item's item_id plus grams/kcal. CALL THIS FIRST whenever the user wants to fix, change or delete something they ALREADY logged ('last night's dinner was 250 g not 300 g'). Never ask the user for a meal id and never send them to the dashboard — this tool gives you the ids. Pass date=YYYY-MM-DD for a past day; meal_type filters to one slot.",
    { type: "object", properties: { telegram_id: { type: "string" }, date: { type: "string", description: "YYYY-MM-DD in the user's timezone; omit for today" }, meal_type: { type: "string" } },
      required: ["telegram_id"] },
    (a) => call("POST", "/tools/list_meals", a, { date: a.date, meal_type: a.meal_type })),
  tool("update_meal", "Correct an EXISTING meal's DAY and/or MEAL TYPE (use when the user says a meal was filed on the wrong day, e.g. \"that was Sunday's dinner\"). Pass eaten_at (YYYY-MM-DD) and/or meal_type; this moves the whole meal. update_meal_item only edits one item's contents. Returns the new day/weekday/local time.",
    { type: "object", properties: { telegram_id: { type: "string" }, meal_id: { type: "string" }, eaten_at: { type: "string" }, meal_type: { type: "string" } }, required: ["telegram_id", "meal_id"] },
    (a) => call("POST", "/tools/update_meal", a, { meal_id: a.meal_id, eaten_at: a.eaten_at, meal_type: a.meal_type })),
  tool("list_my_reminders", "List the user's reminders.",
    { type: "object", properties: { telegram_id: { type: "string" } }, required: ["telegram_id"] },
    (a) => call("GET", "/tools/list_my_reminders", a)),
  tool("create_reminder", "Create a reminder. schedule_json: {\"days\":[\"mon\"],\"time\":\"08:00\"} or {\"cron\":\"0 8 * * 1\"}.",
    { type: "object", properties: { telegram_id: { type: "string" }, type: { type: "string" }, schedule_json: { type: "object" }, timezone: { type: "string" } },
      required: ["telegram_id", "type", "schedule_json"] },
    (a) => call("POST", "/tools/create_reminder", a, { type: a.type, schedule_json: a.schedule_json, timezone: a.timezone || "UTC" })),
  tool("set_user_goal", "Set a health goal for the user.",
    { type: "object", properties: { telegram_id: { type: "string" }, goal_type: { type: "string" }, target_json: { type: "object" }, period: { type: "string" } },
      required: ["telegram_id", "goal_type", "target_json"] },
    (a) => call("POST", "/tools/set_user_goal", a, { goal_type: a.goal_type, target_json: a.target_json, period: a.period })),
  tool("get_my_active_alerts", "List the user's active deterministic rule-engine alerts.",
    { type: "object", properties: { telegram_id: { type: "string" } }, required: ["telegram_id"] },
    (a) => call("GET", "/tools/get_my_active_alerts", a)),
  tool("issue_device_code", "Issue a one-time device code so the user can link the Android bridge app.",
    { type: "object", properties: { telegram_id: { type: "string" } }, required: ["telegram_id"] },
    (a) => call("POST", "/tools/issue_device_code", a, {})),
  tool("issue_web_login_link", "Mint a one-click dashboard login link for the user. Returns {url} the bot should DM to the user — tapping it opens the dashboard already logged in (no code entry).",
    { type: "object", properties: { telegram_id: { type: "string" } }, required: ["telegram_id"] },
    (a) => call("POST", "/tools/issue_web_login_link", a, {})),
    tool("undo_last_user_action", "Undo the user's last logged action.",
    { type: "object", properties: { telegram_id: { type: "string" } }, required: ["telegram_id"] },
    (a) => call("POST", "/tools/undo_last_user_action", a, {})),
  tool("search_food", "Search for foods by name. Returns ranked candidates with nutrition per 100g. Use this to verify and correct food matches before confirming a meal.",
    { type: "object", properties: { telegram_id: { type: "string" }, query: { type: "string" }, limit: { type: "integer" } }, required: ["telegram_id", "query"] },
    (a) => call("POST", "/tools/search_food", a, { query: a.query, limit: a.limit || 5 })),
  tool("set_my_timezone", "Set the user's timezone so all dates, meal days and reminders follow the user's DEVICE clock when they travel. Accepts an IANA name (\"Asia/Tokyo\") or a UTC offset (\"UTC+9\" / \"+09:00\"). Call this when the user says they are somewhere new (e.g. \"I'm in Tokyo now\"). Returns the resulting local date/time.",
    { type: "object", properties: { telegram_id: { type: "string" }, timezone: { type: "string" } }, required: ["telegram_id", "timezone"] },
    (a) => call("POST", "/tools/set_my_timezone", a, { timezone: a.timezone })),
  tool("log_activity", "Log a workout/physical activity the user DID, with calories BURNED (e.g. 'gardening, 850 kcal'). This is the ONLY way to record burned activity energy. NEVER file a workout as a meal — that adds burned kcal to the day's EATEN food total and corrupts the food ledger.",
    { type: "object", properties: { telegram_id: { type: "string" }, title: { type: "string" }, calories_burned: { type: "number" }, activity_date: { type: "string" }, description: { type: "string" } }, required: ["telegram_id", "title", "calories_burned"] },
    (a) => call("POST", "/tools/log_activity", a, { title: a.title, calories_burned: a.calories_burned, activity_date: a.activity_date, description: a.description })),
  tool("list_activities", "List the user's logged activities for a day (default today, user tz), with each id/title/calories_burned and the day's total burned kcal. Pass date=YYYY-MM-DD for another day.",
    { type: "object", properties: { telegram_id: { type: "string" }, date: { type: "string" } }, required: ["telegram_id"] },
    (a) => call("POST", "/tools/list_activities", a, { date: a.date })),
  tool("delete_activity", "Delete a logged activity by activity_id (from list_activities).",
    { type: "object", properties: { telegram_id: { type: "string" }, activity_id: { type: "string" } }, required: ["telegram_id", "activity_id"] },
    (a) => call("POST", "/tools/delete_activity", a, { activity_id: a.activity_id })),
  tool("list_mifitness_archives", "List Mi Fitness export ZIPs staged in the VPS inbox (name, size, mtime) plus whether the .zip_pw password file is present.",
    { type: "object", properties: { telegram_id: { type: "string" } }, required: ["telegram_id"] },
    (a) => call("POST", "/tools/list_mifitness_archives", a, {})),
  tool("import_mifitness_export", "Import a staged Mi Fitness export BY BARE FILENAME from the VPS inbox. DRY RUN FIRST (dry_run=true), show the user the report, then ask before a real import. Gap-filling only: never overwrites existing data. Runs in the background (~3 min) and returns a job_id; poll get_mifitness_import_status. The archive password is read from the sibling .zip_pw file — never send it in chat.",
    { type: "object", properties: { telegram_id: { type: "string" }, filename: { type: "string" }, dry_run: { type: "boolean" } }, required: ["telegram_id", "filename"] },
    (a) => call("POST", "/tools/import_mifitness_export", a, { filename: a.filename, dry_run: !!a.dry_run })),
  tool("get_mifitness_import_status", "Status/report of a Mi Fitness import job. Pass job_id from import_mifitness_export (omit for latest). wait_seconds (0-50) waits for completion in one call.",
    { type: "object", properties: { telegram_id: { type: "string" }, job_id: { type: "string" }, wait_seconds: { type: "integer" } }, required: ["telegram_id"] },
    (a) => call("POST", "/tools/get_mifitness_import_status", a, { job_id: a.job_id, wait_seconds: a.wait_seconds || 0 })),
  tool("ingest_medical_batch", "DETERMINISTIC batch staging — call this FIRST whenever the user sent medical photos (even before transcribing). It uploads ALL pending photos as ONE encounter + ONE record (placeholder content, all scans bound) and returns encounter_id/record_id. Storage is then GUARANTEED: enrich that record with update_medical_record — do NOT create separate records. Optional: encounter_date (YYYY-MM-DD), facility, title, notes, caption.",
    { type: "object", properties: { telegram_id: { type: "string" }, encounter_date: { type: "string" }, facility: { type: "string" }, title: { type: "string" }, notes: { type: "string" }, caption: { type: "string" } }, required: ["telegram_id"] },
    async (a) => ingestPendingBatch(a, a)),
  tool("get_staged_medical_batch", "Return the currently staged medical photo batch (encounter_id/record_id/photo_count) created deterministically by the gateway/bridge, or null if none. Call this to learn the record you must enrich with update_medical_record instead of creating new records.",
    { type: "object", properties: { telegram_id: { type: "string" } }, required: ["telegram_id"] },
    async (a) => {
      // If photos are still pending, stage them first so this never returns null while photos wait.
      await ensureBatchIngested("get_staged_medical_batch");
      const stage = readBatchStage();
      return { ok: true, data: stage || null };
    }),
  tool("update_medical_record", "Enrich an EXISTING medical record in place: title, content (the faithful transcription), recorded_at, and/or encounter_id (re-group). Use this to enrich the record staged by ingest_medical_batch — never create a second record for the same batch. At least one field is required. merge_record_ids: optional list of same-burst placeholder record ids whose scans should fold into this canonical record (completion pass — single canonical record per burst). Surgery encounters: recorded_at is forced to the surgery day (encounter_date) automatically.",
    { type: "object", properties: { telegram_id: { type: "string" }, record_id: { type: "string" }, title: { type: "string" }, content: { type: "string" }, recorded_at: { type: "string" }, encounter_id: { type: "string" }, merge_record_ids: { type: "array", items: { type: "string" } } }, required: ["telegram_id", "record_id"] },
    (a) => call("POST", "/tools/update_medical_record", a, { record_id: a.record_id, title: a.title, content: a.content, recorded_at: a.recorded_at, encounter_id: a.encounter_id, merge_record_ids: a.merge_record_ids })),
  tool("add_medical_record", "Store a medical document the user photographed (lab report, prescription, letter). ONE record per batch: if the user sent several photos as one batch (an album, or 'these are one report'), store ONE record whose content covers the whole batch — NEVER one record per photo. The user's explicit instruction wins: if they say it is one batch, log one record. First transcribe the photo FAITHFULLY — original language, exact numbers/units/dates, no summarizing and no translation — then SHOW the transcription to the user and get their confirmation BEFORE calling this. content = the confirmed transcription; title = optional short label (e.g. 'Lab blood panel'); recorded_at = optional ISO date/datetime the document is dated; encounter_id = optional encounter id from list_medical_encounters to group this document under a visit; attach_latest_media = set true to store the photo the user just sent as the record's source scan (the bridge relays the image bytes server-side). If a batch was already staged by ingest_medical_batch, enrich THAT record with update_medical_record instead of calling this.",
    { type: "object", properties: { telegram_id: { type: "string" }, title: { type: "string" }, content: { type: "string" }, recorded_at: { type: "string" }, encounter_id: { type: "string" }, attach_latest_media: { type: "boolean" } }, required: ["telegram_id", "content"] },
    async (a) => {
      const r = await call("POST", "/tools/add_medical_record", a, { title: a.title, content: a.content, recorded_at: a.recorded_at, encounter_id: a.encounter_id });
      if (!r || r.ok !== true || !a.attach_latest_media) return r;
      const recordId = r && r.data && r.data.id;
      const media = takeLatestInboundMedia();
      if (!media) return { ...r, data: { ...(r.data || {}), scan: null }, message: (r.message || "") + " No recent inbound photo found to attach as scan." };
      const up = await uploadScan(a, media.path, recordId);
      if (!up || up.ok !== true) return { ...r, data: { ...(r.data || {}), scan: { error: (up && up.error) || "upload failed" } } };
      return { ...r, data: { ...(r.data || {}), scan: up.data } };
    }),
  tool("add_medical_encounter", "Create a medical encounter (a visit, lab draw or prescription pickup) that groups related medical records. For a photo batch, create the encounter FIRST, then store the batch as ONE record under it (grouped multi-photo batches are one record, not one per photo). encounter_date = YYYY-MM-DD of the visit; facility = optional clinic/hospital name; title = optional short label; notes = optional free text. Propose facility + date from the documents and CONFIRM with the user before creating. Then store the batch with add_medical_record + the returned encounter_id — or enrich the already-staged batch record with update_medical_record (encounter_id) instead of creating duplicates.",
    { type: "object", properties: { telegram_id: { type: "string" }, encounter_date: { type: "string" }, facility: { type: "string" }, title: { type: "string" }, notes: { type: "string" } }, required: ["telegram_id", "encounter_date"] },
    (a) => call("POST", "/tools/add_medical_encounter", a, { encounter_date: a.encounter_date, facility: a.facility, title: a.title, notes: a.notes })),
  tool("list_medical_encounters", "List the user's medical encounters, newest first, each with its record_count. Use the returned id with get_medical_encounter (details + its records) or delete_medical_encounter.",
    { type: "object", properties: { telegram_id: { type: "string" }, limit: { type: "integer" } }, required: ["telegram_id"] },
    (a) => call("POST", "/tools/list_medical_encounters", a, { limit: a.limit || 20 })),
  tool("get_medical_encounter", "Return one medical encounter and every medical record stored inside it (full transcriptions). encounter_id comes from list_medical_encounters.",
    { type: "object", properties: { telegram_id: { type: "string" }, encounter_id: { type: "string" } }, required: ["telegram_id", "encounter_id"] },
    (a) => call("POST", "/tools/get_medical_encounter", a, { encounter_id: a.encounter_id })),
  tool("delete_medical_encounter", "Delete a medical encounter AND ALL records stored inside it (deletes the documents too). Confirm with the user before calling — this is destructive.",
    { type: "object", properties: { telegram_id: { type: "string" }, encounter_id: { type: "string" } }, required: ["telegram_id", "encounter_id"] },
    (a) => call("POST", "/tools/delete_medical_encounter", a, { encounter_id: a.encounter_id })),
  tool("list_medical_records", "List the user's stored medical records (transcriptions of lab reports, prescriptions, letters), newest first. Pass query to search a fragment across title AND content (a lab name, a medicine, a doctor). Use the returned id with delete_medical_record.",
    { type: "object", properties: { telegram_id: { type: "string" }, query: { type: "string" }, limit: { type: "integer" } }, required: ["telegram_id"] },
    (a) => call("POST", "/tools/list_medical_records", a, { query: a.query, limit: a.limit || 20 })),
  tool("delete_medical_record", "Delete a stored medical record by record_id (from list_medical_records). Confirm with the user before deleting.",
    { type: "object", properties: { telegram_id: { type: "string" }, record_id: { type: "string" } }, required: ["telegram_id", "record_id"] },
    (a) => call("POST", "/tools/delete_medical_record", a, { record_id: a.record_id })),
];

/**
 * Resolve the Telegram identity to send to the API.
 *
 * Security rule (Qodo #10): the bridge MUST NOT forward a model-supplied
 * telegram_id verbatim. It uses the deployment's configured identity
 * (DRHIRO_TELEGRAM_ID) and ignores any divergent value the model provides.
 * When no identity is configured, calls fail closed (no impersonation risk).
 *
 * Single-tenant identity model (Qodo #5):
 *   Each drHiro gateway deploys for ONE paired Telegram identity
 *   (DRHIRO_TELEGRAM_ID). This is intentional: the gateway is a personal,
 *   single-user surface — one gateway instance serves exactly one paired
 *   Telegram user. The bridge enforces this by design: it always uses the
 *   configured identity and silently discards any divergent id the model
 *   provides.
 *
 *   Supporting multiple users on a single gateway would require per-session
 *   bound identities (a separate identity-resolution path that binds each
 *   verified chat/session to its own stored identity), which is out of scope
 *   for the single-tenant deployment model. To serve multiple users, deploy
 *   separate gateway instances, each with its own DRHIRO_TELEGRAM_ID.
 *
 *   DO NOT weaken this gate to recover a "missing" id from the model — that
 *   would let a confused or manipulated agent act as a different paired user.
 */
function resolveTelegramId(modelSupplied) {
  if (!CONFIGURED_TELEGRAM_ID) return null;
  // Ignore any model-supplied value that diverges from the configured identity.
  if (modelSupplied && modelSupplied !== CONFIGURED_TELEGRAM_ID) return CONFIGURED_TELEGRAM_ID;
  return CONFIGURED_TELEGRAM_ID;
}

async function call(method, path, args, body) {
  if (!SERVICE_TOKEN) return { ok: false, error: "DRHIRO_OPENCLAW_SERVICE_TOKEN not set" };
  const telegramId = resolveTelegramId(args && args.telegram_id);
  if (!telegramId) return { ok: false, error: "No configured Telegram identity (DRHIRO_TELEGRAM_ID not set)" };
  try {
    const res = await fetch(API_BASE + path, {
      method,
      headers: {
        "X-Service-Token": SERVICE_TOKEN,
        "X-Telegram-Id": telegramId,
        "Content-Type": "application/json",
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    if (!res.ok) return { ok: false, error: `API ${res.status}: ${text.slice(0, 300)}` };
    try { return JSON.parse(text); } catch { return { ok: true, raw: text }; }
  } catch (e) {
    return { ok: false, error: String(e && e.message || e) };
  }
}

// ---- MCP JSON-RPC over stdio ----
const rl = readline.createInterface({ input: process.stdin, terminal: false });

function respond(id, result) {
  process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id, result }) + "\n");
}
function respondError(id, code, message) {
  process.stdout.write(JSON.stringify({ jsonrpc: "2.0", id, error: { code, message } }) + "\n");
}

rl.on("line", async (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }

  if (msg.method === "initialize") {
    respond(msg.id, {
      protocolVersion: "2024-11-05",
      capabilities: { tools: { listChanged: false } },
      serverInfo: { name: "drhiro", version: "0.1.0" },
    });
    return;
  }
  if (msg.method === "notifications/initialized") return;
  if (msg.method === "tools/list") {
    respond(msg.id, { tools: TOOLS.map(t => ({ name: t.name, description: t.description, inputSchema: t.inputSchema })) });
    return;
  }
  if (msg.method === "tools/call") {
    const name = msg.params && msg.params.name;
    const args = (msg.params && msg.params.arguments) || {};
    const t = TOOLS.find(x => x.name === name);
    if (!t) { respondError(msg.id, -32602, `Unknown tool: ${name}`); return; }
    try {
      // Structural guarantee: stage any pending medical photos BEFORE the
      // requested tool runs, so a record provably exists independent of the
      // model choosing a storage tool. Never blocks the real call on failure.
      const staged = await ensureBatchIngested(name);
      const result = await t.handler(args);
      const out = (staged && result && typeof result === "object" && !Array.isArray(result))
        ? { ...result, staged_batch: staged }
        : result;
      respond(msg.id, { content: [{ type: "text", text: JSON.stringify(out) }] });
    } catch (e) {
      respondError(msg.id, -32603, String(e && e.message || e));
    }
    return;
  }
  if (msg.method === "ping") { respond(msg.id, {}); return; }
  respondError(msg.id, -32601, `Unknown method: ${msg.method}`);
});

process.stdout.write("");
