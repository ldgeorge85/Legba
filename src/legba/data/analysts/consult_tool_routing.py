# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Consult tool ROUTING — the whitelist, the round normalizer and one call's
dispatch, extracted from :mod:`.consult_on_demand` (C-1b, under the
module-size ratchet: that module sat at 2,280 of its 2,310 ceiling).

Extracted VERBATIM and behaviour-free: :mod:`.consult_on_demand` imports
every name back ONE WAY and re-exports it, so ``from .consult_on_demand import
_KNOWN_TOOLS, _dispatch_tool, _normalize_calls, _run_one_call`` (the deep
consult workflow, the inline assessors, the tests) is unchanged, and the
log lines keep the ``consult_on_demand`` logger they always used.

This leaf imports nothing from :mod:`.consult_on_demand` (the deps bundle is a
type-only reference), so the dependency runs one way.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any, Mapping

from .. import raw_reader_known_at as _known_at_cuts
from .consult_substrate_port import SubstrateQueryPort

if TYPE_CHECKING:  # pragma: no cover — type-only; the import would be a cycle
    from .consult_on_demand import ConsultOnDemandDeps

#: The SAME logger the loop uses: these lines were ``consult_on_demand.*`` log
#: records before the extraction and an alert or a caplog filter keyed on the
#: logger name must not notice the move.
logger = logging.getLogger("legba.data.analysts.consult_on_demand")

#: Max tools a planner may request in ONE batch round (the ``{"tools": [...]}``
#: shape). Extra calls past this are dropped and duplicate ``(tool, args)`` pairs
#: deduped, so a runaway planner can't open an unbounded number of concurrent
#: substrate calls in a single round.
MAX_TOOLS_PER_BATCH = 5


_KNOWN_TOOLS = {
    "search_signals",
    "query_facts",
    "inspect_entity",
    "vector_search",
    "search_context",
    "search_corpus",
    "read_document",
    "query_nexuses",
    "query_hypotheses",
    "get_timeline",
    "compare_targets",
    "query_paths",
    "find_proxy_chains",
    "query_brokers",
    "list_findings",
    "list_situations",
    # query_predictions is deliberately NOT offered to the consult: its feed has
    # been frozen since 2026-07-01 (W11-F150). The tool stays on the port and in
    # the desk packs; only this palette stopped listing it.
    "list_targets",
    "list_sources",
    # W11-F141 — the scheduled-events calendar (migration 0240).
    "list_scheduled_events",
    # W11-F162 — the forecast ledger's due-date reader (what resolves next).
    "list_due_resolvers",
    # W11-F152 — the honest liveness read: per-source `staleness`
    # (fresh|quiet|stale|dead against the feed's own cadence), last-poll
    # outcome and newest upstream entry. list_sources is the thin version; a
    # consult that had only it read "empty" polls as a dead fleet.
    "get_source_health",
    # V3/P3 — the decision-time register (spec §3.4).
    "belief_as_of",
    # V3/P6 — the event surface (spec §6.1).
    "query_events",
    "inspect_event",
    # 7g-2 — the COLLECTION series reads. Consult is the first reader the
    # collections firewall opts IN (`firewall.readers_opt_in`), and these are
    # the tools that make "compare the last ten years" a question with
    # citable numbers behind it instead of model knowledge.
    "series_history",
    "series_compare",
    # E2 — the LIVE numeric series (origin_class='live'), a separate reader:
    # the history tools above stay history-only (SEAMS #57/#62).
    "live_series",
}


def _normalize_calls(
    parsed: Mapping[str, Any], *, cap: int = MAX_TOOLS_PER_BATCH,
) -> list[dict[str, Any]]:
    """Normalize a planner round into a list of ``{tool, args}`` calls.

    ``cap`` is the per-round width. It defaults to :data:`MAX_TOOLS_PER_BATCH`
    so every existing caller is unchanged; the native route passes the tighter
    :func:`consult_round_protocol.native_batch_cap` instead, because on that
    route a round's results are replayed into every LATER prompt — width there
    is not a one-round cost, it is a multiplier on the rest of the run.

    Accepts BOTH the single shape ``{"tool": name, "args": {...}}`` and the
    batch shape ``{"tools": [{"tool": ..., "args": {...}}, ...]}`` — so the
    parser is backward-compatible with every single-tool planner. Malformed
    entries (no tool name, non-mapping args) are dropped; duplicate
    ``(tool, args)`` pairs are deduped; the list is capped at
    :data:`MAX_TOOLS_PER_BATCH` so a runaway planner can't fan out unbounded.
    Returns ``[]`` when neither a ``tool`` nor a ``tools`` list is present (the
    caller treats that as "neither tool nor final" and asks for a correction).
    """
    tools = parsed.get("tools")
    if isinstance(tools, list):
        raw_calls: list[Any] = tools
    elif parsed.get("tool"):
        raw_calls = [parsed]
    else:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rc in raw_calls:
        if not isinstance(rc, Mapping):
            continue
        name = str(rc.get("tool") or "")
        if not name:
            continue
        args = rc.get("args") or {}
        if not isinstance(args, Mapping):
            args = {}
        dedupe_key = name + "::" + json.dumps(dict(args), sort_keys=True, default=str)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        out.append({"tool": name, "args": dict(args)})
        if len(out) >= max(1, cap):
            break
    return out


def _findings_paging_kw(args: Mapping[str, Any]) -> dict[str, Any]:
    """W11-F150 — ``list_findings``' survey mode and paging kwargs, only when
    set, so a plain read is unchanged. Mirrors
    ``agency.substrate_read._findings_paging_kw`` (inlined: this module is a
    leaf and never imports from ``agency``). A survey with no ``limit`` takes
    the port's survey default (a page of targets), not the 20-row page one.
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


def _series_subjects(args: Mapping[str, Any]) -> list[str]:
    """``series_compare``'s subject list. Mirrors
    ``agency.substrate_read._subjects`` (inlined: this module is a leaf and
    never imports from ``agency``): a list, or one comma/space-separated
    string; anything else is ``[]``, which the port refuses by name rather
    than widening to every subject.
    """
    raw = args.get("subjects")
    if isinstance(raw, str):
        raw = [p for p in re.split(r"[,\s]+", raw) if p]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


async def _dispatch_tool(
    port: SubstrateQueryPort,
    *,
    name: str,
    args: Mapping[str, Any],
    scope_predicate: str | None,
    known_at: Any = None,
) -> dict[str, Any]:
    """Invoke a whitelisted tool by name.

    Unknown tool names are surfaced as a structured error so the planner
    sees the failure and can recover (rather than crashing the run).

    ``known_at`` (lane E0 — the evaluation harness's record-time cut; no
    product caller passes it): a tool not classified in
    :mod:`legba.data.raw_reader_known_at` is REFUSED; a ``cut`` tool gets
    ``known_at`` forwarded; an ``exempt`` one runs as is. It is never read from
    the planner's ``args`` — the model cannot widen or drop the cut.
    """
    if name not in _KNOWN_TOOLS:
        return {"error": f"unknown_tool: {name!r}"}
    cut: dict[str, Any] = {}
    if known_at is not None:
        if name not in _known_at_cuts.KNOWN_AT_CUTS:
            return {"error": (
                f"no_known_at_cut: {name!r} has no classified record-time cut "
                "(legba.data.raw_reader_known_at); refused under known_at"
            )}
        if name in _known_at_cuts.cut_tools():
            cut = {"known_at": known_at}
    try:
        if name == "search_signals":
            return await port.search_signals(
                query=str(args.get("query", "")),
                limit=int(args.get("limit", 20)),
                scope_predicate=scope_predicate,
                **cut,
            )
        if name == "query_facts":
            return await port.query_facts(
                subject=args.get("subject"),
                predicate=args.get("predicate"),
                value=args.get("value"),
                limit=int(args.get("limit", 30)),
                as_of=args.get("as_of"),
                **cut,
            )
        if name == "inspect_entity":
            return await port.inspect_entity(name=str(args.get("name", "")), **cut)
        if name == "vector_search":
            return await port.vector_search(
                query=str(args.get("query", "")),
                limit=int(args.get("limit", 10)),
                **cut,
            )
        if name == "search_context":
            return await port.search_context(
                query=str(args.get("query", "")),
                corpus=args.get("corpus"),
                country=args.get("country"),
                k=int(args.get("k", 6)),
            )
        if name == "search_corpus":
            return await port.search_corpus(
                query=str(args.get("query", "")),
                filters=args.get("filters"),
                size=int(args.get("size", 10)),
                **cut,
            )
        if name == "read_document":
            return await port.read_document(
                doc_id=str(args.get("doc_id", "")),
                **cut,
            )
        if name == "query_nexuses":
            polarity = args.get("polarity")
            return await port.query_nexuses(
                subject=args.get("subject"),
                obj=args.get("object"),
                rel_type=args.get("rel_type"),
                polarity=int(polarity) if polarity is not None else None,
                limit=int(args.get("limit", 30)),
                as_of=args.get("as_of"),
            )
        if name == "query_hypotheses":
            return await port.query_hypotheses(
                target_id=args.get("target_id"),
                status=args.get("status"),
                situation_id=args.get("situation_id"),
                limit=int(args.get("limit", 30)),
            )
        if name == "get_timeline":
            return await port.get_timeline(
                subject=str(args.get("subject", "")),
                limit=int(args.get("limit", 40)),
                since=args.get("since"),
                until=args.get("until"),
            )
        if name == "compare_targets":
            raw_targets = args.get("target_ids") or []
            target_ids = (
                [str(t) for t in raw_targets]
                if isinstance(raw_targets, list)
                else []
            )
            return await port.compare_targets(target_ids=target_ids)
        if name == "query_paths":
            pp = args.get("polarity_product")
            return await port.query_paths(
                subject=str(args.get("subject", "")),
                obj=str(args.get("object", "")),
                max_hops=int(args.get("max_hops", 3)),
                polarity_product=int(pp) if pp is not None else None,
                limit=int(args.get("limit", 30)),
                families=(
                    [str(x) for x in args["families"]]
                    if isinstance(args.get("families"), list) else None
                ),
                as_of=args.get("as_of"),
            )
        if name == "find_proxy_chains":
            pp = args.get("polarity_product")
            return await port.find_proxy_chains(
                subject=str(args.get("subject", "")),
                obj=str(args.get("object", "")),
                max_hops=int(args.get("max_hops", 3)),
                polarity_product=int(pp) if pp is not None else None,
                limit=int(args.get("limit", 30)),
                families=(
                    [str(x) for x in args["families"]]
                    if isinstance(args.get("families"), list) else None
                ),
                as_of=args.get("as_of"),
            )
        if name == "query_brokers":
            raw_a = args.get("camp_a") or []
            raw_b = args.get("camp_b") or []
            return await port.query_brokers(
                camp_a=[str(x) for x in raw_a] if isinstance(raw_a, list) else [],
                camp_b=[str(x) for x in raw_b] if isinstance(raw_b, list) else [],
                max_hops=int(args.get("max_hops", 3)),
                limit=int(args.get("limit", 50)),
                families=(
                    [str(x) for x in args["families"]]
                    if isinstance(args.get("families"), list) else None
                ),
                as_of=args.get("as_of"),
            )
        if name == "list_findings":
            return await port.list_findings(
                target_id=args.get("target_id"),
                analyst_id=args.get("analyst_id"),
                severity=args.get("severity"),
                since_hours=int(args["since_hours"])
                    if args.get("since_hours") is not None else None,
                # R1 / W2-T1: superseded findings excluded by default; opt in
                # for history/audit reads.
                include_superseded=str(
                    args.get("include_superseded", False)
                ).lower() in ("true", "1"),
                believed_as_of=args.get("believed_as_of"),
                **_findings_paging_kw(args),
                **cut,
            )
        if name == "list_situations":
            return await port.list_situations(
                status=args.get("status"),
                target_id=args.get("target_id"),
                since_hours=int(args["since_hours"])
                    if args.get("since_hours") is not None else None,
                limit=int(args.get("limit", 20)),
                as_of=args.get("as_of"),
            )
        if name == "belief_as_of":
            return await port.belief_as_of(
                as_of=str(args.get("as_of", "")),
                target_id=args.get("target_id"),
                fold_verdicts=str(args.get("fold_verdicts", "as_of")),
                limit=int(args.get("limit", 20)),
                **cut,
            )
        if name == "query_events":
            raw_geo = args.get("geo")
            return await port.query_events(
                target_id=args.get("target_id"),
                geo=(
                    [str(g) for g in raw_geo]
                    if isinstance(raw_geo, list)
                    else (str(raw_geo) if raw_geo is not None else None)
                ),
                category=args.get("category"),
                lifecycle_state=args.get("lifecycle_state"),
                entity=args.get("entity"),
                situation_id=args.get("situation_id"),
                since=args.get("since"),
                until=args.get("until"),
                as_of=args.get("as_of"),
                include_origin=(
                    [str(c) for c in args["include_origin"]]
                    if isinstance(args.get("include_origin"), list) else None
                ),
                limit=int(args.get("limit", 20)),
                **cut,
            )
        if name == "inspect_event":
            return await port.inspect_event(
                event_id=str(args.get("event_id", "")),
                **cut,
            )
        if name == "list_targets":
            return await port.list_targets(
                active_only=bool(args.get("active_only", True)),
            )
        if name == "list_sources":
            return await port.list_sources(
                active_only=bool(args.get("active_only", True)),
                silent_only=bool(args.get("silent_only", False)),
                silent_hours=int(args.get("silent_hours", 48)),
                **cut,
            )
        if name == "list_scheduled_events":
            raw_keys = args.get("subject_keys")
            return await port.list_scheduled_events(
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
        if name == "list_due_resolvers":
            return await port.list_due_resolvers(
                horizon_days=int(args.get("horizon_days", 14)),
                include_closed=bool(args.get("include_closed", False)),
                limit=int(args.get("limit", 50)),
            )
        if name == "get_source_health":
            return await port.get_source_health(
                silent_only=bool(args.get("silent_only", False)),
                silent_hours=int(args.get("silent_hours", 48)),
                limit=int(args.get("limit", 200)),
            )
        # R-0 — the 7g-2 series reads. They were whitelisted above but had no
        # branch here, so every call through this router (the harness binding
        # the bake-off drives, any hand-built deps) fell through to
        # ``unreachable`` (deviation D22: 6 of 6 calls failed). The governed
        # path always reached them through the substrate_read pack handler;
        # this mirrors that handler's arg coercion EXACTLY (``from``/``to`` or
        # ``since``/``until``; the same subjects rule), and a parity test
        # drives both paths with the same args and compares the port kwargs.
        # (E-0 added the same branches for D22; one implementation kept — R-0's,
        # which its parity test pins against the pack handler; E-0's known_at
        # refusal sits at the top of this function and applies to both.)
        if name == "series_history":
            return await port.series_history(
                series_id=str(args.get("series_id", "")),
                subject=str(args.get("subject", "")),
                since=args.get("from") or args.get("since"),
                until=args.get("to") or args.get("until"),
                as_of=args.get("as_of"),
                collection_id=args.get("collection_id"),
                limit=int(args.get("limit", 500)),
                **cut,
            )
        if name == "series_compare":
            return await port.series_compare(
                series_id=str(args.get("series_id", "")),
                subjects=_series_subjects(args),
                since=args.get("from") or args.get("since"),
                until=args.get("to") or args.get("until"),
                as_of=args.get("as_of"),
                collection_id=args.get("collection_id"),
                limit=int(args.get("limit", 500)),
                **cut,
            )
        # E2 — the LIVE series reader (a separate tool from the history
        # reads above, which never select a live row). Same coercion as
        # ``agency.substrate_read._call_port``.
        if name == "live_series":
            return await port.live_series(
                series_id=args.get("series_id"),
                subject=args.get("subject"),
                source_id=args.get("source_id"),
                since=args.get("from") or args.get("since"),
                until=args.get("to") or args.get("until"),
                limit=int(args.get("limit", 200)),
                **cut,
            )
    except Exception as exc:                                # noqa: BLE001
        # Defensive: tool implementations may hit Postgres / Qdrant in
        # ways that raise. Surface the error to the planner rather than
        # bubbling out — the loop has its own cap, so the run terminates.
        logger.warning(
            "consult_on_demand.tool.error tool=%s err=%s",
            name, exc,
        )
        return {"error": f"tool_failed: {exc!s}"}
    # Unreachable — _KNOWN_TOOLS membership was checked above.
    return {"error": "unreachable"}                          # pragma: no cover


async def _run_one_call(
    deps: ConsultOnDemandDeps,
    *,
    tool_name: str,
    tool_args: Mapping[str, Any],
    scope_predicate: str | None,
    analyst_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Execute ONE tool call through the governed binding (or, for tests /
    embedders with no binding, the direct port dispatcher).

    Returns ``(tool_result, meta)`` where ``meta`` carries
    ``{"governed": bool, "admitted": bool | None}`` for the trace. This NEVER
    raises — any failure (including an unexpected binding error) is folded into
    ``tool_result`` as ``{"error": ...}`` so one call in a concurrent batch can
    fail without aborting the whole round (and a single call behaves exactly as
    the historical dispatcher did: error mapped into the conversation).
    """
    if deps.agency_binding is not None:
        # Governed path (A-3a): the binding shapes the ToolCall and runs the
        # full hard-gate pipeline (resolve ∩ allow ∩ applicability, the pack
        # governor, the ledger). scope_predicate is INJECTED here (caller-
        # pinned) so the planner cannot override an operator scope.
        try:
            outcome = await deps.agency_binding.run_tool(
                tool_name,
                {**dict(tool_args), "scope_predicate": scope_predicate},
            )
        except Exception as exc:  # noqa: BLE001 — one call's failure ≠ round failure
            logger.warning(
                "consult_on_demand.tool.governed_error tool=%s err=%s",
                tool_name, exc,
            )
            return {"error": f"tool_failed: {exc!s}"}, {
                "governed": True, "admitted": False,
            }
        if not outcome.admitted:
            return (
                {"error": f"tool_blocked: {outcome.block_cause}: {outcome.detail}"},
                {"governed": True, "admitted": False},
            )
        if outcome.tool_result is None or outcome.tool_result.status == "failed":
            err = (
                outcome.tool_result.error
                if outcome.tool_result is not None
                else "tool produced no result"
            )
            return (
                {"error": f"tool_failed: {err}"},
                {"governed": True, "admitted": True},
            )
        return dict(outcome.tool_result.output), {"governed": True, "admitted": True}

    # UNGOVERNED direct-port dispatch. Reachable only from hand-constructed deps
    # (tests / non-runtime embedders) — the production deps resolver ALWAYS binds
    # the substrate_read pack and fail-closes when it can't
    # (dapr_host._analyst_deps_resolver). Log at WARNING so this can never be a
    # *silent* bypass if it ever appears on a production path.
    logger.warning(
        "consult_on_demand.tool.UNGOVERNED analyst_id=%s tool=%s — agency_binding "
        "not wired; dispatching direct at the substrate port (expected only for "
        "tests/embedders, never the runtime)",
        analyst_id, tool_name,
    )
    tool_result = await _dispatch_tool(
        deps.substrate,
        name=tool_name,
        args=tool_args,
        scope_predicate=scope_predicate,
    )
    return tool_result, {"governed": False, "admitted": None}


# ---------------------------------------------------------------------------
# One round's calls: reads concurrently, then PROVIDER tools serially (C-1b,
# generalised by the C-2a §7 hook)
# ---------------------------------------------------------------------------


async def run_round(
    deps: ConsultOnDemandDeps,
    calls: list[dict[str, Any]],
    *,
    scope_predicate: str | None,
    analyst_id: str | None,
    round_no: int,
    gates: Mapping[str, Any] | None = None,
    case: Any | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Execute one round's calls; results come back POSITIONALLY.

    The built-in reads run concurrently, exactly as the loop always ran them (a
    round with no provider call is the old ``asyncio.gather`` over the same
    calls in the same order). Every PROVIDER tool (``consult_tool_providers``:
    the ``workstation_drive`` actions, the ``consult_write`` writes) runs AFTER
    the reads, SERIALLY, in the order the model emitted it, through its
    provider's run-scoped gate (``gates[pack_id]``) — a case is an ordered
    walk and a write that lands out of order leaves a lineage that does not
    match the transcript. A provider tool on a turn that did not offer its
    family (no gate) gets the provider's own refusal (``drive_not_armed``,
    ``writes_not_armed``). ``case`` is C-1b's spelling of the drive gate.
    """
    import asyncio

    from .consult_tool_providers import binding_for, provider_for_tool

    gates = dict(gates or {})
    if case is not None:
        from ..workstation_vocabulary import WORKSTATION_DRIVE_PACK_ID

        gates.setdefault(WORKSTATION_DRIVE_PACK_ID, case)
    owners = [provider_for_tool(str(call["tool"])) for call in calls]
    results: list[Any] = [None] * len(calls)
    reads = [i for i, owner in enumerate(owners) if owner is None]
    gathered = await asyncio.gather(*[
        _run_one_call(
            deps,
            tool_name=calls[i]["tool"],
            tool_args=calls[i]["args"],
            scope_predicate=scope_predicate,
            analyst_id=analyst_id,
        )
        for i in reads
    ])
    for i, outcome in zip(reads, gathered):
        results[i] = outcome
    for i, call in enumerate(calls):
        owner = owners[i]
        if owner is None:
            continue
        gate = gates.get(owner.pack_id)
        if gate is None:
            results[i] = await owner.refuse(binding_for(deps, owner.pack_id), call["tool"])
        else:
            results[i] = await gate.run(call["tool"], call["args"], round_no)
    return results
