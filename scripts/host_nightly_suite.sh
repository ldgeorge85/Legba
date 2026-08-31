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
  # RETIRED 2026-08-21 (RUST-4 mothball). This section used to carry eleven
  # entries: every test that resolves an optimizer's parent prompt module
  # (the `legba.prompts.*` packages import dspy at module level) dies with
  # ModuleNotFoundError in the dspy-free test image, or a
  # PromptModuleImportError / DescriptorValidationError wrapping one, and
  # they were masked red here instead of paging every night.
  #
  # The GEPA optimizer plane is now MOTHBALLED (docs/SEAMS.md #53,
  # planning/RUST4_EVIDENCE_2026-08-21.md) — installing dspy into
  # legba/legba-test to make these green for real is no longer the operator
  # call this note used to frame it as; the plane isn't shipping. Instead
  # the eleven tests now carry an explicit
  # `@pytest.mark.skip(reason="optimizer plane mothballed 2026-08-21
  # (RUST-4)")` in their own files (NOT deleted — they still exist, still
  # collect, and un-skip trivially if the plane un-mothballs) and so no
  # longer need to be named here: a real pytest skip is honest in a way a
  # standing allowlist entry silently masking a fail never was. Removing them
  # from this array is what makes the nightly stop lying — the mask shrinks
  # from eleven to zero for this class instead of accumulating a stale
  # env-conditional exception nobody re-checks.
  #
  # (The dspy_gepa worker image + `test_v3_optimizer_diff.py`'s subprocess
  # guard that dspy never leaks into the registry process are both still
  # real and still exercised — mothball keeps the code and its tests, it
  # only drops the deploy/schedule surface. See SEAMS #53 "Guard rail".)

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
  # RULE AMENDED 2026-08-15, and it stands: an entry retires on a ROOTED CAUSE
  # (the polluter found and fixed, band-calibration-style), never on a quiet
  # streak. The 2026-08-09 stale-entry sweep retired nine entries on three
  # quiet nights — and seven of them refired within a week (jobs pair
  # 08-11/14/15, all five collection_requirements 08-14/15): a LONG-PERIOD
  # order-dep is silent most nights by definition, so quiet is not evidence.
  # Those seven were RESTORED pending-root and are retired below by that rule,
  # each with its polluter named and a command that reproduces the failure.
  #
  # RETIRED 2026-08-16 (task #23 — rooted, then fixed). Both classes turned out
  # to be the same shape the section header describes: a test asserting a
  # GLOBAL statement over a substrate the whole single-process suite shares,
  # broken by a sibling that wrote to that substrate and never retired what it
  # wrote. Both fixes scope the POLLUTER's cleanup; neither weakens a victim.
  #
  #   * the five tests/data_pkg/test_collection_requirements.py entries.
  #     POLLUTER: test_source_catalog_bringup.py::test_catalog_registers_
  #     head_rows_and_credibility_rows registered the whole embedded catalog —
  #     35-50 ACTIVE is_head rows across all four scope.source_class values,
  #     each with the entry's scope.geo — into the SESSION-shared
  #     source_descriptors and never removed them.
  #     collection_gap._match_candidate_sources reads that table globally, and
  #     the victim's clean_slate only deletes its own owner's rows, so all five
  #     were asserting over 35-50 candidate sources they never seeded. It is
  #     exactly those five and no others because the catalog rows are `active`:
  #     the ORDER BY puts them last and an active candidate never yields a
  #     suggested_fetch_url, which spares the file's two other candidate tests.
  #     REPRO (pre-fix):
  #       bash scripts/run_tests_in_container.sh \
  #         tests/data_pkg/test_source_catalog_bringup.py \
  #         tests/data_pkg/test_collection_requirements.py -p no:randomly
  #       -> exactly those five FAILED; the victim file alone, 21 passed.
  #     FIX: the registration test retires exactly the (descriptor_id, version)
  #     pairs it added and asserts the table is back to its pre-run snapshot.
  #
  #   * the jobs pair. POLLUTER: five tests in tests/runtime/jobs end on a live
  #     'claimed' ledger row on purpose (test_reaper_leaves_fresh_claims_alone
  #     most plainly — that row IS its subject) and the job_pg fixture never
  #     retired them. JobStore.reap_stale_claims sweeps public.legba_jobs
  #     table-wide, so a leaked claim is reapable once it is older than one
  #     lease (ack_wait x max_deliver = 40 s), and both victims state their
  #     result as a global `worker.reaped == 1`.
  #     WHY IT WAS SILENT MOST NIGHTS — the useful half of this one: every test
  #     in that package runs in ~20 ms, so in file order a leak is never 40 s
  #     old when a victim sweeps and the ORDERED phase can never show it.
  #     pytest-randomly sorts MODULES by crc32("<seed>::<module>") with no
  #     regard for package or directory, so a shuffled seed scatters those five
  #     modules through the 16-minute session and minutes separate a leaking
  #     module from a victim. Long-period by construction, and the reason three
  #     quiet nights proved nothing.
  #     REPRO (pre-fix), with a 45 s spacer module standing in for that gap:
  #       bash scripts/run_tests_in_container.sh \
  #         tests/runtime/jobs/test_worker_reaper_backoff.py::test_reaper_leaves_fresh_claims_alone \
  #         tests/runtime/jobs/spacer.py \
  #         tests/runtime/jobs/test_jobs_plane_hardening.py::test_failed_reap_is_not_reenqueued \
  #         -p no:randomly
  #       -> assert 2 == 1 (the captured log names both reaped keys). The same
  #          shape with test_nak_uses_delay_when_sibling_holds_claim as the
  #          polluter fails test_worker_loop_reaps_due_stale_claim.
  #     FIX: job_pg now runs inside job_store_scope, which retires exactly the
  #     ledger rows written inside it; tests/runtime/jobs/
  #     test_ledger_scope_isolation.py pins both halves of that contract.
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
  #
  # RETIRED 2026-08-25 — on the operator's read of the 08-23 and 08-24 runs:
  # both printed "allowlist entry matched nothing (retire it?)" two nights
  # running. Re-verified this session, both directly (ordered, isolated) and
  # inside a full `tests/` ORDERED pass alongside every sibling that could
  # plausibly leak into them — no failure either way. No polluter mechanism
  # is named here because none was found to name; this is the
  # quiet-streak-plus-direct-reproduction basis the 2026-08-15 rule amendment
  # asks to be stated explicitly rather than silently reused, not a
  # rooted-cause fix like the entries above it. If either refires, the right
  # response is the same as always — root it, fix it, and only then drop it
  # from this list.
  #   * tests/data_pkg/test_conversion_webhooks.py::
  #     test_register_webhook_persists_row_and_returns_active
  #     STATUS unchanged as of 2026-08-29 — not observed to refire since
  #     retirement, and out of scope for the shuffle-pollution-class train
  #     below (not investigated this pass).
  #
  # test_postgres_pool_search_path.py::
  # test_actor_state_table_reachable_from_every_acquire — REFIRED 2026-08-29
  # (seed 455838173), four days after the entry above retired it on a quiet
  # streak, EXACTLY as that entry's own comment warned it might
  # ("if it refires, root it, fix it, and only then drop it"). This time
  # rooted, not re-retired on a streak:
  #   POLLUTER: tests/data_pkg/test_runtime_telemetry_api.py's
  #   _insert_actor_state helper (3 call sites) INSERTs directly into the
  #   session-shared actor_state table and nothing there ever cleaned up.
  #   The victim's own assertions (`unqualified count == 0`,
  #   `qualified count == 0`) were incidental scaffolding that assumed a
  #   pristine table — the test's REAL contract (per its own docstring) is
  #   search_path reachability/consistency across acquires, not emptiness.
  #   REPRO (pre-fix):
  #     bash scripts/run_tests_in_container.sh \
  #       tests/data_pkg/test_runtime_telemetry_api.py \
  #       tests/data_pkg/test_postgres_pool_search_path.py -p no:randomly
  #     -> assert 3 == 0 (test_actor_state_table_reachable_from_every_acquire).
  #   FIX (both sides, per this file's own doctrine of fixing the polluter
  #   AND scoping the victim's real claim):
  #     (1) test_postgres_pool_search_path.py's assertion now checks that the
  #         unqualified and public.-qualified reads resolve to the SAME count
  #         on every acquire — true regardless of how many rows exist, and
  #         still fully discriminating for the original 2026-05-21 bug shape
  #         (verified live: reverting both ActorStateStore.SCHEMA's explicit
  #         `public.` qualification AND PostgresStore._setup_connection's
  #         per-acquire search_path re-apply reproduces UndefinedTableError
  #         against the rewritten test; reverting only the fix under test
  #         restores a clean pass — see
  #         planning/CAMPAIGN_2026-08-29/SHUFFLE_FIX_REPORT.md).
  #     (2) test_runtime_telemetry_api.py now truncates actor_state at setup
  #         (autouse `_clean_actor_state`, via the new centralized
  #         `clean_tables` primitive in tests/data_pkg/conftest.py) so it
  #         stops being a source of this class of pollution for whatever
  #         else reads that table next.
  #   Per this file's own rule (a retired-then-refired entry must not
  #   silently return to this list on another quiet streak): NOT re-added
  #   here — rooted and fixed, same disposition as the 2026-08-16/08-10
  #   entries above.
  #
  # Also root-caused and fixed in the SAME 2026-08-29 train (none of these
  # were ever added as a live entry here, per the "an addition means fix it,
  # not list it" rule — documented here only because their nightly history
  # is otherwise easy to mistake for a NEW, still-open shuffle flake):
  #   * test_retention_policies_api.py's `clean_slate` fixture only reset the
  #     shared `retention_policies` seed rows at test SETUP, never at
  #     teardown — the confirmed polluter behind BOTH the 2026-08-17
  #     test_analyst_traces_retention.py failures (`enabled=FALSE` leftover
  #     from test_patch_updates_ttl_days_and_enabled makes the sweep a
  #     silent, correctly-behaving no-op) and the 2026-08-23
  #     test_retention_policies.py::test_migration_0109_table_and_seed_rows
  #     failure (`assert 90 == 0`, the same test's leftover ttl_days). Fixed
  #     by wrapping the reset in try/finally so it also runs at teardown.
  #   * test_evidence_archiver.py's five 2026-08-23 failures
  #     (`data["examined"] == N`): evidence_archiver's candidate SQL has no
  #     tenant/source scoping by design (a global "every verified-cited,
  #     unarchived signal" scan), so ANY of ~20 other files that insert a
  #     signal + a 'Faithfulness verify' critique without cleanup can inflate
  #     the count. Fixed by draining the pre-existing backlog to quiescence
  #     (bounded by evidence_archiver's own `_DEFAULT_MAX_ATTEMPTS`) before
  #     each test's own insert, rather than relaxing the assertions.
  #   * test_corpus_research_backlog.py's 2026-08-22 failure (KeyError
  #     evicting the deliberately-lowest-priority seeded question from a
  #     `resolve_open_questions(limit=8)` call): fixed with a scoped
  #     `DELETE FROM hypotheses WHERE status = 'open_question'` before the
  #     one vulnerable test (NOT the centralized `clean_tables` TRUNCATE
  #     primitive — `hypotheses` is shared by ~a dozen other files under
  #     other `status` values, so a blanket truncate would be collateral
  #     damage rather than a fix).
)

# ---------------------------------------------------------------------------
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="${LOG_ROOT}/${RUN_ID}"
SUMMARY="${RUN_DIR}/summary.txt"

now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }
say() { echo "$(now_iso) [nightly] $*" | tee -a "$SUMMARY"; }

# page <priority> <title> <tags> <body> — the alert idiom shared with
# host_llm_heartbeat.sh / host_stall_watchdog.sh. ntfy is consumed through the
# WEB UI (${LEGBA_ALERT_WEB_URL:?configure your ntfy web UI URL}), never a
# phone app.
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
