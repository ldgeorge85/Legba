#!/usr/bin/env bash
# host_llm_heartbeat.sh — the LLM-plane silence alarm (§21 follow-up, 2026-07-29).
#
# THE GAP THIS CLOSES: the pipeline watchdog watches SIGNALS, which keep
# flowing when only the LLM endpoint dies — so a 9h core-model outage
# (2026-07-29 07:06Z, vLLM HarmonyError crash with supervisord still
# reporting RUNNING) was invisible to every existing alarm. This checks the
# other heartbeat: has ANY LLM-bearing analyst completed a successful run
# recently? Deterministic analysts are excluded — they run fine through an
# LLM outage, which is exactly why signal-freshness cannot see this class.
#
# ALERT-ONLY by design: the fix lives on a REMOTE host (the models box), so
# this never restarts anything — it pages the operator via the local ntfy
# (web UI) and logs. Rate-limited to one page per cooldown window.
# Cron: */10 * * * *  (ride the same /etc/cron.d/legba-watchdog file).
set -u
PG_CONTAINER="${PG_CONTAINER:-legba-postgres-1}"
PG_USER="${PG_USER:-legba}"; PG_DB="${PG_DB:-legba}"
MAX_LLM_AGE_SECS="${MAX_LLM_AGE_SECS:-5400}"   # 90 min: units tick ~30min; 3 misses = real
NTFY_URL="${NTFY_URL:-http://127.0.0.1:8093/legba-alerts}"
COOLDOWN_STAMP="/tmp/legba-llm-heartbeat.cooldown"
COOLDOWN_SECS="${COOLDOWN_SECS:-3600}"
LOG="/var/log/legba-watchdog.log"
log() { echo "$(date -u +%FT%TZ) [llm-heartbeat] $*" >> "$LOG"; }

[ -f /etc/legba-watchdog.disabled ] && exit 0

age="$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tAc "
  SELECT COALESCE(EXTRACT(EPOCH FROM (now() - max(run_started_at)))::int, 999999)
  FROM analyst_traces
  WHERE status='success'
    AND analyst_id NOT IN ('claim_watch','signal_embedder','cross_source_dedup',
      'entity_resolution','corpus_indexer','finding_supersession','entity_gc',
      'alert_trigger_scan','geo_convergence_scan','band_calibration_tracker',
      'fact_decay_scan','nexus_decay','source_track_record','desk_baseline',
      'narrative_mapper','analyst_traces_retention','signals_retention',
      'evidence_archiver','fact_contention_arbiter','situation_clustering',
      'integrity_sweep','composition_lineage_sweep','cross_source_coalesce',
      'signal_summarizer','reenrich_ner','reenrich_translation',
      'indicator_tracker','thematic_proposal','signal_salience')" 2>/dev/null | tr -d ' ')"
# signal_salience is excluded despite being LLM-bearing: it degrades GRACEFULLY
# during an endpoint outage (status=success with all scores drained), so its
# success is not evidence the LLM plane is alive — it masked the 2026-07-30
# outage for 2.5h. indicator_tracker/thematic_proposal are deterministic
# handlers added after this list was first written.
[ -z "$age" ] && { log "SKIP could not read analyst_traces"; exit 0; }
if [ "$age" -lt "$MAX_LLM_AGE_SECS" ]; then rm -f "$COOLDOWN_STAMP"; exit 0; fi

if [ -f "$COOLDOWN_STAMP" ]; then
  last=$(stat -c %Y "$COOLDOWN_STAMP" 2>/dev/null || echo 0)
  [ $(( $(date +%s) - last )) -lt "$COOLDOWN_SECS" ] && exit 0
fi
log "FIRE llm plane silent: newest successful LLM-analyst run ${age}s ago (> ${MAX_LLM_AGE_SECS}s)"
curl -s -m 10 -H "X-Title: Legba: LLM plane silent" -H "X-Priority: 5" -H "X-Tags: rotating_light" \
  -d "No LLM-bearing analyst has completed a run in $((age/60)) min while the signal pipeline flows — the core model endpoint is the first suspect (vLLM on the models host; check supervisorctl status vs the actual port, RUNNING-but-dead is the known failure). Deterministic analysts unaffected." \
  "$NTFY_URL" >/dev/null 2>&1 || log "ntfy send failed"
touch "$COOLDOWN_STAMP"
