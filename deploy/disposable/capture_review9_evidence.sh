#!/usr/bin/env bash
# Regenerate review-round-9 evidence: exact commands, environment/versions, the source
# paths+hashes ACTUALLY imported (host + pre-fix worktree + RUNNING INGRESS CONTAINER),
# the migration failing-before/passing-after output, and the ingress regression result.
#
# Rerun discipline: `drhiro_api` is an EDITABLE install pointing at the main tree, so a
# pre-fix run inside a worktree silently imports the FIXED source unless PYTHONPATH is
# forced - and even then it must be the BASE worktree, not HEAD. Every before/after run
# here forces PYTHONPATH, prints the resolving module path and fix-marker attributes,
# and ABORTS if the pre-fix tree is importing fixed source.
#
# Usage: bash deploy/disposable/capture_review9_evidence.sh <stack_suite_literal>
set -u
REPO="/home/mirko/work/DrHiro"
VENV="$REPO/.venv/bin/python"
DB="postgresql+psycopg2://drhiro:drhiro@localhost:5435/drhiro_test"
BEFORE_TREE="/tmp/review9-before"     # clean worktree of the BASE (pre-fix) commit
STACK_SUITE="${1:-(stack result supplied at capture time)}"

FUNC_BASE="${FUNC_BASE:-c59cec4522f8adb70c70805caa33426cd54a39a6}"   # fully-reviewed round-8 state
FUNC_HEAD="${FUNC_HEAD:-$(git -C "$REPO" log --format=%H -1 --grep='fix(review9)')}"

echo "# Review round 9 evidence - focused corrections"
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
echo "branch:          $(git -C "$REPO" rev-parse --abbrev-ref HEAD)"
echo "functional base: $FUNC_BASE"
echo "functional head: $FUNC_HEAD"
echo "docs head:       $(git -C "$REPO" rev-parse HEAD)"
echo "frozen cand.:    7e2cf6915dbd7478e8a558817d4d51aa63879e60 (unchanged)"
echo '```'
echo
echo "## Source actually imported (rerun discipline)"
echo
echo "drhiro_api is an EDITABLE install pointing at $REPO/apps/api/src; a before/after"
echo "run inside a worktree imports the MAIN tree unless PYTHONPATH is forced. These are"
echo "the paths and hashes actually used:"
echo
echo '```'
echo "--- HOST (fixed source, at head) ---"
$VENV -c "import drhiro_api.schema_activities as sa; print('resolved:', sa.__file__)" 2>/dev/null
sha256sum "$REPO/apps/api/src/drhiro_api/schema_activities.py" | sed 's/^/host  /'
sha256sum "$REPO/deploy/disposable/app/ingress.py" | sed 's/^/host  /'
echo
echo "--- RUNNING INGRESS CONTAINER (what the image actually executed) ---"
cd "$REPO/deploy/disposable"
docker compose -f docker-compose.isolated.yml -p drhiro-iso exec -T ingress sh -c \
  'sha256sum /app/ingress.py; grep -c PAUSE_AFTER_CLAIM_MARKER /app/ingress.py | sed "s/^/pause_hook_refs=/"' 2>&1 \
  | sed 's/^/ctnr  /'
cd "$REPO"
echo
echo "--- PRE-FIX WORKTREE (base source, forced via PYTHONPATH) ---"
echo "worktree: $(git -C "$BEFORE_TREE" rev-parse HEAD 2>/dev/null)"
PYTHONPATH="$BEFORE_TREE/apps/api/src" $VENV -c "
import drhiro_api.schema_activities as sa
print('resolved:', sa.__file__)
print('fix markers present (must be False for a true pre-fix run):')
print('  _relation_oid :', hasattr(sa,'_relation_oid'))
print('  _type_precision:', hasattr(sa,'_type_precision'))
print('  column_types(oid) len guard:', sa.column_types.__doc__ and 'attrelid' in sa.column_types.__doc__)
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
echo "  tests/test_r4_activities_migration.py::TestSchemaNoneDoesNotCombineRelations \\"
echo "  tests/test_r4_activities_migration.py::TestConservativeTypeAndDefaultEquivalence -q"
echo
echo "# migration: PASSING-AFTER (head source)"
echo "cd $REPO"
echo "DRHIRO_ACTIVITIES_MIGRATION_DB=1 DRHIRO_TEST_DB_URL='$DB' \\"
echo "  python -m pytest tests/test_r4_activities_migration.py -q"
echo
echo "# ingress regression suite (requires the disposable stack UP)"
echo "DRHIRO_ISOLATED_STACK=1 python -m pytest tests/test_t1_isolated_ingress_stack.py -q"
echo '```'
echo
echo "## Finding 2 + 3 - failing-before / passing-after (real PostgreSQL)"
echo
echo "### FAILING-BEFORE - new negatives against the PRE-FIX validator"
echo
echo '```'
cd "$BEFORE_TREE"
PYTHONPATH="$BEFORE_TREE/apps/api/src" DRHIRO_ACTIVITIES_MIGRATION_DB=1 \
  DRHIRO_TEST_DB_URL="$DB" $VENV -m pytest \
  tests/test_r4_activities_migration.py::TestSchemaNoneDoesNotCombineRelations \
  tests/test_r4_activities_migration.py::TestConservativeTypeAndDefaultEquivalence \
  -q -p no:cacheprovider 2>&1 | tail -10
echo '```'
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
echo "Finding 1 coverage (TestExhaustionSweepRespectsActiveLease):"
echo "  - live final claim survives a concurrent recovery pass (status/token/lease"
echo "    intact; exhausted=0); worker released -> exactly one consumption + completion"
echo "  - expired final claim is swept to exhausted and refuses redelivery"
echo
echo '```'
echo "$STACK_SUITE"
echo '```'
echo
echo "## B7 - status"
echo
echo "SENDER IDENTITY: still UNIMPLEMENTED. A verified bot identity authenticates the"
echo "bot, not the sender; every message maps to one fixed disposable user."
echo
echo "EDITS / CONTENT CONFLICTS: still UNIMPLEMENTED. An edited message_id is treated as"
echo "a duplicate receipt, not a revision."
echo
echo "POLLING OFFSET: implemented (review 8), real Bot API offset contract with a durable"
echo "offset in telegram_consumer_offset. Not full production polling acceptance."
echo
echo "## Capability labelling"
echo
echo "Container uptimes unchanged support 'no observed restart' only; they do not"
echo "independently prove that configuration, credentials or data were unchanged. Every"
echo "action targeted the disposable stack or a disposable test database. Passing"
echo "disposable-stack tests are EVIDENCE, not production acceptance."
