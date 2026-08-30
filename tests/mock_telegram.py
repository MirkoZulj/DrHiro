"""Mock Telegram Bot API server for offline tests.

Implements the endpoints the bridge uses (getMe, getWebhookInfo, deleteWebhook,
getUpdates, sendMessage, sendChatAction) plus a callback-query queue, all against
an in-memory scripted queue. No real Telegram network access is required.

Run standalone:
    python -m tests.mock_telegram  (starts on 127.0.0.1:18081 by default)
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18081


class MockTelegramState:
    def __init__(self, bot_username: str = "DrHiroMockBot") -> None:
        self.bot_username = bot_username
        self.webhook_url: str = ""  # "" = no webhook configured
        self.update_queue: list[dict] = []
        self.sent_messages: list[dict] = []  # every sendMessage call
        self.document_sends: list[dict] = []  # every sendDocument call
        self.document_uploads: list[dict] = []  # uploads (multipart)
        self.upload_file_id: str = "FILEID-0001"  # file_id returned for uploads
        self.fail_upload: bool = False  # simulate a failed first upload
        self.chat_actions: list[dict] = []
        self.callback_queue: list[dict] = []
        self.offset: int | None = None
        self.lock = threading.Lock()

    # --- helpers to script behaviour from tests -------------------------
    def enqueue_update(self, update_id: int, chat_id: int, text: str, username: str) -> None:
        with self.lock:
            self.update_queue.append({
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "chat": {"id": chat_id, "type": "private"},
                    "from": {"id": chat_id, "username": username},
                    "text": text,
                },
            })

    def enqueue_update_with_id(self, update_id: int, chat_id: int, text: str, user_id: int) -> None:
        """Enqueue a message from a numeric user id (no username)."""
        with self.lock:
            self.update_queue.append({
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "chat": {"id": chat_id, "type": "private"},
                    "from": {"id": user_id},
                    "text": text,
                },
            })

    def enqueue_callback(self, callback_id: str, chat_id: int, data: str) -> None:
        with self.lock:
            self.callback_queue.append({
                "update_id": 90000 + len(self.callback_queue),
                "callback_query": {
                    "id": callback_id,
                    "data": data,
                    "message": {"chat": {"id": chat_id}, "message_id": 1},
                },
            })

    def last_message_text(self) -> str:
        with self.lock:
            return self.sent_messages[-1]["text"] if self.sent_messages else ""


def _extract_multipart(content_type: str, raw: bytes) -> tuple[str, bytes]:
    """Minimal multipart/form-data parser: return the (filename, file bytes) of
    the 'document' part. Good enough for the mock's purposes."""
    boundary = None
    for piece in content_type.split(";"):
        piece = piece.strip()
        if piece.startswith("boundary="):
            boundary = piece[len("boundary="):].strip('"')
    if not boundary:
        return "unknown.apk", raw
    delim = b"--" + boundary.encode("utf-8")
    # Split body by the delimiter; find the part with name="document".
    parts = raw.split(delim)
    filename = "unknown.apk"
    for part in parts:
        if b'name="document"' not in part:
            continue
        header, _, body = part.partition(b"\r\n\r\n")
        for line in header.split(b"\r\n"):
            if b"filename=" in line:
                fn = line.split(b'filename="', 1)[-1].rsplit(b'"', 1)[0]
                filename = fn.decode("utf-8", "replace")
        # Strip trailing CRLF before the next delimiter.
        body = body.rstrip(b"\r\n")
        return filename, body
    return filename, raw


class Handler(BaseHTTPRequestHandler):
    state: MockTelegramState = None  # type: ignore[assignment]

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        ctype = self.headers.get("Content-Type", "")
        path = urllib.parse.urlparse(self.path).path
        st = self.state

        # multipart/form-data = a document upload (sendDocument with file)
        if "multipart/form-data" in ctype and path.endswith("/sendDocument"):
            with st.lock:
                if st.fail_upload:
                    self._send(400, {"ok": False, "description": "simulated upload failure"})
                    return
                filename, file_bytes = _extract_multipart(ctype, raw)
                st.document_uploads.append({"filename": filename, "size": len(file_bytes)})
                fid = st.upload_file_id
                self._send(200, {
                    "ok": True,
                    "result": {"document": {"file_id": fid, "file_name": filename, "file_size": len(file_bytes)}},
                })
            return

        try:
            req = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            req = {}
        with st.lock:
            if path.endswith("/getMe"):
                self._send(200, {"ok": True, "result": {"id": 1, "username": st.bot_username, "first_name": "DrHiro"}})
            elif path.endswith("/getWebhookInfo"):
                self._send(200, {"ok": True, "result": {"url": st.webhook_url}})
            elif path.endswith("/deleteWebhook"):
                st.webhook_url = ""
                self._send(200, {"ok": True, "result": True})
            elif path.endswith("/getUpdates"):
                updates = list(st.update_queue)
                st.update_queue = []
                self._send(200, {"ok": True, "result": updates})
            elif path.endswith("/sendMessage"):
                st.sent_messages.append(req)
                self._send(200, {"ok": True, "result": {"message_id": len(st.sent_messages), **req}})
            elif path.endswith("/sendDocument"):
                # Sending by file_id (JSON body) — record and confirm.
                st.document_sends.append(req)
                self._send(200, {"ok": True, "result": {"document": {"file_id": req.get("document", "")}}})
            elif path.endswith("/sendChatAction"):
                st.chat_actions.append(req)
                self._send(200, {"ok": True, "result": True})
            elif path.endswith("/answerCallbackQuery"):
                self._send(200, {"ok": True, "result": True})
            else:
                self._send(404, {"ok": False, "description": f"unknown method {path}"})

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass


def make_server(state: MockTelegramState | None = None,
                host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
    st = state or MockTelegramState()
    Handler.state = st
    server = ThreadingHTTPServer((host, port), Handler)
    return server, st


def main() -> None:
    port = int(__import__("os").environ.get("MOCK_TG_PORT", DEFAULT_PORT))
    server, st = make_server(port=port)
    print(f"Mock Telegram Bot API on {DEFAULT_HOST}:{port} (bot @{st.bot_username})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
