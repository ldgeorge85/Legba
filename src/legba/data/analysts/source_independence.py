# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE INDEPENDENCE COUNT — how many sources are actually behind a quoted claim.

Program 7 piece 7d. Two sentences, and the whole module is their machinery:

  * **A claim quoted upward with ONE source behind it must say so.** The record
    already prints ``N sources`` on every attribution line, and until this
    module ``N == 1`` was a number a reader had to notice. It is now a MARK:
    ``[single-source]`` renders beside the ordinal, and ``single_source: true``
    rides the citation.
  * **Two outlets carrying the same wire copy are ONE source.** Asharq Al-Awsat
    and CNA ran the same Reuters dispatch 46 times in 30 measured days; before
    this, a claim resting on both counted two.

NO GATE, NO SCORE CHANGE. Nothing here refuses, floors, demotes or re-weights
anything. It is a PUBLISHED MARKER in the aperture-counter tradition: the
record states what it knows about its own sourcing and leaves the judgement to
the reader. ``MIN_INDEPENDENT_SOURCES`` (``edge_qualification``) is the gate
that exists, it sits on relationship reification, and it stays there — a floor
at the composition surface would silently drop the single-sourced claims that
are precisely the ones a reader needs to see flagged.

WHAT COUNTS AS ONE SOURCE
~~~~~~~~~~~~~~~~~~~~~~~~~

The unit of independence is ``signals.source_id`` — the OUTLET ref
("source.bbc.world"), which is already on every unit citation since V-H1. Two
distinct ``source_id`` values fold into one when either holds:

1. **They are the same publisher.** ``source.un_news.africa`` and
   ``source.un_news.peace_security`` are two FEEDS of one newsroom; so are
   ``source.aljazeera.world`` / ``source.aljazeera.arabic`` /
   ``source.rsshub.aljazeera.drcongo``. This is an identity statement, not a
   content judgement, so it folds unconditionally and needs no near-verbatim
   test. Declared in :data:`SAME_PUBLISHER`.
2. **The two items are near-verbatim**, by the test below. The MAP never folds
   on its own — a declared syndication relationship only RELAXES the bar the
   content still has to clear. That asymmetry is the whole safety argument:
   "CNA re-carries Reuters" is true and says nothing whatever about whether
   *this* CNA item and *this* Dawn item are the same dispatch, and a map that
   folded on the relationship alone would erase real, distinct reporting from
   two outlets that happen to share an agency.

THE NEAR-VERBATIM TEST, AND WHY IT IS THIS ONE
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

It is :func:`wire_pair_collapse._normalize_headline` plus
:func:`legba.data._url_canon.normalized_levenshtein` — the two functions the
tree already uses to decide "same dispatch", at the slice renderer and at
dedupe tier 4 respectively. Nothing new is invented here and nothing is
re-implemented: a third opinion about document identity is exactly the drift
this codebase keeps paying for.

``_normalize_headline`` strips agency revision markers ("(LEAD)", "UPDATE 1-"),
NFKC-folds, casefolds and turns punctuation into space — which is what makes
the measured aawsat/CNA pair visible at all. That pair is Title Case against
sentence case with curly quotes against straight ones, so ``signals
.content_hash`` (canonical URL + ``normalize_wire_title``, no case fold) cannot
see it and never will: the URLs differ because the publishers differ.

The same normalizer runs over the BODY, because the brief for this piece is
about bodies and because a masthead rewrites a wire headline far more often
than it rewrites the lede. Bodies are compared over a bounded LEDE window
(:data:`BODY_COMPARE_CHARS`) rather than in full: citations carry different
capture widths (``snippet`` is ~the analyst's working text, ``source_text`` the
raw article to :data:`~legba.data.analysts.inline_target._SOURCE_TEXT_CHARS`),
so a full-length comparison would measure the capture and not the copy.

FOUR PRECISION GUARDS, all resolving ties toward NOT folding — a miss costs a
marker; a false fold erases a real second source, which is strictly worse:

  * **Different ``source_id``, or no fold.** Two items from ONE outlet are one
    source already; folding them changes nothing and testing them only invents
    ways to be wrong. This is also the guard that retires the GDACS class
    ``wire_pair_collapse`` measured — nine distinct wildfires sharing one
    auto-generated headline, all from ``gdacs.org``.
  * **A token floor on headlines** (``wire_pair_collapse._MIN_KEY_TOKENS``):
    "Morning briefing" carries no identity worth trusting.
  * **A character floor on bodies** (:data:`BODY_MIN_CHARS`): a two-line stub
    matches every other two-line stub.
  * **An exact length prefilter.** Normalized Levenshtein is bounded below by
    ``abs(la - lb) / max(la, lb)``, so a pair whose lengths differ by more than
    the bar cannot clear it. That is a proof, not a heuristic — it prunes most
    pairs at zero cost and the answer is unchanged.

And ONE thing that is deliberately not a guard but a cost control, named as
such: :func:`_token_overlap` screens a body pair before the O(n·m) walk runs.
It is a heuristic, it can cost a fold, and it cannot invent one — which is why
it is allowed to exist at all. See its own docstring for why the exact bound
does not prune where the bodies are concerned.

WHERE IT IS READ
~~~~~~~~~~~~~~~~

Both readers are handed the SAME list — the origin head's own
``data.data.citations`` via :func:`head_citations` — so the two surfaces cannot
disagree about a block:

  * ``assembly_payload._signals_block`` stamps the count onto
    ``blocks[].corroboration``, and ``assembly_render._attribution`` renders it.
    The body is rendered FROM the payload, never authored beside it.
  * ``composition_citations._build_composition_citation`` stamps
    ``single_source`` / ``wire_folded`` onto the citation, and the ``cite``
    receipt counts ``claims_single_source`` / ``claims_wire_folded``.

WHAT ABSENCE MEANS, said out loud. A composition block whose origin cites
FINDINGS (a world read carrying country reads) has no ``source_id`` anywhere in
its citations. That is UNKNOWN, not single-sourced, and :attr:`Independence
.known` is False there — no mark, no counter, no key. This mirrors
``assembly_render._attribution``'s existing refusal to print "0 sources" beside
a real read, and for the same reason: a field whose name is false is worse than
a field that is missing.

THE MAP LIVES IN TWO PLACES ON PURPOSE
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:data:`SYNDICATION` / :data:`SAME_PUBLISHER` are declared BOTH here and in
``descriptors/wire_map.yaml``, and ``tests/data_pkg/
test_source_independence.py`` pins them equal. The descriptor is the document
an operator reads and edits; this module is what RUNS, because
``descriptors/`` is in neither container image and is not volume-mounted (see
``scripts/gen_descriptor_prompt_manifest.py``'s own note) — a tree read here
would work in tests and return nothing in production, which is the worst
failure shape available for a map whose absence is invisible. Editing one side
without the other turns the pin red.

THE ROWS IT SHIPS WITH are the ones the corpus proves, measured read-only over
30 days on the live substrate (2026-09-23: verbatim normalized headline, same
publication day, distinct outlets). The top pairs, with their counts:
aawsat/CNA 46, hindustantimes/AP 32, africanews/euronews 28, aljazeera
world/drcongo 24, aawsat/AP 17, npr/AP 15, CNA/dawn 13, CNA/jpost 11,
CNA/dailymaverick 11, CNA/AP 10. Sampling those pairs by hand showed Reuters
copy under two mastheads, AP copy under two mastheads, one newsroom under two
feeds — and nothing else. The map states exactly that and no more.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .._url_canon import normalized_levenshtein
from .wire_pair_collapse import _MIN_KEY_TOKENS, _normalize_headline

__all__ = [
    "BODY_COMPARE_CHARS",
    "BODY_MAX_DISTANCE",
    "BODY_MAX_DISTANCE_DECLARED",
    "BODY_MIN_CHARS",
    "BODY_TOKEN_OVERLAP_FLOOR",
    "HEADLINE_MAX_DISTANCE",
    "HEADLINE_MAX_DISTANCE_DECLARED",
    "Independence",
    "MAX_CITED_ROWS",
    "SAME_PUBLISHER",
    "SINGLE_SOURCE_MARKER",
    "SYNDICATION",
    "WIRE_FOLDED_LABEL",
    "WIRE_MAP_DESCRIPTOR",
    "WIRE_MAP_VERSION",
    "corroboration_block",
    "head_citations",
    "independence_of",
    "publisher_of",
    "shared_wire",
]


#: The operator-facing twin of :data:`SYNDICATION` / :data:`SAME_PUBLISHER`,
#: pinned equal by the test named in the banner. Stated as a path rather than
#: read as one, deliberately — see "THE MAP LIVES IN TWO PLACES ON PURPOSE".
WIRE_MAP_DESCRIPTOR: str = "descriptors/wire_map.yaml"

#: Bumped when the map's SHAPE changes (a new section, a changed key meaning),
#: never when a row is added — a reader comparing two runs needs to know
#: whether the rules moved, and the rows are visible in the diff either way.
WIRE_MAP_VERSION: str = "wire_map.v1"

#: ``source_id`` → the wire services / shared newsrooms that outlet is known to
#: RE-CARRY. An outlet carries several agencies AND its own original reporting,
#: so a row here is never "this outlet IS Reuters"; it is "a Reuters dispatch
#: can appear under this masthead". Folding still requires the near-verbatim
#: test — this only relaxes its bar. Keep in step with the descriptor.
SYNDICATION: Mapping[str, tuple[str, ...]] = {
    # --- the agencies' own feeds ---
    "source.rsshub.apnews.world": ("ap",),
    "source.rsshub.apnews.drcongo": ("ap",),
    "source.rsshub.apnews.haiti": ("ap",),
    "source.rsshub.apnews.niger": ("ap",),
    "source.rsshub.apnews.north_korea": ("ap",),
    "source.rsshub.apnews.taiwan": ("ap",),
    # --- mastheads measured re-carrying agency copy (30 d, 2026-09-23) ---
    "source.cna.all": ("reuters", "ap"),
    "source.aawsat.english": ("reuters", "ap"),
    "source.hindustantimes.world": ("ap", "reuters"),
    "source.npr.world": ("ap",),
    "source.dawn.home": ("reuters",),
    "source.jpost.frontpage": ("reuters",),
    "source.dailymaverick.news": ("reuters",),
    "source.lemonde.english": ("reuters", "afp"),
    "source.france24.english": ("afp", "reuters"),
    "source.japantimes.news": ("reuters",),
    "source.gcaptain.all": ("reuters",),
    # --- one newsroom, two mastheads (Euronews Group) ---
    # NOT in SAME_PUBLISHER: africanews and euronews are distinct mastheads
    # with distinct desks that republish each other. The relationship is real
    # (28 verbatim-headline days in 30) and the content test still has to pass.
    "source.euronews.news": ("euronews_group",),
    "source.africanews.all": ("euronews_group",),
}

#: publisher key → the ``source_id`` values that are FEEDS OF ONE OUTLET. This
#: is an identity statement, so it folds with no content test: a claim resting
#: on two UN News feeds rests on the UN News desk, once.
SAME_PUBLISHER: Mapping[str, tuple[str, ...]] = {
    "aljazeera": (
        "source.aljazeera.world",
        "source.aljazeera.arabic",
        "source.rsshub.aljazeera.drcongo",
    ),
    "ap": (
        "source.rsshub.apnews.world",
        "source.rsshub.apnews.drcongo",
        "source.rsshub.apnews.haiti",
        "source.rsshub.apnews.niger",
        "source.rsshub.apnews.north_korea",
        "source.rsshub.apnews.taiwan",
    ),
    "un_news": (
        "source.un_news.africa",
        "source.un_news.middle_east",
        "source.un_news.peace_security",
    ),
}

_PUBLISHER_OF: dict[str, str] = {
    sid: publisher
    for publisher, members in SAME_PUBLISHER.items()
    for sid in members
}

#: The inline mark, rendered beside the ordinal on the attribution line. A
#: member of ``assembly_render.CONNECTIVES`` — it describes the READ (what the
#: record rests on), never the world.
SINGLE_SOURCE_MARKER: str = "[single-source]"

#: The tail item that EXPLAINS a folded count, so the sources number never
#: moves without saying why. Also a member of ``assembly_render.CONNECTIVES``.
WIRE_FOLDED_LABEL: str = "wire-folded"

#: Headlines: an UNDECLARED pair must normalize to the SAME string (0.0). Two
#: outlets with no known relationship writing byte-identical normalized
#: headlines on one story is syndication essentially always; anything looser
#: starts folding two desks that covered one event.
HEADLINE_MAX_DISTANCE: float = 0.0

#: Headlines, DECLARED pair: a masthead trims a wire headline to its column
#: width. 0.15 of ~70 normalized characters is about ten — a trim, not a
#: rewrite.
HEADLINE_MAX_DISTANCE_DECLARED: float = 0.15

#: Bodies: a tenth of the lede window differing is a dateline swap
#: ("CAIRO (Reuters) -") on one copy and not the other.
BODY_MAX_DISTANCE: float = 0.10

#: Bodies, DECLARED pair: a quarter leaves room for a dateline AND a house
#: intro sentence, which is what the measured re-carries actually do.
BODY_MAX_DISTANCE_DECLARED: float = 0.25

#: How much of each normalized body the comparison sees. Bounded because the
#: capture widths differ between citation shapes (see the banner) and because
#: the DP is O(n·m): 160 keeps a pair under ~2 ms.
BODY_COMPARE_CHARS: int = 160

#: Below this a normalized body is a stub, and every stub matches every other.
BODY_MIN_CHARS: int = 80

#: The token-set Jaccard a body pair must clear before the DP runs at all. A
#: SCREEN, not a bound — see :func:`_token_overlap`. 0.6 sits well under what
#: two copies of one dispatch share (a dateline and a house intro leave the
#: rest verbatim) and well over what two different stories share.
BODY_TOKEN_OVERLAP_FLOOR: float = 0.6

#: A head cites at most a few dozen signals; this bounds the pair walk at
#: ``MAX_CITED_ROWS²/2`` in the pathological case rather than at whatever a
#: future producer decides to hand over. Rows past the bound are still counted
#: as sources — they simply do not participate in folding, which can only ever
#: OVER-count independence (the safe direction).
MAX_CITED_ROWS: int = 40


@dataclass(frozen=True)
class Independence:
    """What the record knows about the sourcing behind ONE quoted claim.

    ``cited`` counts the cited signals that carried a resolvable ``source_id``;
    ``sources`` the distinct outlets among them; ``independent`` the outlets
    left after folding. ``cited == 0`` is the UNKNOWN state — a composition
    origin cites findings, not signals — and every consumer must read
    :attr:`known` before it reads a number.
    """

    cited: int = 0
    sources: int = 0
    independent: int = 0

    @property
    def known(self) -> bool:
        """True when at least one cited signal named an outlet."""
        return self.cited > 0

    @property
    def folded(self) -> int:
        """Outlets absorbed by the wire/publisher fold (0 when nothing folded)."""
        return max(0, self.sources - self.independent)

    @property
    def single_source(self) -> bool:
        """The claim rests on ONE source — after folding, and only when known."""
        return self.known and self.independent == 1


def publisher_of(source_id: Any) -> str | None:
    """The publisher key two feeds of one outlet share, or ``None``."""
    return _PUBLISHER_OF.get(str(source_id or ""))


def shared_wire(a: Any, b: Any) -> bool:
    """True when the map declares a wire service BOTH outlets re-carry.

    A relaxation of the content bar and nothing else — see the banner.
    """
    wires_a = SYNDICATION.get(str(a or ""))
    wires_b = SYNDICATION.get(str(b or ""))
    if not wires_a or not wires_b:
        return False
    return bool(set(wires_a) & set(wires_b))


def head_citations(row: Mapping[str, Any] | Any) -> list[Mapping[str, Any]]:
    """An origin head's own ``data.data.citations`` — the cited SIGNALS.

    Total: any shape that is not the two-level envelope yields ``[]``, so a
    caller reading a hand-built row, a legacy row or a malformed one gets the
    UNKNOWN state rather than an exception. The one reader of this shape used
    to live in ``assembly_payload``; it moved here when the independence count
    needed the identical list from the citation side.
    """
    if not isinstance(row, Mapping):
        return []
    env = row.get("data")
    if not isinstance(env, Mapping):
        return []
    inner = env.get("data")
    if not isinstance(inner, Mapping):
        return []
    cits = inner.get("citations")
    if not isinstance(cits, list):
        return []
    return [c for c in cits if isinstance(c, Mapping)]


def _body_text(citation: Mapping[str, Any]) -> str:
    """The cited item's body, in the precedence the citation shapes offer it.

    ``source_text`` is the RAW authoritative article (the faithfulness trust
    boundary's own field) and leads for that reason; ``snippet`` is the
    analyst's working text; ``evidence_text`` is what the composition-tier
    citation shape carries. First non-empty wins.
    """
    for key in ("source_text", "snippet", "evidence_text"):
        val = citation.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _near_verbatim(
    a_head: str, a_body: str, b_head: str, b_body: str, *, declared: bool
) -> bool:
    """Are these two cited items the SAME dispatch? Headline first, then lede."""
    head_bar = (
        HEADLINE_MAX_DISTANCE_DECLARED if declared else HEADLINE_MAX_DISTANCE
    )
    if (
        a_head
        and b_head
        and len(a_head.split()) >= _MIN_KEY_TOKENS
        and len(b_head.split()) >= _MIN_KEY_TOKENS
        and _within(a_head, b_head, head_bar)
    ):
        return True
    body_bar = BODY_MAX_DISTANCE_DECLARED if declared else BODY_MAX_DISTANCE
    return (
        len(a_body) >= BODY_MIN_CHARS
        and len(b_body) >= BODY_MIN_CHARS
        and _token_overlap(a_body, b_body) >= BODY_TOKEN_OVERLAP_FLOOR
        and _within(a_body, b_body, body_bar)
    )


def _token_overlap(a: str, b: str) -> float:
    """Jaccard over the two ledes' token sets — the SCREEN before the DP.

    THIS ONE IS NOT A BOUND, and the distinction matters enough to name. The
    length prefilter inside :func:`_within` is a proof and cannot change an
    answer; this is a cheap heuristic that decides which pairs are worth an
    O(n·m) comparison at all, and it can therefore cost a FOLD. It cannot ever
    invent one — a pair it screens out is counted as two independent sources,
    which is the direction every guard in this module resolves toward.

    It is here because the DP is the only expensive thing in the file and the
    exact bound does not prune where it needs to: both ledes arrive truncated
    to :data:`BODY_COMPARE_CHARS`, so unrelated bodies have IDENTICAL lengths
    and sail through the length test into a 160x160 walk. Two copies of one
    dispatch share nearly every token; two different stories share stopwords.
    """
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _within(a: str, b: str, bar: float) -> bool:
    """``normalized_levenshtein(a, b) <= bar``, with the exact length prefilter.

    ``normalized_levenshtein`` is bounded below by ``abs(la - lb) / max(la,
    lb)`` — a deletion is a deletion — so a pair outside that band cannot clear
    the bar and is refused without running the DP. The answer is identical; the
    walk is not.
    """
    if a == b:
        return True
    # A zero bar IS equality — ``normalized_levenshtein(a, b) <= 0`` holds only
    # for identical strings — so the undeclared headline test never walks the
    # DP at all. Exact, not an approximation.
    if bar <= 0.0:
        return False
    longest = max(len(a), len(b))
    if longest == 0:
        return False
    if abs(len(a) - len(b)) / longest > bar:
        return False
    return normalized_levenshtein(a, b) <= bar


class _Folds:
    """Union-find over ``source_id`` — the outlets, not the items.

    Folding is transitive by construction and that is correct: if A and B ran
    one dispatch and B and C ran another, the claim still rests on one voice
    reaching three mastheads.
    """

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def add(self, key: str) -> None:
        self._parent.setdefault(key, key)

    def find(self, key: str) -> str:
        root = key
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[key] != root:
            self._parent[key], key = root, self._parent[key]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb

    def groups(self) -> int:
        return len({self.find(k) for k in self._parent})


def independence_of(
    citations: Sequence[Mapping[str, Any]] | None,
) -> Independence:
    """The independence count behind a claim resting on ``citations``.

    ``citations`` is a head's own cited-signal list (``head_citations``) — the
    WHOLE list, unfiltered: the filter below is the definition of the unit and
    lives here so every caller gets the same one. The return is total: an
    empty, malformed or source-less list yields the UNKNOWN ``Independence()``,
    never an exception and never a fabricated count.
    """
    rows: list[tuple[str, str, str]] = []
    sources: set[str] = set()
    cited = 0
    for entry in citations or ():
        if not isinstance(entry, Mapping):
            continue
        # A cited SIGNAL with a named OUTLET, and nothing else. Both halves are
        # load-bearing: the ``signal_id`` filter makes this denominator the one
        # ``assembly_payload._signals_block`` renders from (so the payload and
        # the citation cannot disagree about a block), and the ``source_id``
        # filter is what UNKNOWN means — a grounding block or a composition
        # origin names no outlet and must not be counted as one.
        sid = entry.get("source_id")
        if not sid or not entry.get("signal_id"):
            continue
        key = str(sid)
        cited += 1
        sources.add(key)
        if len(rows) < MAX_CITED_ROWS:
            rows.append((
                key,
                _normalize_headline(entry.get("title")),
                _normalize_headline(_body_text(entry))[:BODY_COMPARE_CHARS],
            ))
    if not sources:
        return Independence()

    folds = _Folds()
    for key in sources:
        folds.add(key)
    # (1) SAME PUBLISHER — an identity statement; no content test.
    by_publisher: dict[str, str] = {}
    for key in sorted(sources):
        publisher = publisher_of(key)
        if publisher is None:
            continue
        first = by_publisher.setdefault(publisher, key)
        folds.union(first, key)
    # (2) NEAR-VERBATIM — every cross-outlet pair whose outlets are not already
    # folded together. The `find` guard is what makes the common slice cheap:
    # each successful fold removes every remaining pair between the two groups.
    for i in range(len(rows)):
        a_src, a_head, a_body = rows[i]
        for j in range(i + 1, len(rows)):
            b_src, b_head, b_body = rows[j]
            if a_src == b_src or folds.find(a_src) == folds.find(b_src):
                continue
            if _near_verbatim(
                a_head, a_body, b_head, b_body,
                declared=shared_wire(a_src, b_src),
            ):
                folds.union(a_src, b_src)
    return Independence(
        cited=cited, sources=len(sources), independent=folds.groups()
    )


def corroboration_block(
    citations: Sequence[Mapping[str, Any]] | None, *, n_desks_sharing: int
) -> dict[str, Any]:
    """``blocks[].corroboration`` — the two historical keys, then the fold.

    ``n_sources`` keeps its meaning (DISTINCT outlets) and its position, and
    the fold keys are written only when they say something: a block with no
    fold and no single-source claim serialises byte-for-byte as it always has.
    """
    ind = independence_of(citations)
    out: dict[str, Any] = {
        "n_sources": ind.sources,
        "n_desks_sharing": n_desks_sharing,
    }
    if ind.folded:
        out["n_independent_sources"] = ind.independent
        out["wire_folded"] = ind.folded
    if ind.single_source:
        out["single_source"] = True
    return out
