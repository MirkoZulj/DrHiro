# drHiro — Rule Governance

The deterministic rule engine is the only source of safety flags. The LLM
(OpenClaw) may explain a flag but must never invent, suppress, or change
it. Medical thresholds are versioned, jurisdiction-aware, and reviewed.

## Registry snapshot (0.1.0)

| code | version | jurisdiction | severity | description |
|---|---|---|---|---|
| bp_extreme_single_reading | 1 | global | warning | Single BP outside safe range -> repeat-check workflow, never dismissal |
| bp_elevated_repeated | 1 | global | info | >=3 elevated systolic readings -> trend flag for LLM explanation |
| weight_extreme | 1 | global | info | Weight outside plausibility band -> confirm entry |
| missing_is_not_zero | 1 | global | info | Enforced invariant: missing wearable data is never zero |

The canonical list lives in code: `packages/rule-engine/src/drhiro_rules/engine.py`.
Run `python -c "import drhiro_rules.engine as e; print(e.rule_registry_snapshot())"`.

## Rule lifecycle

1. **Proposal** — a new threshold or rule is proposed in a PR with
   evidence (guideline, clinician note, user report).
2. **Review** — a human with medical review authority approves; the
   `reviewed_by` field is set on the RuleDefinition row.
3. **Version** — bump `version`, set `active_from`; old versions remain
   for audit. Never mutate an active rule in place.
4. **Deploy** — the rule ships in the package; the API persists its
   definition into `rule_definitions`.
5. **Monitor** — alert volume is reviewed; rules that fire constantly or
   never are re-examined.

## Jurisdictions

- `global` — applies everywhere (plausibility, repeat-check).
- `eu` / `us` / etc. — region-specific medical thresholds (future).

`rules_for_jurisdiction("eu")` returns global + eu rules. The user's
jurisdiction is a profile setting, not guessed.

## Non-negotiable constraints

- The LLM never creates alert thresholds dynamically.
- A flagged reading is never dismissed as "probably wrong"; the repeat
  check protocol is followed instead.
- Photo-derived values stay drafts until confirmed.
- No medication changes; fixed escalation templates only.
- Alerts store `trigger_record_ids` so every flag is traceable to data.

## Escalation template (fixed, reviewed)

Stored in the OpenClaw skill (`openclaw/skills/skill-drhiro/SKILL.md`). The exact
language is reviewed; the agent does not improvise clinical text. For
urgent symptoms or critical alerts it directs the user to local emergency
services with the region-appropriate number.
