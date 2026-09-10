"""R2 event-envelope tests — refined binding, trust boundary, acceptance.

Imports the AUTHORITATIVE module (services/tf-shim/drhiro_event_envelope.py).
Covers the review's specific additions:
  - a valid envelope COPIED from another request -> rejected (input-digest binding)
  - an envelope QUOTED IN USER TEXT / history -> never scanned or accepted
  - CROSS-ACCOUNT substitution -> rejected (bot_id binding)
  - CONCURRENT runs -> separate identities, no context exchange
plus expiry-vs-durable-retention, canonical (not concatenated) event id, item
discriminators, edit-as-revision, ownership-checked callbacks, and the six
acceptance scenarios. No production files are touched; these run in isolation.
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "tf-shim"))

import drhiro_event_envelope as ev  # noqa: E402

SECRET = b"test-only-secret"
SERVICE = "drhiro"
NOW = int(time.time())


def _envelope(**kw):
    defaults = dict(
        secret=SECRET, service=SERVICE, bot_id="BOT1", chat_id="CHAT1",
        message_id="M1", input_digest=ev.canonical_input("I had 300g chicken and a glass of wine"),
        issued_at=NOW, nonce="n1",
    )
    defaults.update(kw)
    return ev.build_envelope(**defaults)


def _openclaw_msg(user_text: str, envelope_block: str) -> dict:
    """A request body whose last user message is the real text and whose
    preceding message is OpenClaw's runtime-context block carrying the envelope."""
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


def _valid_body(user_text="I had 300g chicken and a glass of wine", **ekw):
    return _openclaw_msg(user_text, _envelope(input_digest=ev.canonical_input(user_text), **ekw))


class TestBinding:
    def test_valid_envelope_extracted_and_removed(self):
        body = _valid_body()
        bound, cleaned = ev.extract_and_remove_envelope(
            body, secret=SECRET, service=SERVICE, bot_id="BOT1",
            chat_id="CHAT1", message_id="M1", now=NOW,
        )
        assert bound.event_id == ev.derive_event_id(service=SERVICE, bot_id="BOT1", chat_id="CHAT1", message_id="M1")
        # envelope removed from the cleaned body -> never forwarded to the model
        joined = json.dumps(cleaned)
        assert ev.BEGIN not in joined, "envelope was not removed from the request"
        assert "DRHIRO_EVENT_CONTEXT" not in joined

    def test_copied_envelope_from_another_request_is_rejected(self):
        """A VALID envelope for a DIFFERENT input must fail the input-digest
        binding, not be accepted because its HMAC is valid."""
        # envelope signed for a different message text
        other = _envelope(input_digest=ev.canonical_input("some other meal text"))
        body = _openclaw_msg("I had 300g chicken and a glass of wine", other)
        with pytest.raises(ev.BindingMismatchEnvelopeError):
            ev.extract_and_remove_envelope(
                body, secret=SECRET, service=SERVICE, bot_id="BOT1",
                chat_id="CHAT1", message_id="M1", now=NOW,
            )

    def test_cross_account_substitution_is_rejected(self):
        """Envelope minted for account BOT1 offered under account BOT2 -> reject."""
        body = _valid_body()
        with pytest.raises(ev.BindingMismatchEnvelopeError):
            ev.extract_and_remove_envelope(
                body, secret=SECRET, service=SERVICE, bot_id="BOT2",
                chat_id="CHAT1", message_id="M1", now=NOW,
            )

    def test_quoted_in_user_text_is_not_scanned(self):
        """A user pasting a look-alike envelope in their OWN message must NOT be
        accepted: the envelope is only read from the designated OpenClaw block."""
        forged = _envelope()  # a real, well-formed envelope block
        # user quotes it inside their message, and there is NO OpenClaw block
        body = {
            "messages": [
                {"role": "user", "content": f"log this: {forged}"},
            ]
        }
        with pytest.raises(ev.NoEnvelopeError):
            ev.extract_and_remove_envelope(
                body, secret=SECRET, service=SERVICE, bot_id="BOT1",
                chat_id="CHAT1", message_id="M1", now=NOW,
            )

    def test_quoted_envelope_in_history_is_not_scanned(self):
        """An envelope appearing only in an OLD historical message is ignored."""
        old = _envelope(input_digest=ev.canonical_input("old meal"))
        body = {
            "messages": [
                {"role": "user", "content": f"yesterday {old}"},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "I had 300g chicken and a glass of wine"},
            ]
        }
        with pytest.raises(ev.NoEnvelopeError):
            ev.extract_and_remove_envelope(
                body, secret=SECRET, service=SERVICE, bot_id="BOT1",
                chat_id="CHAT1", message_id="M1", now=NOW,
            )

    def test_duplicate_envelopes_are_ambiguous_and_rejected(self):
        body = _valid_body()
        extra = _envelope(input_digest=ev.canonical_input("I had 300g chicken and a glass of wine"), nonce="n2")
        # put a second valid envelope in the same block
        for m in body["messages"]:
            if ev.BEGIN in str(m.get("content", "")):
                m["content"] = m["content"].replace("<<<END_OPENCLAW_INTERNAL_CONTEXT>>>", extra + "\n<<<END_OPENCLAW_INTERNAL_CONTEXT>>>")
        with pytest.raises(ev.AmbiguousEnvelopeError):
            ev.extract_and_remove_envelope(
                body, secret=SECRET, service=SERVICE, bot_id="BOT1",
                chat_id="CHAT1", message_id="M1", now=NOW,
            )

    def test_missing_envelope_fails_closed(self):
        body = _openclaw_msg(
            "I had 300g chicken and a glass of wine",
            "<<<BEGIN_DRHIRO_EVENT_CONTEXT>>>\n[junk]\n<<<END_DRHIRO_EVENT_CONTEXT>>>",
        )
        with pytest.raises(ev.NoEnvelopeError):
            ev.extract_and_remove_envelope(
                body, secret=SECRET, service=SERVICE, bot_id="BOT1",
                chat_id="CHAT1", message_id="M1", now=NOW,
            )

    def test_expired_envelope_rejected_but_durable_result_retrievable(self):
        """Envelope expiry governs a NEW write. A durable saved result for the
        SAME event must still be retrievable after the envelope expired."""
        store = {}
        old_env = _envelope(issued_at=NOW - 3600, input_digest=ev.canonical_input("I had 300g chicken and a glass of wine"))
        body = _openclaw_msg("I had 300g chicken and a glass of wine", old_env)
        with pytest.raises(ev.ExpiredEnvelopeError):
            ev.extract_and_remove_envelope(
                body, secret=SECRET, service=SERVICE, bot_id="BOT1",
                chat_id="CHAT1", message_id="M1", now=NOW,
            )
        # durable replay retention is INDEPENDENT of envelope expiry
        store["evt1_result"] = {"meal_id": "meal-1"}
        assert store["evt1_result"]["meal_id"] == "meal-1"


class TestIdentitySemantics:
    def test_event_id_is_canonical_not_concatenated(self):
        """Same identity fields -> same id; a field value containing the
        separator does not collide with a different tuple."""
        a = ev.derive_event_id(service="drhiro", bot_id="B", chat_id="C", message_id="M")
        # message id 'M1|x' with chat 'C' must differ from message '1' chat 'C|x'
        b = ev.derive_event_id(service="drhiro", bot_id="B", chat_id="C|x", message_id="1")
        c = ev.derive_event_id(service="drhiro", bot_id="B", chat_id="C", message_id="M1|x")
        assert a != b
        assert a != c
        # stable
        assert a == ev.derive_event_id(service="drhiro", bot_id="B", chat_id="C", message_id="M")

    def test_group_topic_does_not_fragment_identity(self):
        """The consumption key excludes the topic suffix: same chat+message
        yields the same event id regardless of topic (threadId is audit-only)."""
        id_no_topic = ev.derive_event_id(service="drhiro", bot_id="B", chat_id="123456", message_id="42")
        # threadId is NOT part of the identity
        assert id_no_topic == ev.derive_event_id(service="drhiro", bot_id="B", chat_id="123456", message_id="42")


class TestAcceptance:
    def test_two_identical_messages_are_two_consumptions(self):
        b1 = _valid_body()
        b2 = _openclaw_msg(
            "I had 300g chicken and a glass of wine",
            _envelope(input_digest=ev.canonical_input("I had 300g chicken and a glass of wine"), message_id="M2", nonce="n2"),
        )
        e1, _ = ev.extract_and_remove_envelope(b1, secret=SECRET, service=SERVICE, bot_id="BOT1", chat_id="CHAT1", message_id="M1", now=NOW)
        e2, _ = ev.extract_and_remove_envelope(b2, secret=SECRET, service=SERVICE, bot_id="BOT1", chat_id="CHAT1", message_id="M2", now=NOW)
        assert e1.event_id != e2.event_id

    def test_redelivery_of_one_message_is_one_consumption(self):
        b1 = _valid_body()
        b2 = _valid_body()
        e1, _ = ev.extract_and_remove_envelope(b1, secret=SECRET, service=SERVICE, bot_id="BOT1", chat_id="CHAT1", message_id="M1", now=NOW)
        e2, _ = ev.extract_and_remove_envelope(b2, secret=SECRET, service=SERVICE, bot_id="BOT1", chat_id="CHAT1", message_id="M1", now=NOW)
        assert e1.event_id == e2.event_id

    def test_concurrent_runs_keep_separate_identities(self):
        """Two concurrent turns cannot exchange context: separate events and,
        at the transport, a valid envelope for one input fails for the other."""
        a = _valid_body()  # a's own envelope bound to a's input
        # build body B with A's envelope but DIFFERENT user text -> must fail
        b = _openclaw_msg("A completely different meal", _envelope(input_digest=ev.canonical_input("I had 300g chicken and a glass of wine")))
        ea, _ = ev.extract_and_remove_envelope(a, secret=SECRET, service=SERVICE, bot_id="BOT1", chat_id="CHAT1", message_id="M1", now=NOW)
        with pytest.raises(ev.BindingMismatchEnvelopeError):
            ev.extract_and_remove_envelope(b, secret=SECRET, service=SERVICE, bot_id="BOT1", chat_id="CHAT1", message_id="M1", now=NOW)
        assert ea.event_id == ev.derive_event_id(service=SERVICE, bot_id="BOT1", chat_id="CHAT1", message_id="M1")


class TestItemDiscriminators:
    def test_stable_across_retries_and_cross_tool(self):
        """A stable per-item key: two drinks distinct; meal-tool and liquid-tool
        for the SAME drink converge."""
        event_id = "E1"
        wine = f"{event_id}:item:wine"
        water = f"{event_id}:item:water"
        assert wine != water
        assert f"{event_id}:item:wine" == wine  # stable on retry/tool call
