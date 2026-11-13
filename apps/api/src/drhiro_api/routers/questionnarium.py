"""Questionnarium JWT API (0.12.0) — patient health Q&A grounded in the record.

Contract with the web client (apps/web/src/views/Questionnarium.tsx):

    GET  /api/v1/questionnarium/models
         -> {"models": [{"id": "default"|"ling", "label", "ready", "note?"}]}
    POST /api/v1/questionnarium/ask  {question, model?, history?}
         -> {answer, model, chart?, sources?, latency_ms?}
         or {"detail": ...} with a non-2xx status.

Every route is tenant-scoped by user_id. The model switch is served by the
shared model lab and is therefore global; a switch is issued only when the
requested model is not already the loaded one.
"""

from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from drhiro_api.config import get_settings
from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.models import QuestionnariumQuestion, User
from drhiro_api.security import audit
from drhiro_api.services import questionnarium_context as qctx
from drhiro_api.services import questionnarium_lab as lab
from drhiro_api.services import questionnarium_llm as llm
from drhiro_api.services.questionnarium_llm import QuestionnariumError, extract_chart

router = APIRouter(prefix="/questionnarium", tags=["questionnarium"])


class HistoryTurn(BaseModel):
    role: str = "user"
    content: str = Field(min_length=1, max_length=4000)


class AskRequest(BaseModel):
    # Generous transport cap; the configured limit is enforced in the handler.
    question: str = Field(min_length=1, max_length=10_000)
    model: str | None = None
    history: list[HistoryTurn] | None = None


def _clean_history(raw: list[HistoryTurn] | None, max_turns: int) -> list[dict]:
    if not raw:
        return []
    out = []
    for turn in raw[-max_turns:]:
        role = "assistant" if turn.role == "assistant" else "user"
        content = turn.content.strip()
        if content:
            out.append({"role": role, "content": content})
    return out


def _out(row: QuestionnariumQuestion) -> dict:
    return {
        "id": str(row.id),
        "question": row.question,
        "answer": row.answer,
        "model": row.model,
        "profile": row.profile,
        "sources": row.sources_json or [],
        "context": row.context_json,
        "latency_ms": row.latency_ms,
        "status": row.status,
        "error": row.error,
        "created_at": row.created_at,
    }


def _get_question(db: Session, user: User, question_id: str) -> QuestionnariumQuestion:
    try:
        qid = uuid.UUID(question_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Question not found")
    row = (
        db.query(QuestionnariumQuestion)
        .filter(QuestionnariumQuestion.id == qid, QuestionnariumQuestion.user_id == user.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Question not found")
    return row


@router.get("/models")
def list_models(user: User = Depends(get_current_user)):
    """The two selectable models and whether each is currently loaded."""
    current = None
    healthy = False
    lab_ok = True
    try:
        status = lab.lab_status()
        current = status.get("current")
        healthy = lab.profile_health(status.get("health"))
    except Exception:  # noqa: BLE001 - the web client treats this as optional
        lab_ok = False
    current_model = lab.model_from_profile(current)

    models = []
    for model_id in ("default", "ling"):
        ready = bool(lab_ok and healthy and current_model == model_id)
        note = lab.MODEL_NOTES[model_id]
        if not lab_ok:
            note = "model status is unavailable right now"
        elif not ready:
            note = note + " Not loaded — the first request will load it."
        models.append({
            "id": model_id,
            "label": lab.MODEL_LABELS[model_id],
            "ready": ready,
            "note": note,
        })
    return {"models": models, "current": current, "current_model": current_model, "lab_ok": lab_ok}


@router.post("/ask")
def ask(req: AskRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Answer one health question from the patient's own record."""
    started = time.monotonic()
    settings = get_settings()
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question is required")
    if len(question) > settings.questionnarium_max_question_chars:
        raise HTTPException(
            status_code=400,
            detail=f"question is too long (max {settings.questionnarium_max_question_chars} characters)",
        )

    model = (req.model or settings.questionnarium_default_model).strip().lower()
    if model not in lab.MODEL_PROFILES:
        raise HTTPException(status_code=400, detail="model must be 'default' or 'ling'")
    history = _clean_history(req.history, settings.questionnarium_max_history_turns)

    answer = None
    chart = None
    sources: list[str] = []
    context_meta = None
    profile = None
    status = "ok"
    error = None
    failure_status = 503

    try:
        switch = lab.ensure_model(model)
        profile = switch.get("profile")
        context = qctx.build_context(db, user, model)
        sources = context["sources"]
        context_meta = {
            "sections": context["sections"],
            "chars": context["chars"],
            "estimated_tokens": context["estimated_tokens"],
            "truncated": context["truncated"],
            "budget_chars": context["budget_chars"],
        }
        result = llm.ask_model(question, context["text"], model, history=history, locale=user.locale)
        answer = result["answer"]
        chart, answer = extract_chart(answer)
    except lab.LabError as exc:
        status, error, failure_status = "failed", str(exc), 503
    except QuestionnariumError as exc:
        status, error, failure_status = "failed", str(exc), 502
    except Exception as exc:  # noqa: BLE001 - never leak a stack trace to the patient
        status, error, failure_status = "failed", "the question could not be answered", 500
        import logging

        logging.getLogger(__name__).exception("questionnarium ask failed")

    latency_ms = int((time.monotonic() - started) * 1000)
    row = QuestionnariumQuestion(
        user_id=user.id,
        question=question,
        answer=answer,
        model=model,
        profile=profile,
        sources_json=sources,
        context_json=context_meta,
        latency_ms=latency_ms,
        status=status,
        error=error,
    )
    db.add(row)
    db.flush()  # materialise row.id so the audit event can reference it
    audit(
        db,
        "user",
        str(user.id),
        user.id,
        "questionnarium.ask",
        "questionnarium_question",
        str(row.id),
        {"model": model, "status": status, "sources": sources, "latency_ms": latency_ms},
    )
    db.commit()

    if status != "ok":
        raise HTTPException(status_code=failure_status, detail=error or "the question could not be answered")

    db.refresh(row)
    return {
        "id": str(row.id),
        "answer": answer,
        "model": model,
        "profile": profile,
        "chart": chart,
        "sources": sources,
        "latency_ms": latency_ms,
        "context": context_meta,
    }


@router.get("/history")
def list_history(limit: int = 20, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    limit = max(1, min(limit, 100))
    rows = (
        db.query(QuestionnariumQuestion)
        .filter(QuestionnariumQuestion.user_id == user.id)
        .order_by(QuestionnariumQuestion.created_at.desc())
        .limit(limit)
        .all()
    )
    return {"items": [_out(r) for r in rows], "count": len(rows)}


@router.get("/history/{question_id}")
def get_history(question_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return {"item": _out(_get_question(db, user, question_id))}


@router.delete("/history/{question_id}")
def delete_history(question_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = _get_question(db, user, question_id)
    db.delete(row)
    audit(db, "user", str(user.id), user.id, "questionnarium.delete", "questionnarium_question", question_id, {})
    db.commit()
    return {"ok": True}
