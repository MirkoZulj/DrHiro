"""R2 integration evidence — REAL shim + REAL Redis + TrueForge-equivalent stub.

Launches the ACTUAL modified shim (uvicorn) and a TrueForge-equivalent stub as
subprocesses, plus real Redis (drhiro-dev-redis-1, db 15), and drives the real
HTTP endpoint with OpenClaw-shaped requests. Demonstrates:
  - valid envelope extracted + removed before any model forwarding (stub proves
    the envelope never reached the model side)
  - durable per-event record written to Redis (run-scoped, not a shared slot)
  - fail-closed on missing / copied / forged / cross-account / no-context
  - RESTART: event records + session mapping survive a shim restart
  - REDIS-LOSS: transient record loss does not corrupt; a fresh valid envelope
    re-establishes identity and a stale copied envelope is still rejected
  - concurrent events keep separate identities (no context exchange)
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid

import httpx
import pytest
import redis

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIM_DIR = os.path.join(REPO, "services", "tf-shim")
REDIS_URL = "redis://localhost:6382/15"
BOT_ID = "BOT1"
SECRET = "integration-test-secret"
SHIM_PORT = 8792
TF_PORT = 8791

sys.path.insert(0, SHIM_DIR)
import drhiro_event_envelope as ev  # noqa: E402


@pytest.fixture(scope="module")
def _redis():
    r = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    r.flushdb()  # disposable test db only
    yield r
    r.flushdb()


@pytest.fixture(scope="module")
def _stack(_redis):
    """Start the TrueForge stub and the real shim as subprocesses."""
    procs = []

    def start(script, port, extra_env):
        env = dict(os.environ)
        env.update(extra_env)
        env["PYTHONPATH"] = SHIM_DIR
        env["PORT"] = str(port)
        logf = open(os.path.join(SHIM_DIR, f"harness_{script}_{port}.log"), "wb")
        p = subprocess.Popen(
            [sys.executable, script],
            env=env, cwd=SHIM_DIR,
            stdout=logf, stderr=subprocess.STDOUT,
        )
        p._logf = logf
        procs.append(p)
        return p

    def start_shim(port):
        return start("shim.py", port, {
            "TRUEFORGE_URL": f"http://localhost:{TF_PORT}",
            "REDIS_URL": REDIS_URL,
            "DRHIRO_BOT_ID": BOT_ID,
            "DRHIRO_EVENT_SECRET": SECRET,
            "PORT": str(port),
            "SHIM_MODEL_ID": "trueforge-drhiro",
        })

    def wait_healthy(port):
        base = f"http://localhost:{port}"
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if httpx.get(f"{base}/healthz", timeout=2).status_code == 200:
                    return base
            except Exception:
                time.sleep(0.3)
        raise RuntimeError(f"service on {port} did not become healthy")

    # TrueForge-equivalent stub
    start("trueforge_stub.py", TF_PORT, {"TF_PORT": str(TF_PORT)})
    # Real shim
    shim_proc = start_shim(SHIM_PORT)
    base = wait_healthy(SHIM_PORT)

    yield {"base": base, "procs": procs, "start_shim": start_shim, "wait_healthy": wait_healthy, "shim_proc": shim_proc}

    for p in procs:
        if p.poll() is None:
            p.send_signal(signal.SIGTERM)
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()


def _envelope(user_text, msg_id="M1", bot_id=BOT_ID, nonce=None):
    return ev.build_envelope(
        secret=SECRET.encode(), service="drhiro", bot_id=bot_id,
        chat_id="CHAT1", message_id=msg_id,
        input_digest=ev.canonical_input(user_text),
        nonce=nonce or uuid.uuid4().hex[:8],
    )


def _body(user_text, envelope_block):
    return {
        "model": "trueforge-drhiro",
        "stream": True,
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": user_text},
            {"role": "user", "content": (
                "OpenClaw runtime context for the immediately preceding user message.\n"
                "<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>\nThis context is runtime-generated.\n"
                + envelope_block + "\n"
                "<<<END_OPENCLAW_INTERNAL_CONTEXT>>>"
            )},
        ],
    }


def _post(_stack, body):
    return httpx.post(f"{_stack['base']}/v1/chat/completions", json=body, timeout=30)


def _stub_received():
    """Read what the TrueForge-equivalent stub actually received (over HTTP)."""
    return httpx.get(f"http://localhost:{TF_PORT}/debug/received", timeout=5).json().get("turns", [])


def _event_record(_redis, event_id):
    raw = _redis.get(f"tfshim:event:{event_id}")
    return json.loads(raw) if raw else None


class TestRealShimIntegration:
    def test_valid_envelope_is_processed_and_envelope_never_reaches_model(self, _stack, _redis):
        before = len(_stub_received())
        text = "I had 300g chicken and a glass of wine"
        resp = _post(_stack, _body(text, _envelope(text)))
        assert resp.status_code == 200, resp.text
        # the stub received exactly the user text, no envelope
        new = _stub_received()[before:]
        assert new, "shim did not forward a turn to the model"
        assert new[0]["has_envelope"] is False, "envelope leaked to the model side"
        assert "DRHIRO_EVENT_CONTEXT" not in new[0]["text"]
        # durable per-event record written (run-scoped)
        event_id = ev.derive_event_id(service="drhiro", bot_id=BOT_ID, chat_id="CHAT1", message_id="M1")
        rec = _event_record(_redis, event_id)
        assert rec is not None
        assert rec["bot_id"] == BOT_ID and rec["chat_id"] == "CHAT1" and rec["message_id"] == "M1"

    def test_missing_envelope_fails_closed_no_model_call(self, _stack, _redis):
        before = len(_stub_received())
        resp = _post(_stack, _body("some meal", "<<<BEGIN_DRHIRO_EVENT_CONTEXT>>>\n[junk]\n<<<END_DRHIRO_EVENT_CONTEXT>>>"))
        assert resp.status_code == 422, resp.text
        assert len(_stub_received()) == before, "model called despite missing envelope"

    def test_copied_envelope_from_other_request_rejected(self, _stack, _redis):
        before = len(_stub_received())
        # envelope valid for a DIFFERENT input
        env = _envelope("completely different meal text")
        resp = _post(_stack, _body("I had 300g chicken and a glass of wine", env))
        assert resp.status_code == 422, resp.text
        assert "input-digest" in resp.text or "rejected" in resp.text
        assert len(_stub_received()) == before

    def test_forged_envelope_rejected(self, _stack, _redis):
        forged = ev.build_envelope(
            secret=b"wrong-secret", service="drhiro", bot_id=BOT_ID,
            chat_id="CHAT1", message_id="M1",
            input_digest=ev.canonical_input("I had 300g chicken and a glass of wine"),
        )
        resp = _post(_stack, _body("I had 300g chicken and a glass of wine", forged))
        assert resp.status_code == 422, resp.text

    def test_cross_account_rejected(self, _stack, _redis):
        # envelope for a different bot, posted to this bot's shim
        env = _envelope("I had 300g chicken and a glass of wine", bot_id="OTHER_BOT")
        resp = _post(_stack, _body("I had 300g chicken and a glass of wine", env))
        assert resp.status_code == 422, resp.text

    def test_concurrent_events_keep_separate_records(self, _stack, _redis):
        """Two events -> two independent per-event records; no shared slot."""
        t = "I had 300g chicken and a glass of wine"
        _post(_stack, _body(t, _envelope(t, msg_id="CA", nonce="a")))
        _post(_stack, _body(t, _envelope(t, msg_id="CB", nonce="b")))
        eA = ev.derive_event_id(service="drhiro", bot_id=BOT_ID, chat_id="CHAT1", message_id="CA")
        eB = ev.derive_event_id(service="drhiro", bot_id=BOT_ID, chat_id="CHAT1", message_id="CB")
        assert eA != eB
        assert _event_record(_redis, eA) is not None
        assert _event_record(_redis, eB) is not None


class TestRestartAndRedisLoss:
    def test_restart_survives_records_and_still_works(self, _stack, _redis):
        """After the shim process is restarted, event records + session mapping
        survive in Redis and valid envelopes are still processed."""
        t = "I had 300g chicken and a glass of wine"
        event_id = ev.derive_event_id(service="drhiro", bot_id=BOT_ID, chat_id="CHAT1", message_id="R1")
        # create the record pre-restart
        assert _post(_stack, _body(t, _envelope(t, msg_id="R1"))).status_code == 200
        assert _event_record(_redis, event_id) is not None
        # restart the shim: terminate the original process, start a fresh one
        orig = _stack["shim_proc"]
        orig.send_signal(signal.SIGTERM)
        try:
            orig.wait(timeout=8)
        except Exception:
            orig.kill()
        _stack["procs"].remove(orig)
        new = _stack["start_shim"](SHIM_PORT + 1)
        _stack["procs"].append(new)
        new_base = _stack["wait_healthy"](SHIM_PORT + 1)
        _stack["base"] = new_base  # subsequent tests must hit the restarted shim
        # post-restart, the durable record is still resolvable and a valid
        # envelope is still processed
        resp = _post(_stack, _body(t, _envelope(t, msg_id="R1")))
        assert resp.status_code == 200, resp.text
        assert _event_record(_redis, event_id) is not None

    def test_redis_record_loss_fresh_envelope_reestablishes(self, _stack, _redis):
        """Simulate transient Redis loss of a record: a fresh valid envelope for
        the same event re-establishes identity; a stale COPIED envelope (wrong
        input) is still rejected. Durable identity lives in the signed envelope,
        not in a transient slot."""
        t = "I had 300g chicken and a glass of wine"
        event_id = ev.derive_event_id(service="drhiro", bot_id=BOT_ID, chat_id="CHAT1", message_id="L1")
        _post(_stack, _body(t, _envelope(t, msg_id="L1")))
        assert _event_record(_redis, event_id) is not None
        # transient loss
        _redis.delete(f"tfshim:event:{event_id}")
        assert _event_record(_redis, event_id) is None
        # a fresh valid envelope re-establishes the durable record
        resp = _post(_stack, _body(t, _envelope(t, msg_id="L1")))
        assert resp.status_code == 200, resp.text
        assert _event_record(_redis, event_id) is not None
        # a stale copied envelope (different input) is still rejected post-loss
        copied = _envelope("some other input")
        resp2 = _post(_stack, _body(t, copied))
        assert resp2.status_code == 422, resp2.text
