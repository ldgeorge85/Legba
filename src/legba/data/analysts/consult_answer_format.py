# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Consult's answer format and the likelihood words, each stated once.

Two pieces of model-facing text that used to be restated, with differences,
in several prompts:

* the LIKELIHOOD WORDS: seven bands plus "cannot say". The shared analytic
  standards (``_tradecraft``) list them for every analyst, the contract-block
  renderer (``consult_hypotheses``) turns a probability into one of them, and
  the console's badge uses the same cuts. One table here, so a word in a
  prompt and a word in a rendered answer cannot disagree;
* the ANSWER FORMAT: how a Consult reply asks for tools and how it answers.
  The system prompt ends with it, and every later message that repeats the
  format (the unreadable-reply nudges, the forced final answer) points back
  at it instead of restating a shorter version that drops a header line.

The bake-off arms keep the text they were measured with
(``consult_bakeoff_stock``); nothing here reaches them.

Pure text plus one small helper; imports only the reply parser's constants.
"""

from __future__ import annotations

from .consult_reply_parsing import FINAL_SENTINEL, _sentinel_line_index

__all__ = [
    "ANSWER_FORMAT_HEADING",
    "ANSWER_FORMAT_NATIVE",
    "ANSWER_FORMAT_TEXT",
    "CANNOT_SAY",
    "FORCED_FINAL_INSTRUCTION",
    "FORCED_FINAL_SYSTEM",
    "LIKELIHOOD_BANDS",
    "LIKELIHOOD_WORDS",
    "LIKELIHOOD_WORD_LIST",
    "NATIVE_ROUTE_SUFFIX",
    "NUMBERS_RULE",
    "compose_system_prompt",
    "unreadable_reply_nudge",
    "unreadable_reason",
]

# ---------------------------------------------------------------------------
# Likelihood words
# ---------------------------------------------------------------------------

#: The table and both renderings live in the leaf module
#: :mod:`legba.data.likelihood_words` so that prompt builders outside the
#: analyst package can read them without importing it; re-exported here.
from ..likelihood_words import (  # noqa: E402
    CANNOT_SAY,
    LIKELIHOOD_BANDS,
    LIKELIHOOD_WORD_LIST,
    LIKELIHOOD_WORDS,
)

#: Where a Consult answer may carry a likelihood as a number.
NUMBERS_RULE: str = (
    "Numbers for likelihood. Write a likelihood as a number in three places only: "
    "the uncertainty header line, the contract block, and a sentence that also names "
    "what would settle it and by when (\"<p> that <event>, settled by <observable> by "
    "<YYYY-MM-DD>\"). Everywhere else use the likelihood words listed in your standards "
    "above. A number that nothing could settle claims a precision the record does not have."
)

# ---------------------------------------------------------------------------
# Answer format
# ---------------------------------------------------------------------------

#: First words of the format block; :func:`compose_system_prompt` finds it by
#: this heading.
ANSWER_FORMAT_HEADING = "How to reply. Every reply is one of two kinds."

_TOOLS_TEXT = (
    "1. Asking for tools: one JSON object and nothing else, either "
    "{\"tool\": \"<name>\", \"args\": {...}} or {\"tools\": [ ... ]} with at most 5 calls. "
    "Batch only calls that do not need each other's results; a call that needs "
    "another's output waits for the next round."
)
_TOOLS_NATIVE = (
    "1. Asking for tools: call the tools directly through the tool channel, not by "
    "writing JSON. Batch only calls that do not need each other's results."
)

_ANSWER_BODY = f"""2. Answering: not JSON. Write, in this order:
   the line {FINAL_SENTINEL}
   header lines, one per line, leaving out any that would be empty:
     uncertainty: <0 to 1, one minus your confidence in the whole answer>
     cited_refs: <id>, <id>   (ids the tools returned, each once, at most 30)
     unanswered_aspects: <phrase>; <phrase>
     watch_candidates: <indicator naming its subject> by <YYYY-MM-DD after today>; ...   (at most 8)
   a blank line
   the answer, in markdown
   if the turn contract asks for one, a single ```contract block at the very end; it is the only code fence in the answer.
Cite inline: after each sentence that states a fact, put the id of the row it rests on in square brackets, using the full id as the tool returned it: "... was postponed [<id>]." A fact sentence without a bracketed id counts as uncited. Every id you cite inline also goes on the cited_refs line.
Start with the answer itself. Let length follow the question: a lookup gets a few sentences. Use headings only when the answer has several parts. If you cite fewer than five rows, or your rows share one originator, say near the start, in plain words, that the answer rests on little and why. If you cite nothing, set uncertainty to at least 0.85. Add a watch item only when you can name an observable and a date by which it would be seen; do not fill the line."""

#: The format on the JSON-in-text route (the system prompt's default ending).
ANSWER_FORMAT_TEXT: str = f"{ANSWER_FORMAT_HEADING}\n{_TOOLS_TEXT}\n{_ANSWER_BODY}"
#: The format on a route with a real tool-calling channel.
ANSWER_FORMAT_NATIVE: str = f"{ANSWER_FORMAT_HEADING}\n{_TOOLS_NATIVE}\n{_ANSWER_BODY}"

#: Appended on the native route when the system prompt is not the stock one
#: (a descriptor-supplied prompt that does not carry the format block).
NATIVE_ROUTE_SUFFIX: str = (
    "\n\nOn this route you call tools through the tool channel, not by writing JSON. "
    "Call several tools in one round only when none needs another's result. To answer, "
    "reply with text and call no tool, in the answer format given above, including the "
    "watch_candidates line and the contract block when one is required."
)


def compose_system_prompt(base: str, run_context: str, *, native: bool) -> str:
    """The system prompt one run sends: the standing prompt, the run context,
    and the answer format for this run's route, in that order.

    When ``base`` carries :data:`ANSWER_FORMAT_TEXT` once, the run context is
    placed before it (so the format is still near the end) and, on the native
    route, the tool line is swapped for the native one — the model then reads
    one contract, not a JSON contract followed by an override. Any other base
    keeps its text, gets the run context appended, and on the native route the
    short :data:`NATIVE_ROUTE_SUFFIX`.
    """
    if base.count(ANSWER_FORMAT_TEXT) == 1:
        head, tail = base.split(ANSWER_FORMAT_TEXT)
        fmt = ANSWER_FORMAT_NATIVE if native else ANSWER_FORMAT_TEXT
        head = head.rstrip("\n")
        context = run_context.strip("\n")
        middle = f"\n\n{context}\n\n" if context else "\n\n"
        return f"{head}{middle}{fmt}{tail}"
    return base + run_context + (NATIVE_ROUTE_SUFFIX if native else "")


# ---------------------------------------------------------------------------
# Unreadable-reply nudge and the forced final answer
# ---------------------------------------------------------------------------


def unreadable_reason(raw: str | None) -> str:
    """One line saying why a reply could not be read, for the nudge."""
    text = (raw or "").strip()
    if not text:
        return "it was empty"
    if _sentinel_line_index(text.splitlines()) is not None:
        return f"the {FINAL_SENTINEL} line had no answer under its header lines"
    if "{" in text:
        return "it held no JSON object that could be read"
    return f"it was neither one JSON object nor an answer that starts with the {FINAL_SENTINEL} line"


def unreadable_reply_nudge(reason: str) -> str:
    """The user message sent after a reply that could not be read."""
    return (
        f"Your last reply could not be read: {reason}. To call tools, reply with one JSON "
        f"object only. To answer, use the answer format above: {FINAL_SENTINEL}, the header "
        "lines, a blank line, the answer, and the contract block if one is required. If your "
        "last reply was the answer, send it again in that form, unchanged otherwise. No "
        "apology or explanation."
    )


#: Appended to the system prompt when the tool rounds or the time run out.
FORCED_FINAL_SYSTEM: str = (
    "\n\nThe tool budget is used up; no more tools will run. Write the final answer now "
    "from what the tools returned, in the answer format above (including watch_candidates "
    "and the contract block when required). Say near the start that the search was cut "
    "short, and list what you did not get to in unanswered_aspects."
)

#: The user turn that asks for the forced final answer.
FORCED_FINAL_INSTRUCTION: str = "The tool budget is used up. Write the final answer now."
