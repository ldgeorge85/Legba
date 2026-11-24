#!/usr/bin/env bash
# legba-ui-v3/tools/walk/run.sh — run the first-time-user browser walk (README.md).
#
#   tools/walk/run.sh fixture [--build] [-- <walk args>]
#       the built bundle in legba-ui-v3/dist + the committed synthetic fixtures;
#       no network at all while the browser runs. --build (re)builds the bundle
#       first in node:20-slim (vite only; the type check is the gate's own step),
#       after `npm ci` when node_modules is missing or older than the lockfile.
#   tools/walk/run.sh live [--record] [-- <walk args>]
#       the LIVE bundle (docker volume legba_ui_dist, read-only) + the live
#       registry on the compose network, GET-only. The bearer comes from
#       LEGBA_TOKEN in the environment and is handed to the container by NAME
#       (never on a command line, never written down). --record rewrites
#       tools/walk/fixtures/api.json from this run's GET responses, sanitized.
#       With sign-in on, give an account instead of the bearer:
#       LEGBA_WALK_EMAIL and LEGBA_WALK_PASSWORD_FILE (a file holding the
#       password, or a `password: ...` line). The file is mounted read-only
#       into the container; the walk signs in through the real sign-in screen
#       and skips, by name, the checks the account's role cannot make.
#
# Output (report.txt, result.json, screenshots) goes to $LEGBA_WALK_OUT, by
# default /tmp/legba-walk/<mode>-<utc>. Live screenshots show the owner's data:
# they stay on the host and are never committed.
# Exit: 0 every enforced budget held, 1 a budget failed, 2 the walk could not run.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UI="$(cd "${HERE}/../.." && pwd)"
IMAGE="${LEGBA_WALK_IMAGE:-mcr.microsoft.com/playwright:v1.49.1-noble}"
NETWORK="${LEGBA_WALK_NETWORK:-legba_default}"
LIVE_DIST_VOLUME="${LEGBA_WALK_LIVE_DIST:-legba_ui_dist}"
MODE="${1:-}"; shift || true
BUILD=0; RECORD=0; EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --build) BUILD=1 ;;
    --record) RECORD=1 ;;
    --) shift; EXTRA=("$@"); break ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
case "${MODE}" in fixture|live) : ;; *) sed -n '2,18p' "$0" | sed -E 's/^# ?//' >&2; exit 2 ;; esac

OUT="${LEGBA_WALK_OUT:-/tmp/legba-walk/${MODE}-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "${OUT}"

# playwright-core, once, into tools/walk/node_modules (gitignored)
if [ ! -d "${HERE}/node_modules/playwright-core" ]; then
  docker run --rm -v "${UI}:/ui" -w /ui/tools/walk "${IMAGE}" \
    sh -c 'npm install --no-audit --no-fund --loglevel=error >/dev/null' || { echo "walk: npm install failed" >&2; exit 2; }
fi

if [ "${MODE}" = "fixture" ]; then
  if [ "${BUILD}" = "1" ] || [ ! -f "${UI}/dist/index.html" ] || [ ! -f "${UI}/dist/m/index.html" ]; then
    docker run --rm -v "${UI}:/app" -w /app node:20-slim sh -c \
      'if [ ! -f node_modules/.package-lock.json ] || [ package-lock.json -nt node_modules/.package-lock.json ]; then npm ci --no-audit --no-fund --loglevel=error || exit 1; fi; npx vite build --logLevel error && npx vite build --config vite.config.mobile.ts --logLevel error' \
      >"${OUT}/build.log" 2>&1 || { echo "walk: bundle build failed (see ${OUT}/build.log)" >&2; tail -20 "${OUT}/build.log" >&2; exit 2; }
  fi
  exec docker run --rm --network none --init \
    -v "${UI}:/ui:ro" -v "${OUT}:/out" -w /ui/tools/walk "${IMAGE}" \
    node walk.mjs --mode fixture --dist /ui/dist --fixtures /ui/tools/walk/fixtures/api.json --out /out ${EXTRA[@]+"${EXTRA[@]}"}
fi

# live
AUTH_ARGS=()
if [ -n "${LEGBA_WALK_EMAIL:-}" ] && [ -n "${LEGBA_WALK_PASSWORD_FILE:-}" ]; then
  # an account: the email by NAME, the password file mounted read-only (never read here)
  if [ ! -r "${LEGBA_WALK_PASSWORD_FILE}" ]; then
    echo "walk: LEGBA_WALK_PASSWORD_FILE is not a readable file" >&2
    exit 2
  fi
  export LEGBA_WALK_EMAIL
  AUTH_ARGS=(-e LEGBA_WALK_EMAIL -e LEGBA_WALK_PASSWORD_FILE=/run/walk-password
             -v "$(cd "$(dirname "${LEGBA_WALK_PASSWORD_FILE}")" && pwd)/$(basename "${LEGBA_WALK_PASSWORD_FILE}"):/run/walk-password:ro")
elif [ -n "${LEGBA_TOKEN:-}" ]; then
  export LEGBA_TOKEN
  AUTH_ARGS=(-e LEGBA_TOKEN)
else
  echo "walk: live mode needs LEGBA_TOKEN (the registry bearer), or LEGBA_WALK_EMAIL and LEGBA_WALK_PASSWORD_FILE (an account); neither is ever printed" >&2
  exit 2
fi
REC_ARGS=(); UI_MOUNT="${UI}:/ui:ro"
if [ "${RECORD}" = "1" ]; then REC_ARGS=(--record /ui/tools/walk/fixtures/api.json); UI_MOUNT="${UI}:/ui"; fi
exec docker run --rm --network "${NETWORK}" --init "${AUTH_ARGS[@]}" \
  -v "${UI_MOUNT}" -v "${LIVE_DIST_VOLUME}:/dist:ro" -v "${OUT}:/out" -w /ui/tools/walk "${IMAGE}" \
  node walk.mjs --mode live --dist /dist --registry "${LEGBA_WALK_REGISTRY:-http://legba-registry:8090}" --out /out ${REC_ARGS[@]+"${REC_ARGS[@]}"} ${EXTRA[@]+"${EXTRA[@]}"}
