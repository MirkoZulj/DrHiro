"""R1 - key/credential placement assertion for the trusted-ingress split.

Correction #1 requires that "trusted ingress" be separated from model-accessible
execution, and that the separation be ENFORCED rather than assumed. HMAC (or any
signing scheme) establishes nothing if the model-accessible runtime can read the
signing key, the bot token, or write the trusted spool records.

This module turns that requirement into an executable check over the running
containers:

  * model-accessible containers (the conversational gateway and the MCP the model
    calls) must NOT hold the bot token, the JWT signing secret, the ingress
    signing key, or a private signing key;
  * they must NOT mount the trusted spool volume (read or write).

Deliberately reports rather than silently passing: the discovery test prints the
observed inventory, and the enforcement test is marked xfail until the R1 split is
implemented. When the split lands it becomes XPASS, which is visible and prompts
promoting it to a hard assertion.

Gated: requires a reachable Docker daemon and the containers to inspect.

    DRHIRO_DEPLOY_ASSERT=1 python -m pytest tests/test_r1_deployment_isolation.py
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("DRHIRO_DEPLOY_ASSERT") != "1",
    reason="requires a Docker daemon + deployed containers; set DRHIRO_DEPLOY_ASSERT=1",
)

# Containers the model can reach or drive. These must be credential-light.
MODEL_ACCESSIBLE = ["drhiro-openclaw-gateway-1", "drhiro-mcp"]
TRUSTED = ["drhiro-ingress", "drhiro-api-1"]

# Secrets that must not exist in a model-accessible runtime.
FORBIDDEN_ENV = (
    "TELEGRAM_BOT_TOKEN",
    "DRHIRO_TELEGRAM_BOT_TOKEN",
    "DRHIRO_JWT_SECRET",
    "DRHIRO_TELEGRAM_INGRESS_SECRET",
    "DRHIRO_INGRESS_SIGNING_KEY",
    "DRHIRO_INGRESS_PRIVATE_KEY",
)

# The spool the trusted worker claims from; a model-accessible mount is fatal.
SPOOL_MARKERS = (".openclaw", "spool", "telegram-ingress")


def _docker_cmd(*args: str) -> list[str]:
    """Build a docker command, optionally targeting a remote daemon.

    `DRHIRO_DOCKER_CMD` (e.g. 'sshpass -p … ssh root@host docker') lets this run
    from a workstation against the deployment host.
    """
    prefix = os.environ.get("DRHIRO_DOCKER_CMD")
    if prefix:
        return prefix.split() + list(args)
    return ["docker", *args]


def _inspect(container: str) -> dict | None:
    r = subprocess.run(
        _docker_cmd("inspect", container), capture_output=True, text=True
    )
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)[0]
    except Exception:
        return None


def _inventory(container: str) -> dict:
    data = _inspect(container)
    if data is None:
        return {"present": False}
    cfg = data.get("Config") or {}
    env = cfg.get("Env") or []
    keys = sorted(e.split("=", 1)[0] for e in env if "=" in e)
    mounts = [
        {
            "dest": m.get("Destination"),
            "mode": m.get("Mode"),
            "source": m.get("Source"),
        }
        for m in (data.get("Mounts") or [])
    ]
    return {"present": True, "env_keys": keys, "mounts": mounts}


def _findings(container: str) -> list[str]:
    """Credential/mount violations for a model-accessible container."""
    inv = _inventory(container)
    if not inv.get("present"):
        return []
    out: list[str] = []
    keys = set(inv["env_keys"])
    for secret in FORBIDDEN_ENV:
        if secret in keys:
            out.append(f"{container}: env {secret} present")
    for m in inv["mounts"]:
        dest = (m["dest"] or "").lower()
        if any(marker in dest for marker in SPOOL_MARKERS):
            mode = m.get("mode") or "?"
            out.append(f"{container}: spool mount {m['dest']} ({mode})")
    return out


class TestDeploymentIsolation:
    def test_inventory_is_reported(self, capsys):
        """Discovery: report the actual placement. Never asserts placement."""
        report = {}
        for c in MODEL_ACCESSIBLE + TRUSTED:
            inv = _inventory(c)
            report[c] = {
                "present": inv.get("present"),
                "forbidden_env_present": [
                    k for k in inv.get("env_keys", []) if k in FORBIDDEN_ENV
                ],
                "mounts": [m["dest"] for m in inv.get("mounts", [])],
            }
        with capsys.disabled():
            print("\n[R1 deployment inventory]")
            print(json.dumps(report, indent=2))
        assert report, "no containers inspected"

    @pytest.mark.xfail(
        reason=(
            "R1 container split not implemented: the spool volume and Telegram "
            "credentials currently live in the model-accessible gateway"
        ),
        strict=False,
    )
    def test_model_accessible_containers_are_credential_light(self):
        findings: list[str] = []
        for c in MODEL_ACCESSIBLE:
            findings.extend(_findings(c))
        assert not findings, (
            "model-accessible containers hold trusted material: " + "; ".join(findings)
        )

    @pytest.mark.xfail(
        reason="trusted ingress container not yet deployed (R1)",
        strict=False,
    )
    def test_trusted_ingress_owns_the_spool_and_bot_token(self):
        inv = _inventory("drhiro-ingress")
        assert inv.get("present"), "drhiro-ingress not running"
        keys = set(inv.get("env_keys", []))
        assert "TELEGRAM_BOT_TOKEN" in keys, "trusted ingress must hold the bot token"
        assert any("spool" in (m["dest"] or "").lower() for m in inv.get("mounts", []))
