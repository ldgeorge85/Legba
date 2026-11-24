# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Splitting a multi-claim desk sentence into pieces, each typed and gated on
its own (lane S-4).

WHY. A desk sentence often carries several figures or several assertions and
one citation: "Exports rose 12%, imports fell 4% and the deficit hit $3bn [1]."
Gate G2 fails the whole sentence when row 1 states only the 12% — by design.
Split, the 12% piece is supported and the other two fail plainly
(``number_not_in_evidence``), instead of one opaque failure.

THE DETERMINISTIC SPLIT (code, where the structure allows).

* Boundaries are cut at semicolons, after a citation-marker cluster that
  more text follows, at clause connectives (``, and`` ``, while`` ``, but``
  ``, whereas`` ``, with`` and the same without the comma), and at a list
  comma only between two segments that each state a digit quantity ("rose
  12%, fell 4%"). A range ("between 13 and 14 November") is never cut.
* A piece must stand on its own: its own citation and at least
  :data:`MIN_PIECE_WORDS` words, or its own figure (a number or a date) and
  enough words around it (:data:`MIN_DIGIT_PIECE_WORDS`,
  :data:`MIN_FIGURE_PIECE_WORDS`). A piece that does not is
  merged into its neighbour, so "Russia and Ukraine agreed" is never cut at
  its "and", and a bare list item ("two football matches") stays in its
  clause.
* A sentence with at most one figure and one citation cluster is not split.

WHICH CITATIONS A PIECE KEEPS — only those textually attached to it. A marker
cluster covers the text back to the previous cluster: a piece with its own
markers keeps them; a piece without any takes the NEXT cluster after it
(``A rose 3% [1], B fell 2% and C hit 5 [2]`` — the B piece takes ``[2]``); a
piece after the last cluster has no citation and is marked ``uncited`` (its
figures are listed, so the failure says plainly what was left unsupported).

THE MODEL-PROPOSED SPLIT (for what the structure does not show). The typing
seat may propose piece boundaries as character offsets into the desk's own
sentence, each with the piece's text. Code accepts the proposal only if the
pieces, in order, are exact substrings of the sentence that concatenate back
to it (only whitespace and punctuation between them, nothing left over); the
offsets are advisory and corrected to where the text actually is (counted).
The model never rewrites a word: a piece's text that is not verbatim in the
sentence refuses the whole split (``split_text_not_in_sentence``).

PURE. stdlib + the content readers.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .statement_content import date_spans, find_dates, find_numbers, find_years, strip_markers

#: A piece needs at least this many words (markers excluded).
MIN_PIECE_WORDS = 2
#: A piece standing on a figure alone (no citation of its own) needs this many
#: words: three with a digit quantity ("imports fell 4%"), four with only a
#: date or a number word ("On 13 November" and "two football matches" stay
#: in their clause).
MIN_DIGIT_PIECE_WORDS = 3
MIN_FIGURE_PIECE_WORDS = 4

_CLUSTER_RE = re.compile(r"(?:\s*\[\d+\](?:\s*[,;]?\s*\[\d+\])*)")
_ORDINAL_RE = re.compile(r"\[(\d+)\]")
#: Boundary candidates (the cut is at the END of each match).
_BOUNDARY_RE = re.compile(
    r";\s*"
    r"|,\s+(?=(?:and|while|whereas|but|with|as well as)\s)"
    r"|,\s+"
    r"|\s+(?=(?:and|while|whereas|but)\s)",
    re.IGNORECASE,
)
_LEAD_RE = re.compile(r"^[\s,;:]*(?:(?:and|while|whereas|but|with|as well as)\s+)?", re.IGNORECASE)
#: Markdown furniture opening a line: a bullet, a bold label ("**BLUF:**").
_FORMAT_LEAD_RE = re.compile(r"^\s*(?:[-*\u2022]\s+)?(?:\*\*[^*]{1,40}?:?\*\*:?\s*)?")
_TRAIL_RE = re.compile(r"[\s,;:]+$")
_FILLER_RE = re.compile(r"^[\s,;:.!?\-–—]*$")


@dataclass(frozen=True)
class Piece:
    """One piece of a desk sentence.

    ``start``/``end`` index the sentence's MARKED text; ``text`` is exactly
    that slice; ``plain`` is it with markers, a leading connective and
    trailing punctuation trimmed (still the desk's own words, in order)."""

    index: int
    start: int
    end: int
    text: str
    plain: str
    ordinals: tuple[int, ...]
    inherited: bool
    figures: tuple[str, ...]
    split_by: str = "none"
    #: S-6 — a piece CODE typed (``assessment`` or ``absence``): a judgement
    #: tail or an absence tail split off a statement (:func:`split_tails`).
    #: The seat does not type it.
    code_kind: str | None = None
    #: S-6 — the index of the piece a code-typed assessment rests on (its
    #: premise), when there is one.
    rests_on: int | None = None

    @property
    def uncited(self) -> bool:
        return not self.ordinals

    def as_dict(self) -> dict[str, Any]:
        out = {"index": self.index, "span": [self.start, self.end], "ordinals": list(self.ordinals),
               "inherited": self.inherited, "figures": list(self.figures),
               "split_by": self.split_by, "uncited": self.uncited}
        if self.code_kind:
            out.update(code_kind=self.code_kind, rests_on=self.rests_on)
        return out


def figures_of(text: str) -> tuple[str, ...]:
    """The numbers and dates ``text`` states (markers removed)."""
    body = strip_markers(text)
    spans = date_spans(body)
    dates = [d.raw for d in find_dates(body)] + [y.raw for y in find_years(body, skip=[d.span for d in find_dates(body)])]
    nums = [n.raw for n in find_numbers(body, skip=spans)]
    return tuple(dates + nums)


def _clusters(text: str) -> list[tuple[int, int, tuple[int, ...]]]:
    out = []
    for m in _CLUSTER_RE.finditer(text):
        ords = tuple(int(x) for x in _ORDINAL_RE.findall(m.group(0)))
        if ords:
            lead = len(m.group(0)) - len(m.group(0).lstrip())
            out.append((m.start() + lead, m.end(), ords))
    return out


def _plain(text: str) -> str:
    out = strip_markers(text)
    out = _FORMAT_LEAD_RE.sub("", out, count=1)
    out = _LEAD_RE.sub("", out, count=1)
    return _TRAIL_RE.sub("", out).strip()


def _words(text: str) -> int:
    return len(_plain(text).split())


def pieces_from_spans(text: str, spans: Sequence[tuple[int, int]], *, split_by: str) -> tuple[Piece, ...]:
    """Pieces for contiguous ``spans`` of the marked sentence ``text``, with
    the citation-attachment rule applied."""
    clusters = _clusters(text)
    out: list[Piece] = []
    for i, (s, e) in enumerate(spans):
        own = [c for c in clusters if s <= c[0] < e]
        if own:
            ords = tuple(dict.fromkeys(o for c in own for o in c[2]))
            inherited = False
        else:
            nxt = next((c for c in clusters if c[0] >= e), None)
            ords = nxt[2] if nxt else ()
            inherited = bool(nxt)
        chunk = text[s:e]
        out.append(Piece(i, s, e, chunk, _plain(chunk), ords, inherited, figures_of(chunk), split_by))
    return tuple(out)


def _has_digit_quantity(chunk: str) -> bool:
    body = strip_markers(chunk)
    return bool(find_numbers(body, skip=date_spans(body), words="none"))


def _viable(chunk: str) -> bool:
    words = _words(chunk)
    if _clusters(chunk):
        return words >= MIN_PIECE_WORDS
    body = strip_markers(chunk)
    if _has_digit_quantity(body):   # "imports fell 4%"
        return words >= MIN_DIGIT_PIECE_WORDS
    return words >= MIN_FIGURE_PIECE_WORDS and bool(figures_of(chunk))


def split_sentence(text: str) -> tuple[Piece, ...]:
    """The deterministic split of one marked sentence (see the module doc).
    Always returns at least one piece; one piece = not split."""
    clusters = _clusters(text)
    whole = pieces_from_spans(text, [(0, len(text))], split_by="none")
    distinct_clusters = [c for c in clusters if text[c[1]:].strip(" .;:!?")]
    if len(figures_of(text)) < 2 and not distinct_clusters:
        return whole
    cuts: set[int] = set()
    for c in clusters:
        rest = text[c[1]:]
        if rest.strip(" .;:!?"):
            lead = re.match(r"[\s,;]*", rest)
            cuts.add(c[1] + (lead.end() if lead else 0))
    matches = [m for m in _BOUNDARY_RE.finditer(text)
               if not any(c[0] <= m.start() < c[1] for c in clusters)]
    edges = sorted({0, len(text), *cuts, *(m.start() for m in matches), *(m.end() for m in matches)})
    for m in matches:
        # a range or a pair of figures ("between 13 and 14 November",
        # "12, 13 and 14 May") is one expression, never two claims
        if re.search(r"\d\s*$", text[:m.start()]) and re.match(r"(?:and\s+|while\s+|but\s+)?\d", text[m.end():]):
            continue
        if m.group(0).lstrip().startswith(",") and not re.match(r",\s+(?:and|while|whereas|but|with|as well as)\s",
                                                                  m.group(0) + text[m.end():m.end() + 12], re.I):
            # a bare list comma cuts only a list of FIGURES ("rose 12%, fell
            # 4%"): both neighbouring segments state a digit quantity
            before = text[max(e for e in edges if e <= m.start() and e != m.start()) if m.start() else 0:m.start()]
            after = text[m.end():min(e for e in edges if e > m.end())]
            if not (_has_digit_quantity(before) and _has_digit_quantity(after)):
                continue
        cuts.add(m.end())
    bounds = sorted(b for b in cuts if 0 < b < len(text))
    spans = [(a, b) for a, b in zip([0, *bounds], [*bounds, len(text)]) if a < b]
    spans = [sp for sp in spans if not _FILLER_RE.match(text[sp[0]:sp[1]])] or [(0, len(text))]
    # merge pieces that cannot stand alone into a neighbour (next, else previous)
    changed = True
    while changed and len(spans) > 1:
        changed = False
        for i, (a, b) in enumerate(spans):
            # a sentence-final citation covers the whole sentence: it does not
            # make the last fragment stand on its own
            chunk = text[a:b] if b < len(text) else _CLUSTER_RE.sub(" ", text[a:b])
            if _viable(chunk):
                continue
            if i + 1 < len(spans):
                spans[i:i + 2] = [(a, spans[i + 1][1])]
            else:
                spans[i - 1:i + 1] = [(spans[i - 1][0], b)]
            changed = True
            break
    # the last span runs to the end of the sentence (closing punctuation)
    spans[-1] = (spans[-1][0], len(text))
    spans[0] = (0, spans[0][1])
    if len(spans) == 1:
        return whole
    return pieces_from_spans(text, spans, split_by="code")


def verify_model_split(
    text: str, proposed: Sequence[Mapping[str, Any]] | Any,
) -> tuple[tuple[Piece, ...] | None, str | None, dict[str, int]]:
    """Check a model-proposed split of the marked sentence ``text``.

    ``proposed`` is a list of ``{"start": int, "end": int, "text": str}``.
    Returns ``(pieces, None, counts)`` when every piece's text is verbatim in
    the sentence, in order, and the pieces concatenate back to it (only
    whitespace/punctuation between them and after the last); else
    ``(None, reason, counts)``. ``counts['offsets_corrected']`` counts pieces
    whose offsets did not point at their text."""
    counts = {"offsets_corrected": 0}
    if not isinstance(proposed, Sequence) or isinstance(proposed, (str, bytes)) or len(proposed) < 2:
        return None, "split_not_a_list", counts
    pos = 0
    starts: list[int] = []
    for entry in proposed:
        if not isinstance(entry, Mapping):
            return None, "split_entry_not_object", counts
        piece = str(entry.get("text") or "").strip()
        if not piece:
            return None, "split_text_missing", counts
        start = entry.get("start")
        at = start if isinstance(start, int) and text[start:start + len(piece)] == piece and start >= pos else None
        if at is None:
            found = text.find(piece, pos)
            if found < 0:
                return None, "split_text_not_in_sentence", counts
            at = found
            counts["offsets_corrected"] += 1
        if not _FILLER_RE.match(_CLUSTER_RE.sub("", text[pos:at])):
            return None, "split_skips_text", counts
        starts.append(at)
        pos = at + len(piece)
    if not _FILLER_RE.match(_CLUSTER_RE.sub("", text[pos:])):
        return None, "split_leaves_text", counts
    # each piece runs to the next piece's start: the gap (a marker cluster,
    # punctuation) stays with the text before it, where a marker attaches
    bounds = [0, *starts[1:], len(text)]
    spans = list(zip(bounds[:-1], bounds[1:]))
    return pieces_from_spans(text, spans, split_by="model"), None, counts


# ---------------------------------------------------------------------------
# Narrowing a piece to the part its quote supports (S-5)
# ---------------------------------------------------------------------------

#: A narrowed piece keeps at least this many words.
MIN_NARROW_WORDS = 3

#: Words that, in the DROPPED part before the kept span, scope over it: a
#: negation, an attribution, a modal or a condition ("officials denied that
#: …", "according to X, …", "if …"). Dropping them would change what the kept
#: words assert, so the narrowing is refused.
_LEAD_SCOPE_RE = re.compile(
    r"\b(?:not|no|never|none|nor|neither|without|den(?:y|ied|ies|ial)|reject\w*|refut\w*|dismiss\w*|"
    r"sa(?:y|ys|id)|claim\w*|alleg\w*|accord\w*|report\w*|cit(?:e|es|ed|ing)|told|tells?|stat(?:e|es|ed)|"
    r"announc\w*|quot\w*|if|unless|whether|would|could|may|might|should|likely|unlikely|possibl\w*|"
    r"expect\w*|plan\w*|propos\w*|threat\w*|warn\w*|fear\w*|rumou?r\w*|unconfirmed|unverified|"
    r"suspect\w*|purport\w*|seem\w*|appear\w*|believ\w*)\b",
    re.IGNORECASE,
)
#: Words that, in the DROPPED part after the kept span, reach back over it
#: ("…, officials said", "…, which X denied", "…, if confirmed"). A plain
#: negation in a trailing clause asserts something of its own ("…, no
#: casualties were reported" is caught by ``report``; "…, not coercion" does
#: not reach back), so it is not in this list.
_TRAIL_SCOPE_RE = re.compile(
    r"\b(?:den(?:y|ied|ies|ial)|disput\w*|reject\w*|refut\w*|dismiss\w*|"
    r"sa(?:y|ys|id)|claim\w*|alleg\w*|accord\w*|report\w*|reportedly|cit(?:e|es|ed|ing)|told|"
    r"stat(?:e|es|ed)|quot\w*|if|unless|whether|unconfirmed|unverified|purport\w*)\b",
    re.IGNORECASE,
)
#: A kept span may begin after / end before one of these (a clause boundary).
_LEAD_EDGE_RE = re.compile(r"(?:[,;:(—–]|\s-|\b(?:and|while|whereas|but))\s*$", re.IGNORECASE)
_TRAIL_EDGE_RE = re.compile(
    r"^\s*(?:[,;:().—–]|-\s|(?:and|while|whereas|but|amid|after|as|which|with|following|"
    r"because|since|despite|though|although|before|when)\b)",
    re.IGNORECASE,
)


def verify_narrowing(
    text: str, piece: Piece, proposed: Mapping[str, Any] | Any,
) -> tuple[Piece | None, str | None, dict[str, Any]]:
    """Check a seat-proposed NARROWING of ``piece`` to the part its quote
    supports: ``{"start": int, "end": int, "text": str}``, character offsets
    into the marked sentence ``text`` (advisory; corrected to where the text
    is, counted). Accepted only when

    * the kept text is verbatim in the sentence, inside the piece, shorter
      than the piece and at least :data:`MIN_NARROW_WORDS` words;
    * it starts at the piece's start or after a clause boundary, and ends at
      the piece's end or before one (no half-clause);
    * the dropped text before it carries no negation, attribution, modal or
      condition that scopes over it, and the dropped text after it none that
      reaches back (``…, officials said``).

    Returns ``(narrowed_piece, None, detail)`` or ``(None, reason, detail)``;
    the narrowed piece keeps the piece's citations and index. The model never
    rewrites a word: only offsets into the desk's own sentence."""
    detail: dict[str, Any] = {"offsets_corrected": 0}
    if not isinstance(proposed, Mapping):
        return None, "narrow_not_an_object", detail
    kept = str(proposed.get("text") or "").strip()
    if not kept:
        return None, "narrow_text_missing", detail
    start = proposed.get("start")
    region = text[piece.start:piece.end]
    at = None
    if isinstance(start, int) and piece.start <= start and text[start:start + len(kept)] == kept \
            and start + len(kept) <= piece.end:
        at = start
    else:
        found = region.find(kept)
        if found < 0:
            return None, "narrow_text_not_in_piece", detail
        at = piece.start + found
        detail["offsets_corrected"] = 1
    end = at + len(kept)
    lead, trail = text[piece.start:at], text[end:piece.end]
    lead_plain, trail_plain = strip_markers(lead).strip(), strip_markers(trail).strip(" .;:!?")
    if not lead_plain and not trail_plain:
        return None, "narrow_is_whole_piece", detail
    if len(_plain(kept).split()) < MIN_NARROW_WORDS:
        return None, "narrow_too_short", detail
    if lead_plain and not (_FORMAT_LEAD_RE.fullmatch(lead) or _LEAD_EDGE_RE.search(strip_markers(lead))):
        return None, "narrow_starts_mid_clause", detail
    if trail_plain and not _TRAIL_EDGE_RE.match(strip_markers(trail)):
        return None, "narrow_ends_mid_clause", detail
    if lead_plain and _LEAD_SCOPE_RE.search(lead_plain):
        detail["scope_word"] = _LEAD_SCOPE_RE.search(lead_plain).group(0).lower()
        return None, "narrow_drops_scope", detail
    if trail_plain and _TRAIL_SCOPE_RE.search(trail_plain):
        detail["scope_word"] = _TRAIL_SCOPE_RE.search(trail_plain).group(0).lower()
        return None, "narrow_drops_scope", detail
    narrowed = Piece(piece.index, at, end, text[at:end], _plain(text[at:end]), piece.ordinals,
                     piece.inherited, figures_of(text[at:end]), "narrowed")
    detail.update(span=[at, end], piece_span=[piece.start, piece.end],
                  dropped=[[piece.start, at], [end, piece.end]],
                  dropped_figures=list(figures_of(lead) + figures_of(trail)))
    return narrowed, None, detail


# ---------------------------------------------------------------------------
# Judgement and absence TAILS, split in code (S-6)
# ---------------------------------------------------------------------------

#: A head kept after a tail is split off has at least this many words.
MIN_TAIL_HEAD_WORDS = 5
#: A tail split off has at least this many words.
MIN_TAIL_WORDS = 2

#: Where a tail may begin: after a comma, a dash, a semicolon or a colon.
_TAIL_BOUNDARY_RE = re.compile(r"(?:,\s+|\s+[—–-]\s+|\s*[—–]\s*|;\s+|:\s+)")
#: (A) the desk CLASSIFYING what it just reported, by contrast: "…, an
#: internal-security item rather than an interstate one", "— a discussion of
#: support, not a signed transfer".
_CONTRAST_RE = re.compile(
    r"\b(?:rather\s+than|instead\s+of|as\s+opposed\s+to|not\s+(?:a|an|the|any|as)\b|not\s+through\b)",
    re.IGNORECASE,
)
_CONTRAST_LEAD_RE = re.compile(r"^(?:but\s+)?(?:not|rather\s+than|instead\s+of|as\s+opposed\s+to)\b",
                               re.IGNORECASE)
#: (B) an inference or hedge the desk draws: "…, which means …", "…,
#: signalling …", "; this is a diplomatic statement".
_INFERENCE_LEAD_RE = re.compile(
    r"^(?:and\s+|so\s+)?(?:which\s+(?:means|meant|bears|suggests|signals|indicates|points|reflects|"
    r"underscores|highlights|is\s+consistent|would|could|may|might|makes|leaves|keeps|amounts)|"
    r"meaning|suggesting|signall?ing|indicating|pointing\s+to|underscoring|highlighting|reflecting|"
    r"sustaining|consistent\s+with|in\s+line\s+with|a\s+sign\b|a\s+signal\b|likely|unlikely|probably|"
    r"so\b|thus|therefore|hence|this\s+(?:is|was|suggests|signals|indicates|means|reflects|leaves|keeps)|"
    r"that\s+(?:is|suggests|means)|i\.e\.|in\s+effect|effectively|a\s+(?:move|step|measure|development|"
    r"signal|sign|pattern|posture)\s+(?:that|which))",
    re.IGNORECASE,
)
#: (C) an ABSENCE tail: "…, with no new measure in this window", "— and no
#: sanctions … involving Argentina".
_ABSENCE_LEAD_RE = re.compile(
    r"^(?:and\s+|but\s+|with\s+|while\s+|yet\s+)?(?:no|none|nothing|neither|without)(?![\w-])",
    re.IGNORECASE)
#: An absence tail says WHERE nothing was found — the collection, not the
#: world ("… in this window", "none observed", "… appears in this desk's
#: collection"); without such a word a negated tail is not split as one.
_ABSENCE_SCOPE_RE = re.compile(
    r"\b(?:window|slice|collection|desk|signals?|items?|reported|observed|appears?|appearing|recorded|"
    r"collected|this\s+read|cited)\b",
    re.IGNORECASE,
)
#: A contrastive tail after a plain comma must open as an appositive (a
#: determiner) or with the contrast itself — never mid-list ("…, budget and
#: concert items rather than one theme" is a list item, not a tail).
_APPOSITIVE_LEAD_RE = re.compile(
    r"^(?:a|an|the|this|that|these|those|its|their|his|her|one|both|either|neither|not|rather\s+than|"
    r"instead\s+of|as\s+opposed\s+to)\b",
    re.IGNORECASE,
)
#: Attribution or a condition in a tail reaches BACK over the head ("…,
#: officials said", "…, if confirmed"): such a tail is never split off.
_TAIL_REACHES_BACK_RE = re.compile(
    r"\b(?:sa(?:y|ys|id)|claim\w*|alleg\w*|accord\w*|cit(?:e|es|ed|ing)|told|stat(?:e|es|ed)|"
    r"quot\w*|if|unless|whether|unconfirmed|unverified|purport\w*|den(?:y|ied|ies|ial))\b",
    re.IGNORECASE,
)


def _tail_kind(tail_plain: str, *, strong_boundary: bool = True) -> str | None:
    """``assessment`` / ``absence`` / ``None`` for a candidate tail's plain
    text (markers stripped). ``strong_boundary``: the tail follows a dash, a
    semicolon or a colon rather than a plain comma."""
    t = tail_plain.strip()
    if len(t.split()) < MIN_TAIL_WORDS:
        return None
    if _TAIL_REACHES_BACK_RE.search(t):
        return None
    if _ABSENCE_LEAD_RE.match(t):
        return "absence" if _ABSENCE_SCOPE_RE.search(t) else None
    if _INFERENCE_LEAD_RE.match(t):
        return "assessment"
    if _CONTRAST_RE.search(t) and (strong_boundary or _APPOSITIVE_LEAD_RE.match(t)):
        return "assessment"
    return None


def _quantities(text: str) -> bool:
    body = strip_markers(text)
    return bool(find_numbers(body, skip=date_spans(body)))


def _strong(boundary: str) -> bool:
    return not boundary.strip().startswith(",") and bool(boundary.strip())


def _tail_split(text: str, piece: Piece) -> tuple[int, str] | None:
    """Where in ``piece`` a judgement / absence tail begins, and its kind —
    the LATEST boundary whose tail qualifies (the smallest tail), extended
    back one boundary when the tail is a bare contrast hanging on the
    appositive before it ("a discussion of support, not a signed
    transfer")."""
    region = text[piece.start:piece.end]
    bounds = [m for m in _TAIL_BOUNDARY_RE.finditer(region) if m.start() > 0]
    for k in range(len(bounds) - 1, -1, -1):
        m = bounds[k]
        tail = region[m.end():]
        plain = _plain(tail.lstrip(" ,;:—–-"))
        kind = _tail_kind(plain, strong_boundary=_strong(m.group(0)))
        if kind is None:
            continue
        at = m.start()
        if kind == "assessment" and _CONTRAST_LEAD_RE.match(plain) and k > 0:
            prev = bounds[k - 1]
            ext = _plain(region[prev.end():].lstrip(" ,;:—–-"))
            if _APPOSITIVE_LEAD_RE.match(ext) and not _CONTRAST_LEAD_RE.match(ext) \
                    and not _quantities(region[prev.end():m.start()]) \
                    and not figures_of(region[prev.end():m.start()]):
                at = prev.start()
                tail = region[prev.end():]
        head = region[:at]
        if head.count("(") > head.count(")") or head.count("[") > head.count("]"):
            continue                    # never cut inside a parenthesis
        if _words(head) < MIN_TAIL_HEAD_WORDS:
            return None
        # a tail never carries a quantity off: a figure stays where it is checked
        if _quantities(tail):
            return None
        if kind == "assessment" and figures_of(tail):
            return None
        return piece.start + at, kind
    return None


def _renumber(pieces: Sequence[Piece]) -> tuple[Piece, ...]:
    out = []
    for i, p in enumerate(pieces):
        rests = p.rests_on
        out.append(Piece(i, p.start, p.end, p.text, p.plain, p.ordinals, p.inherited, p.figures,
                         p.split_by, p.code_kind, rests))
    return tuple(out)


def split_tails(text: str, pieces: Sequence[Piece]) -> tuple[Piece, ...]:
    """Split, IN CODE, what the structure shows is not a reported fact (S-6):

    * a piece whose last clause is a JUDGEMENT tail — an appositive or
      trailing clause classifying by contrast ("…, an internal item rather
      than an interstate one", "— a discussion, not a transfer") or drawing
      an inference ("…, which means …", "…, signalling …") — becomes the
      statement piece (its head, keeping the piece's citations) and an
      ``assessment`` piece resting on it as a premise;
    * a piece whose last clause states an ABSENCE ("…, with no new measure
      in this window", "— and no sanctions …") becomes the statement piece
      and an ``absence`` piece (mixed fact-plus-absence);
    * a piece with NO citation after the sentence's last citation ("…[14];
      this is a diplomatic statement, not a posture change") is typed by
      code — an absence when it states one, else an assessment resting on
      the piece before it — when it carries no figure; with a figure it stays
      ``uncited`` (a figure is never laundered into a judgement).

    A tail never takes a quantity with it, never one whose words reach back
    over the head (an attribution "…, officials said", a condition "…, if
    confirmed"), and the head keeps at least :data:`MIN_TAIL_HEAD_WORDS`
    words. Code-typed pieces are not shown to the seat for typing; the seat's
    own narrowing remains for everything else."""
    out: list[Piece] = []
    for p in pieces:
        if p.uncited:
            if out and not figures_of(p.text):
                plain = p.plain.lstrip(" ,;:—–-")
                kind = "absence" if _ABSENCE_LEAD_RE.match(plain) and _ABSENCE_SCOPE_RE.search(plain) \
                    else "assessment"
                out.append(Piece(p.index, p.start, p.end, p.text, plain, p.ordinals, p.inherited,
                                 p.figures, "code_tail_uncited", kind,
                                 len(out) - 1 if kind == "assessment" else None))
            else:
                out.append(p)
            continue
        cut = _tail_split(text, p)
        if cut is None:
            out.append(p)
            continue
        at, kind = cut
        head_text, tail_text = text[p.start:at], text[at:p.end]
        head = Piece(p.index, p.start, at, head_text, _plain(head_text), p.ordinals, p.inherited,
                     figures_of(head_text), p.split_by if p.split_by != "none" else "code_tail")
        out.append(head)
        out.append(Piece(p.index, at, p.end, tail_text, _plain(tail_text.lstrip(" ,;:—–-")), p.ordinals, p.inherited,
                         figures_of(tail_text), "code_tail", kind,
                         len(out) - 1 if kind == "assessment" else None))
    return _renumber(out)


__all__ = [
    "MIN_DIGIT_PIECE_WORDS",
    "MIN_TAIL_HEAD_WORDS",
    "MIN_TAIL_WORDS",
    "split_tails",
    "MIN_NARROW_WORDS",
    "MIN_FIGURE_PIECE_WORDS",
    "MIN_PIECE_WORDS",
    "Piece",
    "figures_of",
    "pieces_from_spans",
    "split_sentence",
    "verify_model_split",
    "verify_narrowing",
]
