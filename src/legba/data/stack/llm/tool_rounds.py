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
    "PROVIDER_ANTHROPIC",
    "PROVIDER_OPENAI_COMPAT",
    "TOOL_SCHEMAS",
    "ToolCall",
    "ToolSpec",
    "WRITE_TOOLS",
    "assistant_tool_turn",
    "build_tool_specs",
    "is_write_tool",
    "parse_tool_calls",
    "provider_for_subprovider",
    "render_tools",
    "supports_native_tools",
    "tool_result_messages",
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
        "search_signals row id).",
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
         "as_of": _STR},
        ("subject", "object"),
    ),
    "find_proxy_chains": _s(
        "find_proxy_chains",
        "INDIRECT links only between two entities — multi-hop chains plus "
        "reified A->via->B cut-outs. The proxy path from A to B.",
        {"subject": _STR, "object": _STR, "max_hops": _INT,
         "polarity_product": _INT, "limit": _INT, "families": _STRS,
         "as_of": _STR},
        ("subject", "object"),
    ),
    "query_brokers": _s(
        "query_brokers",
        "Entities that SIT ON paths between two entity sets (the broker "
        "between two camps), ranked by how many A->B paths run through them.",
        {"camp_a": _STRS, "camp_b": _STRS, "max_hops": _INT, "limit": _INT,
         "families": _STRS, "as_of": _STR},
        ("camp_a", "camp_b"),
    ),
    "list_findings": _s(
        "list_findings",
        "The platform's OWN prior assessments/findings. Check these FIRST to "
        "build on and reconcile against earlier work; cite the output_id.",
        {"target_id": _STR, "analyst_id": _STR, "severity": _STR,
         "since_hours": _INT, "include_superseded": _BOOL,
         "believed_as_of": _STR, "limit": _INT},
    ),
    "list_situations": _s(
        "list_situations",
        "Ongoing situation frames the platform has clustered. Use a "
        "situation_id with query_hypotheses to pull its ACH rows. as_of "
        "(ISO-8601) reads the frames that held on that date.",
        {"status": _STR, "target_id": _STR, "since_hours": _INT,
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
        {"target_id": _STR, "geo": {"oneOf": [_STR, _STRS]},
         "category": _STR, "lifecycle_state": _STR, "entity": _STR,
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
        "Ingest sources + freshness; tell 'no coverage on X' apart from 'a "
        "quiet feed'.",
        {"active_only": _BOOL, "silent_only": _BOOL, "silent_hours": _INT},
    ),
    # ---- web_access -------------------------------------------------------
    "web_search": _s(
        "web_search",
        "Query the operator-pinned search endpoint; returns {title, url, "
        "snippet} results. Read-only — nothing is landed.",
        {"query": _STR, "limit": _INT}, ("query",),
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
        "derived_from lineage citing substrate UUIDs is REQUIRED.",
        {"subject": _STR, "predicate": _STR, "value": _STR,
         "derived_from": _STRS, "confidence": {"type": "number"}},
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
        "get_source_health", "Per-source ingestion health; silent sources.",
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


def is_write_tool(name: str) -> bool:
    """Does ``name`` mutate substrate? Unknown names are treated as writes.

    Fail-safe on purpose: an unrecognized tool gets the SERIAL path, which is
    slower and always correct, rather than the concurrent path, which is faster
    and wrong if the tool turns out to write.
    """
    return name not in TOOL_SCHEMAS or name in WRITE_TOOLS


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
