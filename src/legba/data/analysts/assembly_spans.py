"""ASSEMBLY SPANS — the unit of currency, and the gate that lives at construction.

D-1 §1.2 / §1.3b / §3.6. A span is a verbatim slice of ONE origin head's body
that carries its own provenance: the head id, BYTE offsets into that body, and a
sha256 of the whole body at capture time. Never a path, never a paraphrase, never
a depth-2 reference — every tier's quote check is depth-1 against the desk head
that wrote the words.

THE HARD GATE LIVES HERE (D-1 §3.6, ratified F-12). The assembly is
deterministic, so a quote-fidelity failure is not a model error — it is a bug in
this module. Treating it as a verify-time reject would be treating a crash as a
score, and D-1 §3.6 measured that verify has no withhold seam to reject INTO.
So :func:`build_span` self-checks every span against the origin body at
CONSTRUCTION and raises :class:`SpanConstructionError` when the bytes disagree.
A span that fails byte-identity cannot be built, therefore cannot be persisted,
therefore cannot be rendered. Quote fidelity is 1.0 by construction; D-3's arm 1
is an independent auditor that must READ 1.0, and a non-1.0 is a construction
regression that pages.

THE U+2011 LESSON, AS A MECHANIC. Matching folds unicode; STORAGE does not.
:func:`build_span` stores ``text`` as the raw slice of the raw body and offsets
into the raw UTF-8 bytes. The shared fold
(``data.provenance.text_fold.normalize_for_match``) is used ONLY to answer
"are these the same words" — never to compute an offset, never to produce stored
text. Folding is not length-preserving (U+00AD vanishes; U+FF11 goes in as one
character and comes out as a different one), so an offset taken against folded
text is meaningless against the body it claims to index.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

from ..provenance.text_fold import normalize_for_match

__all__ = [
    "CONTEXT_SPAN_ROLES",
    "SPAN_ROLE_BLUF",
    "SPAN_ROLE_CONTEXT_BODY",
    "SPAN_ROLE_WHAT_CHANGED",
    "SPAN_ROLE_BODY",
    "SPAN_ROLE_INDICATOR",
    "SPAN_ROLES",
    "SpanConstructionError",
    "body_sha256",
    "build_context_span",
    "build_span",
    "context_spans",
    "extract_lead_span",
    "is_context_span",
    "quoted_spans",
    "span_markers",
    "first_sentence_end",
    "verify_span",
]


class SpanConstructionError(RuntimeError):
    """A span could not be constructed byte-identically against its origin.

    Raised, never swallowed, never degraded to a soft reason code — the no-stubs
    rule and D-1 §3.6's assemble-time gate. The caller's contract is to fail the
    RUN, not to publish a read with a hole in it.
    """


#: ``spans[].role`` — the closed vocabulary. The role also STAMPS WHICH LOCATOR
#: FIRED (D-1 §1.4d): ``bluf`` means the ``**BLUF:**`` marker was present and
#: parsed, ``what_changed`` and ``body`` are the two declared fallbacks. A run
#: can therefore be audited for how often the fallback fired without re-parsing
#: a single body.
SPAN_ROLE_BLUF: str = "bluf"
SPAN_ROLE_WHAT_CHANGED: str = "what_changed"
SPAN_ROLE_BODY: str = "body"
SPAN_ROLE_INDICATOR: str = "indicator"

#: P3 LANE A — THE CONTEXT SPAN, and why it is a span at all.
#:
#: The country tier's Assessment (``country_assessment``) has to make a
#: CROSS-DIMENSION read: what eight desks TOGETHER say about one country. A lead
#: sentence per desk is enough to rank threads and nowhere near enough to
#: correlate them — the correlation lives in the paragraphs the lead sentence
#: opens. So the country assembly carries each block's origin head in FULL,
#: byte-identical, under its own role.
#:
#: It is a span rather than a loose string because a span is the only shape in
#: this house that carries its own proof: ``build_span`` self-checks the bytes
#: against the origin body at construction, and the digest + offsets make a
#: later divergence detectable. A full body is the degenerate span
#: ``[0:len(body)]`` — it passes the SAME gate every quoted span passes, which
#: is exactly the property that lets the Assessment's evidence map contain it
#: without inventing a second warrant for it.
#:
#: IT IS NOT QUOTED PROSE. Every consumer that RENDERS or REASONS OVER the
#: record's quotations takes :func:`quoted_spans`, never ``block["spans"]``
#: raw — the record body, the region rollup's carry, the absence test, the
#: external-audit claim set and D-3's arms. See each call site's comment.
#:
#: STEP E (2026-09-20) — THE SAME ROLE, ONE TIER UP, AND ITS ORIGIN IS A VOICE.
#: The WORLD tier's input is one desk SENTENCE per country, and the world voice
#: was measured narrating the record's arithmetic because a sentence per country
#: is not enough to argue across countries with. So a world block now carries the
#: COUNTRY ASSESSMENT written FROM the very country assembly the block came
#: through — the country voice's own prose, whole, under this same role and
#: through this same gate.
#:
#: WHAT IS DIFFERENT, AND IT IS THE ONE THING TO HOLD ONTO: at the country tier
#: the context body's ``origin.head_id`` is the block's OWN desk head, so the
#: context and the lead share a warrant. At the world tier they do NOT — the
#: lead's origin is the desk head (carried, depth-1, unchanged) and the
#: context's origin is a DIFFERENT row, the country_assessment head. That is why
#: the citation publishes ``context_origin_id`` separately from ``spine_block``:
#: two origins, named as two, never merged into one claim of provenance.
SPAN_ROLE_CONTEXT_BODY: str = "context_body"

SPAN_ROLES: tuple[str, ...] = (
    SPAN_ROLE_BLUF,
    SPAN_ROLE_WHAT_CHANGED,
    SPAN_ROLE_BODY,
    SPAN_ROLE_INDICATOR,
    SPAN_ROLE_CONTEXT_BODY,
)

#: The roles that are CONTEXT rather than quotation. A frozenset rather than a
#: literal comparison so a second context role (an indicator table, say) is one
#: line here and zero lines at the six call sites that filter on it.
CONTEXT_SPAN_ROLES: frozenset[str] = frozenset({SPAN_ROLE_CONTEXT_BODY})


def is_context_span(span: Any) -> bool:
    """Is this span CONTEXT handed to a reader, rather than a quotation?

    Total: a non-mapping, or a span with no role, is treated as a quotation —
    which is the shipped behaviour for every span built before this role
    existed.
    """
    if not isinstance(span, Mapping):
        return False
    return str(span.get("role") or "") in CONTEXT_SPAN_ROLES


def context_spans(block: Any) -> list[Mapping[str, Any]]:
    """``block["spans"]`` MINUS the quotations — the CONTEXT handed to a reader.

    The exact complement of :func:`quoted_spans`, and it exists for the same
    reason that one does: STEP E gives the WORLD tier a context span too (each
    carried block's COUNTRY ASSESSMENT in full), so the two places that render
    context — the prompt's reads-in-full section and the evidence map's
    ``context_origin_id`` — must agree on what a context span IS with ONE
    predicate rather than two inline role comparisons that can drift.

    ``[]`` on every block that carries none, which is every thematic block and
    every block built before the role existed.
    """
    if not isinstance(block, Mapping):
        return []
    raw = block.get("spans")
    if not isinstance(raw, (list, tuple)):
        return []
    return [s for s in raw if is_context_span(s)]


def quoted_spans(block: Any) -> list[Mapping[str, Any]]:
    """``block["spans"]`` MINUS the context spans — the record's quotations.

    THE ONE FILTER. Every caller that renders a span, ranks on one, tests one
    for absence, or grades one calls this instead of reading ``spans`` raw, so
    a country block carrying its origin head in full is byte-for-byte invisible
    to all of them. STEP E (2026-09-20) put a context span on the WORLD tier's
    carried blocks too — the block's own COUNTRY ASSESSMENT, read in full — so
    the filter now protects two tiers rather than one; on a THEMATIC block,
    which carries none, the list it returns IS ``block["spans"]``.
    """
    if not isinstance(block, Mapping):
        return []
    raw = block.get("spans")
    if not isinstance(raw, (list, tuple)):
        return []
    return [
        s for s in raw
        if isinstance(s, Mapping) and not is_context_span(s)
    ]

# The desk's OWN wire-citation markers, in both spellings. The core plane emits
# full-width 【N】 (feedback-core-plane-fullwidth-citation-brackets) and the
# hosted plane emits [N]; both are canonicalised to [N] on the way out, so a
# reader and a verifier compare one spelling.
_MARKER_RE = re.compile(r"[\[【](\d{1,3})[\]】]")

# The BLUF region: everything after the marker up to a blank line or a heading.
# Deliberately the same bound ``composition_integrity._BLUF_RE`` uses, so the
# tension detector (which reads ``desk_verdict_text``) and the lead span are
# looking at the same text rather than two nearly-identical texts.
_BLUF_RE = re.compile(
    r"\*\*\s*BLUF\s*:?\s*\*\*\s*:?\s*(.+?)(?:\n\s*\n|\n\s*#|\Z)",
    re.DOTALL,
)

# The first "## What changed" bullet — fallback 1.
_WHAT_CHANGED_RE = re.compile(
    r"##\s*What\s+changed\s*\n+\s*[-*•]\s*(.+?)(?:\n\s*[-*•]|\n\s*\n|\n\s*#|\Z)",
    re.DOTALL | re.IGNORECASE,
)

# A leading "*As of ...*" stamp line every desk body opens with; never a claim.
_AS_OF_LINE_RE = re.compile(r"^\s*\*?As of\b.*$", re.IGNORECASE | re.MULTILINE)

#: Fallback 2's floor: a prose sentence shorter than this is a fragment, a
#: heading remnant or a stub, not a lead. D-1 §1.4d fixes it at 40.
MIN_PROSE_SPAN_CHARS: int = 40

# Sentence-terminator abbreviations. A naive "first period" split cuts the very
# first live specimen inspected for this train ("...recent U.S. strikes on
# Iran...") in half, so the terminator is abbreviation-aware. Lower-cased,
# without the trailing dot.
_ABBREVIATIONS: frozenset[str] = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "sen", "rep", "gov", "gen", "adm", "col",
    "lt", "sgt", "capt", "pres", "amb", "sec", "st", "mt", "ft",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec",
    "no", "nos", "vs", "etc", "al", "approx", "est", "inc", "ltd", "co", "corp",
    "fig", "op", "cf", "ca", "vol", "pp", "ed", "eds", "min", "max", "hr", "hrs",
})

# A dotted initialism immediately before the candidate terminator: "U.S.",
# "E.U.", "a.m.", "J. D." — the terminator is part of the token, not the end of
# the sentence.
_DOTTED_INITIALISM_RE = re.compile(r"(?:\b\w\.){1,}\w$")
_TERMINATOR_RE = re.compile(r"[.!?…]")
# What legitimately follows a real terminator: end of text, or whitespace then
# something that can open a sentence.
_SENTENCE_OPENER_RE = re.compile(r'^[\s]*(?:[A-ZÀ-ɏ"“‘(\[【#*\-]|$)')


def body_sha256(body: Any) -> str:
    """sha256 of the FULL origin body, hex.

    Over the whole body, NOT over the captured prefix — D-1 §1.3b. An origin
    that was truncated on capture is then DETECTABLE (``end`` runs past the
    captured text while the digest still describes the real row) rather than
    silently mis-verified against a shorter string.
    """
    return hashlib.sha256(str(body or "").encode("utf-8")).hexdigest()


def span_markers(text: Any) -> list[str]:
    """The desk's own ``[N]`` wire markers inside ``text``, canonical, in order.

    Both bracket spellings are accepted and BOTH are emitted as ``[N]``. First
    occurrence order, de-duplicated — a marker repeated inside one span is one
    piece of evidence, not two.
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in _MARKER_RE.finditer(str(text or "")):
        canon = f"[{int(m.group(1))}]"
        if canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


def first_sentence_end(text: str, start: int = 0) -> int:
    """Index just past the first sentence terminator in ``text[start:]``.

    Returns ``len(text)`` when the region carries no terminator — a BLUF that
    runs to a paragraph break without a full stop is still a lead span; a
    fabricated cut point would not be.

    Abbreviation-aware on purpose, and the reason is a measured one: the first
    live desk head read for this train opens *"A coordinated escalation
    narrative ... links recent U.S. strikes on Iran ..."*, and a first-period
    split truncates it to nine words that assert nothing.
    """
    n = len(text)
    for m in _TERMINATOR_RE.finditer(text, start):
        end = m.end()
        if m.group(0) == ".":
            head = text[:m.start()]
            if _DOTTED_INITIALISM_RE.search(head):
                continue
            word = re.split(r"[^\w]", head)[-1].lower() if head else ""
            # A single letter before the dot is an INITIAL, never a sentence
            # end: "recent U." is the first half of "U.S.", and cutting there
            # is the exact truncation this function exists to refuse.
            if len(word) == 1 or word in _ABBREVIATIONS:
                continue
        if end >= n:
            return n
        if _SENTENCE_OPENER_RE.match(text[end:end + 4]):
            return end
    return n


def _prose_paragraphs(body: str) -> list[tuple[int, str]]:
    """``(char_offset, text)`` for each non-heading, non-as-of paragraph."""
    out: list[tuple[int, str]] = []
    pos = 0
    for chunk in re.split(r"(\n\s*\n)", body):
        if not chunk.startswith("\n"):
            stripped = chunk.strip()
            if (
                stripped
                and not stripped.startswith("#")
                and not _AS_OF_LINE_RE.match(stripped)
            ):
                lead = len(chunk) - len(chunk.lstrip())
                out.append((pos + lead, chunk.strip()))
        pos += len(chunk)
    return out


def verify_span(body: str, start: int, end: int, text: str) -> str | None:
    """``None`` when ``body``'s bytes ``[start:end]`` ARE ``text``; else why not.

    The whole gate in four lines. Raw bytes on both sides — no fold, no strip,
    no case. The fold is available to the CALLER for a "same words" diagnosis,
    and is used below only to make the failure message legible; it never
    decides.
    """
    raw = str(body or "").encode("utf-8")
    if start < 0 or end > len(raw) or start >= end:
        return f"offsets [{start}:{end}] outside a {len(raw)}-byte body"
    try:
        cut = raw[start:end].decode("utf-8")
    except UnicodeDecodeError:
        return f"offsets [{start}:{end}] cut a UTF-8 sequence in half"
    if cut != text:
        same_words = normalize_for_match(cut) == normalize_for_match(text)
        return (
            "span text is not byte-identical to the origin slice"
            + (
                " (they fold EQUAL — this is a unicode-storage bug, not a "
                "content bug: the span was stored from normalised text)"
                if same_words
                else " (they do not even fold equal)"
            )
        )
    return None


def build_span(
    *,
    role: str,
    body: Any,
    start_char: int,
    end_char: int,
    head_id: str,
    scope_predicate: Any = None,
) -> dict[str, Any]:
    """One ``spans[]`` entry, self-checked against ``body``.

    ``start_char`` / ``end_char`` index the RAW body by CHARACTER; the stored
    offsets are BYTE offsets, because that is what D-1 §1.2 specifies and what
    makes a digest and an offset describe the same object. The conversion is
    explicit rather than implicit so nobody later "simplifies" it into a
    character offset that agrees on ASCII and lies on everything else.

    ``scope_predicate`` is ``composition_integrity.has_collection_denominator_
    scope`` (injected so this module stays a leaf of the analysts package and
    does not import the provenance verify tree). When it fires on the SOURCE
    SENTENCE the collection qualifier is recorded in ``scope_tokens`` — D-3's
    arm 2 then has the token it needs to prove the span did not truncate the
    denominator away (the M-8 shape).

    Raises :class:`SpanConstructionError` — never returns a degraded span.
    """
    if role not in SPAN_ROLES:
        raise SpanConstructionError(
            f"unknown span role {role!r}; the vocabulary is {SPAN_ROLES}"
        )
    raw_body = str(body or "")
    text = raw_body[start_char:end_char]
    if not text.strip():
        raise SpanConstructionError(
            f"empty span for head {head_id} at chars [{start_char}:{end_char}]"
        )
    start = len(raw_body[:start_char].encode("utf-8"))
    end = start + len(text.encode("utf-8"))

    problem = verify_span(raw_body, start, end, text)
    if problem is not None:
        raise SpanConstructionError(
            f"span construction failed for head {head_id} ({role}): {problem}"
        )

    scope_tokens: list[str] = []
    if scope_predicate is not None:
        try:
            if scope_predicate(text):
                scope_tokens = ["collection_denominator"]
        except Exception:  # a predicate must never take the run down
            scope_tokens = []

    return {
        "role": role,
        "text": text,
        "origin": {
            "head_id": str(head_id),
            "start": start,
            "end": end,
            "body_sha256": body_sha256(raw_body),
            "body_len": len(raw_body.encode("utf-8")),
        },
        "markers": span_markers(text),
        "scope_tokens": scope_tokens,
    }


def extract_lead_span(
    row: Mapping[str, Any],
    *,
    head_id: str,
    scope_predicate: Any = None,
) -> dict[str, Any]:
    """The ONE lead span a candidate head contributes to the ranking (§1.4d).

    Locator order, each stamped in ``role`` so the fallback rate is auditable:

      1. ``bluf`` — the sentence following ``**BLUF:**``. Live BLUF compliance
         at the composition tier is 100% (n=120) and the unit ``_body_shape``
         mandates it on all nine desks, so this is the path essentially every
         real head takes.
      2. ``what_changed`` — the first ``## What changed`` bullet.
      3. ``body`` — the first prose sentence of at least
         :data:`MIN_PROSE_SPAN_CHARS` characters, skipping headings and the
         ``*As of ...*`` stamp line (which is metadata, not a claim).

    Raises :class:`SpanConstructionError` when none of the three finds text —
    a head with no locatable claim is a head this tier must not quote, and D-1
    §3.6 says that is a construction failure, not a soft skip.
    """
    body = str(row.get("body") or "")
    if not body.strip():
        raise SpanConstructionError(f"head {head_id} has an empty body")

    m = _BLUF_RE.search(body)
    if m is not None:
        region_start = m.start(1)
        region = body[region_start:m.end(1)]
        cut = first_sentence_end(region.rstrip())
        text = region[:cut]
        lead = len(text) - len(text.lstrip())
        return build_span(
            role=SPAN_ROLE_BLUF,
            body=body,
            start_char=region_start + lead,
            end_char=region_start + len(text.rstrip()),
            head_id=head_id,
            scope_predicate=scope_predicate,
        )

    w = _WHAT_CHANGED_RE.search(body)
    if w is not None and w.group(1).strip():
        region_start = w.start(1)
        region = body[region_start:w.end(1)]
        cut = first_sentence_end(region.rstrip())
        text = region[:cut]
        lead = len(text) - len(text.lstrip())
        return build_span(
            role=SPAN_ROLE_WHAT_CHANGED,
            body=body,
            start_char=region_start + lead,
            end_char=region_start + len(text.rstrip()),
            head_id=head_id,
            scope_predicate=scope_predicate,
        )

    for offset, para in _prose_paragraphs(body):
        stripped = para.lstrip("-*• \t")
        lead = len(para) - len(stripped)
        cut = first_sentence_end(stripped.rstrip())
        text = stripped[:cut].rstrip()
        if len(text) >= MIN_PROSE_SPAN_CHARS:
            return build_span(
                role=SPAN_ROLE_BODY,
                body=body,
                start_char=offset + lead,
                end_char=offset + lead + len(text),
                head_id=head_id,
                scope_predicate=scope_predicate,
            )

    raise SpanConstructionError(
        f"head {head_id}: no BLUF, no '## What changed' bullet and no prose "
        f"sentence of {MIN_PROSE_SPAN_CHARS}+ characters — nothing to quote"
    )


def build_context_span(
    row: Mapping[str, Any],
    *,
    head_id: str,
) -> dict[str, Any]:
    """The origin head's FULL body as a span — the country tier's context (P3-A).

    The degenerate span: ``[0:len(body)]``, role
    :data:`SPAN_ROLE_CONTEXT_BODY`, through the SAME :func:`build_span` gate as
    every quoted span. Byte-identity is therefore not asserted here, it is
    PROVED here, by the function that proves it for the lead span — which is the
    whole reason this is a span and not a string on the side.

    No ``scope_predicate``. The predicate answers "does this SENTENCE carry a
    collection denominator", and running it over a whole desk read would answer
    a question nobody asked with a token :func:`block_is_absence` would then
    read as a verdict about the lead. The lead span's own token is the block's
    token; this one carries none, on purpose.

    Raises :class:`SpanConstructionError` on an empty body — the same refusal
    :func:`extract_lead_span` makes one line earlier on the same row, so a head
    that reaches here has a body by construction.
    """
    body = str(row.get("body") or "")
    if not body.strip():
        raise SpanConstructionError(
            f"head {head_id} has an empty body — nothing to carry as context"
        )
    return build_span(
        role=SPAN_ROLE_CONTEXT_BODY,
        body=body,
        start_char=0,
        end_char=len(body),
        head_id=head_id,
    )
