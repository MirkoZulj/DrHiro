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

# The Python snippet used by install.sh to validate AI_MODEL against /models.
# Qodo #10: it must PARSE the JSON and compare IDs — never execute it as code.
_MODEL_CHECK = '''import sys,json,os
model=os.environ["AI_MODEL_SAFE"]
try:
    d=json.load(sys.stdin)
    ids=[m.get("id","") for m in d.get("data",[])]
    sys.exit(0 if any(model==i or model in i for i in ids) else 1)
except Exception:
    sys.exit(2)'''


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


def test_health_check_passes_when_all_ok(stub_bin, tmp_path):
    """All checks passing should yield exit 0 and ALL CHECKS PASSED."""
    _write_stub(stub_bin, "docker", '''#!/usr/bin/env bash
if [[ "$1" == "compose" && "$2" == "ps" ]]; then
  echo "trueforge Up 2 minutes"
fi
''')
    _write_stub(stub_bin, "curl", '''#!/usr/bin/env bash
exit 0
''')
    # Settings watcher is installed (crontab lists the marker) and there is no
    # failed-apply marker in the flag dir (pointed at a temp dir, not /var/lib).
    _write_stub(stub_bin, "crontab", '''#!/usr/bin/env bash
echo "* * * * * root flock drhiro-settings-watcher.sh"
exit 0
''')
    code, out = _run_health_check(
        stub_bin, env_extra={"RESTART_FLAGS_DIR": str(tmp_path / "flags")}
    )
    assert code == 0
    assert "ALL CHECKS PASSED." in out


# ---------------------------------------------------------------------- #
# Qodo #10 — installer model validation must parse JSON, not execute it
# ---------------------------------------------------------------------- #
def _run_model_check(models_json, model):
    env = dict(os.environ)
    env["AI_MODEL_SAFE"] = model
    r = subprocess.run(
        ["python3", "-c", _MODEL_CHECK],
        input=models_json, capture_output=True, text=True, env=env,
    )
    return r.returncode


def test_model_validation_matches_when_present():
    """Qodo #10: a listed model must be accepted (exit 0)."""
    models = '{"data":[{"id":"gpt-4o-mini"},{"id":"gpt-4o"}]}'
    assert _run_model_check(models, "gpt-4o-mini") == 0


def test_model_validation_rejects_missing_model():
    """Qodo #10: an unlisted model must be rejected (exit 1) — previously the
    installer could "succeed" by executing the JSON as a Python program (a JSON
    object IS valid Python source and runs successfully regardless of the model)."""
    models = '{"data":[{"id":"gpt-4o-mini"},{"id":"gpt-4o"}]}'
    # Confirms the dangerous premise: JSON-as-Python runs and exits 0.
    assert subprocess.run(["python3"], input=models, capture_output=True,
                          text=True).returncode == 0
    # The correct validator rejects an unlisted model.
    assert _run_model_check(models, "does-not-exist") == 1


# ---------------------------------------------------------------------- #
# Qodo #11 — configure.sh must define `warn` before use
# ---------------------------------------------------------------------- #
def test_configure_sh_defines_warn():
    """Qodo #11: configure.sh calls `warn` on recoverable API failures but must
    define it; under `set -e` an undefined function would abort the script."""
    script = (os.path.join(SCRIPTS_DIR, "configure.sh"))
    text = open(script).read()
    # A `warn() {` definition must exist before any `|| warn "..."` use.
    assert "warn() {" in text
    first_def = text.index("warn() {")
    # Find the first `|| warn` usage; it must appear AFTER the definition.
    import re
    uses = [m.start() for m in re.finditer(r"\|\|\s*warn\s+", text)]
    assert uses, "configure.sh must use warn on recoverable failures"
    assert all(u > first_def for u in uses), \
        "warn() must be defined before its first use"


def test_configure_sh_syntax():
    """configure.sh must pass bash -n syntax check (defining warn)."""
    r = subprocess.run(["bash", "-n", os.path.join(SCRIPTS_DIR, "configure.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------------- #
# Qodo #12 — installer must not silently drop the TrueForge source pin
# ---------------------------------------------------------------------- #
def test_installer_has_no_unpinned_trueforge_fallback():
    """Qodo #12: install.sh must not silently clone the unpinned default branch
    when the pinned tag clone fails. Every TrueForge clone must carry --branch
    (or a commit pin)."""
    install = os.path.join(os.path.dirname(SCRIPTS_DIR), "install.sh")
    text = open(install).read()
    # The pinned clone spans a line continuation, so scan the whole text (minus
    # whitespace/newlines) for the clone command fragments.
    import re
    # Find every git-clone invocation of trueforge.git with its flags.
    # Join continuation lines, then check each clone block carries --branch.
    joined = re.sub(r"\\\n", " ", text)
    # Every `git clone ... trueforge.git` must include an explicit --branch.
    for m in re.finditer(r"git clone ([^\n;]*trueforge\.git)", joined):
        block = m.group(1)
        assert "--branch" in block or "--commit" in block or "--depth 1 --branch" in block, \
            f"unpinned TrueForge clone: {block.strip()}"
    # No fallback clone without a pin: each clone fragment must mention a pin.
    unpinned = [m.group(1).strip() for m in re.finditer(r"git clone ([^\n;]*trueforge\.git)", joined)
                if "--branch" not in m.group(1) and "--commit" not in m.group(1)]
    assert unpinned == [], "installer must not fall back to an unpinned TrueForge clone"


def test_installer_syntax():
    """install.sh must pass bash -n."""
    install = os.path.join(os.path.dirname(SCRIPTS_DIR), "install.sh")
    r = subprocess.run(["bash", "-n", install], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
