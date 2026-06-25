#!/usr/bin/env bash
# loop_watchdog.sh — detect the scheduler reminder-recurrence stall AND auto-remediate.
#
# The Dapr scheduler can silently stop dispatching recurring reminders (the
# 2026-06 stall). This watchdog (cron */10) detects the symptom — no fresh
# signals/findings — and recovers with a COORDINATED control-plane restart
# (the proven fix; no etcd wipe needed, a restart re-kicks dispatch).
#
# Guards:
#   * cooldown — at most one auto-restart per REMEDIATE_COOLDOWN_MIN, so a
#     genuinely-broken stack isn't restart-looped.
#   * kill-switch — `touch /tmp/legba_watchdog_off` disables auto-remediation
#     (it then only detects + logs), e.g. during a planned maintenance/fan-out.
# Detection thresholds are generous (cadence ~10-15m).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

PG="${LEGBA_PG_CONTAINER:-legba-postgres-1}"
DB="${LEGBA_DATA_PG_DB:-legba}"
SIG_MAX_MIN="${LOOP_SIG_MAX_MIN:-22}"
FND_MAX_MIN="${LOOP_FND_MAX_MIN:-22}"
COOLDOWN_MIN="${REMEDIATE_COOLDOWN_MIN:-30}"
MARKER=/tmp/legba_watchdog_last_remediate
now=$(date -u +%H:%M:%S)

q() { docker exec "$PG" psql -U legba -d "$DB" -tAc "$1" 2>/dev/null | tr -d ' '; }
sig_age=$(q "select coalesce(round(extract(epoch from now()-max(fetched_at))/60)::int,99999) from signals")
fnd_age=$(q "select coalesce(round(extract(epoch from now()-max(created_at))/60)::int,99999) from analyst_outputs")
[ -z "${sig_age:-}" ] && { echo "[$now] watchdog: UNKNOWN (db unreachable)"; exit 2; }

if [ "$sig_age" -le "$SIG_MAX_MIN" ] || [ "$fnd_age" -le "$FND_MAX_MIN" ]; then
  echo "[$now] watchdog: ✅ healthy (signal ${sig_age}m, finding ${fnd_age}m)"; exit 0
fi

echo "[$now] watchdog: 🔴 STALLED — signal ${sig_age}m, finding ${fnd_age}m (>${SIG_MAX_MIN}m)"
if [ -f /tmp/legba_watchdog_off ]; then echo "[$now] auto-remediate DISABLED (/tmp/legba_watchdog_off); detect-only."; exit 1; fi
# cooldown guard
if [ -f "$MARKER" ]; then
  since=$(( ($(date -u +%s) - $(date -u -r "$MARKER" +%s)) / 60 ))
  if [ "$since" -lt "$COOLDOWN_MIN" ]; then echo "[$now] remediated ${since}m ago (<${COOLDOWN_MIN}m cooldown) — not restarting; needs operator."; exit 1; fi
fi
echo "[$now] auto-remediating: coordinated control-plane restart (no etcd wipe)…"
date -u +%s > "$MARKER"
docker compose --profile runtime up -d --force-recreate dapr-placement dapr-scheduler dapr-sidecar legba-runtime-dapr >>/var/log/legba_loop_health.log 2>&1
echo "[$now] restart issued; recurrence should resume within a cron cycle. If it recurs repeatedly, full-wipe etcd per RUNBOOK §0."
exit 1
