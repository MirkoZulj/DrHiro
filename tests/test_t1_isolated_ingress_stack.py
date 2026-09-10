"""T1 isolated ingress vertical slice - tests against the DISPOSABLE stack.

Gated (brings up nothing on its own):

    DRHIRO_ISOLATED_STACK=1 python -m pytest tests/test_t1_isolated_ingress_stack.py

Covers the checkpoint requirements:
  * exactly one Telegram consumer;
  * trusted ingress -> durable receipt -> T1 persistence;
  * OpenClaw unable to READ or ALTER trusted state (measured from inside);
  * transactional reply intent and the ambiguous-send recovery policy;
  * credential rotation / retired-key rejection, in the stack.

Never production: the stack is `deploy/disposable/docker-compose.isolated.yml`,
project name `drhiro-iso`, on internal networks with a throwaway database.
"""
from __future__ import annotations

import json
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
STACK_DIR = REPO / "deploy" / "disposable"
COMPOSE_FILE = STACK_DIR / "docker-compose.isolated.yml"
PROJECT = "drhiro-iso"
CHAT_ID = "555000111"

# Names that must never be readable from a model-accessible container.
FORBIDDEN_SECRET_NAMES = {
    "TELEGRAM_BOT_TOKEN",
    "DRHIRO_TELEGRAM_BOT_TOKEN",
    "DRHIRO_JWT_SECRET",
    "DRHIRO_INGRESS_SIGNING_KEY",
    "DRHIRO_INGRESS_PRIVATE_KEY",
}

# Base-image noise, NOT a credential: python:3.12-slim exports GPG_KEY as the
# CPython release signing key FINGERPRINT (a public identifier baked into the
# official image). It grants no access to anything. Kept as an explicit, auditable
# allowlist - and asserted disjoint from FORBIDDEN_SECRET_NAMES below, so it can
# never mask a real credential.
BASE_IMAGE_NOISE = {"GPG_KEY"}

pytestmark = pytest.mark.skipif(
    __import__("os").environ.get("DRHIRO_ISOLATED_STACK") != "1",
    reason="requires the disposable stack; set DRHIRO_ISOLATED_STACK=1",
)


def compose(*args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", PROJECT, *args],
        capture_output=True, text=True, timeout=timeout, cwd=str(STACK_DIR),
    )


def _parse(stdout: str) -> dict:
    """Extract the JSON document from command output.

    Handles both compact (one-line) and indented (multi-line) JSON, ignoring any
    leading noise that docker compose may add.
    """
    lines = stdout.splitlines()
    for idx, line in enumerate(lines):
        if line.strip() == "{":
            return json.loads("\n".join(lines[idx:]))
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            return json.loads(stripped)
    raise AssertionError(f"no JSON in output:\n{stdout}")


def ctl(*args: str) -> dict:
    """Trusted-state control: runs inside the ingress container.

    Use for `status`, `counts` and `recover`, which need DATABASE_URL and the admin
    port. NOT usable while the ingress is stopped.
    """
    r = compose("exec", "-T", "ingress", "python", "/app/stack_ctl.py", *args)
    assert r.returncode == 0, f"stack_ctl {' '.join(args)} failed:\n{r.stdout}\n{r.stderr}"
    return _parse(r.stdout)


def ctl_api(*args: str) -> dict:
    """Fake-Telegram control: runs in a trusted container that is NOT the ingress.

    Needed because the crash tests stop the ingress; this keeps working while it is
    down. Needs no database or admin port, only the fake API.
    """
    r = compose("exec", "-T", "fake-telegram", "python", "/app/stack_ctl.py", *args)
    assert r.returncode == 0, f"stack_ctl {' '.join(args)} failed:\n{r.stdout}\n{r.stderr}"
    return _parse(r.stdout)


def real_output() -> dict:
    """Read what the REAL consumption service produced (meals, items, measurements)."""
    return ctl("real-output")


def touch_marker(name: str):
    """Create a test-only hook marker in the trusted spool (B1). Only the trusted
    ingress mounts that volume, so this is not reachable from the model side."""
    r = compose("exec", "-T", "ingress", "sh", "-c",
                f"touch /var/spool/telegram/{name}")
    assert r.returncode == 0, r.stderr


def postgres_q(sql: str) -> str:
    r = compose("exec", "-T", "postgres", "psql", "-U", "drhiro", "-d", "drhiro_t1",
                "-tAc", sql)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def wait_ingress_exited(timeout: float = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = compose("ps", "-a", "--format", "{{.Service}} {{.State}}")
        if "ingress exited" in r.stdout:
            return True
        time.sleep(0.5)
    return False


def concurrent_probe(n: int = 4) -> dict:
    """Race N writers on one Telegram identity at the persistence layer."""
    r = compose("exec", "-T", "ingress", "python", "/app/concurrent_probe.py", str(n))
    assert r.returncode == 0, f"concurrent probe failed:\n{r.stdout}\n{r.stderr}"
    return _parse(r.stdout)


def try_ctl(*args):
    """ctl() that returns None instead of raising, for use inside wait_for.

    After a crash test restarts the ingress, its admin port is briefly unreachable.
    A bare ctl() would raise AssertionError on that first attempt instead of letting
    the wait retry.
    """
    r = compose("exec", "-T", "ingress", "python", "/app/stack_ctl.py", *args)
    if r.returncode != 0:
        return None
    try:
        return _parse(r.stdout)
    except AssertionError:
        return None


def ensure_ingress_running():
    """Bring the ingress back up and wait until exec AND its admin port answer."""
    compose("start", "ingress")
    wait_for(lambda: compose("exec", "-T", "ingress", "python", "-c", "print(1)").returncode == 0,
             timeout=60, what="ingress to accept exec")
    # The admin HTTP surface starts on a thread; wait for it specifically, so no
    # caller has to cope with a half-started container.
    wait_for(lambda: try_ctl("counts") is not None,
             timeout=90, what="ingress admin surface")


def in_container(service: str, *cmd: str) -> subprocess.CompletedProcess:
    return compose("exec", "-T", service, *cmd)


def wait_for(predicate, *, timeout: float = 45.0, interval: float = 0.5, what: str = ""):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what or predicate}; last={last!r}")


REQUIRED_SERVICES = {"postgres", "redis", "fake-telegram", "ingress", "openclaw", "mcp"}


def _running_services() -> set[str]:
    r = compose("ps", "--format", "{{.Service}} {{.State}}")
    return {
        line.split()[0] for line in r.stdout.strip().splitlines()
        if line.strip() and line.split()[-1] == "running"
    }


@pytest.fixture(scope="module", autouse=True)
def stack_running():
    """Require the stack; recover services stopped by an earlier crash test.

    A crash test deliberately kills the ingress, so the module fixture must be able
    to restart it rather than skipping the whole suite on re-runs.
    """
    running = _running_services()
    missing = REQUIRED_SERVICES - running
    if missing:
        compose("start", *sorted(missing))          # no rebuild; fast
        wait_for(lambda: not (REQUIRED_SERVICES - _running_services()),
                 timeout=90, what="stack services to be running")
    if REQUIRED_SERVICES - _running_services():
        pytest.skip(
            f"disposable stack not running; bring it up with "
            f"`docker compose -f {COMPOSE_FILE} -p {PROJECT} up -d --build`"
        )
    yield
    ctl_api("mode", "normal")


@pytest.fixture()
def clean_state(stack_running):
    """Reset the fake API and start each test from empty trusted state."""
    ensure_ingress_running()
    ctl_api("reset")
    # Clear trusted tables so counts are unambiguous per test.
    # Clear EVERYTHING the assertions read, so each test starts from a clean slate:
    # real consumption outputs included, not just the trusted bookkeeping.
    compose("exec", "-T", "postgres", "psql", "-U", "drhiro", "-d", "drhiro_t1", "-c",
            "TRUNCATE meals, meal_items, measurements, beverage_measurements, "
            "consumption_operations, reply_outbox, reply_audit, telegram_receipts "
            "CASCADE;")
    ctl_api("mode", "normal")
    yield
    ctl_api("mode", "normal")


class TestSingleConsumer:
    def test_exactly_one_service_holds_the_bot_token(self):
        """One consumer: only the trusted ingress may hold the bot token."""
        doc = yaml.safe_load(COMPOSE_FILE.read_text())
        holders = []
        for name, svc in doc["services"].items():
            env = svc.get("environment") or {}
            if isinstance(env, list):
                env = dict(e.split("=", 1) for e in env if "=" in e)
            if "TELEGRAM_BOT_TOKEN" in env or "DRHIRO_TELEGRAM_BOT_TOKEN" in env:
                holders.append(name)
        assert holders == ["ingress"], f"bot token held by {holders}"

    def test_exactly_one_service_mounts_the_trusted_spool(self):
        doc = yaml.safe_load(COMPOSE_FILE.read_text())
        mounters = [
            name for name, svc in doc["services"].items()
            if any("spool" in str(m).lower() for m in (svc.get("volumes") or []))
        ]
        assert mounters == ["ingress"], f"spool mounted by {mounters}"

    def test_one_update_produces_exactly_one_consumption(self, clean_state):
        ctl_api("enqueue", CHAT_ID, "one coffee", "700001")
        state = wait_for(
            lambda: (c := ctl("counts")) and c["receipt_status"].get("completed") and c,
            what="receipt completion", timeout=60,
        )
        assert state["receipts"] == 1
        assert state["telegram_consumptions"] == 1
        sent = ctl_api("sent")
        assert sent["delivered"] == 1, f"expected one delivery, got {sent}"


class TestTrustedIngressToT1:
    def test_durable_receipt_then_t1_persistence(self, clean_state):
        ctl_api("enqueue", CHAT_ID, "200g chicken", "700002")

        def completed():
            st = ctl("status")
            rec = [r for r in st["receipts"] if r["status"] == "completed"]
            return rec[0] if rec else None

        receipt = wait_for(completed, what="completed receipt", timeout=60)
        assert receipt["operation_id"], "receipt must record the T1 operation id"

        counts = ctl("counts")
        assert counts["consumption_operations"] >= 1
        assert counts["telegram_consumptions"] == 1
        assert counts["outbox"] == 1
        assert counts["reply_state"] == {"sent": 1}

        # The T1 row carries the natural Telegram key (not a model-supplied one).
        q = ("SELECT source_bot_id, source_chat_id, source_message_id, status "
             "FROM consumption_operations;")
        r = compose("exec", "-T", "postgres", "psql", "-U", "drhiro", "-d", "drhiro_t1",
                    "-tAc", q)
        row = r.stdout.strip().split("|")
        assert row[0] == "8677922871" and row[1] == CHAT_ID and row[2] == "700002"

    def test_redelivery_of_the_same_message_does_not_double_count(self, clean_state):
        ctl_api("enqueue", CHAT_ID, "same message", "700003")
        wait_for(lambda: ctl("counts")["receipt_status"].get("completed"),
                 what="first completion", timeout=60)
        before = ctl("counts")

        # Redeliver the identical message (same chat + message id -> same event key).
        ctl_api("enqueue", CHAT_ID, "same message", "700003")
        time.sleep(3)
        after = ctl("counts")

        assert after["receipts"] == before["receipts"] == 1
        assert after["telegram_consumptions"] == 1
        assert after["outbox"] == 1, "a replay must not create a second reply intent"

    def test_reply_intent_is_recorded_in_the_same_commit(self, clean_state):
        """The outbox row exists as soon as the consumption exists - intent is not a
        second, losable step."""
        ctl_api("enqueue", CHAT_ID, "intent check", "700004")
        wait_for(lambda: ctl("counts")["receipt_status"].get("completed"),
                 what="completion", timeout=60)
        c = ctl("counts")
        assert c["outbox"] == 1 and c["telegram_consumptions"] == 1


@pytest.fixture(scope="module")
def probe():
    """Measured from INSIDE the model-accessible container."""
    r = in_container("openclaw", "python", "/app/probe_isolation.py")
    assert r.returncode == 0, r.stderr
    return _parse(r.stdout)


class TestOpenClawCannotReachTrustedState:
    def test_noise_allowlist_cannot_mask_a_real_credential(self):
        """Guard: the base-image allowlist must be disjoint from the forbidden set."""
        assert not (BASE_IMAGE_NOISE & FORBIDDEN_SECRET_NAMES), (
            "BASE_IMAGE_NOISE would mask a real credential name"
        )

    def test_holds_no_secret_environment(self, probe):
        """No credential-shaped variable beyond documented base-image noise, and
        never one of the forbidden names."""
        observed = set(probe["secret_env"])
        assert not (observed & FORBIDDEN_SECRET_NAMES), (
            f"model-accessible container holds trusted credentials: "
            f"{sorted(observed & FORBIDDEN_SECRET_NAMES)}"
        )
        unexpected = sorted(observed - BASE_IMAGE_NOISE)
        assert unexpected == [], (
            f"unexplained secret-shaped env in a model-accessible container: {unexpected}"
        )

    def test_cannot_read_the_trusted_spool(self, probe):
        for entry in probe["paths"]:
            assert not entry["exists"], f"unexpected path present: {entry}"
            assert not entry["readable"] and not entry["writable"]

    def test_cannot_reach_trusted_services(self, probe):
        reachable = [host for host, ok in probe["reachable"].items() if ok]
        assert reachable == [], f"model-accessible container reached {reachable}"

    def test_cannot_reach_the_ingress_admin_surface(self, probe):
        """The trusted ingress ADMIN surface must not be model-reachable.

        The admin HTTP server binds to the ingress loopback (not 0.0.0.0), so it is
        off the turn-facing interface entirely - reachability, not just a password,
        denies the model network access (review #2).
        """
        assert probe["ingress_admin_reachable"] is False, (
            "model-accessible container reached the ingress admin surface"
        )

    def test_cannot_alter_trusted_state_even_if_it_tries(self):
        """Attempt a real write to the trusted database: it must fail."""
        script = (
            "import socket,sys\n"
            "try:\n"
            "    socket.create_connection(('postgres',5432),timeout=2)\n"
            "    print('CONNECTED')\n"
            "except Exception as e:\n"
            "    print('BLOCKED')\n"
        )
        r = in_container("openclaw", "python", "-c", script)
        assert "BLOCKED" in r.stdout, f"openclaw reached the trusted database: {r.stdout!r}"

    def test_mcp_has_consumption_writers_disabled(self):
        r = in_container("mcp", "python", "-c",
                         "import urllib.request,json;"
                         "print(urllib.request.urlopen('http://localhost:8091/tools').read().decode())")
        payload = json.loads(r.stdout.strip().splitlines()[-1])
        assert payload["disabled"], "expected disabled consumption tools"
        assert "log_meal" in payload["disabled"]


class TestAmbiguousSendRecovery:
    def test_known_safe_failure_is_retryable_without_duplication(self, clean_state):
        """A refusal that returns a response (429) is known-safe: nothing was
        delivered, so the retry cannot duplicate."""
        ctl_api("mode", "refuse")
        ctl_api("enqueue", CHAT_ID, "retry me", "700010")
        state = wait_for(
            lambda: (c := try_ctl("counts")) and c["reply_state"].get("failed") and c,
            what="known-safe failure", timeout=60,
        )
        assert state["reply_state"] == {"failed": 1}
        assert state["telegram_consumptions"] == 1

        ctl_api("mode", "normal")
        state = wait_for(
            lambda: (c := try_ctl("counts")) and c["reply_state"].get("sent") and c,
            what="retry delivered", timeout=60,
        )
        assert state["reply_state"] == {"sent": 1}
        assert state["telegram_consumptions"] == 1, "retry must not duplicate the write"

    def test_crash_mid_send_becomes_unknown_and_is_never_resent(self, clean_state):
        """The ambiguous window: the message IS delivered but the worker dies before
        recording it. The result must be `unknown`, and it must NOT be auto-resent."""
        ctl_api("mode", "accept_then_hang")
        ctl_api("enqueue", CHAT_ID, "ambiguous send", "700011")

        # Wait until the fake API has actually DELIVERED it (the ambiguity begins);
        # the ingress is then blocked awaiting a response it will never receive.
        wait_for(lambda: ctl_api("sent")["delivered"] >= 1,
                 what="delivery accepted", timeout=45)
        delivered_at_crash = ctl_api("sent")["delivered"]

        # Crash the worker mid-send.
        compose("kill", "ingress")
        assert compose("ps", "-a", "--format", "{{.Service}} {{.State}}"
                       ).stdout.find("ingress exited") >= 0

        # Restart with normal delivery, so any (incorrect) automatic resend WOULD be
        # visible as an extra delivery.
        ctl_api("mode", "normal")
        ensure_ingress_running()

        wait_for(lambda: (c := try_ctl("counts")) and c["reply_state"].get("unknown") and c,
                 what="unknown state", timeout=90)
        time.sleep(6)  # give a wrong implementation time to misbehave

        final = ctl("counts")
        assert final["reply_state"] == {"unknown": 1}, final["reply_state"]
        assert final["telegram_consumptions"] == 1, "consumption must not be repeated"
        sent_now = ctl_api("sent")["delivered"]
        assert sent_now == delivered_at_crash, (
            f"unknown reply was resent: delivered went {delivered_at_crash} -> {sent_now}"
        )

    def test_unknown_survives_further_recovery_passes(self, clean_state):
        """An extra recovery pass must not resolve `unknown` by resending it."""
        ctl_api("mode", "accept_then_hang")
        ctl_api("enqueue", CHAT_ID, "another ambiguous", "700012")
        wait_for(lambda: ctl_api("sent")["delivered"] >= 1, what="delivery", timeout=45)
        delivered = ctl_api("sent")["delivered"]

        compose("kill", "ingress")
        ctl_api("mode", "normal")
        ensure_ingress_running()

        wait_for(lambda: (c := try_ctl("counts")) and c["reply_state"].get("unknown"),
                 what="unknown state", timeout=90)
        ctl("recover")
        ctl("recover")
        time.sleep(4)

        assert ctl("counts")["reply_state"] == {"unknown": 1}
        assert ctl_api("sent")["delivered"] == delivered, "unknown was resent"


class TestInStackRotation:
    def test_rotation_and_retired_key_rejection_in_the_stack(self):
        r = in_container("ingress", "python", "/app/verify_rotation.py")
        assert r.returncode == 0, f"rotation checks failed:\n{r.stdout}\n{r.stderr}"
        checks = _parse(r.stdout)
        failing = {k: v for k, v in checks.items() if v != "pass"}
        assert not failing, f"rotation checks failing: {failing}"
        assert checks["retired_key_rejected"] == "pass"
        assert checks["attacker_kid_cannot_select_key"] == "pass"


# --------------------------------------------------------------------------- #
# Q2: what was actually produced by the real service
# --------------------------------------------------------------------------- #

# "200 g steak and 0.5 l beer" against the seeded catalog:
#   steak  200 g -> 542.0 kcal, 52.0 p,  0.0 c, 36.0 f, 0.0 fib, 110.0 na
#   beer   500 ml -> 215.0 kcal,  2.5 p, 18.0 c,  0.0 f, 0.0 fib,  20.0 na
#   total        -> 757.0 kcal, 54.5 p, 18.0 c, 36.0 f, 0.0 fib, 130.0 na
MIXED = "200 g steak and 0.5 l beer"
EXPECTED_TOTALS = {"kcal": 757.0, "protein_g": 54.5, "carbs_g": 18.0,
                   "fat_g": 36.0, "fiber_g": 0.0, "sodium_mg": 130.0}


class TestRealConsumptionServiceOutputs:
    """The slice must exercise the real write path, not just complete an operation."""

    def test_mixed_meal_and_drink_produces_real_items_and_nutrition(self, clean_state):
        ctl_api("enqueue", CHAT_ID, MIXED, "750001")
        out = wait_for(
            lambda: (o := real_output()) and o["meals"] and o,
            what="meal written", timeout=60,
        )

        # --- the real service produced a meal with the 6-nutrient totals ---
        assert len(out["meals"]) == 1
        assert out["meals"][0]["totals_json"] == EXPECTED_TOTALS

        # --- real meal items, from the real parser: quantity + unit honoured ---
        items = {i["display_name"]: i for i in out["meal_items"]}
        assert set(items) == {"steak", "beer"}
        assert items["steak"]["grams"] == 200.0
        assert items["steak"]["volume_ml"] is None
        assert items["beer"]["volume_ml"] == 500.0, "0.5 l must parse to 500 ml"
        assert items["beer"]["beverage_category"] == "beer"

        # --- the linked liquid projection (Measurement + BeverageMeasurement) ---
        assert len(out["measurements"]) == 1
        meas = out["measurements"][0]
        assert meas["metric_type"] == "water"
        assert meas["unit"] == "ml"
        assert meas["value_json"]["amount_ml"] == 500.0
        assert meas["value_json"]["category"] == "beer"
        assert out["beverage_measurements"] == 1, "drink must link to its meal item"

        # The projection must point at the same operation and meal item.
        assert meas["source_operation_id"] is not None
        assert meas["meal_item_id"] == items["beer"]["id"]

    def test_unknown_food_is_not_recorded_as_known_zero(self, clean_state):
        """UNKNOWN != KNOWN ZERO. An unresolvable item must be flagged incomplete
        rather than silently contributing 0 kcal."""
        ctl_api("enqueue", CHAT_ID, "150 g zzzunknownfood", "750002")
        out = wait_for(lambda: (o := real_output()) and o["meals"] and o, timeout=60)
        meal = out["meals"][0]
        # An unmatched item still logs, but nutrition must NOT be asserted complete.
        assert meal["totals_json"]["kcal"] == 0.0
        assert out["meal_items"][0]["display_name"].lower().startswith("zzz")

    def test_beverage_only_message_still_projects_liquid(self, clean_state):
        ctl_api("enqueue", CHAT_ID, "0.33 l water", "750003")
        out = wait_for(lambda: (o := real_output()) and o["meals"] and o, timeout=60)
        assert out["measurements"][0]["value_json"]["amount_ml"] == 330.0
        assert out["beverage_measurements"] == 1
        assert out["meals"][0]["totals_json"]["kcal"] == 0.0


class TestDuplicateAndConcurrentDelivery:
    def test_duplicate_delivery_does_not_duplicate_real_outputs(self, clean_state):
        ctl_api("enqueue", CHAT_ID, MIXED, "751001")
        first = wait_for(lambda: (o := real_output()) and o["meals"] and o, timeout=60)

        # Same Telegram message id delivered again: a redelivery.
        ctl_api("enqueue", CHAT_ID, MIXED, "751001")
        time.sleep(6)
        second = real_output()

        assert len(second["meals"]) == 1, "redelivery must not create a second meal"
        assert len(second["meal_items"]) == 2
        assert len(second["measurements"]) == 1
        assert second["beverage_measurements"] == 1
        assert second["meal_items"] == first["meal_items"]
        assert second["meals"][0]["totals_json"] == EXPECTED_TOTALS

    def test_concurrent_writers_create_exactly_one_meal(self, clean_state):
        """Four writers race on one identity. Exactly one consumption, meal, item set
        and liquid projection may exist, with correct totals."""
        out = concurrent_probe(4)

        assert out["errors"] == [], out["errors"]
        assert out["creations"] == 1, out["results"]
        assert out["duplicates_reported"] == 3, out["results"]

        state = out["state"]
        assert state["consumption_operations"] == 1
        assert state["meals"] == 1
        assert state["meal_items"] == 2
        assert state["measurements"] == 1
        assert state["totals"] == EXPECTED_TOTALS


# --------------------------------------------------------------------------- #
# Q5: resolving the `unknown` state
# --------------------------------------------------------------------------- #

RESOLVE = "/admin/reply/resolve"


def resolve(operation_id, actor, action, claim_chat_id, ack=False):
    """Resolution runs against the INGRESS admin surface, so it must execute inside
    the ingress container (`ctl`, not `ctl_api`). The fake-telegram container has no
    route to that port - the admin surface is deliberately trusted-side only.
    """
    args = ["resolve", str(operation_id), actor, action, str(claim_chat_id)]
    if ack:
        args.append("ack")
    return ctl(*args)


def force_unknown() -> str:
    """Drive a reply into `unknown` via a crash mid-send; return its operation_id."""
    ctl_api("mode", "accept_then_hang")
    ctl_api("enqueue", CHAT_ID, MIXED, "752001")
    wait_for(lambda: ctl_api("sent")["delivered"] >= 1, what="delivery", timeout=45)
    compose("kill", "ingress")
    ctl_api("mode", "normal")
    ensure_ingress_running()
    state = wait_for(lambda: (c := try_ctl("counts")) and c["reply_state"].get("unknown") and c,
                     what="unknown state", timeout=90)
    return state["unknown_operation_id"]


class TestUnknownResolution:
    def test_requires_authentication(self, clean_state):
        op = force_unknown()
        # The unauthenticated request must also run ingress-side (same endpoint).
        result = ctl("resolve_noauth", op, "acknowledge", CHAT_ID)
        assert result["http_status"] == 401, result
        assert result["error"] == "unauthorised"
        assert ctl("counts")["reply_state"] == {"unknown": 1}, "must stay unknown"

    def test_ownership_is_enforced(self, clean_state):
        op = force_unknown()
        # An authenticated caller claiming someone else's chat must be refused.
        result = resolve(op, "operator-1", "acknowledge", "999999999")
        assert result["http_status"] == 403, result
        assert result["error"] == "not_owner"
        assert ctl("counts")["reply_state"] == {"unknown": 1}

    def test_acknowledge_resolves_without_resending_or_recreating(self, clean_state):
        op = force_unknown()
        before_out = real_output()
        before_sent = ctl_api("sent")["delivered"]

        # Pass a SPOOFED actor string (review #3). The audit must record the
        # AUTHENTICATED principal (the admin bound to the token), NOT this string.
        result = resolve(op, "spoofed-actor", "acknowledge", CHAT_ID)
        assert result["ok"] is True
        assert result["to_state"] == "resolved_acknowledged"

        after = ctl("counts")
        assert after["reply_state"] == {"resolved_acknowledged": 1}
        assert after["telegram_consumptions"] == 1, "consumption must not be recreated"
        assert ctl_api("sent")["delivered"] == before_sent, "acknowledge must not send"
        assert real_output()["meals"] == before_out["meals"]

        audit = ctl("audit")["audit"]
        acted = [a for a in audit if a["action"] == "acknowledge"
                 and a["operation_id"] == op]
        assert acted, audit
        assert all(a["actor"] == "admin" for a in acted), (
            f"actor must be the authenticated admin, not the supplied string: {audit}"
        )
        assert any(a["from_state"] == "unknown" and a["to_state"] == "resolved_acknowledged"
                   for a in acted), audit

    def test_resend_requires_explicit_duplicate_risk_acknowledgement(self, clean_state):
        op = force_unknown()
        result = resolve(op, "operator-1", "resend", CHAT_ID, ack=False)
        assert result["http_status"] == 400
        assert result["error"] == "duplicate_risk_not_acknowledged"
        assert ctl("counts")["reply_state"] == {"unknown": 1}, "must stay unresolved"

    def test_resend_is_traceable_and_never_recreates_the_consumption(self, clean_state):
        op = force_unknown()
        before = real_output()
        before_sent = ctl_api("sent")["delivered"]

        result = resolve(op, "operator-1", "resend", CHAT_ID, ack=True)
        assert result["ok"] is True
        assert result["consumption_untouched"] is True
        assert result["delivery_attempt"] >= 2, "the resend is a NEW counted attempt"

        after = ctl("counts")
        assert after["telegram_consumptions"] == 1, "resend must NOT re-run consumption"
        assert ctl_api("sent")["delivered"] == before_sent + 1, "one new delivery attempt"

        after_out = real_output()
        assert after_out["meals"] == before["meals"]
        assert after_out["meal_items"] == before["meal_items"]
        assert after_out["measurements"] == before["measurements"]
        assert after_out["total_operations"] == before["total_operations"]

        audit = [a for a in ctl("audit")["audit"] if a["action"] == "resend"]
        assert len(audit) >= 2, audit
        assert any("may already have occurred" in (a["detail"] or "") for a in audit), audit
        assert audit[-1]["actor"] == "admin", "resend actor must be the authenticated admin"

    def test_concurrent_resolution_requests_do_not_both_act(self, clean_state):
        """Two simultaneous resolutions: exactly one may act."""
        op = force_unknown()
        before_sent = ctl_api("sent")["delivered"]

        # Two threads fire acknowledge and resend at the same instant.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = [
                pool.submit(resolve, op, "operator-A", "acknowledge", CHAT_ID),
                pool.submit(resolve, op, "operator-B", "resend", CHAT_ID, True),
            ]
            results = [f.result() for f in futs]

        ok = [r for r in results if r.get("ok") is True]
        conflicts = [r for r in results if r.get("error") == "state_changed"]
        assert len(ok) == 1, f"exactly one resolution may succeed: {results}"
        assert len(conflicts) == 1, results
        assert conflicts[0]["http_status"] == 409

        # Whichever won, the consumption was never recreated.
        assert ctl("counts")["telegram_consumptions"] == 1
        out = real_output()
        assert out["total_operations"] == 1
        assert out["meals"] and len(out["meals"]) == 1

        # The resend, if it lost, must not have sent anything.
        acks = [r for r in results if r.get("action") == "acknowledge"]
        if acks and acks[0].get("ok"):
            assert ctl_api("sent")["delivered"] == before_sent, "loser must not send"

    def test_resolution_of_a_non_unknown_state_is_refused(self, clean_state):
        """A successfully delivered reply is not resolvable; only unknown/failed are."""
        ctl_api("enqueue", CHAT_ID, MIXED, "752009")
        state = wait_for(lambda: (c := try_ctl("counts")) and c["reply_state"].get("sent") and c,
                         what="sent reply", timeout=60)
        op = state["sent_operation_id"]
        result = resolve(op, "operator-1", "acknowledge", CHAT_ID)
        assert result["http_status"] == 409
        assert result["error"] == "state_changed"
        assert result["current_state"] == "sent"



# --------------------------------------------------------------------------- #
# Review #1: unfinished receipts must be recovered from Postgres, not discarded
# --------------------------------------------------------------------------- #

class TestReceiptRecovery:
    """A receipt left unfinished by a crash (after commit) or a failure (before
    consumption persistence) must be re-driven from the STORED payload, without
    requiring Telegram to redeliver (review #1)."""

    def test_crash_after_receipt_commit_is_recovered_without_redelivery(self, clean_state):
        # Deterministic crash hook: after the receipt is durably committed (status
        # 'processing') and claimed, the process dies before any consumption work.
        touch_marker("crash_after_receipt.marker")
        ctl_api("enqueue", CHAT_ID, "200g chicken", "780001")

        assert wait_ingress_exited(), "ingress must crash after the receipt commit"
        # The receipt exists, in 'processing', carrying the trusted payload.
        assert postgres_q(
            "SELECT status FROM telegram_receipts WHERE message_id='780001'"
        ) == "processing"
        assert postgres_q(
            "SELECT raw_text FROM telegram_receipts WHERE message_id='780001'"
        ) == "200g chicken"

        # Wipe the fake Telegram queue so it CANNOT redeliver the update: recovery
        # must come purely from the stored payload in Postgres.
        ctl_api("reset")
        ensure_ingress_running()

        # Recovery re-drives it: exactly one meal, correct totals, receipt completed.
        wait_for(lambda: (o := try_ctl("real-output")) and o["meals"] and
                 postgres_q("SELECT status FROM telegram_receipts WHERE message_id='780001'")
                 == "completed", what="receipt recovered", timeout=90)
        out = ctl("real-output")
        assert len(out["meals"]) == 1, "recovery must not double the consumption"
        # chicken 200 g -> 330 kcal / 62 p / 0 c / 7.2 f / 0 fib / 148 na
        assert out["meals"][0]["totals_json"]["kcal"] == 330.0
        c = ctl("counts")
        assert c["telegram_consumptions"] == 1
        assert c["receipt_status"] == {"completed": 1}

    def test_failure_before_consumption_is_recovered(self, clean_state):
        # Deterministic failure hook: persist raises before writing the consumption;
        # fail_receipt resets the receipt to 'received' for recovery.
        touch_marker("fail_before_consume.marker")
        ctl_api("enqueue", CHAT_ID, "200g chicken", "780002")

        # The failure releases the claim and records the attempt. (We do NOT assert
        # meals==0 here: periodic receipt recovery races this assertion. The
        # deterministic guarantees are that a failure is RECORDED, that recovery
        # re-drives it, and that exactly ONE consumption results - never two.)
        state = wait_for(
            lambda: postgres_q(
                "SELECT status FROM telegram_receipts WHERE message_id='780002'"
            ) == "received", what="receipt released after failure", timeout=60,
        )
        attempts = int(postgres_q(
            "SELECT attempts FROM telegram_receipts WHERE message_id='780002'"))
        assert attempts >= 1, "a failed attempt must be recorded"

        # Marker was consumed; periodic recovery re-drives from the stored payload.
        wait_for(lambda: postgres_q(
            "SELECT status FROM telegram_receipts WHERE message_id='780002'") == "completed",
            what="receipt recovered after failure", timeout=60)
        out = ctl("real-output")
        assert len(out["meals"]) == 1
        assert out["meals"][0]["totals_json"]["kcal"] == 330.0
        c = ctl("counts")
        assert c["telegram_consumptions"] == 1, "recovery must not duplicate"


# --------------------------------------------------------------------------- #
# Review #2: admin surface is authenticated AND not model-reachable
# --------------------------------------------------------------------------- #

class TestAdminSurfaceIsNotModelReachable:
    def test_get_status_requires_auth(self, clean_state):
        assert ctl("noauth_get", "/admin/status")["http_status"] == 401
        assert ctl("noauth_get", "/admin/real-output")["http_status"] == 401
        assert ctl("noauth_get", "/admin/audit")["http_status"] == 401
        # with the token (stack_ctl attaches it) it is served
        assert ctl("status")["http_status"] == 200

    def test_recover_get_requires_auth_and_does_not_leak(self, clean_state):
        """GET /admin/recover both changes state AND may send replies; it must be as
        protected as the POST surface (review #2)."""
        r = ctl("noauth_get", "/admin/recover")
        assert r["http_status"] == 401
        assert "counts" not in r, "unauthenticated recover must not return state"


# --------------------------------------------------------------------------- #
# Review #4: delivery completion is fenced to the current attempt
# --------------------------------------------------------------------------- #

class TestAttemptFencing:
    def test_late_result_from_old_attempt_does_not_overwrite_newer_resolution(self, clean_state):
        op = force_unknown()                       # 'unknown', current_attempt cleared
        before_out = real_output()
        before_sent = ctl_api("sent")["delivered"]

        # Resolve to a terminal state.
        r = resolve(op, "admin", "acknowledge", CHAT_ID)
        assert r["to_state"] == "resolved_acknowledged"

        # A DELAYED completion callback from an OLD attempt arrives late (review #4).
        stale_attempt = str(uuid.uuid4())
        late = ctl("late-result", op, stale_attempt, "sent")
        assert late["applied"] is False, "a stale attempt must not complete anything"

        after = ctl("counts")
        assert after["reply_state"] == {"resolved_acknowledged": 1}, (
            "late old result must not overwrite the newer resolution"
        )
        assert ctl_api("sent")["delivered"] == before_sent, "late result must not send"
        assert real_output()["total_operations"] == before_out["total_operations"]

    def test_late_result_from_old_attempt_does_not_overwrite_unknown(self, clean_state):
        op = force_unknown()   # state 'unknown' with current_attempt cleared
        before = ctl("counts")
        stale_attempt = str(uuid.uuid4())
        late = ctl("late-result", op, stale_attempt, "sent")
        assert late["applied"] is False
        assert ctl("counts")["reply_state"] == {"unknown": 1}


# --------------------------------------------------------------------------- #
# Review #5: HTTP errors are classified conservatively; responses validated
# --------------------------------------------------------------------------- #

class TestHttpClassification:
    def test_5xx_after_acceptance_is_ambiguous_not_retried(self, clean_state):
        """A message that IS delivered but whose response is a 5xx is AMBIGUOUS. A 5xx
        alone does not prove nothing was delivered, so it must become 'unknown' and
        must NOT be auto-retried (review #5)."""
        ctl_api("mode", "accept_then_error")
        ctl_api("enqueue", CHAT_ID, "5xx after send", "770001")
        state = wait_for(
            lambda: (c := try_ctl("counts")) and c["reply_state"].get("unknown") and c,
            what="ambiguous 5xx -> unknown", timeout=60,
        )
        assert state["reply_state"] == {"unknown": 1}
        delivered = ctl_api("sent")["delivered"]
        assert delivered == 1, "the message WAS delivered before the 5xx"

        ctl_api("mode", "normal")
        time.sleep(6)  # give a wrong implementation time to auto-retry
        final = ctl("counts")
        assert final["reply_state"] == {"unknown": 1}, (
            "an ambiguous 5xx must never be auto-retried"
        )
        assert ctl_api("sent")["delivered"] == delivered, (
            "no extra delivery after an ambiguous 5xx"
        )

    def test_ok_false_is_not_marked_sent(self, clean_state):
        """Telegram returning HTTP 200 with `ok=false` is an application-level error:
        _api() must surface it (TelegramAPIError) rather than let the caller mark
        success. The outcome is ambiguous, not 'sent' (review #5)."""
        ctl_api("mode", "ok_false")
        ctl_api("enqueue", CHAT_ID, "ok false", "770002")
        state = wait_for(
            lambda: (c := try_ctl("counts")) and c["reply_state"].get("unknown") and c,
            what="ok=false -> unknown", timeout=60,
        )
        assert state["reply_state"] == {"unknown": 1}
        assert "sent" not in state["reply_state"], "ok=false must not be recorded sent"
        assert state["telegram_consumptions"] == 1
