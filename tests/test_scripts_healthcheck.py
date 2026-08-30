"""Regression tests for the operational shell scripts (Qodo findings).

These invoke the real scripts via subprocess with stubbed external commands so
failures are exercised deterministically without requiring Docker/Telegram/AI.
"""
from __future__ import annotations

import os
import subprocess
import textwrap

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")


@pytest.fixture()
def stub_bin(tmp_path):
    """A temp bin dir with stubbed docker/curl that we can tune per test."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    return bindir


def _write_stub(bindir, name, content):
    p = bindir / name
    p.write_text(textwrap.dedent(content))
    p.chmod(0o755)
    return p


def _run_health_check(bindir, env_extra=None):
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env.setdefault("TELEGRAM_BOT_TOKEN", "tok")
    env.setdefault("AI_BACKEND_BASE_URL", "http://ai.example")
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(
        ["bash", os.path.join(SCRIPTS_DIR, "health-check.sh")],
        capture_output=True, text=True, env=env,
    )
    return r.returncode, r.stdout + r.stderr


def test_health_check_fails_when_container_down(stub_bin):
    """Qodo #4/#5: a stopped container must produce a FAIL and a nonzero exit."""
    _write_stub(stub_bin, "docker", '''#!/usr/bin/env bash
if [[ "$1" == "compose" && "$2" == "ps" ]]; then
  echo "drhiro-tools Down"
  echo "trueforge Up 2 minutes"
fi
''')
    _write_stub(stub_bin, "curl", '''#!/usr/bin/env bash
exit 0
''')
    code, out = _run_health_check(stub_bin)
    assert code != 0
    assert "FAIL container drhiro-tools: Down" in out


def test_health_check_fails_when_sse_probe_fails(stub_bin):
    """Qodo #4: the tools SSE probe must NOT always succeed; a curl failure must
    produce a FAIL and a nonzero exit (previously `|| true` hid it)."""
    _write_stub(stub_bin, "docker", '''#!/usr/bin/env bash
if [[ "$1" == "compose" && "$2" == "ps" ]]; then
  echo "trueforge Up 2 minutes"
fi
''')
    # Fail any request touching the SSE probe (localhost:3100).
    _write_stub(stub_bin, "curl", '''#!/usr/bin/env bash
case "$*" in
  *3100/sse*) exit 1 ;;
esac
exit 0
''')
    code, out = _run_health_check(stub_bin)
    assert code != 0
    assert "FAIL drhiro-tools not reachable on :3100" in out


def test_health_check_passes_when_all_ok(stub_bin):
    """All checks passing should yield exit 0 and ALL CHECKS PASSED."""
    _write_stub(stub_bin, "docker", '''#!/usr/bin/env bash
if [[ "$1" == "compose" && "$2" == "ps" ]]; then
  echo "trueforge Up 2 minutes"
fi
''')
    _write_stub(stub_bin, "curl", '''#!/usr/bin/env bash
exit 0
''')
    code, out = _run_health_check(stub_bin)
    assert code == 0
    assert "ALL CHECKS PASSED." in out
