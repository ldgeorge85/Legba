#!/usr/bin/env bash
# vLLM auto-recovery watchdog — FOR THE MODELS HOST, container edition.
#
# Failure history this codifies (2026-07-28..30, four incidents):
#   MODE A — the model server dies inside its container while supervisord
#            (which runs INSIDE the container) reports RUNNING. In-container
#            probe refuses; everything external 404/502s.
#   MODE B — the model server is HEALTHY but a container restart changed its
#            network address and the aiproxy layer in front kept resolving
#            the stale upstream: external 404/502 against a live model
#            (2026-07-30 16:05, four hours of fleet outage over a proxy
#            that needed a bounce).
# Therefore: probe BOTH layers, remediate the RIGHT one, and after ANY
# model-container restart ALWAYS bounce the proxy layer too.
#
# SAFETY CONTRACT (operator-required):
#   * Restarts are a clean container STOP (generous timeout) then START —
#     never kill, never rm. Name-pinned: touches ONLY $VLLM_CONTAINER and
#     containers matching ^aiproxy. Nothing else on the shared host, ever.
#   * One remediation per $COOLDOWN_SECS, then hands-off until the next
#     window (a crash-looping model is a human's problem, loudly).
#   * Every action logged; ntfy page AFTER recovery (or on failure), never
#     a page that asks a human to do the restart themselves.
#
# INSTALL (on the models host):
#   1. copy this script to /usr/local/bin/ and chmod +x it
#   2. create /etc/vllm-watchdog.env with (values are host-local, not in git):
#        PROXY_PROBE_URL="https://<the public api hostname>/v1/models"
#        NTFY_URL="<optional ntfy topic url for pages>"
#   3. cron:  */5 * * * * root /usr/local/bin/models_host_vllm_watchdog.sh
#
VLLM_CONTAINER="${VLLM_CONTAINER:-vllm-rtx-1}"
LOG="/var/log/vllm_watchdog.log"
COOLDOWN_STAMP="/tmp/vllm_watchdog.cooldown"
COOLDOWN_SECS=1200                    # one remediation per 20 min, max
MODEL_LOAD_WAIT_SECS=900              # a 120B takes minutes to load
[ -f /etc/vllm-watchdog.env ] && . /etc/vllm-watchdog.env

log() { echo "$(date -u +%FT%TZ) [vllm-watchdog] $*" >> "$LOG"; }
page() { # $1 priority, $2 title, $3 body — silent no-op without NTFY_URL
  [ -n "${NTFY_URL:-}" ] && curl -sf -m 10 -H "Priority: $1" -H "Title: $2" \
    -d "$3" "$NTFY_URL" >/dev/null 2>&1
}

# --- probes -----------------------------------------------------------------
# In-container: 200/401/403 all mean "listening" (401/403 = auth-gated, alive).
container_alive() {
  code=$(docker exec "$VLLM_CONTAINER" sh -c \
    'curl -s -m 10 -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/v1/models' 2>/dev/null)
  case "$code" in 200|401|403) return 0;; *) return 1;; esac
}
proxy_alive() {
  [ -z "${PROXY_PROBE_URL:-}" ] && return 0   # unset ⇒ skip layer-2 checks
  code=$(curl -s -m 15 -o /dev/null -w "%{http_code}" "$PROXY_PROBE_URL" 2>/dev/null)
  case "$code" in 200|401|403) return 0;; *) return 1;; esac
}
bounce_proxies() {
  for c in $(docker ps --format '{{.Names}}' | grep -E '^aiproxy'); do
    docker restart "$c" >> "$LOG" 2>&1 && log "bounced proxy $c"
  done
}

# --- healthy fast-path ------------------------------------------------------
# The stamp is the liveness proof: without it, "all healthy" and "cron never
# fires" are indistinguishable (the log is written only on unhealthy ticks).
if container_alive && proxy_alive; then
  touch /tmp/vllm_watchdog.healthy
  rm -f "$COOLDOWN_STAMP"
  exit 0
fi

# --- cooldown gate ----------------------------------------------------------
if [ -f "$COOLDOWN_STAMP" ]; then
  last=$(stat -c %Y "$COOLDOWN_STAMP" 2>/dev/null || echo 0)
  if [ $(( $(date +%s) - last )) -lt "$COOLDOWN_SECS" ]; then
    log "UNHEALTHY but in cooldown — no action this tick"
    exit 0
  fi
fi
touch "$COOLDOWN_STAMP"

# --- remediation ladder -----------------------------------------------------
if ! container_alive; then
  log "MODE A: model server dead in-container — clean stop/start of $VLLM_CONTAINER"
  docker stop -t 90 "$VLLM_CONTAINER" >> "$LOG" 2>&1
  docker start "$VLLM_CONTAINER" >> "$LOG" 2>&1
  waited=0
  until container_alive; do
    sleep 15; waited=$((waited+15))
    if [ "$waited" -ge "$MODEL_LOAD_WAIT_SECS" ]; then
      log "RESTART DID NOT RECOVER within ${MODEL_LOAD_WAIT_SECS}s — leaving it"
      page max "vLLM restart FAILED" "The model container was stop/started but the server did not come back within $((MODEL_LOAD_WAIT_SECS/60)) minutes — manual intervention needed."
      exit 1
    fi
  done
  log "model server back after ${waited}s; bouncing proxies (mandatory post-restart)"
  bounce_proxies
  sleep 20
  if proxy_alive; then
    log "RECOVERED end-to-end (mode A)"
    page high "vLLM auto-recovered" "Model container was dead (supervisord RUNNING-over-dead), clean stop/start + proxy bounce brought it back in ${waited}s. Check the vllm log for the crash cause if this repeats."
  else
    log "model alive but proxy path still dead after bounce"
    page max "vLLM up, proxy path still dead" "The model recovered but the external path did not — the proxy layer needs a human look."
  fi
  exit 0
fi

# Container alive ⇒ MODE B: stale proxy layer.
log "MODE B: model healthy, external path dead — bouncing proxies only"
bounce_proxies
sleep 20
if proxy_alive; then
  log "RECOVERED end-to-end (mode B — stale proxy after a container address change)"
  page high "vLLM path auto-recovered" "The model was healthy the whole time; the proxy layer had gone stale (the 2026-07-30 mode) and was bounced."
else
  log "proxy bounce did not restore the path"
  page max "vLLM path dead, bounce insufficient" "Model healthy, proxy bounced, external path still dead — manual look needed."
fi
exit 0
