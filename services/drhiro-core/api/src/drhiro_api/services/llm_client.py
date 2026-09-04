"""Minimal OpenAI-compatible chat client (httpx, no SDK dependency).

Used by background jobs to extract reusable food-resolution rules when a user
corrects a meal item. Async core + sync wrapper because RQ workers run sync.
"""

from __future__ import annotations

import logging

import httpx

from drhiro_api.config import get_settings

log = logging.getLogger(__name__)


async def chat_complete(messages: list[dict], temperature: float = 0.1) -> str:
    """POST /chat/completions and return the assistant message content."""
    s = get_settings()
    if not s.llm_api_key:
        raise RuntimeError("DRHIRO_LLM_API_KEY is not configured")
    url = s.llm_api_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": s.llm_model,
        "messages": messages,
        "temperature": temperature,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {s.llm_api_key}"},
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
