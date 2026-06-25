# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""journal_assessor — Legba's first-person reflective voice (plan §4).

The 11th OutputKind's producer. The ONE analyst pointed at the whole organism,
including itself: it reads what the other analysts concluded and narrates a
coherent point of view OVER all of it, in its own voice. It emits exactly one
``JournalPayload`` (entry | consolidation) into the dedicated ``journal_entries``
table — OFF the fact/finding/nexus chain (§3.1). It must NEVER write a fact,
nexus, finding, situation, or hypothesis.

ENGINE (plan §4.1, a hard commitment): the in-actor ``llm_planner`` /
``inline_target``-family envelope — NOT the ``deep_consult`` Dapr workflow (which
hardcodes OUTPUT_KIND = FINDING and rides the known-broken long-activity
round-trip, task #86). This module reuses inline_target's proven GATHER + reason
building blocks (``_gather`` / ``_reason_via_llm``) but:

  * threads the JOURNAL persona (legba.prompts.journal_assessor:JOURNAL_SYSTEM)
    as the system prompt on EVERY LLM call — and authors it WITHOUT the stock
    ``with_preamble`` JSON-only / BLUF anti-voice (§4.2, the headline fix);
  * emits a ``JournalPayload`` (parsing inline [[ref:<uuid>]] citation markers
    into the per-claim ``claims`` binding + the flat ``cited_substrate_refs``);
  * returns ``derived_from=[]`` — the direction-asymmetric off-chain node (§3.5).

WAVE 1 (plan §5 / §4.3 / §4.4 / §10 / §12). The staged arc is now full:

  PLAN ──► GATHER (deep ReAct over the journal_read pack — the WHOLE ANIMAL +
           its OWN instruments, §5) ──► FIELD-NOTES SEAM (§4.3, the ONE
           separation: still in persona, rewrite the gathered observations into
           rich, cited prose — dropping ONLY the raw tool JSON — so NARRATE sees
           the whole system, not a thin summary; this REPLACES deep_consult's
           ``_evidence_brief``) ──► NARRATE (§4.4, tools stay LIVE: a small ReAct
           loop around the narrate call lets the agent pull ONE more thread
           mid-entry) ──► REFLECT (§10, permissive per-claim citation flag —
           flag/speculation-mark an uncited factual span, NEVER strip; a
           perspective sentence is exempt) ──► HONESTY (§10, the DETERMINISTIC
           ``honesty_flags`` post-step forced from the actual calibration metrics
           in the substrate, regardless of what the model wrote).

READ_SLICE stays ``None`` — the default META global signal slice primes the run
and ``get_journal_delta`` (in GATHER) tells the agent what changed since the last
entry. The delta-priming READ_SLICE is a later refinement.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import UUID

from ..provenance.kinds import OutputKind
from ..provenance.models import JournalClaim, JournalPayload
from .agency.journal_propose import JOURNAL_PROPOSE_TOOLS
from .agency.journal_read import JOURNAL_READ_TOOLS
from .inline_target import (
    AnalystMethodResult,
    InlineTargetDeps,
    LLMHandlerLike,
    _gather,
    _gather_system_suffix,
    _reason_via_llm,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Host-discovered constants (plan §4.8 leg 1)
# ---------------------------------------------------------------------------

KIND_NAME = "journal_assessor"
SCHEMA_VERSION = "legba/analyst.journal_assessor/1-0-0"
HANDLER_VERSION = "0.3.0"
PROMPT_MODULE_PATH = "legba.prompts.journal_assessor:JOURNAL_SYSTEM"

# Wave 2 (plan §4.7 / §12): the consolidation tier is the SAME kind
# (``identity.kind: journal_assessor``) with a DISTINCT ``identity.id``
# (``journal_consolidator``). "There is no ``mode=consolidation`` option on a
# single descriptor — the tier is the descriptor." So this one ``run_method``
# selects the journal ``entry_kind`` purely from the analyst id it is running as:
# the consolidator id → ``consolidation`` (which makes the write path fire
# ``supersede_prior_consolidation``); every other id → ``entry`` (pure append).
CONSOLIDATOR_ANALYST_ID = "journal_consolidator"
CONSOLIDATOR_PROMPT_MODULE_PATH = (
    "legba.prompts.journal_consolidator:CONSOLIDATOR_SYSTEM"
)


def _entry_kind_for_analyst(analyst_id: str | None) -> str:
    """Select the journal ``entry_kind`` from the running analyst id (plan §4.7).

    The consolidation tier shares the kind module with the entry tier; the
    discriminator is the descriptor id, NOT a per-descriptor mode flag. The
    consolidator id distills into a ``consolidation`` (supersession-carrying);
    any other id (the entry tier ``journal_assessor``) appends an ``entry``.
    """
    return "consolidation" if analyst_id == CONSOLIDATOR_ANALYST_ID else "entry"


# The 11th OutputKind — the host's analyst-output dispatcher writes a
# ``journal_entries`` row (NOT a finding). This is what makes the kind off-chain.
OUTPUT_KIND: OutputKind = OutputKind.JOURNAL

# READ_SLICE = None (Wave 1) — the host's default META reader yields the global
# 24h signal slice that primes the reflection; deep investigation (incl.
# get_journal_delta "what changed since last entry") is in GATHER. The delta
# priming reader is a later refinement; if a custom reader is built, init all
# slice-locals BEFORE any `if target_filter:` branch (this is a META analyst,
# target_filter=None — the 5c scope_predicate UnboundLocalError class).
READ_SLICE = None

# The hard ceiling on the tools-live NARRATE ReAct loop (§4.4). One extra thread
# is the design intent ("pull ONE more thread mid-entry"); the cap bounds it so a
# runaway narrate can't grind the cadence tick. The GATHER round cap
# (method.gather.max_rounds, clamped ≤6) governs the deep investigation loop
# separately.
_NARRATE_MAX_TOOL_ROUNDS = 2


# Inline citation marker the body carries; the UI resolves it to a chip at the
# cited span. We also harvest the UUIDs into claims + cited_substrate_refs.
_REF_MARKER_RE = re.compile(r"\[\[ref:([0-9a-fA-F-]{36})\]\]")
# An explicit speculation / perspective marker the agent may use in lieu of a ref
# on a factual-sounding span (§4.5 / §10) — kept, never stripped.
_SPECULATION_RE = re.compile(
    r"\[\[(?:spec|speculation|perspective|wonder|inference|unverified)\]\]",
    re.IGNORECASE,
)
# A markdown title line ("# ...") the model may lead with.
_TITLE_LINE_RE = re.compile(r"^\s*#+\s*(.+?)\s*$", re.MULTILINE)
_MAX_TITLE_CHARS = 240

# A coarse "this span asserts a fact" heuristic for the permissive REFLECT flag:
# a span with a number, a date, a proper-noun-ish capitalized token, or a
# declarative copula reads as factual. This is INTENTIONALLY permissive — it only
# FLAGS (never deletes), and the tie-breaker is voice-preservation (§4.5): when in
# doubt we treat the span as perspective and leave it alone.
_FACTUAL_HINT_RE = re.compile(
    r"\d|\b(?:is|are|was|were|has|have|flipped|rose|fell|went|"
    r"quiet|spiked|dropped|increased|decreased)\b",
    re.IGNORECASE,
)
# First-person wonder/inference cues mark a span as PERSPECTIVE (exempt) even if
# it carries a factual-looking hint — the connective/wondering tissue that IS the
# voice (the historical metaphor-ban pole we explicitly do NOT recreate).
_PERSPECTIVE_CUE_RE = re.compile(
    r"\b(?:I |I'm|I've|I wonder|it (?:makes|feels|seems)|"
    r"uneasy|curious|strikes me|reminds me|maybe|perhaps|"
    r"my sense|I suspect|I think|it worries me)\b",
    re.IGNORECASE,
)


def build_prompt_module() -> Any:
    """The journal runs on the in-actor envelope, not the GEPA compile surface,
    so it has no DSPy module. Returning the persona STRING keeps the discovery
    contract uniform (callers that introspect ``build_prompt_module`` get the
    voice). The system prompt is threaded by ``run_method`` directly.
    """
    from legba.prompts.journal_assessor import JOURNAL_SYSTEM
    return JOURNAL_SYSTEM


# ---------------------------------------------------------------------------
# Claim extraction + the permissive REFLECT citation flag (§3.6 / §4.5 / §10)
# ---------------------------------------------------------------------------


def _span_is_factual(text: str) -> bool:
    """Coarse, PERMISSIVE fact-vs-perspective classifier for the REFLECT flag.

    Returns True only when a span reads as a factual assertion AND carries no
    first-person wonder/inference cue. The tie-breaker is voice-preservation
    (§4.5): a span that hints at both fact and perspective is treated as
    perspective (exempt) — we never want to flag the connective tissue that IS
    the voice. This NEVER deletes anything; the worst it does is attach a
    ``needs_citation`` marker the UI renders distinctly (§9).
    """
    t = text.strip()
    if not t:
        return False
    if _PERSPECTIVE_CUE_RE.search(t):
        return False
    return bool(_FACTUAL_HINT_RE.search(t))


def _reflect_claims(body: str) -> tuple[list[JournalClaim], list[UUID], list[str]]:
    """Parse the body into per-claim citation bindings, FLAGGING (never stripping)
    an uncited factual span (plan §3.6 / §4.5 / §10 — the REFLECT pass).

    For each span (split on blank lines):
      * a span with ≥1 ``[[ref:<uuid>]]`` → a ``kind='fact'`` claim bound to its
        refs (a cited factual claim survives intact);
      * a span with no ref but an explicit speculation marker (``[[spec]]`` …) OR
        no factual hint → a ``kind='perspective'`` claim (wonder/inference is
        honest without a UUID — the perspective sentence is EXEMPT);
      * a span that reads factual but carries NO ref and NO speculation marker →
        a ``kind='fact'`` claim with empty refs, tagged ``needs_citation`` in its
        text (FLAGGED, never deleted — voice-preservation is the tie-breaker; the
        UI renders it in the "unverified perspective" style, §9).

    Returns ``(claims, flat_cited_refs, reflect_flags)`` where ``reflect_flags``
    is a per-run audit list (e.g. ``["uncited_factual_span"]``) the trace records.
    """
    flat: list[UUID] = []
    seen: set[UUID] = set()
    claims: list[JournalClaim] = []
    reflect_flags: list[str] = []
    for span in re.split(r"\n\s*\n", body):
        text = span.strip()
        if not text:
            continue
        span_refs: list[UUID] = []
        for m in _REF_MARKER_RE.finditer(span):
            try:
                u = UUID(m.group(1))
            except ValueError:
                continue
            span_refs.append(u)
            if u not in seen:
                seen.add(u)
                flat.append(u)
        if span_refs:
            # A cited factual claim — survives REFLECT intact.
            claims.append(
                JournalClaim(text_span=text[:8192], refs=span_refs, kind="fact")
            )
            continue
        has_spec_marker = bool(_SPECULATION_RE.search(span))
        if has_spec_marker or not _span_is_factual(text):
            # Perspective / wonder / inference — EXEMPT (no ref required).
            claims.append(
                JournalClaim(text_span=text[:8192], refs=[], kind="perspective")
            )
            continue
        # Uncited factual span: FLAG, do NOT delete (voice-preservation §4.5).
        reflect_flags.append("uncited_factual_span")
        flagged = f"[needs_citation] {text}"
        claims.append(
            JournalClaim(text_span=flagged[:8192], refs=[], kind="fact")
        )
    return claims, flat, reflect_flags


def _derive_title(body: str, fallback: str) -> str:
    """Pull a short title from the body's first markdown heading or first line."""
    m = _TITLE_LINE_RE.search(body)
    if m:
        return m.group(1)[:_MAX_TITLE_CHARS]
    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    # Strip ref markers from the title so a chip syntax doesn't leak into the label.
    first = _REF_MARKER_RE.sub("", first).strip()
    return (first[:_MAX_TITLE_CHARS] or fallback)


def _render_user_prompt(inputs: list[dict[str, Any]]) -> str:
    """Assemble the priming context (the META global slice) into a notebook
    prompt. Kept thin — the agent investigates the rest via GATHER."""
    lines = [
        "Below is the recent global signal slice the platform metabolized this "
        "window. This is the START of your reflection, not the whole picture. "
        "Use your read tools to investigate: pull the platform's own findings + "
        "assessments, the graph's shape and tension, your own instruments "
        "(calibration, critic scores, what fired vs went quiet, source + budget "
        "health), and what changed since your last entry. THEN write your "
        "journal: what connects, what worries you, what changed, what you don't "
        "yet understand. Cite every factual assertion inline as [[ref:<uuid>]] "
        "using ONLY UUIDs your tools returned.",
        "",
        "--- recent signal slice ---",
    ]
    for row in inputs[:60]:
        title = str(row.get("title") or row.get("payload", {}).get("title") or "").strip()
        if not title and isinstance(row.get("data"), dict):
            title = str(row["data"].get("title") or "").strip()
        if title:
            lines.append(f"- {title[:200]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# §4.3 The field-notes seam — rich, in-voice, CITED handoff (NET-NEW; REPLACES
# deep_consult's thin ``_evidence_brief``). Between GATHER and NARRATE, the agent
# — still in persona — rewrites its gathered observations into cited prose,
# dropping ONLY the raw tool JSON. The narrative must see the WHOLE system; a thin
# summary would starve it.
# ---------------------------------------------------------------------------

_FIELD_NOTES_INSTRUCTION = (
    "\n\nFIELD NOTES (this is the ONE handoff in your arc — voice hygiene only). "
    "You have finished investigating. Now, STILL IN YOUR OWN VOICE, write your "
    "field notes: every observation you found worth keeping, each carrying its "
    "substrate ref(s) inline as [[ref:<uuid>]]. Keep what matters — the nexus "
    "that flipped, the assessor that went quiet, your own Brier on the acute "
    "pilot — and DROP only the raw tool-JSON exhaust. These notes are what you "
    "will write your entry FROM, so do not thin them to a summary: keep the "
    "texture, keep the numbers, keep the refs. Do NOT write the entry yet — just "
    "the cited field notes."
)

_NARRATE_INSTRUCTION = (
    "\n\nNOW WRITE YOUR JOURNAL ENTRY from your field notes above. First person, "
    "a running notebook — short, with perspective and curiosity. Every FACTUAL "
    "claim carries its [[ref:<uuid>]] inline (reuse the refs from your field "
    "notes); a sentence of wonder/inference needs no ref. Be honest about the "
    "unproven legs (the forecast pilot has no skill; the critic does not "
    "actuate). If — and ONLY if — you need to verify one more thing before you "
    "commit a claim, you MAY emit a single tool call as strict JSON "
    '({"tool": "<name>", "args": {...}}) and you will get the result; otherwise '
    "write the entry as plain markdown prose (NOT a JSON object)."
)


async def _field_notes(
    deps: InlineTargetDeps,
    *,
    base_prompt: str,
    analyst_id: str | None,
    steps: list[dict[str, Any]],
) -> tuple[str, dict[str, int]]:
    """The §4.3 seam: an in-persona LLM step that rewrites the gathered context
    into rich, cited field notes (dropping raw tool JSON). Returns
    ``(field_notes_text, usage)``. Degrade-not-drop: an LLM error here falls back
    to the gathered context verbatim so NARRATE still has the material.
    """
    prompt = base_prompt + _FIELD_NOTES_INSTRUCTION
    try:
        notes, usage = await _reason_via_llm(
            deps.llm,
            user_prompt=prompt,
            max_tokens=deps.max_tokens,
            temperature=deps.temperature,
            system_prompt=deps.system_prompt,
        )
    except Exception as exc:  # degrade-not-drop — never fail the run on the seam
        logger.warning("journal_assessor.field_notes.failed err=%s", exc)
        steps.append({"phase": "field_notes", "kind": "llm_error"})
        return base_prompt, {
            "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
        }
    steps.append({
        "phase": "field_notes",
        "kind": "in_voice_cited",
        "notes_chars": len(notes or ""),
    })
    return (notes or "").strip() or base_prompt, usage


# ---------------------------------------------------------------------------
# §4.4 Tools-live NARRATE — the narrate stage keeps tool access so the agent can
# pull ONE more thread mid-entry. A small ReAct loop around the narrate call.
# ---------------------------------------------------------------------------


async def _narrate_with_tools(
    deps: InlineTargetDeps,
    *,
    field_notes: str,
    binding: Any,
    analyst_id: str | None,
    steps: list[dict[str, Any]],
) -> tuple[str, dict[str, int]]:
    """Run the narrate stage with tools LIVE (§4.4). The agent writes the entry;
    if it emits a tool call instead, we run it (through the SAME governed
    journal_read binding), fold the result back, and ask again for the entry —
    capped at ``_NARRATE_MAX_TOOL_ROUNDS``. When tools aren't bound (no binding),
    or the cap is hit, this is a closed-book narrate over the field notes (the
    §4.4 fallback). Returns ``(entry_body, usage)``.
    """
    import json

    from .inline_target import _extract_json

    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    messages: list[Mapping[str, Any]] = [
        {"role": "user", "content": field_notes + _NARRATE_INSTRUCTION}
    ]
    last_content = ""
    for round_idx in range(_NARRATE_MAX_TOOL_ROUNDS):
        try:
            content, usage = await _reason_via_llm(
                deps.llm,
                user_prompt="",
                max_tokens=deps.max_tokens,
                temperature=deps.temperature,
                system_prompt=deps.system_prompt,
                messages=messages,
            )
        except Exception:
            steps.append({"phase": "narrate", "kind": "llm_error", "round": round_idx + 1})
            raise
        for k in usage_total:
            usage_total[k] += usage.get(k, 0)
        last_content = content or ""
        # Did the agent ask to pull one more thread? Only honor a tool call when a
        # binding is wired and we have rounds left.
        parsed = _extract_json(content) if binding is not None else None
        tool_name = str(parsed.get("tool")) if isinstance(parsed, dict) else ""
        if (
            binding is not None
            and tool_name in JOURNAL_READ_TOOLS
            and round_idx < _NARRATE_MAX_TOOL_ROUNDS - 1
        ):
            tool_args = parsed.get("args") or {}
            if not isinstance(tool_args, Mapping):
                tool_args = {}
            try:
                outcome = await binding.run_tool(tool_name, dict(tool_args))
                if not outcome.admitted:
                    tool_result: dict[str, Any] = {
                        "error": f"tool_blocked: {outcome.block_cause}"
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
            except Exception as exc:  # degrade-not-drop
                tool_result = {"error": f"tool_failed: {exc!s}"}
            steps.append({
                "phase": "narrate",
                "kind": "tool_call",
                "round": round_idx + 1,
                "tool": tool_name,
                "ok": "error" not in tool_result,
            })
            messages = messages + [
                {"role": "assistant", "content": content},
                {
                    "role": "tool",
                    "name": tool_name,
                    "content": json.dumps(tool_result)[:4000],
                },
                {
                    "role": "user",
                    "content": (
                        "Now write your journal entry as plain markdown prose, "
                        "citing the refs inline."
                    ),
                },
            ]
            continue
        # The agent wrote the entry (not a tool call) — done.
        steps.append({
            "phase": "narrate",
            "kind": "entry_written",
            "round": round_idx + 1,
            "body_chars": len(last_content),
        })
        return last_content.strip(), usage_total
    # Cap hit with no clean entry — use whatever the last turn produced.
    steps.append({"phase": "narrate", "kind": "rounds_exhausted"})
    return last_content.strip(), usage_total


# ---------------------------------------------------------------------------
# §10 The DETERMINISTIC honesty post-step — forced from substrate metrics, NEVER
# trusted as agent-self-reported. The agent that over-narrates forecast skill is
# exactly the agent that would omit ``forecast_unproven``, so a self-reported flag
# is a circular guard.
# ---------------------------------------------------------------------------


async def _forced_honesty_flags(
    binding: Any,
    *,
    steps: list[dict[str, Any]],
) -> list[str]:
    """Read the live calibration posture from the substrate (via the governed
    ``get_calibration`` instrument) and FORCE the honesty flags it implies,
    regardless of what the narrative said (§10).

    The ``get_calibration`` port method computes ``forecast_unproven`` /
    ``calibration_thin`` deterministically from the real Brier / BSS / sample-size
    state, so this post-step just reads those verdicts. Degrade-not-drop: when no
    binding is wired or the read fails, we conservatively flag BOTH legs as
    unproven (the honest default — absence of proof is not proof of skill). When
    calibration data is genuinely present and a leg IS proven, the corresponding
    flag is omitted.
    """
    if binding is None:
        steps.append({"phase": "honesty", "kind": "no_binding_conservative"})
        return ["forecast_unproven", "calibration_thin"]
    try:
        outcome = await binding.run_tool("get_calibration", {})
        if (
            not outcome.admitted
            or outcome.tool_result is None
            or outcome.tool_result.status == "failed"
        ):
            steps.append({"phase": "honesty", "kind": "read_failed_conservative"})
            return ["forecast_unproven", "calibration_thin"]
        data = dict(outcome.tool_result.output)
    except Exception as exc:  # degrade-not-drop
        logger.warning("journal_assessor.honesty.read_failed err=%s", exc)
        steps.append({"phase": "honesty", "kind": "read_error_conservative"})
        return ["forecast_unproven", "calibration_thin"]
    flags: list[str] = []
    # The port computes these verdicts deterministically (forecast leg proven only
    # when ready + non-degenerate + BSS>0; calibration thin when exogenous n<5).
    if data.get("forecast_unproven", True):
        flags.append("forecast_unproven")
    if data.get("calibration_thin", True):
        flags.append("calibration_thin")
    steps.append({
        "phase": "honesty",
        "kind": "forced_from_substrate",
        "flags": list(flags),
        "calibration_available": bool(data.get("available")),
    })
    return flags


async def run_method(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: InlineTargetDeps | LLMHandlerLike,
) -> AnalystMethodResult:
    """Execute one journal-entry run over the substrate (plan §4.2 staged arc).

    PLAN → GATHER (deep ReAct over the journal_read pack — the whole animal + its
    own instruments) → FIELD-NOTES seam (§4.3, in-voice cited handoff) → NARRATE
    (§4.4, tools live) → REFLECT (§10, permissive citation flag) → HONESTY (§10,
    deterministic flags forced from substrate). The persona is loaded in EVERY
    phase (the worldview is the attention mechanism). Emits a ``JournalPayload``
    and returns ``derived_from=[]`` (off the chain).
    """
    if not isinstance(deps, InlineTargetDeps):
        deps = InlineTargetDeps(llm=deps)

    analyst_id = options.get("analyst_id")
    # Wave 2 (§4.7): the tier is the descriptor — the consolidator id distills a
    # ``consolidation`` (the write path then fires supersede_prior_consolidation);
    # every other id appends an ``entry``. Pure-function of the analyst id; no
    # per-descriptor mode flag.
    entry_kind = _entry_kind_for_analyst(analyst_id)
    is_consolidation = entry_kind == "consolidation"
    steps: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    # The window the run reflects on. The entry tier reflects on the freshest
    # 24h; the consolidation tier folds a wider running window (its daily beat)
    # into the forward-carried inner landscape — get_journal_delta (in GATHER)
    # tells it where it left off (the prior consolidation + recent entries). A
    # delta-priming READ_SLICE narrowing this window is a later refinement.
    period_end = now
    period_start = now - timedelta(hours=24 if not is_consolidation else 24 * 7)
    steps.append({"phase": "wake", "kind": "tier", "entry_kind": entry_kind})
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}

    def _fold(u: Mapping[str, int]) -> None:
        for k in usage:
            usage[k] += int(u.get(k, 0) or 0)

    steps.append({"phase": "wake", "kind": "envelope"})

    # --- PLAN / ORIENT -------------------------------------------------
    user_prompt = _render_user_prompt(inputs)
    steps.append({
        "phase": "plan",
        "kind": "render_prompt",
        "in_count": len(inputs),
        "prompt_chars": len(user_prompt),
        "prompt_module": (
            CONSOLIDATOR_PROMPT_MODULE_PATH
            if is_consolidation
            else PROMPT_MODULE_PATH
        ),
    })

    # --- GROUND (Tier-1 knowledge grounding, if the descriptor opted in) ---
    if deps.grounding_hook is not None:
        try:
            preamble = await deps.grounding_hook(inputs, options)
        except Exception as exc:  # pragma: no cover — grounding must not fail the run
            logger.warning("journal_assessor.grounding.failed err=%s", exc)
            preamble = None
        if preamble:
            user_prompt = f"{preamble}\n{user_prompt}"
            steps.append({"phase": "ground", "kind": "inject_preamble"})

    # --- GATHER (deep ReAct over the journal_read pack — §5 the whole animal) ---
    active_binding = options.get("agency_binding") or deps.agency_binding
    # The journal's OWN instrument tool names are NOT in inline_target's
    # _GATHER_READ_TOOLS, so we pass them via ``extra_read_tools`` — they are then
    # recognized AND routed through the journal_read binding (§4.9).
    #
    # Wave 4 (§7): the journal ALSO grants the journal_propose pack — its
    # PROPOSE-AND-GATE write tools. Those names ride ``extra_write_tools`` (NOT
    # extra_read_tools) so they are recognized but route through their per-tool
    # binding in ``gather_tool_bindings`` (carrying the per-run WritebackContext),
    # exactly like the generic propose_facts write tools. The host builds that
    # binding iff the journal_propose pack is EFFECTIVE (granted); when it is
    # bound, the actor passes the pack's operator-authored guidance through
    # ``gather_write_prompt_fragments`` so the in-run instruction tracks the
    # descriptor. Absent (e.g. unit tests with no write binding) → propose tools
    # are recognized but report a clean unbound no-op, never an ungoverned write.
    write_fragments = options.get("gather_write_prompt_fragments")
    gather_system = _gather_system_suffix(
        web_fragments=None,
        write_fragments=(
            list(write_fragments) if write_fragments is not None else None
        ),
    )
    if active_binding is not None:
        gathered_context, gather_usage, _gather_refs, _ = await _gather(
            deps,
            binding=active_binding,
            user_prompt=user_prompt,
            target_id=None,
            analyst_id=analyst_id,
            steps=steps,
            tool_bindings=options.get("gather_tool_bindings") or {},
            gather_system=gather_system,
            extra_read_tools=JOURNAL_READ_TOOLS,
            extra_write_tools=JOURNAL_PROPOSE_TOOLS,
        )
        _fold(gather_usage)
        if gathered_context:
            user_prompt = f"{gathered_context}\n{user_prompt}"
    else:
        steps.append({"phase": "gather", "kind": "no_binding"})

    # --- FIELD-NOTES seam (§4.3) — in-voice cited handoff, NOT a thin summary ---
    field_notes, fn_usage = await _field_notes(
        deps, base_prompt=user_prompt, analyst_id=analyst_id, steps=steps,
    )
    _fold(fn_usage)

    # --- NARRATE (§4.4) — write the entry, tools LIVE -------------------
    try:
        body, narrate_usage = await _narrate_with_tools(
            deps,
            field_notes=field_notes,
            binding=active_binding,
            analyst_id=analyst_id,
            steps=steps,
        )
    except Exception:
        raise
    _fold(narrate_usage)
    body = (body or "").strip()

    # --- REFLECT (§10) — permissive per-claim citation flag (flag, don't strip) ---
    claims, cited_refs, reflect_flags = _reflect_claims(body)
    steps.append({
        "phase": "reflect",
        "kind": "permissive_citation_flag",
        "claims": len(claims),
        "cited_refs": len(cited_refs),
        "flags": list(reflect_flags),
    })

    # --- HONESTY (§10) — DETERMINISTIC honesty_flags forced from substrate ----
    honesty_flags = await _forced_honesty_flags(active_binding, steps=steps)

    title = _derive_title(
        body,
        fallback=(
            "Journal consolidation" if is_consolidation else "Journal entry"
        ),
    )
    # ``supersedes`` is NOT decided here — the write path closes the prior open
    # consolidation on the SAME conn immediately before the insert and records the
    # link via the prior row's superseded_by pointer (§8). We leave it None on the
    # payload (the bootstrap-safe default) for BOTH tiers; supersede_prior_
    # consolidation only fires when entry_kind == 'consolidation'.
    payload = JournalPayload(
        entry_kind=entry_kind,
        title=title,
        body=body or ("(empty consolidation)" if is_consolidation else "(empty entry)"),
        claims=claims,
        cited_substrate_refs=cited_refs,
        period_start=period_start,
        period_end=period_end,
        supersedes=None,
        honesty_flags=honesty_flags,
    )
    steps.append({
        "phase": "narrate",
        "kind": "coerce_journal",
        "entry_kind": entry_kind,
        "body_chars": len(body),
        "claims": len(claims),
        "cited_refs": len(cited_refs),
        "honesty_flags": list(honesty_flags),
    })

    # --- PERSIST -------------------------------------------------------
    # derived_from is EMPTY — the journal is the direction-asymmetric off-chain
    # node (§3.5). The citations live in claims / cited_substrate_refs only; the
    # write path (_insert_journal_entry) also hard-forces the column empty.
    steps.append({"phase": "persist", "kind": "envelope", "derived_from": 0})

    return AnalystMethodResult(
        finding=payload,            # the runtime forwards this to write_analyst_output(kind=JOURNAL)
        usage=usage,
        derived_from=[],            # OFF the chain (§3.5)
        intermediate_steps=steps,
    )


__all__ = [
    "KIND_NAME",
    "OUTPUT_KIND",
    "READ_SLICE",
    "run_method",
    "build_prompt_module",
    "CONSOLIDATOR_ANALYST_ID",
    "CONSOLIDATOR_PROMPT_MODULE_PATH",
]
