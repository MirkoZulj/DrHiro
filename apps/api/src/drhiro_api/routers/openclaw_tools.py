"""OpenClaw tool contract. Section 9.

The OpenClaw gateway presents a signed service token (type=service) and
identifies the user by telegram_id in the X-Telegram-Id header, resolved
server-side. The LLM never passes an arbitrary user_id for ordinary user
tools.

Each tool maps 1:1 to a function the drHiro skill exposes to the agent.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, selectinload

from drhiro_api.db import get_db
from drhiro_api.deps import get_user_by_telegram_id
from drhiro_api.models import Alert, Goal, Meal, Measurement, Reminder, ReminderOccurrence, User
from drhiro_api.routers.auth import mint_web_login_code
from drhiro_api.routers.dashboard import _measurements_since
from drhiro_api.security import audit, validate_service_token
from drhiro_rules.calculations import weight_trend
from drhiro_schema.metrics import MetricType

router = APIRouter(prefix="/tools", tags=["openclaw-tools"])


def _resolve_user(
    db: Session = Depends(get_db),
    x_telegram_id: str | None = Header(default=None),
    x_service_token: str | None = Header(default=None),
) -> User:
    if not x_service_token or not validate_service_token(x_service_token):
        raise HTTPException(status_code=401, detail="Invalid service token")
    if not x_telegram_id:
        raise HTTPException(status_code=401, detail="Missing X-Telegram-Id")
    user = get_user_by_telegram_id(db, x_telegram_id)
    if not user or user.status != "active":
        raise HTTPException(status_code=404, detail="User not paired")
    return user


def _tool_audit(db: Session, user: User, action: str, resource_type: str | None = None, resource_id: str | None = None):
    audit(db, "openclaw", str(user.id), user.id, action, resource_type, resource_id)


class ToolResponse(BaseModel):
    ok: bool
    data: dict | list | None = None
    message: str | None = None


@router.get("/get_my_today_summary", response_model=ToolResponse)
def tool_today_summary(user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    from drhiro_api.routers.dashboard import dashboard_today
    summary = dashboard_today(user, db)
    _tool_audit(db, user, "tools.today_summary")
    db.commit()
    return ToolResponse(ok=True, data=summary)


class TrendRequest(BaseModel):
    metric: str
    period: str = "30d"


@router.post("/get_my_metric_trend", response_model=ToolResponse)
def tool_metric_trend(req: TrendRequest, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    measurements = _measurements_since(db, user.id, 90)
    if req.metric == "weight":
        data = weight_trend(measurements, 30)
    elif req.metric == "steps":
        data = {"raw_count": sum(1 for m in measurements if m["metric_type"] == MetricType.STEPS)}
    elif req.metric == "blood_pressure":
        data = {"raw_count": sum(1 for m in measurements if m["metric_type"] == MetricType.BLOOD_PRESSURE)}
    else:
        data = {"raw_count": sum(1 for m in measurements if m["metric_type"] == req.metric)}
    _tool_audit(db, user, "tools.metric_trend", "measurement", req.metric)
    db.commit()
    return ToolResponse(ok=True, data=data)


class ManualWeightTool(BaseModel):
    value: float = Field(ge=20, le=400)
    unit: str = "kg"
    measured_at: datetime | None = None


@router.post("/create_manual_weight", response_model=ToolResponse)
def tool_create_weight(req: ManualWeightTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    m = Measurement(
        user_id=user.id,
        metric_type=MetricType.WEIGHT,
        start_at=req.measured_at or datetime.now(timezone.utc),
        end_at=req.measured_at or datetime.now(timezone.utc),
        value_json={"weight_kg": req.value},
        unit="kg",
        source_provider="manual",
        source_record_id=f"tool-{uuid.uuid4().hex}",
        recording_method="manual",
        confidence=1.0,
    )
    db.add(m)
    db.flush()
    _tool_audit(db, user, "tools.manual_weight", "measurement", str(m.id))
    db.commit()
    return ToolResponse(ok=True, message=f"Weight {req.value} kg recorded.", data={"id": str(m.id)})


class ManualBpTool(BaseModel):
    systolic: int = Field(ge=40, le=300)
    diastolic: int = Field(ge=20, le=200)
    pulse: int | None = None
    measured_at: datetime | None = None
    context: str | None = None


@router.post("/create_manual_bp", response_model=ToolResponse)
def tool_create_bp(req: ManualBpTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    m = Measurement(
        user_id=user.id,
        metric_type=MetricType.BLOOD_PRESSURE,
        start_at=req.measured_at or datetime.now(timezone.utc),
        end_at=req.measured_at or datetime.now(timezone.utc),
        value_json={
            "systolic_mmhg": req.systolic,
            "diastolic_mmhg": req.diastolic,
            "pulse_bpm": req.pulse,
            "body_position": req.context,
        },
        unit="mmHg",
        source_provider="manual",
        source_record_id=f"tool-{uuid.uuid4().hex}",
        recording_method="manual",
        confidence=1.0,
    )
    db.add(m)
    db.flush()
    _tool_audit(db, user, "tools.manual_bp", "measurement", str(m.id))
    db.commit()
    return ToolResponse(ok=True, message=f"BP {req.systolic}/{req.diastolic} recorded.", data={"id": str(m.id)})


class MealFromTextTool(BaseModel):
    text: str
    eaten_at: datetime | None = None
    meal_type: str | None = None


@router.post("/create_meal_from_text", response_model=ToolResponse)
def tool_meal_from_text(req: MealFromTextTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    from drhiro_api.routers.meals import MealCreateRequest, MealItemIn, create_meal
    from drhiro_api.services.text_meal_parser import parse_meal_text
    from drhiro_api.services.date_parser import AmbiguousDate, resolve_when, strip_when

    # Resolve any relative date ("yesterday", "on Monday") in the USER's
    # timezone, server-side. The LLM never does date arithmetic: a wrong date
    # silently corrupts the record, so only its words are forwarded here.
    food_text = req.text
    resolved_when = None
    resolved_meal_type = None
    try:
        resolved_when, phrase, resolved_meal_type = resolve_when(
            req.text,
            getattr(user, "timezone", None) or "Europe/Zagreb",
            meal_type=req.meal_type,
        )
        if phrase:
            stripped = strip_when(req.text)
            if stripped:
                food_text = stripped
    except AmbiguousDate as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        pass

    # The raw text is preserved in notes; the parser derives the structured
    # items so nutrition actually attaches. The LLM upstream only supplies
    # cleaned free text -- all structure is derived here, deterministically.
    try:
        parsed = parse_meal_text(db, food_text)
    except Exception:
        parsed = []
    items = []
    for entry in parsed:
        try:
            items.append(MealItemIn(**entry))
        except Exception:
            continue

    meal_req = MealCreateRequest(
        eaten_at=req.eaten_at or resolved_when or datetime.now(timezone.utc),
        meal_type=req.meal_type or resolved_meal_type,
        notes=req.text,
        items=items,
        input_method="text",
    )
    meal = create_meal(meal_req, user, db)
    _tool_audit(db, user, "tools.meal_from_text", "meal", meal.id)
    db.commit()
    return ToolResponse(ok=True, message="Meal draft created from text.", data={"meal_id": meal.id, "status": meal.status})


class MealItemPatchTool(BaseModel):
    display_name: str | None = None
    quantity: float | None = None
    unit: str | None = None
    grams: float | None = None


class UpdateMealItemTool(BaseModel):
    meal_id: str
    item_id: str
    patch: MealItemPatchTool


@router.post("/update_meal_item", response_model=ToolResponse)
def tool_update_meal_item(req: UpdateMealItemTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    from drhiro_api.routers.meals import MealItemPatch, patch_meal_item
    meal = db.query(Meal).filter(Meal.id == uuid.UUID(req.meal_id), Meal.user_id == user.id).first()
    if not meal:
        raise HTTPException(status_code=404, detail="Meal not found")
    patch = MealItemPatch(**req.patch.model_dump(exclude_none=True))
    out = patch_meal_item(req.meal_id, req.item_id, patch, user, db)
    _tool_audit(db, user, "tools.meal_item_update", "meal_item", req.item_id)
    db.commit()
    return ToolResponse(ok=True, data={"meal_id": out.id, "status": out.status})


class ConfirmMealTool(BaseModel):
    meal_id: str


@router.post("/confirm_meal", response_model=ToolResponse)
def tool_confirm_meal(req: ConfirmMealTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    from drhiro_api.routers.meals import confirm_meal
    meal = db.query(Meal).filter(Meal.id == uuid.UUID(req.meal_id), Meal.user_id == user.id).first()
    if not meal:
        raise HTTPException(status_code=404, detail="Meal not found")
    out = confirm_meal(req.meal_id, user, db)
    _tool_audit(db, user, "tools.meal_confirm", "meal", req.meal_id)
    db.commit()
    return ToolResponse(ok=True, message="Meal confirmed.", data={"meal_id": out.id})


@router.post("/undo_last_user_action", response_model=ToolResponse)
def tool_undo_last_action(user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """Undo the user's last action: delete the most recent measurement or
    revert the most recent meal to draft."""
    last_measurement = (
        db.query(Measurement)
        .filter(Measurement.user_id == user.id)
        .order_by(Measurement.created_at.desc())
        .first()
    )
    last_meal = (
        db.query(Meal)
        .filter(Meal.user_id == user.id)
        .order_by(Meal.created_at.desc())
        .first()
    )
    if last_measurement and (not last_meal or last_measurement.created_at >= last_meal.created_at):
        db.delete(last_measurement)
        _tool_audit(db, user, "tools.undo", "measurement", str(last_measurement.id))
        db.commit()
        return ToolResponse(ok=True, message="Last measurement undone.")
    if last_meal:
        last_meal.status = "deleted"
        _tool_audit(db, user, "tools.undo", "meal", str(last_meal.id))
        db.commit()
        return ToolResponse(ok=True, message="Last meal undone.")
    return ToolResponse(ok=False, message="Nothing to undo.")


@router.get("/list_my_reminders", response_model=ToolResponse)
def tool_list_reminders(user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    reminders = db.query(Reminder).filter(Reminder.user_id == user.id, Reminder.enabled.is_(True)).all()
    _tool_audit(db, user, "tools.list_reminders")
    db.commit()
    return ToolResponse(
        ok=True,
        data=[{"id": str(r.id), "type": r.type, "schedule_json": r.schedule_json, "next_due_at": r.next_due_at} for r in reminders],
    )


class SnoozeTool(BaseModel):
    occurrence_id: str
    duration_minutes: int = 15


@router.post("/snooze_reminder", response_model=ToolResponse)
def tool_snooze_reminder(req: SnoozeTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    occ = (
        db.query(ReminderOccurrence)
        .join(Reminder)
        .filter(ReminderOccurrence.id == uuid.UUID(req.occurrence_id), Reminder.user_id == user.id)
        .first()
    )
    if not occ:
        raise HTTPException(status_code=404, detail="Occurrence not found")
    occ.status = "snoozed"
    occ.due_at = datetime.now(timezone.utc) + timedelta(minutes=req.duration_minutes)
    _tool_audit(db, user, "tools.snooze_reminder", "reminder_occurrence", req.occurrence_id)
    db.commit()
    return ToolResponse(ok=True, message=f"Snoozed {req.duration_minutes} minutes.")


class CreateReminderTool(BaseModel):
    type: str  # bp, weight, meal, water, sync, activity, bedtime, weekly
    schedule_json: dict  # {"cron": "0 8 * * 1"} or {"days": ["mon"], "time": "08:00"}
    timezone: str = "UTC"
    quiet_hours_json: dict | None = None
    escalation_policy_json: dict | None = None


@router.post("/issue_device_code", response_model=ToolResponse)
def tool_issue_device_code(user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """Issue a one-time device code for the current user.

    The user enters this code in the drHiro Android Bridge app to link
    their phone. The code is user-bound, expires in 10 minutes, and the
    link persists after exchange (the bridge stores its tokens and
    auto-refreshes).
    """
    from drhiro_api.routers.auth import _DEVICE_CODES, create_device_code

    code = create_device_code()
    _DEVICE_CODES[code] = {"user_id": str(user.id), "expires": 600}
    _tool_audit(db, user, "tools.issue_device_code", "device")
    db.commit()
    return ToolResponse(
        ok=True,
        message=f"Your device code is: {code}\n\nOpen the drHiro Bridge app, enter this code once, and grant Health Connect permissions. It expires in 10 minutes.",
        data={"device_code": code, "expires_in_seconds": 600},
    )


@router.post("/create_reminder", response_model=ToolResponse)
def tool_create_reminder(req: CreateReminderTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    """Create a reminder for the current user.

    schedule_json supports:
      - {"cron": "0 8 * * 1"}         (5-field cron: minute hour dom month dow)
      - {"days": ["mon", "wed"], "time": "08:00"}
    The API computes next_due_at in the user's timezone.
    """
    from drhiro_api.routers.reminders import _compute_next_due

    if "cron" not in req.schedule_json and "days" not in req.schedule_json:
        raise HTTPException(status_code=400, detail="schedule_json needs 'cron' or 'days'")
    reminder = Reminder(
        user_id=user.id,
        type=req.type,
        schedule_json=req.schedule_json,
        timezone=req.timezone,
        enabled=True,
        quiet_hours_json=req.quiet_hours_json,
        escalation_policy_json=req.escalation_policy_json,
    )
    db.add(reminder)
    db.flush()
    reminder.next_due_at = _compute_next_due(reminder)
    _tool_audit(db, user, "tools.create_reminder", "reminder", str(reminder.id))
    db.commit()
    return ToolResponse(
        ok=True,
        message=f"Reminder set: {req.type} with schedule {req.schedule_json} (next: {reminder.next_due_at}).",
        data={"reminder_id": str(reminder.id), "next_due_at": reminder.next_due_at},
    )


class SetGoalTool(BaseModel):
    goal_type: str
    target_json: dict
    period: str | None = None


@router.post("/set_user_goal", response_model=ToolResponse)
def tool_set_goal(req: SetGoalTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    goal = Goal(user_id=user.id, goal_type=req.goal_type, target_json=req.target_json, source="user")
    if req.period and req.period.lower().endswith("d"):
        try:
            days = int(req.period[:-1])
            goal.start_date = datetime.now(timezone.utc).date().isoformat()
            goal.end_date = (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()
        except ValueError:
            pass
    db.add(goal)
    db.flush()
    _tool_audit(db, user, "tools.set_goal", "goal", str(goal.id))
    db.commit()
    return ToolResponse(ok=True, message="Goal set.", data={"goal_id": str(goal.id)})


@router.get("/get_my_active_alerts", response_model=ToolResponse)
def tool_active_alerts(user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    alerts = db.query(Alert).filter(Alert.user_id == user.id, Alert.status == "open").order_by(Alert.created_at.desc()).all()
    _tool_audit(db, user, "tools.active_alerts")
    db.commit()
    return ToolResponse(
        ok=True,
        data=[
            {
                "id": str(a.id),
                "rule_code": a.rule_code,
                "severity": a.severity,
                "explanation_template": a.explanation_template,
                "params_json": a.params_json,
                "created_at": a.created_at,
            }
            for a in alerts
        ],
    )


class AcknowledgeAlertTool(BaseModel):
    alert_id: str


@router.post("/acknowledge_alert", response_model=ToolResponse)
def tool_acknowledge_alert(req: AcknowledgeAlertTool, user: User = Depends(_resolve_user), db: Session = Depends(get_db)):
    alert = db.query(Alert).filter(Alert.id == uuid.UUID(req.alert_id), Alert.user_id == user.id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.status = "acknowledged"
    alert.acknowledged_at = datetime.now(timezone.utc)
    _tool_audit(db, user, "tools.acknowledge_alert", "alert", req.alert_id)
    db.commit()
    return ToolResponse(ok=True, message="Alert acknowledged.")


@router.post("/issue_web_login_link", response_model=ToolResponse)
def tool_issue_web_login_link(
    user: User = Depends(_resolve_user),
    db: Session = Depends(get_db),
    x_telegram_id: str | None = Header(default=None),
):
    """Mint a one-click dashboard login link for the user's Telegram identity.

    Returns a URL the bot can DM; tapping it exchanges the code for real
    tokens (POST /auth/telegram-link/complete) and lands on the dashboard
    with no OTP entry. Works for already-paired users.
    """
    link_code = mint_web_login_code(x_telegram_id or "")
    web_base = os.getenv("DRHIRO_WEB_BASE", "").rstrip("/")
    if not web_base:
        return ToolResponse(
            ok=False,
            message="Web base URL not configured (set DRHIRO_WEB_BASE).",
            data={},
        )
    url = f"{web_base}/auth/link?link={link_code}"
    _tool_audit(db, user, "tools.issue_web_login_link", "user", str(user.id))
    db.commit()
    return ToolResponse(
        ok=True,
        message="Dashboard login link minted.",
        data={"url": url, "link_code": link_code, "expires_in": 1800},
    )
