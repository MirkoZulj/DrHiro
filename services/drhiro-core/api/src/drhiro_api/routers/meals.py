"""Meals endpoints. Sections 5 and 8.3.

Photo-derived meals are DRAFTS until confirmed; OCR/vision output can
never become confirmed data without explicit user confirmation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from sqlalchemy import or_
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, selectinload

from drhiro_api.db import get_db
from drhiro_api.deps import get_current_user
from drhiro_api.food_search import nutrient_map, resolve_food
from drhiro_api.models import Food, FoodCatalogItem, FoodNutrient, Meal, MealItem, Nutrient, User
from drhiro_api.security import audit
from drhiro_nutrition.catalog import FoodItem, NutrientTotals, scale_nutrients
from drhiro_nutrition.composite import CompositeCatalog

router = APIRouter(prefix="/meals", tags=["meals"])


class MealItemIn(BaseModel):
    food_catalog_item_id: str | None = None
    display_name: str
    quantity: float = Field(default=1.0, ge=0)
    unit: str | None = None
    grams: float | None = Field(default=None, ge=0)


class MealCreateRequest(BaseModel):
    eaten_at: datetime | None = None
    meal_type: str | None = None
    notes: str | None = None
    items: list[MealItemIn] = Field(default_factory=list)
    input_method: str = "text"


class MealOut(BaseModel):
    id: str
    user_id: str
    eaten_at: datetime
    meal_type: str | None
    status: str
    input_method: str | None
    notes: str | None
    totals_json: dict | None
    confidence: float | None
    items: list[dict]
    estimated: bool | None = None
    has_unresolved_items: bool | None = None


def _catalog() -> CompositeCatalog:
    return CompositeCatalog()


def _meal_to_out(meal: Meal) -> MealOut:
    return MealOut(
        id=str(meal.id),
        user_id=str(meal.user_id),
        eaten_at=meal.eaten_at,
        meal_type=meal.meal_type,
        status=meal.status,
        input_method=meal.input_method,
        notes=meal.notes,
        totals_json=meal.totals_json,
        confidence=meal.confidence,
        estimated=bool((meal.totals_json or {}).get("estimated")),
        has_unresolved_items=any(
            i.source == "unresolved"
            or (isinstance(i.nutrients_json, dict) and i.nutrients_json.get("unresolved"))
            or i.nutrients_json is None
            for i in meal.items
        ),
        items=[
            {
                "id": str(i.id),
                "display_name": i.display_name,
                "quantity": i.quantity,
                "unit": i.unit,
                "grams": i.grams,
                "nutrients_json": i.nutrients_json,
                "source": i.source,
                "confidence": i.confidence,
                "user_corrected": i.user_corrected,
                "resolved_food": ((i.nutrients_json or {}).get("resolved_food") if isinstance(i.nutrients_json, dict) else None),
                "unresolved": bool(
                    i.source == "unresolved"
                    or (isinstance(i.nutrients_json, dict) and i.nutrients_json.get("unresolved"))
                    or i.nutrients_json is None
                ),
            }
            for i in meal.items
        ],
    )


def _lookup_nutrients(db: Session, item: MealItemIn, user: User) -> tuple[dict | None, float, str | None]:
    """Resolve nutrients for a meal item. Tries local DB first, then composite catalog.

    Confidence semantics:
      0.8  confident local match
      0.5  ambiguous local match -- top candidate used, alternatives recorded
      0.3  unresolved

    Anything below 0.8 flips the meal's `totals_json.estimated` flag, which is
    how the UI knows to prompt. An ambiguous match must never masquerade as
    certain.
    """
    # 1. Try local DB first, ranked.
    result = resolve_food(db, item.display_name, limit=5, user_id=user.id)
    local_food = result.best
    if local_food is not None:
        code_by_id = {n.id: n.nutrient_code for n in db.query(Nutrient).all()}
        nmap = nutrient_map(local_food, code_by_id)
        if nmap:
            grams = item.grams or local_food.serving_grams or 100.0
            energy_per_100g = nmap.get("energy")
            if not energy_per_100g:
                # Most USDA rows in this catalogue lack an explicit energy
                # nutrient but do carry macros. Derive kcal with the standard
                # Atwater factors rather than reporting a food as 0 kcal.
                energy_per_100g = (
                    4.0 * (nmap.get("protein") or 0)
                    + 4.0 * (nmap.get("carbs") or 0)
                    + 9.0 * (nmap.get("fat") or 0)
                )
            totals = NutrientTotals(
                kcal=(energy_per_100g or 0) * grams / 100,
                protein_g=(nmap.get("protein") or 0) * grams / 100,
                carbs_g=(nmap.get("carbs") or 0) * grams / 100,
                fat_g=(nmap.get("fat") or 0) * grams / 100,
                fiber_g=(nmap.get("fiber") or 0) * grams / 100,
                sodium_mg=(nmap.get("sodium") or 0) * grams / 100,
                sources=["usda:fdc-v1"],
            )
            payload = _totals_to_json(totals)
            payload["resolved_food"] = local_food.display_name
            if result.ambiguous:
                # Surface the alternatives instead of silently choosing.
                payload["ambiguous"] = True
                payload["candidates"] = result.candidate_names
                return payload, 0.5, "usda"
            return payload, 0.8, "usda"

    # 2. Fall back to composite catalog
    cat = _catalog()
    try:
        food: FoodItem | None = None
        if item.food_catalog_item_id:
            food = cat.by_id(item.food_catalog_item_id)
        if food is None:
            food = cat.search(item.display_name, limit=1)[0] if cat.search(item.display_name, limit=1) else None
        if food is None:
            return _unresolved_payload(), 0.3, "unresolved"
        grams = item.grams
        if grams is None and food.serving_grams:
            grams = food.serving_grams * item.quantity
        if grams is None:
            grams = 100.0
        totals = scale_nutrients(food, grams)
        return _totals_to_json(totals), 0.7, food.source
    finally:
        cat.close()


def _unresolved_payload() -> dict:
    """Explicit unknown-nutrition payload for a food no catalogue could match.

    Never fabricates numbers: every value stays null and `unresolved` is true,
    so a client can distinguish 'genuinely 0 kcal' from 'we have no idea'.
    Pairs with confidence 0.3 / source 'unresolved' (see _lookup_nutrients).
    """
    return {
        "kcal": None,
        "protein_g": None,
        "carbs_g": None,
        "fat_g": None,
        "fiber_g": None,
        "sodium_mg": None,
        "sources": [],
        "resolved_food": None,
        "unresolved": True,
    }


def _totals_to_json(t: NutrientTotals) -> dict:
    return {
        "kcal": t.kcal,
        "protein_g": t.protein_g,
        "carbs_g": t.carbs_g,
        "fat_g": t.fat_g,
        "fiber_g": t.fiber_g,
        "sodium_mg": t.sodium_mg,
        "sources": t.sources,
    }



def _recompute_totals(meal: Meal) -> dict:
    """Recompute a meal's totals_json from its CURRENT items.

    Single source of truth for meal-level macros. Called on create and after
    every item mutation (patch / add / delete) so the meal's kcal can never
    drift away from the sum of its items.
    """
    kcal = protein = carbs = fat = fiber = 0.0
    estimated = False
    for i in meal.items:
        if (i.confidence if i.confidence is not None else 0.0) < 0.8:
            estimated = True
        n = i.nutrients_json or {}
        kcal += n.get("kcal") or 0.0
        protein += n.get("protein_g") or 0.0
        carbs += n.get("carbs_g") or 0.0
        fat += n.get("fat_g") or 0.0
        fiber += n.get("fiber_g") or 0.0
    return {
        "kcal": round(kcal, 1),
        "protein_g": round(protein, 1),
        "carbs_g": round(carbs, 1),
        "fat_g": round(fat, 1),
        "fiber_g": round(fiber, 1),
        "estimated": estimated,
    }


def _item_spec(item: MealItem) -> MealItemIn:
    """Build the MealItemIn that _lookup_nutrients expects from a stored row."""
    return MealItemIn(
        food_catalog_item_id=(str(item.food_catalog_item_id) if item.food_catalog_item_id else None),
        display_name=item.display_name,
        quantity=item.quantity if item.quantity is not None else 1.0,
        unit=item.unit,
        grams=item.grams,
    )


def _resolve_item_nutrition(db: Session, item: MealItem, user: User) -> None:
    """(Re)resolve nutrition for a persisted MealItem, in place.

    Mirrors exactly what create_meal does for a new item: the same
    _lookup_nutrients call (which scales per-100g values by grams and applies
    the Atwater kcal fallback when the food has no explicit energy nutrient),
    the same source/confidence assignment.
    """
    nutrients, conf, source = _lookup_nutrients(db, _item_spec(item), user)
    item.nutrients_json = nutrients
    item.confidence = conf
    item.source = source or "manual"


def _apply_local_food(db: Session, food: Food, grams: float) -> tuple[dict, str]:
    """Nutrients payload + source for a known local `foods` row at `grams`.

    Mirrors the confident branch of _lookup_nutrients, including the Atwater
    kcal fallback for USDA rows without an explicit energy nutrient.
    """
    code_by_id = {n.id: n.nutrient_code for n in db.query(Nutrient).all()}
    nmap = nutrient_map(food, code_by_id)
    energy_per_100g = nmap.get("energy")
    if not energy_per_100g:
        energy_per_100g = (
            4.0 * (nmap.get("protein") or 0)
            + 4.0 * (nmap.get("carbs") or 0)
            + 9.0 * (nmap.get("fat") or 0)
        )
    totals = NutrientTotals(
        kcal=(energy_per_100g or 0) * grams / 100,
        protein_g=(nmap.get("protein") or 0) * grams / 100,
        carbs_g=(nmap.get("carbs") or 0) * grams / 100,
        fat_g=(nmap.get("fat") or 0) * grams / 100,
        fiber_g=(nmap.get("fiber") or 0) * grams / 100,
        sodium_mg=(nmap.get("sodium") or 0) * grams / 100,
        sources=["usda:fdc-v1"],
    )
    payload = _totals_to_json(totals)
    payload["resolved_food"] = food.display_name
    return payload, "usda"


def _apply_explicit_food(db: Session, item: MealItem, food_ref: str) -> bool:
    """Re-point a meal item at an explicitly chosen catalogue food.

    Tries the local `foods` table first (by uuid or external_id), then the
    composite catalog by external_id -- the same identifiers
    GET /meals/foods/search hands out. Returns False (caller raises 404) when
    nothing matches. Confidence is set to the confident tier (0.8): the user,
    not a ranker, chose this food.
    """
    grams = item.grams or 100.0
    food = None
    try:
        food_uuid = uuid.UUID(str(food_ref))
    except (ValueError, AttributeError, TypeError):
        food_uuid = None
    q = db.query(Food)
    if food_uuid is not None:
        food = q.filter(or_(Food.id == food_uuid, Food.external_id == food_ref)).first()
    if food is None:
        food = db.query(Food).filter(Food.external_id == food_ref).first()
    if food is not None:
        payload, source = _apply_local_food(db, food, grams)
        item.food_catalog_item_id = str(food.external_id or food.id)
        item.display_name = food.display_name
        item.nutrients_json = payload
        item.confidence = 0.8
        item.source = source
        return True

    cat = _catalog()
    try:
        cfood = cat.by_id(food_ref)
    finally:
        cat.close()
    if cfood is None:
        return False
    totals = scale_nutrients(cfood, grams)
    item.food_catalog_item_id = food_ref
    item.display_name = cfood.display_name
    item.nutrients_json = _totals_to_json(totals)
    item.confidence = 0.8
    item.source = cfood.source
    return True


def _sync_totals(db: Session, meal: Meal) -> None:
    """Flush pending item changes, reload items, and rewrite meal.totals_json."""
    db.flush()
    db.expire(meal, ["items"])
    meal.totals_json = _recompute_totals(meal)


def _meal_uuid(meal_id: str) -> uuid.UUID:
    """Parse a meal/item id, 404 (not 500) on a malformed value."""
    try:
        return uuid.UUID(str(meal_id))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="Meal not found")


def _owned_meal(db: Session, meal_id: str, user: User) -> Meal:
    meal = (
        db.query(Meal)
        .options(selectinload(Meal.items))
        .filter(Meal.id == _meal_uuid(meal_id), Meal.user_id == user.id)
        .first()
    )
    if not meal:
        raise HTTPException(status_code=404, detail="Meal not found")
    return meal


@router.post("", response_model=MealOut)
def create_meal(req: MealCreateRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    meal = Meal(
        user_id=user.id,
        eaten_at=req.eaten_at or datetime.now(),
        meal_type=req.meal_type,
        status="confirmed" if not req.items else "needs_review",
        input_method=req.input_method,
        notes=req.notes,
        confidence=1.0 if not req.items else 0.7,
    )
    db.add(meal)
    db.flush()

    totals_kcal = totals_protein = totals_carbs = totals_fat = totals_fiber = 0.0
    estimated = False
    for item in req.items:
        nutrients, conf, source = _lookup_nutrients(db, item, user)
        estimated = estimated or conf < 0.8
        mi = MealItem(
            meal_id=meal.id,
            food_catalog_item_id=item.food_catalog_item_id,
            display_name=item.display_name,
            quantity=item.quantity,
            unit=item.unit,
            grams=item.grams,
            nutrients_json=nutrients,
            source=source or "manual",
            confidence=conf,
        )
        db.add(mi)
        if nutrients:
            totals_kcal += nutrients.get("kcal") or 0
            totals_protein += nutrients.get("protein_g") or 0
            totals_carbs += nutrients.get("carbs_g") or 0
            totals_fat += nutrients.get("fat_g") or 0
            totals_fiber += nutrients.get("fiber_g") or 0

    _ = (totals_kcal, totals_protein, totals_carbs, totals_fat, totals_fiber, estimated)
    _sync_totals(db, meal)
    audit(db, "user", str(user.id), user.id, "meals.create", "meal", str(meal.id), {"input_method": req.input_method})
    db.commit()
    db.refresh(meal)
    return _meal_to_out(meal)


@router.post("/from-text", response_model=MealOut)
def create_meal_from_text(req: MealCreateRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Natural-language meal logging.

    In the full system the text parser (LLM via OpenClaw) extracts items
    and calls this endpoint. For direct API use, items may be passed
    explicitly; the raw text is preserved in notes.
    """
    req.input_method = "text"
    return create_meal(req, user, db)


class PhotoDraftOut(BaseModel):
    meal_id: str
    status: str
    message: str = "Draft created. Confirm before this meal becomes official."


@router.post("/from-photo", response_model=PhotoDraftOut)
async def create_meal_from_photo(
    file: UploadFile = File(...),
    caption: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Photo meal logging.

    The vision worker (separate service) analyzes the image and creates a
    needs_review draft. This endpoint stores the photo reference and
    creates an empty draft; the analysis job fills items asynchronously.
    """
    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Photo too large")

    meal = Meal(
        user_id=user.id,
        eaten_at=datetime.now(),
        meal_type=None,
        status="needs_review",
        input_method="photo",
        notes=caption,
        photo_asset_id=f"pending-{uuid.uuid4().hex}",
        confidence=0.0,
    )
    db.add(meal)
    db.commit()
    db.refresh(meal)
    # TODO: enqueue vision analysis job (worker) with the photo asset id.
    audit(db, "user", str(user.id), user.id, "meals.photo_draft", "meal", str(meal.id))
    db.commit()
    return PhotoDraftOut(meal_id=str(meal.id), status="needs_review")


class BarcodeRequest(BaseModel):
    barcode: str = Field(min_length=8, max_length=32)
    eaten_at: datetime | None = None
    quantity: float = Field(default=1.0, ge=0.1, le=100)


@router.post("/from-barcode", response_model=MealOut)
def create_meal_from_barcode(req: BarcodeRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cat = _catalog()
    try:
        food = cat.by_barcode(req.barcode)
    finally:
        cat.close()
    if food is None:
        raise HTTPException(status_code=404, detail="Barcode not found in any food catalog")

    grams = food.serving_grams * req.quantity if food.serving_grams else 100.0
    totals = scale_nutrients(food, grams)
    meal = Meal(
        user_id=user.id,
        eaten_at=req.eaten_at or datetime.now(),
        meal_type=None,
        status="confirmed",
        input_method="barcode",
        confidence=1.0,
        totals_json=_totals_to_json(totals),
    )
    db.add(meal)
    db.flush()
    db.add(
        MealItem(
            meal_id=meal.id,
            food_catalog_item_id=food.external_id,
            display_name=food.display_name,
            quantity=req.quantity,
            unit=food.serving_unit,
            grams=grams,
            nutrients_json=_totals_to_json(totals),
            source=food.source,
            confidence=1.0,
        )
    )
    db.commit()
    db.refresh(meal)
    return _meal_to_out(meal)


@router.get("", response_model=list[MealOut])
def list_meals(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
):
    """List meals for the user, newest first (eaten_at desc), excluding deleted.
    
    Optional ?from=YYYY-MM-DD & to=YYYY-MM-DD filters by eaten_at date range (inclusive).
    """
    query = (
        db.query(Meal)
        .options(selectinload(Meal.items))
        .filter(Meal.user_id == user.id, Meal.status != "deleted")
    )
    if from_date:
        try:
            fd = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
            query = query.filter(Meal.eaten_at >= fd)
        except ValueError:
            pass
    if to_date:
        try:
            td = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc) + timedelta(days=1)
            query = query.filter(Meal.eaten_at < td)
        except ValueError:
            pass
    meals = query.order_by(Meal.eaten_at.desc()).all()
    return [_meal_to_out(m) for m in meals]


@router.get("/{meal_id}", response_model=MealOut)
def get_meal(meal_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    meal = (
        db.query(Meal)
        .options(selectinload(Meal.items))
        .filter(Meal.id == uuid.UUID(meal_id), Meal.user_id == user.id)
        .first()
    )
    if not meal:
        raise HTTPException(status_code=404, detail="Meal not found")
    return _meal_to_out(meal)


class MealPatchRequest(BaseModel):
    meal_type: str | None = None
    notes: str | None = None
    eaten_at: datetime | None = None


@router.patch("/{meal_id}", response_model=MealOut)
def patch_meal(meal_id: str, req: MealPatchRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    meal = (
        db.query(Meal)
        .options(selectinload(Meal.items))
        .filter(Meal.id == uuid.UUID(meal_id), Meal.user_id == user.id)
        .first()
    )
    if not meal:
        raise HTTPException(status_code=404, detail="Meal not found")
    if req.meal_type is not None:
        meal.meal_type = req.meal_type
    if req.notes is not None:
        meal.notes = req.notes
    if req.eaten_at is not None:
        meal.eaten_at = req.eaten_at
    db.commit()
    db.refresh(meal)
    return _meal_to_out(meal)


class MealItemPatch(BaseModel):
    display_name: str | None = None
    quantity: float | None = None
    unit: str | None = None
    grams: float | None = None
    # Explicit food re-pointing: same identifier shape GET /meals/foods/search
    # returns ("external_id"); "food_catalog_item_id" accepted as alias so a UI
    # can pipe search results straight in.
    food_catalog_item_id: str | None = None
    external_id: str | None = None


@router.patch("/{meal_id}/items/{item_id}", response_model=MealOut)
def patch_meal_item(meal_id: str, item_id: str, req: MealItemPatch, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Correct one item of a meal AND re-resolve its nutrition.

    Editing grams/name/quantity without recalculating nutrients_json stored
    silently-wrong calories (a 100g wine serving corrected to 150g kept the
    100g kcal). We now re-run the same resolution create uses, then rewrite
    the parent meal's totals from all items.
    """
    meal = _owned_meal(db, meal_id, user)
    item = next((i for i in meal.items if str(i.id) == str(item_id)), None)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    old_display_name = item.display_name
    if req.display_name is not None:
        item.display_name = req.display_name
    if req.quantity is not None:
        item.quantity = req.quantity
    if req.unit is not None:
        item.unit = req.unit
    if req.grams is not None:
        item.grams = req.grams
    food_ref = req.food_catalog_item_id or req.external_id
    if food_ref:
        # Explicit correction: re-point at the chosen food, re-resolve nutrition
        # FROM THAT FOOD at the item's grams, adopt its canonical name.
        if not _apply_explicit_food(db, item, food_ref):
            raise HTTPException(status_code=404, detail=f"Food not found: {food_ref}")
    else:
        # Re-resolve nutrition for the NEW field values (scales per-100g by grams,
        # keeps the Atwater kcal fallback).
        _resolve_item_nutrition(db, item, user)
    item.user_corrected = True
    _sync_totals(db, meal)
    audit(db, "user", str(user.id), user.id, "meals.item_patch", "meal", str(meal.id), {"item_id": str(item_id)})
    # Detect a display_name CORRECTION: remember the original text before the
    # rename so the LLM can learn a general rule from it.
    original_text = None
    corrected_food_id = None
    if req.display_name is not None and req.display_name != old_display_name:
        original_text = old_display_name
        corrected_food_id = (
            item.food_catalog_item_id
            or getattr(item, "food_id", None)
        )
    db.commit()
    db.refresh(meal)
    if original_text:
        # Fire-and-forget: a failed enqueue/extraction never fails the PATCH.
        from drhiro_api.services.task_queue import enqueue

        enqueue(
            "drhiro",
            "drhiro_worker.jobs_extract_food_rule.extract_food_rule_job",
            str(user.id),
            original_text,
            req.display_name,
            str(corrected_food_id) if corrected_food_id else None,
        )
    return _meal_to_out(meal)


@router.post("/{meal_id}/items", response_model=MealOut)
def add_meal_item(meal_id: str, req: MealItemIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Add one item to an existing meal, resolving nutrition like create does."""
    meal = _owned_meal(db, meal_id, user)
    nutrients, conf, source = _lookup_nutrients(db, req, user)
    mi = MealItem(
        meal_id=meal.id,
        food_catalog_item_id=req.food_catalog_item_id,
        display_name=req.display_name,
        quantity=req.quantity,
        unit=req.unit,
        grams=req.grams,
        nutrients_json=nutrients,
        source=source or "manual",
        confidence=conf,
    )
    db.add(mi)
    _sync_totals(db, meal)
    audit(db, "user", str(user.id), user.id, "meals.item_add", "meal", str(meal.id), {"display_name": req.display_name})
    db.commit()
    db.refresh(meal)
    return _meal_to_out(meal)


@router.delete("/{meal_id}/items/{item_id}", response_model=MealOut)
def remove_meal_item(meal_id: str, item_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Remove one item from a meal and recompute the meal's totals."""
    meal = _owned_meal(db, meal_id, user)
    item = next((i for i in meal.items if str(i.id) == str(item_id)), None)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    db.delete(item)
    _sync_totals(db, meal)
    audit(db, "user", str(user.id), user.id, "meals.item_remove", "meal", str(meal.id), {"item_id": str(item_id)})
    db.commit()
    db.refresh(meal)
    return _meal_to_out(meal)


@router.post("/{meal_id}/confirm", response_model=MealOut)
def confirm_meal(meal_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    meal = (
        db.query(Meal)
        .options(selectinload(Meal.items))
        .filter(Meal.id == uuid.UUID(meal_id), Meal.user_id == user.id)
        .first()
    )
    if not meal:
        raise HTTPException(status_code=404, detail="Meal not found")
    meal.status = "confirmed"
    meal.confirmed_at = datetime.now()
    meal.confidence = 1.0
    audit(db, "user", str(user.id), user.id, "meals.confirm", "meal", str(meal.id))
    db.commit()
    db.refresh(meal)
    return _meal_to_out(meal)


@router.post("/{meal_id}/copy", response_model=MealOut)
def copy_meal(meal_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    source = (
        db.query(Meal)
        .options(selectinload(Meal.items))
        .filter(Meal.id == uuid.UUID(meal_id), Meal.user_id == user.id)
        .first()
    )
    if not source:
        raise HTTPException(status_code=404, detail="Meal not found")
    meal = Meal(
        user_id=user.id,
        eaten_at=datetime.now(),
        meal_type=source.meal_type,
        status="confirmed",
        input_method="copy",
        notes=source.notes,
        totals_json=source.totals_json,
        confidence=1.0,
    )
    db.add(meal)
    db.flush()
    for i in source.items:
        db.add(
            MealItem(
                meal_id=meal.id,
                food_catalog_item_id=i.food_catalog_item_id,
                display_name=i.display_name,
                quantity=i.quantity,
                unit=i.unit,
                grams=i.grams,
                nutrients_json=i.nutrients_json,
                source=i.source,
                confidence=i.confidence,
            )
        )
    db.commit()
    db.refresh(meal)
    return _meal_to_out(meal)


@router.delete("/{meal_id}")
def delete_meal(meal_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    meal = db.query(Meal).filter(Meal.id == uuid.UUID(meal_id), Meal.user_id == user.id).first()
    if not meal:
        raise HTTPException(status_code=404, detail="Meal not found")
    meal.status = "deleted"
    audit(db, "user", str(user.id), user.id, "meals.delete", "meal", str(meal.id))
    db.commit()
    return {"ok": True}


@router.get("/foods/search")
def search_foods(q: str = Query(min_length=1), limit: int = 10, user: User = Depends(get_current_user)):
    cat = _catalog()
    try:
        items = cat.search(q, limit=limit)
    finally:
        cat.close()
    return [
        {
            "external_id": i.external_id,
            "display_name": i.display_name,
            "source": i.source,
            "kcal_per_100g": i.kcal_per_100g,
            "protein_g_per_100g": i.protein_g_per_100g,
            "carbs_g_per_100g": i.carbs_g_per_100g,
            "fat_g_per_100g": i.fat_g_per_100g,
            "barcode": i.barcode,
        }
        for i in items
    ]


@router.get("/foods/barcode/{barcode}")
def food_by_barcode(barcode: str, user: User = Depends(get_current_user)):
    cat = _catalog()
    try:
        item = cat.by_barcode(barcode)
    finally:
        cat.close()
    if not item:
        raise HTTPException(status_code=404, detail="Not found")
    return {
        "external_id": item.external_id,
        "display_name": item.display_name,
        "source": item.source,
        "kcal_per_100g": item.kcal_per_100g,
        "protein_g_per_100g": item.protein_g_per_100g,
        "carbs_g_per_100g": item.carbs_g_per_100g,
        "fat_g_per_100g": item.fat_g_per_100g,
        "barcode": item.barcode,
    }


class RecipeRequest(BaseModel):
    name: str
    ingredients: list[MealItemIn]
    total_servings: float = Field(default=1, ge=0.1, le=100)


@router.post("/recipes")
def create_recipe(req: RecipeRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Create a private recipe: sum ingredient nutrients, store per serving."""
    total: dict[str, float] = {"kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0, "fiber_g": 0.0}
    for ing in req.ingredients:
        nutrients, _, _ = _lookup_nutrients(db, ing, user)
        if nutrients:
            for k in ("kcal", "protein_g", "carbs_g", "fat_g", "fiber_g"):
                total[k] += nutrients.get(k) or 0.0
    per_serving = {k: round(v / req.total_servings, 2) for k, v in total.items()}
    item = FoodCatalogItem(
        user_id=user.id,
        display_name=req.name,
        nutrients_per_100g_json={"per_serving": per_serving, "servings": req.total_servings},
    )
    db.add(item)
    db.commit()
    return {"id": str(item.id), "display_name": req.name, "per_serving": per_serving}
