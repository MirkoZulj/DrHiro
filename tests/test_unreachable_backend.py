"""Unreachable AI backend (TrueForge down): bridge must fail gracefully."""
from __future__ import annotations

from drhiro_bridge.trueforge_client import TrueForgeClient, TrueForgeError


def test_health_reports_unreachable(mock_tf):
    mock_tf["state"].unreachable = True
    client = TrueForgeClient(mock_tf["base"], "drhiro")
    h = client.health()
    assert h.get("ok") is False


def test_turn_against_unreachable_backend_raises_clean_error(mock_tf):
    mock_tf["state"].unreachable = True
    client = TrueForgeClient(mock_tf["base"], "drhiro")
    try:
        client.create_session()
    except TrueForgeError as e:
        assert "unreachable" in str(e).lower() or "refused" in str(e).lower() or "unreachable" in str(e)
    else:
        raise AssertionError("expected a TrueForgeError for an unreachable backend")


def test_run_turn_unreachable(mock_tf):
    """Direct turn call to an unreachable backend surfaces an error, not a hang."""
    mock_tf["state"].unreachable = True
    client = TrueForgeClient(mock_tf["base"], "drhiro")
    try:
        client.run_turn("sess-x", "hello")
    except TrueForgeError:
        pass
    else:
        raise AssertionError("expected an error for unreachable backend")
