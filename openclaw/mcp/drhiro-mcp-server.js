#!/usr/bin/env node
/**
 * drHiro MCP server (dependency-free Node implementation).
 *
 * Speaks MCP (JSON-RPC over stdio) with zero npm dependencies: node has
 * fetch and readline built in. OpenClaw launches this via the mcpServers
 * config; the agent sees the real drHiro tool schemas and calls them.
 *
 * Tools forward to the drHiro Core API with the signed service token and
 * the sender's Telegram id (supplied by OpenClaw as telegram_id).
 */

"use strict";

const fs = require("fs");
const readline = require("readline");

const API_BASE = process.env.DRHIRO_MCP_API || "http://api:8000/api/v1";
const SERVICE_TOKEN = process.env.DRHIRO_OPENCLAW_SERVICE_TOKEN || "";

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
  tool("create_meal_from_text", "Log a meal from a free-text description.",
    { type: "object", properties: { telegram_id: { type: "string" }, text: { type: "string" }, meal_type: { type: "string" }, eaten_at: { type: "string" } },
      required: ["telegram_id", "text"] },
    (a) => call("POST", "/tools/create_meal_from_text", a, { text: a.text, meal_type: a.meal_type, eaten_at: a.eaten_at })),
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
];

async function call(method, path, args, body) {
  if (!SERVICE_TOKEN) return { ok: false, error: "DRHIRO_OPENCLAW_SERVICE_TOKEN not set" };
  const telegramId = args && args.telegram_id;
  try {
    const res = await fetch(API_BASE + path, {
      method,
      headers: {
        "X-Service-Token": SERVICE_TOKEN,
        "X-Telegram-Id": telegramId || "",
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
      const result = await t.handler(args);
      respond(msg.id, { content: [{ type: "text", text: JSON.stringify(result) }] });
    } catch (e) {
      respondError(msg.id, -32603, String(e && e.message || e));
    }
    return;
  }
  if (msg.method === "ping") { respond(msg.id, {}); return; }
  respondError(msg.id, -32601, `Unknown method: ${msg.method}`);
});

process.stdout.write("");
