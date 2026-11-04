"""Dataclasses (spec §3.2 types.py)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class SeriesInput:
    key: str
    unit: str
    points: list[tuple[date, float | None]]  # from services.series.daily_series, contiguous days


@dataclass(frozen=True)
class TimelineEvent:
    day: date
    kind: str      # med_started | med_stopped | med_dose_changed | med_paused | med_resumed | encounter
    label: str     # "Magnesium 300 mg", "Encounter: KBC Zagreb – Cardiology"
    ref_id: str


@dataclass(frozen=True)
class AnalysisInputs:
    period_start: date
    period_end: date
    baseline_start: date          # period_start - len(period) (clamped to first data day)
    series: list[SeriesInput]     # includes baseline days
    timeline: list[TimelineEvent]
    context: dict                 # counts for unknowns (§3.5)
    locale: str                   # "en" | "hr"


@dataclass
class Finding:
    id: str                       # sha1(kind|metric|anchor_date|direction)[:12] – stable across reruns
    kind: str                     # level_change | trend | variability_change | period_delta | cooccurrence
    metric: str
    direction: str | None         # up | down | None
    anchor_date: date | None
    before: float | None
    after: float | None
    delta: float | None
    unit: str
    stats: dict = field(default_factory=dict)
    confidence: str = "low"       # high | medium | low
    related_events: list[str] = field(default_factory=list)
    related_findings: list[str] = field(default_factory=list)
    text: str = ""
    rank: float = 0.0


@dataclass
class Unknown:
    code: str
    metric: str | None
    detail: dict = field(default_factory=dict)
    text: str = ""


@dataclass
class AnalysisResult:
    engine_version: str
    thresholds_version: str
    period: dict
    metrics: list[dict]
    findings: list[Finding]
    unknowns: list[Unknown]
    timeline: list[TimelineEvent]
