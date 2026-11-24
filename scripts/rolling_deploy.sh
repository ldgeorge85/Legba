#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# scripts/rolling_deploy.sh — the house ROLLING-DEPLOY runbook.
#
# This is NOT deploy/deploy.sh. deploy/deploy.sh is full bring-up: empty
# volumes to a boot-verified instance, baseline SQL, vault load, registrars,
# the works. This script is the OTHER thing an operator does far more often:
# ship a code change to an already-provisioned stack — build the three core
# images, prove they are actually fresh, recreate registry -> migrate ->
# runtime -> worker in the one order that does not race the registry, prove
# specific markers actually landed inside all three containers, and
# optionally roll the UI + caddy behind them. It changes nothing about what
# deploy/deploy.sh does and does not call it.
#
# WHY REGISTRY FIRST, ALWAYS (there is no --registry-only-if-needed flag)
#   The registry and the runtime/worker share one Pydantic schema layer.
#   A runtime recreated against a registry that is still serving the OLD
#   shape sees /typed 500 on the first request that touches a changed field
#   — silently, because a 500 from a dependency looks like network noise, not
#   a schema mismatch, to most callers. Registry-first + wait-healthy is the
#   only ordering that cannot hit this. See the standing house rule this
#   codifies: "rebuild the registry on any shared-schema change, or stale
#   /typed 500s silently stop analysts."
#
# THE TWO TRAPS THIS SCRIPT EXISTS TO CATCH
#   1. FROM-runtime staleness. legba-dapr-workflow-worker's Dockerfile is
#      `FROM legba/legba-runtime-dapr`. If the runtime image is rebuilt and
#      the worker is not rebuilt AFTER it, the worker silently keeps running
#      the OLD runtime layer's code under a WORKER image that looks freshly
#      built (its own build timestamp is recent) but never actually picked
#      up the change. The only real proof is per-file marker verification
#      INSIDE the worker container (step 5 below), not a log line, not the
#      image's own build time.
#   2. The profile-less worker build. `docker compose build
#      legba-dapr-workflow-worker` with no COMPOSE_PROFILES set fails with
#      `no such service: dapr-placement` — a sibling service the worker's
#      own dependency graph needs, gated behind the same profile — and PRINTS
#      NOTHING THAT LOOKS LIKE AN ERROR in some compose versions if the
#      caller doesn't check the exit code. The worker build/recreate MUST
#      run with `COMPOSE_PROFILES=runtime,dapr-workflow` set.
#
# THE GUARD IS ON IMAGES, NEVER ON LOGS. A container can log a fresh-looking
# boot line while the image underneath it is a week old — logs prove the
# process started, not that the code it started is current. Every recreate
# in this script is gated on `docker inspect -f '{{.Created}}'` for all
# three images being younger than --max-image-age-min (default 30). The
# guard runs and can abort BEFORE any container is touched.
#
# ----------------------------------------------------------------------------
# USAGE
#   scripts/rolling_deploy.sh [options]
#
#   --max-image-age-min N   Image-age guard threshold, minutes. Default: 30.
#   --verify-file RELPATH   A file that must exist at
#                           /install/lib/python3.11/site-packages/legba/RELPATH
#                           inside ALL THREE containers post-deploy. Repeatable.
#   --verify-grep 'FILE:PATTERN'   (2026-09-08: an EMPTY --verify-file or --verify-grep list no longer
#                           yields one phantom empty entry — the old "${arr[@]:-}" expansion did, and
#                           produced 3 false 'MISSING ' aborts per roll that gave only one kind of marker)
#                           `grep -q -- PATTERN .../legba/FILE` must succeed
#                           inside ALL THREE containers post-deploy. Repeatable.
#                           PATTERN is a plain grep pattern, not a shell glob —
#                           quote it as one argument, e.g.
#                           --verify-grep 'data/provenance/judge_pipeline_version.py:2026-09-06/1'
#   --verify-import MODULE  `python -c "import MODULE"` must succeed inside ALL
#                           THREE containers post-deploy. Repeatable. For code
#                           outside the legba package — a profile's prompt
#                           package (W9 SPLIT-2), e.g.
#                           --verify-import legba_geopolitics.lens_left
#   --drain-seconds N       W11-F84: give the OLD runtime N seconds (1-60) after
#                           SIGTERM before docker SIGKILLs it at the runtime
#                           recreate (passed as `up --timeout N`; the compose
#                           default is 10 s). It drains only if that container
#                           was started with LEGBA_RUNTIME_DRAIN_SECONDS=D in
#                           .env (runtime/shutdown_drain.py): then the first
#                           SIGTERM closes the listener — daprd delivers no new
#                           actor call — and waits up to D s for the in-flight
#                           turns (an analyst run whose finding is written but
#                           whose critique is not), with a stop at D+10 s.
#                           Use N >= D+10. The roll reads D from the running
#                           container and warns when it is unset (that roll
#                           cannot drain; the next one can). Default 0: the
#                           compose default, as before.
#   --walk                  W11-F161: run the descriptor walker
#                           (scripts/bringup_register_descriptors.py --profile
#                           <walk-profile> --family action_pack --family
#                           analyst, in a one-off registry container: the
#                           scripts/bakeoff/README.md `reg` idiom) AFTER the
#                           registry recreate + migrate and BEFORE the runtime
#                           recreate, so the new runtime builds its analysts'
#                           deps on the new pack heads. Then, once the runtime
#                           is up, wait for its first pack-staleness sweep
#                           (`dapr_actors.pack_staleness.sweep`, one per
#                           periodic resync, ~5 min after start) and read it:
#                           any `pack_staleness.stale` line -> restart the
#                           runtime ONCE and read the next sweep; still stale
#                           -> exit 8. Roll 59 recreated the runtime before
#                           the walker minted substrate_read 4552f78e, and
#                           consult_default served the old pack for hours.
#   --walk-profile DIR      The profile the walker registers from. Default:
#                           profiles/geopolitics.
#   --stale-watch-seconds N How long to wait for a staleness sweep after the
#                           runtime is up. Default 420 (the 5-min resync plus
#                           start-up). No sweep seen in time is a loud warning,
#                           not an abort (the check cannot vouch either way).
#   --ui                    Also build + deploy the UI (legba-ui-build under
#                           the runtime+ui profiles) and restart legba-caddy
#                           to pick up the new static bundle.
#   --dry-run               Print the plan (config, guard readout, every
#                           command that WOULD run) and exit 0. Touches
#                           nothing but the read-only `docker inspect` calls
#                           the guard itself needs to report image ages.
#   --skip-build            Skip the two build steps; deploy whatever images
#                           already exist. The age guard still runs against
#                           them (this is the whole point of --skip-build:
#                           it does not waive the guard, it only waives the
#                           build) — a stale existing image still aborts.
#   --ignore-nightly REASON Roll despite a red nightly record (the NIGHTLY
#                           GATE below). REASON is required, non-empty, and is
#                           appended to the nightly ledger as a
#                           NIGHTLY_GATE_OVERRIDE line with the SHA and the UTC
#                           time — an override is a decision on the record.
#   --nightly-log PATH      The ledger the gate reads. Default:
#                           $LEGBA_NIGHTLY_SELF_LOG or
#                           /var/log/legba_nightly_suite.log.
#   --rolled-sha REV        The commit the gate judges. Default: HEAD of this
#                           checkout (what the build step builds).
#   --tier auto|t0|t1|t2|t3|full
#                           N-P (2026-11-13): the deploy TIER. Default `full`:
#                           today's path, unchanged. `auto` takes the tier
#                           scripts/deploy_tier.py computes for the range
#                           (deployed SHA..rolled SHA) after the backstops;
#                           an explicit tier is checked against that and a
#                           LOWER one is refused (exit 9). Below full, this
#                           script runs scripts/run_targeted_gate.sh itself
#                           and rolls only what the tier needs:
#                             t0   nothing (merge only; the SHA is recorded)
#                             t1   the descriptor walk for the changed
#                                  families, then the staleness sweep (one
#                                  runtime restart only if a pack is stale)
#                                  and the parse-error check
#                             t2   the UI build + caddy restart only
#                             t3   build + recreate ONLY the changed service
#                                  (registry, or runtime + its worker); no
#                                  migration; walk/UI when the range needs them
#                           `auto` resolving to full also runs the full gate
#                           (run_targeted_gate.sh full) and adds --walk / --ui
#                           when the range changed descriptors / UI.
#   --force-tier            Roll an explicit --tier below the computed one.
#                           Recorded in the deploy log (forced=1, forced_by=).
#   --range FROM..TO        The range the tier is computed over. Default: the
#                           deploy log's last successful roll .. --rolled-sha.
#   --deploy-log PATH       Default: $LEGBA_DEPLOY_LOG or
#                           /var/log/legba_deploy.log. One DEPLOY_ROLL line per
#                           real (non-dry) roll: time, range, tier computed /
#                           required / used, gate and result, services
#                           recreated, who forced. Its newest result=ok line is
#                           the DEPLOYED SHA (nothing recorded it before).
#   -h, --help              Print this header and exit 0.
#
#   LEGBA_BUILD_CACHE_KEEP_GB (env) The build cache kept after a successful
#                           roll, in GB. Default 4 (see BUILD_CACHE_KEEP_GB).
#   LEGBA_DEPLOY_ACTOR (env) Who rolls (forced_by=); default $SUDO_USER/$USER.
#
# THE NIGHTLY GATE (X1-A4, step 0 — before any build, and ENFORCED UNDER
# --dry-run too: a gate a dry run waves through proves nothing)
#   scripts/host_nightly_suite.sh appends one `NIGHTLY_VERDICT` line per run
#   (sha=, verdict=PASS|FAIL, phases=, dirty=). The gate takes the NEWEST
#   record that (a) ran the `ordered` full-suite phase, (b) came from a clean
#   tree (dirty=0) and (c) names the rolled SHA or one of its ancestors. If
#   that record is FAIL the roll stops with exit 6. No such record at all is
#   a loud WARNING, not a refusal (the gate cannot vouch either way; the
#   first nightly after a merge supplies one). To clear a red record without
#   an override, run the suite on the new HEAD by hand:
#     PHASES="lint ordered" scripts/host_nightly_suite.sh
#
# EXIT CODES
#   0  success
#   1  usage / argument error
#   2  image-age guard failed (one or more of the three images too old)
#   3  a recreated container did not report Health=healthy in time
#   4  the registry migration (`python -m legba.data.migrate`) failed
#   5  post-deploy marker verification failed in one or more containers
#   6  the NIGHTLY GATE refused: the newest nightly record for this SHA or an
#      ancestor is FAIL (override: --ignore-nightly "<reason>")
#   7  --walk: the descriptor walker failed (the runtime was NOT recreated)
#   8  --walk: the runtime still binds a stale pack after one restart
#   9  --tier: refused — an explicit tier below the computed/backstopped one
#      without --force-tier, or the classifier could not vouch for it
#   10 --tier: the tier's gate (run_targeted_gate.sh) FAILED; nothing rolled
#   11 --tier t1: the runtime logged a descriptor parse failure after the walk
#
# EXAMPLE
#   scripts/rolling_deploy.sh \
#     --verify-grep 'data/provenance/judge_pipeline_version.py:2026-09-06/1' \
#     --verify-file data/analysts/composition_prompt_assembly.py \
#     --ui
# ----------------------------------------------------------------------------

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# --- defaults ----------------------------------------------------------------
MAX_IMAGE_AGE_MIN=30
DO_UI=0
DRY_RUN=0
SKIP_BUILD=0
ENV_FILE=".env"
COMPOSE_FILE="docker-compose.yml"
VERIFY_FILES=()
VERIFY_GREPS=()
VERIFY_IMPORTS=()
IGNORE_NIGHTLY_REASON=""
IGNORE_NIGHTLY=0
NIGHTLY_LOG="${LEGBA_NIGHTLY_SELF_LOG:-/var/log/legba_nightly_suite.log}"
ROLLED_REV="HEAD"
DRAIN_SECONDS=0
MAX_DRAIN_SECONDS=60
DO_WALK=0
WALK_PROFILE="profiles/geopolitics"
STALE_WATCH_S=420
STALE_POLL_S=10
# The walker's families. --walk keeps its historical pair; t1/t3 set the
# families the range changed.
WALK_FAMILIES=(action_pack analyst)
# N-P deploy tiers (see --tier). TIER_USED=full is today's path exactly.
TIER_REQUESTED="full"
FORCE_TIER=0
TIER_RANGE=""
DEPLOY_LOG="${LEGBA_DEPLOY_LOG:-/var/log/legba_deploy.log}"
DEPLOY_ACTOR="${LEGBA_DEPLOY_ACTOR:-${SUDO_USER:-${USER:-unknown}}}"
TIER_COMPUTED="unknown"; TIER_REQUIRED="full"; TIER_USED="full"; TIER_FORCED=0
TIER_BACKSTOP_CODES=""; TIER_FROM=""; TIER_TO=""
GATE_KIND="nightly-ledger"; GATE_RESULT="-"
# Which of the three core services this roll builds / recreates / verifies.
ROLL_SERVICES=(registry runtime worker)
DO_MIGRATE=1
DEPLOY_LOG_ARMED=0
DEPLOY_LOG_RESULT=""
# BUILD-CACHE CAP (roll 65). After a successful roll the build cache is pruned
# down to this many GB (`docker builder prune --max-used-space`; images are
# never touched — a builder prune removes build-cache records only, so no image
# a container runs on can go). NOT `--reserved-space` (roll 87: 8.8 GB of
# reclaimable cache, the prune reclaimed 0 B, on Docker 29.2.1 / buildx
# 0.31.1 / BuildKit 0.27.1): in BuildKit's current GC `--reserved-space` is a
# FLOOR ("always allowed to keep") and a prune given only a floor has no
# ceiling to prune down to, so it deletes nothing; `--max-used-space`
# ("maximum allowed to keep") is the ceiling. A builder whose prune has no
# --max-used-space predates that GC, and there `--keep-storage` was the
# ceiling. WHY 4, MEASURED on roll 65 (the first roll of the cap, built
# from an empty cache): one full registry + runtime + worker build leaves
# 4.2 GB of build cache, 2.24 GB of it private (the rest shared with the
# images): the largest records are the two runtime /install layers (1.35 +
# 1.34 GB), the registry ones (284 + 256 MB), the builder apt layer (346 MB)
# and the worker's dspy layer (277 MB). A cap of 4 GB keeps one
# roll's set, so the next build stays warm, and lets nothing older pile up on
# top of it; the old 8 GB kept two rolls' worth. Override:
# LEGBA_BUILD_CACHE_KEEP_GB.
BUILD_CACHE_KEEP_GB="${LEGBA_BUILD_CACHE_KEEP_GB:-4}"

REGISTRY_SVC="legba-registry"
RUNTIME_SVC="legba-runtime-dapr"
WORKER_SVC="legba-dapr-workflow-worker"
UI_SVC="legba-ui-build"
CADDY_SVC="legba-caddy"

REGISTRY_IMAGE="legba/legba-registry:latest"
RUNTIME_IMAGE="legba/legba-runtime-dapr:latest"
WORKER_IMAGE="legba/legba-dapr-workflow-worker:latest"

CONTAINER_LEGBA_BASE="/install/lib/python3.11/site-packages/legba"
HEALTH_TIMEOUT_S=300
HEALTH_POLL_S=5

# --- pretty logging (UTC-timestamped, per house convention) ------------------
_c() { printf '\033[%sm' "$1" 2>/dev/null || true; }
BOLD="$(_c '1')"; RED="$(_c '31')"; GRN="$(_c '32')"; YEL="$(_c '33')"; CYN="$(_c '36')"; RST="$(_c '0')"
ts()    { date -u +%Y-%m-%dT%H:%M:%SZ; }
phase() { printf '\n%s[%s] == %s ==%s\n' "${BOLD}${CYN}" "$(ts)" "$*" "${RST}"; }
info()  { printf '%s[%s]    - %s%s\n' "${RST}" "$(ts)" "$*" "${RST}"; }
ok()    { printf '%s[%s]    [OK] %s%s\n' "${GRN}" "$(ts)" "$*" "${RST}"; }
warn()  { printf '%s[%s]    [!]  %s%s\n' "${YEL}" "$(ts)" "$*" "${RST}"; }
abort() {
  local code="$1"; shift
  printf '\n%s[%s] [ABORT exit=%s] %s%s\n' "${BOLD}${RED}" "$(ts)" "$code" "$*" "${RST}" >&2
  exit "$code"
}

# --- arg parse -----------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --max-image-age-min)   MAX_IMAGE_AGE_MIN="${2:?--max-image-age-min needs a value}"; shift 2 ;;
    --max-image-age-min=*) MAX_IMAGE_AGE_MIN="${1#*=}"; shift ;;
    --verify-file)   VERIFY_FILES+=("${2:?--verify-file needs a value}"); shift 2 ;;
    --verify-file=*) VERIFY_FILES+=("${1#*=}"); shift ;;
    --verify-grep)   VERIFY_GREPS+=("${2:?--verify-grep needs a value}"); shift 2 ;;
    --verify-grep=*) VERIFY_GREPS+=("${1#*=}"); shift ;;
    --verify-import)   VERIFY_IMPORTS+=("${2:?--verify-import needs a value}"); shift 2 ;;
    --verify-import=*) VERIFY_IMPORTS+=("${1#*=}"); shift ;;
    --env-file)   ENV_FILE="${2:?--env-file needs a value}"; shift 2 ;;
    --env-file=*) ENV_FILE="${1#*=}"; shift ;;
    --ignore-nightly)
      # The reason is the NEXT argument and may not be another flag: a bare
      # `--ignore-nightly --dry-run` must not swallow --dry-run as its reason.
      case "${2-}" in ''|--*) abort 1 "--ignore-nightly needs a reason as its next argument" ;; esac
      IGNORE_NIGHTLY=1; IGNORE_NIGHTLY_REASON="$2"; shift 2 ;;
    --ignore-nightly=*) IGNORE_NIGHTLY=1; IGNORE_NIGHTLY_REASON="${1#*=}"; shift ;;
    --nightly-log)   NIGHTLY_LOG="${2:?--nightly-log needs a value}"; shift 2 ;;
    --nightly-log=*) NIGHTLY_LOG="${1#*=}"; shift ;;
    --rolled-sha)    ROLLED_REV="${2:?--rolled-sha needs a value}"; shift 2 ;;
    --rolled-sha=*)  ROLLED_REV="${1#*=}"; shift ;;
    --drain-seconds)   DRAIN_SECONDS="${2:?--drain-seconds needs a value}"; shift 2 ;;
    --drain-seconds=*) DRAIN_SECONDS="${1#*=}"; shift ;;
    --walk)       DO_WALK=1; shift ;;
    --walk-profile)   WALK_PROFILE="${2:?--walk-profile needs a value}"; shift 2 ;;
    --walk-profile=*) WALK_PROFILE="${1#*=}"; shift ;;
    --stale-watch-seconds)   STALE_WATCH_S="${2:?--stale-watch-seconds needs a value}"; shift 2 ;;
    --stale-watch-seconds=*) STALE_WATCH_S="${1#*=}"; shift ;;
    --tier)       TIER_REQUESTED="${2:?--tier needs a value}"; shift 2 ;;
    --tier=*)     TIER_REQUESTED="${1#*=}"; shift ;;
    --force-tier) FORCE_TIER=1; shift ;;
    --range)      TIER_RANGE="${2:?--range needs a value}"; shift 2 ;;
    --range=*)    TIER_RANGE="${1#*=}"; shift ;;
    --deploy-log)   DEPLOY_LOG="${2:?--deploy-log needs a value}"; shift 2 ;;
    --deploy-log=*) DEPLOY_LOG="${1#*=}"; shift ;;
    --ui)         DO_UI=1; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    -h|--help)    grep -E '^#( |$)' "$0" | sed -E 's/^# ?//'; exit 0 ;;
    *)            abort 1 "unknown argument: $1 (try --help)" ;;
  esac
done

if [ "${IGNORE_NIGHTLY}" -eq 1 ] && [ -z "${IGNORE_NIGHTLY_REASON//[[:space:]]/}" ]; then
  abort 1 "--ignore-nightly needs a non-empty reason, e.g. --ignore-nightly \"hotfix for the 10-08 page; nightly re-run queued\""
fi

case "${MAX_IMAGE_AGE_MIN}" in
  ''|*[!0-9]*) abort 1 "--max-image-age-min must be a positive integer, got '${MAX_IMAGE_AGE_MIN}'" ;;
esac

case "${DRAIN_SECONDS}" in
  ''|*[!0-9]*) abort 1 "--drain-seconds must be an integer 0-${MAX_DRAIN_SECONDS}, got '${DRAIN_SECONDS}'" ;;
esac
if [ "${DRAIN_SECONDS}" -gt "${MAX_DRAIN_SECONDS}" ]; then
  abort 1 "--drain-seconds is at most ${MAX_DRAIN_SECONDS} (a bounded drain), got ${DRAIN_SECONDS}"
fi

case "${BUILD_CACHE_KEEP_GB}" in
  ''|*[!0-9]*) abort 1 "LEGBA_BUILD_CACHE_KEEP_GB must be a whole number of GB, got '${BUILD_CACHE_KEEP_GB}'" ;;
esac

case "${STALE_WATCH_S}" in
  ''|*[!0-9]*) abort 1 "--stale-watch-seconds must be a positive integer, got '${STALE_WATCH_S}'" ;;
esac
if [ "${DO_WALK}" -eq 1 ] && [ ! -d "${REPO_ROOT}/${WALK_PROFILE}" ]; then
  abort 1 "--walk-profile: no such profile directory: ${WALK_PROFILE}"
fi
case "${TIER_REQUESTED}" in
  auto|t0|t1|t2|t3|full) : ;;
  *) abort 1 "--tier must be auto|t0|t1|t2|t3|full, got '${TIER_REQUESTED}'" ;;
esac
if [ "${FORCE_TIER}" -eq 1 ] && { [ "${TIER_REQUESTED}" = "auto" ] || [ "${TIER_REQUESTED}" = "full" ]; }; then
  abort 1 "--force-tier forces an explicit lower tier (t0..t3); it means nothing with --tier ${TIER_REQUESTED}"
fi
case "${TIER_RANGE}" in ''|?*..?*) : ;; *) abort 1 "--range must be FROM..TO, got '${TIER_RANGE}'" ;; esac

# dc = docker compose, this stack's env-file + compose file, every call.
dc() {
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
}

# dcp PROFILES CMD... — `dc` under COMPOSE_PROFILES=PROFILES. A shell function
# cannot be exec'd by `env`, so the profile set is applied as a temporary
# assignment on the docker binary itself (2026-09-06: the first live run died
# here with `env: 'dc': No such file or directory`, before any recreate).
dcp() {
  local profiles="$1"; shift
  COMPOSE_PROFILES="${profiles}" docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
}

# run CMD... — executes CMD, or under --dry-run just announces it.
run() {
  info "\$ $*"
  if [ "${DRY_RUN}" -eq 1 ]; then
    return 0
  fi
  "$@"
}

# --- the three core services, by role (registry | runtime | worker) --------
svc_of() {
  case "$1" in registry) echo "${REGISTRY_SVC}" ;; runtime) echo "${RUNTIME_SVC}" ;; worker) echo "${WORKER_SVC}" ;; esac
}
image_of() {
  case "$1" in registry) echo "${REGISTRY_IMAGE}" ;; runtime) echo "${RUNTIME_IMAGE}" ;; worker) echo "${WORKER_IMAGE}" ;; esac
}
rolls() { case " ${ROLL_SERVICES[*]-} " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

# --- the image-age guard: images, never logs ----------------------------
guard_image_ages() {
  phase "image-age guard (max ${MAX_IMAGE_AGE_MIN}m)"
  local now max_age_s failed=0
  now=$(date +%s)
  max_age_s=$(( MAX_IMAGE_AGE_MIN * 60 ))
  local img s images=()
  for s in ${ROLL_SERVICES[@]+"${ROLL_SERVICES[@]}"}; do images+=("$(image_of "$s")"); done
  for img in ${images[@]+"${images[@]}"}; do
    local created_raw created_s age_s age_min
    if ! created_raw=$(docker inspect -f '{{.Created}}' "${img}" 2>/dev/null); then
      warn "guard: image not found: ${img}"
      failed=1
      continue
    fi
    created_s=$(date -d "${created_raw}" +%s)
    age_s=$(( now - created_s ))
    age_min=$(( age_s / 60 ))
    if [ "${age_s}" -gt "${max_age_s}" ]; then
      warn "guard: ${img} is ${age_min}m old — STALE (max ${MAX_IMAGE_AGE_MIN}m)"
      failed=1
    else
      ok "guard: ${img} is ${age_min}m old"
    fi
  done
  if [ "${failed}" -ne 0 ]; then
    if [ "${DRY_RUN}" -eq 1 ]; then
      warn "guard would ABORT here (exit 2) in a real run — continuing because --dry-run"
    else
      abort 2 "image-age guard failed — one or more images predate this deploy window. Rebuild (drop --skip-build), or raise --max-image-age-min if this is deliberate."
    fi
  fi
}

# --- the NIGHTLY GATE (X1-A4): a red nightly blocks the roll ----------------
# _field KEY LINE — the value of `KEY=` in a NIGHTLY_VERDICT line ("" if absent;
# never a non-zero status, which `set -e` would turn into an aborted roll).
_field() { { grep -oE "(^| )$1=[^ ]*" <<<"$2" | head -1 | sed -E "s/^ ?$1=//"; } || true; }

nightly_gate() {
  phase "nightly gate"
  local rolled
  if ! rolled="$(git -C "${REPO_ROOT}" rev-parse --verify --quiet "${ROLLED_REV}^{commit}")"; then
    abort 1 "nightly gate: cannot resolve the rolled revision '${ROLLED_REV}'"
  fi
  ROLLED_SHA="${rolled}"
  info "rolled sha ${rolled}; ledger ${NIGHTLY_LOG}"
  local line sha verdict phases dirty found=""
  if [ -r "${NIGHTLY_LOG}" ]; then
    # Newest first. Only full-suite (ordered) records from a clean tree vouch.
    while IFS= read -r line; do
      sha="$(_field sha "${line}")"; verdict="$(_field verdict "${line}")"
      phases="$(_field phases "${line}")"; dirty="$(_field dirty "${line}")"
      case ",${phases}," in *,ordered,*) : ;; *) continue ;; esac
      [ "${dirty}" = "0" ] || continue
      [ -n "${sha}" ] || continue
      if [ "${sha}" = "${rolled}" ] \
         || git -C "${REPO_ROOT}" merge-base --is-ancestor "${sha}" "${rolled}" 2>/dev/null; then
        found="${line}"
        break
      fi
    done < <(grep -E '^NIGHTLY_VERDICT ' "${NIGHTLY_LOG}" 2>/dev/null | tac)
  fi
  if [ -z "${found}" ]; then
    warn "nightly gate: NO full-suite nightly record for ${rolled:0:12} or any ancestor in ${NIGHTLY_LOG} — the gate cannot vouch for this roll (proceeding)"
    return 0
  fi
  verdict="$(_field verdict "${found}")"; sha="$(_field sha "${found}")"
  local rel="ancestor ${sha:0:12}"
  [ "${sha}" = "${rolled}" ] && rel="this sha"
  if [ "${verdict}" = "PASS" ]; then
    ok "nightly gate: newest record (${rel}) is PASS — ${found#NIGHTLY_VERDICT }"
    return 0
  fi
  warn "nightly gate: newest record (${rel}) is ${verdict:-unparsable} — ${found#NIGHTLY_VERDICT }"
  if [ "${IGNORE_NIGHTLY}" -eq 1 ]; then
    local note
    note="NIGHTLY_GATE_OVERRIDE $(ts) sha=${rolled} record_sha=${sha} record_verdict=${verdict} dry_run=${DRY_RUN} reason=${IGNORE_NIGHTLY_REASON//$'\n'/ }"
    if echo "${note}" >> "${NIGHTLY_LOG}" 2>/dev/null; then
      warn "nightly gate OVERRIDDEN (--ignore-nightly) — logged to ${NIGHTLY_LOG}: ${IGNORE_NIGHTLY_REASON}"
    else
      abort 6 "nightly gate: --ignore-nightly given but the override could not be logged to ${NIGHTLY_LOG}; refusing an unrecorded override"
    fi
    return 0
  fi
  abort 6 "nightly gate: the newest nightly record for ${rolled:0:12} (${rel}) is ${verdict:-unparsable}. Fix and re-run the suite (PHASES=\"lint ordered\" scripts/host_nightly_suite.sh), or roll anyway with --ignore-nightly \"<reason>\" (logged)."
}

# --- wait for a service's compose Health to read healthy ------------------
wait_healthy() {
  local svc="$1"
  if [ "${DRY_RUN}" -eq 1 ]; then
    info "(dry-run) would poll '${svc}' for Health=healthy, up to ${HEALTH_TIMEOUT_S}s"
    return 0
  fi
  local waited=0 health=""
  while [ "${waited}" -lt "${HEALTH_TIMEOUT_S}" ]; do
    health=$(dc ps --format '{{.Service}} {{.Health}}' 2>/dev/null | awk -v s="${svc}" '$1==s{print $2}')
    if [ "${health}" = "healthy" ]; then
      ok "${svc} healthy (${waited}s)"
      return 0
    fi
    sleep "${HEALTH_POLL_S}"
    waited=$(( waited + HEALTH_POLL_S ))
  done
  abort 3 "${svc} did not report Health=healthy within ${HEALTH_TIMEOUT_S}s (last seen: '${health:-unknown}')"
}

# --- W11-F84: does the RUNNING runtime drain at SIGTERM? --------------------
# The drain bound is read by the old process at its start, so it is the
# container being replaced whose env decides this roll. Warn, never abort: the
# roll is still correct without a drain, it only loses the in-flight turns.
drain_env_check() {
  if [ "${DRY_RUN}" -eq 1 ]; then
    info "(dry-run) would read LEGBA_RUNTIME_DRAIN_SECONDS from the running ${RUNTIME_SVC}"
    return 0
  fi
  local d
  d="$(dc exec -T "${RUNTIME_SVC}" printenv LEGBA_RUNTIME_DRAIN_SECONDS 2>/dev/null | tr -d '[:space:]' || true)"
  case "${d}" in
    ''|0|*[!0-9]*)
      warn "drain: the running ${RUNTIME_SVC} has no LEGBA_RUNTIME_DRAIN_SECONDS — it stops at SIGTERM without draining, so THIS roll drains nothing; set it in ${ENV_FILE} (the new container drains from the next roll)" ;;
    *)
      if [ $(( d + 10 )) -gt "${DRAIN_SECONDS}" ]; then
        warn "drain: the running ${RUNTIME_SVC} drains for up to ${d}s (+10s backstop) but docker kills it after ${DRAIN_SECONDS}s — raise --drain-seconds to $(( d + 10 )) or lower LEGBA_RUNTIME_DRAIN_SECONDS"
      else
        ok "drain: the running ${RUNTIME_SVC} drains for up to ${d}s; docker waits ${DRAIN_SECONDS}s"
      fi ;;
  esac
}

# --- W11-F161: the descriptor walk, BEFORE the runtime recreate -------------
# A pack PUT never rebinds the analysts that already cached the old pack (no
# reverse index; RIDER #236), so the runtime must START on the new heads.
walk_descriptors() {
  phase "descriptor walk: ${WALK_FAMILIES[*]} heads (${WALK_PROFILE}) — before the runtime recreate"
  local fam fam_args=()
  for fam in "${WALK_FAMILIES[@]}"; do fam_args+=(--family "${fam}"); done
  if ! run dcp runtime run --rm --no-deps \
      -v "${REPO_ROOT}:${REPO_ROOT}" -w "${REPO_ROOT}" \
      -e LEGBA_DATA_PG_DB=legba \
      -e LEGBA_REGISTRY_URL=http://legba-registry:8090/api/v1/registry \
      -e "PYTHONPATH=/install/lib/python3.11/site-packages:${REPO_ROOT}/${WALK_PROFILE}/prompts" \
      --entrypoint python "${REGISTRY_SVC}" \
      scripts/bringup_register_descriptors.py --profile "${WALK_PROFILE}" \
      "${fam_args[@]}"; then
    abort 7 "descriptor walk failed — the runtime is NOT recreated (still the old container; under --tier t1 nothing else was touched). Fix the walk and re-run, or roll without --walk and restart the runtime after walking by hand."
  fi
  [ "${DRY_RUN}" -eq 1 ] || ok "descriptor walk done"
}

# _sweep_seen SINCE — 0 when the runtime has logged a staleness sweep since
# SINCE (an RFC 3339 UTC instant). _stale_lines SINCE — the stale lines since.
_runtime_log_since() { dc logs --no-color --no-log-prefix --since "$1" "${RUNTIME_SVC}" 2>&1 || true; }
_sweep_seen() { _runtime_log_since "$1" | grep -q 'pack_staleness\.sweep '; }
_stale_lines() { _runtime_log_since "$1" | grep 'pack_staleness\.stale ' || true; }

# wait_for_sweep SINCE — block until a sweep is logged (0) or the watch ends (1).
wait_for_sweep() {
  local since="$1" waited=0
  while [ "${waited}" -lt "${STALE_WATCH_S}" ]; do
    if _sweep_seen "${since}"; then
      ok "staleness sweep seen (${waited}s after ${since})"
      return 0
    fi
    sleep "${STALE_POLL_S}"
    waited=$(( waited + STALE_POLL_S ))
  done
  return 1
}

# check_pack_staleness SINCE — read the first sweep after SINCE; on any stale
# binding restart the runtime ONCE and read the next sweep; still stale -> 8.
check_pack_staleness() {
  local since="$1" stale
  phase "pack staleness: the first sweep after the runtime came up (up to ${STALE_WATCH_S}s)"
  if [ "${DRY_RUN}" -eq 1 ]; then
    info "(dry-run) would wait for 'pack_staleness.sweep' in ${RUNTIME_SVC}'s log since the recreate, count 'pack_staleness.stale' lines, restart ${RUNTIME_SVC} once if any, re-check, and exit 8 if still stale"
    return 0
  fi
  if ! wait_for_sweep "${since}"; then
    warn "pack staleness: NO sweep logged within ${STALE_WATCH_S}s — the check cannot vouch for this roll (proceeding; grep the runtime log for 'pack_staleness.stale' after the next resync)"
    return 0
  fi
  stale="$(_stale_lines "${since}")"
  if [ -z "${stale}" ]; then
    ok "pack staleness: 0 stale bindings"
    return 0
  fi
  warn "pack staleness: $(printf '%s\n' "${stale}" | wc -l) stale binding line(s) — restarting ${RUNTIME_SVC} once:"
  printf '%s\n' "${stale}" | cut -c1-300 >&2
  since="$(ts)"
  run dcp runtime restart -t "$(( DRAIN_SECONDS > 10 ? DRAIN_SECONDS : 10 ))" "${RUNTIME_SVC}"
  wait_healthy "${RUNTIME_SVC}"
  if ! wait_for_sweep "${since}"; then
    warn "pack staleness: NO sweep logged within ${STALE_WATCH_S}s after the restart — cannot vouch (proceeding)"
    return 0
  fi
  stale="$(_stale_lines "${since}")"
  if [ -n "${stale}" ]; then
    printf '%s\n' "${stale}" | cut -c1-300 >&2
    abort 8 "pack staleness: still stale after one runtime restart — a pack head the runtime cannot bind; see the lines above"
  fi
  ok "pack staleness: 0 stale bindings after the restart"
}

# --- roll 65: cap the build cache after a successful roll ------------------
# _cache_total — the build cache's total size as `docker buildx du` prints it.
_cache_total() { docker buildx du 2>/dev/null | awk '/^Total:/{print $2}' | tail -1 || true; }

prune_build_cache() {
  phase "build cache: cap at ${BUILD_CACHE_KEEP_GB}GB (build-cache records only; images are never touched)"
  local flag="--max-used-space" out reclaimed before after
  if [ "${DRY_RUN}" -eq 1 ]; then
    run docker builder prune -f "${flag}" "${BUILD_CACHE_KEEP_GB}GB"
    return 0
  fi
  # The ceiling: --max-used-space; a builder without it (before BuildKit's GC
  # rework) knows only --keep-storage, which was the ceiling there. Never
  # --reserved-space: that is a floor and prunes nothing on its own.
  case "$(docker builder prune --help 2>&1 || true)" in
    *--max-used-space*) : ;;
    *) flag="--keep-storage" ;;
  esac
  before="$(_cache_total)"
  info "\$ docker builder prune -f ${flag} ${BUILD_CACHE_KEEP_GB}GB"
  # A prune failure never fails a finished roll.
  if ! out="$(docker builder prune -f "${flag}" "${BUILD_CACHE_KEEP_GB}GB" 2>&1)"; then
    warn "builder prune failed (non-fatal): $(printf '%s\n' "${out}" | tail -1)"
    return 0
  fi
  reclaimed="$(printf '%s\n' "${out}" | awk '/^Total:/{print $2}' | tail -1)"
  after="$(_cache_total)"
  ok "build cache: ${before:-?} -> ${after:-?}, reclaimed ${reclaimed:-0B} (cap ${BUILD_CACHE_KEEP_GB}GB)"
  info "disk after the prune: $(df -h / 2>/dev/null | awk 'NR==2{print $5" used, "$4" free"}')"
}

# --- marker verification, inside ALL THREE containers ----------------------
verify_markers() {
  if [ "${#VERIFY_FILES[@]}" -eq 0 ] && [ "${#VERIFY_GREPS[@]}" -eq 0 ] && [ "${#VERIFY_IMPORTS[@]}" -eq 0 ]; then
    info "no --verify-file / --verify-grep / --verify-import given — skipping marker verification"
    return 0
  fi
  phase "marker verification (registry + runtime + worker)"
  if [ "${DRY_RUN}" -eq 1 ]; then
    for relpath in ${VERIFY_FILES[@]+"${VERIFY_FILES[@]}"}; do
      info "(dry-run) would require file present: ${CONTAINER_LEGBA_BASE}/${relpath}"
    done
    for spec in ${VERIFY_GREPS[@]+"${VERIFY_GREPS[@]}"}; do
      info "(dry-run) would require grep match: ${spec}"
    done
    for mod in ${VERIFY_IMPORTS[@]+"${VERIFY_IMPORTS[@]}"}; do
      info "(dry-run) would require import: ${mod}"
    done
    return 0
  fi
  local svc role failures=0 svcs=()
  # Every container this roll recreated (all three on the full path).
  for role in ${ROLL_SERVICES[@]+"${ROLL_SERVICES[@]}"}; do svcs+=("$(svc_of "$role")"); done
  for svc in ${svcs[@]+"${svcs[@]}"}; do
    local relpath
    for relpath in ${VERIFY_FILES[@]+"${VERIFY_FILES[@]}"}; do
      if dc exec -T "${svc}" test -f "${CONTAINER_LEGBA_BASE}/${relpath}"; then
        ok "${svc}: present ${relpath}"
      else
        warn "${svc}: MISSING ${relpath}"
        failures=$(( failures + 1 ))
      fi
    done
    local spec file pattern
    for spec in ${VERIFY_GREPS[@]+"${VERIFY_GREPS[@]}"}; do
      file="${spec%%:*}"
      pattern="${spec#*:}"
      if dc exec -T "${svc}" grep -q -- "${pattern}" "${CONTAINER_LEGBA_BASE}/${file}" 2>/dev/null; then
        ok "${svc}: matched '${pattern}' in ${file}"
      else
        warn "${svc}: NO MATCH for '${pattern}' in ${file}"
        failures=$(( failures + 1 ))
      fi
    done
    local mod
    for mod in ${VERIFY_IMPORTS[@]+"${VERIFY_IMPORTS[@]}"}; do
      if ! [[ "${mod}" =~ ^[A-Za-z_][A-Za-z0-9_.]*$ ]]; then
        warn "${svc}: '${mod}' is not a dotted module name"
        failures=$(( failures + 1 ))
      elif dc exec -T "${svc}" python -c "import ${mod}" >/dev/null 2>&1; then
        ok "${svc}: imports ${mod}"
      else
        warn "${svc}: CANNOT IMPORT ${mod}"
        failures=$(( failures + 1 ))
      fi
    done
  done
  if [ "${failures}" -ne 0 ]; then
    abort 5 "marker verification failed: ${failures} miss(es) across registry/runtime/worker — see [!] lines above for which container"
  fi
  ok "all markers verified in every recreated container (${ROLL_SERVICES[*]-})"
}

# ============================================================================
# N-P DEPLOY TIERS — scripts/deploy_tier.py decides, this script obeys.
# ============================================================================
TIER_SCRIPT="${LEGBA_DEPLOY_TIER_SCRIPT:-${SCRIPT_DIR}/deploy_tier.py}"
GATE_SCRIPT="${LEGBA_TARGETED_GATE_SCRIPT:-${SCRIPT_DIR}/run_targeted_gate.sh}"
ROLLED_SHA=""
WALK_STARTED_AT=""

# write_deploy_log EXIT_CODE — the one DEPLOY_ROLL line per real roll (EXIT
# trap: refusals, gate failures and aborts are on the record too). Its newest
# result=ok line is the deployed SHA deploy_tier.py ranges from. A dry run
# prints the line and writes nothing.
write_deploy_log() {
  local code="$1" result line services walk
  [ "${DEPLOY_LOG_ARMED}" -eq 1 ] || return 0
  DEPLOY_LOG_ARMED=0
  result="${DEPLOY_LOG_RESULT}"
  if [ -z "${result}" ]; then
    if [ "${code}" -eq 0 ]; then result=ok; else result=aborted; fi
  fi
  services="$(IFS=,; echo "${ROLL_SERVICES[*]-}")"
  walk="-"; [ "${DO_WALK}" -eq 1 ] && walk="$(IFS=,; echo "${WALK_FAMILIES[*]}")"
  line="DEPLOY_ROLL $(ts) result=${result} exit=${code} from=${TIER_FROM:--} to=${ROLLED_SHA:-${TIER_TO:--}}"
  line+=" tier_requested=${TIER_REQUESTED} tier_computed=${TIER_COMPUTED:-unknown} tier_required=${TIER_REQUIRED:--}"
  line+=" tier_used=${TIER_USED} forced=${TIER_FORCED} forced_by=$([ "${TIER_FORCED}" = 1 ] && echo "${DEPLOY_ACTOR//[[:space:]]/_}" || echo -)"
  line+=" backstops=${TIER_BACKSTOP_CODES:--} gate=${GATE_KIND} gate_result=${GATE_RESULT// /_}"
  line+=" services=${services:--} walk=${walk} ui=${DO_UI} migrate=${DO_MIGRATE} dry_run=${DRY_RUN} actor=${DEPLOY_ACTOR//[[:space:]]/_}"
  if [ "${DRY_RUN}" -eq 1 ]; then
    info "(dry-run) deploy log line, NOT written: ${line}"
    return 0
  fi
  if ! echo "${line}" >> "${DEPLOY_LOG}" 2>/dev/null; then
    printf '%s[%s]    [!]  could not append to the deploy log %s: %s%s\n' "${YEL}" "$(ts)" "${DEPLOY_LOG}" "${line}" "${RST}" >&2
  fi
}

# resolve_tier — ask the classifier, apply the answer to what this roll does.
resolve_tier() {
  phase "deploy tier (requested ${TIER_REQUESTED})"
  local args=(--repo "${REPO_ROOT}" --deploy-log "${DEPLOY_LOG}" --nightly-log "${NIGHTLY_LOG}"
              decide --requested "${TIER_REQUESTED}" --to "${ROLLED_SHA:-${ROLLED_REV}}" --format shell)
  [ -n "${TIER_RANGE}" ] && args+=(--range "${TIER_RANGE}")
  [ "${FORCE_TIER}" -eq 1 ] && args+=(--force-tier)
  local out="" rc=0
  if out="$(timeout 300 python3 "${TIER_SCRIPT}" "${args[@]}" 2>&1)"; then rc=0; else rc=$?; fi
  if [ "${rc}" -eq 0 ] || [ "${rc}" -eq 3 ]; then
    eval "$(grep -E '^TIER_[A-Z_]+=' <<<"${out}")"
  fi
  if [ "${rc}" -ne 0 ] && [ "${rc}" -ne 3 ]; then
    warn "tier classifier failed (rc=${rc}): $(printf '%s\n' "${out}" | tail -1)"
    TIER_COMPUTED="unknown"
    case "${TIER_REQUESTED}" in
      full) : ;;
      auto) TIER_REQUIRED="full"; TIER_USED="full"
            warn "tier auto: the classifier cannot vouch for anything lower — rolling FULL" ;;
      *)    if [ "${FORCE_TIER}" -eq 1 ]; then
              TIER_USED="${TIER_REQUESTED}"; TIER_FORCED=1; TIER_SERVICES="registry runtime worker"
              TIER_WALK_FAMILIES=""; TIER_UI=0
              warn "tier ${TIER_REQUESTED} FORCED without a classifier verdict (recorded)"
            else
              DEPLOY_LOG_RESULT=refused
              abort 9 "tier ${TIER_REQUESTED}: the classifier could not compute the range's tier, so a lower tier cannot be vouched for — roll --tier full, or --force-tier (recorded)"
            fi ;;
    esac
  fi
  if [ "${rc}" -eq 3 ]; then
    DEPLOY_LOG_RESULT=refused
    abort 9 "${TIER_MESSAGE:-tier refused} ${TIER_BACKSTOPS:+— backstops: ${TIER_BACKSTOPS}}"
  fi
  if [ "${TIER_REQUESTED}" = "full" ]; then
    TIER_USED="full"
  fi
  info "tier: computed=${TIER_COMPUTED} required=${TIER_REQUIRED:-?} used=${TIER_USED} forced=${TIER_FORCED} range=${TIER_FROM:0:12}..${TIER_TO:0:12}"
  [ -n "${TIER_BACKSTOPS:-}" ] && warn "backstop(s): ${TIER_BACKSTOPS}"
  [ "${TIER_FORCED}" = "1" ] && warn "tier ${TIER_USED} FORCED below the required ${TIER_REQUIRED} by ${DEPLOY_ACTOR} — recorded in ${DEPLOY_LOG}"

  if [ "${TIER_USED}" = "full" ]; then
    ROLL_SERVICES=(registry runtime worker); DO_MIGRATE=1
    if [ "${TIER_REQUESTED}" = "auto" ]; then
      # auto -> full: the full gate runs here, and the range's walk / UI ride along.
      GATE_KIND="full"
      if [ -n "${TIER_WALK_FAMILIES:-}" ]; then
        DO_WALK=1; read -r -a WALK_FAMILIES <<<"${TIER_WALK_FAMILIES}"
      fi
      [ "${TIER_UI:-0}" = "1" ] && DO_UI=1
    fi
    return 0
  fi
  # Below full: the classifier's union of actions, never more.
  GATE_KIND="targeted"; DO_MIGRATE=0
  read -r -a ROLL_SERVICES <<<"${TIER_SERVICES:-}"
  if [ -n "${TIER_WALK_FAMILIES:-}" ]; then
    DO_WALK=1; read -r -a WALK_FAMILIES <<<"${TIER_WALK_FAMILIES}"
  fi
  [ "${TIER_UI:-0}" = "1" ] && DO_UI=1
  if [ "${TIER_FORCED}" = "1" ]; then
    # A forced tier does only what that tier may do.
    case "${TIER_USED}" in
      t0) ROLL_SERVICES=(); DO_WALK=0; DO_UI=0 ;;
      t1) ROLL_SERVICES=(); DO_UI=0 ;;
      t2) ROLL_SERVICES=(); DO_WALK=0 ;;
      t3) : ;;
    esac
  fi
  info "tier ${TIER_USED}: services=[${ROLL_SERVICES[*]-}] walk=$([ "${DO_WALK}" -eq 1 ] && echo "${WALK_FAMILIES[*]}" || echo no) ui=${DO_UI} migrate=no"
}

# run_tier_gate — the tier's own gate (below full, and auto->full). The verdict
# line is GATE_VERDICT (run_targeted_gate.sh); anything but PASS stops the roll
# before a single build or container is touched.
run_tier_gate() {
  [ "${GATE_KIND}" = "nightly-ledger" ] && return 0
  local range="${TIER_FROM}..${TIER_TO:-${ROLLED_SHA}}"
  if [ -z "${TIER_FROM}" ]; then
    if [ "${GATE_KIND}" = "full" ]; then range="${ROLLED_SHA}..${ROLLED_SHA}"; else
      DEPLOY_LOG_RESULT=refused
      abort 9 "tier ${TIER_USED}: no deployed SHA to range from — pass --range FROM..TO"
    fi
  fi
  local tier_arg="${TIER_USED}"
  phase "gate: ${GATE_KIND} for tier ${TIER_USED} over ${range:0:12}..${range##*..}"
  if [ "${DRY_RUN}" -eq 1 ]; then
    info "\$ bash ${GATE_SCRIPT} ${tier_arg} ${range}"
    info "(dry-run) the gate is not run; a real roll stops here on anything but GATE_VERDICT verdict=PASS"
    GATE_RESULT="dry-run"
    return 0
  fi
  local out="" line verdict
  out="$(LEGBA_REPO_ROOT="${REPO_ROOT}" bash "${GATE_SCRIPT}" "${tier_arg}" "${range}" 2>&1)" || true
  printf '%s\n' "${out}" | tail -40
  line="$(grep -E '^GATE_VERDICT ' <<<"${out}" | tail -1 || true)"
  verdict="$(_field verdict "${line}")"
  GATE_RESULT="${verdict:-NONE}($(_field targeted "${line}"))"
  if [ "${verdict}" != "PASS" ]; then
    DEPLOY_LOG_RESULT=gate_fail
    abort 10 "gate ${GATE_KIND} (tier ${TIER_USED}) did not PASS: ${line:-no GATE_VERDICT line} — nothing was built or recreated"
  fi
  ok "gate PASS — ${line#GATE_VERDICT }"
}

# check_parse_errors SINCE — a descriptor the runtime cannot parse surfaces as
# `*_deps_resolver.parse_failed` on its next resolve; t1 has no other proof.
check_parse_errors() {
  local since="$1" lines
  phase "descriptor parse check (runtime log since ${since})"
  if [ "${DRY_RUN}" -eq 1 ]; then
    info "(dry-run) would grep ${RUNTIME_SVC}'s log since the walk for 'deps_resolver.parse_failed' and exit 11 on any"
    return 0
  fi
  lines="$(_runtime_log_since "${since}" | grep -E 'deps_resolver\.parse_failed' || true)"
  if [ -n "${lines}" ]; then
    printf '%s\n' "${lines}" | cut -c1-300 >&2
    abort 11 "the runtime logged $(printf '%s\n' "${lines}" | wc -l) descriptor parse failure(s) since the walk — see above"
  fi
  ok "no descriptor parse failures since the walk"
}

# ============================================================================
main() {
  phase "rolling deploy starting"
  info "env-file=${ENV_FILE} compose-file=${COMPOSE_FILE} max-image-age-min=${MAX_IMAGE_AGE_MIN} ui=${DO_UI} dry-run=${DRY_RUN} skip-build=${SKIP_BUILD} drain-seconds=${DRAIN_SECONDS} walk=${DO_WALK} tier=${TIER_REQUESTED}"

  # Every exit from here on leaves one DEPLOY_ROLL line (real rolls only).
  DEPLOY_LOG_ARMED=1
  trap 'write_deploy_log $?' EXIT

  # 0 — the nightly gate, before anything is built. Enforced under --dry-run.
  nightly_gate

  # 0b — N-P: the tier (full = today's path) and, below full, its own gate,
  # both before anything is built.
  resolve_tier
  run_tier_gate

  local did_build=0
  if [ "${#ROLL_SERVICES[@]}" -eq 0 ]; then
    phase "tier ${TIER_USED}: no image is built and no core container is recreated"
  else
    # 1 + 2 — build registry + runtime, then the worker AFTER runtime, profiled,
    # no cache (the worker is FROM runtime; a cached layer can otherwise ship
    # stale code under a fresh-looking image).
    if [ "${SKIP_BUILD}" -eq 1 ]; then
      phase "skipping build (--skip-build) — deploying existing images"
    else
      if rolls registry && rolls runtime; then
        phase "build: registry + runtime"
        run dc build "${REGISTRY_SVC}" "${RUNTIME_SVC}"
      elif rolls registry; then
        phase "build: registry only (tier ${TIER_USED})"
        run dc build "${REGISTRY_SVC}"
      elif rolls runtime; then
        phase "build: runtime only (tier ${TIER_USED})"
        run dc build "${RUNTIME_SVC}"
      fi

      if rolls worker; then
        phase "build: worker (FROM runtime — profiled, no-cache)"
        # No COMPOSE_PROFILES here => `no such service: dapr-placement` and a
        # build that looks like it did nothing. This is trap #2 in the header.
        run dcp runtime,dapr-workflow build --no-cache "${WORKER_SVC}"
      fi
      did_build=1
    fi

    # 3 — the age guard. Runs whether or not a build just happened; this is
    # what makes --skip-build safe (existing images still have to be fresh).
    guard_image_ages
  fi

  if [ "${DRY_RUN}" -eq 1 ]; then
    phase "dry-run: the following would execute in order (nothing above touched a container)"
  fi

  # 4 — registry recreate -> wait healthy -> migrate -> runtime recreate ->
  # wait healthy -> worker recreate. Registry ALWAYS first whenever it rolls:
  # see the header's "why registry first" note. A t3 roll leaves the registry
  # alone only when the classifier proved no changed module is in its import
  # graph (and no schema, migration or shared module changed at all).
  if rolls registry; then
    phase "registry: recreate"
    run dcp runtime up -d --no-deps --force-recreate "${REGISTRY_SVC}"
    wait_healthy "${REGISTRY_SVC}"
  fi

  if [ "${DO_MIGRATE}" -eq 1 ]; then
    phase "registry: migrate"
    if [ "${DRY_RUN}" -eq 1 ]; then
      info "(dry-run) would run: python -m legba.data.migrate inside a one-off ${REGISTRY_SVC} container"
    else
      if ! dcp runtime run --rm --no-deps \
          -e LEGBA_DATA_PG_DB=legba --entrypoint python "${REGISTRY_SVC}" -m legba.data.migrate; then
        abort 4 "migration failed — see the migrate container's output above"
      fi
      ok "migration applied"
    fi
  fi

  # W11-F161 — the walk lands the new pack/analyst heads BEFORE the runtime
  # that will cache them starts.
  if [ "${DO_WALK}" -eq 1 ]; then
    WALK_STARTED_AT="$(ts)"
    walk_descriptors
  fi

  if rolls runtime; then
    phase "runtime: recreate"
    RUNTIME_STARTED_AT="$(ts)"
    if [ "${DRAIN_SECONDS}" -gt 0 ]; then
      # W11-F84: the old actor host keeps its in-flight turns for up to D s after
      # SIGTERM (uvicorn's graceful shutdown); docker SIGKILLs only after N s.
      drain_env_check
      info "drain: the old runtime gets ${DRAIN_SECONDS}s after SIGTERM to finish in-flight actor turns"
      run dcp runtime up -d --no-deps --force-recreate --timeout "${DRAIN_SECONDS}" "${RUNTIME_SVC}"
    else
      run dcp runtime up -d --no-deps --force-recreate "${RUNTIME_SVC}"
    fi
    wait_healthy "${RUNTIME_SVC}"
  fi

  if rolls worker; then
    phase "worker: recreate (profiled)"
    run dcp runtime,dapr-workflow up -d --no-deps --force-recreate "${WORKER_SVC}"
    # The worker is a durabletask gRPC WorkflowRuntime, not an HTTP app — its
    # compose healthcheck is not the runtime's curl-based one. Give it a fixed
    # settle window rather than polling a Health value that may not mean the
    # same thing here.
    if [ "${DRY_RUN}" -eq 0 ]; then
      info "worker settle window (10s)"
      sleep 10
    fi
  fi

  if [ "${#ROLL_SERVICES[@]}" -gt 0 ]; then
    phase "post-recreate state"
    if [ "${DRY_RUN}" -eq 0 ]; then
      dc ps --format '{{.Service}}\t{{.State}}\t{{.Health}}' | grep -E "^(${REGISTRY_SVC}|${RUNTIME_SVC}|${WORKER_SVC})\b" || true
    fi

    # 5 — marker verification, inside every recreated container.
    verify_markers
  fi

  # 5b — W11-F161: the runtime's first staleness sweep reads clean. Under t1
  # the runtime was not recreated: read the first sweep after the walk (it
  # restarts the runtime once only if a pack binding is stale), then the
  # parse check.
  if [ "${DO_WALK}" -eq 1 ]; then
    check_pack_staleness "${RUNTIME_STARTED_AT:-${WALK_STARTED_AT}}"
    if ! rolls runtime; then
      check_parse_errors "${WALK_STARTED_AT}"
    fi
  fi

  # 6 — UI + caddy, only if asked.
  if [ "${DO_UI}" -eq 1 ]; then
    phase "ui: build + deploy"
    run dcp runtime,ui build "${UI_SVC}"
    run dcp runtime,ui up -d "${UI_SVC}"
    if [ "${DRY_RUN}" -eq 0 ]; then
      info "ui settle window (20s)"
      sleep 20
    fi
    phase "caddy: restart (picks up the new static bundle)"
    run dcp runtime,ui restart "${CADDY_SVC}"
  fi

  # Post-roll: three rolls left 23 GB of build cache (09-28); roll 65 caps it at
  # BUILD_CACHE_KEEP_GB and logs what it reclaimed. (A t0/t1 roll builds
  # nothing, so it has nothing to prune.)
  if [ "${TIER_USED}" = "full" ] || [ "${did_build}" -eq 1 ] || [ "${DO_UI}" -eq 1 ]; then
    prune_build_cache
  fi

  phase "rolling deploy done (tier ${TIER_USED})"
}

main "$@"
