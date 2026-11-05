"""Analytics JWT API (0.9.0, spec §3.7, scope-limited per standing amendments).

Web-facing analytics runs (0.10.x wave 3): kind="general" AND kind="visit"
(§3.8), visit profiles, metric suggestions, PDF report queueing (§3.9,
dashboard-only download via /files).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.models import AnalysisRun, GeneratedFile, MedicationItem, MedicalEncounter, MedicalRecord, User
from drhiro_api.security import audit
from drhiro_api.services.day_boundary import resolve_tz

router = APIRouter(prefix="/analytics", tags=["analytics"])

MAX_PERIOD_DAYS = 5 * 365


class PeriodSpec(BaseModel):
    preset: str = "90d"
    start: date | None = None
    end: date | None = None


class RunCreateRequest(BaseModel):
    kind: str = "general"
    period: PeriodSpec = PeriodSpec()
    metrics: list[str] | None = None
    intent: dict | None = None  # VisitIntent fields, kind="visit" only


PROVIDER_TYPES = ("gp", "cardiology", "endocrinology", "gastroenterology", "nephrology",
                  "sleep", "nutrition", "sports_medicine", "psychiatry", "other")


class VisitIntent(BaseModel):
    visit_date: date | None = None
    provider_type: str = "gp"
    facility: str | None = None
    purpose: str = Field(default="", max_length=500)
    questions: list[str] = Field(default_factory=list, max_length=15)
    include_metrics: list[str] = []
    exclude_metrics: list[str] = []
    include_record_ids: list[str] = []
    include_meds: bool = True
    include_health_card: bool = True

    def clean(self) -> dict:
        for q in self.questions:
            if len(q) > 300:
                raise HTTPException(status_code=400, detail="questions max 300 chars each")
        if self.provider_type not in PROVIDER_TYPES:
            raise HTTPException(status_code=400, detail=f"provider_type must be one of {PROVIDER_TYPES}")
        return {
            "visit_date": self.visit_date.isoformat() if self.visit_date else None,
            "provider_type": self.provider_type,
            "facility": self.facility,
            "purpose": self.purpose,
            "questions": self.questions,
            "include_metrics": self.include_metrics,
            "exclude_metrics": self.exclude_metrics,
            "include_record_ids": self.include_record_ids,
            "include_meds": self.include_meds,
            "include_health_card": self.include_health_card,
        }


def _idempotency_key(user_id, kind: str, start: date, end: date, params: dict) -> str:
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
    raw = f"{user_id}|{kind}|{start}|{end}|{canonical}"
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


def resolve_period(preset: str, start: date | None, end: date | None, user: User, db_q=None) -> tuple[date, date]:
    """Presets are resolved server-side in the user's tz (local today)."""
    today = datetime.now(resolve_tz(user.timezone)).date()
    if preset == "custom":
        if not start or not end:
            raise HTTPException(status_code=400, detail="custom period needs start and end")
    elif preset == "30d":
        start, end = today - timedelta(days=29), today
    elif preset == "90d":
        start, end = today - timedelta(days=89), today
    elif preset == "6m":
        start, end = today - timedelta(days=182), today
    elif preset == "12m":
        start, end = today - timedelta(days=364), today
    elif preset == "since_last_encounter":
        from drhiro_api.models import MedicalEncounter

        last = (
            db_q.query(MedicalEncounter)
            .filter(MedicalEncounter.user_id == user.id, MedicalEncounter.encounter_date.isnot(None))
            .order_by(MedicalEncounter.encounter_date.desc())
            .first()
        )
        start = last.encounter_date if last else today - timedelta(days=89)
        end = today
    elif preset == "all":
        # first data day is resolved in the worker; API sets a 5y window
        start, end = today - timedelta(days=MAX_PERIOD_DAYS - 1), today
    else:
        raise HTTPException(status_code=400, detail="unknown preset")
    if (end - start).days + 1 > MAX_PERIOD_DAYS:
        raise HTTPException(status_code=400, detail="period too long (max 5 years)")
    if end > today:
        end = today
    if start > end:
        raise HTTPException(status_code=400, detail="start after end")
    return start, end


def _run_out(run: AnalysisRun, include_result: bool = True) -> dict:
    out = {
        "id": str(run.id),
        "kind": run.kind,
        "status": run.status,
        "requested_via": run.requested_via,
        "period_start": run.period_start,
        "period_end": run.period_end,
        "params_json": run.params_json,
        "idempotency_key": run.idempotency_key,
        "data_fingerprint": run.data_fingerprint,
        "engine_version": run.engine_version,
        "thresholds_version": run.thresholds_version,
        "narrative_text": run.narrative_text,
        "narrative_status": run.narrative_status,
        "error": run.error,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "files": [
            {
                "id": str(g.id), "kind": g.kind, "status": g.status, "filename": g.filename,
                "mime": g.mime, "size_bytes": g.size_bytes, "created_at": g.created_at,
                "expires_at": g.expires_at, "error": g.error,
            }
            for g in (run.files or [])
        ],
    }
    if include_result:
        out["result"] = run.result_json
    return out


@router.get("/catalogue")
def catalogue(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from drhiro_analytics.catalogue import METRIC_CATALOGUE
    from drhiro_api.services.series import daily_series

    today = datetime.now(resolve_tz(user.timezone)).date()
    metrics = []
    for key, (unit, kind, wadj, group) in METRIC_CATALOGUE.items():
        first_day = last_day = None
        try:
            vals = daily_series(db, user, key, today - timedelta(days=364), today)
            days = [d.day for d in vals if d.value is not None]
            if days:
                first_day, last_day = days[0], days[-1]
        except Exception:
            pass
        metrics.append({
            "key": key, "unit": unit, "group": group,
            "tracked": first_day is not None,
            "first_day": first_day, "last_day": last_day,
        })
    return {"metrics": metrics}


@router.post("/runs")
def create_run(req: RunCreateRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from drhiro_analytics.catalogue import METRIC_CATALOGUE
    from drhiro_analytics.visit_profiles import VISIT_PROFILES_VERSION, resolve_visit_metrics

    intent_clean = None
    params_extra: dict = {}
    if req.kind == "visit":
        intent = VisitIntent(**(req.intent or {}))
        intent_clean = intent.clean()
        try:
            keys = resolve_visit_metrics(
                intent.provider_type, intent.include_metrics, intent.exclude_metrics
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        params_extra = {
            "intent": intent_clean,
            "visit_metrics": keys,
            "profile_version": VISIT_PROFILES_VERSION,
        }
    elif req.kind != "general":
        raise HTTPException(status_code=400, detail="kind must be 'general' or 'visit'")

    if req.metrics:
        unknown = [m for m in req.metrics if m not in METRIC_CATALOGUE]
        if unknown:
            raise HTTPException(status_code=400, detail=f"unknown metrics: {unknown}")
    start, end = resolve_period(req.period.preset, req.period.start, req.period.end, user, db)
    params = {"metrics": req.metrics, "preset": req.period.preset, **params_extra}
    key = _idempotency_key(user.id, req.kind, start, end, params)
    existing = (
        db.query(AnalysisRun)
        .filter(AnalysisRun.user_id == user.id, AnalysisRun.idempotency_key == key)
        .first()
    )
    if existing:
        if existing.status in ("queued", "running"):
            return {"run": _run_out(existing, include_result=False), "reused": True}
        return {"run": _run_out(existing), "reused": True}
    run = AnalysisRun(
        user_id=user.id,
        kind=req.kind,
        status="queued",
        requested_via="web",
        period_start=start,
        period_end=end,
        params_json=params,
        idempotency_key=key,
    )
    db.add(run)
    db.flush()
    # F9: enqueue() swallows failures — if it returns None, mark the run failed
    # immediately instead of leaving it queued forever.
    from drhiro_api.services.task_queue import enqueue

    job_id = enqueue("analytics", "drhiro_worker.jobs_analytics.run_analysis", str(run.id))
    if job_id is None:
        run.status = "failed"
        run.error = "queue unavailable — run not enqueued"
        db.commit()
        return {"run": _run_out(run), "reused": False}
    run.job_id = job_id
    audit(db, "user", str(user.id), user.id, "analytics.create", "analysis_run", str(run.id), {})
    db.commit()
    db.refresh(run)
    return {"run": _run_out(run, include_result=False), "reused": False}


@router.get("/runs")
def list_runs(limit: int = 20, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    runs = (
        db.query(AnalysisRun)
        .filter(AnalysisRun.user_id == user.id)
        .order_by(AnalysisRun.created_at.desc())
        .limit(limit)
        .all()
    )
    out = []
    for r in runs:
        n = len((r.result_json or {}).get("findings", [])) if r.result_json else 0
        out.append({
            "id": str(r.id), "kind": r.kind, "status": r.status,
            "period_start": r.period_start, "period_end": r.period_end,
            "created_at": r.created_at, "findings_count": n,
        })
    return {"runs": out, "count": len(out)}


@router.get("/runs/{run_id}")
def get_run(run_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    run = _get_run(db, user, run_id)
    return {"run": _run_out(run)}


@router.get("/runs/{run_id}/series/{key}")
def run_series(run_id: str, key: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from drhiro_analytics.catalogue import METRIC_CATALOGUE
    from drhiro_api.services.series import daily_series

    run = _get_run(db, user, run_id)
    if key not in METRIC_CATALOGUE:
        raise HTTPException(status_code=400, detail="unknown metric")
    period_len = (run.period_end - run.period_start).days + 1
    baseline_start = run.period_start - timedelta(days=period_len)
    vals = daily_series(db, user, key, baseline_start, run.period_end)
    markers = []
    for f in (run.result_json or {}).get("findings", []):
        if f.get("metric") == key and f.get("anchor_date"):
            markers.append({"date": f["anchor_date"], "kind": f["kind"], "finding_id": f["id"]})
    return {
        "key": key,
        "unit": METRIC_CATALOGUE[key][0],
        "points": [{"date": d.day, "value": d.value} for d in vals],
        "markers": markers,
    }


@router.delete("/runs/{run_id}")
def delete_run(run_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    run = _get_run(db, user, run_id)
    db.delete(run)
    audit(db, "user", str(user.id), user.id, "analytics.delete", "analysis_run", run_id, {})
    db.commit()
    return {"ok": True}


def _get_run(db: Session, user: User, run_id: str) -> AnalysisRun:
    try:
        rid = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Run not found")
    run = db.query(AnalysisRun).filter(AnalysisRun.id == rid, AnalysisRun.user_id == user.id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run

@router.get("/visit-profiles")
def visit_profiles(user: User = Depends(get_current_user)):
    from drhiro_analytics.visit_profiles import VISIT_PROFILES, VISIT_PROFILES_VERSION

    return {
        "profile_version": VISIT_PROFILES_VERSION,
        "profiles": [{"code": k, "label": k.replace("_", " ").title(), "metrics": v}
                     for k, v in VISIT_PROFILES.items()],
    }


@router.post("/visit-suggest")
def visit_suggest(body: dict, user: User = Depends(get_current_user)):
    """Deterministic keyword suggestions for extra metrics (spec §3.8).
    The optional LLM assist is not wired in this release; suggestions are
    keyword-map only and are always confirmed by the user in the UI."""
    from drhiro_analytics.catalogue import METRIC_CATALOGUE

    purpose = str(body.get("purpose", "")).lower()
    suggestion_sets = {
        r"tlak|pressure|hipertenz|blood.?press": ["bp_sys", "bp_dia", "bp_pulse"],
        r"san|sleep|nesanic|insomn": ["sleep_duration_min", "sleep_midpoint_min"],
        r"umor|fatigue|tired": ["sleep_duration_min", "steps", "resting_hr"],
        r"kilaž|kilaž|weight|težin|kilogram": ["weight_kg", "kcal_in"],
        r"alkohol|alcohol|piće|pić": ["alcohol_ml"],
        r"probav|stomach|gut|crijev": ["kcal_in", "fat_g", "carbs_g", "protein_g", "liquids_total_ml"],
        r"šecer|secer|sugar|dijabet|diabet": ["weight_kg", "kcal_in", "carbs_g", "fat_g"],
        r"kolen|cholesterol|lipid": ["weight_kg", "kcal_in", "fat_g", "bp_sys", "bp_dia"],
    }
    import re

    suggested: list[str] = []
    for pattern, keys in suggestion_sets.items():
        if re.search(pattern, purpose):
            for k in keys:
                if k in METRIC_CATALOGUE and k not in suggested:
                    suggested.append(k)
    return {"suggested_metrics": suggested}


class ReportRequest(BaseModel):
    send_telegram: bool = False  # ignored: dashboard-only delivery (A2 for bundles; PDFs also stay in-dashboard)


@router.post("/runs/{run_id}/report")
def request_report(run_id: str, body: ReportRequest | None = None,
                   user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Queue a PDF report for this run (202). Download via /files/{id}/download."""
    run = _get_run(db, user, run_id)
    from drhiro_api.services.task_queue import enqueue

    template = "visit" if run.kind == "visit" else "analysis"
    gf = GeneratedFile(
        user_id=user.id, kind="visit_report" if run.kind == "visit" else "analysis_report",
        status="queued", analysis_run_id=run.id,
        params_json={"run_id": str(run.id), "template": template,
                     "lang": user.locale if user.locale in ("en", "hr") else "en"},
    )
    db.add(gf)
    db.flush()
    job_id = enqueue("drhiro", "drhiro_worker.jobs_reports.build_report", str(gf.id))
    if job_id is None:
        gf.status, gf.error = "failed", "queue unavailable — report not enqueued"
        db.commit()
        return {"file": {"id": str(gf.id), "status": gf.status, "error": gf.error}}
    gf.job_id = job_id
    audit(db, "user", str(user.id), user.id, "analytics.report", "generated_file", str(gf.id),
          {"run_id": str(run.id), "template": template})
    db.commit()
    return {"file": {"id": str(gf.id), "status": gf.status}}
