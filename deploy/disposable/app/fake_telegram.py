"""Minimal stand-in for the Telegram Bot API (disposable stack only).

Implements just enough of the surface the trusted ingress uses: getMe, getUpdates,
sendMessage. Plus control endpoints so tests can enqueue updates and drive the
delivery-outcome modes that produce the `unknown` reply state.

Delivery modes (set via POST /_control/mode):
  normal             accept the message, return ok            -> reply_state 'sent'
  accept_then_hang   RECORD the delivery, never respond        -> ambiguous send:
                     the message WAS delivered, the caller cannot confirm it, so a
                     correct implementation must end in 'unknown', NOT 'sent' and
                     NOT silently retried
  refuse             fail before acceptance (connection reset) -> known-safe failure
                     -> 'failed' -> safe to retry

The fake is deliberately faithful on the one point that matters: in
accept_then_hang the message IS delivered. A retry would therefore duplicate it.
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOT_ID = int(os.environ.get("FAKE_BOT_ID", "8677922871"))
BOT_USERNAME = os.environ.get("FAKE_BOT_USERNAME", "disposable_bot")

_lock = threading.Lock()
_updates: list[dict] = []
_next_update_id = [1000]
_sent: list[dict] = []
_mode = ["normal"]
_delivered = [0]


def _next_id() -> int:
    _next_update_id[0] += 1
    return _next_update_id[0]


def enqueue_message(chat_id: str, text: str, message_id: str | None = None) -> dict:
    with _lock:
        update = {
            "update_id": _next_id(),
            "message": {
                "message_id": int(message_id) if message_id else _next_id(),
                "date": int(time.time()),
                "chat": {"id": int(chat_id), "type": "private"},
                "from": {"id": int(chat_id), "is_bot": False, "first_name": "Tester"},
                "text": text,
            },
        }
        _updates.append(update)
        return update


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep test output readable
        pass

    # -- helpers ---------------------------------------------------------- #
    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {"_raw": raw.decode(errors="replace")}

    def _method(self) -> str:
        return self.path.rstrip("/").split("/")[-1]

    # -- API -------------------------------------------------------------- #
    def _handle_get_me(self):
        return self._send_json({
            "ok": True,
            "result": {"id": BOT_ID, "is_bot": True, "username": BOT_USERNAME,
                       "first_name": "Disposable"},
        })

    def _handle_get_updates(self):
        # NOTE: delivery-failure modes must NOT affect polling, or a test that wants
        # a failing *send* would stop the consumer from receiving anything at all.
        with _lock:
            return self._send_json({"ok": True, "result": list(_updates)})

    def _handle_sent(self):
        with _lock:
            return self._send_json({"ok": True, "sent": list(_sent),
                                    "delivered": _delivered[0]})

    def do_GET(self):
        # The real Bot API is method-agnostic for these; clients commonly POST.
        if self.path == "/health":
            return self._send_json({"ok": True})
        if self.path == "/_control/sent":
            return self._handle_sent()

        method = self._method()
        if method == "getMe":
            return self._handle_get_me()
        if method == "getUpdates":
            return self._handle_get_updates()
        return self._send_json({"ok": False, "description": "not found"}, 404)

    def do_POST(self):
        body = self._read_body()

        if self.path == "/_control/enqueue":
            up = enqueue_message(str(body["chat_id"]), body["text"],
                                 str(body.get("message_id")) if body.get("message_id") else None)
            return self._send_json({"ok": True, "update": up})

        if self.path == "/_control/mode":
            with _lock:
                _mode[0] = body["mode"]
            return self._send_json({"ok": True, "mode": _mode[0]})

        if self.path == "/_control/sent":
            with _lock:
                return self._send_json({"ok": True, "sent": list(_sent),
                                        "delivered": _delivered[0]})

        if self.path == "/_control/reset":
            with _lock:
                _updates.clear()
                _sent.clear()
                _delivered[0] = 0
                _mode[0] = "normal"
                _next_update_id[0] = 1000
            return self._send_json({"ok": True})

        if self.path == "/_control/ack":
            # Acknowledge delivered updates so getUpdates stops returning them
            # (mirrors offset-based consumption).
            with _lock:
                acked = {int(u) for u in body.get("update_ids", [])}
                _updates[:] = [u for u in _updates if u["update_id"] not in acked]
            return self._send_json({"ok": True, "remaining": len(_updates)})

        method = self._method()
        if method == "getMe":
            return self._handle_get_me()
        if method == "getUpdates":
            return self._handle_get_updates()

        if method == "sendMessage":
            mode = _mode[0]

            if mode == "refuse":
                # A RESPONSE is returned (429): the request reached Telegram and was
                # refused before acceptance, so a retry provably cannot duplicate.
                return self._send_json(
                    {"ok": False, "error_code": 429,
                     "description": "Too Many Requests: retry after 1"},
                    code=429,
                )

            if mode == "refuse_conn":
                # Connection dropped before any response. Ambiguous from the client's
                # point of view; the ingress must treat this conservatively.
                self.close_connection = True
                try:
                    self.wfile.close()
                except Exception:
                    pass
                return

            with _lock:
                _delivered[0] += 1
                _sent.append({
                    "chat_id": str(body.get("chat_id")),
                    "text": body.get("text"),
                    "message_id": _next_id(),
                    "delivered": True,
                })

            if mode == "accept_then_hang":
                # Delivered, but no response reaches the caller. This is the
                # ambiguous window: 'sent' would be a guess, so the ingress must
                # record 'unknown' and leave it for resolution.
                time.sleep(30)
                return

            with _lock:
                mid = _sent[-1]["message_id"]
            return self._send_json({"ok": True, "result": {"message_id": mid,
                                                           "chat": {"id": body.get("chat_id")}}})

        if method == "editMessageText":
            return self._send_json({"ok": True, "result": {}})

        return self._send_json({"ok": False, "description": "not found"}, 404)


def main():
    port = int(os.environ.get("FAKE_TELEGRAM_PORT", "8081"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"fake-telegram listening on :{port} (bot id {BOT_ID})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
