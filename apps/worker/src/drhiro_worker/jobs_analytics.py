"""Analytics worker job (0.9.0, spec §3.5). Deterministic, no LLM decisions.

The optional narrative (§3.6) is out of this wave's scope: narrative_status
stays "none" and narrative_text stays None — the run still succeeds.
Timeline/context builders live in drhiro_api.services.analytics_inputs (shared
with the API-side agent tools, one definition).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from drhiro_api.db import SessionLocal
from drhiro_api.models import AnalysisRun, User
from drhiro_api.services.analytics_inputs import data_fingerprint
from drhiro_api.services.series import daily_series
from drhiro_analytics import ENGINE_VERSION
from drhiro_analytics.catalogue import METRIC_CATALOGUE
from drhiro_analytics.run import analyse, result_to_dict
from drhiro_analytics.thresholds import THRESHOLDS_VERSION
from drhiro_analytics.types import AnalysisInputs, SeriesInput
from drhiro_api.services.analytics_inputs import encounter_timeline, med_timeline, unknowns_context


def selected_metric_keys(run: AnalysisRun) -> list[str]:
    params = run.params_json or {}
    keys = params.get("metrics") or list(METRIC_CATALOGUE.keys())
    return [k for k in keys if k in METRIC_CATALOGUE]


def run_analysis(run_id: str) -> dict:
    db = SessionLocal()
    try:
        run = db.get(AnalysisRun, uuid.UUID(run_id))
        if not run or run.status not in ("queued",):
            return {"skipped": run and run.status}
        run.status, run.started_at = "running", datetime.now(timezone.utc)
        db.commit()
        try:
            user = db.get(User, run.user_id)
            keys = selected_metric_keys(run)
            period_start, period_end = run.period_start, run.period_end
            period_len = (period_end - period_start).days + 1
            baseline_start = period_start - timedelta(days=period_len)
            series = [
                SeriesInput(
                    k,
                    METRIC_CATALOGUE[k][0],
                    [(d.day, d.value) for d in daily_series(db, user, k, baseline_start, period_end)],
                )
                for k in keys
            ]
            timeline = med_timeline(db, user, baseline_start, period_end) + encounter_timeline(
                db, user, baseline_start, period_end
            )
            context = unknowns_context(db, user, period_start, period_end)
            result = analyse(AnalysisInputs(
                period_start=period_start, period_end=period_end,
                baseline_start=baseline_start, series=series, timeline=timeline,
                context=context, locale=user.locale if user.locale in ("en", "hr") else "en",
            ))
            run.result_json = result_to_dict(result)
            run.data_fingerprint = data_fingerprint(db, user.id, baseline_start, period_end)
            run.engine_version, run.thresholds_version = ENGINE_VERSION, THRESHOLDS_VERSION
            run.narrative_text, run.narrative_status = None, "none"  # narrative = later wave
            run.status, run.finished_at = "succeeded", datetime.now(timezone.utc)
            from drhiro_api.security import audit
            audit(db, "system", None, user.id, "analytics.run", "analysis_run", str(run.id),
                  {"findings": len(result.findings)})
            db.commit()
            return {"findings": len(result.findings), "status": "succeeded"}
        except Exception as e:
            db.rollback()
            run.status, run.error, run.finished_at = "failed", repr(e)[:2000], datetime.now(timezone.utc)
            db.commit()
            raise
    finally:
        db.close()
