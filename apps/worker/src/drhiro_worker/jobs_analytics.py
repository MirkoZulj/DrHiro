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
from drhiro_api.models import (
    AnalysisRun,
    MedicalEncounter,
    MedicalRecord,
    MedicationItem,
    MedicationItemEvent,
    HealthProfile,
    User,
)
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
    if run.kind == "visit" and params.get("visit_metrics"):
        return [k for k in params["visit_metrics"] if k in METRIC_CATALOGUE]
    keys = params.get("metrics") or list(METRIC_CATALOGUE.keys())
    return [k for k in keys if k in METRIC_CATALOGUE]


def _visit_extras(db, run: AnalysisRun, user: User, baseline_start, period_end) -> dict:
    """result_json.visit for visit runs (spec §3.8). Attached record content is
    copied VERBATIM — never summarised (medical.py data-honesty rule)."""
    from drhiro_api.services.medications import adherence

    params = run.params_json or {}
    intent = params.get("intent") or {}
    p_start = run.period_start

    meds = []
    if intent.get("include_meds", True):
        items = db.query(MedicationItem).filter(
            MedicationItem.user_id == user.id, MedicationItem.status.in_(("active", "paused"))
        ).order_by(MedicationItem.created_at.asc()).all()
        for it in items:
            ad = adherence(db, user, it, p_start, period_end)
            meds.append({
                "name": it.name, "dose": it.dose_amount, "dose_unit": it.dose_unit,
                "schedule": it.schedule_json, "start_date": it.start_date.isoformat() if it.start_date else None,
                "status": it.status,
                "adherence_pct_period": ad.get("pct_taken"),
                "adherence_known": ad.get("pct_taken") is not None,
            })

    changes = db.query(MedicationItemEvent).filter(
        MedicationItemEvent.user_id == user.id,
        MedicationItemEvent.effective_date >= baseline_start,
        MedicationItemEvent.effective_date <= period_end,
    ).order_by(MedicationItemEvent.effective_date.asc()).all()
    medication_changes = [
        {"day": c.effective_date.isoformat(), "kind": c.kind, "item_id": str(c.item_id),
         "before": c.before_json, "after": c.after_json}
        for c in changes
    ]

    encounters = db.query(MedicalEncounter).filter(
        MedicalEncounter.user_id == user.id,
        MedicalEncounter.encounter_date >= p_start,
        MedicalEncounter.encounter_date <= period_end,
    ).order_by(MedicalEncounter.encounter_date.asc()).all()
    enc_out = [
        {"id": str(e.id), "date": e.encounter_date.isoformat() if e.encounter_date else None,
         "facility": e.facility, "title": e.title}
        for e in encounters
    ]

    attached = []
    for rid in intent.get("include_record_ids") or []:
        try:
            r = db.query(MedicalRecord).filter(
                MedicalRecord.id == uuid.UUID(str(rid)), MedicalRecord.user_id == user.id
            ).first()
        except ValueError:
            r = None
        if r:
            attached.append({
                "id": str(r.id), "title": r.title,
                "recorded_at": r.recorded_at.isoformat() if r.recorded_at else None,
                "content": r.content,
            })

    card = db.query(HealthProfile).filter(HealthProfile.user_id == user.id).first()
    card_snapshot = None
    if intent.get("include_health_card", True) and card:
        card_snapshot = {
            "blood_type": card.blood_type, "organ_donor": card.organ_donor,
            "conditions": card.conditions_json or [], "allergies": card.allergies_json or [],
            "emergency_contacts": card.emergency_contacts_json or [],
            "care_team": card.care_team_json or [], "insurance": card.insurance_json,
            "reviewed_at": card.reviewed_at.isoformat() if card.reviewed_at else None,
        }

    return {
        "intent": intent,
        "profile_version": params.get("profile_version"),
        "metrics_used": selected_metric_keys(run),
        "medications": meds,
        "medication_changes_in_period": medication_changes,
        "encounters_in_period": enc_out,
        "attached_records": attached,
        "health_card_snapshot": card_snapshot,
    }


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
            if run.kind == "visit":
                run.result_json["visit"] = _visit_extras(db, run, user, baseline_start, period_end)
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
