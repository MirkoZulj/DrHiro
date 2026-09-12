"""Qodo #2 — account deletion must purge the user's Activity rows (and all other user-owned data).

Extends the existing privacy/deletion test pattern in apps/api/tests/.
Verifies that after confirming account deletion, every per-user model
(including Activity, which was OMITTED before the fix) is purged from the database.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from drhiro_api.models import (
    Activity,
    Alert,
    BeverageMeasurement,
    ConsentGrant,
    ConsumptionItem,
    ConsumptionOperation,
    DailyAggregate,
    DeviceConnection,
    ExternalIdentity,
    FoodCatalogItem,
    FoodResolutionRule,
    Goal,
    IngestBatch,
    Meal,
    MealItem,
    Measurement,
    Reminder,
    User,
)
from drhiro_api.security import create_access_token


def _seed_activity_data(db, user: User):
    """Create activity, measurement, meal, reminder, and goal rows for a user."""
    db.add(
        Activity(
            user_id=user.id,
            activity_date=date(2026, 1, 1),
            title="Morning run",
            description="5km jog",
            calories_burned=350.0,
        )
    )
    db.add(
        Activity(
            user_id=user.id,
            activity_date=date(2026, 1, 2),
            title="Swimming",
            description="1km laps",
            calories_burned=420.0,
        )
    )
    db.add(
        Measurement(
            user_id=user.id,
            metric_type="weight",
            start_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            value_json={"weight_kg": 80.0},
            source_provider="manual",
            source_record_id=f"meas-{user.id}-1",
        )
    )
    meal = Meal(
        user_id=user.id,
        eaten_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        meal_type="lunch",
        status="confirmed",
        totals_json={"kcal": 500.0},
    )
    db.add(meal)
    db.flush()
    db.add(MealItem(meal_id=meal.id, display_name="Chicken", grams=200))
    db.add(
        Reminder(
            user_id=user.id,
            type="water",
            schedule_json={"cron": "0 8 * * *"},
        )
    )
    db.add(
        Goal(
            user_id=user.id,
            goal_type="steps",
            target_json={"steps": 10000},
        )
    )
    db.commit()


def _seed_consent_grants(db, user_a: User, user_b: User):
    """Create a consent grant: user_a grants user_b access."""
    db.add(
        ConsentGrant(
            grantor_user_id=user_a.id,
            grantee_user_id=user_b.id,
            scope="activity",
            access_level="read",
        )
    )
    db.commit()


def _user_activities_count(db, user_id) -> int:
    return db.query(Activity).filter(Activity.user_id == user_id).count()


def _user_rows_count(db, model, user_id) -> int:
    return db.query(model).filter(model.user_id == user_id).count()


class TestAccountDeletionPurgesActivity:
    def test_activity_rows_purged_on_deletion(self, client, db):
        from apps.api.tests.conftest import make_user

        user = make_user(db, "TestUser", telegram_id="99001")
        _seed_activity_data(db, user)

        # Confirm activities exist before deletion
        assert _user_activities_count(db, user.id) == 2

        token = create_access_token(user.id)
        resp = client.post(
            "/api/v1/account/deletion-request",
            json={"confirm": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True

        # After deletion: activities must be gone
        assert _user_activities_count(db, user.id) == 0

    def test_all_user_owned_models_purged(self, client, db):
        from apps.api.tests.conftest import make_user

        user = make_user(db, "PurgeAllUser", telegram_id="99002")
        _seed_activity_data(db, user)

        # Seed additional user-owned rows
        db.add(Alert(user_id=user.id, rule_code="test", severity="info"))
        db.add(
            BeverageMeasurement(
                user_id=user.id,
                meal_item_id=uuid.uuid4(),
                measurement_id=uuid.uuid4(),
            )
        )
        db.add(
            ConsumptionOperation(
                user_id=user.id,
                source="telegram",
                result_json={},
            )
        )
        db.flush()  # Get the operation ID
        operation_id = db.query(ConsumptionOperation).filter(ConsumptionOperation.user_id == user.id).first().id
        db.add(
            ConsumptionItem(
                user_id=user.id,
                operation_id=operation_id,
                item_key="food",
                display_name="test",
                nutrients_per_100={},
                nutrients_scaled={},
            )
        )
        db.add(
            DailyAggregate(
                user_id=user.id,
                local_date="2026-01-01",
                metric_type="steps",
                value_json={"steps": 1000},
            )
        )
        db.add(
            DeviceConnection(
                user_id=user.id,
                provider="health_connect",
                device_name="TestPhone",
                external_device_id_hash="abc123",
            )
        )
        db.add(
            FoodCatalogItem(
                user_id=user.id,
                display_name="TestFood",
                nutrients_per_100g_json={},
            )
        )
        db.add(
            FoodResolutionRule(
                user_id=user.id,
                original_pattern="test",
                rule_text="test",
            )
        )
        db.add(
            IngestBatch(
                user_id=user.id,
                installation_id="inst-1",
                batch_id="batch-1",
                result_json={},
            )
        )
        db.commit()

        # Verify seeded
        assert _user_activities_count(db, user.id) == 2
        assert _user_rows_count(db, Measurement, user.id) == 1
        assert _user_rows_count(db, Meal, user.id) == 1
        assert _user_rows_count(db, Reminder, user.id) == 1
        assert _user_rows_count(db, Goal, user.id) == 1

        token = create_access_token(user.id)
        resp = client.post(
            "/api/v1/account/deletion-request",
            json={"confirm": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

        # All user-owned data must be purged
        assert _user_activities_count(db, user.id) == 0
        assert _user_rows_count(db, Measurement, user.id) == 0
        assert _user_rows_count(db, Meal, user.id) == 0
        assert _user_rows_count(db, Reminder, user.id) == 0
        assert _user_rows_count(db, Goal, user.id) == 0
        assert _user_rows_count(db, Alert, user.id) == 0
        assert _user_rows_count(db, BeverageMeasurement, user.id) == 0
        assert _user_rows_count(db, ConsumptionOperation, user.id) == 0
        assert _user_rows_count(db, ConsumptionItem, user.id) == 0
        assert _user_rows_count(db, DailyAggregate, user.id) == 0
        assert _user_rows_count(db, DeviceConnection, user.id) == 0
        assert _user_rows_count(db, FoodCatalogItem, user.id) == 0
        assert _user_rows_count(db, FoodResolutionRule, user.id) == 0
        assert _user_rows_count(db, IngestBatch, user.id) == 0
        # ExternalIdentity (telegram link) must also be purged
        assert _user_rows_count(db, ExternalIdentity, user.id) == 0

    def test_consent_grants_purged_both_directions(self, client, db):
        from apps.api.tests.conftest import make_user

        user_a = make_user(db, "Granter", telegram_id="99003")
        user_b = make_user(db, "Grantee", telegram_id="99004")
        _seed_consent_grants(db, user_a, user_b)

        # Confirm grant exists
        assert (
            db.query(ConsentGrant)
            .filter(
                ConsentGrant.grantor_user_id == user_a.id,
                ConsentGrant.grantee_user_id == user_b.id,
            )
            .count()
            == 1
        )

        # Delete user_a — grant must be purged
        token_a = create_access_token(user_a.id)
        resp = client.post(
            "/api/v1/account/deletion-request",
            json={"confirm": True},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert resp.status_code == 200, resp.text

        assert (
            db.query(ConsentGrant)
            .filter(ConsentGrant.grantor_user_id == user_a.id)
            .count()
            == 0
        )
        # Grantee's copy (grantee_user_id) must also be purged
        assert (
            db.query(ConsentGrant)
            .filter(ConsentGrant.grantee_user_id == user_b.id)
            .count()
            == 0
        )

    def test_deletion_without_confirm_is_noop(self, client, db):
        from apps.api.tests.conftest import make_user

        user = make_user(db, "NoopUser", telegram_id="99005")
        _seed_activity_data(db, user)

        token = create_access_token(user.id)
        resp = client.post(
            "/api/v1/account/deletion-request",
            json={"confirm": False},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert "Confirm" in data["message"]

        # Data must still be present
        assert _user_activities_count(db, user.id) == 2

    def test_meal_items_purged_via_meal_join(self, client, db):
        """MealItem has no user_id — verify it is purged via the Meal join."""
        from apps.api.tests.conftest import make_user

        user = make_user(db, "MealItemUser", telegram_id="99006")
        meal = Meal(
            user_id=user.id,
            eaten_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
            meal_type="lunch",
            status="confirmed",
        )
        db.add(meal)
        db.flush()
        db.add(MealItem(meal_id=meal.id, display_name="Steak", grams=250))
        db.add(MealItem(meal_id=meal.id, display_name="Rice", grams=150))
        db.commit()

        assert db.query(MealItem).filter(MealItem.meal_id == meal.id).count() == 2

        token = create_access_token(user.id)
        resp = client.post(
            "/api/v1/account/deletion-request",
            json={"confirm": True},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

        # MealItems should be gone (purged via meal_id join)
        assert db.query(MealItem).filter(MealItem.meal_id == meal.id).count() == 0
        # And the meal itself
        assert _user_rows_count(db, Meal, user.id) == 0
