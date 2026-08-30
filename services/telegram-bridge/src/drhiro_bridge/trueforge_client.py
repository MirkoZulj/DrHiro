"""TrueForge client — drives the agent execution loop over TrueForge's HTTP/SSE API.

TrueForge (open source, MIT) manages the full agent loop: model calls, MCP tools,
approvals, context, and session state. The bridge is a thin client that:

  - opens a persistent session per conversation (agent spec referenced by name),
  - streams a turn over SSE,
  - collects the assistant reply,
  - surfaces `tool.approval_required` pauses so the operator can Allow/Deny,
  - resumes with `user.tool_approval`.

No provider keys or tokens are logged here; errors are returned as plain messages.
"""
from __future__ import annotations

import http.client
import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("drhiro_bridge.trueforge")


class TrueForgeError(RuntimeError):
    pass


class ApprovalRequired(Exception):
    """A turn paused because a gated tool needs explicit human approval."""

    def __init__(self, pending: list[dict]) -> None:
        super().__init__("tool approval required")
        self.pending = pending  # list of {event, thread_id, tool_call_id, tool_name, args}


class TrueForgeClient:
    def __init__(
        self,
        base_url: str,
        agent_name: str,
        timeout: int = 600,
        api_base: str = "/api/v1",
    ) -> None:
        self._base = base_url.rstrip("/")
        self._api = f"{self._base}{api_base}"
        self._agent = agent_name
        self._timeout = timeout

    # ------------------------------------------------------------------ #
    # Low-level helpers
    # ------------------------------------------------------------------ #
    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self._api}/{path.lstrip('/')}"
        data = json.dumps(body or {}).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise TrueForgeError(f"TrueForge HTTP {e.code}: {e.read().decode('utf-8')[:200]}") from e
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            raise TrueForgeError(f"TrueForge unreachable: {e}") from e

    def health(self) -> dict:
        """Best-effort health probe. Returns status or a descriptive error."""
        try:
            with urllib.request.urlopen(f"{self._base}/healthz", timeout=10) as resp:
                return {"ok": resp.status == 200, "status": resp.status}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------ #
    # Sessions & turns
    # ------------------------------------------------------------------ #
    def create_session(self, agent_name: str | None = None) -> str:
        """Open a persistent TrueForge session on the named agent."""
        name = agent_name or self._agent
        result = self._request(
            "POST", "/sessions", {"agent": {"name": name}}
        )
        try:
            return result["data"]["id"]
        except (KeyError, TypeError) as e:
            raise TrueForgeError(f"unexpected create_session response: {result}") from e

    def run_turn(self, session_id: str, user_text: str) -> tuple[str, list[dict]]:
        """
        Run one user turn, streaming over SSE. Returns (reply, pending_approvals).

        If a gated tool pauses the turn, `pending_approvals` is populated and the
        caller must present them for human decision, then call resume_with_approvals.
        """
        body = {"input": [{"type": "user.message", "content": user_text}]}
        url = f"{self._api}/sessions/{session_id}/turns"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        )
        chunks: list[str] = []
        pending: list[dict] = []
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        evt = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    etype = evt.get("type")
                    if etype == "model.message.delta":
                        content = evt.get("content")
                        if isinstance(content, str):
                            chunks.append(content)
                    elif etype == "tool.approval_required":
                        pending.append(evt)
                    elif etype in ("turn.completed", "turn.done"):
                        pass
        except urllib.error.HTTPError as e:
            raise TrueForgeError(f"TrueForge turn HTTP {e.code}: {e.read().decode('utf-8')[:200]}") from e
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            raise TrueForgeError(f"TrueForge unreachable during turn: {e}") from e

        return "".join(chunks).strip(), pending

    def resume_with_approvals(
        self, session_id: str, approvals: list[dict]
    ) -> tuple[str, list[dict]]:
        """
        Resume a paused turn after the operator has decided each approval.

        `approvals` is a list of {thread_id, tool_call_id, status, reason?}.
        Returns the continuing reply and any further pending approvals.
        """
        body = {"input": approvals}
        url = f"{self._api}/sessions/{session_id}/turns"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        )
        chunks: list[str] = []
        pending: list[dict] = []
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        evt = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    etype = evt.get("type")
                    if etype == "model.message.delta":
                        content = evt.get("content")
                        if isinstance(content, str):
                            chunks.append(content)
                    elif etype == "tool.approval_required":
                        pending.append(evt)
        except urllib.error.HTTPError as e:
            raise TrueForgeError(f"TrueForge resume HTTP {e.code}") from e
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            raise TrueForgeError(f"TrueForge unreachable during resume: {e}") from e
        return "".join(chunks).strip(), pending

    def cancel(self, session_id: str) -> None:
        try:
            self._request("POST", f"/sessions/{session_id}/cancel", {})
        except TrueForgeError:
            log.debug("cancel failed (non-fatal)")
