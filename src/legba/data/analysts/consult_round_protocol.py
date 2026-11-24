# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Consult round protocol — the native-tool route and the loop's budget (D-7).

Two things live here, both lifted out of ``consult_on_demand`` so that module
keeps its shape (and its size ceiling) while gaining them.

1. The native tool-use round protocol
=====================================

The ReAct loop's round protocol is parsed out of the model's **text**: a strict
JSON object means "call tools", the ``<<<FINAL>>>`` sentinel means "here is the
answer", and anything else is ``unparseable`` — a wasted round, a wasted Opus
call, and a step the operator sees as a failure. Run 3ae77c64 burned one that
way, and each burned round on a pinned-context Opus consult costs 40-100s of
the wall clock that later ran out.

Both of our planes have a *structured* channel for exactly this, and neither
was using it. Anthropic has ``tool_use`` blocks; the core plane's vLLM is
launched with ``--enable-auto-tool-choice --tool-call-parser openai`` and
``VLLMProviderHandler`` has sent ``tools`` and parsed ``tool_calls`` all along
— measured working end-to-end through the production gateway, at ~1s per tool
round against the 40-100s a text round costs on Opus. In the 24h before this
change, zero of 1,552 core-plane calls carried a tool call: nothing in
production drove it. So native is now the default on **both** routes, and the
JSON-in-text protocol is the fallback for a handler we don't recognise.

On either route "prose plus JSON" cannot be ambiguous, because the two travel
in different fields:

* a reply with ``tool_use`` blocks is a tool round, full stop;
* a reply with none is the answer, full stop — its text is the §28.4 markdown
  FINAL, and if it doesn't carry the sentinel header block we take the prose as
  the answer rather than burning a round asking for a re-format.

So the ``unparseable`` class does not exist on this route. It is not made
rarer; there is no branch that can produce it.

**The grammar is shared, not re-authored.** ``stack/llm/tool_rounds`` owns the
tool schemas, both provider wire shapes, the parser and the replay builders,
because the agency's GATHER loop needs exactly the same things and two copies
would become two dialects. This module holds only what is specific to THIS
loop: which tools it offers, what its system prompt says about the protocol,
and how a round maps onto its existing ``{tool, args}`` dispatch.

Each whitelisted tool is offered under its own name with its own argument
schema from that shared catalogue, so the provider rejects a bad call before it
costs a round-trip. Five of the nineteen have no catalogue entry yet and get a
permissive free-form spec; their signatures are still stated in the system
prompt's tool roster, which is where they have always been.

**Never key on ``finish_reason``.** Measured on the core plane: a forced tool
choice returns ``tool_calls`` with ``finish_reason: "stop"``. The presence of
tool calls is the signal; the finish reason is not. The shared parser enforces
this; it is repeated here because it is the mistake to make.

**The paranoid fallback.** If a handler silently ignored ``tools``, a model
still emitting the old JSON would come back as text — and native mode would
persist that JSON *as the answer*. :func:`text_tool_round_fallback` catches
exactly that: text with no tool call that parses as a tool-request object is
treated as a tool round, not a final. So the native route is strictly safer
than the text route even where native isn't really there.

2. The loop's wall-clock budget
===============================

The loop had a drilling budget (``LEGBA_CONSULT_WALL_BUDGET_SECONDS``, 210s)
that stops it *requesting more tools*. What it did not have was a ceiling on
the whole run: the forced-final synthesis after that check was unbounded, and a
single LLM call had no deadline at all. That is the shape of the 2026-09-16
failure — the budget check passed at round 6, the final synthesis started at
269s, and the front door's clock ran out while it was still generating.

A budget you can overrun is not a budget. So there are now three:

* ``wall_budget_seconds`` — stop drilling (unchanged, 210s);
* ``round_deadline_seconds`` — per-LLM-call deadline (150s), so one stuck call
  can't eat the run;
* ``total_budget_seconds`` — the hard ceiling on everything (480s, env
  ``LEGBA_CONSULT_BUDGET_SECONDS``), inside which the forced final must also
  fit.

When the total is gone the loop does **not** die: it emits
:func:`degraded_final_payload`, an honest answer built from what it actually
has, that says how many rounds it got and what it never folded in.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..stack.llm import tool_rounds as _tr

# The three clock defaults and their env readers live in the stdlib-only leaf
# ``legba.data.consult_clocks`` so the registry can derive its invoke timeout
# from the SAME readers without importing this package (run 9b902fec). Every
# name is re-exported here unchanged.
from ..consult_clocks import (
    DEFAULT_FINAL_FLOOR_SECONDS,
    DEFAULT_ROUND_DEADLINE_SECONDS,
    DEFAULT_TOTAL_BUDGET_SECONDS,
    default_final_floor_seconds,
    default_round_deadline_seconds,
    default_total_budget_seconds,
)
from ..consult_clocks import env_seconds as _env_seconds

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

#: Reserve carved out of the total for the forced final. Once less than this
#: remains the loop stops drilling even if the drilling budget is untouched —
#: an answer synthesised from four rounds beats a timeout after six.
FINAL_RESERVE_SECONDS = 150.0

#: Native multi-call batch cap per round. Below ``MAX_TOOLS_PER_BATCH`` (5)
#: because on the native route every result is replayed into every subsequent
#: prompt, so a round's width multiplies the transcript for the REST of the
#: run. Four wide reads still survey; the fifth mostly buys tokens. A
#: descriptor may raise it.
DEFAULT_NATIVE_BATCH_CAP = 4


def final_synthesis_budget(remaining_seconds: float, *, floor: float | None = None) -> float:
    """Seconds the final synthesis gets: the remainder, never below the floor.

    ``max`` and not ``min``. Stated that plainly because the bug was a ``min``
    and it read as reasonable in review.
    """
    return max(remaining_seconds, default_final_floor_seconds() if floor is None else floor)


def native_batch_cap(descriptor_cap: int | None = None) -> int:
    """Per-round native call cap. Env ``LEGBA_CONSULT_NATIVE_BATCH_CAP``.

    Precedence: an explicit descriptor cap wins, then the env pin, then the
    default — so a descriptor that knows its tool mix needs five wide reads can
    say so without the operator editing the environment.
    """
    if descriptor_cap is not None and descriptor_cap > 0:
        return int(descriptor_cap)
    return int(_env_seconds("LEGBA_CONSULT_NATIVE_BATCH_CAP", DEFAULT_NATIVE_BATCH_CAP))


def partial_final_payload(
    *,
    partial_text: str,
    rounds_used: int,
    rounds_available: int,
    elapsed_s: float,
    reason: str,
    last_tool_incorporated: bool,
) -> dict[str, Any]:
    """The FINAL payload for a synthesis that was CUT while generating.

    The counterpart to :func:`degraded_final_payload`, and the reason that one
    is now the rarer branch. When the synthesis produced no text at all there
    is nothing to deliver and the degraded message is honest. When it produced
    eleven thousand characters and was cut at the twelve-thousandth, replacing
    them with an apology destroys the only thing the run made — and the
    operator was billed for every one of those characters.

    So: the partial text IS the answer, prefixed with a banner that says what
    it is. The banner leads rather than trails because a reader scanning a long
    answer must learn it is incomplete before they act on it, not after.

    ``uncertainty`` is 0.7 — above a normal answer (this one did not get to
    state its own confidence or write its caveats) but below the 0.85 of a run
    that synthesised nothing at all, because the substance here is real.
    """
    text = (partial_text or "").strip()
    approx_tokens = len(text) // 4
    tail = "" if last_tool_incorporated else " The last tool result was not incorporated."
    banner = (
        f"> **Synthesis interrupted at ~{approx_tokens:,} tokens "
        f"({len(text):,} characters).** The answer below is what the model had "
        f"written when {reason}; it stops mid-thought and its closing caveats "
        f"are missing. Evidence and citations are on this turn's trace, and "
        f'"Synthesize from evidence" will re-run the synthesis over exactly '
        f"that evidence without drilling again.{tail}\n\n---\n\n"
    )
    return {
        "final": True,
        "answer": banner + text,
        "uncertainty": 0.7,
        "synthesis_partial": True,
        "unanswered_aspects": [
            f"The synthesis was cut off after ~{approx_tokens:,} tokens: {reason}.",
        ],
    }


#: ``finish_reason`` values that say nothing about WHY a call stopped, so the
#: completion-token count decides instead.
_UNKNOWN_FINISH = (None, "", "unknown")


def output_limit(llm: Any, fallback: int | None) -> int | None:
    """The output limit a call on ``llm`` actually runs under (roll-86
    follow-up: no output caps — a caller's ``max_tokens`` never reaches a
    wire): the model's declared maximum (``model_max_output_tokens``, the one
    declared place), else ``fallback`` for a handler that declares none (a test
    double). The "partial" label's count rule and the turn's spend projection
    read this, not the descriptor's ``method.llm.max_tokens``."""
    probe = getattr(llm, "model_max_output_tokens", None)
    try:
        limit = probe() if callable(probe) else None
    except Exception:  # noqa: BLE001 — a label must never fail a turn
        limit = None
    return int(limit) if isinstance(limit, int) and limit > 0 else fallback


def output_cap_hit(response: Any, usage: Mapping[str, Any], max_tokens: int | None) -> bool:
    """Did this terminal call stop at its OUTPUT cap rather than finish?

    ``finish_reason == "length"`` says so directly (both handlers normalize to
    it). When the provider reported no reason, a completion count at or over
    the call's ``max_tokens`` is the same fact. Read ONLY on a terminal answer
    call: on a tool round the module docstring's rule stands — never key on
    ``finish_reason`` there.
    """
    finish = getattr(response, "finish_reason", None)
    if finish == "length":
        return True
    if finish not in _UNKNOWN_FINISH or not max_tokens:
        return False
    return int(usage.get("completion_tokens") or 0) >= int(max_tokens)


def settle_final_reply(
    parsed: Mapping[str, Any],
    response: Any,
    usage: Mapping[str, Any],
    *,
    max_tokens: int | None,
    rounds_used: int,
    rounds_available: int,
    elapsed_s: float,
    last_tool_incorporated: bool,
) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
    """``(payload, synthesis_status, step)`` for one terminal answer reply (W11-F160).

    * No answer body — a sentinel and header lines only, or nothing — is NOT an
      answer: ``(None, "none", step)``, so the turn stores an empty answer and
      the panel offers "Synthesize from evidence". Header text is never stored
      as the answer (turn ``91b6260c``).
    * A body cut at the output cap is ``"partial"``: the prose is kept under the
      :func:`partial_final_payload` banner, with the model's own ``cited_refs``.
    * Otherwise the parsed payload is returned untouched as ``"complete"``.

    ``step`` is the trace frame to record, ``None`` for a complete answer.
    """
    finish = getattr(response, "finish_reason", None)
    capped = output_cap_hit(response, usage, max_tokens)
    answer = str(parsed.get("answer") or "").strip()
    if not answer:
        return None, "none", {
            "phase": "reflect", "kind": "degraded_final",
            "reason": "empty_answer_body", "finish_reason": finish,
            "output_cap_hit": capped,
        }
    if not capped:
        return dict(parsed), "complete", None
    reason = f"the model hit its {int(max_tokens or 0):,}-token output cap"
    payload = partial_final_payload(
        partial_text=answer, rounds_used=rounds_used,
        rounds_available=rounds_available, elapsed_s=elapsed_s,
        reason=reason, last_tool_incorporated=last_tool_incorporated,
    )
    if parsed.get("cited_refs"):
        payload["cited_refs"] = parsed["cited_refs"]
    return payload, "partial", {
        "phase": "reflect", "kind": "partial_final", "reason": "output_token_cap",
        "finish_reason": finish, "max_tokens": max_tokens,
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "chars": len(answer),
    }


#: The stamp a held answer carries on the wire, so the panel keys on a token
#: rather than on the banner's prose.
PROVENANCE_HOLD_REASON = "provenance_hold"


def provenance_hold(census: Mapping[str, Any] | None) -> tuple[str, str] | None:
    """``(banner, hold_reason)`` for an answer that resolved NOTHING, else None.

    The sibling of :func:`partial_final_payload`, and the same posture: an
    answer the reader must not take at face value leads with a line saying so,
    because a reader scanning a long answer must learn what it is before they
    act on it, not after.

    THE CASE IT CATCHES, from the 2026-09-28 review: the core-plane run
    fabricated a tool RESULT into its own ARGS, quit at round 3 of 10, and
    wrote a confident answer citing six ids that do not exist. Nothing in the
    platform said a word. The answer rested on the model's own knowledge and
    read exactly like one resting on the corpus.

    THE TEST, and it is deliberately narrow. Both halves must be MEASURED —
    the census reports ``None``, never a fabricated zero, when it could not
    read a class, and an unmeasured census holds nothing:

      * no cited ref resolved to ANY row — not a LIVE or HISTORY signal, not a
        web-retrieval or seed row, and not a platform row without an origin
        class (a situation, finding, hypothesis, event or nexus; those are the
        corpus's own finished work and the prompt says to read them first);
      * and the answer still asserts — at least one sentence naming no
        evidence at all.

    Zero resolved refs with zero asserting sentences is not a held answer; it
    is a refusal, an empty answer, or a pure framing note, and banner-ing that
    would be noise. ``uncertainty`` is NOT touched here: the evidence floor is
    its own change and lives on its own path.
    """
    if not isinstance(census, Mapping):
        return None
    live = census.get("live")
    history = census.get("history")
    model_knowledge = census.get("model_knowledge")
    if not isinstance(live, int) or not isinstance(history, int):
        return None
    if not isinstance(model_knowledge, int) or model_knowledge < 1:
        return None
    if live + history > 0:
        return None
    # W4b F-9 — refs past the classify cap were never looked up, so "nothing
    # resolved" has not been measured for them. An unmeasured remainder holds
    # nothing, same as an unmeasured class.
    over_cap = census.get("unclassified_over_cap")
    if isinstance(over_cap, int) and over_cap > 0:
        return None
    if isinstance(census.get("web_retrieval"), int) and census["web_retrieval"] > 0:
        return None
    if isinstance(census.get("seed"), int) and census["seed"] > 0:
        return None
    # A citation that landed on a REAL platform row that carries no origin
    # class — a situation, a finding, a hypothesis, an event, a nexus — is
    # evidence the corpus produced, and the prompt tells the model to consult
    # exactly those rows FIRST. The hold is for an answer whose citations
    # checked out NOWHERE (fabricated, malformed, or none at all) — the
    # roll-28 proof held an answer cited to twelve real situations, which is
    # the opposite of the case this exists for.
    if isinstance(census.get("unresolved"), int) and census["unresolved"] > 0:
        return None
    by_kind = census.get("by_kind")
    if isinstance(by_kind, Mapping) and any(
        isinstance(v, int) and v > 0 for v in by_kind.values()
    ):
        return None
    examined = census.get("sentences_examined")
    sentences = (
        f"{model_knowledge} of {examined} sentences"
        if isinstance(examined, int) and examined
        else f"{model_knowledge} sentence" + ("" if model_knowledge == 1 else "s")
    )
    tail = []
    for key, phrase in (
        ("unresolved", "{n} cited id{s} resolved to a record carrying no origin class"),
        ("fabricated", "{n} cited id{s} matched no record at all"),
    ):
        value = census.get(key)
        if isinstance(value, int) and value > 0:
            tail.append(phrase.format(n=value, s="" if value == 1 else "s"))
    malformed = census.get("malformed")
    if isinstance(malformed, list) and malformed:
        shown = ", ".join(str(m) for m in malformed[:3])
        tail.append(
            f"{len(malformed)} citation token"
            f"{'' if len(malformed) == 1 else 's'} were not ids at all ({shown})"
        )
    detail = f" {'; '.join(tail)}." if tail else ""
    banner = (
        "> **Held: nothing in this answer resolved to live or held reporting.** "
        f"Not one cited reference came back from the corpus, and {sentences} "
        f"name no evidence.{detail} Read what follows as the model's own "
        "knowledge until it is grounded — the citations below did not check "
        "out.\n\n---\n\n"
    )
    return banner, PROVENANCE_HOLD_REASON


def degraded_final_payload(
    *,
    rounds_used: int,
    rounds_available: int,
    elapsed_s: float,
    reason: str,
    last_tool_incorporated: bool,
) -> dict[str, Any]:
    """The FINAL payload for a run that ran out of budget before synthesising.

    Returns the same shape a parsed sentinel final produces, so every consumer
    downstream is unchanged. The answer text is deliberately plain about what
    the operator is looking at — a consult that says "I got four of ten rounds
    and never folded in the last tool result" is useful; one that silently
    presents a thin answer as a complete one is not.

    ``uncertainty`` is pinned high (0.85): this run did not get to weigh its
    evidence, and the number is the honest consequence of that.
    """
    tail = (
        ""
        if last_tool_incorporated
        else " The last tool result was not incorporated."
    )
    answer = (
        f"**This consult ran out of its time budget before it could "
        f"synthesise a full answer.**\n\n"
        f"It completed {rounds_used} of {rounds_available} available rounds in "
        f"{elapsed_s:.0f}s and stopped because {reason}.{tail}\n\n"
        f"The tool calls it did make are on this turn's trace — they are real "
        f"results, not a summary of them. Re-ask with a narrower question, or "
        f"raise `LEGBA_CONSULT_BUDGET_SECONDS`, to get a synthesised answer "
        f"over the same evidence."
    )
    return {
        "final": True,
        "answer": answer,
        "uncertainty": 0.85,
        "unanswered_aspects": [
            f"Synthesis was not performed: {reason}.",
        ],
    }


# ---------------------------------------------------------------------------
# Native tool-call protocol — the consult loop's half of it
# ---------------------------------------------------------------------------
#
# The GRAMMAR lives in ``stack/llm/tool_rounds``: the tool schemas, the two
# provider wire shapes, the parser, and the replay builders, shared with the
# agency's GATHER loop so the two cannot drift into two dialects. What lives
# HERE is only what is specific to this loop — which tools it offers, what its
# system prompt says about the protocol, and how a round maps onto its existing
# ``{tool, args}`` dispatch.


def native_tools_enabled() -> bool:
    """Kill switch: ``LEGBA_CONSULT_NATIVE_TOOLS=0`` forces the text protocol.

    Default ON — the native route is the fix, not an experiment. The switch
    exists so an operator can fall back in one env change if a provider's tool
    surface regresses, without a rollback.
    """
    raw = os.getenv("LEGBA_CONSULT_NATIVE_TOOLS", "").strip().lower()
    return raw not in ("0", "false", "off", "no")


def supports_native_tools(llm: Any) -> bool:
    """Whether ``llm`` should run the native tool-call round protocol.

    The decision is the shared module's (one list of proven planes), gated by
    this loop's kill switch. Conservative either way: an unrecognised handler —
    including a test double with no ``subprovider`` — keeps the text protocol,
    which works everywhere.
    """
    return native_tools_enabled() and _tr.supports_native_tools(llm)


def native_wire(llm: Any) -> str:
    """Which wire grammar ``llm`` speaks: ``anthropic`` | ``openai_compat``."""
    return (
        _tr.provider_for_subprovider(getattr(llm, "subprovider", None))
        or _tr.PROVIDER_OPENAI_COMPAT
    )


def native_tool_specs(known_tools: Any) -> list[_tr.ToolSpec]:
    """Specs for the consult whitelist, from the shared tool catalogue.

    Every tool is offered under its OWN name with its own argument schema.
    The PLATFORM validates the arguments against that schema, not the
    provider: this comment used to claim the provider did, and on 2026-09-28
    a core-plane call carrying a fabricated ``result`` array and a
    ``status: "ok"`` that no situation has went through the endpoint, through
    this loop and into a SQL predicate without one word of complaint. The
    check is ``tool_rounds.validate_args``, run at the agency hard gate
    (``agency.Agency._run_pack_tool`` step 2.5), and a failure comes back to
    the model as ``tool_blocked: <cause>: <detail>``. Five of the nineteen
    have no catalogue entry yet and get the shared module's
    permissive free-form spec — the loop can route them, so it must be able to
    offer them, and their signatures are still stated in the system prompt's
    tool roster.
    """
    return _tr.build_tool_specs(known_tools)


def render_tools_for(wire: str, known_tools: Any) -> list[dict[str, Any]]:
    """The ``tools`` payload to put on the wire for ``known_tools``.

    One call so the loop never holds specs and a wire payload at the same time
    and has to remember which is which.
    """
    return _tr.render_tools(wire, native_tool_specs(known_tools))


def native_system_suffix(final_sentinel: str) -> str:
    """Protocol override appended to the system prompt on the native route.

    The descriptor's prompt describes the JSON-in-text protocol. On this route
    that protocol is replaced, not extended, so the suffix says so plainly —
    otherwise the model reads two contradictory contracts and picks one at
    random, which is its own source of malformed rounds.
    """
    return (
        "\n\n## Round protocol override (native tools)\n\n"
        "On THIS plane you have a real tool-calling channel, and it replaces "
        "the JSON reply shape described above. Ignore the instruction to emit "
        "strict JSON for tool calls — call the tools directly, and emit every "
        "call you need this round in ONE reply: they run concurrently and cost "
        "a single round.\n\n"
        "To ANSWER, simply reply with text and call no tool. Keep the answer "
        f"format exactly as described above: the `{final_sentinel}` line, the "
        "`uncertainty` / `cited_refs` / `unanswered_aspects` header lines, "
        "then your answer as plain markdown. Never put the answer inside a "
        "tool call, and never wrap it in JSON."
    )


@dataclass
class NativeRound:
    """One parsed native-protocol round, in the consult loop's terms.

    ``calls`` is the provider-independent list from the shared parser, ids
    intact for the replay; ``batch`` is the same round expressed as the
    ``{tool, args}`` dicts this loop's dispatch already takes, so nothing below
    the parse had to learn a new shape.
    """

    #: The raw ``LLMResponse``. Held because the shared replay builder reads
    #: the visible text off it — and reads ONLY that, so private reasoning is
    #: structurally unable to ride along.
    response: Any
    #: Assistant text alongside the calls (or the whole reply, when there were
    #: none). The core plane sends ``content: null`` on a tool round.
    text: str
    calls: list[_tr.ToolCall] = field(default_factory=list)
    batch: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_final(self) -> bool:
        """No tool call was emitted ⇒ this reply IS the answer.

        The whole point of the native route: there is no third outcome, so
        there is nothing left to classify as unparseable.
        """
        return not self.calls


def parse_native_reply(
    response: Any, *, provider: str, normalize_calls: Any
) -> NativeRound:
    """Split an ``LLMResponse`` into its tool calls and its text.

    Parsing is the shared module's — keyed on the calls the provider returned,
    never on ``finish_reason`` (a forced choice on the core plane returns
    ``stop`` WITH calls present). What is consult-specific is projecting those
    calls onto ``{tool, args}`` and running them through the kind's
    ``_normalize_calls`` — passed in rather than imported, to keep this module
    free of a circular import back into ``consult_on_demand`` — so the batch
    cap, the dedupe and the malformed-entry drops behave identically on both
    routes.
    """
    calls = _tr.parse_tool_calls(provider, response)
    batch = normalize_calls(
        {"tools": [{"tool": c.name, "args": dict(c.args)} for c in calls]}
    )
    return NativeRound(
        response=response,
        text=_tr.visible_text(response),
        calls=calls,
        batch=batch,
    )


def native_assistant_message(round_: NativeRound, *, wire: str) -> dict[str, Any]:
    """The assistant turn to replay, in ``wire``'s shape, ids intact."""
    return _tr.assistant_tool_turn(wire, round_.response, round_.calls)


def native_tool_result_messages(
    round_: NativeRound,
    results: Sequence[Any],
    *,
    wire: str,
    bounded_json: Any,
    limit: int = 8000,
) -> list[dict[str, Any]]:
    """The reply turn(s) carrying one result per issued call.

    ``results`` is positional against ``round_.calls``. Bodies are rendered
    with the kind's ``_bounded_tool_json`` so truncation is the same JSON-safe
    cut the text route uses; the shared builder then knows the per-provider
    message shape. A call the loop dropped (past the batch cap) still gets a
    body saying so — an issued call with no result is a hard protocol error on
    both routes.
    """
    bodies: list[str] = []
    for index, _call in enumerate(round_.calls):
        if index < len(results):
            bodies.append(bounded_json(results[index], limit))
        else:
            bodies.append(_DROPPED_CALL_REPLY)
    return _tr.tool_result_messages(wire, round_.calls, bodies)


#: Body for a call the round cap dropped. Named so the model can act on it
#: (ask for fewer next round) rather than reading a blank.
_DROPPED_CALL_REPLY = (
    '{"error": "not executed — this round exceeded the per-round call cap; '
    'request fewer tools in one round"}'
)


def text_tool_round_fallback(text: str, *, normalize_calls: Any) -> list[dict[str, Any]]:
    """Tool calls hiding in a native route's TEXT reply, or ``[]``.

    The safety net for a handler that accepted ``tools`` and ignored them. On
    the native route a reply with no tool call is taken as the answer, so a
    model still emitting the old ``{"tool": ...}`` JSON would have that JSON
    persisted as its answer — worse than the unparseable round this route
    exists to remove. Parsing it as a tool round instead costs nothing when the
    reply really is prose, because prose does not parse.
    """
    stripped = (text or "").strip()
    if not stripped.startswith("{"):
        return []
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(parsed, dict) or parsed.get("final") is True:
        return []
    return normalize_calls(parsed)


def synthetic_round(batch: list[dict[str, Any]]) -> NativeRound:
    """A :class:`NativeRound` for calls that arrived as TEXT.

    Used only by the paranoid fallback above, so the round stays on the single
    native code path. The ids are manufactured: there is no provider call to
    correlate with, and the replayed assistant turn is a reconstruction either
    way.
    """
    calls = [
        _tr.ToolCall(id=f"fallback-{i}", name=c["tool"], args=dict(c.get("args") or {}))
        for i, c in enumerate(batch)
    ]
    return NativeRound(response=None, text="", calls=calls, batch=list(batch))


def native_round_summary(round_: NativeRound) -> dict[str, Any]:
    """Compact description of a native round, for the step trace."""
    return {
        "calls": len(round_.calls),
        "executed": len(round_.batch),
        "text_chars": len(round_.text),
    }


__all__ = [
    "DEFAULT_FINAL_FLOOR_SECONDS",
    "DEFAULT_ROUND_DEADLINE_SECONDS",
    "DEFAULT_TOTAL_BUDGET_SECONDS",
    "FINAL_RESERVE_SECONDS",
    "NativeRound",
    "default_final_floor_seconds",
    "default_round_deadline_seconds",
    "default_total_budget_seconds",
    "degraded_final_payload",
    "native_assistant_message",
    "native_round_summary",
    "native_system_suffix",
    "native_tool_result_messages",
    "native_tool_specs",
    "render_tools_for",
    "native_tools_enabled",
    "native_wire",
    "output_cap_hit",
    "output_limit",
    "parse_native_reply",
    "settle_final_reply",
    "supports_native_tools",
    "synthetic_round",
    "text_tool_round_fallback",
]
