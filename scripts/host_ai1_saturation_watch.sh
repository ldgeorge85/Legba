#!/usr/bin/env bash
# vLLM saturation watch — FOR THE MODELS HOST (ai1), container edition.
# Task #21 (headroom visibility, plan §7 M-d): the OTHER half of the vLLM
# alarm family. models_host_vllm_watchdog.sh answers "is it ALIVE?"; this
# answers "is it DROWNING?" — the question that must go red BEFORE the
# operator raises analyst budgets (~100K plan) onto a box with no headroom.
#
# WHAT IT READS (names confirmed against the live /metrics, 2026-08-15;
# vLLM v1 engine serving gpt-oss-120b, labels {engine,model_name} summed over):
#   vllm:num_requests_running          gauge    in-batch requests
#   vllm:num_requests_waiting          gauge    queued requests (THE headroom read)
#   vllm:kv_cache_usage_perc           gauge    1.0 == 100%
#   vllm:num_preemptions_total         counter  requests evicted mid-generation
#   vllm:time_to_first_token_seconds   histogram (p95 computed over the
#                                      inter-sample bucket DELTA, not lifetime)
#
# WHEN IT PAGES (and why these three):
#   1. waiting > WAITING_BAR across STREAK_N consecutive samples — a queue
#      that survives three ticks is sustained saturation, not a burst.
#   2. kv_cache_usage_perc > KV_BAR — the cache is the hard wall; past ~0.90
#      the scheduler starts preempting and TTFT falls off a cliff.
#   3. ANY increase in num_preemptions_total — the headroom-UNSAFE signal.
#      One preemption means the box already ran out; budgets must not rise.
# TTFT p95 never pages by itself (a slow box is context, not an incident);
# it rides every log line and page body so the page arrives with the number.
#
# ALERT-ONLY by design: saturation is remediated by the OPERATOR (pause an
# analyst, hold a budget raise), never by restarting anything. This script
# touches nothing but its own state file and log.
#
# INSTALL (operator, on ai1 — see deploy/cron.d/ai1-vllm-saturation-watch):
#   1. scp scripts/host_ai1_saturation_watch.sh ai1:/usr/local/bin/ && chmod +x
#   2. scp deploy/cron.d/ai1-vllm-saturation-watch ai1:/etc/cron.d/ (mode 0644)
#   3. put NTFY_URL=<topic url> in /etc/vllm-saturation.env — NOTE: as of
#      2026-08-15 /etc/vllm-watchdog.env on ai1 carries NO NTFY_URL, so the
#      existing watchdog's pages are silent no-ops; this watch needs the URL
#      or it degrades to log-only exactly the same way.
# Disable during maintenance: touch /etc/vllm-saturation.disabled
# Verify it is running: stat -c %y /tmp/vllm_saturation.healthy
set -u
VLLM_CONTAINER="${VLLM_CONTAINER:-vllm-rtx-1}"
METRICS_URL="${METRICS_URL:-http://127.0.0.1:8000/metrics}"
LOG="${LOG:-/var/log/vllm_saturation.log}"
STATE="${STATE:-/tmp/legba-ai1-saturation.state}"
HEALTHY_STAMP="/tmp/vllm_saturation.healthy"

WAITING_BAR="${WAITING_BAR:-4}"        # queue depth that counts as saturated
STREAK_N="${STREAK_N:-3}"              # consecutive samples before it pages
KV_BAR="${KV_BAR:-0.90}"               # KV-cache share that pages immediately
COOLDOWN_SECS="${COOLDOWN_SECS:-1800}" # one page per condition per 30 min
WAIT_COOLDOWN="/tmp/vllm_saturation.wait.cooldown"
KV_COOLDOWN="/tmp/vllm_saturation.kv.cooldown"
PREEMPT_COOLDOWN="/tmp/vllm_saturation.preempt.cooldown"

# NTFY_URL comes from the env files (host-local, never in git). The watchdog's
# env is sourced first so one NTFY_URL can serve both scripts; the saturation
# env can override it.
[ -f /etc/vllm-watchdog.env ] && . /etc/vllm-watchdog.env
[ -f /etc/vllm-saturation.env ] && . /etc/vllm-saturation.env

log() { echo "$(date -u +%FT%TZ) [vllm-saturation] $*" >> "$LOG"; }
page() { # $1 priority, $2 title, $3 body — silent no-op without NTFY_URL
  [ -n "${NTFY_URL:-}" ] && curl -sf -m 10 -H "Priority: $1" -H "Title: $2" \
    -d "$3" "$NTFY_URL" >/dev/null 2>&1
}
# cooled_down <stamp> — true (0) when the stamp is fresh, i.e. SUPPRESS.
cooled_down() {
  [ -f "$1" ] || return 1
  last=$(stat -c %Y "$1" 2>/dev/null || echo 0)
  [ $(( $(date +%s) - last )) -lt "$COOLDOWN_SECS" ]
}

[ -f /etc/vllm-saturation.disabled ] && exit 0

# --- sample -------------------------------------------------------------
METRICS="$(docker exec "$VLLM_CONTAINER" sh -c \
  "curl -s -m 10 $METRICS_URL" 2>/dev/null)"
if [ -z "$METRICS" ]; then
  # A dead/unreachable server is the vllm WATCHDOG's page. One fault, one
  # alarm — this tick just records that it could not sample.
  log "SKIP no metrics from $VLLM_CONTAINER (outage is the watchdog's page)"
  exit 0
fi

# Scalars, summed across {engine,model_name} label sets so a model swap or a
# second engine never breaks the parse. kv is MAX (a full cache on any engine
# is the wall). The waiting match is anchored before '{' so the sibling
# metric num_requests_waiting_by_reason can never double-count into it.
read -r RUNNING WAITING KV PREEMPT <<EOF
$(printf '%s\n' "$METRICS" | awk '
  /^vllm:num_requests_running[{ ]/  { run += $NF }
  /^vllm:num_requests_waiting[{ ]/  { wait += $NF }
  /^vllm:kv_cache_usage_perc[{ ]/   { if ($NF > kv) kv = $NF }
  /^vllm:num_preemptions_total[{ ]/ { pre += $NF }
  END { printf "%d %d %.4f %d\n", run+0, wait+0, kv+0, pre+0 }')
EOF

# TTFT histogram: cumulative bucket counts keyed by le, summed across labels,
# serialized "le:count le:count ..." sorted by le so prev/cur align.
CUR_BUCKETS="$(printf '%s\n' "$METRICS" | awk '
  /^vllm:time_to_first_token_seconds_bucket\{/ {
    le = $0; sub(/.*le="/, "", le); sub(/".*/, "", le)
    if (le == "+Inf") le = "inf"
    b[le] += $NF
  }
  END {
    n = 0
    for (le in b) key[++n] = le
    # numeric sort with inf last
    for (i = 1; i <= n; i++) for (j = i+1; j <= n; j++) {
      a = (key[i] == "inf") ? 1e18 : key[i]+0
      c = (key[j] == "inf") ? 1e18 : key[j]+0
      if (a > c) { t = key[i]; key[i] = key[j]; key[j] = t }
    }
    for (i = 1; i <= n; i++) printf "%s%s:%d", (i>1 ? " " : ""), key[i], b[key[i]]
    printf "\n"
  }')"

# --- state --------------------------------------------------------------
PREV_PREEMPT=""
PREV_STREAK=0
PREV_BUCKETS=""
if [ -f "$STATE" ]; then
  PREV_PREEMPT="$(sed -n 's/^preempt=//p' "$STATE" | head -1)"
  PREV_STREAK="$(sed -n 's/^streak=//p' "$STATE" | head -1)"
  PREV_BUCKETS="$(sed -n 's/^buckets=//p' "$STATE" | head -1)"
fi
case "$PREV_STREAK" in ''|*[!0-9]*) PREV_STREAK=0;; esac

# TTFT p95 over the inter-sample DELTA (this tick's traffic, not the server's
# lifetime — a lifetime p95 dilutes today's stall under last week's health).
# Linear interpolation inside the deciding bucket; counter reset (any cur <
# prev) falls back to the lifetime histogram for one tick.
TTFT_P95="$(awk -v prev="$PREV_BUCKETS" -v cur="$CUR_BUCKETS" 'BEGIN {
  np = split(prev, p, " "); nc = split(cur, c, " ")
  for (i = 1; i <= np; i++) { split(p[i], kv, ":"); pb[kv[1]] = kv[2] }
  reset = 0; total = 0
  for (i = 1; i <= nc; i++) {
    split(c[i], kv, ":"); le[i] = kv[1]
    d[i] = kv[2] - (le[i] in pb ? pb[le[i]] : 0)
    if (d[i] < 0) reset = 1
  }
  if (reset || np == 0) for (i = 1; i <= nc; i++) { split(c[i], kv, ":"); d[i] = kv[2] }
  if (nc == 0 || d[nc] <= 0) { print "n/a"; exit }
  target = 0.95 * d[nc]; lo = 0
  for (i = 1; i <= nc; i++) {
    if (d[i] >= target) {
      hi = (le[i] == "inf") ? lo : le[i] + 0
      dcount = d[i] - (i > 1 ? d[i-1] : 0)
      base = (i > 1 ? d[i-1] : 0)
      if (dcount <= 0 || le[i] == "inf") { printf "%.2fs", hi; exit }
      printf "%.2fs", lo + (hi - lo) * (target - base) / dcount; exit
    }
    lo = (le[i] == "inf") ? lo : le[i] + 0
  }
  print "n/a"
}')"

# --- judgments ----------------------------------------------------------
STREAK=0
if [ "$WAITING" -gt "$WAITING_BAR" ]; then STREAK=$(( PREV_STREAK + 1 )); fi

PREEMPT_DELTA=0
if [ -n "$PREV_PREEMPT" ] && [ "$PREEMPT" -gt "$PREV_PREEMPT" ] 2>/dev/null; then
  PREEMPT_DELTA=$(( PREEMPT - PREV_PREEMPT ))
fi
# cur < prev = counter reset (container restart) — a new baseline, not a page.

KV_OVER=$(awk -v kv="$KV" -v bar="$KV_BAR" 'BEGIN { print (kv > bar) ? 1 : 0 }')

log "sample running=$RUNNING waiting=$WAITING kv=$KV preempt_total=$PREEMPT preempt_delta=$PREEMPT_DELTA streak=$STREAK ttft_p95=${TTFT_P95}"

if [ "$STREAK" -ge "$STREAK_N" ]; then
  if ! cooled_down "$WAIT_COOLDOWN"; then
    log "FIRE sustained queue: waiting=$WAITING > $WAITING_BAR for $STREAK samples"
    page high "ai1 vLLM saturated: queue" \
      "num_requests_waiting=$WAITING (> $WAITING_BAR) for $STREAK consecutive samples; running=$RUNNING kv_cache=$KV ttft_p95=${TTFT_P95}. The box is scheduling more than it can batch — hold any budget raise and consider pausing a heavy analyst."
    touch "$WAIT_COOLDOWN"
  fi
else
  [ "$STREAK" -eq 0 ] && rm -f "$WAIT_COOLDOWN"
fi

if [ "$KV_OVER" = "1" ]; then
  if ! cooled_down "$KV_COOLDOWN"; then
    log "FIRE kv cache: $KV > $KV_BAR"
    page high "ai1 vLLM saturated: KV cache" \
      "kv_cache_usage_perc=$KV (> $KV_BAR); running=$RUNNING waiting=$WAITING ttft_p95=${TTFT_P95}. Past this line the scheduler preempts and latency cliffs — the next signal is preemptions, and that one means headroom is GONE."
    touch "$KV_COOLDOWN"
  fi
else
  rm -f "$KV_COOLDOWN"
fi

if [ "$PREEMPT_DELTA" -gt 0 ]; then
  if ! cooled_down "$PREEMPT_COOLDOWN"; then
    log "FIRE preemption: +$PREEMPT_DELTA (total $PREEMPT)"
    page max "ai1 vLLM PREEMPTING (headroom unsafe)" \
      "num_preemptions_total rose by $PREEMPT_DELTA (now $PREEMPT). The engine evicted running requests — the box already ran OUT of headroom at current load. Do NOT raise analyst budgets; kv_cache=$KV waiting=$WAITING ttft_p95=${TTFT_P95}."
    touch "$PREEMPT_COOLDOWN"
  fi
fi

# --- persist ------------------------------------------------------------
{
  echo "preempt=$PREEMPT"
  echo "streak=$STREAK"
  echo "buckets=$CUR_BUCKETS"
} > "$STATE"
touch "$HEALTHY_STAMP"
exit 0
