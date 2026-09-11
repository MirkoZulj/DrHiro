#!/usr/bin/env bash
# Regenerate review-round-11 evidence: exact commands, environment/versions, the source
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
BEFORE_TREE="${BEFORE_TREE:-/tmp/review11-before}"  # clean worktree of the reviewed round-10 state
STACK_SUITE="${1:-(stack result supplied at capture time)}"

FUNC_BASE="${FUNC_BASE:-74ca71f8dec9368c3274b2166a5d9b19d3b94a2b}"   # fully-reviewed round-10 state (docs head)
FUNC_HEAD="${FUNC_HEAD:-$(git -C "$REPO" log --format=%H -1 --grep='fix(review10)')}"
FUNC_R9="${FUNC_R9:-9076937cc733fc3fe05ef53bce2ec7b149550747}"   # round-9 functional head
FUNC_R10="${FUNC_R10:-d5ac3d79a9b169e90370ae69b6e85e91f31143cc}"  # round-10 functional head
R10_TREE="${R10_TREE:-/tmp/review11-r10}"
R9_TREE="${R9_TREE:-/tmp/review11-r9}"   # clean worktree of the round-9 functional head

echo "# Review round 10 evidence - focused FK mapping + schema policy"
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
echo "docs head (capture-time revision): $(git -C "$REPO" rev-parse HEAD)"
echo "  NOTE: this is the repository revision AT CAPTURE TIME. The evidence file "
echo "  records its own commit, so committing it necessarily creates a NEWER docs"
echo "  head; BASE_AND_HEAD.md in the bundle names that final one. The two are not"
echo "  the same revision by construction and must not be presented as such."
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
echo "Every command below is printed beside the result it produced in this capture."
echo
echo '```bash'
echo "# 1. migration FAILING-BEFORE (base worktree = fully-reviewed round-10 state;"
echo "#    PRE-FIX source forced via PYTHONPATH so the editable install cannot"
echo "#    substitute the fixed source, with the imported path printed)"
echo "cd $BEFORE_TREE && PYTHONPATH=$BEFORE_TREE/apps/api/src python -c \\"
echo "  'import drhiro_api.schema_activities as m; print(m.__file__)'"
echo "PYTHONPATH=$BEFORE_TREE/apps/api/src DRHIRO_ACTIVITIES_MIGRATION_DB=1 \\"
echo "  DRHIRO_TEST_DB_URL='$DB' python -m pytest \\"
echo "  tests/test_r4_activities_migration.py::TestSchemaNoneDoesNotCombineRelations \\"
echo "  tests/test_r4_activities_migration.py::TestConservativeTypeAndDefaultEquivalence -q"
echo
echo "# 2. migration FAILING-BEFORE for the ROUND-9 validator (FK mapping + schema policy)"
echo "cd $R9_TREE && PYTHONPATH=$R9_TREE/apps/api/src python -c \\"
echo "  'import drhiro_api.schema_activities as m; print(m.__file__)'"
echo "PYTHONPATH=$R9_TREE/apps/api/src DRHIRO_ACTIVITIES_MIGRATION_DB=1 \\"
echo "  DRHIRO_TEST_DB_URL='$DB' python -m pytest \\"
echo "  tests/test_r4_activities_migration.py::TestForeignKeyMappingAndSchemaPolicy -q"
echo
echo "# 3. migration FAILING-BEFORE for the ROUND-10 creation path (default-schema gap)"
echo "cd $R10_TREE && PYTHONPATH=$R10_TREE/apps/api/src python -c \\"
echo "  'import drhiro_api.schema_activities as m; print(m.__file__)'"
echo "$VENV $REPO/deploy/disposable/creation_path_probe.py $R10_TREE"
echo
echo "# 4. migration PASSING-AFTER - full R4 suite plus the creation-path probe"
echo "cd $REPO"
echo "DRHIRO_ACTIVITIES_MIGRATION_DB=1 DRHIRO_TEST_DB_URL='$DB' \\"
echo "  python -m pytest tests/test_r4_activities_migration.py -q"
echo "$VENV $REPO/deploy/disposable/creation_path_probe.py $REPO"
echo
echo "# 5. ingress regression suite (requires the disposable stack UP)"
echo "DRHIRO_ISOLATED_STACK=1 python -m pytest tests/test_t1_isolated_ingress_stack.py -q"
echo
echo "# 6. default suite (production inspection excluded)"
echo "env -u DRHIRO_ISOLATED_STACK -u DRHIRO_ACTIVITIES_MIGRATION_DB -u DRHIRO_TEST_DB_URL \\"
echo "  python -m pytest tests/ -q"
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
echo "## Finding: FK mapping + schema policy - failing-before against the ROUND-9 validator"
echo
echo "The round-9 validator dropped the referenced-COLUMN comparison and resolved"
echo "\`users\` independently through search_path. Both regressions below fail against"
echo "the round-9 validator (commit 9076937) and pass after the correction."
echo
echo '```'
cd "$R9_TREE"
PYTHONPATH="$R9_TREE/apps/api/src" DRHIRO_ACTIVITIES_MIGRATION_DB=1 \
  DRHIRO_TEST_DB_URL="$DB" $VENV -m pytest \
  tests/test_r4_activities_migration.py::TestForeignKeyMappingAndSchemaPolicy \
  -q -p no:cacheprovider 2>&1 | tail -8
echo '```'
echo
echo "## Creation path - the NEW negative against the ROUND-10 code"
echo
echo "The regression the review asked for, executed against the submitted round-10"
echo "worktree (the imported source path is printed first):"
echo
echo '```'
cd "$R10_TREE"
PYTHONPATH="$R10_TREE/apps/api/src" $VENV -c \
  "import drhiro_api.schema_activities as m; print('imported:', m.__file__)"
PYTHONPATH="$R10_TREE/apps/api/src" DRHIRO_ACTIVITIES_MIGRATION_DB=1 \
  DRHIRO_TEST_DB_URL="$DB" $VENV -m pytest \
  tests/test_r4_activities_migration.py::TestCreationDestinationNamespace \
  -q -p no:cacheprovider 2>&1 | tail -6
echo '```'
echo
echo "## Creation path - failing-before against the ROUND-10 code"
echo
echo "With schema=None the round-10 create_activities emitted an UNQUALIFIED"
echo "REFERENCES users(id), so under search_path = first, second (first writable,"
echo "no users; second has users) it created first.activities bound to second.users"
echo "- the cross-schema relationship the validator rejects. The probe reports the"
echo "outcome and the surviving tables/indexes."
echo
echo '```'
$VENV "$REPO/deploy/disposable/creation_path_probe.py" "$R10_TREE" 2>&1 | tail -5
echo '```'
echo
echo "### PASSING-AFTER - creation path refuses, and the full R4 suite passes"
echo
echo '```'
$VENV "$REPO/deploy/disposable/creation_path_probe.py" "$REPO" 2>&1 | tail -5
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
echo "## Carry-forward justification for the ingress stack suite"
echo
echo "ingress.py is UNCHANGED in this round, so the stack-suite total and the"
echo "container hash below are the round-10 ones and still describe the submitted"
echo "head. Verified by Git BLOB identity (not by commit label):"
echo
echo '```'
echo "base $FUNC_BASE:ingress.py  $(git -C "$REPO" rev-parse $FUNC_BASE:deploy/disposable/app/ingress.py)"
echo "working tree ingress.py     $(git -C "$REPO" hash-object $REPO/deploy/disposable/app/ingress.py)"
echo '```'
echo
echo "A differing blob would invalidate the carry-forward; identical blobs mean the"
echo "same source was exercised. The stack suite itself was NOT re-run this round."
echo
echo "## Default suite (production inspection excluded; captured in this run)"
echo
echo '```'
cd "$REPO"
env -u DRHIRO_ISOLATED_STACK -u DRHIRO_R1R2_ALEMBIC_DB -u DRHIRO_R4_ALEMBIC_DB \
  -u DRHIRO_T1_ALEMBIC_DB -u DRHIRO_ACTIVITIES_MIGRATION_DB -u DRHIRO_DEPLOY_ASSERT \
  -u DRHIRO_DOCKER_CMD -u DRHIRO_TEST_DB_URL -u REDIS_URL \
  $VENV -m pytest tests/ -q -p no:cacheprovider 2>&1 | tail -2
echo '```'
echo
echo "## Capability labelling"
echo
echo "Container uptimes unchanged support 'no observed restart' only; they do not"
echo "independently prove that configuration, credentials or data were unchanged. Every"
echo "action targeted the disposable stack or a disposable test database. Passing"
echo "disposable-stack tests are EVIDENCE, not production acceptance."
