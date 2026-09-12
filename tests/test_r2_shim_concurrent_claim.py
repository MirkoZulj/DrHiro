"""Unit test for atomic per-event claim in tf-shim (Qodo #3).

Verifies that when two concurrent deliveries of the same event arrive,
exactly one wins the Redis SETNX claim and runs the model/tools; the
loser returns an "in progress" response without running tools.
"""
from __future__ import annotations

import asyncio
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "tf-shim"))
import shim


@pytest.fixture
def mock_redis():
    """Create a mock Redis that supports SET NX + eval (Lua) semantics."""
    r = AsyncMock()
    store = {}

    async def mock_get(key):
        return store.get(key)

    async def mock_set(key, value, nx=False, ex=None):
        if nx:
            if key in store:
                return None  # SET NX fails: key exists
            store[key] = value
            return True
        store[key] = value
        return True

    async def mock_delete(key):
        store.pop(key, None)
        return 1

    async def mock_eval(script, numkeys, *keys_and_args):
        # Simulate the _COWNER_RELEASE_LUA script
        key = keys_and_args[0]
        arg = keys_and_args[1]
        if store.get(key) == arg:
            store.pop(key, None)
            return 1
        return 0

    r.get = mock_get
    r.set = mock_set
    r.delete = mock_delete
    r.eval = mock_eval
    return r, store


@pytest.fixture
def mock_redis_conn(mock_redis):
    r, store = mock_redis
    with patch.object(shim, "redis_conn", new_callable=AsyncMock, return_value=r):
        yield r, store


@pytest.mark.asyncio
async def test_try_claim_event_wins_when_no_competition(mock_redis_conn):
    """First caller wins the claim and receives a token."""
    r, store = mock_redis_conn
    result = await shim._try_claim_event("event-1")
    assert result is not None  # a token string
    assert result  # non-empty
    assert "tfshim:claim:event-1" in store
    assert store["tfshim:claim:event-1"] == result


@pytest.mark.asyncio
async def test_try_claim_event_loser_fails(mock_redis_conn):
    """Second concurrent caller loses the claim."""
    r, store = mock_redis_conn
    winner = await shim._try_claim_event("event-1")
    loser = await shim._try_claim_event("event-1")
    assert winner is not None  # token
    assert winner
    assert loser is None


@pytest.mark.asyncio
async def test_try_claim_event_fail_open_on_error():
    """Cache failure must not block traffic (fail open with empty token)."""
    with patch.object(shim, "redis_conn", new_callable=AsyncMock, side_effect=Exception("redis down")):
        result = await shim._try_claim_event("event-1")
        assert result == ""  # fail open: empty sentinel


@pytest.mark.asyncio
async def test_release_claim_clears_key(mock_redis_conn):
    """Release removes the claim key when token matches."""
    r, store = mock_redis_conn
    token = await shim._try_claim_event("event-1")
    assert "tfshim:claim:event-1" in store
    await shim._release_claim("event-1", token)
    assert "tfshim:claim:event-1" not in store


@pytest.mark.asyncio
async def test_release_claim_is_ownership_aware(mock_redis_conn):
    """Qodo #6: a stale owner CANNOT delete a successor's claim.

    Scenario: original turn outlives the claim TTL, a retry acquires a new
    claim with a different token. The original turn's release must NOT
    delete the successor's claim — otherwise a later delivery could start a
    new turn and double-execute.
    """
    r, store = mock_redis_conn

    # Original owner wins the claim
    original_token = await shim._try_claim_event("event-ttl")
    assert original_token
    assert store["tfshim:claim:event-ttl"] == original_token

    # TTL expires — claim evicted (simulated by overwriting the key)
    # A retry now acquires a replacement claim with a NEW token
    successor_token = "successor-unique-token"
    store["tfshim:claim:event-ttl"] = successor_token

    # Original turn finally completes and tries to release its stale claim
    await shim._release_claim("event-ttl", original_token)

    # Successor's claim MUST survive — not deleted by the stale owner
    assert "tfshim:claim:event-ttl" in store
    assert store["tfshim:claim:event-ttl"] == successor_token

    # The successor CAN release its own claim
    await shim._release_claim("event-ttl", successor_token)
    assert "tfshim:claim:event-ttl" not in store


@pytest.mark.asyncio
async def test_release_claim_empty_token_is_noop(mock_redis_conn):
    """Fail-open empty sentinel must not delete anything."""
    r, store = mock_redis_conn
    token = await shim._try_claim_event("event-1")
    assert token
    await shim._release_claim("event-1", "")
    assert "tfshim:claim:event-1" in store
    # Original owner can still release properly
    await shim._release_claim("event-1", token)
    assert "tfshim:claim:event-1" not in store


@pytest.mark.asyncio
async def test_concurrent_deliveries_only_one_runs_model(mock_redis_conn):
    """Two concurrent deliveries of the same event: only one runs the model."""
    r, store = mock_redis_conn

    run_count = 0

    async def mock_run_turn(*args, **kwargs):
        nonlocal run_count
        run_count += 1
        await asyncio.sleep(0.1)  # simulate slow model
        return "model reply"

    async def mock_get_or_create_session(*args, **kwargs):
        return "session-123"

    with patch.object(shim, "get_or_create_session", mock_get_or_create_session), \
         patch.object(shim, "run_turn", mock_run_turn), \
         patch.object(shim, "bind_event", new_callable=AsyncMock):

        # Simulate two concurrent calls
        async def delivery():
            claim_token = await shim._try_claim_event("event-concurrent")
            if claim_token is None:
                return "lost_claim"
            try:
                await mock_get_or_create_session("key")
                reply = await mock_run_turn("session-123", "text")
                return f"ran:{reply}"
            finally:
                await shim._release_claim("event-concurrent", claim_token)

        # Run two deliveries concurrently
        results = await asyncio.gather(delivery(), delivery())

    # Exactly one ran the model
    ran_results = [r for r in results if r.startswith("ran:")]
    lost_results = [r for r in results if r == "lost_claim"]
    assert len(ran_results) == 1, f"Expected 1 to run, got {ran_results}, lost={lost_results}"
    assert len(lost_results) == 1
    assert run_count == 1, f"Model ran {run_count} times, expected 1"
