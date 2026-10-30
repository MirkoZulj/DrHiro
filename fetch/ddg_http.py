#!/usr/bin/env python3
"""ddg-http.py — thin HTTP wrapper around ddg_fallback.py, bound to the docker
bridge gateway so the intelligent-meal container can reach the VPS-host
Camoufox + tunnel for DuckDuckGo nutrition lookups.

Host-side service. Bind 172.20.0.1:<port>. Callable from containers as:
  POST http://172.20.0.1:8098/lookup   {"query": "..."}
  GET  http://172.20.0.1:8098/health
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading

import ddg_fallback

HOST = "172.20.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8098
_LOOKUP_LOCK = threading.Lock()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # silence

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/lookup"):
            return self._send(404, {"error": "not found"})
        try:
            ln = int(self.headers.get("Content-Length", 0))
            q = json.loads(self.rfile.read(ln)).get("query", "")
        except Exception:
            return self._send(400, {"error": "bad json, need {query}"})
        if not q:
            return self._send(400, {"error": "empty query"})
        try:
            with _LOOKUP_LOCK:
                # throttle: DDG one query at a time through the shared browser
                result = ddg_fallback.search_nutrition(q)
            return self._send(200, result)
        except Exception as e:
            return self._send(500, {"error": str(e), "candidates": []})


if __name__ == "__main__":
    print(f"ddg-http listening on {HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
