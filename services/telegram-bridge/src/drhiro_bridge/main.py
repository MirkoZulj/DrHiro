"""drHiro Telegram bridge main loop.

Flow per authorized message:
  user message (Telegram, long polling)
    -> persistent TrueForge session for that conversation
    -> run_turn (TrueForge agent loop; tools called by the model)
    -> if a gated tool (save_visit_brief) pauses for approval:
         surface Allow/Deny to the user in Telegram
         resume with user.tool_approval on the chosen decision
    -> stream the final reply back to Telegram

Security:
  - Only the configured ALLOWED_USERNAME may talk to the bot.
  - Long polling only; never run while a Telegram webhook is set.
  - No tokens or message bodies are logged.
"""
from __future__ import annotations

import json
import logging
import signal
import sys
import threading
import time

from .apk_distribution import ApkManager
from .config import Config
from .telegram_client import (
    TelegramClient,
    WebhookConflictError,
    send_chat_action_keepalive,
)
from .trueforge_client import TrueForgeClient

log = logging.getLogger("drhiro_bridge")

_APPROVE = "drhiro_approve"
_DENY = "drhiro_deny"


def _authorized(message: dict, cfg: Config) -> bool:
    """True if the sender is the configured allowed username OR the resolved
    allowed user id (trusted once verified)."""
    sender = message.get("from") or {}
    user = sender.get("username") or ""
    if user and user.lower() == cfg.allowed_username.lower():
        return True
    sender_id = sender.get("id")
    if cfg.allowed_user_id and sender_id is not None:
        return str(sender_id) == str(cfg.allowed_user_id)
    return False


def _extract_text(message: dict) -> str:
    return (message.get("text") or "").strip()


def _inline_approval_markup(tool_name: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Allow", "callback_data": _APPROVE},
                {"text": "⛔ Deny", "callback_data": _DENY},
            ]
        ],
        "force_reply": False,
    }


def _build_approval_prompt(pending: list[dict]) -> str:
    lines = ["⚠️ *Approval required* — the agent wants to run a gated action:"]
    for evt in pending:
        tool_calls = evt.get("toolCalls") or []
        for tc in tool_calls:
            name = (tc.get("toolInfo") or {}).get("name", "?")
            args = (tc.get("function") or {}).get("arguments") or "{}"
            try:
                parsed = json.loads(args) if isinstance(args, str) else args
            except json.JSONDecodeError:
                parsed = args
            lines.append(f"- Tool: `{name}`")
            lines.append(f"  Args: `{json.dumps(parsed)[:200]}`")
    lines.append("\nAllow or deny?")
    return "\n".join(lines)


class Bridge:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.tg = TelegramClient(cfg.bot_token)
        self.tf = TrueForgeClient(cfg.trueforge_url, cfg.agent_name)
        self.apk = ApkManager(cfg.apk_dir, max_size_mb=cfg.apk_max_size_mb)
        self._sessions: dict[int, str] = {}  # chat_id -> trueforge session_id
        self._pending_approvals: dict[int, list[dict]] = {}
        self._running = True

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)

        # 1. Validate the token without exposing it.
        me = self.tg.get_me()
        log.info("Connected to Telegram bot username=%s", me.get("username"))
        log.info("Long-polling transport selected. No webhook will be used.")

        # 2. Enforce webhook/polling exclusivity BEFORE polling.
        if not self.tg.ensure_polling_only():
            raise WebhookConflictError("cannot start long polling while a webhook is set")

        # 3. Confirm TrueForge is reachable (best-effort).
        h = self.tf.health()
        if not h.get("ok"):
            log.warning("TrueForge not reachable yet: %s — will retry per update", h.get("error"))

        self._poll_loop()

    def _stop(self, *_) -> None:
        log.info("Shutting down bridge.")
        self._running = False

    def _poll_loop(self) -> None:
        offset: int | None = None
        while self._running:
            try:
                updates = self.tg.get_updates(offset=offset, timeout=self.cfg.poll_timeout)
            except WebhookConflictError as e:
                log.error("Webhook conflict: %s", e)
                self._notify_admin(str(e))
                self._running = False
                break
            except Exception as e:  # noqa: BLE001
                log.warning("getUpdates failed: %s", e)
                time.sleep(5)
                continue

            for upd in updates:
                if not self._running:
                    break
                self._process_update(upd)
                if (upd.get("update_id") or 0) >= (offset or 0):
                    offset = upd.get("update_id") + 1

    # ------------------------------------------------------------------ #
    def _process_update(self, update: dict) -> None:
        # Callback query = user answered an approval prompt.
        cb = update.get("callback_query")
        if cb:
            self._handle_callback(cb)
            return

        message = update.get("message")
        if not message:
            return
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None:
            return

        if not _authorized(message, self.cfg):
            log.info("Ignoring message from unauthorized sender (chat=%s)", chat_id)
            self.tg.send_message(chat_id, "Sorry, you are not authorized to use this bot.")
            return

        # After a successful verification, remember the sender's numeric id so it
        # becomes an additional authorization key for future /apk and /apkinfo.
        self._remember_user_id(message)

        text = _extract_text(message)
        if not text:
            return

        # Route bot commands BEFORE the agent loop.
        if text.startswith("/"):
            if self._handle_command(chat_id, text):
                return

        # Start a typing keepalive in the background for long turns.
        threading.Thread(
            target=self._keepalive_loop, args=(chat_id,), daemon=True
        ).start()

        try:
            reply, pending = self._run_conversation(chat_id, text)
        except Exception as e:  # noqa: BLE001
            log.warning("turn failed: %s", e)
            reply = "Sorry, something went wrong on my side. Please try again."
            pending = []

        if pending:
            self._pending_approvals[chat_id] = pending
            self.tg.send_message(
                chat_id, _build_approval_prompt(pending), reply_markup=_inline_approval_markup("gated")
            )
        else:
            self._send_reply(chat_id, reply)

    def _remember_user_id(self, message: dict) -> None:
        """Persist the verified sender's numeric id once, so future requests are
        authorized by id even if the username changes. Does not override an
        explicitly configured ALLOWED_USER_ID."""
        sender = message.get("from") or {}
        sender_id = sender.get("id")
        if sender_id is None:
            return
        if not self.cfg.allowed_user_id:
            self.cfg.allowed_user_id = str(sender_id)
            log.info("Resolved authorized user id (stored in memory for this run)")

    def _handle_command(self, chat_id: int, text: str) -> bool:
        """Handle bot commands. Returns True if the message was fully handled."""
        cmd = text.split()[0].lower()
        try:
            if cmd == "/apk":
                self._cmd_apk(chat_id)
                return True
            if cmd == "/apkinfo":
                self._cmd_apkinfo(chat_id)
                return True
            if cmd == "/status":
                self._cmd_status(chat_id)
                return True
            if cmd == "/help":
                self._cmd_help(chat_id)
                return True
            if cmd == "/start":
                self._cmd_start(chat_id)
                return True
        except Exception as e:  # noqa: BLE001
            log.warning("command %s failed: %s", cmd, e)
            self.tg.send_message(
                chat_id, "Sorry, that command failed. See /help.", parse_mode=None
            )
            return True
        return False

    # -- APK / status / help commands -------------------------------- #
    def _cmd_apk(self, chat_id: int) -> None:
        # Only authorized users reach here (checked in _process_update).
        try:
            result = self.apk.send(self.tg, chat_id)
            self.tg.send_message(chat_id, result, parse_mode=None)
        except Exception as e:  # noqa: BLE001
            log.warning("APK delivery failed: %s", e)
            self.tg.send_message(
                chat_id,
                "Could not send the APK right now. See /apkinfo or check server logs.",
                parse_mode=None,
            )

    def _cmd_apkinfo(self, chat_id: int) -> None:
        try:
            info = self.apk.validate()
            self.tg.send_message(
                chat_id,
                f"drHiro Bridge v{info['version']}\n"
                f"Android: {info['android_requirement']}\n"
                f"Size: {info['size_mb']} MB\n"
                f"SHA-256: {info['sha256']}",
                parse_mode=None,
            )
        except Exception as e:  # noqa: BLE001
            self.tg.send_message(
                chat_id,
                f"APK not ready: {e}. Place drhiro-bridge.apk on the server first.",
                parse_mode=None,
            )

    def _cmd_status(self, chat_id: int) -> None:
        s = self.apk.status()
        self.tg.send_message(
            chat_id,
            f"drHiro server status:\n"
            f"- APK: {s.get('apk')} (v{s.get('version', '?')}, {s.get('size_mb', 0)} MB)\n"
            f"- Registered with Telegram: {s.get('registered', False)}",
            parse_mode=None,
        )

    def _cmd_help(self, chat_id: int) -> None:
        self.tg.send_message(
            chat_id,
            "Commands:\n"
            "/apk — download the drHiro Bridge Android app\n"
            "/apkinfo — app version, size, and SHA-256\n"
            "/status — non-sensitive server status\n"
            "/help — this help\n\n"
            "Only install APKs sent by your own authorized drHiro bot.",
            parse_mode=None,
        )

    def _cmd_start(self, chat_id: int) -> None:
        self.tg.send_message(
            chat_id,
            "Welcome to drHiro — your privacy-first health information agent.\n"
            "Send /apk to download the Android Bridge app, or just start chatting.",
            parse_mode=None,
        )

    def _run_conversation(self, chat_id: int, text: str) -> tuple[str, list[dict]]:
        session_id = self._sessions.get(chat_id)
        if not session_id:
            session_id = self.tf.create_session()
            self._sessions[chat_id] = session_id
            log.info("Opened TrueForge session %s for chat %s", str(session_id), chat_id)
        return self.tf.run_turn(session_id, text)

    def _handle_callback(self, cb: dict) -> None:
        data = cb.get("data")
        message = cb.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None or data not in (_APPROVE, _DENY):
            return
        pending = self._pending_approvals.pop(chat_id, None)
        if not pending:
            self.tg.send_message(chat_id, "This approval is no longer pending.")
            return
        session_id = self._sessions.get(chat_id)
        if not session_id:
            self.tg.send_message(chat_id, "Session lost; please send a new message.")
            return

        allowed = data == _APPROVE
        approvals: list[dict] = []
        for evt in pending:
            thread_id = evt.get("threadId")
            for tc in evt.get("toolCalls") or []:
                approvals.append({
                    "type": "user.tool_approval",
                    "threadId": thread_id,
                    "toolCallId": tc.get("id"),
                    "approval": {
                        "status": "allow" if allowed else "deny",
                        "reason": None if allowed else "denied by user",
                    },
                })
        try:
            reply, more = self.tf.resume_with_approvals(session_id, approvals)
        except Exception as e:  # noqa: BLE001
            log.warning("resume failed: %s", e)
            self.tg.send_message(chat_id, "Sorry, resuming the action failed.")
            return

        if more:
            self._pending_approvals[chat_id] = more
            self.tg.send_message(chat_id, _build_approval_prompt(more), reply_markup=_inline_approval_markup("gated"))
        else:
            self._send_reply(chat_id, reply)

    def _send_reply(self, chat_id: int, reply: str) -> None:
        if not reply:
            reply = "Done."
        # Strip Markdown formatting that could render badly; keep it plain-safe.
        self.tg.send_message(chat_id, reply, parse_mode=None)

    def _keepalive_loop(self, chat_id: int) -> None:
        end = time.time() + 300
        while time.time() < end:
            send_chat_action_keepalive(self.tg, chat_id)
            time.sleep(4)

    def _notify_admin(self, msg: str) -> None:
        """Best-effort: post an operational notice to the allowed user's chat."""
        try:
            me = self.tg.get_me()
            # We can't derive the user's numeric id from username here; log only.
            log.warning("Operational notice (see logs): %s", msg)
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG if __import__("os").environ.get("DRHIRO_DEBUG") == "true" else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = Config()
    missing = cfg.validate()
    if missing:
        log.error("Missing required configuration: %s", ", ".join(missing))
        sys.exit(2)
    Bridge(cfg).start()


if __name__ == "__main__":
    main()
