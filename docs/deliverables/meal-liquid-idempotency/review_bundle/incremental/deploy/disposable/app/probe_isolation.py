"""Probe run INSIDE a model-accessible container: what can it actually reach?

Compose files describe intent; this measures reality from inside the container. Run
by the stack tests via `docker compose exec`:

    python /app/probe_isolation.py

Emits JSON so the test can assert on it. Reports:

  * credential-shaped environment variables present;
  * whether the trusted spool path exists and is readable/writable;
  * whether an administrative interface (Docker socket) is present;
  * whether trusted services (postgres, fake-telegram) are reachable over the
    network - reachability is access.
"""
from __future__ import annotations

import json
import os
import socket
import sys

SECRET_KEY_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "PRIVATE", "JWT", "_KEY")

TRUSTED_HOSTS = [
    ("postgres", 5432),
    ("redis", 6379),
    ("fake-telegram", 8081),
]

SPOOL_PATHS = ["/var/spool/telegram", "/home/node/.openclaw", "/var/run/docker.sock"]


def secret_env() -> list[str]:
    return sorted(
        k for k in os.environ
        if any(m in k.upper() for m in SECRET_KEY_MARKERS)
    )


def reachable(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def path_report(path: str) -> dict:
    exists = os.path.exists(path)
    report = {"path": path, "exists": exists, "readable": False, "writable": False}
    if not exists:
        return report
    report["readable"] = os.access(path, os.R_OK)
    report["writable"] = os.access(path, os.W_OK)
    return report


def main() -> int:
    result = {
        "hostname": socket.gethostname(),
        "secret_env": secret_env(),
        "paths": [path_report(p) for p in SPOOL_PATHS],
        "reachable": {f"{h}:{p}": reachable(h, p) for h, p in TRUSTED_HOSTS},
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
