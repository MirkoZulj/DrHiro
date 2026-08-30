"""Shared pytest fixtures: boot mock Telegram + mock TrueForge servers."""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

# Allow importing test helpers as top-level modules.
TESTS_DIR = Path(__file__).parent
sys.path.insert(0, str(TESTS_DIR))
# Allow importing the bridge package.
sys.path.insert(0, str(TESTS_DIR.parent / "services" / "telegram-bridge" / "src"))
# Allow importing the tools package.
sys.path.insert(0, str(TESTS_DIR.parent / "services" / "drhiro-tools" / "src"))

import mock_telegram  # noqa: E402
import mock_trueforge  # noqa: E402


def _boot(server):
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


@pytest.fixture()
def mock_tg():
    server, state = mock_telegram.make_server(port=0)
    _boot(server)
    host, port = server.server_address[0], server.server_address[1]
    yield {"base": f"http://{host}:{port}", "state": state}
    server.shutdown()


@pytest.fixture()
def mock_tf():
    server, state = mock_trueforge.make_server(port=0)
    _boot(server)
    host, port = server.server_address[0], server.server_address[1]
    yield {"base": f"http://{host}:{port}", "state": state}
    server.shutdown()


@pytest.fixture()
def bridge(mock_tg, mock_tf, monkeypatch, tmp_path):
    """A Bridge wired to the mock servers with a fixed allowed user."""
    from drhiro_bridge.config import Config
    from drhiro_bridge.main import Bridge

    cfg = Config()
    cfg.bot_token = "123456:TESTTOKEN"
    cfg.allowed_username = "alice"
    cfg.trueforge_url = mock_tf["base"]
    cfg.agent_name = "drhiro"
    cfg.poll_timeout = 2
    cfg.pairing_state_dir = str(tmp_path / "pairing")

    b = Bridge(cfg)
    # Point Telegram client at the mock API base.
    b.tg._api = f"{mock_tg['base']}/bot{cfg.bot_token}"
    return b
