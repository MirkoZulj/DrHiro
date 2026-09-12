"""TrueForge-equivalent stub for the R2 integration harness.

Faithfully emulates the TrueForge surface the shim talks to:
  POST   /api/v1/sessions                     -> create a session
  GET    /api/v1/sessions/{id}/turns          -> list turns (poll fallback)
  POST   /api/v1/sessions/{id}/turns          -> run a turn (SSE stream)
  GET    /api/v1/sessions/{id}/events         -> fetch_pending_question probe
  GET    /healthz
  GET    /debug/received                       -> what the shim forwarded to us

The turn handler records the EXACT `text` it received (so the harness can prove
the envelope was removed before forwarding) and streams a canned reply shaped
like TrueForge's events. The model never receives the envelope.
"""
from __future__ import annotations

import json
import time
import uuid

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

RECEIVED_TURNS: list[dict] = []
SESSIONS: dict[str, dict] = {}


def _sid(request: Request) -> str:
    return str(request.path_params.get("id", ""))


async def create_session(request: Request):
    sid = uuid.uuid4().hex
    SESSIONS[sid] = {"turns": []}
    return JSONResponse({"data": {"id": sid}})


async def list_turns(request: Request):
    sid = _sid(request)
    if sid not in SESSIONS:
        return JSONResponse({"data": []})
    return JSONResponse({"data": [{"state": {"status": "done", "output": {"content": "ok"}}}]})


async def events(request: Request):
    return JSONResponse({"data": []})


async def run_turn(request: Request):
    sid = _sid(request)
    body = await request.json()
    inp = body.get("input") or []
    text = ""
    for i in inp:
        if i.get("type") == "user.message":
            text = i.get("content", "")
    RECEIVED_TURNS.append({"session_id": sid, "text": text, "has_envelope": "DRHIRO_EVENT_CONTEXT" in str(text)})
    reply = "Logged your meal."
    created = int(time.time())
    cid = f"cmpl-{uuid.uuid4().hex[:12]}"

    def sse():
        half = len(reply) // 2 or 1
        for chunk in [reply[:half], reply[half:]]:
            yield f"data: {json.dumps({'id': cid, 'created': created, 'type': 'model.message.delta', 'content': chunk})}\n\n"
        yield f"data: {json.dumps({'id': cid, 'created': created, 'type': 'model.message.delta', 'content': '', 'finish_reason': 'stop'})}\n\n"
        yield f"data: {json.dumps({'id': cid, 'created': created, 'type': 'turn.completed'})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")


async def healthz(request: Request):
    return JSONResponse({"ok": True})


async def debug_received(request: Request):
    return JSONResponse({"turns": RECEIVED_TURNS})


app = Starlette(routes=[
    Route("/api/v1/sessions", create_session, methods=["POST"]),
    Route("/api/v1/sessions/{id}/turns", run_turn, methods=["POST"]),
    Route("/api/v1/sessions/{id}/turns", list_turns, methods=["GET"]),
    Route("/api/v1/sessions/{id}/events", events, methods=["GET"]),
    Route("/debug/received", debug_received, methods=["GET"]),
    Route("/healthz", healthz, methods=["GET"]),
])

if __name__ == "__main__":
    import os
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("TF_PORT", "8791")))
