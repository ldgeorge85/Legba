# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-170 inline_target analyst kind.

Reads ONE target's substrate slice (signals with ``produced_by ==
target_id``) and emits one structured :class:`FindingPayload` describing
the most significant patterns or events in that slice.

This is the cognitive replacement for the pre-reshape Legba cycle
(``src/legba/agent/cycle.py``). The seven phases of the legacy cycle are
preserved as the *cycle envelope* (§Cycle Envelope below); the actual
LLM reasoning is wrapped as a DSPy module per the L-105 eval-loop spec.

Per topology-redesign v2 §5.1::

    Reads:  one target's substrate slice
    Method: cycle pattern (LLM planner), per-target tool whitelist
    Writes: findings, hypotheses, situations scoped to the target

For Phase 5/6 this module produces ``FindingPayload`` only.  Future
expansions (situations + hypotheses) will land as additional output
emissions in the NARRATE phase, but the envelope stays the same.

GATHER phase (S5 — agentic assessors)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The cycle is single-shot by default.  S5 adds an OPTIONAL, bounded
**GATHER** tool-call phase between GROUND and REASON+ACT that lets the
assessor query the substrate mid-run (the new S4 read tools —
``query_nexuses`` / ``query_hypotheses`` / ``get_timeline`` /
``compare_targets`` + the original ``search_signals`` etc.) before it
synthesizes the finding.  This is the literal mechanism that moves the
cadence assessors from a *fluent one-shot* to an actual *investigation*.

It deliberately reuses consult's machinery rather than re-implementing a
ReAct loop: the JSON-extraction helper (:func:`_extract_json`), the
governed dispatch shape (``deps.agency_binding.run_tool`` →
``Agency.run_pack_tool`` — resolve ∩ allow ∩ applicability → governor →
ledger), and the tool-result → conversation folding.  It differs from
consult in three deliberate ways:

  * **Single round by default.**  Consult defaults to 6 rounds; GATHER
    defaults to ONE (``deps.max_rounds=1``, wired from
    ``method.gather.max_rounds``).  The cadence assessors run under the
    P-1 ~180s invoke timeout AND a tight per-day token budget, so the
    default stays cheap; an operator raises ``max_rounds`` (and the
    matching ``budget_tokens_per_day``) for an assessor they want to
    investigate harder.
  * **EFFECTIVE-pack opt-in, NO new flag.**  GATHER engages ONLY when a
    read pack resolves *effective* for the (assessor, target) pair — the
    assessor grants the pack via ``action_packs`` AND the target allows
    it via ``allowed_action_packs`` (inherited by the inline analyst).
    That three-way agency gate is the opt-in; the deps-builder wires
    ``deps.agency_binding`` only when the pack binds, so an un-opted-in
    target leaves ``agency_binding=None`` and the run is byte-for-byte
    the legacy single-shot path.  There is no ``inline_target_gather_
    enabled`` boolean.
  * **Budget-gate before gather, degrade-not-drop.**  The actor's
    per-analyst ``precall_check`` already gates the whole run; GATHER
    adds an OPTIONAL ``deps.budget_precheck`` hook the actor wires so the
    extra rounds are skipped (not the finding!) when headroom is tight.
    GATHER's LLM tokens flow into the run's ``usage`` and are recorded
    against the SAME per-descriptor ``budget_tokens_per_day`` by the
    actor's post-run ``budget.record`` — no new budget machinery.  If
    GATHER is skipped, errors, over-budget, or approaches the timeout,
    REFLECT still lands a finding from the one-shot synthesis.

Cycle Envelope
~~~~~~~~~~~~~~

The seven phases of the legacy cycle map onto this kind as follows:

  1. **WAKE**    — runtime gates already covered this (the actor's
                   ``_on_activate``); per-run we just record the run start.
  2. **ORIENT**  — sort + trim the substrate slice to the most relevant
                   signals (deterministic — the LLM doesn't pick).
  3. **PLAN**    — pick the prompt module + LLM options (budget hints,
                   max_tokens, temperature).  Deterministic.
  4. **REASON+ACT** — the actual LLM call.  This is where DSPy lives.
                   Tools/MCP integration is the future ReAct expansion;
                   single-shot Predict is the current shape.
  5. **REFLECT** — parse and validate the LLM JSON response into a
                   :class:`FindingPayload`.  Malformed JSON is
                   downgraded to an "unstructured" finding rather than
                   crashed (DLQ routing is a runtime concern).
  6. **NARRATE** — set provenance + tags on the finding.  This is where
                   ``derived_from = [signal.uuid, ...]`` gets attached
                   so the lineage walker can backtrack.
  7. **PERSIST** — actually writing to substrate is the runtime's job
                   (``write_analyst_output`` in ``data.provenance``).
                   The kind handler returns the *typed payload*; the
                   actor host does the I/O.  This keeps the kind pure
                   so the optimizer can replay it deterministically.

Per L-102 KC-7: the three analyst-protocol phases (fetch_inputs / run /
emit_outputs) are runtime-enforced.  The runtime calls ``run_method``
from inside its ``run`` phase — *we are just the LLM-driven reasoning
step*.  Persisting to substrate happens in ``emit_outputs`` upstream.

DSPy module wrapping (L-105 §2)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The prompt is exposed as ``legba.prompts.inline_target.v1`` per the
import path convention (``legba.prompts.<analyst_kind>.<version>``;
the per-target customizations
``legba.prompts.inline_target.<analyst_id_slug>.v1`` land when the
india_energy / taiwan / ukraine analysts ship as instances of this
kind).

DSPy is loaded lazily — installed via ``dspy-ai>=2.5`` in
``pyproject.toml`` — so the analyst module imports cleanly in
environments where dspy isn't installed (degrades to direct LLM call).
Tests that actually compile the DSPy module use
``pytest.importorskip("dspy")``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol, runtime_checkable
from uuid import UUID

from ..provenance.kinds import OutputKind
from ..provenance.models import FindingPayload
from ..schemas.analyst import IndicatorEntry
# S5 GATHER reuses consult's JSON-extraction + tool-ref helpers rather than
# re-implementing them — same shape, one source of truth.
from .consult_on_demand import (
    _coerce_uuid_list,
    _extract_json,
    _refs_from_tool_result,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Kind identity (registry key — see kind_contracts §1)
# ---------------------------------------------------------------------------


KIND_NAME = "inline_target"
SCHEMA_VERSION = "legba/analyst.inline_target/1-0-0"
HANDLER_VERSION = "0.1.0"
PROMPT_MODULE_PATH = "legba.prompts.inline_target.v1"

# Output kind the host's analyst-output dispatcher writes for this analyst.
# Read by ``legba.runtime.dapr_actors`` / ``legba.runtime.actors`` so the
# fixed ``OutputKind.FINDING`` constant gets resolved per-kind.
OUTPUT_KIND: OutputKind = OutputKind.FINDING

# READ_SLICE is None — the host uses its default signals-only reader.
READ_SLICE = None


# ---------------------------------------------------------------------------
# LLM port (Protocol re-exported so the spike's downstream callers keep working)
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMHandlerLike(Protocol):
    """Minimum slice of ``LLMProviderHandler`` the analyst method depends on.

    Mirrors the structural shape implemented by
    :class:`legba.data.stack.llm.openai.OpenAIProviderHandler` (gpt-oss-120b via
    vLLM by default per Phase 5a Q&A F) and the test-double shape used in
    ``tests/runtime/test_spike_integration.py``.
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
    """Result of one ``inline_target`` invocation.

    The runtime takes ``finding`` and emits it through the analyst's
    configured output kinds (substrate write → findings table is the
    default per topology §4.5).  ``usage`` flows into the budget enforcer
    (L-163) and the trace logger (L-105 §3).
    """

    finding: FindingPayload
    usage: dict[str, int] = field(default_factory=dict)
    # `derived_from` is the lineage tag — the signal UUIDs this finding
    # was reasoned over.  Provenance gets attached in NARRATE; the
    # runtime forwards it into ``write_analyst_output``'s `derived_from`
    # arg so a lineage walker can backtrack.
    derived_from: list[UUID] = field(default_factory=list)
    # Per-phase intermediate steps for the eval-loop trace (L-105 §3.1
    # ``intermediate_steps``).  Populated by the cycle envelope; the
    # runtime writes the list to ``analyst_traces.intermediate_steps``.
    intermediate_steps: list[dict[str, Any]] = field(default_factory=list)
    # Per-call tool invocations the analyst kind made during run_method
    # (L-175 tool-threading).  Each entry is a JSON-serializable mapping
    # describing one tool call: at minimum ``{"name", "args", "result",
    # "round"}``.  The runtime forwards the list to
    # ``receipt_chain.record(tool_calls=...)`` which lands it in
    # ``analyst_traces.tool_calls`` (JSONB).  Default empty for kinds
    # that don't invoke tools (most analyst kinds today).
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # When True, the runtime forces this run's effective output kind to
    # TRACE_ONLY — the ``analyst_outputs`` FEED row is SKIPPED (the trace +
    # in-run side-writes still run). Set by deterministic meta-handlers whose
    # summary finding is a per-run RECEIPT that should only reach the feed when
    # something actually CHANGED (e.g. situation_clustering with 0 new
    # situations, or thematic_proposal whose proposal set is unchanged from the
    # last emission) — so an idempotent re-run doesn't repeat the same finding.
    force_trace_only: bool = False


# ---------------------------------------------------------------------------
# Phase 1 — ORIENT: shape the input slice
# ---------------------------------------------------------------------------


# INPUT-side budget (2026-06-22). The assessor prompt is bounded by an estimated
# INPUT-token budget, NOT a fixed signal count and NOT a max_tokens cap on the
# model's OUTPUT: the core (vLLM / OpenAI-compat) plane serves its own output budget —
# we never send max_tokens to it — so the only lever for a sane prompt is how
# much INPUT we pack. ``_orient`` admits recency-ordered signals until the budget
# is reached; ``_MAX_INPUT_SIGNALS`` is now just a hard backstop so a flood of
# tiny signals can't blow past a sane row count.
_MAX_INPUT_SIGNALS = 200        # hard backstop count (the token budget is the real bound)
_MAX_TITLE_CHARS = 200
_MAX_SNIPPET_CHARS = 1500       # fuller per-article context (was 400)
# #116(e): a COMPACT snippet persisted onto each citation entry (data['citations'])
# — the verify judge's evidence text, kept far tighter than the prompt-render
# snippet so the JSONB row and the judge prompt stay bounded.
_CITATION_SNIPPET_CHARS = 300
_DEFAULT_INPUT_TOKEN_BUDGET = 32000
_CHARS_PER_TOKEN = 4            # rough estimate; we don't tokenize on the hot path


def _input_token_budget() -> int:
    """Estimated INPUT-token budget for the assembled signals block.

    Env ``LEGBA_LLM_INPUT_TOKEN_BUDGET`` (default 32000). Bounds the SIGNALS
    block only; the separately-bounded grounding preamble is prepended on top in
    ``run_method``. Output is never capped on the core plane.
    """
    raw = os.getenv("LEGBA_LLM_INPUT_TOKEN_BUDGET")
    if raw and raw.strip():
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            pass
    return _DEFAULT_INPUT_TOKEN_BUDGET


def _estimate_tokens(text: str) -> int:
    """Cheap chars/4 token estimate (no tokenizer on the inference hot path)."""
    return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def _produced_at_sort_key(value: Any) -> str:
    """Comparison-safe sort key for a row's ``produced_at``.

    Slice rows carry ``produced_at`` as a ``datetime`` (DB rows), a ``str``
    (JSON payloads), or NULL/absent. A bare ``... or ""`` key mixes ``datetime``
    with ``str`` and raises ``TypeError: '<' not supported between instances of
    'datetime.datetime' and 'str'`` on the FIRST NULL/str row in an
    otherwise-datetime slice — which hard-froze world_assessor + country_assessor
    (the run died before writing a trace, so they looked dormant, not failing).
    Coerce every value to a string: strings never raise on comparison and
    ISO-8601 sorts chronologically, so the recency order is preserved and the
    sort can never hard-fail. Absent → "" (sorts oldest/last under
    ``reverse=True``), matching the prior intent.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else str(value)


def _orient(
    inputs: list[Mapping[str, Any]],
    target_id: str | None,
) -> tuple[list[Mapping[str, Any]], list[UUID]]:
    """Sort + pack the substrate slice under the INPUT-token budget.

    Sort: produced_at descending so the freshest signals get the most
    weight in the LLM context (absent timestamps sort last).

    Pack: admit recency-ordered signals until the estimated INPUT-token budget
    (:func:`_input_token_budget`) is reached, capped by the
    ``_MAX_INPUT_SIGNALS`` backstop. The first signal is always admitted, so a
    single oversized article still reaches the LLM. This replaces the old fixed
    20-row trim — the bound is the INPUT-token budget, NOT a hard count, and we
    never cap the model's OUTPUT (the core plane serves its own output budget).

    Returns the list of signal UUIDs in ``derived_from`` order so the
    NARRATE phase can attach provenance.
    """
    ordered = sorted(
        inputs, key=lambda r: _produced_at_sort_key(r.get("produced_at")), reverse=True
    )

    budget = _input_token_budget()
    trimmed: list[Mapping[str, Any]] = []
    used = 0
    for row in ordered:
        if len(trimmed) >= _MAX_INPUT_SIGNALS:
            break
        cost = _estimate_tokens(_render_signal(len(trimmed) + 1, row))
        # Always admit the first signal even if it alone exceeds the budget.
        if trimmed and used + cost > budget:
            break
        trimmed.append(row)
        used += cost

    derived_from: list[UUID] = []
    for row in trimmed:
        raw_id = row.get("id")
        if raw_id is None:
            continue
        if isinstance(raw_id, UUID):
            derived_from.append(raw_id)
        else:
            try:
                derived_from.append(UUID(str(raw_id)))
            except (ValueError, AttributeError):
                # Malformed id — skip silently; the lineage walker
                # tolerates partial derived_from lists.
                continue

    logger.debug(
        "inline_target.orient target_id=%s in=%d kept=%d est_tokens=%d budget=%d derived=%d",
        target_id, len(inputs), len(trimmed), used, budget, len(derived_from),
    )
    return trimmed, derived_from


# ---------------------------------------------------------------------------
# Phase 2 — PLAN: render the user prompt
# ---------------------------------------------------------------------------


from ._tradecraft import with_preamble  # noqa: E402

_SYSTEM_PROMPT = with_preamble(
    """TASK — per-country situational assessment. Read the signals for the target country and produce ONE FINDING that SYNTHESIZES the pattern across them — the connective tissue, trajectory, or emerging/contradicting narrative — not a list of headlines.
Structure `body` as GitHub-flavored markdown:
  - a one-sentence BLUF judgment;
  - "## Key developments" — the load-bearing developments, each claim citing its signal [N];
  - "## Assessment" — what it means and the most plausible near-term trajectory (label inference as assessed);
  - "## Indicators to watch" — concrete developments that would confirm or break this assessment.
Assign an overall severity: one of low / moderate / elevated / high / critical.
Respond with STRICT JSON, nothing else:
{"title": "...", "body": "...", "confidence": 0.0-1.0, "evidence": ["..."], "tags": ["..."]}
`title`: a short dated headline. `evidence`: the most load-bearing source headlines/observations. `tags`: ALWAYS include exactly one 'severity:<level>' tag (level = the severity you assigned), the target id, and salient topic tags."""
)


def _render_signal(idx: int, row: Mapping[str, Any]) -> str:
    """Render ONE signal block (title + provenance + truncated snippet).

    Shared by the user-prompt renderer and the ORIENT token-budget estimator so
    the token accounting matches the bytes actually sent to the LLM.
    """
    title = str(row.get("title") or "(untitled)")[:_MAX_TITLE_CHARS]
    produced_at = row.get("produced_at")
    source = row.get("source_url") or ""
    data = row.get("data")
    # `produced_at` is INGESTION (fetch) time, NOT the event date — label it
    # honestly and surface the article's own published date when present, so the
    # LLM can't read fetch-time as event-time (the world-assessor temporal-collapse
    # class: a fresh June article about a Feb event got dated "today").
    published_at = data.get("published_at") if isinstance(data, dict) else None
    snippet = ""
    if isinstance(data, dict):
        snippet = (
            data.get("summary")
            or data.get("description")
            or data.get("content_text")
            or data.get("snippet")
            or ""
        )
        if not isinstance(snippet, str):
            snippet = str(snippet)
        snippet = snippet[:_MAX_SNIPPET_CHARS]
    published_str = f" published={published_at}" if published_at else ""
    return (
        f"[{idx}] {title}\n"
        f"    ingested={produced_at}{published_str} source={source}\n"
        f"    snippet={snippet}"
    )


def _render_user_prompt(
    inputs: list[Mapping[str, Any]],
    target_id: str | None,
) -> str:
    """Render the (already ORIENTed) substrate slice into a user prompt.

    Each signal carries title + provenance + a snippet truncated to
    ``_MAX_SNIPPET_CHARS``; the slice itself is bounded by the INPUT-token
    budget in :func:`_orient`.
    """
    header = (
        f"Target: {target_id or 'unspecified'}\n"
        f"Number of signals: {len(inputs)}\n\n"
    )
    body_lines = [_render_signal(i, row) for i, row in enumerate(inputs, start=1)]
    return header + "\n".join(body_lines)


# ---------------------------------------------------------------------------
# Citation index (P0-T1 — cite the prose)
# ---------------------------------------------------------------------------
#
# The assessor prompt asks every claim to cite its signal ``[N]`` (see
# ``_SYSTEM_PROMPT`` "## Key developments"), where ``N`` is the 1-based position
# of the signal in the ORIENTed slice — the SAME index ``_render_signal`` stamps
# onto each rendered block. That ``N -> signal.id`` correspondence is the only
# bridge from the prose markers back to the substrate rows, so we capture it at
# render time and reuse it in REFLECT to resolve the markers into structured
# ``data['citations']`` (the prose itself is left intact — we ADD ids alongside it).

# A bare ``[3]`` marker (not ``[3](url)`` / ``[3,4]`` / ``[link]``). Matches the
# digits inside the brackets; the synthesis prose uses ``[N]`` per the prompt.
_CITATION_MARKER_RE = re.compile(r"\[(\d+)\]")

# Non-ASCII citation brackets that wrap a bare integer. Some core-plane models
# (gpt-oss, Qwen-family) emit full-width / CJK lenticular brackets — ``【3】`` /
# ``［3］`` / ``〔3〕`` — instead of ASCII ``[3]``. Without normalization the
# ``[N]`` parser misses them entirely, so a CORRECTLY-cited finding resolves to
# ZERO citations: it breaks drill-to-source AND tanks the faithfulness verify
# (an apparently-cited claim that the verifier can't bind to a signal). Only
# digit-wrapping pairs are rewritten, so non-citation prose using these glyphs is
# left intact. (Caught live 2026-06-30 on the energy_security unit: ``【3】【76】``
# scored faithfulness 0.00 with 0 resolved citations.)
_VARIANT_CITATION_RE = re.compile(r"[【［〔〖](\s*\d+\s*)[】］〕〗]")


def _normalize_citation_markers(text: str) -> str:
    """Rewrite ``【N】``/``［N］``-style citation brackets to ASCII ``[N]``.

    Idempotent on already-ASCII prose. Run BEFORE ``_extract_citations`` and
    persist the result so the stored prose, the marker parser, and the UI
    citation chips all key on the SAME ``[N]``.
    """
    if not text:
        return text
    return _VARIANT_CITATION_RE.sub(lambda m: f"[{m.group(1).strip()}]", text)


def _build_citation_index(
    sliced: list[Mapping[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Map each ``[N]`` index -> the signal it rendered for, plus cheap fields.

    ``N`` is the 1-based position in the ORIENTed slice (``_render_signal``'s
    ``idx``), so this is the authoritative reverse map for the markers the LLM
    cites. Captures the signal_id (when the row carries a resolvable id) plus the
    title/source that are already on the row, so a citation entry can render a
    chip without re-joining the substrate. Rows with no resolvable id are still
    indexed (id ``None``) so the parser can count a marker as present-but-unmapped
    rather than silently swallow it.
    """
    index: dict[int, dict[str, Any]] = {}
    for n, row in enumerate(sliced, start=1):
        raw_id = row.get("id")
        signal_id: str | None = None
        if isinstance(raw_id, UUID):
            signal_id = str(raw_id)
        elif raw_id is not None:
            try:
                signal_id = str(UUID(str(raw_id)))
            except (ValueError, AttributeError):
                signal_id = None
        title = row.get("title")
        source = row.get("source_url")
        # #116(e): capture a compact evidence SNIPPET (the signal's own
        # summary/lede) alongside the title so the faithfulness judge can confirm
        # a specific claim against the cited signal's CONTENT, not just its
        # headline (a title alone is often too terse → a properly-cited clause is
        # mis-graded down). Same field precedence _render_signal uses.
        data = row.get("data")
        snippet = ""
        if isinstance(data, dict):
            raw_snip = (
                data.get("summary")
                or data.get("description")
                or data.get("content_text")
                or data.get("snippet")
                or ""
            )
            if not isinstance(raw_snip, str):
                raw_snip = str(raw_snip)
            snippet = raw_snip.strip()[:_CITATION_SNIPPET_CHARS]
        index[n] = {
            "signal_id": signal_id,
            "title": str(title) if title is not None else None,
            "source": str(source) if source else None,
            "snippet": snippet or None,
        }
    return index


def _extract_citations(
    body: str,
    index: Mapping[int, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    """Resolve the ``[N]`` markers in ``body`` against the render-time index.

    Returns ``(citations, marker_count, resolved_count)``:
      * ``citations`` — one entry per DISTINCT marker that maps to a real
        signal_id, in first-appearance order:
        ``{"marker": "[N]", "signal_id": <id>, "title"?, "source"?}``. A marker
        whose index has no resolvable signal_id is COUNTED but NOT emitted (no
        fabricated id) — same for an out-of-range marker.
      * ``marker_count`` — the number of DISTINCT ``[N]`` markers in the prose.
      * ``resolved_count`` — how many of those resolved to a citation entry.
    """
    citations: list[dict[str, Any]] = []
    seen: set[int] = set()
    marker_count = 0
    for match in _CITATION_MARKER_RE.finditer(body or ""):
        n = int(match.group(1))
        if n in seen:
            continue
        seen.add(n)
        marker_count += 1
        entry = index.get(n)
        if not entry or not entry.get("signal_id"):
            # Out-of-range or unresolved index — count it, never fabricate an id.
            continue
        citation: dict[str, Any] = {
            "marker": f"[{n}]",
            "signal_id": entry["signal_id"],
        }
        if entry.get("title"):
            citation["title"] = entry["title"]
        if entry.get("source"):
            citation["source"] = entry["source"]
        # #116(e): carry the compact evidence snippet so the verify judge sees the
        # cited signal's CONTENT, not just its headline (see _marker_to_evidence).
        if entry.get("snippet"):
            citation["snippet"] = entry["snippet"]
        citations.append(citation)
    return citations, marker_count, len(citations)


# ---------------------------------------------------------------------------
# Phase 4 — REFLECT: parse the LLM response into FindingPayload
# ---------------------------------------------------------------------------


def _title_from_text(text: str, *, fallback_title: str) -> str:
    """D27: derive an LLM-authored title from free-text content.

    The static ``fallback_title`` ("Assessment for country_g20_XX") is a
    placeholder that surfaces on the product UI as a non-title; when the LLM
    DID author prose (even unparseable JSON / a bare narrative), there is almost
    always a real headline in it. Prefer, in order:
      1. a leading markdown heading line (``# ...`` / ``## ...``);
      2. a ``BLUF:`` / ``Bottom line:`` lead-in (strip the label, take the line);
      3. the first non-empty, non-JSON-punctuation line.
    Falls back to ``fallback_title`` only when no usable line exists (e.g. the
    LLM returned whitespace). Returned trimmed + length-capped to the column.
    """
    if not text:
        return fallback_title[:2048]
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Skip a bare JSON-envelope open/close brace line.
        if line in ("{", "}", "[", "]"):
            continue
        # Markdown heading → use the heading text.
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            if heading:
                return heading[:2048]
            continue
        # BLUF / bottom-line lead-in → strip the label.
        low = line.lower()
        for label in ("bluf:", "bottom line:", "bottom-line:", "**bluf:**"):
            if low.startswith(label):
                rest = line[len(label):].strip().lstrip("*").strip()
                if rest:
                    return rest[:2048]
        # First usable narrative line — strip a leading markdown emphasis.
        cleaned = line.strip("*").strip()
        if cleaned and cleaned not in ("{", "}"):
            return cleaned[:2048]
    return fallback_title[:2048]


def _unwrap_envelope(body: str, *, _max_depth: int = 6) -> tuple[str, str | None]:
    """D27 (second pass): unwrap a body that is itself a stringified
    ``{title, body}`` JSON envelope, returning ``(inner_body, inner_title)``.

    The W4 D27 fix only handled the double-wrapped *dict* case
    (``parsed["body"]`` is itself a stringified envelope). The live us/cn/fr/za
    findings hit a wider shape: the ``body`` COLUMN is a JSON-stringified
    envelope — a plain string that literally starts with ``{"title": ...}`` (or
    a fenced ```` ```json ```` block of one). When that shape is detected this
    parses it, returns the inner ``body`` (markdown) AND lifts the inner
    ``title`` so the caller can prefer it over the static placeholder. When the
    input is NOT such an envelope, returns ``(body, None)`` unchanged — the
    plain-markdown / non-JSON path is byte-for-byte preserved.

    #125: unwraps RECURSIVELY (bounded by ``_max_depth``) so an envelope nested
    more than one level deep — ``body`` is a stringified envelope whose own
    ``body`` is *again* a stringified envelope — is fully peeled to the innermost
    markdown instead of leaving a raw JSON string in the body column. The lifted
    title tracks the innermost envelope that carried one.
    """
    if not body:
        return body, None
    current = body
    title: str | None = None
    for _ in range(_max_depth):
        candidate = current.strip()
        if candidate.startswith("```"):
            candidate = candidate.strip("`")
            if candidate.lower().startswith("json"):
                candidate = candidate[4:]
            candidate = candidate.strip()
        if not candidate.startswith("{"):
            break
        try:
            inner = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            break
        if not (isinstance(inner, dict) and "body" in inner):
            break
        inner_body = inner.get("body")
        if not (isinstance(inner_body, str) and inner_body.strip()):
            break
        inner_title = inner.get("title")
        if isinstance(inner_title, str) and inner_title.strip():
            title = inner_title.strip()
        current = inner_body
    if current is body:
        return body, None
    return current, title


def _unwrap_envelope_body(body: str) -> str:
    """D27: if a body string is itself a raw ``{title, body}`` JSON envelope,
    unwrap it so the body column holds the rendered markdown, not the JSON.

    Backward-compatible thin wrapper over :func:`_unwrap_envelope` that returns
    only the inner body (markdown). Returns the input unchanged when no envelope
    shape is detected. Use :func:`_unwrap_envelope` directly when you also need
    the lifted inner title.
    """
    inner_body, _inner_title = _unwrap_envelope(body)
    return inner_body


def _coerce_indicators(raw: Any) -> list[dict[str, Any]]:
    """Lenient per-entry coercion of the LLM's ``indicators`` array (S3-T1).

    Coerces each entry into the typed :class:`IndicatorEntry` shape and DROPS any
    malformed entry (degrade-not-drop) so a single bad indicator never DLQs an
    otherwise-good finding; the strict ``FindingPayload`` validator then re-checks
    what survives. Returns ``[]`` when the block is absent / not a list.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(IndicatorEntry(**entry).model_dump(mode="json"))
        except Exception as exc:  # noqa: BLE001 — degrade-not-drop; skip the bad one
            logger.debug("inline_target.indicator.dropped err=%s entry=%s", exc, entry)
            continue
    return out


# #125: locate the ``"body": "..."`` field inside a broken/truncated envelope.
_ENVELOPE_BODY_KEY_RE = re.compile(r'"body"\s*:\s*"', re.IGNORECASE)


def _salvage_envelope_body(raw: str) -> str | None:
    """#125: best-effort recovery of the inner markdown ``body`` from a
    MALFORMED or TRUNCATED ``{"title": ..., "body": ...}`` JSON envelope that
    :func:`json.loads` could not parse.

    The parse-fallback branches of :func:`_coerce_finding` used to store the raw
    LLM text verbatim. When that raw text is a *recognizable but broken* JSON
    envelope (a truncated stream that cut off mid-``body``, or an envelope with
    trailing corruption), storing it verbatim leaks a raw ``{"title": ...}``
    JSON string into the body column — the exact residual #125 targets.

    This fence-strips the raw, and when it still looks like a JSON object,
    extracts the ``body`` string value by scanning from its opening quote to the
    first UNESCAPED closing quote (or to end-of-text when the envelope is
    truncated mid-string), then JSON-decodes the escapes so the body column holds
    rendered markdown. Returns ``None`` when the raw is NOT an envelope object or
    carries no ``body`` field — the caller then keeps the raw prose as-is (it is
    not JSON scaffolding), preserving the plain-text fallback byte-for-byte.
    """
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    if not text.startswith("{"):
        return None
    match = _ENVELOPE_BODY_KEY_RE.search(text)
    if not match:
        return None
    start = match.end()  # first char of the string value
    idx = start
    escaped = False
    end: int | None = None
    while idx < len(text):
        char = text[idx]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            end = idx
            break
        idx += 1
    raw_value = text[start:end] if end is not None else text[start:]
    # Decode JSON string escapes (\n, \", \uXXXX, ...). A truncated tail can
    # leave the encoded string unparseable — fall back to a minimal unescape so
    # we still surface markdown rather than the JSON wrapper.
    try:
        decoded = json.loads('"' + raw_value + '"')
    except (json.JSONDecodeError, ValueError):
        decoded = (
            raw_value.replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace('\\"', '"')
        )
    decoded = decoded.strip()
    return decoded or None


def _coerce_finding(raw: str, *, fallback_title: str) -> FindingPayload:
    """Parse the LLM's JSON response into a :class:`FindingPayload`.

    Robust to:
      * markdown code fences (```json ... ```).
      * trailing text after the JSON object.
      * malformed JSON — falls back to an "unstructured" finding whose
        body carries the raw LLM output so the operator can re-curate.
      * field-shape errors — same fallback.

    D27 (product surface): on every fallback path, derive the title from the
    LLM-authored prose (``_title_from_text``) instead of the static
    "Assessment for country_g20_XX" placeholder; and unwrap a body that is a
    JSON-stringified ``{title, body}`` envelope (``_unwrap_envelope``) so the
    body column holds rendered markdown rather than raw JSON — lifting the
    inner envelope's title when the outer title is missing.

    Failure is non-fatal here on purpose: the runtime's
    ``write_analyst_output`` validates against the iglu schema and
    routes a *truly* malformed payload to the DLQ.  This function's job
    is to land a row that *looks* like a finding so the analyst's run
    stays auditable; the DLQ catches anything still wrong.
    """
    parsed: Any
    try:
        candidate = raw.strip()
        # Strip markdown fences if present.
        if candidate.startswith("```"):
            candidate = candidate.strip("`")
            if candidate.lower().startswith("json"):
                candidate = candidate[4:]
            candidate = candidate.strip()
        # Strip trailing garbage past the closing brace.
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
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("inline_target.finding.parse_failed err=%s", exc)
        # D27: the LLM authored prose that didn't parse as JSON — keep its text
        # as the body (rendered markdown) and lift a real title from it instead
        # of the static placeholder.
        # #125: but if the un-parseable raw is a MALFORMED/TRUNCATED {title,body}
        # envelope, salvage the inner markdown body first — never persist a raw
        # {"title": ..., "body": ...} JSON string as the body column.
        salvaged = _salvage_envelope_body(raw)
        body = salvaged if salvaged is not None else raw
        return FindingPayload(
            title=_title_from_text(body, fallback_title=fallback_title),
            body=body[:65536],
            confidence=0.3,
            tags=["unstructured"],
        )

    if not isinstance(parsed, dict):
        body = str(parsed)
        return FindingPayload(
            title=_title_from_text(body, fallback_title=fallback_title),
            body=body[:65536],
            confidence=0.3,
            tags=["unstructured"],
        )

    try:
        # D27 (second pass): the live us/cn/fr/za findings carry a body that is
        # itself a JSON-STRINGIFIED ``{title, body}`` envelope. Unwrap it to the
        # inner markdown body AND lift its inner title so a real headline
        # survives even when the OUTER title is missing/placeholder.
        body, inner_title = _unwrap_envelope(str(parsed.get("body") or ""))
        # Title precedence: the LLM's own outer title → the unwrapped inner
        # envelope's title → a heading lifted from the rendered body → the
        # static placeholder.
        title = str(parsed.get("title") or "").strip()
        if not title and inner_title:
            title = inner_title
        if not title:
            title = _title_from_text(body, fallback_title=fallback_title)
        # S3-T1: the OPTIONAL structured I&W block. The unit prompts emit it as a
        # top-level `indicators` array alongside the prose; tolerate a model that
        # nested it under `data`. Coerce leniently (drop malformed entries) so a
        # bad indicator never DLQs an otherwise-good finding.
        ind_raw = parsed.get("indicators")
        if ind_raw is None and isinstance(parsed.get("data"), dict):
            ind_raw = parsed["data"].get("indicators")
        data: dict[str, Any] = {"raw_llm_response": raw[:8000]}
        indicators = _coerce_indicators(ind_raw)
        if indicators:
            data["indicators"] = indicators
        return FindingPayload(
            title=title[:2048],
            body=body[:65536],
            confidence=float(parsed.get("confidence", 0.5)),
            evidence=[str(e) for e in (parsed.get("evidence") or [])][:50],
            tags=[str(t) for t in (parsed.get("tags") or [])][:50],
            data=data,
        )
    except Exception as exc:                            # pragma: no cover
        logger.warning("inline_target.finding.coerce_failed err=%s", exc)
        # #125: same envelope-salvage guard as the parse-failed branch.
        salvaged = _salvage_envelope_body(raw)
        body = salvaged if salvaged is not None else raw
        return FindingPayload(
            title=_title_from_text(body, fallback_title=fallback_title),
            body=body[:65536],
            confidence=0.3,
            tags=["coerce_failed"],
        )


# ---------------------------------------------------------------------------
# Phase 5 — NARRATE: stamp tags + provenance hooks (in-process only)
# ---------------------------------------------------------------------------


def _narrate(
    finding: FindingPayload,
    *,
    target_id: str | None,
    analyst_id: str | None,
) -> FindingPayload:
    """Stamp the finding with provenance hints the runtime expects.

    The actual ``produced_by = analyst_id`` and ``derived_from = [...]``
    columns are set by ``write_analyst_output`` from ``AnalystContext +
    derived_from``; we surface the in-payload tags here so the operator
    UI and lineage queries can filter by target without joining the
    substrate row.
    """
    # Don't double-stamp if the LLM already produced these tags.
    tags = list(finding.tags)
    if target_id and target_id not in tags:
        tags = tags + [f"target:{target_id}"]
    if analyst_id and analyst_id not in tags:
        tags = tags + [f"analyst:{analyst_id}"]
    # Cap tag count to keep within payload limits.
    tags = tags[:50]

    # Lift the LLM's entity tags into STRUCTURED data so the deterministic
    # finding_supersession analyst can derive a situation signature and cluster
    # near-duplicate findings. Without data.key_entities + data.category,
    # derive_signature() returns None for every finding and the supersession
    # leg produces zero links (the inert-analysis-leg gap). The entities are the
    # LLM's own tags (pre-synthetic ones, here = finding.tags); category=target
    # scope keeps clusters per-target so unrelated same-entity findings across
    # countries don't over-merge.
    key_entities = [
        t for t in finding.tags
        if t
        and not t.startswith(("target:", "analyst:"))
        and not str(t).strip().isdigit()
        and len(str(t).strip()) >= 2
    ][:32]
    new_data = dict(finding.data) if isinstance(finding.data, dict) else {}
    if key_entities and "key_entities" not in new_data:
        new_data["key_entities"] = key_entities
    if target_id and "category" not in new_data:
        new_data["category"] = target_id
    return finding.model_copy(update={"tags": tags, "data": new_data})


# ---------------------------------------------------------------------------
# Phase 3 — REASON+ACT: DSPy module wrapping (lazy import)
# ---------------------------------------------------------------------------


def build_prompt_module() -> Any:
    """Construct and return the DSPy module bound to this analyst kind.

    Imported by the L-176 optimizer (DSPy + GEPA) to compile a candidate
    over a recorded trace set.  Imported by the runtime at analyst-actor
    activate time *only when DSPy is installed* — otherwise the runner
    falls back to the direct ``chat_complete`` path with the same prompt
    text.

    Raises :class:`ModuleNotFoundError` if dspy isn't installed.  Call
    sites that don't want to hard-require dspy must guard with
    ``importlib.util.find_spec("dspy")`` or wrap in try/except.
    """
    # Late import — keeps module-level import cheap and dspy optional.
    from legba.prompts.inline_target.v1 import InlineTargetCycle
    return InlineTargetCycle()


# ---------------------------------------------------------------------------
# Phase 3 — REASON+ACT: the LLM call (direct fallback, no dspy needed)
# ---------------------------------------------------------------------------


async def _reason_via_llm(
    llm: LLMHandlerLike,
    *,
    user_prompt: str,
    max_tokens: int,
    temperature: float,
    system_prompt: str,
    messages: list[Mapping[str, Any]] | None = None,
) -> tuple[str, dict[str, int]]:
    """Single chat_complete call.  Mirrors the spike's shape.

    Returns ``(content_str, usage_dict)`` where ``usage_dict`` is the
    flat token-accounting form the budget enforcer expects.  Raises
    whatever the underlying handler raises — the caller is responsible
    for catching and routing to ``TransientFailure`` / ``HardFailure``.

    The single-shot synthesis call passes ``user_prompt`` (a one-message
    conversation).  The S5 GATHER loop passes a full ``messages`` list (the
    accumulating tool-call conversation) instead.
    """
    if messages is None:
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
# Public entry — ``run_method`` (the runtime's AnalystRunFn)
# ---------------------------------------------------------------------------


# Tier-1 knowledge-grounding hook (L-241 grounding). When the descriptor's
# ``grounding`` block is enabled, the deps-builder closes over a substrate
# resolver + the descriptor's grounding config and installs this callable on
# the deps. Called once per run with the (already-read) signal slice + the run
# options (carrying ``target_id``); returns the dated "AUTHORITATIVE CURRENT
# CONTEXT" preamble string to PREPEND to the LLM user prompt, or ``None`` when
# there is nothing current to inject. Off (``None``) for every analyst that
# doesn't opt in — the run path is byte-for-byte unchanged for them.
GroundingHook = Callable[
    [list[Mapping[str, Any]], Mapping[str, Any]], Awaitable[str | None]
]


# PER-PHASE LLM SPLIT — the gpt-oss "Reasoning: high" directive. vLLM does NOT
# honor a ``reasoning_effort`` wire arg (vllm.py:106,:129); gpt-oss takes this
# directive injected into the system/message content. It is prepended to the
# GATHER system prompt ONLY when ``deps.gather_reasoning_high`` is set (the
# journal's gpt-oss/vLLM gather plane), so the heavy investigation rounds think hard
# while the Opus voice calls — which never carry the gather suffix — are unpolluted.
_REASONING_HIGH_DIRECTIVE = "Reasoning: high"


# S5 — GATHER phase tuning.
#
# Default ONE round (vs consult's 6): the cadence assessors run under the P-1
# ~180s invoke timeout + a tight per-day token budget. ``_GATHER_ROUNDS_CEILING``
# is the hard clamp the runner applies regardless of what a descriptor requests,
# so a mis-set ``method.gather.max_rounds`` can never grind forever inside a
# cadence tick.
_GATHER_DEFAULT_ROUNDS = 1
_GATHER_ROUNDS_CEILING = 6

# Soft latency guard: when this much of the descriptor's invoke timeout has
# already elapsed, stop opening new GATHER rounds and go straight to synthesis
# so the run always lands a finding inside the P-1 window (degrade-not-drop).
_GATHER_TIMEOUT_BUDGET_FRACTION = 0.6

# The GATHER tool surface, appended to the assessor's system prompt only for the
# tool-call turns. Mirrors consult's tool catalogue (the same ``substrate_read``
# pack), but the loop protocol is GATHER-shaped: a tool call OR a single
# ``{"done": true}`` to proceed to synthesis. The final FINDING is produced by
# the existing one-shot REASON+ACT call (NOT here), so the assessor's finding
# schema and prompt stay unchanged — GATHER only enriches the context.
#
# SEAM #22: the read surface is ALWAYS described; the external (``web_access``)
# and write-back (``propose_facts``) tool guidance is spliced in ONLY when the
# running assessor is bound to those packs — see ``_gather_system_suffix``. The
# splice text is lifted verbatim from each pack descriptor's
# ``prompt_fragments`` + ``rules`` (the operator-authored tool-use guidance), so
# the in-run instruction tracks the descriptor, not a hardcoded copy that can
# drift from it.
_GATHER_SYSTEM_SUFFIX = (
    "\n\nBefore you write the finding you may FIRST query the substrate to "
    "ground your assessment. Each query must be a single strict-JSON object.\n"
    "Available tools:\n"
    "  - search_signals(query, [category], [limit]) — full-text signal search.\n"
    "  - query_facts([subject], [predicate], [value], [limit]) — fact store.\n"
    "  - inspect_entity(name) — entity profile + recent facts.\n"
    "  - query_nexuses([subject], [object], [rel_type], [polarity], [limit]) — "
    "open signed/typed relationships.\n"
    "  - query_hypotheses([target_id], [status], [situation_id], [limit]) — "
    "competing-hypothesis (ACH) rows.\n"
    "  - get_timeline(subject, [limit]) — time-ordered facts ∪ signals.\n"
    "  - compare_targets(target_ids) — side-by-side substrate rollup.\n"
    "  - list_findings([target_id], [analyst_id], [severity], [since_hours], "
    "[limit]) — the platform's OWN prior assessments/findings (analyst "
    "products). Check these FIRST to build on and reconcile against earlier "
    "work; cite the output_id. effective_confidence already folds in the "
    "critic.\n"
    "  - list_situations([status], [target_id], [since_hours], [limit]) — "
    "ongoing situation frames the platform has clustered (analysis-derived). "
    "Use a situation_id with query_hypotheses to pull its ACH rows.\n"
    "  - query_predictions([target_id], [status], [limit]) — the platform's "
    "event-volume forecasts (forecast_method='naive_mean' means no trend could "
    "be fit, low-confidence; 'auto_arima' means fitted); "
    "cite the output_id.\n\n"
    "Protocol:\n"
    '  - To query, reply with strict JSON: {"tool": "<name>", "args": {...}}\n'
    '  - When you have gathered enough, reply with: {"done": true}\n'
    "  - Do not write the finding yet — you will be asked for it after gathering."
)

# SEAM #22 — external + write tool guidance, spliced into the GATHER suffix only
# when the matching pack is EFFECTIVE for this (assessor, target) run. The
# descriptions name the tool signatures; the operator-authored tool-use rules
# (cite-the-URL / require derived_from / propose-not-assert) come from the pack
# descriptors via ``_gather_system_suffix``.
_WEB_TOOLS_SUFFIX = (
    "\n\nEXTERNAL EVIDENCE (web_access pack — egress is SSRF-guarded; a blocked "
    "host is a clean tool failure, not a crash):\n"
    "  - web_search(query, [limit]) — query the operator-pinned search endpoint; "
    "returns {title, url, snippet} results.\n"
    "  - web_fetch(url) — GET one absolute http(s) URL through the guarded "
    "transport; returns its (capped) text body.\n"
)
_WRITE_TOOLS_SUFFIX = (
    "\n\nWRITE-BACK (propose_facts pack — these PROPOSE, they do NOT assert "
    "truth; every write REQUIRES derived_from lineage citing the substrate "
    "UUIDs it is grounded in):\n"
    "  - propose_fact(subject, predicate, value, derived_from=[uuid,...], "
    "[confidence]) — write one proposed-grade fact (source_type='proposed', "
    "confidence clamped).\n"
    "  - request_source(need, [rationale], derived_from=[uuid,...]) — record a "
    "coverage / evidence gap.\n"
    "  - open_question(question, [counter], derived_from=[uuid,...]) — record an "
    "unresolved analytical question.\n"
)

# GATHER read tools — the substrate_read pack's tool surface (S4).
_GATHER_READ_TOOLS = (
    "search_signals",
    "query_facts",
    "inspect_entity",
    "vector_search",
    "query_nexuses",
    "query_hypotheses",
    "get_timeline",
    "compare_targets",
    # Finished-intelligence reads — the platform's OWN prior products, so an
    # assessor can build on (and reconcile against) earlier assessments rather
    # than re-derive from the raw signal firehose every run.
    "list_findings",
    "list_situations",
    "query_predictions",
)

# SEAM #22 — external (web_access) + write-back (propose_facts) tool names. A
# GATHER round may invoke these ONLY when the runner is passed a per-tool
# binding for the owning pack (``options['gather_tool_bindings']``); the binding
# is built by the host iff the pack is EFFECTIVE (assessor grant ∩ target allow)
# and re-pointed per run by the actor. Read tools route through the default
# ``substrate_read`` binding; these route through their own pack's binding so
# ``Agency.run_pack_tool`` enforces tool↔pack ownership.
_GATHER_WEB_TOOLS = (
    "web_fetch",
    "web_search",
)
_GATHER_WRITE_TOOLS = (
    "propose_fact",
    "request_source",
    "open_question",
)

# The full set the GATHER loop will dispatch. Membership here only means "a
# recognized tool name"; whether a call is actually admitted is decided by the
# three-way gate inside the routed binding's ``run_pack_tool``. A read tool with
# no write/web binding wired simply has no per-tool binding and falls back to the
# substrate_read binding; a write/web tool with no binding is reported as an
# unbound tool (a loud no-op folded back to the planner), never an ungoverned call.
_GATHER_TOOLS = _GATHER_READ_TOOLS + _GATHER_WEB_TOOLS + _GATHER_WRITE_TOOLS


def _gather_system_suffix(
    *,
    web_fragments: list[str] | None = None,
    write_fragments: list[str] | None = None,
) -> str:
    """Build the GATHER system suffix, splicing in external/write guidance.

    The read surface is always present. ``web_fragments`` / ``write_fragments``
    are the owning pack descriptors' ``prompt_fragments`` + ``rules`` (operator
    authored), appended verbatim under the tool-signature block so the in-run
    instruction tracks the descriptor. Empty/None → that section is omitted
    (the pack is not bound for this run), keeping the read-only suffix
    byte-for-byte unchanged for a non-write assessor.
    """
    suffix = _GATHER_SYSTEM_SUFFIX
    if web_fragments is not None:
        suffix += _WEB_TOOLS_SUFFIX
        for frag in web_fragments:
            frag = str(frag).strip()
            if frag:
                suffix += f"  {frag}\n"
    if write_fragments is not None:
        suffix += _WRITE_TOOLS_SUFFIX
        for frag in write_fragments:
            frag = str(frag).strip()
            if frag:
                suffix += f"  {frag}\n"
    return suffix


# Optional budget-headroom precheck. The actor wires this (closing over the
# per-analyst BudgetEnforcer + a connection) so GATHER can be skipped — NOT the
# finding — when the per-day cap has no room for the extra rounds. Returns True
# when there is headroom to gather, False to skip. ``None`` → no precheck (the
# default for tests/embedders; the run still budget-records its usage).
BudgetPrecheck = Callable[[], Awaitable[bool]]


@dataclass
class InlineTargetDeps:
    """Bundle the runtime passes to ``run_method``.

    The runtime resolves these from the analyst descriptor's
    ``method.llm.primary`` StackRef + budget block + cadence block.
    """

    llm: LLMHandlerLike
    max_tokens: int = 1024
    temperature: float = 0.2
    system_prompt: str = _SYSTEM_PROMPT
    # PER-PHASE LLM SPLIT (journal §4.1). Optional SECOND handler for the VOICE
    # phases (the journal's field-notes seam + NARRATE). ``None`` → every phase
    # uses the single primary ``llm`` handler — the BYTE-FOR-BYTE back-compat
    # path for every other analyst (no descriptor sets ``method.llm.narrate``).
    # When set (journal_assessor / journal_consolidator wire
    # ``method.llm.narrate.raw`` to the Opus plane), the heavy GATHER loop stays
    # on ``llm`` (the gpt-oss / vLLM plane, reasoning=high) while the voice phases route to
    # ``llm_narrate`` (Opus) via :meth:`narrate_llm`. The narrate handler may carry
    # its OWN output cap — see ``narrate_max_tokens`` — because the Anthropic plane
    # caps OUTPUT with max_tokens whereas the vLLM plane serves its own budget.
    llm_narrate: LLMHandlerLike | None = None
    # Optional OUTPUT cap for the narrate (Opus) phases. ``None`` → reuse
    # ``max_tokens``. On the Anthropic plane max_tokens IS the output cap, so the
    # journal sets a larger narrate cap (entry 16384 / consolidation 24576) than
    # the inert gather cap.
    narrate_max_tokens: int | None = None
    # Optional Tier-1 grounding hook (see GroundingHook). ``None`` → no
    # grounding injection (the default for every non-opted-in analyst).
    grounding_hook: GroundingHook | None = None
    # S5 — agentic GATHER deps. ALL optional; absent → the legacy single-shot
    # path runs byte-for-byte unchanged (NO-OP back-compat).
    #
    # ``agency_binding`` is the (assessor, target) ``substrate_read`` binding.
    # The deps-builder wires it ONLY when the pack resolves EFFECTIVE (assessor
    # grant ∩ target allow). None → GATHER never engages (the opt-in IS the
    # binding's presence). EVERY tool call routes through it →
    # ``Agency.run_pack_tool`` (the governed path); there is no ungoverned
    # direct-port fallback for inline_target (unlike consult, which keeps one
    # for hand-built test deps).
    agency_binding: Any | None = None
    # GATHER round cap (from ``method.gather.max_rounds``). Clamped to
    # [1, _GATHER_ROUNDS_CEILING] by the runner.
    max_rounds: int = _GATHER_DEFAULT_ROUNDS
    # Optional per-run invoke timeout (seconds) the soft latency guard measures
    # against (``method.timeout_seconds``). None → no latency guard.
    invoke_timeout_seconds: float | None = None
    # Optional budget-headroom precheck (see BudgetPrecheck). None → no
    # precheck; GATHER engages whenever a binding is present.
    budget_precheck: BudgetPrecheck | None = None
    # PER-PHASE LLM SPLIT — inject the gpt-oss ``Reasoning: high`` directive into
    # the GATHER system prompt ONLY (the heavy gpt-oss/vLLM investigation rounds).
    # Default False → the gather suffix is byte-for-byte unchanged for every
    # assessor + the Opus voice calls. The journal sets it True so the gpt-oss/vLLM
    # gather thinks hard; the Opus field-notes/NARRATE calls are NOT polluted
    # because they never pass through the gather suffix. ``reasoning_effort`` is
    # NOT a vLLM wire arg — gpt-oss takes a ``Reasoning: high`` content directive
    # (see vllm.py:106,:129), so the CALLER (here) injects it into the prompt.
    gather_reasoning_high: bool = False

    def narrate_llm(self) -> LLMHandlerLike:
        """The handler for the VOICE phases (field-notes + NARRATE).

        Falls back to the primary ``llm`` when no second handler is wired —
        the zero-regression path for every analyst that doesn't split (no
        descriptor sets ``method.llm.narrate``). When ``llm_narrate`` IS set
        (the journal's Opus plane) the voice routes there while GATHER stays on
        ``llm`` (the gpt-oss/vLLM plane).
        """
        return self.llm_narrate or self.llm

    def narrate_tokens(self) -> int:
        """The OUTPUT-token cap for the narrate phases.

        ``narrate_max_tokens`` when set (the Anthropic plane caps OUTPUT, so the
        journal raises it past the inert gather cap), else ``max_tokens``.
        """
        return self.narrate_max_tokens if self.narrate_max_tokens is not None else self.max_tokens


# ---------------------------------------------------------------------------
# Unit-factory (P2-T1): the descriptor-supplied system prompt drives synthesis
# ---------------------------------------------------------------------------


def _effective_system_prompt(deps: InlineTargetDeps) -> str:
    """The system prompt that drives THIS unit's synthesis.

    P2-T1 (unit-factory): a bounded reasoning unit is JUST a descriptor — its
    OWN ``method.system_prompt`` / ``method.prompt_module`` is threaded into
    ``deps.system_prompt`` by the deps-builder and drives the LLM synthesis here
    (so a new unit needs NO new entry in ``_KIND_MODULE_NAMES``). Falls back to
    the kind default ``_SYSTEM_PROMPT`` when ``deps.system_prompt`` is unset /
    ``None`` — a directly-constructed deps or the bare-LLM back-compat path — so
    both the GATHER suffix and the synthesis call always have a real prompt to
    build on (and never crash on ``None + str``).
    """
    return deps.system_prompt or _SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Phase 2.5 — GATHER: bounded substrate tool-call loop (S5 agentic assessors)
# ---------------------------------------------------------------------------


async def _gather(
    deps: InlineTargetDeps,
    *,
    binding: Any,
    user_prompt: str,
    target_id: str | None,
    analyst_id: str | None,
    steps: list[dict[str, Any]],
    tool_bindings: Mapping[str, Any] | None = None,
    gather_system: str | None = None,
    extra_read_tools: tuple[str, ...] = (),
    extra_write_tools: tuple[str, ...] = (),
) -> tuple[str, dict[str, int], list[UUID], list[dict[str, Any]]]:
    """Run the bounded GATHER tool-call loop, returning the enrichment.

    Engages ONLY when ``binding`` is wired (the EFFECTIVE-pack opt-in — the
    per-run, target-scoped ``AgencyToolBinding`` for ``substrate_read``) and an
    optional ``deps.budget_precheck`` reports headroom. Each round the assessor
    LLM emits either a tool call (governed via the OWNING pack's
    ``binding.run_tool``) or ``{"done": true}``; tool results fold back into the
    conversation. The loop is capped at ``deps.max_rounds`` (clamped to
    ``_GATHER_ROUNDS_CEILING``) and stops early when the soft latency guard
    fires — degrade-not-drop, the one-shot synthesis always still runs.

    SEAM #22 — multi-pack routing: ``binding`` is the default ``substrate_read``
    binding (read tools). ``tool_bindings`` maps a write/web tool name → the
    per-run binding for ITS pack (``web_access`` / ``propose_facts``), built by
    the host iff that pack is EFFECTIVE and re-pointed per run by the actor. Each
    tool call routes to its owning pack's binding so ``Agency.run_pack_tool``
    enforces tool↔pack ownership and the per-pack governor. A write/web tool the
    planner names without a wired binding is a clean "unbound" no-op folded back
    to the planner — never an ungoverned call.

    Returns ``(gathered_context, usage, refs, gather_steps)``:
      * ``gathered_context`` — a "SUBSTRATE INVESTIGATION" preamble built from
        the tool results, to PREPEND to the synthesis prompt (empty string when
        nothing useful was gathered).
      * ``usage`` — aggregate token usage across the GATHER rounds (folded into
        the run's total so the budget records it).
      * ``refs`` — substrate UUIDs the tools returned (extend ``derived_from``).
      * ``gather_steps`` — trace steps appended to ``intermediate_steps``.
    """
    aggregate_usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    refs: list[UUID] = []
    gather_steps: list[dict[str, Any]] = []
    tool_summaries: list[str] = []

    # Budget gate BEFORE gather (degrade-not-drop): skip the extra rounds, not
    # the finding, when the per-day cap has no headroom.
    if deps.budget_precheck is not None:
        try:
            has_headroom = await deps.budget_precheck()
        except Exception as exc:  # pragma: no cover — never fail the run on a precheck
            logger.warning("inline_target.gather.budget_precheck_failed err=%s", exc)
            has_headroom = True
        if not has_headroom:
            step = {"phase": "gather", "kind": "skipped_budget"}
            steps.append(step)
            gather_steps.append(step)
            return "", aggregate_usage, refs, gather_steps

    # A NEW llm_planner kind (the journal_assessor, plan §5/§4.9) reaches its
    # OWN read tools through this loop. Those tools are NOT in inline_target's
    # _GATHER_READ_TOOLS (they are journal_read-pack-specific instruments), so
    # the caller passes them in ``extra_read_tools`` — they are then BOTH
    # recognized as valid tool names AND routed through the read ``binding``
    # (the journal_read binding, which Agency.run_pack_tool governs against the
    # journal_read pack). Empty for inline_target → byte-for-byte unchanged.
    #
    # ``extra_write_tools`` (plan §7 / Wave 4 — the journal_propose pack) carries
    # the journal's OWN write/propose tool names. They are recognized as valid
    # tool names but — unlike read tools — are NOT added to ``read_tools``, so
    # they route through their per-tool binding in ``tool_bindings`` (the
    # journal_propose binding, carrying the per-run WritebackContext) exactly like
    # the generic propose_facts write tools, NEVER through the read binding.
    read_tools = set(_GATHER_READ_TOOLS) | set(extra_read_tools)
    recognized_tools = (
        set(_GATHER_TOOLS) | set(extra_read_tools) | set(extra_write_tools)
    )
    max_rounds = max(1, min(_GATHER_ROUNDS_CEILING, deps.max_rounds))
    deadline = (
        time.monotonic()
        + deps.invoke_timeout_seconds * _GATHER_TIMEOUT_BUDGET_FRACTION
        if deps.invoke_timeout_seconds
        else None
    )
    # SEAM #22: the suffix is built by the caller (so it can splice the
    # web/write guidance from the bound packs' descriptors); fall back to the
    # read-only suffix when not supplied (back-compat: read-only assessors).
    gather_system = _effective_system_prompt(deps) + (gather_system or _GATHER_SYSTEM_SUFFIX)
    # PER-PHASE LLM SPLIT — prepend the gpt-oss "Reasoning: high" directive to the
    # GATHER system prompt ONLY when this deps opted in (the journal's gpt-oss/vLLM
    # gather plane). Default off → byte-for-byte unchanged for every assessor;
    # the Opus field-notes/NARRATE calls never reach this code path so they are
    # NEVER polluted with the directive.
    if deps.gather_reasoning_high:
        gather_system = f"{_REASONING_HIGH_DIRECTIVE}\n\n{gather_system}"
    tool_bindings = tool_bindings or {}
    messages: list[Mapping[str, Any]] = [
        {"role": "user", "content": user_prompt}
    ]

    rounds_used = 0
    for round_idx in range(max_rounds):
        # Soft latency guard: stop opening new rounds once we've burned the
        # latency budget so synthesis still lands inside the invoke timeout.
        if deadline is not None and time.monotonic() >= deadline:
            step = {
                "phase": "gather",
                "kind": "timeout_guard",
                "round": rounds_used,
            }
            steps.append(step)
            gather_steps.append(step)
            logger.warning(
                "inline_target.gather.approaching_timeout analyst_id=%s "
                "target_id=%s rounds_used=%d — stopping GATHER, proceeding to "
                "synthesis",
                analyst_id, target_id, rounds_used,
            )
            break
        rounds_used = round_idx + 1
        try:
            content, usage = await _reason_via_llm(
                deps.llm,
                user_prompt="",  # full conversation passed via messages below
                max_tokens=deps.max_tokens,
                temperature=deps.temperature,
                system_prompt=gather_system,
                messages=messages,
            )
        except Exception as exc:  # GATHER must degrade-not-drop — never fail the run
            logger.warning(
                "inline_target.gather.llm_error analyst_id=%s round=%d err=%s",
                analyst_id, rounds_used, exc,
            )
            step = {"phase": "gather", "kind": "llm_error", "round": rounds_used}
            steps.append(step)
            gather_steps.append(step)
            break
        for k in aggregate_usage:
            aggregate_usage[k] += usage.get(k, 0)

        parsed = _extract_json(content)
        if not parsed or parsed.get("done") is True:
            step = {"phase": "gather", "kind": "done", "round": rounds_used}
            steps.append(step)
            gather_steps.append(step)
            break

        tool_name = str(parsed.get("tool") or "")
        tool_args = parsed.get("args") or {}
        if not isinstance(tool_args, Mapping) or tool_name not in recognized_tools:
            # Neither a recognized tool nor done — nudge once, then move on.
            step = {
                "phase": "gather",
                "kind": "unrecognized",
                "round": rounds_used,
            }
            steps.append(step)
            gather_steps.append(step)
            messages = messages + [
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": (
                        'Reply with a tool call ({"tool": ..., "args": ...}) '
                        'or {"done": true}.'
                    ),
                },
            ]
            continue

        # SEAM #22 — route to the OWNING pack's binding. Read tools use the
        # default substrate_read ``binding``; web/write tools use their per-tool
        # binding from ``tool_bindings`` (built by the host iff the pack is
        # EFFECTIVE). A write/web tool named without a wired binding is reported
        # as an unbound no-op (folded back to the planner), never dispatched
        # through the substrate_read binding (where run_pack_tool would block it
        # as unknown_tool anyway) — keep the cause explicit.
        if tool_name in read_tools:
            routed = binding
        else:
            routed = tool_bindings.get(tool_name)
        if routed is None:
            tool_result: dict[str, Any] = {
                "error": (
                    f"tool_unbound: {tool_name} requires its action pack to be "
                    "EFFECTIVE for this (assessor, target) — not granted/allowed"
                )
            }
            step = {
                "phase": "gather",
                "kind": "tool_call",
                "round": rounds_used,
                "tool": tool_name,
                "admitted": False,
                "ok": False,
            }
            steps.append(step)
            gather_steps.append(step)
            messages = messages + [
                {"role": "assistant", "content": content},
                {
                    "role": "tool",
                    "name": tool_name,
                    "content": json.dumps(tool_result)[:4000],
                },
            ]
            continue

        outcome = await routed.run_tool(tool_name, dict(tool_args))
        if not outcome.admitted:
            tool_result = {
                "error": f"tool_blocked: {outcome.block_cause}: {outcome.detail}"
            }
        elif outcome.tool_result is None or outcome.tool_result.status == "failed":
            err = (
                outcome.tool_result.error
                if outcome.tool_result is not None
                else "tool produced no result"
            )
            tool_result = {"error": f"tool_failed: {err}"}
        else:
            tool_result = dict(outcome.tool_result.output)

        ok = "error" not in tool_result
        step = {
            "phase": "gather",
            "kind": "tool_call",
            "round": rounds_used,
            "tool": tool_name,
            "admitted": outcome.admitted,
            "ok": ok,
        }
        steps.append(step)
        gather_steps.append(step)

        new_refs = _refs_from_tool_result(tool_result)
        # SEAM #22: a write tool returns the id of the row it LANDED
        # (``fact_id`` / ``hypothesis_id``) rather than a ``refs`` list — that
        # landed row is itself a real substrate ref the finding derives from, so
        # fold it into lineage alongside any queried refs.
        for _id_key in ("fact_id", "hypothesis_id"):
            _landed = tool_result.get(_id_key)
            if _landed:
                try:
                    _uid = UUID(str(_landed))
                except (ValueError, TypeError):
                    _uid = None
                if _uid is not None and _uid not in new_refs:
                    new_refs.append(_uid)
        for r in new_refs:
            if r not in refs:
                refs.append(r)
        if ok:
            tool_summaries.append(
                f"{tool_name}({json.dumps(dict(tool_args))[:200]}) -> "
                f"{json.dumps(tool_result)[:600]}"
            )
        messages = messages + [
            {"role": "assistant", "content": content},
            {
                "role": "tool",
                "name": tool_name,
                "content": json.dumps(tool_result)[:4000],
            },
        ]

    gathered_context = ""
    if tool_summaries:
        gathered_context = (
            "SUBSTRATE INVESTIGATION (results of your own pre-finding queries; "
            "ground the finding in these and cite returned UUIDs):\n"
            + "\n".join(f"- {s}" for s in tool_summaries)
            + "\n"
        )
    return gathered_context, aggregate_usage, refs, gather_steps


async def run_method(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: InlineTargetDeps | LLMHandlerLike,
) -> AnalystMethodResult:
    """Execute one inline_target run over the substrate slice.

    ``deps`` accepts either:

      * An :class:`InlineTargetDeps` bundle (production path — the
        runtime builds this from the analyst descriptor).
      * A bare :class:`LLMHandlerLike` (test path + backward-compat
        with the spike's ``LLMAnalystRunner.__call__(inputs, options)``
        signature — the runtime keeps the LLMAnalystRunner adapter
        around so existing deps stay wired).

    Returns :class:`AnalystMethodResult` with the typed finding, the
    flat usage dict, ``derived_from`` lineage UUIDs, and the per-phase
    intermediate_steps trace.
    """
    if not isinstance(deps, InlineTargetDeps):
        # Backward-compat path — deps is a bare LLM handler.
        deps = InlineTargetDeps(llm=deps)

    target_id = options.get("target_id")
    analyst_id = options.get("analyst_id")
    steps: list[dict[str, Any]] = []

    # --- WAKE ----------------------------------------------------------
    steps.append({"phase": "wake", "kind": "envelope"})

    # --- ORIENT --------------------------------------------------------
    sliced, derived_from = _orient(inputs, target_id)
    steps.append({
        "phase": "orient",
        "kind": "deterministic",
        "in_count": len(inputs),
        "kept_count": len(sliced),
        "derived_count": len(derived_from),
    })

    if not sliced:
        # Empty input — the runtime would normally short-circuit before
        # calling us (see AnalystActor.run NOOP/no_inputs branch in
        # ``dapr_actors.py:649``), but be defensive: emit a minimal
        # diagnostic finding rather than crash.  The runtime treats this
        # as a successful run with a trivial output; the operator will
        # see the analyst is firing but has nothing to chew on, which is
        # the correct signal.
        steps.append({"phase": "reflect", "kind": "noop_no_inputs"})
        finding = FindingPayload(
            title=f"No signals for {target_id or 'target'}",
            body="The substrate slice for this run was empty.",
            confidence=0.0,
            tags=["empty_slice"],
        )
        narrated = _narrate(finding, target_id=target_id, analyst_id=analyst_id)
        steps.append({"phase": "narrate", "kind": "envelope"})
        steps.append({"phase": "persist", "kind": "envelope"})
        return AnalystMethodResult(
            finding=narrated,
            usage={},
            derived_from=[],
            intermediate_steps=steps,
        )

    # --- PLAN ----------------------------------------------------------
    user_prompt = _render_user_prompt(sliced, target_id)
    # P0-T1: capture the render-time {N -> signal_id} map so REFLECT can resolve
    # the prose's [N] citation markers back to substrate ids. Built from the SAME
    # ORIENTed slice _render_user_prompt rendered, so the 1-based N matches the
    # [N] _render_signal stamped onto each block.
    citation_index = _build_citation_index(sliced)
    steps.append({
        "phase": "plan",
        "kind": "render_prompt",
        "prompt_chars": len(user_prompt),
        "prompt_module": PROMPT_MODULE_PATH,
    })

    # --- GROUND (Tier-1 knowledge grounding) ---------------------------
    # When the analyst opted in (descriptor grounding.enabled), prepend a
    # dated "AUTHORITATIVE CURRENT CONTEXT" preamble built from the CURRENT
    # authoritative substrate facts about the target geo + slice entities —
    # the fix for stale-cutoff models backfilling e.g. "former president".
    # Degrade-not-drop: a resolver failure leaves the prompt untouched.
    if deps.grounding_hook is not None:
        try:
            preamble = await deps.grounding_hook(sliced, options)
        except Exception as exc:  # pragma: no cover — enrichment must not fail the run
            logger.warning("inline_target.grounding.failed err=%s", exc)
            preamble = None
        if preamble:
            user_prompt = f"{preamble}\n{user_prompt}"
            steps.append({
                "phase": "ground",
                "kind": "inject_preamble",
                "preamble_chars": len(preamble),
            })
        else:
            steps.append({"phase": "ground", "kind": "no_current_facts"})

    # --- GATHER (S5 agentic investigation) -----------------------------
    # OPT-IN via the EFFECTIVE read pack: the deps-builder wires
    # ``agency_binding`` only when the assessor grants ``substrate_read``. The
    # actor re-points it to the RUNNING target's allow-list per run (the
    # three-way gate's allow leg is per-target — a fan-out assessor visits many
    # targets, only some of which allow the pack) and passes it via
    # ``options['agency_binding']``; that per-run, target-scoped binding wins
    # over the deps default. When engaged, run a bounded tool-call loop to query
    # the substrate BEFORE synthesizing; prepend the results to the synthesis
    # prompt. Degrade-not-drop: any GATHER failure/over-budget/timeout leaves
    # the one-shot synthesis to land a finding unchanged.
    gather_usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    gather_refs: list[UUID] = []
    active_binding = options.get("agency_binding") or deps.agency_binding
    # SEAM #22: per-tool bindings for the external (web_access) + write-back
    # (propose_facts) packs, each EFFECTIVE-gated + re-pointed per run by the
    # actor. {} when no write/web pack is bound — read-only GATHER, unchanged.
    tool_bindings = options.get("gather_tool_bindings") or {}
    # Splice the bound packs' operator-authored tool-use guidance into the GATHER
    # system suffix. Fragments come through options (the actor lifts them from
    # the bound pack descriptors); None for a pack → its section is omitted.
    web_fragments = options.get("gather_web_prompt_fragments")
    write_fragments = options.get("gather_write_prompt_fragments")
    gather_system = _gather_system_suffix(
        web_fragments=list(web_fragments) if web_fragments is not None else None,
        write_fragments=list(write_fragments) if write_fragments is not None else None,
    )
    if active_binding is not None:
        gathered_context, gather_usage, gather_refs, _ = await _gather(
            deps,
            binding=active_binding,
            user_prompt=user_prompt,
            target_id=target_id,
            analyst_id=analyst_id,
            steps=steps,
            tool_bindings=tool_bindings,
            gather_system=gather_system,
        )
        if gathered_context:
            user_prompt = f"{gathered_context}\n{user_prompt}"

    # --- REASON+ACT ----------------------------------------------------
    # NOTE: when DSPy is wired into the runtime (L-176 GEPA replays this
    # phase), the call below is the boundary the optimizer monkey-patches
    # to swap candidate prompt modules.  Keep it a single function call
    # for replay simplicity.
    try:
        content, usage = await _reason_via_llm(
            deps.llm,
            user_prompt=user_prompt,
            max_tokens=deps.max_tokens,
            temperature=deps.temperature,
            # P2-T1: the descriptor-supplied unit prompt drives synthesis; fall
            # back to the kind default when unset/None (see _effective_system_prompt).
            system_prompt=_effective_system_prompt(deps),
        )
    except Exception:
        # Re-raise — let the runtime classify (TransientFailure /
        # BudgetExhausted / HardFailure) per kind_contracts §7.  We
        # don't swallow here because the runtime's failure semantics
        # gate cooldowns + reminders + DLQ routing.
        steps.append({"phase": "reason", "kind": "llm_error"})
        raise

    # Fold the GATHER rounds' token usage into the run total so the actor's
    # post-run ``budget.record`` charges them against the SAME per-descriptor
    # ``budget_tokens_per_day`` — no separate budget machinery.
    for _k in usage:
        usage[_k] = usage.get(_k, 0) + gather_usage.get(_k, 0)
    steps.append({
        "phase": "reason",
        "kind": "llm_call",
        "subprovider": getattr(deps.llm, "subprovider", "unknown"),
        "tokens": usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0),
    })

    # --- REFLECT -------------------------------------------------------
    fallback_title = f"Assessment for {target_id or 'target'}"
    finding = _coerce_finding(content, fallback_title=fallback_title)

    # P0-T1 (cite the prose): parse the [N] markers the synthesis prose already
    # carries (the prompt asks every key-development claim to cite its signal [N])
    # and resolve each against the render-time {N -> signal_id} map. Persist the
    # resolved mapping as data['citations'] ALONGSIDE the untouched prose — this
    # is the substrate change the unit loop needs (machine-checkable claim->source
    # binding); it needs no migration (FindingPayload.data is a free-form dict).
    # Normalize any non-ASCII citation brackets (【N】 / ［N］) the model emitted
    # to ASCII [N] BEFORE parsing, and persist the normalized prose so the body,
    # the marker parser, and the UI drill-to-source chips all key on the same [N].
    normalized_body = _normalize_citation_markers(finding.body)
    if normalized_body != finding.body:
        finding = finding.model_copy(update={"body": normalized_body})
    citations, marker_count, resolved_count = _extract_citations(
        finding.body, citation_index
    )
    if citations:
        citation_data = dict(finding.data) if isinstance(finding.data, dict) else {}
        citation_data["citations"] = citations
        finding = finding.model_copy(update={"data": citation_data})
    steps.append({
        "phase": "reflect",
        "kind": "coerce_finding",
        "confidence": finding.confidence,
        "evidence_count": len(finding.evidence),
        "structured": "unstructured" not in finding.tags,
        # Citation accounting folded into the coerce step so the 7-phase envelope
        # sequence is unchanged (no extra reflect step).
        "citation_markers": marker_count,
        "citations_resolved": resolved_count,
    })

    # --- NARRATE -------------------------------------------------------
    narrated = _narrate(finding, target_id=target_id, analyst_id=analyst_id)
    steps.append({
        "phase": "narrate",
        "kind": "stamp_provenance",
        "tags": len(narrated.tags),
    })

    # --- PERSIST -------------------------------------------------------
    # Runtime does the actual substrate write; we just record the
    # envelope step for the trace. GATHER refs (substrate UUIDs the assessor
    # queried) extend the lineage so the finding cites what it investigated.
    full_derived = list(derived_from)
    for r in gather_refs:
        if r not in full_derived:
            full_derived.append(r)
    steps.append({
        "phase": "persist",
        "kind": "envelope",
        "derived_from": len(full_derived),
    })

    # D4 off-target guard. A PER-COUNTRY assessment whose finding names ONLY
    # other countries and not its own target is the contamination shape (an
    # Indonesia run that emitted a fully US-focused report). Publish it as
    # TRACE_ONLY rather than as this country's product — the run is still fully
    # traced, only the misattributed feed/output row is suppressed. A meta /
    # no-target run (world_assessor) is never gated (the helper returns False for
    # a non-country target). Best-effort: any guard error degrades to publishing.
    force_trace_only = False
    try:
        from ...runtime.grounding import finding_is_off_target

        guard_geo = [g for g in (narrated.data or {}).get("geo", []) if isinstance(g, str)] \
            if isinstance(narrated.data, dict) else []
        guard_entities = (narrated.data or {}).get("key_entities", []) \
            if isinstance(narrated.data, dict) else []
        if finding_is_off_target(
            target_id=target_id,
            text=f"{narrated.title}\n{narrated.body}",
            key_entities=[e for e in guard_entities if isinstance(e, str)],
            geo=guard_geo,
        ):
            force_trace_only = True
            steps.append({
                "phase": "reflect",
                "kind": "off_target_guard",
                "target_id": target_id,
                "action": "force_trace_only",
            })
            logger.warning(
                "inline_target.off_target_guard target=%s analyst=%s — finding "
                "names only other countries; forcing TRACE_ONLY (D4)",
                target_id, analyst_id,
            )
    except Exception as exc:  # pragma: no cover — guard must never break a run
        logger.debug("inline_target.off_target_guard.skipped err=%s", exc)

    return AnalystMethodResult(
        finding=narrated,
        usage=usage,
        derived_from=full_derived,
        intermediate_steps=steps,
        force_trace_only=force_trace_only,
    )


# ---------------------------------------------------------------------------
# Adapter — closure-shaped runner the runtime already knows how to call
# ---------------------------------------------------------------------------


class InlineTargetRunner:
    """``AnalystRunFn``-shaped wrapper around :func:`run_method`.

    The runtime constructs one of these per analyst actor and stashes
    it on the ``_AnalystDeps.run_method`` field.  ``__call__`` accepts
    the spike's existing ``(inputs, options)`` signature so
    ``AnalystActor.run`` (in ``runtime/dapr_actors.py``) doesn't need to
    change.

    Supersedes the spike's :class:`LLMAnalystRunner`.  The legacy class
    name is re-exported from ``legba.runtime.analyst_method`` as a thin
    shim so callers don't break.
    """

    def __init__(
        self,
        llm: LLMHandlerLike,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        system_prompt: str | None = None,
        grounding_hook: GroundingHook | None = None,
        # S5 GATHER deps — all default-off so the legacy construction shape
        # (the spike's LLMAnalystRunner) stays a single-shot runner unchanged.
        agency_binding: Any | None = None,
        max_rounds: int = _GATHER_DEFAULT_ROUNDS,
        invoke_timeout_seconds: float | None = None,
        budget_precheck: BudgetPrecheck | None = None,
        # PER-PHASE LLM SPLIT — default-off so the legacy inline_target runner is
        # byte-for-byte unchanged (no narrate handler, no reasoning directive).
        llm_narrate: LLMHandlerLike | None = None,
        narrate_max_tokens: int | None = None,
        gather_reasoning_high: bool = False,
    ) -> None:
        self._deps = InlineTargetDeps(
            llm=llm,
            max_tokens=max_tokens,
            temperature=temperature,
            system_prompt=system_prompt or _SYSTEM_PROMPT,
            llm_narrate=llm_narrate,
            narrate_max_tokens=narrate_max_tokens,
            grounding_hook=grounding_hook,
            agency_binding=agency_binding,
            max_rounds=max_rounds,
            invoke_timeout_seconds=invoke_timeout_seconds,
            budget_precheck=budget_precheck,
            gather_reasoning_high=gather_reasoning_high,
        )

    async def __call__(
        self,
        inputs: list[dict[str, Any]],
        options: Mapping[str, Any],
    ) -> AnalystMethodResult:
        return await run_method(inputs, options, self._deps)


__all__ = [
    "AnalystMethodResult",
    "BudgetPrecheck",
    "GroundingHook",
    "HANDLER_VERSION",
    "InlineTargetDeps",
    "InlineTargetRunner",
    "KIND_NAME",
    "LLMHandlerLike",
    "OUTPUT_KIND",
    "PROMPT_MODULE_PATH",
    "READ_SLICE",
    "SCHEMA_VERSION",
    "_REASONING_HIGH_DIRECTIVE",
    "build_prompt_module",
    "run_method",
]
