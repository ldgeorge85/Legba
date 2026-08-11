# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render a byte-faithful PROMPT PACK for operator review.

The operator wants to personally read EXACTLY what the models are sent.
``analyst_traces.prompt_rendered`` is NULL on every row (never wired), and
``analyst_traces.input_payload`` is ``{}`` on every row — so this script
REPLAYS each analyst's most recent real run through the REAL render
functions and writes one markdown file per prompt surface:

  * one ``<analyst_id>.md`` per inline_target analyst — the exact effective
    SYSTEM prompt (live descriptor -> promoted-candidate check ->
    ``with_preamble_if_absent`` -> ``with_grounding_clause``, the same
    resolution order as ``runtime/analyst_deps_builder._build_inline_target``),
    the replayed USER prompt (real ``_orient`` + ``_render_user_prompt`` over
    the run's ``input_row_refs`` re-fetched from ``signals``, plus the
    grounding preamble captured verbatim in the trace's ``inject_preamble``
    step), and the call parameters actually used;
  * ``judge_generic.md`` — the faithfulness judge: both system prompts
    (live + staged), a REAL evidence prompt rebuilt by driving the real
    ``verify._run_judge`` with a capturing stand-in judge, and a caps annex;
  * ``composition.md`` / ``journal_narrate.md`` — best-effort (documented);
  * ``INDEX.md`` — one line per file.

BYTE-FAITHFULNESS ORACLE: every provider-plane LLM call records
``llm_calls[].prompt_sha256`` = sha256 over
``json.dumps({"system": None, "messages": [system-msg, user-msg]},
sort_keys=True, default=str, ensure_ascii=False)`` (see
``data/run_accounting.prompt_digest`` + ``data/stack/llm/base.py``
``chat_complete``/``_account_call``).  The replay recomputes that digest and
reports MATCH / MISMATCH per file, so "byte-faithful" is proven rather than
asserted.  Known unreconstructable parts (documented per file):

  * id-less ``[ASSESSED STRUCTURE]`` context rows (graph_metrics state has
    advanced; they sort to the tail ordinals, so signal ordinals are stable);
  * the DESK GROUNDING blocks (rendered from live prior-read/situations/
    baseline/questions state at run time; re-rendered here from CURRENT state
    through the real ``unit_grounding`` readers — ages/NOW()-relative text
    differ) — where the run's finding persisted a block's ``evidence_text``
    (exact bytes) it is shown alongside;
  * GATHER-gathered corpus blocks (live tool results, not persisted).

READ-ONLY BY CONSTRUCTION: all DB access goes through
``docker exec legba-postgres-1 psql`` and every statement is asserted to be a
SELECT; the registry is only ever GET.  No deploys, no writes, no PUTs.

Usage:
    python3 scripts/render_prompt_pack.py --out DIR [--analyst ID] [surfaces]
    # surfaces: --units --judge --composition --journal (default: all)

Auth: registry bearer token from ``LEGBA_REGISTRY_API_TOKEN`` (falls back to
reading the runtime container's env, read-only ``docker exec``).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

# The 12 inline_target analysts (9 bounded units incl. disruption_status,
# + the three non-unit inline_target analysts). p2_probe_unit is excluded
# (retired probe, not an operator-facing surface).
UNIT_ANALYSTS: tuple[str, ...] = (
    "escalation",
    "energy_security",
    "internal_stability",
    "military_posture",
    "narrative_coordination",
    "leadership_transition",
    "economic_coercion",
    "proliferation_watch",
    "disruption_status",
    "cross_doc_corroborator",
    "corpus_researcher",
    "country_assessor",
)

PG_CONTAINER = "legba-postgres-1"
RUNTIME_CONTAINER = "legba-legba-runtime-dapr-1"
REGISTRY_URL = os.getenv("LEGBA_PROMPT_PACK_REGISTRY_URL", "http://127.0.0.1:8090")

# The runtime deployment's input-token budget (verified against the live
# container env; ``LEGBA_LLM_INPUT_TOKEN_BUDGET=65536``). ``_orient`` reads the
# env at call time, so the replay must run under the same value.
DEFAULT_INPUT_TOKEN_BUDGET = "65536"


# ---------------------------------------------------------------------------
# Read-only DB access — docker exec psql, SELECT-only, JSON out
# ---------------------------------------------------------------------------


def _assert_select(sql: str) -> None:
    head = sql.lstrip().split(None, 1)[0].lower() if sql.strip() else ""
    if head not in ("select", "with"):
        raise ValueError(f"refusing non-SELECT statement: {sql[:80]!r}")
    for banned in (";", "insert ", "update ", "delete ", "drop ", "alter ",
                   "truncate ", "create "):
        if banned in sql.lower():
            raise ValueError(f"refusing statement containing {banned!r}")


def db_json(sql: str) -> list[dict[str, Any]]:
    """Run ONE SELECT via docker-exec psql; rows come back as JSON dicts."""
    _assert_select(sql)
    wrapped = (
        "SELECT COALESCE(json_agg(row_to_json(t)), '[]'::json) FROM ("
        + sql
        + ") t"
    )
    proc = subprocess.run(
        ["docker", "exec", PG_CONTAINER, "psql", "-U", "legba", "-d", "legba",
         "-X", "-At", "-c", wrapped],
        capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


def sql_lit(value: Any) -> str:
    """A safely-quoted SQL literal for inlining shim parameters."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "ARRAY[" + ", ".join(sql_lit(v) for v in value) + "]"
    text = str(value).replace("'", "''")
    return f"'{text}'"


class PsqlConn:
    """Async asyncpg-conn lookalike over docker-exec psql (SELECT-only).

    Lets the REAL conn-based readers (``unit_grounding.gather_unit_grounding_
    rows``, ``meta_findings_synthesizer.READ_SLICE``) run unmodified against
    the live DB without opening a direct connection.  ``$N`` placeholders are
    inlined as quoted literals.  Timestamps come back as ISO strings (not
    datetimes) — a documented divergence where a renderer prints them.
    """

    @staticmethod
    def _inline(sql: str, params: Sequence[Any]) -> str:
        for i in range(len(params), 0, -1):
            sql = sql.replace(f"${i}", sql_lit(params[i - 1]))
        return sql

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        return db_json(self._inline(sql, params))

    async def fetchrow(self, sql: str, *params: Any) -> dict[str, Any] | None:
        rows = await self.fetch(sql, *params)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *params: Any) -> Any:
        row = await self.fetchrow(sql, *params)
        if row is None:
            return None
        return next(iter(row.values()), None)


# ---------------------------------------------------------------------------
# Read-only registry access — GET only
# ---------------------------------------------------------------------------


def _registry_token(cli_token: str | None) -> str:
    if cli_token:
        return cli_token
    env = os.getenv("LEGBA_REGISTRY_API_TOKEN")
    if env:
        return env
    # Read-only fallback: lift the runtime container's own token.
    proc = subprocess.run(
        ["docker", "exec", RUNTIME_CONTAINER, "env"],
        capture_output=True, text=True, check=True,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("LEGBA_REGISTRY_API_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(
        "no registry token: pass --registry-token or set LEGBA_REGISTRY_API_TOKEN"
    )


def registry_get(path: str, token: str) -> dict[str, Any]:
    req = urllib.request.Request(
        REGISTRY_URL.rstrip("/") + path,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# The wire digest — byte-identical to run_accounting.prompt_digest over the
# vllm handler's wire shape (base._translate_messages prepends the system
# message; wire_system is None for OpenAI-style providers).
# ---------------------------------------------------------------------------


def wire_digest(system: str, user: str) -> tuple[str, int]:
    wire = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    blob = json.dumps(
        {"system": None, "messages": wire},
        sort_keys=True, default=str, ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest(), len(blob)


# ---------------------------------------------------------------------------
# System-prompt resolution — mirrors analyst_deps_builder._build_inline_target
# ---------------------------------------------------------------------------


def resolve_promoted_prompt(analyst_id: str) -> str | None:
    """The SAME query as ``optimizer.resolve_promoted_system_prompt`` (P4-T6
    guard included), via psql."""
    rows = db_json(
        "SELECT data->>'candidate_prompt_module_text' AS text "
        "FROM analyst_outputs "
        "WHERE kind = 'prompt_module_candidate' "
        f"  AND data->>'analyst_id' = {sql_lit(analyst_id)} "
        "  AND data->>'promotion_gate' = 'promoted' "
        "  AND ( data->'data'->'eval' IS NULL "
        "        OR (data->'data'->'eval'->>'promotable') = 'true' ) "
        "ORDER BY created_at DESC LIMIT 1"
    )
    if rows and isinstance(rows[0].get("text"), str) and rows[0]["text"].strip():
        return rows[0]["text"]
    return None


def effective_unit_system_prompt(
    descriptor_body: Mapping[str, Any], analyst_id: str,
) -> tuple[str, list[str]]:
    """(system_prompt_bytes, resolution_notes) for one inline_target analyst.

    Order of operations = ``_build_inline_target`` (analyst_deps_builder.py
    ~522-564) then ``inline_target._effective_system_prompt`` (~2698-2722):
      prompt_module -> inline method.system_prompt -> promoted candidate ->
      with_preamble_if_absent -> with_grounding_clause.
    """
    from legba.data.analysts import inline_target
    from legba.data.analysts._tradecraft import with_preamble_if_absent
    from legba.data.analysts.unit_grounding import with_grounding_clause

    notes: list[str] = []
    method = descriptor_body.get("method") or {}
    system_prompt: str | None = None

    spec = method.get("prompt_module")
    if isinstance(spec, str) and ":" in spec:
        mod_name, _, attr = spec.partition(":")
        import importlib
        module = importlib.import_module(mod_name)
        system_prompt = getattr(module, attr)
        notes.append(f"prompt_module {spec!r} resolved")
    if system_prompt is None:
        inline_prompt = method.get("system_prompt")
        if isinstance(inline_prompt, str) and inline_prompt.strip():
            system_prompt = inline_prompt
            notes.append("inline method.system_prompt (descriptor-borne)")

    promoted = resolve_promoted_prompt(analyst_id)
    if promoted is not None:
        system_prompt = promoted
        notes.append("GEPA-PROMOTED candidate ACTIVE (replaces descriptor prompt)")
    else:
        notes.append("no promoted candidate (baseline prompt live)")

    system_prompt = with_preamble_if_absent(system_prompt)
    if system_prompt is None:
        system_prompt = inline_target._SYSTEM_PROMPT
        notes.append("kind default _SYSTEM_PROMPT (descriptor declared none)")
    effective = with_grounding_clause(system_prompt)
    notes.append(
        "with_preamble_if_absent (_tradecraft.py:38) + with_grounding_clause "
        "(unit_grounding.py:849) applied — the synthesis-call bytes "
        "(inline_target._effective_system_prompt, inline_target.py:2698)"
    )
    return effective, notes


# ---------------------------------------------------------------------------
# Trace + slice replay
# ---------------------------------------------------------------------------


def _step(steps: Sequence[Mapping[str, Any]], kind: str) -> Mapping[str, Any] | None:
    for s in steps:
        if s.get("kind") == kind:
            return s
    return None


def _has_gather(steps: Sequence[Mapping[str, Any]]) -> bool:
    return any(s.get("phase") == "gather" for s in steps)


def fetch_traces(analyst_id: str, limit: int = 40) -> list[dict[str, Any]]:
    return db_json(
        "SELECT run_id, target_id, run_started_at, input_row_refs, "
        "       intermediate_steps, llm_calls, output_row_refs "
        "FROM analyst_traces "
        f"WHERE analyst_id = {sql_lit(analyst_id)} AND status = 'success' "
        "  AND array_length(input_row_refs, 1) > 0 "
        f"ORDER BY run_started_at DESC LIMIT {int(limit)}"
    )


def classify_trace(trace: Mapping[str, Any]) -> dict[str, Any]:
    steps = trace.get("intermediate_steps") or []
    orient = _step(steps, "deterministic") or {}
    return {
        "orient": orient,
        "render": _step(steps, "render_prompt") or {},
        "preamble": _step(steps, "inject_preamble"),
        "ground_blocks": _step(steps, "desk_grounding_blocks"),
        "has_gather": _has_gather(steps),
        "structure_rows": (
            int(orient.get("kept_count", 0)) - int(orient.get("derived_count", 0))
        ),
    }


def pick_traces(
    traces: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(primary, verification): primary = most recent; verification = most
    recent fully-replayable one (no id-less structure rows, no GATHER rounds,
    no desk-grounding blocks — every byte reconstructable)."""
    primary = traces[0] if traces else None
    verification = None
    for t in traces:
        c = classify_trace(t)
        if (
            c["structure_rows"] == 0
            and not c["has_gather"]
            and c["ground_blocks"] is None
        ):
            verification = t
            break
    return primary, verification


def fetch_slice_rows(refs: Sequence[str], target_id: str | None) -> tuple[
    list[dict[str, Any]], int
]:
    """Re-fetch the run's admitted signal rows and re-apply the slice reader's
    back-compat shaping (actor_substrate_slice._read_substrate_slice:429-444).
    Returns (rows, missing_count)."""
    if not refs:
        return [], 0
    id_list = ", ".join(sql_lit(r) for r in refs)
    rows = db_json(
        "SELECT id, source_id, source_version, canonical_url, payload, "
        "       language, geo, tags, fetched_at, derived_from, "
        "       entity_classes, source_credibility, modality, salience "
        f"FROM signals WHERE id IN ({id_list}) "
        "ORDER BY fetched_at DESC"
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        payload = d.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        # Restore the asyncpg-native datetime so ``_render_signal``'s
        # ``ingested={produced_at}`` renders the same bytes as the live run
        # (str(datetime) — space separator — not the JSON ISO 'T' form).
        fetched = d.get("fetched_at")
        if isinstance(fetched, str):
            try:
                fetched = _dt.datetime.fromisoformat(fetched)
            except ValueError:
                pass
        d["target_id"] = target_id
        d["target_version"] = None
        d["source_url"] = d.get("canonical_url")
        d["title"] = payload.get("title") if isinstance(payload, dict) else None
        d["data"] = payload
        d["produced_at"] = fetched
        out.append(d)
    return out, len(refs) - len(rows)


def replay_unit_user_prompt(
    analyst_id: str,
    descriptor_body: Mapping[str, Any],
    trace: Mapping[str, Any],
) -> dict[str, Any]:
    """Replay ONE trace's synthesis user prompt through the real functions.

    Returns a dict with the assembled prompt, per-part receipts, and the list
    of divergences that make it non-byte-exact (empty = candidate for an
    exact digest match).
    """
    from legba.data.analysts import inline_target
    from legba.data.analysts import unit_grounding
    from legba.runtime.actor_substrate_slice import resolve_slice_window_hours

    info = classify_trace(trace)
    divergences: list[str] = []
    target_id = trace.get("target_id")
    refs = trace.get("input_row_refs") or []

    rows, missing = fetch_slice_rows(refs, target_id)
    if missing:
        divergences.append(
            f"{missing} of {len(refs)} input signal rows no longer exist "
            "(corpus retention) — the replayed slice omits them and every "
            "ordinal after the first missing row shifts"
        )
    if info["structure_rows"]:
        divergences.append(
            f"{info['structure_rows']} id-less [ASSESSED STRUCTURE] context "
            "row(s) rendered at the slice tail are not reconstructable "
            "(graph_metrics state has advanced); the replayed prompt omits "
            "them, so the header signal count and the tail ordinals differ"
        )

    # Focus + orient — the REAL functions, under the runtime's input budget.
    method = descriptor_body.get("method") or {}
    options = dict(method.get("options") or {})
    focus = inline_target._resolve_slice_focus(options)
    stats: dict[str, Any] = {}
    sliced, derived = inline_target._orient(
        rows, target_id, stats=stats, focus=focus,
    )
    if len(sliced) != len(rows):
        divergences.append(
            f"replayed _orient admitted {len(sliced)}/{len(rows)} re-fetched "
            "rows (payload sizes have changed since the run; the original "
            "admitted set was exactly the input_row_refs)"
        )

    # Window + run-date — the same resolver the actor uses (D8a), and the
    # run's OWN date so the header matches what the model saw.
    sub = descriptor_body.get("subscription") or {}
    targets = sub.get("targets") or {}
    desc_shim = SimpleNamespace(
        subscription=SimpleNamespace(
            targets=SimpleNamespace(time_window=targets.get("time_window")),
            time_window=None,
            time_window_hours=None,
        )
    )
    window_hours = resolve_slice_window_hours(desc_shim)
    run_started = str(trace.get("run_started_at") or "")
    run_date = run_started[:10] or None

    slice_prompt = inline_target._render_user_prompt(
        sliced, target_id, run_date=run_date, window_hours=window_hours,
    )
    rendered_step = info["render"]
    slice_expected = rendered_step.get("prompt_chars")
    if slice_expected is not None and len(slice_prompt) != int(slice_expected):
        # The substrate rows are LIVING — enrichment (archived_text /
        # translations / distilled bodies) keeps landing on signal payloads
        # after the run, and the render's body precedence then resolves a
        # different field. Quantify the drift via the ORIENT receipt.
        receipt = {
            k: info["orient"].get(k)
            for k in ("gdelt_prosed", "full_body_rows", "teaser_rows",
                      "empty_body_rows", "untranslated_marked")
        }
        replayed = {k: stats.get(k) for k in receipt}
        drift = {k: (receipt[k], replayed[k]) for k in receipt
                 if receipt[k] != replayed[k]}
        divergences.append(
            f"slice render is {len(slice_prompt)} chars vs {slice_expected} "
            "at run time — signal payloads have been ENRICHED since the run "
            "(post-run archived_text/translation/distillation writes change "
            "the body-precedence pick); ORIENT receipt drift (run -> replay): "
            f"{drift or 'none visible in the counters'}"
        )

    # GATHER — live tool results are not persisted; nothing to replay.
    if info["has_gather"]:
        divergences.append(
            "this run engaged the GATHER phase; any gathered corpus blocks / "
            "tool summaries prepended to the synthesis prompt are live tool "
            "results and are NOT reconstructable (see the GATHER note below)"
        )

    # Grounding preamble — captured VERBATIM in the trace (DQ P6).
    preamble = None
    if info["preamble"] is not None:
        preamble = info["preamble"].get("preamble_text") or ""
        cap = inline_target._PREAMBLE_TRACE_CHAR_CAP
        if int(info["preamble"].get("preamble_chars") or 0) > cap:
            divergences.append(
                f"grounding preamble exceeded the {cap}-char trace capture cap; "
                "the tail beyond the cap is lost"
            )

    # Desk grounding blocks — re-rendered from CURRENT DB state through the
    # real readers (ages / NOW()-relative text and any advanced prior read
    # differ from run time; exact run-time bytes exist only for blocks the
    # finding actually cited, shown separately).
    grounding_text = ""
    grounding_note = None
    if info["ground_blocks"] is not None:
        start = int(info["ground_blocks"].get("start_ordinal") or (len(sliced) + 1))
        try:
            g_rows = asyncio.run(
                unit_grounding.gather_unit_grounding_rows(
                    PsqlConn(), analyst_id=analyst_id, target_filter=target_id,
                )
            )
            grounding_text, _stamped = unit_grounding.render_grounding_section(
                g_rows, start_ordinal=start,
            )
        except Exception as exc:  # best-effort — never lose the pack
            grounding_note = f"desk-grounding re-render failed: {exc!r}"
            g_rows = []
        run_chars = info["ground_blocks"].get("block_chars")
        divergences.append(
            "DESK GROUNDING section re-rendered from CURRENT DB state "
            f"(run-time {run_chars} chars vs replay {len(grounding_text)} — "
            "prior read / situation ages / baselines have advanced since the run)"
        )

    parts = []
    if preamble:
        parts.append(preamble)
    parts.append(slice_prompt)
    user_prompt = "\n".join(parts)
    if grounding_text:
        user_prompt = f"{user_prompt}\n\n{grounding_text}"

    return {
        "user_prompt": user_prompt,
        "slice_prompt_chars": len(slice_prompt),
        "slice_expected_chars": slice_expected,
        "orient_stats": stats,
        "kept": len(sliced),
        "derived": len(derived),
        "preamble_chars": len(preamble) if preamble else 0,
        "grounding_chars": len(grounding_text),
        "grounding_note": grounding_note,
        "divergences": divergences,
        "info": info,
    }


def synthesis_llm_call(trace: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The run's SYNTHESIS llm_calls receipt = the last non-judge entry."""
    calls = [
        c for c in (trace.get("llm_calls") or [])
        if not c.get("leg") and c.get("status") == "success"
    ]
    return calls[-1] if calls else None


# ---------------------------------------------------------------------------
# Per-analyst markdown
# ---------------------------------------------------------------------------


def _descriptor_temperature(descriptor_body: Mapping[str, Any]) -> str:
    """The synthesis temperature: descriptor value, else the builder default
    (analyst_deps_builder.py:523)."""
    t = ((descriptor_body.get("method") or {}).get("llm") or {}).get("temperature")
    return str(t) if t is not None else "0.2(default)"


def unit_call_params_md(descriptor_body: Mapping[str, Any]) -> str:
    method = descriptor_body.get("method") or {}
    llm = method.get("llm") or {}
    primary_ref = ((llm.get("primary") or {}).get("raw")) or "(unset)"
    comp = db_json(
        "SELECT body->'config'->'model_name'->>'raw' AS model, "
        "       body->'config'->'api_endpoint'->>'raw' AS endpoint, "
        "       body->'config'->'max_tokens'->>'raw' AS component_max_tokens, "
        "       body->'config'->'timeout_seconds'->>'raw' AS timeout_seconds "
        f"FROM stack_components WHERE component_id = {sql_lit(primary_ref)} "
        "AND is_head"
    )
    c = comp[0] if comp else {}
    lines = [
        "## Call parameters (synthesis call)",
        "",
        f"- model route: `{primary_ref}` -> model `{c.get('model')}` at "
        f"`{c.get('endpoint')}` (stack_components head; handler class = "
        "VLLMProviderHandler — component id ends `.openai_compat`, "
        "`runtime/analyst_deps_builder.infer_llm_subprovider` "
        "analyst_deps_builder.py:2317-2357)",
        f"- temperature: `{_descriptor_temperature(descriptor_body)}` — "
        "descriptor `method.llm.temperature`, read at "
        "analyst_deps_builder.py:523 (default 0.2), passed at "
        "inline_target.py:3464 and SENT on the wire (vllm.py:114-117)",
        f"- max_tokens: `{llm.get('max_tokens')}` — descriptor "
        "`method.llm.max_tokens` (analyst_deps_builder.py:522, "
        "inline_target.py:3463) but NOT SENT on the wire: the vLLM handler "
        "omits `max_tokens` unless `LEGBA_LLM_SEND_MAX_TOKENS` is set "
        "(vllm.py:118-131; env is UNSET in the live runtime) — the server "
        "serves its own output budget",
        f"- request timeout: `{c.get('timeout_seconds')}`s (stack component); "
        f"invoke timeout `{method.get('timeout_seconds')}`s (descriptor)",
        f"- token budget/day: `{method.get('budget_tokens_per_day')}`",
        "- input-token budget for the signals block: env "
        f"`LEGBA_LLM_INPUT_TOKEN_BUDGET` = {os.getenv('LEGBA_LLM_INPUT_TOKEN_BUDGET')}"
        " (chars/4 estimator, _llm_budget.py)",
        "- messages on the wire: `[{role: system, content: <SYSTEM PROMPT "
        "below>}, {role: user, content: <USER PROMPT below>}]` "
        "(base.py:712-728 prepends the system message; "
        "inline_target.py:2217-2224 builds the one-message conversation)",
    ]
    return "\n".join(lines)


def render_unit_file(
    out_dir: Path, analyst_id: str, token: str,
) -> dict[str, Any] | None:
    reg = registry_get(f"/api/v1/registry/descriptors/analyst/{analyst_id}", token)
    body = reg.get("body") or {}
    system_prompt, sys_notes = effective_unit_system_prompt(body, analyst_id)

    traces = fetch_traces(analyst_id)
    if not traces:
        (out_dir / f"{analyst_id}.md").write_text(
            f"# {analyst_id}\n\nNo successful trace with inputs found — "
            "nothing to replay.\n"
        )
        return None
    primary, verification = pick_traces(traces)

    replay = replay_unit_user_prompt(analyst_id, body, primary)
    user_prompt = replay["user_prompt"]
    sha, chars = wire_digest(system_prompt, user_prompt)
    call = synthesis_llm_call(primary)
    live_sha = (call or {}).get("prompt_sha256")
    match = "MATCH" if live_sha and sha == live_sha else "MISMATCH"

    # Independent verification on the cleanest recent trace, if different.
    verify_line = "no fully-replayable recent trace (see divergences)"
    if verification is not None:
        vrep = replay_unit_user_prompt(analyst_id, body, verification)
        vsha, _ = wire_digest(system_prompt, vrep["user_prompt"])
        vcall = synthesis_llm_call(verification)
        vlive = (vcall or {}).get("prompt_sha256")
        vmatch = "MATCH" if vlive and vsha == vlive else "MISMATCH"
        verify_line = (
            f"cleanest recent trace {verification['run_id']} "
            f"({verification['run_started_at']}): digest {vmatch}"
        )

    info = replay["info"]
    lines: list[str] = []
    lines.append(f"# {analyst_id} — live prompt surfaces")
    lines.append("")
    lines.append(f"- descriptor: `{reg.get('descriptor_id')}` version "
                 f"`{str(reg.get('version'))[:16]}…` state `{reg.get('state')}`")
    lines.append(f"- replayed trace: `{primary['run_id']}` started "
                 f"`{primary['run_started_at']}` target `{primary.get('target_id')}`")
    if call:
        lines.append(
            f"- live call receipt: model `{call.get('model')}` "
            f"prompt_tokens {call.get('prompt_tokens')} completion "
            f"{call.get('completion_tokens')} prompt_sha256 `{live_sha}`"
        )
    lines.append(f"- replayed digest: `{sha}` -> **{match}** "
                 "(sha256 over the wire JSON, run_accounting.prompt_digest)")
    lines.append(f"- byte-verification: {verify_line}")
    lines.append("")
    lines.append("## Divergences from the live bytes (this replay)")
    lines.append("")
    if replay["divergences"]:
        for d in replay["divergences"]:
            lines.append(f"- {d}")
    else:
        lines.append("- none — every part reconstructed from persisted state")
    if replay["grounding_note"]:
        lines.append(f"- {replay['grounding_note']}")
    lines.append("")
    lines.append("## System-prompt resolution")
    lines.append("")
    for n in sys_notes:
        lines.append(f"- {n}")
    lines.append("")
    lines.append(unit_call_params_md(body))
    lines.append("")
    lines.append(f"## SYSTEM PROMPT — exact bytes ({len(system_prompt)} chars)")
    lines.append("")
    lines.append("````text")
    lines.append(system_prompt)
    lines.append("````")
    lines.append("")
    lines.append(
        f"## USER PROMPT — replayed bytes ({len(user_prompt)} chars; "
        f"slice render {replay['slice_prompt_chars']} vs run-time "
        f"{replay['slice_expected_chars']}; preamble {replay['preamble_chars']}; "
        f"desk grounding {replay['grounding_chars']})"
    )
    lines.append("")
    lines.append("````text")
    lines.append(user_prompt)
    lines.append("````")
    lines.append("")
    if info["has_gather"]:
        from legba.data.analysts import inline_target
        lines.append("## GATHER phase (this analyst runs an agentic gather)")
        lines.append("")
        lines.append(
            "The gather rounds are separate LLM calls whose system prompt is "
            "the SYSTEM PROMPT above + the suffix below "
            "(inline_target.py:2845); gathered corpus blocks are prepended to "
            "the synthesis prompt as extra [N] ordinals and are not "
            "reconstructable post-hoc."
        )
        lines.append("")
        lines.append("````text")
        lines.append(inline_target._GATHER_SYSTEM_SUFFIX)
        lines.append("````")
        lines.append("")
    (out_dir / f"{analyst_id}.md").write_text("\n".join(lines))
    return {
        "analyst": analyst_id,
        "trace": str(primary["run_id"]),
        "total_chars": len(system_prompt) + len(user_prompt),
        "match": match,
        "verify_line": verify_line,
        "params": (
            f"temp={_descriptor_temperature(body)} "
            "max_tokens=not-sent model=InnoGPT-1"
        ),
    }


# ---------------------------------------------------------------------------
# The faithfulness judge
# ---------------------------------------------------------------------------


class _CaptureJudge:
    """Stand-in judge: records every (system, user) pair the REAL _run_judge
    sends, and answers with all-supported verdicts so every partition runs."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def chat_complete(self, messages, **kw):  # type: ignore[no-untyped-def]
        user = messages[0]["content"] if messages else ""
        self.calls.append({
            "system": kw.get("system") or "",
            "user": user,
            "max_tokens": kw.get("max_tokens"),
            "temperature": kw.get("temperature"),
        })
        n = 0
        for m in re.finditer(r"(?m)^(\d+)\. ", user):
            n = max(n, int(m.group(1)))
        verdicts = json.dumps({"verdicts": ["supported"] * n})
        return SimpleNamespace(content=verdicts)


def pick_judge_example() -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(critique_row, finding_row) — the most recent llm-judged UNIT critique
    whose finding still exists and carries source_text-bearing citations."""
    crits = db_json(
        "SELECT id, analyst_id, run_id, created_at, "
        "       data->>'analyzed_output_id' AS analyzed_output_id, "
        "       data->'data'->'verification' AS verification "
        "FROM analyst_outputs WHERE kind = 'critique' "
        "  AND data->'data'->'verification'->>'judge_status' = 'llm' "
        "ORDER BY created_at DESC LIMIT 40"
    )
    best: tuple[int, dict[str, Any], dict[str, Any]] | None = None
    for crit in crits:
        fid = crit.get("analyzed_output_id")
        if not fid:
            continue
        frows = db_json(
            "SELECT id, analyst_id, target_id, body, "
            "       data->'data'->'citations' AS citations "
            f"FROM analyst_outputs WHERE id = {sql_lit(fid)}"
        )
        if not frows:
            continue
        finding = frows[0]
        cits = finding.get("citations") or []
        sourced = sum(
            1 for c in cits
            if isinstance(c, dict) and c.get("signal_id") and c.get("source_text")
        )
        if sourced == 0 or any(
            isinstance(c, dict) and c.get("ref_kind") == "finding" for c in cits
        ):
            continue
        # Prefer the RICHEST recent example (most source_text-bearing
        # citations — a fuller truncation annex); newest wins ties.
        if best is None or sourced > best[0]:
            best = (sourced, crit, finding)
    if best is None:
        return None
    return best[1], best[2]


def render_judge_file(out_dir: Path) -> dict[str, Any] | None:
    from legba.data.provenance import verify
    from legba.data.provenance.judge_evidence import _marker_to_evidence

    picked = pick_judge_example()
    lines: list[str] = []
    lines.append("# The faithfulness judge — live prompt surfaces")
    lines.append("")
    lines.append(
        "- LIVE system prompt: `_GENERIC_JUDGE_SYSTEM` (verify.py:4855). "
        "Profile resolution: env `LEGBA_JUDGE_PROMPT_PROFILE` -> `current` "
        "default (verify.py:1347, :4898-4907). The `independent` profile is "
        "**STAGED, NOT LIVE** (env unset in the runtime container)."
    )
    lines.append(
        "- judge route: env `LEGBA_JUDGE_STACK_REF=llm.judge."
        "cerebras_gemma4_31b.openai_compat` (ladder rung 1, "
        "analyst_deps_builder.py:2488-2606) -> model `gemma-4-31b` at "
        "`https://api.cerebras.ai`, stack-component timeout 90s."
    )
    lines.append(
        "- call params: `max_tokens=16384, temperature=0.0` at the ONE judge "
        "call site `_judge_claim_partition` (verify.py:4932-4947). NOTE: the "
        "component id ends `.openai_compat` so the vLLM handler serves it, "
        "and that handler DROPS `max_tokens` from the wire payload unless "
        "`LEGBA_LLM_SEND_MAX_TOKENS` is set (vllm.py:118-131; env UNSET) — "
        "the requested 16384 cap is NOT actually sent to Cerebras. "
        "`temperature=0.0` IS sent (vllm.py:114-117)."
    )
    lines.append("")
    lines.append(
        f"## LIVE system prompt — `_GENERIC_JUDGE_SYSTEM` "
        f"({len(verify._GENERIC_JUDGE_SYSTEM)} chars)"
    )
    lines.append("")
    lines.append("````text")
    lines.append(verify._GENERIC_JUDGE_SYSTEM)
    lines.append("````")
    lines.append("")
    lines.append(
        f"## STAGED (NOT LIVE) A/B variant — `_INDEPENDENT_JUDGE_SYSTEM` "
        f"({len(verify._INDEPENDENT_JUDGE_SYSTEM)} chars; profile "
        "`independent`, env `LEGBA_JUDGE_PROMPT_PROFILE`)"
    )
    lines.append("")
    lines.append("````text")
    lines.append(verify._INDEPENDENT_JUDGE_SYSTEM)
    lines.append("````")
    lines.append("")

    result: dict[str, Any] = {"analyst": "judge_generic", "trace": "-",
                              "total_chars": 0, "match": "-", "params":
                              "temp=0.0 max_tokens=16384(requested,not sent) "
                              "model=gemma-4-31b"}
    annex_rows: list[dict[str, Any]] = []
    if picked is None:
        lines.append("No recent llm-judged unit critique found to replay.")
    else:
        crit, finding = picked
        body = finding.get("body") or ""
        citations = finding.get("citations") or []
        judge = _CaptureJudge()
        try:
            asyncio.run(verify._run_judge(
                judge, body=body, citations=citations, judge_prompt_profile=None,
            ))
        except Exception as exc:
            lines.append(f"(judge replay stopped early: {exc!r} — the calls "
                         "captured before the stop are shown)")
            lines.append("")
        # Compare against the producing run's verify_judge receipt(s).
        live = db_json(
            "SELECT c AS call FROM analyst_traces, "
            "jsonb_array_elements(llm_calls) c "
            f"WHERE run_id = {sql_lit(crit.get('run_id'))} "
            "  AND c->>'leg' = 'verify_judge'"
        )
        live_shas = [r["call"].get("prompt_sha256") for r in live]
        lines.append("## REAL rendered judge user prompt(s) — replayed through "
                     "`verify._run_judge` for a live finding")
        lines.append("")
        lines.append(
            f"- finding: `{finding['id']}` ({finding.get('analyst_id')} @ "
            f"{finding.get('target_id')}); critique `{crit['id']}` at "
            f"`{crit.get('created_at')}`; producing run `{crit.get('run_id')}`"
        )
        lines.append(
            f"- live verify_judge receipts on that run: {len(live_shas)} "
            f"call(s), sha256 {live_shas}"
        )
        lines.append("")
        overall = "MISMATCH"
        for i, call in enumerate(judge.calls, start=1):
            sha, _ = wire_digest(call["system"], call["user"])
            m = "MATCH" if sha in live_shas else "MISMATCH"
            if m == "MATCH":
                overall = "MATCH"
            sys_name = (
                "_GENERIC_JUDGE_SYSTEM"
                if call["system"] == verify._GENERIC_JUDGE_SYSTEM
                else "(specialized rubric — survey/absence branch)"
            )
            lines.append(
                f"### Judge call {i} — system={sys_name}, "
                f"user {len(call['user'])} chars, replay sha `{sha}` "
                f"-> **{m}** vs live receipts"
            )
            lines.append("")
            if call["system"] != verify._GENERIC_JUDGE_SYSTEM:
                lines.append("````text")
                lines.append(call["system"])
                lines.append("````")
                lines.append("")
            lines.append("````text")
            lines.append(call["user"])
            lines.append("````")
            lines.append("")
        result["trace"] = str(crit.get("run_id"))
        result["total_chars"] = sum(len(c["user"]) for c in judge.calls)
        result["match"] = overall

        # ---- CAPS ANNEX ---------------------------------------------------
        evidence = _marker_to_evidence(citations)
        lines.append("## CAPS ANNEX — evidence truncation")
        lines.append("")
        lines.append("Constants (src/legba/data/provenance/verify.py:1715-1739):")
        lines.append("")
        lines.append(f"- `_EVIDENCE_SOURCE_CHARS` = {verify._EVIDENCE_SOURCE_CHARS} "
                     "(SOURCE portion of one evidence entry)")
        lines.append(f"- `_EVIDENCE_TOTAL_CHARS` = {verify._EVIDENCE_TOTAL_CHARS} "
                     "(whole evidence entry)")
        lines.append(f"- `_EVIDENCE_LEGACY_CHARS` = {verify._EVIDENCE_LEGACY_CHARS} "
                     "(entries with NO source_text)")
        lines.append(f"- `_EVIDENCE_GROUNDING_CHARS` = "
                     f"{verify._EVIDENCE_GROUNDING_CHARS} (desk-grounding blocks)")
        lines.append(
            "- build-time capture caps (inline_target.py:304/:327): citation "
            "snippet 1500 chars; `source_text` 3200 chars "
            "(`source_truncated` stamped when the cleaned article exceeded it)"
        )
        lines.append("")
        lines.append("Per-citation, for the rendered example above:")
        lines.append("")
        lines.append("| marker | signal_id | source_text stored | "
                     "source_truncated at build | judge shown (whole entry) | "
                     "shown/stored |")
        lines.append("|---|---|---|---|---|---|")
        for c in citations:
            if not isinstance(c, dict):
                continue
            marker = c.get("marker")
            n = None
            m = re.search(r"\[(\d+)\]", str(marker or ""))
            if m:
                n = int(m.group(1))
            shown = len(evidence.get(n, "")) if n is not None else 0
            stored = len(c.get("source_text") or "")
            ratio = f"{shown / stored:.2f}" if stored else "-"
            row = {
                "marker": marker, "signal_id": c.get("signal_id"),
                "stored": stored, "trunc": c.get("source_truncated"),
                "shown": shown, "ratio": ratio,
            }
            annex_rows.append(row)
            lines.append(
                f"| {marker} | {c.get('signal_id') or c.get('ref_kind')} | "
                f"{stored} | {bool(c.get('source_truncated'))} | {shown} | "
                f"{ratio} |"
            )
        lines.append("")
        lines.append(
            "`judge shown` = the FULL evidence-map entry for that ordinal "
            "(OUTLET/SOURCE/Analyst-summary lines assembled by "
            "`judge_evidence._marker_to_evidence`), capped at "
            "`_EVIDENCE_TOTAL_CHARS`; a ratio > 1.0 means the entry carries "
            "labels + summary on top of the stored source excerpt."
        )
    (out_dir / "judge_generic.md").write_text("\n".join(lines))
    result["annex_rows"] = annex_rows
    return result


# ---------------------------------------------------------------------------
# Composition + journal — best effort, loudly labeled
# ---------------------------------------------------------------------------


def render_composition_file(out_dir: Path, token: str) -> dict[str, Any] | None:
    from legba.data.analysts import meta_findings_synthesizer as meta

    analyst_id = "country_composition"
    reg = registry_get(f"/api/v1/registry/descriptors/analyst/{analyst_id}", token)
    body = reg.get("body") or {}
    traces = fetch_traces(analyst_id)
    primary = traces[0] if traces else None

    lines: list[str] = []
    lines.append("# composition — meta_findings_synthesizer prompt surfaces "
                 "(BEST EFFORT)")
    lines.append("")
    lines.append(
        "The composition prompt is assembled inside "
        "`meta_findings_synthesizer.run_method` from the finding blocks PLUS "
        "several runtime-conditional sections (salience lead block, freshness "
        "advisory, continuity blocks, contested-facts block, coverage blocks "
        "— meta_findings_synthesizer.py:1509/3208/2332/3432/3490). Those "
        "sections read live state at run time and are NOT byte-replayed here; "
        "this file shows the exact SYSTEM prompts, the call params, and the "
        "CORE user-prompt render (real `_orient` + `_render_user_prompt`) over "
        "the replayed run's actual input findings."
    )
    lines.append("")
    if primary is not None:
        call = synthesis_llm_call(primary)
        lines.append(
            f"- replayed trace: `{primary['run_id']}` started "
            f"`{primary['run_started_at']}` target `{primary.get('target_id')}`; "
            f"live receipt prompt_chars {(call or {}).get('prompt_chars')} "
            f"sha `{(call or {}).get('prompt_sha256')}`"
        )
    llm = (body.get("method") or {}).get("llm") or {}
    lines.append(
        f"- call params: temperature `{llm.get('temperature')}` (sent), "
        f"max_tokens `{llm.get('max_tokens')}` (NOT sent — vllm.py:118-131), "
        f"route `{(llm.get('primary') or {}).get('raw')}` -> InnoGPT-1"
    )
    lines.append("")
    for name in ("_COMPOSITION_SYSTEM", "_REGION_COMPOSITION_SYSTEM",
                 "_WORLD_OVER_REGIONS_SYSTEM", "_THEMATIC_COMPOSITION_SYSTEM",
                 "_SYSTEM_PROMPT"):
        prompt = getattr(meta, name, None)
        if not isinstance(prompt, str):
            continue
        which = {
            "_COMPOSITION_SYSTEM": "per-COUNTRY composition (country_composition)",
            "_REGION_COMPOSITION_SYSTEM": "per-REGION composition",
            "_WORLD_OVER_REGIONS_SYSTEM": "WORLD-over-regions composition",
            "_THEMATIC_COMPOSITION_SYSTEM": "THEMATIC composition",
            "_SYSTEM_PROMPT": "legacy global meta (non-composition fallback)",
        }[name]
        lines.append(f"## SYSTEM PROMPT `{name}` — {which} ({len(prompt)} chars)")
        lines.append("")
        lines.append("````text")
        lines.append(prompt)
        lines.append("````")
        lines.append("")

    if primary is not None:
        refs = primary.get("input_row_refs") or []
        id_list = ", ".join(sql_lit(r) for r in refs)
        rows = db_json(
            "SELECT id, analyst_id, title, body, confidence, data, "
            "       produced_at, target_id "
            f"FROM analyst_outputs WHERE id IN ({id_list})"
        )
        try:
            sliced, _derived, contributing = meta._orient(rows)
            core = meta._render_user_prompt(
                sliced, contributing, include_source_ids=True,
            )
            lines.append(
                f"## CORE USER-PROMPT RENDER — replayed input findings "
                f"({len(core)} chars; run-time full prompt was "
                f"{(classify_trace(primary)['render'] or {}).get('prompt_chars')}"
                " chars before the conditional blocks listed above)"
            )
            lines.append("")
            lines.append(
                "NOTE: rows are re-fetched raw from `analyst_outputs`; the "
                "live READ_SLICE projects the verify-floored "
                "`effective_confidence` fold and tier/continuity markers, so "
                "block scores/labels here may differ from the run."
            )
            lines.append("")
            lines.append("````text")
            lines.append(core)
            lines.append("````")
        except Exception as exc:
            lines.append(f"core render failed: {exc!r}")
    (out_dir / "composition.md").write_text("\n".join(lines))
    return {"analyst": "composition", "trace":
            str(primary["run_id"]) if primary else "-", "total_chars": 0,
            "match": "best-effort", "params":
            f"temp={llm.get('temperature')} route={(llm.get('primary') or {}).get('raw')}"}


def render_journal_file(out_dir: Path, token: str) -> dict[str, Any] | None:
    from legba.data.analysts import journal_assessor as ja

    analyst_id = "journal_assessor"
    try:
        reg = registry_get(
            f"/api/v1/registry/descriptors/analyst/{analyst_id}", token,
        )
        body = reg.get("body") or {}
    except Exception:
        body = {}
    traces = fetch_traces(analyst_id)
    primary = traces[0] if traces else None
    lines: list[str] = []
    lines.append("# journal narrate — prompt surfaces (BEST EFFORT)")
    lines.append("")
    lines.append(
        "The journal's narrate call is the FINAL turn of a live multi-round "
        "GATHER conversation (its user side = accumulated tool calls/results "
        "across the run) — the conversation is not persisted, so the narrate "
        "user prompt is NOT replayable. The system prompt and params below "
        "are the exact live surfaces."
    )
    lines.append("")
    llm = (body.get("method") or {}).get("llm") or {}
    lines.append(
        f"- call params: temperature `{llm.get('temperature')}`; narrate "
        f"max_tokens `{llm.get('max_tokens')}` (sent ONLY when the narrate "
        "plane is Anthropic — analyst_deps_builder.py:621-656; on the core "
        "vLLM plane it is not sent); routes: primary "
        f"`{(llm.get('primary') or {}).get('raw')}`, narrate "
        f"`{(llm.get('narrate') or {}).get('raw') or '(primary)'}`"
    )
    if primary is not None:
        calls = [c for c in (primary.get("llm_calls") or [])]
        lines.append(
            f"- latest run `{primary['run_id']}` made {len(calls)} LLM call(s) "
            f"on `{calls[0].get('component_id') if calls else '?'}` — the "
            "narrate call is the last one "
            f"(prompt_chars {calls[-1].get('prompt_chars') if calls else '?'}, "
            f"sha `{calls[-1].get('prompt_sha256') if calls else '?'}`)"
        )
    lines.append("")
    # The journal persona prompt is a prompt-module constant
    # (journal_assessor.PROMPT_MODULE_PATH = "legba.prompts.journal_assessor:
    # JOURNAL_SYSTEM"), resolved by the deps builder the same way a unit's
    # prompt_module would be.
    import importlib
    spec = getattr(ja, "PROMPT_MODULE_PATH", "")
    if isinstance(spec, str) and ":" in spec:
        mod_name, _, attr = spec.partition(":")
        try:
            val = getattr(importlib.import_module(mod_name), attr)
        except Exception as exc:
            val = f"(failed to resolve {spec!r}: {exc!r})"
        lines.append(f"## `{spec}` — the narrate/persona SYSTEM prompt "
                     f"({len(val)} chars)")
        lines.append("")
        lines.append("````text")
        lines.append(val)
        lines.append("````")
        lines.append("")
    (out_dir / "journal_narrate.md").write_text("\n".join(lines))
    return {"analyst": "journal_narrate", "trace":
            str(primary["run_id"]) if primary else "-", "total_chars": 0,
            "match": "best-effort", "params": f"temp={llm.get('temperature')}"}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--analyst", action="append", default=None,
                        help="limit to specific analyst id(s)")
    parser.add_argument("--registry-token", default=None)
    parser.add_argument("--input-token-budget",
                        default=os.getenv("LEGBA_LLM_INPUT_TOKEN_BUDGET",
                                          DEFAULT_INPUT_TOKEN_BUDGET))
    parser.add_argument("--units", action="store_true")
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--composition", action="store_true")
    parser.add_argument("--journal", action="store_true")
    args = parser.parse_args(argv)

    # Match the live runtime's input budget BEFORE any _orient call.
    os.environ["LEGBA_LLM_INPUT_TOKEN_BUDGET"] = str(args.input_token_budget)

    everything = not (args.units or args.judge or args.composition or args.journal)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    token = _registry_token(args.registry_token)

    index: list[dict[str, Any]] = []
    if args.units or everything:
        targets = args.analyst or list(UNIT_ANALYSTS)
        for aid in targets:
            print(f"[units] {aid} …", flush=True)
            try:
                row = render_unit_file(out_dir, aid, token)
            except Exception as exc:
                print(f"  FAILED: {exc!r}", flush=True)
                row = {"analyst": aid, "trace": "-", "total_chars": 0,
                       "match": f"ERROR {type(exc).__name__}", "params": "-"}
            if row:
                index.append(row)
                print(f"  -> {row['match']}", flush=True)
    if args.judge or everything:
        print("[judge] …", flush=True)
        row = render_judge_file(out_dir)
        if row:
            index.append({k: v for k, v in row.items() if k != "annex_rows"})
            print(f"  -> {row['match']}", flush=True)
            if row.get("annex_rows"):
                print("  truncation table:", flush=True)
                for r in row["annex_rows"]:
                    print(f"    {r}", flush=True)
    if args.composition or everything:
        print("[composition] …", flush=True)
        row = render_composition_file(out_dir, token)
        if row:
            index.append(row)
    if args.journal or everything:
        print("[journal] …", flush=True)
        row = render_journal_file(out_dir, token)
        if row:
            index.append(row)

    if everything and not args.analyst:
        lines = ["# Prompt pack index", ""]
        lines.append(
            "| file | trace replayed | total prompt chars | digest | params |"
        )
        lines.append("|---|---|---|---|---|")
        for r in index:
            lines.append(
                f"| {r['analyst']}.md | {r['trace']} | {r['total_chars']} | "
                f"{r['match']} | {r['params']} |"
            )
        (out_dir / "INDEX.md").write_text("\n".join(lines) + "\n")
    else:
        print("(partial run — INDEX.md left untouched)", flush=True)
    print(f"pack written to {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
