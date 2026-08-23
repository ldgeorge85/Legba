#!/usr/bin/env bash
# deploy_smoke_cold_activation.sh — force ONE analyst through a COLD activation
# and prove it left a trace. Run this immediately after any recreate.
#
# ============================================================================
# INVOCATION
# ============================================================================
#
#   scripts/deploy_smoke_cold_activation.sh
#   scripts/deploy_smoke_cold_activation.sh --unit escalation --desk country_g20_de
#   scripts/deploy_smoke_cold_activation.sh --dry-run          # probe + print, force nothing
#   scripts/deploy_smoke_cold_activation.sh --timeout 240
#   scripts/deploy_smoke_cold_activation.sh --typed-unit signal_embedder
#
# Options (all optional):
#   --unit ID        analyst descriptor to force        (default: escalation)
#   --desk TARGET    target_filter to run it against    (default: country_g20_de)
#   --typed-unit ID  deterministic analyst to TYPE-probe (default: claim_watch)
#   --timeout SECS   bound on waiting for the trace     (default: 180)
#   --env-file PATH  where LEGBA_REGISTRY_API_TOKEN lives (default: .env)
#   --dry-run        run the read-only probes, force no run, then stop
#
# Exit 0 = a fresh SUCCESS trace landed. Any other exit is LOUD and nonzero.
#
# ============================================================================
# WHY THIS EXISTS
# ============================================================================
#
# Yesterday's outage was invisible to everything we had. The schema bug broke
# descriptor PARSING, which only bites on the COLD path — resolving deps for an
# actor that is not already resident. Actors that were already warm kept
# running from cached deps, so the fleet looked fine until the next recreate
# evicted them. The full test suite missed it too: every one of its
# ten-thousand-odd tests constructs descriptors IN PROCESS, and none of them
# traverse registry-fetch → parse → activate → run against a live sidecar.
#
# So the gap is specifically: "can a COLD actor activate and complete a run
# against the CURRENTLY DEPLOYED image?" That question is only answerable
# after a recreate, from outside the process, by forcing a run and looking for
# the receipt. That is all this script does.
#
# Three distinguishable outcomes, because they have different causes:
#
#   NO TRACE AT ALL   — the actor never got far enough to record anything.
#                       Cold-activation failure: descriptor parse, deps
#                       resolution, registry reachability. This is the
#                       2026-08-01 shape.
#   FAILED TRACE      — the actor activated and the RUN died. The trace's
#                       error_payload names the class and the attempt count.
#                       (Failure traces only exist as of the §31.1 runtime
#                       fix; before it, a dead run was indistinguishable from
#                       no activation at all — which is exactly why this
#                       script can now tell them apart.)
#   SUCCESS TRACE     — cold path healthy. Exit 0.
#
# A FOURTH outcome was added 2026-08-05, ahead of the forced run:
#
#   TYPED 500         — the REGISTRY cannot parse an options-bearing
#                       DETERMINISTIC descriptor. Registry-side, not runtime;
#                       invisible to forcing `escalation`, which is LLM-kind
#                       and never walks the handler-options catalog. This is
#                       the 2026-08-01/08-04 shape and it silenced claim_watch
#                       + signal_embedder for 14h behind green health probes.
#                       See section 2.
#
set -u

UNIT="escalation"
DESK="country_g20_de"
# The /typed canary unit. MUST be a deterministic, sub_handler-bearing analyst
# — that is the population the outage class selects for (section 2); anything
# else makes this check decorative, and the script asserts the property rather
# than trusting it.
TYPED_UNIT="claim_watch"
# Traces write at run END, and a healthy unit run can legitimately take
# minutes (the core-plane LLM timeout alone is 240s) — a bound tighter than
# the slowest healthy run misreads "slow" as "cold-activation dead".
TIMEOUT_SECS=420
ENV_FILE=".env"
DRY_RUN=0

PG_CONTAINER="${PG_CONTAINER:-legba-postgres-1}"
PG_USER="${PG_USER:-legba}"; PG_DB="${PG_DB:-legba}"
REGISTRY_URL="${REGISTRY_URL:-http://127.0.0.1:8090}"
SIDECAR_URL="${SIDECAR_URL:-http://127.0.0.1:3500}"
ACTOR_TYPE="${ACTOR_TYPE:-AnalystActor}"
POLL_INTERVAL=5

while [ $# -gt 0 ]; do
  case "$1" in
    --unit)     UNIT="${2:?--unit needs a value}"; shift 2 ;;
    --unit=*)   UNIT="${1#*=}"; shift ;;
    --desk)     DESK="${2:?--desk needs a value}"; shift 2 ;;
    --desk=*)   DESK="${1#*=}"; shift ;;
    --typed-unit)   TYPED_UNIT="${2:?--typed-unit needs a value}"; shift 2 ;;
    --typed-unit=*) TYPED_UNIT="${1#*=}"; shift ;;
    --timeout)  TIMEOUT_SECS="${2:?--timeout needs a value}"; shift 2 ;;
    --timeout=*) TIMEOUT_SECS="${1#*=}"; shift ;;
    --env-file) ENV_FILE="${2:?--env-file needs a value}"; shift 2 ;;
    --env-file=*) ENV_FILE="${1#*=}"; shift ;;
    --dry-run)  DRY_RUN=1; shift ;;
    # Through the end of the options block — keep in step when the header grows.
    -h|--help)  sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
  esac
done

say()  { echo "[cold-smoke] $*"; }
die()  {
  echo ""
  echo "############################################################"
  echo "## COLD-ACTIVATION SMOKE FAILED"
  echo "## $*"
  echo "############################################################"
  echo ""
  exit 1
}

# --- 1. resolve the unit's CURRENT version from the registry ----------------
# Deliberately the registry API and not the DB: this asserts the registry is
# serving, which is itself part of the cold path an actor walks.
TOKEN="${LEGBA_REGISTRY_API_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -f "$ENV_FILE" ]; then
  TOKEN="$(grep -E '^[[:space:]]*LEGBA_REGISTRY_API_TOKEN=' "$ENV_FILE" \
           | head -1 | cut -d= -f2- | tr -d '"'"'"' \r')"
fi
[ -n "$TOKEN" ] || die "no LEGBA_REGISTRY_API_TOKEN in env or '$ENV_FILE' — cannot query the registry."

DESCRIPTOR_JSON="$(curl -s -m 20 -H "Authorization: Bearer $TOKEN" \
  "${REGISTRY_URL}/api/v1/registry/descriptors/analyst/${UNIT}" 2>/dev/null)"
[ -n "$DESCRIPTOR_JSON" ] || die "registry at ${REGISTRY_URL} returned nothing for analyst '${UNIT}' — is legba-registry up?"

VERSION="$(printf '%s' "$DESCRIPTOR_JSON" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("version",""))' 2>/dev/null)"
[ -n "$VERSION" ] || die "could not read a version for analyst '${UNIT}' from the registry. Response was: $(printf '%s' "$DESCRIPTOR_JSON" | head -c 300)"

# Actor id grammar: kind::descriptor_id::version[:16]
# (mirrors runtime.reconcile._default_actor_id / consult_api._build_actor_id).
ACTOR_ID="analyst::${UNIT}::$(printf '%s' "$VERSION" | cut -c1-16)"
INVOKE_URL="${SIDECAR_URL}/v1.0/actors/${ACTOR_TYPE}/${ACTOR_ID}/method/run"

say "unit=${UNIT} desk=${DESK}"
say "version=${VERSION}"
say "actor_id=${ACTOR_ID}"
say "invoke=${INVOKE_URL}"

# --- 2. the /typed canary: one OPTIONS-BEARING DETERMINISTIC analyst --------
#
# WHY A SECOND UNIT, AND WHY THIS ONE.
#
# The 2026-08-04 outage was not visible from `escalation`. The registry image's
# pip list was missing pycountry, so inside the REGISTRY PROCESS ONLY the chain
#   legba.data.schemas.analyst -> analysts.handler_options
#     -> analysts.deterministic_handlers -> entity_resolution -> filters.geocode
# raised ModuleNotFoundError. That chain is walked when the registry types a
# descriptor whose method resolves handler options — i.e. exactly the
# DETERMINISTIC, sub_handler-bearing population and nothing else. `escalation`
# is an LLM-kind unit, never touches the options catalog, and typed and ran
# perfectly green for the entire 14h that claim_watch and signal_embedder were
# silent. Forcing escalation therefore CANNOT see this class, which is the
# whole reason it went unnoticed for 14h; a descriptor PUT came back 422 in the
# same window from the same import.
#
# So: probe the SHAPE the escalation force is blind to. GET /typed is the
# cheapest possible expression of it — it is the registry doing the parse, in
# the registry's own process, against the currently deployed registry image.
# A 500 here is the outage; a 200 here is the population healthy.
#
# Read-only, so it runs under --dry-run too — that makes `--dry-run` a genuine
# post-recreate registry health check rather than just an argument echo.
TYPED_URL="${REGISTRY_URL}/api/v1/registry/descriptors/analyst/${TYPED_UNIT}/typed"
say "typed-canary: GET ${TYPED_URL}"

TYPED_OUT=/tmp/legba-cold-smoke-typed.out
TYPED_CODE="$(curl -s -m 25 -o "$TYPED_OUT" -w '%{http_code}' \
  -H "Authorization: Bearer $TOKEN" "$TYPED_URL" 2>/dev/null)"
# TYPED_BODY is for HUMAN MESSAGES ONLY and is truncated. The parse below reads
# the FILE, never this variable: a typed dump runs to several KB and feeding a
# truncated one to json.load reports "Unterminated string" — i.e. it would
# accuse a perfectly healthy descriptor of having lost its method block. Caught
# by pointing --typed-unit at a larger descriptor before shipping.
TYPED_BODY="$(head -c 2000 "$TYPED_OUT" 2>/dev/null)"

case "$TYPED_CODE" in
  200) ;;
  000) die "typed-canary: could not reach the registry at ${REGISTRY_URL} for '${TYPED_UNIT}'." ;;
  404) die "typed-canary: registry has no analyst '${TYPED_UNIT}' (HTTP 404).
##
## Either the canary unit was renamed/retired — repoint --typed-unit at
## another DETERMINISTIC, sub_handler-bearing analyst (signal_embedder,
## entity_resolution, ...) — or a registration did not land." ;;
  5*) die "typed-canary: GET /typed for '${TYPED_UNIT}' returned HTTP ${TYPED_CODE}.
##
## THIS IS THE 2026-08-04 OUTAGE SHAPE and it is REGISTRY-SIDE, not runtime.
## The registry could not TYPE an options-bearing deterministic descriptor.
## Every actor already warm keeps running and every health probe stays green,
## so a healthy dashboard does not contradict this. Descriptor PUTs for this
## population will also be failing 422 right now.
##
## The known cause is a dependency present in pyproject.toml and in
## docker/Dockerfile.runtime but ABSENT from the registry image's explicit pip
## list in docker/Dockerfile.registry (pycountry, 2026-08-04). Confirm with:
##   docker logs --tail 200 legba-legba-registry-1 | grep -iE 'ModuleNotFound|unimportable|dead-options'
##   docker exec legba-legba-registry-1 python -c 'import legba.data.analysts.handler_options'
##
## Registry response body: ${TYPED_BODY}" ;;
  *) die "typed-canary: GET /typed for '${TYPED_UNIT}' returned HTTP ${TYPED_CODE}. Body: ${TYPED_BODY}" ;;
esac

# 200 alone is not the assertion — assert the registry parsed the very block
# whose parse walks the options catalog. A body that types but has lost its
# deterministic method block would mean the descriptor changed shape and this
# canary has quietly stopped covering the population it was chosen for.
TYPED_METHOD="$(python3 -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        d = json.load(fh)
except Exception as exc:
    print("UNPARSEABLE:%s" % exc); raise SystemExit(0)
m = d.get("method") or {}
print("%s|%s" % (m.get("kind", "") or "", m.get("sub_handler", "") or ""))
' "$TYPED_OUT" 2>/dev/null)"

case "$TYPED_METHOD" in
  deterministic\|?*)
    say "typed-canary OK — ${TYPED_UNIT} typed 200 (method=${TYPED_METHOD%%|*} sub_handler=${TYPED_METHOD#*|})" ;;
  *)
    die "typed-canary: '${TYPED_UNIT}' answered 200 but is no longer a
## deterministic, sub_handler-bearing analyst (parsed method: '${TYPED_METHOD}').
##
## This canary only covers the outage class while it stays in that population.
## Point --typed-unit at a unit that still is one, or this check is decorative." ;;
esac

if [ "$DRY_RUN" = "1" ]; then
  say "--dry-run: resolved cleanly and the typed canary passed; invoking nothing."
  exit 0
fi

# --- 3. mark the watermark, then force the run ------------------------------
# The watermark is Postgres' own clock, so the comparison can't drift against
# host time (host cron here is PDT, not UTC).
# ISO-8601 with a 'T' separator: `now()::text` carries an internal space that
# a naive whitespace strip would eat, leaving an unparseable literal that made
# every poll below error out silently — i.e. this script's own first live run
# reported NO TRACE against a run that succeeded.
SINCE="$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tAc \
  "SELECT replace(now()::text, ' ', 'T')" 2>/dev/null | tr -d ' \r')"
[ -n "$SINCE" ] || die "could not read the clock from ${PG_CONTAINER} — is Postgres up?"
say "watermark=${SINCE}"

HTTP_CODE="$(curl -s -m 60 -o /tmp/legba-cold-smoke.out -w '%{http_code}' \
  -X PUT -H 'Content-Type: application/json' \
  -d "{\"target_filter\": \"${DESK}\", \"trigger_kind\": \"method\"}" \
  "$INVOKE_URL" 2>/dev/null)"
say "sidecar responded ${HTTP_CODE}: $(head -c 400 /tmp/legba-cold-smoke.out 2>/dev/null)"
case "$HTTP_CODE" in
  2*) ;;
  000) die "could not reach the Dapr sidecar at ${SIDECAR_URL}. The actor host may be down, or the sidecar is still starting." ;;
  *)  die "sidecar refused the invoke with HTTP ${HTTP_CODE}. Body: $(head -c 500 /tmp/legba-cold-smoke.out 2>/dev/null)" ;;
esac

# --- 4. wait, bounded, for a trace newer than the watermark -----------------
# A 2xx from the sidecar is NOT the assertion — the sidecar happily returns 200
# for a run that recorded nothing. The trace row is the assertion.
say "waiting up to ${TIMEOUT_SECS}s for an analyst_traces row newer than the watermark..."
deadline=$(( $(date +%s) + TIMEOUT_SECS ))
row=""
while [ "$(date +%s)" -lt "$deadline" ]; do
  row="$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tAc "
    SELECT status || '|' || COALESCE(error_payload::text, '')
    FROM analyst_traces
    WHERE analyst_id = '${UNIT}' AND run_started_at > '${SINCE}'::timestamptz
    ORDER BY run_started_at DESC LIMIT 1" 2>/dev/null | tr -d '\r')"
  [ -n "$row" ] && break
  sleep "$POLL_INTERVAL"
done

if [ -z "$row" ]; then
  # Re-run the poll once WITHOUT the stderr swallow: if the emptiness was a
  # SQL/psql failure rather than a genuinely absent row, say so out loud
  # instead of misreporting it as a cold-activation failure.
  say "final poll attempt with errors visible:"
  docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tAc "
    SELECT status || '|' || COALESCE(error_payload::text, '')
    FROM analyst_traces
    WHERE analyst_id = '${UNIT}' AND run_started_at > '${SINCE}'::timestamptz
    ORDER BY run_started_at DESC LIMIT 1" || true
  die "NO TRACE. Unit '${UNIT}' was forced on desk '${DESK}' and wrote NOTHING to analyst_traces in ${TIMEOUT_SECS}s.
##
## This is the cold-activation failure shape: the actor could not get far
## enough to record a run. Warm actors elsewhere in the fleet will keep
## working and every health probe will stay green, so DO NOT treat a healthy
## dashboard as contradicting this.
##
## Look at, in order:
##   docker logs --tail 200 legba-legba-runtime-dapr-1 | grep -iE 'deps|descriptor|validation|activate'
##   curl -H \"Authorization: Bearer \$TOKEN\" ${REGISTRY_URL}/api/v1/registry/descriptors/analyst/${UNIT}/typed
## A descriptor that will not PARSE is the known cause (2026-08-01)."
fi

status="${row%%|*}"
err="${row#*|}"
case "$status" in
  success)
    say "OK — ${UNIT} activated cold on ${DESK} and recorded a SUCCESS trace."
    exit 0 ;;
  *)
    die "RUN FAILED. Unit '${UNIT}' activated and recorded a trace with status='${status}'.
##
## Cold activation itself WORKED — the actor resolved its deps and started.
## The run then died, so this is a runtime/dependency fault rather than the
## descriptor-parse shape.
##
## error_payload: $(printf '%s' "$err" | head -c 600)" ;;
esac
