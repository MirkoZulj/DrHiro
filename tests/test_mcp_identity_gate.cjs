#!/usr/bin/env node
/**
 * Test for Qodo #10: the MCP bridge must ignore a model-supplied telegram_id
 * that differs from the configured DRHIRO_TELEGRAM_ID.
 *
 * Strategy: spin up a tiny HTTP server that captures request headers, point
 * the bridge at it with DRHIRO_TELEGRAM_ID=123, then send a tools/call with
 * telegram_id=999 (impersonation attempt). The bridge MUST forward X-Telegram-Id: 123.
 */

"use strict";

const http = require("http");
const { spawn } = require("child_process");
const path = require("path");
const assert = require("assert");

const MCP_PATH = path.join(__dirname, "..", "openclaw", "mcp", "drhiro-mcp-server.js");

function makeServer(capture) {
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      capture.headers = req.headers;
      res.statusCode = 200;
      res.end(JSON.stringify({ ok: true }));
    });
    srv.listen(0, "127.0.0.1", () => resolve(srv));
  });
}

function sendRpc(proc, msg) {
  return new Promise((resolve, reject) => {
    const line = JSON.stringify(msg) + "\n";
    let buf = "";
    const onData = (chunk) => {
      buf += chunk.toString();
      const idx = buf.indexOf("\n");
      if (idx !== -1) {
        proc.stdout.off("data", onData);
        try {
          resolve(JSON.parse(buf.slice(0, idx)));
        } catch (e) {
          reject(e);
        }
      }
    };
    proc.stdout.on("data", onData);
    proc.stdin.write(line);
  });
}

async function run() {
  const capture = {};
  const srv = await makeServer(capture);
  const { port } = srv.address();

  // Configured identity is 123; the model will try to supply 999.
  const child = spawn(process.execPath, [MCP_PATH], {
    env: {
      ...process.env,
      DRHIRO_MCP_API: `http://127.0.0.1:${port}/api/v1`,
      DRHIRO_OPENCLAW_SERVICE_TOKEN: "test-svc-token",
      DRHIRO_TELEGRAM_ID: "123",
    },
    stdio: ["pipe", "pipe", "inherit"],
  });

  try {
    // initialize handshake
    await sendRpc(child, {
      jsonrpc: "2.0",
      id: "init",
      method: "initialize",
      params: {
        protocolVersion: "2024-11-05",
        capabilities: {},
        clientInfo: { name: "test", version: "1.0.0" },
      },
    });

    // tools/call with impersonation attempt (telegram_id=999)
    const resp = await sendRpc(child, {
      jsonrpc: "2.0",
      id: "call1",
      method: "tools/call",
      params: {
        name: "get_my_today_summary",
        arguments: { telegram_id: "999" },
      },
    });

    assert.ok(resp.result, `expected result, got error: ${JSON.stringify(resp.error)}`);
    const content = resp.result.content && resp.result.content[0];
    assert.ok(content, "expected content in response");
    const body = JSON.parse(content.text);
    assert.ok(body.ok, `API call should have succeeded, got: ${content.text}`);

    // The decisive assertion: the bridge must send the CONFIGURED identity,
    // not the model-supplied one.
    assert.strictEqual(
      capture.headers["x-telegram-id"],
      "123",
      `Bridge forwarded model-supplied telegram_id instead of configured one. ` +
      `x-telegram-id=${capture.headers["x-telegram-id"]}`
    );
    assert.strictEqual(capture.headers["x-service-token"], "test-svc-token");

    console.log("PASS: bridge forwards configured telegram_id, ignores model-supplied 999");
  } finally {
    child.kill();
    srv.close();
  }
}

run().catch((e) => {
  console.error("FAIL:", e.message);
  process.exit(1);
});
