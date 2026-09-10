"""R1 - MANDATORY isolation test for the target stack (runs in the default suite).

This is enforcement, not a diagnostic. It asserts the isolation contract of
`deploy/disposable/docker-compose.isolated.yml` on EFFECTIVE ACCESS - secrets,
mounts, privileges, administrative interfaces and network reach - rather than on
environment-variable names alone.

Two properties make it meaningful:

  * it is a normal test that must pass (no xfail, no "known exposure" escape);
  * `TestCheckerIsNotVacuous` proves the checker actually CATCHES violations, so a
    green run cannot be an artefact of a checker that finds nothing by construction.

Current-deployment (production) inspection is deliberately NOT here. It lives in
`scripts/diagnose_deployment_isolation.py` and requires an explicit read-only
invocation.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
STACK_DIR = REPO / "deploy" / "disposable"
COMPOSE = STACK_DIR / "docker-compose.isolated.yml"

sys.path.insert(0, str(STACK_DIR))
from stack_access import analyze_compose, violations_for  # noqa: E402

# Model-accessible = the model can reach or drive it.
MODEL_ACCESSIBLE = ("openclaw", "mcp")
# The trusted ingress is the only component allowed to hold trusted material.
TRUSTED = "ingress"
# Secrets that must never be readable by a model-accessible service.
FORBIDDEN_SECRETS = {
    "TELEGRAM_BOT_TOKEN",
    "DRHIRO_TELEGRAM_BOT_TOKEN",
    "DRHIRO_JWT_SECRET",
    "DRHIRO_INGRESS_SIGNING_KEY",
    "DRHIRO_INGRESS_PRIVATE_KEY",
}
ALLOWED_NETWORKS = {"turn"}


@pytest.fixture(scope="module")
def analysis():
    assert COMPOSE.exists(), f"target stack definition missing: {COMPOSE}"
    return analyze_compose(COMPOSE)


class TestTargetStackIsolation:
    def test_stack_defines_the_expected_roles(self, analysis):
        for name in (*MODEL_ACCESSIBLE, TRUSTED):
            assert name in analysis.services, f"{name} missing from the target stack"

    @pytest.mark.parametrize("service", MODEL_ACCESSIBLE)
    def test_model_accessible_service_has_no_trusted_access(self, analysis, service):
        """The mandatory assertion: no secrets, no trusted mounts, no privilege,
        no administrative interface, no reach beyond the turn network."""
        violations = violations_for(
            analysis,
            service,
            forbidden_secrets=FORBIDDEN_SECRETS,
            allow_networks=ALLOWED_NETWORKS,
        )
        assert not violations, (
            f"{service} has effective access it must not have:\n  - "
            + "\n  - ".join(violations)
        )

    def test_trusted_ingress_is_the_sole_holder_of_trusted_material(self, analysis):
        """Guards the test from passing vacuously: the ingress really does hold the
        bot token, the signing key, and the spool mount."""
        acc = analysis.access(TRUSTED)
        assert "TELEGRAM_BOT_TOKEN" in acc.secrets, "ingress must hold the bot token"
        assert "DRHIRO_INGRESS_SIGNING_KEY" in acc.secrets
        assert acc.trusted_resource_mounts(), "ingress must mount the trusted spool"

        holders = [
            name
            for name in analysis.services
            if name != TRUSTED
            and (
                (analysis.access(name).secrets & FORBIDDEN_SECRETS)
                or analysis.access(name).trusted_resource_mounts()
            )
        ]
        assert holders == [], f"trusted material also held by: {holders}"

    @pytest.mark.parametrize("service", MODEL_ACCESSIBLE)
    def test_model_accessible_service_cannot_reach_trusted_services(self, analysis, service):
        """Network reach is access: postgres/redis/fake-telegram must be on the
        trusted network only, and unreachable from the model-accessible services."""
        assert analysis.access(service).networks == ALLOWED_NETWORKS
        for data_service in ("postgres", "redis", "fake-telegram"):
            assert "trusted" not in analysis.access(data_service).networks - {"trusted"} or True
        for data_service in ("postgres", "redis", "fake-telegram"):
            assert analysis.access(data_service).networks == {"trusted"}, (
                f"{data_service} must be on the trusted network only"
            )


class TestCheckerIsNotVacuous:
    """Proves the analyzer detects real violations, so a green mandatory test means
    something. Each case injects one violation into the target stack and asserts it
    is caught."""

    def _mutate(self, tmp_path: Path, mutate) -> list[str]:
        doc = yaml.safe_load(COMPOSE.read_text())
        mutate(doc)
        path = tmp_path / "mutated.yml"
        path.write_text(yaml.safe_dump(doc))
        return violations_for(
            analyze_compose(path),
            "openclaw",
            forbidden_secrets=FORBIDDEN_SECRETS,
            allow_networks=ALLOWED_NETWORKS,
        )

    def test_detects_leaked_secret(self, tmp_path):
        def mutate(doc):
            doc["services"]["openclaw"]["environment"]["DRHIRO_JWT_SECRET"] = "leaked"
        v = self._mutate(tmp_path, mutate)
        assert any("DRHIRO_JWT_SECRET" in x for x in v), v

    def test_detects_trusted_spool_mount(self, tmp_path):
        def mutate(doc):
            doc["services"]["openclaw"]["volumes"] = ["trusted-spool:/var/spool/telegram"]
        v = self._mutate(tmp_path, mutate)
        assert any("trusted resource" in x for x in v), v

    def test_detects_docker_socket_mount(self, tmp_path):
        def mutate(doc):
            doc["services"]["openclaw"]["volumes"] = ["/var/run/docker.sock:/var/run/docker.sock"]
        v = self._mutate(tmp_path, mutate)
        assert any("administrative interface" in x for x in v), v

    def test_detects_privileged(self, tmp_path):
        def mutate(doc):
            doc["services"]["openclaw"]["privileged"] = True
        v = self._mutate(tmp_path, mutate)
        assert any("privileged" in x for x in v), v

    def test_detects_dangerous_capability(self, tmp_path):
        def mutate(doc):
            doc["services"]["openclaw"]["cap_add"] = ["SYS_ADMIN"]
        v = self._mutate(tmp_path, mutate)
        assert any("SYS_ADMIN" in x for x in v), v

    def test_detects_host_network_and_pid(self, tmp_path):
        def mutate(doc):
            doc["services"]["openclaw"]["network_mode"] = "host"
            doc["services"]["openclaw"]["pid"] = "host"
        v = self._mutate(tmp_path, mutate)
        assert any("network_mode" in x for x in v), v
        assert any("pid" in x for x in v), v

    def test_detects_reach_into_the_trusted_network(self, tmp_path):
        def mutate(doc):
            doc["services"]["openclaw"]["networks"] = ["turn", "trusted"]
        v = self._mutate(tmp_path, mutate)
        assert any("trusted" in x for x in v), v

    def test_detects_trusted_env_file(self, tmp_path):
        def mutate(doc):
            doc["services"]["openclaw"]["env_file"] = ["telegram-bot.env"]
        v = self._mutate(tmp_path, mutate)
        assert any("env_file" in x for x in v), v
