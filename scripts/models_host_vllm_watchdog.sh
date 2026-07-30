#!/usr/bin/env bash
# vLLM auto-restart watchdog — FOR THE MODELS HOST (ai1), not this host.
#
# The failure class (three occurrences 2026-07-28..30, ~29-30h apart): vLLM
# crashes (HarmonyError-class) while supervisord keeps reporting RUNNING —
# the port refuses connections but nothing restarts it, and every consumer
# 502s until an operator notices. Detection on the Legba host takes >=90min
# (LLM heartbeat); recovery took a half-day of operator attention twice.
# This converts that to ~3 minutes, unattended, with a page AFTER recovery.
#
# INSTALL (operator, on the models host):
#   scp scripts/models_host_vllm_watchdog.sh <models-host>:/usr/local/bin/
#   ssh <models-host> 'chmod +x /usr/local/bin/models_host_vllm_watchdog.sh'
#   then add to /etc/cron.d/ (or root crontab) on the models host:
#   */5 * * * * root /usr/local/bin/models_host_vllm_watchdog.sh
#
# CONFIG — adjust to the models host's actual values before install:
PORT="${VLLM_PORT:-8000}"                # the vLLM OpenAI-compat listen port
SUPERVISOR_PROGRAM="${VLLM_PROGRAM:-vllm}"  # supervisorctl program name
NTFY_URL="${VLLM_NTFY_URL:-}"            # optional: ntfy topic URL for the recovery page
LOG="/var/log/vllm_watchdog.log"
COOLDOWN_STAMP="/tmp/vllm_watchdog.cooldown"
COOLDOWN_SECS=900                        # never restart more than once per 15 min

log() { echo "$(date -u +%FT%TZ) [vllm-watchdog] $*" >> "$LOG"; }

# 1. Probe the actual serving port — a completions-models listing is cheap and
#    exercises the same path consumers use. supervisord's own status is NOT
#    trusted (RUNNING-over-dead is the documented failure).
if curl -sf -m 10 "http://127.0.0.1:${PORT}/v1/models" >/dev/null 2>&1; then
  rm -f "$COOLDOWN_STAMP"
  exit 0
fi

# 2. Port dead. Rate-limit restarts so a hard-crashlooping server cannot be
#    hammered — one attempt per cooldown window, then stay quiet and leave
#    the trail in the log.
if [ -f "$COOLDOWN_STAMP" ]; then
  last=$(stat -c %Y "$COOLDOWN_STAMP" 2>/dev/null || echo 0)
  if [ $(( $(date +%s) - last )) -lt "$COOLDOWN_SECS" ]; then
    log "DEAD but in cooldown — not restarting again yet"
    exit 0
  fi
fi
touch "$COOLDOWN_STAMP"

status=$(supervisorctl status "$SUPERVISOR_PROGRAM" 2>&1 | tr -s ' ')
log "DEAD port ${PORT} refused; supervisord says: ${status}; restarting"
supervisorctl restart "$SUPERVISOR_PROGRAM" >> "$LOG" 2>&1

# 3. Verify recovery (model load can take a while — poll up to 5 min).
for _ in $(seq 1 30); do
  sleep 10
  if curl -sf -m 10 "http://127.0.0.1:${PORT}/v1/models" >/dev/null 2>&1; then
    log "RECOVERED after restart"
    [ -n "$NTFY_URL" ] && curl -sf -m 10 -H "Priority: high" -H "Title: vLLM auto-recovered" \
      -d "vLLM on the models host died (supervisord said: ${status}) and was auto-restarted; serving again. Check the vllm log for the crash traceback if this recurs." \
      "$NTFY_URL" >/dev/null 2>&1
    exit 0
  fi
done
log "RESTART DID NOT RECOVER within 5 min — leaving it; next cron tick retries after cooldown"
[ -n "$NTFY_URL" ] && curl -sf -m 10 -H "Priority: max" -H "Title: vLLM restart FAILED" \
  -d "vLLM died and the automatic restart did not bring the port back within 5 minutes — manual intervention needed on the models host." \
  "$NTFY_URL" >/dev/null 2>&1
exit 1
