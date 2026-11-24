# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``substrate_read`` pack's tool handlers (A-3a / review G2).

The consult kind's read tools used to dispatch straight to the
:class:`SubstrateQueryPort`, bypassing the agency plane entirely — the
review's G2 finding: the headline resolve ∩ allow ∩ applicability +
governor + invocation-ledger pipeline had zero production callers. These
handlers expose the SAME port surface as pack tools, so the consult loop's
tool calls now flow through :meth:`Agency.run_pack_tool` — identical
behavior for the planner, but every call is allow-listed, rate/budget
governed, and ledgered in ``action_pack_invocations``.

The original four primitives (``search_signals`` / ``query_facts`` /
``inspect_entity`` / ``vector_search``) are joined by four richer readers
(S4, Phase 1) — ``query_nexuses`` (open signed/typed reified
relationships), ``query_hypotheses`` (ACH competing-hypothesis rows),
``get_timeline`` (facts ∪ signals merged on a temporal anchor), and
``compare_targets`` (per-target substrate rollup) — so the agentic
assessors can investigate rather than one-shot.

Handlers read the port from ``ToolContext.substrate`` (wired per-binding by
the runtime). Argument coercion mirrors the consult kind's historical
dispatcher exactly, including the caller-pinned ``scope_predicate`` for
``search_signals`` (the planner cannot override a scope the operator set).

Read tools return ``ToolResult(status="completed", output=<port mapping>)``;
port-level failures return a ``failed`` ToolResult (the agency settles the
invocation row accordingly and the consult loop folds the error back into
the conversation so the planner can recover).
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Any

from ...schemas.action_pack import ActionPack
from .tools import ToolCall, ToolContext, ToolRegistry, ToolResult

logger = logging.getLogger(__name__)

SUBSTRATE_READ_PACK_ID = "substrate_read"

SUBSTRATE_READ_TOOLS = (
    "search_signals",
    "query_facts",
    "inspect_entity",
    "vector_search",
    # S5-T4 — RAG over the curated Lane-4 reference corpora (world_context /
    # tradecraft) via the port embedder (S5-T1) + S5-T2 collections. Read-only
    # BACKGROUND/method knowledge, not live substrate.
    "search_context",
    # Stage 1 — the OpenSearch full-text corpus (index legba_signals_corpus):
    # BM25 lexical search over the WHOLE raw body of every ingested signal +
    # a by-id fetch of one signal's full indexed doc. Makes the corpus AGENTIC.
    "search_corpus",
    "read_document",
    "query_nexuses",
    "query_hypotheses",
    "get_timeline",
    "compare_targets",
    # P5 / #99 — DB-side recursive-CTE graph traversal over the open nexus
    # graph so an analyst can pull the proxy path / the broker between two
    # camps on demand (bounded: max_hops<=3, visited-set guard, row caps).
    "query_paths",
    "find_proxy_chains",
    "query_brokers",
    # Palette expansion — the platform's OWN finished intelligence, so an
    # agentic assessor builds on prior products instead of re-deriving from
    # the raw signal firehose (read-only; reuses this pack's kind).
    "list_findings",
    "list_situations",
    "query_predictions",
    # V3/P3 — the decision-time register: "what did Legba believe on date D"
    # (spec §3.4). Read-only; reuses this pack's kind.
    "belief_as_of",
    # V3/P6 — the event surface (spec §6.1): the filtered bounded-occurrence
    # list and the one-event dossier (signals / actors / edges / tracking
    # situations / lifecycle ledger). Read-only; reuses this pack's kind.
    "query_events",
    "inspect_event",
    # Navigation readers — consult-surface ONLY (the GATHER assessors do NOT
    # offer these: an assessor already has its target_id). They MUST still be
    # pack tools, because the production consult loop is GOVERNED through this
    # pack — a tool absent from the pack is blocked as unknown_tool even on the
    # consult path. (Not added to inline_target._GATHER_READ_TOOLS by design.)
    "list_targets",
    "list_sources",
    # W11-F141 — the scheduled-events calendar (migration 0240): the
    # operator-entered register of FUTURE dated things, so a consult can tell
    # "nothing scheduled" apart from "could not look".
    "list_scheduled_events",
    # W11-F162 — the forecast ledger's due-date reader (migrations 0242/0244).
    "list_due_resolvers",
    # W11-F152 — per-source health with a staleness verdict (fresh / quiet /
    # stale / dead against the feed's own cadence). The journal_read pack
    # already grants it; the consult loop is governed through THIS pack, so
    # without the entry it is blocked as unknown_tool.
    "get_source_health",
    # 7g-2 — the COLLECTION series reads. HISTORY, not now: the only tools in
    # this pack that reach `observations` (the bitemporal store a curated
    # holding loads once and nothing schedules), and the only ones whose rows
    # are not live substrate. They stay in THIS pack because the production
    # consult loop is governed through it — a tool absent from the pack is
    # blocked as unknown_tool — and because they are reads like every other
    # tool here. What makes them different is stated in their own rows, not in
    # a second pack: every row carries its valid period, its record time and
    # the sha256 of the file the number came from.
    "series_history",
    "series_compare",
    # E2 — the LIVE numeric series (chokepoint transits, policy rates:
    # `observations` rows with origin_class='live', collection 'live:<source>').
    # A SEPARATE, explicitly labelled reader: the two history tools above never
    # select a live row (migration 0246, SEAMS #57/#62) and that stays so.
    "live_series",
)


def _families(args: Any) -> list[str] | None:
    """Read an optional ``edge_family`` filter off an untyped tool-call arg.

    W3-A: the graph walks became family-aware, so a caller can ask for the
    co-mention cloud explicitly (``families: ["cooccurrence"]``) instead of
    getting it by default. Anything that is not a list of strings degrades to
    ``None`` (the port's asserting default) rather than raising — these args
    arrive from LLM tool-use and a malformed one must not fail the turn. The
    port validates the values; unknown families fall back there.
    """
    raw = args.get("families") or args.get("edge_families")
    if not isinstance(raw, list):
        return None
    out = [str(x).strip() for x in raw if str(x).strip()]
    return out or None


def _subjects(args: Any) -> list[str]:
    """The subject list for ``series_compare``, off an untyped tool-call arg.

    Accepts a list, or a single string (a planner that wrote ``subjects:
    "US"`` meant one subject, and refusing that would spend a round teaching
    it JSON). Anything else yields ``[]``, which the port turns into a NAMED
    refusal rather than a silent all-subjects read.
    """
    raw = args.get("subjects")
    if isinstance(raw, str):
        raw = [p for p in re.split(r"[,\s]+", raw) if p]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


# ---------------------------------------------------------------------------
# X3/G0 — PACK-CONFIGURED read scopes (the bake-off's arm C).
#
# The handler is shared by every pack that lists the tool; what differs per pack
# is the ToolSpec ``config``. Two keys are read, and ONLY from the pack — never
# from the planner's args (the agency's arg grammar refuses a planner-supplied
# ``verified_only`` as ``invalid_arg``; the handler drops it again), so a
# reader can neither escape the scope nor opt into it:
#
#   * ``verified_only: true`` on ``list_findings`` / ``belief_as_of`` — only the
#     findings a composition would admit (``legba.data.verified_findings``);
#   * ``receipt_scoped: true`` on ``read_document`` — only a signal an admitted
#     finding cites. Anything else is refused by name, not read.
#
# A pack that sets neither (``substrate_read``) behaves byte-identically.
# ---------------------------------------------------------------------------

_VERIFIED_KEY = "verified_only"


def _tool_config(pack: ActionPack | None, name: str) -> dict[str, Any]:
    for spec in getattr(pack, "tools", None) or []:
        if getattr(spec, "name", None) == name:
            return dict(getattr(spec, "config", None) or {})
    return {}


def _scoped_call(call: ToolCall, pack: ActionPack | None, name: str) -> ToolCall:
    """``call`` with the planner's ``verified_only`` replaced by the pack's."""
    args = {k: v for k, v in call.args.items() if k != _VERIFIED_KEY}
    if _tool_config(pack, name).get(_VERIFIED_KEY) is True:
        args[_VERIFIED_KEY] = True
    return replace(call, args=args)


def _verified_kw(args: dict[str, Any]) -> dict[str, Any]:
    """The port kwarg, only when set — a port without the flag stays callable."""
    return {_VERIFIED_KEY: True} if args.get(_VERIFIED_KEY) is True else {}


def _findings_paging_kw(args: dict[str, Any]) -> dict[str, Any]:
    """W11-F150 — ``list_findings``' survey mode and paging kwargs, only when
    set, so a port without them stays callable and a plain read is unchanged.

    ``limit`` rides here too: a survey with no ``limit`` takes the port's
    survey default (a page of targets), not the 20-row page default.
    """
    kw: dict[str, Any] = {}
    if args.get("mode") is not None:
        kw["mode"] = str(args["mode"]).strip().lower()
    raw = args.get("target_ids")
    if isinstance(raw, str):
        raw = [p for p in re.split(r"[,\s]+", raw) if p]
    if isinstance(raw, (list, tuple)) and raw:
        kw["target_ids"] = [str(x) for x in raw]
    if args.get("offset") is not None:
        kw["offset"] = int(args["offset"])
    if args.get("limit") is not None:
        kw["limit"] = int(args["limit"])
    elif kw.get("mode") != "survey":
        kw["limit"] = 20
    return kw


async def _call_port(
    call: ToolCall, ctx: ToolContext, name: str
) -> ToolResult:
    port = ctx.substrate
    if port is None:
        return ToolResult(
            status="failed",
            error=f"no SubstrateQueryPort wired for {name} (ToolContext.substrate is None)",
        )
    args = call.args
    try:
        if name == "search_signals":
            out = await port.search_signals(
                query=str(args.get("query", "")),
                limit=int(args.get("limit", 20)),
                scope_predicate=args.get("scope_predicate"),
            )
        elif name == "query_facts":
            out = await port.query_facts(
                subject=args.get("subject"),
                predicate=args.get("predicate"),
                value=args.get("value"),
                limit=int(args.get("limit", 30)),
                as_of=args.get("as_of"),
            )
        elif name == "inspect_entity":
            out = await port.inspect_entity(name=str(args.get("name", "")))
        elif name == "vector_search":
            out = await port.vector_search(
                query=str(args.get("query", "")),
                limit=int(args.get("limit", 10)),
            )
        elif name == "search_context":
            out = await port.search_context(
                query=str(args.get("query", "")),
                corpus=args.get("corpus"),
                country=args.get("country"),
                k=int(args.get("k", 6)),
            )
        elif name == "search_corpus":
            out = await port.search_corpus(
                query=str(args.get("query", "")),
                filters=args.get("filters"),
                size=int(args.get("size", 10)),
            )
        elif name == "read_document":
            out = await port.read_document(
                doc_id=str(args.get("doc_id", "")),
            )
        elif name == "query_nexuses":
            out = await port.query_nexuses(
                subject=args.get("subject"),
                obj=args.get("object"),
                rel_type=args.get("rel_type"),
                polarity=(
                    int(args["polarity"])
                    if args.get("polarity") is not None
                    else None
                ),
                limit=int(args.get("limit", 30)),
                as_of=args.get("as_of"),
            )
        elif name == "query_hypotheses":
            out = await port.query_hypotheses(
                target_id=args.get("target_id"),
                status=args.get("status"),
                situation_id=args.get("situation_id"),
                limit=int(args.get("limit", 30)),
            )
        elif name == "get_timeline":
            out = await port.get_timeline(
                subject=str(args.get("subject", "")),
                limit=int(args.get("limit", 40)),
                since=args.get("since"),
                until=args.get("until"),
            )
        elif name == "compare_targets":
            raw_targets = args.get("target_ids") or []
            target_ids = [str(t) for t in raw_targets] if isinstance(raw_targets, list) else []
            out = await port.compare_targets(target_ids=target_ids)
        elif name == "query_paths":
            pp = args.get("polarity_product")
            out = await port.query_paths(
                subject=str(args.get("subject", "")),
                obj=str(args.get("object", "")),
                max_hops=int(args.get("max_hops", 3)),
                polarity_product=int(pp) if pp is not None else None,
                limit=int(args.get("limit", 30)),
                families=_families(args),
                as_of=args.get("as_of"),
            )
        elif name == "find_proxy_chains":
            pp = args.get("polarity_product")
            out = await port.find_proxy_chains(
                subject=str(args.get("subject", "")),
                obj=str(args.get("object", "")),
                max_hops=int(args.get("max_hops", 3)),
                polarity_product=int(pp) if pp is not None else None,
                limit=int(args.get("limit", 30)),
                families=_families(args),
                as_of=args.get("as_of"),
            )
        elif name == "query_brokers":
            raw_a = args.get("camp_a") or []
            raw_b = args.get("camp_b") or []
            out = await port.query_brokers(
                camp_a=[str(x) for x in raw_a] if isinstance(raw_a, list) else [],
                camp_b=[str(x) for x in raw_b] if isinstance(raw_b, list) else [],
                max_hops=int(args.get("max_hops", 3)),
                limit=int(args.get("limit", 50)),
                families=_families(args),
                as_of=args.get("as_of"),
            )
        elif name == "list_findings":
            out = await port.list_findings(
                target_id=args.get("target_id"),
                analyst_id=args.get("analyst_id"),
                severity=args.get("severity"),
                since_hours=(
                    int(args["since_hours"])
                    if args.get("since_hours") is not None
                    else None
                ),
                # R1 / W2-T1: superseded findings excluded by default; opt in
                # for history/audit reads.
                include_superseded=str(
                    args.get("include_superseded", False)
                ).lower() in ("true", "1"),
                believed_as_of=args.get("believed_as_of"),
                **_verified_kw(args),
                **_findings_paging_kw(args),
            )
        elif name == "list_situations":
            out = await port.list_situations(
                status=args.get("status"),
                target_id=args.get("target_id"),
                since_hours=(
                    int(args["since_hours"])
                    if args.get("since_hours") is not None
                    else None
                ),
                limit=int(args.get("limit", 20)),
                as_of=args.get("as_of"),
            )
        elif name == "belief_as_of":
            out = await port.belief_as_of(
                as_of=str(args.get("as_of", "")),
                target_id=args.get("target_id"),
                fold_verdicts=str(args.get("fold_verdicts", "as_of")),
                limit=int(args.get("limit", 20)),
                **_verified_kw(args),
            )
        elif name == "query_events":
            raw_geo = args.get("geo")
            raw_origin = args.get("include_origin")
            out = await port.query_events(
                target_id=args.get("target_id"),
                geo=(
                    [str(g) for g in raw_geo]
                    if isinstance(raw_geo, list)
                    else (str(raw_geo) if raw_geo is not None else None)
                ),
                category=args.get("category"),
                lifecycle_state=args.get("lifecycle_state"),
                entity=args.get("entity"),
                since=args.get("since"),
                until=args.get("until"),
                as_of=args.get("as_of"),
                include_origin=(
                    [str(c) for c in raw_origin]
                    if isinstance(raw_origin, list)
                    else None
                ),
                situation_id=args.get("situation_id"),
                limit=int(args.get("limit", 20)),
            )
        elif name == "inspect_event":
            out = await port.inspect_event(
                event_id=str(args.get("event_id", "")),
            )
        elif name == "query_predictions":
            out = await port.query_predictions(
                target_id=args.get("target_id"),
                status=args.get("status"),
                limit=int(args.get("limit", 20)),
            )
        elif name == "list_targets":
            out = await port.list_targets(
                active_only=bool(args.get("active_only", True)),
            )
        elif name == "list_sources":
            out = await port.list_sources(
                active_only=bool(args.get("active_only", True)),
                silent_only=bool(args.get("silent_only", False)),
                silent_hours=int(args.get("silent_hours", 48)),
            )
        elif name == "list_scheduled_events":
            raw_keys = args.get("subject_keys")
            out = await port.list_scheduled_events(
                window_start=args.get("window_start"),
                window_end=args.get("window_end"),
                scope=args.get("scope"),
                subject_keys=(
                    [str(k) for k in raw_keys]
                    if isinstance(raw_keys, list)
                    else ([str(raw_keys)] if raw_keys is not None else None)
                ),
                limit=int(args.get("limit", 50)),
            )
        elif name == "list_due_resolvers":
            out = await port.list_due_resolvers(
                horizon_days=int(args.get("horizon_days", 14)),
                include_closed=bool(args.get("include_closed", False)),
                limit=int(args.get("limit", 50)),
            )
        elif name == "get_source_health":
            out = await port.get_source_health(
                silent_only=bool(args.get("silent_only", False)),
                silent_hours=int(args.get("silent_hours", 48)),
                limit=int(args.get("limit", 200)),
            )
        elif name == "series_history":
            out = await port.series_history(
                series_id=str(args.get("series_id", "")),
                subject=str(args.get("subject", "")),
                since=args.get("from") or args.get("since"),
                until=args.get("to") or args.get("until"),
                as_of=args.get("as_of"),
                collection_id=args.get("collection_id"),
                limit=int(args.get("limit", 500)),
            )
        elif name == "series_compare":
            out = await port.series_compare(
                series_id=str(args.get("series_id", "")),
                subjects=_subjects(args),
                since=args.get("from") or args.get("since"),
                until=args.get("to") or args.get("until"),
                as_of=args.get("as_of"),
                collection_id=args.get("collection_id"),
                limit=int(args.get("limit", 500)),
            )
        elif name == "live_series":
            out = await port.live_series(
                series_id=args.get("series_id"),
                subject=args.get("subject"),
                source_id=args.get("source_id"),
                since=args.get("from") or args.get("since"),
                until=args.get("to") or args.get("until"),
                limit=int(args.get("limit", 200)),
            )
        else:  # pragma: no cover — registry only maps the known names
            return ToolResult(status="failed", error=f"unknown substrate tool {name!r}")
    except Exception as exc:  # noqa: BLE001 — port failures fold into the loop
        logger.warning("substrate_read.tool.error tool=%s err=%s", name, exc)
        return ToolResult(status="failed", error=f"tool_failed: {exc!s}")
    return ToolResult(status="completed", output=dict(out))


async def search_signals_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "search_signals")


async def query_facts_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "query_facts")


async def inspect_entity_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "inspect_entity")


async def vector_search_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "vector_search")


async def search_context_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "search_context")


async def search_corpus_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "search_corpus")


async def read_document_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    if _tool_config(pack, "read_document").get("receipt_scoped") is True:
        checker = getattr(ctx.substrate, "verified_receipt", None)
        if checker is None:
            return ToolResult(
                status="failed",
                error="receipt_scope_unavailable: this pack opens receipts of "
                "verified findings only, and the wired port cannot check scope",
            )
        doc_id = str(call.args.get("doc_id", ""))
        try:
            admitted = await checker(doc_id=doc_id)
        except Exception as exc:  # noqa: BLE001 — a failed check is a refusal
            logger.warning("substrate_read.receipt_scope.error err=%s", exc)
            admitted = False
        if not admitted:
            return ToolResult(
                status="failed",
                error=f"receipt_scope: {doc_id!r} is not cited by any verified "
                "finding — this reader opens the receipts of verified findings "
                "only (pass a signal id a returned finding cites)",
            )
    return await _call_port(call, ctx, "read_document")


async def query_nexuses_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "query_nexuses")


async def query_hypotheses_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "query_hypotheses")


async def get_timeline_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "get_timeline")


async def compare_targets_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "compare_targets")


async def query_paths_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "query_paths")


async def find_proxy_chains_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "find_proxy_chains")


async def query_brokers_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "query_brokers")


async def list_findings_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(_scoped_call(call, pack, "list_findings"), ctx, "list_findings")


async def list_situations_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "list_situations")


async def query_predictions_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "query_predictions")


async def belief_as_of_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(_scoped_call(call, pack, "belief_as_of"), ctx, "belief_as_of")


async def query_events_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "query_events")


async def inspect_event_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "inspect_event")


async def list_targets_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "list_targets")


async def list_sources_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "list_sources")


async def list_scheduled_events_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "list_scheduled_events")


async def list_due_resolvers_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "list_due_resolvers")


async def get_source_health_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "get_source_health")


async def series_history_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "series_history")


async def series_compare_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "series_compare")


async def live_series_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    return await _call_port(call, ctx, "live_series")


def register_substrate_read_tools(registry: ToolRegistry) -> None:
    """Register the read handlers (called by ``default_tool_registry``)."""
    registry.register("search_signals", search_signals_tool)
    registry.register("query_facts", query_facts_tool)
    registry.register("inspect_entity", inspect_entity_tool)
    registry.register("vector_search", vector_search_tool)
    # S5-T4 — RAG over the curated reference corpora.
    registry.register("search_context", search_context_tool)
    # Stage 1 — OpenSearch full-text corpus readers.
    registry.register("search_corpus", search_corpus_tool)
    registry.register("read_document", read_document_tool)
    registry.register("query_nexuses", query_nexuses_tool)
    registry.register("query_hypotheses", query_hypotheses_tool)
    registry.register("get_timeline", get_timeline_tool)
    registry.register("compare_targets", compare_targets_tool)
    # P5 / #99 — recursive-CTE graph traversal read tools.
    registry.register("query_paths", query_paths_tool)
    registry.register("find_proxy_chains", find_proxy_chains_tool)
    registry.register("query_brokers", query_brokers_tool)
    # Palette expansion — finished-intelligence reads.
    registry.register("list_findings", list_findings_tool)
    registry.register("list_situations", list_situations_tool)
    registry.register("query_predictions", query_predictions_tool)
    # V3/P3 — the decision-time register.
    registry.register("belief_as_of", belief_as_of_tool)
    # V3/P6 — the event surface readers.
    registry.register("query_events", query_events_tool)
    registry.register("inspect_event", inspect_event_tool)
    # Navigation readers (consult-surface only; still pack-governed).
    registry.register("list_targets", list_targets_tool)
    registry.register("list_sources", list_sources_tool)
    # W11-F141 — the scheduled-events calendar.
    registry.register("list_scheduled_events", list_scheduled_events_tool)
    registry.register("list_due_resolvers", list_due_resolvers_tool)
    registry.register("get_source_health", get_source_health_tool)
    # 7g-2 — the collection series reads.
    registry.register("series_history", series_history_tool)
    registry.register("series_compare", series_compare_tool)
    # E2 — the live series reader (a separate tool from the history reads).
    registry.register("live_series", live_series_tool)


__all__ = [
    "SUBSTRATE_READ_PACK_ID",
    "SUBSTRATE_READ_TOOLS",
    "register_substrate_read_tools",
    "search_signals_tool",
    "query_facts_tool",
    "inspect_entity_tool",
    "vector_search_tool",
    "search_context_tool",
    "search_corpus_tool",
    "read_document_tool",
    "query_nexuses_tool",
    "query_hypotheses_tool",
    "get_timeline_tool",
    "compare_targets_tool",
    "query_paths_tool",
    "find_proxy_chains_tool",
    "query_brokers_tool",
    "list_findings_tool",
    "list_situations_tool",
    "query_predictions_tool",
    "belief_as_of_tool",
    "query_events_tool",
    "inspect_event_tool",
    "list_targets_tool",
    "list_sources_tool",
    "list_scheduled_events_tool",
    "list_due_resolvers_tool",
    "get_source_health_tool",
    "series_history_tool",
    "series_compare_tool",
    "live_series_tool",
]
