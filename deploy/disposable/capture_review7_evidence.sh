#!/usr/bin/env bash
# Regenerate review #7 evidence with exact commands, environment/versions, and the
# migration's failing-before / passing-after output.
#
# Deterministic sections are re-run here. The disposable-stack suite result was
# captured while the stack was running and is reproduced verbatim from the previous
# capture: the stack was subsequently STOPPED on instruction and is not restarted by
# this script.
#
# Usage: bash deploy/disposable/capture_review7_evidence.sh > <evidence file>
set -u
REPO="/home/mirko/work/DrHiro"
VENV="$REPO/.venv/bin/python"
DB="postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_test"
R1R4_DB="postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_r1r4_alembic"
BEFORE_TREE="/tmp/review7-before"          # clean worktree of base 8921c30
BASE=8921c305bce543f38653a5a948018a6ba33ba997
HEAD=8ea0bcbced78b0f2c1a210047873779cc9008577

echo "# Review #7 evidence — blocker fixes on the isolated branch"
echo
echo "Generated: $(date -u '+%Y-%m-%dT%H:%M:%SZ') (UTC)"
echo
echo "## Environment"
echo
echo '```'
echo "host:            $(uname -srm)"
echo "os:              $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"
echo "python:          $($VENV --version 2>&1)"
echo "docker:          $(docker --version 2>/dev/null)"
echo "compose:         $(docker compose version --short 2>/dev/null)"
echo "postgres:        $(psql "postgresql://drhiro:drhiro@localhost:5435/drhiro_test" -tAc 'select version()' 2>/dev/null | head -1)"
echo "server_version:  $(psql "postgresql://drhiro:drhiro@localhost:5435/drhiro_test" -tAc 'show server_version' 2>/dev/null)"
echo "sqlalchemy:      $($VENV -c 'import sqlalchemy;print(sqlalchemy.__version__)' 2>/dev/null)"
echo "psycopg2:        $($VENV -c 'import psycopg2;print(psycopg2.__version__)' 2>/dev/null)"
echo "branch:          $(cd $REPO && git rev-parse --abbrev-ref HEAD)"
echo "base:            $BASE"
echo "head:            $HEAD"
echo "frozen cand.:    7e2cf6915dbd7478e8a558817d4d51aa63879e60 (unchanged)"
echo '```'
echo
echo "Note on capability labelling: container uptimes were unchanged across the session."
echo "That supports **\"no observed restart\"**. It does NOT independently prove that"
echo "configuration, credentials, or data were unchanged. The evidence for 'no"
echo "production change' is that every action targeted the disposable stack (drhiro-iso)"
echo "or a disposable test database, and production was consulted read-only."
echo
echo "## Exact commands"
echo
echo '```bash'
echo "# migration: FAILING-BEFORE (clean worktree of the base, PRE-FIX source forced via"
echo "# PYTHONPATH because drhiro_api is an editable install pointing at the main tree -"
echo "# without this the pre-fix run silently imports the FIXED code and passes)"
echo "cd $BEFORE_TREE"
echo "PYTHONPATH=$BEFORE_TREE/apps/api/src DRHIRO_ACTIVITIES_MIGRATION_DB=1 \\"
echo "  DRHIRO_TEST_DB_URL='$DB' python -m pytest \\"
echo "  tests/test_r4_activities_migration.py::TestAdoptionRejectsInvalidSchemas -q"
echo
echo "# migration: PASSING-AFTER (HEAD source)"
echo "cd $REPO"
echo "DRHIRO_ACTIVITIES_MIGRATION_DB=1 DRHIRO_TEST_DB_URL='$DB' \\"
echo "  python -m pytest tests/test_r4_activities_migration.py -q"
echo
echo "# disposable stack suite (requires the stack UP; captured before the instructed stop)"
echo "DRHIRO_ISOLATED_STACK=1 python -m pytest tests/test_t1_isolated_ingress_stack.py -q"
echo
echo "# default suite, production inspection excluded"
echo "env -u DRHIRO_ISOLATED_STACK -u DRHIRO_R1R2_ALEMBIC_DB -u DRHIRO_R4_ALEMBIC_DB \\"
echo "  -u DRHIRO_T1_ALEMBIC_DB -u DRHIRO_ACTIVITIES_MIGRATION_DB -u DRHIRO_DEPLOY_ASSERT \\"
echo "  -u DRHIRO_DOCKER_CMD -u DRHIRO_TEST_DB_URL -u REDIS_URL python -m pytest tests/ -q"
echo
echo "# R1/R2 gated"
echo "DRHIRO_R1R2_ALEMBIC_DB=1 DRHIRO_R4_ALEMBIC_DB=1 DRHIRO_TEST_DB_URL='$R1R4_DB' \\"
echo "  REDIS_URL='redis://localhost:6382/15' python -m pytest \\"
echo "  tests/test_r4_orm_alembic_parity.py tests/test_r4_migration_chain.py \\"
echo "  tests/test_r1_nutrition_resolution.py tests/test_r2_trusted_key_set.py \\"
echo "  tests/test_r1_stack_isolation.py -q"
echo '```'
echo
echo "## B6 — migration adoption validator: FAILING-BEFORE / PASSING-AFTER"
echo
echo "### Proof the pre-fix run really used the PRE-FIX source"
echo
echo '```'
cd "$BEFORE_TREE"
PYTHONPATH="$BEFORE_TREE/apps/api/src" $VENV -c "
import drhiro_api.schema_activities as sa
print('schema_activities from:', sa.__file__)
print('_canon_check_expr (fix marker) present:', hasattr(sa,'_canon_check_expr'))
print('EXPECTED_DEFAULTS (fix marker) present:', hasattr(sa,'EXPECTED_DEFAULTS'))" 2>&1
echo '```'
echo
echo "### FAILING-BEFORE — the 7 new negative tests against the PRE-FIX validator (base $BASE)"
echo
echo '```'
PYTHONPATH="$BEFORE_TREE/apps/api/src" DRHIRO_ACTIVITIES_MIGRATION_DB=1 \
  DRHIRO_TEST_DB_URL="$DB" $VENV -m pytest \
  tests/test_r4_activities_migration.py::TestAdoptionRejectsInvalidSchemas \
  -q -p no:cacheprovider 2>&1 | tail -14
echo '```'
echo
echo "### PASSING-AFTER — full R4 suite against the FIXED validator (head $HEAD)"
echo
echo '```'
cd "$REPO"
DRHIRO_ACTIVITIES_MIGRATION_DB=1 DRHIRO_TEST_DB_URL="$DB" $VENV -m pytest \
  tests/test_r4_activities_migration.py -q -p no:cacheprovider 2>&1 | tail -4
echo '```'
echo
echo "## Disposable stack suite (captured while the stack was RUNNING)"
echo
echo "The stack was stopped on instruction after this capture and is NOT restarted here."
echo
echo '```'
echo "=== REVIEW #7 stack suite ==="
echo ".....................................                                    [100%]"
echo "37 passed in 413.13s (0:06:53)"
echo '```'
echo
echo "Recorded verbatim from the capture made while the stack was running (preserved in"
echo "the git history, commit 42fe5ba). The stack could not be re-run for this evidence"
echo "because it was stopped on instruction. NOTE: the earlier capture of that same"
echo "suite, made before the concurrent-probe isolation fix, showed 36 passed / 1 failed"
echo "(conflicting_payload_reuse); the 37-passed run is the post-fix result."
echo
echo
echo "Independent checks run against the live stack before the stop:"
echo
echo '```'
echo "from the model-accessible 'openclaw' container:"
echo "  GET http://ingress:8082/admin/status  -> UNREACHABLE (URLError)   [B2: network-level denial]"
echo "from the trusted ingress loopback, NO Authorization header:"
echo "  GET /admin/status -> HTTP 401 {\"error\":\"unauthorised\"}          [B2: auth enforced on GET]"
echo '```'
echo
echo "## Default suite (production inspection excluded)"
echo
echo '```'
cd "$REPO"
env -u DRHIRO_ISOLATED_STACK -u DRHIRO_R1R2_ALEMBIC_DB -u DRHIRO_R4_ALEMBIC_DB \
  -u DRHIRO_T1_ALEMBIC_DB -u DRHIRO_ACTIVITIES_MIGRATION_DB -u DRHIRO_DEPLOY_ASSERT \
  -u DRHIRO_DOCKER_CMD -u DRHIRO_TEST_DB_URL -u REDIS_URL \
  $VENV -m pytest tests/ -q -p no:cacheprovider 2>&1 | tail -2
echo '```'
echo
echo "## R1/R2 gated"
echo
echo '```'
DRHIRO_R1R2_ALEMBIC_DB=1 DRHIRO_R4_ALEMBIC_DB=1 DRHIRO_TEST_DB_URL="$R1R4_DB" \
  REDIS_URL='redis://localhost:6382/15' $VENV -m pytest \
  tests/test_r4_orm_alembic_parity.py tests/test_r4_migration_chain.py \
  tests/test_r1_nutrition_resolution.py tests/test_r2_trusted_key_set.py \
  tests/test_r1_stack_isolation.py -q -p no:cacheprovider 2>&1 | tail -2
echo '```'
echo
echo "## B7 — status: NOT CLOSED (wording corrected, behaviour unimplemented)"
echo
echo "Real polling-offset, sender-identity resolution, and edit/content-conflict"
echo "handling are UNIMPLEMENTED. No code path implements any of the three. A verified"
echo "bot identity authenticates the bot, not the sender."
echo
echo "## Attempt-fencing coverage (precise)"
echo
echo "COVERED by TestAttemptFencing (2 tests), both injecting a delayed completion from"
echo "an old attempt id:"
echo "  (1) late old result arriving AFTER an acknowledgement;"
echo "  (2) late old result arriving AFTER the transition to unknown."
echo
echo "NOT COVERED: a late old result arriving WHILE A NEWER ATTEMPT IS ACTIVELY IN"
echo "FLIGHT (state in_flight with current_attempt_id set to a different, newer attempt"
echo "id). No test exercises that interleaving. The implementation guards it by"
echo "construction (completions filter on current_attempt_id = the completing attempt),"
echo "but that is a code-reading claim, not a verified one. Coverage gap."
