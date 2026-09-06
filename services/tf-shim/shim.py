"""
TrueForge → OpenAI-compatible shim.

OpenClaw points at this service as its model provider. We accept
/v1/chat/completions, map the conversation to a persistent TrueForge
session, run a turn against the `drhiro` agent, and return the reply
shaped as an OpenAI completion.

OpenClaw keeps Telegram/WhatsApp transport. TrueForge does the thinking.
"""
import asyncio
import hashlib
import json
import os
import re
import time
import uuid

import httpx
import redis.asyncio as aioredis
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

TRUEFORGE_URL = os.environ.get("TRUEFORGE_URL", "http://trueforge:8790")
AGENT_NAME = os.environ.get("TRUEFORGE_AGENT", "drhiro")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/3")
MODEL_ID = os.environ.get("SHIM_MODEL_ID", "trueforge-drhiro")
TURN_TIMEOUT = int(os.environ.get("TURN_TIMEOUT", "600"))
SESSION_TTL = int(os.environ.get("SESSION_TTL", str(60 * 60 * 24 * 90)))

_redis = None


async def redis_conn():
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


# ---------------------------------------------------------------------------
# Conversation identity
# ---------------------------------------------------------------------------

def conversation_key(body: dict) -> str:
    """Stable key for this conversation.

    Preference order:
      1. explicit `user` field (OpenAI standard, OpenClaw may set it)
      2. metadata.chat_id / metadata.session_id if present
      3. hash of the FIRST user message — stable for the life of a thread
         because OpenClaw replays full history on every turn
    """
    user = body.get("user")
    if isinstance(user, str) and user.strip():
        return f"user:{user.strip()}"

    meta = body.get("metadata") or {}
    for field in ("chat_id", "session_id", "conversation_id", "thread_id"):
        val = meta.get(field)
        if isinstance(val, (str, int)) and str(val).strip():
            return f"meta:{field}:{val}"

    for m in body.get("messages", []):
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, list):  # multimodal parts
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            if content:
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]
                return f"first:{digest}"

    return "anon:default"


# Markers identifying OpenClaw's runtime-context envelope. It arrives as its
# own user-role message and is NOT authored by the user.
_RUNTIME_CONTEXT_MARKERS = (
    "BEGIN_OPENCLAW_INTERNAL_CONTEXT",
    "END_OPENCLAW_INTERNAL_CONTEXT",
    "OpenClaw runtime context for the immediately preceding user message",
)


def _is_runtime_context(text: str) -> bool:
    """True when this message is OpenClaw's context envelope, not user words."""
    if not text:
        return False
    return any(marker in text for marker in _RUNTIME_CONTEXT_MARKERS)


def _message_text(m: dict) -> str:
    content = m.get("content")
    if isinstance(content, list):
        return " ".join(
            p.get("text", "") for p in content if isinstance(p, dict)
        ).strip()
    if isinstance(content, str):
        return content.strip()
    return ""


def latest_user_message(body: dict) -> str:
    """The newest genuinely user-authored turn.

    OpenClaw appends a runtime-context envelope as a separate user message.
    Returning that instead of the real text made the agent answer whichever
    stale turn appeared in the envelope's embedded conversation history, so
    those envelopes are skipped. TrueForge keeps its own history regardless.
    """
    messages = body.get("messages", [])
    fallback = ""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        text = _message_text(m)
        if not text:
            continue
        if _is_runtime_context(text):
            fallback = fallback or text
            continue
        return text
    # Every user message was an envelope: better to forward something than
    # nothing, but strip the embedded history so stale turns cannot be answered.
    if fallback:
        head = fallback.split("Conversation context")[0]
        return head.strip() or fallback
    return ""


# ---------------------------------------------------------------------------
# TrueForge plumbing
# ---------------------------------------------------------------------------

async def get_or_create_session(key: str) -> str:
    r = await redis_conn()
    redis_key = f"tfshim:session:{key}"
    existing = await r.get(redis_key)

    if existing:
        # Confirm it still exists server-side; TrueForge may have been reset.
        async with httpx.AsyncClient(timeout=20) as c:
            probe = await c.get(f"{TRUEFORGE_URL}/api/v1/sessions/{existing}/turns")
            if probe.status_code == 200:
                await r.expire(redis_key, SESSION_TTL)
                return existing

    async with httpx.AsyncClient(timeout=30) as c:
        resp = await c.post(
            f"{TRUEFORGE_URL}/api/v1/sessions",
            json={"agent": {"name": AGENT_NAME}},
        )
        resp.raise_for_status()
        session_id = resp.json()["data"]["id"]

    await r.set(redis_key, session_id, ex=SESSION_TTL)
    return session_id


async def stash_user_text(text: str) -> None:
    """Record the user's raw words for the MCP layer.

    Qwen paraphrases when it calls tools and frequently drops the day words
    ("On Monday for dinner ..." becomes "200g chicken and 150g rice"), which
    would silently log the meal against today. The MCP server reads this key to
    recover the date phrase, so correctness does not depend on the model
    faithfully echoing the sentence.
    """
    try:
        r = await redis_conn()
        await r.set("tfshim:last_user_text", text, ex=900)
    except Exception:
        pass


async def run_turn(session_id: str, text: str) -> str:
    """POST a turn and accumulate the streamed assistant reply."""
    await stash_user_text(text)
    chunks: list[str] = []
    finished = False

    async with httpx.AsyncClient(timeout=TURN_TIMEOUT) as c:
        async with c.stream(
            "POST",
            f"{TRUEFORGE_URL}/api/v1/sessions/{session_id}/turns",
            json={"input": [{"type": "user.message", "content": text}]},
            headers={"Accept": "text/event-stream"},
        ) as stream:
            async for line in stream.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    evt = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                if evt.get("type") == "model.message.delta":
                    piece = evt.get("content")
                    if isinstance(piece, str):
                        chunks.append(piece)
                    if evt.get("finish_reason"):
                        finished = True
                elif evt.get("type") in ("turn.completed", "turn.done"):
                    finished = True

    reply = "".join(chunks).strip()

    # Fallback: the stream can be cut before the final assembly lands.
    if not reply or not finished:
        polled = await poll_last_output(session_id)
        if polled:
            reply = polled

    if not reply:
        reply = await fetch_pending_question(session_id)
    return reply or "I could not retrieve that just now. Please try again in a moment."


async def fetch_pending_question(session_id: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            resp = await c.get(f"{TRUEFORGE_URL}/api/v1/sessions/{session_id}/events?limit=12")
            if resp.status_code != 200:
                return ""
            for x in reversed(resp.json().get("data", [])):
                ev = x.get("event") or {}
                if ev.get("type") == "model.message":
                    tc = ev.get("tool_calls") or []
                    if any((t.get("function") or {}).get("name") == "ask_user_question" for t in tc):
                        txt = (ev.get("content") or "").strip()
                        for t2 in tc:
                            fn = (t2.get("function") or {})
                            if fn.get("name") == "ask_user_question":
                                try:
                                    a = json.loads(fn.get("arguments") or "{}")
                                    opts = a.get("options") or a.get("choices") or []
                                    if isinstance(opts, list) and opts:
                                        txt += "\n\n" + "\n".join(f"- {o}" if isinstance(o, str) else f"- {json.dumps(o, ensure_ascii=False)}" for o in opts)
                                    q2 = a.get("question") or a.get("question_text")
                                    if q2 and q2 not in txt:
                                        txt = q2 + "\n\n" + txt
                                except Exception:
                                    pass
                        if txt:
                            return txt
    except Exception:
        pass
    return ""


async def poll_last_output(session_id: str) -> str:
    deadline = time.time() + 180
    async with httpx.AsyncClient(timeout=30) as c:
        while time.time() < deadline:
            resp = await c.get(f"{TRUEFORGE_URL}/api/v1/sessions/{session_id}/turns")
            if resp.status_code != 200:
                return ""
            turns = resp.json().get("data", [])
            if turns:
                state = turns[-1].get("state", {})
                status = state.get("status")
                if status == "done":
                    out = state.get("output") or {}
                    return (out.get("content") or "").strip()
                if status in ("failed", "error"):
                    return ""
            await asyncio.sleep(5)
    return ""


# ---------------------------------------------------------------------------
# OpenAI-shaped surface
# ---------------------------------------------------------------------------

"""Build completion envelope; attach Telegram buttons when reply flags mismatches."""

def build_fix_buttons(reply: str):
    if 'mismatch' not in reply.lower() and 'instead of' not in reply.lower() and 'wrong' not in reply.lower():
        return None
    buttons = []
    for m in re.finditer(r'([A-Za-z ,\'-]{3,30}?)\s*(?:matched to|instead of|->|\u2192)\s*([A-Za-z ,\'-]{3,40})', reply):
        wrong, right = m.group(1).strip(' ,-'), m.group(2).strip(' ,-')
        if wrong and right and wrong.lower() != right.lower():
            buttons.append({'label': ('Fix: ' + right)[:60], 'action': {'type': 'callback', 'value': 'fix_meal:' + wrong + '|' + right}})
    if not buttons:
        return None
    return {'blocks': [{'type': 'buttons', 'buttons': buttons[:4]}]}

def completion_envelope(reply: str, model: str) -> dict:
    now = int(time.time())
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": now,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "presentation": build_fix_buttons(reply),
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": {"message": "invalid JSON body", "type": "invalid_request_error"}},
            status_code=400,
        )

    text = latest_user_message(body)
    if not text:
        return JSONResponse(
            {"error": {"message": "no user message found", "type": "invalid_request_error"}},
            status_code=400,
        )

    key = conversation_key(body)
    model = body.get("model") or MODEL_ID

    try:
        session_id = await get_or_create_session(key)
        reply = await run_turn(session_id, text)
    except Exception as e:  # surface the failure to OpenClaw rather than hanging
        return JSONResponse(
            {"error": {"message": f"trueforge error: {e}", "type": "server_error"}},
            status_code=502,
        )

    envelope = completion_envelope(reply, model)

    if not body.get("stream"):
        return JSONResponse(envelope)

    # Streaming form: single content chunk then terminator. OpenClaw is happy
    # with this and it keeps the client from timing out on its own reader.
    def sse():
        created = envelope["created"]
        cid = envelope["id"]
        head = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": reply}, "finish_reason": None}
            ],
        }
        yield f"data: {json.dumps(head)}\n\n"
        tail = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(tail)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream")


async def list_models(request: Request):
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {
                    "id": MODEL_ID,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "trueforge",
                }
            ],
        }
    )


async def healthz(request: Request):
    detail = {"ok": True, "trueforge": TRUEFORGE_URL, "agent": AGENT_NAME}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{TRUEFORGE_URL}/healthz")
            detail["trueforge_status"] = r.status_code
    except Exception as e:
        detail["ok"] = False
        detail["trueforge_error"] = str(e)
    try:
        r = await redis_conn()
        await r.ping()
        detail["redis"] = "ok"
    except Exception as e:
        detail["ok"] = False
        detail["redis_error"] = str(e)
    return JSONResponse(detail, status_code=200 if detail["ok"] else 503)


async def sessions_debug(request: Request):
    r = await redis_conn()
    keys = [k async for k in r.scan_iter("tfshim:session:*")]
    out = {}
    for k in keys[:100]:
        out[k] = await r.get(k)
    return JSONResponse({"count": len(keys), "sessions": out})


app = Starlette(
    routes=[
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        Route("/v1/models", list_models, methods=["GET"]),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/debug/sessions", sessions_debug, methods=["GET"]),
    ]
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "3200")))
