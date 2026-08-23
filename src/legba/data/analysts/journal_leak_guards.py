# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Task #236 — the NARRATE tool-call leak guard's PURE predicates.

Extracted verbatim from ``journal_assessor.py`` when the 2026-08-10 JSON-lines
branch pushed that module past its size ceiling; behavior is byte-identical and
``journal_assessor`` re-exports every name, so importers and tests keep their
spelling. The async retry wrapper stays behind — it needs the narrate deps.
"""

from __future__ import annotations

import json
from typing import Any

class NarrateToolCallLeakError(RuntimeError):
    """The NARRATE completion returned raw tool-call JSON as the entry body
    (task #236) even after one retry with a hard prose-only instruction.

    Caught live 2026-07-24: the core-plane model (gpt-oss-120b) sometimes
    emits its LAST turn as bare tool-call JSON — e.g.
    ``{"tool": "get_assessments", "args": {}}`` (39 chars) — instead of prose,
    and (pre-fix) that JSON became BOTH the journal entry's body AND its
    title (``_derive_title`` just takes the first line). Raising here (rather
    than writing the junk) makes the run fail ``hard_fail`` so the cadence
    self-retries next tick — a failed run is recoverable; a junk journal
    entry poisons the panel + the verify ledger and is NOT easily undone.
    """


# The bare tool-call envelope's allowed key set (task #236 predicate).
# The live junk shape (``{"tool": "get_assessments", "args": {}}``) plus the
# sibling shapes different providers use for the same intent (``name`` /
# ``function`` / ``parameters`` / an OpenAI-style ``tool_calls`` envelope).
_TOOL_CALL_LEAK_KEYS = frozenset(
    {"tool", "name", "args", "arguments", "function", "parameters", "tool_calls",
     # 2026-08-10 08:30Z: the leaked transcript interleaves the calls with the
     # apparatus's OWN error echo ({"error": "Invalid arguments for tool …"}).
     # An "entry" that is one bare error object is exhaust, not prose.
     "error"}
)
# Below this many characters, a successfully-whole-string-JSON-parsed
# "entry" reads as apparatus exhaust, not prose, EVEN if its keys fall
# outside ``_TOOL_CALL_LEAK_KEYS`` (a malformed/truncated tool call, or a
# provider-specific shape this allowlist doesn't yet name). The live junk was
# 39 chars; 120 gives headroom above any plausible one-line JSON envelope
# while staying well under even the shortest legitimate entry sentence.
_TOOL_CALL_LEAK_MIN_PROSE_CHARS = 120

_NARRATE_RETRY_PROSE_ONLY_INSTRUCTION = (
    "\n\nYour last turn was tool-call JSON, not your entry. Tools are no "
    "longer available this round. Respond with prose only — no JSON, no "
    "tool syntax — write the entry itself as plain markdown."
)


def _strip_code_fence(text: str) -> str:
    """Strip a surrounding ``` / ```json fence, mirroring _extract_json's
    fence-stripping so the whole-output leak check tolerates the same
    fenced-JSON shape that helper already tolerates mid-conversation."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
        candidate = candidate.strip()
    return candidate


def _is_tool_call_leak(content: str) -> bool:
    """Task #236 guard predicate: does the NARRATE completion's WHOLE trimmed
    output read as tool-call JSON exhaust rather than a written entry?

    Deliberately WHOLE-STRING, unlike ``_extract_json`` (which hunts for a
    JSON object anywhere in the text, tolerating leading prose by design —
    exactly the wrong behavior here). Legitimate prose that happens to
    mention or quote a JSON snippet mid-paragraph must NEVER trip this: a
    ``json.loads`` over the fully trimmed/fence-stripped string fails
    (raises) the instant there is a single stray character of prose before
    or after the JSON span, so this only fires when the ENTIRE output IS
    that JSON — never a substring match.

    True when the whole string parses as JSON AND either:
      (a) it is a non-empty object whose keys are ALL in
          ``_TOOL_CALL_LEAK_KEYS`` (the live shape, plus sibling envelopes
          other providers use for the same intent), or a non-empty array
          where EVERY element is such an object; OR
      (b) it is short — under ``_TOOL_CALL_LEAK_MIN_PROSE_CHARS`` — which
          catches a malformed/truncated tool call or the empty/degenerate
          ``{}``/``[]`` case that (a)'s key-subset check would otherwise miss.
    """
    candidate = _strip_code_fence(content)
    if not candidate:
        return False

    def _is_tool_call_object(obj: Any) -> bool:
        return (
            isinstance(obj, dict)
            and bool(obj)
            and set(obj.keys()) <= _TOOL_CALL_LEAK_KEYS
        )

    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        # Not whole-string JSON. One more shape before calling it prose —
        # JSON LINES (the 2026-08-10 08:30Z leak): one tool-call object per
        # line, which whole-string json.loads rejects as "extra data" and the
        # original predicate therefore published verbatim. Fires only when
        # EVERY non-empty line parses as a leak-shaped object — a single line
        # of prose anywhere keeps the never-fire-on-analysis property.
        lines = [ln.strip() for ln in candidate.splitlines() if ln.strip()]
        if len(lines) < 2:
            return False
        for ln in lines:
            try:
                obj = json.loads(ln)
            except (json.JSONDecodeError, ValueError):
                return False
            if not _is_tool_call_object(obj):
                return False
        return True

    shaped = _is_tool_call_object(parsed) or (
        isinstance(parsed, list) and bool(parsed)
        and all(_is_tool_call_object(item) for item in parsed)
    )
    return shaped or len(candidate) < _TOOL_CALL_LEAK_MIN_PROSE_CHARS
