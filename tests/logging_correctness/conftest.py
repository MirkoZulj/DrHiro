"""Keep this suite isolated from the repo-wide conftest.

Loads BEFORE the test module, so DRHIRO_DATABASE_URL is set and apps/api/src is on
sys.path before drhiro_api is imported anywhere — the engine must bind to the
disposable logging-correctness database, never to production or to drhiro_test.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "apps/api/src"))

DB_URL = "postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_logtest"
os.environ["DRHIRO_DATABASE_URL"] = DB_URL
os.environ.setdefault("DRHIRO_REDIS_URL", "redis://localhost:6379/0")
# Inert-by-default gates, matching production.
os.environ.pop("DRHIRO_TELEGRAM_INGRESS_SECRET", None)
os.environ.pop("DRHIRO_TELEGRAM_BOT_ID", None)
os.environ.pop("DRHIRO_TELEGRAM_INGRESS_ENABLED", None)
