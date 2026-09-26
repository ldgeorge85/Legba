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
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..stack.llm import tool_rounds as _tr

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

#: Hard ceiling on the WHOLE ReAct loop — with ONE deliberate exception, the
#: final-synthesis floor below, which may carry a run past this rather than
#: throw away an answer in progress.
#:
#: Sized under the detached run's invoke timeout (900s since the c8a0105c
#: train) with room for the actor's own envelope assembly, so this is what ends
#: a long run. See ``consult_runs.invoke_timeout_seconds`` for the full sum —
#: raising this number means re-checking it.
DEFAULT_TOTAL_BUDGET_SECONDS = 480.0

#: Per-LLM-call deadline. Above the slowest observed healthy Opus consult turn
#: (~101s on run 3ae77c64) with headroom, so it catches a stuck call without
#: cutting a legitimately slow one.
DEFAULT_ROUND_DEADLINE_SECONDS = 150.0

#: Reserve carved out of the total for the forced final. Once less than this
#: remains the loop stops drilling even if the drilling budget is untouched —
#: an answer synthesised from four rounds beats a timeout after six.
FINAL_RESERVE_SECONDS = 150.0

#: FLOOR under the final synthesis's slice — never a ceiling.
#:
#: This is the 2026-09-16 c8a0105c fix. The synthesis used to get
#: ``min(round_deadline, remaining)``, i.e. the same 150s slice as any drilling
#: round. But the synthesis is not a drilling round: it is the only turn that
#: produces the product, it is the longest generation of the run by
#: construction (it writes the whole answer), and it happens when the
#: transcript is at its largest. Capping it at a drilling round's deadline is
#: exactly backwards — it cut a 13,000-character answer off at 150s and
#: DISCARDED it.
#:
#: The synthesis now gets the REMAINING total budget, floored here so a loop
#: that overran still gets a real slice rather than the scraps. That the floor
#: can push the run past ``total_budget_seconds`` is deliberate and is the
#: whole point: finishing the answer 60s late beats throwing it away on time.
DEFAULT_FINAL_FLOOR_SECONDS = 240.0

#: Native multi-call batch cap per round. Below ``MAX_TOOLS_PER_BATCH`` (5)
#: because on the native route every result is replayed into every subsequent
#: prompt, so a round's width multiplies the transcript for the REST of the
#: run. Four wide reads still survey; the fifth mostly buys tokens. A
#: descriptor may raise it.
DEFAULT_NATIVE_BATCH_CAP = 4


def default_final_floor_seconds() -> float:
    """Floor under the final synthesis. Env ``LEGBA_CONSULT_FINAL_FLOOR_SECONDS``."""
    return _env_seconds(
        "LEGBA_CONSULT_FINAL_FLOOR_SECONDS", DEFAULT_FINAL_FLOOR_SECONDS
    )


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


def _env_seconds(name: str, default: float) -> float:
    """A positive float from ``name``, else ``default``.

    Unset / empty / non-numeric / non-positive all fall back: a malformed pin
    must never zero a budget, which would make every consult degrade instantly.
    """
    raw = os.getenv(name, "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    logger.debug("consult.budget.env_ignored name=%s raw=%r", name, raw)
    return default


def default_total_budget_seconds() -> float:
    """Total wall-clock budget for the loop. Env ``LEGBA_CONSULT_BUDGET_SECONDS``."""
    return _env_seconds("LEGBA_CONSULT_BUDGET_SECONDS", DEFAULT_TOTAL_BUDGET_SECONDS)


def default_round_deadline_seconds() -> float:
    """Per-LLM-call deadline. Env ``LEGBA_CONSULT_ROUND_DEADLINE_SECONDS``."""
    return _env_seconds(
        "LEGBA_CONSULT_ROUND_DEADLINE_SECONDS", DEFAULT_ROUND_DEADLINE_SECONDS
    )


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

    Every tool is offered under its OWN name with its own argument schema, so
    the provider validates the arguments before they cost a round-trip. Five of
    the nineteen have no catalogue entry yet and get the shared module's
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
    "DEFAULT_ROUND_DEADLINE_SECONDS",
    "DEFAULT_TOTAL_BUDGET_SECONDS",
    "FINAL_RESERVE_SECONDS",
    "NativeRound",
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
    "parse_native_reply",
    "supports_native_tools",
    "synthetic_round",
    "text_tool_round_fallback",
]
