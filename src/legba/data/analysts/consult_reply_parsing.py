# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Consult reply parsing — the text round protocol's reader (extracted D-7).

Everything here answers one question: *what did the planner just say?* — for
the JSON-in-text round protocol, where the answer has to be recovered from
prose because there is no structured channel to read it from.

Why it is its own module
========================

It was ~345 lines inside ``consult_on_demand``, and it is a genuinely separate
subsystem: it touches no dependency, no substrate, no LLM handler and no loop
state. It parses strings. The D-7 work (the detached run's budget guards and
the native tool-call route) pushed ``consult_on_demand`` past its size ceiling,
and this was the cleanest seam — the ceiling stays where it is and the parser
gets somewhere to be read on its own terms.

``consult_on_demand`` re-exports every name below, so importers are unchanged.

What is in here
===============

* :func:`_extract_json` — the legacy strict-JSON envelope reader, still the
  tool-round shape and still the fallback final shape.
* The §28.4 plain-markdown FINAL contract: :data:`FINAL_SENTINEL`,
  :func:`_parse_sentinel_final` and friends. Its docstring carries the full
  account of why the answer is NOT JSON — read it before changing the shape.
* :func:`_parse_round_reply` — the one entry point the loop calls, which tries
  the sentinel form and falls back to the JSON envelope.
* :func:`_unwrap_double_envelope` — salvage for a model that JSON-wrapped its
  answer twice.

Note the arc this module sits inside: the sentinel contract removed the
unparseable class for *answers*, and the native tool-call route
(``consult_round_protocol``) removes it for *tool calls* on every plane that
has a structured channel. This module is what remains necessary where neither
applies.
"""

from __future__ import annotations

import json
import re
from typing import Any


def _extract_json(raw: str) -> dict[str, Any] | None:
    """Pull a strict-JSON object out of an LLM response.

    Tolerates markdown fences and trailing prose past the closing brace.
    Returns None on parse failure (caller decides how to recover).

    STRING-AWARE brace matching: a ``{`` or ``}`` that appears INSIDE a quoted
    JSON string value (e.g. ``{"answer": "the set is }"}``) is literal text, not
    structural — so the close-brace scan skips characters inside strings and
    honors backslash escapes (``\\"`` does NOT close the string). A naive
    depth-counter that ignores strings closes early on the first in-string ``}``
    and truncates the object into invalid JSON; this is the fix for that class.
    """
    candidate = (raw or "").strip()
    if not candidate:
        return None
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
        candidate = candidate.strip()
    # Start at the first brace so leading prose ("Here is the JSON: {...}") or a
    # thinking/preamble line doesn't defeat the parse, then brace-match the close.
    start = candidate.find("{")
    if start == -1:
        return None
    candidate = candidate[start:]
    depth = 0
    end = len(candidate)
    in_string = False
    escaped = False
    for i, c in enumerate(candidate):
        if in_string:
            # Inside a quoted string: braces are literal. Track escapes so an
            # escaped quote (\") does not close the string, and a literal brace
            # in the value can never shift the structural depth.
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    candidate = candidate[:end]
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


# ---------------------------------------------------------------------------
# The plain-markdown FINAL contract (§28.4 — kills the unparseable class)
# ---------------------------------------------------------------------------


#: First line of a FINAL reply. Deliberately not JSON — see
#: :func:`_parse_sentinel_final` for the shape and the reason.
FINAL_SENTINEL = "<<<FINAL>>>"

#: The metadata lines a sentinel final may carry between the sentinel and the
#: markdown body. Short and FIRST, so a cap-truncated answer loses the tail of
#: the prose and never the metadata.
#:
#: ``watch_candidates`` (CONSULT_REVIEW_2026-09-28 §B.4) makes the answer's
#: "what would change my mind" section machine-readable: the same items the
#: prose closes with, semicolon-separated, so a later lane can turn them into
#: ``claim_watch`` rows instead of a reader re-reading the prose for them. It
#: parses through the same list branch as ``unanswered_aspects`` — a header
#: line is a header line — and nothing is minted from it here.
_FINAL_HEADER_RE = re.compile(
    r"^\s*(uncertainty|cited_refs|unanswered_aspects|watch_candidates)\s*:\s*(.*)$",
    re.IGNORECASE,
)


#: Every form of the sentinel a model actually emits (W11 SWEEP-2): the
#: documented ``<<<FINAL>>>``, and the drifted ones the live turns carry —
#: ``<<<FINAL>>`` (10-04, two persisted answers leaked their whole header block
#: because the strict prefix test missed it), one or no closing bracket, inner
#: spaces, any case. Never a longer word (``<<<FINALLY``).
_SENTINEL_RE = re.compile(r"<{2,3}\s*FINAL\s*>{0,3}(?![A-Za-z0-9_])", re.IGNORECASE)

#: The same marker ENDING a line of narration ("…compose the final answer.
#: <<<FINAL>>>") — the mid-text form. Only at the end of the line: a marker
#: quoted mid-sentence is a quotation, not the frame.
_SENTINEL_TAIL_RE = re.compile(
    r"(?:^|\s)<{2,3}\s*FINAL\s*>{0,3}$", re.IGNORECASE,
)


def _sentinel_lead(line: str) -> str | None:
    """If ``line`` opens the FINAL frame, return whatever trails the sentinel
    on that line (usually ``""``); otherwise None.

    THE ONE EXTRACTION POINT for the frame's marker: every caller that finds,
    strips or routes on the sentinel reaches it through here. Tolerant of
    decoration a model may wrap around it — backticks, bold stars, a heading
    marker, surrounding whitespace — and of the drifted spellings
    :data:`_SENTINEL_RE` names, because the sentinel is a routing token, not
    content: a form this misses is a form that lands in the persisted answer.
    """
    stripped = line.strip().lstrip("#>*_` \t")
    match = _SENTINEL_RE.match(stripped)
    if match is not None:
        return stripped[match.end():].lstrip(" \t:*`>")
    if _SENTINEL_TAIL_RE.search(line.rstrip().rstrip("*_` \t")):
        return ""
    return None


def _sentinel_line_index(lines: list[str]) -> tuple[int, str] | None:
    """Locate the line that OPENS the FINAL block, with whatever trails it.

    The documented contract is that the sentinel is the first line, and that
    is still the preferred read. It was also, until the 2026-09-16 review, the
    ONLY accepted one — "the sentinel must LEAD; one buried in prose is more
    likely the model quoting the contract back at us mid-explanation".

    That guard cost a whole live consult. A reasoning-tier planner narrates
    before it acts, and turn ``4835dfa8`` opened with one sentence of plan
    ("I now have enough to write a full assessment… Let me compose the final
    answer.") before a perfectly-formed ``<<<FINAL>>>`` block carrying
    ``uncertainty: 0.55`` and fifteen deliberately chosen ``cited_refs``. The
    lead rule rejected it; the caller's salvage stamped a DEFAULT 0.60 and
    dropped the refs entirely — which is both halves of the review's defect 4
    (a header that disagreed with the FINAL the operator could read three
    lines below it) and a silent downgrade of the citation set from the
    model's fifteen picks to all 97 tool refs.

    So: prefer a leading sentinel; otherwise take the LAST standalone sentinel
    line that is not inside a code fence. Last, because a model that quotes the
    contract and then answers emits the quote first and the real final after —
    the ordering the old comment was worried about still resolves correctly.
    A sentinel INSIDE a fence is never a candidate: that is a quotation by
    construction.
    """
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines):
        return None
    lead = _sentinel_lead(lines[idx])
    if lead is not None:
        return idx, lead

    fenced = False
    found: tuple[int, str] | None = None
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        trailing = _sentinel_lead(line)
        if trailing is not None:
            found = (i, trailing)
    return found


def _parse_header_list(raw: str) -> list[str]:
    """Split a header list value into its items.

    Accepts the documented ``a, b`` / ``a; b`` forms AND a JSON array — a model
    that just spent the whole loop emitting JSON will sometimes reach for
    brackets out of habit, and that is not worth burning a round over.
    """
    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
    separator = ";" if ";" in text else ","
    return [part.strip() for part in text.split(separator) if part.strip()]


#: The most ``cited_refs`` a FINAL header may carry (W11-F160). Turn
#: ``91b6260c`` wrote ~101 distinct ids and then looped the same line until the
#: 32,768-token output cap: 969 ids, 102 unique, cut mid-uuid, no answer under
#: it. The prompt asks for at most this many; the parser enforces it.
CITED_REFS_MAX = 30

#: A cut-off uuid prefix — what a cap-truncated ``cited_refs`` line ends in.
_UUID_FRAGMENT_RE = re.compile(r"^[0-9a-fA-F]{1,8}(?:-[0-9a-fA-F]{0,12}){0,4}$")


def _parse_cited_refs_header(raw: str) -> list[str]:
    """``cited_refs`` header tokens as an ordered, unique, bounded list.

    The degenerate-repetition guard (W11-F160): stop at the FIRST repeated id
    (a model that starts re-listing ids is looping, and nothing after the
    repeat is a new choice), drop a trailing cut-off uuid fragment, and keep
    at most :data:`CITED_REFS_MAX` ids. Tokens are returned exactly as written,
    so a normal header (no repeats, under the cap) parses byte-identically to
    :func:`_parse_header_list`; a non-uuid label is kept for the malformed-ref
    seam downstream.
    """
    tokens = _parse_header_list(raw)
    out: list[str] = []
    seen: set[str] = set()
    had_uuid = False
    for pos, token in enumerate(tokens):
        if len(seen) >= CITED_REFS_MAX:
            break
        bare = strip_ref_wrappers(token)
        if not bare:
            out.append(token)  # pure decoration; the ref seam drops it
            continue
        hits = _TOKEN_UUID_RE.findall(bare)
        had_uuid = had_uuid or bool(hits)
        if not hits:
            if pos == len(tokens) - 1 and had_uuid and _UUID_FRAGMENT_RE.match(bare):
                break  # a uuid list cut mid-uuid, not a short label
            hits = [bare]
        fresh = {h.lower(): h for h in hits if h.lower() not in seen}
        if len(fresh) == len({h.lower() for h in hits}) and (
            len(seen) + len(fresh) <= CITED_REFS_MAX
        ):
            seen.update(fresh)
            out.append(token)
            continue
        # A repeat (or a multi-id token straddling the cap): keep its new ids
        # up to the cap, one bare id each, then stop.
        for key, hit in list(fresh.items())[: CITED_REFS_MAX - len(seen)]:
            seen.add(key)
            out.append(hit)
        break
    return out


def _read_header(payload: dict[str, Any], key: str, value: str) -> None:
    """Store one FINAL header line's value on ``payload``."""
    if key == "uncertainty":
        try:
            payload["uncertainty"] = float(value.strip())
        except (TypeError, ValueError):
            pass  # _build_consult_response applies its own default
    elif key == "cited_refs":
        payload[key] = _parse_cited_refs_header(value)
    else:
        payload[key] = _parse_header_list(value)


# ---------------------------------------------------------------------------
# THE REF PARSE SEAM (2026-09-28 consult review §B.3)
# ---------------------------------------------------------------------------
#
# THE DEFECT. A ``cited_refs:`` header reaches ``_coerce_uuid_list`` as a list
# of tokens and each one goes through a bare ``UUID()`` inside a
# ``try/except: continue``. Every token the core plane decorates — and it
# decorates constantly; the 2026-09-28 core run wrote every citation in
# ``【…】`` — fails that constructor and is dropped with no record anywhere.
# Two different things were being thrown away in the same silence:
#
#   * a REAL id the model wrapped in a bracket (``【a1b2…】``, ``[a1b2…]``,
#     ``(a1b2…)``). This is a parse failure of OURS and the id is recoverable.
#   * a FABRICATED non-uuid label (``sit-001``, the same run's first tool
#     call). This is a fact about the answer and the operator must see it.
#
# So: strip the wrappers, recover what is recoverable, and hand back what is
# not as a MALFORMED list the caller carries on the response rather than
# dropping. Bounded and de-duplicated, because it is model-authored text.

#: Bracket / quote pairs a model wraps a bare id in. The full-width four are
#: the same set ``provenance.citation_markers`` normalizes in prose; they turn
#: up in the header line for exactly the same reason.
_REF_WRAPPERS = "[]()【】［］〔〕〖〗<>\"'`*«»“”‘’ \t"

#: A bare uuid anywhere inside a token. The recovery of last resort: a model
#: that writes ``cited_refs: 【a…】【b…】`` produces ONE comma-free token
#: holding two real ids, and splitting on the bracket glyphs would be a second
#: dialect of the same rule. Only well-formed uuids are ever recovered, so this
#: can add a citation the model actually wrote and can never invent one.
_TOKEN_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

#: How much of a malformed token to keep, and how many. Model-authored text on
#: a response the panel renders: bounded at the seam, never at the reader.
MALFORMED_REF_MAX_LEN = 40
MALFORMED_REF_MAX_ITEMS = 20


def strip_ref_wrappers(token: Any) -> str:
    """One header/JSON ref token with its decoration removed.

    Idempotent, and a no-op on an already-bare uuid.
    """
    return str(token).strip().strip(_REF_WRAPPERS)


def split_ref_tokens(raw: Any) -> tuple[list[str], list[str]]:
    """``(uuid strings, malformed tokens)`` for one ``cited_refs`` value.

    Order-preserving and de-duplicated on both sides. The uuids come back as
    the model wrote them (case included); the CALLER canonicalizes by
    constructing a ``UUID``, because that is where the failure has somewhere to
    go. A token that is neither a uuid nor a wrapper around one is malformed
    and is REPORTED — never dropped, which is the whole point of this seam.
    """
    if not isinstance(raw, (list, tuple)):
        # A non-list ``cited_refs`` is not a citation list and never was: the
        # loop has always returned nothing for it, and widening that here would
        # be a second change hiding inside this one.
        return [], []
    items: list[Any] = list(raw)
    found: list[str] = []
    seen: set[str] = set()
    malformed: list[str] = []
    bad_seen: set[str] = set()
    for item in items:
        text = strip_ref_wrappers(item)
        if not text:
            continue
        hits = _TOKEN_UUID_RE.findall(text)
        if hits:
            for hit in hits:
                key = hit.lower()
                if key not in seen:
                    seen.add(key)
                    found.append(hit)
            continue
        if len(malformed) >= MALFORMED_REF_MAX_ITEMS:
            continue
        clipped = text[:MALFORMED_REF_MAX_LEN]
        if clipped not in bad_seen:
            bad_seen.add(clipped)
            malformed.append(clipped)
    return found, malformed


def _parse_sentinel_final(raw: str) -> dict[str, Any] | None:
    r"""Parse the plain-markdown FINAL reply into the loop's final-payload shape.

    THE CONTRACT (rendered verbatim in the system prompt's Loop protocol)::

        <<<FINAL>>>
        uncertainty: 0.35
        cited_refs: <uuid>, <uuid>
        unanswered_aspects: one gap; another gap
        watch_candidates: an indicator that would change this; another

        ## Bottom line
        ...markdown...

    WHY IT IS NOT JSON. The previous contract asked for one strict-JSON object
    whose ``answer`` value was a multi-KB markdown document inside a JSON
    string, under a 2048-token output cap. Real turn ``07a69948`` /
    ``39b4d769`` is what that collides into: three consecutive rounds produced a
    complete, well-cited answer that was cut mid-string, failed ``json.loads``,
    and was thrown away — each recovery re-asking for the WHOLE answer under the
    SAME cap, ratcheting the transcript (and the bill) 30k → 32k → 34k → 36k
    tokens before the loop accepted a SHORTER, worse fourth answer. Every
    quote, newline and backslash of ~3,000 chars of prose had to survive JSON
    escaping AND the envelope had to close inside the cap.

    With the wrapper gone there is no envelope to close. A cap-truncated final
    now degrades to a complete-looking-but-cut markdown answer the operator can
    read and the parser accepts, instead of a parse failure plus a retry
    ratchet. The headers lead for the same reason: truncation eats the tail of
    the prose, never the metadata.

    Returns the SAME dict shape the JSON contract produced — so
    :func:`_build_consult_response` and every downstream consumer are
    unchanged — or None when this is not a sentinel final, in which case the
    caller falls back to the legacy JSON shape.
    """
    text = (raw or "").strip()
    if not text:
        return None
    # A fence around the WHOLE reply is decoration; strip it before looking.
    if text.startswith("```"):
        fenced = text.splitlines()[1:]
        while fenced and fenced[-1].strip().startswith("```"):
            fenced.pop()
        text = "\n".join(fenced).strip()

    lines = text.splitlines()
    found = _sentinel_line_index(lines)
    if found is None:
        return None
    idx, trailing = found
    idx += 1

    payload: dict[str, Any] = {"final": True}
    body_lines: list[str] = [trailing] if trailing else []
    if not body_lines:
        # Header block: consecutive `key: value` lines, any order, all
        # optional. The first line that is not one begins the answer. Blank
        # lines are skipped only when a header NOT yet read follows them, so a
        # stray blank between two headers cannot push one into the answer.
        seen_keys: set[str] = set()
        while idx < len(lines):
            nxt = idx
            while nxt < len(lines) and not lines[nxt].strip():
                nxt += 1
            match = _FINAL_HEADER_RE.match(lines[nxt]) if nxt < len(lines) else None
            if match is None or (nxt > idx and match.group(1).lower() in seen_keys):
                break
            key = match.group(1).lower()
            seen_keys.add(key)
            _read_header(payload, key, match.group(2))
            idx = nxt + 1

    body = "\n".join([*body_lines, *lines[idx:]]).strip()
    if not body:
        # Sentinel with nothing under it is not a usable answer. Return None so
        # the loop asks for a correction instead of storing an empty answer.
        return None
    payload["answer"] = body
    return payload


def _parse_round_reply(raw: str) -> dict[str, Any] | None:
    """Parse one planner round into the loop's internal shape.

    Accepts BOTH final contracts. The plain-markdown sentinel is current; the
    ``{"final": true, "answer": ...}`` JSON envelope is legacy and still parses
    for the transition — persisted sessions replayed through the loop, a
    descriptor still carrying an older system prompt, and the deep_consult
    analyze stage, which re-enters this loop. Tool rounds are unchanged and
    always strict JSON.

    The shapes cannot be confused: a sentinel reply is never valid JSON, and a
    JSON reply never leads with the sentinel.
    """
    sentinel = _parse_sentinel_final(raw)
    if sentinel is not None:
        return sentinel
    return _extract_json(raw)


#: The uncertainty a salvaged final carries when the model wrote NO header at
#: all. ONE named constant, referenced by the one builder below — the review's
#: defect 4 was literally two copies of ``0.6`` in ``consult_on_demand``, each
#: able to contradict a number the model had actually written.
SALVAGE_UNCERTAINTY = 0.6

#: How far into a headerless salvage to look for stray header lines. The
#: contract puts them FIRST, so a handful of lines is the whole window; going
#: deeper would start matching prose that merely discusses uncertainty.
_SALVAGE_HEADER_SCAN_LINES = 6


def _lift_leading_headers(payload: dict[str, Any], body: str) -> str:
    """Move any leading ``key: value`` header lines off ``body`` into ``payload``.

    Reached only when no sentinel was found at all. A model that drops the
    sentinel often still writes the header lines, and leaving them in the prose
    is what produces the mismatch the operator saw: the panel reporting a
    default while the answer text three lines down states the model's own
    number. One value, one owner.
    """
    lines = body.splitlines()
    consumed = 0
    seen_keys: set[str] = set()
    for i, line in enumerate(lines[:_SALVAGE_HEADER_SCAN_LINES]):
        if not line.strip():
            continue
        match = _FINAL_HEADER_RE.match(line)
        if match is None:
            break
        key = match.group(1).lower()
        # Past a blank line only a header NOT yet read is still a header (the
        # same rule as the sentinel block, W11-F160).
        if i > consumed and key in seen_keys:
            break
        seen_keys.add(key)
        _read_header(payload, key, match.group(2))
        consumed = i + 1
    return "\n".join(lines[consumed:]).strip() if consumed else body


def final_payload_from_text(raw: str) -> dict[str, Any]:
    """Build a FINAL payload from a model's free text — THE one builder.

    Every terminal route in the consult loop lands here: the native tool-rounds
    route whose text carried no tool call, and the forced-final synthesis. Both
    used to inline their own ``{"answer": …, "uncertainty": 0.6}`` fallback,
    which is how run ``4835dfa8`` reported ``uncertainty 0.60`` in its header
    over an answer that said ``0.55``.

    The ladder is: the sentinel contract, then the legacy JSON envelope, then
    prose — and prose still gets its leading headers lifted, so the model's own
    number wins whenever the model wrote one. :data:`SALVAGE_UNCERTAINTY` is
    the single default, used only when it genuinely wrote none.
    """
    parsed = _parse_sentinel_final(raw)
    if parsed is not None:
        parsed["final"] = True
        return parsed
    legacy = _extract_json(raw)
    if (
        isinstance(legacy, dict)
        and legacy.get("final") is True
        and legacy.get("answer")
    ):
        legacy["final"] = True
        return legacy
    text = _strip_leading_sentinel(raw)
    payload: dict[str, Any] = {
        "final": True, "uncertainty": SALVAGE_UNCERTAINTY,
    }
    # W11-F160: the answer is the BODY. A reply that was only a sentinel and
    # header lines has no answer, and its header text must never be stored as
    # one (turn 91b6260c) — the caller reads the empty answer as "not
    # synthesized" and offers re-synthesis from the evidence.
    payload["answer"] = _lift_leading_headers(payload, text)
    return payload


def _strip_leading_sentinel(text: str) -> str:
    """Prose with a bare leading FINAL sentinel line removed.

    Only reached on the terminal forced-final salvage, where the model wrote an
    answer with no parseable header block at all.
    """
    stripped = (text or "").strip()
    if not stripped:
        return ""
    head, _, rest = stripped.partition("\n")
    trailing = _sentinel_lead(head)
    if trailing is None:
        return stripped
    return "\n".join([trailing, rest]).strip() if trailing else rest.strip()


# Short JSON string escapes we honor when salvaging a malformed nested envelope.
_JSON_STR_ESCAPES = {
    '"': '"', "\\": "\\", "/": "/",
    "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
}

#: Matches either a ``\uXXXX`` unicode escape or any short ``\x`` escape.
_JSON_ESCAPE_RE = re.compile(r"\\u[0-9a-fA-F]{4}|\\.", re.DOTALL)


def _unescape_json_str(s: str) -> str:
    r"""Decode JSON string escapes (``\n``, ``\t``, ``\"``, ``\\``, ``\uXXXX``).

    Operates on the str directly (NOT via ``unicode_escape``, which mangles real
    UTF-8 — em-dashes, smart quotes) so multibyte LITERALS survive untouched,
    while ``\uXXXX`` escapes are decoded to their code point.
    """
    def _one(m: re.Match[str]) -> str:
        esc = m.group(0)
        if len(esc) == 6 and esc[1] == "u":  # \uXXXX
            try:
                return chr(int(esc[2:], 16))
            except ValueError:  # pragma: no cover — regex already constrains hex
                return esc
        return _JSON_STR_ESCAPES.get(esc[1], esc)

    return _JSON_ESCAPE_RE.sub(_one, s)


# Keys that mark the END of a nested ``"answer"`` value when the inner envelope
# is malformed and ``json.loads`` cannot parse it. ``final`` is deliberately NOT
# here: in a final-envelope it precedes ``answer``, so it never marks the tail.
_NESTED_TAIL_KEYS = (
    "uncertainty", "cited_refs", "unanswered_aspects", "watch_candidates",
)


def _unwrap_double_envelope(answer: str) -> str:
    """Recover a markdown answer the planner double-wrapped in a JSON envelope.

    LEGACY / TRANSITION PATH. The current FINAL contract is plain markdown
    behind a sentinel (:func:`_parse_sentinel_final`), which has no envelope to
    double-wrap and no escaping to slip — so a sentinel final never needs this.
    It stays for the JSON finals that still arrive: replayed sessions, an older
    system prompt, and a model that reverts to the shape it spent the loop
    emitting. It also still runs over a sentinel answer's body, harmlessly, in
    case a model mixes the two.

    Some planner turns emit ``{"final": true, "answer": "<a NESTED
    {\"final\":...} JSON string>"}``. :func:`_extract_json` parses the OUTER
    object, so ``answer`` ends up being the raw inner JSON TEXT rather than the
    prose, and when the inner has unescaped quotes/newlines it is not even valid
    JSON — so the UI renders a raw ``{...}`` block.

    This lifts ONE level: when ``answer`` itself is a final-envelope, return its
    inner ``answer`` prose; otherwise return the input unchanged. It NEVER raises
    and NEVER eats content — on ANY doubt it returns the original, so we degrade
    to "ugly but complete", never to a truncated/empty answer.
    """
    s = (answer or "").strip()
    # Gate hard: only engage on an envelope-SHAPED object. A legitimate answer
    # that merely mentions a brace or the word "final" is left untouched.
    if not (s.startswith("{") and '"answer"' in s and '"final"' in s):
        return answer
    # Clean nested case: the inner WAS valid JSON, so it parses. Lift the inner
    # answer (one level; guard against a non-string / empty inner).
    inner = _extract_json(s)
    if isinstance(inner, dict) and "answer" in inner:
        lifted = inner.get("answer")
        if isinstance(lifted, str) and lifted.strip():
            return lifted
    # Malformed nested case: unescaped quotes/newlines defeat json.loads, so we
    # regex-lift the answer value. The body is UNTRUSTED prose that can itself
    # contain a decoy `","uncertainty":` sequence (e.g. an answer that quotes a
    # JSON example), so we anchor on the RIGHTMOST real tail boundary — the
    # genuine envelope end — not the first in-prose match, and refuse to lift
    # when doing so would discard most of the content (a false anchor).
    open_m = re.search(r'"answer"\s*:\s*"', s)
    if open_m is None:
        return answer
    body_region = s[open_m.end():]
    tail_alt = "|".join(_NESTED_TAIL_KEYS)
    tail_matches = list(re.finditer(
        r'"\s*,\s*"(?:' + tail_alt + r')"\s*:', body_region,
    ))
    if tail_matches:
        raw_body = body_region[: tail_matches[-1].start()]   # rightmost = real tail
    else:
        close_m = re.search(r'"\s*\}\s*$', body_region)
        if close_m is not None:
            raw_body = body_region[: close_m.start()]
        else:
            # No clean envelope tail — a legacy JSON final TRUNCATED by
            # max_tokens mid-string (the class the sentinel contract removes;
            # this arm covers the finals that still arrive JSON-wrapped). The
            # `{"final":...,"answer":"` PREFIX is definitely not answer
            # content, so lift the remainder (dropping a dangling partial
            # escape) rather than render raw JSON.
            raw_body = body_region.rstrip("\\")
    body = _unescape_json_str(raw_body).strip()
    # "Never eat content": if the lift would discard more than half of the body
    # region, the anchor is almost certainly a false positive — degrade to the
    # original (ugly-but-complete) rather than truncate a legitimate answer.
    if not body or len(body) < 0.5 * len(body_region):
        return answer
    return body


__all__ = [
    "FINAL_SENTINEL",
    "MALFORMED_REF_MAX_ITEMS",
    "MALFORMED_REF_MAX_LEN",
    "SALVAGE_UNCERTAINTY",
    "_extract_json",
    "_parse_header_list",
    "_parse_round_reply",
    "_parse_sentinel_final",
    "_sentinel_lead",
    "_strip_leading_sentinel",
    "_unescape_json_str",
    "_unwrap_double_envelope",
    "final_payload_from_text",
    "split_ref_tokens",
    "strip_ref_wrappers",
]
