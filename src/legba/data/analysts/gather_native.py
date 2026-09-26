# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native tool rounds for the GATHER loop — the decision, the call, the batch.

This module holds the three things the GATHER loop needs in order to speak the
providers' structured tool channel instead of asking for JSON in prose, and
NOTHING else. The loop itself — routing a call to its owning pack's binding,
numbering citable results, folding lineage — stays in ``inline_target``, where
it has always been.

  * :func:`native_round_plan` — MAY this run go native, and with which tools?
  * :func:`complete_with_tools` — one round's LLM call, with the tools attached.
  * :func:`execute_batch` — run the N calls of one assistant turn under the
    concurrency policy their write-ness demands.

THE FLAG. ``LEGBA_AGENCY_NATIVE_TOOLS`` is OFF in the tree and flipped by the
orchestrator at deploy. It is read at CALL time, never cached at import, so a
runtime env change takes effect on the next run — the house convention (see
``deterministic_handlers/graph_mining.py``'s flag note). With it off,
:func:`native_round_plan` returns ``None`` before touching anything, and the
loop is byte-for-byte what it was.

WHAT ELSE HAS TO BE TRUE. The flag alone is not enough — three more conditions,
each of which can independently send the run back to the text protocol:

  1. The bound handler's provider must have a PROVEN native tool surface
     (``tool_rounds.supports_native_tools``). An unknown subprovider is not
     assumed to work; it falls back.
  2. The run must have tools to offer. A composition or assessment run binds
     NO action pack, so its tool set is empty and there is nothing to send —
     which is exactly why the five R4-frozen units' request payload is
     unchanged whether the flag is on or off. That is not an accident of this
     module; it is the first thing it checks.
  3. The deny list (``LEGBA_NATIVE_TOOLS_DENY``) must not name the provider.

CONCURRENCY IS DECIDED BY WRITE-NESS, NOT BY SPEED. A turn that asks for three
corpus searches gets three corpus searches at once; a turn that asks for two
``propose_fact`` writes gets them one after the other, in the order the model
emitted them. The reason is not caution in general — it is that the write tools
share one per-run ``WritebackContext`` and land rows whose lineage is read back
by later rounds, so an interleaved pair produces a transcript that does not
match the substrate. Reads have no such coupling. A mixed batch keeps relative
order: consecutive reads gather together, and each write flushes the group and
runs alone. An unknown tool name counts as a write (``is_write_tool``) — slower
and always correct beats faster and wrong.

RESULTS COME BACK IN EMITTED ORDER regardless of how they were run, because the
caller folds them into the citation numbering, and a citation number that
depended on which read finished first would not be reproducible.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ..stack.llm.tool_rounds import (
    ToolCall,
    ToolSpec,
    build_tool_specs,
    is_write_tool,
    provider_for_subprovider,
    render_tools,
    supports_native_tools,
)

logger = logging.getLogger(__name__)

__all__ = [
    "NATIVE_TOOLS_ENV",
    "NativeRounds",
    "complete_with_tools",
    "execute_batch",
    "native_round_plan",
    "native_tools_enabled",
    "usage_dict",
]

#: Operator flag. Default OFF in the tree; the orchestrator flips it at deploy.
NATIVE_TOOLS_ENV = "LEGBA_AGENCY_NATIVE_TOOLS"

_TRUTHY = ("1", "true", "yes", "on")


def native_tools_enabled() -> bool:
    """Is the native GATHER protocol switched on for this process?

    Read at call time so a runtime env change lands on the next run. Only the
    values in :data:`_TRUTHY` turn it on, so an ambiguous value stays OFF —
    the flag's whole purpose is a blast radius that is shut by default.
    """
    return os.getenv(NATIVE_TOOLS_ENV, "").strip().lower() in _TRUTHY


@dataclass(frozen=True)
class NativeRounds:
    """The decision, made once per run, carried through every round."""

    provider: str
    specs: tuple[ToolSpec, ...]
    wire_tools: tuple[Mapping[str, Any], ...]

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.specs)


def native_round_plan(
    llm: Any,
    *,
    routable: Sequence[str],
    component: str | None = None,
) -> NativeRounds | None:
    """The native plan for this run, or ``None`` to keep the text protocol.

    ``routable`` is every tool name the loop can actually dispatch this run —
    the read surface plus whichever web/write tools have a wired per-tool
    binding. Offering the model anything else would earn a ``tool_unbound``
    no-op, which is a wasted round; offering it less would hide a granted tool.
    """
    if not native_tools_enabled():
        return None
    names = [n for n in dict.fromkeys(str(n) for n in routable) if n]
    if not names:
        # The tool-less path (composition / assessment runs). Nothing to offer,
        # so nothing changes — the request payload is identical to flag-off.
        return None
    provider = provider_for_subprovider(llm)
    if provider is None or not supports_native_tools(llm, component):
        logger.info(
            "gather.native.unsupported subprovider=%s component=%s — text "
            "protocol retained",
            getattr(llm, "subprovider", None), component,
        )
        return None
    specs = build_tool_specs(names)
    if not specs:  # pragma: no cover — names is non-empty above
        return None
    return NativeRounds(
        provider=provider,
        specs=tuple(specs),
        wire_tools=tuple(render_tools(provider, specs)),
    )


def usage_dict(response: Any) -> dict[str, int]:
    """The flat token accounting the budget enforcer expects.

    Same extraction ``_reason_via_llm`` performs, on the same attribute names,
    so a native round bills exactly like a text round.
    """
    usage_raw = getattr(response, "usage", None)
    return {
        "prompt_tokens": getattr(usage_raw, "prompt_tokens", 0) if usage_raw else 0,
        "completion_tokens": (
            getattr(usage_raw, "completion_tokens", 0) if usage_raw else 0
        ),
        "reasoning_tokens": (
            getattr(usage_raw, "reasoning_tokens", 0) if usage_raw else 0
        ),
    }


async def complete_with_tools(
    llm: Any,
    *,
    messages: list[Mapping[str, Any]],
    system_prompt: str,
    max_tokens: int,
    temperature: float,
    plan: NativeRounds,
) -> Any:
    """One native round's completion. Returns the provider response object.

    ``tool_choice`` is deliberately NOT sent: ``auto`` is the endpoint default
    on both routes, and it is the only mode under which "no tool call" can mean
    "I am done gathering" — which is the loop's terminal condition. Forcing a
    call would make the loop unable to stop.
    """
    return await llm.chat_complete(
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_prompt,
        tools=list(plan.wire_tools),
    )


async def execute_batch(
    calls: Sequence[ToolCall],
    run_one: Callable[[ToolCall], Awaitable[Any]],
) -> list[Any]:
    """Run one assistant turn's calls; return results in EMITTED order.

    Consecutive read-only calls are gathered concurrently; a write call flushes
    the pending group and runs alone. See the module note for why write-ness,
    not speed, decides.
    """
    results: list[Any] = []
    pending: list[ToolCall] = []

    async def _flush() -> None:
        if not pending:
            return
        if len(pending) == 1:
            results.append(await run_one(pending[0]))
        else:
            results.extend(await asyncio.gather(*(run_one(c) for c in pending)))
        pending.clear()

    for call in calls:
        if is_write_tool(call.name):
            await _flush()
            results.append(await run_one(call))
        else:
            pending.append(call)
    await _flush()
    return results
