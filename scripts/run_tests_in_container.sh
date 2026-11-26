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
#   The test conftests connect to backing services on hardcoded 127.0.0.1
#   ports (Postgres 5432, NATS 4222, ...) and resolve a REPO_ROOT that must
#   match the bind-mounted host path (the matching `-v`/`-w` mount keeps
#   REPO_ROOT identical inside and out).
#
# THE TEST STACK (2026-10-08) — ISOLATED BY DEFAULT:
#   The container joins the network namespace of deploy/compose.test.yml's
#   `pg` service (`--network container:legba-test-pg-1`). In that namespace
#   127.0.0.1:5432 / :4222 / :6379 / :6333 / :5544 / :3500 are the TEST
#   Postgres, NATS, Redis, Qdrant, journal-AGE fixture and daprd (with its
#   own placement + scheduler), and the production stack's
#   loopback ports are unreachable — so the suite can no longer write test
#   streams into the live JetStream or test databases into the live Postgres
#   server. The stack is brought up on demand (idempotent, ~7 s cold).
#     LEGBA_TEST_STACK=isolated   (default) the compose.test.yml namespace
#     LEGBA_TEST_FRESH=1          tear the test stack down first (clean slate)
#     LEGBA_TEST_STACK=host       the OLD `--network host` rig against the
#                                 production services — opt-in only, loud.
#                                 Since N-V every Postgres-backed test is
#                                 REFUSED there (tests/_pg_test_server_guard.py:
#                                 the live server carries no test marker).
#
# THE LOCK (N-V): the runner holds /tmp/legba-container-tests.lock (the
#   nightly's lock) for its whole run, waiting up to
#   LEGBA_TEST_LOCK_WAIT_SECONDS (7200) for it; re-entrant under the nightly.
#   Because NATS is disposable here, LEGBA_TEST_ISOLATED_NATS=1 is set for
#   the isolated stack (tests/_isolated_nats_gate.py): the five tests that
#   publish onto subjects the live runtime captures now run instead of skip.
#
# SELF-STANDING:
#   The recipe provisions its own backing state. The fixed
#   `legba_pivot_test` database (used by the direct-connect acceptance
#   tests) is created + migrated on first run — via the session bootstrap in
#   tests/data_pkg/conftest.py AND, as a belt-and-suspenders shell-level
#   guard below, before pytest is invoked. Migrations are CREATE-only +
#   idempotent, so re-running against an already-migrated DB is a no-op.
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

# The checkout to mount. Defaults to the main checkout, which is what the
# operator and the nightly cron run. A WORKTREE agent must point this at its
# own tree (`LEGBA_REPO_ROOT=$PWD bash scripts/run_tests_in_container.sh`) --
# a hardcoded path here is how worktree agents kept "verifying" their branch
# against main's code and reporting green for changes that were never loaded.
# Host path == container path is load-bearing (the conftests resolve a
# REPO_ROOT that must match the bind mount), so the mount and -w follow this
# one variable and nothing else needs to change.
REPO_ROOT="${LEGBA_REPO_ROOT:-/usr/local/deployments/active/legba}"
BASE_IMAGE="legba/legba-runtime-dapr:latest"
TEST_IMAGE="legba/legba-test:latest"

# Everything the recipe needs on TOP of the runtime base image:
#   pytest<9        -- 9.x trips a CPython AST SystemError during collection.
#   pytest-asyncio  -- asyncio_mode=auto in pyproject.
#   pytest-randomly -- the nightly's second, SHUFFLED pass (R7). Ordered runs
#                      must therefore pass `-p no:randomly` to stay ordered.
#                      PINNED to the host's release: the module
#                      order a seed produces is the plugin's own (5.x sorts
#                      modules by a hash of seed + module name), so a host
#                      replay of a nightly seed reproduces the nightly's order
#                      only when both sides run the same version. Bump it here
#                      and in pyproject's dev extra together.
#   ruff            -- the nightly lint ratchet, run through this same image so
#                      lint and tests can never disagree about the interpreter.
#   zstd (CLI)      -- N-K: backup_lib's archive re-hash falls back to the zstd
#                      CLI when the zstandard module is absent (the host restore
#                      drill's system python); strict mode needs it on the rig.
PYTEST_RANDOMLY_VERSION="5.0.0"
TEST_PIP_LINE="'pytest>=8,<9' pytest-asyncio pytest-randomly==${PYTEST_RANDOMLY_VERSION} ruff"

# Build the test image if it is missing OR predates a package added above.
# `docker image inspect` only answers "does the tag exist", so an image built
# before pytest-randomly/ruff joined the list would otherwise be reused
# forever and the shuffled pass would silently run in file order. The same
# holds for the pin: an image carrying another pytest-randomly is rebuilt.
_test_image_current() {
  docker image inspect "${TEST_IMAGE}" >/dev/null 2>&1 || return 1
  docker run --rm --entrypoint python "${TEST_IMAGE}" -c \
    "import pytest, pytest_asyncio, pytest_randomly, ruff, shutil, importlib.metadata as m; assert m.version('pytest-randomly') == '${PYTEST_RANDOMLY_VERSION}'; assert shutil.which('zstd')" \
    >/dev/null 2>&1
}
if ! _test_image_current; then
  echo ">> Building ${TEST_IMAGE} from ${BASE_IMAGE} ..." >&2
  docker build -t "${TEST_IMAGE}" - <<EOF
FROM ${BASE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends zstd && rm -rf /var/lib/apt/lists/*
RUN pip install ${TEST_PIP_LINE}
EOF
fi

# --- the test stack (see THE TEST STACK above) ------------------------------
LEGBA_TEST_STACK="${LEGBA_TEST_STACK:-isolated}"
TEST_COMPOSE=(docker compose -p legba-test -f "${REPO_ROOT}/deploy/compose.test.yml")
TEST_NETNS_CONTAINER="legba-test-pg-1"
case "${LEGBA_TEST_STACK}" in
  isolated)
    NETWORK_ARG="container:${TEST_NETNS_CONTAINER}"
    ;;
  host)
    NETWORK_ARG="host"
    echo ">> WARNING: LEGBA_TEST_STACK=host — the suite runs against the PRODUCTION" >&2
    echo ">>          Postgres server / NATS / Redis / Qdrant on 127.0.0.1." >&2
    ;;
  *)
    echo ">> LEGBA_TEST_STACK must be 'isolated' or 'host' (got '${LEGBA_TEST_STACK}')" >&2
    exit 2
    ;;
esac
# Lint is a pure static pass: no network at all, and no test stack to wait for.
[ "${1:-}" = "--lint" ] && NETWORK_ARG="none"

DOCKER_RUN=(docker run --rm
  --network "${NETWORK_ARG}"
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
# The isolated stack's NATS is disposable by construction, which is exactly the
# assertion tests/_isolated_nats_gate.py asks the caller to make. Never set on
# the host rig, where 4222 IS the live bus.
if [ "${LEGBA_TEST_STACK}" = "isolated" ]; then
  DOCKER_RUN+=(-e "LEGBA_TEST_ISOLATED_NATS=1")
  # N-V: the declaration tests/_pg_test_server_guard.py requires before it
  # trusts ANY Postgres server — and even then the server must carry the
  # stack's `-c legba.test_stack=on` marker (deploy/compose.test.yml). Never
  # set on the host rig: there 127.0.0.1:5432 is the LIVE server, and every
  # Postgres-backed test is refused.
  DOCKER_RUN+=(-e "LEGBA_TEST_PG_ISOLATED=1")
fi

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

# LINT MODE (R7) — `run_tests_in_container.sh --lint [paths...]` runs ruff in
# this same image instead of pytest. It lives here, rather than as its own
# script, so lint and tests can never drift onto different interpreters or a
# different checkout: one image, one mount, one REPO_ROOT. It returns BEFORE
# the substrate bootstrap below because ruff is a pure static pass — it needs
# no Postgres, no AGE container, and must stay runnable on a rig where those
# are down. `--no-cache` keeps the run from depending on (or writing) a
# .ruff_cache in a shared checkout.
if [ "${1:-}" = "--lint" ]; then
  shift
  exec "${DOCKER_RUN[@]}" -m ruff check --no-cache "${@:-.}"
fi

# --- the host-wide test lock (N-V) -------------------------------------------
# One suite run at a time against the shared test stack: the nightly holds
# /tmp/legba-container-tests.lock around each phase (scripts/host_nightly_suite.sh),
# and an ad-hoc run that ignored it collided with the nightly on 2026-11-13
# (shared legba_pivot_test, a LEGBA_TEST_FRESH teardown under a running suite).
# The runner now takes the SAME lock for the rest of its life (fd 8 survives
# the final exec, so the lock is held until pytest exits). Re-entrant: when an
# ANCESTOR process already holds it — the nightly's `flock ... bash runner` —
# or the caller says so with LEGBA_TEST_LOCK_HELD=1, it is not taken again
# (taking it again on a new descriptor would wait on ourselves). Lint mode
# returned above: it touches no shared state.
TEST_LOCK_FILE="${LEGBA_TEST_LOCK_FILE:-/tmp/legba-container-tests.lock}"
_test_lock_held_by_ancestor() {
  local holders pid h
  holders="$(lslocks --noheadings --raw -o PID,PATH 2>/dev/null \
    | awk -v p="${TEST_LOCK_FILE}" '$2 == p { print $1 }')"
  [ -n "${holders}" ] || return 1
  pid=$$
  while [ -n "${pid}" ] && [ "${pid}" -gt 1 ] 2>/dev/null; do
    for h in ${holders}; do
      [ "${h}" = "${pid}" ] && return 0
    done
    pid="$(ps -o ppid= -p "${pid}" 2>/dev/null | tr -d ' ')"
  done
  return 1
}
if [ "${LEGBA_TEST_LOCK_HELD:-0}" != "1" ] && ! _test_lock_held_by_ancestor; then
  exec 8>>"${TEST_LOCK_FILE}"
  if ! flock -n 8; then
    echo ">> waiting for the test lock ${TEST_LOCK_FILE} (another suite run — the nightly or a lane — holds it) ..." >&2
    if ! flock -w "${LEGBA_TEST_LOCK_WAIT_SECONDS:-7200}" 8; then
      echo ">> ERROR: the test lock ${TEST_LOCK_FILE} was not released within ${LEGBA_TEST_LOCK_WAIT_SECONDS:-7200}s" >&2
      exit 75
    fi
  fi
fi

if [ "${LEGBA_TEST_STACK}" = "isolated" ]; then
  # Bring the test stack up (idempotent: a running, unchanged stack is left
  # alone). It carries the journal_w1/w2/w4 AGE fixture on 5544 inside the
  # namespace, replacing the hand-started `legba-test-age-w1` host container.
  if [ "${LEGBA_TEST_FRESH:-0}" = "1" ]; then
    echo ">> LEGBA_TEST_FRESH=1: tearing the test stack down first ..." >&2
    "${TEST_COMPOSE[@]}" down --remove-orphans >/dev/null 2>&1 || true
  fi
  if ! "${TEST_COMPOSE[@]}" up -d --wait --quiet-pull >/dev/null 2>&1; then
    echo ">> ERROR: the isolated test stack (deploy/compose.test.yml) did not come up healthy." >&2
    "${TEST_COMPOSE[@]}" ps >&2 || true
    exit 3
  fi
  # The test daprd has no compose healthcheck (distroless image); wait for its
  # outbound probe (components loaded) on the published 13500, up to 60 s.
  # Best-effort: a daprd that never comes up surfaces as the actor tests'
  # strict-mode infra failures, which name it.
  _dapr_deadline=$((SECONDS + 60))
  until [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:13500/v1.0/healthz/outbound 2>/dev/null)" = "204" ]; do
    if [ $SECONDS -ge $_dapr_deadline ]; then
      echo ">> WARN: the test daprd's outbound health never answered 204 on 127.0.0.1:13500" >&2
      break
    fi
    sleep 1
  done
else
  # HOST RIG (legacy): the disposable Postgres+AGE fixture for
  # tests/journal_w1, w2, w4 on 127.0.0.1:5544 (TEST_DEBT_RECON.md Bucket C).
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
  _age_deadline=$((SECONDS + 30))
  while [ $SECONDS -lt $_age_deadline ]; do
    if docker exec "${_AGE_TEST_CONTAINER}" pg_isready -U legba >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
fi

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

# R7-fix (2026-08-30): `-q` hides pytest-randomly's seed header, which made
# every shuffled gate/nightly failure UNREPLAYABLE (the gate-green train had
# to re-bisect from scratch). Mint the seed here, log it loudly, pass it
# explicitly. Replay a run with LEGBA_TEST_SHUFFLE_SEED=<seed>. Ordered
# callers pass `-p no:randomly`, where the option would be unknown — skip it.
SEED_ARGS=()
case " $* " in
  *" no:randomly "*|*"-p no:randomly"*) : ;;  # ordered run — no seed to mint
  *)
    SHUFFLE_SEED="${LEGBA_TEST_SHUFFLE_SEED:-$(( (RANDOM << 15) | RANDOM ))}"
    echo ">> shuffle seed=${SHUFFLE_SEED} (replay: LEGBA_TEST_SHUFFLE_SEED=${SHUFFLE_SEED})"
    SEED_ARGS=(-p "randomly" "--randomly-seed=${SHUFFLE_SEED}")
    ;;
esac

exec "${DOCKER_RUN[@]}" \
  -m pytest "${@:-tests/}" -q -p no:cacheprovider -o addopts='' "${SEED_ARGS[@]}"
