"""Blocked save: a gated write (save_visit_brief) must NOT run without approval."""
from __future__ import annotations


def test_gated_tool_pauses_without_confirmation(mock_tf):
    """A turn hitting a gated tool must pause and NOT auto-approve."""
    mock_tf["state"].approval_needed = True
    from drhiro_bridge.trueforge_client import TrueForgeClient

    client = TrueForgeClient(mock_tf["base"], "drhiro")
    sess = client.create_session()
    reply, pending = client.run_turn(sess, "please save the brief")

    # The turn paused for approval; no final reply yet, and no decision recorded.
    assert pending, "expected a tool.approval_required pause"
    assert mock_tf["state"].approval_decisions == []
    assert reply == ""


def test_bridge_surfaces_approval_prompt(bridge, mock_tg, mock_tf):
    """The bridge, on a paused gated tool, sends an Allow/Deny prompt to Telegram
    and does NOT auto-approve or persist."""
    mock_tf["state"].approval_needed = True
    bridge.cfg.allowed_username = "alice"
    bridge.tg._api = f"{mock_tg['base']}/bottok"

    # Enqueue an authorized user message.
    mock_tg["state"].enqueue_update(1, chat_id=111, text="please save the brief", username="alice")
    update = mock_tg["state"].update_queue.pop(0)
    bridge._process_update(update)

    # The bridge posted an approval prompt (not the final reply) and recorded no decision.
    last = mock_tg["state"].last_message_text()
    assert "Approval required" in last
    assert mock_tf["state"].approval_decisions == []
    assert "allow" not in [str(x).lower() for x in mock_tf["state"].approval_decisions]


def test_deny_does_not_persist(bridge, mock_tg, mock_tf):
    """Denying the approval must resume WITHOUT running the gated write."""
    mock_tf["state"].approval_needed = True
    from drhiro_bridge.trueforge_client import TrueForgeClient

    client = TrueForgeClient(mock_tf["base"], "drhiro")
    sess = client.create_session()
    _, pending = client.run_turn(sess, "save it")

    # Build a deny approval from the pending event and resume.
    approvals = []
    for evt in pending:
        for tc in evt.get("toolCalls") or []:
            approvals.append({
                "type": "user.tool_approval",
                "threadId": evt.get("threadId"),
                "toolCallId": tc.get("id"),
                "approval": {"status": "deny", "reason": "denied by user"},
            })
    reply, more = client.resume_with_approvals(sess, approvals)

    # The decision was recorded as deny, and the turn completed without persisting.
    assert "deny" in mock_tf["state"].approval_decisions
    assert more == []
    assert reply == mock_tf["state"].reply_text
