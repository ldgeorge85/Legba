#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# scripts/fleet_start.sh — RESUME THE FLEET after scripts/fleet_stop.sh.
#
# What it does, in order:
#   1. start the runtime:  up -d --no-deps legba-runtime-dapr
#   2. wait for it to report Health=healthy — the same bounded
#      `docker compose ps` poll scripts/rolling_deploy.sh uses (300 s, 5 s
#      steps); exit 3 if it never does, and the worker is NOT started
#   3. start the worker:   up -d --no-deps legba-dapr-workflow-worker
#   4. the host watchdogs: `rm -f /etc/legba-watchdog.disabled` only with
#      --resume-watchdog; by default the flag stays and a reminder is printed
#      (a resume you are still watching by hand should not race the stall
#      watchdog's own recovery).
#
# Every compose call runs under COMPOSE_PROFILES=runtime,dapr-workflow with
# --env-file .env -f docker-compose.yml: the worker's dependency graph names
# `dapr-placement`, a service behind the `dapr`/`runtime` profiles, and
# without them compose answers `no such service: dapr-placement` (see
# scripts/rolling_deploy.sh, header trap 2). --no-deps keeps compose from
# touching the registry, the substrate or the Dapr control plane, which
# fleet_stop.sh left running.
#
# `up -d` on a stopped container starts the SAME container with the SAME
# environment. An .env change (a new key, a repointed model) needs the
# container re-created: pass --recreate, which uses
# `up -d --force-recreate --no-deps` for both.
#
# USAGE
#   scripts/fleet_start.sh [--recreate] [--resume-watchdog] [--dry-run] [--env-file PATH]
#
#   --recreate         Re-create both containers (picks up .env changes).
#   --resume-watchdog  Remove /etc/legba-watchdog.disabled once both are up.
#   --dry-run          Print every command that WOULD run and exit 0. Touches
#                      nothing: no docker call, no flag file.
#   --env-file PATH    Compose env file. Default: .env (repo root).
#
# Exit codes: 0 ok · 1 bad argument · 3 the runtime never reported healthy.
# Prints no secret: the env file is handed to compose by path, never read here.
# ----------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# --- defaults (project + env/compose files match scripts/rolling_deploy.sh) --
PROJECT="legba"
ENV_FILE=".env"
COMPOSE_FILE="docker-compose.yml"
PROFILES="runtime,dapr-workflow"
WATCHDOG_FLAG="/etc/legba-watchdog.disabled"
RUNTIME_SVC="legba-runtime-dapr"
WORKER_SVC="legba-dapr-workflow-worker"
HEALTH_TIMEOUT_S=300
HEALTH_POLL_S=5
DRY_RUN=0
RECREATE=0
RESUME_WATCHDOG=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)         DRY_RUN=1; shift ;;
    --recreate)        RECREATE=1; shift ;;
    --resume-watchdog) RESUME_WATCHDOG=1; shift ;;
    --env-file)        ENV_FILE="${2:?--env-file needs a value}"; shift 2 ;;
    --env-file=*)      ENV_FILE="${1#*=}"; shift ;;
    -h|--help)         grep -E '^#( |$)' "$0" | sed -E 's/^# ?//'; exit 0 ;;
    *)                 echo "fleet_start: unknown argument: $1 (try --help)" >&2; exit 1 ;;
  esac
done

# run CMD... — executes CMD, or under --dry-run only announces it.
run() {
  printf '$ %s\n' "$*"
  if [ "${DRY_RUN}" -eq 1 ]; then
    return 0
  fi
  "$@"
}

# dcp CMD... — docker compose for this stack, under the runtime+worker profiles.
dcp() {
  run env COMPOSE_PROFILES="${PROFILES}" docker compose -p "${PROJECT}" \
    --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
}

# wait_healthy SVC — the rolling_deploy.sh poll: compose ps Health, bounded.
wait_healthy() {
  local svc="$1"
  if [ "${DRY_RUN}" -eq 1 ]; then
    echo "(dry-run) would poll '${svc}' for Health=healthy, up to ${HEALTH_TIMEOUT_S}s"
    return 0
  fi
  local waited=0 health=""
  while [ "${waited}" -lt "${HEALTH_TIMEOUT_S}" ]; do
    health=$(COMPOSE_PROFILES="${PROFILES}" docker compose -p "${PROJECT}" \
      --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" ps --format '{{.Service}} {{.Health}}' 2>/dev/null \
      | awk -v s="${svc}" '$1==s{print $2}') || true
    if [ "${health}" = "healthy" ]; then
      echo "fleet_start: ${svc} healthy (${waited}s)"
      return 0
    fi
    sleep "${HEALTH_POLL_S}"
    waited=$(( waited + HEALTH_POLL_S ))
  done
  echo "fleet_start: ABORT — ${svc} did not report Health=healthy within ${HEALTH_TIMEOUT_S}s (last seen: '${health:-unknown}'); the worker was NOT started" >&2
  exit 3
}

UP=(up -d --no-deps)
if [ "${RECREATE}" -eq 1 ]; then
  UP=(up -d --force-recreate --no-deps)
fi

if [ "${DRY_RUN}" -eq 1 ]; then
  echo "fleet_start: DRY RUN — nothing below is executed"
fi

echo "fleet_start: starting ${RUNTIME_SVC}$([ "${RECREATE}" -eq 1 ] && echo ' (re-create)')"
dcp "${UP[@]}" "${RUNTIME_SVC}"
wait_healthy "${RUNTIME_SVC}"

echo "fleet_start: starting ${WORKER_SVC}$([ "${RECREATE}" -eq 1 ] && echo ' (re-create)')"
dcp "${UP[@]}" "${WORKER_SVC}"

echo
echo "fleet_start: STARTED ${RUNTIME_SVC} ${WORKER_SVC}"
if [ "${RESUME_WATCHDOG}" -eq 1 ]; then
  echo "fleet_start: re-enabling the host watchdogs"
  run rm -f "${WATCHDOG_FLAG}"
else
  echo "fleet_start: REMINDER — the host watchdogs are still disabled (${WATCHDOG_FLAG})."
  echo "fleet_start:   re-enable with: rm -f ${WATCHDOG_FLAG}   (or re-run with --resume-watchdog)"
fi
