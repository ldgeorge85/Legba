# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounding the prompt a multi-round tool loop carries — a sibling of
``tool_rounds``, which owns the grammar and is deliberately untouched here.

The cost shape this fixes
=========================

A ReAct loop replays its whole transcript on every round. With a per-TOOL
bound of 8 KB and five calls per batch, round *n* carries up to ``40 KB * n``
of tool bodies — and the model re-reads all of it, at input-token prices, on
every subsequent call. Run c8a0105c did ten rounds of exactly that: the late
calls each carried ~100k+ input tokens, on a route priced at $15/M input, and
the operator paid roughly $10 for an answer that was then thrown away.

Nothing in that prompt needed to be there verbatim. A tool result matters to a
later round for two things: **what it pointed at** (its refs — which are what
the answer cites, so they must survive byte-exact) and **what it broadly
said** (a count, an error, the shape of the rows). The full row payloads matter
once, to the round that asked for them.

So this module does two bounded things, and the raw payload is never lost —
it stays on the persisted step trace, which is where an operator auditing the
run actually looks:

1. :func:`allocate_round_bodies` — a PER-ROUND total bound that replaces the
   per-tool bound as the operative limit. Max-min fair: small results are never
   truncated to make room for a sibling, and the slack they leave is
   redistributed to the large ones, so a 200-byte error and a 40 KB row dump
   share 16 KB as 200 bytes + 15.8 KB rather than 8 KB + 8 KB.
2. :func:`compact_prior_tool_messages` — older rounds' tool bodies collapse to
   ``{refs, count, summary}``. Refs verbatim, everything else one line.

Both provider wire shapes are handled, because the loop runs on both: the
OpenAI-compatible ``role=tool`` message and Anthropic's ``role=user`` message
carrying ``tool_result`` blocks. Compaction rewrites only the ``content`` of a
tool result — never an id, never a role, never the block ordering — so the
call/result correlation the providers enforce is structurally preserved.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Per-ROUND total bound on tool-result bodies (bytes of rendered JSON).
#: Replaces the per-tool bound as the operative limit. 16 KB holds a real
#: five-call survey; ten rounds of it is 160 KB of transcript rather than 400.
DEFAULT_ROUND_RESULT_BOUND = 16_000

#: Floor per result inside a round, so a big sibling can never starve a small
#: one into unreadability. Five calls * 600 = 3 KB, comfortably inside the
#: round bound.
MIN_BODY_CHARS = 600

#: How many of the most recent rounds keep their FULL tool bodies. One: the
#: round just executed is the one the next call reasons over. Everything older
#: is available as refs + digest, and in full on the trace.
DEFAULT_KEEP_FULL_ROUNDS = 1

#: Cap on the human-readable half of a digest.
DIGEST_SUMMARY_CHARS = 200


def round_result_bound() -> int:
    """Per-round tool-result bound. Env ``LEGBA_CONSULT_ROUND_RESULT_BYTES``."""
    return _env_int("LEGBA_CONSULT_ROUND_RESULT_BYTES", DEFAULT_ROUND_RESULT_BOUND)


def keep_full_rounds() -> int:
    """Recent rounds exempt from compaction. Env ``LEGBA_CONSULT_KEEP_FULL_ROUNDS``."""
    return _env_int("LEGBA_CONSULT_KEEP_FULL_ROUNDS", DEFAULT_KEEP_FULL_ROUNDS)


def _env_int(name: str, default: int) -> int:
    """A non-negative int from ``name``, else ``default``.

    Malformed pins fall back rather than zeroing a bound — a zeroed bound would
    compact every round to nothing and silently destroy the loop's evidence.
    """
    raw = os.getenv(name, "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
        logger.debug("consult.compaction.env_ignored name=%s raw=%r", name, raw)
    return default


# ---------------------------------------------------------------------------
# 1. Per-round allocation
# ---------------------------------------------------------------------------


def allocate_body_limits(
    sizes: Sequence[int],
    *,
    total_bound: int,
    min_each: int = MIN_BODY_CHARS,
) -> list[int]:
    """Max-min fair per-result character limits summing to <= ``total_bound``.

    ``sizes`` are the results' natural (untruncated) rendered lengths. A result
    that already fits inside its fair share is granted its full size and the
    remainder is redistributed to those that do not — repeated until no further
    result can be satisfied. The result is that truncation falls entirely on
    the oversized payloads, which is where the tokens actually are.

    ``min_each`` is honoured even when the arithmetic would go below it; with
    enough results that can push the sum over ``total_bound``, which is correct
    — an unreadable stub for every call is worse than a slightly larger prompt,
    and the batch cap bounds how many results there can be.
    """
    n = len(sizes)
    if n == 0:
        return []
    limits = [0] * n
    unresolved = set(range(n))
    budget = float(total_bound)
    while unresolved:
        share = budget / len(unresolved)
        fitting = [i for i in unresolved if sizes[i] <= share]
        if not fitting:
            for i in unresolved:
                limits[i] = max(min_each, int(share))
            break
        for i in fitting:
            limits[i] = sizes[i]
            budget -= sizes[i]
            unresolved.discard(i)
    return limits


def allocate_round_bodies(
    results: Sequence[Any],
    *,
    render: Any,
    total_bound: int | None = None,
    min_each: int = MIN_BODY_CHARS,
) -> list[str]:
    """Render one round's tool results under a shared total bound.

    ``render(result, limit)`` is the caller's own JSON-safe bounded renderer
    (the consult kind's ``_bounded_tool_json``), so truncation stays the same
    explicit, model-detectable cut it has always been — this function decides
    only how the budget is SPLIT, never how a body is cut.
    """
    bound = round_result_bound() if total_bound is None else total_bound
    if not results:
        return []
    # Natural sizes first: rendering at a very large limit costs one pass and
    # is what makes the allocation fair rather than uniform.
    natural = [render(r, bound + 1) for r in results]
    limits = allocate_body_limits(
        [len(b) for b in natural], total_bound=bound, min_each=min_each,
    )
    out: list[str] = []
    for body, limit in zip(natural, limits):
        out.append(body if len(body) <= limit else render_at(body, limit, render))
    return out


def render_at(natural: str, limit: int, render: Any) -> str:
    """Re-render a body at ``limit``.

    Separated so the natural-size pass above reads as one thing. ``render`` is
    given the already-parsed value when the natural body is JSON, so the
    caller's truncation marker logic sees a real object rather than a string.
    """
    try:
        return render(json.loads(natural), limit)
    except (json.JSONDecodeError, ValueError):
        return natural[:limit]


# ---------------------------------------------------------------------------
# 2. Across-round compaction
# ---------------------------------------------------------------------------


@dataclass
class CompactionStats:
    """What one compaction pass did, for the step trace."""

    messages_rewritten: int = 0
    bodies_compacted: int = 0
    chars_before: int = 0
    chars_after: int = 0

    @property
    def chars_saved(self) -> int:
        return max(0, self.chars_before - self.chars_after)

    def as_step(self) -> dict[str, Any]:
        return {
            "messages_rewritten": self.messages_rewritten,
            "bodies_compacted": self.bodies_compacted,
            "chars_before": self.chars_before,
            "chars_after": self.chars_after,
            "chars_saved": self.chars_saved,
        }


def digest_tool_body(body: str, *, summary_chars: int = DIGEST_SUMMARY_CHARS) -> str:
    """Collapse one rendered tool result to refs + a one-line digest.

    The refs are carried through VERBATIM and unbounded: they are what the
    answer cites, and a citation that survives only in a truncated list is a
    citation the model will get wrong. Everything else becomes a count, an
    error, or a short shape description.

    A body that does not parse as a JSON object is kept as a truncated string
    rather than dropped — an unparseable result is still evidence that the call
    happened and returned something.

    IDEMPOTENT. The loop compacts after every round, so a body from round 2 is
    offered to this function again in rounds 3, 4, 5... Without the
    already-compacted check each pass would re-digest its own digest: the
    summary line would nest inside the next summary, drift toward the 200-char
    cut, and inflate ``bodies_compacted`` with work that changed nothing.
    """
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return json.dumps(
            {"compacted": True, "summary": body[:summary_chars]}, default=str,
        )
    if not isinstance(parsed, Mapping):
        return json.dumps(
            {"compacted": True, "summary": str(parsed)[:summary_chars]}, default=str,
        )
    if parsed.get("compacted") is True:
        return body

    out: dict[str, Any] = {"compacted": True}
    refs = parsed.get("refs")
    if isinstance(refs, list) and refs:
        out["refs"] = [str(r) for r in refs]
    if parsed.get("error") is not None:
        out["error"] = str(parsed["error"])[:summary_chars]
    rows = parsed.get("rows") or parsed.get("items")
    count = parsed.get("count")
    if count is None and isinstance(rows, list):
        count = len(rows)
    if count is not None:
        out["count"] = count
    out["summary"] = _summarize(parsed, rows, summary_chars)
    return json.dumps(out, default=str)


def _summarize(parsed: Mapping[str, Any], rows: Any, limit: int) -> str:
    """One line describing what the full payload held.

    Names the row keys rather than the row values: the keys tell the model what
    it could ask for again, the values are what it already used.
    """
    if parsed.get("error") is not None:
        return f"call failed: {str(parsed['error'])[:limit]}"
    bits: list[str] = []
    if isinstance(rows, list) and rows:
        first = rows[0]
        if isinstance(first, Mapping):
            keys = ", ".join(sorted(str(k) for k in first)[:12])
            bits.append(f"{len(rows)} row(s) with fields: {keys}")
        else:
            bits.append(f"{len(rows)} item(s)")
    scalars = [
        f"{k}={parsed[k]!r}"
        for k in sorted(parsed)
        if k not in {"refs", "rows", "items", "count", "error"}
        and isinstance(parsed[k], (str, int, float, bool))
    ]
    if scalars:
        bits.append("; ".join(scalars))
    line = " | ".join(bits) if bits else "result retained on the run trace"
    return (
        "full payload on the run trace (this round's results were compacted "
        f"to save context): {line}"
    )[:limit]


def _compact_content(content: Any, stats: CompactionStats) -> Any:
    """Compact one message's ``content``, in either provider's shape.

    Only bodies that actually SHRANK are counted. Because the loop compacts
    every round, most bodies reaching this on a later pass are already digests
    and come back unchanged; counting those would report steady compaction work
    on a transcript that had stopped changing.
    """
    if isinstance(content, str):
        digested = digest_tool_body(content)
        if digested != content:
            stats.chars_before += len(content)
            stats.chars_after += len(digested)
            stats.bodies_compacted += 1
        return digested
    if isinstance(content, list):
        out: list[Any] = []
        for block in content:
            if (
                isinstance(block, Mapping)
                and block.get("type") == "tool_result"
                and isinstance(block.get("content"), str)
            ):
                body = block["content"]
                digested = digest_tool_body(body)
                if digested != body:
                    stats.chars_before += len(body)
                    stats.chars_after += len(digested)
                    stats.bodies_compacted += 1
                out.append({**block, "content": digested})
            else:
                out.append(block)
        return out
    return content


def _is_tool_result_message(message: Mapping[str, Any]) -> bool:
    """Whether ``message`` carries tool-result bodies, on either wire."""
    if message.get("role") == "tool":
        return True
    if message.get("role") != "user":
        return False
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(b, Mapping) and b.get("type") == "tool_result" for b in content
    )


def compact_prior_tool_messages(
    messages: Sequence[Mapping[str, Any]],
    *,
    keep_rounds: int | None = None,
) -> tuple[list[Mapping[str, Any]], CompactionStats]:
    """Digest tool results older than the most recent ``keep_rounds`` rounds.

    Walks from the END so "recent" is defined by position rather than by a
    round number the messages do not carry. A contiguous run of tool-result
    messages is ONE round on the OpenAI-compatible wire (one message per call)
    and one message on Anthropic's, so the run is the unit that gets kept.

    Returns a NEW list; the input is never mutated. Messages that are not tool
    results — the user prompt, assistant turns, the seeded transcript — are
    passed through untouched, so the loop's protocol shape is unchanged.
    """
    keep = keep_full_rounds() if keep_rounds is None else keep_rounds
    stats = CompactionStats()
    out = [dict(m) for m in messages]

    # Identify contiguous runs of tool-result messages, newest first.
    runs: list[list[int]] = []
    current: list[int] = []
    for idx in range(len(out) - 1, -1, -1):
        if _is_tool_result_message(out[idx]):
            current.append(idx)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)

    for run in runs[keep:]:
        for idx in run:
            message = out[idx]
            original = message.get("content")
            compacted = _compact_content(original, stats)
            # Equality, not identity: the list branch always builds a fresh
            # list, so an identity check would report every already-digested
            # round as rewritten on every subsequent pass.
            if compacted != original:
                out[idx] = {**message, "content": compacted}
                stats.messages_rewritten += 1
    return out, stats


__all__ = [
    "DEFAULT_KEEP_FULL_ROUNDS",
    "DEFAULT_ROUND_RESULT_BOUND",
    "MIN_BODY_CHARS",
    "CompactionStats",
    "allocate_body_limits",
    "allocate_round_bodies",
    "compact_prior_tool_messages",
    "digest_tool_body",
    "keep_full_rounds",
    "round_result_bound",
]
