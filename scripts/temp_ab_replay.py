#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""temp_ab_replay.py — P0-3b offline temperature A/B (0.2 vs 1.0) on the core plane.

Measures the effect of following the gpt-oss-120b model-card sampling
recommendation (temperature=1.0) versus the plane's live client-side pin (0.2)
by REPLAYING captured real inputs through the SAME prompt-assembly code path at
both temperatures, with the faithfulness judge held fixed (temperature 0.0 is
HARDCODED inside ``verify._judge_claim_partition`` — both arms share the same
yardstick by construction). Spec: ``planning/LLM_SAMPLING_AUDIT_2026-07-24.md``
§5; the replay machinery is the unit_optimizer's paired MEASURE stage
(``runtime/dapr_workflow/gepa.py::_candidate_faithfulness_for_finding``), which
this script REUSES rather than reimplements:

  * slice reconstruction — a finding's ``derived_from`` signal ids →
    ``gepa._fetch_signal_render_rows`` → ``inline_target._render_user_prompt``
    (the byte-level renderer live REASON uses, including the Target/Number-of-
    signals header);
  * system prompt — the deps-builder's exact resolution order
    (``method.prompt_module`` → inline ``method.system_prompt`` →
    ``resolve_promoted_system_prompt`` → ``with_preamble_if_absent``); journal
    voices resolve ``prompt_module`` → ``JOURNAL_SYSTEM`` with NO preamble wrap,
    mirroring ``_build_journal_assessor``;
  * post-processing — ``_coerce_finding`` → ``_normalize_citation_markers`` →
    ``_build_citation_index`` → ``_extract_citations`` →
    ``verify_finding_faithfulness`` (judge pinned 0.0 inside verify), identical
    to the GEPA candidate arm;
  * wire layer — the REAL ``VLLMProviderHandler`` built by
    ``build_llm_handler_from_stack_component`` from the live registry row, so
    the payload semantics (temperature always sent; top_p/top_k/max_tokens
    omitted) are the production ones.

CAPTURE-FIDELITY CAVEATS (same as the GEPA replay): the live REASON prompt may
additionally carry a grounding preamble and agentic GATHER context that are not
persisted; the replay omits both, for BOTH arms — the paired design compares
0.2-replay vs 1.0-replay on IDENTICAL inputs, never replay vs live. The
journal-voice arm reconstructs a field-notes block from the entry's cited
substrate refs (the original in-persona field notes are not persisted), then
applies the REAL per-tier narrate instruction + persona.

SAFETY: read-only DB access (the sampling/slice pool opens with
``default_transaction_read_only=on`` server-enforced); no finding is persisted;
no descriptor is touched; nothing is rebuilt. LLM calls go to the $0 core plane
(``llm.primary.openai_compat``) with bounded concurrency (default 3) so live
analyst traffic is not starved.

USAGE
-----
    # 1. Draw the frozen case sample (read-only; writes only the JSONL file).
    python3 scripts/temp_ab_replay.py sample \
        --out planning/temp_ab_inputs_2026-07-24.jsonl \
        --units-per-analyst 5 --journal 8

    # 2. Run both arms (idempotent + resumable — completed (case, arm) rows in
    #    the results file are skipped on re-run; failures are retried).
    python3 scripts/temp_ab_replay.py run \
        --inputs planning/temp_ab_inputs_2026-07-24.jsonl \
        --out planning/temp_ab_results_2026-07-24.jsonl \
        --env-file /path/to/deployment/.env --concurrency 3

    # 3. Aggregate + paired analysis (markdown to stdout).
    python3 scripts/temp_ab_replay.py report \
        --results planning/temp_ab_results_2026-07-24.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

CORE_COMPONENT = "llm.primary.openai_compat"
ARMS = (0.2, 1.0)

#: Rendered-prompt char budget for the replay user prompt. The live ORIENT
#: phase bounds the slice by LEGBA_LLM_INPUT_TOKEN_BUDGET (32k tokens); we
#: bound the reconstructed slice to ~26k tokens-worth of chars (chars/3.5)
#: so a 120-signal corroborator slice can't blow the server context. The
#: truncation is DETERMINISTIC (prefix of the stored derived_from order) and
#: shared by both arms, so pairing is unaffected.
_MAX_PROMPT_CHARS = 90_000
_MAX_NOTES_CHARS = 60_000

# The unit analysts named by the audit §5 design (bounded reasoning units on
# the core plane, builder-default temperature 0.2).
UNIT_ANALYSTS = (
    "escalation",
    "military_posture",
    "internal_stability",
    "leadership_transition",
    "economic_coercion",
    "proliferation_watch",
    "narrative_coordination",
    "energy_security",
    "corpus_researcher",
    "cross_doc_corroborator",
)

_JOURNAL_REF_RE = re.compile(r"\[\[ref:([0-9a-fA-F\-]{8,})\]\]")


def _load_env(env_file: str | None) -> None:
    """Load LEGBA_* connection env from ``env_file`` (or repo-root .env)."""
    try:
        from dotenv import load_dotenv
    except Exception:  # pragma: no cover - dotenv optional
        return
    path = Path(env_file) if env_file else (_REPO_ROOT / ".env")
    if path.exists():
        load_dotenv(path, override=False)


# ---------------------------------------------------------------------------
# Degeneration / repetition metrics (audit §5.3)
# ---------------------------------------------------------------------------


def degeneration_metrics(text: str) -> dict[str, Any]:
    """Repetition markers over one output: repeated 4-gram rate, top 4-gram
    multiplicity, unique-bigram ratio, and a tail-loop detector (any normalized
    sentence of ≥30 chars appearing ≥3 times)."""
    words = (text or "").split()
    n = len(words)
    out: dict[str, Any] = {"len_chars": len(text or ""), "len_words": n}
    bigrams = list(zip(words, words[1:]))
    fourgrams = list(zip(words, words[1:], words[2:], words[3:]))
    out["unique_bigram_ratio"] = (
        round(len(set(bigrams)) / len(bigrams), 4) if bigrams else None
    )
    if fourgrams:
        counts = Counter(fourgrams)
        out["repeated_fourgram_rate"] = round(
            1.0 - (len(counts) / len(fourgrams)), 4
        )
        out["top_fourgram_count"] = counts.most_common(1)[0][1]
    else:
        out["repeated_fourgram_rate"] = None
        out["top_fourgram_count"] = 0
    sentences = [
        s.strip().lower()
        for s in re.split(r"[.!?\n]+", text or "")
        if len(s.strip()) >= 30
    ]
    sc = Counter(sentences)
    top = sc.most_common(1)[0][1] if sc else 0
    out["tail_loop"] = bool(top >= 3)
    out["max_sentence_repeat"] = top
    return out


# ---------------------------------------------------------------------------
# sample — draw + freeze the case set (read-only)
# ---------------------------------------------------------------------------

_UNIT_SAMPLE_SQL = """
    SELECT finding_id, analyst_id, target_id, title, derived_from,
           faithfulness_score, judge_status, body_len
      FROM (
        SELECT f.id::text AS finding_id,
               f.analyst_id,
               f.target_id,
               f.title,
               f.derived_from::text[] AS derived_from,
               length(f.body) AS body_len,
               v.faithfulness_score,
               v.judge_status,
               ROW_NUMBER() OVER (
                   PARTITION BY f.analyst_id
                   ORDER BY f.produced_at DESC, f.id DESC
               ) AS rn
          FROM analyst_outputs f
          LEFT JOIN LATERAL (
              SELECT (cr.data->>'overall_score')::real AS faithfulness_score,
                     cr.data->'data'->'verification'->>'judge_status' AS judge_status
                FROM analyst_outputs cr
               WHERE cr.kind = 'critique'
                 AND cr.data->>'analyzed_output_id' = f.id::text
                 AND cr.data->>'overall_score' IS NOT NULL
                 AND cr.title LIKE 'Faithfulness verify%'
               ORDER BY cr.produced_at DESC, cr.id DESC
               LIMIT 1
          ) v ON TRUE
         WHERE f.kind = 'finding'
           AND f.analyst_id = ANY($1::text[])
           AND f.superseded_by IS NULL
           AND f.produced_at > now() - interval '30 days'
           AND COALESCE(array_length(f.derived_from, 1), 0) BETWEEN 3 AND 130
      ) ranked
     WHERE rn <= $2
     ORDER BY analyst_id, rn
"""

_JOURNAL_SAMPLE_SQL = """
    SELECT id::text AS entry_id, analyst_id, entry_kind, title, target_id,
           cited_substrate_refs::text[] AS refs, length(body) AS body_len
      FROM journal_entries
     WHERE created_at > now() - interval '30 days'
       AND superseded_by IS NULL
       AND COALESCE(array_length(cited_substrate_refs, 1), 0) >= 4
     ORDER BY created_at DESC
     LIMIT $1
"""


async def cmd_sample(args: argparse.Namespace) -> int:
    import asyncpg

    from legba.data.config import PostgresConfig

    cfg = PostgresConfig.from_env()
    conn = await asyncpg.connect(
        host=cfg.host, port=cfg.port, user=cfg.user,
        password=cfg.password, database=cfg.database,
        server_settings={"default_transaction_read_only": "on"},
    )
    cases: list[dict[str, Any]] = []
    try:
        rows = await conn.fetch(
            _UNIT_SAMPLE_SQL, list(UNIT_ANALYSTS), int(args.units_per_analyst)
        )
        for r in rows:
            cases.append({
                "case_id": f"unit:{r['finding_id']}",
                "surface": "unit",
                "analyst_id": r["analyst_id"],
                "target_id": r["target_id"],
                "finding_id": r["finding_id"],
                "signal_ids": list(r["derived_from"] or []),
                "stored_faithfulness": (
                    float(r["faithfulness_score"])
                    if r["faithfulness_score"] is not None else None
                ),
                "stored_judge_status": r["judge_status"],
                "stored_body_len": r["body_len"],
                "stored_title": r["title"],
            })
        jrows = await conn.fetch(_JOURNAL_SAMPLE_SQL, int(args.journal))
        for r in jrows:
            cases.append({
                "case_id": f"journal:{r['entry_id']}",
                "surface": "journal",
                "analyst_id": r["analyst_id"] or "journal_assessor",
                "entry_kind": r["entry_kind"],
                "target_id": r["target_id"],
                "entry_id": r["entry_id"],
                "ref_ids": list(r["refs"] or []),
                "stored_body_len": r["body_len"],
                "stored_title": r["title"],
            })
    finally:
        await conn.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for c in cases:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    units = sum(1 for c in cases if c["surface"] == "unit")
    print(
        f"sampled {len(cases)} cases ({units} unit, {len(cases) - units} "
        f"journal-family) -> {out}"
    )
    return 0


# ---------------------------------------------------------------------------
# run — the paired replay
# ---------------------------------------------------------------------------


class _ArmContext:
    """Shared resolved state for the run: LLM handler, registry, pools."""

    def __init__(self) -> None:
        self.handler: Any = None
        self.registry: Any = None
        self.vault_store: Any = None
        self.ro_pool: Any = None
        self._prompt_cache: dict[str, str | None] = {}
        self._typed_cache: dict[str, dict[str, Any] | None] = {}

    async def open(self, component_id: str) -> None:
        import asyncpg

        from legba.data.config import PostgresConfig
        from legba.data.postgres import PostgresStore
        from legba.data.registry.credentials import CredentialVault
        from legba.runtime.analyst_deps_builder import (
            build_llm_handler_from_stack_component,
        )
        from legba.runtime.registry_client import RegistryHTTPClient

        cfg = PostgresConfig.from_env()
        # Read-only pool for ALL substrate reads this harness performs —
        # server-enforced (a stray write raises, never silently lands).
        self.ro_pool = await asyncpg.create_pool(
            host=cfg.host, port=cfg.port, user=cfg.user,
            password=cfg.password, database=cfg.database,
            min_size=1, max_size=4,
            server_settings={"default_transaction_read_only": "on"},
        )
        # The vault store only ever SELECTs (secret resolve) but uses the
        # production PostgresStore, mirroring gepa._resolve_candidate_arm.
        self.vault_store = PostgresStore(cfg)
        await self.vault_store.connect()
        vault = CredentialVault(self.vault_store)

        async def _secrets_resolve(secret_id: str) -> bytes:
            return await vault.resolve(secret_id)

        self.registry = RegistryHTTPClient()
        self.handler = await build_llm_handler_from_stack_component(
            component_id,
            registry_client=self.registry,
            secrets_resolve=_secrets_resolve,
        )

    async def close(self) -> None:
        if self.handler is not None and hasattr(self.handler, "on_deactivate"):
            try:
                await self.handler.on_deactivate(None)
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        if self.registry is not None:
            try:
                await self.registry.aclose()
            except Exception:  # noqa: BLE001
                pass
        if self.vault_store is not None:
            try:
                await self.vault_store.close()
            except Exception:  # noqa: BLE001
                pass
        if self.ro_pool is not None:
            try:
                await self.ro_pool.close()
            except Exception:  # noqa: BLE001
                pass

    async def typed_descriptor(self, analyst_id: str) -> dict[str, Any] | None:
        if analyst_id not in self._typed_cache:
            try:
                typed = await self.registry.get_descriptor_typed(
                    analyst_id, family="analyst"
                )
            except Exception:  # noqa: BLE001 - descriptor fetch best-effort
                typed = None
            self._typed_cache[analyst_id] = typed if isinstance(typed, dict) else None
        return self._typed_cache[analyst_id]

    async def unit_system_prompt(self, analyst_id: str) -> str:
        """The EXACT live prompt-resolution order for an inline_target unit
        (mirrors ``analyst_deps_builder._build_inline_target``):
        prompt_module → inline system_prompt → promoted GEPA candidate →
        tradecraft preamble wrap → kind-default fallback."""
        key = f"unit:{analyst_id}"
        if key in self._prompt_cache:
            return self._prompt_cache[key] or ""
        from legba.data.analysts._tradecraft import with_preamble_if_absent
        from legba.data.analysts.inline_target import _SYSTEM_PROMPT
        from legba.data.analysts.optimizer import resolve_promoted_system_prompt
        from legba.runtime.analyst_deps_builder import _resolve_prompt_module

        typed = await self.typed_descriptor(analyst_id)
        method = (typed or {}).get("method") or {}
        system_prompt = _resolve_prompt_module(method.get("prompt_module"))
        if system_prompt is None:
            inline = method.get("system_prompt")
            if isinstance(inline, str) and inline.strip():
                system_prompt = inline
        system_prompt = await resolve_promoted_system_prompt(
            self.ro_pool, analyst_id, default=system_prompt
        )
        system_prompt = with_preamble_if_absent(system_prompt)
        effective = system_prompt or _SYSTEM_PROMPT
        self._prompt_cache[key] = effective
        return effective

    async def journal_system_prompt(self, analyst_id: str) -> str:
        """Journal persona resolution (mirrors ``_build_journal_assessor``):
        prompt_module → JOURNAL_SYSTEM; NEVER preamble-wrapped (§4.2)."""
        key = f"journal:{analyst_id}"
        if key in self._prompt_cache:
            return self._prompt_cache[key] or ""
        from legba.runtime.analyst_deps_builder import _resolve_prompt_module

        typed = await self.typed_descriptor(analyst_id)
        method = (typed or {}).get("method") or {}
        system_prompt = _resolve_prompt_module(method.get("prompt_module"))
        if system_prompt is None:
            from legba.prompts.journal_assessor import JOURNAL_SYSTEM

            system_prompt = JOURNAL_SYSTEM
        self._prompt_cache[key] = system_prompt
        return system_prompt


async def _fetch_render_rows(ctx: _ArmContext, ids: list[str]) -> dict[str, dict]:
    """Signal id → render-shape row, via the GEPA fetch (reused verbatim)."""
    from legba.runtime.dapr_workflow.gepa import _fetch_signal_render_rows

    return await _fetch_signal_render_rows(ctx.ro_pool, ids)


def _bound_slice(rows: list[dict], render_one, budget: int) -> tuple[list[dict], bool]:
    """Deterministic prefix truncation of the slice to the char budget."""
    kept: list[dict] = []
    used = 0
    truncated = False
    for i, row in enumerate(rows, start=1):
        block = render_one(i, row)
        if kept and used + len(block) > budget:
            truncated = True
            break
        kept.append(row)
        used += len(block)
    return kept, truncated


async def _build_unit_input(ctx: _ArmContext, case: dict) -> dict | None:
    """(system, user, slice_rows, meta) for a unit case, or None (unbuildable)."""
    from legba.data.analysts.inline_target import (
        _render_signal,
        _render_user_prompt,
    )

    ordered_ids = [str(s) for s in case.get("signal_ids") or [] if s]
    if not ordered_ids:
        return None
    rows_by_id = await _fetch_render_rows(ctx, ordered_ids)
    slice_rows = [rows_by_id[sid] for sid in ordered_ids if sid in rows_by_id]
    if not slice_rows:
        return None
    slice_rows, truncated = _bound_slice(slice_rows, _render_signal, _MAX_PROMPT_CHARS)
    user_prompt = _render_user_prompt(slice_rows, case.get("target_id"))
    system = await ctx.unit_system_prompt(case["analyst_id"])
    typed = await ctx.typed_descriptor(case["analyst_id"])
    method = (typed or {}).get("method") or {}
    llm_block = method.get("llm") or {}
    max_tokens = llm_block.get("max_tokens") or 4096
    return {
        "system": system,
        "user": user_prompt,
        "slice_rows": slice_rows,
        "max_tokens": int(max_tokens),
        "n_signals_total": len(ordered_ids),
        "n_signals_used": len(slice_rows),
        "slice_truncated": truncated,
    }


def _render_note_line(idx: int, row: dict) -> str:  # idx unused; kept for _bound_slice
    from legba.data.analysts.inline_target import _clean_body_text

    data = row.get("data") or {}
    title = str(
        (data.get("title_en") if isinstance(data, dict) else None)
        or row.get("title") or "(untitled)"
    )[:300]
    snippet = ""
    if isinstance(data, dict):
        raw = (
            data.get("distilled_body") or data.get("raw_body")
            or data.get("summary") or data.get("description")
            or data.get("content_text") or data.get("snippet") or ""
        )
        if not isinstance(raw, str):
            raw = str(raw)
        snippet = _clean_body_text(raw)[:600]
    return (
        f"- [[ref:{row['id']}]] {title} "
        f"(source={row.get('source_url') or ''}, ingested={row.get('produced_at')})\n"
        f"  {snippet}"
    )


async def _build_journal_input(ctx: _ArmContext, case: dict) -> dict | None:
    """(system, user, offered_refs, meta) for a journal-family case.

    The original in-persona field notes are not persisted, so the notes block
    is RECONSTRUCTED from the entry's cited substrate refs (same snippet
    precedence as ``_render_signal``); the narrate instruction + persona are
    the REAL per-tier constants (``_NARRATE_INSTRUCTION_FOR`` /
    ``_entry_kind_for_analyst``), mirroring ``_narrate_with_tools`` closed-book.
    """
    from legba.data.analysts.journal_assessor import (
        _NARRATE_INSTRUCTION,
        _NARRATE_INSTRUCTION_FOR,
        _entry_kind_for_analyst,
    )

    ordered_ids = [str(s) for s in case.get("ref_ids") or [] if s]
    if not ordered_ids:
        return None
    rows_by_id = await _fetch_render_rows(ctx, ordered_ids)
    rows = [rows_by_id[sid] for sid in ordered_ids if sid in rows_by_id]
    if not rows:
        return None
    rows, truncated = _bound_slice(rows, _render_note_line, _MAX_NOTES_CHARS)
    notes = (
        "FIELD NOTES (reconstructed from this entry's cited substrate — each "
        "item carries its citable ref):\n\n"
        + "\n".join(_render_note_line(i, r) for i, r in enumerate(rows, start=1))
    )
    analyst_id = case.get("analyst_id") or "journal_assessor"
    instruction = _NARRATE_INSTRUCTION_FOR.get(
        _entry_kind_for_analyst(analyst_id), _NARRATE_INSTRUCTION
    )
    system = await ctx.journal_system_prompt(analyst_id)
    typed = await ctx.typed_descriptor(analyst_id)
    method = (typed or {}).get("method") or {}
    llm_block = method.get("llm") or {}
    max_tokens = llm_block.get("max_tokens") or 16384
    return {
        "system": system,
        "user": notes + instruction,
        "offered_refs": {str(r["id"]).lower() for r in rows},
        "max_tokens": int(max_tokens),
        "n_refs_total": len(ordered_ids),
        "n_refs_used": len(rows),
        "slice_truncated": truncated,
    }


async def _eval_unit_arm(
    ctx: _ArmContext, case: dict, built: dict, temperature: float,
    gen_timeout: float, judge_profile: str = "current",
) -> dict[str, Any]:
    """Generate one unit arm + post-process exactly like the GEPA candidate
    arm: coerce → normalize brackets → extract [N] citations → verify with the
    LLM judge (judge temperature hardcoded 0.0 inside verify).

    ``judge_profile`` (P2-4) selects the judge PROMPT PROFILE on the judge side
    (``current`` = the live calibrated prompt, ``independent`` = the staged
    adversarial-reviewer posture) so a future run can A/B judge profiles the
    same way this script A/B'd temperature."""
    from legba.data.analysts.inline_target import (
        _VARIANT_CITATION_RE,
        _build_citation_index,
        _coerce_finding,
        _extract_citations,
        _normalize_citation_markers,
    )
    from legba.data.provenance.verify import verify_finding_faithfulness

    t0 = time.monotonic()
    resp = await asyncio.wait_for(
        ctx.handler.chat_complete(
            [{"role": "user", "content": built["user"]}],
            system=built["system"],
            max_tokens=built["max_tokens"],  # inert at wire (vllm drops it)
            temperature=temperature,
        ),
        timeout=gen_timeout,
    )
    latency = round(time.monotonic() - t0, 2)
    raw = getattr(resp, "content", "") or ""
    usage = getattr(resp, "usage", None)
    rec: dict[str, Any] = {
        "latency_s": latency,
        "finish_reason": getattr(resp, "finish_reason", None),
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "raw_chars": len(raw),
    }
    if not raw.strip():
        rec["status"] = "empty_generation"
        return rec

    variant_brackets = len(_VARIANT_CITATION_RE.findall(raw))
    finding = _coerce_finding(raw, fallback_title="temp_ab_replay")
    json_parse_ok = not (
        "unstructured" in (finding.tags or []) or "coerce_failed" in (finding.tags or [])
    )
    body = _normalize_citation_markers(str(finding.body or ""))
    index = _build_citation_index(built["slice_rows"])
    citations, marker_count, resolved_count = _extract_citations(body, index)
    rec.update({
        "status": "ok",
        "json_parse_ok": json_parse_ok,
        "variant_bracket_count": variant_brackets,
        "marker_count": marker_count,
        "resolved_count": resolved_count,
        "citation_parse_failure": bool(marker_count > 0 and resolved_count == 0),
        "zero_markers": bool(marker_count == 0),
        "confidence": float(getattr(finding, "confidence", 0.0) or 0.0),
        **degeneration_metrics(body),
        "body_excerpt": body[:1500],
    })
    report = await verify_finding_faithfulness(
        body=body,
        citations=citations,
        judge_llm=ctx.handler,
        title=str(finding.title or ""),
        target_id=case.get("target_id"),
        judge_prompt_profile=judge_profile,
    )
    rec["faithfulness"] = {
        "score": round(float(report.faithfulness_score), 4),
        "checkable": report.checkable_claims,
        "supported": report.supported_claims,
        "unsupported": len(report.unsupported_spans),
        "judge_status": report.judge_status,
        "judge_unavailable_reason": report.judge_unavailable_reason,
        "judge_profile": judge_profile,
    }
    return rec


async def _eval_journal_arm(
    ctx: _ArmContext, case: dict, built: dict, temperature: float,
    gen_timeout: float,
) -> dict[str, Any]:
    """Generate one journal-voice arm + measure prose/ref integrity (no judge —
    journal is off-chain; its [[ref:uuid]] convention is graded for resolution
    against the offered refs, plus the #236 tool-call-leak predicate)."""
    from legba.data.analysts.journal_assessor import _is_tool_call_leak

    t0 = time.monotonic()
    resp = await asyncio.wait_for(
        ctx.handler.chat_complete(
            [{"role": "user", "content": built["user"]}],
            system=built["system"],
            max_tokens=built["max_tokens"],  # inert at wire (vllm drops it)
            temperature=temperature,
        ),
        timeout=gen_timeout,
    )
    latency = round(time.monotonic() - t0, 2)
    raw = getattr(resp, "content", "") or ""
    usage = getattr(resp, "usage", None)
    rec: dict[str, Any] = {
        "latency_s": latency,
        "finish_reason": getattr(resp, "finish_reason", None),
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "raw_chars": len(raw),
    }
    if not raw.strip():
        rec["status"] = "empty_generation"
        return rec
    offered = built["offered_refs"]
    refs = [m.group(1).lower() for m in _JOURNAL_REF_RE.finditer(raw)]
    resolved = sum(1 for r in refs if r in offered)
    rec.update({
        "status": "ok",
        "tool_call_leak": _is_tool_call_leak(raw),
        "ref_count": len(refs),
        "ref_resolved": resolved,
        "ref_unresolved": len(refs) - resolved,
        "zero_refs": bool(not refs),
        **degeneration_metrics(raw),
        "body_excerpt": raw[:1500],
    })
    return rec


def _load_done(out_path: Path) -> set[tuple[str, str]]:
    """(case_id, arm) keys already completed OK in the results file."""
    done: set[tuple[str, str]] = set()
    if not out_path.exists():
        return done
    with out_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if row.get("status") == "ok":
                done.add((str(row.get("case_id")), str(row.get("arm"))))
    return done


async def cmd_run(args: argparse.Namespace) -> int:
    # The verify judge is flag-gated (code default OFF); this harness NEEDS it
    # on to score the arms — process-local env only, nothing live is touched.
    os.environ.setdefault("LEGBA_VERIFY_LLM_JUDGE", "1")

    inputs_path = Path(args.inputs)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cases = [
        json.loads(line)
        for line in inputs_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.limit:
        cases = cases[: args.limit]
    done = _load_done(out_path)
    print(
        f"{len(cases)} cases x {len(ARMS)} arms; {len(done)} arm-results "
        f"already complete (resume)"
    )

    ctx = _ArmContext()
    await ctx.open(args.component)
    sem = asyncio.Semaphore(max(1, int(args.concurrency)))
    write_lock = asyncio.Lock()
    counters = {"ok": 0, "failed": 0, "skipped": 0, "unbuildable": 0}

    async def _emit(row: dict[str, Any]) -> None:
        async with write_lock:
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    async def _run_case(case: dict[str, Any]) -> None:
        surface = case["surface"]
        pending = [t for t in ARMS if (case["case_id"], str(t)) not in done]
        if not pending:
            counters["skipped"] += 1
            return
        async with sem:
            try:
                built = (
                    await _build_unit_input(ctx, case)
                    if surface == "unit"
                    else await _build_journal_input(ctx, case)
                )
            except Exception as exc:  # noqa: BLE001 - unbuildable case
                built = None
                build_err = f"{type(exc).__name__}: {exc}"
            else:
                build_err = None
            if built is None:
                counters["unbuildable"] += 1
                await _emit({
                    "case_id": case["case_id"], "surface": surface,
                    "arm": None, "status": "unbuildable", "error": build_err,
                })
                return
            base = {
                "case_id": case["case_id"],
                "surface": surface,
                "analyst_id": case.get("analyst_id"),
                "target_id": case.get("target_id"),
                "entry_kind": case.get("entry_kind"),
                "n_inputs_total": built.get("n_signals_total", built.get("n_refs_total")),
                "n_inputs_used": built.get("n_signals_used", built.get("n_refs_used")),
                "slice_truncated": built.get("slice_truncated"),
                "stored_faithfulness": case.get("stored_faithfulness"),
                "stored_body_len": case.get("stored_body_len"),
            }
            for temp in pending:
                try:
                    rec = (
                        await _eval_unit_arm(
                            ctx, case, built, temp, args.gen_timeout,
                            judge_profile=args.judge_profile,
                        )
                        if surface == "unit"
                        else await _eval_journal_arm(
                            ctx, case, built, temp, args.gen_timeout
                        )
                    )
                    status = rec.get("status", "ok")
                except Exception as exc:  # noqa: BLE001 - one bad arm, keep going
                    rec = {"error": f"{type(exc).__name__}: {exc}"}
                    status = "failed"
                rec.setdefault("status", status)
                counters["ok" if rec["status"] == "ok" else "failed"] += 1
                await _emit({**base, "arm": str(temp), **rec, "ts": time.time()})
                print(
                    f"  {case['case_id']} arm={temp} -> {rec.get('status')}"
                    + (
                        f" faith={rec['faithfulness']['score']}"
                        f"/{rec['faithfulness']['judge_status']}"
                        if isinstance(rec.get("faithfulness"), dict) else ""
                    ),
                    flush=True,
                )

    try:
        await asyncio.gather(*(_run_case(c) for c in cases))
    finally:
        await ctx.close()
    print(f"done: {counters}")
    return 0


# ---------------------------------------------------------------------------
# report — per-arm aggregates + paired deltas + sign test
# ---------------------------------------------------------------------------


def _sign_test_p(pos: int, neg: int) -> float | None:
    """Two-sided exact sign test (binomial, p=0.5, ties excluded)."""
    n = pos + neg
    if n == 0:
        return None
    k = min(pos, neg)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return round(min(1.0, 2.0 * tail), 4)


def _mean(vals: list[float]) -> float | None:
    return round(statistics.fmean(vals), 4) if vals else None


def _median(vals: list[float]) -> float | None:
    return round(statistics.median(vals), 4) if vals else None


def _fmt(v: Any) -> str:
    return "—" if v is None else str(v)


def cmd_report(args: argparse.Namespace) -> int:
    results_path = Path(args.results)
    rows = [
        json.loads(line)
        for line in results_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ok = [r for r in rows if r.get("status") == "ok" and r.get("arm")]
    # Latest row wins per (case, arm) — reruns append.
    latest: dict[tuple[str, str], dict] = {}
    for r in ok:
        latest[(r["case_id"], r["arm"])] = r
    by_arm: dict[str, list[dict]] = {}
    for (_, arm), r in latest.items():
        by_arm.setdefault(arm, []).append(r)

    lines: list[str] = []
    add = lines.append

    for surface in ("unit", "journal"):
        arms = {
            arm: [r for r in rs if r.get("surface") == surface]
            for arm, rs in sorted(by_arm.items())
        }
        n_by_arm = {a: len(rs) for a, rs in arms.items()}
        if not any(n_by_arm.values()):
            continue
        add(f"### Surface: {surface} (n per arm: {n_by_arm})")
        add("")
        if surface == "unit":
            add(
                "| arm | n | judge-scored | faithfulness mean | median | "
                "JSON-parse fail | cit-parse fail (markers→0) | unresolved-marker rate | "
                "variant-bracket outputs | zero-marker outputs | mean markers | "
                "rep-4gram rate | tail-loop | finish=length | mean len (chars) | "
                "mean completion tok | mean latency s |"
            )
            add("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
            for arm, rs in arms.items():
                faith = [
                    r["faithfulness"]["score"] for r in rs
                    if isinstance(r.get("faithfulness"), dict)
                    and r["faithfulness"].get("judge_status") == "llm"
                ]
                markers = [r.get("marker_count", 0) for r in rs]
                unresolved_rates = [
                    1.0 - (r["resolved_count"] / r["marker_count"])
                    for r in rs if r.get("marker_count")
                ]
                add(
                    f"| {arm} | {len(rs)} | {len(faith)} | {_fmt(_mean(faith))} | "
                    f"{_fmt(_median(faith))} | "
                    f"{sum(1 for r in rs if not r.get('json_parse_ok'))} | "
                    f"{sum(1 for r in rs if r.get('citation_parse_failure'))} | "
                    f"{_fmt(_mean(unresolved_rates))} | "
                    f"{sum(1 for r in rs if r.get('variant_bracket_count'))} | "
                    f"{sum(1 for r in rs if r.get('zero_markers'))} | "
                    f"{_fmt(_mean([float(m) for m in markers]))} | "
                    f"{_fmt(_mean([r['repeated_fourgram_rate'] for r in rs if r.get('repeated_fourgram_rate') is not None]))} | "
                    f"{sum(1 for r in rs if r.get('tail_loop'))} | "
                    f"{sum(1 for r in rs if r.get('finish_reason') == 'length')} | "
                    f"{_fmt(_mean([float(r['len_chars']) for r in rs if r.get('len_chars') is not None]))} | "
                    f"{_fmt(_mean([float(r['completion_tokens']) for r in rs if r.get('completion_tokens')]))} | "
                    f"{_fmt(_mean([float(r['latency_s']) for r in rs if r.get('latency_s')]))} |"
                )
        else:
            add(
                "| arm | n | tool-call leak | zero-ref outputs | mean refs | "
                "unresolved-ref rate | rep-4gram rate | tail-loop | "
                "finish=length | mean len (chars) | mean completion tok | "
                "mean latency s |"
            )
            add("|---|---|---|---|---|---|---|---|---|---|---|---|")
            for arm, rs in arms.items():
                unresolved_rates = [
                    r["ref_unresolved"] / r["ref_count"]
                    for r in rs if r.get("ref_count")
                ]
                add(
                    f"| {arm} | {len(rs)} | "
                    f"{sum(1 for r in rs if r.get('tool_call_leak'))} | "
                    f"{sum(1 for r in rs if r.get('zero_refs'))} | "
                    f"{_fmt(_mean([float(r.get('ref_count', 0)) for r in rs]))} | "
                    f"{_fmt(_mean(unresolved_rates))} | "
                    f"{_fmt(_mean([r['repeated_fourgram_rate'] for r in rs if r.get('repeated_fourgram_rate') is not None]))} | "
                    f"{sum(1 for r in rs if r.get('tail_loop'))} | "
                    f"{sum(1 for r in rs if r.get('finish_reason') == 'length')} | "
                    f"{_fmt(_mean([float(r['len_chars']) for r in rs if r.get('len_chars') is not None]))} | "
                    f"{_fmt(_mean([float(r['completion_tokens']) for r in rs if r.get('completion_tokens')]))} | "
                    f"{_fmt(_mean([float(r['latency_s']) for r in rs if r.get('latency_s')]))} |"
                )
        add("")

        # Paired deltas (1.0 − 0.2) on the cases with BOTH arms ok.
        lo, hi = str(ARMS[0]), str(ARMS[1])
        paired_cases = [
            cid for (cid, arm) in latest
            if arm == lo and (cid, hi) in latest
            and latest[(cid, lo)].get("surface") == surface
        ]
        if surface == "unit":
            deltas = []
            for cid in paired_cases:
                a, b = latest[(cid, lo)], latest[(cid, hi)]
                fa, fb = a.get("faithfulness"), b.get("faithfulness")
                if (
                    isinstance(fa, dict) and fa.get("judge_status") == "llm"
                    and isinstance(fb, dict) and fb.get("judge_status") == "llm"
                ):
                    deltas.append(fb["score"] - fa["score"])
            pos = sum(1 for d in deltas if d > 0)
            neg = sum(1 for d in deltas if d < 0)
            ties = len(deltas) - pos - neg
            add(
                f"Paired faithfulness (judge@0.0 both arms, n={len(deltas)}): "
                f"mean Δ(1.0−0.2) = {_fmt(_mean(deltas))}, median Δ = "
                f"{_fmt(_median(deltas))}, improved {pos} / worse {neg} / tied "
                f"{ties}, sign-test p = {_fmt(_sign_test_p(pos, neg))}"
            )
        # Length + repetition paired reads (both surfaces).
        len_deltas, rep_deltas = [], []
        for cid in paired_cases:
            a, b = latest[(cid, lo)], latest[(cid, hi)]
            if a.get("len_chars") is not None and b.get("len_chars") is not None:
                len_deltas.append(float(b["len_chars"] - a["len_chars"]))
            if (
                a.get("repeated_fourgram_rate") is not None
                and b.get("repeated_fourgram_rate") is not None
            ):
                rep_deltas.append(
                    b["repeated_fourgram_rate"] - a["repeated_fourgram_rate"]
                )
        rp, rn = (
            sum(1 for d in rep_deltas if d > 0),
            sum(1 for d in rep_deltas if d < 0),
        )
        add(
            f"Paired length Δ(1.0−0.2) mean = {_fmt(_mean(len_deltas))} chars; "
            f"paired repeated-4gram-rate Δ mean = {_fmt(_mean(rep_deltas))} "
            f"(up {rp} / down {rn}, sign-test p = {_fmt(_sign_test_p(rp, rn))})"
        )
        add("")

    print("\n".join(lines))
    return 0


# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sample = sub.add_parser("sample", help="draw + freeze the case set (read-only)")
    p_sample.add_argument("--out", required=True)
    p_sample.add_argument("--units-per-analyst", type=int, default=5)
    p_sample.add_argument("--journal", type=int, default=8)
    p_sample.add_argument("--env-file", default=None)

    p_run = sub.add_parser("run", help="run both temperature arms (resumable)")
    p_run.add_argument("--inputs", required=True)
    p_run.add_argument("--out", required=True)
    p_run.add_argument("--env-file", default=None)
    p_run.add_argument("--component", default=CORE_COMPONENT)
    p_run.add_argument("--concurrency", type=int, default=3)
    p_run.add_argument("--gen-timeout", type=float, default=900.0)
    p_run.add_argument("--limit", type=int, default=0)
    # P2-4: the judge-side prompt profile — lets a future run A/B the staged
    # independence-posture judge prompt exactly like temperature was A/B'd
    # (run once with current, once with independent, on the same frozen cases;
    # separate --out files; the report step compares). Default = live behavior.
    p_run.add_argument(
        "--judge-profile", choices=("current", "independent"), default="current",
    )

    p_report = sub.add_parser("report", help="aggregate + paired analysis")
    p_report.add_argument("--results", required=True)

    args = parser.parse_args()
    if args.cmd == "report":
        return cmd_report(args)
    _load_env(args.env_file)
    if args.cmd == "sample":
        return asyncio.run(cmd_sample(args))
    return asyncio.run(cmd_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
