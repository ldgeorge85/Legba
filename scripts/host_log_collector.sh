#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# host_log_collector.sh — get container logs OFF the container, onto the host,
# before a recreate destroys them (S-5).
#
# WHY THIS EXISTS. Docker's json-file driver is capped (100m x 5, compose
# `x-logging`), but the log lives INSIDE the container's storage and dies with
# it. `docker compose up -d --force-recreate` therefore deletes history, and
# that has already cost a real investigation: on 2026-08-01 the runtime froze
# at 17:30 and the 19:31 recreate destroyed the container that froze, taking
# every line with it. The minute-level reconstruction of that outage was only
# possible because `legba-registry` happened NOT to be part of that recreate,
# so its access log still covered the window. That is luck, not observability.
#
# THE MECHANISM. One detached `docker logs --follow --timestamps` per compose
# container, appending to /var/log/legba/containers/<service>.log. Because the
# follower streams CONTINUOUSLY, every line is already on the host filesystem
# by the time the container is destroyed — "surviving recreates" means the
# history was copied off before the destruction, not that the file is magic.
# This script is the SUPERVISOR for those followers: run it from cron every
# minute and it (re)starts any follower that is missing, dead, or pointed at a
# container id that no longer exists (i.e. was recreated under it).
#
# Deliberately NOT Vector / Fluent Bit / an OpenSearch pipeline (the design
# drafted in FEATURE_COMPLETE_PLAN.md). Those are the right answer for a fleet;
# this is one host that needs its evidence to outlive a recreate, today, with
# no new container, no new dependency, and nothing to keep healthy.
#
# ROTATION is copy-truncate, done here rather than by logrotate. That is not a
# preference, it is the only correct shape: the follower holds an OPEN append
# fd on the live file, so a rename-based rotation (`mv live live.1`) would
# leave it writing into `live.1` forever while `live` stayed empty. Copying
# then truncating in place keeps the fd valid — an O_APPEND writer always
# writes at EOF, which is 0 after the truncate.
#
# ABSOLUTE PATHS EVERYWHERE. `scripts/loop_healthcheck.sh` was installed with a
# relative path and no `cd`, so cron ran it from /root and it failed 7,834 out
# of 7,834 times over 54 days without one line of its body ever executing. The
# cron line below is absolute, every path in this file is absolute, and nothing
# here reads $PWD.
#
# Install (docs/RUNBOOK.md 24.2) — deploy/cron.d/legba-log-collector:
#   * * * * * root /usr/local/deployments/active/legba/scripts/host_log_collector.sh >> /var/log/legba_log_collector.log 2>&1
#
# Operator surface:
#   host_log_collector.sh            supervise once (what cron runs)
#   host_log_collector.sh status     one line per service: follower + size
#   host_log_collector.sh stop       stop every follower (leaves the files)
#   DRY_RUN=1 host_log_collector.sh  print what it WOULD start; start nothing
#
# Disable during maintenance: touch /etc/legba-watchdog.disabled  (shared with
# the 24 stall watchdog — one flag quiets the whole host-side layer).
#
# DISK. Worst case is SERVICES x MAX_BYTES x (KEEP+1). At the defaults below
# and the current 15-container project that is 15 x 32 MB x 4 = ~1.9 GB. The
# host has been running at ~86% disk, so this number is deliberately modest and
# stated rather than buried; trim it with LEGBA_LOGSHIP_SERVICES (an explicit
# allowlist) or MAX_BYTES / KEEP before raising anything else.
#
# PER-SERVICE BUDGET OVERRIDE (2026-08-29, ops-heartbeat-retention). The
# uniform 4x32MiB=128MiB budget starves a genuinely high-volume container: on
# legba-runtime-dapr, the by-design reminder-GC existence-check sweep
# (src/legba/runtime/reminder_gc.py — ~300 GET .../reminders/run_cadence
# calls every 5 min, 100% 404, "in steady state `removed` is genuinely
# zero... legitimate re-checking, not a bug" per its own docstring) plus
# httpx INFO-level request logging (60% of its lines) measured LIVE at
# ~17.3 MiB/hour (173.95 MB retained across 9h34m43s, 2026-08-29
# 10:21:33Z-19:56:15Z, computed from the four retained files' byte counts and
# their first/last embedded --timestamps lines). The stock 128 MiB budget
# only covers ~7.4h at that rate — consistent with the observed ~9h
# retention window that left 08-27->08-29 10:21Z unrecoverable for this one
# service. See MAX_BYTES_OVERRIDE below: this raises ONLY that service's
# budget rather than the global default, because doing this for all 15
# services would add ~19 GB against a host already at ~86-92% disk.
#
# CROSS-CHECK (post-host-reboot, same day): the host hard-locked and
# rebooted ~21:01-21:08Z; legba-runtime-dapr came back at 21:08:03Z. A clean
# 3-minute post-restart window (21:08:09.733Z-21:11:11.990Z, 255,169 bytes)
# reads ~4.6 MiB/hour — lower than the ~17.3 MiB/hour pre-reboot figure, but
# a 3-minute sample is shorter than the reminder-GC sweep's own 5-minute
# cadence, so it plausibly caught zero or a partial sweep and undercounts
# steady state. Sizing below stays on the longer, higher (conservative)
# pre-reboot measurement.
#
# MEMORY. One `docker logs --follow` CLI process per container, ~15 MB RSS
# each, ~225 MB across the project. That is the honest cost of the simple
# design. LEGBA_LOGSHIP_SERVICES trims it if the host needs the RAM back.

set -u

# --- config (env-overridable, all absolute) ----------------------------------
PROJECT="${LEGBA_COMPOSE_PROJECT:-legba}"
LOG_DIR="${LEGBA_LOGSHIP_DIR:-/var/log/legba/containers}"
STATE_DIR="${LEGBA_LOGSHIP_STATE_DIR:-/var/lib/legba-logship}"
MAX_BYTES="${LEGBA_LOGSHIP_MAX_BYTES:-33554432}"   # 32 MiB per live file
KEEP="${LEGBA_LOGSHIP_KEEP:-3}"                    # .1 .. .3 kept behind it
# Explicit allowlist of compose SERVICE names (space/comma separated). Empty =
# every container in the compose project, which is the safe default: the one
# service you forgot to list is the one whose logs you will want.
SERVICES_FILTER="${LEGBA_LOGSHIP_SERVICES:-}"
# How far back to reach on the FIRST EVER follow of a service. Not "beginning":
# compose caps each container's json log at 100m x 5, so a from-the-beginning
# first run backfills up to 500 MB PER CONTAINER in one burst (measured: 125 MB
# from legba-caddy alone). Any `docker logs --since` expression.
FIRST_RUN_LOOKBACK="${LEGBA_LOGSHIP_FIRST_RUN_LOOKBACK:-1h}"
DRY_RUN="${DRY_RUN:-0}"
DISABLE_FLAG="/etc/legba-watchdog.disabled"
# This script's OWN cron log, rotated by the same copy-truncate. P6 6 item 11
# is "no logrotate for any /var/log/legba*.log"; a log collector that leaks its
# own log would be a poor answer to that. Cron holds it O_APPEND, so
# copy-truncate is valid here for exactly the reason it is for the followers.
SELF_LOG="${LEGBA_LOGSHIP_SELF_LOG:-/var/log/legba_log_collector.log}"

# Per-service MAX_BYTES override, keyed by compose SERVICE name (the same
# name `discover()` yields and the log file is named after). Falls back to
# the global $MAX_BYTES for every service not listed here — see the DISK
# comment above for the legba-runtime-dapr arithmetic.
#   360 MiB/file x (KEEP+1 = 4 files) = 1440 MiB total, ~83.2h retention at
#   the measured ~17.3 MiB/hour rate — a ~15% margin over the 72h target.
#   Extra disk vs. the stock budget: (360-32) MiB x 4 = 1312 MiB (~1.28 GiB),
#   for this one service only.
declare -A MAX_BYTES_OVERRIDE=(
    [legba-runtime-dapr]="${LEGBA_LOGSHIP_RUNTIME_DAPR_MAX_BYTES:-377487360}"
)
max_bytes_for() {
    local svc="$1"
    if [ -n "${MAX_BYTES_OVERRIDE[$svc]:-}" ]; then
        echo "${MAX_BYTES_OVERRIDE[$svc]}"
    else
        echo "$MAX_BYTES"
    fi
}

now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "$(now_iso) $*"; }

# --- service discovery -------------------------------------------------------
# Compose labels, not a hardcoded list: a service added to docker-compose.yml is
# collected on the next tick with no edit here. It also excludes non-compose
# strays automatically — `legba-test-age-w1`, the orphan test-fixture Postgres,
# carries no compose labels and is correctly skipped.
discover() {
    docker ps --filter "label=com.docker.compose.project=${PROJECT}" \
        --format '{{.Names}}|{{.Label "com.docker.compose.service"}}' 2>/dev/null \
        | sort
}

wanted() {
    local svc="$1"
    [ -z "$SERVICES_FILTER" ] && return 0
    case " ${SERVICES_FILTER//,/ } " in
        *" $svc "*) return 0 ;;
        *) return 1 ;;
    esac
}

follower_alive() {
    local pid="$1"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    # `kill -0` alone answers "some process holds this pid", not "OUR follower
    # does". After a reboot or heavy churn the pid can belong to something else
    # entirely, and we would then believe a dead follower is healthy FOREVER —
    # a silent stop, which is the exact failure class this whole area keeps
    # having. Confirm it is still a docker CLI.
    grep -qa docker "/proc/${pid}/cmdline" 2>/dev/null
}

# --- rotation (copy-truncate; see the header) --------------------------------
# rotate_if_needed <file> [max_bytes] — max_bytes defaults to the global
# $MAX_BYTES; callers pass a per-service override (max_bytes_for) where one
# applies.
rotate_if_needed() {
    local file="$1"
    local max_bytes="${2:-$MAX_BYTES}"
    [ -f "$file" ] || return 0
    local size
    size="$(stat -c %s "$file" 2>/dev/null || echo 0)"
    [ "$size" -le "$max_bytes" ] && return 0

    local i
    i="$KEEP"
    rm -f "${file}.${i}" 2>/dev/null
    while [ "$i" -gt 1 ]; do
        [ -f "${file}.$((i - 1))" ] && mv -f "${file}.$((i - 1))" "${file}.${i}"
        i=$((i - 1))
    done
    # cp then truncate-in-place: the follower's O_APPEND fd stays valid and its
    # next write lands at offset 0. `mv` here would silently strand the writer
    # on the rotated inode — the classic logrotate-without-copytruncate bug.
    cp -f "$file" "${file}.1" 2>/dev/null && : > "$file"
    log "ROTATED $file (${size}B > ${max_bytes}B), keeping ${KEEP}"
}

# --- follower lifecycle ------------------------------------------------------
start_follower() {
    local name="$1" svc="$2" cid="$3" reason="$4"
    local file="${LOG_DIR}/${svc}.log"
    local since=""

    # THREE cases, and conflating the first two costs gigabytes:
    #
    #  * `recreated` — we followed a DIFFERENT container id before. The new
    #    container is seconds-to-minutes old, so replaying it from the beginning
    #    is both cheap and exactly right: it is the window a recreate would
    #    otherwise have destroyed, which is the entire point of this script.
    #  * `first_run` — we have never followed this service at all. Replaying
    #    from the beginning here dumps the container's WHOLE retained docker log,
    #    which compose caps at 100m x 5 = 500 MB PER CONTAINER. Measured on this
    #    host: arming the collector against the live stack wrote 125 MB from
    #    `legba-caddy` alone in the first seconds, against 23 GB free at 84%
    #    disk. Steady-state growth for the same container is ~180 bytes/20 s —
    #    four orders of magnitude apart, so this burst is the whole risk and it
    #    is pure backfill of history the json-file driver still holds anyway.
    #    Bounded to FIRST_RUN_LOOKBACK; nothing is lost that `docker logs` on the
    #    still-living container cannot still produce.
    #  * `follower_died` — same container, our follower crashed. Resume from the
    #    last timestamp we actually wrote so a respawn does not duplicate.
    #    `--timestamps` is what makes that resumable.
    if [ "$reason" = "first_run" ]; then
        since="$FIRST_RUN_LOOKBACK"
    elif [ "$reason" = "follower_died" ]; then
        # Scan a WINDOW, not just the final line: the docker CLI's own error
        # output shares this file and carries no `--timestamps` prefix, so a
        # crash whose last line is a CLI message would otherwise yield an empty
        # `since` and replay the entire container log. Fall through to the most
        # recent rotated file when the live one has nothing usable — a death
        # right after a rotation is exactly when the live file is empty, and
        # that is a common pairing, not a corner case.
        for probe in "$file" "${file}.1"; do
            [ -s "$probe" ] || continue
            since="$(tail -n 20 "$probe" 2>/dev/null \
                | grep -oE '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[^ ]+' | tail -n 1)"
            [ -n "$since" ] && break
        done
    fi

    if [ "$DRY_RUN" = "1" ]; then
        log "DRY_RUN WOULD-START service=$svc container=$name cid=${cid:0:12} reason=$reason since=${since:-beginning}"
        return 0
    fi

    # setsid detaches the follower into its own session so it outlives this
    # cron invocation; without it cron's process-group teardown kills it and
    # the collector silently collects nothing.
    #
    # `2>&1` — ONE merged file, matching what `docker logs` shows an operator.
    # This is not cosmetic: `docker logs` writes the container's stdout to its
    # stdout and stderr to its stderr, and Python's logging module defaults to
    # STDERR, so splitting the streams left `legba-registry.log` at 0 bytes
    # while every line of the access log went to a `.err` sidecar nobody would
    # think to open. Caught by testing this script against the live registry.
    # `--timestamps` keeps the merge chronologically readable.
    #
    # `9>&-` — close the supervisor's flock fd in the child. A background child
    # INHERITS open fds, and an inherited fd shares the open file description
    # that carries the lock, so a follower would hold the single-flight lock for
    # its entire life and every subsequent tick would `flock -n` fail and exit
    # silently: rotation would stop, and a recreated container would never be
    # re-followed. Also caught by testing (ticks 2 and 3 were no-ops for exactly
    # this reason, and looked like a clean idempotent pass).
    if [ -n "$since" ]; then
        setsid nohup docker logs --follow --timestamps --since "$since" "$cid" \
            >> "$file" 2>&1 < /dev/null 9>&- &
    else
        setsid nohup docker logs --follow --timestamps "$cid" \
            >> "$file" 2>&1 < /dev/null 9>&- &
    fi
    local pid=$!
    disown "$pid" 2>/dev/null
    echo "$pid" > "${STATE_DIR}/${svc}.pid"
    echo "$cid" > "${STATE_DIR}/${svc}.cid"
    log "STARTED service=$svc container=$name cid=${cid:0:12} pid=$pid reason=$reason since=${since:-beginning}"
}

stop_all() {
    local svc pid
    for f in "${STATE_DIR}"/*.pid; do
        [ -e "$f" ] || continue
        svc="$(basename "$f" .pid)"
        pid="$(cat "$f" 2>/dev/null)"
        if follower_alive "$pid"; then
            kill "$pid" 2>/dev/null
            log "STOPPED service=$svc pid=$pid"
        fi
        rm -f "$f" "${STATE_DIR}/${svc}.cid"
    done
}

status() {
    local name svc cid pid file size
    while IFS='|' read -r name svc; do
        [ -n "$svc" ] || continue
        wanted "$svc" || continue
        pid="$(cat "${STATE_DIR}/${svc}.pid" 2>/dev/null)"
        cid="$(cat "${STATE_DIR}/${svc}.cid" 2>/dev/null)"
        file="${LOG_DIR}/${svc}.log"
        size="$(stat -c %s "$file" 2>/dev/null || echo 0)"
        if follower_alive "$pid"; then
            printf '%-32s following pid=%-8s cid=%-12s %sB\n' \
                "$svc" "$pid" "${cid:0:12}" "$size"
        else
            printf '%-32s NOT-FOLLOWING                          %sB\n' "$svc" "$size"
        fi
    done < <(discover)
}

# --- main --------------------------------------------------------------------
mkdir -p "$LOG_DIR" "$STATE_DIR" 2>/dev/null

case "${1:-supervise}" in
    status) status; exit 0 ;;
    stop)   stop_all; exit 0 ;;
    supervise) : ;;
    *) log "ERROR unknown mode '${1}' (supervise|status|stop)"; exit 2 ;;
esac

# Single-flight: a slow tick must not stack a second supervisor that then
# double-starts every follower.
exec 9>"${STATE_DIR}/lock"
if ! flock -n 9; then
    exit 0
fi

# A heartbeat stamp proves the CRON ran even when the tick is silent (the
# steady state is silent). `stat -c %Y` on this file is the check that
# loop_healthcheck's 54 dead days had no equivalent of.
touch "${STATE_DIR}/heartbeat"

if [ -e "$DISABLE_FLAG" ]; then
    exit 0
fi

rotate_if_needed "$SELF_LOG"

if ! docker ps >/dev/null 2>&1; then
    log "ERROR docker unreachable — no followers touched this tick"
    exit 0
fi

started=0
while IFS='|' read -r name svc; do
    [ -n "$name" ] && [ -n "$svc" ] || continue
    wanted "$svc" || continue

    cid="$(docker inspect -f '{{.Id}}' "$name" 2>/dev/null)"
    [ -n "$cid" ] || continue

    rotate_if_needed "${LOG_DIR}/${svc}.log" "$(max_bytes_for "$svc")"

    pid="$(cat "${STATE_DIR}/${svc}.pid" 2>/dev/null)"
    known_cid="$(cat "${STATE_DIR}/${svc}.cid" 2>/dev/null)"

    if follower_alive "$pid" && [ "$known_cid" = "$cid" ]; then
        continue                      # healthy — the common case, stays silent
    fi

    if follower_alive "$pid"; then
        # The container was RECREATED under us. The old follower is streaming a
        # dead container's tail; let it drain naturally rather than killing it
        # mid-line, and bind a new one to the new id.
        log "RECREATE service=$svc old_cid=${known_cid:0:12} new_cid=${cid:0:12} — rebinding"
        kill "$pid" 2>/dev/null
        reason="recreated"
    elif [ -n "$known_cid" ] && [ "$known_cid" = "$cid" ]; then
        reason="follower_died"
    elif [ -n "$known_cid" ]; then
        reason="recreated"
    else
        reason="first_run"
    fi

    start_follower "$name" "$svc" "$cid" "$reason"
    started=$((started + 1))
done < <(discover)

[ "$started" -gt 0 ] && log "SUPERVISE started=$started followers"
exit 0
