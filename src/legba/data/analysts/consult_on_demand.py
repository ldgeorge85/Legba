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
    ROUND ← LLM emits either {"final": true, ...} OR
            {"tool": "<name>", "args": {...}}
    ACT   → if a tool was requested, dispatch via the ToolDispatcher; the
            tool's JSON result is appended to the conversation as a
            "tool" role message.
    LOOP  ← back to ROUND, max MAX_TOOL_ROUNDS iterations.  After the cap
            we force a final synthesis turn (no tools available) so the
            operator always gets a structured answer.

The tool whitelist is small on purpose — the legacy ConsultPanel exposed
~22 tools (see ``ui/consult.py:CONSULT_TOOLS``).  For the kind's first
shipment, we restrict to four read-only primitives that cover the bulk
of Lewis's actual consult traffic per the L-178 note ("daily-use
pattern"):

  * ``search_signals``  — substrate signal search (title ILIKE / category
                          / time-window).
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

from ..provenance.models import ConsultResponsePayload, FindingPayload

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


def _default_max_tokens() -> int:
    """Per-LLM-call output budget, env-tunable via ``LEGBA_CONSULT_MAX_TOKENS``.

    The runtime deps builder does NOT override this (it only sizes the LLM
    handler's ceiling from the descriptor), so this IS the operative per-call
    cap. The sweet spot balances completeness vs the hosted LLM's per-call
    timeout: 1024 truncated real "world report" answers mid-string (raw {...}
    block); 4096 made a broad forced-final's large generation slow enough to hit
    the provider timeout → ``network error`` → actor retry storm → 504. 2048
    (~8k chars) lets a thorough answer through while staying inside the provider
    window, and `_unwrap_double_envelope` still recovers any over-cap truncation
    as clean markdown. Operators can raise/lower it live (env + recreate, no
    rebuild) without code changes.
    """
    raw = os.getenv("LEGBA_CONSULT_MAX_TOKENS", "").strip()
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return 2048


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
# Substrate tool ports
# ---------------------------------------------------------------------------


@runtime_checkable
class SubstrateQueryPort(Protocol):
    """The substrate-side tool surface the consult kind invokes.

    The runtime constructs one of these per analyst-actor activation,
    binding it to ``deps.pg_pool`` (+ optional vector store via
    ``deps.extras``).  Tests pass a hand-rolled stub that returns fixed
    rows for a known query — the LLM boundary is the test double, but the
    substrate boundary stays real per the no-mocks rule.

    Each tool returns a JSON-serializable mapping; the dispatcher folds
    that into the next ROUND's tool-result message.  The mapping includes
    a ``"refs"`` list of substrate UUIDs whenever rows were returned so
    the kind can build :attr:`ConsultResponsePayload.cited_substrate_refs`.
    """

    async def search_signals(
        self,
        *,
        query: str,
        category: str | None = None,
        limit: int = 20,
        scope_predicate: str | None = None,
    ) -> dict[str, Any]: ...

    async def query_facts(
        self,
        *,
        subject: str | None = None,
        predicate: str | None = None,
        value: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]: ...

    async def inspect_entity(
        self,
        *,
        name: str,
    ) -> dict[str, Any]: ...

    async def vector_search(
        self,
        *,
        query: str,
        limit: int = 10,
    ) -> dict[str, Any]: ...

    async def query_nexuses(
        self,
        *,
        subject: str | None = None,
        obj: str | None = None,
        rel_type: str | None = None,
        polarity: int | None = None,
        limit: int = 30,
    ) -> dict[str, Any]: ...

    async def query_hypotheses(
        self,
        *,
        target_id: str | None = None,
        status: str | None = None,
        situation_id: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]: ...

    async def get_timeline(
        self,
        *,
        subject: str,
        limit: int = 40,
    ) -> dict[str, Any]: ...

    async def compare_targets(
        self,
        *,
        target_ids: list[str],
    ) -> dict[str, Any]: ...

    async def query_paths(
        self,
        *,
        subject: str,
        obj: str,
        max_hops: int = 3,
        polarity_product: int | None = None,
        limit: int = 30,
    ) -> dict[str, Any]: ...

    async def find_proxy_chains(
        self,
        *,
        subject: str,
        obj: str,
        max_hops: int = 3,
        polarity_product: int | None = None,
        limit: int = 30,
    ) -> dict[str, Any]: ...

    async def query_brokers(
        self,
        *,
        camp_a: list[str],
        camp_b: list[str],
        max_hops: int = 3,
        limit: int = 50,
    ) -> dict[str, Any]: ...

    # Finished-intelligence + navigation readers (palette expansion).
    async def list_findings(
        self,
        *,
        target_id: str | None = None,
        analyst_id: str | None = None,
        severity: str | None = None,
        since_hours: int | None = None,
        limit: int = 20,
    ) -> dict[str, Any]: ...

    async def list_situations(
        self,
        *,
        status: str | None = None,
        target_id: str | None = None,
        since_hours: int | None = None,
        limit: int = 20,
    ) -> dict[str, Any]: ...

    async def query_predictions(
        self,
        *,
        target_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]: ...

    async def list_targets(self, *, active_only: bool = True) -> dict[str, Any]: ...

    async def list_sources(
        self,
        *,
        active_only: bool = True,
        silent_only: bool = False,
        silent_hours: int = 48,
    ) -> dict[str, Any]: ...


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
  - search_signals(query, [category], [limit], [scope_predicate]) — full-text search over indexed signals.
  - query_facts([subject], [predicate], [value], [limit]) — fact store; at least one of subject/predicate/value is required.
  - inspect_entity(name) — canonical entity profile + recent facts.
  - vector_search(query, [limit]) — semantic similarity over signal embeddings.
  - query_nexuses([subject], [object], [rel_type], [polarity], [limit]) — open signed/typed relationships (A->[intermediary]->B; polarity +1 supportive / -1 antagonistic / 0 neutral/dual-use).
  - query_hypotheses([target_id], [status], [situation_id], [limit]) — competing-hypothesis (ACH) rows (thesis vs counter_thesis, evidence balance, status: active / confirmed / refuted).
  - get_timeline(subject, [limit]) — time-ordered merge of current facts and recent signals about one subject.
  - compare_targets(target_ids) — side-by-side substrate rollup for two or more target ids.
  - query_paths(subject, object, [max_hops<=3], [polarity_product], [limit]) — ranked SIGNED paths A->...->B over open nexuses; each path carries its net polarity_product (the structural-balance sign of the chain: +1 net-supportive / -1 net-antagonistic). polarity_product filters to that net sign.
  - find_proxy_chains(subject, object, [max_hops<=3], [polarity_product], [limit]) — INDIRECT links only (multi-hop chains + reified A->via->B cut-outs); the proxy path from A to B.
  - query_brokers(camp_a, camp_b, [max_hops<=3], [limit]) — entities that SIT ON paths between two entity sets (the broker between two camps), ranked by how many A->B paths run through them.

Finished intelligence — the platform's OWN analysis (analysis-derived; consult these FIRST, they encode prior work — weigh per the provenance rules above):
  - list_findings([target_id], [analyst_id], [severity], [since_hours], [limit]) — recent findings the platform already produced (country/world situational assessments, meta-findings); effective_confidence folds in the critic's grade. Cite the finding id.
  - list_situations([status], [target_id], [since_hours], [limit]) — ongoing clustered situation frames, each with intensity_score + event_count (rank severity by these); call with NO filters (limit 20-30) for a world-state survey. Pass a returned situation_id to query_hypotheses for its ACH rows.
  - query_predictions([target_id], [status], [limit]) — event-volume forecasts (forecast_method 'naive_mean' ⇒ no trend could be fit, low-confidence; 'auto_arima' ⇒ fitted). Cite the id.
  - list_targets() — the monitored targets + their ids (e.g. country_g20_ir); call this to resolve a place/topic to a valid target_id before query_hypotheses / compare_targets / list_findings.
  - list_sources([active_only], [silent_only]) — ingest sources + freshness; use to tell "no coverage on X" apart from "a quiet feed".

Loop protocol:
  - To call ONE tool, reply with strict JSON: {"tool": "<name>", "args": {...}}
  - To call SEVERAL INDEPENDENT tools in the SAME round (they run concurrently and cost ONE round, not one per tool), reply with strict JSON: {"tools": [{"tool": "<name>", "args": {...}}, {"tool": "<name>", "args": {...}}]} — up to 5. Batch ONLY tools that do NOT depend on each other's output; a call that needs a prior call's result (e.g. compare_targets after list_targets resolves the ids) must wait for the next round.
  - To finish, reply with strict JSON:
    {"final": true, "answer": "...", "uncertainty": 0.0-1.0, "cited_refs": ["<uuid>", ...], "unanswered_aspects": ["...", ...]}

Strategy — SURVEY THEN DRILL:
  - For a BROAD / world-state / open-ended question ("how's the world looking", "what's going on", anything not about a single named entity), your FIRST round MUST survey the platform's own active picture: batch list_situations (NO filters, limit 20-30 — it ranks the live frames by intensity_score and event_count) WITH list_findings, and add query_predictions when forecasts are relevant. An answer to a broad question that never called list_situations is INCOMPLETE — you cannot describe "the world" without first reading the active situation frames.
  - For a NARROW question about one place/topic, go straight to the relevant reader; resolve the place to a target_id with list_targets first if you need one for list_findings / query_hypotheses / compare_targets.
  - In BOTH cases the platform's OWN finished intelligence — list_findings / list_situations (and query_predictions) — comes FIRST; build on it, and use raw search_signals / vector_search to survey broadly, verify, update, or fill gaps, not to re-derive from scratch. THEN drill into the specific entities, facts, or time windows. Prefer two or three cheap wide calls (batched into one round) over one narrow guess. Only finish once you have gathered enough or exhausted the useful calls.
Answer quality: the reply ENVELOPE is strict JSON, but the `answer` field's VALUE must be plain GitHub-flavored MARKDOWN prose written for a human reader — headings (##), **bold**, bullet lists, `---` rules as needed. The `answer` value must NEVER itself be a JSON object, a quoted JSON string, or a nested {"final": ...} envelope, and must not be wrapped in ``` fences. WRONG: "answer": "{\\"final\\": true, \\"answer\\": \\"...\\"}". RIGHT: "answer": "## Bottom line\\n\\nThe situation is …". LEAD with the bottom-line judgment, then support it with cited substrate (`cited_refs` = the UUIDs you actually used). Set `uncertainty` as 1 minus your calibrated confidence in the answer (high — >= 0.7 — when the substrate lacks the material), and list the parts you could not address in `unanswered_aspects`. Do not invent UUIDs or facts the tools did not return."""
)


def _render_user_prompt(question: str, scope_predicate: str | None) -> str:
    body = f"Operator question:\n{question.strip()}"
    if scope_predicate:
        body += f"\n\nScope predicate (apply to substrate queries): {scope_predicate}"
    return body


# ---------------------------------------------------------------------------
# JSON parse helpers (shared shape with inline_target's _coerce_finding)
# ---------------------------------------------------------------------------


def _extract_json(raw: str) -> dict[str, Any] | None:
    """Pull a strict-JSON object out of an LLM response.

    Tolerates markdown fences and trailing prose past the closing brace.
    Returns None on parse failure (caller decides how to recover).
    """
    candidate = (raw or "").strip()
    if not candidate:
        return None
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
        candidate = candidate.strip()
    # Start at the first brace so leading prose ("Here is the JSON: {...}") or a
    # thinking/preamble line doesn't defeat the parse, then brace-match the close.
    start = candidate.find("{")
    if start == -1:
        return None
    candidate = candidate[start:]
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
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


# Short JSON string escapes we honor when salvaging a malformed nested envelope.
_JSON_STR_ESCAPES = {
    '"': '"', "\\": "\\", "/": "/",
    "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
}

#: Matches either a ``\uXXXX`` unicode escape or any short ``\x`` escape.
_JSON_ESCAPE_RE = re.compile(r"\\u[0-9a-fA-F]{4}|\\.", re.DOTALL)


def _unescape_json_str(s: str) -> str:
    r"""Decode JSON string escapes (``\n``, ``\t``, ``\"``, ``\\``, ``\uXXXX``).

    Operates on the str directly (NOT via ``unicode_escape``, which mangles real
    UTF-8 — em-dashes, smart quotes) so multibyte LITERALS survive untouched,
    while ``\uXXXX`` escapes are decoded to their code point.
    """
    def _one(m: re.Match[str]) -> str:
        esc = m.group(0)
        if len(esc) == 6 and esc[1] == "u":  # \uXXXX
            try:
                return chr(int(esc[2:], 16))
            except ValueError:  # pragma: no cover — regex already constrains hex
                return esc
        return _JSON_STR_ESCAPES.get(esc[1], esc)

    return _JSON_ESCAPE_RE.sub(_one, s)


# Keys that mark the END of a nested ``"answer"`` value when the inner envelope
# is malformed and ``json.loads`` cannot parse it. ``final`` is deliberately NOT
# here: in a final-envelope it precedes ``answer``, so it never marks the tail.
_NESTED_TAIL_KEYS = ("uncertainty", "cited_refs", "unanswered_aspects")


def _unwrap_double_envelope(answer: str) -> str:
    """Recover a markdown answer the planner double-wrapped in a JSON envelope.

    Some planner turns emit ``{"final": true, "answer": "<a NESTED
    {\"final\":...} JSON string>"}``. :func:`_extract_json` parses the OUTER
    object, so ``answer`` ends up being the raw inner JSON TEXT rather than the
    prose, and when the inner has unescaped quotes/newlines it is not even valid
    JSON — so the UI renders a raw ``{...}`` block.

    This lifts ONE level: when ``answer`` itself is a final-envelope, return its
    inner ``answer`` prose; otherwise return the input unchanged. It NEVER raises
    and NEVER eats content — on ANY doubt it returns the original, so we degrade
    to "ugly but complete", never to a truncated/empty answer.
    """
    s = (answer or "").strip()
    # Gate hard: only engage on an envelope-SHAPED object. A legitimate answer
    # that merely mentions a brace or the word "final" is left untouched.
    if not (s.startswith("{") and '"answer"' in s and '"final"' in s):
        return answer
    # Clean nested case: the inner WAS valid JSON, so it parses. Lift the inner
    # answer (one level; guard against a non-string / empty inner).
    inner = _extract_json(s)
    if isinstance(inner, dict) and "answer" in inner:
        lifted = inner.get("answer")
        if isinstance(lifted, str) and lifted.strip():
            return lifted
    # Malformed nested case: unescaped quotes/newlines defeat json.loads, so we
    # regex-lift the answer value. The body is UNTRUSTED prose that can itself
    # contain a decoy `","uncertainty":` sequence (e.g. an answer that quotes a
    # JSON example), so we anchor on the RIGHTMOST real tail boundary — the
    # genuine envelope end — not the first in-prose match, and refuse to lift
    # when doing so would discard most of the content (a false anchor).
    open_m = re.search(r'"answer"\s*:\s*"', s)
    if open_m is None:
        return answer
    body_region = s[open_m.end():]
    tail_alt = "|".join(_NESTED_TAIL_KEYS)
    tail_matches = list(re.finditer(
        r'"\s*,\s*"(?:' + tail_alt + r')"\s*:', body_region,
    ))
    if tail_matches:
        raw_body = body_region[: tail_matches[-1].start()]   # rightmost = real tail
    else:
        close_m = re.search(r'"\s*\}\s*$', body_region)
        if close_m is not None:
            raw_body = body_region[: close_m.start()]
        else:
            # No clean envelope tail — typically a forced-final envelope
            # TRUNCATED by max_tokens mid-string. The `{"final":...,"answer":"`
            # PREFIX is definitely not answer content, so lift the remainder
            # (dropping a dangling partial escape) rather than render raw JSON.
            raw_body = body_region.rstrip("\\")
    body = _unescape_json_str(raw_body).strip()
    # "Never eat content": if the lift would discard more than half of the body
    # region, the anchor is almost certainly a false positive — degrade to the
    # original (ugly-but-complete) rather than truncate a legitimate answer.
    if not body or len(body) < 0.5 * len(body_region):
        return answer
    return body


def _trim_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """Compact a tool's args for the lightweight trace (keeps the SSE step
    stream and the persisted ``tool_calls`` small). Caps string/list sizes;
    scalars pass through."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str):
            out[str(k)] = v[:200]
        elif isinstance(v, bool) or v is None or isinstance(v, (int, float)):
            out[str(k)] = v
        elif isinstance(v, list):
            out[str(k)] = [str(x)[:120] for x in v[:10]]
        else:
            out[str(k)] = str(v)[:200]
    return out


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
}


def _normalize_calls(parsed: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalize a planner round into a list of ``{tool, args}`` calls.

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
        if len(out) >= MAX_TOOLS_PER_BATCH:
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
                category=args.get("category"),
                limit=int(args.get("limit", 20)),
                scope_predicate=scope_predicate,
            )
        if name == "query_facts":
            return await port.query_facts(
                subject=args.get("subject"),
                predicate=args.get("predicate"),
                value=args.get("value"),
                limit=int(args.get("limit", 30)),
            )
        if name == "inspect_entity":
            return await port.inspect_entity(name=str(args.get("name", "")))
        if name == "vector_search":
            return await port.vector_search(
                query=str(args.get("query", "")),
                limit=int(args.get("limit", 10)),
            )
        if name == "query_nexuses":
            polarity = args.get("polarity")
            return await port.query_nexuses(
                subject=args.get("subject"),
                obj=args.get("object"),
                rel_type=args.get("rel_type"),
                polarity=int(polarity) if polarity is not None else None,
                limit=int(args.get("limit", 30)),
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
            )
        if name == "find_proxy_chains":
            pp = args.get("polarity_product")
            return await port.find_proxy_chains(
                subject=str(args.get("subject", "")),
                obj=str(args.get("object", "")),
                max_hops=int(args.get("max_hops", 3)),
                polarity_product=int(pp) if pp is not None else None,
                limit=int(args.get("limit", 30)),
            )
        if name == "query_brokers":
            raw_a = args.get("camp_a") or []
            raw_b = args.get("camp_b") or []
            return await port.query_brokers(
                camp_a=[str(x) for x in raw_a] if isinstance(raw_a, list) else [],
                camp_b=[str(x) for x in raw_b] if isinstance(raw_b, list) else [],
                max_hops=int(args.get("max_hops", 3)),
                limit=int(args.get("limit", 50)),
            )
        if name == "list_findings":
            return await port.list_findings(
                target_id=args.get("target_id"),
                analyst_id=args.get("analyst_id"),
                severity=args.get("severity"),
                since_hours=int(args["since_hours"])
                    if args.get("since_hours") is not None else None,
                limit=int(args.get("limit", 20)),
            )
        if name == "list_situations":
            return await port.list_situations(
                status=args.get("status"),
                target_id=args.get("target_id"),
                since_hours=int(args["since_hours"])
                    if args.get("since_hours") is not None else None,
                limit=int(args.get("limit", 20)),
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
) -> ConsultResponsePayload:
    """Build the typed :class:`ConsultResponsePayload`.

    Defensive against the LLM returning malformed final-JSON: we fall
    back to a high-uncertainty empty answer with the original question
    in :attr:`ConsultResponsePayload.unanswered_aspects`.
    """
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
            },
        )

    # Repair the double-wrap: some planner turns nest a whole {"final":...}
    # envelope INSIDE the answer string. Lift the inner prose so the UI renders
    # clean markdown instead of a raw JSON block (degrades to the original on
    # any doubt). Covers BOTH the normal final and the forced-final path — both
    # funnel through here.
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
    """One chat_complete turn.  Mirrors inline_target's helper."""
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
    # Per-LLM-call output budget — env-tunable, see _default_max_tokens().
    max_tokens: int = field(default_factory=_default_max_tokens)
    # Wall-clock budget for the tool loop — env-tunable, see
    # _default_wall_budget_seconds(). Caps over-drilling so broad questions
    # return before the blocking endpoint's invoke timeout instead of 504-ing.
    wall_budget_seconds: float = field(default_factory=_default_wall_budget_seconds)
    temperature: float = 0.2
    system_prompt: str = _SYSTEM_PROMPT
    max_rounds: int = MAX_TOOL_ROUNDS
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

    # --- Step trace + live telemetry (Piece 1, D5) --------------------
    # ``steps`` is the durable trace returned as ``intermediate_steps``.
    # ``_record`` appends to it AND pushes the same dict to ``deps.step_publish``
    # when wired, so the live SSE stream and the trace are one source of truth.
    steps: list[dict[str, Any]] = []

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

    await _record(
        {
            "phase": "plan",
            "kind": "render_prompt",
            "question_chars": len(question),
            "scope_predicate": bool(scope_predicate),
            "prompt_module": PROMPT_MODULE_PATH,
        }
    )

    # --- Effective round count (Piece 1, D1) --------------------------
    # The per-run override may arrive on the question row or in ``options``;
    # clamp it to [1, ROUNDS_CEILING] and use a LOCAL — never mutate ``deps``
    # (shared across concurrent runs).
    requested = first.get("max_tool_rounds") or options.get("max_tool_rounds")
    effective_rounds = deps.max_rounds
    if requested is not None:
        try:
            effective_rounds = int(requested)
        except (TypeError, ValueError):
            effective_rounds = deps.max_rounds
    effective_rounds = max(1, min(ROUNDS_CEILING, effective_rounds))

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
    user_prompt = _render_user_prompt(question, scope_predicate)
    messages: list[Mapping[str, Any]] = [
        *seeded,
        {"role": "user", "content": user_prompt},
    ]

    collected_refs: list[UUID] = []
    aggregate_usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}

    final_payload: dict[str, Any] | None = None
    forced_final = False
    last_raw: str = ""

    rounds_used = 0
    loop_started = time.monotonic()
    for round_idx in range(effective_rounds):
        # Wall-clock guard: once we've spent the budget, stop drilling and fall
        # through to the forced-final synthesis so a broad question RETURNS a
        # real answer before the blocking endpoint's invoke timeout instead of
        # 504-ing. ``round_idx > 0`` so the first (survey) round always runs.
        if (
            round_idx > 0
            and time.monotonic() - loop_started > deps.wall_budget_seconds
        ):
            await _record({
                "phase": "reflect",
                "kind": "wall_budget_reached",
                "round": round_idx,
                "elapsed_s": round(time.monotonic() - loop_started, 1),
            })
            break
        rounds_used = round_idx + 1
        try:
            content, usage = await _reason_via_llm(
                deps.llm,
                messages=messages,
                max_tokens=deps.max_tokens,
                temperature=deps.temperature,
                system_prompt=deps.system_prompt,
            )
        except Exception:
            await _record({"phase": "reason", "kind": "llm_error", "round": rounds_used})
            raise
        last_raw = content

        for k in aggregate_usage:
            aggregate_usage[k] += usage.get(k, 0)
        await _record({
            "phase": "reason",
            "kind": "llm_call",
            "round": rounds_used,
            "tokens": usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0),
        })

        parsed = _extract_json(content)
        if not parsed:
            # Planner produced unparseable output — feed the parse-error
            # back so it can recover.  Cheaper than aborting.
            await _record({
                "phase": "reflect",
                "kind": "unparseable",
                "round": rounds_used,
            })
            messages = messages + [
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": (
                        "Your reply was not valid JSON. Respond with ONLY a "
                        "strict-JSON object that is either a tool call "
                        '({"tool": ..., "args": ...}) or a final answer '
                        '({"final": true, "answer": ..., "uncertainty": ..., '
                        '"cited_refs": [...], "unanswered_aspects": [...]}).'
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
        calls = _normalize_calls(parsed)
        if not calls:
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
                        "Your JSON had neither `tool`/`tools` nor `final`. Emit "
                        'either {"tool": ..., "args": ...}, '
                        '{"tools": [{"tool": ..., "args": ...}, ...]}, or '
                        '{"final": true, ...}.'
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

        # Coalesce: record each call (with a compact result summary for the
        # trace), lift refs, and append one "tool"-role message per call before
        # the next round. The "tool" role follows OpenAI's tool-use convention;
        # vLLM passes it through as a system-of-record message.
        tool_messages: list[Mapping[str, Any]] = []
        for (tool_result, meta), call in zip(results, calls):
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
                "content": json.dumps(tool_result)[:8000],
            })
        messages = messages + [
            {"role": "assistant", "content": content},
            *tool_messages,
        ]

    # ---- Force a final turn if the cap was hit without final --------
    if final_payload is None:
        forced_final = True
        force_system = (
            deps.system_prompt
            + "\n\nYou have reached the tool-round cap. You MUST now produce "
            'the final JSON ({"final": true, ...}) using only what the tool '
            "calls already returned. Do not request more tools."
        )
        try:
            content, usage = await _reason_via_llm(
                deps.llm,
                messages=messages
                + [
                    {
                        "role": "user",
                        "content": (
                            "Round cap reached. Emit the final JSON now."
                        ),
                    },
                ],
                max_tokens=deps.max_tokens,
                temperature=deps.temperature,
                system_prompt=force_system,
            )
            for k in aggregate_usage:
                aggregate_usage[k] += usage.get(k, 0)
            last_raw = content
            await _record({
                "phase": "reason",
                "kind": "forced_final",
                "tokens": usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0),
            })
            final_payload = _extract_json(content)
            if final_payload is None and (content or "").strip():
                # Forced-final is the terminal turn (tools withheld). If the model
                # wrote a prose answer instead of the JSON wrapper, use it rather
                # than discarding a real answer as "(no answer produced)".
                final_payload = {
                    "final": True,
                    "answer": content.strip(),
                    "uncertainty": 0.6,
                }
        except Exception:
            await _record({"phase": "reason", "kind": "forced_final_error"})
            # Re-raise — let the runtime classify (transient vs hard).
            raise

    # --- REFLECT / NARRATE --------------------------------------------
    consult = _build_consult_response(
        question=question,
        final_payload=final_payload,
        collected_refs=collected_refs,
        rounds_used=rounds_used,
        forced_final=forced_final,
        subprovider=getattr(deps.llm, "subprovider", None),
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
    data_update: dict[str, Any] = {"tool_calls": tool_trace}
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
    "HANDLER_VERSION",
    "KIND_NAME",
    "LLMHandlerLike",
    "MAX_TOOL_ROUNDS",
    "OUTPUT_KIND",
    "PROMPT_MODULE_PATH",
    "READ_SLICE",
    "SCHEMA_VERSION",
    "SubstrateQueryPort",
    "build_prompt_module",
    "run_method",
]
