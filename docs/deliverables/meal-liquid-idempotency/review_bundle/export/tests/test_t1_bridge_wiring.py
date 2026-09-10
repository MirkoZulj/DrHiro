"""T1 — bridge wiring: the trusted worker owns consumption, the model does not.

Verifies the bridge routes consumption-eligible turns to the trusted ingress and
that, when the ingress declines, the conversational path is still used.

Run: python -m pytest tests/test_t1_bridge_wiring.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "telegram-bridge", "src"))

from drhiro_bridge.ingress import extract_event  # noqa: E402


class _StubIngress:
    """Stands in for the HTTP ingress client (transport-level seam)."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.delivered: list = []

    def deliver(self, event):
        self.delivered.append(event)
        return self.outcome


def _update(text: str, message_id: int = 1, user_id: int = 555, chat_id: int = -100200300):
    return {
        "update_id": 1,
        "message": {
            "message_id": message_id,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id, "username": "alice"},
            "text": text,
        },
    }


class TestTrustedIngressOwnership:
    def test_consumption_turn_is_owned_by_ingress_not_the_model(self, bridge, mock_tf, mock_tg):
        bridge._bot_id = "1234567890"
        bridge._ingress = _StubIngress({
            "status": "completed",
            "result": {
                "ok": True,
                "data": {
                    "meal_id": "m1",
                    "totals": {"kcal": 813.0, "protein_g": 78.0, "carbs_g": 0.0, "fat_g": 54.0},
                    "nutrition_complete": True,
                },
            },
        })

        bridge._handle_trusted_ingress(_update("I had 300g steak"))

        # The model was NOT consulted for this turn.
        assert mock_tf["state"].turns_received == []
        # And the user got the trusted result.
        assert mock_tg["state"].sent_messages, "no reply sent"
        text = mock_tg["state"].sent_messages[-1]["text"]
        assert "813 kcal" in text

    def test_no_consumption_falls_through_to_the_model(self, bridge, mock_tf, mock_tg):
        bridge._bot_id = "1234567890"
        bridge._ingress = _StubIngress({"status": "no_consumption", "operation_id": "op1"})

        handled = bridge._handle_trusted_ingress(_update("what is my weight trend?"))

        assert handled is False, "a non-consumption turn must not be hijacked"

    def test_clarification_does_not_log_and_asks(self, bridge, mock_tf, mock_tg):
        bridge._bot_id = "1234567890"
        bridge._ingress = _StubIngress({"status": "needs_clarification", "rejected": ["x"]})

        handled = bridge._handle_trusted_ingress(_update("I had some food"))

        assert handled is True
        assert mock_tf["state"].turns_received == []
        text = mock_tg["state"].sent_messages[-1]["text"].lower()
        assert "haven't logged" in text or "rephrase" in text


class TestVerifiedBotIdentity:
    def test_verified_id_is_taken_from_getme(self, bridge):
        bridge.cfg.telegram_bot_id = ""
        assert bridge._verify_bot_id({"id": 424242, "username": "bot"}) == "424242"

    def test_configured_id_must_match_getme(self, bridge):
        bridge.cfg.telegram_bot_id = "999999"
        with pytest.raises(RuntimeError, match="does not match"):
            bridge._verify_bot_id({"id": 424242, "username": "bot"})

    def test_missing_getme_id_fails_closed(self, bridge):
        bridge.cfg.telegram_bot_id = ""
        with pytest.raises(RuntimeError, match="no bot id"):
            bridge._verify_bot_id({"username": "bot"})


class TestRawUpdateExtraction:
    """Identity comes from the authentic update, never from model output."""

    def test_message_identity(self):
        ev = extract_event(_update("hello", message_id=77), "1234567890")
        assert ev.kind == "message"
        assert ev.bot_id == "1234567890"
        assert ev.chat_id == "-100200300"
        assert ev.message_id == "77"
        assert ev.user_id == "555"

    def test_edited_message_is_an_edit(self):
        upd = _update("corrected text", message_id=78)
        upd["edited_message"] = upd.pop("message")
        ev = extract_event(upd, "1234567890")
        assert ev.kind == "edit"
        assert ev.message_id == "78"

    def test_callback_references_the_operation(self):
        upd = {
            "update_id": 5,
            "callback_query": {
                "id": "cb1",
                "from": {"id": 555, "username": "alice"},
                "message": {"message_id": 9, "chat": {"id": -100200300, "type": "private"}},
                "data": "confirm:op-123",
            },
        }
        ev = extract_event(upd, "1234567890")
        assert ev.kind == "callback"
        assert ev.callback_data == "confirm:op-123"
        # The callback's own message id is not a consumption identity.
        assert ev.message_id == "cb1"

    def test_unrelated_update_yields_none(self):
        assert extract_event({"update_id": 1}, "123") is None
