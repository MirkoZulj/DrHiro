"""drHiro MCP server — exposes drHiro Core API tools to OpenClaw.

This is the REAL tool surface for the OpenClaw agent. Instead of prose
skill instructions ("use the exec tool"), the agent receives actual MCP
tool schemas and calls them directly — no shell, no improvising.

Each tool:
- infers the user from X-Telegram-Id (passed via the tool argument that
  OpenClaw fills from the session sender)
- authenticates with the signed service token
- returns the drHiro API JSON
"""

from __future__ import annotations

import os

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("drhiro")

API_BASE = os.environ.get("DRHIRO_MCP_API", "http://api:8000/api/v1")
SERVICE_TOKEN = os.environ.get("DRHIRO_OPENCLAW_SERVICE_TOKEN", "")


def _call(method: str, path: str, telegram_id: str, body: dict | None = None) -> dict:
    """Call the drHiro API with the signed service identity."""
    if not SERVICE_TOKEN:
        return {"ok": False, "error": "DRHIRO_OPENCLAW_SERVICE_TOKEN not set"}
    headers = {
        "X-Service-Token": SERVICE_TOKEN,
        "X-Telegram-Id": telegram_id,
        "Content-Type": "application/json",
    }
    with httpx.Client(base_url=API_BASE, timeout=60) as client:
        try:
            resp = client.request(method, path, headers=headers, json=body)
            if resp.status_code >= 400:
                return {"ok": False, "error": f"API {resp.status_code}: {resp.text[:300]}"}
            return resp.json()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}


@mcp.tool()
def get_my_today_summary(telegram_id: str) -> dict:
    """Get the current user's today summary (steps, BP, weight, water, meals)."""
    return _call("GET", "/tools/get_my_today_summary", telegram_id)


@mcp.tool()
def get_my_metric_trend(telegram_id: str, metric: str, period: str = "30d") -> dict:
    """Get a trend for a metric (weight, steps, blood_pressure, sleep)."""
    return _call("POST", "/tools/get_my_metric_trend", telegram_id,
                 {"metric": metric, "period": period})


@mcp.tool()
def create_manual_weight(telegram_id: str, value: float, measured_at: str | None = None) -> dict:
    """Log a manual weight in kg for the user."""
    return _call("POST", "/tools/create_manual_weight", telegram_id,
                 {"value": value, "measured_at": measured_at})


@mcp.tool()
def create_manual_bp(telegram_id: str, systolic: int, diastolic: int, pulse: int | None = None,
                     measured_at: str | None = None, context: str | None = None) -> dict:
    """Log a manual blood pressure reading (systolic/diastolic mmHg, optional pulse)."""
    return _call("POST", "/tools/create_manual_bp", telegram_id,
                 {"systolic": systolic, "diastolic": diastolic, "pulse": pulse,
                  "measured_at": measured_at, "context": context})


@mcp.tool()
def create_meal_from_text(telegram_id: str, text: str, meal_type: str | None = None,
                          eaten_at: str | None = None) -> dict:
    """Log a meal from a free-text description."""
    return _call("POST", "/tools/create_meal_from_text", telegram_id,
                 {"text": text, "meal_type": meal_type, "eaten_at": eaten_at})


@mcp.tool()
def list_my_reminders(telegram_id: str) -> dict:
    """List the user's reminders."""
    return _call("GET", "/tools/list_my_reminders", telegram_id)


@mcp.tool()
def create_reminder(telegram_id: str, type: str, schedule_json: dict,
                    timezone: str = "UTC") -> dict:
    """Create a reminder. schedule_json: {"days":["mon"],"time":"08:00"} or {"cron":"0 8 * * 1"}."""
    return _call("POST", "/tools/create_reminder", telegram_id,
                 {"type": type, "schedule_json": schedule_json, "timezone": timezone})


@mcp.tool()
def set_user_goal(telegram_id: str, goal_type: str, target_json: dict, period: str | None = None) -> dict:
    """Set a health goal for the user."""
    return _call("POST", "/tools/set_user_goal", telegram_id,
                 {"goal_type": goal_type, "target_json": target_json, "period": period})


@mcp.tool()
def get_my_active_alerts(telegram_id: str) -> dict:
    """List the user's active deterministic rule-engine alerts."""
    return _call("GET", "/tools/get_my_active_alerts", telegram_id)


@mcp.tool()
def issue_device_code(telegram_id: str) -> dict:
    """Issue a one-time device code so the user can link the Android bridge app."""
    return _call("POST", "/tools/issue_device_code", telegram_id, {})


@mcp.tool()
def undo_last_user_action(telegram_id: str) -> dict:
    """Undo the user's last logged action."""
    return _call("POST", "/tools/undo_last_user_action", telegram_id, {})


if __name__ == "__main__":
    mcp.run(transport="stdio")
