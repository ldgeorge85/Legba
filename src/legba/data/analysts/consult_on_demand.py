# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-178 ``consult_on_demand`` analyst kind.

**Purpose.** Preserves the heavily-used consult capability from the
retiring legacy ``ConsultPanel`` UI (see ``src/legba/ui/consult.py``) as
the 9th analyst kind in the topology v2 taxonomy (per
``plans/design/legba_topology_redesign.md`` §5 + ``§5.9 Open taxonomy``).

Per L-178 in ``plans/task_tracker.md`` §15.6:

  * **No scheduled cadence.** Purely on-demand. Dispatched via A2A skill,
    MCP tool, or a future operator panel.
  * **Reads.** A free-form natural-language question (``inputs[0]["question"]``)
    plus optional ``scope_predicate`` string. Reads substrate via direct
    queries scoped by the predicate; optionally other analysts' findings.
  * **Method.** LLM planner with a tool whitelist — *not* the
    seven-phase cycle envelope used by ``inline_target``. A single-turn
    ReAct loop with substrate-tool calls. Capped at
    :data:`MAX_TOOL_ROUNDS` rounds so a misbehaving planner can't grind
    forever.
  * **Writes.** A structured :class:`ConsultResponsePayload` (added to
    ``data/provenance/models.py`` per the L-178 spec one-liner —
    extending provenance models is the surgically smallest carrier for
    the new shape). Carried into the substrate as a
    ``FindingPayload.data`` payload so the existing ``OutputKind.FINDING``
    write path (``runtime/dapr_actors.py:704``) stays untouched, and
    returned directly to non-runtime dispatchers (A2A skill, MCP tool,
    panel) via :attr:`AnalystMethodResult.consult_response`.

Dispatch shape (for the integration pass):

  * **A2A skill name:** ``intelligence.consult_on_demand``
  * **MCP tool name:** ``legba_consult`` (input schema: ``{"question": str,
    "scope_predicate": str | None}``)
  * **Operator panel:** ``O-Consult`` per the §15.6 / L-178 note.

ReAct loop (max :data:`MAX_TOOL_ROUNDS`=6 rounds)::

    PLAN  → render system prompt + tool whitelist + the operator's question
    ROUND ← LLM emits either {"tool": "<name>", "args": {...}} (strict JSON)
            OR the FINAL reply: the :data:`FINAL_SENTINEL` line, a short
            header block, then the answer as raw markdown — NOT JSON.
    ACT   → if a tool was requested, dispatch via the ToolDispatcher; the
            tool's JSON result is appended to the conversation as a
            "tool" role message.
    LOOP  ← back to ROUND, max MAX_TOOL_ROUNDS iterations.  After the cap
            we force a final synthesis turn (no tools available) so the
            operator always gets a structured answer.

The two-shape split is deliberate: mid-loop tool calls are machine-to-machine
and stay strict JSON, while the FINAL answer is prose for a human and is not
wrapped in anything.  See :func:`_parse_sentinel_final` for why.

The tool whitelist is small on purpose — the legacy ConsultPanel exposed
~22 tools (see ``ui/consult.py:CONSULT_TOOLS``).  For the kind's first
shipment, we restrict to four read-only primitives that cover the bulk
of Lewis's actual consult traffic per the L-178 note ("daily-use
pattern"):

  * ``search_signals``  — substrate signal search (Postgres FTS over
                          title + summary).
  * ``query_facts``     — substrate fact search (subject/predicate/value
                          ILIKE).
  * ``inspect_entity``  — entity profile + recent fact bundle for one
                          canonical name.
  * ``vector_search``   — semantic search over signal embeddings (only
                          available when ``deps.extras["vector_store"]``
                          is wired; otherwise reports as unavailable).

Write-side tools (the legacy panel had ``add_entity_assertion``,
``update_situation`` etc.) are deliberately NOT in this kind's whitelist
— the consult kind is a *read* over substrate; write-back belongs to
operator-driven panels with explicit audit. Adding write tools later is
a config-level decision per analyst descriptor (the descriptor's
``method.tools_whitelist`` block) without code changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol, runtime_checkable
from uuid import UUID

from ..pinned_context import render_pinned_context_block
from ..provenance.models import ConsultResponsePayload, FindingPayload
from ..stack.llm import tool_round_compaction as _tc
from ..stack.llm.stream_observer import TextDeltaSink, capture_text_deltas
from . import consult_round_protocol as _cp
from . import consult_transcript as _tx
# V3/P3 — the SubstrateQueryPort protocol moved to a leaf when the temporal
# params grew this module past its size ceiling; imported back and
# re-exported so every call site is byte-identical.
from .consult_provenance_census import build_provenance_census
from .consult_substrate_port import SubstrateQueryPort
from .consult_spend_guard import SpendGuard

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Kind identity (registry key — see kind_contracts §1)
# ---------------------------------------------------------------------------


KIND_NAME = "consult_on_demand"
SCHEMA_VERSION = "legba/analyst.consult_on_demand/1-0-0"
HANDLER_VERSION = "0.1.0"
PROMPT_MODULE_PATH = "legba.prompts.consult_on_demand.v1"

# Host-discovered constants for the per-kind dispatch path.
# READ_SLICE is None — consult_on_demand receives its inputs directly via
# the A2A skill / MCP tool / panel invocation (one row, carrying the
# operator's question + scope_predicate); it does NOT walk the substrate
# slice in the actor's pre-run path.
from ..provenance.kinds import OutputKind as _OutputKind  # noqa: E402

OUTPUT_KIND: _OutputKind = _OutputKind.FINDING
READ_SLICE = None

#: Max ReAct rounds before forcing a final synthesis turn.  At 6 rounds
#: with a tool result per round, the typical consult exchange (per the
#: legacy panel's MAX_TOOL_STEPS=10) lands within budget while leaving
#: headroom for the planner to refine.
MAX_TOOL_ROUNDS = 6

#: Per-run round controls for the chat consult path (Piece 1, D1).  The chat
#: default lifts the round budget so a back-and-forth can survey broadly then
#: drill; ``ROUNDS_CEILING`` is the hard clamp the kind applies regardless of
#: what a caller requests, so a runaway slider can never grind forever.
CHAT_DEFAULT_ROUNDS = 10
ROUNDS_CEILING = 30

#: Max tools a planner may request in ONE batch round (the ``{"tools": [...]}``
#: shape). Extra calls past this are dropped and duplicate ``(tool, args)`` pairs
#: deduped, so a runaway planner can't open an unbounded number of concurrent
#: substrate calls in a single round.
MAX_TOOLS_PER_BATCH = 5

#: Below this much remaining total budget, the forced-final synthesis is not
#: attempted at all — starting an Opus synthesis with 8 seconds left produces a
#: timeout, not an answer. The loop emits its degraded FINAL instead.
_MIN_FINAL_SECONDS = 15.0

#: Characters of streamed synthesis to buffer before relaying one frame to the
#: live stream. Per-delta relay would flood the SSE relay's 256-slot queue,
#: which drops on full — the operator would watch an answer form with holes in
#: it. 400 is roughly a sentence: fast enough to read as live typing.
_ANSWER_DELTA_CHARS = 400

#: F1 model picker — the sanctioned LLM planes a consult / deep_consult request
#: may switch to per-request. The registry front door maps the operator's
#: friendly choice ("opus"/"fable"/"core") to one of THESE component ids and
#: threads it as ``inputs[0]["llm_component_override"]``; the runtime's by-id
#: resolver is bound to this set so ONLY these planes ever resolve at run time
#: (a raw component id that somehow reached the override field is refused).
#: "opus" = the billed Anthropic Opus plane (the ACTIVATE-time default — no
#: override needed); "fable" = the billed Anthropic Claude Fable 5.1 plane
#: (selectable, never default); "core" = the free self-hosted core
#: (openai_compat) plane.
LLM_OVERRIDE_ALLOWLIST = frozenset(
    {
        "llm.anthropic.opus_4_7",
        "llm.anthropic.fable_5_1",
        "llm.primary.openai_compat",
    }
)


#: Fallback per-LLM-call output budget — reached ONLY when neither the env
#: override nor a descriptor cap is present (hand-built test/embedder deps).
#: The production deps builder always threads the descriptor's
#: ``method.llm.max_tokens``, so this is not the operative production cap.
_FALLBACK_MAX_TOKENS = 2048


def _env_max_tokens() -> int | None:
    """The ``LEGBA_CONSULT_MAX_TOKENS`` emergency override, or None.

    Unset / empty / non-positive / non-numeric all resolve to None so a
    malformed pin can never zero the budget.
    """
    raw = os.getenv("LEGBA_CONSULT_MAX_TOKENS", "").strip()
    if not raw:
        return None
    try:
        val = int(raw)
    except ValueError:
        return None
    return val if val > 0 else None


def resolve_output_budget(
    descriptor_max_tokens: int | None,
    *,
    fallback: int = _FALLBACK_MAX_TOKENS,
) -> int:
    """Per-LLM-call output budget: DESCRIPTOR-governed, env-OVERRIDABLE.

    Precedence:

      1. ``LEGBA_CONSULT_MAX_TOKENS`` — an EMERGENCY valve only. When it is
         set AND a descriptor cap exists, the env value wins and the override
         is logged LOUDLY (WARNING) every resolve, so a forgotten env pin can
         never silently defeat the descriptor-governed budget.
      2. the descriptor's ``method.llm.max_tokens`` — the governed production
         value, threaded by the runtime deps builder
         (``_build_consult_on_demand`` / ``_build_deep_consult``).
      3. ``fallback`` — hand-built deps with no descriptor (tests, embedders).

    HISTORY (why the cap used to be 2048, and why it no longer is): under the
    old NON-STREAMING Anthropic wire call the cap was welded to the provider's
    HTTP window, not to answer quality — 1024 truncated real "world report"
    answers mid-string, and 4096 made a broad forced-final's generation slow
    enough to outrun the provider timeout → ``network error`` → actor retry
    storm → 504, so 2048 was the survivable sweet spot. The Anthropic handler
    now STREAMS every generation (the connection stays live for the whole
    output; see ``stack/llm/anthropic.py:_call_chat_streaming``), which
    removes the transport coupling entirely: the budget is sized for complete
    answers (32768 on the consult descriptors) and is governed where the rest
    of the method knobs live — the descriptor. An over-budget answer is still
    not a parse hazard: under the plain-markdown FINAL contract
    (:func:`_parse_sentinel_final`) a cap-cut answer degrades to readable
    markdown, never an unclosed JSON envelope.
    """
    env_val = _env_max_tokens()
    cap = None
    if descriptor_max_tokens is not None:
        try:
            cap = int(descriptor_max_tokens)
        except (TypeError, ValueError):
            cap = None
        if cap is not None and cap <= 0:
            cap = None
    if env_val is not None:
        if cap is not None and env_val != cap:
            logger.warning(
                "consult.max_tokens.ENV_OVERRIDE LEGBA_CONSULT_MAX_TOKENS=%d "
                "OVERRIDES the descriptor-governed output budget "
                "(method.llm.max_tokens=%d) — emergency valve engaged; unset "
                "the env var to restore descriptor governance",
                env_val, cap,
            )
        return env_val
    if cap is not None:
        return cap
    return fallback


def _default_max_tokens() -> int:
    """Legacy default for deps built WITHOUT a descriptor.

    Kept as the :class:`ConsultOnDemandDeps.max_tokens` default_factory so
    hand-built test/embedder deps behave as before (env override → 2048).
    Production deps come from the runtime builder, which passes the
    descriptor's cap through :func:`resolve_output_budget` explicitly.
    """
    return resolve_output_budget(None)


def _default_wall_budget_seconds() -> float:
    """Wall-clock budget for the ReAct tool loop, env-tunable via
    ``LEGBA_CONSULT_WALL_BUDGET_SECONDS``.

    Once the loop has run this long, it STOPS requesting tools and forces the
    final synthesis — so a broad question that would otherwise fan out 10 slow
    LLM rounds RETURNS a real (if less-drilled) answer instead of 504-ing at the
    blocking endpoint's invoke timeout (``DAPR_INVOKE_TIMEOUT_SECONDS`` = 300).
    The first (survey) round always runs. Default 210 leaves ~90s headroom for
    the forced-final synthesis + response assembly under the 300s ceiling.
    """
    raw = os.getenv("LEGBA_CONSULT_WALL_BUDGET_SECONDS", "").strip()
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return 210.0


# ---------------------------------------------------------------------------
# LLM port (mirrors inline_target.LLMHandlerLike — kept local so the kind
# stays import-cheap when other analyst kinds aren't loaded)
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMHandlerLike(Protocol):
    """Minimum slice of ``LLMProviderHandler`` the consult kind depends on.

    Mirrors the structural shape implemented by
    :class:`legba.data.stack.llm.openai.OpenAIProviderHandler` and the
    test-double in ``tests/data_pkg/test_analyst_consult_on_demand.py``.
    """

    subprovider: str

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any: ...



# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------


@dataclass
class AnalystMethodResult:
    """Result of one ``consult_on_demand`` invocation.

    ``finding`` carries the structured response in :attr:`FindingPayload.data`
    so the runtime's ``write_analyst_output`` write path (which assumes
    ``OutputKind.FINDING``) stays untouched.  ``consult_response`` is the
    same payload as a typed :class:`ConsultResponsePayload` for callers
    that bypass the runtime (A2A skill, MCP tool, operator panel).
    """

    finding: FindingPayload
    consult_response: ConsultResponsePayload
    usage: dict[str, int] = field(default_factory=dict)
    derived_from: list[UUID] = field(default_factory=list)
    intermediate_steps: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


from ._tradecraft import with_preamble  # noqa: E402

_SYSTEM_PROMPT = with_preamble(
    """TASK — answer an operator's question over the substrate. You may call tools to gather evidence before answering; each call is a single strict-JSON object.

Available tools:
  - search_signals(query, [limit], [scope_predicate]) — full-text search over indexed signals (title + summary).
  - query_facts([subject], [predicate], [value], [limit], [as_of]) — fact store; at least one of subject/predicate/value is required. as_of (ISO-8601) reads the facts that held on that date, including rows superseded since.
  - inspect_entity(name) — canonical entity profile + recent facts.
  - vector_search(query, [limit]) — semantic similarity over signal embeddings.
  - search_context(query, [corpus], [country], [k]) — semantic search over the CURATED reference corpora (world_context = country/topic priors + doctrine summaries; tradecraft = analytic standards / SAT handbooks). Returns cited chunks (corpus, doc_id, title, section, countries, source_url, effective_date). corpus narrows to one of world_context / tradecraft; country filters to chunks tagged for that country. This is BACKGROUND / method knowledge, NOT live substrate — use it to ground an assessment or recall a technique, not as current evidence. Its chunk refs are `ctx:`-prefixed and NON-CITABLE: never put a ctx: ref (or its bare UUID) in cited_refs — cite only substrate UUIDs other tools returned.
  - search_corpus(query, [filters], [size]) — LEXICAL BM25 keyword search over the FULL raw text of ALL ingested signals (the live news/report corpus, ~106k docs), returning scored rows. Optional keyword filters narrow by facet: geo, tags, source_id, language, modality, entity_classes, retention_class, license_class (a scalar or a list per key). Use it to FIND source documents by keyword across the whole corpus (broader recall than search_signals' title+summary FTS and complementary to vector_search's semantic match). A row's id is the signal id — pass it to read_document for the full body.
  - read_document(doc_id) — fetch ONE signal's full stored body + metadata by its doc_id (the signal id, e.g. from a search_corpus / search_signals hit) when you need the WHOLE article text, not a snippet. Returns status ('found' / 'not_found') and the full indexed document (title, raw_body, facets).
  - query_nexuses([subject], [object], [rel_type], [polarity], [limit], [as_of]) — open signed/typed relationships (A->[intermediary]->B; polarity +1 supportive / -1 antagonistic / 0 neutral/dual-use). as_of reads the relationships that held on that date.
  - query_hypotheses([target_id], [status], [situation_id], [limit]) — competing-hypothesis (ACH) rows (thesis vs counter_thesis, evidence balance, status: active / confirmed / refuted).
  - get_timeline(subject, [limit], [since], [until]) — time-ordered merge of current facts and recent signals about one subject; since/until (ISO-8601) bound the window on each item's anchor.
  - compare_targets(target_ids) — side-by-side substrate rollup for two or more target ids.
  - query_paths(subject, object, [max_hops<=3], [polarity_product], [limit], [families], [as_of]) — ranked SIGNED paths A->...->B over the open entity graph; each path carries its net polarity_product (the structural-balance sign of the chain: +1 net-supportive / -1 net-antagonistic). polarity_product filters to that net sign. as_of walks the graph as it stood on that date.
  - find_proxy_chains(subject, object, [max_hops<=3], [polarity_product], [limit], [families], [as_of]) — INDIRECT links only (multi-hop chains + reified A->via->B cut-outs); the proxy path from A to B.
  - query_brokers(camp_a, camp_b, [max_hops<=3], [limit], [families], [as_of]) — entities that SIT ON paths between two entity sets (the broker between two camps), ranked by how many A->B paths run through them.

  All three walks traverse ASSERTED relationships only (families relation/reference). A co-mention is not a relationship, so pass families=["cooccurrence"] to walk the co-mention cloud deliberately. An endpoint naming no entity (or an ambiguous one) comes back in `warnings` — an empty result with a warning is NOT "they are unconnected".

Finished intelligence — the platform's OWN analysis (analysis-derived; consult these FIRST, they encode prior work — weigh per the provenance rules above):
  - list_findings([target_id], [analyst_id], [severity], [since_hours], [include_superseded], [believed_as_of], [limit]) — recent LIVE findings the platform already produced (country/world situational assessments, meta-findings; superseded revisions are excluded unless include_superseded=true); effective_confidence folds in the critic's grade. believed_as_of (ISO-8601) reads what the platform had published and not yet superseded on that date, with the verdict it held then. Cite the finding id.
  - list_situations([status], [target_id], [since_hours], [as_of], [limit]) — ongoing clustered situation frames, each with intensity_score + event_count (rank severity by these); call with NO filters (limit 20-30) for a world-state survey. as_of reads the frames that held on that date, including ones since closed. Pass a returned situation_id to query_hypotheses for its ACH rows.
  - query_events([target_id], [geo], [category], [lifecycle_state], [entity], [situation_id], [since], [until], [as_of], [limit]) — bounded real-world occurrences the platform has clustered (a thing that HAPPENED, where a situation is a thing being watched). lifecycle_state is one of emerging/developing/active/evolving/resolved; geo is an ISO2 code or list; entity is an actor-name substring; situation_id returns the events a situation tracks; since/until (ISO-8601) bound the occurrence span by overlap; as_of reads the events that held on that date.
  - inspect_event(event_id) — the one-event dossier: the event row, its ranked evidence signals, its actors with roles, its event edges, the situations tracking it, and its lifecycle ledger oldest→newest. The row ids it returns are citable refs.
  - belief_as_of(as_of, [target_id], [fold_verdicts], [limit]) — the findings Legba had published and not yet superseded on date as_of (ISO-8601, required), each with its own effective_confidence under the verdict fold you name: 'as_of' (default — the verdict held on that date; ungraded-yet rows come back effective_confidence=null and count in verdict_pending_at_as_of) or 'latest' (today's verdict). There is no pooled score by design.
  - query_predictions([target_id], [status], [limit]) — event-volume forecasts (forecast_method 'naive_mean' ⇒ no trend could be fit, low-confidence; 'auto_arima' ⇒ fitted). The feed is FROZEN (writer retired 2026-07-01) — treat rows as historical, check latest_produced_at, never present one as a current forecast. Cite the id.
  - list_targets() — the monitored targets + their ids (e.g. country_g20_ir); call this to resolve a place/topic to a valid target_id before query_hypotheses / compare_targets / list_findings.
  - list_sources([active_only], [silent_only]) — ingest sources + freshness; use to tell "no coverage on X" apart from "a quiet feed".

OUTPUT CONTRACT — there are TWO reply shapes, and which round you are in decides which one you use:
  * A TOOL round (you want more evidence first) is EXACTLY ONE strict-JSON object and NOTHING else: no text before or after it, no markdown code fences, no comments, no trailing commas; all keys and string values in double quotes; the first character is { and the last is }. Any character outside that single JSON object breaks the parser and wastes a round.
  * The FINAL round (you are answering) is NOT JSON. It is the sentinel line <<<FINAL>>>, then a short header block, then your answer written straight out as markdown. Never wrap the answer in a JSON object, never escape it as a JSON string, never put it in ``` fences. This clause OVERRIDES the preamble's output-discipline rule for the final reply only — every other reply is strict JSON as that rule requires.

Loop protocol:
  - To call ONE tool, reply with strict JSON: {"tool": "<name>", "args": {...}}
  - To call SEVERAL INDEPENDENT tools in the SAME round (they run concurrently and cost ONE round, not one per tool), reply with strict JSON: {"tools": [{"tool": "<name>", "args": {...}}, {"tool": "<name>", "args": {...}}]} — up to 5. Batch ONLY tools that do NOT depend on each other's output; a call that needs a prior call's result (e.g. compare_targets after list_targets resolves the ids) must wait for the next round.
  - To finish, reply in EXACTLY this shape:

    <<<FINAL>>>
    uncertainty: 0.35
    cited_refs: 792fd4d7-ff16-4a34-80bf-f1af1a58c14c, d6b35902-1b3f-410f-8c2f-009275454a35
    unanswered_aspects: what the substrate could not tell you; another genuine gap

    ## Bottom line
    ...the rest of your answer, as ordinary GitHub-flavored markdown...

    <<<FINAL>>> is the FIRST line. The three header lines follow, one per line, in any order; drop a header only when it is genuinely empty. cited_refs is a comma-separated list of bare UUIDs and unanswered_aspects is a semicolon-separated list of phrases — plain text, no brackets, no quotes, no JSON. A blank line ends the header block and EVERYTHING after it is your answer, written directly with no quoting and no escaping.

Strategy — SURVEY THEN DRILL:
  - For a BROAD / world-state / open-ended question ("how's the world looking", "what's going on", anything not about a single named entity), your FIRST round MUST survey the platform's own active picture: batch list_situations (NO filters, limit 20-30 — it ranks the live frames by intensity_score and event_count) WITH list_findings, and add query_predictions when forecasts are relevant. An answer to a broad question that never called list_situations is INCOMPLETE — you cannot describe "the world" without first reading the active situation frames.
  - For a NARROW question about one place/topic, go straight to the relevant reader; resolve the place to a target_id with list_targets first if you need one for list_findings / query_hypotheses / compare_targets.
  - In BOTH cases the platform's OWN finished intelligence — list_findings / list_situations (and query_predictions) — comes FIRST; build on it, and use raw search_signals / vector_search to survey broadly, verify, update, or fill gaps, not to re-derive from scratch. THEN drill into the specific entities, facts, or time windows. Prefer two or three cheap wide calls (batched into one round) over one narrow guess. Only finish once you have gathered enough or exhausted the useful calls.
Answer quality: the answer under the header block is plain GitHub-flavored MARKDOWN prose written for a human reader — headings (##), **bold**, bullet lists, `---` rules as needed. LEAD with the bottom-line judgment, then support it with cited substrate (`cited_refs` = the UUIDs you actually used). Set `uncertainty` as 1 minus your calibrated confidence in the answer (high — >= 0.7 — when the substrate lacks the material), and list the parts you could not address in `unanswered_aspects`. Do not invent UUIDs or facts the tools did not return. Because the answer is NOT inside a JSON string there is no escaping to get wrong and no envelope that has to close — spend the room on substance: give the operator the analysis the question deserves rather than a shortened one."""
)


def _render_user_prompt(
    question: str,
    scope_predicate: str | None,
    pinned_block: str = "",
) -> str:
    """Render the consult's first user turn.

    ``pinned_block`` is the rendered ``PINNED CONTEXT`` block (see
    ``legba.data.pinned_context``) and leads the turn so the planner reads the
    operator's pinned records BEFORE the question they qualify. Empty when the
    request carried no pins — which is every pre-``pinned_context`` request,
    and which makes this function byte-identical to its previous form there.
    """
    body = f"Operator question:\n{question.strip()}"
    if scope_predicate:
        body += f"\n\nScope predicate (apply to substrate queries): {scope_predicate}"
    if pinned_block:
        body = f"{pinned_block}\n\n{body}"
    return body


# ---------------------------------------------------------------------------
# Reply parsing — EXTRACTED to ``consult_reply_parsing`` (D-7)
#
# The JSON-envelope reader and the whole §28.4 plain-markdown FINAL contract
# moved out one-way when this module crossed its size ceiling; they are a
# self-contained string-parsing subsystem with no dependency on anything else
# here. Re-exported so every importer (tests included) is unchanged, and so
# the loop below reads exactly as it did.
# ---------------------------------------------------------------------------

from .consult_reply_parsing import (  # noqa: E402
    FINAL_SENTINEL,
    SALVAGE_UNCERTAINTY,
    _extract_json,
    _parse_header_list,
    _parse_round_reply,
    _parse_sentinel_final,
    _sentinel_lead,
    _strip_leading_sentinel,
    _unescape_json_str,
    _unwrap_double_envelope,
    final_payload_from_text,
)

# Tool-result rendering moved to ``consult_tool_rendering`` under the
# module-size ratchet (2026-09-16). Re-exported HERE because that is where
# ``inline_target``, ``journal_assessor`` and the existing tests import these
# from, and an extraction must not become an import-path break for callers who
# had no stake in it. One way: this module imports that one, never the reverse.
from .consult_tool_rendering import (  # noqa: E402
    _BOUNDED_JSON_LIST_KEYS,
    _CITATION_ONLY_KEYS,
    _bounded_tool_json,
    _shed_citation_payload,
    _trim_args,
)


def _coerce_uuid_list(raw: Any) -> list[UUID]:
    if not isinstance(raw, list):
        return []
    out: list[UUID] = []
    for item in raw:
        try:
            out.append(UUID(str(item)))
        except (ValueError, AttributeError):
            continue
    return out


def _merge_refs(*lists: list[UUID]) -> list[UUID]:
    """Order-preserving dedupe across multiple ref lists."""
    seen: set[UUID] = set()
    out: list[UUID] = []
    for lst in lists:
        for ref in lst:
            if ref not in seen:
                seen.add(ref)
                out.append(ref)
    return out


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------


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
    "query_predictions",
    "list_targets",
    "list_sources",
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


async def _dispatch_tool(
    port: SubstrateQueryPort,
    *,
    name: str,
    args: Mapping[str, Any],
    scope_predicate: str | None,
) -> dict[str, Any]:
    """Invoke a whitelisted tool by name.

    Unknown tool names are surfaced as a structured error so the planner
    sees the failure and can recover (rather than crashing the run).
    """
    if name not in _KNOWN_TOOLS:
        return {"error": f"unknown_tool: {name!r}"}
    try:
        if name == "search_signals":
            return await port.search_signals(
                query=str(args.get("query", "")),
                limit=int(args.get("limit", 20)),
                scope_predicate=scope_predicate,
            )
        if name == "query_facts":
            return await port.query_facts(
                subject=args.get("subject"),
                predicate=args.get("predicate"),
                value=args.get("value"),
                limit=int(args.get("limit", 30)),
                as_of=args.get("as_of"),
            )
        if name == "inspect_entity":
            return await port.inspect_entity(name=str(args.get("name", "")))
        if name == "vector_search":
            return await port.vector_search(
                query=str(args.get("query", "")),
                limit=int(args.get("limit", 10)),
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
            )
        if name == "read_document":
            return await port.read_document(
                doc_id=str(args.get("doc_id", "")),
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
                limit=int(args.get("limit", 20)),
                believed_as_of=args.get("believed_as_of"),
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
            )
        if name == "inspect_event":
            return await port.inspect_event(
                event_id=str(args.get("event_id", "")),
            )
        if name == "query_predictions":
            return await port.query_predictions(
                target_id=args.get("target_id"),
                status=args.get("status"),
                limit=int(args.get("limit", 20)),
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
# Result coercion
# ---------------------------------------------------------------------------


def _build_consult_response(
    *,
    question: str,
    final_payload: dict[str, Any] | None,
    collected_refs: list[UUID],
    rounds_used: int,
    forced_final: bool,
    subprovider: str | None,
    extra_data: Mapping[str, Any] | None = None,
) -> ConsultResponsePayload:
    """Build the typed :class:`ConsultResponsePayload`.

    Defensive against the LLM returning malformed final-JSON: we fall
    back to a high-uncertainty empty answer with the original question
    in :attr:`ConsultResponsePayload.unanswered_aspects`.

    ``extra_data`` merges into the payload's ``data`` bag. It carries the
    run's SPEND and its SYNTHESIS STATUS — the two facts an operator needs
    about a run that cost money and may not have finished, and the two the
    front door reads to decide whether to offer "Synthesize from evidence".
    """
    extra = dict(extra_data or {})
    if not final_payload:
        return ConsultResponsePayload(
            question=question,
            answer="",
            cited_substrate_refs=collected_refs,
            uncertainty=1.0,
            unanswered_aspects=[question],
            data={
                "rounds_used": rounds_used,
                "forced_final": forced_final,
                "subprovider": subprovider,
                "error": "no_final_payload",
                **extra,
            },
        )

    # Repair the double-wrap (LEGACY JSON finals): some planner turns nest a
    # whole {"final":...} envelope INSIDE the answer string. Lift the inner
    # prose so the UI renders clean markdown instead of a raw JSON block
    # (degrades to the original on any doubt). A sentinel final's answer is
    # already markdown and passes through untouched; this covers the normal
    # final, the forced-final, and replayed sessions — all funnel through here.
    answer = _unwrap_double_envelope(str(final_payload.get("answer") or ""))[:65000]
    raw_uncertainty = final_payload.get("uncertainty", 0.5)
    try:
        uncertainty = float(raw_uncertainty)
    except (TypeError, ValueError):
        uncertainty = 0.5
    uncertainty = max(0.0, min(1.0, uncertainty))

    llm_refs = _coerce_uuid_list(final_payload.get("cited_refs"))
    # The collected-from-tools refs are the authoritative set; we trust
    # those over LLM-emitted ones (planner could hallucinate UUIDs).
    # The LLM's cited_refs are kept but filtered to those we actually
    # observed so the planner can narrow but not invent.
    collected_set = set(collected_refs)
    confirmed_llm_refs = [r for r in llm_refs if r in collected_set]
    # Prefer the LLM-confirmed subset (the planner's narrowing); fall
    # back to the full collected set when the planner didn't narrow.
    cited = confirmed_llm_refs if confirmed_llm_refs else list(collected_refs)

    unanswered_raw = final_payload.get("unanswered_aspects") or []
    if not isinstance(unanswered_raw, list):
        unanswered_raw = [str(unanswered_raw)]
    unanswered = [str(u)[:512] for u in unanswered_raw][:20]

    # If the LLM signaled high uncertainty but left unanswered_aspects
    # empty, surface the question itself as the unaddressed aspect so
    # downstream surfaces (panel, A2A response) always show *something*.
    if uncertainty >= 0.7 and not unanswered:
        unanswered = [question[:512]]

    return ConsultResponsePayload(
        question=question,
        answer=answer,
        cited_substrate_refs=cited,
        uncertainty=uncertainty,
        unanswered_aspects=unanswered,
        data={
            "rounds_used": rounds_used,
            "forced_final": forced_final,
            "subprovider": subprovider,
            **extra,
        },
    )


def _wrap_as_finding(
    consult: ConsultResponsePayload,
    *,
    analyst_id: str | None,
) -> FindingPayload:
    """Project the consult response into a ``FindingPayload`` carrier.

    Lifts ``answer`` into the body so the substrate row reads sensibly
    even when consumers only know the FINDING shape.  The structured
    payload sits in ``data["consult_response"]`` for typed consumers.
    """
    title = f"Consult: {consult.question}"[:2048]
    body = consult.answer or "(no answer produced)"
    # Confidence here mirrors (1.0 - uncertainty), capped at the
    # finding schema's [0, 1].  Operators reading findings get a sane
    # confidence dimension; the structured uncertainty stays in `data`.
    confidence = max(0.0, min(1.0, 1.0 - consult.uncertainty))
    tags = ["consult_on_demand"]
    if analyst_id:
        tags.append(f"analyst:{analyst_id}")
    if consult.unanswered_aspects:
        tags.append("has_unanswered")
    return FindingPayload(
        title=title,
        body=body[:65000],
        confidence=confidence,
        evidence=[str(ref) for ref in consult.cited_substrate_refs][:50],
        tags=tags[:50],
        data={"consult_response": consult.model_dump(mode="json")},
    )


# ---------------------------------------------------------------------------
# ReAct loop
# ---------------------------------------------------------------------------


async def _reason_via_llm(
    llm: LLMHandlerLike,
    *,
    messages: list[Mapping[str, Any]],
    max_tokens: int,
    temperature: float,
    system_prompt: str,
) -> tuple[str, dict[str, int]]:
    """One chat_complete turn.  Mirrors inline_target's helper.

    UNCHANGED, deliberately: ``deep_consult``'s plan + extract stages import
    this by name and unpack two values. The native route needs a third (the
    raw response, for its ``tool_calls``), so it calls
    :func:`_reason_with_response` instead and this stays a thin projection of
    it — one request path, two return shapes, no duplicated call site.
    """
    content, usage, _response = await _reason_with_response(
        llm,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        system_prompt=system_prompt,
    )
    return content, usage


async def _reason_with_response(
    llm: LLMHandlerLike,
    *,
    messages: list[Mapping[str, Any]],
    max_tokens: int,
    temperature: float,
    system_prompt: str,
    tools: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, int], Any]:
    """One chat_complete turn, returning ``(content, usage, response)``.

    The raw response is surfaced because the native tool-call route needs its
    ``tool_calls``, which by definition are NOT in ``content`` — that is the
    whole point of the structured channel.

    ``tools`` is threaded ONLY when non-empty, so every existing caller (and
    the text-protocol route) produces a request byte-identical to the one it
    produced before this parameter existed. No shared provider handler was
    changed to support this: ``LLMProviderHandler.chat_complete`` has always
    taken ``tools``, and both handlers we drive already translate it.
    """
    kwargs: dict[str, Any] = {}
    if tools:
        kwargs["tools"] = tools
    response = await llm.chat_complete(
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_prompt,
        **kwargs,
    )
    content = getattr(response, "content", "") or ""
    usage_raw = getattr(response, "usage", None)
    usage_dict = {
        "prompt_tokens": getattr(usage_raw, "prompt_tokens", 0) if usage_raw else 0,
        "completion_tokens": (
            getattr(usage_raw, "completion_tokens", 0) if usage_raw else 0
        ),
        "reasoning_tokens": (
            getattr(usage_raw, "reasoning_tokens", 0) if usage_raw else 0
        ),
    }
    return content, usage_dict, response


def _refs_from_tool_result(tool_result: Mapping[str, Any]) -> list[UUID]:
    """Lift any substrate UUIDs out of a tool's JSON response."""
    raw = tool_result.get("refs") if isinstance(tool_result, Mapping) else None
    return _coerce_uuid_list(raw or [])


# ---------------------------------------------------------------------------
# Deps + public entry
# ---------------------------------------------------------------------------


@dataclass
class ConsultOnDemandDeps:
    """Bundle the runtime passes to ``run_method``.

    The runtime resolves ``llm`` from the analyst descriptor's
    ``method.llm.primary`` StackRef + budget block + cadence block.
    ``substrate`` is wired by the runtime from the actor's
    ``deps.pg_pool`` (and ``deps.extras["vector_store"]`` for the
    vector_search tool) at activate time.
    """

    llm: LLMHandlerLike
    substrate: SubstrateQueryPort
    # F1 model picker: an OPTIONAL allowlist-bound by-id LLM resolver the runtime
    # threads at deps-build. When a request carries ``llm_component_override``,
    # ``run_method`` calls this to build a FRESH handler for THAT plane for the
    # one request, instead of the cached ``llm`` (the ACTIVATE-time primary =
    # Opus default). None (hand-built test/embedder deps) ⇒ the override is
    # ignored and the cached primary is used, unchanged. The resolver only
    # resolves the sanctioned planes (see LLM_OVERRIDE_ALLOWLIST); it raises
    # otherwise, and run_method degrades to the cached primary on any failure.
    resolve_llm_component: (
        Callable[[str], Awaitable[LLMHandlerLike]] | None
    ) = None
    # Per-LLM-call output budget. DESCRIPTOR-governed in production: the
    # runtime builder passes method.llm.max_tokens through
    # resolve_output_budget() (LEGBA_CONSULT_MAX_TOKENS demoted to an
    # emergency override, logged loudly when it wins). The default_factory
    # only serves hand-built test/embedder deps.
    max_tokens: int = field(default_factory=_default_max_tokens)
    # Wall-clock budget for the tool loop — env-tunable, see
    # _default_wall_budget_seconds(). Caps over-drilling so broad questions
    # return before the blocking endpoint's invoke timeout instead of 504-ing.
    wall_budget_seconds: float = field(default_factory=_default_wall_budget_seconds)
    # HARD ceiling on the whole run, forced-final synthesis included. The
    # drilling budget above only stops the loop asking for MORE tools; it never
    # bounded the synthesis that follows, which is where run 3ae77c64 ran out
    # of the front door's clock. Past this the loop emits a degraded-but-honest
    # FINAL rather than dying. Env ``LEGBA_CONSULT_BUDGET_SECONDS``.
    total_budget_seconds: float = field(
        default_factory=_cp.default_total_budget_seconds
    )
    # Per-LLM-call deadline, so one stuck call can't consume the whole budget
    # while every other round starves. Env ``LEGBA_CONSULT_ROUND_DEADLINE_SECONDS``.
    round_deadline_seconds: float = field(
        default_factory=_cp.default_round_deadline_seconds
    )
    # DEAD KNOB on the deployed plane — comment truth, not aspiration. The
    # ACTIVATE-time primary is `llm.anthropic.opus_4_7`, whose live model_name
    # is claude-opus-4-8, and Anthropic deprecated `temperature` on that line:
    # the handler matches TEMPERATURE_DEPRECATED_PREFIXES and drops it before
    # the wire (it now says so once per session in the runtime log). This value
    # therefore only takes effect on a plane that still accepts the parameter —
    # today that is the `"core"` picker override (llm.primary.openai_compat).
    # Do not tune consult determinism here expecting it to reach Opus.
    temperature: float = 0.2
    system_prompt: str = _SYSTEM_PROMPT
    max_rounds: int = MAX_TOOL_ROUNDS
    # Per-round native call width. None ⇒ ``_cp.native_batch_cap()`` (4, env
    # ``LEGBA_CONSULT_NATIVE_BATCH_CAP``). A descriptor whose tool mix genuinely
    # needs five wide reads in one round raises it here; everything else gets
    # the tighter default, because on the native route a round's results ride
    # in EVERY later prompt.
    native_batch_cap: int | None = None
    # Per-run spend ceilings — the budget denominated in money rather than
    # seconds. See ``consult_spend_guard``: run c8a0105c honoured all three
    # clocks and still cost ~$10, because what made it expensive was the
    # transcript it replayed, which no clock can see. None ⇒ the env-tunable
    # defaults (150k input tokens / $3.00 estimated).
    max_input_tokens_per_run: int | None = None
    max_cost_usd_per_run: float | None = None
    # A-3a (review G2): when the runtime wires an AgencyToolBinding for the
    # ``substrate_read`` pack, EVERY tool call routes through
    # ``Agency.run_pack_tool`` — resolve ∩ allow ∩ applicability, the pack
    # governor, and the ``action_pack_invocations`` ledger — instead of
    # dispatching straight at the port. None = direct port dispatch (kept
    # for tests and non-runtime embedders that construct deps by hand; the
    # production deps builder ALWAYS binds it and fails loud if it can't).
    agency_binding: Any | None = None
    # Per-run step telemetry sink (Piece 1, D5). When wired by the actor for a
    # streaming consult run, EVERY trace step recorded during the ReAct loop is
    # also pushed here so the live SSE stream and the durable trace are ONE
    # source of truth. None = no streaming (the trace is still built as today).
    # Never let a publish failure break the run (the emitter swallows).
    step_publish: Callable[[dict[str, Any]], Awaitable[None]] | None = None


async def run_method(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: ConsultOnDemandDeps,
) -> AnalystMethodResult:
    """Execute one consult_on_demand run.

    ``inputs[0]`` MUST carry ``question`` (NL string).  ``scope_predicate``
    is optional (string-form Starlark predicate per L-104).  Both flow
    into the ReAct loop's first turn.

    The function is a single-turn ReAct loop with a ``MAX_TOOL_ROUNDS``
    cap.  Each round the LLM emits either a tool-call or a final-JSON;
    after the cap we force one last turn with the tool surface withheld
    so the planner is forced to synthesize whatever it has.

    Errors at the LLM boundary propagate (the runtime classifies them
    per ``kind_contracts §7``).  Tool errors are folded into the
    conversation so the planner can recover.
    """
    if not inputs:
        raise ValueError("consult_on_demand requires inputs[0] with 'question'")
    first = inputs[0]
    if not isinstance(first, Mapping) or "question" not in first:
        raise ValueError("consult_on_demand requires inputs[0]['question']")
    question = str(first["question"]).strip()
    if not question:
        raise ValueError("consult_on_demand 'question' must be non-empty")
    scope_predicate = first.get("scope_predicate")
    if scope_predicate is not None:
        scope_predicate = str(scope_predicate)

    analyst_id = options.get("analyst_id")

    # --- F1 model picker: optional per-request LLM plane override ------
    # The registry front door maps the operator's friendly choice ("opus"/"core")
    # to a sanctioned stack-component id and threads it on the question row as
    # ``llm_component_override``. Resolve THAT component fresh for this run — the
    # cached ``deps.llm`` is the ACTIVATE-time primary (Opus default), shared
    # across concurrent runs, so we never mutate it. Absent / None ⇒ the cached
    # primary, unchanged (today's behavior).
    #
    # FAIL CLOSED (F-A): when an override IS present but cannot be honored we
    # RAISE rather than falling back to the cached primary. A silent fallback
    # would BILL Opus while the front door echoes the requested plane and the
    # actor's budget re-keys to the chosen ($0) plane — a silent cost + an
    # honesty lie. We raise ``ValueError`` (the module's existing hard-error
    # type → classified "hard" by the actor → surfaced as outcome!="success");
    # the consult front door then renders an actionable provider-error message
    # (H4a). The deep path already fails closed by construction (an unresolvable
    # ``llm_component_id`` errors when the workflow stage deps build).
    active_llm = deps.llm
    override_component = first.get("llm_component_override")
    if override_component:
        if deps.resolve_llm_component is None:
            raise ValueError(
                f"llm plane override {override_component!r} requested but no "
                f"by-id resolver is wired for this analyst"
            )
        try:
            active_llm = await deps.resolve_llm_component(str(override_component))
        except Exception as exc:  # noqa: BLE001 — fail closed, never bill Opus
            raise ValueError(
                f"requested llm plane {override_component!r} is unavailable: {exc}"
            ) from exc
        logger.info(
            "consult_on_demand.llm_override.active override=%s subprovider=%s",
            override_component, getattr(active_llm, "subprovider", None),
        )

    # --- Round protocol selection (D-7) --------------------------------
    # On a plane with a real tool-calling channel we drive it instead of
    # parsing tool calls out of prose. The `unparseable` step kind — a burned
    # round and a burned Opus call — cannot occur on that route, because a
    # reply either carries tool_use blocks (a tool round) or does not (the
    # answer). Everything else, including the §28.4 markdown FINAL, is
    # unchanged. See ``consult_round_protocol``.
    native_tools_route = _cp.supports_native_tools(active_llm)
    native_wire = _cp.native_wire(active_llm)
    native_tool_payload = (
        _cp.render_tools_for(native_wire, _KNOWN_TOOLS)
        if native_tools_route
        else None
    )
    effective_system_prompt = deps.system_prompt + (
        _cp.native_system_suffix(FINAL_SENTINEL) if native_tools_route else ""
    )

    # --- Step trace + live telemetry (Piece 1, D5) --------------------
    # ``steps`` is the durable trace returned as ``intermediate_steps``.
    # ``_record`` appends to it AND pushes the same dict to ``deps.step_publish``
    # when wired, so the live SSE stream and the trace are one source of truth.
    steps: list[dict[str, Any]] = []

    # STREAM-ONLY frames go through ``_emit_step``; everything else through
    # ``_record``, which does both. There is exactly ONE stream-only kind —
    # ``answer_delta``, the live synthesis text — and it is stream-only for a
    # reason that does not generalise: its frames re-carry the whole answer in
    # 400-character pieces, and persisting them would put a second, chunked
    # copy of a 13,000-character answer into the turn's step trace beside the
    # answer itself. Every other frame is cheap, bounded, and belongs in both.
    async def _emit_step(step: dict[str, Any]) -> None:
        if deps.step_publish is not None:
            try:
                await deps.step_publish(step)
            except Exception:  # never let telemetry break the run
                logger.debug(
                    "consult_on_demand.step_publish.failed", exc_info=True
                )

    async def _record(step: dict[str, Any]) -> None:
        steps.append(step)
        await _emit_step(step)

    # --- Recovery: finish a run that was cut, without drilling again -------
    # ``synthesize_from`` carries a PERSISTED turn's evidence. The whole ReAct
    # loop below is skipped: this path writes an answer over evidence that
    # already exists and was already paid for. It lives in the analyst rather
    # than the front door because the tools, the governed binding and the model
    # planes are all here, and it arrives on the request row so the actor
    # surface is unchanged.
    recovery = first.get("synthesize_from")
    if recovery:
        from . import consult_resynthesis as _rs

        return await _rs.run_synthesis_only(
            recovery,
            deps=deps,
            active_llm=active_llm,
            wire=native_wire,
            system_prompt=effective_system_prompt,
            record=_record,
            analyst_id=analyst_id,
        )

    # --- Effective round count (Piece 1, D1; re-defaulted after c8a0105c) ---
    # The per-run override may arrive on the question row or in ``options``;
    # clamp it to [1, ROUNDS_CEILING] and use a LOCAL — never mutate ``deps``
    # (shared across concurrent runs).
    #
    # WHERE 10 CAME FROM. ``MAX_TOOL_ROUNDS`` is 6 and always was, and
    # ``deps.max_rounds`` defaults to it. What sent run c8a0105c to ten rounds
    # on a Fable-priced route was the chat front door's REQUEST MODEL, which
    # defaulted ``max_tool_rounds`` to 10 (``CHAT_DEFAULT_ROUNDS``) — so every
    # chat request arrived carrying an explicit 10 and the kind's own default
    # was unreachable from the panel. The front door now sends ``None`` when
    # the operator did not choose a value, and "did not choose" lands here as
    # ``deps.max_rounds``: 6, the descriptor's number.
    #
    # An explicit request still wins, up to ROUNDS_CEILING — a broad survey is
    # a legitimate thing to ask for, as long as asking is deliberate.
    requested = first.get("max_tool_rounds")
    if requested is None:
        requested = options.get("max_tool_rounds")
    rounds_source = "request"
    if requested is None:
        rounds_source = "default"
        effective_rounds = deps.max_rounds
    else:
        try:
            effective_rounds = int(requested)
        except (TypeError, ValueError):
            rounds_source = "default_malformed_request"
            effective_rounds = deps.max_rounds
    effective_rounds = max(1, min(ROUNDS_CEILING, effective_rounds))

    # --- Per-run spend ceiling ----------------------------------------
    # Per RUN, never on ``deps`` (shared across concurrent runs).
    spend = SpendGuard(
        **{
            k: v
            for k, v in (
                ("max_input_tokens", deps.max_input_tokens_per_run),
                ("max_cost_usd", deps.max_cost_usd_per_run),
            )
            if v is not None
        }
    )
    batch_cap = _cp.native_batch_cap(deps.native_batch_cap)

    def _normalize_round_calls(parsed: Mapping[str, Any]) -> list[dict[str, Any]]:
        """``_normalize_calls`` at THIS run's per-round width."""
        return _normalize_calls(parsed, cap=batch_cap)

    # The run's FIRST frame now carries the effective caps, not just the prompt
    # shape: an operator watching a run start can see what it is allowed to
    # spend BEFORE it spends it. ``rounds_source`` says whether the cap is the
    # operator's own choice or the plane's default, which is the question
    # c8a0105c could not answer from its trace.
    await _record(
        {
            "phase": "plan",
            "kind": "render_prompt",
            "question_chars": len(question),
            "scope_predicate": bool(scope_predicate),
            "prompt_module": PROMPT_MODULE_PATH,
            "round_protocol": "native_tools" if native_tools_route else "json_text",
            "max_rounds": effective_rounds,
            "rounds_source": rounds_source,
            "native_batch_cap": batch_cap,
            "round_result_bytes": _tc.round_result_bound(),
            "total_budget_s": deps.total_budget_seconds,
            "final_floor_s": _cp.default_final_floor_seconds(),
            **spend.snapshot(),
        }
    )

    # --- Initial conversation -----------------------------------------
    # Seed with prior turns (multi-turn, D6): the request row may carry a
    # client-held transcript. Filter to user/assistant roles, clamp each
    # message body, and keep only the most recent turns to bound prompt size.
    prior = first.get("messages") or []
    seeded: list[Mapping[str, Any]] = []
    if isinstance(prior, list):
        for m in prior:
            if isinstance(m, Mapping) and m.get("role") in {"user", "assistant"}:
                seeded.append(
                    {
                        "role": str(m["role"]),
                        "content": str(m.get("content", ""))[:16000],
                    }
                )
    seeded = seeded[-20:]
    # Records the operator pinned to the conversation (registry-validated, but
    # this arrives as raw JSON off the actor queue so the renderer re-clamps).
    pinned_block = render_pinned_context_block(first.get("pinned_context") or [])
    if pinned_block:
        logger.info(
            "consult.pinned_context analyst_id=%s chars=%d",
            analyst_id, len(pinned_block),
        )
    user_prompt = _render_user_prompt(question, scope_predicate, pinned_block)
    messages: list[Mapping[str, Any]] = [
        *seeded,
        {"role": "user", "content": user_prompt},
    ]

    collected_refs: list[UUID] = []
    aggregate_usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}

    final_payload: dict[str, Any] | None = None
    forced_final = False
    last_raw: str = ""
    #: Set when a round's tool results have been appended but not yet fed to an
    #: LLM call. If the loop degrades while this is set, the honest FINAL says
    #: the last tool result was never incorporated.
    pending_tool_results = False
    #: Why the loop stopped drilling, for the degraded FINAL's prose.
    stop_reason = "the round budget was reached"

    #: Whether the answer this run returns is a finished synthesis
    #: ("complete"), the prefix of one that was cut ("partial"), or an honest
    #: apology because none was produced ("none"). Set at each terminal branch
    #: rather than re-derived downstream from an uncertainty value or a step
    #: kind — this is a fact the loop knows and nobody else should have to
    #: reconstruct.
    synthesis_status = "complete"
    #: The synthesis request as sent, recorded so a CUT run can be finished
    #: later over the same prompt rather than a summary of it. Only set once
    #: the forced-final block builds it, and only PERSISTED when the run turns
    #: out to need recovery — a run that answered has nothing to replay.
    replay_transcript: dict[str, Any] | None = None
    rounds_used = 0
    loop_started = time.monotonic()

    def _elapsed() -> float:
        return time.monotonic() - loop_started

    def _remaining_total() -> float:
        return deps.total_budget_seconds - _elapsed()

    for round_idx in range(effective_rounds):
        # Wall-clock guard: once we've spent the budget, stop drilling and fall
        # through to the forced-final synthesis so a broad question RETURNS a
        # real answer before the blocking endpoint's invoke timeout instead of
        # 504-ing. ``round_idx > 0`` so the first (survey) round always runs.
        if round_idx > 0 and _elapsed() > deps.wall_budget_seconds:
            stop_reason = "the tool-drilling budget was spent"
            await _record({
                "phase": "reflect",
                "kind": "wall_budget_reached",
                "round": round_idx,
                "elapsed_s": round(_elapsed(), 1),
            })
            break
        # TOTAL-budget guard (D-7). The drilling budget above says "stop asking
        # for more tools"; this one says "there is no longer room to SYNTHESISE
        # what you have". Reserving the final's cost up front is what turns a
        # timeout mid-answer into a shorter answer that actually arrives.
        if round_idx > 0 and _remaining_total() <= _cp.FINAL_RESERVE_SECONDS:
            stop_reason = "the total time budget was nearly spent"
            await _record({
                "phase": "reflect",
                "kind": "total_budget_reserve_reached",
                "round": round_idx,
                "elapsed_s": round(_elapsed(), 1),
                "remaining_s": round(_remaining_total(), 1),
            })
            break
        # SPEND guard. Checked before the call, so the ceiling is never
        # breached by the round that discovers it — the point is not to spend
        # the money. This is the guard that would have ended c8a0105c around
        # round 5 instead of round 10, with the same answer and a fifth of the
        # bill; every clock in the loop passed that run.
        spent_reason = spend.exhausted_reason()
        if round_idx > 0 and spent_reason is not None:
            stop_reason = spent_reason
            await _record({
                "phase": "reflect",
                "kind": "spend_ceiling_reached",
                "round": round_idx,
                "reason": spent_reason,
                **spend.snapshot(),
            })
            break
        rounds_used = round_idx + 1
        round_budget = max(1.0, min(deps.round_deadline_seconds, _remaining_total()))
        try:
            content, usage, llm_response = await asyncio.wait_for(
                _reason_with_response(
                    active_llm,
                    messages=messages,
                    max_tokens=deps.max_tokens,
                    temperature=deps.temperature,
                    system_prompt=effective_system_prompt,
                    tools=native_tool_payload,
                ),
                timeout=round_budget,
            )
        except asyncio.TimeoutError:
            # A single stuck call must not consume the run. Record it and fall
            # through to the forced final with whatever the prior rounds got.
            stop_reason = (
                f"round {rounds_used} exceeded its {round_budget:.0f}s deadline"
            )
            await _record({
                "phase": "reason",
                "kind": "round_deadline_exceeded",
                "round": rounds_used,
                "deadline_s": round(round_budget, 1),
            })
            rounds_used = max(0, rounds_used - 1)
            break
        except Exception:
            await _record({"phase": "reason", "kind": "llm_error", "round": rounds_used})
            raise
        pending_tool_results = False
        last_raw = content

        for k in aggregate_usage:
            aggregate_usage[k] += usage.get(k, 0)
        spend.record(active_llm, llm_response, usage)
        # The running spend rides ON the existing per-round frame rather than
        # in one of its own: the panel gets a live figure every round, the
        # trace gains the per-round cost history the c8a0105c post-mortem could
        # not reconstruct, and the ReAct phase accounting is untouched — a
        # separate frame would have doubled every "reason" count in the trace.
        await _record({
            "phase": "reason",
            "kind": "llm_call",
            "round": rounds_used,
            "tokens": usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0),
            "usage": spend.snapshot(),
        })

        # --- Round protocol: native vs JSON-in-text (D-7) --------------
        # On the native route the reply is unambiguous by construction: tool
        # calls came back in their own field, or they didn't and this is the
        # answer. There is no third outcome, so the `unparseable` branch below
        # is unreachable there — which is the point.
        native_round: _cp.NativeRound | None = None
        if native_tools_route:
            native_round = _cp.parse_native_reply(
                llm_response,
                provider=native_wire,
                normalize_calls=_normalize_round_calls,
            )
            if native_round.is_final:
                salvaged = _cp.text_tool_round_fallback(
                    content, normalize_calls=_normalize_round_calls,
                )
                if salvaged:
                    # The handler took `tools` and the model answered in the
                    # old JSON anyway. Treat it as the tool round it is rather
                    # than persisting JSON as an answer.
                    await _record({
                        "phase": "reflect",
                        "kind": "native_text_tool_fallback",
                        "round": rounds_used,
                        "calls": len(salvaged),
                    })
                    native_round = _cp.synthetic_round(salvaged)
                    parsed = {"tools": salvaged}
                else:
                    # No tool call ⇒ the text IS the answer. Read its metadata
                    # if it carried the sentinel header block (or, for a
                    # replayed session, the legacy JSON final), and otherwise
                    # take the prose as-is rather than burning a round asking
                    # for a re-format: the headers are metadata, the prose is
                    # the product.
                    #
                    # Deliberately NOT ``_parse_round_reply`` here. That helper
                    # also recognises a TOOL-call JSON object, and on this route
                    # a bare tool object in the text is handled above — letting
                    # it through would produce a "final" with no answer in it.
                    #
                    # ONE builder (review defect 4). This arm used to inline its
                    # own ``uncertainty: 0.6`` fallback, and the forced-final arm
                    # below inlined a second copy — so a reply the parser bounced
                    # got a DEFAULT uncertainty in the header over an answer that
                    # stated the model's own. ``final_payload_from_text`` owns the
                    # ladder and the single default.
                    parsed = final_payload_from_text(content)
            else:
                parsed = {"tools": list(native_round.batch)}
        else:
            parsed = _parse_round_reply(content)
        if not parsed:
            # Planner produced unparseable output — feed the parse-error
            # back so it can recover.  Cheaper than aborting.
            await _record({
                "phase": "reflect",
                "kind": "unparseable",
                "round": rounds_used,
                # Persist the RAW unparseable reply so a malformed turn leaves a
                # debuggable trail in the durable step trace (→
                # consult_turns.steps) instead of silently vanishing.
                "raw": (content or "")[:4000],
            })
            messages = messages + [
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": (
                        "Your reply matched neither reply shape. To CALL "
                        "TOOLS, respond with ONLY a strict-JSON object "
                        '({"tool": ..., "args": ...} or {"tools": [...]}). '
                        "To ANSWER, do not use JSON at all — reply with the "
                        f"line {FINAL_SENTINEL}, then the uncertainty / "
                        "cited_refs / unanswered_aspects header lines, then "
                        "your answer as plain markdown. If your last reply "
                        "WAS the answer, re-send it in that form: the prose "
                        "does not need to be shortened or escaped."
                    ),
                },
            ]
            continue

        if parsed.get("final") is True:
            final_payload = parsed
            await _record({
                "phase": "reflect",
                "kind": "final",
                "round": rounds_used,
            })
            break

        # Normalize the single {"tool": ...} and batch {"tools": [...]} shapes
        # into one list of independent calls. A batch runs CONCURRENTLY and
        # counts as ONE round (the latency lever — an N-tool survey costs one
        # round-trip, not N), directly attacking the long-loop 504.
        # Flatten the round's calls, and on the native route remember how many
        # belong to each emitted block: every tool_use id MUST get exactly one
        # result back, so the batch cap has to be applied with the block
        # boundaries still visible.
        if native_round is not None:
            # ``_normalize_calls`` already applied the batch cap and the dedupe
            # when the round was parsed, so this IS the executable list.
            calls = list(native_round.batch)
        else:
            calls = _normalize_calls(parsed)
        if not calls and native_round is None:
            # Neither a tool/tools call nor a final payload — prompt for a
            # corrected reply.
            await _record({
                "phase": "reflect",
                "kind": "missing_tool_or_final",
                "round": rounds_used,
            })
            messages = messages + [
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": (
                        "Your JSON had neither `tool` nor `tools`. To call "
                        'tools emit {"tool": ..., "args": ...} or '
                        '{"tools": [{"tool": ..., "args": ...}, ...]}. To '
                        f"answer, emit the {FINAL_SENTINEL} line + header "
                        "lines + markdown instead — the final answer is NOT "
                        "JSON."
                    ),
                },
            ]
            continue

        # Execute every call in the round concurrently. Each routes through the
        # SAME hard-gate (governed binding) or direct dispatch as a single call,
        # with scope_predicate caller-injected, and NEVER raises — a sibling's
        # failure folds into its own tool_result so the batch survives.
        #
        # Governor note (best-effort under batching): the pack's per-minute /
        # per-hour invocation caps are enforced by a check-then-record step
        # inside each run_tool. Firing up to MAX_TOOLS_PER_BATCH calls
        # concurrently means the siblings can evaluate that check against the
        # same pre-batch ledger and transiently OVERSHOOT a rate cap by up to
        # MAX_TOOLS_PER_BATCH-1 before any record lands. Acceptable here: these
        # are read-only, zero-cost substrate reads, the overshoot is small and
        # bounded, and the next round sees every recorded row. An atomic
        # reserve (shared txn / advisory lock around the gate) is the correct
        # hardening but lives in the shared agency/governor layer — tracked as a
        # follow-up, not blocking this read-only path.
        results = await asyncio.gather(*[
            _run_one_call(
                deps,
                tool_name=call["tool"],
                tool_args=call["args"],
                scope_predicate=scope_predicate,
                analyst_id=analyst_id,
            )
            for call in calls
        ])

        # Render this round's bodies under ONE shared bound instead of a
        # per-tool one. The per-tool 8 KB bound multiplied by the batch width:
        # five calls could put 40 KB into the transcript, and the transcript is
        # replayed on every later round. The round bound (16 KB) is allocated
        # max-min fair, so a small result is never truncated to make room for a
        # large sibling — the cut lands on the payloads that hold the tokens.
        round_bodies = _tc.allocate_round_bodies(
            [r[0] for r in results], render=_bounded_tool_json,
        )

        # Coalesce: record each call (with a compact result summary for the
        # trace), lift refs, and append one "tool"-role message per call before
        # the next round. The "tool" role follows OpenAI's tool-use convention;
        # vLLM passes it through as a system-of-record message.
        tool_messages: list[Mapping[str, Any]] = []
        for idx, ((tool_result, meta), call) in enumerate(zip(results, calls)):
            new_refs = _refs_from_tool_result(tool_result)
            if new_refs:
                collected_refs = _merge_refs(collected_refs, new_refs)
            ok = "error" not in tool_result
            act_step: dict[str, Any] = {
                "phase": "act",
                "kind": "tool_call",
                "round": rounds_used,
                "tool": call["tool"],
                "args": _trim_args(call["args"]),
                "governed": meta["governed"],
                "ok": ok,
                "result": (
                    tool_result.get("error")
                    if not ok
                    else {
                        # Most tools return a "count"; the rest carry "rows" or
                        # (get_timeline) "items" — fall back so the trace count
                        # isn't null for the common readers.
                        "count": (
                            tool_result.get("count")
                            if tool_result.get("count") is not None
                            else len(
                                tool_result.get("rows")
                                or tool_result.get("items")
                                or []
                            )
                        ),
                        "refs": len(new_refs),
                    }
                ),
            }
            if meta.get("admitted") is not None:
                act_step["admitted"] = meta["admitted"]
            await _record(act_step)
            tool_messages.append({
                "role": "tool",
                "name": call["tool"],
                # R2 / W2-T3: JSON-safe cut with an explicit truncated marker —
                # never a blind mid-JSON chop the model can't detect. The cut
                # is the same one; only the budget it is given changed.
                "content": round_bodies[idx],
            })
        if native_round is not None:
            # Native route: hand the results back through the SAME structured
            # channel the calls came in on, in the shape this provider wants.
            # Partition the flat results by block so each call id is answered.
            # The bodies are already rendered under the round bound, so the
            # builder is handed strings and an identity renderer rather than
            # being asked to re-cut them at a per-tool limit.
            messages = messages + [
                _cp.native_assistant_message(native_round, wire=native_wire),
                *_cp.native_tool_result_messages(
                    native_round,
                    round_bodies,
                    wire=native_wire,
                    bounded_json=lambda body, _limit: body,
                ),
            ]
        else:
            messages = messages + [
                {"role": "assistant", "content": content},
                *tool_messages,
            ]
        pending_tool_results = True

        # COMPACTION. The round just appended keeps its full bodies — it is
        # what the next call reasons over. Everything older collapses to refs
        # (verbatim, because they are what the answer cites) plus a one-line
        # digest. The raw payloads are already on ``steps`` and go to the
        # persisted trace, so nothing is lost to the operator; what is lost is
        # paying to re-read them on every remaining round.
        messages, compaction = _tc.compact_prior_tool_messages(messages)
        if compaction.bodies_compacted:
            await _record({
                "phase": "reflect",
                "kind": "compaction",
                "round": rounds_used,
                **compaction.as_step(),
            })

    # ---- Force a final turn if the cap was hit without final --------
    if final_payload is None:
        forced_final = True
        # D-7: the forced final gets its OWN slice of the total budget, and if
        # there is none left it does not run at all. An unbounded synthesis
        # here is what turned run 3ae77c64 from "a shorter answer" into "no
        # answer": the loop passed its round check at 269s and was still
        # generating when the front door's clock ran out.
        # THE c8a0105c FIX. This was ``min(round_deadline, remaining)`` — the
        # synthesis was handed a drilling round's 150s slice, ran long on a
        # large transcript, and was cancelled with ~11k characters of finished
        # answer in flight, all of it billed and all of it discarded.
        #
        # The synthesis is not a drilling round. It is the turn that makes the
        # product, it is the longest generation of the run by construction, and
        # it happens when the prompt is at its biggest. So it gets the
        # REMAINDER of the budget, floored — ``max``, not ``min``. Overrunning
        # the total by a minute to finish the answer is the correct trade; the
        # detached run's invoke timeout (600s) is the real outer bound.
        final_budget = _cp.final_synthesis_budget(_remaining_total())
        if final_budget < _MIN_FINAL_SECONDS:
            await _record({
                "phase": "reflect",
                "kind": "degraded_final",
                "reason": "no_budget_for_synthesis",
                "elapsed_s": round(_elapsed(), 1),
            })
            synthesis_status = "none"
            final_payload = _cp.degraded_final_payload(
                rounds_used=rounds_used,
                rounds_available=effective_rounds,
                elapsed_s=_elapsed(),
                reason=stop_reason,
                last_tool_incorporated=not pending_tool_results,
            )
        # ONE definition of this prompt, shared with the recovery path in
        # ``consult_transcript``. A second copy of the wording here is exactly
        # what would make "the replay sends the same prompt" quietly false.
        force_system = _tx.synthesis_system(effective_system_prompt, FINAL_SENTINEL)
        synthesis_messages = messages + [
            {"role": "user", "content": _tx.synthesis_instruction(FINAL_SENTINEL)},
        ]
        # Record the request BEFORE sending it: a run that is cut mid-synthesis
        # is precisely the run whose prompt we will want back, and recording it
        # after the call would miss exactly that case.
        replay_transcript = _tx.build_transcript(
            system=force_system, messages=synthesis_messages,
        )
        if final_payload is not None:
            pass  # already degraded above — no budget left to synthesise in
        else:
            # Watch the synthesis as it streams. The handler has always
            # streamed this generation off the wire; what it lacked was a way
            # to let the caller hold the prefix. With a sink installed, a
            # timeout cancels the call but leaves every delivered token in
            # ``sink`` — which is the difference between an apology and an
            # answer. Deltas are relayed live so the panel shows the answer
            # forming rather than a spinner.
            #
            # Relay is throttled by CHARACTERS, not per delta: Anthropic emits
            # many small text_deltas and one frame each would flood the SSE
            # relay's 256-slot queue, which drops on full — the live answer
            # would arrive with holes in it.
            relay_buf: list[str] = []
            relay_len = 0
            relay_tasks: set[Any] = set()

            def _flush_delta() -> None:
                nonlocal relay_len
                if not relay_buf:
                    return
                chunk = "".join(relay_buf)
                relay_buf.clear()
                relay_len = 0
                task = asyncio.ensure_future(
                    _emit_step({
                        "phase": "narrate",
                        "kind": "answer_delta",
                        "text": chunk,
                    })
                )
                relay_tasks.add(task)
                task.add_done_callback(relay_tasks.discard)

            def _on_delta(chunk: str) -> None:
                nonlocal relay_len
                relay_buf.append(chunk)
                relay_len += len(chunk)
                if relay_len >= _ANSWER_DELTA_CHARS:
                    _flush_delta()

            sink = TextDeltaSink(on_delta=_on_delta)
            try:
                # Tools are WITHHELD here (no ``tools=``), on both routes. On
                # the native route that is what leaves "reply with text" as the
                # only available move — which is precisely the answer we want.
                with capture_text_deltas(sink):
                    content, usage, synth_response = await asyncio.wait_for(
                        _reason_with_response(
                            active_llm,
                            messages=synthesis_messages,
                            max_tokens=deps.max_tokens,
                            temperature=deps.temperature,
                            system_prompt=force_system,
                        ),
                        timeout=final_budget,
                    )
                _flush_delta()
                for k in aggregate_usage:
                    aggregate_usage[k] += usage.get(k, 0)
                spend.record(active_llm, synth_response, usage)
                last_raw = content
                await _record({
                    "phase": "reason",
                    "kind": "forced_final",
                    "tokens": usage.get("prompt_tokens", 0)
                    + usage.get("completion_tokens", 0),
                })
                # Forced-final is the terminal turn (tools withheld). If the
                # model wrote a bare prose answer — no sentinel header block
                # and no JSON wrapper — use it rather than discarding a real
                # answer as "(no answer produced)". Under the markdown
                # contract that prose IS the answer minus its metadata, so
                # accepting it costs only the metadata. The SAME builder as
                # the native arm above, so there is exactly one place that can
                # decide this turn's uncertainty (review defect 4).
                final_payload = final_payload_from_text(content)
                if not final_payload.get("answer"):
                    final_payload = None
            except asyncio.TimeoutError:
                # The synthesis ran past its slice. What happens next depends
                # on whether it had produced anything: DELIVER the partial if
                # it did (it is real, finished prose, and it is already paid
                # for), and only fall back to the honest apology if the model
                # never got a token out.
                _flush_delta()
                cut_reason = (
                    f"the synthesis exceeded its {final_budget:.0f}s slice of "
                    f"the time budget"
                )
                partial_text = sink.text
                if partial_text.strip():
                    last_raw = partial_text
                    await _record({
                        "phase": "reflect",
                        "kind": "partial_final",
                        "reason": "synthesis_deadline_exceeded",
                        "deadline_s": round(final_budget, 1),
                        "elapsed_s": round(_elapsed(), 1),
                        "chars": len(partial_text),
                        "approx_tokens": sink.approx_tokens,
                    })
                    synthesis_status = "partial"
                    final_payload = _cp.partial_final_payload(
                        partial_text=partial_text,
                        rounds_used=rounds_used,
                        rounds_available=effective_rounds,
                        elapsed_s=_elapsed(),
                        reason=cut_reason,
                        last_tool_incorporated=not pending_tool_results,
                    )
                else:
                    await _record({
                        "phase": "reflect",
                        "kind": "degraded_final",
                        "reason": "synthesis_deadline_exceeded",
                        "deadline_s": round(final_budget, 1),
                        "elapsed_s": round(_elapsed(), 1),
                    })
                    synthesis_status = "none"
                    final_payload = _cp.degraded_final_payload(
                        rounds_used=rounds_used,
                        rounds_available=effective_rounds,
                        elapsed_s=_elapsed(),
                        reason=cut_reason,
                        last_tool_incorporated=not pending_tool_results,
                    )
            except Exception:
                await _record({"phase": "reason", "kind": "forced_final_error"})
                # Re-raise — let the runtime classify (transient vs hard).
                raise

    # --- REFLECT / NARRATE --------------------------------------------
    # SYNTHESIS STATUS is a first-class fact about the turn, not something to
    # be re-derived by scanning the step trace for a kind name. "complete" =
    # the model finished; "partial" = it was cut and we are delivering what it
    # wrote; "none" = it never produced a token. Only the last two are worth
    # offering "Synthesize from evidence" on, and the front door decides that
    # from this field.
    if final_payload is None:
        synthesis_status = "none"
    consult = _build_consult_response(
        question=question,
        final_payload=final_payload,
        collected_refs=collected_refs,
        rounds_used=rounds_used,
        forced_final=forced_final,
        subprovider=getattr(active_llm, "subprovider", None),
        extra_data={
            "synthesis_status": synthesis_status,
            "resynthesizable": synthesis_status != "complete",
            "usage": spend.snapshot(),
            # Carried ONLY for a run that may need finishing. A run that
            # answered has nothing to replay, and storing its prompt would put
            # a copy of every transcript in the turns table for no reader.
            **(
                {"replay_transcript": replay_transcript}
                if replay_transcript is not None and synthesis_status != "complete"
                else {}
            ),
        },
    )
    # Project the per-round tool trace into the payload's data bag so the
    # consult front door (consult_api._project_consult_response reads
    # data["tool_calls"]) can surface "what it did" to the operator instead of
    # returning tool_calls:[]. One entry per executed call, in batch+round order.
    tool_trace = [
        {
            "tool": s.get("tool"),
            "args": s.get("args", {}),
            "result": s.get("result"),
            "round": s.get("round"),
            "governed": s.get("governed"),
            "ok": s.get("ok"),
        }
        for s in steps
        if s.get("kind") == "tool_call"
    ]
    # Carry the FULL per-round ReAct step trace on the payload so the SINGLE
    # ConsultResponsePayload carrier surfaces it to BOTH transports (chat
    # envelope + deep row read-back) — the consult front door then persists it
    # into ``consult_turns.steps`` so a turn is inspectable after the fact
    # (previously never populated). Rounds are hard-capped (ROUNDS_CEILING), so
    # the trace is bounded; the raw fields on unparseable steps are truncated.
    data_update: dict[str, Any] = {"tool_calls": tool_trace, "steps": list(steps)}
    # 7g-2 — THE PROVENANCE CENSUS. "Mostly model knowledge" was a judgement a
    # reader had to take on trust; this is the count behind it. Composed HERE,
    # on the server, off the answer's own cited refs and its own prose: a
    # share the client derived could disagree with the answer it is printed
    # beside. Best-effort by contract — a census that cannot be measured
    # reports None per class (never a fabricated zero) and never costs the
    # caller an answer.
    try:
        _classification = None
        _classify = getattr(deps.substrate, "classify_cited_refs", None)
        _cited = [str(r) for r in consult.cited_substrate_refs]
        if callable(_classify) and _cited:
            _classification = await _classify(refs=_cited)
        data_update["provenance_census"] = build_provenance_census(
            answer=consult.answer,
            cited_refs=_cited,
            classification=_classification,
        )
    except Exception as exc:  # noqa: BLE001 — a census never fails an answer
        logger.warning("consult.provenance_census.failed err=%s", exc)
    # Also stash the raw final reply so the operator can audit
    # malformed-but-recovered cases.
    if final_payload is None:
        data_update["raw_final"] = last_raw[:4000]
    consult = consult.model_copy(
        update={"data": {**consult.data, **data_update}}
    )
    await _record({
        "phase": "reflect",
        "kind": "build_consult_response",
        "uncertainty": consult.uncertainty,
        "cited_refs_count": len(consult.cited_substrate_refs),
        "unanswered_aspects_count": len(consult.unanswered_aspects),
    })

    finding = _wrap_as_finding(consult, analyst_id=analyst_id)
    await _record({
        "phase": "narrate",
        "kind": "wrap_finding",
        "tags": len(finding.tags),
    })

    return AnalystMethodResult(
        finding=finding,
        consult_response=consult,
        usage=aggregate_usage,
        derived_from=list(collected_refs),
        intermediate_steps=steps,
    )


# ---------------------------------------------------------------------------
# Adapter — closure-shaped runner the runtime already knows how to call
# ---------------------------------------------------------------------------


class ConsultOnDemandRunner:
    """``AnalystRunFn``-shaped wrapper around :func:`run_method`.

    Conforms to ``AnalystRunFn = Callable[[list[dict], Mapping], Awaitable]``
    so :class:`legba.runtime.dapr_actors.AnalystActor` can dispatch this
    kind without modifications.  The runtime constructs one per analyst
    actor at activate time and stashes it on ``_AnalystDeps.run_method``.
    """

    def __init__(
        self,
        llm: LLMHandlerLike,
        substrate: SubstrateQueryPort,
        *,
        max_tokens: int | None = None,
        temperature: float = 0.2,
        system_prompt: str | None = None,
        max_rounds: int = MAX_TOOL_ROUNDS,
    ) -> None:
        # `temperature` reaches the wire only on a plane that still accepts it
        # — see the note on ConsultOnDemandDeps.temperature.
        self._deps = ConsultOnDemandDeps(
            llm=llm,
            substrate=substrate,
            max_tokens=max_tokens if max_tokens is not None else _default_max_tokens(),
            temperature=temperature,
            system_prompt=system_prompt or _SYSTEM_PROMPT,
            max_rounds=max_rounds,
        )

    async def __call__(
        self,
        inputs: list[dict[str, Any]],
        options: Mapping[str, Any],
    ) -> AnalystMethodResult:
        return await run_method(inputs, options, self._deps)


def build_prompt_module() -> Any:
    """Construct the DSPy module bound to this kind.

    Wave B prereq #4: backfilled to return a real
    :class:`legba.prompts.consult_on_demand.v1.ConsultOnDemandRound`.

    The kind is a ReAct loop (``MAX_TOOL_ROUNDS = 6`` rounds + one
    forced-final), so the returned DSPy module exposes the *per-round*
    decision step.  The kind handler's outer loop in
    :func:`legba.data.analysts.consult_on_demand.run_method` orchestrates
    tool dispatch between rounds — that stays in Python; only the LLM-
    bearing step is the optimizer's compile surface.

    Lazy-imports so this file imports cleanly when dspy isn't installed;
    raises :class:`ModuleNotFoundError` otherwise.
    """
    from legba.prompts.consult_on_demand.v1 import build as _build
    return _build()


__all__ = [
    "AnalystMethodResult",
    "ConsultOnDemandDeps",
    "ConsultOnDemandRunner",
    "FINAL_SENTINEL",
    "HANDLER_VERSION",
    "KIND_NAME",
    "LLMHandlerLike",
    "LLM_OVERRIDE_ALLOWLIST",
    "MAX_TOOL_ROUNDS",
    "OUTPUT_KIND",
    "PROMPT_MODULE_PATH",
    "READ_SLICE",
    "SCHEMA_VERSION",
    "SubstrateQueryPort",
    "build_prompt_module",
    "resolve_output_budget",
    "run_method",
]
