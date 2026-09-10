# Disposable isolated stack — bring-up and evidence

Disposable only. **Never production.** Nothing here touches the deployed stack,
its containers, its database, or its credentials.

## Bring up

```bash
cd deploy/disposable
docker compose -f docker-compose.isolated.yml -p drhiro-iso up -d --build
docker compose -f docker-compose.isolated.yml -p drhiro-iso ps
```

Services: `postgres` + `redis` + `fake-telegram` (trusted network), `ingress`
(trusted + turn; the single Telegram consumer), `openclaw` + `mcp` (turn only;
model-accessible), `migrate` (one-shot; runs the real Alembic chain).

## Run the tests

```bash
DRHIRO_ISOLATED_STACK=1 python -m pytest tests/test_t1_isolated_ingress_stack.py -v
```

The suite restarts any service a previous crash test stopped. `ingress` is killed
mid-send on purpose by `test_crash_mid_send_becomes_unknown_and_is_never_resent`;
that is the test working, not the stack failing.

## Tear down

```bash
docker compose -f docker-compose.isolated.yml -p drhiro-iso down -v
```

## What the boundary actually is

Isolation is enforced by `tests/test_r1_stack_isolation.py` (mandatory, in the
default suite) over **effective access**: secrets, mounts, privileges,
administrative interfaces and network reach. `TestCheckerIsNotVacuous` injects eight
violations (leaked secret, trusted mount, docker socket, privileged, SYS_ADMIN, host
network/pid, reach into the trusted network, trusted env_file) and asserts each is
caught — so a green run cannot mean "the checker finds nothing".

## Diagnosing the CURRENT deployment (read-only, NOT a test)

```bash
# local docker
python scripts/diagnose_deployment_isolation.py

# deployed host, read-only `docker inspect`
DRHIRO_DOCKER_CMD="ssh root@<host> docker" \
  python scripts/diagnose_deployment_isolation.py --json
```

This prints key NAMES and mount paths/modes only — never values — and is deliberately
outside `tests/` so production inspection can never run as part of the suite.

## What is REAL here, and what is a substitute

Stated plainly so the evidence is not over-read:

- **REAL:** PostgreSQL; the frozen candidate's `drhiro_api` domain **unmodified**; the
  real Alembic chain (the `migrate` service runs it); the seeded food catalog used for
  genuine DB-first nutrition resolution.
- **REAL but NEW, not deployed:** `app/ingress.py`, the trusted side.
- **TEST SUBSTITUTES:** `fake_telegram.py`, `openclaw_stub.py`, `mcp_stub.py`.

`openclaw_stub.py` **is not OpenClaw**. Isolation assertions taken from inside it
prove the *topology* denies that container access to trusted state; they do **not**
prove the real (Node) OpenClaw functions without the bot token and spool mount. That
compatibility check is still pending.

Only the fake Telegram API and throwaway credentials are used. No production bot, no
production bot token, no production storage, no production volume.

## Resolution of the `unknown` reply state

`POST /admin/reply/resolve` on the trusted admin surface:

```bash
# acknowledge: accept the ambiguity, send nothing
docker compose ... exec ingress python /app/stack_ctl.py resolve <op_id> operator-1 acknowledge 555000111
# resend: explicit, audited, requires the duplicate-risk acknowledgement
docker compose ... exec ingress python /app/stack_ctl.py resolve <op_id> operator-1 resend 555000111 ack
```

Authenticated (`Bearer $INGRESS_ADMIN_TOKEN`, fails closed if unset), ownership-checked
against `reply_owners`, audited in `reply_audit`, serialised by `SELECT ... FOR UPDATE`.
A resend records a new traceable delivery attempt and **never** re-runs the
consumption.

## Evidence capture

```bash
bash deploy/disposable/capture_evidence.sh
```

Writes sanitised evidence (image tags/digests, postgres version, Alembic revision,
table inventory, real consumption output, trusted bookkeeping, credential key
**names**) to the review bundle's `evidence/` directory.

## Files

| path | role |
|---|---|
| `docker-compose.isolated.yml` | the target stack |
| `Dockerfile` | one image, several roles; carries the real T1 source + migrations |
| `stack_access.py` | effective-access analyzer (used by the mandatory test) |
| `capture_evidence.sh` | sanitised evidence capture for the review artifact |
| `app/ingress.py` | trusted worker: receipt → claim → **real service write** → reply intent → delivery; plus resolution |
| `app/seed_catalog.py` | deterministic offline food catalog so resolution is real, not stubbed |
| `app/concurrent_probe.py` | N writers race one identity at the persistence layer |
| `app/fake_telegram.py` | Bot API stand-in with delivery-failure modes |
| `app/openclaw_stub.py`, `app/mcp_stub.py` | model-accessible stand-ins (NOT the real apps) |
| `app/probe_isolation.py` | measures reach from inside the untrusted container |
| `app/verify_rotation.py` | in-stack rotation / retired-key / kid-abuse checks |
| `app/stack_ctl.py` | test control, trusted-state reads, resolution calls |
| `sql/001_trusted.sql` | receipt, offset, reply-outbox, reply-audit, reply-owner tables |
