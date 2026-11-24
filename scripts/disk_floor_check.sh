#!/bin/bash
# legba disk floor — pages the engine topic when / ENTERS a usage band, not on
# every tick; prunes build cache weekly (Sun 04:00Z). Cron: */30 (deploy/cron.d/legba-disk.cron),
# run straight from the main checkout.
#
# X4 (A-F1): it used to post to "$LEGBA_ALERT_NTFY_URL/engine" — the IN-NETWORK
# topic URL (http://ntfy/legba-alerts) plus a path, unresolvable from the host,
# with a fallback on the wrong port (8089); the page could never arrive. The
# host reaches ntfy on its loopback port, like host_llm_heartbeat.sh.
#
# N-M (11-13): it had no state and paged EVERY 30 min while / sat at >= 85 % —
# 48 identical pages a day on 17 of 19 days, which buried the one day it hit
# 95 %. Now it pages by BAND, from a small state file:
#
#   band      enters at   ntfy priority   why this number
#   warning   >= 85 %     3               OpenSearch low watermark (85 %)
#   urgent    >= 90 %     4               OpenSearch high watermark (90 %)
#   critical  >= 93 %     5               two points ahead of OpenSearch's 95 %
#                                         flood stage (indices go read-only,
#                                         corpus indexing stops silently).
#                                         OpenSearch's own figure runs ~1 point
#                                         above df near full.
#
#   * One page on ENTERING a band (including a jump straight past one).
#   * Staying in a band never repeats — except critical, which re-notifies
#     every DISK_RENOTIFY_SECS (6 h) while it stays open.
#   * One recovery notice when usage drops out of a band, with hysteresis: a
#     band is left only below (its threshold - DISK_HYSTERESIS_PCT), 2 points,
#     so a reading wobbling across a threshold does not flap.
#   * Missing or corrupt state file ⇒ first run (state "ok" now): a disk already
#     inside a band pages that band once.
#
# The measurement is df's Use% (used / (used + available), rounded up) — the
# same figure the operator reads in `df` and host_llm_heartbeat.sh now uses.
set -u
cd "${LEGBA_REPO_DIR:-/usr/local/deployments/active/legba}" || exit 0

ENGINE_NTFY_URL="${LEGBA_ENGINE_NTFY_URL:-http://127.0.0.1:8093/engine}"
DISK_PATH="${DISK_PATH:-/}"
DISK_LOG="${DISK_LOG:-/var/log/legba-disk.log}"
DISK_STATE_FILE="${DISK_STATE_FILE:-/var/log/legba-disk.state}"
DISK_WARN_PCT="${DISK_WARN_PCT:-85}"
DISK_URGENT_PCT="${DISK_URGENT_PCT:-90}"
DISK_CRITICAL_PCT="${DISK_CRITICAL_PCT:-93}"
DISK_HYSTERESIS_PCT="${DISK_HYSTERESIS_PCT:-2}"
DISK_RENOTIFY_SECS="${DISK_RENOTIFY_SECS:-21600}"
NOW="${DISK_NOW:-$(date -u +%s)}"

BAND_NAMES=(ok warning urgent critical)
BAND_PCT=(0 "$DISK_WARN_PCT" "$DISK_URGENT_PCT" "$DISK_CRITICAL_PCT")
BAND_PRIO=(2 3 4 5)

log() { echo "$(date -u +%FT%TZ) $*" >> "$DISK_LOG"; }

# L-1 (single alert path): raise through scripts/lib/legba_alert.sh first; the
# direct push below is the fallback (and the only path unless LEGBA_ALERT_RAISE=1).
_legba_alert_lib="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/legba_alert.sh"
if [ -r "$_legba_alert_lib" ]; then . "$_legba_alert_lib"; else legba_raise() { return 1; }; fi

page() {  # page <priority> <title> <body> [close | down:<priority of the band now held>]
  local sev; sev=$(legba_severity_for_priority "$1")
  case "${4:-}" in
    close)  legba_raise host_disk low "$DISK_PATH" "$2" "$3" --close && return 0 ;;
    down:*) legba_raise host_disk "$(legba_severity_for_priority "${4#down:}")" "$DISK_PATH" "$2" "$3" && return 0 ;;
    *)      legba_raise host_disk "$sev" "$DISK_PATH" "$2" "$3" && return 0 ;;
  esac
  curl -s -m 10 -H "Title: $2" -H "Priority: $1" -H "Tags: floppy_disk" \
    -d "$3" "$ENGINE_NTFY_URL" >/dev/null 2>&1 || log "ntfy send failed ($2)"
}

dur() {  # dur <seconds> → "2d3h" / "5h12m" / "14m"
  local s=$(( $1 < 0 ? 0 : $1 ))
  if [ "$s" -ge 86400 ]; then echo "$(( s / 86400 ))d$(( s % 86400 / 3600 ))h"
  elif [ "$s" -ge 3600 ]; then echo "$(( s / 3600 ))h$(( s % 3600 / 60 ))m"
  else echo "$(( s / 60 ))m"; fi
}

# --- measure (df's Use% and available space) --------------------------------
read -r USE FREE < <(df -h --output=pcent,avail "$DISK_PATH" | tail -1 | tr -d '%')
case "${USE:-}" in ''|*[!0-9]*)
  log "disk=unreadable path=${DISK_PATH} (df gave no percent)"; exit 0 ;;
esac

# --- the band the reading sits in, ignoring hysteresis ----------------------
raw=0
for b in 3 2 1; do
  if [ "$USE" -ge "${BAND_PCT[$b]}" ]; then raw=$b; break; fi
done

# --- last state: "<band> <since_epoch> <last_notify_epoch>" -----------------
prev=0; since=$NOW; last_notify=0; first_run=1
if [ -r "$DISK_STATE_FILE" ]; then
  read -r s_band s_since s_notify _rest < "$DISK_STATE_FILE" 2>/dev/null || true
  if [[ "${s_band:-}" =~ ^[0-3]$ && "${s_since:-}" =~ ^[0-9]+$ && "${s_notify:-}" =~ ^[0-9]+$ ]]; then
    prev=$s_band; since=$s_since; last_notify=$s_notify; first_run=0
  else
    log "disk state file unreadable (${DISK_STATE_FILE}) — treated as first run"
  fi
fi

# --- next band: rise at once; fall only past threshold - hysteresis ---------
band=$prev
if [ "$raw" -ge "$prev" ]; then
  band=$raw
else
  while [ "$band" -gt "$raw" ] && [ "$USE" -lt $(( ${BAND_PCT[$band]} - DISK_HYSTERESIS_PCT )) ]; do
    band=$(( band - 1 ))
  done
fi

name=${BAND_NAMES[$band]}
action=none
if [ "$band" -gt "$prev" ]; then
  action=enter
  was=""
  [ "$first_run" -eq 0 ] && was=" Was ${BAND_NAMES[$prev]} for $(dur $(( NOW - since )))."
  page "${BAND_PRIO[$band]}" "legba disk ${USE}% - ${name}" \
    "/ is at ${USE}% (df), ${FREE} free: entered the ${name} band (>= ${BAND_PCT[$band]}%) just now.${was} OpenSearch stops indexing at 95%. Reclaim: docker builder prune -af; docker image prune -f; check /var/lib/docker/volumes/legba_archive."
  since=$NOW; last_notify=$NOW
elif [ "$band" -lt "$prev" ]; then
  action=recover
  page 2 "legba disk ${USE}% - back below ${BAND_NAMES[$prev]}" \
    "/ is at ${USE}% (df), ${FREE} free: left the ${BAND_NAMES[$prev]} band (>= ${BAND_PCT[$prev]}%, cleared below $(( ${BAND_PCT[$prev]} - DISK_HYSTERESIS_PCT ))%) after $(dur $(( NOW - since ))). Now: ${name}." \
    "$([ "$band" -eq 0 ] && echo close || echo "down:${BAND_PRIO[$band]}")"
  since=$NOW; last_notify=$NOW
elif [ "$band" -eq 3 ] && [ $(( NOW - last_notify )) -ge "$DISK_RENOTIFY_SECS" ]; then
  action=renotify
  page 5 "legba disk ${USE}% - still critical" \
    "/ is at ${USE}% (df), ${FREE} free: critical (>= ${BAND_PCT[3]}%) for $(dur $(( NOW - since ))). OpenSearch stops indexing at 95%. Reclaim: docker builder prune -af; docker image prune -f; check /var/lib/docker/volumes/legba_archive."
  last_notify=$NOW
fi

# --- persist (atomic) --------------------------------------------------------
tmp="${DISK_STATE_FILE}.tmp.$$"
if { printf '%s %s %s\n' "$band" "$since" "$last_notify" > "$tmp"; } 2>/dev/null; then
  mv -f "$tmp" "$DISK_STATE_FILE" 2>/dev/null || { rm -f "$tmp"; log "disk state write failed (${DISK_STATE_FILE})"; }
else
  log "disk state write failed (${DISK_STATE_FILE})"
fi

if [ "${DISK_PRUNE_ENABLED:-1}" = "1" ] && [ "$(date -u +%u%H)" = "704" ]; then
  docker builder prune -af --keep-storage 8GB >/dev/null 2>&1; docker image prune -f >/dev/null 2>&1
fi
log "disk=${USE}% free=${FREE} band=${name} action=${action}"
