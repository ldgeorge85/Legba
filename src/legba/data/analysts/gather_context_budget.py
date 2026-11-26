# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A bound on what a multi-round GATHER run carries into its calls.

What happened
=============

On 2026-10-16 the ``corpus_researcher`` sent its rounds with about 124,000
characters of evidence in the prompt while the core plane's input budget
(``LEGBA_LLM_INPUT_TOKEN_BUDGET``) was 32,000 tokens; the completion came back
empty, or as a tool call written as text, and the run produced nothing.

Why the budget did not catch it
===============================

The budget was enforced in exactly one place, and on one part of the prompt:

* ``inline_target._orient`` packs the SLICE until its chars/4 estimate reaches
  the WHOLE budget. The slice alone was allowed to fill 100% of the window —
  31,000 estimated tokens is ~124,000 characters, the figure seen live — and
  nothing reserved room for what rides on top of it: the system prompt (the
  researcher's, with the grounding clause and the gather suffix, is ~28,000
  characters), the grounding preamble, and the GATHER transcript.
* The GATHER loop re-sends the slice as ``messages[0]`` on every round and
  appends every round's assistant turn and tool results to it. Each result is
  bounded on its own (``_bounded_tool_json``, 4,000 characters) and the
  numbered set in the synthesis preamble is bounded (``_GATHER_MAX_CITED_
  SIGNALS``), but the ACCUMULATED array was never measured against anything.
  The bound was per message, never on the whole.
* The estimate is chars/4. On JSON bodies, URLs, numbers and non-Latin text the
  tokenizer yields a token every 2.5–3.5 characters, so even a prompt the
  estimator called "at the budget" was over it in real tokens.

The consult kind's ``context_ceiling_tokens`` (``consult_context_ceiling``)
bounds that kind's calls only; it was never applied to this path.

The bound
=========

One declared option, ``evidence_char_cap`` (``ANALYST_KIND_OPTIONS
["inline_target"]``), in characters, on the message array a GATHER call sends.

Derived default — the arithmetic::

    whole prompt target  = 0.6 × B tokens            (PROMPT_BUDGET_FRACTION)
                         = 0.6 × B × 4 characters    (CHARS_PER_TOKEN)
    evidence_char_cap    = that − len(gather system prompt), floor 16,000

    B = 126,976 (a 262,144 window, 131,072 output reserved):
                        0.6 × 126,976 × 4 = 304,742 − 28,082 ≈ 276,700 chars
    B = 65,536:         0.6 × 65,536 × 4 = 157,286 − 28,082 ≈ 129,200 chars
    B = 32,000 (10-16): 0.6 × 32,000 × 4 =  76,800 − 28,082 ≈  48,700 chars

B is ``_llm_budget.input_token_budget(llm)``: the explicit override when set,
else derived from the route's context window, else the default.

The 40% left over is headroom for the estimator's error (at three characters a
token a chars/4 count of 60% is 80% real) and for the tool schemas the native
protocol sends beside the prompt. The slice is packed to ``SLICE_SHARE`` (60%)
of the cap — ~77,500 characters live, ~29,000 at the 10-16 budget, where the
run that failed had packed ~124,000 — so the transcript starts with ~40% of the
cap (~51,700 characters, a dozen full tool results) before compaction is
needed at all.

The derived default engages only on a multi-round GATHER run (``max_rounds``
>= 2 after the clamp) — the accumulation needs more than one round; a single-
round or tool-less unit is untouched. An explicit value engages it on any
GATHER run; ``0`` turns it off.

Compaction — why truncation and not a summary
=============================================

When the array is over the cap, older tool RESULTS are replaced by a digest:
oldest first, never the newest round (the results the next call reasons over),
never the slice, never an assistant turn, never an id or role, so the
provider's call/result correlation survives. The digest keeps every ``[N]``
number the result produced with its document id and title.

That is enough because the citation machinery never reads the transcript:
``_fold`` assigns a gathered document its ``[N]`` from the STRUCTURED tool
result at fold time and stores it in the citation extension and the numbered
preamble block, which is what the synthesis call is shown. Replacing a body in
the transcript afterwards cannot renumber, orphan or drop a citation. A
summarising model call could — it is a second chance to lose an id or a number
— so it is not used.

The pre-flight
==============

Before every round the whole input (system prompt + message array + native
tool schemas) is estimated with the house estimator. Over the budget, the round
is not sent: the loop stops, the gathered preamble says so, and the synthesis
writes with what it has. A send that comes back empty is the failure this
replaces.

The receipt
===========

A GATHER round whose completion carries no text and no tool call is a named
step (``empty_completion``), never a clean ``done`` — under the native protocol
it used to read exactly like one. When anything here fires, one
``context_budget`` step carries the counts. A run where nothing fires has a
trace byte-identical to a build without this module.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ._llm_budget import CHARS_PER_TOKEN, estimate_tokens, input_token_budget
from .consult_context_ceiling import _dumped_len, _last_round, _result_slots
from ..option_catalog.handler_options_gather import EVIDENCE_CHAR_CAP_OPTION

#: Share of the input budget the WHOLE prompt is sized to.
PROMPT_BUDGET_FRACTION = 0.6

#: Share of the evidence cap the slice is packed to; the rest is the transcript's.
SLICE_SHARE = 0.6

#: Floor on a derived cap, so a small budget or a huge prompt never derives <= 0.
MIN_EVIDENCE_CHARS = 16_000

#: Trace step kinds.
STEP_KIND = "context_budget"
EMPTY_COMPLETION_KIND = "empty_completion"
PREFLIGHT_REFUSED_KIND = "preflight_refused"
SYNTHESIS_OVER_BUDGET_KIND = "synthesis_over_budget"

#: Appended to the gathered preamble when the pre-flight stopped the loop.
STOPPED_EARLY_NOTE = (
    "INVESTIGATION STOPPED EARLY: the next research round would have exceeded "
    "the input budget, so no further tools were called. Write the finding now "
    "with the evidence above; say plainly in the body what you could not check."
)

#: Head of a non-signal result kept in its digest.
_DIGEST_HEAD_CHARS = 300


def derive_evidence_char_cap(system_chars: int, budget_tokens: int) -> int:
    """``0.6 × budget × 4 − system_chars``, floored at :data:`MIN_EVIDENCE_CHARS`."""
    whole = int(PROMPT_BUDGET_FRACTION * budget_tokens * CHARS_PER_TOKEN)
    return max(MIN_EVIDENCE_CHARS, whole - max(0, system_chars))


@dataclass
class GatherContextBudget:
    """One run's cap, its budget, and what the bound did (the receipt)."""

    evidence_char_cap: int
    input_token_budget: int
    derived: bool
    compactions: int = 0
    results_compacted: int = 0
    chars_saved: int = 0
    preflight_refused: bool = False
    peak_input_tokens_est: int = 0
    empty_completions: int = 0

    @property
    def slice_budget_tokens(self) -> int:
        """The token budget the slice pack (``_orient``) is given."""
        return max(1, int(self.evidence_char_cap * SLICE_SHARE) // CHARS_PER_TOKEN)

    @property
    def fired(self) -> bool:
        return bool(self.compactions or self.preflight_refused or self.empty_completions)

    def receipt(self) -> dict[str, Any]:
        return {
            "phase": "gather",
            "kind": STEP_KIND,
            "evidence_char_cap": self.evidence_char_cap,
            "cap_derived": self.derived,
            "input_token_budget": self.input_token_budget,
            "compactions": self.compactions,
            "results_compacted": self.results_compacted,
            "chars_saved": self.chars_saved,
            "preflight_refused": self.preflight_refused,
            "peak_input_tokens_est": self.peak_input_tokens_est,
            "empty_completions": self.empty_completions,
        }


def resolve_gather_context_budget(
    options: Mapping[str, Any] | None,
    *,
    gather_system: str,
    max_rounds: int,
    engaged: bool,
    llm: Any = None,
) -> GatherContextBudget | None:
    """The run's bound, or ``None`` when it does not apply.

    ``None`` when no GATHER binding is engaged, when the option is ``0``, or
    when it is absent on a single-round run. A malformed per-run value (the
    descriptor's is catalog-validated) falls back to the derived default.
    """
    if not engaged:
        return None
    # ``llm`` is the run's handler: its context window sizes the budget.
    budget = input_token_budget(llm)
    raw = (options or {}).get(EVIDENCE_CHAR_CAP_OPTION)
    explicit: int | None = None
    if raw is not None and not isinstance(raw, bool):
        try:
            explicit = int(raw)
        except (TypeError, ValueError):
            explicit = None
        if explicit is not None and explicit < 0:
            explicit = None
    if explicit == 0:
        return None
    if explicit is not None:
        return GatherContextBudget(explicit, budget, derived=False)
    if max_rounds < 2:
        return None
    return GatherContextBudget(
        derive_evidence_char_cap(len(gather_system or ""), budget), budget,
        derived=True,
    )


def evidence_chars(messages: Sequence[Mapping[str, Any]]) -> int:
    """The message array's size as it goes on the wire (its JSON)."""
    return len(json.dumps(list(messages), default=str, ensure_ascii=False))


def input_tokens_est(
    system: str, messages: Sequence[Mapping[str, Any]], tools: Any = None,
) -> int:
    """House estimate of one call's input: system + messages (+ tool schemas)."""
    total = estimate_tokens(system or "") + estimate_tokens(
        json.dumps(list(messages), default=str, ensure_ascii=False)
    )
    if tools:
        total += estimate_tokens(json.dumps(list(tools), default=str))
    return total


def result_digest(
    tool_name: str, numbered: Sequence[tuple[int, str, str]], body: str,
) -> str:
    """What an older tool result is replaced by: its ``[N]`` numbers, ids, titles.

    A result that produced no numbered document keeps a bounded head instead.
    """
    out: dict[str, Any] = {
        "compacted": True,
        "tool": tool_name,
        "original_chars": len(body),
    }
    if numbered:
        out["numbered_documents"] = [
            {"n": n, "id": doc_id, "title": (title or "")[:160]}
            for n, doc_id, title in numbered
        ]
        out["note"] = (
            "older result compacted by the evidence cap; each numbered document "
            "stays citable by its [N] in the SUBSTRATE INVESTIGATION block"
        )
    else:
        out["head"] = body[:_DIGEST_HEAD_CHARS]
        out["note"] = "older result compacted by the evidence cap"
    return json.dumps(out, ensure_ascii=False)


def compact_transcript(
    messages: Sequence[Mapping[str, Any]],
    digests: Sequence[str],
    ctx: GatherContextBudget,
) -> Sequence[Mapping[str, Any]]:
    """Replace older tool results by their digests until the array fits the cap.

    ``digests[k]`` is the digest of the k-th tool result in the array, in the
    order results were appended. Under the cap the array comes back as the SAME
    object. Updates ``ctx``'s counters.
    """
    total = evidence_chars(messages)
    if total <= ctx.evidence_char_cap:
        return messages
    exempt = _last_round(messages)
    replaced: dict[tuple[int, int | None], str] = {}
    for k, (i, j, body) in enumerate(_result_slots(messages)):
        if total <= ctx.evidence_char_cap or k >= len(digests):
            break
        if i in exempt or len(digests[k]) >= len(body):
            continue  # the newest round, or already a digest
        total -= _dumped_len(body) - _dumped_len(digests[k])
        replaced[(i, j)] = digests[k]
    if not replaced:
        return messages
    out: list[Mapping[str, Any]] = list(messages)
    for (i, j), digest in replaced.items():
        message = out[i]
        if j is None:
            out[i] = {**message, "content": digest}
        else:
            blocks = list(message["content"])
            blocks[j] = {**blocks[j], "content": digest}
            out[i] = {**message, "content": blocks}
    saved = evidence_chars(messages) - evidence_chars(out)
    ctx.compactions += 1
    ctx.results_compacted += len(replaced)
    ctx.chars_saved += saved
    return out


def preflight_step(
    ctx: GatherContextBudget,
    *,
    system: str,
    messages: Sequence[Mapping[str, Any]],
    tools: Any,
    round_no: int,
) -> dict[str, Any] | None:
    """``None`` to send the round; the refusal step when it is over the budget."""
    est = input_tokens_est(system, messages, tools)
    ctx.peak_input_tokens_est = max(ctx.peak_input_tokens_est, est)
    if est <= ctx.input_token_budget:
        return None
    ctx.preflight_refused = True
    return {
        "phase": "gather",
        "kind": PREFLIGHT_REFUSED_KIND,
        "round": round_no,
        "input_tokens_est": est,
        "input_token_budget": ctx.input_token_budget,
    }


def is_empty_completion(content: str | None, calls: Sequence[Any]) -> bool:
    """No tool call and no text: what an overflowed core-plane call returns."""
    return not calls and not (content or "").strip()


def gather_receipt(
    ctx: GatherContextBudget | None, empty_completions: int,
) -> dict[str, Any] | None:
    """The ``context_budget`` step, or ``None`` when nothing fired.

    A run without a cap (the journal, the inquiry kind, a single-round unit)
    still gets the count when a round came back empty, so the failure is
    countable on every GATHER path, not only the capped one.
    """
    if ctx is None:
        if not empty_completions:
            return None
        return {"phase": "gather", "kind": STEP_KIND,
                "empty_completions": empty_completions}
    ctx.empty_completions = empty_completions
    return ctx.receipt() if ctx.fired else None


def synthesis_over_budget_step(
    ctx: GatherContextBudget | None, *, system: str, user_prompt: str,
) -> dict[str, Any] | None:
    """The named step when the synthesis input is over the budget, else ``None``.

    The synthesis is still sent — it is the run's last call, and the
    final-answer receipt names an empty reply — but the trace says it went
    over, and by how much.
    """
    if ctx is None:
        return None
    est = estimate_tokens(system or "") + estimate_tokens(user_prompt or "")
    if est <= ctx.input_token_budget:
        return None
    return {
        "phase": "reason",
        "kind": SYNTHESIS_OVER_BUDGET_KIND,
        "input_tokens_est": est,
        "input_token_budget": ctx.input_token_budget,
    }


__all__ = [
    "EMPTY_COMPLETION_KIND",
    "GatherContextBudget",
    "MIN_EVIDENCE_CHARS",
    "PREFLIGHT_REFUSED_KIND",
    "PROMPT_BUDGET_FRACTION",
    "SLICE_SHARE",
    "STEP_KIND",
    "STOPPED_EARLY_NOTE",
    "SYNTHESIS_OVER_BUDGET_KIND",
    "compact_transcript",
    "derive_evidence_char_cap",
    "evidence_chars",
    "gather_receipt",
    "input_tokens_est",
    "is_empty_completion",
    "preflight_step",
    "resolve_gather_context_budget",
    "result_digest",
    "synthesis_over_budget_step",
]
