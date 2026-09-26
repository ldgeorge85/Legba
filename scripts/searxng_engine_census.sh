#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# searxng_engine_census.sh — a daily read of which SearXNG engines actually
# answer from THIS host (Program 2 track S2, search hygiene). Runs three
# fixed, broad current-events queries against the LOCAL instance and reports,
# per query: the result count, the engines that answered, and the engines
# SearXNG itself reports as unresponsive (with SearXNG's own reason string).
#
# WHY `unresponsive_engines` IS THE SIGNAL THAT MATTERS: a blocked engine
# still answers HTTP 200 with a short/empty `results[]` — see
# src/legba/data/stack/search/searxng.py, which reads this exact field to
# distinguish "engines refused us" from "nothing exists". This script reads
# the same field a human can watch trend on, day over day.
#
# Read-only: three GET requests to the local instance's host-mapped port
# (127.0.0.1, per docker-compose.yml — never leaves the host). No secrets,
# no write path into the platform, no container recreate.
#
# Usage:
#   scripts/searxng_engine_census.sh
#
# Env:
#   LEGBA_SEARXNG_CENSUS_URL   override the base URL entirely
#                              (default: parsed from docker-compose.yml's
#                              `searxng:` `ports:` host mapping, falling back
#                              to http://127.0.0.1:8888 if parsing fails)
#   LEGBA_SEARXNG_CENSUS_LOG   override the JSONL log path
#                              (default: /var/log/legba/searxng_census.jsonl,
#                              falling back to /tmp/legba_searxng_census.jsonl
#                              if that directory can't be created/written)
#
# Cron suggestion (NOT installed by this script — an operator's call; host
# cron runs in PDT, not UTC — see MEMORY.md "Host cron + bare date -d are
# PDT, not UTC" before picking a wall-clock time):
#   7 */6 * * * /usr/local/deployments/active/legba/scripts/searxng_engine_census.sh >>/var/log/legba/searxng_census.cron.log 2>&1
#
# Exit codes: 0 = ran (even if every query was fully degraded — that IS the
# finding); 1 = could not reach the instance at all on any of the 3 queries.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="${LEGBA_COMPOSE_FILE:-$REPO_ROOT/docker-compose.yml}"

# ── resolve the host port (docker-compose.yml `searxng:` `ports:` mapping) ──
default_base_url="http://127.0.0.1:8888"
resolved_base_url=""
if [ -z "${LEGBA_SEARXNG_CENSUS_URL:-}" ] && [ -f "$COMPOSE_FILE" ]; then
    host_port="$(sed -n '/^  searxng:/,/^  [A-Za-z0-9_-]\+:$/p' "$COMPOSE_FILE" \
        | grep -oE '127\.0\.0\.1:[0-9]+:[0-9]+' \
        | head -1 \
        | cut -d: -f2)"
    if [ -n "${host_port:-}" ]; then
        resolved_base_url="http://127.0.0.1:${host_port}"
    fi
fi
BASE_URL="${LEGBA_SEARXNG_CENSUS_URL:-${resolved_base_url:-$default_base_url}}"

# ── resolve the log path (create /var/log/legba if missing; fall back to /tmp) ──
default_log="/var/log/legba/searxng_census.jsonl"
LOG_PATH="${LEGBA_SEARXNG_CENSUS_LOG:-$default_log}"
log_dir="$(dirname "$LOG_PATH")"
if ! mkdir -p "$log_dir" 2>/dev/null || [ ! -w "$log_dir" ]; then
    LOG_PATH="/tmp/legba_searxng_census.jsonl"
    echo "WARN: cannot write $log_dir — falling back to $LOG_PATH" >&2
fi

# ── the three fixed queries (stable topics, broad enough to answer across ──
# ── both meta-index engines like bing/yahoo and single-source regional    ──
# ── scrapers like tagesschau/ansa/abcnyheter) ──────────────────────────────
QUERIES=(
    "Ukraine Russia war latest"
    "Israel Gaza ceasefire negotiations"
    "global inflation interest rates 2026"
)

echo "SearXNG engine census — $(date -u +%FT%TZ) — $BASE_URL"
echo "log: $LOG_PATH"
echo

overall_ok=0
run_ts="$(date -u +%FT%TZ)"

for query in "${QUERIES[@]}"; do
    resp_file="$(mktemp)"
    http_code="$(curl -sS -G "$BASE_URL/search" \
        --data-urlencode "q=$query" \
        --data-urlencode "format=json" \
        -w '%{http_code}' \
        -o "$resp_file" \
        --max-time 20 2>/dev/null)"
    curl_status=$?

    if [ "$curl_status" -ne 0 ] || [ "$http_code" != "200" ]; then
        echo "QUERY: \"$query\""
        echo "  UNREACHABLE (curl exit $curl_status, HTTP ${http_code:-none}) — is legba-searxng-1 up? (docker compose --profile search up -d searxng)"
        echo
        python3 - "$run_ts" "$query" "$http_code" "$curl_status" "$LOG_PATH" <<'PYEOF'
import json, sys
run_ts, query, http_code, curl_status, log_path = sys.argv[1:6]
row = {
    "ts": run_ts,
    "query": query,
    "ok": False,
    "http_code": http_code or None,
    "curl_exit": int(curl_status),
    "result_count": None,
    "answering_engines": [],
    "unresponsive_engines": {},
}
try:
    with open(log_path, "a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")
except OSError as e:
    print(f"WARN: could not append to {log_path}: {e}", file=sys.stderr)
PYEOF
        rm -f "$resp_file"
        continue
    fi

    overall_ok=1
    python3 - "$run_ts" "$query" "$resp_file" "$LOG_PATH" <<'PYEOF'
import json, sys

run_ts, query, resp_file, log_path = sys.argv[1:5]

with open(resp_file) as f:
    raw = f.read()

try:
    payload = json.loads(raw)
except json.JSONDecodeError:
    # search.formats didn't actually serve JSON (the load-bearing-line trap
    # this file's header warns about) — surface it loudly rather than
    # silently reporting zero results.
    print(f'  NOT JSON (search.formats missing "json"? got {len(raw)} bytes '
          f'starting "{raw[:60]!r}")')
    row = {
        "ts": run_ts, "query": query, "ok": False, "http_code": 200,
        "curl_exit": 0, "result_count": None, "answering_engines": [],
        "unresponsive_engines": {}, "error": "response was not JSON",
    }
    with open(log_path, "a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")
    sys.exit(0)

results = payload.get("results", [])
answering = sorted({e for r in results for e in r.get("engines", [r.get("engine")]) if e})
unresponsive_raw = payload.get("unresponsive_engines", [])
# SearXNG serializes unresponsive_engines as a list of [engine, reason] pairs
# in the JSON API (not a dict) — normalize to a dict for the log and the report.
unresponsive = {}
for item in unresponsive_raw:
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        unresponsive[str(item[0])] = str(item[1])
    elif isinstance(item, dict):
        unresponsive[str(item.get("engine"))] = str(item.get("error") or item.get("reason") or "unknown")
    else:
        unresponsive[str(item)] = "unknown"

print(f'QUERY: "{query}"')
print(f"  results: {len(results)}")
print(f"  answering engines ({len(answering)}): {', '.join(answering) if answering else '(none)'}")
if unresponsive:
    print(f"  unresponsive engines ({len(unresponsive)}):")
    for eng, reason in sorted(unresponsive.items()):
        print(f"    - {eng}: {reason}")
else:
    print("  unresponsive engines: (none)")
print()

row = {
    "ts": run_ts,
    "query": query,
    "ok": True,
    "http_code": 200,
    "curl_exit": 0,
    "result_count": len(results),
    "answering_engines": answering,
    "unresponsive_engines": unresponsive,
}
try:
    with open(log_path, "a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")
except OSError as e:
    print(f"WARN: could not append to {log_path}: {e}", file=sys.stderr)
PYEOF
    rm -f "$resp_file"
done

if [ "$overall_ok" -eq 0 ]; then
    echo "FAIL: all $((${#QUERIES[@]})) queries unreachable — instance likely down." >&2
    exit 1
fi

exit 0
