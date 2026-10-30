# Jev ledger classifier — spec (free-text routing guard)

**Status:** approved by Kresimir 2026-10-05 ("Can we use jev to classify something as food, drink, activity,
weight, bp, hr, etc?" → "Do it"). Corrected by him: Jev already differentiates food from drink downstream, so
this classifier must NOT try to.

**Owner:** Jeeves (spec, verification) → Jerry (implementation)

---

## 1. Goal

Decide which LEDGER a free-text message belongs to, using Jev, and use that decision as (a) a router when the
deterministic parser finds nothing and (b) a guard before any write. The guard exists to prevent the class of
bug that already cost the user real data: a workout filed as a meal (which corrupted 850 kcal of food totals
and silently lost two activity logs).

## 2. Non-goals

- **Food vs drink is NOT classified here.** The resolution path already decides liquid vs solid, and the
  catalogue carries `is_liquid` / `beverage_category`. This classifier stops at "consumption".
- Not a replacement for the deterministic parser in `services/log_intents.py` (regexes: `_ACT_RE`, `_KCAL_RE`,
  `_MIN_RE`, `_MULT_RE`, drink patterns, meal slots). That stays the fast path.
- No new ledger semantics, no schema changes, no changes to nutrition maths.

## 3. The labels

Six, each mapping to an existing destination:

| label | destination |
|---|---|
| `consumption` | meals (+ water via `is_liquid`/`beverage_category` downstream) |
| `activity` | `activities` table (via the new `log_activity` tool) |
| `weight` | `measurements.metric_type='weight'` |
| `blood_pressure` | `measurements.metric_type='blood_pressure'` |
| `heart_rate` | `measurements.metric_type='heart_rate'` |
| `sleep` | `measurements.metric_type='sleep'` |

"No ledger at all" (chatter such as "good morning") is represented by **every** score being low — there is no
extra label to maintain.

## 4. How Jev is asked (verified working, 2026-10-05)

One request, all six questions at once — `services/jev_client.py` already speaks this protocol:

```json
{
  "model": "<jev_model>",
  "state": "User message: \"<text>\".",
  "questions": {
    "consumption":     {"type": "noul", "instructions": "...", "criteria": {"true": "<criteria>", "false": "..."}},
    "activity":        {"type": "noul", ...},
    "weight":          {"type": "noul", ...},
    "blood_pressure":  {"type": "noul", ...},
    "heart_rate":      {"type": "noul", ...},
    "sleep":           {"type": "noul", ...}
  }
}
```

Answers come back per question as `{"type": "noul", "noul": 0.97}`.

**Measured separation** (live, one call per message, 2026-10-05):

| text | winner | score | runner-up |
|---|---|---|---|
| "CrossFit 350 kcal" | activity | 0.97 | 0.08 |
| "did crossfit today, burned about 250" | activity | 0.98 | 0.19 |
| "2 eggs and a slice of sourdough bread" | consumption | 0.97 | 0.03 |
| "500 ml water" | consumption | 0.91 | 0.03 |
| "weight 98.5" | weight | 0.96 | 0.03 |
| "bp 130/85" | blood_pressure | 0.98 | 0.03 |
| "pulse 72" | heart_rate | 0.97 | 0.05 |
| "slept 7 hours" | sleep | 0.98 | 0.19 |
| "good morning, how are you" | none | max 0.03 | — |

The existing `jev_threshold` (0.88) works unchanged: correct labels land 0.91–0.98, wrong ones 0.01–0.19.
Chatter is separable by an absolute floor — do not lower the threshold to catch it.

## 5. API

`services/ledger_classifier.py`:

```python
@dataclass
class LedgerVerdict:
    label: str | None          # winning ledger, or None when nothing scores high enough
    score: float | None        # winner's noul
    runner_up: str | None
    runner_up_score: float | None
    margin: float | None       # score - runner_up_score
    scores: dict[str, float | None]
    available: bool            # False when Jev is unconfigured/unreachable
    reason: str | None         # "unavailable" | "no_ledger" | "classified"

def classify_ledger(text: str) -> LedgerVerdict: ...
```

Rules:
- Reuse `get_settings()` (`jev_api_url`, `jev_api_key`, `jev_model`, `jev_threshold`). Never a new key.
- Timeout and error handling as in `jev_client`: any failure → `available=False`, never an exception upward.
- Empty/whitespace text → `reason="no_ledger"`, no call.
- `label` is the argmax ONLY when its score ≥ `jev_threshold`; otherwise `label=None` with
  `reason="no_ledger"`.
- **English-first:** Jev is English-first and the user is not a native speaker. Translate before classifying,
  reusing the existing translation helper if it accepts arbitrary text; otherwise document the limitation in
  the module docstring and skip translation for v1 rather than inventing a second translator.

## 6. Where it is used

**(a) Router — when the deterministic parser finds nothing.**
In the free-text log path (`services/log_intents.py`), if `parse_log_text` yields no intents and the classifier
names a ledger, use it to decide what to do next instead of failing or letting the model guess. A `sleep`,
`weight`, `blood_pressure` or `heart_rate` verdict routes to the measurement writer; `activity` to the activity
writer; `consumption` to the meal parser.

**(b) Guard — before writing.**
Compare the classifier's verdict with the ledger the caller is about to write:

| condition | behaviour |
|---|---|
| target ledger score ≥ 0.88 | proceed silently (the normal case) |
| target score < 0.88 AND a different label ≥ 0.88 | **block**: return a structured "which did you mean?" payload naming both, so the bot asks the user |
| 0.5 ≤ target score < 0.88 | proceed but mark the write `needs_review` (existing status/confirm-gate machinery) |
| every score < 0.5 | do not log; return `no_ledger` so the bot asks rather than inventing |
| `available=False` | **fail open** — log exactly as today, emit a warning, never block the user's logging because an external service is down |

The guard must never silently drop or rewrite a log. When in doubt it asks.

**(c) Tool (optional, only if it stays cheap).** Expose `classify_log_text` to the bot so it can ask before
acting. Three activity tools were just added (surface is 21); add this only if the prompt-budget cost is
acceptable and the description is terse.

## 7. Constraints

- Tests must **stub** the Jev client — no live Jev calls in the suite. Cover: high-confidence agreement,
  disagreement/block, the 0.5–0.88 band, all-low chatter, and unavailable→fail-open.
- The guard must be disableable by configuration (one flag) so it can be switched off without a code change.
- No behaviour change when Jev is unconfigured (the current production default must keep working).
- Dev database only for tests; verified `pg_dump` before any production deploy; rollback image tagged.
- Ship as `drhiro-api:0.1.26-ledger-classifier-tf`. `--force-recreate api` only. If the MCP surface changes,
  activate with `openclaw mcp reload`, never a gateway restart.

## 8. Acceptance

1. The nine texts in §4 classify as tabulated, through the shipped code path (not a scratch script).
2. "CrossFit 350 kcal" sent down the **meal** path is blocked with a which-did-you-mean payload; the same text
   down the **activity** path proceeds. This is the regression the guard exists for.
3. "good morning" yields `no_ledger` and writes nothing.
4. With `jev_api_key` unset, a normal meal log succeeds exactly as before and the warning is logged once.
5. The food/drink distinction is untouched: "500 ml water" still becomes a liquid meal through the existing
   `is_liquid`/`beverage_category` path, with no new classification involved.
6. Full suite on the final image: baseline 84 passed / 2 failed (the two pre-existing failures must be the
   only ones).
7. Evidence: the real Jev scores from a live call, the blocked-path response, the unavailable-path behaviour,
   the pytest summary, image tag, `/health`, files changed, and anything not verified.
