"""Free-text logging must not double-write on a repeated identical tool call.

Regression for the 2026-10-02 production incident: the OpenClaw agent called
POST /tools/create_meal_from_text four times for one 26-character Telegram
message. The MCP bridge forwards no per-message Telegram identity, so each call
wrote ``anon-<uuid>`` rows and nothing could recognise the repeat: 750 ml became
3000 ml.

The guard under test is a server-side content fingerprint in
``services/log_intents.commit_intents``. It keys on
user + source + normalised text + meal_type + resolved local date and suppresses
a repeat inside a 120 s window, returning the ORIGINAL result marked
``duplicate_suppressed=true`` and writing an audit event.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from drhiro_api.models import AuditEvent, Measurement
from drhiro_api.security import create_service_token

TOOL_URL = "/api/v1/tools/create_meal_from_text"

# The exact production line and its truth: 3 glasses x 250 ml = 750 ml.
PROD_TEXT = "I drank 3 glasses of water"
PROD_TOTAL_ML = 750.0


def _headers(telegram_id: str = "1001", **extra) -> dict:
    h = {
        "X-Service-Token": create_service_token("openclaw"),
        "X-Telegram-Id": telegram_id,
    }
    h.update(extra)
    return h


def _call(client, text=PROD_TEXT, headers=None, **body):
    payload = {"text": text}
    payload.update(body)
    resp = client.post(TOOL_URL, json=payload, headers=headers or _headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("ok") is True, body
    return body["data"]


def _water_rows(db, user):
    return [
        m for m in db.query(Measurement).filter(Measurement.user_id == user.id).all()
        if (m.value_json or {}).get("category") == "water"
        or m.metric_type == "water"
    ]


def _water_total(db, user) -> float:
    return sum((m.value_json or {}).get("amount_ml") or 0 for m in _water_rows(db, user))


# --------------------------------------------------------------------------- #
# 1. the loop: four identical calls -> exactly one set of records
# --------------------------------------------------------------------------- #
def test_four_identical_calls_write_once(client, db, user_a):
    first = _call(client, PROD_TEXT)
    later = [_call(client, PROD_TEXT) for _ in range(3)]

    assert not first.get("duplicate_suppressed")
    for i, data in enumerate(later, start=2):
        assert data.get("duplicate_suppressed") is True, f"call {i}: {data}"
        # the ORIGINAL ids are returned, not new ones
        assert data.get("liquid_ids") == first.get("liquid_ids"), f"call {i}"

    rows = _water_rows(db, user_a)
    assert len(rows) == 1, f"expected one water row, got {len(rows)}"
    assert _water_total(db, user_a) == PROD_TOTAL_ML


# --------------------------------------------------------------------------- #
# 2. the exact production case -> 750 ml, not 3000 ml
# --------------------------------------------------------------------------- #
def test_production_case_three_glasses_four_times(client, db, user_a):
    datas = [_call(client, PROD_TEXT) for _ in range(4)]
    assert sum(1 for d in datas if d.get("duplicate_suppressed")) == 3
    total = _water_total(db, user_a)
    assert total == 750.0, f"water total {total} (truth 750, bug wrote 3000)"
    assert total != 3000.0


# --------------------------------------------------------------------------- #
# 3. false-positive guard: identical calls OUTSIDE the window both write
# --------------------------------------------------------------------------- #
def test_identical_calls_outside_window_both_write(client, db, user_a):
    t0 = datetime(2026, 10, 2, 10, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=300)  # > FINGERPRINT_WINDOW_SECONDS (120)

    first = _call(client, PROD_TEXT, eaten_at=t0.isoformat())
    second = _call(client, PROD_TEXT, eaten_at=t1.isoformat())

    assert not first.get("duplicate_suppressed")
    assert not second.get("duplicate_suppressed"), second
    rows = _water_rows(db, user_a)
    assert len(rows) == 2, f"expected two water rows, got {len(rows)}"
    assert _water_total(db, user_a) == 1500.0


# --------------------------------------------------------------------------- #
# 4. genuinely different content is NEVER suppressed as a near-duplicate
# --------------------------------------------------------------------------- #
def test_different_quantities_are_not_near_duplicates(client, db, user_a):
    a = _call(client, "1 glass of water")
    b = _call(client, "2 glasses of water")

    assert not a.get("duplicate_suppressed")
    assert not b.get("duplicate_suppressed"), b
    rows = _water_rows(db, user_a)
    assert len(rows) == 2, f"expected two water rows, got {len(rows)}"
    assert _water_total(db, user_a) == 750.0  # 250 + 500


# --------------------------------------------------------------------------- #
# 5. a real per-message identity still uses the true idempotency key and
#    is NOT routed through the fingerprint guard
# --------------------------------------------------------------------------- #
def test_identity_bearing_writes_use_real_idempotency_key(client, db, user_a):
    h1 = _headers(**{"X-Telegram-Chat-Id": "555", "X-Telegram-Message-Id": "10"})
    h2 = _headers(**{"X-Telegram-Chat-Id": "555", "X-Telegram-Message-Id": "11"})

    # Same message id twice -> replay, one write.
    one = _call(client, "I ate an apple", headers=h1)
    replay = _call(client, "I ate an apple", headers=h1)
    assert replay.get("replayed") is True
    assert replay.get("meal_id") == one.get("meal_id")

    # A DIFFERENT message with identical text is a genuinely distinct log and
    # must write even inside the window (it has its own verified identity).
    two = _call(client, "I ate an apple", headers=h2)
    assert not two.get("duplicate_suppressed")
    assert two.get("meal_id") != one.get("meal_id")


# --------------------------------------------------------------------------- #
# 6. the suppression is visible in the audit trail, never silent
# --------------------------------------------------------------------------- #
def test_suppression_is_audited(client, db, user_a):
    _call(client, PROD_TEXT)
    _call(client, PROD_TEXT)
    events = (db.query(AuditEvent)
              .filter(AuditEvent.action == "log.duplicate_suppressed").all())
    assert len(events) >= 1, "duplicate suppression was silent (no audit event)"
    assert events[0].user_id_affected == user_a.id


# --------------------------------------------------------------------------- #
# 7. the MCP identity gate still holds (reuses tests/test_mcp_identity_gate.cjs)
# --------------------------------------------------------------------------- #
def test_mcp_identity_gate_still_holds():
    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "tests" / "test_mcp_identity_gate.cjs"
    assert script.exists(), f"missing {script}"
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=60,
        cwd=str(repo_root),
    )
    assert proc.returncode == 0, f"identity gate FAILED:\n{proc.stdout}\n{proc.stderr}"
    assert "PASS" in proc.stdout


# --------------------------------------------------------------------------- #
# 8. when a repeat IS suppressed, the model must be told — "Logged." for a write
#    that never happened is silent data loss, not a guard
# --------------------------------------------------------------------------- #
def test_suppressed_repeat_is_announced_to_the_model(client, db, user_a):
    t0 = datetime(2026, 10, 2, 10, 0, 0, tzinfo=timezone.utc)

    first = client.post(
        TOOL_URL, json={"text": PROD_TEXT, "eaten_at": t0.isoformat()},
        headers=_headers()).json()
    second = client.post(
        TOOL_URL,
        json={"text": PROD_TEXT, "eaten_at": (t0 + timedelta(seconds=10)).isoformat()},
        headers=_headers()).json()

    assert first["ok"] is True and first["data"].get("duplicate_suppressed") is None, first
    assert first["message"] == "Logged.", first

    assert second["ok"] is True, second
    assert second["data"].get("duplicate_suppressed") is True, second
    assert second["message"] != "Logged.", (
        "a suppressed write still answered 'Logged.' — the agent would claim a "
        "record that was never written")
    assert "already" in second["message"].lower(), second["message"]

    # and nothing extra was written
    assert len(_water_rows(db, user_a)) == 1
    assert _water_total(db, user_a) == PROD_TOTAL_ML
