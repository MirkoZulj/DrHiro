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

## Files

| path | role |
|---|---|
| `docker-compose.isolated.yml` | the target stack |
| `Dockerfile` | one image, several roles; carries the real T1 source + migrations |
| `stack_access.py` | effective-access analyzer (used by the mandatory test) |
| `app/ingress.py` | trusted worker: receipt → claim → T1 → reply intent → delivery |
| `app/fake_telegram.py` | Bot API stand-in with delivery-failure modes |
| `app/openclaw_stub.py`, `app/mcp_stub.py` | model-accessible stand-ins |
| `app/probe_isolation.py` | measures reach from inside the untrusted container |
| `app/verify_rotation.py` | in-stack rotation / retired-key / kid-abuse checks |
| `app/stack_ctl.py` | test control + trusted-state reads |
| `sql/001_trusted.sql` | receipt, offset and reply-outbox tables |
