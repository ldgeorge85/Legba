#!/usr/bin/env bash
# loop_healthcheck.sh — detect the "scheduler stopped recurring" stall.
#
# Symptom of the 2026-06 failure (corrupted dapr-scheduler etcd): the loop
# fires once then goes silent — no new signals/findings even though every
# container looks healthy. This catches that within minutes (cron it, e.g.
# */10), instead of discovering it days later.
#
# Exit 0 = healthy, 1 = STALLED (and prints the remediation pointer).
# Thresholds are generous (cadence is ~10-15m); tune via env if needed.
set -uo pipefail

PG="${LEGBA_PG_CONTAINER:-legba-postgres-1}"
DB="${LEGBA_DATA_PG_DB:-legba}"
SIG_MAX_MIN="${LOOP_SIG_MAX_MIN:-35}"      # alert if no signal in this many minutes
FND_MAX_MIN="${LOOP_FND_MAX_MIN:-30}"      # alert if no finding in this many minutes

q() { docker exec "$PG" psql -U legba -d "$DB" -tAc "$1" 2>/dev/null | tr -d ' '; }

sig_age=$(q "select coalesce(round(extract(epoch from now()-max(fetched_at))/60)::int, 99999) from signals")
fnd_age=$(q "select coalesce(round(extract(epoch from now()-max(created_at))/60)::int, 99999) from analyst_outputs")
now=$(date -u +%H:%M:%S)

if [ -z "$sig_age" ] || [ -z "$fnd_age" ]; then
  echo "[$now] loop_healthcheck: UNKNOWN — could not query $PG/$DB"
  exit 2
fi

if [ "$sig_age" -gt "$SIG_MAX_MIN" ] && [ "$fnd_age" -gt "$FND_MAX_MIN" ]; then
  echo "[$now] loop_healthcheck: 🔴 STALLED — newest signal ${sig_age}m ago (>${SIG_MAX_MIN}m), newest finding ${fnd_age}m ago (>${FND_MAX_MIN}m)."
  echo "  Likely the dapr-scheduler reminder-recurrence stall. Check: docker logs --since 20m legba-dapr-scheduler-1 | grep -i 'Triggering job'"
  echo "  Remediation (RUNBOOK §0, FULL wipe — partial clear makes it WORSE):"
  echo "    docker compose --profile runtime stop legba-runtime-dapr dapr-sidecar dapr-scheduler"
  echo "    rm -rf deploy/dapr-scheduler-data/* deploy/dapr-scheduler-data/.[!.]*"
  echo "    docker compose --profile runtime up -d --force-recreate dapr-scheduler dapr-sidecar legba-runtime-dapr"
  exit 1
fi

echo "[$now] loop_healthcheck: ✅ healthy — newest signal ${sig_age}m ago, newest finding ${fnd_age}m ago"
exit 0
