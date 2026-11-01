"""Contract tests for the camoufox SSH helper.

The helper must never build a password-based command and must degrade to an
empty command when no host is configured, so the caller skips instead of
running ssh with an empty target.
"""

from drhiro_api.services.intelligent_food_search import _camoufox_ssh_command


def test_builds_key_only_command(monkeypatch):
    monkeypatch.setenv("DRHIRO_CAMOUFOX_SSH_HOST", "100.64.0.9")
    monkeypatch.setenv("DRHIRO_CAMOUFOX_SSH_USER", "mirko")
    monkeypatch.setenv("DRHIRO_CAMOUFOX_SSH_KEY", "/run/secrets/camoufox_key")

    cmd = _camoufox_ssh_command("echo hi")

    assert cmd[0] == "ssh"
    assert cmd[cmd.index("-i") + 1] == "/run/secrets/camoufox_key"
    assert "-o" in cmd
    assert "BatchMode=yes" in cmd
    assert "IdentitiesOnly=yes" in cmd
    assert "sshpass" not in " ".join(cmd)
    assert "-p" not in cmd
    assert cmd[-2] == "mirko@100.64.0.9"
    assert cmd[-1] == "echo hi"


def test_no_host_yields_no_command(monkeypatch):
    monkeypatch.delenv("DRHIRO_CAMOUFOX_SSH_HOST", raising=False)
    monkeypatch.delenv("DRHIRO_CAMOUFOX_SSH_USER", raising=False)

    assert _camoufox_ssh_command("echo hi") == []


def test_host_with_explicit_user_is_used_verbatim(monkeypatch):
    monkeypatch.setenv("DRHIRO_CAMOUFOX_SSH_HOST", "other@10.0.0.5")

    assert _camoufox_ssh_command("x")[-2] == "other@10.0.0.5"
