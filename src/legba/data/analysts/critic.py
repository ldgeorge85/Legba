# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-175 critic analyst kind.

Implements the eval-loop critic-revise pattern per
``plans/design/legba_eval_loop.md`` §3:

    Reads:   ONE analyst output (FindingPayload / PredictionPayload /
             MetaFindingPayload / …) plus its analyzed analyst's
             ``descriptor.eval.rubric`` block.
    Method:  LLM judge against the rubric.  The judge model MUST differ
             from the analyzed model unless the analyzed analyst's
             descriptor explicitly sets ``eval.allow_self_correlated =
             true`` (typed schema field per L-105 §3; the legacy
             ``eval.optimizer["allow_self_correlated"]`` location is
             still accepted by the runtime as a fall-through during the
             Wave-B → Phase 9 transition).  See
             :class:`legba.data.schemas.analyst.EvalBlock`.
    Writes:  scored :class:`CritiquePayload` carrying per-rubric-dimension
             scores, an overall confidence, and a ``revision_delta``
             string the L-176 optimizer (DSPy + GEPA) consumes as a
             candidate-mutation hint.

The kind conforms to the L-102 analyst-kind contract (per
``plans/design/legba_kind_contracts.md`` §5):

  * ``KIND_NAME``           — registry key (matches ``AnalystKind.CRITIC``).
  * ``OUTPUT_KIND``         — :class:`OutputKind.CRITIQUE`.  The runtime's
                              analyst-output dispatcher reads this and
                              routes the row to ``analyst_outputs`` (the
                              CritiquePayload is the row payload; the
                              dedicated ``analyst_critiques`` table from
                              migration 0005 stays the eval-loop's
                              trace-level critique sink, NOT the
                              kind's per-run output sink — see the
                              "Output payload routing" note below).
  * ``READ_SLICE``          — per-kind reader that walks
                              ``analyst_outputs`` for ONE analyzed-output
                              row plus its rubric.
  * ``run_method``          — async ``(inputs, options, deps)`` entry
                              point.
  * ``build_prompt_module`` — lazy DSPy wrapper for the optimizer.

Output payload routing
~~~~~~~~~~~~~~~~~~~~~~

The task spec mentions an ``analyst_critiques`` table.  That table exists
(migration ``0005_runtime_tables.sql`` lines 40-53) and is the
**trace-level** sink: keyed by ``trace_id`` referencing
``analyst_traces.run_id``, holding ``rubric_uri`` + ``scores`` + ``revision
_delta`` as JSONB.  It is written by the runtime's trace-finalizer, not
by an analyst kind.

The critic analyst kind writes to the standard analyst-output dispatch
path — ``analyst_outputs`` with ``kind='critique'``.  This keeps the
output discoverable through every existing operator query (lineage,
correlator, output-feed UI) and lets the regular DLQ / NATS publish
path own its semantics.  The trace-level row in ``analyst_critiques``
remains available for the optimizer's training-window query (which
joins ``analyst_traces`` ↔ ``analyst_critiques`` on ``run_id``) — a
separate, runtime-emitted write that doesn't go through this kind.

See the "Bugs / spec gaps" note in the L-175 report for the alignment
work this leaves open.

Heterogeneity guard (per L-105)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The judge LLM must differ from the analyzed analyst's LLM unless the
analyzed analyst's descriptor sets ``eval.allow_self_correlated = true``
(typed :class:`EvalBlock` field per L-105 §3 / Wave-B integration; the
legacy ``eval.optimizer["allow_self_correlated"]`` location is still
honored by the runtime resolver as a fall-through for descriptors that
predate the typed field).  :func:`_assert_heterogeneous` enforces this;
the kind handler raises :class:`SelfCorrelatedJudgeError` when the guard
trips so the runtime can route to the DLQ via the existing kind_contracts
§7 failure semantics rather than silently land a self-graded row.

The escape hatch is intentionally awkward — analyzed analysts that want
to be self-judged have to opt in via descriptor config, which surfaces
in registry diffs and operator review.

Missing rubric
~~~~~~~~~~~~~~

If the analyzed analyst's descriptor has no ``eval.rubric``, the critic
raises :class:`MissingRubricError`.  Per L-105 §3, an analyst without a
rubric cannot be critiqued — the operator must add one before the
critic can run.  This is intentionally a hard failure (not a graceful
fallback) so the operator sees the gap.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence, runtime_checkable
from uuid import UUID

import asyncpg

from ..provenance.kinds import OutputKind
from ..provenance.models import CritiquePayload, FindingPayload
from ..tools import get_tool_definition as _get_tool_definition

# Share the package-canonical result + LLM port types with the inline_target
# sibling so the runtime's actor wrapper can dispatch this kind through the
# same code path.  (See the cross_target_raw kind for the established
# pattern.)
from .inline_target import AnalystMethodResult, LLMHandlerLike

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Kind identity (registry key — see kind_contracts §1)
# ---------------------------------------------------------------------------


KIND_NAME: str = "critic"
SCHEMA_VERSION: str = "legba/analyst.critic/1-0-0"
HANDLER_VERSION: str = "0.1.0"
PROMPT_MODULE_PATH: str = "legba.prompts.critic.v1"
"""Canonical ``legba.prompts.<kind>.v1`` path per Wave B prereq #4."""

# Output kind the host's analyst-output dispatcher writes for this kind.
# CritiquePayload + ``analyst_outputs`` table with kind='critique'.
OUTPUT_KIND: OutputKind = OutputKind.CRITIQUE


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CriticError(RuntimeError):
    """Base class for critic-kind errors."""


class SelfCorrelatedJudgeError(CriticError):
    """Raised when the judge model matches the analyzed model without the
    descriptor's ``eval.optimizer["allow_self_correlated"]`` escape hatch.

    Per L-105: a model grading its own output yields correlated noise
    rather than signal.  The runtime classifies this as a hard failure
    (kind_contracts §7); the analyzed analyst's descriptor must opt in
    explicitly before a self-graded row can land.
    """


class MissingRubricError(CriticError):
    """Raised when the analyzed analyst has no ``eval.rubric``.

    An analyst without a rubric cannot be critiqued — the operator must
    add one before the critic can run.  Hard failure on purpose so the
    gap surfaces.
    """


# ---------------------------------------------------------------------------
# Deps bundle the host injects at activation time
# ---------------------------------------------------------------------------


@runtime_checkable
class CriticDepsProtocol(Protocol):
    """Minimum dep surface ``run_method`` needs.

    Plain object with an ``llm`` attribute satisfies it; tests use a stub.
    The richer :class:`CriticDeps` dataclass is the production shape the
    runtime builds from the descriptor.
    """

    llm: LLMHandlerLike


from ._tradecraft import with_preamble  # noqa: E402

_SYSTEM_PROMPT_DEFAULT = with_preamble(
    """TASK — adversarially grade ONE analyst output you did NOT write. Your job is to find the weakest claim, not to agree. Read both blocks below: the ANALYZED OUTPUT (title, body, confidence, evidence, tags) and the RUBRIC (free-form, usually JSON named dimensions).
Identify each rubric dimension and score it 0.0-1.0 using these anchors:
  0.9 fully met with concrete evidence; 0.6 mostly met, minor gaps; 0.3 weak/partial; 0.0 absent or contradicted.
Factuality check — do this explicitly: name any claim in the body that is NOT supported by an item in the evidence list. Unsupported claims MUST lower the factuality score.
Output strict JSON, nothing else:
{"scores": {"<dimension_name>": 0.0-1.0, ...}, "overall_score": 0.0-1.0, "revision_delta": "<the single highest-impact concrete change to the analyst's prompt module — what to add / remove / reorder; empty string if none>", "confidence": 0.0-1.0}
Be conservative: a high score requires concrete evidence in the analyzed output. revision_delta must name a concrete change, never 'improve clarity'."""
)


#: Default cap on tool-use rounds in the critic's ReAct loop.
#:
#: Per L-175 the critic threads the descriptor's ``method.tools_whitelist``
#: to the judge LLM via Anthropic's native ``tool_use`` blocks.  Each
#: round = one ``chat_complete`` call.  After ``MAX_TOOL_ROUNDS`` rounds
#: without a final-text response we force a synthesis turn with tools
#: withheld so a misbehaving planner can't grind forever.  The default
#: is conservative — most critic-side trust queries resolve in 1 round.
MAX_TOOL_ROUNDS: int = 3


# Tool callable shape — the deps bundle carries a name → async-callable
# mapping the loop dispatches against.  Each callable takes the
# LLM-emitted ``args`` dict and returns a JSON-serializable result the
# loop wraps in a ``tool_result`` block.  Tool deps (HTTP clients,
# signing keys, …) are closed over by the runtime when it constructs
# the callable bound to the actor's lifecycle.
ToolCallable = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass
class CriticDeps:
    """Deps bundle the host injects at activation time.

    The runtime resolves these from the analyst descriptor's
    ``method.llm.primary`` StackRef + budget block.

    Tool surface (L-175)
    ~~~~~~~~~~~~~~~~~~~~

    ``tools`` is a name → async-callable map the critic's ReAct loop
    dispatches against when the LLM emits a ``tool_use`` block.  The
    runtime resolves this from the descriptor's ``method.tools_whitelist``
    + tool-side deps (e.g. :class:`MnemosyneTrustQueryDeps` for the
    L-211 trust-query tool); empty when the descriptor whitelists no
    tools, in which case the loop short-circuits to the single-turn
    no-tools path (no regression).  ``max_tool_rounds`` caps the loop;
    after the cap the loop forces a final-text turn with tools withheld.
    """

    llm: LLMHandlerLike
    max_tokens: int = 1536
    temperature: float = 0.1  # low — we want consistent grading
    system_prompt: str = _SYSTEM_PROMPT_DEFAULT
    tools: dict[str, ToolCallable] = field(default_factory=dict)
    tools_whitelist: list[str] = field(default_factory=list)
    max_tool_rounds: int = MAX_TOOL_ROUNDS


# ---------------------------------------------------------------------------
# Heterogeneity guard
# ---------------------------------------------------------------------------


def _assert_heterogeneous(
    analyzed_model: str,
    judge_model: str,
    *,
    allow_self_correlated: bool = False,
) -> None:
    """Refuse to grade an analyst's output with the same model that produced it.

    Per L-105 §3: a model judging its own output yields correlated noise
    (overconfidence, blind-spot mirroring).  The default refuses; the
    analyzed analyst's descriptor opts in via
    ``eval.allow_self_correlated = true`` (typed :class:`EvalBlock`
    field) if the operator accepts the noise floor.  The legacy
    ``eval.optimizer["allow_self_correlated"]`` location is still
    accepted by the runtime resolver as a fall-through for descriptors
    that predate the typed field.

    Raises
    ------
    SelfCorrelatedJudgeError
        When ``analyzed_model == judge_model`` and the escape hatch is
        not set.  The strings are compared case-insensitively after
        whitespace strip so ``"OpenAI:gpt-4o"`` and ``"openai:gpt-4o"``
        trip the guard.
    """
    a = (analyzed_model or "").strip().lower()
    j = (judge_model or "").strip().lower()
    if not a or not j:
        # Missing model identity → can't enforce; let it through with a
        # warning so the operator sees the audit gap rather than silent
        # success.  This is rare in production (the runtime always knows
        # both model strings); it's mostly a test-path scenario.
        logger.warning(
            "critic.heterogeneity.unknown_models analyzed=%r judge=%r — "
            "cannot enforce guard",
            analyzed_model, judge_model,
        )
        return
    if a == j and not allow_self_correlated:
        raise SelfCorrelatedJudgeError(
            f"refusing to self-correlate: judge_model={judge_model!r} "
            f"matches analyzed_model={analyzed_model!r}. Set "
            f"eval.optimizer.allow_self_correlated=true on the analyzed "
            f"analyst's descriptor to override."
        )


# ---------------------------------------------------------------------------
# Input shaping
# ---------------------------------------------------------------------------


_MAX_TITLE_CHARS = 200
_MAX_BODY_CHARS = 2000
_MAX_RUBRIC_CHARS = 4000
_MAX_EVIDENCE_ITEMS = 20
_MAX_TAG_ITEMS = 20


def _coerce_uuid(value: Any) -> UUID | None:
    """Best-effort UUID coercion. Returns ``None`` on failure."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _render_analyzed_output(row: Mapping[str, Any]) -> str:
    """Render the analyzed-output row into a single text block for the prompt.

    Tolerates the analyst_outputs column projection (top-level ``title`` /
    ``body`` / ``confidence``) AND the nested-payload form (``data ->
    'title'``) — the actor's substrate read can return either depending
    on which table the analyzed row lives in.
    """
    payload = row.get("payload") or row.get("data") or {}
    if not isinstance(payload, dict):
        payload = {}
    title = str(
        row.get("title")
        or payload.get("title")
        or "(untitled)"
    )[:_MAX_TITLE_CHARS]
    body = str(
        row.get("body")
        or payload.get("body")
        or ""
    )[:_MAX_BODY_CHARS]
    confidence = row.get("confidence")
    if confidence is None:
        confidence = payload.get("confidence", 0.5)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.5

    raw_evidence = row.get("evidence") or payload.get("evidence") or []
    evidence: list[str] = []
    if isinstance(raw_evidence, (list, tuple)):
        evidence = [str(e)[:200] for e in raw_evidence][:_MAX_EVIDENCE_ITEMS]

    raw_tags = row.get("tags") or payload.get("tags") or []
    tags: list[str] = []
    if isinstance(raw_tags, (list, tuple)):
        tags = [str(t)[:64] for t in raw_tags][:_MAX_TAG_ITEMS]

    lines = [
        f"TITLE:      {title}",
        f"CONFIDENCE: {confidence:.2f}",
        f"BODY:",
        body,
        "",
        f"EVIDENCE ({len(evidence)} items):",
    ]
    for i, ev in enumerate(evidence, start=1):
        lines.append(f"  [{i}] {ev}")
    lines.append("")
    lines.append(f"TAGS: {', '.join(tags) if tags else '(none)'}")
    return "\n".join(lines)


def _render_user_prompt(
    analyzed_output_block: str,
    rubric: str,
    analyzed_analyst_id: str,
) -> str:
    """Assemble the critic's user prompt."""
    rubric_trimmed = (rubric or "")[:_MAX_RUBRIC_CHARS]
    return (
        f"ANALYZED ANALYST ID: {analyzed_analyst_id}\n"
        f"\n"
        f"=== RUBRIC ===\n"
        f"{rubric_trimmed}\n"
        f"\n"
        f"=== ANALYZED OUTPUT ===\n"
        f"{analyzed_output_block}\n"
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _strip_code_fence(raw: str) -> str:
    """Strip a leading ```json fence + trailing garbage past the closing ``}``.

    Same defensive-parse shape used by the sibling kinds
    (cross_target_raw, cross_analyst_correlator, etc.).
    """
    candidate = raw.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
        candidate = candidate.strip()
    if candidate.startswith("{"):
        depth = 0
        end = len(candidate)
        for i, c in enumerate(candidate):
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        candidate = candidate[:end]
    return candidate


def _clamp_unit(value: Any, default: float = 0.5) -> float:
    """Coerce + clamp to [0.0, 1.0]; defaults on parse failure."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


def _coerce_scores(value: Any) -> dict[str, float]:
    """Coerce a JSON dict into a clean {str: float-in-unit-interval} mapping.

    Tolerates the LLM emitting scores as strings ("0.8") or as integers
    (1 / 0).  Non-coercible values drop silently — the operator sees the
    missing dimension in the row but the row still lands.
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not k:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f < 0.0:
            f = 0.0
        if f > 1.0:
            f = 1.0
        out[k[:64]] = f
    return out


def _coerce_critique(
    raw: str,
    *,
    fallback_title: str,
    analyzed_output_id: UUID | None,
    analyzed_analyst_id: str,
    analyzed_analyst_version: str,
    analyzed_model: str,
    judge_model: str,
    extra_derived: Sequence[UUID] = (),
) -> CritiquePayload:
    """Parse the LLM's JSON judgment into a :class:`CritiquePayload`.

    Robust to:
      * markdown code fences (```json ... ```),
      * trailing prose after the JSON object,
      * malformed JSON — falls back to a zero-score critique with the
        raw output in ``body`` (low confidence, ``unstructured`` tag)
        so the row still lands and the DLQ catches truly bad shapes via
        the iglu schema check at write time.
      * field-shape errors — same fallback.
    """
    candidate = _strip_code_fence(raw)
    parsed: Any
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("critic.parse_failed err=%s", exc)
        return CritiquePayload(
            title=fallback_title[:200],
            body=raw[:32000],
            confidence=0.3,
            tags=["unstructured", "critic"],
            data={"raw_llm_response": raw[:8000]},
            analyzed_output_id=analyzed_output_id,
            analyzed_analyst_id=analyzed_analyst_id[:256],
            analyzed_analyst_version=analyzed_analyst_version[:64],
            analyzed_model=analyzed_model[:128],
            judge_model=judge_model[:128],
            scores={},
            overall_score=0.0,
            revision_delta=None,
        )

    if not isinstance(parsed, dict):
        return CritiquePayload(
            title=fallback_title[:200],
            body=str(parsed)[:32000],
            confidence=0.3,
            tags=["unstructured", "critic"],
            data={"raw_llm_response": raw[:8000]},
            analyzed_output_id=analyzed_output_id,
            analyzed_analyst_id=analyzed_analyst_id[:256],
            analyzed_analyst_version=analyzed_analyst_version[:64],
            analyzed_model=analyzed_model[:128],
            judge_model=judge_model[:128],
            scores={},
            overall_score=0.0,
            revision_delta=None,
        )

    scores = _coerce_scores(parsed.get("scores"))
    overall_score = _clamp_unit(parsed.get("overall_score"), default=0.5)
    confidence = _clamp_unit(parsed.get("confidence"), default=0.5)

    raw_delta = parsed.get("revision_delta")
    revision_delta: str | None
    if raw_delta is None:
        revision_delta = None
    else:
        rd = str(raw_delta)
        revision_delta = rd[:8192] if rd.strip() else None

    # Build a narrative body for operator readability.  The structured
    # data (scores / overall_score) is on the row's typed fields; the
    # body is the human-readable summary.
    body_lines = [
        f"Critique of analyst {analyzed_analyst_id}",
        f"  judge_model={judge_model}",
        f"  overall_score={overall_score:.2f}",
        f"  confidence={confidence:.2f}",
    ]
    if scores:
        body_lines.append("  scores:")
        for dim, val in scores.items():
            body_lines.append(f"    {dim}: {val:.2f}")
    if revision_delta:
        body_lines.append(f"  revision_delta: {revision_delta}")
    body = "\n".join(body_lines)

    return CritiquePayload(
        title=fallback_title[:200],
        body=body[:65536],
        confidence=confidence,
        evidence=[],
        tags=["critic"],
        data={"raw_llm_response": raw[:8000]},
        analyzed_output_id=analyzed_output_id,
        analyzed_analyst_id=analyzed_analyst_id[:256],
        analyzed_analyst_version=analyzed_analyst_version[:64],
        analyzed_model=analyzed_model[:128],
        judge_model=judge_model[:128],
        scores=scores,
        overall_score=overall_score,
        revision_delta=revision_delta,
    )


# ---------------------------------------------------------------------------
# Substrate-read helper — one analyzed-output row
# ---------------------------------------------------------------------------


async def read_analyzed_output(
    conn: asyncpg.Connection,
    *,
    analyzed_output_id: UUID,
) -> dict[str, Any] | None:
    """Fetch one analyst-output row by id from ``analyst_outputs``.

    The critic reads exactly one row per run — the analyst-output the
    runtime selected for grading.  Returns ``None`` if the row doesn't
    exist (the runtime treats this as a NOOP).

    Column projection mirrors the sibling readers so the analyst-actor
    wrapper's lineage extraction (``derived_from = [row.id]``) keeps
    working.
    """
    row = await conn.fetchrow(
        """
        SELECT id, kind, title, body, confidence, severity, data,
               target_id, target_version, analyst_id, analyst_version,
               produced_at, derived_from, schema_uri, run_id
        FROM analyst_outputs
        WHERE id = $1
        """,
        analyzed_output_id,
    )
    if row is None:
        return None
    return dict(row)


# ---------------------------------------------------------------------------
# Per-kind substrate-slice reader bound to the actor-host dispatcher.
# ---------------------------------------------------------------------------


async def READ_SLICE(  # noqa: N802 — host-discovered constant alias
    conn,  # type: ignore[no-untyped-def]
    *,
    descriptor,  # type: ignore[no-untyped-def]
    target_filter,  # type: ignore[no-untyped-def]
    analyzed_output_id: UUID | str | None = None,
) -> list[dict[str, Any]]:
    """Adapter exposing :func:`read_analyzed_output` under the host
    dispatcher's signature.

    The critic's input is ONE analyst-output row (the row being graded)
    plus the analyzed analyst's rubric.  The rubric comes from the
    runtime via ``options["rubric"]`` (resolved upstream from the
    analyzed analyst's descriptor); the row itself is read here.

    The analyzed-output row id is resolved in this priority order:

      1. explicit ``analyzed_output_id=`` argument (test path),
      2. ``target_filter`` string when it parses as a UUID (the actor's
         per-run dispatch passes the row id here for this kind),
      3. empty list (runtime treats as NOOP).

    Returns a single-row list (or empty) so downstream lineage extraction
    (``derived_from = [row.id]``) keeps the same shape as the sibling
    kinds.
    """
    oid: UUID | None
    if analyzed_output_id is not None:
        oid = _coerce_uuid(analyzed_output_id)
    elif target_filter is not None:
        oid = _coerce_uuid(target_filter)
    else:
        oid = None

    if oid is None:
        return []

    row = await read_analyzed_output(conn, analyzed_output_id=oid)
    if row is None:
        return []
    return [row]


# ---------------------------------------------------------------------------
# REASON+ACT — direct LLM call (DSPy wrapping deferred to L-176)
# ---------------------------------------------------------------------------


async def _reason_via_llm(
    llm: LLMHandlerLike,
    *,
    user_prompt: str,
    max_tokens: int,
    temperature: float,
    system_prompt: str,
) -> tuple[str, dict[str, int]]:
    """Single chat_complete call.  Same flat-usage shape as the sibling kinds."""
    messages = [{"role": "user", "content": user_prompt}]
    response = await llm.chat_complete(
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_prompt,
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
    return content, usage_dict


# ---------------------------------------------------------------------------
# REASON+ACT (with native Anthropic tool_use blocks) — L-175 tool threading
# ---------------------------------------------------------------------------


def _resolve_tool_definitions(
    tools_whitelist: Sequence[str],
    *,
    tools: Mapping[str, ToolCallable],
) -> list[dict[str, Any]]:
    """Build the Anthropic-shaped tool list from a descriptor whitelist.

    For each whitelisted name we require BOTH:

      * A registry entry (``data/tools.get_tool_definition``) — provides
        the LLM-facing ``{name, description, input_schema}``;
      * A runtime-resolved async callable in ``tools[name]`` — what the
        loop actually dispatches against on a ``tool_use`` block.

    Names missing from either side are skipped with a warning so an
    operator-side typo or partially-wired tool doesn't crash the run.
    Returns an empty list when no usable tools are configured — the
    caller short-circuits to the single-turn no-tools path.
    """
    out: list[dict[str, Any]] = []
    for name in tools_whitelist:
        if name not in tools:
            logger.warning(
                "critic.tools.unresolved name=%r — not in deps.tools "
                "(check runtime tool-wiring for this analyst)", name,
            )
            continue
        definition = _get_tool_definition(name)
        if definition is None:
            logger.warning(
                "critic.tools.no_definition name=%r — not in TOOL_DEFINITIONS "
                "registry (add an entry in data/tools/__init__.py)", name,
            )
            continue
        out.append(definition)
    return out


def _extract_text_and_tool_uses(
    response: Any,
) -> tuple[str, list[dict[str, Any]]]:
    """Pull the text content + tool_use block list out of an LLM response.

    Handles two response shapes:

      * Pre-parsed :class:`LLMResponse`-shaped objects (production path
        via :class:`AnthropicProviderHandler`): ``content`` is the
        concatenated text-block string, ``tool_calls`` is a list of
        :class:`LLMToolCall` dataclasses with ``id`` / ``name`` /
        ``arguments`` fields.
      * Raw test-double responses with ``content`` (str) +
        ``tool_calls`` (list of dicts or duck-typed objects).

    Returns ``(text, tool_uses)`` where each tool_use is a dict shaped
    like Anthropic's tool_use block: ``{"id", "name", "input"}``.
    Empty list when the response has no tool_use blocks.
    """
    text = str(getattr(response, "content", "") or "")
    raw_calls = getattr(response, "tool_calls", None) or []
    tool_uses: list[dict[str, Any]] = []
    for tc in raw_calls:
        if isinstance(tc, Mapping):
            tc_id = tc.get("id") or ""
            tc_name = tc.get("name") or ""
            tc_args = tc.get("arguments") or tc.get("input") or {}
        else:
            tc_id = getattr(tc, "id", "") or ""
            tc_name = getattr(tc, "name", "") or ""
            tc_args = (
                getattr(tc, "arguments", None)
                or getattr(tc, "input", None)
                or {}
            )
        if not isinstance(tc_args, dict):
            tc_args = {"_value": tc_args}
        tool_uses.append({
            "id": str(tc_id),
            "name": str(tc_name),
            "input": tc_args,
        })
    return text, tool_uses


def _extract_usage(response: Any) -> dict[str, int]:
    """Pull a flat usage dict out of an LLM response.

    Tolerates both :class:`LLMUsage` dataclass and bare attribute shape.
    """
    usage_raw = getattr(response, "usage", None)
    if usage_raw is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    return {
        "prompt_tokens": int(getattr(usage_raw, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage_raw, "completion_tokens", 0) or 0),
        "reasoning_tokens": int(getattr(usage_raw, "reasoning_tokens", 0) or 0),
    }


async def _reason_via_llm_with_tools(
    llm: LLMHandlerLike,
    *,
    user_prompt: str,
    max_tokens: int,
    temperature: float,
    system_prompt: str,
    tool_definitions: list[dict[str, Any]],
    tool_callables: Mapping[str, ToolCallable],
    max_rounds: int,
) -> tuple[str, dict[str, int], list[dict[str, Any]], list[dict[str, Any]]]:
    """ReAct loop using native Anthropic ``tool_use`` blocks.

    Each round:

      1. Call ``chat_complete(messages, tools=tool_definitions)``.
      2. Aggregate the response's text-blocks + tool_use blocks.
      3. If no tool_use → break, return the text content.
      4. Otherwise execute each tool_use's callable against
         ``tool_callables[name]``, append the assistant turn and a
         user-role ``tool_result`` block, loop.

    Mirrors :func:`legba.data.analysts.consult_on_demand` 's loop
    structure but uses the LLM provider's NATIVE tool-call surface
    (``tools=[...]`` payload + parsed ``tool_calls`` on the response)
    rather than parsing JSON out of free-form content.  The two
    approaches converge — both yield a ``tool_calls_log`` the runtime
    lands in the trace's ``tool_calls`` JSONB column.

    After ``max_rounds`` rounds with no final-text response, we force
    one last synthesis turn with tools withheld so the operator always
    gets a structured critique.

    Returns
    -------
    tuple
        ``(final_text, aggregated_usage, tool_calls_log, react_steps)``.

      * ``final_text`` — the assistant's last text-only block (the JSON
        critique the caller's ``_coerce_critique`` parses).
      * ``aggregated_usage`` — sum of per-round usage dicts.
      * ``tool_calls_log`` — one entry per tool_use the LLM emitted.
        Each is JSON-serializable: ``{round, tool_use_id, name, args,
        result, ok}``.  Lands in ``analyst_traces.tool_calls`` so the
        Analyst Detail UI panel surfaces them.
      * ``react_steps`` — per-round phase trace entries the caller
        merges into the ``intermediate_steps`` list.
    """
    aggregated_usage = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
    }
    tool_calls_log: list[dict[str, Any]] = []
    react_steps: list[dict[str, Any]] = []
    messages: list[Mapping[str, Any]] = [
        {"role": "user", "content": user_prompt},
    ]
    final_text = ""

    for round_idx in range(1, max_rounds + 1):
        try:
            response = await llm.chat_complete(
                messages,
                tools=tool_definitions,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_prompt,
            )
        except Exception:
            react_steps.append({
                "phase": "reason",
                "kind": "llm_error",
                "round": round_idx,
            })
            raise

        text, tool_uses = _extract_text_and_tool_uses(response)
        round_usage = _extract_usage(response)
        for k in aggregated_usage:
            aggregated_usage[k] += round_usage.get(k, 0)
        react_steps.append({
            "phase": "reason",
            "kind": "llm_call",
            "round": round_idx,
            "tokens": (
                round_usage.get("prompt_tokens", 0)
                + round_usage.get("completion_tokens", 0)
            ),
            "tool_use_count": len(tool_uses),
        })

        if not tool_uses:
            # No tool requested — this turn IS the final answer.
            final_text = text
            break

        # Build the assistant-turn content block list (Anthropic shape).
        # The assistant message must echo BOTH the text blocks (when
        # present) and the tool_use blocks so the follow-up tool_result
        # message has a matching tool_use_id to reference.
        assistant_blocks: list[dict[str, Any]] = []
        if text.strip():
            assistant_blocks.append({"type": "text", "text": text})
        for tu in tool_uses:
            assistant_blocks.append({
                "type": "tool_use",
                "id": tu["id"],
                "name": tu["name"],
                "input": tu["input"],
            })

        # Dispatch each tool_use to its runtime-resolved callable.
        # Errors are surfaced INTO the conversation (as ``is_error: true``
        # tool_result blocks) so the LLM can recover rather than the
        # whole run crashing — same pattern as consult_on_demand.
        tool_result_blocks: list[dict[str, Any]] = []
        for tu in tool_uses:
            name = tu["name"]
            args = tu["input"] if isinstance(tu["input"], dict) else {}
            log_entry: dict[str, Any] = {
                "round": round_idx,
                "tool_use_id": tu["id"],
                "name": name,
                "args": args,
            }
            callable_ = tool_callables.get(name)
            if callable_ is None:
                err_msg = f"unknown_tool: {name!r}"
                log_entry["result"] = {"error": err_msg}
                log_entry["ok"] = False
                tool_calls_log.append(log_entry)
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": json.dumps({"error": err_msg}),
                    "is_error": True,
                })
                react_steps.append({
                    "phase": "act",
                    "kind": "tool_call",
                    "round": round_idx,
                    "tool": name,
                    "ok": False,
                    "detail": "unknown_tool",
                })
                continue
            try:
                result = await callable_(args)
            except Exception as exc:                            # noqa: BLE001
                logger.warning(
                    "critic.tool.error round=%d tool=%s err=%s",
                    round_idx, name, exc,
                )
                err_msg = f"tool_failed: {exc!s}"
                log_entry["result"] = {"error": err_msg}
                log_entry["ok"] = False
                tool_calls_log.append(log_entry)
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": json.dumps({"error": err_msg}),
                    "is_error": True,
                })
                react_steps.append({
                    "phase": "act",
                    "kind": "tool_call",
                    "round": round_idx,
                    "tool": name,
                    "ok": False,
                    "detail": "exception",
                })
                continue
            # JSON-serialize the result for the tool_result content
            # (Anthropic accepts string content; the model parses).
            if not isinstance(result, (dict, list, str, int, float, bool)) and result is not None:
                result = {"_value": str(result)}
            log_entry["result"] = result
            log_entry["ok"] = "error" not in (result or {}) if isinstance(result, dict) else True
            tool_calls_log.append(log_entry)
            try:
                result_str = json.dumps(result)
            except (TypeError, ValueError):
                result_str = json.dumps({"_value": str(result)})
            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": tu["id"],
                "content": result_str[:8000],
            })
            react_steps.append({
                "phase": "act",
                "kind": "tool_call",
                "round": round_idx,
                "tool": name,
                "ok": log_entry["ok"],
            })

        # Append the assistant turn + tool_result user turn to the
        # conversation and loop.
        messages = list(messages) + [
            {"role": "assistant", "content": assistant_blocks},
            {"role": "user", "content": tool_result_blocks},
        ]
    else:
        # Loop exhausted without a final-text response.  Force one more
        # turn with tools WITHHELD so the LLM has to produce text.
        # Mirrors the consult_on_demand forced-final shape.
        force_system = (
            system_prompt
            + "\n\nYou have reached the tool-round cap. Produce the final "
            "strict-JSON critique now using only the tool results already "
            "returned. Do not request additional tools."
        )
        try:
            response = await llm.chat_complete(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                system=force_system,
            )
        except Exception:
            react_steps.append({
                "phase": "reason",
                "kind": "forced_final_error",
            })
            raise
        text, _ = _extract_text_and_tool_uses(response)
        round_usage = _extract_usage(response)
        for k in aggregated_usage:
            aggregated_usage[k] += round_usage.get(k, 0)
        react_steps.append({
            "phase": "reason",
            "kind": "forced_final",
            "tokens": (
                round_usage.get("prompt_tokens", 0)
                + round_usage.get("completion_tokens", 0)
            ),
        })
        final_text = text

    return final_text, aggregated_usage, tool_calls_log, react_steps


# ---------------------------------------------------------------------------
# DSPy prompt module (lazy import)
# ---------------------------------------------------------------------------


def build_prompt_module() -> Any:
    """Construct and return the DSPy module bound to this analyst kind.

    Lazy-imports :class:`legba.prompts.critic.v1.CriticJudge` so this
    module imports cleanly when dspy isn't installed; raises
    :class:`ModuleNotFoundError` otherwise — matching the inline_target
    contract.

    Used by:
      * the L-176 optimizer to compile candidates against the trace set,
      * the runtime's analyst actor when DSPy is enabled (the direct
        ``chat_complete`` path is still the default for environments
        without dspy).
    """
    from legba.prompts.critic.v1 import build as _build
    return _build()


# ---------------------------------------------------------------------------
# Public entry — ``run_method`` (the runtime's AnalystRunFn)
# ---------------------------------------------------------------------------


async def run_method(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: CriticDeps | CriticDepsProtocol | LLMHandlerLike,
) -> AnalystMethodResult:
    """Execute one critic run over one analyzed-output row.

    Parameters
    ----------
    inputs:
        One-element list carrying the analyzed-output row dict (as
        returned by :func:`READ_SLICE` / :func:`read_analyzed_output`).
        An empty list is permitted — the runner emits a zero-score
        unstructured critique so the trace records the attempt.
        Multi-element lists are tolerated (the first row is graded);
        the extras get a ``multi_input`` tag for the audit trail.
    options:
        Per-run metadata.  Conventional keys:

          * ``analyst_id``, ``analyst_version``, ``run_id`` — provenance.
          * ``rubric`` — the analyzed analyst's
            ``descriptor.eval.rubric`` string.  Required when ``inputs``
            is non-empty; raises :class:`MissingRubricError` otherwise.
          * ``analyzed_model`` — model string the analyzed analyst used
            (for the heterogeneity guard).  Required for the guard to
            run; missing → guard logs a warning and lets the call
            through (audit gap surfaces in trace).
          * ``judge_model`` — model string this critic is using.
            Defaults to the critic's LLM ``subprovider`` attribute when
            absent.
          * ``allow_self_correlated`` — bool escape hatch (mirrors the
            analyzed descriptor's typed ``eval.allow_self_correlated``
            field; the runtime's critic-options resolver also accepts
            the legacy ``eval.optimizer["allow_self_correlated"]`` key
            as a fall-through for older descriptors).  Default False.

    deps:
        :class:`CriticDeps` bundle (production path) OR a bare
        :class:`LLMHandlerLike` (test path) OR any object satisfying
        :class:`CriticDepsProtocol`.

    Returns
    -------
    AnalystMethodResult
        Carrying a :class:`CritiquePayload` (typed against the L-175
        L-105 §3.2 fields) and the flat usage dict.  ``derived_from``
        includes the analyzed-output row id plus any extra context
        rows supplied via ``options["context_refs"]``.

    Raises
    ------
    SelfCorrelatedJudgeError
        When ``analyzed_model == judge_model`` and the escape hatch
        isn't set.  The runtime classifies this as a hard failure.
    MissingRubricError
        When the analyzed analyst's descriptor has no
        ``eval.rubric``.  Hard failure on purpose.
    """
    # Deps coercion — accept the dataclass, the Protocol, or a bare LLM.
    if isinstance(deps, CriticDeps):
        deps_bundle = deps
    elif hasattr(deps, "llm"):
        deps_bundle = CriticDeps(llm=deps.llm)  # type: ignore[attr-defined]
    else:
        # Bare LLM handler.
        deps_bundle = CriticDeps(llm=deps)  # type: ignore[arg-type]

    steps: list[dict[str, Any]] = []

    # --- ORIENT --------------------------------------------------------
    if not inputs:
        # Empty input — emit a zero-score unstructured critique so the
        # trace records the attempt (the runtime would normally short-
        # circuit before calling us via the NOOP/no_inputs branch in
        # AnalystActor.run, but be defensive).
        steps.append({"phase": "orient", "kind": "noop_no_inputs"})
        critique = CritiquePayload(
            title="critic: no analyzed output",
            body="The critic was invoked with an empty input slice.",
            confidence=0.0,
            tags=["empty_slice", "critic"],
        )
        return AnalystMethodResult(
            finding=critique,  # type: ignore[arg-type]
            usage={},
            derived_from=[],
            intermediate_steps=steps,
        )

    analyzed_row = inputs[0]
    if len(inputs) > 1:
        logger.warning(
            "critic.multi_input n=%d — grading first row only", len(inputs),
        )

    analyzed_output_id = _coerce_uuid(analyzed_row.get("id"))
    analyzed_analyst_id = str(
        analyzed_row.get("analyst_id")
        or options.get("analyzed_analyst_id")
        or ""
    )
    analyzed_analyst_version = str(
        analyzed_row.get("analyst_version")
        or options.get("analyzed_analyst_version")
        or ""
    )

    derived_from: list[UUID] = []
    if analyzed_output_id is not None:
        derived_from.append(analyzed_output_id)
    # Optional extra context refs (the judge may have cited these);
    # the runtime carries them through to substrate as additional
    # derived_from edges so lineage queries see the full citation set.
    raw_ctx = options.get("context_refs") or []
    if isinstance(raw_ctx, (list, tuple)):
        for ref in raw_ctx:
            u = _coerce_uuid(ref)
            if u is not None and u not in derived_from:
                derived_from.append(u)

    steps.append({
        "phase": "orient",
        "kind": "deterministic",
        "analyzed_output_id": str(analyzed_output_id) if analyzed_output_id else None,
        "analyzed_analyst_id": analyzed_analyst_id,
        "derived_count": len(derived_from),
    })

    # --- PLAN ----------------------------------------------------------
    rubric_raw = options.get("rubric")
    rubric: str = "" if rubric_raw is None else str(rubric_raw)
    if not rubric.strip():
        steps.append({"phase": "plan", "kind": "missing_rubric"})
        raise MissingRubricError(
            f"analyzed analyst {analyzed_analyst_id!r} has no eval.rubric — "
            f"cannot critique without a rubric."
        )

    analyzed_model = str(options.get("analyzed_model") or "")
    judge_model = str(
        options.get("judge_model")
        or getattr(deps_bundle.llm, "subprovider", "")
        or ""
    )
    allow_self_correlated = bool(options.get("allow_self_correlated", False))

    # --- Heterogeneity guard -------------------------------------------
    _assert_heterogeneous(
        analyzed_model,
        judge_model,
        allow_self_correlated=allow_self_correlated,
    )
    steps.append({
        "phase": "plan",
        "kind": "heterogeneity_guard_passed",
        "analyzed_model": analyzed_model or None,
        "judge_model": judge_model or None,
        "allow_self_correlated": allow_self_correlated,
    })

    analyzed_block = _render_analyzed_output(analyzed_row)
    user_prompt = _render_user_prompt(
        analyzed_output_block=analyzed_block,
        rubric=rubric,
        analyzed_analyst_id=analyzed_analyst_id,
    )
    steps.append({
        "phase": "plan",
        "kind": "render_prompt",
        "prompt_chars": len(user_prompt),
        "prompt_module": PROMPT_MODULE_PATH,
    })

    # --- REASON+ACT ----------------------------------------------------
    # L-175 tool threading: when the descriptor whitelists tools AND the
    # runtime resolved at least one callable + LLM-facing definition,
    # use the native-tool ReAct loop.  Otherwise fall back to the
    # original single-turn path so descriptors WITHOUT a tools_whitelist
    # see zero behavior change (no regression).
    tool_definitions = _resolve_tool_definitions(
        deps_bundle.tools_whitelist,
        tools=deps_bundle.tools,
    )
    tool_calls_log: list[dict[str, Any]] = []
    if tool_definitions:
        steps.append({
            "phase": "plan",
            "kind": "tools_resolved",
            "tool_count": len(tool_definitions),
            "names": [t["name"] for t in tool_definitions],
            "max_rounds": deps_bundle.max_tool_rounds,
        })
        try:
            content, usage, tool_calls_log, react_steps = (
                await _reason_via_llm_with_tools(
                    deps_bundle.llm,
                    user_prompt=user_prompt,
                    max_tokens=deps_bundle.max_tokens,
                    temperature=deps_bundle.temperature,
                    system_prompt=deps_bundle.system_prompt,
                    tool_definitions=tool_definitions,
                    tool_callables=deps_bundle.tools,
                    max_rounds=deps_bundle.max_tool_rounds,
                )
            )
        except Exception:
            steps.append({"phase": "reason", "kind": "llm_error"})
            raise
        # Merge per-round react trace into the overall step list (gives
        # the optimizer visibility into the per-round token cost +
        # tool-dispatch outcomes without flattening the structure).
        steps.extend(react_steps)
        steps.append({
            "phase": "reason",
            "kind": "tool_loop_complete",
            "subprovider": getattr(deps_bundle.llm, "subprovider", "unknown"),
            "tool_call_count": len(tool_calls_log),
            "tokens": (
                usage.get("prompt_tokens", 0)
                + usage.get("completion_tokens", 0)
            ),
        })
    else:
        try:
            content, usage = await _reason_via_llm(
                deps_bundle.llm,
                user_prompt=user_prompt,
                max_tokens=deps_bundle.max_tokens,
                temperature=deps_bundle.temperature,
                system_prompt=deps_bundle.system_prompt,
            )
        except Exception:
            steps.append({"phase": "reason", "kind": "llm_error"})
            raise
        steps.append({
            "phase": "reason",
            "kind": "llm_call",
            "subprovider": getattr(deps_bundle.llm, "subprovider", "unknown"),
            "tokens": (
                usage.get("prompt_tokens", 0)
                + usage.get("completion_tokens", 0)
            ),
        })

    # --- REFLECT -------------------------------------------------------
    fallback_title = (
        f"Critique of {analyzed_analyst_id}"
        if analyzed_analyst_id
        else "Critic judgment"
    )
    critique = _coerce_critique(
        content,
        fallback_title=fallback_title,
        analyzed_output_id=analyzed_output_id,
        analyzed_analyst_id=analyzed_analyst_id,
        analyzed_analyst_version=analyzed_analyst_version,
        analyzed_model=analyzed_model,
        judge_model=judge_model,
    )
    steps.append({
        "phase": "reflect",
        "kind": "coerce_critique",
        "overall_score": critique.overall_score,
        "score_dim_count": len(critique.scores),
        "has_revision_delta": critique.revision_delta is not None,
        "structured": "unstructured" not in critique.tags,
    })

    # --- NARRATE -------------------------------------------------------
    # Stamp tags for operator filtering — the kind constants are already
    # in tags via _coerce_critique; add analyzed_analyst_id so lineage
    # queries can find all critiques of one analyst.
    if analyzed_analyst_id:
        tags = list(critique.tags)
        marker = f"analyzed:{analyzed_analyst_id}"
        if marker not in tags:
            tags.append(marker)
        critique = critique.model_copy(update={"tags": tags[:50]})
    steps.append({
        "phase": "narrate",
        "kind": "stamp_tags",
        "tags": len(critique.tags),
    })

    # --- PERSIST envelope step (runtime does the write) ----------------
    steps.append({
        "phase": "persist",
        "kind": "envelope",
        "derived_from": len(derived_from),
    })

    return AnalystMethodResult(
        finding=critique,  # type: ignore[arg-type]
        usage=usage,
        derived_from=derived_from,
        intermediate_steps=steps,
        tool_calls=tool_calls_log,
    )


# ---------------------------------------------------------------------------
# AnalystRunFn-shaped adapter (back-compat with the spike's 2-arg call site)
# ---------------------------------------------------------------------------


class CriticRunner:
    """``AnalystRunFn``-shaped wrapper around :func:`run_method`.

    Mirrors :class:`legba.data.analysts.inline_target.InlineTargetRunner`
    so the actor wrapper in :mod:`legba.runtime.dapr_actors` can dispatch
    the critic kind through the same 2-arg ``(inputs, options)`` call
    site.
    """

    def __init__(
        self,
        llm: LLMHandlerLike,
        *,
        max_tokens: int = 1536,
        temperature: float = 0.1,
        system_prompt: str | None = None,
        tools: dict[str, ToolCallable] | None = None,
        tools_whitelist: list[str] | None = None,
        max_tool_rounds: int = MAX_TOOL_ROUNDS,
    ) -> None:
        self._deps = CriticDeps(
            llm=llm,
            max_tokens=max_tokens,
            temperature=temperature,
            system_prompt=system_prompt or _SYSTEM_PROMPT_DEFAULT,
            tools=dict(tools or {}),
            tools_whitelist=list(tools_whitelist or []),
            max_tool_rounds=max_tool_rounds,
        )

    async def __call__(
        self,
        inputs: list[dict[str, Any]],
        options: Mapping[str, Any],
    ) -> AnalystMethodResult:
        return await run_method(inputs, options, self._deps)


# ---------------------------------------------------------------------------
# Public re-exports
# ---------------------------------------------------------------------------


__all__ = [
    "AnalystMethodResult",
    "CriticDeps",
    "CriticDepsProtocol",
    "CriticError",
    "CriticRunner",
    "HANDLER_VERSION",
    "KIND_NAME",
    "LLMHandlerLike",
    "MAX_TOOL_ROUNDS",
    "MissingRubricError",
    "ToolCallable",
    "OUTPUT_KIND",
    "PROMPT_MODULE_PATH",
    "READ_SLICE",
    "SCHEMA_VERSION",
    "SelfCorrelatedJudgeError",
    "_assert_heterogeneous",
    "_coerce_critique",
    "_render_analyzed_output",
    "_render_user_prompt",
    "_strip_code_fence",
    "build_prompt_module",
    "read_analyzed_output",
    "run_method",
]
