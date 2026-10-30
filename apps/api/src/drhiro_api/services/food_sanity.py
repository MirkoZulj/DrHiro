"""LLM sanity check for online nutrition values.

Jev verifies that a candidate *name* matches the user's input. It says nothing
about the numbers attached to it, so a correct name with wrong values sails
through -- e.g. USDA "COFFEE" at 310 kcal/100 g (that is dry coffee, not the
brewed drink) or a DuckDuckGo scrape returning 832 kcal/100 g for coffee.

This asks the local LLM one bounded question: are these per-100 g values
plausible for this food? Fails open (returns True) on any error, so an LLM
outage degrades to the previous behaviour rather than blocking logging.
"""

from __future__ import annotations

import logging
import time

from drhiro_api.services.llm_client import chat_complete_sync

log = logging.getLogger(__name__)

# A sanity check is advisory: it must never hold a meal write open. The model
# box can be off or busy, so bound each call and, after one failure, skip the
# check for a cooldown instead of paying the timeout again for every item.
SANITY_TIMEOUT_S = 8.0
FAILURE_COOLDOWN_S = 120.0

_unavailable_until = 0.0

_SYSTEM = (
    "You sanity-check nutrition data. You are given a food name and its values "
    "per 100 g. Answer with only YES or NO: are those values plausible for that "
    "food? Answer NO when the numbers belong to a different form of the food "
    "(for example dry coffee instead of brewed, raw rice instead of cooked), or "
    "when they are physically impossible. Answer only YES or NO, nothing else."
)


def llm_values_plausible(
    name: str,
    kcal: float | None,
    protein: float | None,
    carbs: float | None,
    fat: float | None,
) -> bool:
    """True if the values look plausible for `name`. Fails open and fast."""
    global _unavailable_until

    if not name or kcal is None:
        return True
    if time.monotonic() < _unavailable_until:
        return True
    try:
        out = chat_complete_sync(
            [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Food: {name}\n"
                        f"Per 100 g: {kcal} kcal, {protein} g protein, "
                        f"{carbs} g carbs, {fat} g fat."
                    ),
                },
            ],
            temperature=0.0,
            timeout=SANITY_TIMEOUT_S,
        )
    except Exception:
        _unavailable_until = time.monotonic() + FAILURE_COOLDOWN_S
        log.warning(
            "LLM value sanity check unavailable for %r; skipping checks for %ss",
            name,
            int(FAILURE_COOLDOWN_S),
            exc_info=True,
        )
        return True

    verdict = (out or "").strip().upper()
    if verdict.startswith("NO"):
        log.info("LLM rejected values for %r: %s", name, (out or "").strip()[:80])
        return False
    return True