"""T1 — MCP consumption-writer gate (the DECISIVE model-side control).

The model reaches /ingest and /meals with a USER JWT, which an API header gate
cannot distinguish from a real user. Therefore the decisive writer-ownership
control is at the MCP tool layer: when the trusted ingress owns consumption, the
model's consumption-writing tools are disabled and return model_writer_disabled.

This drives the REAL MCP server's request handler with DRHIRO_TRUSTED_INGRESS_WRITERS_DISABLED=1
via a subprocess, so it exercises the actual tool-call dispatch.

Run: python -m pytest tests/test_t1_mcp_writer_gate.py -q
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

MCP_DIR = Path(__file__).resolve().parent.parent / "packages" / "drhiro-mcp" / "src"
# The MCP server hard-codes its port to 3100 (uvicorn.run(port=3100)); probe it
# there. This is safe in this environment (checked free).
PORT = 3100
MCP_TEST_TOKEN = "pytest-mcp-test-token-do-not-use-in-prod"


@pytest.fixture(scope="module")
def mcp_proc():
    env = dict(os.environ)
    env["DRHIRO_API_URL"] = "http://localhost:8099/api/v1"  # unreachable
    env["DRHIRO_TRUSTED_INGRESS_WRITERS_DISABLED"] = "true"
    env["PORT"] = str(PORT)
    env["PYTHONPATH"] = str(MCP_DIR)
    env["DRHIRO_MCP_SERVER_TOKEN"] = MCP_TEST_TOKEN
    p = subprocess.Popen(
        [sys.executable, str(MCP_DIR / "drhiro_mcp" / "sse_server.py")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    base = f"http://localhost:{PORT}"
    # The MCP endpoint is POST /mcp; /healthz is the readiness probe.
    ok = False
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/healthz", timeout=2).status_code == 200:
                ok = True
                break
        except Exception:
            time.sleep(0.4)
    if not ok:
        p.kill()
        pytest.skip("MCP server did not start")
    yield f"{base}/mcp"
    p.send_signal(signal.SIGTERM)
    try:
        p.wait(timeout=5)
    except Exception:
        p.kill()


def _call(base, name, args=None, session=None):
    headers = {"X-MCP-Token": MCP_TEST_TOKEN}
    if session:
        headers["mcp-session-id"] = session
    r = httpx.post(
        base,
        json={"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "tools/call",
              "params": {"name": name, "arguments": args or {}}},
        headers=headers, timeout=10,
    )
    return r


CONSUMPTION_TOOLS = [
    "log_meal", "log_meal_intelligent", "confirm_intelligent_meal",
    "log_water", "log_liquid", "log_recipe_meal", "build_recipe",
    "delete_meal", "correct_meal_item",
]


class TestMCPWriterGate:
    @pytest.mark.parametrize("tool", CONSUMPTION_TOOLS)
    def test_consumption_tools_are_disabled(self, mcp_proc, tool):
        r = _call(mcp_proc, tool, {"text": "chicken 200g", "amount_ml": 250})
        assert r.status_code == 200
        body = r.json()
        result = body.get("result", {})
        assert result.get("ok") is False
        assert "model_writer_disabled" in result.get("error", "")
        assert tool in result.get("error", "")

    def test_non_consumption_tool_still_dispatches(self, mcp_proc):
        # get_steps is not a consumption writer; it should dispatch (and fail
        # against the unreachable API, but NOT with model_writer_disabled).
        r = _call(mcp_proc, "get_steps", {"days": 7})
        body = r.json()
        result = body.get("result", {})
        # Not the model-writer-disabled sentinel.
        assert "model_writer_disabled" not in str(result)


class TestMCPAuthentication:
    """Qodo #15 — caller authentication gate."""

    def test_unauthenticated_request_is_401(self, mcp_proc):
        """tools/call without a token must be rejected with 401."""
        r = httpx.post(
            mcp_proc,
            json={"jsonrpc": "2.0", "id": "1", "method": "tools/call",
                  "params": {"name": "get_steps", "arguments": {}}},
            timeout=10,
        )
        assert r.status_code == 401, r.text

    def test_invalid_token_is_401(self, mcp_proc):
        """tools/call with a WRONG token must be rejected with 401."""
        r = httpx.post(
            mcp_proc,
            json={"jsonrpc": "2.0", "id": "1", "method": "tools/call",
                  "params": {"name": "get_steps", "arguments": {}}},
            headers={"X-MCP-Token": "wrong-token"},
            timeout=10,
        )
        assert r.status_code == 401, r.text

    def test_bearer_token_auth_works(self, mcp_proc):
        """Authorization: Bearer <token> must be accepted."""
        r = httpx.post(
            mcp_proc,
            json={"jsonrpc": "2.0", "id": "1", "method": "tools/list",
                  "params": {}},
            headers={"Authorization": f"Bearer {MCP_TEST_TOKEN}"},
            timeout=10,
        )
        assert r.status_code == 200, r.text

    def test_initialize_still_works_without_token(self, mcp_proc):
        """The initialize handshake is exempt from auth so clients can discover."""
        r = httpx.post(
            mcp_proc,
            json={"jsonrpc": "2.0", "id": "1", "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "1.0.0"}}},
            timeout=10,
        )
        # initialize is exempt — should succeed (200) even without auth
        assert r.status_code == 200, r.text

    def test_get_endpoint_requires_auth(self, mcp_proc):
        """GET /mcp must also require auth (Qodo #15)."""
        base = mcp_proc.rsplit("/mcp", 1)[0]
        r = httpx.get(f"{base}/mcp", timeout=10)
        assert r.status_code == 401, r.text

        r = httpx.get(f"{base}/mcp", headers={"X-MCP-Token": MCP_TEST_TOKEN}, timeout=10)
        assert r.status_code == 200, r.text

    def test_healthz_remains_open(self, mcp_proc):
        """The /healthz liveness probe must remain reachable without auth."""
        base = mcp_proc.rsplit("/mcp", 1)[0]
        r = httpx.get(f"{base}/healthz", timeout=10)
        assert r.status_code == 200, r.text
