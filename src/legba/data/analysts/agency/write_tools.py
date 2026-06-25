# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``propose_facts`` pack's OPERATOR-GATED write-back tools (S6, review S-1).

Three tools let an agentic assessor PROPOSE a write back into the substrate —
but only through the same provenance-stamped, governed, DLQ-safe writers the
analyst run path uses. The threat the gate closes (review S-1): untrusted RSS
text reaches an assessor; nothing it proposes may auto-mutate the knowledge
layer. So every write here is:

  * three-way-gated — the tool runs only when a write pack is EFFECTIVE for an
    (assessor, target) pair (analyst grant ∩ target allow ∩ applicability) and
    passes the per-pack governor. Operators control this by NOT allowing a
    write pack on a target; the default posture is no write surface at all.
  * provenance-stamped — written via :func:`write_fact` / :func:`write_hypothesis`
    with the run's :class:`AnalystContext` (analyst id/version + run id +
    target). NEVER ``_insert_fact`` directly — that bypasses validation,
    junk-gating, supersession, and the DLQ.
  * lineage-required — ``derived_from`` is MANDATORY in the tool args. A
    proposed fact with no cited source is refused (a write with no warrant is
    exactly what the gate exists to stop).
  * DLQ-safe — a malformed payload routes to ``output_dead_letter`` (the
    writer's ValidationError path) and the tool reports a clean failure; it
    never crashes the GATHER loop.

The tools:

  * ``propose_fact``    — write one ``(subject, predicate, value)`` triple to
                          the ``facts`` table with ``source_type='proposed'``
                          (distinct from ingestion-owned / seed / agent facts),
                          after the SAME ``_is_junk_triple`` gate the ingest
                          path uses. Confidence is clamped to a cautious ceiling
                          — a proposed fact is a hypothesis-grade assertion, not
                          ground truth.
  * ``request_source``  — record a COVERAGE / evidence gap the assessor hit
                          ("no source covers X") as a ``hypotheses`` row with
                          ``status='source_request'``. Operator-visible and
                          queryable (the hypotheses read paths), not a job kind
                          with no worker (no dead-letter-forever).
  * ``open_question``   — record an unresolved analytical question as a
                          ``hypotheses`` row with ``status='open_question'`` —
                          a real open thesis the ACH / consult loops can later
                          pick up, not a dropped note.

A handler is ``async (call, pack, ctx) -> ToolResult``; agency was decided
upstream. The write surface comes from ``ctx.writeback`` (the per-run pg_pool +
AnalystContext); absent → a ``failed`` ToolResult naming the missing surface.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from ...filters.fact_extractor import _is_junk_triple
from ...provenance import (
    AnalystContext,
    HypothesisPayload,
    write_fact,
    write_hypothesis,
)
from ...schemas.action_pack import ActionPack
from .tools import ToolCall, ToolContext, ToolResult, WritebackContext

logger = logging.getLogger(__name__)

WRITE_PACK_ID = "propose_facts"

WRITE_TOOLS = (
    "propose_fact",
    "request_source",
    "open_question",
)

# A proposed fact is a hypothesis-grade assertion from an LLM over untrusted
# text — never let it land at full confidence. Clamp to a cautious ceiling so
# it competes with, but does not dominate, source-owned facts. Mirrors the
# Phase-B ingestion fallback posture (0.75) rather than the legacy 1.0.
_PROPOSED_FACT_CONFIDENCE_CEILING = 0.6

# GROUNDING LIFECYCLE (F4 — make the proposed→grounded path explicit).
# A proposed fact lands with source_type='proposed', which is NOT in the default
# grounding trusted set (runtime/grounding.py::trusted_source_types →
# ('seed','curated')). So a proposed fact is deliberately EXCLUDED from the
# "AUTHORITATIVE CURRENT CONTEXT" ground-truth preamble — it is a lead awaiting
# operator vetting, not ground truth. Two ways a proposed fact graduates to
# grounding, both operator-gated by construction:
#   (a) PROMOTE — an operator re-asserts the vetted fact through the curated seed
#       path (source_type='curated'/'seed'; see seeds/world_baseline + scripts/
#       seed.py), which supersedes the proposed row and IS grounded; or
#   (b) ADMIT — an operator adds 'proposed' to
#       LEGBA_GROUNDING_TRUSTED_SOURCE_TYPES at a deployment that wants
#       unpromoted proposals to ground (a deliberate, reversible env choice).
# There is intentionally NO automatic proposed→curated rewrite: promotion is an
# operator act, consistent with "analysts PROPOSE, promotion ACTIVATEs".


def _writeback(ctx: ToolContext, tool_name: str) -> WritebackContext | None:
    wb = ctx.writeback
    if wb is None or wb.pg_pool is None or wb.analyst_ctx is None:
        return None
    return wb


def _coerce_derived_from(raw: Any) -> tuple[list[UUID], str | None]:
    """Parse the REQUIRED ``derived_from`` arg into a non-empty UUID list.

    Accepts a single id or a list. Returns ``(uuids, None)`` on success, or
    ``([], error)`` when missing / empty / unparseable — the caller refuses the
    write (lineage is mandatory: a proposed write must cite what it derives
    from). Non-UUID entries are dropped with the error naming the first bad one.
    """
    if raw is None:
        return [], "derived_from is required (cite the substrate refs this is derived from)"
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out: list[UUID] = []
    for it in items:
        s = str(it).strip()
        if not s:
            continue
        try:
            out.append(UUID(s))
        except (ValueError, TypeError):
            return [], f"derived_from entry is not a UUID: {s!r}"
    if not out:
        return [], "derived_from must contain at least one substrate UUID"
    return out, None


async def propose_fact_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    """Propose one fact triple → ``write_fact`` (source_type='proposed').

    ``args``:
      * ``subject`` / ``predicate`` / ``value`` (required) — the triple.
      * ``derived_from`` (required) — the substrate UUID(s) it is grounded in.
      * ``confidence`` (optional) — clamped to ``_PROPOSED_FACT_CONFIDENCE_CEILING``.
      * ``valid_from`` / ``valid_until`` (optional) — passed through if set.

    Runs the SAME ``_is_junk_triple`` gate as the ingest path (drop+report,
    never raise), then writes via :func:`write_fact` so the row is validated,
    supersession-aware, and DLQ-safe. A schema-invalid payload routes to the
    DLQ and the tool reports a clean failure.
    """
    wb = _writeback(ctx, "propose_fact")
    if wb is None:
        return ToolResult(
            status="failed",
            error="no writeback surface wired for propose_fact (ctx.writeback is None)",
        )

    args = call.args
    subject = str(args.get("subject", "")).strip()
    predicate = str(args.get("predicate", "")).strip()
    value = str(args.get("value", "")).strip()
    if not subject or not predicate or not value:
        return ToolResult(
            status="failed",
            error="propose_fact requires non-empty subject, predicate, value",
        )

    derived_from, df_err = _coerce_derived_from(args.get("derived_from"))
    if df_err is not None:
        return ToolResult(status="failed", error=df_err)

    # Same junk gate the ingest write-path uses (NER artifacts / self-reference
    # / numeric / HTML-entity). Drop+report — an agent proposing junk is a clean
    # failure, not a crash.
    if _is_junk_triple(subject, predicate, value):
        logger.info(
            "propose_fact.rejected_junk subject=%r predicate=%r value=%r",
            subject, predicate, value,
        )
        return ToolResult(
            status="failed",
            error="proposed triple rejected as junk (NER artifact / self-reference)",
        )

    confidence = min(
        _PROPOSED_FACT_CONFIDENCE_CEILING,
        max(0.0, float(args.get("confidence", _PROPOSED_FACT_CONFIDENCE_CEILING))),
    )
    # Pass a RAW dict (not a pre-built FactPayload) so the canonical write path
    # validates it: an oversize / malformed value is routed to output_dead_letter
    # by write_analyst_output (returns row=None) instead of raising at FactPayload
    # construction here and bypassing the dead-letter path.
    fact_payload = {
        "subject": subject,
        "predicate": predicate,
        "value": value,
        "confidence": confidence,
        "source_type": "proposed",
        "valid_from": args.get("valid_from"),
        "valid_until": args.get("valid_until"),
        "data": {
            "proposed_by": call.requested_by,
            "pack_id": pack.identity.id,
        },
    }

    async with wb.pg_pool.acquire() as conn:
        row, dlq = await write_fact(
            conn,
            analyst_ctx=wb.analyst_ctx,
            payload=fact_payload,
            derived_from=derived_from,
            publish_fn=wb.publish_fn,
            source_type="proposed",
        )
    if row is None:
        # Schema validation failed → routed to output_dead_letter by the writer.
        return ToolResult(
            status="failed",
            error="propose_fact payload rejected to dead-letter (schema validation)",
        )
    return ToolResult(
        status="completed",
        output={
            "fact_id": str(row.id),
            "subject": subject,
            "predicate": predicate,
            "value": value,
            "confidence": confidence,
            "source_type": "proposed",
        },
        units=1,
    )


async def request_source_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    """Record a coverage / evidence gap → ``write_hypothesis`` (status='source_request').

    ``args``:
      * ``need`` (required) — what coverage is missing ("no source covers
        Kazakhstan energy policy").
      * ``rationale`` (optional) — why it matters / what the gap blocked.
      * ``derived_from`` (required) — the substrate refs that surfaced the gap.

    Lands a REAL, operator-visible row in ``hypotheses`` (the read paths already
    surface hypotheses; status='source_request' tags it as a coverage ask).
    NOT a job kind with no worker — there is no dead-letter-forever path here.
    """
    wb = _writeback(ctx, "request_source")
    if wb is None:
        return ToolResult(
            status="failed",
            error="no writeback surface wired for request_source (ctx.writeback is None)",
        )

    need = str(call.args.get("need", "")).strip()
    if not need:
        return ToolResult(
            status="failed",
            error="request_source requires a non-empty 'need' (what coverage is missing)",
        )
    derived_from, df_err = _coerce_derived_from(call.args.get("derived_from"))
    if df_err is not None:
        return ToolResult(status="failed", error=df_err)

    rationale = str(call.args.get("rationale", "")).strip()
    payload = HypothesisPayload(
        thesis=f"Source coverage gap: {need}"[:4096],
        counter_thesis=rationale[:4096],
        status="source_request",
        data={
            "kind": "source_request",
            "requested_by": call.requested_by,
            "pack_id": pack.identity.id,
        },
    )
    async with wb.pg_pool.acquire() as conn:
        row, _dlq = await write_hypothesis(
            conn,
            analyst_ctx=wb.analyst_ctx,
            payload=payload,
            derived_from=derived_from,
            publish_fn=wb.publish_fn,
        )
    if row is None:
        return ToolResult(
            status="failed",
            error="request_source payload rejected to dead-letter (schema validation)",
        )
    return ToolResult(
        status="completed",
        output={"hypothesis_id": str(row.id), "status": "source_request", "need": need},
        units=1,
    )


async def open_question_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    """Record an unresolved analytical question → ``write_hypothesis`` (status='open_question').

    ``args``:
      * ``question`` (required) — the open analytical question.
      * ``counter`` (optional) — a competing reading / null hypothesis.
      * ``derived_from`` (required) — the substrate refs that raised it.

    A real open thesis (status='open_question') the ACH / consult loops can pick
    up later — recorded, queryable, lineage-stamped; never a dropped note.
    """
    wb = _writeback(ctx, "open_question")
    if wb is None:
        return ToolResult(
            status="failed",
            error="no writeback surface wired for open_question (ctx.writeback is None)",
        )

    question = str(call.args.get("question", "")).strip()
    if not question:
        return ToolResult(
            status="failed",
            error="open_question requires a non-empty 'question'",
        )
    derived_from, df_err = _coerce_derived_from(call.args.get("derived_from"))
    if df_err is not None:
        return ToolResult(status="failed", error=df_err)

    counter = str(call.args.get("counter", "")).strip()
    payload = HypothesisPayload(
        thesis=question[:4096],
        counter_thesis=counter[:4096],
        status="open_question",
        data={
            "kind": "open_question",
            "requested_by": call.requested_by,
            "pack_id": pack.identity.id,
        },
    )
    async with wb.pg_pool.acquire() as conn:
        row, _dlq = await write_hypothesis(
            conn,
            analyst_ctx=wb.analyst_ctx,
            payload=payload,
            derived_from=derived_from,
            publish_fn=wb.publish_fn,
        )
    if row is None:
        return ToolResult(
            status="failed",
            error="open_question payload rejected to dead-letter (schema validation)",
        )
    return ToolResult(
        status="completed",
        output={"hypothesis_id": str(row.id), "status": "open_question", "question": question},
        units=1,
    )


def register_write_tools(registry: "Any") -> None:
    """Register the three operator-gated write handlers (called by
    ``default_tool_registry``)."""
    registry.register("propose_fact", propose_fact_tool)
    registry.register("request_source", request_source_tool)
    registry.register("open_question", open_question_tool)


__all__ = [
    "WRITE_PACK_ID",
    "WRITE_TOOLS",
    "open_question_tool",
    "propose_fact_tool",
    "register_write_tools",
    "request_source_tool",
]
