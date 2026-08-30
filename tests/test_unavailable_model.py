"""Unavailable model: the turn fails cleanly (no hang, clear error)."""
from __future__ import annotations

from drhiro_bridge.trueforge_client import TrueForgeClient, TrueForgeError


def test_model_unavailable_turn_returns_failed_state(mock_tf):
    """A turn against a model that errors should return an empty/failed reply, not hang."""
    mock_tf["state"].model_unavailable = True
    client = TrueForgeClient(mock_tf["base"], "drhiro")
    sess = client.create_session()
    # The mock streams a turn.completed with status failed and no content.
    reply, pending = client.run_turn(sess, "hello")
    assert reply == ""
    assert pending == []


def test_model_unavailable_does_not_hang(mock_tf):
    """Guard: run_turn must return (bounded) even when the model is unavailable."""
    mock_tf["state"].model_unavailable = True
    client = TrueForgeClient(mock_tf["base"], "drhiro")
    sess = client.create_session()
    import time
    start = time.time()
    client.run_turn(sess, "hello")
    assert time.time() - start < 30


def test_unreachable_model_endpoint_surfaces_error():
    """Pointing at a dead endpoint yields a clean TrueForgeError."""
    client = TrueForgeClient("http://127.0.0.1:1", "drhiro")
    try:
        client.create_session()
    except TrueForgeError as e:
        assert "unreachable" in str(e).lower() or "refused" in str(e).lower()
    else:
        raise AssertionError("expected TrueForgeError for a dead endpoint")
