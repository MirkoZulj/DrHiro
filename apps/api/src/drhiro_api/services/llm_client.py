"""Minimal OpenAI-compatible chat client (httpx, no SDK dependency).

Used by background jobs to extract reusable food-resolution rules when a user
corrects a meal item. Async core + sync wrapper because RQ workers run sync.
"""

from __future__ import annotations

import logging
import os

import httpx

from drhiro_api.config import get_settings

log = logging.getLogger(__name__)


def _runtime_llm() -> tuple[str, str, str]:
    """Return (url, api_key, model) — store-first, .env fallback.

    Prefers the runtime settings store (app_settings row) so a deployer's
    Settings-screen change takes effect without editing .env; falls back to
    the bootstrap env when the store is empty/unreachable. Never logs values.
    """
    url = os.environ.get("DRHIRO_LLM_API_URL") or ""
    key = os.environ.get("DRHIRO_LLM_API_KEY") or ""
    model = os.environ.get("DRHIRO_LLM_MODEL") or ""
    # Try the DB store when a session is available. get_db is a generator
    # dependency; here we open a short-lived session for the read.
    try:
        from drhiro_api.db import SessionLocal
        from drhiro_api.services.settings_store import resolve_runtime

        with SessionLocal() as db:
            eff = resolve_runtime(db, dict(os.environ))
        if eff.get("ai_backend_url"):
            url = eff["ai_backend_url"]
        if eff.get("ai_api_key"):
            key = eff["ai_api_key"]
        if eff.get("model_name"):
            model = eff["model_name"]
    except Exception as e:  # DB not ready/configured -> env fallback
        log.debug("runtime settings store unavailable, using env: %s", e)
    if not url:
        url = get_settings().llm_api_url
    if not key:
        key = get_settings().llm_api_key
    if not model:
        model = get_settings().llm_model
    return url, key, model


async def chat_complete(messages: list[dict], temperature: float = 0.1) -> str:
    """POST /chat/completions and return the assistant message content."""
    url, api_key, model = _runtime_llm()
    if not api_key:
        raise RuntimeError("AI API key is not configured")
    request_url = url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            request_url,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()
    return data["choices"][0]["message"]["content"]


def chat_complete_sync(messages: list[dict], temperature: float = 0.1) -> str:
    """Sync wrapper for RQ workers / other non-async contexts."""
    import asyncio

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Nested loop (rare): run in a fresh one on a worker thread.
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                return pool.submit(
                    asyncio.run, chat_complete(messages, temperature)
                ).result()
        return loop.run_until_complete(chat_complete(messages, temperature))
    except RuntimeError:
        return asyncio.run(chat_complete(messages, temperature))
