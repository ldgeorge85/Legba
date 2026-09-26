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
#   -h, --help              Print this header and exit 0.
#
# EXIT CODES
#   0  success
#   1  usage / argument error
#   2  image-age guard failed (one or more of the three images too old)
#   3  a recreated container did not report Health=healthy in time
#   4  the registry migration (`python -m legba.data.migrate`) failed
#   5  post-deploy marker verification failed in one or more containers
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
    --env-file)   ENV_FILE="${2:?--env-file needs a value}"; shift 2 ;;
    --env-file=*) ENV_FILE="${1#*=}"; shift ;;
    --ui)         DO_UI=1; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    -h|--help)    grep -E '^#( |$)' "$0" | sed -E 's/^# ?//'; exit 0 ;;
    *)            abort 1 "unknown argument: $1 (try --help)" ;;
  esac
done

case "${MAX_IMAGE_AGE_MIN}" in
  ''|*[!0-9]*) abort 1 "--max-image-age-min must be a positive integer, got '${MAX_IMAGE_AGE_MIN}'" ;;
esac

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

# --- the image-age guard: images, never logs ----------------------------
guard_image_ages() {
  phase "image-age guard (max ${MAX_IMAGE_AGE_MIN}m)"
  local now max_age_s failed=0
  now=$(date +%s)
  max_age_s=$(( MAX_IMAGE_AGE_MIN * 60 ))
  local img
  for img in "${REGISTRY_IMAGE}" "${RUNTIME_IMAGE}" "${WORKER_IMAGE}"; do
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

# --- marker verification, inside ALL THREE containers ----------------------
verify_markers() {
  if [ "${#VERIFY_FILES[@]}" -eq 0 ] && [ "${#VERIFY_GREPS[@]}" -eq 0 ]; then
    info "no --verify-file / --verify-grep given — skipping marker verification"
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
    return 0
  fi
  local svc failures=0
  for svc in "${REGISTRY_SVC}" "${RUNTIME_SVC}" "${WORKER_SVC}"; do
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
  done
  if [ "${failures}" -ne 0 ]; then
    abort 5 "marker verification failed: ${failures} miss(es) across registry/runtime/worker — see [!] lines above for which container"
  fi
  ok "all markers verified in all three containers"
}

# ============================================================================
main() {
  phase "rolling deploy starting"
  info "env-file=${ENV_FILE} compose-file=${COMPOSE_FILE} max-image-age-min=${MAX_IMAGE_AGE_MIN} ui=${DO_UI} dry-run=${DRY_RUN} skip-build=${SKIP_BUILD}"

  # 1 + 2 — build registry + runtime, then the worker AFTER runtime, profiled,
  # no cache (the worker is FROM runtime; a cached layer can otherwise ship
  # stale code under a fresh-looking image).
  if [ "${SKIP_BUILD}" -eq 1 ]; then
    phase "skipping build (--skip-build) — deploying existing images"
  else
    phase "build: registry + runtime"
    run dc build "${REGISTRY_SVC}" "${RUNTIME_SVC}"

    phase "build: worker (FROM runtime — profiled, no-cache)"
    # No COMPOSE_PROFILES here => `no such service: dapr-placement` and a
    # build that looks like it did nothing. This is trap #2 in the header.
    run dcp runtime,dapr-workflow build --no-cache "${WORKER_SVC}"
  fi

  # 3 — the age guard. Runs whether or not a build just happened; this is
  # what makes --skip-build safe (existing images still have to be fresh).
  guard_image_ages

  if [ "${DRY_RUN}" -eq 1 ]; then
    phase "dry-run: the following would execute in order (nothing above touched a container)"
  fi

  # 4 — registry recreate -> wait healthy -> migrate -> runtime recreate ->
  # wait healthy -> worker recreate. Registry ALWAYS first: see the header's
  # "why registry first" note. There is no --registry-only-if-needed.
  phase "registry: recreate"
  run dcp runtime up -d --no-deps --force-recreate "${REGISTRY_SVC}"
  wait_healthy "${REGISTRY_SVC}"

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

  phase "runtime: recreate"
  run dcp runtime up -d --no-deps --force-recreate "${RUNTIME_SVC}"
  wait_healthy "${RUNTIME_SVC}"

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

  phase "post-recreate state"
  if [ "${DRY_RUN}" -eq 0 ]; then
    dc ps --format '{{.Service}}\t{{.State}}\t{{.Health}}' | grep -E "^(${REGISTRY_SVC}|${RUNTIME_SVC}|${WORKER_SVC})\b" || true
  fi

  # 5 — marker verification, inside all three containers.
  verify_markers

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

  phase "rolling deploy done"
}

main "$@"
