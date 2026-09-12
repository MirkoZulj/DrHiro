"""MODEL-ACCESSIBLE conversational stand-in (disposable stack).

Deliberately holds no Telegram credential, no signing key and no trusted mount. It
receives an already-authenticated turn from the trusted ingress and returns text -
which is all the real OpenClaw gateway should be trusted to do.

Anything it *could* do to trusted state would be a bug in the split, so the stack
tests probe it rather than trusting the compose file.
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._json({"ok": True, "role": "openclaw-stub"})
        return self._json({"ok": False}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        if self.path == "/turn":
            text = str(body.get("text", ""))
            return self._json({
                "ok": True,
                "reply": f"Understood: {text}" if text else "Understood.",
                "engine": "openclaw-stub",
            })
        return self._json({"ok": False}, 404)


def main():
    port = int(os.environ.get("OPENCLAW_PORT", "8090"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"openclaw-stub listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
