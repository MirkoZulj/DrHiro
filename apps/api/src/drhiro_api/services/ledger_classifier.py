"""Jev ledger classifier -- free-text routing and pre-write guard.

One Jev request carries SIX ``noul`` questions, one per ledger, and returns a
score per ledger. The verdict is used as

  (a) a ROUTER, when the deterministic parser in ``services/log_intents.py``
      finds nothing, to decide which ledger a free-text line belongs to, and
  (b) a GUARD, before a write, so a workout can never be filed as a meal -- the
      real incident this exists for logged 850 kcal of activity as food and
      silently lost two activity logs.

Labels and their destinations::

    consumption     -> meals (+ water via is_liquid/beverage_category downstream)
    activity        -> activities table
    weight          -> measurements.metric_type = 'weight'
    blood_pressure  -> measurements.metric_type = 'blood_pressure'
    heart_rate      -> measurements.metric_type = 'heart_rate'
    sleep           -> measurements.metric_type = 'sleep'

"Nothing loggable" (chatter such as "good morning") is represented by EVERY
score being low; there is no seventh label.

Food vs drink is deliberately NOT decided here. The resolution path already
chooses liquid vs solid from ``is_liquid`` / ``beverage_category``; this
classifier stops at "consumption".

English-first: Jev scores Croatian poorly, so text is normalised to English
through the existing food-translation helper before classification. The
translation is best-effort and skipped for plain-ASCII text with no known
foreign token, so an already-English log pays no extra model latency. A
limitation remains for foreign text whose words are neither in the food
glossary nor non-ASCII; it is classified as-is.

The guard FAILS OPEN: when Jev is unconfigured or unreachable a log proceeds
exactly as it did before this module existed. It is disableable by one config
flag (``DRHIRO_JEV_LEDGER_GUARD_ENABLED=false``).
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

import httpx

from drhiro_api.config import get_settings

log = logging.getLogger(__name__)

#: The six ledgers, in a stable order.
LEDGERS: tuple[str, ...] = (
    "consumption", "activity", "weight", "blood_pressure", "heart_rate", "sleep",
)

#: Human-facing names, used to build the "which did you mean?" payload.
LEDGER_NAMES: dict[str, str] = {
    "consumption": "a meal (food/drink)",
    "activity": "an activity (workout, calories burned)",
    "weight": "a weight measurement",
    "blood_pressure": "a blood-pressure measurement",
    "heart_rate": "a heart-rate measurement",
    "sleep": "a sleep record",
}

#: Which shipped tool writes each ledger, for the router's routing hint.
LEDGER_TOOLS: dict[str, str] = {
    "consumption": "create_meal_from_text",
    "activity": "log_activity",
    "weight": "create_manual_weight",
    "blood_pressure": "create_manual_bp",
    "heart_rate": "create_manual_bp",
    "sleep": "log_measurement",
}

#: Ask shape (calibrated 2026-10-05; winners score 0.91-0.99, wrong labels
#: 0.01-0.19, chatter max 0.03 -- so the existing 0.88 threshold is unchanged).
_ASK = {
    "consumption": (
        "The user message reports food or drink the user ate or drank -- meals, "
        "snacks, individual ingredients, or any calorie intake.",
        {"true": "The message names any food, drink, ingredient or meal, or "
                 "reports eating, drinking or calorie intake.",
         "false": "The message reports no food or drink and no eating or drinking."},
    ),
    "activity": (
        "The user message reports physical activity or exercise the user did, "
        "such as a workout, sport, or calories burned.",
        {"true": "The message reports performing an exercise, sport, workout or "
                 "physical activity, or calories burned by activity.",
         "false": "The message does not report any physical activity or exercise."},
    ),
    "weight": (
        "The user message reports the user's body weight.",
        {"true": "The message reports a body weight measurement.",
         "false": "The message does not report body weight."},
    ),
    "blood_pressure": (
        "The user message reports a blood pressure reading.",
        {"true": "The message reports a blood pressure measurement (systolic "
                 "over diastolic).",
         "false": "The message does not report blood pressure."},
    ),
    "heart_rate": (
        "The user message reports a heart rate or pulse reading.",
        {"true": "The message reports a heart rate or pulse measurement.",
         "false": "The message does not report heart rate."},
    ),
    "sleep": (
        "The user message reports sleep.",
        {"true": "The message reports how long the user slept or a sleep record.",
         "false": "The message does not report sleep."},
    ),
}


@dataclass
class LedgerVerdict:
    label: str | None            # winning ledger, or None when nothing scores high enough
    score: float | None          # winner's noul
    runner_up: str | None
    runner_up_score: float | None
    margin: float | None         # score - runner_up_score
    scores: dict[str, float | None]
    available: bool              # False when Jev is unconfigured/unreachable
    reason: str | None           # "unavailable" | "no_ledger" | "classified"

    def as_dict(self) -> dict:
        return asdict(self)


_WARNED: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        log.warning(message)


def _needs_translation(text: str) -> bool:
    """True only when a non-ASCII character is present or a food-glossary
    token matches. Keeps already-English input off the translation model."""
    if any(ord(c) > 127 for c in text):
        return True
    try:
        from drhiro_api.services.food_translate import _glossary_lookup
        return _glossary_lookup(text) is not None
    except Exception:
        return False


def _to_english(text: str) -> str:
    """Best-effort English normalisation, reusing the food translation helper.

    Never raises; falls back to the input.
    """
    if not _needs_translation(text):
        return text
    try:
        from drhiro_api.services.food_translate import translate_food_to_english
        out = translate_food_to_english(text)
        return out or text
    except Exception:
        log.exception("ledger-classifier translation failed; using raw text")
        return text


def _classify_scores(text: str) -> dict[str, float | None] | None:
    """ONE Jev request with six noul questions. This is the test seam.

    Returns a ``{label: noul|None}`` map on a successful call, or ``None`` when
    Jev is unconfigured or the call fails -- never raises.
    """
    s = get_settings()
    if not s.jev_api_key:
        return None
    questions = {
        label: {"type": "noul", "instructions": instr, "criteria": crit}
        for label, (instr, crit) in _ASK.items()
    }
    url = s.jev_api_url.rstrip("/") + "/v1/systemone"
    payload = {
        "model": s.jev_model,
        "state": f'User message: "{text}".',
        "questions": questions,
    }
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                url, json=payload,
                headers={"Authorization": f"Bearer {s.jev_api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        log.exception("Jev ledger classification call failed")
        return None
    answers = data.get("answers") or {}
    scores: dict[str, float | None] = {}
    for label in LEDGERS:
        ans = answers.get(label) or {}
        value = ans.get("noul") if ans.get("type") == "noul" else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            value = None
        elif not (0.0 <= float(value) <= 1.0):
            value = None
        scores[label] = float(value) if value is not None else None
    if all(v is None for v in scores.values()):
        # A response with no usable noul is indistinguishable from unavailable.
        return None
    return scores


def _argmax(scores: dict[str, float | None]):
    ranked = sorted(
        ((k, v) for k, v in scores.items() if v is not None),
        key=lambda kv: kv[1], reverse=True,
    )
    if not ranked:
        return None, None, None, None
    best, best_score = ranked[0]
    runner, runner_score = ranked[1] if len(ranked) > 1 else (None, None)
    return best, best_score, runner, runner_score


def classify_ledger(text: str) -> LedgerVerdict:
    """Classify one free-text log line into a ledger. Never raises."""
    s = get_settings()
    empty = {label: None for label in LEDGERS}
    raw = "" if text is None else str(text).strip()
    if not raw:
        return LedgerVerdict(None, None, None, None, None, empty, True, "no_ledger")

    scores = _classify_scores(_to_english(raw))
    if scores is None:
        return LedgerVerdict(None, None, None, None, None, empty, False, "unavailable")

    best, best_score, runner, runner_score = _argmax(scores)
    if best is None:
        return LedgerVerdict(None, None, None, None, None, scores, True, "no_ledger")
    margin = (best_score - runner_score) if (runner_score is not None) else best_score

    if best_score >= s.jev_threshold:
        return LedgerVerdict(best, best_score, runner, runner_score, margin, scores,
                             True, "classified")
    return LedgerVerdict(None, best_score, runner, runner_score, margin, scores,
                         True, "no_ledger")


@dataclass
class GuardDecision:
    action: str                  # proceed | review | block | no_ledger | fail_open
    target_ledger: str
    reason: str
    message: str
    verdict: LedgerVerdict | None = None
    which_did_you_mean: dict | None = None

    def as_dict(self) -> dict:
        out = {
            "action": self.action,
            "target_ledger": self.target_ledger,
            "reason": self.reason,
            "message": self.message,
            "verdict": self.verdict.as_dict() if self.verdict else None,
        }
        if self.which_did_you_mean:
            out["which_did_you_mean"] = self.which_did_you_mean
        return out


def guard_write(text: str, target_ledger: str) -> GuardDecision:
    """Compare the classifier's verdict with the ledger about to be written.

    Rules (spec section 6b):
      * target score >= threshold           -> proceed silently
      * target < threshold and another
        label >= threshold                    -> block (which did you mean?)
      * review_floor <= target < threshold  -> proceed, flag needs_review
      * everything below review_floor       -> no_ledger (do not log; ask)
      * Jev unavailable                     -> fail open, warn once

    Never drops or rewrites a log silently. Never raises.
    """
    s = get_settings()
    if not s.jev_ledger_guard_enabled:
        return GuardDecision("proceed", target_ledger, "disabled",
                             "Ledger guard disabled by configuration.", None)

    verdict = classify_ledger(text)
    if not verdict.available:
        _warn_once(
            "ledger_guard_unavailable",
            "ledger guard unavailable (Jev unconfigured/unreachable); "
            "logging proceeds unchanged (fail-open).",
        )
        return GuardDecision("fail_open", target_ledger, "unavailable",
                             "Ledger guard unavailable; logging proceeded unchanged.",
                             verdict)

    scores = verdict.scores
    target_score = scores.get(target_ledger) or 0.0
    best, best_score, _runner, _rs = _argmax(scores)

    if target_score >= s.jev_threshold:
        return GuardDecision("proceed", target_ledger, "target_confident",
                             "", verdict)

    if best is not None and best != target_ledger and (best_score or 0.0) >= s.jev_threshold:
        return _block_decision(target_ledger, best, verdict)

    if target_score >= s.jev_review_floor:
        return GuardDecision(
            "review", target_ledger, "low_confidence",
            f"Low confidence this is {LEDGER_NAMES.get(target_ledger, target_ledger)} "
            f"(score {target_score:.2f}); proceeding marked needs_review.",
            verdict)

    if best is not None and best != target_ledger and (best_score or 0.0) >= s.jev_review_floor:
        return _block_decision(target_ledger, best, verdict)

    return GuardDecision(
        "no_ledger", target_ledger, "no_ledger",
        "This does not look like a log line; ask the user what they meant "
        "rather than inventing a record.",
        verdict)


def _block_decision(target_ledger: str, suggested: str, verdict: LedgerVerdict) -> GuardDecision:
    target_name = LEDGER_NAMES.get(target_ledger, target_ledger)
    suggested_name = LEDGER_NAMES.get(suggested, suggested)
    return GuardDecision(
        "block", target_ledger, "ledger_disagreement",
        f"This looks like {suggested_name}, not {target_name}. Ask the user "
        f"which they meant before logging anything.",
        verdict,
        which_did_you_mean={
            "target_ledger": target_ledger,
            "target_name": target_name,
            "suggested_ledger": suggested,
            "suggested_name": suggested_name,
            "suggested_tool": LEDGER_TOOLS.get(suggested),
            "scores": verdict.scores,
        },
    )
