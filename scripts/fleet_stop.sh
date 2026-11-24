#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# scripts/fleet_stop.sh — HALT THE FLEET: stop the two model-calling processes
# and nothing else.
#
# What it does, in order:
#   1. touch /etc/legba-watchdog.disabled — the maintenance flag both host
#      cron jobs honour (scripts/host_stall_watchdog.sh skips its recovery,
#      scripts/host_llm_heartbeat.sh exits 0). Without it the stall watchdog
#      sees a silent actor plane and restarts the very runtime this stopped.
#   2. docker compose -p legba stop legba-runtime-dapr legba-dapr-workflow-worker
#      — the runtime (analysts, ingestion, actor host) and the workflow worker
#      are the only two processes that call a model. The substrate, the Dapr
#      control plane, the registry, the UI and caddy stay up, so the read
#      surfaces keep answering and resume is two containers, not a bring-up.
#
# The worker sits behind the `dapr-workflow` profile and depends on
# `dapr-placement` (profiles `dapr`, `runtime`): every compose call here runs
# under COMPOSE_PROFILES=runtime,dapr-workflow, the same set
# scripts/rolling_deploy.sh uses, or compose answers `no such service:
# dapr-placement` (see that script's header, trap 2).
#
# Resume with scripts/fleet_start.sh (the last lines printed name the command).
#
# USAGE
#   scripts/fleet_stop.sh [--dry-run] [--env-file PATH]
#
#   --dry-run          Print every command that WOULD run and exit 0. Touches
#                      nothing: no flag file, no docker call.
#   --env-file PATH    Compose env file. Default: .env (repo root).
#
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
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)    DRY_RUN=1; shift ;;
    --env-file)   ENV_FILE="${2:?--env-file needs a value}"; shift 2 ;;
    --env-file=*) ENV_FILE="${1#*=}"; shift ;;
    -h|--help)    grep -E '^#( |$)' "$0" | sed -E 's/^# ?//'; exit 0 ;;
    *)            echo "fleet_stop: unknown argument: $1 (try --help)" >&2; exit 1 ;;
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
# `env` carries the profile set onto the docker binary (a shell function cannot
# be exec'd by env — rolling_deploy.sh's 2026-09-06 lesson — so none is used).
dcp() {
  run env COMPOSE_PROFILES="${PROFILES}" docker compose -p "${PROJECT}" \
    --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
}

if [ "${DRY_RUN}" -eq 1 ]; then
  echo "fleet_stop: DRY RUN — nothing below is executed"
fi

echo "fleet_stop: disabling the host watchdogs (${WATCHDOG_FLAG})"
run touch "${WATCHDOG_FLAG}"

echo "fleet_stop: stopping the model-calling processes"
dcp stop "${RUNTIME_SVC}" "${WORKER_SVC}"

echo
echo "fleet_stop: STOPPED ${RUNTIME_SVC} ${WORKER_SVC}"
echo "fleet_stop: still up: substrate, Dapr control plane, legba-registry, UI, caddy"
echo "fleet_stop: watchdogs disabled until ${WATCHDOG_FLAG} is removed"
echo "fleet_stop: resume with: scripts/fleet_start.sh --resume-watchdog"
echo "fleet_stop:   after an .env change: scripts/fleet_start.sh --recreate --resume-watchdog"
