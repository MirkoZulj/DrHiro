"""Allowed save: after explicit confirmation, the gated write runs and the
turn completes with the reply delivered back to Telegram."""
from __future__ import annotations

from drhiro_bridge.main import _APPROVE


def _enqueue_and_process(bridge, mock_tg, chat_id, text, username):
    mock_tg["state"].enqueue_update(100 + chat_id, chat_id, text, username)
    update = mock_tg["state"].update_queue.pop(0)
    bridge._process_update(update)


def test_allowed_save_after_confirmation(bridge, mock_tg, mock_tf):
    """Full flow: user asks to save -> bridge prompts -> user clicks Allow ->
    TrueForge resumes -> reply is delivered and approval recorded as allow."""
    mock_tf["state"].approval_needed = True
    mock_tf["state"].reply_text = "Brief saved successfully."
    bridge.cfg.allowed_username = "alice"
    bridge.tg._api = f"{mock_tg['base']}/bottok"

    # 1. Authorized user sends a message requesting a save.
    _enqueue_and_process(bridge, mock_tg, chat_id=222, text="save the brief", username="alice")

    # Bridge posted the approval prompt and stored pending approvals for this chat.
    assert "Approval required" in mock_tg["state"].last_message_text()
    assert 222 in bridge._pending_approvals

    # 2. User clicks Allow via the callback.
    mock_tg["state"].enqueue_callback("cb-1", chat_id=222, data=_APPROVE)
    cb_update = mock_tg["state"].callback_queue.pop(0)
    bridge._handle_callback(cb_update["callback_query"])

    # 3. The gated action was approved; TrueForge recorded 'allow' and the
    #    final reply was delivered to Telegram.
    assert "allow" in mock_tf["state"].approval_decisions
    assert mock_tf["state"].turns_received  # a resume turn happened
    assert mock_tf["state"].reply_text in mock_tg["state"].last_message_text()


def test_allowed_save_via_deny_does_not_approve(bridge, mock_tg, mock_tf):
    """Deny path: the gated action is recorded as denied and the turn still
    completes (with the reply), but no 'allow' decision is recorded."""
    mock_tf["state"].approval_needed = True
    mock_tf["state"].reply_text = "Okay, I will not save the brief."
    bridge.cfg.allowed_username = "alice"
    bridge.tg._api = f"{mock_tg['base']}/bottok"

    _enqueue_and_process(bridge, mock_tg, chat_id=333, text="save it", username="alice")
    assert "Approval required" in mock_tg["state"].last_message_text()

    from drhiro_bridge.main import _DENY
    mock_tg["state"].enqueue_callback("cb-2", chat_id=333, data=_DENY)
    cb_update = mock_tg["state"].callback_queue.pop(0)
    bridge._handle_callback(cb_update["callback_query"])

    assert "deny" in mock_tf["state"].approval_decisions
    assert "allow" not in mock_tf["state"].approval_decisions
    assert mock_tf["state"].reply_text in mock_tg["state"].last_message_text()


def test_unauthorized_user_cannot_trigger_save(bridge, mock_tg, mock_tf):
    """An unauthorized sender must not reach the agent loop at all."""
    mock_tf["state"].approval_needed = True
    bridge.cfg.allowed_username = "alice"
    bridge.tg._api = f"{mock_tg['base']}/bottok"

    _enqueue_and_process(bridge, mock_tg, chat_id=444, text="save the brief", username="eve")

    # No session was created and no approval prompt was posted.
    assert mock_tf["state"].sessions_created == 0
    last = mock_tg["state"].last_message_text()
    assert "authorized" in last.lower()
