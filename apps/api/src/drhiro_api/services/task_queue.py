"""Fire-and-forget RQ enqueueing from the API process.

The API only needs redis + rq installed; job functions are referenced by
dotted path so the API never imports the worker package. Any failure is
logged and swallowed — a failed rule extraction must never fail the PATCH.
"""

from __future__ import annotations

import logging

from drhiro_api.config import get_settings

log = logging.getLogger(__name__)


def enqueue(queue_name: str, func_ref: str, *args):
    """Enqueue func_ref(*args) on an RQ queue. Returns job id or None."""
    try:
        from redis import Redis
        from rq import Queue

        conn = Redis.from_url(get_settings().redis_url)
        job = Queue(queue_name, connection=conn).enqueue(func_ref, *args)
        log.info("enqueued %s on %s as %s", func_ref, queue_name, job.id)
        return job.id
    except Exception:
        log.exception("enqueue failed for %s — skipping", func_ref)
        return None
