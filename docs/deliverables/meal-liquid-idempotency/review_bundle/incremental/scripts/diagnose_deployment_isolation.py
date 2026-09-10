#!/usr/bin/env python
"""READ-ONLY deployment diagnostic: what access does each running container have?

This is a DIAGNOSTIC, not a test and not enforcement. Production inspection must
never run as part of the default test suite, so this lives outside `tests/` and
requires an explicit invocation:

    # local Docker
    python scripts/diagnose_deployment_isolation.py

    # remote host (read-only; docker inspect only)
    DRHIRO_DOCKER_CMD="ssh root@host docker" \
        python scripts/diagnose_deployment_isolation.py --container drhiro-mcp

    # machine-readable
    python scripts/diagnose_deployment_isolation.py --json

It uses `docker inspect` ONLY. It starts nothing, stops nothing, writes nothing, and
never prints secret VALUES - only key names, mount paths and modes.

Exit status is 0 unless --fail-on-exposure is given, in which case it exits 1 when a
model-accessible container currently holds trusted material. That flag is for
reporting pipelines, not for the test suite.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "deploy" / "disposable"))

from stack_access import (  # noqa: E402
    ADMIN_PATH_MARKERS,
    DANGEROUS_CAPS,
    SECRET_KEY_RE,
    TRUSTED_RESOURCE_MARKERS,
)

DEFAULT_MODEL_ACCESSIBLE = ["drhiro-openclaw-gateway-1", "drhiro-mcp"]
FORBIDDEN_ENV = (
    "TELEGRAM_BOT_TOKEN",
    "DRHIRO_TELEGRAM_BOT_TOKEN",
    "DRHIRO_JWT_SECRET",
    "DRHIRO_TELEGRAM_INGRESS_SECRET",
    "DRHIRO_INGRESS_SIGNING_KEY",
    "DRHIRO_INGRESS_PRIVATE_KEY",
)


def _docker_cmd(*args: str) -> list[str]:
    prefix = __import__("os").environ.get("DRHIRO_DOCKER_CMD")
    return (prefix.split() if prefix else ["docker"]) + list(args)


def inspect(container: str) -> dict | None:
    r = subprocess.run(_docker_cmd("inspect", container), capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)[0]
    except Exception:
        return None


def effective_access(container: str) -> dict:
    """Effective access of a running container: env secrets, mounts, privileges,
    administrative interfaces, network modes. Values are never read."""
    data = inspect(container)
    if data is None:
        return {"present": False}

    cfg = data.get("Config") or {}
    host = data.get("HostConfig") or {}
    env = cfg.get("Env") or []
    env_keys = sorted(e.split("=", 1)[0] for e in env if "=" in e)
    secrets = [k for k in env_keys if SECRET_KEY_RE.search(k)]

    mounts = []
    for m in data.get("Mounts") or []:
        mounts.append({
            "source": m.get("Source", ""),
            "target": m.get("Destination", ""),
            "mode": m.get("Mode", ""),
            "rw": bool(m.get("RW")),
        })

    privileges = []
    if host.get("Privileged"):
        privileges.append("privileged")
    for cap in host.get("CapAdd") or []:
        if str(cap).upper() in DANGEROUS_CAPS:
            privileges.append(f"cap_add:{cap}")
    for key, label in (
        ("UsernsMode", "userns_mode"),
        ("PidMode", "pid"),
        ("IpcMode", "ipc"),
    ):
        val = host.get(key)
        if val and val not in ("default", "private", ""):
            privileges.append(f"{label}:{val}")
    # NetworkMode is the network NAME for ordinary bridge attachments; only host or
    # sharing another container's namespace removes the network boundary.
    net_mode = str(host.get("NetworkMode") or "")
    if net_mode == "host" or net_mode.startswith("container:"):
        privileges.append(f"network_mode:{net_mode}")
    for opt in host.get("SecurityOpt") or []:
        privileges.append(f"security_opt:{opt}")
    for dev in host.get("Devices") or []:
        privileges.append(f"device:{dev.get('PathOnHost', dev)}")

    admin = []
    for m in mounts:
        blob = f"{m['source']}{m['target']}".lower()
        if any(marker in blob for marker in ADMIN_PATH_MARKERS):
            admin.append(f"mount:{m['source'] or m['target']}")

    return {
        "present": True,
        "env_keys": env_keys,
        "secrets": secrets,
        "mounts": mounts,
        "privileges": privileges,
        "admin_interfaces": admin,
    }


def exposures(container: str, forbidden: tuple[str, ...]) -> list[str]:
    acc = effective_access(container)
    if not acc.get("present"):
        return []
    out = []
    for secret in forbidden:
        if secret in acc["secrets"]:
            out.append(f"{container}: reads {secret}")
    for m in acc["mounts"]:
        blob = f"{m['source']} {m['target']}".lower()
        if any(marker in blob for marker in TRUSTED_RESOURCE_MARKERS):
            out.append(
                f"{container}: mounts trusted resource {m['target']} "
                f"({'rw' if m['rw'] else 'ro'})"
            )
    for p in acc["privileges"]:
        out.append(f"{container}: {p}")
    for a in acc["admin_interfaces"]:
        out.append(f"{container}: {a}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--container", action="append", default=None,
                    help="model-accessible container to report (repeatable)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--fail-on-exposure", action="store_true",
                    help="exit 1 if a model-accessible container holds trusted material")
    args = ap.parse_args()

    containers = args.container or DEFAULT_MODEL_ACCESSIBLE
    report = {c: effective_access(c) for c in containers}
    findings: list[str] = []
    for c in containers:
        findings.extend(exposures(c, FORBIDDEN_ENV))

    if args.json:
        print(json.dumps({"access": report, "exposures": findings}, indent=2))
    else:
        print("READ-ONLY DIAGNOSTIC - current deployment access (no enforcement)\n")
        for c, acc in report.items():
            if not acc.get("present"):
                print(f"  {c}: NOT PRESENT")
                continue
            print(f"  {c}:")
            print(f"    credential-shaped env keys: {acc['secrets']}")
            for m in acc["mounts"]:
                print(f"    mount: {m['target']} <- {m['source']} ({'rw' if m['rw'] else 'ro'})")
            if acc["privileges"]:
                print(f"    privileges: {acc['privileges']}")
            if acc["admin_interfaces"]:
                print(f"    admin interfaces: {acc['admin_interfaces']}")
        print("\nOBSERVED EXPOSURES (diagnostic; exposure != abuse):")
        for f in findings or ["  none"]:
            print(f"  - {f}")
        print(
            "\nNOTE: enforcement is tests/test_r1_stack_isolation.py over the target "
            "stack.\n      This diagnostic records what the CURRENT deployment does; it "
            "does not\n      fail a suite and must not be wired into one."
        )

    if args.fail_on_exposure and findings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
