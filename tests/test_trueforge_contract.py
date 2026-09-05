"""Contract probe — the test that would have caught D7.

Asserts that EVERY endpoint the shipped integration calls (configure.sh and
telegram-bridge) actually exists and responds on the TrueForge deployment it
is pointed at. This is the gate that failed the validation window: install.sh
pinned TrueForge v0.1.4, whose API is /v1/* — but the shipped code targets
/api/v1/* (the contract production runs, present in v0.1.9+).

Run modes:
  - Against a LIVE deployment:  TRUEFORGE_URL=http://host:8790 pytest tests/test_trueforge_contract.py
  - Against the offline mock:   (default) starts mock_trueforge in-process.

A 200 (or a defined 4xx that proves routing, e.g. 404 on a bogus session id
with the /turns route present) is a PASS. A bare 404 on a route the code
calls is a FAIL — it means the pinned TrueForge version exposes a different
API surface.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from tests.mock_trueforge import DEFAULT_PORT, make_server

# Endpoints the shipped integration calls (configure.sh + telegram-bridge).
# Each is a (method, path, expected_statuses). The bridge POSTs to /turns with
# a real session; here we use the mock's own session flow.
CONTRACT = [
    ("GET", "/healthz", {200}),
    ("GET", "/api/v1/agents", {200}),
    ("POST", "/api/v1/sessions", {200}),
    ("POST", "/api/v1/settings/model-providers", {200}),
    ("POST", "/api/v1/settings/mcp-servers", {200}),
]


@pytest.fixture(scope="module")
def base_url():
    live = os.environ.get("TRUEFORGE_URL")
    if live:
        yield live.rstrip("/")
        return
    # Offline mock.
    server, state = make_server(port=DEFAULT_PORT)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{DEFAULT_PORT}"
    server.shutdown()


def _status(method, url):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


@pytest.mark.parametrize("method,path,expected", CONTRACT)
def test_contract_endpoint(base_url, method, path, expected):
    """Every endpoint the shipped code calls must exist and respond."""
    status = _status(method, f"{base_url}{path}")
    assert status in expected, (
        f"{method} {path} -> {status}; expected one of {expected}. "
        "If this is a bare 404 the pinned TrueForge exposes a different API "
        "surface (D7)."
    )


def test_bridge_session_turn_flow(base_url):
    """The full bridge flow: create session -> run turn -> assistant reply."""
    # create session
    body = '{"agent": {"name": "drhiro"}}'
    req = urllib.request.Request(
        f"{base_url}/api/v1/sessions", data=body.encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        created = json.loads(r.read())
    sid = created["data"]["id"]
    # run a turn
    turn = '{"input": [{"type": "user.message", "content": "hello"}]}'
    req2 = urllib.request.Request(
        f"{base_url}/api/v1/sessions/{sid}/turns", data=turn.encode(), method="POST",
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
    with urllib.request.urlopen(req2, timeout=10) as r:
        stream = r.read().decode()
    assert "model.message.delta" in stream or "reply" in stream.lower(), (
        "turn did not stream an assistant reply (contract or version mismatch)"
    )
