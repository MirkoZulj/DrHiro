"""Mock TrueForge server for offline tests.

Simulates the subset of TrueForge's HTTP/SSE API the bridge uses:
  - GET  /healthz
  - POST /api/v1/sessions            -> returns {data:{id}}
  - POST /api/v1/sessions/{id}/turns -> SSE stream of events

Configurable behaviours for the test matrix:
  - `reply_text`: the assistant reply to stream.
  - `approval_needed`: if True, emit a tool.approval_required event and hold the
    turn paused until the client resumes with user.tool_approval, then emit the
    reply. Records whether approval was allowed/denied.
  - `unreachable`: if True, refuse connections (simulates backend down).
  - `model_unavailable`: if True, error on the turn (simulates bad model).

Run standalone:
    python -m tests.mock_trueforge  (starts on 127.0.0.1:18790 by default)
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18790


class MockTrueForgeState:
    def __init__(self) -> None:
        self.reply_text: str = "This is a mock TrueForge reply."
        self.approval_needed: bool = False
        self.unreachable: bool = False
        self.model_unavailable: bool = False
        self.health_ok: bool = True
        self.approval_decisions: list[str] = []  # 'allow' | 'deny'
        self.sessions_created: int = 0
        self.turns_received: list[str] = []
        self.lock = threading.Lock()

    def approve_all(self) -> None:
        self.approval_decisions.append("allow")

    def deny_all(self) -> None:
        self.approval_decisions.append("deny")


class Handler(BaseHTTPRequestHandler):
    state: MockTrueForgeState = None  # type: ignore[assignment]

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, events: list[dict]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for evt in events:
            line = f"data: {json.dumps(evt)}\n\n"
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()
            time.sleep(0.01)

    def do_GET(self) -> None:  # noqa: N802
        st = self.state
        path = urllib.parse.urlparse(self.path).path
        if path == "/healthz":
            if st.unreachable:
                # Simulate connection refusal by closing abruptly.
                self.close_connection = True
                return
            code = 503 if not st.health_ok else 200
            self._send_json(code, {"ok": st.health_ok, "status": "OK!" if st.health_ok else "degraded"})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        st = self.state
        if st.unreachable:
            self.close_connection = True
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            req = {}
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/v1/sessions":
            with st.lock:
                st.sessions_created += 1
            self._send_json(200, {"data": {"id": f"mock-sess-{st.sessions_created}"}})
            return

        if path.endswith("/turns"):
            input_items = req.get("input") or []
            with st.lock:
                st.turns_received.append(json.dumps(input_items)[:200])
                # Decide on any approval responses in the input.
                for item in input_items:
                    if item.get("type") == "user.tool_approval":
                        st.approval_decisions.append(item.get("approval", {}).get("status", "deny"))
            if st.model_unavailable:
                self._send_sse([{"type": "turn.completed", "state": {"status": "failed"}}])
                return
            if st.approval_needed and not st.approval_decisions:
                # First turn pauses for approval.
                self._send_sse([{
                    "type": "tool.approval_required",
                    "threadId": "thread-1",
                    "toolCalls": [{
                        "id": "call-1",
                        "toolInfo": {"name": "save_visit_brief"},
                        "function": {"name": "save_visit_brief", "arguments": json.dumps({"case_id": "demo-001"})},
                    }],
                }])
                return
            # Normal (or post-approval) reply.
            self._send_sse([
                {"type": "model.message.delta", "content": st.reply_text, "finish_reason": None},
                {"type": "turn.completed", "state": {"status": "done"}},
            ])
            return

        self._send_json(404, {"error": "not found"})

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass


def make_server(state: MockTrueForgeState | None = None,
                host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
    st = state or MockTrueForgeState()
    Handler.state = st
    server = ThreadingHTTPServer((host, port), Handler)
    return server, st


def main() -> None:
    port = int(__import__("os").environ.get("MOCK_TF_PORT", DEFAULT_PORT))
    server, st = make_server(port=port)
    print(f"Mock TrueForge on {DEFAULT_HOST}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
