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
# §31.1 — THREE CHECKS, because the silence alarm alone is a LAGGING signal
# (it needs 90 min of nothing before it fires) and liveness pings lie:
#
#   1. SILENCE     — has any LLM-bearing analyst succeeded lately? (lagging,
#                    but the only check that sees a plane-wide stall.)
#   2. COMPLETION  — does the model actually COMPLETE? A real chat completion
#                    through the DEPLOYED component config. This is the check
#                    that was missing: a /v1/models 200 stayed green through
#                    19h of dead completions, because serving a model LIST and
#                    generating TOKENS are different code paths. Leading
#                    signal — fires in one tick, not ninety minutes.
#   3. LONG-CONTEXT — does a ~24k-char prompt complete? The real analyst
#                    prompts are large (LEGBA_LLM_INPUT_TOKEN_BUDGET=32k); a
#                    server can answer "PONG" happily and still OOM, truncate
#                    or hang on a production-sized slice. Runs every Nth tick
#                    (hourly by default) because it is the expensive one.
#
# The completion probes resolve the model name, endpoint and credential from
# the LIVE stack component via the registry + vault — NEVER hardcoded, so the
# probe follows a component edit instead of silently testing a stale target.
# They run inside the app container (which already carries the code and the
# LEGBA_* env) via a piped heredoc, so this script works the moment it lands
# on the host — no image rebuild, no deploy train.
#
# ALERT-ONLY by design: the fix lives on a REMOTE host (the models box), so
# this never restarts anything — it pages the operator via the local ntfy
# (web UI) and logs. Each check rate-limits on its OWN cooldown stamp so a
# silence page cannot mask a completion page.
# Cron: */10 * * * *  (ride the same /etc/cron.d/legba-watchdog file).
set -u
PG_CONTAINER="${PG_CONTAINER:-legba-postgres-1}"
PG_USER="${PG_USER:-legba}"; PG_DB="${PG_DB:-legba}"
MAX_LLM_AGE_SECS="${MAX_LLM_AGE_SECS:-5400}"   # 90 min: units tick ~30min; 3 misses = real
NTFY_URL="${NTFY_URL:-http://127.0.0.1:8093/legba-alerts}"
COOLDOWN_STAMP="/tmp/legba-llm-heartbeat.cooldown"
COOLDOWN_SECS="${COOLDOWN_SECS:-3600}"
LOG="${LOG:-/var/log/legba-watchdog.log}"

# --- completion-probe knobs -------------------------------------------------
APP_CONTAINER="${APP_CONTAINER:-legba-legba-registry-1}"
# The stack component to probe. The probe reads its model_name / api_endpoint /
# credential from the registry row, so this is the only identifier here — the
# model name itself is never written down in this file.
PROBE_COMPONENT="${PROBE_COMPONENT:-llm.primary.openai_compat}"
PROBE_ENABLED="${PROBE_ENABLED:-1}"
SHORT_TIMEOUT="${SHORT_TIMEOUT:-120}"          # a 1-token reply; 120s is generous
SHORT_COOLDOWN_STAMP="/tmp/legba-llm-completion.cooldown"
# Long-context: its own generous timeout (a 24k-char prompt on a loaded 120B
# can take minutes) and its own cadence — every Nth tick, so at */10 cron the
# default 6 means hourly.
LONGCTX_ENABLED="${LONGCTX_ENABLED:-1}"
LONGCTX_TIMEOUT="${LONGCTX_TIMEOUT:-600}"
LONGCTX_CHARS="${LONGCTX_CHARS:-24000}"
LONGCTX_EVERY_N="${LONGCTX_EVERY_N:-6}"
LONGCTX_COOLDOWN_STAMP="/tmp/legba-llm-longctx.cooldown"
TICK_COUNTER="/tmp/legba-llm-heartbeat.tick"

log() { echo "$(date -u +%FT%TZ) [llm-heartbeat] $*" >> "$LOG"; }

# page <priority> <title> <tags> <body> — the script's existing ntfy idiom.
page() {
  curl -s -m 10 -H "X-Title: $2" -H "X-Priority: $1" -H "X-Tags: $3" \
    -d "$4" "$NTFY_URL" >/dev/null 2>&1 || log "ntfy send failed ($2)"
}

# cooled_down <stamp> — true (0) when the stamp is fresh, i.e. SUPPRESS.
cooled_down() {
  [ -f "$1" ] || return 1
  last=$(stat -c %Y "$1" 2>/dev/null || echo 0)
  [ $(( $(date +%s) - last )) -lt "$COOLDOWN_SECS" ]
}

[ -f /etc/legba-watchdog.disabled ] && exit 0

# ---------------------------------------------------------------------------
# 1) SILENCE — no LLM-bearing analyst has succeeded in MAX_LLM_AGE_SECS.
# ---------------------------------------------------------------------------
check_silence() {
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
  #
  # NOTE (§31.1): the status filter is now load-bearing in a second way. Dead
  # runs write status='failed' rows, so `status='success'` is what keeps a
  # fleet that is failing every tick from reading as fresh.
  [ -z "$age" ] && { log "SKIP could not read analyst_traces"; return 0; }
  if [ "$age" -lt "$MAX_LLM_AGE_SECS" ]; then rm -f "$COOLDOWN_STAMP"; return 0; fi
  cooled_down "$COOLDOWN_STAMP" && return 0
  log "FIRE llm plane silent: newest successful LLM-analyst run ${age}s ago (> ${MAX_LLM_AGE_SECS}s)"
  page 5 "Legba: LLM plane silent" "rotating_light" \
    "No LLM-bearing analyst has completed a run in $((age/60)) min while the signal pipeline flows — the core model endpoint is the first suspect (vLLM on the models host; check supervisorctl status vs the actual port, RUNNING-but-dead is the known failure). Deterministic analysts unaffected."
  touch "$COOLDOWN_STAMP"
}

# ---------------------------------------------------------------------------
# 2+3) COMPLETION / LONG-CONTEXT — a REAL generation through the live component.
#
# run_probe <mode> <timeout> — echoes the probe's one-line verdict, returns the
# probe's exit status. Everything the probe needs comes from the container's
# own env; the only inputs are the mode, the timeout and the component id.
# ---------------------------------------------------------------------------
run_probe() {
  docker exec -i \
    -e "PROBE_COMPONENT=$PROBE_COMPONENT" \
    -e "PROBE_MODE=$1" \
    -e "PROBE_TIMEOUT=$2" \
    -e "PROBE_LONG_CHARS=$LONGCTX_CHARS" \
    "$APP_CONTAINER" python3 - <<'PY' 2>&1 | grep -E '^(OK|FAIL) ' | tail -1
import asyncio
import os
import sys
import time

COMPONENT = os.environ.get("PROBE_COMPONENT", "llm.primary.openai_compat")
MODE = os.environ.get("PROBE_MODE", "short")
TIMEOUT = float(os.environ.get("PROBE_TIMEOUT", "120"))
LONG_CHARS = int(os.environ.get("PROBE_LONG_CHARS", "24000"))
# A token that cannot appear in filler, so "did the model READ the prompt"
# is separable from "did the model reply at all".
NEEDLE = "ZEPHYR-7741"


def _long_prompt() -> str:
    """~LONG_CHARS of inert filler with the needle buried in the MIDDLE.

    The middle matters: a server that silently truncates long input keeps the
    head and the tail, so a needle at either end would still be found.
    """
    filler = (
        "Routine situation-report filler paragraph used only to occupy "
        "context. It carries no analytic content and should be ignored. "
    )
    body = filler * max(1, LONG_CHARS // len(filler))
    mid = len(body) // 2
    return (
        body[:mid]
        + f"\n\nThe authorization code for this report is {NEEDLE}.\n\n"
        + body[mid:]
    )


async def main() -> int:
    from legba.data.config import PostgresConfig
    from legba.data.postgres import PostgresStore
    from legba.data.registry.credentials import CredentialVault
    from legba.runtime.analyst_deps_builder import (
        build_llm_handler_from_stack_component,
    )
    from legba.runtime.registry_client import RegistryHTTPClient

    store = PostgresStore(PostgresConfig.from_env())
    await store.connect()
    vault = CredentialVault(store)

    async def _resolve(secret_id: str) -> bytes:
        return await vault.resolve(secret_id)

    registry = RegistryHTTPClient()
    handler = None
    try:
        # The whole point: endpoint + model name + credential come from the
        # LIVE stack component, so the probe tracks a component edit instead
        # of testing a target nobody serves any more.
        handler = await build_llm_handler_from_stack_component(
            COMPONENT, registry_client=registry, secrets_resolve=_resolve,
        )
        cfg = getattr(handler, "_cfg", None)
        model = getattr(getattr(cfg, "model_name", None), "raw", "?")

        if MODE == "long":
            user = _long_prompt() + (
                "\nWhat is the authorization code stated in the report above? "
                "Reply with the code and nothing else."
            )
            system = "You extract one fact from a long document. Be terse."
            sentinel = NEEDLE
        else:
            user = "Reply with exactly the word PONG and nothing else."
            system = "You are a liveness probe. Answer in one word."
            sentinel = "PONG"

        t0 = time.monotonic()
        resp = await asyncio.wait_for(
            handler.chat_complete(
                [{"role": "user", "content": user}],
                system=system,
                max_tokens=64,
                temperature=0.0,
            ),
            timeout=TIMEOUT,
        )
        latency = round(time.monotonic() - t0, 1)
        content = (getattr(resp, "content", "") or "").strip()
        if not content:
            # A 200 with an empty body is the exact shape that kept /v1/models
            # green for 19h. It is a FAILURE, not a quiet success.
            print(
                f"FAIL mode={MODE} model={model} latency={latency}s "
                f"chars={len(user)} reason=empty_completion "
                f"finish={getattr(resp, 'finish_reason', None)}"
            )
            return 1
        # Sentinel MISS is reported but never paged on: a chatty or degraded
        # reply is a quality signal, not proof the plane is down, and a
        # watchdog that cries wolf gets muted. The call itself succeeding is
        # the pass/fail line.
        matched = sentinel.lower() in content.lower()
        print(
            f"OK mode={MODE} model={model} latency={latency}s "
            f"chars={len(user)} reply_chars={len(content)} "
            f"sentinel={'hit' if matched else 'MISS'}"
        )
        return 0
    except asyncio.TimeoutError:
        print(f"FAIL mode={MODE} reason=timeout after {TIMEOUT}s")
        return 1
    except Exception as exc:                      # noqa: BLE001 - probe reports all
        print(f"FAIL mode={MODE} reason={type(exc).__name__}: {str(exc)[:300]}")
        return 1
    finally:
        if handler is not None:
            try:
                await handler.on_deactivate(None)
            except Exception:                     # noqa: BLE001 - best-effort
                pass
        try:
            await registry.aclose()
        except Exception:                         # noqa: BLE001
            pass
        try:
            await store.close()
        except Exception:                         # noqa: BLE001
            pass


sys.exit(asyncio.run(main()))
PY
}

check_completion() {
  [ "$PROBE_ENABLED" = "1" ] || return 0
  verdict="$(run_probe short "$SHORT_TIMEOUT")"
  case "$verdict" in
    OK\ *) log "probe.completion $verdict"; rm -f "$SHORT_COOLDOWN_STAMP"; return 0;;
  esac
  [ -z "$verdict" ] && verdict="FAIL mode=short reason=no_probe_output (container ${APP_CONTAINER} unreachable?)"
  log "FIRE completion probe failed: $verdict"
  cooled_down "$SHORT_COOLDOWN_STAMP" && return 0
  page 5 "Legba: LLM completion DEAD" "rotating_light,skull" \
    "A real chat completion against ${PROBE_COMPONENT} FAILED: ${verdict}. This is the check /v1/models cannot make — serving a model list and generating tokens are different code paths, and a liveness 200 stayed green through 19h of dead completions. Analysts will start failing within one cadence. Check vLLM on the models host (RUNNING-but-dead is the known failure) and the aiproxy upstream."
  touch "$SHORT_COOLDOWN_STAMP"
}

check_longctx() {
  [ "$PROBE_ENABLED" = "1" ] || return 0
  [ "$LONGCTX_ENABLED" = "1" ] || return 0
  # Tick gate — the long probe is the expensive one, so it runs every Nth
  # invocation rather than every cron tick.
  tick=$(cat "$TICK_COUNTER" 2>/dev/null || echo 0)
  case "$tick" in ''|*[!0-9]*) tick=0;; esac
  tick=$(( tick + 1 ))
  echo "$tick" > "$TICK_COUNTER"
  [ "$LONGCTX_EVERY_N" -gt 0 ] || return 0
  [ $(( tick % LONGCTX_EVERY_N )) -eq 0 ] || return 0

  verdict="$(run_probe long "$LONGCTX_TIMEOUT")"
  case "$verdict" in
    OK\ *) log "probe.longctx $verdict"; rm -f "$LONGCTX_COOLDOWN_STAMP"; return 0;;
  esac
  [ -z "$verdict" ] && verdict="FAIL mode=long reason=no_probe_output (container ${APP_CONTAINER} unreachable?)"
  log "FIRE long-context probe failed: $verdict"
  cooled_down "$LONGCTX_COOLDOWN_STAMP" && return 0
  page 4 "Legba: LLM long-context DEGRADED" "warning" \
    "A ~${LONGCTX_CHARS}-char prompt against ${PROBE_COMPONENT} FAILED while short completions may still pass: ${verdict}. Production analyst prompts are this size (LEGBA_LLM_INPUT_TOKEN_BUDGET=32k), so the fleet will fail on real slices while every small probe stays green. Suspect context-length config, KV-cache/OOM, or a proxy read timeout."
  touch "$LONGCTX_COOLDOWN_STAMP"
}

check_silence
check_completion
check_longctx
exit 0
