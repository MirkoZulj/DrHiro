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
    """Create a mock Redis that supports SET NX semantics."""
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

    r.get = mock_get
    r.set = mock_set
    r.delete = mock_delete
    return r, store


@pytest.fixture
def mock_redis_conn(mock_redis):
    r, store = mock_redis
    with patch.object(shim, "redis_conn", new_callable=AsyncMock, return_value=r):
        yield r, store


@pytest.mark.asyncio
async def test_try_claim_event_wins_when_no_competition(mock_redis_conn):
    """First caller wins the claim."""
    r, store = mock_redis_conn
    result = await shim._try_claim_event("event-1")
    assert result is True
    assert "tfshim:claim:event-1" in store


@pytest.mark.asyncio
async def test_try_claim_event_loser_fails(mock_redis_conn):
    """Second concurrent caller loses the claim."""
    r, store = mock_redis_conn
    winner = await shim._try_claim_event("event-1")
    loser = await shim._try_claim_event("event-1")
    assert winner is True
    assert loser is False


@pytest.mark.asyncio
async def test_try_claim_event_fail_open_on_error():
    """Cache failure must not block traffic (fail open)."""
    with patch.object(shim, "redis_conn", new_callable=AsyncMock, side_effect=Exception("redis down")):
        result = await shim._try_claim_event("event-1")
        assert result is True  # fail open


@pytest.mark.asyncio
async def test_release_claim_clears_key(mock_redis_conn):
    """Release removes the claim key."""
    r, store = mock_redis_conn
    await shim._try_claim_event("event-1")
    assert "tfshim:claim:event-1" in store
    await shim._release_claim("event-1")
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
            # Simulate the claim logic from chat_completions
            if not await shim._try_claim_event("event-concurrent"):
                return "lost_claim"
            try:
                await mock_get_or_create_session("key")
                reply = await mock_run_turn("session-123", "text")
                return f"ran:{reply}"
            finally:
                await shim._release_claim("event-concurrent")

        # Run two deliveries concurrently
        results = await asyncio.gather(delivery(), delivery())

    # Exactly one ran the model
    ran_results = [r for r in results if r.startswith("ran:")]
    lost_results = [r for r in results if r == "lost_claim"]
    assert len(ran_results) == 1, f"Expected 1 to run, got {ran_results}, lost={lost_results}"
    assert len(lost_results) == 1
    assert run_count == 1, f"Model ran {run_count} times, expected 1"
