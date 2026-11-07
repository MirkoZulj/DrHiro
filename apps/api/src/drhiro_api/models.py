"""Canonical drHiro data model.

Implements Section 7 of the blueprint: UUID PKs, UTC timestamps,
per-user row-level authorization via user_id on every tenant table,
and provenance on every measurement.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from drhiro_api.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    locale: Mapped[str] = mapped_column(String(16), default="en", nullable=False)
    date_of_birth: Mapped[str | None] = mapped_column(String(10), nullable=True)  # ISO date, optional
    sex_for_health_calculations: Mapped[str | None] = mapped_column(String(16), nullable=True)
    height_cm: Mapped[float | None] = mapped_column(nullable=True)
    basal_metabolism_kcal: Mapped[float | None] = mapped_column(nullable=True)  # Katch-McArdle BMR, kcal/day
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)

    identities: Mapped[list["ExternalIdentity"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    devices: Mapped[list["DeviceConnection"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class ExternalIdentity(Base, TimestampMixin):
    __tablename__ = "external_identities"
    __table_args__ = (UniqueConstraint("provider", "provider_subject", name="uq_external_identity"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # telegram, web, android_installation
    provider_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="identities")


class DeviceConnection(Base, TimestampMixin):
    __tablename__ = "device_connections"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # health_connect, omron, manual
    device_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    device_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_device_id_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    permissions_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)

    user: Mapped[User] = relationship(back_populates="devices")


class Activity(Base, TimestampMixin):
    __tablename__ = "activities"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    activity_date: Mapped[date] = mapped_column(Date, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    calories_burned: Mapped[float] = mapped_column(nullable=False)

    user: Mapped["User"] = relationship()


class Measurement(Base, TimestampMixin):
    __tablename__ = "measurements"
    __table_args__ = (
        UniqueConstraint("user_id", "source_provider", "source_record_id", name="uq_measurement_source"),
        Index("ix_measurements_user_metric_time", "user_id", "metric_type", "start_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    metric_type: Mapped[str] = mapped_column(String(32), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    value_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_device_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    recording_method: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class DailyAggregate(Base, TimestampMixin):
    __tablename__ = "daily_aggregates"
    __table_args__ = (UniqueConstraint("user_id", "local_date", "metric_type", name="uq_daily_aggregate"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    local_date: Mapped[str] = mapped_column(String(10), nullable=False)  # YYYY-MM-DD in user tz
    metric_type: Mapped[str] = mapped_column(String(32), nullable=False)
    value_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    coverage_score: Mapped[float | None] = mapped_column(nullable=True)
    source_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # Provenance (mifitness-import): makes "Health Connect leads" enforceable in
    # data rather than by convention.  NULL = legacy/computed row.  Added by the
    # guarded alembic migration f7a8b9c0d1e2; existing rows backfilled 'computed'.
    source_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_record_id: Mapped[str | None] = mapped_column(String(255), nullable=True)


class FoodCatalogItem(Base, TimestampMixin):
    """Private drHiro food catalog: user-created foods and corrected matches.

    When the item corresponds to a canonical food in the normalized
    ``foods`` table, ``food_id`` links to it.  The denormalised
    ``nutrients_per_100g_json`` is retained for user overrides and
    fast reads.
    """

    __tablename__ = "food_catalog_items"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)  # null = system
    food_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("foods.id"), nullable=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    barcode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    nutrients_per_100g_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    source: Mapped[str] = mapped_column(String(32), default="drhiro_private", nullable=False)
    source_version: Mapped[str] = mapped_column(String(32), default="1", nullable=False)

    food: Mapped["Food | None"] = relationship()


class Meal(Base, TimestampMixin):
    __tablename__ = "meals"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    eaten_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    meal_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)  # draft, needs_review, confirmed, deleted
    input_method: Mapped[str | None] = mapped_column(String(32), nullable=True)  # text, photo, barcode, copy, recipe
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    photo_asset_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    totals_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items: Mapped[list["MealItem"]] = relationship(back_populates="meal", cascade="all, delete-orphan")


class MealItem(Base, TimestampMixin):
    __tablename__ = "meal_items"

    id: Mapped[uuid.UUID] = uuid_pk()
    meal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("meals.id"), nullable=False)
    food_catalog_item_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[float] = mapped_column(default=1.0, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    grams: Mapped[float | None] = mapped_column(nullable=True)
    volume_ml: Mapped[float | None] = mapped_column(nullable=True)
    beverage_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    nutrients_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    user_corrected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    meal: Mapped[Meal] = relationship(back_populates="items")


class Reminder(Base, TimestampMixin):
    __tablename__ = "reminders"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)  # bp, weight, meal, water, sync, activity, bedtime, weekly, medication
    schedule_json: Mapped[dict] = mapped_column(JSON, nullable=False)  # {"cron": "0 8 * * *"} or {"days": [...], "time": "08:00"}
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    quiet_hours_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {"start": "22:00", "end": "07:00"}
    escalation_policy_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {"max_followups": 2}
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReminderOccurrence(Base, TimestampMixin):
    __tablename__ = "reminder_occurrences"
    __table_args__ = (Index("ix_occurrences_status_due", "status", "due_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    reminder_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reminders.id"), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_by_record_id: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Goal(Base, TimestampMixin):
    __tablename__ = "goals"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    goal_type: Mapped[str] = mapped_column(String(32), nullable=False)  # weight, steps, bp, protein, water
    target_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    start_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    end_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="user", nullable=False)  # user, clinician, system_suggestion
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)


class RuleDefinition(Base, TimestampMixin):
    __tablename__ = "rule_definitions"

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    jurisdiction: Mapped[str] = mapped_column(String(16), default="global", nullable=False)
    logic_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Alert(Base, TimestampMixin):
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    rule_code: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    trigger_record_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    severity: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False)  # open, acknowledged, resolved
    explanation_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    params_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ConsentGrant(Base, TimestampMixin):
    __tablename__ = "consent_grants"

    id: Mapped[uuid.UUID] = uuid_pk()
    grantor_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    grantee_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)  # activity, sleep, weight, bp, nutrition, summaries
    access_level: Mapped[str] = mapped_column(String(16), default="read", nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)  # user, openclaw, android, system, admin
    actor_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_id_affected: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class IngestBatch(Base, TimestampMixin):
    """Idempotency record for ingestion batches.

    Replaying the same (user_id, installation_id, batch_id) returns the
    stored result instead of creating duplicate measurements.
    """

    __tablename__ = "ingest_batches"
    __table_args__ = (UniqueConstraint("user_id", "batch_id", name="uq_ingest_batch_user_batch"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    installation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False)


# ── Normalized food-nutrient schema ──────────────────────────────────────────


class DataSource(Base, TimestampMixin):
    """Provenance for food data sources (USDA, Open Food Facts, Ciqual, user-created)."""

    __tablename__ = "data_sources"

    id: Mapped[uuid.UUID] = uuid_pk()
    source_key: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    source_label: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    license: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Nutrient(Base):
    """Master list of nutrient types (energy, protein, carbs, etc.)."""

    __tablename__ = "nutrients"

    id: Mapped[uuid.UUID] = uuid_pk()
    nutrient_code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    nutrient_label: Mapped[str] = mapped_column(String(255), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(String(16), default="other", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Food(Base, TimestampMixin):
    """Core food entity — generic/raw (USDA), branded/packaged (OFF), European (Ciqual), or user-created."""

    __tablename__ = "foods"
    __table_args__ = (
        UniqueConstraint("data_source_id", "external_id", name="uq_food_source_external"),
        Index("ix_foods_barcode", "barcode"),
        Index("ix_foods_display_name", "display_name"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    data_source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("data_sources.id"), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name_en: Mapped[str | None] = mapped_column(String(255), nullable=True)
    barcode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_generic: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_liquid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Curated "plain default" marker for a bare generic word (drhiro-api
    # 0.1.19). Ranking alone cannot know which variant of a food is plain
    # ("beer" could be light/regular/higher-alcohol), so the human choice is
    # recorded here and preferred by resolve_food(). NULL = not a curated
    # default. Set by scripts/set_food_defaults.py, never scattered SQL.
    is_default: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    serving_grams: Mapped[float | None] = mapped_column(nullable=True)
    serving_unit: Mapped[str | None] = mapped_column(String(32), nullable=True)

    data_source: Mapped[DataSource] = relationship()
    nutrients: Mapped[list["FoodNutrient"]] = relationship(back_populates="food", cascade="all, delete-orphan")
    brand: Mapped["FoodBrand | None"] = relationship(back_populates="food", cascade="all, delete-orphan", uselist=False)
    ingredients: Mapped[list["FoodIngredient"]] = relationship(back_populates="food", cascade="all, delete-orphan")


class FoodNutrient(Base):
    """Per-food nutrient amounts — one row per (food, nutrient) pair."""

    __tablename__ = "food_nutrients"
    __table_args__ = (
        UniqueConstraint("food_id", "nutrient_id", name="uq_food_nutrient"),
        Index("ix_food_nutrients_nutrient", "nutrient_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    food_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("foods.id"), nullable=False)
    nutrient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("nutrients.id"), nullable=False)
    amount_per_100g: Mapped[float | None] = mapped_column(nullable=True)
    amount_per_serving: Mapped[float | None] = mapped_column(nullable=True)

    food: Mapped[Food] = relationship(back_populates="nutrients")
    nutrient: Mapped[Nutrient] = relationship()


class FoodResolutionRule(Base, TimestampMixin):
    """Reusable food-matching rule learned from a user's correction.

    When a user corrects a meal item's display_name, an LLM distils the
    correction into a general pattern ("wine means beverage, not vinegar")
    stored here and consulted FIRST by resolve_food().
    """

    __tablename__ = "food_resolution_rules"
    __table_args__ = (
        Index("ix_food_rules_user", "user_id", "active"),
        CheckConstraint("scope IN ('user', 'global')", name="ck_food_rules_scope"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    original_pattern: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_food_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("foods.id"), nullable=True
    )
    rule_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope: Mapped[str] = mapped_column(String(16), default="user", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class FoodBrand(Base, TimestampMixin):
    """Branded/packaged food info (brand name, manufacturer, packaging)."""

    __tablename__ = "food_brands"

    id: Mapped[uuid.UUID] = uuid_pk()
    food_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("foods.id"), nullable=False)
    brand_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    brand_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    packaging_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin_country: Mapped[str | None] = mapped_column(String(64), nullable=True)

    food: Mapped[Food] = relationship(back_populates="brand")


class FoodIngredient(Base):
    """Ingredient lists for packaged foods."""

    __tablename__ = "food_ingredients"

    id: Mapped[uuid.UUID] = uuid_pk()
    food_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("foods.id"), nullable=False)
    ingredient_text: Mapped[str] = mapped_column(Text, nullable=False)
    ingredient_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_allergen: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    food: Mapped[Food] = relationship(back_populates="ingredients")


class ConsumptionOperation(Base, TimestampMixin):
    """Dedup tracking for consumption operations (meals, measurements).

    Prevents duplicate writes when the same Telegram message is processed
    multiple times (retries, webhook redeliveries).
    """

    __tablename__ = "consumption_operations"
    __table_args__ = (
        Index("ix_consumption_op_user", "user_id", "source"),
        Index(
            "uq_consumption_op_telegram",
            "user_id",
            "source",
            "source_chat_id",
            "source_message_id",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_chat_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    source_message_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    source_bot_id: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    result_json: Mapped[dict] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="applied")
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=True)


class MedicalRecord(Base, TimestampMixin):
    """A transcribed medical document (lab report, prescription, letter).

    The content is the agent's FAITHFUL transcription of the document the user
    photographed (original language, exact numbers/units/dates) — never a
    summary or interpretation.
    """

    __tablename__ = "medical_records"
    __table_args__ = (Index("ix_medical_records_user_recorded", "user_id", "recorded_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), default="telegram_photo", nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # Optional link to the clinical encounter this document belongs to.
    encounter_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medical_encounters.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Optional relative path (within the medical-scans data volume) of the
    # scanned document image this record was transcribed from. Served only to
    # the owning user via /api/v1/medical/scan/{record_id}.
    source_image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Additional scan paths for a multi-photo batch logged as ONE record
    # (source_image_path holds the first scan; this JSON list holds ALL scans
    # including the first, in upload order). NULL for legacy/single records.
    source_image_paths_json: Mapped[list | None] = mapped_column(JSON, nullable=True)

    user: Mapped["User"] = relationship()
    encounter: Mapped["MedicalEncounter | None"] = relationship(back_populates="records")


class MedicalEncounter(Base, TimestampMixin):
    """A clinical encounter (visit, lab draw, prescription pickup) that groups
    one or more transcribed medical records and their source scans.

    Records point at it via ``medical_records.encounter_id``; deleting an
    encounter deletes the records inside it (the agent confirms first).
    """

    __tablename__ = "medical_encounters"
    __table_args__ = (Index("ix_medical_encounters_user_date", "user_id", "encounter_date"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    encounter_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    facility: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped["User"] = relationship()
    records: Mapped[list["MedicalRecord"]] = relationship(
        back_populates="encounter", cascade="all, delete-orphan", passive_deletes=True
    )


class LogWriteFingerprint(Base, TimestampMixin):
    """Server-side content fingerprint for a free-text logging write.

    The MCP bridge forwards NO per-message Telegram identity, so every
    model-initiated ``create_meal_from_text`` call writes an ``anon-<uuid>``
    order and nothing can recognise a repeat: a looping agent wrote the same
    26-character water line four times and produced 3000 ml where the truth was
    750 ml. This table is the dedupe memory for that path.

    Key = (user_id, source, sha256(normalised text + meal_type + local date)).
    A row is written after a successful ledger write; a later identical call
    inside ``WINDOW_SECONDS`` finds it and is suppressed instead of writing.
    Unlike ``consumption_operations`` (which needs a real per-message id) this
    key is derived ONLY from content the server itself parsed, so it trusts
    nothing from the model.
    """

    __tablename__ = "log_write_fingerprints"
    __table_args__ = (
        Index("ix_log_fingerprints_user_source_fp", "user_id", "source", "fingerprint"),
        Index("ix_log_fingerprints_user_seen", "user_id", "seen_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # The resolved write instant the fingerprint was formed against. Kept
    # explicit so the window comparison uses the same clock the write used and
    # is testable without freezing wall time.
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


# ── Supplements & medications (0.9.0, spec §5) ──────────────────────────────


class MedicationItem(Base, TimestampMixin):
    """A supplement or medication the user says they take.

    Records ONLY what the user stated — the agent never suggests starting,
    stopping or changing a dose. An expected dose with no log is "not logged",
    never "missed" and never 0% adherence. Interaction checking is out of
    scope; the only deterministic check is duplicate ingredients (rule engine,
    MED_DUPLICATE_INGREDIENT).
    """

    __tablename__ = "medication_items"
    __table_args__ = (
        Index("ix_med_items_user_status", "user_id", "status"),
        CheckConstraint("kind IN ('supplement','medication')", name="ck_med_kind"),
        CheckConstraint("status IN ('draft','active','paused','stopped')", name="ck_med_status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    brand: Mapped[str | None] = mapped_column(String(255))
    form: Mapped[str | None] = mapped_column(String(24))
    # [{"name":"cholecalciferol","amount":50,"unit":"µg","per":"unit"}]  per = unit|serving|ml
    ingredients_json: Mapped[list | None] = mapped_column(JSON)
    dose_amount: Mapped[float | None] = mapped_column()
    dose_unit: Mapped[str | None] = mapped_column(String(16))
    # 0.11.0-a: per-unit description (capsule|tablet|softgel|ml|g|scoop|drop|sachet|other)
    unit_label: Mapped[str | None] = mapped_column(String(24))
    # Owner-chosen abbreviation shown on weekly chips (e.g. Mg, MultiV).
    # NULL = fall back to the computed 12-char short_name.
    short_name: Mapped[str | None] = mapped_column(String(32))
    # {"type":"daily","times":["08:00"]} | {"type":"weekly","days":["mon","thu"],"times":["08:00"]}
    # | {"type":"interval","every_days":3,"times":["08:00"],"anchor":"2026-11-03"} | {"type":"prn"}
    # optional: "with_food": "with|before|after|any"
    schedule_json: Mapped[dict | None] = mapped_column(JSON)
    timezone: Mapped[str | None] = mapped_column(String(64))  # None = follow user.timezone
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)  # planned end (course), may be None
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    prescribed_by: Mapped[str | None] = mapped_column(String(255))
    medical_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("medical_records.id", ondelete="SET NULL")
    )
    source: Mapped[str] = mapped_column(String(24), default="manual", nullable=False)
    label_image_path: Mapped[str | None] = mapped_column(String(512))
    # D1 reminder design (owner decision 2026-11-03): ONE Reminder per time
    # slot, built with the existing `_compute_next_due` semantics (which
    # handles exactly one `{"days":[...],"time":"HH:MM"}` or one cron slot).
    # A schedule with multiple times therefore needs multiple Reminder rows,
    # so the slot→reminder mapping is stored as a JSON list of reminder ids
    # (spec §5.5 `reminder_ids_json`). Single-slot schedules carry exactly one.
    reminder_ids_json: Mapped[list | None] = mapped_column(JSON)
    stock_quantity: Mapped[float | None] = mapped_column()
    stock_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)


class MedicationItemEvent(Base):
    """Append-only history: start/stop/pause/resume/dose change. Feeds analytics timeline."""

    __tablename__ = "medication_item_events"
    __table_args__ = (Index("ix_med_item_events_user_day", "user_id", "effective_date"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("medication_items.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # started|stopped|paused|resumed|dose_changed|schedule_changed|edited
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    before_json: Mapped[dict | None] = mapped_column(JSON)
    after_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class DoseEvent(Base, TimestampMixin):
    """One logged dose. An expected dose with no event stays "not logged" —
    never "missed", never 0% adherence ("missing stays missing")."""

    __tablename__ = "dose_events"
    __table_args__ = (
        Index("ix_dose_events_user_time", "user_id", "taken_at"),
        # one record per scheduled slot; PRN/ad-hoc doses have scheduled_for NULL (not constrained)
        Index(
            "uq_dose_slot",
            "item_id",
            "scheduled_for",
            unique=True,
            postgresql_where=text("scheduled_for IS NOT NULL"),
        ),
        CheckConstraint("status IN ('taken','skipped')", name="ck_dose_status"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("medication_items.id", ondelete="CASCADE"), nullable=False)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # slot this answers
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="taken", nullable=False)
    amount: Mapped[float | None] = mapped_column()  # defaults to item.dose_amount
    unit: Mapped[str | None] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(24), default="telegram", nullable=False)
    note: Mapped[str | None] = mapped_column(Text)


# ── Health card (0.10.0 wave 3, spec §6) ────────────────────────────────────


class HealthProfile(Base, TimestampMixin):
    """One row per user: the self-maintained health card.

    JSON sections keep the schema flexible; every list entry carries its own
    `id` (uuid4 string) so the agent and UI can edit a single entry. Derived
    fields (display name, DOB, age, sex, height, latest weight, current
    medications) are computed at read time from `users`, the weight series and
    `medication_items` — never stored here (spec §6.1).
    """

    __tablename__ = "health_profiles"
    __table_args__ = (UniqueConstraint("user_id", name="uq_health_profile_user"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # A+, A-, B+, B-, AB+, AB-, 0+, 0-, unknown
    blood_type: Mapped[str | None] = mapped_column(String(8))
    organ_donor: Mapped[str | None] = mapped_column(String(8))  # yes|no|unknown
    conditions_json: Mapped[list] = mapped_column(JSON, default=list)
    allergies_json: Mapped[list] = mapped_column(JSON, default=list)
    intolerances_json: Mapped[list] = mapped_column(JSON, default=list)
    implants_devices_json: Mapped[list] = mapped_column(JSON, default=list)
    vaccinations_json: Mapped[list] = mapped_column(JSON, default=list)
    surgeries_json: Mapped[list] = mapped_column(JSON, default=list)
    family_history_json: Mapped[list] = mapped_column(JSON, default=list)
    emergency_contacts_json: Mapped[list] = mapped_column(JSON, default=list)
    care_team_json: Mapped[list] = mapped_column(JSON, default=list)
    insurance_json: Mapped[dict | None] = mapped_column(JSON)  # {provider, number}
    notes: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # "card is up to date"


# ── Analytics runs (0.9.0 wave 2, spec §3.4) ────────────────────────────────


class AnalysisRun(Base, TimestampMixin):
    """One deterministic analytics run. Findings stay in result_json —
    immutable per run, stable ids comparable across runs."""

    __tablename__ = "analysis_runs"
    __table_args__ = (
        Index("ix_analysis_runs_user_created", "user_id", "created_at"),
        UniqueConstraint("user_id", "idempotency_key", name="uq_analysis_run_idem"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # general | visit
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)  # queued|running|succeeded|failed
    requested_via: Mapped[str] = mapped_column(String(16), default="web", nullable=False)  # web | telegram
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    params_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    data_fingerprint: Mapped[str | None] = mapped_column(String(64))
    engine_version: Mapped[str | None] = mapped_column(String(16))
    thresholds_version: Mapped[str | None] = mapped_column(String(32))
    result_json: Mapped[dict | None] = mapped_column(JSON)
    narrative_text: Mapped[str | None] = mapped_column(Text)
    narrative_status: Mapped[str] = mapped_column(String(16), default="none", nullable=False)  # none|ok|rejected|unavailable
    job_id: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    files: Mapped[list["GeneratedFile"]] = relationship(
        primaryjoin="AnalysisRun.id == foreign(GeneratedFile.analysis_run_id)",
        viewonly=True,
    )


# ── Generated artefacts (0.10.0 wave 3, spec §2.2) ──────────────────────────


class GeneratedFile(Base, TimestampMixin):
    """One downloadable artefact: analysis/visit/health-card PDFs, data-export
    bundles, system backups, restore tests. Files live on the filesystem under
    generated_files_dir (layout: generated/<user_id>/<uuid>.<ext>); this row is
    the metadata + status record."""

    __tablename__ = "generated_files"
    __table_args__ = (Index("ix_generated_files_user_kind", "user_id", "kind", "created_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # analysis_report | visit_report | health_card | data_export | system_backup | restore_test
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)  # queued|running|ready|failed|expired
    rel_path: Mapped[str | None] = mapped_column(String(512))  # relative to generated_files_dir (or backups_dir for system_backup)
    filename: Mapped[str | None] = mapped_column(String(255))  # download name
    mime: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    params_json: Mapped[dict | None] = mapped_column(JSON)
    analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("analysis_runs.id", ondelete="SET NULL")
    )
    error: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    job_id: Mapped[str | None] = mapped_column(String(64))

# ── Runtime settings singleton (restores the AppSetting model that
#    services/settings_store.py always imported but which was missing from
#    models.py — a latent import-time break exposed when routers/backups.py
#    started importing the settings gate). Table matches the guarded SQL in
#    migration b8c9d0e1f2a3.


class AppSetting(Base):
    __tablename__ = "app_settings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # "singleton"
    ai_backend_url: Mapped[str | None] = mapped_column(String(512))
    model_name: Mapped[str | None] = mapped_column(String(255))
    ai_api_key: Mapped[str | None] = mapped_column(Text)
    telegram_bot_token: Mapped[str | None] = mapped_column(Text)
    telegram_allowed_username: Mapped[str | None] = mapped_column(String(255))
    telegram_allowed_user_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

class FoodMicroBackfill(Base, TimestampMixin):
    __tablename__ = "food_micro_backfill"
    __table_args__ = (UniqueConstraint("food_id", name="uq_food_micro_backfill_food"),)
    id: Mapped[uuid.UUID] = uuid_pk()
    food_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("foods.id", ondelete="CASCADE"), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    nutrients_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
