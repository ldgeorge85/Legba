# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""journal_assessor — Legba's first-person reflective voice (plan §4).

The 11th OutputKind's producer. The ONE analyst pointed at the whole organism,
including itself: it reads what the other analysts concluded and narrates a
coherent point of view OVER all of it, in its own voice. It emits exactly one
``JournalPayload`` (entry | consolidation) into the dedicated ``journal_entries``
table — OFF the fact/finding/nexus chain (§3.1). It must NEVER write a fact,
nexus, finding, situation, or hypothesis.

ENGINE (plan §4.1, a hard commitment): the in-actor ``llm_planner`` /
``inline_target``-family envelope — NOT the ``deep_consult`` Dapr workflow (which
hardcodes OUTPUT_KIND = FINDING and rides the known-broken long-activity
round-trip, task #86). This module reuses inline_target's proven GATHER + reason
building blocks (``_gather`` / ``_reason_via_llm``) but:

  * threads the JOURNAL persona (legba.prompts.journal_assessor:JOURNAL_SYSTEM)
    as the system prompt on EVERY LLM call — and authors it WITHOUT the stock
    ``with_preamble`` JSON-only / BLUF anti-voice (§4.2, the headline fix);
  * emits a ``JournalPayload`` (parsing inline [[ref:<uuid>]] citation markers
    into the per-claim ``claims`` binding + the flat ``cited_substrate_refs``);
  * returns ``derived_from=[]`` — the direction-asymmetric off-chain node (§3.5).

WAVE 0 cut (plan §4.10 / §12): single tier, ENTRY only (no consolidation yet);
READ_SLICE = None → the default META global signal slice primes the run and deep
investigation happens in GATHER over the journal_read pack (ONE reused tool,
list_findings). The delta-priming READ_SLICE + the in-voice field-notes seam +
tools-live NARRATE are Wave 1.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import UUID

from ..provenance.kinds import OutputKind
from ..provenance.models import JournalClaim, JournalPayload
from .inline_target import (
    AnalystMethodResult,
    InlineTargetDeps,
    LLMHandlerLike,
    _gather,
    _gather_system_suffix,
    _reason_via_llm,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Host-discovered constants (plan §4.8 leg 1)
# ---------------------------------------------------------------------------

KIND_NAME = "journal_assessor"
SCHEMA_VERSION = "legba/analyst.journal_assessor/1-0-0"
HANDLER_VERSION = "0.1.0"
PROMPT_MODULE_PATH = "legba.prompts.journal_assessor:JOURNAL_SYSTEM"

# The 11th OutputKind — the host's analyst-output dispatcher writes a
# ``journal_entries`` row (NOT a finding). This is what makes the kind off-chain.
OUTPUT_KIND: OutputKind = OutputKind.JOURNAL

# READ_SLICE = None (Wave 0) — the host's default META reader yields the global
# 24h signal slice that primes the reflection; deep investigation is in GATHER.
# The Wave-1 delta-priming reader (get_journal_delta since last period_end) will
# replace this; if a custom reader is built, init all slice-locals BEFORE any
# `if target_filter:` branch (this is a META analyst, target_filter=None — the
# 5c scope_predicate UnboundLocalError class).
READ_SLICE = None


# Inline citation marker the body carries; the UI resolves it to a chip at the
# cited span. We also harvest the UUIDs into claims + cited_substrate_refs.
_REF_MARKER_RE = re.compile(r"\[\[ref:([0-9a-fA-F-]{36})\]\]")
# A markdown title line ("# ...") the model may lead with.
_TITLE_LINE_RE = re.compile(r"^\s*#+\s*(.+?)\s*$", re.MULTILINE)
_MAX_TITLE_CHARS = 240


def build_prompt_module() -> Any:
    """The journal runs on the in-actor envelope, not the GEPA compile surface,
    so it has no DSPy module. Returning the persona STRING keeps the discovery
    contract uniform (callers that introspect ``build_prompt_module`` get the
    voice). The system prompt is threaded by ``run_method`` directly.
    """
    from legba.prompts.journal_assessor import JOURNAL_SYSTEM
    return JOURNAL_SYSTEM


def _extract_claims(body: str) -> tuple[list[JournalClaim], list[UUID]]:
    """Parse inline ``[[ref:<uuid>]]`` markers out of the body into the per-claim
    ``claims`` binding + the flat ``cited_substrate_refs`` union (plan §3.6).

    Wave-0 binding granularity: each sentence/paragraph carrying ≥1 ref becomes a
    ``kind='fact'`` claim bound to its refs; the flat union is every distinct
    ref. A more precise span-level binding (and the permissive REFLECT
    fact-vs-perspective flag) is Wave 1 — here we never delete voice, we only
    surface what the model cited. Refs that don't parse as UUIDs are dropped from
    the structured binding (the marker stays in the body text).
    """
    flat: list[UUID] = []
    seen: set[UUID] = set()
    claims: list[JournalClaim] = []
    # Split into spans on blank lines; a span with refs becomes a fact claim.
    for span in re.split(r"\n\s*\n", body):
        span_refs: list[UUID] = []
        for m in _REF_MARKER_RE.finditer(span):
            try:
                u = UUID(m.group(1))
            except ValueError:
                continue
            span_refs.append(u)
            if u not in seen:
                seen.add(u)
                flat.append(u)
        if span_refs:
            text = span.strip()
            claims.append(
                JournalClaim(
                    text_span=text[:8192] if text else "(cited span)",
                    refs=span_refs,
                    kind="fact",
                )
            )
    return claims, flat


def _derive_title(body: str, fallback: str) -> str:
    """Pull a short title from the body's first markdown heading or first line."""
    m = _TITLE_LINE_RE.search(body)
    if m:
        return m.group(1)[:_MAX_TITLE_CHARS]
    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    # Strip ref markers from the title so a chip syntax doesn't leak into the label.
    first = _REF_MARKER_RE.sub("", first).strip()
    return (first[:_MAX_TITLE_CHARS] or fallback)


def _render_user_prompt(inputs: list[dict[str, Any]]) -> str:
    """Assemble the priming context (the META global slice) into a notebook
    prompt. Kept thin — the agent investigates the rest via GATHER."""
    lines = [
        "Below is the recent global signal slice the platform metabolized this "
        "window. Reflect on it in your journal: what connects, what worries you, "
        "what changed, what you don't yet understand. Use your read tools to pull "
        "the platform's own findings before you commit a claim. Cite every "
        "factual assertion inline as [[ref:<uuid>]] using only UUIDs your tools "
        "returned.",
        "",
        "--- recent signal slice ---",
    ]
    for row in inputs[:60]:
        title = str(row.get("title") or row.get("payload", {}).get("title") or "").strip()
        if not title and isinstance(row.get("data"), dict):
            title = str(row["data"].get("title") or "").strip()
        if title:
            lines.append(f"- {title[:200]}")
    return "\n".join(lines)


async def run_method(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: InlineTargetDeps | LLMHandlerLike,
) -> AnalystMethodResult:
    """Execute one journal-entry run over the substrate (plan §4.2 staged arc).

    PLAN (what's worth reflecting on?) → GATHER (deep ReAct over the journal_read
    pack) → NARRATE (write the entry). The persona is loaded in EVERY phase (the
    worldview is the attention mechanism). Emits a ``JournalPayload`` and returns
    ``derived_from=[]`` (off the chain).
    """
    if not isinstance(deps, InlineTargetDeps):
        deps = InlineTargetDeps(llm=deps)

    analyst_id = options.get("analyst_id")
    steps: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    # The window the entry reflects on. Wave 0: the default META 24h slice; Wave 1
    # narrows this to (last_entry.period_end, now] via the delta reader.
    period_end = now
    period_start = now - timedelta(hours=24)

    steps.append({"phase": "wake", "kind": "envelope"})

    # --- PLAN / ORIENT -------------------------------------------------
    user_prompt = _render_user_prompt(inputs)
    steps.append({
        "phase": "plan",
        "kind": "render_prompt",
        "in_count": len(inputs),
        "prompt_chars": len(user_prompt),
        "prompt_module": PROMPT_MODULE_PATH,
    })

    # --- GROUND (Tier-1 knowledge grounding, if the descriptor opted in) ---
    if deps.grounding_hook is not None:
        try:
            preamble = await deps.grounding_hook(inputs, options)
        except Exception as exc:  # pragma: no cover — grounding must not fail the run
            logger.warning("journal_assessor.grounding.failed err=%s", exc)
            preamble = None
        if preamble:
            user_prompt = f"{preamble}\n{user_prompt}"
            steps.append({"phase": "ground", "kind": "inject_preamble"})

    # --- GATHER (deep ReAct over the journal_read pack) ----------------
    gather_usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    active_binding = options.get("agency_binding") or deps.agency_binding
    # The journal_read pack is read-only in Wave 0 (no write/web tools), so no
    # per-tool write bindings — just the read suffix.
    gather_system = _gather_system_suffix(web_fragments=None, write_fragments=None)
    if active_binding is not None:
        gathered_context, gather_usage, _gather_refs, _ = await _gather(
            deps,
            binding=active_binding,
            user_prompt=user_prompt,
            target_id=None,
            analyst_id=analyst_id,
            steps=steps,
            tool_bindings=options.get("gather_tool_bindings") or {},
            gather_system=gather_system,
        )
        if gathered_context:
            user_prompt = f"{gathered_context}\n{user_prompt}"
    else:
        steps.append({"phase": "gather", "kind": "no_binding"})

    # --- NARRATE (write the entry, in voice) ---------------------------
    # The system prompt is the JOURNAL persona + self-anatomy MAP + narrate
    # contract (deps.system_prompt), threaded WITHOUT with_preamble (§4.2).
    try:
        content, usage = await _reason_via_llm(
            deps.llm,
            user_prompt=user_prompt,
            max_tokens=deps.max_tokens,
            temperature=deps.temperature,
            system_prompt=deps.system_prompt,
        )
    except Exception:
        steps.append({"phase": "narrate", "kind": "llm_error"})
        raise

    for _k in usage:
        usage[_k] = usage.get(_k, 0) + gather_usage.get(_k, 0)

    body = (content or "").strip()
    claims, cited_refs = _extract_claims(body)
    title = _derive_title(body, fallback="Journal entry")
    payload = JournalPayload(
        entry_kind="entry",
        title=title,
        body=body or "(empty entry)",
        claims=claims,
        cited_substrate_refs=cited_refs,
        period_start=period_start,
        period_end=period_end,
        supersedes=None,
        # honesty_flags forced by the Wave-1 deterministic post-step; empty here.
        honesty_flags=[],
    )
    steps.append({
        "phase": "narrate",
        "kind": "coerce_journal",
        "body_chars": len(body),
        "claims": len(claims),
        "cited_refs": len(cited_refs),
    })

    # --- PERSIST -------------------------------------------------------
    # derived_from is EMPTY — the journal is the direction-asymmetric off-chain
    # node (§3.5). The citations live in claims / cited_substrate_refs only; the
    # write path (_insert_journal_entry) also hard-forces the column empty.
    steps.append({"phase": "persist", "kind": "envelope", "derived_from": 0})

    return AnalystMethodResult(
        finding=payload,            # the runtime forwards this to write_analyst_output(kind=JOURNAL)
        usage=usage,
        derived_from=[],            # OFF the chain (§3.5)
        intermediate_steps=steps,
    )


__all__ = [
    "KIND_NAME",
    "OUTPUT_KIND",
    "READ_SLICE",
    "run_method",
    "build_prompt_module",
]
