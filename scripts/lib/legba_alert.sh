# shellcheck shell=bash
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# legba_alert.sh — the host scripts' one way to raise an alert (L-1, the single
# alert path). Source it; call legba_raise.
#
#   legba_raise <kind> <severity> <subject> <summary> <detail> [extra cli args...]
#
# It runs `python -m legba.data.alerts.cli raise` inside the runtime container
# (its database and push-service settings), which logs the alert, lets the ONE
# page rule decide, and records any push on the delivery ledger.
#
# Returns 0 when the alert was raised and recorded (paged or not — the rule
# decided). Returns non-zero when it could not be (the container is down, the
# database is unreachable, the image predates the alert log): the CALLER then
# falls back to its own direct push, so losing the database never loses a page.
#
#   LEGBA_ALERT_CLI_CONTAINER   the container to exec in (default legba-legba-runtime-dapr-1)
#   LEGBA_ALERT_RAISE=1         turn the alert path ON. It is OFF unless set
#                               (the cron files under deploy/cron.d set it), so a
#                               test or a hand run of a script never reaches the
#                               live runtime: off, every caller keeps its direct
#                               push exactly as before.
#   LEGBA_ALERT_RAISE_TIMEOUT   seconds before giving up (default 60)

# legba_severity_for_priority <ntfy priority 1-5> → the ladder word.
legba_severity_for_priority() {
  case "${1:-3}" in
    5) echo critical ;; 4) echo high ;; 3) echo medium ;; 2) echo low ;; *) echo info ;;
  esac
}

# legba_slug <text> → a lowercase subject id (letters, digits, _).
legba_slug() {
  printf '%s' "$1" | sed -e 's/^[Ll]egba: *//' | tr '[:upper:]' '[:lower:]' \
    | sed -e 's/[^a-z0-9]\{1,\}/_/g' -e 's/^_//' -e 's/_$//' | cut -c1-80
}

legba_raise() {
  local kind="$1" severity="$2" subject="$3" summary="$4" detail="$5"
  shift 5
  [ "${LEGBA_ALERT_RAISE:-0}" = "1" ] || return 1
  command -v docker >/dev/null 2>&1 || return 1
  local container="${LEGBA_ALERT_CLI_CONTAINER:-legba-legba-runtime-dapr-1}"
  printf '%s' "$detail" | timeout "${LEGBA_ALERT_RAISE_TIMEOUT:-60}" \
    docker exec -i "$container" python -m legba.data.alerts.cli raise \
      --kind "$kind" --severity "$severity" --subject "$subject" \
      --summary "$summary" --raised-by "$(basename "$0")" --detail-stdin "$@" \
      >/dev/null 2>&1
}
