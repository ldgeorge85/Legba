# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The synthesis prompt, kept so a cut run can be finished without re-drilling.

The problem
===========

Run c8a0105c drilled for 314 seconds across ten rounds and 50 tool calls, and
then its synthesis was cut and discarded. The evidence it had gathered was
real, it was persisted, and it had already been paid for — but there was no way
to ask a model to write the answer over it. Re-asking the question meant paying
for all fifty tool calls again.

"Synthesize from evidence" is that missing move. The requirement it has to meet
is strict, and it is the reason this module exists rather than a helper that
summarises the trace: **the recovery must send the model the same prompt the
loop itself would have sent.** Not a fresh prompt over digested evidence — the
same messages, in the same order, with the same bounded tool bodies. Anything
looser is a different question producing a different answer while wearing the
name of a recovery.

Two paths, and they are not equally good
========================================

**(a) EXACT — the recorded transcript.** From this change on, a run that
reaches its synthesis records the prompt it sent: the system string and the
full message list, byte for byte. Replay sends exactly those. Identity is not
re-derived and therefore cannot drift; it is the same object.

**(b) REBUILT — re-execute the recorded calls.** A run persisted BEFORE this
change has only a trace: ``steps`` carries tool names, trimmed args and a tiny
result digest, never the bodies the model read, and never the assistant's free
text between rounds. For those runs the transcript is reconstructed by
RE-EXECUTING the recorded tool calls in recorded order — same tools, same args,
$0, against the same corpus — and re-bounding the results with the same
function the loop used. What cannot be recovered is stated rather than papered
over:

  * the assistant's free text between rounds (never persisted) — the replayed
    assistant turns carry their tool calls and no prose;
  * the exact ARGS of a call whose arguments were long, because the trace
    stores them through ``_trim_args`` (strings cut at 200 chars, lists at 10);
  * any result that has since changed, because the corpus is live — every
    re-executed call is compared against its recorded digest and the
    differences are reported per call.

A rebuilt replay is labelled as one, everywhere it surfaces. It is a good
answer over honestly-reconstructed evidence, and calling it anything else would
be the same dishonesty the degraded-final message was written to avoid.

One definition of the prompt
============================

:func:`synthesis_system` and :func:`synthesis_instruction` live here and the
LOOP calls them too. That is what makes the identity claim testable: there is
no second copy of the wording to fall out of step with the first.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..stack.llm import tool_round_compaction as _tc
from .consult_tool_rendering import _bounded_tool_json

logger = logging.getLogger(__name__)

#: Record format version. Bumped if the shape changes so a replay can refuse a
#: record it does not understand rather than mis-sending it.
TRANSCRIPT_VERSION = 1

#: Ceiling on a stored transcript. Past this the record stores what fits and
#: marks itself truncated — a replay then reports "exact" only if untruncated.
#: Sized well above a compacted ten-round run (~60 KB) so truncation is the
#: rare, flagged case rather than the norm.
MAX_TRANSCRIPT_CHARS = 600_000


# ---------------------------------------------------------------------------
# The prompt, defined ONCE
# ---------------------------------------------------------------------------


def synthesis_system(base_system: str, final_sentinel: str) -> str:
    """The system prompt for the terminal synthesis turn.

    Called by the loop when it forces a final, and by the recovery path when it
    replays one. Two callers, one string — the byte-identity test depends on
    that being literally true rather than approximately true.
    """
    return (
        base_system
        + "\n\nYou have reached the tool-round cap. You MUST now produce "
        f"the FINAL reply — the {final_sentinel} line, its header lines, "
        "then your answer as markdown — using only what the tool calls "
        "already returned. Do not request more tools, and do not wrap the "
        "answer in JSON."
    )


def synthesis_instruction(final_sentinel: str) -> str:
    """The user turn appended to force the synthesis."""
    return (
        "Round cap reached. Emit the final answer "
        f"now, in the {final_sentinel} form."
    )


# ---------------------------------------------------------------------------
# (a) The recorded transcript
# ---------------------------------------------------------------------------


def build_transcript(
    *, system: str, messages: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Freeze the synthesis request into a persistable record.

    Stores the request as SENT. The messages are already JSON-safe (strings and
    the providers' block lists), so this is a copy and a size check, not a
    projection — a projection is exactly what would let the replay drift.
    """
    payload = [dict(m) for m in messages]
    try:
        size = len(json.dumps(payload, default=str)) + len(system)
    except (TypeError, ValueError):  # pragma: no cover — messages are JSON-safe
        logger.warning("consult.transcript.unserialisable — recording none")
        return {"version": TRANSCRIPT_VERSION, "usable": False, "reason": "unserialisable"}
    truncated = size > MAX_TRANSCRIPT_CHARS
    return {
        "version": TRANSCRIPT_VERSION,
        "usable": not truncated,
        "truncated": truncated,
        "chars": size,
        "system": system,
        "messages": [] if truncated else payload,
    }


def replay_request(record: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]] | None:
    """``(system, messages)`` from a recorded transcript, or ``None``.

    Refuses anything it does not fully understand — a wrong version, a
    truncated record, a record that failed to serialise. The caller then falls
    back to the rebuild path and SAYS so, which is strictly better than
    replaying a partial prompt under the "exact" label.
    """
    if not isinstance(record, Mapping):
        return None
    if record.get("version") != TRANSCRIPT_VERSION or not record.get("usable"):
        return None
    messages = record.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    return str(record.get("system") or ""), [dict(m) for m in messages]


# ---------------------------------------------------------------------------
# (b) The rebuilt transcript
# ---------------------------------------------------------------------------


@dataclass
class CallDrift:
    """How one re-executed call compared with what the run recorded."""

    tool: str
    round: int
    recorded_count: Any = None
    replayed_count: Any = None
    recorded_refs: int = 0
    replayed_refs: int = 0
    error: str | None = None

    @property
    def drifted(self) -> bool:
        return (
            self.error is not None
            or self.recorded_count != self.replayed_count
            or self.recorded_refs != self.replayed_refs
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "round": self.round,
            "recorded_count": self.recorded_count,
            "replayed_count": self.replayed_count,
            "recorded_refs": self.recorded_refs,
            "replayed_refs": self.replayed_refs,
            "error": self.error,
            "drifted": self.drifted,
        }


@dataclass
class RebuiltTranscript:
    """A transcript reconstructed from a trace, with its own honesty attached."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    calls_replayed: int = 0
    drift: list[CallDrift] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def drifted_calls(self) -> int:
        return sum(1 for d in self.drift if d.drifted)

    def note(self) -> str:
        """The sentence shown to the operator. Never reassuring by omission."""
        bits = [
            f"transcript rebuilt by re-executing {self.calls_replayed} tool "
            f"call(s) against the live corpus"
        ]
        if self.drifted_calls:
            bits.append(
                f"{self.drifted_calls} returned different results than the "
                f"original run recorded"
            )
        if self.missing:
            bits.append("; ".join(self.missing))
        return "; ".join(bits)


#: What a rebuild structurally cannot recover, stated once.
_UNRECOVERABLE = (
    "assistant free text between rounds not recoverable (never persisted)",
    "tool arguments replay from the trace's trimmed copy (strings cut at 200 "
    "chars, lists at 10 entries)",
)


def _rounds_from_steps(
    steps: Sequence[Mapping[str, Any]],
) -> list[list[Mapping[str, Any]]]:
    """Group persisted ``tool_call`` steps into their rounds, in order."""
    by_round: dict[int, list[Mapping[str, Any]]] = {}
    for step in steps:
        if step.get("kind") != "tool_call":
            continue
        try:
            rnd = int(step.get("round") or 0)
        except (TypeError, ValueError):
            rnd = 0
        by_round.setdefault(rnd, []).append(step)
    return [by_round[r] for r in sorted(by_round)]


def _recorded_digest(step: Mapping[str, Any]) -> tuple[Any, int]:
    """``(count, refs)`` as the original run recorded them, for drift."""
    result = step.get("result")
    if isinstance(result, Mapping):
        try:
            refs = int(result.get("refs") or 0)
        except (TypeError, ValueError):
            refs = 0
        return result.get("count"), refs
    return None, 0


async def rebuild_transcript(
    steps: Sequence[Mapping[str, Any]],
    *,
    run_tool: Any,
    user_prompt: str,
    wire: str,
    seed_messages: Sequence[Mapping[str, Any]] = (),
) -> RebuiltTranscript:
    """Reconstruct a synthesis transcript by re-executing the recorded calls.

    ``run_tool(name, args)`` must be the SAME dispatch the loop used, so the
    results come back in the same shapes and through the same governance. The
    bodies are re-bounded with the same per-round allocation the loop applies,
    which is what makes a rebuilt prompt the right SIZE as well as the right
    content.

    The assistant turns are reconstructed from the recorded call names and args
    with manufactured ids. Ids never have to match the original — they only
    have to correlate a call with its result inside this one request, which
    they do — but the free text those turns originally carried is gone, and
    that is recorded in ``missing`` rather than silently replaced with "".
    """
    from . import consult_round_protocol as _cp

    out = RebuiltTranscript(missing=list(_UNRECOVERABLE))
    messages: list[dict[str, Any]] = [dict(m) for m in seed_messages]
    messages.append({"role": "user", "content": user_prompt})

    for round_index, round_steps in enumerate(_rounds_from_steps(steps)):
        batch = [
            {
                "tool": str(s.get("tool") or ""),
                "args": dict(s.get("args") or {}),
            }
            for s in round_steps
            if s.get("tool")
        ]
        if not batch:
            continue
        results: list[Any] = []
        for call, step in zip(batch, round_steps):
            try:
                result = await run_tool(call["tool"], call["args"])
            except Exception as exc:  # noqa: BLE001 — a dead tool must not kill recovery
                result = {"error": f"replay_failed: {exc}"}
            results.append(result)
            recorded_count, recorded_refs = _recorded_digest(step)
            replayed_refs = 0
            replayed_count: Any = None
            if isinstance(result, Mapping):
                refs = result.get("refs")
                replayed_refs = len(refs) if isinstance(refs, list) else 0
                replayed_count = result.get("count")
                if replayed_count is None:
                    rows = result.get("rows") or result.get("items") or []
                    replayed_count = len(rows) if isinstance(rows, list) else None
            out.drift.append(
                CallDrift(
                    tool=call["tool"],
                    round=round_index + 1,
                    recorded_count=recorded_count,
                    replayed_count=replayed_count,
                    recorded_refs=recorded_refs,
                    replayed_refs=replayed_refs,
                    error=(
                        str(result.get("error"))
                        if isinstance(result, Mapping) and result.get("error")
                        else None
                    ),
                )
            )
            out.calls_replayed += 1

        bodies = _tc.allocate_round_bodies(results, render=_bounded_tool_json)
        native_round = _cp.synthetic_round(batch)
        messages.append(_cp.native_assistant_message(native_round, wire=wire))
        messages.extend(
            _cp.native_tool_result_messages(
                native_round,
                bodies,
                wire=wire,
                bounded_json=lambda body, _limit: body,
            )
        )
        messages, _ = _tc.compact_prior_tool_messages(messages)

    out.messages = messages
    return out


__all__ = [
    "MAX_TRANSCRIPT_CHARS",
    "TRANSCRIPT_VERSION",
    "CallDrift",
    "RebuiltTranscript",
    "build_transcript",
    "rebuild_transcript",
    "replay_request",
    "synthesis_instruction",
    "synthesis_system",
]
