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
from .pairing import PairingManager
from .pairing_http import serve as serve_pairing_http
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


def _authorized_callback(cb: dict, cfg: Config) -> bool:
    """Authorize the user who pressed an inline button (callback_query.from).

    Callback updates carry the presser's identity in `from`, not in a `message`
    field, so they are routed through their own authorization check before any
    pending approval state is consumed or TrueForge is resumed.
    """
    sender = cb.get("from") or {}
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
        self.pairing = PairingManager(
            cfg.pairing_state_dir,
            token_ttl=cfg.pairing_ttl,
            allow_http_lan=cfg.allow_http_lan,
        )
        self._sessions: dict[int, str] = {}  # chat_id -> trueforge session_id
        self._pending_approvals: dict[int, list[dict]] = {}
        self._pending_revoke: dict[int, str] = {}  # chat_id -> device_id awaiting confirm
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

        # 3. Start the device-facing pairing HTTP server (background).
        threading.Thread(
            target=serve_pairing_http,
            args=(self.pairing,),
            kwargs={
                "host": self.cfg.pairing_http_host,
                "port": self.cfg.pairing_http_port,
                "service_token": self.cfg.pairing_service_token,
            },
            daemon=True,
        ).start()

        # 4. Confirm TrueForge is reachable (best-effort).
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
        # Callback query = user answered an approval prompt. Authorize the
        # sender (callback_query.from) BEFORE consuming pending approval state
        # or resuming TrueForge — otherwise any group member could Allow/Deny
        # the configured user's gated action.
        cb = update.get("callback_query")
        if cb:
            if not _authorized_callback(cb, self.cfg):
                chat_id = ((cb.get("message") or {}).get("chat") or {}).get("id")
                if chat_id is not None:
                    log.info("Ignoring callback from unauthorized sender (chat=%s)", chat_id)
                    self.tg.send_message(
                        chat_id, "Sorry, you are not authorized to use this bot."
                    )
                return
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

        # Handle the /revoke confirmation reply.
        if text.upper() == "CONFIRM" and chat_id in self._pending_revoke:
            device_id = self._pending_revoke.pop(chat_id)
            ok = self.pairing.revoke_device(device_id, self._chat_user_id(chat_id))
            if ok:
                self.tg.send_message(
                    chat_id, f"Device `{device_id[:8]}…` revoked.", parse_mode=None
                )
            else:
                self.tg.send_message(
                    chat_id, "Device not found or not yours.", parse_mode=None
                )
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
            if cmd == "/pair":
                self._cmd_pair(chat_id)
                return True
            if cmd == "/devices":
                self._cmd_devices(chat_id)
                return True
            if cmd.startswith("/revoke"):
                self._cmd_revoke(chat_id, text)
                return True
        except Exception as e:  # noqa: BLE001
            log.warning("command %s failed: %s", cmd, e)
            self.tg.send_message(
                chat_id, "Sorry, that command failed. See /help.", parse_mode=None
            )
            return True
        return False

    # -- APK / status / help commands -------------------------------- #
    def _server_public_url(self) -> str:
        """The URL the Android Bridge uses to reach this server for pairing.

        Must be a reachable HTTPS (or trusted-LAN) URL. If DRHIRO_PUBLIC_URL is
        unset we refuse to mint a pairing link rather than silently embedding
        http://localhost (which on the phone points to the phone, not the
        server), so pairing can never produce an unreachable deep link.
        """
        if self.cfg.server_public_url:
            return self.cfg.server_public_url
        raise ValueError(
            "Pairing requires a reachable server URL: set DRHIRO_PUBLIC_URL to an "
            "HTTPS (or trusted-LAN) address the Android device can reach, and make "
            "sure the pairing port is exposed via a reverse proxy or host mapping."
        )

    def _connect_button(self, link: str) -> dict:
        return {"inline_keyboard": [[{"text": "🔗 Connect drHiro Bridge", "url": link}]]}

    def _cmd_apk(self, chat_id: int) -> None:
        # Only authorized users reach here (checked in _process_update).
        try:
            result = self.apk.send(self.tg, chat_id)
            self.tg.send_message(chat_id, result, parse_mode=None)
            # Create a pairing token + Connect button for the requesting user.
            self._send_pairing_link(chat_id)
        except Exception as e:  # noqa: BLE001
            log.warning("APK delivery failed: %s", e)
            self.tg.send_message(
                chat_id,
                "Could not send the APK right now. See /apkinfo or check server logs.",
                parse_mode=None,
            )

    def _cmd_pair(self, chat_id: int) -> None:
        """Generate a fresh pairing link without resending the APK."""
        self._send_pairing_link(chat_id)

    def _send_pairing_link(self, chat_id: int) -> None:
        """Create a single-use pairing token bound to the requesting user and
        post a 'Connect drHiro Bridge' inline button with the deep link."""
        from .pairing import RateLimitedError

        user_id = self._chat_user_id(chat_id)
        try:
            server = self._server_public_url()
        except ValueError as e:
            # DRHIRO_PUBLIC_URL not configured — no unreachable link allowed.
            self.tg.send_message(chat_id, str(e), parse_mode=None)
            return
        try:
            created = self.pairing.create_token(user_id, server)
        except RateLimitedError as e:
            self.tg.send_message(chat_id, str(e), parse_mode=None)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("pairing token creation failed: %s", e)
            self.tg.send_message(
                chat_id, "Could not create a pairing link. Try again shortly.", parse_mode=None
            )
            return

        lines = [
            "📲 *Pair your drHiro Bridge*",
            f"Token valid for 10 minutes (single use).",
        ]
        if created.get("warning"):
            lines.append(created["warning"])
        self.tg.send_message(
            chat_id,
            "\n".join(lines),
            reply_markup=self._connect_button(created["link"]),
        )

    def _cmd_devices(self, chat_id: int) -> None:
        user_id = self._chat_user_id(chat_id)
        devices = self.pairing.list_devices(user_id)
        if not devices:
            self.tg.send_message(chat_id, "No linked devices yet.", parse_mode=None)
            return
        lines = ["📱 *Linked devices*"]
        for d in devices:
            status = "revoked" if d["revoked"] else "active"
            lines.append(
                f"- {d['device_name']} (`{d['device_id'][:8]}…`) · {status}"
            )
        lines.append("\nTo revoke: `/revoke <device-id>`")
        self.tg.send_message(chat_id, "\n".join(lines), parse_mode=None)

    def _cmd_revoke(self, chat_id: int, text: str) -> None:
        parts = text.split()
        if len(parts) < 2:
            self.tg.send_message(
                chat_id,
                "Usage: `/revoke <device-id>` — see /devices for your device ids.",
                parse_mode=None,
            )
            return
        device_id = parts[1].strip()
        user_id = self._chat_user_id(chat_id)
        # Confirm before revoking (destructive-ish).
        self._pending_revoke[chat_id] = device_id
        self.tg.send_message(
            chat_id,
            f"Revoke device `{device_id[:8]}…`? Reply `CONFIRM` to proceed.",
            parse_mode=None,
        )

    def _chat_user_id(self, chat_id: int) -> str:
        """Best-effort: the numeric user id for the chat. Falls back to a
        stable per-chat id so pairing still works when the sender id is unknown."""
        # In production the sender's numeric id is resolved at first contact and
        # stored on cfg.allowed_user_id; use it when available.
        if self.cfg.allowed_user_id:
            return self.cfg.allowed_user_id
        return f"chat:{chat_id}"

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
            "/apk — download the drHiro Bridge Android app + pairing link\n"
            "/apkinfo — app version, size, and SHA-256\n"
            "/pair — fresh pairing link (no APK resend)\n"
            "/devices — list your linked devices\n"
            "/revoke <device> — revoke a linked device\n"
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
