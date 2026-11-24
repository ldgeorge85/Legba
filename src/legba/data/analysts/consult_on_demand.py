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
from legba.shared.sampling import HOUSE_TEMPERATURE

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
from ..provenance.citation_markers import normalize_inline_citation_markers
from ..provenance.models import ConsultResponsePayload, FindingPayload
from ..stack.llm import tool_round_compaction as _tc
from ..stack.llm.base import StreamInterruptedFailure
from ..stack.llm.call_gate import interactive_calls
from ..stack.llm.stream_observer import TextDeltaSink, capture_text_deltas
from . import consult_thinking as _think  # T-1: which consult calls think
from . import consult_context_ceiling as _cc
from . import consult_round_protocol as _cp
from . import consult_transcript as _tx
# V3/P3 — the SubstrateQueryPort protocol moved to a leaf when the temporal
# params grew this module past its size ceiling; imported back and
# re-exported so every call site is byte-identical.
from .consult_provenance_census import compose_census
from . import consult_evidence_floor as _floor
from . import consult_judging as _judging
from . import consult_prompt_context as _pctx
from . import consult_watch_mint as _wm
from .consult_substrate_port import SubstrateQueryPort
from .consult_spend_guard import SpendGuard
from . import consult_contract as _ct  # Q-1: the turn contract (window, explanations, record-only)
from .consult_turn_cost import arm_turn_ceiling_observed

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

#: Max tools a planner may request in ONE batch round — defined beside the
#: round normalizer in ``consult_tool_routing`` (C-1b extraction), re-exported.
from .consult_tool_routing import MAX_TOOLS_PER_BATCH  # noqa: E402

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
    # CONSULT_REVIEW_2026-09-28 B.10: the run's own summed cost estimate
    # (``SpendGuard.cost_usd`` — the running total ridden on each step's
    # ``usage.est_cost_usd``), so the runtime's budget_ledger write can carry
    # THIS number into ``cost_estimate_usd`` instead of recomputing from
    # (provider, model, aggregate tokens), which loses call-to-call model
    # changes (a plane override mid-run) that SpendGuard tracked per call.
    # ``None`` for every other analyst kind — the runtime keeps recomputing.
    cost_estimate_usd: float | None = None


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------



# The system prompt text lives in its own leaf (wave Q, 2026-09-28 — the size
# gate); imported one way and re-exported under the same name so readers of
# ``consult_on_demand._SYSTEM_PROMPT`` are unaffected.
from .consult_system_prompt import _SYSTEM_PROMPT  # noqa: E402



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
    split_ref_tokens,
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
    tool_context_renderer,
)


def _coerce_uuid_list(raw: Any) -> list[UUID]:
    """STRICT uuid coercion — the ids in ``raw``, anything else dropped.

    Deliberately unchanged, and deliberately NOT the tolerant reader below.
    This one parses ids the PLATFORM produced — a tool result's ``refs``, a
    persisted turn's ``cited_refs`` — where a token that is not a bare uuid is
    not a citable substrate row and must not become one. ``search_context``
    returns ``ctx:<uuid>`` chunk ids for exactly that reason: the prefix is
    what stops a corpus chunk masquerading as a signal, and a reader that
    reached inside the token for the uuid would undo it.
    """
    if not isinstance(raw, list):
        return []
    out: list[UUID] = []
    for item in raw:
        try:
            out.append(UUID(str(item)))
        except (ValueError, AttributeError):
            continue
    return out


def _coerce_refs_audited(raw: Any) -> tuple[list[UUID], list[str]]:
    """``(refs, malformed)`` for the MODEL's own ``cited_refs`` header.

    2026-09-28 §B.3, and the counterpart to the strict reader above. A bare
    ``UUID(str(item))`` under ``except: continue`` threw away two different
    things in the same silence: a REAL id the model wrapped in a bracket (the
    core plane writes ``【a1b2…】`` by default) and a FABRICATED label
    (``sit-001``). The first is our parse bug and is recoverable; the second is
    a fact about the answer. ``split_ref_tokens`` separates them at the seam
    and the malformed tail is carried on the response rather than dropped.

    Tolerance is right HERE and wrong in :func:`_coerce_uuid_list`, because
    this reads prose a model wrote and that one reads data we wrote. The refs
    this recovers are still filtered against the tool-confirmed set by the
    caller, so recovering one can never admit an id no tool returned.
    """
    tokens, malformed = split_ref_tokens(raw)
    out: list[UUID] = []
    for token in tokens:
        try:
            out.append(UUID(token))
        except (ValueError, AttributeError):  # pragma: no cover — regex-gated
            continue
    return out, malformed


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
# Tool dispatcher — EXTRACTED to ``consult_tool_routing`` (C-1b)
#
# The whitelist, the round normalizer, the per-tool dispatcher and the one-call
# governed router moved out one-way under the module-size ratchet. Re-exported
# so every importer (the deep consult workflow, the inline assessors, the
# tests) is unchanged and the loop below reads exactly as it did.
# ---------------------------------------------------------------------------

from . import consult_bakeoff as _bk  # noqa: E402 — X3/G0, the bake-off arms
from . import consult_case as _case  # noqa: E402 — C-1b, the case gate
from . import consult_tool_providers as _prov  # noqa: E402 — C-2a §7, the hook
from . import consult_tool_routing as _route  # noqa: E402
from .consult_tool_routing import (  # noqa: E402,F401
    _KNOWN_TOOLS,
    _dispatch_tool,
    _normalize_calls,
    _run_one_call,
)

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
    # 2026-09-28 §B.3 — NORMALIZE THE MARKERS ONCE, HERE, and persist the
    # result. The core plane writes ``【3】`` / ``【ref:2】`` / ``【2†L1-L2】``
    # where ``[3]`` belongs; every other analyst path has normalized that at
    # write time since 2026-06-30, and consult was the one that did not,
    # because the rule lived in a module consult cannot import (see the note in
    # ``provenance.citation_markers``). Un-normalized, the answer's own
    # ordinals are invisible to the provenance census, which then grades a
    # correctly-cited sentence as model knowledge. Idempotent on ASCII prose,
    # so a normal answer is byte-identical through it.
    answer = normalize_inline_citation_markers(answer)
    raw_uncertainty = final_payload.get("uncertainty", 0.5)
    try:
        uncertainty = float(raw_uncertainty)
    except (TypeError, ValueError):
        uncertainty = 0.5
    uncertainty = max(0.0, min(1.0, uncertainty))

    llm_refs, malformed_refs = _coerce_refs_audited(final_payload.get("cited_refs"))
    # The collected-from-tools refs are the authoritative set; we trust
    # those over LLM-emitted ones (planner could hallucinate UUIDs).
    # The LLM's cited_refs are kept but filtered to those we actually
    # observed so the planner can narrow but not invent.
    collected_set = set(collected_refs)
    confirmed_llm_refs = [r for r in llm_refs if r in collected_set]
    # Prefer the LLM-confirmed subset (the planner's narrowing); fall
    # back to the full collected set when the planner didn't narrow.
    cited = confirmed_llm_refs if confirmed_llm_refs else list(collected_refs)
    # 2026-09-28 §B.3 — STAMP WHICH FALLBACK THIS WAS. "The planner declined to
    # narrow" and "every ref the planner named failed to parse or to intersect"
    # arrive here as the same empty ``confirmed_llm_refs`` and leave as the same
    # whole collected set, and the operator cannot tell a deliberate breadth
    # from a swallowed citation list. Three numbers separate them, and they are
    # only emitted when the fallback actually fired: how many refs the header
    # carried, how many of those no tool had returned, and how many tokens were
    # not refs at all.
    refs_audit: dict[str, Any] = {}
    if malformed_refs:
        refs_audit["cited_refs_malformed"] = malformed_refs
    if not confirmed_llm_refs and (llm_refs or malformed_refs):
        refs_audit["header_refs_unparsed"] = len(malformed_refs)
        refs_audit["header_refs_unconfirmed"] = len(llm_refs)
        refs_audit["cited_refs_fallback"] = "collected_set"
    elif confirmed_llm_refs and len(confirmed_llm_refs) < len(llm_refs):
        refs_audit["header_refs_unconfirmed"] = len(llm_refs) - len(confirmed_llm_refs)

    unanswered_raw = final_payload.get("unanswered_aspects") or []
    if not isinstance(unanswered_raw, list):
        unanswered_raw = [str(unanswered_raw)]
    unanswered = [str(u)[:512] for u in unanswered_raw][:20]

    # If the LLM signaled high uncertainty but left unanswered_aspects
    # empty, surface the question itself as the unaddressed aspect so
    # downstream surfaces (panel, A2A response) always show *something*.
    if uncertainty >= 0.7 and not unanswered:
        unanswered = [question[:512]]

    # WATCH CANDIDATES (CONSULT_REVIEW_2026-09-28 §B.4) — the answer's own
    # "what would change my mind" items, off the header line, structured. The
    # key is ABSENT when the model wrote none: an empty list would read as
    # "nothing would change this call", which is a claim and not the same
    # fact as "it did not say". Nothing is minted HERE: once the answer is
    # accepted, ``consult_watch_mint`` (F-4) turns them into the open questions
    # claim_watch watches, and stamps ``watch_minted`` on this same bag.
    watch_raw = final_payload.get("watch_candidates") or []
    if not isinstance(watch_raw, list):
        watch_raw = [str(watch_raw)]
    watch = [str(w).strip()[:512] for w in watch_raw if str(w).strip()][:8]

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
            **({"watch_candidates": watch} if watch else {}),
            **refs_audit,
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
    thinking: bool = False,
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
        thinking=thinking,
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
    thinking: bool = False,
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
    # T-1: ``thinking`` rides only when True (the caller checked the plane can
    # think), so a thinking-off call is byte-identical to before. Every consult
    # call is INTERACTIVE on the core-plane gate: a person is waiting.
    if thinking:
        kwargs["thinking"] = True
    with interactive_calls():
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


def _raise_if_stream_cut(content: str, response: Any) -> None:
    """Raise a RETURNED stream cut as the loop's one cut shape (W11-F133).

    Anthropic's handler returns a cut stream as the partial body with
    ``finish_reason="error"`` and a ``legba_stream_error`` marker; the core
    plane raises :class:`StreamInterruptedFailure`. The loop handles one shape,
    so a cut generation is never mistaken for a finished answer, nor a cut
    tool call for a round. Loop-only: the shared ``_reason*`` helpers (deep
    consult's stages, the recovery replay) are unchanged.
    """
    raw = getattr(response, "raw_response", None)
    stream_cut = raw.get("legba_stream_error") if isinstance(raw, Mapping) else None
    if stream_cut:
        raise StreamInterruptedFailure(
            f"stream interrupted after {len(content)} chars: {stream_cut}",
            partial_text=content,
            tool_use_started=bool(getattr(response, "tool_calls", None)),
            partial_body=raw,
        )


def _refs_from_tool_result(tool_result: Mapping[str, Any]) -> list[UUID]:
    """Lift any substrate UUIDs out of a tool's JSON response."""
    raw = tool_result.get("refs") if isinstance(tool_result, Mapping) else None
    return _coerce_uuid_list(raw or [])


#: The payload keys the whitelist's tools carry their result list under, in
#: probe order. ``rows``/``items`` were the only two the trace looked at, so
#: ``compare_targets`` — which returns ``targets`` — logged count 0 on every
#: call it has ever made and a six-target comparison read as an empty result
#: in the 2026-09-28 review. ``count`` still wins when a tool reports its own.
_RESULT_LIST_KEYS: tuple[str, ...] = (
    "rows", "items", "targets", "compared", "entries",
)


def _tool_result_count(tool_result: Mapping[str, Any]) -> int:
    """How many things one tool result carries, for the ``act`` trace step."""
    own = tool_result.get("count")
    if isinstance(own, int) and not isinstance(own, bool):
        return own
    for key in _RESULT_LIST_KEYS:
        value = tool_result.get(key)
        if isinstance(value, (list, tuple)):
            return len(value)
    return 0


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
    temperature: float = HOUSE_TEMPERATURE
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
    # F-4: the write surface the watch mint (``consult_watch_mint``) writes through;
    # None (hand-built deps) => no candidate is minted and no stamp is written.
    pg_pool: Any | None = None
    # C-1b: the ``workstation_drive`` pack binding ("present a case"). Used ONLY
    # on a turn the operator armed (``present_case``), re-pointed per request;
    # an armed turn without it fails loud (``case_unavailable``), never plain.
    drive_binding: Any | None = None
    # C-2a §7 — the per-analyst binding of every provider pack the consult
    # descriptor grants, keyed by pack id (``runtime/consult_bindings``). A
    # family is offered only on a turn armed for it; ``drive_binding`` above is
    # C-1b's spelling of the drive pack's entry and is still honoured.
    provider_bindings: Mapping[str, Any] | None = None
    # X2 / C1 — the by-id LLM resolver the turn's JUDGE is built through (the
    # host's cached handler factory, unfiltered by the plane allowlist: the
    # judge is not a consult plane). None ⇒ the turn is labelled
    # ``not_judged: no_judge_route`` — never silently unjudged.
    judge_resolver: Callable[[str], Awaitable[Any]] | None = None
    # Q-1 — the TURN CONTRACT (``consult_contract``): armed by the production
    # builder; off for hand-built deps, whose turns stay byte-identical.
    # ``contract_pool`` is the READ pool its coverage / resolution reads use
    # (falls back to ``pg_pool``; deep consult passes its pool here so the
    # watch mint, which writes through ``pg_pool``, stays off on that path).
    turn_contract: bool = False
    contract_pool: Any | None = None
    # Q-2 — the READ pool the settle-point JUDGE reads cited evidence through
    # (falls back to ``pg_pool``). Deep consult sets it beside
    # ``judge_resolver`` so its turns are judged, while ``pg_pool`` — and so
    # the chat-only watch mint — stays unset on that path.
    judge_pool: Any | None = None
    # T-1 — ``method.llm.thinking`` of the descriptor (``consult_default`` and
    # ``deep_consult`` declare "on"). Off by default: hand-built deps and the
    # bake-off arms (whose descriptors declare nothing) call as before. Which
    # CALLS think is ``consult_thinking``'s table, not this flag.
    thinking: bool = False


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
    # C-1b (decision D1), generalised by the C-2a §7 hook: a provider family
    # (the case's drive actions, the write plane) is OPT-IN per turn. Unarmed,
    # nothing below differs by a byte; armed, the family's tools and its
    # protocol block are offered, and an armed turn with no binding refuses
    # rather than silently answering plainly.
    flags = _prov.RunFlags.from_request(first)
    offered = _prov.offered_providers(flags)
    for _p in offered:
        if _prov.binding_for(deps, _p.pack_id) is None:
            raise ValueError(_p.unavailable)
    # X3/G0 — a bake-off ARM (the consult descriptor's read pack decides; see
    # ``consult_bakeoff``) offers only its pack's tools and reads its own prompt.
    arm = _bk.variant_for_binding(deps.agency_binding)
    base_tools = set(arm.tools) if arm else _KNOWN_TOOLS
    native_tool_payload = (
        _cp.render_tools_for(
            native_wire,
            [*sorted(base_tools), *(n for _p in offered for n in _p.tool_names)]
            if offered else base_tools,
        )
        if native_tools_route
        else None
    )
    # --- RUN CONTEXT (CONSULT_REVIEW_2026-09-28 §B.4) -------------------
    # What is TRUE at this run's start, appended to the standing prompt rather
    # than baked into it: which curated series the platform actually holds
    # (the reason series_history/series_compare had been invoked zero times —
    # nothing ever said there was anything to read), and the substrate_read
    # pack's OWN prompt_fragments + rules, read off the live descriptor the
    # binding already resolved. The runtime injected those fragments only on
    # the inline_target GATHER binding; the consult path is governed through
    # the same pack and never saw them. It lands BEFORE the native-protocol
    # suffix so the output contract stays the last thing the model reads, and
    # it is empty (prompt byte-identical) for a deps bundle with neither a
    # holdings reader nor a binding.
    run_context = await _pctx.render_run_context(
        substrate=None if arm else deps.substrate,  # an arm holds no series tools
        agency_binding=deps.agency_binding,
    ) + "".join(_p.fragment(_prov.binding_for(deps, _p.pack_id)) for _p in offered)
    effective_system_prompt = (arm.system_prompt if arm else deps.system_prompt) + run_context + (
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

    # T-1 — this turn's thinking (``consult_thinking``): the descriptor's
    # choice, a request row's per-turn override, never on a bake-off arm, and
    # only on a plane that can think. Recorded when asked for, so a turn's
    # trace says whether it thought and, if not, why.
    thinking = _think.ConsultThinking.decide(
        deps.thinking, first=first, arm=arm is not None, llm=active_llm,
    )
    if thinking.requested:
        await _record(thinking.step())

    # --- Recovery: finish a run that was cut, without drilling again -------
    # ``synthesize_from`` carries a PERSISTED turn's evidence. The whole ReAct
    # loop below is skipped: this path writes an answer over evidence that
    # already exists and was already paid for. It lives in the analyst rather
    # than the front door because the tools, the governed binding and the model
    # planes are all here, and it arrives on the request row so the actor
    # surface is unchanged.
    # F-6: the input ceiling every call below honours (``consult_context_ceiling``).
    context_ceiling = _cc.resolve_ceiling(options)
    ceiling_source = _cc.SOURCE_DESCRIPTOR
    if context_ceiling is None:
        # W11-F133 (consult 6cd91190): no descriptor ceiling is no longer "no
        # ceiling" — the route's own window bounds the loop's input.
        context_ceiling, ceiling_source = _cc.derive_ceiling(
            active_llm, max_tokens=deps.max_tokens,
        )
    ceiling_stamp: dict[str, Any] | None = None
    recovery = first.get("synthesize_from")
    if recovery:
        from . import consult_resynthesis as _rs

        # Q-2 — the recovered answer carries the turn contract like any other:
        # the original turn's window / grounding / coverage, settled over the
        # NEW prose by the same functions (``consult_contract``).
        r_contract = await _ct.open_recovery_turn(recovery=recovery, deps=deps, record=_record)

        async def _settle_recovered(consult: Any, r_spend: Any) -> Any:
            async def _r_repair(system: str, msgs: list[dict[str, Any]]) -> str:
                text, rep_usage, rep_response = await _reason_with_response(
                    active_llm, messages=msgs, max_tokens=deps.max_tokens,
                    temperature=deps.temperature, system_prompt=system,
                    thinking=thinking.for_role(_think.ROLE_REPAIR),
                )
                r_spend.record(active_llm, rep_response, rep_usage)
                return text

            return await _ct.settle_answer(
                consult, r_contract, steps=[], spend=r_spend, llm_call=_r_repair,
                record=_record, turn_ref=str(first.get("request_id") or ""),
                first={"on_screen": r_contract.on_screen} if r_contract and r_contract.on_screen else {},
            )

        recovered = await _rs.run_synthesis_only(
            recovery,
            deps=deps,
            active_llm=active_llm,
            wire=native_wire,
            system_prompt=effective_system_prompt,
            record=_record,
            analyst_id=analyst_id,
            context_ceiling=context_ceiling,
            context_ceiling_source=ceiling_source,
            settle=_settle_recovered if r_contract is not None else None,
            thinking=thinking.for_role(_think.ROLE_RECOVERY),
        )
        recovered = await _wm.mint_on_result(recovered, first=first, options=options, deps=deps, record=_record)
        # W11-F93 — the recovered answer is judged once it has SETTLED, too.
        recovered = await _judging.judge_settled_result(recovered, deps=deps, record=_record)
        return await _ct.enforce_after_judge(recovered, r_contract, record=_record)

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
        effective_rounds = (
            _case.V.LIMITS["case_rounds_default"] if flags.present_case else deps.max_rounds
        )
    else:
        try:
            effective_rounds = int(requested)
        except (TypeError, ValueError):
            rounds_source = "default_malformed_request"
            effective_rounds = deps.max_rounds
    effective_rounds = max(1, min(ROUNDS_CEILING, effective_rounds))

    # --- Per-run spend ceiling ----------------------------------------
    # Per RUN, never on ``deps`` (shared across concurrent runs).
    spend = _bk.spend_guard_for(  # X3/G0: a bake-off row arms its hard stop
        first,
        stock_kwargs={
            k: v
            for k, v in (
                ("max_input_tokens", deps.max_input_tokens_per_run),
                ("max_cost_usd", deps.max_cost_usd_per_run),
            )
            if v is not None
        },
        max_output_tokens=deps.max_tokens,
    )
    # Q-1 (E) — the per-TURN ceiling (``consult_turn_cost``), on a priced route.
    out_limit = _cp.output_limit(active_llm, deps.max_tokens)  # the model's own (no output caps)
    spend = await arm_turn_ceiling_observed(spend, active_llm, pool=deps.pg_pool or deps.contract_pool)
    batch_cap = _cp.native_batch_cap(deps.native_batch_cap)

    # C-1b / C-2a §7 — each offered family's RUN-SCOPED GATE (the case gate:
    # read set, seq, cap, seal; the write gate: read set, caps, spend), over a
    # binding re-pointed at this request. Armed turns only.
    gates = {
        _p.pack_id: _p.open_gate(
            _prov.binding_for(deps, _p.pack_id).for_request(
                session_id=first.get("session_id"), request_id=first.get("request_id"),
            ),
            record=_record, first=first, spend=spend,
        )
        for _p in offered
    }
    case = gates.get(_case.V.WORKSTATION_DRIVE_PACK_ID)

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
            "context_ceiling_tokens": context_ceiling,
            "context_ceiling_source": ceiling_source,
            "run_context_chars": len(run_context),
            "total_budget_s": deps.total_budget_seconds,
            "final_floor_s": _cp.default_final_floor_seconds(),
            **spend.snapshot(),
            **({"present_case": True} if case else {}),
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
    # Q-1 (A, C) — the window in force, the grounding mode, the coverage read.
    # Q-2: ``contract_question`` is the ASKER's question when the row's
    # ``question`` carries more (deep consult appends its pre-acquired evidence
    # brief, whose dates and words are not the asker's window or mode).
    contract = await _ct.open_turn(
        first=first, question=str(first.get("contract_question") or question),
        deps=deps, arm=arm, record=_record,
    )
    user_prompt = _ct.lead_user_prompt(user_prompt, contract, first)  # + ON SCREEN (U-6)
    messages: list[Mapping[str, Any]] = [
        *seeded,
        {"role": "user", "content": user_prompt},
    ]

    collected_refs: list[UUID] = []
    aggregate_usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    # Roll 86: per-call receipts = the run account's entries from here on (unbound → none).
    from ..registry.consult_turn_stamps import call_receipts as _call_receipts
    from ..run_accounting import llm_call_watermark as _acct_wm, llm_calls_since as _acct_since
    _calls_from = _acct_wm()

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
    #: A terminal round answered with no body (W11-F160): settled as "none"
    #: with no forced final, which would burn the same output cap again.
    answered_empty = False
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

    # --- Live answer capture (c8a0105c, extended by 9b902fec) ----------
    # A sink installed around a generation holds every streamed token, so a
    # timeout cancels the call but leaves the paid-for prefix in the caller's
    # hands; deltas are relayed live so the panel shows the answer forming.
    # Run 9b902fec (2026-10-29) is why this is no longer forced-final-only: on
    # the native route a DRILLING round with no tool call IS the answer, and
    # round 13 was cut at 480s with no sink installed — the answer was thrown
    # away and a second long generation bought to replace it.
    #
    # Relay is throttled by CHARACTERS, not per delta: Anthropic emits many
    # small text_deltas and one frame each would flood the SSE relay's
    # 256-slot queue, which drops on full — the live answer would arrive with
    # holes in it. Each frame names its ``segment`` (``round-<n>`` / ``final``)
    # so a reader can tell a tool round's preamble from the answer in flight.
    relay_tasks: set[Any] = set()

    def _answer_sink(segment: str) -> tuple[TextDeltaSink, Callable[[], None]]:
        relay_buf: list[str] = []
        relay_len = 0

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
                    "segment": segment,
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

        return TextDeltaSink(on_delta=_on_delta), _flush_delta

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
        sent, ceiling_stamp = _cc.apply_context_ceiling(
            messages, system=effective_system_prompt, ceiling=context_ceiling,
            source=ceiling_source,
        )
        # Native route only: there a reply with no tool call IS the answer, so
        # streamed text may be the product. On the JSON-text route the text is
        # a tool-call object as often as an answer and is never relayed.
        round_sink, round_flush = (
            _answer_sink(f"round-{rounds_used}") if native_tools_route
            else (TextDeltaSink(), lambda: None)
        )
        try:
            with capture_text_deltas(round_sink):
                content, usage, llm_response = await asyncio.wait_for(
                    _reason_with_response(
                        active_llm,
                        messages=sent,
                        max_tokens=deps.max_tokens,
                        temperature=deps.temperature,
                        system_prompt=effective_system_prompt,
                        tools=native_tool_payload,
                        thinking=thinking.for_role(_think.ROLE_ROUND),
                    ),
                    timeout=round_budget,
                )
            round_flush()
            _raise_if_stream_cut(content, llm_response)
        except asyncio.TimeoutError:
            round_flush()
            partial_text = round_sink.text if native_tools_route else ""
            # A reply that opens as JSON is the old text tool-call shape in
            # flight (``text_tool_round_fallback``), not prose: not an answer.
            json_shaped = partial_text.lstrip().startswith(("{", "```json"))
            if partial_text.strip() and not round_sink.tool_use_started and not json_shaped:
                # Run 9b902fec. The model was ANSWERING (text, no tool call on
                # the native route) when the round's deadline fired. That text
                # is the answer, it is paid for, and it is delivered — the
                # forced final is SKIPPED, because re-generating the answer
                # from scratch is a second bill for what was just discarded.
                cut_reason = (
                    f"round {rounds_used} exceeded its {round_budget:.0f}s "
                    f"deadline while writing the answer"
                )
                stop_reason = cut_reason
                last_raw = partial_text
                await _record({
                    "phase": "reflect",
                    "kind": "partial_final",
                    "reason": "round_deadline_exceeded_while_answering",
                    "round": rounds_used,
                    "deadline_s": round(round_budget, 1),
                    "elapsed_s": round(_elapsed(), 1),
                    "chars": len(partial_text),
                    "approx_tokens": round_sink.approx_tokens,
                })
                synthesis_status = "partial"
                final_payload = _cp.partial_final_payload(
                    partial_text=partial_text,
                    rounds_used=rounds_used,
                    rounds_available=effective_rounds,
                    elapsed_s=_elapsed(),
                    reason=cut_reason,
                    # The answering round was SENT every prior tool result.
                    last_tool_incorporated=True,
                )
                break
            # A single stuck call must not consume the run. Record it and fall
            # through to the forced final with whatever the prior rounds got —
            # nothing streamed, or the round was a tool call in progress.
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
        except Exception as exc:
            # W11-F133 (consult 6cd91190): ANY cut, not only the deadline. The
            # same test as the TimeoutError arm — native route, text streamed,
            # no tool call begun, not the JSON tool shape — and the same
            # outcome: the text IS the answer, delivered, no forced final.
            round_flush()
            partial_text = round_sink.text if native_tools_route else ""
            json_shaped = partial_text.lstrip().startswith(("{", "```json"))
            tool_use = round_sink.tool_use_started or bool(
                getattr(exc, "tool_use_started", False)
            )
            if partial_text.strip() and not tool_use and not json_shaped:
                cut_reason = (
                    f"round {rounds_used}'s generation failed while writing the "
                    f"answer ({type(exc).__name__})"
                )
                stop_reason = cut_reason
                last_raw = partial_text
                await _record({
                    "phase": "reflect",
                    "kind": "partial_final",
                    "reason": type(exc).__name__,
                    "error": str(exc)[:300],
                    "round": rounds_used,
                    "elapsed_s": round(_elapsed(), 1),
                    "chars": len(partial_text),
                    "approx_tokens": round_sink.approx_tokens,
                })
                synthesis_status = "partial"
                final_payload = _cp.partial_final_payload(
                    partial_text=partial_text,
                    rounds_used=rounds_used,
                    rounds_available=effective_rounds,
                    elapsed_s=_elapsed(),
                    reason=cut_reason,
                    last_tool_incorporated=True,
                )
                break
            if isinstance(exc, StreamInterruptedFailure):
                # The stream was cut mid-tool-call (or on the JSON-text route):
                # a cut like the deadline's, and handled like it — the forced
                # final synthesises what the prior rounds gathered.
                stop_reason = f"round {rounds_used}'s stream was interrupted"
                await _record({
                    "phase": "reason",
                    "kind": "round_stream_interrupted",
                    "round": rounds_used,
                    "error": str(exc)[:300],
                })
                rounds_used = max(0, rounds_used - 1)
                break
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
            **_cc.stamp_fields(ceiling_stamp),
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
            # W11-F160: an empty body settles "none", a cap-cut one "partial".
            final_payload, synthesis_status, settle_step = _cp.settle_final_reply(
                parsed, llm_response, usage, max_tokens=out_limit,
                rounds_used=rounds_used, rounds_available=effective_rounds,
                elapsed_s=_elapsed(), last_tool_incorporated=True,
            )
            answered_empty = final_payload is None
            if settle_step is not None:
                await _record({**settle_step, "round": rounds_used})
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
        # C-1b: drive tools (an armed case turn) run AFTER the reads, serially,
        # in emitted order; a round without one is the same gather as ever.
        results = await _route.run_round(
            deps, calls, scope_predicate=scope_predicate,
            analyst_id=analyst_id, gates=gates, round_no=rounds_used,
        )

        # Render this round's bodies under ONE shared bound instead of a
        # per-tool one. The per-tool 8 KB bound multiplied by the batch width:
        # five calls could put 40 KB into the transcript, and the transcript is
        # replayed on every later round. The round bound (16 KB) is allocated
        # max-min fair, so a small result is never truncated to make room for a
        # large sibling — the cut lands on the payloads that hold the tokens.
        # X2-B4: the renderer is the CONSULT CONTEXT BUILDER's display-policy
        # choice — a reader's consult sees tool results through the reader
        # projection (cited spans, never archived text); an operator's renders
        # exactly as before. See consult_tool_rendering.tool_context_renderer.
        round_bodies = _tc.allocate_round_bodies(
            [r[0] for r in results], render=tool_context_renderer(first),
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
                        # Most tools return a "count"; the rest name their
                        # payload themselves. Probing only rows/items logged
                        # compare_targets as count 0 on EVERY call it has ever
                        # made — it returns "targets" — so the 2026-09-28
                        # review read a six-target comparison as an empty
                        # result. Probe every payload key the whitelist
                        # actually returns.
                        "count": _tool_result_count(tool_result),
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
        for _gate in gates.values():
            # This round's READ results join each gate's read set only now,
            # after its provider calls ran: a ref from the same batch never
            # counts (C-1a §2, C-2a §5.2).
            _gate.harvest(rounds_used, calls, results)
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
    if final_payload is None and not answered_empty:
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
        # detached run's invoke timeout (derived from these same clocks plus a
        # margin, ``consult_runs.invoke_timeout_seconds``) is the outer bound.
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
        synthesis_messages, ceiling_stamp = _cc.apply_context_ceiling(
            messages + [
                {"role": "user", "content": _tx.synthesis_instruction(FINAL_SENTINEL)},
            ],
            system=force_system, ceiling=context_ceiling, source=ceiling_source,
        )
        # Record the request BEFORE sending it: a run that is cut mid-synthesis
        # is precisely the run whose prompt we will want back, and recording it
        # after the call would miss exactly that case.
        replay_transcript = _tx.build_transcript(
            system=force_system, messages=synthesis_messages,
        )
        if final_payload is not None:
            pass  # already degraded above — no budget left to synthesise in
        else:
            # Watch the synthesis as it streams (``_answer_sink`` above): a
            # timeout cancels the call but leaves every delivered token in
            # ``sink`` — the difference between an apology and an answer.
            sink, _flush_delta = _answer_sink("final")
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
                            thinking=thinking.for_role(_think.ROLE_FINAL),
                        ),
                        timeout=final_budget,
                    )
                _flush_delta()
                _raise_if_stream_cut(content, synth_response)
                for k in aggregate_usage:
                    aggregate_usage[k] += usage.get(k, 0)
                spend.record(active_llm, synth_response, usage)
                last_raw = content
                await _record({
                    "phase": "reason",
                    "kind": "forced_final",
                    "tokens": usage.get("prompt_tokens", 0)
                    + usage.get("completion_tokens", 0),
                    **_cc.stamp_fields(ceiling_stamp),
                })
                # Forced-final is the terminal turn (tools withheld). If the
                # model wrote a bare prose answer — no sentinel header block
                # and no JSON wrapper — use it rather than discarding a real
                # answer as "(no answer produced)". Under the markdown
                # contract that prose IS the answer minus its metadata, so
                # accepting it costs only the metadata. The SAME builder as
                # the native arm above, so there is exactly one place that can
                # decide this turn's uncertainty (review defect 4).
                #
                # W11-F160 (turn 91b6260c): a header-only reply is NO answer
                # (status "none", re-synthesizable), and a reply cut at the
                # output cap is "partial" with its prose kept.
                final_payload, synthesis_status, settle_step = _cp.settle_final_reply(
                    final_payload_from_text(content), synth_response, usage,
                    max_tokens=out_limit, rounds_used=rounds_used,
                    rounds_available=effective_rounds, elapsed_s=_elapsed(),
                    last_tool_incorporated=not pending_tool_results,
                )
                if settle_step is not None:
                    await _record(settle_step)
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
            except Exception as exc:
                # W11-F133 (consult 6cd91190): a synthesis that FAILED — the
                # connection dropped, the stream was cut, the provider erred —
                # after writing text is delivered exactly like one that ran
                # out of time. Only a failure with nothing written propagates.
                _flush_delta()
                partial_text = sink.text or str(getattr(exc, "partial_text", "") or "")
                if not partial_text.strip():
                    await _record({"phase": "reason", "kind": "forced_final_error"})
                    # Re-raise — let the runtime classify (transient vs hard).
                    raise
                cut_reason = f"the synthesis failed ({type(exc).__name__})"
                last_raw = partial_text
                await _record({
                    "phase": "reflect",
                    "kind": "partial_final",
                    "reason": type(exc).__name__,
                    "error": str(exc)[:300],
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

    # Every relayed answer frame reaches the stream BEFORE the run returns, so
    # the registry (which builds a cut run's partial from these frames) and the
    # panel never see the terminal frame overtake the answer's last chunk.
    if relay_tasks:
        await asyncio.gather(*list(relay_tasks), return_exceptions=True)

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
            # X2 / C1 — the receipt: the meter plus model, elapsed, priced.
            "usage": {
                **spend.receipt(active_llm, elapsed_s=_elapsed()),
                # Roll 86: one receipt per model call (consult_turn_stamps.call_receipts).
                "call_receipts": _call_receipts(_acct_since(_calls_from)),
            },
            **_cc.stamp_fields(ceiling_stamp),
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
    # Q-1 (A, B, E) — window lines, typed explanations (one bounded repair),
    # coverage block, the cost-stop line. Before the census, mint and judge.
    async def _repair_call(system: str, msgs: list[dict[str, Any]]) -> str:
        text, rep_usage, rep_response = await _reason_with_response(
            active_llm, messages=msgs, max_tokens=deps.max_tokens,
            temperature=deps.temperature, system_prompt=system,
            thinking=thinking.for_role(_think.ROLE_REPAIR),
        )
        spend.record(active_llm, rep_response, rep_usage)
        return text

    consult = await _ct.settle_answer(
        consult, contract, steps=steps, spend=spend, llm_call=_repair_call,
        record=_record, turn_ref=str(first.get("request_id") or ""), first=first,
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
    # ONE classification of the answer's cited refs, read here and used twice
    # below: by the EVIDENCE FLOOR and by the PROVENANCE CENSUS. Reading the
    # rows twice would let the two numbers printed beside each other disagree.
    _cited = [str(r) for r in consult.cited_substrate_refs]
    for _gate in gates.values():
        # The census union (C-1a §5): the turn's cited set is the answer's refs
        # plus every record the case's actions cite (narration included). Each
        # gate's record lands on the bag under its own key.
        _cited += [r for r in _gate.cited_uuids() if r not in _cited]
        data_update.update(_gate.bag())
    _classification: Mapping[str, Any] | None = None
    _resolved: int | None = None
    try:
        _classification, _resolved = await _floor.resolved_ref_count(
            deps.substrate, _cited
        )
    except Exception as exc:  # noqa: BLE001 — never fails an answer
        logger.warning("consult.ref_classification.failed err=%s", exc)
    # THE EVIDENCE FLOOR (CONSULT_REVIEW_2026-09-28 §B.3/§B.4). Until now the
    # turn's uncertainty was the model's own header number and nothing else:
    # session 96d7bfc2 made two tool calls, one of them carrying a fabricated
    # result in its ARGS, and published `uncertainty: 0.30`. The floor is keyed
    # to the count of cited refs that RESOLVE to a real row — the same
    # measurement the census below reports, read ONCE above and shared, so the
    # two can never disagree. An unmeasured run is not floored; see the leaf.
    consult = _floor.apply_uncertainty_floor(consult, resolved=_resolved)
    # 7g-2 — THE PROVENANCE CENSUS. "Mostly model knowledge" was a judgement a
    # reader had to take on trust; this is the count behind it. Composed HERE,
    # on the server, off the answer's own cited refs and its own prose: a
    # share the client derived could disagree with the answer it is printed
    # beside. Best-effort by contract — a census that cannot be measured
    # reports None per class (never a fabricated zero) and never costs the
    # caller an answer.
    # 2026-09-28 §B.3 — THE CENSUS + THE PROVENANCE HOLD, over the SAME classify
    # read the floor just made (one read serves both; the two cannot disagree).
    # An answer that resolved not one citation to live or held reporting, and
    # still asserts, leads with a line saying so. The banner goes on the ANSWER
    # (it is what a reader reads) and the reason goes on the wire (it is what
    # the panel keys on).
    _census, _hold_banner, _hold_reason = await compose_census(
        deps.substrate,
        answer=consult.answer,
        cited_refs=_cited,
        malformed_refs=consult.data.get("cited_refs_malformed"),
        classification=_classification,
    )
    if _census is not None:
        data_update["provenance_census"] = _census
    if _hold_banner and _hold_reason:
        data_update["hold_reason"] = _hold_reason
        consult = consult.model_copy(
            update={"answer": (_hold_banner + consult.answer)[:65000]}
        )
    # C-2a §5.4 — THE TRUTH LINE: an armed write turn ENDS with the server's
    # count of what changed (``writes: executed n · staged n · refused n`` or
    # ``writes: none``), whatever the prose above it claims.
    for _gate in gates.values():
        _suffix = _gate.answer_suffix() if hasattr(_gate, "answer_suffix") else ""
        if _suffix:
            consult = consult.model_copy(
                update={"answer": consult.answer[: 65000 - len(_suffix)] + _suffix}
            )
    # Also stash the raw final reply so the operator can audit
    # malformed-but-recovered cases.
    if final_payload is None:
        data_update["raw_final"] = last_raw[:4000]
    consult = consult.model_copy(
        update={"data": {**consult.data, **data_update}}
    )
    # The JUDGE is not here: it runs at the settle point below (W11-F93).
    # Both B.4 facts ride the FRAME as well as the payload, deliberately: the
    # payload's ``data`` bag dies at the consult front door (``consult_turns``
    # has no column for it, and the chat path writes no analyst_outputs row),
    # while ``steps`` is what a turn durably persists. A later lane reading
    # watch candidates back out of a turn reads them from here.
    await _record({
        "phase": "reflect",
        "kind": "build_consult_response",
        "uncertainty": consult.uncertainty,
        "cited_refs_count": len(consult.cited_substrate_refs),
        "unanswered_aspects_count": len(consult.unanswered_aspects),
        "resolved_refs": _resolved,
        **{
            key: consult.data[key]
            for key in ("uncertainty_floor_applied", "watch_candidates", _cc.STAMP_KEY)
            if key in consult.data
        },
    })

    finding = _wrap_as_finding(consult, analyst_id=analyst_id)
    await _record({
        "phase": "narrate",
        "kind": "wrap_finding",
        "tags": len(finding.tags),
    })

    result = AnalystMethodResult(
        finding=finding,
        consult_response=consult,
        usage=aggregate_usage,
        derived_from=list(collected_refs),
        intermediate_steps=steps,
        cost_estimate_usd=spend.cost_usd,
    )
    result = await _wm.mint_on_result(result, first=first, options=options, deps=deps, record=_record)
    # X2 / C1, W11-F93 — THE SETTLE POINT. The judge grades the answer and the
    # cited refs this run RETURNS, after every edit and the mint, and stamps a
    # digest of them the front door re-checks against what it persists. It is
    # exempt from the run's spend ceiling (``spend`` above never meters it): a
    # turn the cap stopped is judged, on the judge's own input budget.
    result = await _judging.judge_settled_result(result, deps=deps, record=_record)
    # Q-1 (C, D) — record-only enforcement over the model's prose, using the
    # judge's list; the verification digest is chained, never silently broken.
    return await _ct.enforce_after_judge(result, contract, record=_record)


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
        temperature: float = HOUSE_TEMPERATURE,
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
