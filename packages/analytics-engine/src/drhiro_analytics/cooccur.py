"""Co-occurring changes (spec §3.2 cooccur.py) — no correlation statistics.

Groups level_change / variability_change findings whose anchor dates are within
COOCCUR_WINDOW_DAYS of each other (single-linkage). Sets related_findings on
every member and emits ONE cooccurrence finding per group of 2+.
"""

from __future__ import annotations

import hashlib

from drhiro_analytics.thresholds import COOCCUR_WINDOW_DAYS
from drhiro_analytics.types import Finding, TimelineEvent


def group(findings: list[Finding]) -> list[list[Finding]]:
    """Single-linkage grouping by anchor date proximity."""
    anchorable = [f for f in findings if f.kind in ("level_change", "variability_change") and f.anchor_date]
    anchorable = sorted(anchorable, key=lambda f: (f.anchor_date, f.metric, f.kind))
    groups: list[list[Finding]] = []
    current: list[Finding] = []
    for f in anchorable:
        if current:
            if any(
                abs((f.anchor_date - g.anchor_date).days) <= COOCCUR_WINDOW_DAYS
                for g in current if g.anchor_date
            ):
                current.append(f)
                continue
            groups.append(current)
            current = []
        current = [f]
    if current:
        groups.append(current)
    return [g for g in groups if len(g) >= 2]


def cooccur_findings(findings: list[Finding], timeline: list[TimelineEvent], locale: str = "en") -> list[Finding]:
    """Set related_findings on members and build cooccurrence findings."""
    from drhiro_analytics.render import fmt_date, label

    out: list[Finding] = []
    for g in group(findings):
        ids = [f.id for f in g]
        for f in g:
            f.related_findings = [i for i in ids if i != f.id]
        anchor = min((f.anchor_date for f in g if f.anchor_date), default=None)
        members = ", ".join(
            f"{label(f.metric, locale)} {'up' if f.direction == 'up' else 'down'}" for f in g
        )
        d = fmt_date(anchor, locale)
        if locale == "hr":
            text = f"Ok oko {d} (±7 dana) nekoliko se mjera promijenilo zajedno: {members}."
        else:
            text = f"Around {d} (±7 days) several metrics changed together: {members}."
        out.append(Finding(
            id=_group_id(g),
            kind="cooccurrence",
            metric=",".join(sorted({f.metric for f in g})),
            direction=None,
            anchor_date=anchor,
            before=None,
            after=None,
            delta=None,
            unit="",
            stats={"members": ids, "caveats": []},
            confidence=max(g, key=lambda f: {"high": 3, "medium": 2, "low": 1}[f.confidence]).confidence,
            related_findings=ids,
            related_events=[
                ev.ref_id for ev in timeline
                if anchor is not None and abs((ev.day - anchor).days) <= COOCCUR_WINDOW_DAYS
            ],
            text=text,
            rank=0.0,
        ))
    return out


def _group_id(g: list[Finding]) -> str:
    raw = "cooccurrence|" + ",".join(sorted(f.id for f in g))
    return hashlib.sha1(raw.encode()).hexdigest()[:12]
