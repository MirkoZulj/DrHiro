"""MODEL-ACCESSIBLE MCP stand-in (disposable stack).

Represents the MCP server the model calls. It holds no trusted credential and is on
the `turn` network only, so it cannot reach the trusted database, spool, or Telegram
surface. Consumption-writing tools are reported as disabled, matching the writer-gate
policy retained as a second, independent control.
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DISABLED_TOOLS = [
    "log_meal", "log_meal_intelligent", "confirm_intelligent_meal", "log_water",
    "log_liquid", "log_recipe_meal", "build_recipe", "delete_meal",
    "correct_meal_item",
]


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
            return self._json({"ok": True, "role": "mcp-stub"})
        if self.path == "/tools":
            return self._json({
                "ok": True,
                "disabled": DISABLED_TOOLS,
                "reason": "model_writer_disabled",
            })
        return self._json({"ok": False}, 404)


def main():
    port = int(os.environ.get("MCP_PORT", "8091"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"mcp-stub listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
