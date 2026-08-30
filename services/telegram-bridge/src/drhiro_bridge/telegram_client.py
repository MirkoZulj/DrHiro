"""Minimal Telegram Bot API HTTP client.

Implements exactly what the bridge needs: getMe, getUpdates (long polling),
getWebhookInfo, deleteWebhook, sendMessage, and (for approvals) a reply keyboard.
Kept dependency-light (stdlib urllib + json) so it runs in a slim container.

SECRET-SAFE: never log the bot token or message bodies.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("drhiro_bridge.telegram")


class TelegramError(RuntimeError):
    pass


class WebhookConflictError(TelegramError):
    """A webhook is configured; long polling must not start until it is removed."""


class TelegramClient:
    def __init__(self, token: str, api_base: str = "https://api.telegram.org") -> None:
        self._token = token
        self._api = f"{api_base}/bot{token}"

    def _call(self, method: str, payload: dict | None = None) -> dict:
        url = f"{self._api}/{method}"
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise TelegramError(f"Telegram API HTTP {e.code}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise TelegramError(f"Telegram API unreachable: {e.reason}") from e
        if not body.get("ok"):
            raise TelegramError(f"Telegram API error: {body.get('description')}")
        return body.get("result", {})

    def get_me(self) -> dict:
        return self._call("getMe")

    def get_webhook_info(self) -> dict:
        return self._call("getWebhookInfo")

    def delete_webhook(self, drop_pending: bool = True) -> dict:
        return self._call("deleteWebhook", {"drop_pending_updates": drop_pending})

    def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict]:
        payload = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload)
        return result if isinstance(result, list) else []

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: str | None = "Markdown",
        reply_markup: dict | None = None,
    ) -> dict:
        payload: dict = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._call("sendMessage", payload)

    def send_document_file_id(self, chat_id: int, file_id: str, caption: str = "") -> dict:
        """Send a previously-uploaded document by its file_id (no re-upload)."""
        payload: dict = {"chat_id": chat_id, "document": file_id}
        if caption:
            payload["caption"] = caption
        return self._call("sendDocument", payload)

    def send_document_file(
        self, path: str, filename: str | None = None, caption: str = ""
    ) -> str:
        """Upload a local file as a document (multipart/form-data) and return
        the Telegram file_id. Used to register the APK on first setup."""
        from pathlib import Path

        filename = filename or Path(path).name
        boundary = "----drhiro" + __import__("uuid").uuid4().hex
        with open(path, "rb") as f:
            file_bytes = f.read()

        parts: list[bytes] = []
        # Optional caption part
        if caption:
            parts.append(
                (
                    f"--{boundary}\r\n"
                    'Content-Disposition: form-data; name="caption"\r\n\r\n'
                    f"{caption}\r\n"
                ).encode("utf-8")
            )
        # File part
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="document"; '
                f'filename="{filename}"\r\n'
                "Content-Type: application/vnd.android.package-archive\r\n\r\n"
            ).encode("utf-8")
        )
        parts.append(file_bytes)
        parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)

        url = f"{self._api}/sendDocument"
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise TelegramError(f"Telegram sendDocument HTTP {e.code}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise TelegramError(f"Telegram sendDocument unreachable: {e.reason}") from e
        if not result.get("ok"):
            raise TelegramError(f"Telegram sendDocument error: {result.get('description')}")
        doc = result.get("result", {}).get("document", {})
        file_id = doc.get("file_id")
        if not file_id:
            raise TelegramError("Telegram sendDocument returned no file_id")
        return file_id

    # ------------------------------------------------------------------ #
    # Webhook / polling conflict management
    # ------------------------------------------------------------------ #
    def ensure_polling_only(self) -> bool:
        """
        Ensure we can run long polling safely.

        - If no webhook is set, we are clear to poll (returns True).
        - If a webhook IS set, we raise WebhookConflictError: the operator must
          explicitly confirm removal before polling starts. We never delete a
          webhook and start polling in the same automatic step, and we never run
          both at once.
        """
        info = self.get_webhook_info()
        url = (info or {}).get("url", "")
        if not url:
            return True
        raise WebhookConflictError(
            "A Telegram webhook is configured. Long polling cannot run while a "
            "webhook is set (they cannot run simultaneously). To proceed, the "
            "operator must explicitly confirm deleting the webhook."
        )


def send_chat_action_keepalive(client: TelegramClient, chat_id: int) -> None:
    """Best-effort typing indicator so long model turns don't look dead."""
    try:
        client._call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:  # noqa: BLE001
        log.debug("sendChatAction keepalive failed (non-fatal)")
