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
    nutrients_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    user_corrected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    meal: Mapped[Meal] = relationship(back_populates="items")


class Reminder(Base, TimestampMixin):
    __tablename__ = "reminders"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)  # bp, weight, meal, water, sync, activity, bedtime, weekly
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


class AppSetting(Base):
    """Singleton row holding instance-global runtime settings.

    This is the SOURCE OF TRUTH for settings editable from the web Settings
    screen. `.env` is bootstrap-only: install.sh writes it, first boot seeds
    this table from it, and thereafter this row is authoritative.

    Only one row exists (id = 'singleton'). Columns are the editable fields:
      - ai_backend_url, model_name       -> the AI model drHiro/TrueForge uses
      - telegram_bot_token, telegram_allowed_username -> Telegram comms
      - ai_api_key                       -> secret (never returned in full)

    Secret values are write-only via the API: reads return a masked set/not-set
    indicator, never the stored secret.
    """

    __tablename__ = "app_settings"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default="singleton")
    ai_backend_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ai_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)          # secret
    telegram_bot_token: Mapped[str | None] = mapped_column(Text, nullable=True)  # secret
    telegram_allowed_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


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
