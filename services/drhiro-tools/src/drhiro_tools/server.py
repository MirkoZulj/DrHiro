"""
drHiro MCP tool server — exposes the four submission tools to TrueForge.

TrueForge attaches this server (by URL) to the drHiro agent. TrueForge runs the
agent execution loop (model calls, context, session state); this server is the
narrow, consent-scoped tool surface. `save_visit_brief` is gated: the agent spec
declares it in `require_approval_for_tools`, so TrueForge pauses for explicit
human approval (tool.approval_required) before it runs.

Tools:
  - get_demo_case      read-only synthetic fixture retrieval
  - create_visit_brief structured non-diagnostic care-preparation output
  - save_visit_brief   approval-gated persistent/export action
  - get_service_status non-sensitive status for authorized users
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import store as _store

mcp = FastMCP("drhiro-tools")

EXPORT_DIR = Path(os.environ.get("EXPORT_DIR", "/data/exports"))
START_TIME = time.time()

APP_VERSION = os.environ.get("DRHIRO_VERSION", "0.1.0")


def _redact(value: str, keep: int = 4) -> str:
    """Secret-safe: never echo full credentials/tokens into tool output or logs."""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)


@mcp.tool()
def get_demo_case(case_id: str | None = None) -> dict:
    """Read-only retrieval of a bundled SYNTHETIC demo case (no case_id lists all)."""
    return _store.get_demo_case(case_id)


@mcp.tool()
def create_visit_brief(case_id: str, focus: str, user_notes: str = "") -> dict:
    """
    Produce a structured, NON-DIAGNOSTIC care-preparation brief for a SYNTHETIC case.

    Organizes discussion points and sample questions for an upcoming visit. Makes no
    diagnosis, prescription, or triage recommendation.
    """
    return _store.create_visit_brief(case_id, focus, user_notes)


@mcp.tool()
def save_visit_brief(case_id: str, brief: dict) -> dict:
    """
    APPROVAL-GATED export: persist a visit brief to the exports directory.

    This is the one write/export action in the agent. TrueForge pauses before
    calling it and requires explicit human approval (Allow/Deny). It writes a JSON
    file under EXPORT_DIR and returns its path. No data leaves the container.
    """
    if not isinstance(brief, dict) or not brief:
        return {"ok": False, "error": "brief must be a non-empty object"}
    if brief.get("synthetic") is not True:
        return {
            "ok": False,
            "error": "refusing to save: brief is not marked SYNTHETIC",
        }
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    name = f"visit-brief-{case_id}-{uuid.uuid4().hex[:8]}.json"
    path = EXPORT_DIR / name
    payload = {
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "case_id": case_id,
        "brief": brief,
    }
    path.write_text(_json_dumps(payload), encoding="utf-8")
    return {
        "ok": True,
        "saved": True,
        "file": str(path),
        "filename": name,
        "synthetic": True,
        "notice": "Saved a SYNTHETIC care-preparation brief. Not a medical record.",
    }


@mcp.tool()
def get_service_status() -> dict:
    """Non-sensitive service status: version, uptime, export count. No user data."""
    try:
        exports = list(EXPORT_DIR.glob("visit-brief-*.json"))
        export_count = len(exports)
    except Exception:  # noqa: BLE001
        export_count = 0
    return {
        "ok": True,
        "service": "drhiro-tools",
        "version": APP_VERSION,
        "uptime_seconds": int(time.time() - START_TIME),
        "export_count": export_count,
        "synthetic_only": True,
        "notice": "Status is non-sensitive and reveals no user data.",
    }


def _json_dumps(payload: dict) -> str:
    import json

    return json.dumps(payload, indent=2, sort_keys=True)


def main() -> None:
    # SSE transport on port 3100 — TrueForge registers this URL as an MCP server.
    mcp.run(transport="sse")


if __name__ == "__main__":
    main()
