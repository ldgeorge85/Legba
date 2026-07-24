#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# b0_13_catch.sh — the ONE-SHOT instrumented catch for the B0-13 :52 event-loop
# freeze (plan: planning/B0_13_ROOT_CAUSE_2026-07-23.md Part 6). READ-ONLY
# except a py-spy pip install inside the runtime container (ephemeral).
#
# Run at ~00:44 UTC (cron /etc/cron.d/legba-b0-13-catch — REMOVE after the
# catch). Polls the runtime healthcheck; the instant it starts failing
# (expected ~00:52) it captures: the frozen Python stack (py-spy, twice),
# pg_stat_activity + ungranted pg_locks, sidecar/scheduler tails, and then the
# scheduler's 'Sidecar connected' actorTypes lines after the watchdog's FIRST
# restart (validates the P2 Fix-2a order). Exits by TIMEBOX if no freeze.

set -u
RUNTIME="legba-legba-runtime-dapr-1"
PG="legba-postgres-1"
TIMEBOX_MIN="${TIMEBOX_MIN:-40}"          # give up 40 min after start
log() { echo "$(date -u +%FT%TZ) [catch] $*"; }

log "armed — timebox ${TIMEBOX_MIN}m; pre-installing py-spy"
docker exec "$RUNTIME" pip install -q py-spy 2>/dev/null && log "py-spy ready" \
    || log "WARN py-spy install failed (will retry at trigger)"

deadline=$(( $(date +%s) + TIMEBOX_MIN * 60 ))
trigger=0
while [ "$(date +%s)" -lt "$deadline" ]; do
    hs="$(docker inspect -f '{{.State.Health.Status}} {{.State.Health.FailingStreak}}' "$RUNTIME" 2>/dev/null)"
    log "health: ${hs:-unknown}"
    streak="${hs##* }"
    case "$streak" in (''|*[!0-9]*) streak=0;; esac
    if [ "$streak" -ge 1 ]; then trigger=1; break; fi
    sleep 5
done

if [ "$trigger" -ne 1 ]; then
    log "NO FREEZE inside the timebox — exiting (rearm manually if wanted)"
    exit 0
fi

log "=== FREEZE DETECTED — capturing ==="
log "--- step 1: frozen stack (py-spy dump x2, 10s apart)"
docker exec "$RUNTIME" pip install -q py-spy 2>/dev/null
docker exec "$RUNTIME" sh -c 'py-spy dump --pid 1' 2>&1 || log "WARN py-spy dump 1 failed"
sleep 10
docker exec "$RUNTIME" sh -c 'py-spy dump --pid 1' 2>&1 || log "WARN py-spy dump 2 failed"

log "--- step 2: pg activity + ungranted locks"
docker exec "$PG" psql -U legba -d legba -tAc \
 "SELECT pid,state,wait_event_type,wait_event,now()-query_start AS age,left(query,120)
  FROM pg_stat_activity WHERE state<>'idle' ORDER BY age DESC LIMIT 15;" 2>&1
docker exec "$PG" psql -U legba -d legba -tAc \
 "SELECT locktype,relation::regclass,mode,granted,pid FROM pg_locks WHERE NOT granted;" 2>&1

log "--- step 3: delivery-plane innocence check (expect ~empty)"
docker logs legba-dapr-sidecar-1 --since "$(date -u -d '3 min ago' +%FT%TZ)" 2>&1 | grep -v 'Removed 0 expired rows' | tail -20
docker logs legba-dapr-scheduler-1 --since "$(date -u -d '3 min ago' +%FT%TZ)" 2>&1 | grep -vi compact | tail -10

log "--- step 4: wait for the watchdog FIRE (P2 validation) — up to 45 min"
fire_deadline=$(( $(date +%s) + 45 * 60 ))
while [ "$(date +%s)" -lt "$fire_deadline" ]; do
    if tail -5 /var/log/legba_host_watchdog.log 2>/dev/null | grep -q " FIRE "; then
        log "watchdog FIRED — capturing scheduler actorTypes over the next 4 min"
        sleep 240
        docker logs legba-dapr-scheduler-1 --since "$(date -u -d '6 min ago' +%FT%TZ)" 2>&1 | grep 'Sidecar connected' | tail -6
        log "P2 PASS iff the FIRST post-FIRE line lists SourceActor/AnalystActor/TargetActor"
        break
    fi
    sleep 20
done
log "=== catch complete ==="
