"""Test fixtures: real Postgres (DRHIRO_TEST_DATABASE_URL or dev default),
ephemeral schema per run, TestClient with auth helpers."""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

os.environ.setdefault(
    "DRHIRO_DATABASE_URL",
    "postgresql+psycopg://drhiro:drhiro@localhost:5435/drhiro",
)
os.environ.setdefault("DRHIRO_REDIS_URL", "redis://localhost:6382/0")
os.environ.setdefault("DRHIRO_JWT_SECRET", "test-secret-key-0123456789abcdefghijklmnopqrstuv")
os.environ.setdefault("DRHIRO_TELEGRAM_BOT_TOKEN", "123456:test-bot-token-for-initdata")

from drhiro_api.db import Base, engine, SessionLocal  # noqa: E402
from drhiro_api.main import app  # noqa: E402
from drhiro_api.models import DeviceConnection, ExternalIdentity, User  # noqa: E402
from drhiro_api.security import create_access_token, validate_telegram_init_data  # noqa: E402


@pytest.fixture(scope="session")
def db_engine():
    # Drop + recreate all tables for a clean run against the real DB.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    # Leave the schema in place for inspection; tests use unique users.


@pytest.fixture(autouse=True)
def _clean_tables(db_engine):
    """Truncate all tables before each test for deterministic isolation."""
    from sqlalchemy import text
    with db_engine.begin() as conn:
        conn.execute(text(
            "DO $$ DECLARE r RECORD; BEGIN "
            "FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename != 'alembic_version') LOOP "
            "EXECUTE 'TRUNCATE TABLE ' || quote_ident(r.tablename) || ' CASCADE'; "
            "END LOOP; END $$;"
        ))
    yield


@pytest.fixture()
def db(db_engine):
    TestingSession = sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)
    session = TestingSession()
    yield session
    session.close()


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def make_user(db, display_name: str, telegram_id: str | None = None) -> User:
    user = User(display_name=display_name, timezone="Europe/Zagreb")
    db.add(user)
    db.flush()
    if telegram_id:
        db.add(
            ExternalIdentity(
                provider="telegram",
                provider_subject=telegram_id,
                user_id=user.id,
                verified_at=None,
            )
        )
    db.commit()
    db.refresh(user)
    return user


def auth_headers(user: User) -> dict:
    token = create_access_token(user.id)
    return {"Authorization": f"Bearer {token}"}


def link_device(db, user: User, installation_id: str | None = None) -> str:
    if installation_id is None:
        installation_id = str(uuid.uuid4())
    db.add(
        DeviceConnection(
            user_id=user.id,
            provider="health_connect",
            device_name="TestPhone",
            external_device_id_hash=installation_id,
            status="active",
        )
    )
    db.commit()
    return installation_id


@pytest.fixture()
def user_a(db):
    return make_user(db, "Alice", telegram_id="1001")


@pytest.fixture()
def user_b(db):
    return make_user(db, "Bob", telegram_id="1002")
