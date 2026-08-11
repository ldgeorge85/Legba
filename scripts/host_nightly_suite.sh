#!/usr/bin/env bash
# host_nightly_suite.sh — CI-lite: the nightly full-suite run (R7).
#
# THE GAP THIS CLOSES: this repo has no CI. The suite is green whenever
# somebody last remembered to run it, and "somebody last remembered" has been
# as much as a week. A regression therefore surfaces when the next person runs
# the suite for an unrelated reason — usually mid-wave, usually attributed to
# whatever they just changed. This runs the whole thing every night and pages
# the operator (ntfy web UI) the FIRST morning something breaks, with the
# failing node ids already in the message.
#
# THREE PHASES, cheapest first:
#
#   1. LINT      — ruff, per the [tool.ruff] ratchet in pyproject.toml. Seconds.
#                  Runs first so a syntax-level mistake pages in one minute
#                  instead of two hours.
#   2. ORDERED   — the full suite in file order (`-p no:randomly`). This is the
#                  run whose result is comparable night to night.
#   3. SHUFFLED  — the full suite again with pytest-randomly, under a SEED that
#                  is logged and echoed in the page. File order is an accident,
#                  not a contract; a suite that only passes in one order is
#                  hiding shared state between tests. The seed is what makes a
#                  shuffled failure reproducible instead of folklore.
#
# WHY AN ALLOWLIST: a nightly that always fails is a nightly nobody reads. The
# KNOWN_FAILURES list below names the failures this rig produces for reasons
# that are not the code's fault, each with a reason. Anything NOT on that list
# pages. Anything ON it that stops failing is reported in the summary as a
# stale entry to retire — the list is meant to shrink.
#
# ALERT-ONLY. It never restarts, redeploys, or mutates the stack; the tests run
# in a throwaway container against `legba_pivot_test`, never the live `legba`
# DB (see scripts/run_tests_in_container.sh, which owns that pinning).
#
# INSTALL: deploy/cron.d/legba-nightly-suite (operator copies it into
# /etc/cron.d/). Docs: docs/RUNBOOK.md §24.4.
#
# Run it by hand exactly as cron does:
#   /usr/local/deployments/active/legba/scripts/host_nightly_suite.sh
# Skip the slow half while testing the plumbing:
#   PHASES=lint /usr/local/deployments/active/legba/scripts/host_nightly_suite.sh
#
set -uo pipefail

# --- configuration ----------------------------------------------------------
# EVERY path here is absolute. cron runs from $HOME, and a relative path in a
# cron-invoked script is how scripts/loop_healthcheck.sh managed 7,834 runs
# over 54 days without executing one line of its body.
REPO_ROOT="${LEGBA_REPO_ROOT:-/usr/local/deployments/active/legba}"
RUNNER="${REPO_ROOT}/scripts/run_tests_in_container.sh"
LOG_ROOT="${LEGBA_NIGHTLY_LOG_DIR:-/var/log/legba/nightly}"
# Rotation is this script's own job. P6 §6 item 11 is "no logrotate for any
# /var/log/legba*.log", and the log collector answers that the same way.
KEEP_RUNS="${LEGBA_NIGHTLY_KEEP_RUNS:-14}"
NTFY_URL="${NTFY_URL:-http://127.0.0.1:8093/legba-alerts}"
LOCK_FILE="${LEGBA_NIGHTLY_LOCK_FILE:-/var/lock/legba-nightly-suite.lock}"
# The cron-level log (see deploy/cron.d/legba-nightly-suite). It only ever
# holds a duplicate of the per-run summary plus anything that goes wrong
# BEFORE this script can create a run dir — which is exactly why it is worth
# keeping — but nothing rotates /var/log/legba*.log, so cap it here. cron
# holds it O_APPEND, so copy-truncate is the valid shape, same as the log
# collector's self-log.
SELF_LOG="${LEGBA_NIGHTLY_SELF_LOG:-/var/log/legba_nightly_suite.log}"
SELF_LOG_MAX_BYTES="${LEGBA_NIGHTLY_SELF_LOG_MAX:-5242880}"   # 5 MiB
# Per-phase wall clock. A full pass measured ~16 min on the dev rig
# (2026-08-04, 10,337 tests); 2h is a wide margin on a loaded host and still
# guarantees a genuinely hung phase cannot eat the next night's run.
PHASE_TIMEOUT="${LEGBA_NIGHTLY_PHASE_TIMEOUT:-7200}"
# Which phases to run, space- or comma-separated. Full set by default.
PHASES="${PHASES:-lint ordered shuffled}"
# Shuffle seed. Generated per run and printed everywhere it could be needed;
# pin it to replay a specific night.
SEED="${LEGBA_NIGHTLY_SEED:-$(( (RANDOM << 15) | RANDOM ))}"

# Maintenance switches: the shared one silences the whole watchdog family
# during a deploy window, the dedicated one silences only this.
[ -f /etc/legba-watchdog.disabled ] && exit 0
[ -f /etc/legba-nightly-suite.disabled ] && exit 0

# ---------------------------------------------------------------------------
# KNOWN FAILURES — the allowlist. Extended regexes matched against the pytest
# node id (the `FAILED <nodeid>` / `ERROR <nodeid>` token).
#
# THE BAR FOR ADDING ONE: the failure must be a property of the RIG, not of the
# code — something a clean checkout on a clean host would not reproduce. A test
# that is merely flaky, order-dependent or wrong does NOT belong here; it
# belongs in a fix. Four candidates were fixed on 2026-08-04 rather than
# listed: the production-gauge/claim_watch truncation trap, the dspy
# instruction-text assertion, the dspy import-order assertion, and the K-4
# acceptance gate whose subprocess env cost it `pydantic`.
#
# Each entry carries its reason. Re-verify them when the rig changes.
# ---------------------------------------------------------------------------
# Entries are NODE IDs, not file globs, on purpose: every one of these files
# also contains tests that pass, and allowlisting a whole file would silence a
# real regression sitting next to a known one.
#
# Measured 2026-08-04 by running the full ordered suite twice: once at this
# branch's base and once in the MAIN CHECKOUT, which is the environment cron
# actually runs in. Entries marked (base-only) failed at the base but PASS in
# the main checkout — later commits there fixed them. They are kept rather
# than deleted so a merge that lands before those fixes does not page on night
# one; the summary reports any entry that matched nothing as stale, so a
# single clean night is enough evidence to delete them.
#
# Two further classes fail ONLY in a git worktree and so are deliberately
# absent — they cannot fire where cron runs this:
#   * tests/data_pkg/test_dockerfiles_build_clean.py (6) hardcodes
#     REPO_ROOT = /usr/local/deployments/active/legba, which is not mounted in
#     a worktree container.
#   * tests/data_pkg/test_seed.py + test_substrate_export_import.py (6) need
#     seeds/ — curated data that is gitignored and lives only in the main
#     checkout.
# If a worktree dry-run shows those twelve, that is the worktree talking.
KNOWN_FAILURES=(
  # --- 1. dspy is a WORKER-ONLY dependency ---------------------------------
  # The litellm/dspy production ban keeps dspy out of the runtime image, and
  # the test image is built FROM the runtime image — so every test that
  # resolves an optimizer's parent prompt module (the `legba.prompts.*`
  # packages import dspy at module level) dies with ModuleNotFoundError, or a
  # PromptModuleImportError / DescriptorValidationError wrapping one. These
  # are real tests with real coverage; they belong to the GEPA worker image,
  # which is the only image that carries dspy.
  #
  # THIS IS THE BIGGEST BLOCK AND IT IS THE ONE TO RETIRE. Installing dspy
  # into legba/legba-test (a TEST image, not a production one) would delete
  # all eighteen entries at a stroke AND make
  # test_v3_optimizer_diff::test_diff_route_does_not_import_dspy meaningful
  # instead of vacuous — it can only catch a real violation where dspy is
  # importable. That is an operator call, because it puts litellm inside an
  # image derived from the runtime one.
  # RETIRED 2026-08-09 (the stale-entry sweep): every entry marked (base-only)
  # in this section matched nothing on 2026-08-07, -08 AND -09 — the main
  # checkout's later commits fixed them, exactly as the note above predicted —
  # so all eight went, along with the whole "live registry is fail-closed"
  # section below (its single entry was also base-only and also clean three
  # nights running).
  'tests/data_pkg/test_analyst_optimizer\.py::test_in_process_client_runs_loop_when_enough_traces'
  'tests/data_pkg/test_analyst_optimizer\.py::test_naive_fallback_respects_max_generations_bound'
  'tests/data_pkg/test_descriptor_reference_resolution_k3\.py::test_optimizer_still_loads_a_real_parent_prompt'
  'tests/data_pkg/test_optimizer_prompt_module_convention\.py::test_convention_is_the_kind_not_the_analyst_id'
  'tests/data_pkg/test_optimizer_prompt_module_convention\.py::test_every_optimizer_resolves_a_prompt_module_that_imports'
  'tests/runtime/test_dapr_workflow_optimizer\.py::test_compile_activity_returns_candidate_dict'
  'tests/runtime/test_optimizer_gepa_loop\.py::test_activity_failure_propagates_through_loop'
  'tests/runtime/test_optimizer_gepa_loop\.py::test_in_process_is_deterministic_for_same_input'
  'tests/runtime/test_optimizer_gepa_loop\.py::test_in_process_result_carries_usage_dict'
  'tests/runtime/test_optimizer_gepa_loop\.py::test_in_process_returns_bounded_result'
  'tests/runtime/test_optimizer_payload_by_reference\.py::test_empty_training_set_still_noops'

  # --- 2. LEGBA_TEST_STRICT infra escalations ------------------------------
  # Strict mode escalates INFRA-GATED skips to FAILURES on purpose, so a
  # degraded rig cannot silently shrink coverage. On a host where the LIVE
  # stack already owns the ports and NATS subjects these tests want, that
  # escalation is the rig talking, not the code — each one logs its own
  # "[LEGBA_TEST_STRICT] infra-gated skip escalated to FAILURE" line with the
  # reason. Documented in RUNBOOK §24.1 and PROJECT_STATE ("the 9 errors are
  # port-6090/NATS contention"). They would pass on a host with the stack
  # down, which is the definition of environmental.
  'tests/data_pkg/agency/test_agency_hard_gate\.py::test_process_media_pack_enqueues_real_job'
  'tests/data_pkg/test_output_webhook\.py::test_emit_4xx_does_not_dlq'
  'tests/data_pkg/test_output_webhook\.py::test_emit_retry_exhausted_routes_to_dlq'
  'tests/runtime/test_critic_descriptor_e2e\.py::test_critic_actor_activates_and_writes_critique_through_daprd'
  'tests/runtime/test_critic_descriptor_e2e\.py::test_critic_heterogeneity_guard_rejects_self_correlated'
  'tests/runtime/test_critic_descriptor_e2e\.py::test_critic_missing_rubric_rejection'
  'tests/runtime/test_webhook_alert_e2e\.py::test_webhook_4xx_no_retry_no_dlq'
  'tests/runtime/test_webhook_alert_e2e\.py::test_webhook_5xx_retries_then_dlqs'
)

# ---------------------------------------------------------------------------
# KNOWN SHARED-STATE FAILURES — a WORK QUEUE, not an excuse.
#
# These are NOT environmental. Every one is a real order dependency: a test
# that reads state some other test wrote, in a suite that shares one Postgres.
# They are frozen here for one reason only — so that a NEW one is visible above
# them. Without this list the shuffled pass reports fourteen failures every
# night, nobody reads it, and the fifteenth arrives unnoticed.
#
# THE RULE: this list may only ever SHRINK. An addition means someone
# introduced shared state, and the correct response is a fix, not an entry.
#
# Measured 2026-08-04 at this branch's tip: full suite ordered (baseline) vs
# full suite under `--randomly-seed=20260804`. Reproduce any of them with
#   bash scripts/run_tests_in_container.sh tests/ --randomly-seed=20260804
#
# The shape is always the same and is worth naming, because it is what the
# gauge fixture fix in this same branch taught: an assertion written as a
# GLOBAL statement ("nothing else was wired", "exactly 3 entities damped")
# over a substrate the whole suite shares. Written as a statement about the
# test's OWN rows it would be order-proof. That is the fix each of these
# wants; none of them is a product bug, and none of them should be silenced
# any other way.
#
# RETIRED 2026-08-06 (the nightly-debt pass), fixed rather than carried:
#   * test_alert_trigger_scan::test_band_crossing_seeds_then_fires_once — the
#     band watermark count is now scoped to the test's own desk key.
#   * test_entity_resolve_keeper_e1::test_governance_drops_keeper_self_loop_n4 —
#     the global `promoted == 0` became "MY pair is rejected and wrote no nexus".
# Both were fixed alongside the four UNEXPECTED failures in the same files, and
# leaving an entry listed after its cause is gone is how a list stops shrinking.
KNOWN_SHARED_STATE=(
  # RETIRED 2026-08-09 (the stale-entry sweep): nine entries — the
  # test_claim_watch global-damping one, all five test_collection_requirements
  # ones, the test_discovery_p13 auto-wire one, and both jobs-plane ones —
  # matched nothing three nights running (2026-08-07/-08/-09, three different
  # shuffle seeds). The list only shrinks: a clean streak is the evidence, and
  # a recurrence is a NEW page with a NEW fix, not a re-listing.
  #
  # RETIRED 2026-08-10 (fixed, not aged out — the #16/#17 hygiene pass):
  #   * test_claim_watch::test_stream_hub_entities_are_floored_… — the same
  #     mechanism as that morning's global-damping page: the global-df
  #     filler was stamped into the shared PAST, so recent sibling leftovers
  #     (alert_trigger_scan's spike stream) displaced it out of the df window
  #     and the lever went inert. The filler now rides the file's own
  #     future-stamped stream; both df tests replay green under seeds
  #     174413029 / 912147235 / 277595060.
  #   * test_seed::test_world_baseline_end_to_end_and_idempotent — mechanism
  #     SETTLED by the 2026-08-10 shuffled failure itself: seed_import (the
  #     export/import re-home) mints its own batch row, the corroboration
  #     upsert never restamps seed_batch_id, so whichever sibling seeded
  #     first owned the Modi row's stamp (and noisy-OR-lifted 0.95→0.99).
  #     The test now retires the world-baseline family's rows AND ledger
  #     rows before its first run, so it is a genuine first seeding in any
  #     order.
  'tests/data_pkg/test_conversion_webhooks\.py::test_register_webhook_persists_row_and_returns_active'
  'tests/data_pkg/test_postgres_pool_search_path\.py::test_actor_state_table_reachable_from_every_acquire'
)

# ---------------------------------------------------------------------------
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${LOG_ROOT}/${RUN_ID}"
SUMMARY="${RUN_DIR}/summary.txt"

now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }
say() { echo "$(now_iso) [nightly] $*" | tee -a "$SUMMARY"; }

# page <priority> <title> <tags> <body> — the alert idiom shared with
# host_llm_heartbeat.sh / host_stall_watchdog.sh. ntfy is consumed through the
# WEB UI (alerts.legba.civislux.us), never a phone app.
page() {
  curl -s -m 10 -H "X-Title: $2" -H "X-Priority: $1" -H "X-Tags: $3" \
    -d "$4" "$NTFY_URL" >/dev/null 2>&1 \
    || say "WARN ntfy send failed ($2)"
}

# is_known <nodeid> — true when the node id matches a known entry, in EITHER
# list. Both are checked in both phases: the shared-state failures are not
# shuffle-exclusive (the discovery one fires in file order too), and splitting
# the check by phase would only mean a known failure pages from one pass and
# not the other.
is_known() {
  local nodeid="$1" pat
  for pat in "${KNOWN_FAILURES[@]}" "${KNOWN_SHARED_STATE[@]}"; do
    [[ "$nodeid" =~ $pat ]] && return 0
  done
  return 1
}

# failures_from <logfile> — the FAILED/ERROR node ids pytest reported. `-rfE`
# is passed to every pass so this summary section is always present; without it
# a quiet run and a broken run parse identically.
#
# The `tests/` anchor is load-bearing. Captured application logging routinely
# emits its own lines beginning "ERROR <logger>:<file>:<line>", and a looser
# pattern turns every logged error in a PASSING run into a phantom failure
# (measured: 16 of them in one clean pass). Every real node id starts with
# `tests/` because that is `testpaths` in pyproject.toml.
failures_from() {
  grep -E '^(FAILED|ERROR) tests/' "$1" 2>/dev/null | awk '{print $2}' | sort -u
}

# ---------------------------------------------------------------------------
mkdir -p "$RUN_DIR" || { echo "cannot create ${RUN_DIR}" >&2; exit 1; }

# Cap the cron-level self-log before this run appends to it.
if [ -f "$SELF_LOG" ]; then
  _self_bytes="$(stat -c %s "$SELF_LOG" 2>/dev/null || echo 0)"
  if [ "$_self_bytes" -gt "$SELF_LOG_MAX_BYTES" ]; then
    tail -c "$((SELF_LOG_MAX_BYTES / 2))" "$SELF_LOG" > "${SELF_LOG}.tmp" 2>/dev/null \
      && cat "${SELF_LOG}.tmp" > "$SELF_LOG" \
      && rm -f "${SELF_LOG}.tmp"
  fi
fi

# One run at a time. A phase that overruns must not have the next night's run
# land on top of it — they share `legba_pivot_test` and would corrupt each
# other's fixtures.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  say "SKIP another nightly run holds ${LOCK_FILE}"
  exit 0
fi

HEAD_SHA="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
HEAD_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
say "start run=${RUN_ID} repo=${REPO_ROOT} branch=${HEAD_BRANCH} head=${HEAD_SHA}"
say "shuffle seed=${SEED}"

FAILED_PHASES=()
UNEXPECTED=()
SEEN_KNOWN=()

wants_phase() {
  case " ${PHASES//,/ } " in *" $1 "*) return 0;; *) return 1;; esac
}

# run_pytest_phase <name> <extra pytest args...>
run_pytest_phase() {
  local name="$1"; shift
  local log="${RUN_DIR}/${name}.log"
  local rc=0
  say "phase=${name} starting"
  # LEGBA_REPO_ROOT is forwarded so the nightly can be pointed at a worktree
  # for a dry run; in cron it is the main checkout, which is also the only
  # checkout with the gitignored seed data (seeds/) present. A worktree run
  # will show seed-dependent failures that the real nightly never sees.
  LEGBA_REPO_ROOT="$REPO_ROOT" timeout "$PHASE_TIMEOUT" \
    bash "$RUNNER" tests/ -rfE "$@" >"$log" 2>&1
  rc=$?
  if [ "$rc" -eq 124 ]; then
    say "phase=${name} TIMEOUT after ${PHASE_TIMEOUT}s"
    FAILED_PHASES+=("${name}:timeout")
    return 1
  fi

  local nodeid
  local -a phase_unexpected=()
  # PARSED counts EVERY node id pytest reported for this phase, known or not.
  # It is what separates "the suite ran and only known things failed" from
  # "pytest never ran" — see the rc!=0 branch below.
  local parsed=0
  while read -r nodeid; do
    [ -z "$nodeid" ] && continue
    parsed=$((parsed + 1))
    if is_known "$nodeid"; then
      SEEN_KNOWN+=("$nodeid")
    else
      phase_unexpected+=("${name}: ${nodeid}")
    fi
  done < <(failures_from "$log")

  local tail_line
  # pytest -q ends with a bare counts line: "31 failed, 10337 passed, ... in 936.03s".
  tail_line="$(grep -E '(^|[[:space:]])[0-9]+ (passed|failed|error)' "$log" | tail -1)"
  say "phase=${name} rc=${rc} :: ${tail_line}"

  if [ "${#phase_unexpected[@]}" -gt 0 ]; then
    UNEXPECTED+=("${phase_unexpected[@]}")
    FAILED_PHASES+=("${name}")
    return 1
  fi
  # A non-zero rc with NOTHING parsed means pytest never got as far as running
  # tests — a collection error, a missing image, an unreachable docker. That is
  # a failure of the nightly itself and must page, not pass quietly.
  #
  # `parsed -eq 0` is the whole test, and leaving it out is what the 2026-08-04
  # summary caught live: the ordered phase reported nineteen FAILED node ids,
  # every one of them on the allowlist, so `phase_unexpected` was empty and rc
  # was still 1 — pytest exits 1 whenever anything failed, allowlisted or not.
  # The bare `rc -ne 0` fell straight through to here and paged
  # "run did not complete (rc=1, no test failures parsed)" about a run that had
  # completed and whose failures had all been parsed and recognised. That
  # mislabels the ONE signal this phase exists to give — it sent the operator
  # hunting a broken rig instead of reading the allowlist — and it also masks
  # the real infra case, because once "infra" is what a normal known-failure
  # night looks like, nobody believes it on the night it is true.
  if [ "$rc" -ne 0 ] && [ "$parsed" -eq 0 ]; then
    UNEXPECTED+=("${name}: run did not complete (rc=${rc}, no test failures parsed)")
    FAILED_PHASES+=("${name}:infra")
    return 1
  fi
  # rc != 0 with everything parsed and everything known is the designed steady
  # state of this rig: the allowlist absorbed it, the phase did not fail.
  if [ "$rc" -ne 0 ]; then
    say "phase=${name} rc=${rc}, all ${parsed} reported failure(s) known — not a page"
  fi
  return 0
}

# --- phase 1: lint ----------------------------------------------------------
if wants_phase lint; then
  say "phase=lint starting"
  if LEGBA_REPO_ROOT="$REPO_ROOT" timeout 600 bash "$RUNNER" --lint \
      >"${RUN_DIR}/lint.log" 2>&1; then
    say "phase=lint OK"
  else
    say "phase=lint FAILED ($(grep -c . "${RUN_DIR}/lint.log") lines)"
    UNEXPECTED+=("lint: $(tail -1 "${RUN_DIR}/lint.log")")
    FAILED_PHASES+=("lint")
  fi
fi

# --- phase 2: ordered -------------------------------------------------------
wants_phase ordered && run_pytest_phase ordered -p no:randomly

# --- phase 3: shuffled ------------------------------------------------------
wants_phase shuffled && run_pytest_phase shuffled "--randomly-seed=${SEED}"

# --- stale allowlist entries ------------------------------------------------
# An entry that matched nothing all night is either fixed or misspelled. Both
# are worth knowing; neither is worth a page.
#
# Only meaningful when a pytest phase actually ran — a `PHASES=lint` invocation
# would otherwise report the entire list as stale every time.
if wants_phase ordered || wants_phase shuffled; then
  for pat in "${KNOWN_FAILURES[@]}" "${KNOWN_SHARED_STATE[@]}"; do
    matched=0
    for nodeid in ${SEEN_KNOWN[@]+"${SEEN_KNOWN[@]}"}; do
      [[ "$nodeid" =~ $pat ]] && { matched=1; break; }
    done
    [ "$matched" -eq 0 ] && say "allowlist entry matched nothing (retire it?): ${pat}"
  done
fi

# --- verdict ----------------------------------------------------------------
REPLAY="LEGBA_REPO_ROOT=${REPO_ROOT} bash ${RUNNER} tests/ --randomly-seed=${SEED}"
say "known-failure hits: ${#SEEN_KNOWN[@]}"

if [ "${#UNEXPECTED[@]}" -gt 0 ]; then
  say "VERDICT FAIL phases=[${FAILED_PHASES[*]}] unexpected=${#UNEXPECTED[@]}"
  printf '%s\n' "${UNEXPECTED[@]}" | tee -a "$SUMMARY"
  body="$(printf 'Nightly suite FAILED on %s (%s).\n\n%s\n\nReplay the shuffled pass with the SAME order:\n  %s\n\nFull logs: %s\n' \
    "$HEAD_BRANCH" "$HEAD_SHA" "$(printf '%s\n' "${UNEXPECTED[@]}" | head -25)" "$REPLAY" "$RUN_DIR")"
  page 4 "Legba: nightly suite FAILED" "warning,test_tube" "$body"
else
  say "VERDICT PASS phases=[${PHASES}] seed=${SEED}"
fi

# --- rotation ---------------------------------------------------------------
ln -sfn "$RUN_DIR" "${LOG_ROOT}/latest"
# Keep the newest KEEP_RUNS run directories; the names sort chronologically
# because they are UTC timestamps.
mapfile -t _old < <(find "$LOG_ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' \
  2>/dev/null | sort -r | tail -n +"$((KEEP_RUNS + 1))")
for d in ${_old[@]+"${_old[@]}"}; do
  rm -rf "${LOG_ROOT:?}/${d}"
done
[ "${#_old[@]}" -gt 0 ] && say "rotated ${#_old[@]} old run dir(s), keeping ${KEEP_RUNS}"

exit 0
