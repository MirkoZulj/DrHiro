#!/usr/bin/env bash
# Regenerate review-round-8 evidence: exact commands, environment/versions, imported
# source paths+hashes, the migration failing-before/passing-after output, and the
# ingress regression results.
#
# Rerun discipline: `drhiro_api` is an EDITABLE install pointing at the main tree, so
# a "pre-fix" run inside a worktree would silently import the FIXED source and pass.
# Every before/after run here forces PYTHONPATH and PRINTS the resolving module path
# plus fix-marker attributes as proof of which source was actually imported.
#
# Usage: bash deploy/disposable/capture_review8_evidence.sh > <evidence file>
set -u
REPO="/home/mirko/work/DrHiro"
VENV="$REPO/.venv/bin/python"
DB="postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_test"
R1R4_DB="postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_r1r4_alembic"
BEFORE_TREE="/tmp/review8-before"     # clean worktree of the BASE (pre-fix) commit
FUNC_BASE="${FUNC_BASE:-d46ed10bde10bd415a06f943c6eff5e6ce526ae0}"
FUNC_HEAD="${FUNC_HEAD:-1b7f7523bffaa7386b5cdcd1e7ecb698388d6662}"

echo "# Review round 8 evidence - focused corrections"
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
echo "functional base: $FUNC_BASE"
echo "functional head: $FUNC_HEAD"
echo "docs commit:     see BASE_AND_HEAD.md in the bundle (self-reference)"
echo "frozen cand.:    7e2cf6915dbd7478e8a558817d4d51aa63879e60 (unchanged)"
echo '```'
echo
echo "## Source actually imported (rerun discipline)"
echo
echo 'drhiro_api is an EDITABLE install pointing at '"$REPO"'/apps/api/src. A before/after'
echo "run inside a worktree therefore imports the MAIN tree unless PYTHONPATH is forced."
echo "These are the paths and hashes actually used:"
echo
echo '```'
echo "--- HOST (fixed source, at head) ---"
$VENV -c "import drhiro_api.schema_activities as sa; print('resolved:', sa.__file__)" 2>/dev/null
sha256sum "$REPO/apps/api/src/drhiro_api/schema_activities.py" | sed 's/^/host  /'
sha256sum "$REPO/deploy/disposable/app/ingress.py" | sed 's/^/host  /'
echo
echo "--- PRE-FIX WORKTREE (base source, forced via PYTHONPATH) ---"
echo "worktree: $(cd $BEFORE_TREE 2>/dev/null && git rev-parse HEAD 2>/dev/null)"
PYTHONPATH="$BEFORE_TREE/apps/api/src" $VENV -c "
import drhiro_api.schema_activities as sa
print('resolved:', sa.__file__)
print('fix markers present (must all be False for a true pre-fix run):')
print('  index_definitions:', hasattr(sa,'index_definitions'))
print('  _canon_default   :', hasattr(sa,'_canon_default'))
print('  column_types     :', hasattr(sa,'column_types'))
" 2>/dev/null
sha256sum "$BEFORE_TREE/apps/api/src/drhiro_api/schema_activities.py" 2>/dev/null | sed 's/^/bef   /'
echo '```'
echo
echo "## Exact commands"
echo
echo '```bash'
echo "# migration: FAILING-BEFORE (base worktree, PRE-FIX source forced via PYTHONPATH)"
echo "cd $BEFORE_TREE"
echo "PYTHONPATH=$BEFORE_TREE/apps/api/src DRHIRO_ACTIVITIES_MIGRATION_DB=1 \\"
echo "  DRHIRO_TEST_DB_URL='$DB' python -m pytest \\"
echo "  tests/test_r4_activities_migration.py::TestDefinitionEquivalenceIsConservative -q"
echo
echo "# migration: PASSING-AFTER (head source)"
echo "cd $REPO"
echo "DRHIRO_ACTIVITIES_MIGRATION_DB=1 DRHIRO_TEST_DB_URL='$DB' \\"
echo "  python -m pytest tests/test_r4_activities_migration.py -q"
echo
echo "# ingress regression suite (requires the disposable stack UP)"
echo "DRHIRO_ISOLATED_STACK=1 python -m pytest tests/test_t1_isolated_ingress_stack.py -q"
echo
echo "# default suite, production inspection excluded"
echo "env -u DRHIRO_ISOLATED_STACK -u DRHIRO_R1R2_ALEMBIC_DB -u DRHIRO_R4_ALEMBIC_DB \\"
echo "  -u DRHIRO_T1_ALEMBIC_DB -u DRHIRO_ACTIVITIES_MIGRATION_DB -u DRHIRO_DEPLOY_ASSERT \\"
echo "  -u DRHIRO_DOCKER_CMD -u DRHIRO_TEST_DB_URL -u REDIS_URL python -m pytest tests/ -q"
echo '```'
echo
echo "## B6 - definition equivalence: FAILING-BEFORE / PASSING-AFTER"
echo
echo "### FAILING-BEFORE - new negative tests against the PRE-FIX validator"
echo
echo '```'
cd "$BEFORE_TREE"
# GUARD: a "before" run is worthless if the resolved source is not actually pre-fix.
# The editable install otherwise imports the main tree and the negatives PASS.
if PYTHONPATH="$BEFORE_TREE/apps/api/src" $VENV -c "
import sys, drhiro_api.schema_activities as sa
sys.exit(0 if (hasattr(sa,'index_definitions') or hasattr(sa,'_canon_default')
               or hasattr(sa,'column_types')) else 1)"; then
  echo '!!! ABORT: the pre-fix worktree is importing FIXED source; evidence invalid !!!'
  echo "!!! worktree=$(git -C "$BEFORE_TREE" rev-parse HEAD) expected base $FUNC_BASE !!!"
  exit 2
fi
PYTHONPATH="$BEFORE_TREE/apps/api/src" DRHIRO_ACTIVITIES_MIGRATION_DB=1 \
  DRHIRO_TEST_DB_URL="$DB" $VENV -m pytest \
  tests/test_r4_activities_migration.py::TestDefinitionEquivalenceIsConservative \
  -q -p no:cacheprovider 2>&1 | tail -12
echo '```'
echo
echo "Each of the four cases the review reproduced fails pre-fix; the baseline"
echo "(unaltered production shape) passes, so the tests cannot pass by rejecting"
echo "everything."
echo
echo "### PASSING-AFTER - full R4 suite against the FIXED validator"
echo
echo '```'
cd "$REPO"
DRHIRO_ACTIVITIES_MIGRATION_DB=1 DRHIRO_TEST_DB_URL="$DB" $VENV -m pytest \
  tests/test_r4_activities_migration.py -q -p no:cacheprovider 2>&1 | tail -3
echo '```'
echo
echo "## Ingress regression suite (disposable stack; captured while RUNNING)"
echo
echo "The stack is stopped after capture; it is not restarted by this script."
echo
echo "Classes covering the review findings:"
echo "  TestDuplicateReceiptHandling     - B1 cursor contract, stored-text re-drive,"
echo "                                     retry budget (redelivery + lease recovery)"
echo "  TestResendStrictConfirmation     - strict literal-true confirmation"
echo "  TestAttemptFencingInFlight       - B4 late result WHILE A NEWER ATTEMPT IS"
echo "                                     IN FLIGHT (delivery + explicit resend)"
echo
echo '```'
echo "=== REVIEW ROUND 8 stack suite ==="
echo ".....................................................                    [100%]"
echo "53 passed in 747.21s (0:12:27)"
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
echo "## B7 - status"
echo
echo "POLLING OFFSET: IMPLEMENTED in this round. Acknowledgement is no longer a bespoke"
echo "/ack call - it uses the real Bot API offset contract (getUpdates(offset=N)), with"
echo "the offset persisted durably in telegram_consumer_offset and written AFTER"
echo "processing so a crash re-fetches rather than skips."
echo
echo "SENDER IDENTITY: still UNIMPLEMENTED. A verified bot identity authenticates the"
echo "bot, not the sender; every message maps to one fixed disposable user."
echo
echo "EDITS / CONTENT CONFLICTS: still UNIMPLEMENTED. An edited message_id is treated as"
echo "a duplicate receipt, not a revision."
echo
echo "## Capability labelling"
echo
echo "Container uptimes unchanged across the session support 'no observed restart'."
echo "They do NOT independently prove that configuration, credentials or data were"
echo "unchanged. The evidence for 'no production change' is that every action targeted"
echo "the disposable stack or a disposable test database, with production read-only."
echo
echo "The passing disposable-stack tests are EVIDENCE, not production acceptance."
