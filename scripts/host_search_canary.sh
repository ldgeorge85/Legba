#!/usr/bin/env bash
# host_search_canary.sh — the SEARCH-plane liveness canary (SEAMS #50, 2026-07-29).
#
# THE GAP THIS CLOSES: host_llm_heartbeat.sh watches the CORE LLM plane; it has
# no visibility into the `search_provider` stack family at all — a dead SearXNG
# engine set is invisible to every existing host-side alarm. The control probe
# that measures this already exists (`verify_engine_liveness`,
# src/legba/data/stack/search/liveness.py) and already fires REACTIVELY, on a
# web_search call that gets zero results. What is still missing is the
# SCHEDULED half: a low-cadence job that calls it with `force=True` on its own
# clock, so the verdict is fresh BEFORE an analyst needs it, not only after one
# hits an empty (ARCHITECTURE.md §14 / docs/SEAMS.md #50). This script is that
# scheduled half, run host-side against the live runtime container exactly the
# way host_llm_heartbeat.sh docker-execs into its own target container.
#
# ALERT-ONLY by design: the fix (repairing SearXNG / the registered
# search_provider component) is an operator action, so this never restarts
# anything — it only probes, logs, and pages via the local ntfy (web UI).
# Pages only on TWO CONSECUTIVE failed probes (a persisted streak counter, not
# a single blip) and is rate-limited to one page per cooldown window after
# that.
#
# Cron (NOT installed by this script — host-side install stays with the main
# session): */15 * * * *  (ride the same /etc/cron.d/legba-watchdog file as
# host_llm_heartbeat.sh; 15 min keeps the probe budget well under one query
# per SearXNG-goodwill-conscious hour while still catching an outage fast).
#   */15 * * * * root /usr/local/deployments/active/legba/scripts/host_search_canary.sh
set -u
RUNTIME_CONTAINER="${RUNTIME_CONTAINER:-legba-legba-runtime-dapr-1}"
SEARCH_COMPONENT_ID="${SEARCH_COMPONENT_ID:-search.searxng.local}"
NTFY_URL="${NTFY_URL:-http://127.0.0.1:8093/legba-alerts}"
NTFY_TOKEN="${LEGBA_ALERT_NTFY_TOKEN:-}"   # optional bearer token; NEVER logged/printed
STATE_FILE="/tmp/legba-search-canary.streak"
COOLDOWN_STAMP="/tmp/legba-search-canary.cooldown"
COOLDOWN_SECS="${COOLDOWN_SECS:-3600}"
FAIL_THRESHOLD="${FAIL_THRESHOLD:-2}"      # consecutive not-live probes before paging
LOG="/var/log/legba-watchdog.log"
log() { echo "$(date -u +%FT%TZ) [search-canary] $*" >> "$LOG"; }

[ -f /etc/legba-watchdog.disabled ] && exit 0

# The probe itself: resolve the registered `search_provider` stack component
# exactly the way the runtime does (build_search_handler_from_stack_component,
# the same factory `dapr_host.py`'s ToolContext.search binding uses), then
# call verify_engine_liveness(..., force=True) — bypassing the freshness
# cache, since this IS the scheduled refresh. Prints "<verdict>|<detail>" on
# stdout (verdict is one of LivenessVerdict's live/dead/unverified/
# probe_failed); any resolution failure prints "dead" alone so the shell side
# treats "could not even build the handler" the same as "built it and it's
# dead" — both license the same conclusion: the plane cannot be trusted.
read -r -d '' PYPROBE <<'PYEOF' || true
import asyncio, os, sys

async def main():
    from legba.data.postgres import PostgresStore
    from legba.data.registry.credentials import CredentialVault
    from legba.runtime.registry_client import RegistryHTTPClient
    from legba.runtime.analyst_deps_builder import (
        build_search_handler_from_stack_component,
    )
    from legba.data.stack.search import verify_engine_liveness

    component_id = os.environ.get("SEARCH_COMPONENT_ID", "search.searxng.local")
    store = PostgresStore.from_env()
    await store.connect()
    try:
        vault = CredentialVault(store)
        registry_client = RegistryHTTPClient()
        try:
            handler = await build_search_handler_from_stack_component(
                component_id,
                registry_client=registry_client,
                secrets_resolve=vault.resolve,
            )
        except Exception as exc:
            print(f"handler build failed: {exc}", file=sys.stderr)
            print("dead")
            return
        verdict, detail = await verify_engine_liveness(
            handler, provider_key="host_search_canary", force=True,
        )
        print(f"{verdict.value}|{detail}")
    finally:
        await store.close()

asyncio.run(main())
PYEOF

out="$(docker exec -e SEARCH_COMPONENT_ID="$SEARCH_COMPONENT_ID" "$RUNTIME_CONTAINER" \
  python3 -c "$PYPROBE" 2>>"$LOG")"
verdict="${out%%|*}"

if [ "$verdict" = "live" ]; then
  rm -f "$STATE_FILE" "$COOLDOWN_STAMP"
  exit 0
fi

# Not live (dead / unverified / probe_failed / empty — docker exec itself
# failing counts too, since $out is then empty and $verdict != "live").
# Bump the persisted consecutive-failure streak; page only at the threshold.
streak=0
[ -f "$STATE_FILE" ] && streak="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
case "$streak" in ''|*[!0-9]*) streak=0 ;; esac
streak=$((streak + 1))
echo "$streak" > "$STATE_FILE"
log "PROBE not-live verdict=${verdict:-<none>} detail=${out#*|} streak=$streak"

[ "$streak" -lt "$FAIL_THRESHOLD" ] && exit 0

if [ -f "$COOLDOWN_STAMP" ]; then
  last=$(stat -c %Y "$COOLDOWN_STAMP" 2>/dev/null || echo 0)
  [ $(( $(date +%s) - last )) -lt "$COOLDOWN_SECS" ] && exit 0
fi

log "FIRE search plane dead: ${streak} consecutive failed probes (verdict=${verdict:-<none>})"
headers=(-H "X-Title: Legba: search plane dead" -H "X-Priority: 5" -H "X-Tags: rotating_light")
[ -n "$NTFY_TOKEN" ] && headers+=(-H "Authorization: Bearer $NTFY_TOKEN")
curl -s -m 10 "${headers[@]}" \
  -d "The search-plane control probe has failed ${streak} consecutive times (verdict=${verdict:-<none>}) — web_search is DEAD or unverifiable, not merely empty. Check the search_provider stack component ($SEARCH_COMPONENT_ID) and the SearXNG service." \
  "$NTFY_URL" >/dev/null 2>&1 || log "ntfy send failed"
touch "$COOLDOWN_STAMP"
