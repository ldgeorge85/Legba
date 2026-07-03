# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Output-emit / NATS adapters + escalation — extracted from dapr_actors.py (#93), behavior-preserving move."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import asyncpg

from ..data.provenance._core import AnalystContext
from ..data.provenance.kinds import OutputKind
from ..data.provenance.models import severity_from_tags

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# (Legacy E2 target-owned acquisition helpers — _make_source_context,
# _parse_source_config, _pipe_one, _write_signal — were removed with L-205.
# SourceActor owns acquisition; TargetActor is a passive subscriber.)


# Per-kind NATS subject channel suffixes (DESIGN.md §11 / L-191).
# (No SIGNAL entry: signals are source-owned rows published by
# source_actor's canonical write path, never an analyst output kind.)
_NATS_CHANNEL_BY_KIND: dict[OutputKind, str] = {
    OutputKind.FINDING: "findings",
    OutputKind.SITUATION: "situations",
    OutputKind.HYPOTHESIS: "hypotheses",
    OutputKind.PREDICTION: "predictions",
    OutputKind.ALERT: "alerts",
    OutputKind.META_FINDING: "meta_findings",
    OutputKind.CRITIQUE: "critiques",
    OutputKind.FACT: "facts",
    OutputKind.NEXUS: "nexuses",
}


def _channel_for_kind(output_kind: OutputKind) -> str:
    """Channel name for the NATS subject ``analyst.<id>.<channel>``."""
    return _NATS_CHANNEL_BY_KIND.get(output_kind, output_kind.value)


# --- Output-kind emit dispatch (L-195/L-197) ---------------------------------
# Producer output kinds (e.g. stix_bundle) expose a uniform
# ``async def emit(payload, *, descriptor, deps, ...)`` surface. The analyst run
# path invokes them for the descriptor's declared output bindings so the LIVE
# payload reaches the producer (the prior gap: handlers existed but nothing
# called them, so STIX produced 0 bundles in the live system).

_OUTPUT_KIND_HANDLERS: dict[str, Any] | None = None


def _output_kind_handlers() -> dict[str, Any]:
    """Cached kind→OutputHandler map (discovery is a filesystem walk)."""
    global _OUTPUT_KIND_HANDLERS
    if _OUTPUT_KIND_HANDLERS is None:
        try:
            from ..data.outputs import discover_output_kinds

            _OUTPUT_KIND_HANDLERS = discover_output_kinds()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("dapr_actors.output_kinds.discover_failed err=%s", exc)
            _OUTPUT_KIND_HANDLERS = {}
    return _OUTPUT_KIND_HANDLERS


class _NatsPublishAdapter:
    """Adapt the StandardDeps ``nats_publish`` callable to the OutputDeps
    ``NatsPublisher`` protocol.

    The alert sink (data/outputs/alert_sinks/nats.py) publishes via
    ``publish_core`` (``legba.alerts.*`` is streamless — see _nats_publish).
    DQ-C2 (2026-06-21): this adapter previously exposed ONLY ``publish_json``,
    so every alert raised ``AttributeError: ... has no attribute publish_core``
    and 100% of alert deliveries failed. Both methods delegate to the single
    ``nats_publish`` closure, which routes alert subjects to core NATS and
    durable substrate-write subjects to JetStream — so the transport is correct
    regardless of which method the caller invokes."""

    def __init__(self, fn: Callable[[str, bytes], Awaitable[None]]) -> None:
        self._fn = fn

    async def publish_json(self, subject: str, payload: bytes) -> None:
        await self._fn(subject, payload)

    async def publish_core(self, subject: str, payload: bytes) -> None:
        await self._fn(subject, payload)


async def _emit_output_bindings(
    *,
    descriptor: Any,
    payload: Any,
    output_id: Any,
    derived_from: Any,
    target_id: str | None,
    nats_publish: Callable[[str, bytes], Awaitable[None]] | None,
    pg_pool: Any = None,
    http_client: Any = None,
) -> None:
    """Invoke emit-capable output-kind handlers for ``descriptor.outputs``.

    Best-effort: the finding is already durable, so any export failure logs and
    never breaks the run. Output kinds with no ``emit`` surface (``substrate`` /
    ``a2a_skill`` / ``mcp_tool``) are skipped.

    When ``pg_pool`` is supplied it is threaded onto ``OutputDeps.pg_pool`` and
    a per-run ``OutputContext`` (analyst identity + the persisted output row id
    as ``alert_row_id``) is passed so the ``alert`` kind can write its
    ``alert_sink_deliveries`` audit rows (L-197 delivery wiring — the prior gap
    that left ``alert_sink_deliveries=0``).
    """
    bindings = list(getattr(descriptor, "outputs", None) or [])
    if not bindings:
        return
    handlers = _output_kind_handlers()
    # Build the descriptor-shaped mapping each emit handler reads its config
    # from (``outputs[].config``), and a single OutputDeps for the transports.
    # Inject the run's target_id into each binding's config so emit handlers
    # (e.g. stix_bundle) key their subject/file by the country target rather
    # than falling back to "unknown". A config-level target_id still wins.
    desc_map = {
        "outputs": [
            {
                "kind": getattr(b, "kind", None),
                "config": {
                    **({"target_id": target_id} if target_id else {}),
                    **(dict(getattr(b, "config", None) or {})),
                },
            }
            for b in bindings
        ]
    }
    try:
        from ..data.outputs._contract import OutputContext, OutputDeps

        odeps = OutputDeps(
            nats=_NatsPublishAdapter(nats_publish) if nats_publish is not None else None,
            pg_pool=pg_pool,
            http=http_client,
        )
        identity = getattr(descriptor, "identity", None)
        octx = OutputContext(
            analyst_id=str(getattr(identity, "id", "") or ""),
            analyst_version=str(getattr(identity, "version", "") or ""),
            target_id=str(target_id or ""),
            alert_row_id=str(output_id) if output_id is not None else None,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("dapr_actors.output_emit.deps_failed err=%s", exc)
        return
    import inspect

    # The emit-capable kinds have heterogeneous signatures (stix_bundle/alert
    # take ctx+output_id+derived_from; webhook takes only descriptor+deps;
    # nats_stream takes subject+deps). Pass each emit ONLY the kwargs it
    # actually declares so a binding to a narrow-signature kind doesn't raise
    # on unexpected kwargs.
    _candidate_kwargs = {
        "descriptor": desc_map,
        "deps": odeps,
        "ctx": octx,
        "output_id": output_id,
        "derived_from": derived_from,
    }
    for binding in bindings:
        kind = getattr(binding, "kind", None)
        handler = handlers.get(kind) if kind else None
        emit = getattr(handler, "emit", None) if handler is not None else None
        if emit is None:
            continue
        try:
            params = inspect.signature(emit).parameters
            accepts_var_kw = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
            kwargs = (
                dict(_candidate_kwargs)
                if accepts_var_kw
                else {k: v for k, v in _candidate_kwargs.items() if k in params}
            )
            await emit(payload, **kwargs)
            logger.info("dapr_actors.output_emit.ok kind=%s output_id=%s", kind, output_id)
        except Exception as exc:  # noqa: BLE001 — never break a durable run
            logger.warning(
                "dapr_actors.output_emit.failed kind=%s err=%s", kind, exc,
            )


def _is_indicator_activation(payload: Any) -> bool:
    """S3-T4 trigger class (b) — is this finding a S3-T2 indicator FLIP into
    ``triggered``?

    ``indicator_tracker`` emits a summary FINDING carrying
    ``data['activation_count']`` (the count of ``not_observed → triggered``
    flips this sweep) and, when any activation fired, the ``indicator_triggered``
    tag. Either signal marks a pre-registered warning signpost firing — an
    escalation trigger in its own right, independent of the finding's
    confidence/severity. Tolerant of the free-form JSONB shape; returns False on
    anything malformed (degrade, never raise into the run).
    """
    data = getattr(payload, "data", None)
    if isinstance(data, dict):
        try:
            if int(data.get("activation_count") or 0) > 0:
                return True
        except (TypeError, ValueError):
            pass
    tags = getattr(payload, "tags", None)
    if isinstance(tags, (list, tuple)) and "indicator_triggered" in tags:
        return True
    return False


async def _maybe_escalate_finding(
    conn: Any,
    *,
    escalation: Any,
    payload: Any,
    output_row_id: Any,
    target_id: str | None,
    actor_id: str,
    verification_block: dict[str, Any] | None = None,
) -> None:
    """A-3c / S3-T4 — gate + fire the ``escalate_finding`` pack for one landed
    finding.

    TWO escalation trigger classes feed the escalate pack (which stays the
    delivery edge):

      (a) the ``effective_confidence × severity`` score crosses the gate
          (:func:`escalation_gate_decision`); OR
      (b) a S3-T2 ``indicator_tracker`` FLIP into ``triggered``
          (:func:`_is_indicator_activation`) — a pre-registered warning signpost
          firing escalates on its own, independent of confidence/severity.

    Severity (trigger a) resolves from, in order: the payload ``severity`` field
    (alert kinds), ``payload.data['severity']``, then the ``severity:<level>``
    TAG the bounded units stamp (S3-T4 keys the alert path on the unit tag,
    mirroring the write-path column lift in ``provenance/writes``).

    S8-T2 — the score gates on the verify-DEMOTED EFFECTIVE confidence, not the
    raw LLM-asserted ``payload.confidence``. When the faithfulness verify pass
    produced a verdict for this finding (``verification_block``), fold it exactly
    as the read-path gate does: ``effective = min(confidence,
    faithfulness_score[, confidence_ceiling])``. A finding the verify pass
    floored therefore cannot escalate on its pre-verify number — nor on a
    high-severity tag alone (the raw-confidence gate S3-T4 closes).
    ``verification_block`` is NULL when nothing was verified (TRACE_ONLY / a
    non-verify kind) → effective == raw (gate unchanged).

    The target leg resolves PER RUN: the finding's target supplies its
    ``allowed_action_packs`` + scope for the applicability predicate. No
    target in context → empty allow-list → the resolution denies with an
    operator-visible governor BLOCK (escalation is a target-bound
    capability by design) — so trigger (b) on a target-less META flip finding is
    RECOGNIZED here but still governed at the delivery edge.
    """
    from ..data.analysts.agency.binding import (
        GLOBAL_SCOPE,
        escalation_gate_decision,
    )
    from ..data.analysts.agency.resolution import scope_view_from_target

    severity = getattr(payload, "severity", None)
    data = getattr(payload, "data", None)
    if severity is None and isinstance(data, dict):
        severity = data.get("severity")
    if severity is None:
        severity = severity_from_tags(getattr(payload, "tags", None))
    confidence = getattr(payload, "confidence", None)
    # Fold the faithfulness verdict into the gated confidence. Mirrors the
    # critique payload's ``overall_score = min(faithfulness_score,
    # confidence_ceiling)`` and the read-path ``effective_confidence`` — both
    # keys live on ``verification_block`` (FaithfulnessReport.as_dict).
    if confidence is not None and verification_block is not None:
        for _cap_key in ("faithfulness_score", "confidence_ceiling"):
            _cap = verification_block.get(_cap_key)
            if isinstance(_cap, (int, float)) and not isinstance(_cap, bool):
                confidence = min(confidence, float(_cap))
    if not (
        escalation_gate_decision(
            severity=severity,
            confidence=confidence,
            severity_gate=escalation.severity_gate,
            confidence_gate=escalation.confidence_gate,
        )
        or _is_indicator_activation(payload)
    ):
        return

    target_allows: list[Any] | None = None
    scope = None
    if target_id:
        row = await conn.fetchrow(
            "SELECT body FROM target_descriptors "
            "WHERE descriptor_id = $1 AND is_head = TRUE",
            target_id,
        )
        if row is not None:
            body = row["body"]
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except Exception:
                    body = {}
            target_allows = list((body or {}).get("allowed_action_packs") or [])
            scope = scope_view_from_target(body or {})
    if scope is None:
        scope = GLOBAL_SCOPE

    bound = escalation.binding.for_target(scope=scope, target_allows=target_allows)
    outcome = await bound.run_tool(
        "escalate",
        {
            "severity": str(severity or "high"),
            "title": str(getattr(payload, "title", ""))[:512],
            "detail": str(getattr(payload, "body", "") or "")[:2000],
            "target_ref": f"analyst_outputs:{output_row_id}",
            "action": "escalate",
            # Durable per-delivery audit inputs (migration 0061): the finding id,
            # its country, and the verify-FOLDED effective confidence the gate
            # crossed — threaded so the ChannelEmitter writes an auditable
            # "who got alerted" row keyed on the SAME numbers this gate used.
            "output_id": str(output_row_id) if output_row_id is not None else None,
            "target_id": target_id,
            "effective_confidence": confidence,
        },
    )
    if outcome.admitted and outcome.tool_result is not None:
        logger.info(
            "dapr_actors.analyst.escalated actor_id=%s target_id=%s "
            "output_id=%s status=%s",
            actor_id, target_id, output_row_id, outcome.tool_result.status,
        )
    else:
        logger.info(
            "dapr_actors.analyst.escalation.blocked actor_id=%s target_id=%s "
            "cause=%s detail=%s",
            actor_id, target_id, outcome.block_cause, outcome.detail,
        )


async def _gather_binding_for_target(
    conn: asyncpg.Connection,
    *,
    base: Any,
    target_id: str | None,
) -> Any:
    """Re-point the inline_target GATHER binding to one target's allow-list (S5).

    The base binding carries the assessor-constant legs (agency, pack, grants);
    the allow leg + applicability scope are PER-TARGET — a fan-out assessor
    visits many targets and only some allow the `substrate_read` pack. Mirrors
    the escalation hook's per-run re-pointing exactly: read the target's
    ``allowed_action_packs`` + scope from ``target_descriptors`` and return a
    ``for_target``-ed copy.

    META analyst path (``target_id=None`` — world_assessor / journal_assessor):
    there is no target row to read an allow-list from, so the binding SELF-ALLOWS
    its own pack under the GLOBAL scope (mirrors the consult META self-allow in
    dapr_host — a self-allow for THIS pack only, the grant leg stays real). The
    journal (§4.9) reaches this branch every run; without the self-allow its
    journal_read GATHER calls would deny at the allow leg and the agentic loop
    would silently run with no tools.
    """
    from ..data.analysts.agency.binding import GLOBAL_SCOPE
    from ..data.analysts.agency.resolution import scope_view_from_target

    target_allows: list[Any] | None = None
    scope = None
    if target_id:
        row = await conn.fetchrow(
            "SELECT body FROM target_descriptors "
            "WHERE descriptor_id = $1 AND is_head = TRUE",
            target_id,
        )
        if row is not None:
            body = row["body"]
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except Exception:
                    body = {}
            target_allows = list((body or {}).get("allowed_action_packs") or [])
            scope = scope_view_from_target(body or {})
    else:
        # META analyst (no target in context): self-allow this binding's own
        # pack so the ALLOW leg passes under the global scope (§4.9).
        from ..data.schemas.action_pack import ActionPackRef

        target_allows = [ActionPackRef(pack_id=base.pack.identity.id)]
    if scope is None:
        scope = GLOBAL_SCOPE
    return base.for_target(scope=scope, target_allows=target_allows)


async def _gather_write_bindings_for_target(
    conn: asyncpg.Connection,
    *,
    base: dict[str, Any],
    target_id: str | None,
    target_version: str | None,
    run_id: Any,
    analyst_id: str,
    analyst_version: str,
    nats_publish: Any,
) -> dict[str, Any]:
    """Re-point the SEAM #22 write/web GATHER bindings to one target — and
    inject the per-run WritebackContext into the write pack's binding.

    COPY-ON-WRITE, mirroring ``_gather_binding_for_target`` + the escalation
    hook: the host wired the assessor-constant base bindings; here we (1) read
    the running target's ``allowed_action_packs`` + scope ONCE and ``for_target``
    each base binding (re-pointing the allow leg — the per-target leg of the
    three-way gate), and (2) for a write binding, build a per-run
    :class:`WritebackContext` (the run's pg_pool + a fresh per-run
    :class:`AnalystContext`) and clone the binding's ToolContext WITH that
    writeback set. We NEVER mutate the shared base binding or its ToolContext —
    a fan-out assessor runs many targets concurrently against the SAME base, so
    mutating it would race (the documented risk). Returns the options payload the
    inline_target runner consumes: ``{"bindings": {tool -> re-pointed binding},
    "web_fragments": [...]|None, "write_fragments": [...]|None}``.
    """
    from ..data.analysts.agency.binding import GLOBAL_SCOPE
    from ..data.analysts.agency.journal_propose import JOURNAL_PROPOSE_PACK_ID
    from ..data.analysts.agency.resolution import scope_view_from_target
    from ..data.analysts.agency.tools import ToolContext, WritebackContext
    from ..data.analysts.agency.write_tools import WRITE_PACK_ID

    target_allows: list[Any] | None = None
    scope = None
    if target_id:
        row = await conn.fetchrow(
            "SELECT body FROM target_descriptors "
            "WHERE descriptor_id = $1 AND is_head = TRUE",
            target_id,
        )
        if row is not None:
            body = row["body"]
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except Exception:
                    body = {}
            target_allows = list((body or {}).get("allowed_action_packs") or [])
            scope = scope_view_from_target(body or {})
    if scope is None:
        scope = GLOBAL_SCOPE

    base_bindings: dict[str, Any] = base.get("bindings") or {}
    # The pack ids whose tools NEED the per-run WritebackContext (a connection
    # source + the run identity). propose_facts writes live facts/hypotheses;
    # journal_propose (plan §7 / Wave 4) writes a pending journal_proposals row.
    # Both reach ctx.writeback; web_access does not. NOTE: journal_propose's
    # writeback carries pg_pool + AnalystContext ONLY — it reaches NO provenance
    # writer (its handlers run a single INSERT into journal_proposals).
    writeback_pack_ids = frozenset({WRITE_PACK_ID, JOURNAL_PROPOSE_PACK_ID})

    # One per-run WritebackContext + AnalystContext shared by the write pack's
    # tools (they all stamp the same run identity). Built once per run, not per
    # tool, so a propose_fact + an open_question in the same GATHER share lineage.
    analyst_ctx = AnalystContext(
        analyst_id=analyst_id,
        analyst_version=analyst_version,
        run_id=run_id,
        target_id=target_id,
        target_version=target_version,
    )

    # for_target returns a fresh binding; we replace its ToolContext only for the
    # write pack (where the writeback is needed) — also a copy, never the base's.
    repointed: dict[str, Any] = {}
    # Cache one re-pointed binding per distinct base binding object so the two
    # web tools (or three write tools) that share a pack share one re-point.
    by_base: dict[int, Any] = {}
    for tool_name, base_binding in base_bindings.items():
        key = id(base_binding)
        bound = by_base.get(key)
        if bound is None:
            bound = base_binding.for_target(
                scope=scope, target_allows=target_allows
            )
            if base_binding.pack.identity.id in writeback_pack_ids:
                # Copy-on-write the ToolContext WITH the per-run writeback. The
                # base ToolContext is shared across runs — clone its fields.
                src_ctx = bound.tool_context
                bound.tool_context = ToolContext(
                    queue=src_ctx.queue,
                    emit=src_ctx.emit,
                    substrate=src_ctx.substrate,
                    writeback=WritebackContext(
                        pg_pool=bound.pg_pool,
                        analyst_ctx=analyst_ctx,
                        publish_fn=nats_publish,
                    ),
                )
            by_base[key] = bound
        repointed[tool_name] = bound

    return {
        "bindings": repointed,
        "web_fragments": base.get("web_fragments"),
        "write_fragments": base.get("write_fragments"),
    }
