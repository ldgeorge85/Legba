# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V-N1 — the inline_target OUTPUT CONTRACT: what the model returns is a finding.

WHAT THIS CLOSES. Finding ``16f6a460-541a-4deb-b536-407433411173``
(``cross_doc_corroborator``) was persisted with the literal sentence
``We will use search_corpus.`` as its TITLE and a raw JSON blob as its BODY.
Nothing was broken at the time — the write path did exactly what it was written
to do. Traced through ``inline_target._coerce_finding``:

  1. the raw completion was ``We will use search_corpus.\\n{"title": ..., ...}``
     — a tool-plan sentence the model emitted before its JSON object;
  2. ``candidate.startswith("{")`` is FALSE, so the brace-balance scan that
     trims trailing garbage never ran (it only ever handled garbage AFTER the
     object, never BEFORE it);
  3. ``json.loads`` raised, and the parse-failed branch called
     ``_salvage_envelope_body``, which ALSO tests ``text.startswith("{")`` and
     so returned ``None``;
  4. the fallback therefore stored the WHOLE raw string as the body, and
     ``_title_from_text`` took its first non-empty line — the plan sentence —
     as the title.

Every layer that could have caught it was anchored on position 0. The model's
JSON contract was RIGHT THERE, one line down, fully parseable, carrying a real
title, body, confidence, evidence and tags — all discarded.

THE THREE RULES THIS MODULE IMPLEMENTS:

  * FIND THE CONTRACT ANYWHERE. :func:`extract_json_object` scans for the first
    BALANCED, string-aware JSON object at any offset, not just at position 0.
    A preamble before the object stops being fatal.
  * A TOOL PLAN IS NOT PROSE. :func:`strip_tool_plan_preamble` removes leading
    planning sentences ("We will use search_corpus.", "Let me search for…",
    "I'll check the corpus first.") from text that is about to become a body or
    a title. The house standard (``_tradecraft.ANALYTIC_PREAMBLE`` rule 8)
    already forbids them; this is what happens when the model emits one anyway.
  * DEGRADE, BUT NEVER FABRICATE. When something readable survives, it is kept
    (the D27 plain-markdown path is preserved byte-for-byte — a model that
    answers in markdown instead of JSON still lands a finding). When NOTHING
    readable survives, :class:`OutputContractError` is raised and the run FAILS
    LOUD rather than persisting a row whose title is the model's inner
    monologue. A garbage row is worse than no row: it is indistinguishable from
    analysis at every layer above this one, and the operator found this one by
    reading it, not by seeing it flagged.

WHY A SEPARATE MODULE. ``inline_target.py`` sits at 3,697 lines against a 3,980
ceiling (``tests/test_module_size_gate.py``), and the house rule on that ratchet
is to extract a cohesive unit rather than raise the number. Output-contract
enforcement IS a cohesive unit: it is pure, dependency-free, and testable
without a slice, an LLM or a DB. ``inline_target`` imports it one way.

SCOPE. This is the shared ``inline_target`` write path, so it covers all twelve
descriptors of that kind — the NINE bounded units (``disruption_status``
included, per DS-1) AND the three non-unit analysts
(``cross_doc_corroborator``, ``corpus_researcher``, ``country_assessor``). The
defect was found on the corroborator because its GATHER loop makes a tool-plan
sentence most likely there, but the hole was never analyst-specific.
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = [
    "OutputContractError",
    "extract_json_object",
    "is_unusable_output",
    "repair_confidence_word_token",
    "strip_tool_plan_preamble",
]


class OutputContractError(RuntimeError):
    """The model's output cannot be read as a finding at all.

    Raised ONLY when every recovery path is exhausted and nothing readable
    survives — an empty completion, or output that is pure tool-plan / JSON
    scaffolding with no content behind it. Deliberately a loud failure: the
    caller re-raises it into the runtime, which classifies it per
    ``kind_contracts`` §7 and routes the run to the DLQ, where a human sees it.

    It is NOT raised for a model that answered in prose instead of JSON. That
    is a formatting miss over real analysis, and the existing "unstructured"
    degrade path keeps it — the house posture is degrade-not-fabricate, and
    prose IS content.
    """


# ---------------------------------------------------------------------------
# Tool-plan preamble
# ---------------------------------------------------------------------------
#
# The sentence forms a reasoning model emits when it narrates its plan instead
# of executing it. Anchored at the START of the remaining text and matched one
# sentence at a time so a legitimate body is never truncated mid-argument: the
# first line that is NOT a plan sentence stops the strip.
#
# Deliberately NOT a general first-person ban. "We assess that…" is house
# estimative language (``ANALYTIC_PREAMBLE`` rule 4) and must survive; the
# openers below are all PROCESS narration — a statement about which tool the
# model is about to call, not a statement about the world.

_PLAN_OPENERS = (
    r"we\s+(?:will|need\s+to|should|must|can|are\s+going\s+to)",
    r"i\s+(?:will|need\s+to|should|must|can|am\s+going\s+to)",
    r"i'?ll",
    r"i'?m\s+going\s+to",
    r"let\s+me",
    r"let'?s",
    r"first,?\s+(?:i|we|let)",
    r"next,?\s+(?:i|we|let)",
    r"now,?\s+(?:i|we|let)",
    r"(?:ok|okay|alright|sure),?",
)

#: A plan sentence: an opener, then anything that is not a sentence break, then
#: a terminator. Bounded to 300 chars so a long analytic sentence that happens
#: to open with "We can" is never swallowed whole — a real plan sentence is
#: short ("We will use search_corpus.").
_PLAN_SENTENCE_RE = re.compile(
    r"^\s*(?:" + "|".join(_PLAN_OPENERS) + r")\b[^.\n!?]{0,300}?[.\n!?]+",
    re.IGNORECASE,
)

#: A line that is nothing but a fenced-block marker or JSON scaffolding — the
#: residue left behind once a preamble and an object have both been lifted.
_SCAFFOLD_ONLY_RE = re.compile(r"^[\s`{}\[\],:\"']*$")

#: ``json``/``JSON`` alone on a line is the fence's language tag, not content.
_FENCE_TAG_RE = re.compile(r"^\s*json\s*$", re.IGNORECASE)


def strip_tool_plan_preamble(text: str) -> tuple[str, str]:
    """Split ``text`` into ``(content, stripped_preamble)``.

    Removes leading tool-plan sentences one at a time until the head of the
    text is real content. Returns the ORIGINAL text as ``content`` (and an
    empty preamble) when nothing matches, so the overwhelmingly common
    well-formed case is byte-for-byte untouched.

    Strips COMPLETELY — text that is ENTIRELY tool plan comes back as ``""``.
    That is deliberate at both call sites, and it is why this function needs no
    mode flag: an emptied TITLE falls through to the next title source, and an
    emptied BODY is caught by :func:`is_unusable_output`, which raises. Stopping
    short of the last sentence to "preserve something" would do the opposite of
    what either slot needs — it would keep the model's inner monologue as the
    only thing a reader sees.

    The stripped text is returned rather than discarded so the caller can log
    it — a model narrating its plan into the output channel is a prompt defect
    worth counting, not just worth deleting.
    """
    if not text:
        return text, ""
    remaining = text
    consumed: list[str] = []
    while True:
        match = _PLAN_SENTENCE_RE.match(remaining)
        if match is None:
            break
        consumed.append(match.group(0).strip())
        remaining = remaining[match.end():]
    if not consumed:
        return text, ""
    return remaining.lstrip(), " ".join(consumed)


# ---------------------------------------------------------------------------
# The confidence digit-then-number-word token
# ---------------------------------------------------------------------------
#
# The core plane occasionally emits ``"confidence": 0. nine`` instead of
# ``"confidence": 0.9`` — a digit, a literal period, whitespace, then the
# fractional digit spelled out as an English word. ``0.`` alone is not a
# valid JSON number (the grammar requires a digit immediately after the
# decimal point), so ``json.loads`` fails on the token, and — before this
# fix — took the WHOLE finding down with it: the primary parse AND
# ``parse_finding_envelope``'s "find it anywhere" recovery both run
# ``json.loads`` over the same malformed number and fail the same way,
# landing the finding in the unstructured salvage path at a flat 0.30
# confidence with an EMPTY ``indicators`` array. That is exactly backwards —
# measured in ``planning/VOICE_REPLAY_2026-08-20/runs/
# REVISION_RESULT_2026-08-21.md`` §5, the token only shows up where the
# model is writing a confident positive: five cells across the 2026-08-21
# narrative replay and the frozen 2026-08-20 corpus, every one a
# coordination-positive call, every one spelling out ``nine``.
#
# Repaired TEXTUALLY, before either parse attempt, so a normal
# ``json.loads`` recovers the whole contract — confidence, evidence, tags
# and (the field this defect actually costs) ``indicators`` — rather than
# degrading through the salvage path at all.
_CONFIDENCE_DIGIT_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}

#: Anchored on the ``"confidence":`` key so a body sentence that happens to
#: spell out a number is never touched — only the confidence VALUE itself.
_CONFIDENCE_WORD_TOKEN_RE = re.compile(
    r'("confidence"\s*:\s*-?\d+)\.\s+(' + "|".join(_CONFIDENCE_DIGIT_WORDS) + r')\b',
    re.IGNORECASE,
)


def repair_confidence_word_token(text: str) -> str:
    """Rewrite a ``"confidence": 0. nine``-shaped token to ``"confidence": 0.9``.

    Bounded to the ten single-digit words a 0.0-1.0 confidence can spell out
    for its fractional digit — not a general English-number parser. A no-op
    on well-formed input: ``0.9`` has no whitespace after the dot, so the
    pattern never matches it, and the substitution is idempotent.
    """
    def _sub(match: "re.Match[str]") -> str:
        word = match.group(2).lower()
        return f"{match.group(1)}.{_CONFIDENCE_DIGIT_WORDS[word]}"

    return _CONFIDENCE_WORD_TOKEN_RE.sub(_sub, text)


# ---------------------------------------------------------------------------
# Balanced, string-aware JSON-object extraction
# ---------------------------------------------------------------------------


def extract_json_object(text: str) -> str | None:
    """The first BALANCED JSON object in ``text``, at ANY offset, or ``None``.

    Two properties the in-place scanner in ``_coerce_finding`` did not have:

      * IT DOES NOT REQUIRE OFFSET 0. The object may sit behind a tool-plan
        sentence, a stray "Here is the finding:", or a fence — the exact shape
        that made the corroborator write its plan sentence as a title.
      * IT IS STRING-AWARE. A naive depth counter closes the object on the
        first ``}`` it sees, including one inside a JSON string value — and an
        analytic body legitimately contains braces. This tracks quoting and
        backslash escapes, so the returned slice is the whole object.

    Returns the raw substring (NOT parsed) so the caller owns decoding and its
    error handling. ``None`` when there is no balanced object — an unterminated
    one is not returned, because a truncated object is what
    ``_salvage_envelope_body`` exists to handle.
    """
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for idx in range(start, len(text)):
            char = text[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start:idx + 1]
        # Unbalanced from here on; there is no later object either, because any
        # later '{' is nested inside this unterminated one.
        return None
    return None


def parse_finding_envelope(text: str) -> dict[str, Any] | None:
    """The model's finding contract as a dict, found anywhere in ``text``.

    Fence-strips, then extracts the first balanced object and decodes it.
    Returns ``None`` unless the result is a dict carrying at least one of the
    contract's own keys — ``title`` / ``body`` — so a stray JSON object the
    model quoted mid-prose (a tool call it echoed, a sample payload) is never
    mistaken for the finding itself.
    """
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
        candidate = candidate.strip()
    blob = extract_json_object(candidate)
    if blob is None:
        return None
    try:
        parsed = json.loads(blob)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    if "title" not in parsed and "body" not in parsed:
        return None
    return parsed


# ---------------------------------------------------------------------------
# The unusable-output test — the one condition that FAILS the run
# ---------------------------------------------------------------------------


def is_unusable_output(body: str) -> bool:
    """True when ``body`` carries no readable content at all.

    The narrow definition matters: this is the predicate that turns a degrade
    into a RAISE, so it must never fire on a real analytic body. It is true
    only when, line by line, everything is one of:

      * empty / whitespace;
      * bare JSON scaffolding (braces, brackets, commas, colons, quotes);
      * a code-fence marker or its ``json`` language tag.

    A body with ONE line of prose in it is usable, and is kept. Prose that
    happens to be badly formatted is still analysis; a payload of punctuation
    is not.
    """
    if not body or not body.strip():
        return True
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("```"):
            continue
        if _FENCE_TAG_RE.match(line):
            continue
        if _SCAFFOLD_ONLY_RE.match(line):
            continue
        return False
    return True
