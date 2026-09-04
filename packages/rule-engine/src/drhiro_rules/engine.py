"""Layer 2: versioned, jurisdiction-aware safety rules.

Rules are pure Python objects registered with a code, version,
jurisdiction, and severity. They are evaluated deterministically. The
LLM must never create or modify these at runtime.

Rule output is always a structured AlertCandidate:
    {"rule_code", "rule_version", "severity", "explanation_template",
     "trigger_record_ids", "params"}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from drhiro_schema.metrics import MetricType

from drhiro_rules.calculations import bp_average_by_context, numeric_value

Severity = str  # "info" | "warning" | "critical"


@dataclass(frozen=True)
class AlertCandidate:
    rule_code: str
    rule_version: int
    severity: Severity
    explanation_template: str
    trigger_record_ids: list[str] = field(default_factory=list)
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RuleDefinition:
    code: str
    version: int
    jurisdiction: str  # "global" | "eu" | "us" ...
    severity: Severity
    description: str
    evaluate: Callable[[list[dict], dict], list[AlertCandidate] | None]


_RULES: list[RuleDefinition] = []


def register_rule(code, version, jurisdiction, severity, description):
    def decorator(fn):
        _RULES.append(
            RuleDefinition(
                code=code,
                version=version,
                jurisdiction=jurisdiction,
                severity=severity,
                description=description,
                evaluate=fn,
            )
        )
        return fn
    return decorator


def all_rules() -> list[RuleDefinition]:
    return list(_RULES)


def rules_for_jurisdiction(jurisdiction: str = "global") -> list[RuleDefinition]:
    """Active rules matching the jurisdiction (global rules apply everywhere)."""
    return [r for r in _RULES if r.jurisdiction in ("global", jurisdiction)]


def evaluate_rules(measurements: list[dict], jurisdiction: str = "global") -> list[AlertCandidate]:
    """Evaluate all applicable rules; return candidates in severity order."""
    candidates: list[AlertCandidate] = []
    for rule in rules_for_jurisdiction(jurisdiction):
        try:
            result = rule.evaluate(measurements, {})
        except Exception:  # a rule bug must not take down ingestion
            continue
        if result:
            candidates.extend(result)
    order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(candidates, key=lambda c: (order.get(c.severity, 9), c.rule_code))


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

@register_rule(
    code="bp_extreme_single_reading",
    version=1,
    jurisdiction="global",
    severity="warning",
    description="A single BP reading outside safe ranges triggers a repeat-check workflow, never dismissal.",
)
def _bp_extreme(measurements: list[dict], _ctx: dict):
    out = []
    for m in measurements:
        if m.get("metric_type") != MetricType.BLOOD_PRESSURE:
            continue
        v = m.get("value_json") or {}
        sys, dia = v.get("systolic_mmhg"), v.get("diastolic_mmhg")
        if not sys or not dia:
            continue
        if sys >= 180 or dia >= 120:
            out.append(
                AlertCandidate(
                    rule_code="bp_extreme_single_reading",
                    rule_version=1,
                    severity="warning",
                    explanation_template=(
                        "BP reading {systolic}/{diastolic} is very high. This may be a "
                        "measurement error. Do not dismiss it. Rest 5 minutes, check cuff "
                        "placement, and repeat the measurement per protocol."
                    ),
                    trigger_record_ids=[m["id"]],
                    params={"systolic": sys, "diastolic": dia},
                )
            )
        elif sys <= 70 or dia <= 40:
            out.append(
                AlertCandidate(
                    rule_code="bp_extreme_single_reading",
                    rule_version=1,
                    severity="warning",
                    explanation_template=(
                        "BP reading {systolic}/{diastolic} is very low. This may be a "
                        "measurement error or a medical situation. If you feel unwell, "
                        "seek care. Otherwise rest and repeat the measurement."
                    ),
                    trigger_record_ids=[m["id"]],
                    params={"systolic": sys, "diastolic": dia},
                )
            )
    return out or None


@register_rule(
    code="bp_elevated_repeated",
    version=1,
    jurisdiction="global",
    severity="info",
    description="Repeated elevated BP readings (>=3 in 14 days) produce a trend flag for the LLM to explain.",
)
def _bp_repeated(measurements: list[dict], _ctx: dict):
    bps = []
    for m in measurements:
        if m.get("metric_type") != MetricType.BLOOD_PRESSURE:
            continue
        v = m.get("value_json") or {}
        if v.get("systolic_mmhg") and v["systolic_mmhg"] >= 135:
            bps.append(m)
    if len(bps) < 3:
        return None
    avg = bp_average_by_context(bps)
    return [
        AlertCandidate(
            rule_code="bp_elevated_repeated",
            rule_version=1,
            severity="info",
            explanation_template=(
                "{count} BP readings in the recent period are at or above 135 systolic "
                "(avg {avg_sys}/{avg_dia}). This is a trend flag, not a diagnosis. "
                "Discuss with your clinician."
            ),
            trigger_record_ids=[m["id"] for m in bps],
            params={"count": len(bps), **({"avg_sys": avg["avg_systolic"], "avg_dia": avg["avg_diastolic"]} if avg else {})},
        )
    ]


@register_rule(
    code="weight_extreme",
    version=1,
    jurisdiction="global",
    severity="info",
    description="Weight outside a wide plausibility band flags likely data-entry error.",
)
def _weight_extreme(measurements: list[dict], _ctx: dict):
    out = []
    for m in measurements:
        if m.get("metric_type") != MetricType.WEIGHT:
            continue
        v = m.get("value_json") or {}
        w = v.get("weight_kg")
        if w is None:
            continue
        if w < 30 or w > 300:
            out.append(
                AlertCandidate(
                    rule_code="weight_extreme",
                    rule_version=1,
                    severity="info",
                    explanation_template=(
                        "Weight {weight} kg is outside the expected range. Please confirm "
                        "this was entered correctly."
                    ),
                    trigger_record_ids=[m["id"]],
                    params={"weight": w},
                )
            )
    return out or None


@register_rule(
    code="missing_is_not_zero",
    version=1,
    jurisdiction="global",
    severity="info",
    description="Never interpret a day with no wearable data as zero activity.",
)
def _missing_not_zero(measurements: list[dict], _ctx: dict):
    # This rule produces no alert itself; it is enforced by the coverage
    # indicator and the agent instructions. It exists so the rule is
    # versioned and auditable.
    return None


def serialize_candidate(c: AlertCandidate) -> dict:
    return {
        "rule_code": c.rule_code,
        "rule_version": c.rule_version,
        "severity": c.severity,
        "explanation_template": c.explanation_template,
        "trigger_record_ids": c.trigger_record_ids,
        "params": c.params,
    }


def rule_registry_snapshot() -> str:
    """JSON snapshot of the rule registry for audit/governance."""
    return json.dumps(
        [
            {
                "code": r.code,
                "version": r.version,
                "jurisdiction": r.jurisdiction,
                "severity": r.severity,
                "description": r.description,
            }
            for r in _RULES
        ],
        indent=2,
    )
