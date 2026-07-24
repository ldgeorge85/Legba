#!/usr/bin/env bash
#
# run_tests_in_container.sh — run the Legba pytest suite inside a container.
#
# WHY THIS EXISTS:
#   The host's pytest is 9.x, which trips a SystemError in CPython's AST
#   compilation path during collection (assertion rewriting), so the suite
#   cannot be collected/run on the host at all. The fix is to pin pytest<9.
#   Rather than downgrade the host interpreter's tooling, we run the suite in
#   a throwaway image (legba/legba-test:latest) built from the runtime image
#   with 'pytest>=8,<9' + pytest-asyncio installed.
#
#   We pass `--network host` because the test conftests connect to backing
#   services on hardcoded 127.0.0.1 ports (Postgres, etc.) and resolve a
#   REPO_ROOT that must match the bind-mounted host path. Host networking
#   lets the in-container 127.0.0.1 reach the same services the host sees,
#   and the matching `-v`/`-w` mount keeps REPO_ROOT identical inside and out.
#
# SELF-STANDING:
#   The recipe provisions its own backing state. Substrate containers are
#   started by the data_pkg conftest if they aren't already up, and the
#   fixed `legba_pivot_test` database (used by the direct-connect acceptance
#   tests) is created + migrated on first run — both via the session
#   bootstrap in tests/data_pkg/conftest.py AND, as a belt-and-suspenders
#   shell-level guard below, before pytest is invoked. Migrations are
#   CREATE-only + idempotent, so re-running against an already-migrated DB
#   (the live dev rig) is a no-op.
#
# USAGE:
#   bash scripts/run_tests_in_container.sh                 # whole suite (tests/)
#   bash scripts/run_tests_in_container.sh tests/data_pkg  # subset / specific path(s)
#
# STRICT MODE (C-1 — now the canonical DEFAULT, item 2.8):
#   bash scripts/run_tests_in_container.sh            # strict (default)
#   LEGBA_TEST_STRICT=0 bash scripts/run_tests_in_container.sh  # opt OUT
#   Strict escalates INFRA-GATED skips (Postgres/NATS/pivot-DB/daprd/Qdrant/
#   Redis gates) to FAILURES via the central hook in
#   tests/conftest.py, so a degraded rig can't silently shrink coverage.
#   Opt-in external-creds skips (LLM tokens, live third-party feeds) are
#   unaffected. Set LEGBA_TEST_STRICT=0 to restore the old non-strict run.
#
set -euo pipefail

REPO_ROOT="/usr/local/deployments/active/legba"
BASE_IMAGE="legba/legba-runtime-dapr:latest"
TEST_IMAGE="legba/legba-test:latest"

# Build the test image if it is missing.
if ! docker image inspect "${TEST_IMAGE}" >/dev/null 2>&1; then
  echo ">> Building ${TEST_IMAGE} from ${BASE_IMAGE} ..." >&2
  docker build -t "${TEST_IMAGE}" - <<EOF
FROM ${BASE_IMAGE}
RUN pip install 'pytest>=8,<9' pytest-asyncio
EOF
fi

DOCKER_RUN=(docker run --rm
  --network host
  -v "${REPO_ROOT}:${REPO_ROOT}"
  -w "${REPO_ROOT}"
  -e PYTHONPATH="${REPO_ROOT}/src:/install/lib/python3.11/site-packages"
  # Pin the default DB to the pivot test DB so any test that builds a store via
  # PostgresConfig.from_env() lands in `legba_pivot_test`, NEVER the live `legba`
  # DB. (A from_env() fixture in test_discovery_p13 previously materialised
  # discovery descriptors into production, churning the live workingset.) The
  # pivot-DB bootstrap below uses a hardcoded admin DSN, so this is safe.
  -e LEGBA_DATA_PG_DB="${LEGBA_PIVOT_PG_DB:-legba_pivot_test}"
)

# Strict no-skip is the canonical default (C-1 cutover / item 2.8): the
# central hook in tests/conftest.py escalates INFRA-GATED skips to FAILURES,
# so a degraded rig can't silently shrink coverage. Default ON when unset,
# but a caller can still opt out with LEGBA_TEST_STRICT=0 (one-line override).
# Strict is now the conftest library default (strict unless the value is
# exactly "0"), so we ALWAYS forward the resolved value into the container —
# otherwise an explicit `LEGBA_TEST_STRICT=0` opt-out would be lost (unset
# inside the container → library default ON) and the no-infra escape hatch
# would silently do nothing.
LEGBA_TEST_STRICT="${LEGBA_TEST_STRICT:-1}"
DOCKER_RUN+=(-e "LEGBA_TEST_STRICT=${LEGBA_TEST_STRICT}")

DOCKER_RUN+=(--entrypoint python "${TEST_IMAGE}")

# Ensure the DISPOSABLE Postgres+AGE fixture for tests/journal_w1, w2, w4
# is up before pytest runs (TEST_DEBT_RECON.md Bucket C). This is a
# throwaway container, distinct from the persistent `postgres` compose
# service (5432) — it lives on 127.0.0.1:5544 and is never part of the
# live stack (grep confirms port 5544 appears nowhere in docker-compose*.yml).
# Start-if-absent + idempotent, mirroring the legba_pivot_test pattern below:
# a container already running is left alone; a stopped one is restarted;
# a missing one is created. Best-effort — if docker itself is unreachable
# here, the journal_w1/w2/w4 conftest's own skip (escalated to FAIL under
# strict mode) surfaces the gap loudly rather than this step silently
# swallowing it.
_AGE_TEST_CONTAINER="legba-test-age-w1"
if ! docker inspect "${_AGE_TEST_CONTAINER}" >/dev/null 2>&1; then
  echo ">> Starting disposable ${_AGE_TEST_CONTAINER} (Postgres+AGE, 127.0.0.1:5544) ..." >&2
  docker run -d --name "${_AGE_TEST_CONTAINER}" \
    -e POSTGRES_USER=legba -e POSTGRES_PASSWORD=legba -e POSTGRES_DB=legba_w1 \
    -p 127.0.0.1:5544:5432 apache/age:latest >/dev/null \
    || echo ">> WARN: could not start ${_AGE_TEST_CONTAINER}; tests/journal_w1/w2/w4 will skip/fail their infra gate" >&2
elif [ "$(docker inspect -f '{{.State.Running}}' "${_AGE_TEST_CONTAINER}" 2>/dev/null)" != "true" ]; then
  echo ">> Restarting existing (stopped) ${_AGE_TEST_CONTAINER} ..." >&2
  docker start "${_AGE_TEST_CONTAINER}" >/dev/null \
    || echo ">> WARN: could not restart ${_AGE_TEST_CONTAINER}; tests/journal_w1/w2/w4 will skip/fail their infra gate" >&2
fi
# Wait up to 30s for it to accept connections (fresh apache/age init is fast,
# ~5s observed) — best-effort, never blocks the run past the deadline.
_age_deadline=$((SECONDS + 30))
while [ $SECONDS -lt $_age_deadline ]; do
  if docker exec "${_AGE_TEST_CONTAINER}" pg_isready -U legba >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# Ensure the fixed legba_pivot_test DB exists + is migrated before pytest
# runs, so subsets that don't pull in the data_pkg conftest (e.g. a single
# tests/runtime path) are still self-standing. Idempotent + best-effort:
# a Postgres that is down here just lets the per-test skips kick in.
"${DOCKER_RUN[@]}" -c "
import asyncio, importlib.util
spec = importlib.util.spec_from_file_location('legba_test_conftest', '${REPO_ROOT}/tests/data_pkg/conftest.py')
cf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cf)
cf._ensure_pivot_test_db()
" || echo ">> WARN: legba_pivot_test provisioning skipped (Postgres unreachable?)" >&2

exec "${DOCKER_RUN[@]}" \
  -m pytest "${@:-tests/}" -q -p no:cacheprovider -o addopts=''
