# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native tool-call rounds — the provider-shaped protocol both loops share.

WHY THIS MODULE EXISTS
----------------------
Legba has two multi-round tool loops (the agency GATHER loop in
``data/analysts/inline_target.py`` and the consult ReAct loop in
``data/analysts/consult_on_demand.py``) and both spoke the same JSON-IN-PROSE
protocol: the system prompt asked for ``{"tool": ..., "args": {...}}``, the
model wrote that object into its ordinary text completion, and the loop
``json.loads``-ed it back out. That protocol failed live, repeatedly and in
three distinct ways, all of them recorded:

  * **Wrong envelope** — the model emitted ``{"action": …, "query": …}`` and
    the loop read ``parsed["tool"]``, saw ``None``, and folded the turn back as
    ``unrecognized`` (2026-09-07/08; patched in ``planner_action``).
  * **Narration instead of action** — the model described the call it was about
    to make and that sentence was published as the finding (2026-09-08).
  * **Silent multi-call loss** — the live ``corpus_researcher`` turn of
    2026-09-15 03:37Z emitted FIVE tool objects plus narration in one
    completion. The text reader takes the first object it can parse; the other
    four calls were dropped on the floor with nothing in the trace to say so.

None of these are model stupidity. They are what happens when a structured
protocol is carried in an unstructured channel. The providers both planes run
on have a STRUCTURED channel for exactly this, and Legba was not using it:
of 1,552 core-plane calls in the 24 h to 2026-09-16, ZERO carried a tool call.
The core plane (``gpt-oss-120b``, served with
``--enable-auto-tool-choice --tool-call-parser openai``) was probed through the
production gateway that day and answers correctly on all four modes: ``auto`` →
``finish_reason=tool_calls`` with correct args, ``required`` → tool_calls,
``forced`` → **tool_calls present with ``finish_reason=stop``**, and a
tool-result round trip consumed and answered.

THE ONE RULE THAT PROBE BOUGHT
------------------------------
A forced call came back with ``finish_reason=stop``. So a parser that keys on
``finish_reason == "tool_calls"`` misses real tool calls. :func:`parse_tool_calls`
keys ONLY on the structured payload — ``message.tool_calls`` for the
OpenAI-compatible route, ``tool_use`` content blocks for Anthropic — and never
reads ``finish_reason`` at all. That is deliberate and it is tested.

MULTI-CALL BATCHES ARE FIRST CLASS
----------------------------------
A model that wants three reads in one turn should get three reads in one turn.
:func:`parse_tool_calls` returns every call in the assistant turn, in order;
:func:`tool_result_messages` returns one result per call, in the SAME order,
correlated by the provider's own id. The caller executes them (concurrently for
read-only tools, serially for write tools — see :func:`is_write_tool`) and the
whole batch folds back as ONE round. The 2026-09-15 turn above becomes three
executed reads instead of one executed and two lost.

REPLAY IS VERBATIM AND ID-STABLE
--------------------------------
:func:`assistant_tool_turn` rebuilds the assistant turn that carried the calls
from NORMALIZED fields only (text + the calls' own ids/names/args) — never by
copying the provider's raw message. That is not fastidiousness: the core plane
populates ``reasoning_content`` on every reply, and copying the raw message
would carry the model's private chain-of-thought into the next request AND into
``analyst_traces.prompt_rendered`` (which is captured from the pre-translation
messages, ``base.py``'s ``record_prompt_rendered``). Reasoning content stays
out of persisted text, by construction. :func:`visible_text` is the one
accessor callers should use for "what the model said".

OWNERSHIP
---------
Shared by the ``agency-native-tool-rounds`` and ``consult-resilience`` lanes to
a fixed interface: :class:`ToolSpec` · :func:`build_tool_specs` ·
:func:`render_tools` · :class:`ToolCall` · :func:`parse_tool_calls` ·
:func:`tool_result_messages` · :func:`supports_native_tools`. It is a LEAF:
it imports nothing from ``legba.data.analysts`` and holds no policy about when
a loop should go native — that decision (and its env flag) belongs to the loop.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "ARG_BLOCK_CAUSES",
    "CAUSE_INVALID_ARG",
    "CAUSE_RESULT_SHAPED_ARG",
    "INJECTED_ARG_KEYS",
    "PROVIDER_ANTHROPIC",
    "PROVIDER_OPENAI_COMPAT",
    "RESULT_SHAPED_ARG_KEYS",
    "TOOL_SCHEMAS",
    "ToolCall",
    "ToolSpec",
    "WRITE_TOOLS",
    "assistant_tool_turn",
    "build_tool_specs",
    "is_write_tool",
    "parse_tool_calls",
    "provider_for_subprovider",
    "register_tool_schema",
    "render_tools",
    "split_arg_problem",
    "supports_native_tools",
    "tool_result_messages",
    "validate_args",
    "visible_text",
]


# ---------------------------------------------------------------------------
# Provider routes
# ---------------------------------------------------------------------------

#: The OpenAI Chat-Completions tool grammar — ``tools=[{type: function, ...}]``
#: on the way out, ``message.tool_calls`` on the way back. Spoken by the
#: ``openai`` and ``vllm`` handlers alike (vLLM's OpenAI-compat surface).
PROVIDER_OPENAI_COMPAT = "openai_compat"

#: The Anthropic Messages tool grammar — ``tools=[{name, description,
#: input_schema}]`` out, ``tool_use`` content blocks back.
PROVIDER_ANTHROPIC = "anthropic"

#: ``LLMProviderHandler.subprovider`` → the tool grammar it speaks. A
#: subprovider absent from this map has no PROVEN native tool surface and is
#: routed to the text fallback; adding one here is the whole opt-in.
_SUBPROVIDER_ROUTE: dict[str, str] = {
    "openai": PROVIDER_OPENAI_COMPAT,
    "vllm": PROVIDER_OPENAI_COMPAT,
    "anthropic": PROVIDER_ANTHROPIC,
}

#: Operator escape hatch: a comma-separated list of component ids (or bare
#: subprovider names) that must NEVER be sent native tool specs, even when the
#: caller's own flag is on. For pinning a single misbehaving endpoint without
#: turning the protocol off fleet-wide. Read at call time, never at import.
NATIVE_TOOLS_DENY_ENV = "LEGBA_NATIVE_TOOLS_DENY"


def provider_for_subprovider(subprovider: Any) -> str | None:
    """Map a handler's ``subprovider`` to its tool grammar, or ``None``.

    ``None`` means "no known native tool surface" — the caller keeps the
    JSON-text protocol. Accepts the handler itself as a convenience (anything
    carrying a ``subprovider`` attribute), because call sites hold the handler,
    not the string.
    """
    if not isinstance(subprovider, str):
        subprovider = getattr(subprovider, "subprovider", None)
    if not isinstance(subprovider, str):
        return None
    return _SUBPROVIDER_ROUTE.get(subprovider.strip().lower())


def _deny_list() -> frozenset[str]:
    raw = os.getenv(NATIVE_TOOLS_DENY_ENV, "")
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def supports_native_tools(provider: Any, component: str | None = None) -> bool:
    """Can ``provider`` carry a native tool round for ``component``?

    ``provider`` is either a grammar constant (:data:`PROVIDER_OPENAI_COMPAT` /
    :data:`PROVIDER_ANTHROPIC`), a raw ``subprovider`` string, or a handler.
    ``component`` is the stack-component id when the caller knows it — used
    only by the deny list, never to widen support.
    """
    if not isinstance(provider, str):
        provider = getattr(provider, "subprovider", None)
    if not isinstance(provider, str):
        return False
    name = provider.strip().lower()
    grammar = name if name in (PROVIDER_OPENAI_COMPAT, PROVIDER_ANTHROPIC) else (
        _SUBPROVIDER_ROUTE.get(name)
    )
    if grammar is None:
        return False
    deny = _deny_list()
    if deny and (name in deny or (component and component in deny)):
        logger.info(
            "tool_rounds.native_denied provider=%s component=%s env=%s",
            name, component, NATIVE_TOOLS_DENY_ENV,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Tool specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """One tool offered to the model, in provider-neutral form."""

    name: str
    description: str
    json_schema: Mapping[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """One call the model emitted, normalized across providers.

    ``raw`` keeps the provider's own payload for the trace/debug path; it is
    NEVER sent back on the wire (see :func:`assistant_tool_turn`).
    """

    id: str
    name: str
    args: dict[str, Any]
    raw: Any = field(default=None, repr=False)


def _obj(
    properties: Mapping[str, Any], required: Sequence[str] = (),
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": dict(properties),
    }
    if required:
        schema["required"] = list(required)
    return schema


_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_STRS = {"type": "array", "items": {"type": "string"}}


def _s(name: str, description: str, properties, required=()) -> ToolSpec:
    return ToolSpec(name=name, description=description,
                    json_schema=_obj(properties, required))


#: The argument grammar of every tool the two loops can bind, authored from the
#: handlers' own ``call.args`` reads (``agency/substrate_read.py``,
#: ``web_tools.py``, ``write_tools.py``, ``research_tools.py``,
#: ``journal_read.py``, ``journal_propose.py``). The descriptions are the ONE
#: place the tool signature is stated for the native channel — the text
#: protocol states the same signatures in prose in ``inline_target``'s GATHER
#: system suffix, and the two must stay in step.
#:
#: An unknown tool name is NOT an error: :func:`build_tool_specs` synthesizes a
#: permissive free-form spec, so a tool added to a pack without a schema here
#: is still callable (loosely typed) rather than silently invisible.
TOOL_SCHEMAS: dict[str, ToolSpec] = {
    # ---- substrate_read ---------------------------------------------------
    "search_signals": _s(
        "search_signals",
        "Full-text search over signal titles and summaries. Returns rows with "
        "signal ids; the rows carry NO body, so never cite one with [N].",
        {"query": _STR, "limit": _INT}, ("query",),
    ),
    "search_corpus": _s(
        "search_corpus",
        "BM25 keyword search over the FULL raw body of every ingested signal "
        "(the whole corpus, not this run's slice). Use it to FIND source "
        "documents; a row's id is the signal id.",
        {"query": _STR, "filters": {"type": "object"}, "size": _INT}, ("query",),
    ),
    "read_document": _s(
        "read_document",
        "The FULL stored body of ONE signal by its id (a search_corpus / "
        "search_signals row id), or of one finding by its list_findings id.",
        {"doc_id": _STR}, ("doc_id",),
    ),
    "query_facts": _s(
        "query_facts",
        "Query the fact store by subject / predicate / value. as_of (ISO-8601) "
        "reads the facts that held on that date.",
        {"subject": _STR, "predicate": _STR, "value": _STR, "limit": _INT,
         "as_of": _STR},
    ),
    "inspect_entity": _s(
        "inspect_entity", "Entity profile plus that entity's recent facts.",
        {"name": _STR}, ("name",),
    ),
    "vector_search": _s(
        "vector_search", "Semantic (embedding) search over the signal corpus.",
        {"query": _STR, "limit": _INT}, ("query",),
    ),
    "search_context": _s(
        "search_context",
        "RAG over the curated reference corpora (country/topic priors, "
        "doctrine, tradecraft). BACKGROUND knowledge, not live substrate.",
        {"query": _STR, "corpus": _STR, "country": _STR, "k": _INT}, ("query",),
    ),
    "query_nexuses": _s(
        "query_nexuses",
        "Open signed/typed relationships between entities. as_of (ISO-8601) "
        "reads the relationships that held on that date.",
        {"subject": _STR, "object": _STR, "rel_type": _STR,
         "polarity": _INT, "limit": _INT, "as_of": _STR},
    ),
    "query_hypotheses": _s(
        "query_hypotheses",
        "Competing-hypothesis (ACH) rows, optionally scoped to a target or a "
        "situation.",
        {"target_id": _STR, "status": _STR, "situation_id": _STR, "limit": _INT},
    ),
    "get_timeline": _s(
        "get_timeline",
        "Time-ordered facts union signals for one subject. since/until "
        "(ISO-8601) bound the window on each item's anchor.",
        {"subject": _STR, "limit": _INT, "since": _STR, "until": _STR},
        ("subject",),
    ),
    "compare_targets": _s(
        "compare_targets", "Side-by-side substrate rollup across targets.",
        {"target_ids": _STRS}, ("target_ids",),
    ),
    # V3/P3 — the three graph walks and the two navigation readers had NO
    # schema before: an unknown name fell through to the permissive fallback
    # spec, which `is_write_tool` counted as a WRITE (serializing three
    # read-only walks and two readers behind every write tool call).
    "query_paths": _s(
        "query_paths",
        "Ranked SIGNED paths A->...->B over the open entity graph; each path "
        "carries its net polarity_product. as_of (ISO-8601) walks the graph "
        "as it stood on that date.",
        {"subject": _STR, "object": _STR, "max_hops": _INT,
         "polarity_product": _INT, "limit": _INT, "families": _STRS,
         "edge_families": _STRS, "as_of": _STR},
        ("subject", "object"),
    ),
    "find_proxy_chains": _s(
        "find_proxy_chains",
        "INDIRECT links only between two entities — multi-hop chains plus "
        "reified A->via->B cut-outs. The proxy path from A to B.",
        {"subject": _STR, "object": _STR, "max_hops": _INT,
         "polarity_product": _INT, "limit": _INT, "families": _STRS,
         "edge_families": _STRS, "as_of": _STR},
        ("subject", "object"),
    ),
    "query_brokers": _s(
        "query_brokers",
        "Entities that SIT ON paths between two entity sets (the broker "
        "between two camps), ranked by how many A->B paths run through them.",
        {"camp_a": _STRS, "camp_b": _STRS, "max_hops": _INT, "limit": _INT,
         "families": _STRS, "edge_families": _STRS, "as_of": _STR},
        ("camp_a", "camp_b"),
    ),
    "list_findings": _s(
        "list_findings",
        "The platform's OWN prior assessments/findings. Check these FIRST to "
        "build on and reconcile against earlier work; cite the output_id. "
        "Default mode 'page': the newest live findings with full bodies, "
        "newest first (since_hours is only a floor, so widening it returns "
        "the same first page). mode='survey': one row per target, its newest "
        "live read, digest only (about 240 characters), ordered by "
        "target_id; use it to see EVERY target at once, then drill by "
        "target_id (or read_document on a finding id) for the body. Both modes "
        "page: rows_total is the true count; pass offset=next_offset for more.",
        {"target_id": _STR, "target_ids": _STRS, "analyst_id": _STR,
         "severity": _STR, "since_hours": _INT, "include_superseded": _BOOL,
         "believed_as_of": _STR, "limit": _INT, "offset": _INT,
         "mode": {"type": "string", "enum": ["page", "survey"]}},
    ),
    "list_situations": _s(
        "list_situations",
        "Ongoing situation frames the platform has clustered, newest and "
        "hottest first. Use a situation_id with query_hypotheses to pull its "
        "ACH rows. as_of (ISO-8601) reads the frames that held on that date.",
        # ``status`` is an ENUM, not free text. It was declared a bare string,
        # and on 2026-09-28 a core-plane run passed status="ok" — the reply
        # envelope it had invented — which ran as a SQL predicate no row can
        # match and came back count 0. The vocabulary is the clustering
        # handler's own (``situation_clustering._situation_status``) and is
        # kept in step with it by ``runtime.substrate_frame_reads``, which
        # holds :data:`SITUATION_STATUSES` and names the same three values
        # when a read comes back empty on a status outside them.
        {"status": {"type": "string",
                    "enum": ["active", "dormant", "closed"]},
         "target_id": _STR, "since_hours": _INT,
         "as_of": _STR, "limit": _INT},
    ),
    "query_predictions": _s(
        "query_predictions",
        "The platform's event-volume forecasts. The feed is FROZEN — rows are "
        "historical; never present one as a current forecast.",
        {"target_id": _STR, "status": _STR, "limit": _INT},
    ),
    # V3/P3 — the decision-time register (spec §3.4). Read-only.
    "belief_as_of": _s(
        "belief_as_of",
        "The findings Legba had published and not yet superseded on date "
        "as_of (ISO-8601, required), each with its own effective_confidence "
        "under the named verdict fold ('as_of' or 'latest'). No pooled score.",
        {"as_of": _STR, "target_id": _STR, "fold_verdicts": _STR,
         "limit": _INT},
        ("as_of",),
    ),
    # V3/P6 — the event surface (spec §6.1). Read-only.
    "query_events": _s(
        "query_events",
        "Bounded real-world occurrences the platform has clustered "
        "(lifecycle_state: emerging/developing/active/evolving/resolved). "
        "geo filters by ISO2 code(s); entity by actor name; since/until "
        "(ISO-8601) bound the event's occurrence span by overlap; as_of "
        "reads the events that held on that date.",
        # ``lifecycle_state`` is an ENUM for the same reason ``list_situations
        # .status`` is: an invented value ran as a SQL predicate no row can
        # match. The five states are the events CHECK vocabulary
        # (``legba.data.events.lifecycle.LIFECYCLE_STATES``, migration 0204),
        # held in order by ``runtime.substrate_frame_reads
        # .EVENT_LIFECYCLE_STATES``; this module is a LEAF and cannot import
        # either, so ``tests/data_pkg/test_native_tool_rounds.py`` pins them.
        {"target_id": _STR, "geo": {"oneOf": [_STR, _STRS]},
         "category": _STR,
         "lifecycle_state": {"type": "string",
                             "enum": ["emerging", "developing", "active",
                                      "evolving", "resolved"]},
         "entity": _STR,
         "situation_id": _STR, "since": _STR, "until": _STR,
         "as_of": _STR, "include_origin": _STRS, "limit": _INT},
    ),
    "inspect_event": _s(
        "inspect_event",
        "The one-event dossier: the event row, its ranked evidence signals, "
        "its actors with roles, its event edges, the situations tracking "
        "it, and its lifecycle ledger oldest→newest.",
        {"event_id": _STR},
        ("event_id",),
    ),
    "list_targets": _s(
        "list_targets",
        "The monitored targets + their ids; resolve a place/topic to a valid "
        "target_id before calling target-scoped readers.",
        {"active_only": _BOOL},
    ),
    "list_sources": _s(
        "list_sources",
        "Ingest sources + freshness (freshest first, whole-fleet summary "
        "first); tell 'no coverage on X' apart from 'a quiet feed'. "
        "last_poll_outcome 'empty' = the latest poll found no NEW items, not "
        "dead; liveness is each row's `staleness`.",
        {"active_only": _BOOL, "silent_only": _BOOL, "silent_hours": _INT},
    ),
    # W11-F162 — the forecast ledger's due-date reader.
    "list_due_resolvers": _s(
        "list_due_resolvers",
        "The sealed forecast ledger by due date: questions due (or overdue) "
        "within horizon_days (default 14), the resolver that decides each, "
        "every issuer's number, and the newest rule proposal with the human "
        "judgement it waits on. A proposal is a rule's reading, not an "
        "outcome; only a resolution is.",
        {"horizon_days": _INT, "include_closed": _BOOL, "limit": _INT},
    ),
    # W11-F141 — the scheduled-events calendar (migration 0240): the
    # operator-entered register of FUTURE dated things.
    "list_scheduled_events": _s(
        "list_scheduled_events",
        "The platform's scheduled-events calendar — known FUTURE dated "
        "things (summits, votes, deadlines, strikes), operator-entered. "
        "window_start/window_end (ISO-8601) bound the window by overlap, "
        "default now → +14 days. The coverage block is part of the answer: "
        "n=0 means no events HELD for the window — never read it as "
        "'nothing is scheduled'.",
        {"window_start": _STR, "window_end": _STR, "scope": _STR,
         "subject_keys": _STRS, "limit": _INT},
    ),
    # ---- web_access -------------------------------------------------------
    "web_search": _s(
        "web_search",
        "Query the operator-pinned search endpoint; returns {title, url, "
        "snippet} results. Read-only — nothing is landed. `provider` pins one "
        "rung of the provider ladder by component id or subprovider name; "
        "omit it to take the ladder in order.",
        # ``provider`` is READ by the handler (``web_tools.py``'s
        # ``call.args.get("provider")``) and was never declared here. Under
        # ``validate_args`` an undeclared argument is now a block, so the
        # grammar has to state every key the handler honours or a working
        # pin becomes a refusal.
        {"query": _STR, "limit": _INT, "provider": _STR}, ("query",),
    ),
    "web_fetch": _s(
        "web_fetch",
        "GET one absolute http(s) URL through the SSRF-guarded transport; "
        "returns its capped text body.",
        {"url": _STR}, ("url",),
    ),
    # ---- research (WRITES) ------------------------------------------------
    "web_evidence": _s(
        "web_evidence",
        "Search the open web and LAND what comes back as permanent substrate "
        "evidence. `rows` are licence-cleared full-text hits (citable [N]); "
        "`teaser_hits` carry NO body (name them in prose, never cite them). "
        "Pass hypothesis_id when draining a standing question.",
        {"query": _STR, "limit": _INT, "fetch": _BOOL, "hypothesis_id": _STR},
        ("query",),
    ),
    # ---- propose_facts (WRITES) ------------------------------------------
    "propose_fact": _s(
        "propose_fact",
        "Write ONE proposed-grade fact. PROPOSES, does not assert truth; "
        "derived_from lineage citing substrate UUIDs is REQUIRED. "
        "valid_from/valid_until (ISO-8601, optional) bound the period the "
        "fact is ABOUT.",
        # valid_from / valid_until are read by ``write_tools.py`` and were
        # never declared — see the ``web_search`` note above.
        {"subject": _STR, "predicate": _STR, "value": _STR,
         "derived_from": _STRS, "confidence": {"type": "number"},
         "valid_from": _STR, "valid_until": _STR},
        ("subject", "predicate", "value", "derived_from"),
    ),
    "request_source": _s(
        "request_source", "Record a coverage / evidence gap.",
        {"need": _STR, "rationale": _STR, "derived_from": _STRS},
        ("need", "derived_from"),
    ),
    "open_question": _s(
        "open_question", "Record an unresolved analytical question.",
        {"question": _STR, "counter": _STR, "derived_from": _STRS},
        ("question", "derived_from"),
    ),
    # ---- journal_read -----------------------------------------------------
    "get_assessments": _s(
        "get_assessments", "Recent analyst assessments across the platform.",
        {"analyst_id": _STR, "target_id": _STR, "since_hours": _INT, "limit": _INT},
    ),
    "get_graph_structure": _s(
        "get_graph_structure", "The nexus graph's current structure.",
        {"limit": _INT},
    ),
    "get_structural_balance": _s(
        "get_structural_balance", "Signed-graph structural balance triads.",
        {"limit": _INT},
    ),
    "get_critic_scores": _s(
        "get_critic_scores", "Critic scores per analyst over a window.",
        {"analyst_id": _STR, "since_hours": _INT, "limit": _INT},
    ),
    "get_calibration": _s(
        "get_calibration", "Platform-wide forecast calibration.", {},
    ),
    "get_run_health": _s(
        "get_run_health", "Per-analyst run health and quiet analysts.",
        {"analyst_id": _STR, "quiet_hours": _INT, "limit": _INT},
    ),
    "get_source_health": _s(
        "get_source_health",
        "Per-source ingestion health with a staleness verdict "
        "(fresh/quiet/stale/dead vs the feed's cadence); silent sources.",
        {"silent_only": _BOOL, "silent_hours": _INT, "limit": _INT},
    ),
    "get_budget_status": _s(
        "get_budget_status", "Per-analyst budget and demotion status.",
        {"analyst_id": _STR, "demotion_lookback_hours": _INT, "limit": _INT},
    ),
    "get_journal_delta": _s(
        "get_journal_delta", "What changed in the journal since a timestamp.",
        {"since": _STR, "limit": _INT},
    ),
    "get_lens_reads": _s(
        "get_lens_reads", "Recent lens (faculty) reads.",
        {"since": _STR, "limit": _INT},
    ),
    # ---- journal_propose (WRITES) ----------------------------------------
    "propose_correction": _s(
        "propose_correction",
        "Propose a CORRECTION to the journal. Writes a PENDING proposal row "
        "only — never the journal itself.",
        {"rationale": _STR, "diff": {"type": "object"},
         "cited_substrate_refs": _STRS}, ("rationale", "diff"),
    ),
    "propose_change": _s(
        "propose_change",
        "Propose a CHANGE to the journal. Writes a PENDING proposal row only.",
        {"rationale": _STR, "diff": {"type": "object"},
         "cited_substrate_refs": _STRS}, ("rationale", "diff"),
    ),
    "propose_self_revision": _s(
        "propose_self_revision",
        "Propose a SELF-REVISION of the journal's own standing text. Writes a "
        "PENDING proposal row only.",
        {"rationale": _STR, "diff": {"type": "object"},
         "cited_substrate_refs": _STRS}, ("rationale", "diff"),
    ),
    # ---- substrate_read: the COLLECTION series reads (7g-2) ---------------
    # HISTORY, not now. `from`/`to` bound the VALID time (the period a number
    # is ABOUT) and are required — a series read is never all-time. `as_of`
    # bounds the RECORD time (which revision the provider had published by
    # then), which is a different question and the descriptions say so,
    # because a planner that conflates the two writes a 2016 figure as a
    # current one.
    "series_history": _s(
        "series_history",
        "One curated HISTORICAL series for one subject (ISO-3166-1 alpha-2), "
        "over a valid-time window. from/to are REQUIRED and bound the period "
        "the numbers are ABOUT (YYYY or YYYY-MM-DD). Optional as_of bounds "
        "the provider's RECORD time: with it you get the latest revision "
        "published on or before that instant, without it the latest on "
        "record. Rows are not live reporting — each carries its valid "
        "period, its record time, the value with its unit, the source URL "
        "and the sha256 of the file it was read out of.",
        {"series_id": _STR, "subject": _STR, "from": _STR, "to": _STR,
         "as_of": _STR, "collection_id": _STR, "limit": _INT},
        ("series_id", "subject", "from", "to"),
    ),
    "series_compare": _s(
        "series_compare",
        "The same historical series across SEVERAL subjects over one "
        "valid-time window — one call, not one per subject. Same required "
        "from/to and optional as_of as series_history. A subject the holding "
        "does not carry returns no rows and is named in "
        "subjects_with_no_rows; it is never padded with a zero.",
        {"series_id": _STR, "subjects": _STRS, "from": _STR, "to": _STR,
         "as_of": _STR, "collection_id": _STR, "limit": _INT},
        ("series_id", "subjects", "from", "to"),
    ),
    # ---- substrate_read: the LIVE series read (E2) -------------------------
    # NOW, not history: the rows a source's poller writes (origin_class
    # 'live'). A separate tool so the history tools above stay history-only;
    # the bounds are in the schema (limit max, the window cap in the text) and
    # enforced again by the reader (runtime/_observations_live_read.py).
    "live_series": _s(
        "live_series",
        "LIVE numeric series written by the platform's pollers (chokepoint "
        "transits, central-bank policy rates) — NOT the curated history the "
        "series_history tool reads. Without series_id (or without subject) "
        "it lists the live series held: source_id, series_id, subject, unit, "
        "period, newest period and newest record/load time. With series_id "
        "and subject it reads that series over a valid window from/to "
        "(YYYY-MM-DD; default the last 90 days; at most 366 days), oldest "
        "period first, EVERY revision, each row labelled live with its "
        "source_id, record_time (provider revision or first fetch) and "
        "loaded_at. At most 500 rows (truncated says when the cap bit).",
        {"series_id": _STR, "subject": _STR, "source_id": _STR,
         "from": _STR, "to": _STR,
         "limit": {"type": "integer", "minimum": 1, "maximum": 500}},
    ),
}


#: Tools whose dispatch CHANGES substrate state. A batch containing any of
#: these must be executed SERIALLY, in emitted order — two proposals racing
#: each other through one writeback context is not a hypothetical (the journal
#: propose path holds a per-run ``WritebackContext``), and a write that lands
#: out of order leaves a lineage that does not match the transcript.
WRITE_TOOLS: frozenset[str] = frozenset({
    "web_evidence",          # lands one signals row per kept hit
    "propose_fact",
    "request_source",
    "open_question",
    "propose_correction",
    "propose_change",
    "propose_self_revision",
})


#: Tools a provider registered through :func:`register_tool_schema` with
#: ``writes=True``. Kept apart from :data:`WRITE_TOOLS` (a frozen literal the
#: tests pin) and read by :func:`is_write_tool` beside it.
_REGISTERED_WRITE_TOOLS: set[str] = set()


def register_tool_schema(spec: ToolSpec, *, writes: bool = False) -> None:
    """Add ``spec`` to :data:`TOOL_SCHEMAS` — the entry point a tool provider
    outside this module uses (C-1b's ``workstation_drive`` tools first; the
    C-2a write frame next).

    Idempotent for the SAME spec (a re-import registers nothing new); a
    DIFFERENT spec under a name already present raises, because two grammars
    for one tool name would make :func:`validate_args` check against whichever
    import happened last. ``writes=True`` makes :func:`is_write_tool` answer
    True for it, so a loop that orders writes serially orders it too.
    """
    existing = TOOL_SCHEMAS.get(spec.name)
    if existing is not None and existing != spec:
        raise ValueError(
            f"tool schema {spec.name!r} is already registered with a different "
            "grammar; a tool name has exactly one schema"
        )
    TOOL_SCHEMAS[spec.name] = spec
    if writes:
        _REGISTERED_WRITE_TOOLS.add(spec.name)


def is_write_tool(name: str) -> bool:
    """Does ``name`` mutate substrate? Unknown names are treated as writes.

    Fail-safe on purpose: an unrecognized tool gets the SERIAL path, which is
    slower and always correct, rather than the concurrent path, which is faster
    and wrong if the tool turns out to write.
    """
    return (
        name not in TOOL_SCHEMAS
        or name in WRITE_TOOLS
        or name in _REGISTERED_WRITE_TOOLS
    )


def _fallback_spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=(
            f"{name} — arguments are passed through as given (no declared "
            "schema for this tool)."
        ),
        json_schema={"type": "object", "properties": {}, "additionalProperties": True},
    )


def build_tool_specs(bindings: Any) -> list[ToolSpec]:
    """Tool specs for everything ``bindings`` can actually reach.

    ``bindings`` is either a mapping of ``tool_name -> binding`` (the shape the
    GATHER loop holds in ``tool_bindings``) or a plain iterable of tool names.
    Order is preserved from the input for a mapping/sequence and sorted for an
    unordered set, so the rendered ``tools`` array is STABLE across runs — an
    unstable tool order would change the request payload for identical inputs
    and break every byte-identity claim downstream.

    A name with no entry in :data:`TOOL_SCHEMAS` gets a permissive free-form
    spec rather than being dropped: a loop that can route a tool must be able
    to offer it.
    """
    if bindings is None:
        return []
    if isinstance(bindings, Mapping):
        names: Iterable[Any] = list(bindings.keys())
    elif isinstance(bindings, (str, bytes)):
        names = [bindings]
    elif isinstance(bindings, (set, frozenset)):
        names = sorted(str(n) for n in bindings)
    elif isinstance(bindings, Iterable):
        names = list(bindings)
    else:
        return []
    specs: list[ToolSpec] = []
    seen: set[str] = set()
    for raw in names:
        name = str(raw)
        if not name or name in seen:
            continue
        seen.add(name)
        specs.append(TOOL_SCHEMAS.get(name) or _fallback_spec(name))
    return specs


def render_tools(provider: str, specs: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    """Render specs into ``provider``'s wire grammar.

    Both shapes survive their handler's ``_translate_tools`` untouched — the
    OpenAI shape is that handler's native input, and the Anthropic handler
    passes an already-``input_schema``-shaped spec straight through — so this
    is the final wire form, not an intermediate one.
    """
    out: list[dict[str, Any]] = []
    for spec in specs:
        schema = dict(spec.json_schema)
        if provider == PROVIDER_ANTHROPIC:
            out.append({
                "name": spec.name,
                "description": spec.description,
                "input_schema": schema,
            })
        else:
            out.append({
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": schema,
                },
            })
    return out


# ---------------------------------------------------------------------------
# Reading the reply
# ---------------------------------------------------------------------------


def _coerce_args(raw: Any) -> dict[str, Any]:
    """A tool call's arguments, as a dict, whatever the provider sent.

    OpenAI-compatible endpoints send a JSON *string*; Anthropic sends an object;
    a degraded endpoint sends neither. An unparseable payload is preserved
    under ``_raw`` rather than discarded — the tool will refuse it loudly, which
    is a better outcome than a silently empty argument set.
    """
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {"_raw": raw}
        if isinstance(parsed, Mapping):
            return dict(parsed)
        return {"_value": parsed}
    if raw is None:
        return {}
    return {"_value": raw}


def _calls_from_normalized(response: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for tc in getattr(response, "tool_calls", None) or []:
        name = str(getattr(tc, "name", "") or "")
        if not name:
            continue
        calls.append(ToolCall(
            id=str(getattr(tc, "id", "") or uuid.uuid4().hex),
            name=name,
            args=_coerce_args(getattr(tc, "arguments", None)),
            raw=tc,
        ))
    return calls


def _calls_from_raw_openai(raw: Mapping[str, Any]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for choice in raw.get("choices") or []:
        if not isinstance(choice, Mapping):
            continue
        message = choice.get("message")
        if not isinstance(message, Mapping):
            continue
        for tc in message.get("tool_calls") or []:
            if not isinstance(tc, Mapping):
                continue
            fn = tc.get("function") if isinstance(tc.get("function"), Mapping) else {}
            name = str(fn.get("name") or tc.get("name") or "")
            if not name:
                continue
            calls.append(ToolCall(
                id=str(tc.get("id") or uuid.uuid4().hex),
                name=name,
                args=_coerce_args(fn.get("arguments", tc.get("arguments"))),
                raw=dict(tc),
            ))
        # Only the primary choice carries tool_calls on the planes we run
        # (vLLM's reasoning choices do not); stop once one has produced them so
        # a coalesced multi-choice reply cannot double-execute a batch.
        if calls:
            break
    return calls


def _calls_from_raw_anthropic(raw: Mapping[str, Any]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for block in raw.get("content") or []:
        if not isinstance(block, Mapping) or block.get("type") != "tool_use":
            continue
        name = str(block.get("name") or "")
        if not name:
            continue
        calls.append(ToolCall(
            id=str(block.get("id") or uuid.uuid4().hex),
            name=name,
            args=_coerce_args(block.get("input")),
            raw=dict(block),
        ))
    return calls


def parse_tool_calls(provider: str, response: Any) -> list[ToolCall]:
    """Every tool call in one assistant turn, in emitted order.

    Keyed ONLY on the structured payload — ``message.tool_calls`` /
    ``tool_use`` content blocks. ``finish_reason`` is never consulted, because
    the live core plane returns ``finish_reason=stop`` on a FORCED tool call
    (probed 2026-09-16); a parser that gated on it would drop that call.

    The handler's own normalization (``LLMResponse.tool_calls``) is preferred
    when present; the raw walk below is the path for a handler or double that
    returned the wire payload unnormalized.
    """
    calls = _calls_from_normalized(response)
    if calls:
        return calls
    raw = getattr(response, "raw_response", None)
    if not isinstance(raw, Mapping):
        raw = response if isinstance(response, Mapping) else None
    if not isinstance(raw, Mapping):
        return []
    if provider == PROVIDER_ANTHROPIC:
        return _calls_from_raw_anthropic(raw)
    return _calls_from_raw_openai(raw)


def visible_text(response: Any) -> str:
    """What the model SAID — never what it privately reasoned.

    Reads ``LLMResponse.content`` only. Both handlers build ``content`` from
    text blocks alone (Anthropic skips ``thinking`` blocks; the OpenAI-compat
    handler reads ``message.content`` and never ``message.reasoning_content``),
    so this is already clean — the function exists so that every caller goes
    through one accessor whose contract is stated, instead of each reaching for
    an attribute and one of them eventually reaching for the wrong one.
    """
    return str(getattr(response, "content", "") or "")


# ---------------------------------------------------------------------------
# Validating the arguments that came BACK
# ---------------------------------------------------------------------------
#
# The grammar above was rendered TO the model and never checked ON the reply:
# :func:`_coerce_args` returned whatever arrived and every dispatcher reads it
# with ``args.get()``, so an argument the schema does not declare was dropped
# in silence and an argument it does declare was honoured whatever it said.
#
# 2026-09-28 00:58Z, consult session 96d7bfc2 on the core plane: the model's
# FIRST tool call was ``list_situations`` with
# ``{"result": [six fabricated situation rows], "status": "ok",
#   "metadata": {}}`` — it had written the reply it expected into the call it
# was making. ``result`` and ``metadata`` went on the floor; ``status="ok"``
# became a SQL predicate (no situation carries that status), the read returned
# count 0, the loop marked the round ``ok``, and the answer cited the six ids
# the model had invented. Nothing in the platform said a word.
#
# :func:`validate_args` is the check. It runs at ONE place on the governed
# path (``agency.Agency._run_pack_tool``, between the handler check and the
# governor) so a refusal is a normal BLOCK: a ``governor_events`` row, the
# arguments on the run receipt, and ``tool_blocked: <cause>: <detail>`` handed
# back to the model in the tool message, which is a round it can recover from.


#: Top-level argument names that name a tool's OUTPUT rather than its input.
#: Checked FIRST and reported on their own, because "you wrote a result into
#: the arguments" is a different mistake from "that argument does not exist"
#: and only the first one tells the model what it actually did wrong.
RESULT_SHAPED_ARG_KEYS: frozenset[str] = frozenset({
    "result", "results", "data", "items", "rows", "output", "response",
    "count", "refs", "metadata",
})

#: Keys the PLATFORM adds to a call's arguments after the model wrote them.
#: They are never part of a rendered schema and must never be read as the
#: model's own work: ``scope_predicate`` is injected caller-side by the
#: consult / deep-consult loops so a planner cannot override an operator
#: scope, and ``_raw`` / ``_value`` are what :func:`_coerce_args` leaves
#: behind when a provider sent an unparseable argument payload. Whitelisting
#: ``_raw`` hides nothing: a call carrying only ``_raw`` still fails the
#: required-argument check below, by name.
INJECTED_ARG_KEYS: frozenset[str] = frozenset({
    "scope_predicate", "_raw", "_value",
})

#: The model wrote a tool RESULT into the tool's ARGUMENTS.
CAUSE_RESULT_SHAPED_ARG = "result_shaped_arg"
#: An unknown argument, a missing required one, or a coarse type mismatch.
CAUSE_INVALID_ARG = "invalid_arg"

#: Every cause :func:`validate_args` can report — the caller's block-cause
#: vocabulary, so a cause cannot appear in a governor event without appearing
#: here first.
ARG_BLOCK_CAUSES: frozenset[str] = frozenset({
    CAUSE_RESULT_SHAPED_ARG, CAUSE_INVALID_ARG,
})


def _names(names: Iterable[str]) -> str:
    return ", ".join(sorted(str(n) for n in names))


def _declared_schema(spec: Any) -> Mapping[str, Any] | None:
    """``spec``'s object schema, or ``None`` when nothing was declared.

    ``None`` — validation is SKIPPED — for a tool that resolved to
    :func:`_fallback_spec`: that spec exists precisely because the tool has no
    authored grammar, it is rendered ``additionalProperties: True``, and
    refusing arguments against a schema we never stated would block a working
    tool for the crime of not being in :data:`TOOL_SCHEMAS`.
    """
    schema = getattr(spec, "json_schema", None)
    if not isinstance(schema, Mapping):
        return None
    if schema.get("additionalProperties") is True:
        return None
    if not isinstance(schema.get("properties"), Mapping):
        return None
    return schema


def _string_parses_as(value: Any, declared: str) -> bool:
    """Is ``value`` a string that parses cleanly to ``declared``?

    The type check is deliberately COARSE. Every dispatcher on both loops
    coerces its own scalars (``int(args.get("limit", 20))``,
    ``str(args.get("include_superseded", False)).lower() in ("true", "1")``),
    so a model that wrote ``"20"`` for an integer has made no mistake this
    validator should spend a round on. A value that does NOT parse — an object
    where a number was declared, a list where a string was — is a real
    mismatch and is reported.
    """
    if not isinstance(value, str):
        return False
    s = value.strip()
    if declared == "boolean":
        return s.lower() in ("true", "false", "1", "0")
    try:
        int(s, 10) if declared == "integer" else float(s)
    except ValueError:
        return False
    return True


def _type_matches(value: Any, schema: Mapping[str, Any]) -> bool:
    branches = schema.get("oneOf") or schema.get("anyOf")
    if isinstance(branches, (list, tuple)) and branches:
        return any(
            _type_matches(value, b) for b in branches if isinstance(b, Mapping)
        )
    declared = schema.get("type")
    if not isinstance(declared, str):
        return True
    if declared == "object":
        return isinstance(value, Mapping)
    if declared == "array":
        # A bare string where a list is declared is the HANDLERS' own
        # documented tolerance — ``substrate_read._subjects`` reads
        # ``subjects: "US"`` as one subject and
        # ``write_tools._coerce_derived_from`` reads a single id as a
        # one-element list, both on the stated grounds that refusing it would
        # spend a round teaching the model JSON. The validator does not
        # overrule them.
        return isinstance(value, (list, tuple, str))
    if declared == "boolean":
        return isinstance(value, bool) or _string_parses_as(value, declared)
    if declared == "integer":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        if isinstance(value, float):
            return value.is_integer()
        return _string_parses_as(value, declared)
    if declared == "number":
        if isinstance(value, bool):
            return False
        return isinstance(value, (int, float)) or _string_parses_as(
            value, declared)
    if declared == "string":
        return isinstance(value, str)
    return True


def validate_args(spec: Any, args: Any) -> str | None:
    """Check one tool call's arguments against the grammar we rendered.

    Returns ``None`` when the call is well formed, or ``"<cause>: <detail>"``
    where ``<cause>`` is one of :data:`ARG_BLOCK_CAUSES` and ``<detail>`` is
    written FOR THE MODEL — it names the tool, what was wrong, and the
    arguments the tool actually declares. :func:`split_arg_problem` takes the
    two apart for the caller's block event.

    ``spec`` is the :class:`ToolSpec` for the call — ``TOOL_SCHEMAS.get(name)``
    is the intended lookup, and a tool with no entry (``None``, or the
    permissive :func:`_fallback_spec`) is NOT validated.

    Order matters. A result-shaped key is reported first and alone: it is the
    live failure this function was written for, and telling the model "no such
    argument ``result``" would understate what it did.
    """
    if not isinstance(args, Mapping):
        return None
    schema = _declared_schema(spec)
    if schema is None:
        return None
    name = str(getattr(spec, "name", "") or "tool")
    props: Mapping[str, Any] = schema["properties"]
    declared = _names(props) or "(none — this tool takes no arguments)"

    undeclared = [
        str(k) for k in args
        if str(k) not in props and str(k) not in INJECTED_ARG_KEYS
    ]
    result_shaped = [k for k in undeclared if k.lower() in RESULT_SHAPED_ARG_KEYS]
    if result_shaped:
        return (
            f"{CAUSE_RESULT_SHAPED_ARG}: {name} was called with "
            f"{_names(result_shaped)} — those name a tool's OUTPUT and you "
            "have written the result you expected into the call's INPUT. The "
            "tool has not run yet and nothing you put there is real. Call "
            f"{name} again with its declared arguments only ({declared}), "
            "then read the actual result from the tool message that comes "
            "back."
        )
    if undeclared:
        return (
            f"{CAUSE_INVALID_ARG}: {name} has no argument "
            f"{_names(undeclared)}; its declared arguments are {declared}."
        )

    required = schema.get("required")
    if isinstance(required, (list, tuple)):
        missing = [str(r) for r in required if args.get(str(r)) is None]
        if missing:
            return (
                f"{CAUSE_INVALID_ARG}: {name} requires {_names(missing)}; "
                f"its declared arguments are {declared}."
            )

    for key, value in args.items():
        prop = props.get(str(key))
        if not isinstance(prop, Mapping) or value is None:
            continue
        if not _type_matches(value, prop):
            want = prop.get("type") or "one of its declared forms"
            return (
                f"{CAUSE_INVALID_ARG}: {name} argument {key!r} must be "
                f"{want}, got {type(value).__name__}."
            )
        enum = prop.get("enum")
        if (
            isinstance(enum, (list, tuple)) and enum
            and isinstance(value, str) and value not in enum
        ):
            return (
                f"{CAUSE_INVALID_ARG}: {name} argument {key!r} must be one of "
                f"{_names(enum)}; got {value!r}."
            )
    if schema.get("additionalProperties") is False:
        for key, value in args.items():
            prop = props.get(str(key))
            if isinstance(prop, Mapping) and value is not None:
                problem = _closed_problem(name, prop, value, str(key))
                if problem is not None:
                    return problem
    return None


def _closed_problem(name: str, schema: Mapping[str, Any], value: Any, path: str) -> str | None:
    """The DEEP check, for a schema that declares itself closed.

    Only a tool whose top-level schema says ``additionalProperties: false``
    reaches here (the ``workstation_drive`` tools, C-1b), so every schema
    authored before it is validated exactly as it was. Below the top level the
    check is strict where the shallow one is tolerant on purpose: a nested
    object must be an object, an array must be a list (no bare-string
    tolerance — no handler down here reads one), undeclared nested keys and
    missing nested required keys are refused, and enums, string lengths and
    item counts are enforced.
    """
    declared = schema.get("type")
    where = f"{name} argument {path!r}"
    if declared == "object":
        if not isinstance(value, Mapping):
            return f"{CAUSE_INVALID_ARG}: {where} must be an object, got {type(value).__name__}."
        props = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
        if schema.get("additionalProperties") is False:
            extra = [str(k) for k in value if str(k) not in props]
            if extra:
                return (
                    f"{CAUSE_INVALID_ARG}: {where} has no field {_names(extra)}; "
                    f"its fields are {_names(props) or '(none)'}."
                )
        for req in schema.get("required") or ():
            if value.get(str(req)) is None:
                return f"{CAUSE_INVALID_ARG}: {where} requires {req!r}."
        for key, sub in value.items():
            prop = props.get(str(key))
            if isinstance(prop, Mapping) and sub is not None:
                problem = _closed_problem(name, prop, sub, f"{path}.{key}")
                if problem is not None:
                    return problem
        return None
    if declared == "array":
        if not isinstance(value, (list, tuple)):
            return f"{CAUSE_INVALID_ARG}: {where} must be a list, got {type(value).__name__}."
        lo, hi = schema.get("minItems"), schema.get("maxItems")
        if isinstance(lo, int) and len(value) < lo:
            return f"{CAUSE_INVALID_ARG}: {where} needs at least {lo} item(s)."
        if isinstance(hi, int) and len(value) > hi:
            return f"{CAUSE_INVALID_ARG}: {where} takes at most {hi} item(s)."
        items = schema.get("items")
        if isinstance(items, Mapping):
            for idx, item in enumerate(value):
                problem = _closed_problem(name, items, item, f"{path}[{idx}]")
                if problem is not None:
                    return problem
        return None
    if not _type_matches(value, schema):
        return f"{CAUSE_INVALID_ARG}: {where} must be {declared or 'one of its declared forms'}."
    if declared == "string" and isinstance(value, str):
        enum = schema.get("enum")
        if isinstance(enum, (list, tuple)) and enum and value not in enum:
            return f"{CAUSE_INVALID_ARG}: {where} must be one of {_names(enum)}; got {value!r}."
        lo, hi = schema.get("minLength"), schema.get("maxLength")
        if isinstance(lo, int) and len(value) < lo:
            return f"{CAUSE_INVALID_ARG}: {where} must not be empty."
        if isinstance(hi, int) and len(value) > hi:
            return f"{CAUSE_INVALID_ARG}: {where} is longer than {hi} characters."
    return None


def split_arg_problem(problem: str) -> tuple[str, str]:
    """``"<cause>: <detail>"`` from :func:`validate_args` → ``(cause, detail)``.

    A string that does not start with a known cause is reported whole under
    :data:`CAUSE_INVALID_ARG` rather than being silently reshaped — a block
    event must never carry a cause nobody declared.
    """
    cause, sep, detail = str(problem).partition(": ")
    if not sep or cause not in ARG_BLOCK_CAUSES:
        return CAUSE_INVALID_ARG, str(problem)
    return cause, detail


# ---------------------------------------------------------------------------
# Writing the next turn
# ---------------------------------------------------------------------------


def assistant_tool_turn(
    provider: str, response: Any, calls: Sequence[ToolCall],
) -> dict[str, Any]:
    """Rebuild the assistant turn that carried ``calls``, for replay.

    Built from normalized fields ONLY, so the ids the provider issued are
    preserved (the tool results correlate by id) while ``reasoning_content`` /
    ``thinking`` blocks are structurally unable to come along.
    """
    text = visible_text(response)
    if provider == PROVIDER_ANTHROPIC:
        blocks: list[dict[str, Any]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        for call in calls:
            blocks.append({
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": dict(call.args),
            })
        return {"role": "assistant", "content": blocks}
    return {
        "role": "assistant",
        # OpenAI requires the key to be present even when the turn was pure
        # tool calls; null is the documented value for "no prose this turn".
        "content": text or None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(dict(call.args), sort_keys=True),
                },
            }
            for call in calls
        ],
    }


def tool_result_messages(
    provider: str, calls: Sequence[ToolCall], results: Sequence[Any],
) -> list[dict[str, Any]]:
    """The result side of one batch, correlated by the provider's own ids.

    OpenAI-compatible: ONE ``role=tool`` message per call, each carrying its
    ``tool_call_id`` — order matters to readers but correlation does not depend
    on it. Anthropic: ONE ``role=user`` message whose content is all the
    ``tool_result`` blocks IN ORDER, which is what that API requires (a
    separate user turn per result is a protocol error).

    ``results`` is positional against ``calls``; a short ``results`` yields an
    explicit "no result" body rather than a silently missing message, because
    a missing tool_result for an issued call is a hard protocol error on both
    routes and must not be produced by an off-by-one.
    """
    bodies: list[str] = []
    for idx, _call in enumerate(calls):
        if idx < len(results):
            payload = results[idx]
        else:  # pragma: no cover — guarded by the caller, kept fail-loud
            payload = {"error": "tool_result_missing: no result for this call"}
        bodies.append(
            payload if isinstance(payload, str)
            else json.dumps(payload, default=str)
        )
    if provider == PROVIDER_ANTHROPIC:
        return [{
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": body,
                }
                for call, body in zip(calls, bodies)
            ],
        }] if calls else []
    return [
        {
            "role": "tool",
            "tool_call_id": call.id,
            "name": call.name,
            "content": body,
        }
        for call, body in zip(calls, bodies)
    ]
