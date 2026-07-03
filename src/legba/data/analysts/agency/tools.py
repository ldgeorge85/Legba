# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Action-pack tool interface + seed handler library (P-11 / PIVOT §4.8).

A pack's :class:`legba.data.schemas.action_pack.ToolSpec` names an action; this
module is the runtime side — the tool HANDLER registry + the seed handlers the
P-11 task asks for. The mechanism is complete (resolve → govern → dispatch);
the library is intentionally minimal but real:

  * ``process_media``    — wires to the W2 job plane: builds a
                           :class:`JobEnvelope` for the ``process_media`` job
                           kind and enqueues it onto the NATS work-queue. The
                           tool RETURNS the enqueued job id; the worker pool
                           (P-07) does the extraction + lands the derived
                           signal. ``async_job=True`` on the ToolSpec.
  * ``escalate`` /
    ``create_incident``  — emit to the pack's channels (existing output kinds:
                           alert / webhook / nats_stream / a2a_skill / …) via
                           the channel emitter. Synchronous; returns the
                           emitted-channel summary.
  * the four ``substrate_read`` tools (``search_signals`` / ``query_facts``
    / ``inspect_entity`` / ``vector_search``) live in
    :mod:`.substrate_read` and query the read-only SubstrateQueryPort —
    the consult loop's governed tool surface (A-3a).

(``discover_sources`` was removed per decision F-1 — its enqueue fed job
kinds no worker handler ever consumed; deep-crawl discovery is a designed
direction item, docs/DIRECTION.md §8.)

A handler is ``async (call, ctx) -> ToolResult``. It NEVER decides agency —
resolution + the governor have already admitted the call when a handler runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from uuid import UUID

from ...schemas.action_pack import ActionPack, Channel, ToolSpec

if TYPE_CHECKING:  # pragma: no cover — typing only
    from ...provenance import AnalystContext
    from ....runtime.jobs.queue import JobQueue

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool call + result + context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """A request to run one pack tool.

    ``args`` are the tool-specific inputs (e.g. ``media_ref`` + ``extraction``
    for process_media). ``requested_by`` / ``budget_account`` / ``tenant_id``
    bill + audit the call; they flow into the job envelope + the governor ledger.
    """

    pack_id: str
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    requested_by: str = "system"
    budget_account: str = "system"
    tenant_id: str = "default"


@dataclass(frozen=True)
class ToolResult:
    """A tool handler's outcome.

    ``status`` is the handler-level result; ``output`` carries handler-specific
    refs (an enqueued job id, the emitted channels). ``cost_usd`` / ``units``
    are the cost dimensions the governor records onto the invocation ledger.
    """

    status: str                       # enqueued | emitted | failed | noop
    output: dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0
    units: int = 1
    job_id: UUID | None = None
    error: str | None = None


@dataclass
class WritebackContext:
    """The operator-gated write surface a propose/write tool reaches for (S6).

    The write tools (``propose_fact`` / ``request_source`` / ``open_question``)
    must flow through the SAME provenance-stamped writers the analyst run path
    uses — ``write_fact`` / ``write_hypothesis`` — never a raw INSERT, so an
    agent's write-back is gated, lineage-stamped, and DLQ-safe exactly like a
    first-class output. This holder carries the two things those writers need
    that the (constant) :class:`ToolContext` cannot pin at binding time:

      * ``pg_pool``    — the connection source (the writers own the txn).
      * ``analyst_ctx``— the per-RUN provenance identity (analyst id/version +
                         run id + the running target). The runtime re-points
                         this per run (mirroring the escalation binding's
                         per-target re-point) so a write is stamped to the run
                         that proposed it.

    Both are required for a write tool to run; absent → the handler returns a
    ``failed`` ToolResult naming the missing surface (no silent no-op, no
    un-stamped write).
    """

    pg_pool: Any
    analyst_ctx: "AnalystContext"
    publish_fn: Any | None = None


@dataclass
class ToolContext:
    """Shared dependencies a tool handler may reach for.

    ``queue`` is the W2 job plane (process_media enqueue). ``emit`` is the
    channel emitter (escalate / create_incident). ``substrate`` is the
    read-only SubstrateQueryPort the ``substrate_read`` pack's tools query
    (A-3a — the consult loop's governed tool surface). ``writeback`` is the
    operator-gated write surface the propose/write tools use (S6 — only wired
    for an analyst that grants a write pack on an allowing target). Any may be
    None when a process doesn't wire that surface; a handler that needs a
    missing dependency returns a ``failed`` ToolResult rather than raising.
    """

    queue: "JobQueue | None" = None
    emit: "ChannelEmitter | None" = None
    substrate: Any | None = None
    writeback: "WritebackContext | None" = None


ToolHandler = Callable[[ToolCall, ActionPack, ToolContext], Awaitable[ToolResult]]


# ---------------------------------------------------------------------------
# Channel emitter — escalate / create_incident → existing output kinds.
# ---------------------------------------------------------------------------


class ChannelEmitter:
    """Emits a pack action to its declared channels (existing output kinds).

    The default emitter publishes ``alert`` / ``nats_stream`` channels onto NATS
    (coarse subject) and logs the rest; an operator wires a richer emitter (real
    webhook POST, a2a skill invoke) by subclassing / injecting. Kept thin so the
    SEAM is complete without re-implementing every sink — the output-kind
    surface is reused, not re-cut.

    ``pg_pool`` (optional, migration 0061) makes the emit DURABLY auditable: the
    NATS publish stays the delivery edge, but every emit ALSO writes one row to
    ``alert_sink_deliveries`` (repurposed into the unified per-delivery audit —
    see the migration comment) recording WHAT was delivered WHERE and whether
    the publish confirmed. That is the durable answer to "who got alerted?" — the
    in-memory ``emitted`` list is process-local and vanishes on restart. The
    write is best-effort: an audit-write failure is logged, never raised, so a
    blipped writer connection cannot break an escalation the operator needs.
    """

    def __init__(
        self,
        *,
        nats_publish: Callable[[str, bytes], Awaitable[None]] | None = None,
        pg_pool: Any | None = None,
    ) -> None:
        self._nats_publish = nats_publish
        self._pg_pool = pg_pool
        self.emitted: list[dict[str, Any]] = []

    async def emit(
        self, channel: Channel, payload: dict[str, Any]
    ) -> dict[str, Any]:
        import json

        record = {
            "channel": channel.name,
            "kind": channel.kind,
            "config": dict(channel.config),
            "payload": payload,
        }
        subject = str(channel.config.get("subject") or f"channels.{channel.name}")
        if channel.kind in ("alert", "nats_stream") and self._nats_publish is not None:
            try:
                await self._nats_publish(subject, json.dumps(record).encode("utf-8"))
                record["delivered"] = True
                record["subject"] = subject
            except Exception as exc:  # delivery failure is reported, not raised
                logger.warning("channel emit failed name=%s: %s", channel.name, exc)
                record["delivered"] = False
                record["error"] = str(exc)
        else:
            # webhook / a2a_skill / mcp_tool / stix_bundle, or no NATS wired:
            # log the intent. A richer emitter overrides this path.
            logger.info(
                "channel.emit name=%s kind=%s payload=%s",
                channel.name, channel.kind, payload,
            )
            record["delivered"] = self._nats_publish is None and channel.kind in (
                "alert", "nats_stream"
            )
        self.emitted.append(record)
        await self._write_delivery_audit(channel, subject, payload, record)
        return record

    async def _write_delivery_audit(
        self,
        channel: Channel,
        subject: str,
        payload: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        """Write one durable ``alert_sink_deliveries`` audit row for this emit.

        The DURABLE record of what was delivered (migration 0061). Fields the
        escalate/incident gate resolves flow in via the emit ``payload``
        (``output_id`` = the persisted analyst_outputs finding, ``target_id`` =
        the country, ``severity`` + ``effective_confidence`` = the verify-FOLDED
        alert-score inputs). ``attempted_at`` defaults to now() = the emit time.

        Best-effort: no pool wired → no-op (unit paths / test rigs). Any write
        error (FK blip, connection drop) is swallowed with a warning — the
        delivery already happened; the audit is observability, not correctness.
        """
        if self._pg_pool is None:
            return
        import json
        from uuid import UUID

        raw_output = payload.get("output_id")
        output_uuid: UUID | None = None
        if raw_output is not None:
            try:
                output_uuid = raw_output if isinstance(raw_output, UUID) else UUID(str(raw_output))
            except (ValueError, AttributeError, TypeError):
                output_uuid = None

        conf = payload.get("effective_confidence")
        try:
            eff_conf = float(conf) if conf is not None else None
        except (ValueError, TypeError):
            eff_conf = None

        delivered = bool(record.get("delivered"))
        summary = {
            "action": payload.get("action"),
            "title": str(payload.get("title") or "")[:200],
            "requested_by": payload.get("requested_by"),
            "delivered": delivered,
        }
        sql = """
            INSERT INTO alert_sink_deliveries (
                alert_row_id, channel_name, sink_kind, sink_target,
                target_id, severity, effective_confidence, attempt_number,
                status, error_message, delivered_at, payload_summary
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, 1, $8, $9, $10, $11::jsonb)
        """
        import datetime as _dt

        args = (
            output_uuid,
            channel.name,
            channel.kind,
            subject,
            payload.get("target_id"),
            payload.get("severity"),
            eff_conf,
            "delivered" if delivered else "failed",
            record.get("error"),
            _dt.datetime.now(tz=_dt.timezone.utc) if delivered else None,
            json.dumps(summary, separators=(",", ":")),
        )
        try:
            # Duck-typed .execute — an asyncpg Pool or a raw connection both work
            # (mirrors legba.data.outputs.alert._record_delivery), so this module
            # stays library-agnostic at type level.
            await self._pg_pool.execute(sql, *args)
        except Exception as exc:  # pragma: no cover - fault-injection path
            logger.warning(
                "channel.emit.audit_write_failed name=%s status=%s err=%s",
                channel.name, record.get("delivered"), exc,
            )


# ---------------------------------------------------------------------------
# Seed handlers
# ---------------------------------------------------------------------------


def _tool_spec(pack: ActionPack, tool_name: str) -> ToolSpec | None:
    for t in pack.tools:
        if t.name == tool_name:
            return t
    return None


async def process_media_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    """Enqueue a ``process_media`` job onto the W2 job plane.

    Wires the pack tool to ``legba.runtime.jobs`` (P-07): builds the generic
    :class:`JobEnvelope` with ``job_kind='process_media'`` + the typed
    ``input_refs`` (media_ref / extraction / derived_from / modality), bills it
    to the caller's ``budget_account``, and enqueues. The worker pool runs the
    extraction + lands the DERIVED signal; this tool returns the job id so the
    caller can correlate.
    """
    from ....data.jobs.envelope import JobEnvelope
    from ....data.jobs.media import configured_media_endpoint

    if ctx.queue is None:
        return ToolResult(status="failed", error="no job queue wired for process_media")

    # A-2 hard guard: refuse to enqueue when no real media endpoint is
    # configured. The worker would refuse the job anyway (no stub output may
    # land in the pool) — refusing here keeps the queue free of doomed work
    # and surfaces the misconfiguration at the caller.
    if configured_media_endpoint() is None:
        logger.error(
            "tool.process_media.refused pack=%s account=%s "
            "reason=endpoint_not_configured env=LEGBA_MEDIA_API_URL",
            pack.identity.id, call.budget_account,
        )
        return ToolResult(
            status="failed",
            error=(
                "media endpoint not configured (LEGBA_MEDIA_API_URL unset) — "
                "refusing to enqueue process_media; no stub output may land "
                "in the signal pool"
            ),
        )

    args = call.args
    required = ("media_ref", "extraction", "derived_from")
    missing = [k for k in required if k not in args]
    if missing:
        return ToolResult(
            status="failed",
            error=f"process_media missing args: {missing}",
        )

    spec = _tool_spec(pack, "process_media")
    idem = args.get("idempotency_key") or (
        f"process_media:{call.budget_account}:{args['derived_from']}:{args['extraction']}"
    )
    env = JobEnvelope(
        job_kind="process_media",
        requested_by=call.requested_by,
        budget_account=call.budget_account,
        tenant_id=call.tenant_id,
        idempotency_key=str(idem),
        input_refs={
            "media_ref": args["media_ref"],
            "extraction": args["extraction"],
            "derived_from": str(args["derived_from"]),
            "modality": args.get("modality", "audio"),
            "mime_type": args.get("mime_type"),
            "language_hint": args.get("language_hint"),
        },
    )
    await ctx.queue.enqueue(env)
    # Per-call cost: from the tool spec config (an estimate), else 0.
    cost = float((spec.config.get("cost_usd_per_call") if spec else 0) or 0)
    logger.info(
        "tool.process_media.enqueued pack=%s job_id=%s account=%s",
        pack.identity.id, env.job_id, call.budget_account,
    )
    return ToolResult(
        status="enqueued",
        output={"job_id": str(env.job_id), "idempotency_key": env.idempotency_key},
        cost_usd=cost,
        units=1,
        job_id=env.job_id,
    )


async def escalate_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    """Emit an escalation to the pack's channels (existing output kinds).

    ``args`` carry the escalation body (severity / title / detail / target_ref).
    Every channel the pack declares (or the subset named in
    ``args['channels']``) is emitted to. Synchronous (no job plane).
    """
    return await _emit_to_channels(call, pack, ctx, default_action="escalate")


async def create_incident_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    """Open an incident by emitting to the pack's channels.

    Same channel mechanism as ``escalate`` — distinct tool so a pack can grant
    one without the other (incident-create vs. escalate-existing) and so the
    governor counts them separately.
    """
    return await _emit_to_channels(call, pack, ctx, default_action="create_incident")


async def _emit_to_channels(
    call: ToolCall, pack: ActionPack, ctx: ToolContext, *, default_action: str
) -> ToolResult:
    if ctx.emit is None:
        return ToolResult(status="failed", error="no channel emitter wired")
    if not pack.channels:
        return ToolResult(status="failed", error=f"pack {pack.identity.id} has no channels")

    named = set(call.args.get("channels") or [])
    targets = [c for c in pack.channels if not named or c.name in named]
    if not targets:
        return ToolResult(
            status="failed",
            error=f"none of the requested channels {sorted(named)} exist on the pack",
        )

    payload = {
        "action": call.args.get("action", default_action),
        "severity": call.args.get("severity", "info"),
        "title": call.args.get("title", ""),
        "detail": call.args.get("detail", ""),
        "target_ref": call.args.get("target_ref"),
        "requested_by": call.requested_by,
        # Durable-audit fields (migration 0061): the ChannelEmitter reads these
        # off the payload to write the per-delivery audit row. ``output_id`` is
        # the persisted analyst_outputs finding, ``target_id`` the country, and
        # ``effective_confidence`` the verify-FOLDED alert-score confidence the
        # escalate gate crossed. Absent for a caller that does not set them
        # (e.g. a bare consult-loop create_incident) → the audit columns are NULL.
        "output_id": call.args.get("output_id"),
        "target_id": call.args.get("target_id"),
        "effective_confidence": call.args.get("effective_confidence"),
    }
    emitted = []
    for ch in targets:
        emitted.append(await ctx.emit.emit(ch, payload))
    return ToolResult(
        status="emitted",
        output={"channels": emitted},
        units=len(emitted),
    )


# (discover_sources_tool was REMOVED per decision F-1, 2026-06-09: it
# enqueued crawl_discovery/query_discovery jobs that no worker handler has
# ever consumed — a terminal "no handler" failure dressed as an enqueue.
# Source discovery ships via the registry discovery route; job-based deep
# crawl is a designed direction item — docs/DIRECTION.md §8.)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """Maps a tool name → its handler. Seeded with the P-11 library."""

    def __init__(self) -> None:
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, tool_name: str, handler: ToolHandler) -> None:
        self._handlers[tool_name] = handler

    def handler_for(self, tool_name: str) -> ToolHandler | None:
        return self._handlers.get(tool_name)

    @property
    def names(self) -> list[str]:
        return sorted(self._handlers)


def default_tool_registry() -> ToolRegistry:
    """The seed handler library wired to the W2 job plane + channels +
    the read-only substrate port (A-3a) + the S6 external/write tools.

    The external (``web_fetch`` / ``web_search``) and write (``propose_fact`` /
    ``request_source`` / ``open_question``) handlers are registered here too,
    so the agency dispatch can resolve them by name — but they only RUN when a
    pack that grants them is EFFECTIVE for an (analyst, target) pair and passes
    the governor (web tools need nothing extra; write tools also need a wired
    ``ctx.writeback``). Registration is the name→handler map; the three-way
    gate decides whether the handler is reached at all.
    """
    # Local import — substrate_read / web_tools / write_tools import
    # ToolResult/ToolContext from this module, so a module-level import here
    # would be a cycle.
    from .journal_propose import register_journal_propose_tools
    from .journal_read import register_journal_read_tools
    from .substrate_read import register_substrate_read_tools
    from .web_tools import register_web_access_tools
    from .write_tools import register_write_tools

    r = ToolRegistry()
    r.register("process_media", process_media_tool)
    r.register("escalate", escalate_tool)
    r.register("create_incident", create_incident_tool)
    register_substrate_read_tools(r)
    # journal_read reuses the substrate_read list_findings handler (idempotent
    # re-register of the same callable) so the journal_read pack's tool surface
    # is self-contained even if substrate_read is ever disabled (plan §5).
    register_journal_read_tools(r)
    # journal_propose (plan §7 / Wave 4) — the journal's PROPOSE-AND-GATE write
    # surface: each handler writes ONLY a pending journal_proposals row (asserted
    # by the gating test, tests/journal_w4). Registered here so the agency
    # dispatch resolves the name; it RUNS only when the journal grants the
    # journal_propose pack AND a per-run ctx.writeback is wired.
    register_journal_propose_tools(r)
    register_web_access_tools(r)
    register_write_tools(r)
    return r


__all__ = [
    "ChannelEmitter",
    "ToolCall",
    "ToolContext",
    "ToolHandler",
    "ToolRegistry",
    "ToolResult",
    "WritebackContext",
    "create_incident_tool",
    "default_tool_registry",
    "escalate_tool",
    "process_media_tool",
]
