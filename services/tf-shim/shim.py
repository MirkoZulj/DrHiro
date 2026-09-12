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

import drhiro_event_envelope as eventenv

TRUEFORGE_URL = os.environ.get("TRUEFORGE_URL", "http://trueforge:8790")
AGENT_NAME = os.environ.get("TRUEFORGE_AGENT", "drhiro")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/3")
MODEL_ID = os.environ.get("SHIM_MODEL_ID", "trueforge-drhiro")
TURN_TIMEOUT = int(os.environ.get("TURN_TIMEOUT", "600"))
SESSION_TTL = int(os.environ.get("SESSION_TTL", str(60 * 60 * 24 * 90)))

# R2 trusted-config identity. DRHIRO_BOT_ID is the VERIFIED Telegram bot id
# (getMe.id) mapped from the channel account at provisioning. DRHIRO_EVENT_SECRET
# is the HMAC signing key shared only with the minting adapter; it must live in
# secret management, NEVER in source control or prompts.
SERVICE = os.environ.get("DRHIRO_SERVICE", "drhiro")
DRHIRO_BOT_ID = os.environ.get("DRHIRO_BOT_ID", "")
EVENT_SECRET = os.environ.get("DRHIRO_EVENT_SECRET", "")
EVENT_MAX_AGE_S = int(os.environ.get("DRHIRO_EVENT_MAX_AGE_S", "300"))
EVENT_RECORD_TTL = int(os.environ.get("DRHIRO_EVENT_RECORD_TTL", str(60 * 60 * 24 * 7)))

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


async def stash_user_text(text: str, conversation_id: str = "") -> None:
    """Record the user's raw words for the MCP layer, scoped per conversation.

    Qwen paraphrases when it calls tools and frequently drops the day words
    ("On Monday for dinner ..." becomes "200g chicken and 150g rice"), which
    would silently log the meal against today. The MCP server reads this key to
    recover the date phrase, so correctness does not depend on the model
    faithfully echoing the sentence.

    conversation_id is the shim's stable conversation key. If empty/missing,
    we DO NOT WRITE AT ALL — a global fallback would leak state across
    concurrent users.
    """
    try:
        r = await redis_conn()
        if not conversation_id:
            return  # fail-safe: never fall back to a global key
        await r.set(f"tfshim:last_user_text:{conversation_id}", text, ex=900)
    except Exception:
        pass


async def bind_event(event_id: str, *, input_digest: str, issued_at: int,
                     bot_id: str, chat_id: str, message_id: str) -> None:
    """Persist durable, run-scoped trusted context keyed by event_id.

    NOT a shared 'latest event' record: each event has its own key, so
    concurrent turns cannot overwrite one another. The record outlives the
    envelope (EVENT_RECORD_TTL >> EVENT_MAX_AGE_S) so a fresh authenticated
    retry of an old event still resolves its durable result even after the
    envelope credentials expired. The MCP/API resolve event context from this
    record (run-scoped transport); identity never arrives as model args.
    """
    r = await redis_conn()
    payload = json.dumps({
        "event_id": event_id,
        "service": SERVICE,
        "bot_id": bot_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "input_digest": input_digest,
        "issued_at": issued_at,
        "bound_at": int(time.time()),
    })
    await r.set(f"tfshim:event:{event_id}", payload, ex=EVENT_RECORD_TTL)


async def _store_result(event_id: str, result: dict) -> None:
    """Persist a completed model turn result keyed by event_id.

    A later authenticated retry of the same event (e.g. due to a network
    timeout) can replay this stored result instead of re-running the model
    turn, avoiding duplicate side-effects.

    IMPORTANT: this runs AFTER the model turn has completed. A cache-write
    failure here must NOT be converted into an error response — doing so
    would invite a client retry that re-runs the turn and double-executes
    side-effects. Failures are logged and swallowed; only the dedup replay
    on retry is lost.
    """
    try:
        r = await redis_conn()
        payload = json.dumps(result)
        await r.set(f"tfshim:result:{event_id}", payload, ex=EVENT_RECORD_TTL)
    except Exception as e:
        print(f"[_store_result] cache store failed for {event_id}: {e!r}", flush=True)


async def _get_stored_result(event_id: str) -> dict | None:
    """Return a previously stored model turn result, or None if not present.

    A cache outage during lookup is treated as "no cached result" so the
    request proceeds to run the model turn rather than surfacing an
    unhandled error to the client.
    """
    try:
        r = await redis_conn()
        raw = await r.get(f"tfshim:result:{event_id}")
    except Exception as e:
        print(f"[_get_stored_result] cache lookup failed for {event_id}: {e!r}", flush=True)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


async def run_turn(session_id: str, text: str, conversation_id: str = "") -> str:
    """POST a turn and accumulate the streamed assistant reply."""
    await stash_user_text(text, conversation_id=conversation_id)
    chunks: list[str] = []
    finished = False

    # Carry the conversation id into the turn so the agent can pass it to
    # tools via their conversation_id argument. This is the only conduit
    # from the shim to the MCP server (the model's tool-call arguments).
    turn_input = [{"type": "user.message", "content": text}]
    if conversation_id:
        turn_input.append({"type": "context", "content": f"drhiro_conversation_id={conversation_id}"})

    async with httpx.AsyncClient(timeout=TURN_TIMEOUT) as c:
        async with c.stream(
            "POST",
            f"{TRUEFORGE_URL}/api/v1/sessions/{session_id}/turns",
            json={"input": turn_input},
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

    # ---- R2: extract, verify, and REMOVE the trusted event envelope before
    # anything is forwarded to the model. This is the adapter seam. If the
    # trusted config (bot id / signing key) is missing we FAIL CLOSED: a
    # consumption event must never be processed without verified identity.
    if not DRHIRO_BOT_ID or not EVENT_SECRET:
        return JSONResponse(
            {"error": {"message": "event identity not configured; failing closed",
                       "type": "server_error"}},
            status_code=503,
        )

    try:
        bound, cleaned = eventenv.extract_and_remove_envelope(
            body,
            secret=EVENT_SECRET.encode("utf-8"),
            service=SERVICE,
            bot_id=DRHIRO_BOT_ID,
            now=int(time.time()),
            max_age_s=EVENT_MAX_AGE_S,
        )
    except eventenv.NoEnvelopeError:
        return JSONResponse(
            {"error": {"message": "no event envelope in the designated block; refusing to log",
                       "type": "invalid_request_error"}},
            status_code=422,
        )
    except eventenv.EnvelopeError as e:
        return JSONResponse(
            {"error": {"message": f"event envelope rejected: {e.code}",
                       "type": "invalid_request_error"}},
            status_code=422,
        )

    # Use the CLEANED body so the envelope is never part of what reaches the
    # model, the conversation history, or any prompt log.
    text = latest_user_message(cleaned)
    if not text:
        return JSONResponse(
            {"error": {"message": "no user message found", "type": "invalid_request_error"}},
            status_code=400,
        )

    key = conversation_key(cleaned)
    model = body.get("model") or MODEL_ID

    # Durable, run-scoped binding (per-event key, not a shared 'latest' slot).
    try:
        await bind_event(
            bound.event_id, input_digest=bound.input_digest, issued_at=bound.issued_at,
            bot_id=bound.bot_id, chat_id=bound.chat_id, message_id=bound.message_id,
        )
    except Exception:
        return JSONResponse(
            {"error": {"message": "event binding failed; refusing to log",
                       "type": "server_error"}},
            status_code=503,
        )

    # Dedup: if we already completed this event, return the cached result.
    # This prevents a retry (after a timeout, for example) from re-running
    # the model turn and possibly causing duplicate side-effects.
    cached = await _get_stored_result(bound.event_id)
    if cached is not None:
        if not body.get("stream"):
            return JSONResponse(cached)
        # Streaming replay would be complex; just return non-stream for cached
        return JSONResponse(cached)

    try:
        session_id = await get_or_create_session(key)
        reply = await run_turn(session_id, text, conversation_id=key)
    except Exception as e:  # surface the failure to OpenClaw rather than hanging
        return JSONResponse(
            {"error": {"message": f"trueforge error: {e}", "type": "server_error"}},
            status_code=502,
        )

    envelope = completion_envelope(reply, model)

    # Store the completed result so a retry can replay it.
    await _store_result(bound.event_id, envelope)

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
