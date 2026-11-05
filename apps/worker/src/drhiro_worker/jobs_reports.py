"""Worker job: PDF report rendering (0.10.x wave 3, spec §3.9 / §6.4).

build_report(file_id) renders analysis_report / visit_report / health_card
PDFs from GeneratedFile.params_json:
  {"template": "analysis"|"visit"|"health_card", "run_id"?: str, "lang": "en"|"hr"}

Engine: WeasyPrint + Jinja2 templates in drhiro_worker/reports/templates/.
WeasyPrint is a worker-image dependency; if it isn't importable the job fails
with a clear error (the row keeps status failed — never silently "ready").

DASHBOARD-ONLY: reports are downloaded via /files/{id}/download. Nothing is
Telegram-delivered in this release (A2 spirit; spec §3.10 send_pdf stays a
dashboard download).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

from drhiro_api.config import get_settings
from drhiro_api.db import SessionLocal
from drhiro_api.models import AnalysisRun, GeneratedFile, HealthProfile, User
from drhiro_worker import files as gfiles

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "reports", "templates")

try:
    from weasyprint import HTML  # noqa: F401

    HAS_WEASYPRINT = True
except Exception:  # pragma: no cover - depends on image
    HAS_WEASYPRINT = False

try:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from jinja2 import TemplateNotFound
except Exception:  # pragma: no cover
    Environment = None

_labels_en = {
    "sleep_duration_min": "Sleep duration", "sleep_midpoint_min": "Sleep midpoint",
    "steps": "Steps", "active_minutes": "Active minutes", "active_kcal": "Active kcal",
    "activity_kcal_logged": "Logged activity kcal", "weight_kg": "Weight",
    "bp_sys": "Systolic BP", "bp_dia": "Diastolic BP", "bp_pulse": "Pulse (BP)",
    "resting_hr": "Resting heart rate", "heart_rate_avg": "Heart rate (avg)",
    "spo2_avg": "SpO2", "stress_avg": "Stress", "kcal_in": "Calories in",
    "protein_g": "Protein", "carbs_g": "Carbs", "fat_g": "Fat",
    "liquids_total_ml": "Liquids", "water_ml": "Water", "alcohol_ml": "Alcohol",
    "meals_confirmed": "Meals",
}
_labels_hr = {
    "sleep_duration_min": "Trajanje sna", "sleep_midpoint_min": "Sredina sna",
    "steps": "Koraci", "active_minutes": "Aktivne minute", "active_kcal": "Aktivne kcal",
    "activity_kcal_logged": "Zabilježena aktivnost (kcal)", "weight_kg": "Tjelesna masa",
    "bp_sys": "Sistolički tlak", "bp_dia": "Dijastolički tlak", "bp_pulse": "Puls (TLK)",
    "resting_hr": "Srce u mirovanju", "heart_rate_avg": "Srce (prosjek)",
    "spo2_avg": "SpO2", "stress_avg": "Stres", "kcal_in": "Unesene kalorije",
    "protein_g": "Proteini", "carbs_g": "Ugljikohidrati", "fat_g": "Masti",
    "liquids_total_ml": "Tekućina", "water_ml": "Voda", "alcohol_ml": "Alkohol",
    "meals_confirmed": "Obroci",
}
LABELS = {"en": _labels_en, "hr": _labels_hr}


def _labels(lang: str) -> dict:
    return LABELS.get(lang, LABELS["en"])


def _env():
    return Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
    )


def _fmt_val(v, unit: str) -> str:
    if v is None:
        return "—"
    if unit == "min" and abs(v) >= 60:
        sign = "-" if v < 0 else ""
        v = abs(v)
        return f"{sign}{int(v // 60)}h{int(round(v % 60)):02d}"
    if unit == "ml" and v >= 1000:
        return f"{v / 1000:.1f} L"
    if unit in ("mmHg", "bpm", "steps"):
        return f"{v:.0f}"
    if unit == "kg":
        return f"{v:.1f} kg"
    return f"{v:.4g}"


def _run_context(db, run: AnalysisRun, lang: str) -> dict:
    labels = _labels(lang)
    result = run.result_json or {}
    findings = [
        {**f, "metric_label": labels.get(f.get("metric"), f.get("metric")),
         "before_s": _fmt_val(f.get("before"), f.get("unit") or ""),
         "after_s": _fmt_val(f.get("after"), f.get("unit") or ""),
         "delta_s": _fmt_val(f.get("delta"), f.get("unit") or ""),
         "anchor_s": f.get("anchor_date")}
        for f in result.get("findings", [])
    ]
    visit = result.get("visit") or {}
    return {
        "run": {
            "period_start": run.period_start.isoformat(),
            "period_end": run.period_end.isoformat(),
            "engine_version": run.engine_version,
            "thresholds_version": run.thresholds_version,
            "data_fingerprint": (run.data_fingerprint or "")[:12],
        },
        "metrics": result.get("metrics", []),
        "findings": findings,
        "unknowns": result.get("unknowns", []),
        "narrative_text": run.narrative_text,
        "narrative_status": run.narrative_status,
        "visit": visit,
        "labels": labels,
    }


def _card_context(db, user: User, lang: str) -> dict:
    from drhiro_api.routers.health_card import _card_out

    row = db.query(HealthProfile).filter(HealthProfile.user_id == user.id).first()
    if not row:
        row = HealthProfile(user_id=user.id)
        db.add(row)
        db.flush()
    card = _card_out(db, user, row)
    return {"card": card, "labels": _labels(lang)}


def build_report(file_id: str) -> dict:
    if not HAS_WEASYPRINT or Environment is None:
        gfiles.fail(file_id, "PDF engine unavailable: weasyprint/jinja2 not installed in this image")
        return {"status": "failed", "reason": "weasyprint missing"}
    db = SessionLocal()
    try:
        f = db.get(GeneratedFile, uuid.UUID(file_id))
        if not f or f.status not in ("queued", "failed"):
            return {"skipped": f and f.status}
        user = db.get(User, f.user_id)
        params = f.params_json or {}
        template_name = params.get("template", "analysis")
        lang = params.get("lang") if params.get("lang") in ("en", "hr") else (user.locale or "en")

        gfiles.begin(file_id)

        env = _env()
        try:
            tpl = env.get_template(f"{template_name}.html.j2")
        except Exception:
            gfiles.fail(file_id, f"template not found: {template_name}.html.j2")
            return {"status": "failed", "reason": "template missing"}

        ctx = {"lang": lang, "generated_at": datetime.now(timezone.utc).isoformat()}
        if template_name in ("analysis", "visit"):
            run = db.get(AnalysisRun, uuid.UUID(str(params.get("run_id"))))
            if not run:
                gfiles.fail(file_id, f"run {params.get('run_id')} not found")
                return {"status": "failed", "reason": "run missing"}
            ctx.update(_run_context(db, run, lang))
        elif template_name == "health_card":
            ctx.update(_card_context(db, user, lang))
        else:
            gfiles.fail(file_id, f"unknown template: {template_name}")
            return {"status": "failed", "reason": "unknown template"}

        html = tpl.render(**ctx)
        pdf_bytes = __import__("weasyprint", fromlist=["HTML"]).HTML(
            string=html, base_url=TEMPLATES_DIR
        ).write_pdf()

        names = {"analysis": "drhiro-analysis", "visit": "drhiro-visit-summary", "health_card": "drhiro-health-card"}
        filename = f"{names.get(template_name, 'drhiro-report')}-{datetime.now(timezone.utc).strftime('%Y%m%d')}.pdf"
        gfiles.write_bytes(file_id, pdf_bytes, filename, "application/pdf")
        return {"status": "ready", "size_bytes": len(pdf_bytes)}
    except Exception as e:
        gfiles.fail(file_id, repr(e)[:2000])
        raise
    finally:
        db.close()
