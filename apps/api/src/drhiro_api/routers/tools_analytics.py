"""OpenClaw agent tools — analytics (0.9.0). New module; openclaw_tools.py untouched.

Scope-limited set per the standing amendments: get_metric_summary,
get_metric_changes, list_analyzable_metrics. Deterministic, facts-only (D3):
they run the engine synchronously on a bounded window and quote findings.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from drhiro_api.db import get_db
from drhiro_api.routers.openclaw_tools import ToolResponse, _resolve_user, _tool_audit
from drhiro_analytics.catalogue import METRIC_CATALOGUE
from drhiro_analytics.run import analyse
from drhiro_analytics.thresholds import THRESHOLDS_VERSION
from drhiro_analytics.types import AnalysisInputs, SeriesInput

from drhiro_api.services.series import daily_series
from drhiro_api.services.analytics_inputs import (
    encounter_timeline,
    med_timeline,
    unknowns_context,
)

router = APIRouter(prefix="/tools", tags=["openclaw-tools-analytics"])

MAX_PERIOD_DAYS = 5 * 365


class SummaryRequest(BaseModel):
    metric: str = Field(min_length=1)
    days: int = Field(default=90, ge=7, le=MAX_PERIOD_DAYS)


class ChangesRequest(BaseModel):
    metric: str | None = None
    days: int = Field(default=90, ge=14, le=MAX_PERIOD_DAYS)


class CatalogueRequest(BaseModel):
    group: str | None = None


def _baseline(days: int) -> int:
    return min(days * 2, MAX_PERIOD_DAYS)


def _run_engine(db: Session, user, days: int, keys: list[str]):
    today = datetime.now().astimezone().date()
    period = (today - timedelta(days=days - 1), today)
    baseline_start = today - timedelta(days=_baseline(days) - 1)
    series = [
        SeriesInput(k, METRIC_CATALOGUE[k][0],
                    [(d.day, d.value) for d in daily_series(db, user, k, baseline_start, period[1])])
        for k in keys if k in METRIC_CATALOGUE
    ]
    timeline = med_timeline(db, user, baseline_start, period[1]) + encounter_timeline(db, user, baseline_start, period[1])
    context = unknowns_context(db, user, period[0], period[1])
    return analyse(AnalysisInputs(
        period_start=period[0], period_end=period[1], baseline_start=baseline_start,
        series=series, timeline=timeline, context=context,
        locale=user.locale if user.locale in ("en", "hr") else "en",
    ))


@router.post("/get_metric_summary", response_model=ToolResponse)
def tool_metric_summary(req: SummaryRequest, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    if req.metric not in METRIC_CATALOGUE:
        return ToolResponse(ok=False, message=f"Unknown metric '{req.metric}'. Use list_analyzable_metrics.")
    res = _run_engine(db, user, req.days, [req.metric])
    m = res.metrics[0] if res.metrics else None
    _tool_audit(db, user, "tools.get_metric_summary", "measurement", req.metric)
    db.commit()
    return ToolResponse(ok=True, data={
        "metric": req.metric,
        "unit": METRIC_CATALOGUE[req.metric][0],
        "days": req.days,
        "summary": m,
        "tracked": bool(m and m.get("tracked")),
        "thresholds_version": THRESHOLDS_VERSION,
    })


@router.post("/get_metric_changes", response_model=ToolResponse)
def tool_metric_changes(req: ChangesRequest, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    keys = [req.metric] if req.metric else list(METRIC_CATALOGUE.keys())
    bad = [k for k in keys if k not in METRIC_CATALOGUE]
    if bad:
        return ToolResponse(ok=False, message=f"Unknown metrics: {bad}")
    res = _run_engine(db, user, req.days, keys)
    findings = [
        {
            "kind": f.kind, "metric": f.metric, "direction": f.direction,
            "anchor_date": f.anchor_date, "before": f.before, "after": f.after,
            "delta": f.delta, "unit": f.unit, "confidence": f.confidence,
            "text": f.text, "related_events": f.related_events,
        }
        for f in res.findings if f.kind != "cooccurrence"
    ]
    unknowns = [{"code": u.code, "metric": u.metric, "text": u.text} for u in res.unknowns]
    _tool_audit(db, user, "tools.get_metric_changes", "measurement", req.metric)
    db.commit()
    return ToolResponse(ok=True, data={
        "days": req.days,
        "findings": findings,
        "unknowns": unknowns,
        "thresholds_version": THRESHOLDS_VERSION,
    })


@router.post("/list_analyzable_metrics", response_model=ToolResponse)
def tool_list_metrics(req: CatalogueRequest, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    today = datetime.now().astimezone().date()
    out = []
    for key, (unit, kind, _wadj, group) in METRIC_CATALOGUE.items():
        if req.group and group != req.group:
            continue
        tracked = False
        try:
            vals = daily_series(db, user, key, today - timedelta(days=90), today)
            tracked = any(d.value is not None for d in vals)
        except Exception:
            pass
        out.append({"key": key, "unit": unit, "group": group, "tracked": tracked})
    _tool_audit(db, user, "tools.list_analyzable_metrics")
    db.commit()
    return ToolResponse(ok=True, data={"metrics": out})
