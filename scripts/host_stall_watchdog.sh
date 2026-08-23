#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# host_stall_watchdog.sh — the HOST-SIDE auto-recover actuator for the Dapr
# actor-plane stall (B0-12 remainder).
#
# WHY THIS EXISTS: twice (2026-07-14, 2026-07-15) the sidecar's actor plane
# degraded silently — reminders/invokes stopped, ingestion + every analyst
# cadence died, yet every container stayed "healthy". The IN-CONTAINER
# liveness_watchdog detects this and writes a durable alert, but it has no
# actuator (no docker socket) — the second stall cost ~39h of downtime that a
# 2-minute restart fixes. This script is the actuator: a root cron (every 5
# min) that checks the freshest signal's age straight from Postgres and, when
# the pipeline is provably dead, executes the operator-approved recovery
# recipe — restart the sidecar, then the runtime
# (RESTART, never recreate: recreate churn is itself implicated in degrading
# the actor plane).
#
# RUST-4 MOTHBALL (2026-08-21): the dapr-workflow worker used to be the third
# leg of this recipe (the 2026-07-18..21 soak had credited it as the recovery
# differentiator, 8/8) — dropped here now that the GEPA optimizer plane is
# mothballed (docs/SEAMS.md #53, planning/RUST4_EVIDENCE_2026-08-21.md): the
# worker container stays deployed (it also hosts deep_consult_workflow — see
# docker-compose.yml), but "deep" consult mode had 2 sessions in the 6 weeks
# the evidence was gathered, and the worker's earlier restart-differentiator
# role is attributed to the optimizer's actor-plane load, not deep_consult's.
# If that attribution turns out wrong, re-add `docker restart
# "$WORKER_CONTAINER"` alongside the sidecar/runtime restarts below, citing
# the same evidence file.
#
# SAFETY LADDER (every rung must pass before a restart):
#   1. /etc/legba-watchdog.disabled exists           -> skip (maintenance flag)
#   2. postgres/sidecar/runtime not all running      -> skip (deploy/operator action)
#   3. runtime started < GRACE_SECS ago              -> skip (warmup window)
#   4. the age query itself fails                    -> log only, NEVER restart
#                                                       (fault not proven to be the actor plane)
#   5. freshest signal younger than MAX_AGE_SECS     -> healthy, exit silent
#   6. last auto-restart < COOLDOWN_SECS ago         -> ESCALATE in the log, do
#                                                       NOT restart again (a loop
#                                                       of restarts = the churn
#                                                       that causes stalls)
#   7. fire: restart sidecar -> sleep -> restart runtime, stamp the cooldown,
#      write a durable alert_sink_deliveries row (status='auto_recovered') so
#      the recovery is visible in the escalations panel next to the
#      in-container watchdog's 'logged_only' stall row.
#
# Logs: events only (skips/errors/fires) to stdout — cron redirects to
# /var/log/legba_host_watchdog.log. A heartbeat stamp file's mtime proves the
# cron itself is alive even when the log is silent.
#
# Install (documented in docs/RUNBOOK.md):
#   /etc/cron.d/legba-watchdog:
#     */5 * * * * root /usr/local/deployments/active/legba/scripts/host_stall_watchdog.sh >> /var/log/legba_host_watchdog.log 2>&1
# Disable during maintenance:  touch /etc/legba-watchdog.disabled
# Dry run (no restart, no DB write):  DRY_RUN=1 ./host_stall_watchdog.sh

set -u

# --- config (env-overridable) ------------------------------------------------
MAX_AGE_SECS="${MAX_AGE_SECS:-1800}"        # 30 min: healthy p100 inter-signal gap
                                            # measured <=18 min over 7 days (2026-07-21
                                            # review) — every gap beyond that was a stall
COOLDOWN_SECS="${COOLDOWN_SECS:-2700}"      # 45 min between auto-restarts
GRACE_SECS="${GRACE_SECS:-900}"             # 15 min post-start warmup, hands off
DRY_RUN="${DRY_RUN:-0}"
STATE_DIR="${STATE_DIR:-/var/lib/legba-watchdog}"
DISABLE_FLAG="/etc/legba-watchdog.disabled"

PG_CONTAINER="legba-postgres-1"
SIDECAR_CONTAINER="legba-dapr-sidecar-1"
RUNTIME_CONTAINER="legba-legba-runtime-dapr-1"
# WORKER_CONTAINER dropped from the recovery recipe — RUST-4 mothball
# (2026-08-21), see the header comment.
PG_USER="legba"
PG_DB="legba"

now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "$(now_iso) $*"; }

mkdir -p "$STATE_DIR"
HEARTBEAT="$STATE_DIR/heartbeat"
COOLDOWN_STAMP="$STATE_DIR/last_restart"
LOCK="$STATE_DIR/lock"

# Single-flight: a hung previous run must not stack restarts.
exec 9>"$LOCK"
if ! flock -n 9; then
    log "SKIP another watchdog run holds the lock"
    exit 0
fi

touch "$HEARTBEAT"

# --- rung 1: maintenance flag -----------------------------------------------
if [ -e "$DISABLE_FLAG" ]; then
    log "SKIP disabled via $DISABLE_FLAG"
    exit 0
fi

# --- rung 2: all three containers must be RUNNING ----------------------------
for c in "$PG_CONTAINER" "$SIDECAR_CONTAINER" "$RUNTIME_CONTAINER"; do
    state="$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null | tr -d '[:space:]')"
    state="${state:-missing}"
    if [ "$state" != "true" ]; then
        log "SKIP container $c not running (state=$state) — deploy or operator action in progress"
        exit 0
    fi
done

# --- rung 3: runtime warmup grace --------------------------------------------
started="$(docker inspect -f '{{.State.StartedAt}}' "$RUNTIME_CONTAINER" 2>/dev/null || echo '')"
if [ -n "$started" ]; then
    started_epoch="$(date -d "$started" +%s 2>/dev/null || echo 0)"
    if [ "$started_epoch" -gt 0 ] && [ $(( $(date +%s) - started_epoch )) -lt "$GRACE_SECS" ]; then
        log "SKIP runtime started ${started} (<${GRACE_SECS}s ago) — warmup grace"
        exit 0
    fi
fi

# --- rung 4+5: the freshest-signal age, straight from Postgres ---------------
age="$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tAc \
    "SELECT COALESCE(EXTRACT(EPOCH FROM (now() - max(fetched_at)))::int, 999999) FROM signals;" 2>/dev/null)"
case "$age" in
    ''|*[!0-9]*)
        log "ERROR age query failed (got '${age:-}') — NOT restarting (fault unproven)"
        exit 0
        ;;
esac
if [ "$age" -lt "$MAX_AGE_SECS" ]; then
    exit 0   # healthy — silent
fi

# --- rung 6: cooldown / escalate ----------------------------------------------
if [ -e "$COOLDOWN_STAMP" ]; then
    last="$(stat -c %Y "$COOLDOWN_STAMP" 2>/dev/null || echo 0)"
    if [ $(( $(date +%s) - last )) -lt "$COOLDOWN_SECS" ]; then
        log "ESCALATE stall persists (age=${age}s) but last auto-restart <${COOLDOWN_SECS}s ago — restart did NOT recover the pipeline; a human is needed"
        exit 0
    fi
fi

# --- rung 7: fire the documented recovery ------------------------------------
# ORDER IS LOAD-BEARING (B0-13 P2, scheduler-log-proven 12/12 on 2026-07-23):
# the old sidecar-first order made the sidecar re-report its host to placement
# BEFORE the slow Python app bound :6090 (cold Init ~19s), so the placement
# table published with ONLY the workflow-engine actor types — business actors
# absent, reminders had no host, and the first restart ALWAYS failed while an
# identical second restart (app image hot in page cache, <1s boot) always won.
# It was never the delay and never the worker. Fix-2a: restart the RUNTIME
# first, WAIT for its healthcheck, then the sidecar (which now reports a host
# that already answers with the full actor set). (The worker was the third
# leg here; dropped RUST-4 2026-08-21 — see the header comment.)
if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN WOULD-RESTART pipeline stalled (freshest signal ${age}s > ${MAX_AGE_SECS}s): $RUNTIME_CONTAINER first (wait healthy), then $SIDECAR_CONTAINER"
    exit 0
fi

log "FIRE pipeline stalled (freshest signal ${age}s > ${MAX_AGE_SECS}s) — P2 order: $RUNTIME_CONTAINER first (wait healthy), then $SIDECAR_CONTAINER"
docker restart "$RUNTIME_CONTAINER" >/dev/null 2>&1
# Bounded wait for the app to answer before the sidecar re-reports its host to
# placement. Timeout proceeds anyway — the verify-recovery rung backstops.
hs=""
for _i in $(seq 1 30); do
    hs="$(docker inspect -f '{{.State.Health.Status}}' "$RUNTIME_CONTAINER" 2>/dev/null)"
    [ "$hs" = "healthy" ] && break
    sleep 5
done
log "FIRE runtime health after wait: ${hs:-unknown}"
docker restart "$SIDECAR_CONTAINER" >/dev/null 2>&1
touch "$COOLDOWN_STAMP"

# VERIFY-RECOVERY: wait for warmup + the first source polls, re-read the age,
# and if flow has NOT resumed repeat the pair ONCE, cooldown-exempt. This rung
# is the backstop for the day the first fire stops being sufficient. One
# retry only — a loop of restarts IS the churn.
VERIFY_WAIT="${VERIFY_WAIT:-720}"   # 12 min: warmup grace (~5) + poll headroom
sleep "$VERIFY_WAIT"
age2="$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tAc \
    "SELECT COALESCE(EXTRACT(EPOCH FROM (now() - max(fetched_at)))::int, 999999) FROM signals;" 2>/dev/null)"
case "$age2" in
    ''|*[!0-9]*)
        log "WARN verify-recovery age query failed (got '${age2:-}') — no retry (fault unproven)"
        ;;
    *)
        if [ "$age2" -lt "$VERIFY_WAIT" ]; then
            log "VERIFIED recovery took (freshest signal ${age2}s old)"
        else
            log "RETRY first restart did NOT recover (age still ${age2}s) — repeating: sidecar + runtime"
            docker restart "$SIDECAR_CONTAINER" >/dev/null 2>&1
            sleep 5
            docker restart "$RUNTIME_CONTAINER" >/dev/null 2>&1
            touch "$COOLDOWN_STAMP"
        fi
        ;;
esac

# Durable, operator-visible recovery row (fail-safe: a failed insert never
# undoes the recovery — log and move on).
payload="{\"kind\":\"pipeline_stall_auto_recovery\",\"age_seconds\":${age},\"threshold_seconds\":${MAX_AGE_SECS},\"recovered_at\":\"$(now_iso)\",\"recipe\":\"restart sidecar + runtime (host_stall_watchdog)\"}"
if ! docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -qc \
    "INSERT INTO alert_sink_deliveries (sink_kind, sink_target, status, channel_name, severity, payload_summary)
     VALUES ('host_watchdog', 'operator', 'auto_recovered', 'liveness_stall', 'high', '${payload}'::jsonb);" >/dev/null 2>&1; then
    log "WARN recovery succeeded but the alert_sink_deliveries insert failed"
fi

log "RECOVERED restart issued; runtime will re-warm (~2 min). Cooldown ${COOLDOWN_SECS}s armed."
exit 0
