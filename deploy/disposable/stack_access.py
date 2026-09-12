"""Effective-access analysis for a container stack definition.

Correction #1 requires isolation to be tested on *effective access*, not on
environment-variable names. A container's effective access is the union of:

  * secrets it can read (env values, env_file contents, secret mounts);
  * filesystem reach (bind mounts and volumes), and the mode they are mounted with;
  * privilege escalation surfaces (privileged, capabilities, security_opt,
    userns/pid/ipc/host-network modes, devices);
  * administrative interfaces (the Docker socket, or any socket/port that grants
    control over other containers);
  * network reach (which networks it is attached to, and therefore which services
    it can talk to).

This module computes that from a compose file. It is deliberately pure: it reads a
YAML file and returns findings. No Docker daemon, no production access, so the
mandatory test can run in the default suite.

Findings are split into `violations` (must never appear on a model-accessible
service) and `notes` (informational, e.g. which secrets a trusted service holds).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Names that indicate a credential. Matched case-insensitively against env keys.
SECRET_KEY_RE = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|_KEY$|PRIVATE|CREDENTIAL|APIKEY|API_KEY|JWT)",
    re.IGNORECASE,
)

# Paths that constitute an administrative interface to the container runtime.
ADMIN_PATH_MARKERS = ("docker.sock", "containerd.sock", "/run/docker", "podman.sock")

# Capabilities that defeat the container boundary if granted.
DANGEROUS_CAPS = {
    "SYS_ADMIN", "SYS_PTRACE", "SYS_MODULE", "DAC_READ_SEARCH", "DAC_OVERRIDE",
    "NET_ADMIN", "NET_RAW", "SETFCAP", "SYS_CHROOT", "ALL",
}

# Resource markers for the trusted Telegram spool / bot configuration. Note
# ".openclaw" (with the leading dot) rather than "openclaw": the real spool path is
# /home/node/.openclaw, and a service must not be flagged merely for mounting its
# own code directory.
TRUSTED_RESOURCE_MARKERS = ("spool", "telegram", ".openclaw", "bot-token", "botconf")


@dataclass
class ServiceAccess:
    name: str
    secrets: set[str] = field(default_factory=set)
    mounts: list[dict[str, str]] = field(default_factory=list)
    privileges: set[str] = field(default_factory=set)
    admin_interfaces: set[str] = field(default_factory=set)
    networks: set[str] = field(default_factory=set)
    ports: list[str] = field(default_factory=list)

    def trusted_resource_mounts(self) -> list[dict[str, str]]:
        out = []
        for m in self.mounts:
            blob = f"{m.get('source','')} {m.get('target','')}".lower()
            if any(marker in blob for marker in TRUSTED_RESOURCE_MARKERS):
                out.append(m)
        return out


@dataclass
class StackAnalysis:
    services: dict[str, ServiceAccess]

    def access(self, name: str) -> ServiceAccess:
        return self.services[name]


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _parse_mount(entry: Any) -> dict[str, str]:
    """Normalise both short syntax ('src:dst:mode') and long syntax mappings."""
    if isinstance(entry, dict):
        return {
            "type": str(entry.get("type", "")),
            "source": str(entry.get("source", "")),
            "target": str(entry.get("target", "")),
            "mode": str(entry.get("read_only", "")),
        }
    parts = str(entry).split(":")
    source = parts[0] if parts else ""
    target = parts[1] if len(parts) > 1 else ""
    mode = parts[2] if len(parts) > 2 else ""
    return {"type": "", "source": source, "target": target, "mode": mode}


def _env_secrets(service: dict) -> set[str]:
    out: set[str] = set()
    for item in _as_list(service.get("environment")):
        if isinstance(item, dict):
            pairs = item.items()
        else:
            if "=" not in str(item):
                continue
            k, v = str(item).split("=", 1)
            pairs = [(k, v)]
        for k, v in pairs:
            if SECRET_KEY_RE.search(str(k)):
                out.add(str(k))
    return out


def analyze_compose(path: str | Path) -> StackAnalysis:
    doc = yaml.safe_load(Path(path).read_text()) or {}
    services: dict[str, ServiceAccess] = {}

    for name, svc in (doc.get("services") or {}).items():
        svc = svc or {}
        acc = ServiceAccess(name=name)
        acc.secrets = _env_secrets(svc)

        # env_file: the file's keys are readable secrets; record the file as a mount
        # too, because its contents are reachable from inside the container.
        for ef in _as_list(svc.get("env_file")):
            if isinstance(ef, dict):
                acc.mounts.append({"type": "env_file", "source": str(ef.get("source", ef)),
                                   "target": "", "mode": ""})
            else:
                acc.mounts.append({"type": "env_file", "source": str(ef),
                                   "target": "", "mode": ""})

        for m in _as_list(svc.get("volumes")) + _as_list(svc.get("secrets")):
            acc.mounts.append(_parse_mount(m))

        if svc.get("privileged"):
            acc.privileges.add("privileged")
        for cap in _as_list(svc.get("cap_add")):
            acc.privileges.add(f"cap_add:{cap}")
        for opt in _as_list(svc.get("security_opt")):
            acc.privileges.add(f"security_opt:{opt}")
        for key in ("userns_mode", "pid", "ipc", "network_mode"):
            val = svc.get(key)
            if val:
                acc.privileges.add(f"{key}:{val}")
        for dev in _as_list(svc.get("devices")):
            acc.privileges.add(f"device:{dev}")

        for m in acc.mounts:
            blob = f"{m.get('source','')}{m.get('target','')}".lower()
            if any(marker in blob for marker in ADMIN_PATH_MARKERS):
                acc.admin_interfaces.add(f"mount:{m.get('source') or m.get('target')}")
        if str(svc.get("network_mode", "")).lower() in {"host", "container"}:
            acc.admin_interfaces.add(f"network_mode:{svc['network_mode']}")

        for net in _as_list(svc.get("networks")):
            if isinstance(net, dict):
                acc.networks.update(str(k) for k in net)
            else:
                acc.networks.add(str(net))

        acc.ports = [str(p) for p in _as_list(svc.get("ports"))]
        services[name] = acc

    return StackAnalysis(services=services)


def violations_for(
    analysis: StackAnalysis,
    service: str,
    *,
    forbidden_secrets: set[str] | None = None,
    allow_networks: set[str] | None = None,
) -> list[str]:
    """Every way the named (model-accessible) service has real access it must not.

    Returns human-readable violations; an empty list means the service is clean.
    """
    acc = analysis.access(service)
    out: list[str] = []

    forbidden = forbidden_secrets or set()
    leaked = sorted(acc.secrets & forbidden)
    if leaked:
        out.append(f"{service}: reads trusted secrets {leaked}")

    for m in acc.trusted_resource_mounts():
        mode = m.get("mode") or "rw"
        out.append(
            f"{service}: mounts trusted resource {m.get('source') or m.get('target')} "
            f"(mode={mode})"
        )
    for m in acc.mounts:
        if m.get("type") == "env_file" and any(
            marker in str(m.get("source", "")).lower() for marker in TRUSTED_RESOURCE_MARKERS
        ):
            out.append(f"{service}: reads trusted env_file {m.get('source')}")

    if "privileged" in acc.privileges:
        out.append(f"{service}: privileged container")
    for p in sorted(acc.privileges):
        if p.startswith("cap_add:"):
            cap = p.split(":", 1)[1].upper()
            if cap in DANGEROUS_CAPS:
                out.append(f"{service}: dangerous capability {cap}")
        elif p.startswith(("userns_mode:", "pid:", "ipc:")):
            out.append(f"{service}: {p}")
        elif p.startswith("network_mode:") and p.split(":", 1)[1] in {"host", "container"}:
            out.append(f"{service}: {p}")

    for admin in sorted(acc.admin_interfaces):
        out.append(f"{service}: administrative interface {admin}")

    if allow_networks is not None:
        extra = sorted(acc.networks - allow_networks)
        if extra:
            out.append(f"{service}: attached to networks {extra}")

    return out
