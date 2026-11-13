"""Questionnarium backend tests (0.12.0).

The model lab and the chat gateway are monkeypatched: these tests exercise the
router, the record curation and persistence, never the real GPU box.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from drhiro_api.models import (
    AuditEvent,
    HealthProfile,
    Meal,
    MealItem,
    Measurement,
    MedicalRecord,
    MedicationItem,
    QuestionnariumQuestion,
)
from drhiro_api.services import questionnarium_context as qctx
from drhiro_api.services import questionnarium_lab as lab
from drhiro_api.services import questionnarium_llm as llm
from tests.conftest import auth_headers


def _seed_record(db, user):
    db.add(HealthProfile(
        user_id=user.id,
        blood_type="A+",
        conditions_json=[
            {"name": "Type 2 diabetes mellitus", "status": "active", "since": "2019-05"},
            {"name": "Hypertension", "status": "monitoring", "since": "2015-01"},
        ],
        allergies_json=[
            {"substance": "Penicillin", "category": "drug", "severity": "severe", "confirmed_by_hcp": True},
        ],
        intolerances_json=[{"substance": "Lactose"}],
    ))
    db.add(MedicationItem(
        user_id=user.id, kind="medication", name="Metformin", status="active",
        dose_amount=500, dose_unit="mg", reason="type 2 diabetes",
        start_date=date(2019, 5, 1),
        ingredients_json=[{"name": "metformin hydrochloride", "amount": 500, "unit": "mg"}],
    ))
    db.add(MedicationItem(
        user_id=user.id, kind="supplement", name="Vitamin D3", status="stopped",
        dose_amount=2000, dose_unit="IU",
    ))
    db.add(MedicalRecord(
        user_id=user.id, title="Laboratory findings",
        content="HbA1c 6.8 % (ref < 6.5) | creatinine 78 µmol/L | eGFR 92",
        recorded_at=datetime.now(timezone.utc) - timedelta(days=20),
    ))
    db.add(Measurement(
        user_id=user.id, metric_type="weight",
        start_at=datetime.now(timezone.utc) - timedelta(days=1),
        value_json={"value": 64.2}, unit="kg",
        source_provider="manual", source_record_id="w-1",
    ))
    db.add(Measurement(
        user_id=user.id, metric_type="blood_pressure",
        start_at=datetime.now(timezone.utc) - timedelta(days=1),
        value_json={"systolic_mmhg": 124, "diastolic_mmhg": 80, "pulse_bpm": 62},
        unit="mmHg", source_provider="manual", source_record_id="bp-1",
    ))
    meal = Meal(
        user_id=user.id, eaten_at=datetime.now(timezone.utc) - timedelta(hours=6),
        meal_type="dinner", status="confirmed",
        totals_json={"kcal": 620, "protein_g": 28, "carbs_g": 72, "fat_g": 20},
    )
    db.add(meal)
    db.flush()
    db.add(MealItem(meal_id=meal.id, display_name="Chicken", quantity=1, unit="serving"))
    db.add(MealItem(meal_id=meal.id, display_name="Rice", quantity=150, unit="g"))
    db.commit()


def _patch_model(monkeypatch, answer="Your record looks stable.", captured=None):
    def fake_ensure(model):
        return {"profile": lab.MODEL_PROFILES[model], "switched": False,
                "current": lab.MODEL_PROFILES[model]}

    def fake_ask(question, context_text, model, history=None, locale="en", **kwargs):
        if captured is not None:
            captured["context"] = context_text
            captured["question"] = question
            captured["history"] = history
            captured["model"] = model
        return {"answer": answer, "served_model": "test-model", "usage": {"total_tokens": 10}}

    monkeypatch.setattr(lab, "ensure_model", fake_ensure)
    monkeypatch.setattr(llm, "ask_model", fake_ask)


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #

def test_models_lists_default_and_ling(client, user_a, monkeypatch):
    monkeypatch.setattr(lab, "lab_status", lambda *a, **k: {
        "current": "single-256k-dflash2",
        "health": {"loaded": True},
    })
    r = client.get("/api/v1/questionnarium/models", headers=auth_headers(user_a))
    assert r.status_code == 200
    models = {m["id"]: m for m in r.json()["models"]}
    assert set(models) == {"default", "ling"}
    assert models["default"]["ready"] is True
    assert models["ling"]["ready"] is False
    assert models["default"]["label"]


def test_models_reports_ling_ready(client, user_a, monkeypatch):
    monkeypatch.setattr(lab, "lab_status", lambda *a, **k: {
        "current": "ling-sante-iq3xs",
        "health": {"status": "ok"},
    })
    models = {m["id"]: m for m in client.get(
        "/api/v1/questionnarium/models", headers=auth_headers(user_a)).json()["models"]}
    assert models["ling"]["ready"] is True
    assert models["default"]["ready"] is False


def test_models_survives_lab_outage(client, user_a, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("lab down")

    monkeypatch.setattr(lab, "lab_status", boom)
    r = client.get("/api/v1/questionnarium/models", headers=auth_headers(user_a))
    assert r.status_code == 200
    assert all(m["ready"] is False for m in r.json()["models"])


# --------------------------------------------------------------------------- #
# ask
# --------------------------------------------------------------------------- #

def test_ask_grounds_answer_in_record_and_persists(client, db, user_a, monkeypatch):
    _seed_record(db, user_a)
    captured = {}
    answer = (
        "Your latest blood pressure is 124/80 mmHg.\n"
        "```chart\n"
        '{"type":"line","title":"Blood pressure","x":["day 1","day 2"],'
        '"series":[{"name":"systolic","values":[122,124]}],"unit":"mmHg"}\n'
        "```\n"
    )
    _patch_model(monkeypatch, answer=answer, captured=captured)

    r = client.post("/api/v1/questionnarium/ask", headers=auth_headers(user_a),
                    json={"question": "What is my blood pressure?", "model": "default"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == "Your latest blood pressure is 124/80 mmHg."
    assert body["chart"]["type"] == "line"
    assert body["model"] == "default"
    assert isinstance(body["latency_ms"], int) and body["latency_ms"] >= 0
    for source in ("health card conditions", "allergies", "current medications",
                   "recent measurements", "recent meals", "medical records"):
        assert source in body["sources"], source

    context = captured["context"]
    assert "Type 2 diabetes mellitus" in context
    assert "Penicillin" in context
    assert "Metformin" in context
    assert "HbA1c" in context
    assert "Chicken" in context
    assert "124/80" in context

    rows = db.query(QuestionnariumQuestion).filter_by(user_id=user_a.id).all()
    assert len(rows) == 1
    assert rows[0].status == "ok"
    assert rows[0].answer == body["answer"]
    assert rows[0].model == "default"

    audits = db.query(AuditEvent).filter_by(action="questionnarium.ask").all()
    assert len(audits) == 1
    assert audits[0].resource_id == str(rows[0].id)
    assert audits[0].user_id_affected == user_a.id


def test_ask_history_is_passed_to_the_model(client, db, user_a, monkeypatch):
    _seed_record(db, user_a)
    captured = {}
    _patch_model(monkeypatch, captured=captured)
    history = [
        {"role": "user", "content": "Earlier question"},
        {"role": "assistant", "content": "Earlier answer"},
    ]
    r = client.post("/api/v1/questionnarium/ask", headers=auth_headers(user_a),
                    json={"question": "Follow-up?", "model": "default", "history": history})
    assert r.status_code == 200
    assert captured["history"] == history


def test_ask_rejects_bad_input(client, db, user_a, monkeypatch):
    _patch_model(monkeypatch)
    h = auth_headers(user_a)
    assert client.post("/api/v1/questionnarium/ask", headers=h,
                       json={"question": "   "}).status_code == 400  # blank after strip
    assert client.post("/api/v1/questionnarium/ask", headers=h,
                       json={"question": "x", "model": "gpt-9"}).status_code == 400


def test_ask_returns_503_when_lab_unreachable(client, db, user_a, monkeypatch):
    def boom(model):
        raise lab.LabError("model lab is unreachable: connection refused")

    monkeypatch.setattr(lab, "ensure_model", boom)
    r = client.post("/api/v1/questionnarium/ask", headers=auth_headers(user_a),
                    json={"question": "Hello?", "model": "ling"})
    assert r.status_code == 503
    assert "unreachable" in r.json()["detail"]
    row = db.query(QuestionnariumQuestion).filter_by(user_id=user_a.id).first()
    assert row.status == "failed"
    assert "unreachable" in row.error


def test_ask_returns_502_when_model_fails(client, db, user_a, monkeypatch):
    monkeypatch.setattr(lab, "ensure_model", lambda model: {"profile": lab.MODEL_PROFILES[model]})
    monkeypatch.setattr(llm, "ask_model",
                        lambda *a, **k: (_ for _ in ()).throw(llm.QuestionnariumError("no answer")))
    r = client.post("/api/v1/questionnarium/ask", headers=auth_headers(user_a),
                    json={"question": "Hello?", "model": "default"})
    assert r.status_code == 502


# --------------------------------------------------------------------------- #
# history / tenancy
# --------------------------------------------------------------------------- #

def test_history_is_tenant_scoped(client, db, user_a, user_b, monkeypatch):
    _seed_record(db, user_a)
    _patch_model(monkeypatch)
    r = client.post("/api/v1/questionnarium/ask", headers=auth_headers(user_a),
                    json={"question": "Mine?", "model": "default"})
    qid = r.json()["id"]

    ha = auth_headers(user_a)
    hb = auth_headers(user_b)
    assert client.get("/api/v1/questionnarium/history", headers=ha).json()["count"] == 1
    assert client.get("/api/v1/questionnarium/history", headers=hb).json()["count"] == 0
    assert client.get(f"/api/v1/questionnarium/history/{qid}", headers=hb).status_code == 404
    assert client.delete(f"/api/v1/questionnarium/history/{qid}", headers=hb).status_code == 404

    assert client.delete(f"/api/v1/questionnarium/history/{qid}", headers=ha).json() == {"ok": True}
    assert client.get("/api/v1/questionnarium/history/{qid}", headers=ha).status_code == 404


# --------------------------------------------------------------------------- #
# context curation
# --------------------------------------------------------------------------- #

def test_context_budget_and_full_must_sections(client, db, user_a):
    _seed_record(db, user_a)
    # A lot of long documents: older data must collapse, not blow the budget.
    for i in range(25):
        db.add(MedicalRecord(
            user_id=user_a.id, title=f"Document {i}",
            content=("lab line " * 220),
            recorded_at=datetime.now(timezone.utc) - timedelta(days=30 + i),
        ))
    db.commit()

    ling = qctx.build_context(db, user_a, "ling")
    assert ling["chars"] <= qctx.MODEL_CONTEXT_BUDGET["ling"]
    assert "Type 2 diabetes mellitus" in ling["text"]
    assert "Penicillin" in ling["text"]
    assert "Metformin" in ling["text"]
    assert "records_recent" in ling["sections"]
    assert "medical records" in ling["sources"]
    assert ling["truncated"] is True

    default = qctx.build_context(db, user_a, "default")
    assert default["chars"] <= qctx.MODEL_CONTEXT_BUDGET["default"]
    assert "Type 2 diabetes mellitus" in default["text"]


def test_lab_mapping_helpers():
    assert lab.model_from_profile("single-256k-dflash2") == "default"
    assert lab.model_from_profile("dual-2x128k-mtp") == "default"
    assert lab.model_from_profile("ling-sante-iq3xs") == "ling"
    assert lab.model_from_profile("something-else") is None
    assert lab.model_from_profile(None) is None
    assert lab.profile_health({"loaded": True}) is True
    assert lab.profile_health({"status": "ok"}) is True
    assert lab.profile_health({"status": "loading"}) is False
    assert lab.profile_health(None) is False


@pytest.mark.parametrize("locale", ["en", "hr"])
def test_chart_extraction_shapes(locale):
    spec, text = llm.extract_chart('Intro\n```chart\n{"type":"bar","x":["a","b"],'
                                   '"series":[{"name":"n","values":[1,2]}]}\n```\nOutro')
    assert spec is not None
    assert spec["type"] == "bar"
    assert "Intro" in text and "Outro" in text and "chart" not in text
    assert llm.extract_chart("no chart here") == (None, "no chart here")
    # invalid JSON is left alone
    bad = '```chart\nnot json\n```'
    assert llm.extract_chart(bad) == (None, bad)


# --------------------------------------------------------------------------- #
# reasoning-budget retry (Ling emits no content until its CoT fits)
# --------------------------------------------------------------------------- #

def test_ask_model_uses_a_per_model_budget(monkeypatch):
    calls = []

    def fake_complete(gateway, payload, budget):
        calls.append(payload["max_tokens"])
        return {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}

    monkeypatch.setattr(llm, "_complete", fake_complete)
    monkeypatch.setattr(llm, "_served_model_id", lambda *a, **k: "ling-sante")
    llm.ask_model("q", "ctx", "ling")
    assert calls == [llm.MODEL_MAX_TOKENS["ling"]]


def test_ask_model_retries_when_reasoning_eats_the_budget(monkeypatch):
    calls = []

    def fake_complete(gateway, payload, budget):
        calls.append(payload["max_tokens"])
        if len(calls) == 1:
            return {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}
        return {"choices": [{"finish_reason": "stop", "message": {"content": "answer"}}],
                "model": "ling-sante"}

    monkeypatch.setattr(llm, "_complete", fake_complete)
    monkeypatch.setattr(llm, "_served_model_id", lambda *a, **k: "ling-sante")
    out = llm.ask_model("q", "ctx", "ling")
    assert out["answer"] == "answer"
    assert calls == [llm.MODEL_MAX_TOKENS["ling"],
                     min(llm.MODEL_MAX_TOKENS["ling"] * 2, llm.MAX_TOKENS_CEILING)]


def test_ask_model_does_not_retry_a_genuine_empty_answer(monkeypatch):
    calls = []

    def fake_complete(gateway, payload, budget):
        calls.append(payload["max_tokens"])
        return {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}

    monkeypatch.setattr(llm, "_complete", fake_complete)
    monkeypatch.setattr(llm, "_served_model_id", lambda *a, **k: "ling-sante")
    with pytest.raises(llm.QuestionnariumError):
        llm.ask_model("q", "ctx", "ling")
    assert len(calls) == 1
