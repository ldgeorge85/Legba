#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# scripts/run_targeted_gate.sh — the gate a deploy TIER needs, and only that.
#
#   scripts/run_targeted_gate.sh <tier> <from>..<to> [--dry-run]
#
# <tier> is t0|t1|t2|t3|full (scripts/deploy_tier.py). The test selection is
# the classifier's, never this script's: `deploy_tier.py gate-plan` names the
# test paths (the changed tests, every test whose import closure reaches a
# changed module or that names a changed script/descriptor, the t1 walker set
# when descriptors changed, the slim-image probes when registry code changed)
# and ALWAYS the guard set (module-size gate, no-undeclared-stubs, taxonomy,
# slim-import, alert-topic, option-catalog guards). `full` is the whole
# ordered suite, exactly the nightly's ordered phase.
#
# PHASES, cheapest first; any failure is a FAIL (no allowlist: a targeted gate
# is stricter than the nightly, never looser):
#   lint      ruff over the tree (run_tests_in_container.sh --lint), seconds
#   targeted  pytest over the selected paths in the test container, ordered
#             (-p no:randomly), on the isolated test stack, under the host
#             test lock the nightly holds (one suite on the stack at a time)
#   ui        when UI files changed: tsc, vitest and the two UI lint ratchets
#             (lint:tokens, lint:absence) in node:20-slim
#   walk      when UI files changed and the ui phase passed: the first-time-
#             user browser walk (legba-ui-v3/tools/walk/run.sh fixture --build)
#             against the freshly built bundle and the committed synthetic
#             fixtures, no network; its budgets (no main-thread task over 1 s,
#             no page overflow, a usable Consult box, no "0" before a count is
#             read, no page/console errors, the Flow budget refusing) fail the
#             gate. Report + screenshots land in <logs>/walk/
#   descriptors (t1) the walker test registers the WHOLE tree into the test
#             DB through the real registry — every descriptor validated
#
# THE GATE TESTS THE CHECKOUT. The container mounts this checkout's working
# tree, so it must sit at <to> with no tracked change; otherwise the verdict
# would name a SHA it never tested, and it is FAIL.
#
# RESULT LINE — the same key=value shape as the nightly's NIGHTLY_VERDICT, so
# the same parser reads it (rolling_deploy.sh greps it from stdout):
#   GATE_VERDICT <utc> sha=<to> verdict=PASS|FAIL tier=<t> tier_computed=<t>
#     range=<from>..<to> phases=lint,targeted[,ui] dirty=0|1 run=<id>
#     targeted=<p>p/<f>f/<e>e lint=ok|fail ui=ok|fail|- walk=ok|fail|-
#     tests=<n> wall=<s>s
# A targeted record never lists `ordered`, so the nightly roll gate can never
# take one for a full-suite record. Exit 0 PASS, 1 FAIL, 2 usage.
#
# Env: LEGBA_REPO_ROOT (default: this checkout), LEGBA_GATE_RUNNER (default
# scripts/run_tests_in_container.sh), LEGBA_GATE_WALK (default
# legba-ui-v3/tools/walk/run.sh), LEGBA_TEST_LOCK_FILE, LEGBA_GATE_TIMEOUT
# (s, default 7200), LEGBA_GATE_LOG_DIR (default /var/log/legba/gate).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${LEGBA_REPO_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
RUNNER="${LEGBA_GATE_RUNNER:-${REPO_ROOT}/scripts/run_tests_in_container.sh}"
WALK_RUNNER="${LEGBA_GATE_WALK:-${REPO_ROOT}/legba-ui-v3/tools/walk/run.sh}"
TEST_LOCK_FILE="${LEGBA_TEST_LOCK_FILE:-/tmp/legba-container-tests.lock}"
GATE_TIMEOUT="${LEGBA_GATE_TIMEOUT:-7200}"
LOG_ROOT="${LEGBA_GATE_LOG_DIR:-/var/log/legba/gate}"

usage() { sed -n '5,9p' "$0" | sed -E 's/^# ?//' >&2; exit 2; }
[ $# -ge 2 ] || usage
TIER="$1"; RANGE="$2"; shift 2
DRY_RUN=0
for a in "$@"; do
  case "$a" in --dry-run) DRY_RUN=1 ;; *) usage ;; esac
done
case "${TIER}" in t0|t1|t2|t3|full) : ;; *) echo "tier must be t0|t1|t2|t3|full, got '${TIER}'" >&2; exit 2 ;; esac
case "${RANGE}" in ?*..?*) : ;; *) echo "range must be <from>..<to>, got '${RANGE}'" >&2; exit 2 ;; esac

now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-${TIER}"
T0_S=$SECONDS

TESTS_FILE="$(mktemp)"
trap 'rm -f "${TESTS_FILE}"' EXIT
if ! PLAN="$(python3 "${SCRIPT_DIR}/deploy_tier.py" --repo "${REPO_ROOT}" gate-plan \
      --range "${RANGE}" --tier "${TIER}" --format shell --tests-out "${TESTS_FILE}")"; then
  echo "GATE_VERDICT $(now_iso) sha=- verdict=FAIL tier=${TIER} tier_computed=- range=${RANGE} phases=plan dirty=- run=${RUN_ID} targeted=- lint=- ui=- tests=0 wall=$((SECONDS - T0_S))s reason=classifier_failed"
  exit 1
fi
eval "${PLAN}"
mapfile -t TESTS < "${TESTS_FILE}"

echo ">> gate tier=${TIER} (computed ${GATE_TIER_COMPUTED}) range=${GATE_FROM:0:12}..${GATE_TO:0:12}"
if [ "${GATE_FULL}" = "1" ]; then
  echo ">> full ordered suite (est. ~$((GATE_EST_S / 60)) min)"
else
  echo ">> ${#TESTS[@]} test path(s), est. ~$(( (GATE_EST_S + 59) / 60 )) min:"
  printf '>>   %s\n' ${TESTS[@]+"${TESTS[@]}"}
fi
[ "${GATE_UI}" = "1" ] && echo ">> + UI: tsc, vitest, lint:tokens, lint:absence, then the browser walk (fixture mode)"

PYTEST_ARGS=()
if [ "${GATE_FULL}" = "1" ]; then PYTEST_ARGS=(tests/); else PYTEST_ARGS=(${TESTS[@]+"${TESTS[@]}"}); fi
PYTEST_ARGS+=(-p no:randomly -rfE)
# node_modules is the checkout's own (mounted): refreshed with `npm ci` when it is
# missing or OLDER than the lockfile (a stale node_modules once sat on maplibre-gl 4.7.1
# for a day after the lockfile moved to 5.24.0, and the gate tested the old library).
UI_CMD='if [ ! -f node_modules/.package-lock.json ] || [ package-lock.json -nt node_modules/.package-lock.json ]; then npm ci --no-audit --no-fund; fi && npm run lint && npm run lint:tokens && npm run lint:absence && npm run test'

if [ "${DRY_RUN}" -eq 1 ]; then
  echo ">> (dry-run) would run: LEGBA_REPO_ROOT=${REPO_ROOT} bash ${RUNNER} --lint"
  echo ">> (dry-run) would run: flock -w ${GATE_TIMEOUT} ${TEST_LOCK_FILE} bash ${RUNNER} ${PYTEST_ARGS[*]}"
  [ "${GATE_UI}" = "1" ] && echo ">> (dry-run) would run in node:20-slim: ${UI_CMD}"
  [ "${GATE_UI}" = "1" ] && echo ">> (dry-run) would run the walk: bash ${WALK_RUNNER} fixture --build"
  exit 0
fi

RUN_DIR="${LOG_ROOT}/${RUN_ID}"
mkdir -p "${RUN_DIR}" 2>/dev/null || RUN_DIR="$(mktemp -d)"

# The verdict names <to>; the container tests the working tree. They must agree.
HEAD_SHA="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)"
if git -C "${REPO_ROOT}" diff --quiet HEAD -- 2>/dev/null; then DIRTY=0; else DIRTY=1; fi
REASONS=()
[ "${HEAD_SHA}" = "${GATE_TO}" ] || REASONS+=("checkout_at_${HEAD_SHA:0:12}_not_${GATE_TO:0:12}")
[ "${DIRTY}" = "0" ] || REASONS+=("tracked_changes_in_checkout")

LINT=fail; TARGETED="-"; UI="-"; WALK="-"; FAILED=0
if [ "${#REASONS[@]}" -eq 0 ]; then
  if LEGBA_REPO_ROOT="${REPO_ROOT}" timeout 600 bash "${RUNNER}" --lint >"${RUN_DIR}/lint.log" 2>&1; then
    LINT=ok
  else
    FAILED=1; REASONS+=("lint")
  fi

  log="${RUN_DIR}/targeted.log"
  flock -w "${GATE_TIMEOUT}" "${TEST_LOCK_FILE}" \
    env LEGBA_REPO_ROOT="${REPO_ROOT}" timeout "${GATE_TIMEOUT}" bash "${RUNNER}" "${PYTEST_ARGS[@]}" >"${log}" 2>&1
  rc=$?
  tail_line="$(grep -E '(^|[[:space:]])[0-9]+ (passed|failed|error)' "${log}" | tail -1)"
  p="$(grep -oE '[0-9]+ passed' <<<"${tail_line}" | grep -oE '^[0-9]+' || true)"
  f="$(grep -oE '[0-9]+ failed' <<<"${tail_line}" | grep -oE '^[0-9]+' || true)"
  e="$(grep -oE '[0-9]+ errors?' <<<"${tail_line}" | grep -oE '^[0-9]+' || true)"
  TARGETED="${p:-0}p/${f:-0}f/${e:-0}e"
  nfail="$(grep -cE '^(FAILED|ERROR) tests/' "${log}" || true)"
  if [ "${rc}" -ne 0 ] || [ "${nfail:-0}" -gt 0 ] || [ -z "${tail_line}" ]; then
    FAILED=1
    if [ -z "${tail_line}" ]; then REASONS+=("targeted_did_not_complete_rc${rc}"); else REASONS+=("targeted"); fi
    grep -E '^(FAILED|ERROR) tests/' "${log}" | head -25 >&2 || true
  fi

  if [ "${GATE_UI}" = "1" ]; then
    if docker run --rm -v "${REPO_ROOT}/legba-ui-v3:/app" -w /app node:20-slim sh -c "${UI_CMD}" \
        >"${RUN_DIR}/ui.log" 2>&1; then
      UI=ok
      # the first-time-user walk, on a bundle built from this tree (README in tools/walk)
      if [ -f "${WALK_RUNNER}" ] && LEGBA_WALK_OUT="${RUN_DIR}/walk" timeout 900 bash "${WALK_RUNNER}" fixture --build \
          >"${RUN_DIR}/walk.log" 2>&1; then
        WALK=ok
      else
        WALK=fail; FAILED=1; REASONS+=("ui_walk")
        { grep -A40 -E '^WALK_VERDICT' "${RUN_DIR}/walk.log" || tail -20 "${RUN_DIR}/walk.log"; } 2>/dev/null | head -25 >&2 || true
      fi
    else
      UI=fail; FAILED=1; REASONS+=("ui")
    fi
  fi
else
  FAILED=1
fi

PHASES="lint,targeted"; [ "${GATE_FULL}" = "1" ] && PHASES="lint,full"
[ "${GATE_UI}" = "1" ] && PHASES="${PHASES},ui,walk"
VERDICT=PASS; [ "${FAILED}" -eq 0 ] && [ "${#REASONS[@]}" -eq 0 ] || VERDICT=FAIL
REASON_FIELD=""
[ "${#REASONS[@]}" -gt 0 ] && REASON_FIELD=" reason=$(IFS=,; echo "${REASONS[*]}")"
N_TESTS="${#TESTS[@]}"; [ "${GATE_FULL}" = "1" ] && N_TESTS="all"
echo "GATE_VERDICT $(now_iso) sha=${GATE_TO} verdict=${VERDICT} tier=${TIER} tier_computed=${GATE_TIER_COMPUTED} range=${GATE_FROM}..${GATE_TO} phases=${PHASES} dirty=${DIRTY} run=${RUN_ID} targeted=${TARGETED} lint=${LINT} ui=${UI} walk=${WALK} tests=${N_TESTS} wall=$((SECONDS - T0_S))s logs=${RUN_DIR}${REASON_FIELD}"
[ "${VERDICT}" = "PASS" ]
