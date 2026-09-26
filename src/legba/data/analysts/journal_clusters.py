# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""T2.2 (JOURNAL_CONNECTIVE_AUDIT_PROPOSAL_2026-09-09 §3 P5 / §6) — CLUSTER-FIRST
selection for the journal's priming window, plus the context-only desk roster.

THE DEFECT THIS ADDRESSES, in the audit's own words (§1.4): *"A cluster of many
moderate-magnitude rows about one event is fragmented by that sort; the voice got
Burnham and Turkey as two fragments and never the object."* The journal's window
is ``journal_slice._select_journal_slice`` — a pure (magnitude, authority) sort
with a freshness floor over an already per-source-diversity-capped pool. That
sort is right about CONSEQUENCE and blind to IDENTITY: twenty-three rows that are
all one event compete against each other for window slots at their individual
magnitudes, so the biggest STORY of a window can lose to a handful of unrelated
higher-magnitude singles and reach the narrator only as debris.

WHAT THIS MODULE ADDS. A second axis, behind ``LEGBA_JOURNAL_CLUSTER_FIRST``
(default OFF):

  1. **Grouping** — every candidate row is keyed by the POLITIES ITS PROSE NAMES,
     read with R1-a's own matcher (``_frame_anchor.PolitySurfaceIndex`` over
     ``candidate_polities()``, the class-gated canon). Two rules, and the choice
     between them is per-row rather than a graph walk (see GROUPING below).
  2. **Ranking** — ``mass = member count × distinct source count``.
  3. **Window fill** — the top ``_JOURNAL_CLUSTER_N`` clusters each contribute a
     LEAD row plus up to ``_JOURNAL_CLUSTER_K`` members, source-diverse; the
     remaining slots are filled by the EXISTING magnitude tail, unchanged, with
     the freshness floor preserved at its full reserve.
  4. **Render** — the lead row carries a header line so the narrator sees the
     OBJECT ("▸ Israel–United Kingdom · settlements — 23 rows · 6 sources ·
     lead: …") with its fragments attached under it. Single rows are untouched.
  5. **Roster** — a trailing, explicitly NON-CITABLE "Desks today" block read off
     the newest world read's ``## Coverage`` / ``## Not carried`` sections, so
     the voice can say what the desks carried and what they dropped.

GROUPING — the rule, and why this one. The task offered "shared >=2 anchors, or
1 anchor + same geo". Both are taken, but as a PER-ROW KEY rather than as an
edge predicate over a graph, because the graph form chains: A shares
{Israel, United Kingdom} with B, B shares {Israel, United States} with C, and the
transitive closure swallows three stories into one component. A key assigns each
row to exactly one cluster, is O(n), is order-independent, and is what a test can
pin. The key is built from the row's own surviving anchors:

  * >=2 anchors survive -> key = THE PAIR THE POOL SUPPORTS MOST. A row naming
    three polities offers three pairs, and it joins the pair the most other rows
    also name — so "US lawmaker lashes out at UK over reported plans to ban trade
    with Israeli settlements" ({Israel, United Kingdom, United States}) lands
    with the forty other UK/Israel settlement rows rather than with everything
    else that mentions Washington. Ties break to the RARER pair (a rare
    co-naming carries more information than a common one), then alphabetically:
    a total order, so the answer never depends on row order.
  * exactly 1 anchor survives -> key = (that anchor, the row's geo tags). The geo
    tag is the second axis the task named, and it is what keeps
    ``SETTLEMENT <-> ATTORNEY GENERAL: protest in Wisconsin`` (one anchor,
    ``{US}``) out of the UK/Israel settlements cluster it shares a word with.
  * 0 anchors survive -> NO cluster. The row keeps today's behaviour exactly: it
    competes in the magnitude tail as a single row.

THE UBIQUITY CEILING is why "surviving" is not "named". Over a live 24 h pool
``United States`` is named by a large share of all rows, so an anchor that
ubiquitous is a fact about the pool and not about a story: without the ceiling
every US-only row lands in one enormous ``(United States, {US})`` bucket whose
mass wins the ranking and whose members share nothing. An anchor naming
``_JOURNAL_CLUSTER_MAX_UBIQUITY`` or more of the pool is dropped from the KEY
(never from the row — the row is still selected the way it always was). Measured
on the two live windows in ``planning/JOURNAL_CLUSTER_FIRST_SLICE_REPORT.md``.

FLAG OFF is byte-identical and structurally so, not merely asserted: nothing here
runs. ``journal_slice._journal_window`` calls ``_select_journal_slice`` directly,
no anchor index is built, no header is stamped, and ``_render_user_prompt``
appends neither preamble nor roster.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

#: The env flag. Default OFF — the orchestrator flips it after the replay reads
#: clean (the reversal is one env var, exactly as the proposal's §4 names it).
CLUSTER_FIRST_ENV = "LEGBA_JOURNAL_CLUSTER_FIRST"

#: How many clusters lead the window. SIX, and the number is a budget rather than
#: a taste: the window is 60 rows with 12 reserved for the freshness floor, so
#: 6 clusters x (1 lead + 3 members) = 24 rows = 40% of the window and exactly
#: half of the 48 non-floor slots. The magnitude tail — the thing that is right
#: today — keeps the majority of the window, and the floor keeps its full
#: reserve. Measured against the live pools: on both replayed windows the mass
#: distribution falls off sharply after the top handful, so a larger N buys
#: near-noise clusters at the price of the consequence-first lead.
_JOURNAL_CLUSTER_N = 6

#: Members a cluster contributes BESIDES its lead. THREE: a lead plus three
#: fragments is enough for the voice to see an object with corroboration
#: (distinct sources, distinct angles) without any one story taking a fifth of
#: the window. Members are picked source-diverse, so three members is three
#: sources whenever the cluster has them.
_JOURNAL_CLUSTER_K = 3

#: A "cluster" of one is a single row, which the magnitude tail already handles.
_JOURNAL_CLUSTER_MIN_MEMBERS = 2

#: An anchor naming this share (or more) of the candidate pool is dropped from
#: the KEY — it is a fact about the pool, not about a story. R1-a's own D-t
#: clause, retuned for a signal pool (its 0.75 is a per-desk finding bar).
_JOURNAL_CLUSTER_MAX_UBIQUITY = 0.20

#: …but never below this many ROWS. A share alone is meaningless on a small
#: pool — over three rows the 20% ceiling is 0.6, so every anchor is "ubiquitous"
#: and nothing can ever cluster. The floor is ``_JOURNAL_FRESH_RESERVE``'s twelve
#: and shares its logic: an anchor named by fewer rows than the freshness reserve
#: cannot saturate a 60-row window, so there is nothing for the ceiling to
#: protect against. On the live 120-row ``inputs`` the share wins (24 > 12) and
#: this floor changes nothing; on a unit-test pool of three rows it is the whole
#: reason the bar behaves.
_JOURNAL_CLUSTER_UBIQUITY_FLOOR_ROWS = 12

#: Body characters folded into a row's prose alongside its title. Bounded the way
#: ``_frame_anchor.finding_prose`` bounds a finding body, and for the same
#: reason: the answer must not depend on which read produced the row.
_JOURNAL_CLUSTER_BODY_CHARS = 600

#: Cap on the header's rendered lead title — the row lines use 200.
_HEADER_TITLE_CHARS = 140

#: The key the lead row carries so the render loops can print the header without
#: knowing anything about clustering.
CLUSTER_HEADER_KEY = "journal_cluster_header"


def cluster_first_enabled() -> bool:
    """``LEGBA_JOURNAL_CLUSTER_FIRST`` — default OFF, house 0/1 boolean shape."""
    raw = os.getenv(CLUSTER_FIRST_ENV)
    return bool(raw) and raw.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Row prose + geo
# ---------------------------------------------------------------------------


def _payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("data", "payload"):
        v = row.get(key)
        if isinstance(v, Mapping):
            return v
    return {}


def _row_title(row: Mapping[str, Any]) -> str:
    """The row's reader-facing title — the SAME preference order
    ``journal_assessor._row_title`` uses (stored English translation first), so
    a cluster is grouped over the text the narrator is actually shown."""
    payload = _payload(row)
    for cand in (payload.get("title_en"), row.get("title"), payload.get("title")):
        if isinstance(cand, str) and cand.strip():
            return cand.strip()
    return ""


def _row_prose(row: Mapping[str, Any]) -> str:
    """Title || bounded body — the substrate the anchor matcher reads."""
    from .._frame_anchor import PROSE_JOIN

    payload = _payload(row)
    body = ""
    for cand in (payload.get("summary"), payload.get("raw_body"),
                 payload.get("text")):
        if isinstance(cand, str) and cand.strip():
            body = cand.strip()[:_JOURNAL_CLUSTER_BODY_CHARS]
            break
    return f"{_row_title(row)}{PROSE_JOIN}{body}"


def _row_geo_key(row: Mapping[str, Any]) -> str:
    """The row's geo tags as one canonical, order-independent string."""
    geo = row.get("geo")
    if isinstance(geo, str):
        geo = [geo]
    if not isinstance(geo, (list, tuple, set, frozenset)):
        return ""
    return ",".join(sorted({str(g) for g in geo if g}))


_INDEX: Any = None
#: polity -> (single-word folded surfaces, multi-word folded surfaces)
_STRICT: dict[str, tuple[frozenset[str], tuple[str, ...]]] = {}


def _surface_index() -> Any:
    """R1-a's batched matcher over the class-gated polity canon, built once.

    ``candidate_polities()`` runs the canon's class gate (which is what
    structurally refuses ``United Nations`` / ``NATO`` rather than stoplisting
    them) and ``PolitySurfaceIndex`` folds every surface once. Built lazily so a
    flag-off run never pays for it. The per-polity surface split beside it is
    what :func:`row_anchors` confirms a hit against — see there for why."""
    global _INDEX
    if _INDEX is None:
        from .._frame_anchor import PolitySurfaceIndex, candidate_polities
        from .._polity_match import entity_surfaces, normalize_prose

        names = candidate_polities()
        index = PolitySurfaceIndex(names)
        for name in names:
            singles: set[str] = set()
            phrases: list[str] = []
            for surface in entity_surfaces(name):
                folded = normalize_prose(surface)
                if not folded:
                    continue
                if " " in folded:
                    phrases.append(folded)
                else:
                    singles.add(folded)
            _STRICT[name] = (frozenset(singles), tuple(phrases))
        _INDEX = index
    return _INDEX


def row_anchors(row: Mapping[str, Any]) -> frozenset[str]:
    """Every canonical polity this row's prose NAMES.

    R1-a's ``PolitySurfaceIndex`` is the PREFILTER — one fold of the prose,
    whatever the candidate count — and then each hit is CONFIRMED against a
    whole-token or whole-phrase match. The confirmation is not belt-and-braces;
    it is the difference between a working bar and a broken one on this
    substrate.

    ``PolitySurfaceIndex`` WAS "deliberately GENEROUS" in a way that broke here:
    one leg of that generosity was a squeezed-substring test, and ``United
    States`` carries the surface ``u.s``, which folds to the two-token ``u s``
    and squeezes to ``us`` — a substring of *russia*, *australia*, *focus*,
    *because*, *industry*. Over the live 2026-09-09 24 h pool that leg put
    ``United States`` on 1,868 of 3,570 rows (52.3%); with the confirmation, 877
    (24.6%). On a desk's FINDING bodies the noise is diluted by long authored
    prose; on a one-line signal title it decides the key.

    AMENDMENT 7g FIXED IT AT THE SOURCE — both multi-word legs of the shared
    matcher now respect token boundaries, so the index no longer makes the hits
    this function was subtracting. Measured over the live 72 h pool
    (2026-09-09/10): index hits 6,423, after confirmation 6,423, **zero rows
    differ**. The confirmation is kept because it is not quite the same rule —
    it still declines the index's run-together squeezed leg ("SouthAfrica" as
    one token), which no live row exercises — and because a prefilter this
    module owns is cheaper to reason about than a promise about a shared one.
    Retiring it is the journal lane's call, not this repair's.
    """
    from .._polity_match import normalize_prose

    folded = normalize_prose(_row_prose(row))
    hits = _surface_index().names_in_folded(folded)
    if not hits:
        return frozenset()
    tokens = set(folded.split())
    # The same plural relaxation the index itself applies ("americans" names
    # America), kept so the confirmation never refuses a hit the index made for
    # a reason this module agrees with.
    stems = {t[:-1] for t in tokens if t.endswith("s")}
    keep = set()
    for name in hits:
        singles, phrases = _STRICT.get(name, (frozenset(), ()))
        if (tokens & singles) or (stems & singles):
            keep.add(name)
        elif any(phrase in folded for phrase in phrases):
            keep.add(name)
    return frozenset(keep)


# ---------------------------------------------------------------------------
# The cluster
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SliceCluster:
    """One story the candidate pool contains, and everything the fill needs."""

    #: Deterministic identity — ``anchors|A|B`` or ``geo|A|GB,IL``.
    key: str
    #: The canonical polities the key was built from, alphabetical.
    anchors: tuple[str, ...]
    #: The geo string, for a one-anchor key; ``""`` otherwise.
    geo: str
    #: Members, salience-ordered (stable, so ties keep the delivered order).
    members: tuple[dict[str, Any], ...]
    #: ``len(members) x distinct source_id count`` — THE ranking number.
    mass: int
    #: Distinct ``source_id`` across the members.
    sources: int
    #: The highest member magnitude (the ranking tiebreak).
    max_magnitude: float
    #: The content terms at least two members' titles share — computed once, at
    #: construction, because they are BOTH the label and (for a one-anchor key)
    #: the admission test.
    terms: tuple[str, ...] = ()

    @property
    def lead(self) -> dict[str, Any]:
        """The highest (magnitude, authority) member — the cluster's face.

        Always a TITLED row: ``cluster_pool`` never admits a titleless one,
        because the render loops skip a row with no title, so a titleless lead
        would print a header over rows the narrator cannot read and a member
        count that counts rows nobody sees."""
        return self.members[0]

    def label(self) -> str:
        """The object's name: the anchor pair, plus the terms its members share."""
        anchor_part = "-".join(self.anchors)
        if self.geo:
            # "geo" spelled out: a bare "Iran · US" reads as a second polity,
            # which is the one thing the label must not say.
            anchor_part = f"{anchor_part} · geo {self.geo}"
        return (
            f"{anchor_part} · {', '.join(self.terms)}" if self.terms
            else anchor_part
        )

    def header(self) -> str:
        """The rendered window header line."""
        title = _row_title(self.lead)[:_HEADER_TITLE_CHARS]
        return (
            f"▸ {self.label()} — {len(self.members)} rows · "
            f"{self.sources} sources · lead: {title}"
        )


#: Tokens that carry no story. Deliberately SHORT — the >=4-character floor and
#: the >=half-the-members frequency bar do most of the work, and a long curated
#: stoplist is the kind of thing that rots.
_LABEL_STOPWORDS = frozenset({
    "about", "after", "against", "amid", "another", "back", "been", "before",
    "being",
    "could", "days", "does", "down", "during", "first", "from", "have", "into",
    "less", "like", "make", "many", "more", "most", "much", "must", "near",
    "need", "news", "next", "note", "only", "other", "over", "part", "past",
    "president", "report", "reports", "said", "says", "several", "since",
    "some", "state", "still", "such", "take", "than", "that", "their", "them",
    "then", "there", "these", "they", "this", "those", "three", "through",
    "time", "under", "until", "very", "week", "were", "what", "when", "where",
    "which", "while", "will", "with", "within", "without", "would", "year",
    "years",
})


def _label_terms(
    members: Sequence[Mapping[str, Any]], anchors: Sequence[str]
) -> list[str]:
    """Up to two content terms at least HALF the members' titles share — and
    never fewer than TWO members, whatever half is.

    The floor is ``max(2, ceil(n/2))`` and the ``2`` is load-bearing. Without it
    a two-member cluster's "shared" term is a term in ONE of them, which is not
    shared at all: that is how "masked anti-migrant protesters in the UK" and
    "air traffic control issue disrupts flights at major UK airports" acquired
    the joint label "about, airports" on the live 2026-09-09 window. Since a
    one-anchor key is ADMITTED on having terms, the floor is also the guard that
    keeps two unrelated same-country rows from being announced as one object.

    Excludes the anchors' own surfaces — a cluster keyed on Israel and the
    United Kingdom must not be labelled "israel, britain", which says only what
    the key already said."""
    from .._polity_match import entity_surfaces, normalize_prose

    banned: set[str] = set()
    for name in anchors:
        for surface in entity_surfaces(name):
            banned |= set(normalize_prose(surface).split())
    df: dict[str, int] = {}
    for row in members:
        for token in set(normalize_prose(_row_title(row)).split()):
            if len(token) < 4 or token.isdigit():
                continue
            if token in _LABEL_STOPWORDS or token in banned:
                continue
            df[token] = df.get(token, 0) + 1
    floor = max(2, (len(members) + 1) // 2)
    ranked = sorted(
        (t for t, n in df.items() if n >= floor), key=lambda t: (-df[t], t),
    )
    return ranked[:2]


def cluster_pool(rows: Sequence[Mapping[str, Any]]) -> list[SliceCluster]:
    """Group the candidate pool into stories, ranked by mass. PURE and DB-free.

    Ranking: ``mass`` DESC, then ``max_magnitude`` DESC, then ``key`` ASC — a
    total order over the pool, so the same pool always yields the same window."""
    from .._polity_match import normalize_prose
    from .signal_salience import magnitude_of, salience_sort_key

    # A row the render loops SKIP (no title) is a row the narrator never sees,
    # so it is neither a member nor a lead: counting it would put invisible rows
    # in a header's "23 rows" and could make a titleless row a cluster's face.
    titled = [r for r in rows if _row_title(r)]
    if not titled:
        return []
    rows = titled
    anchors_by_row: list[frozenset[str]] = [row_anchors(r) for r in rows]
    df: dict[str, int] = {}
    for names in anchors_by_row:
        for name in names:
            df[name] = df.get(name, 0) + 1
    ceiling = max(
        _JOURNAL_CLUSTER_MAX_UBIQUITY * len(rows),
        float(_JOURNAL_CLUSTER_UBIQUITY_FLOOR_ROWS),
    )

    surviving_by_row = [
        sorted(n for n in names if df[n] < ceiling) for names in anchors_by_row
    ]
    # THE PAIR A ROW JOINS is the pair the POOL supports most. A row naming
    # three polities offers three pairs; picking by pair frequency puts
    # "US lawmaker lashes out at UK over Israeli settlements" with the forty
    # other UK/Israel settlement rows rather than with whatever else happens to
    # mention Washington, and it does so for a stated reason ("the biggest story
    # this row belongs to") rather than by a proxy for one. Ties break to the
    # RARER pair (a rare co-naming is more informative than a common one), then
    # alphabetically — a total order, so the answer never depends on row order.
    pair_df: dict[tuple[str, str], int] = {}
    for surviving in surviving_by_row:
        for i, a in enumerate(surviving):
            for b in surviving[i + 1:]:
                pair_df[(a, b)] = pair_df.get((a, b), 0) + 1

    buckets: dict[str, list[Any]] = {}
    meta: dict[str, tuple[tuple[str, ...], str]] = {}
    for row, surviving in zip(rows, surviving_by_row):
        if len(surviving) >= 2:
            pair = min(
                (
                    (a, b)
                    for i, a in enumerate(surviving) for b in surviving[i + 1:]
                ),
                key=lambda p: (-pair_df[p], df[p[0]] + df[p[1]], p),
            )
            key = "anchors|" + "|".join(pair)
            meta.setdefault(key, (pair, ""))
        elif len(surviving) == 1:
            geo = _row_geo_key(row)
            key = f"geo|{surviving[0]}|{geo}"
            meta.setdefault(key, ((surviving[0],), geo))
        else:
            continue
        buckets.setdefault(key, []).append(row)

    clusters: list[SliceCluster] = []
    for key, members in buckets.items():
        if len(members) < _JOURNAL_CLUSTER_MIN_MEMBERS:
            continue
        ordered = sorted(
            members, key=lambda r: salience_sort_key(r.get("salience")),
            reverse=True,
        )
        anchor_names, geo = meta[key]
        terms = tuple(_label_terms(ordered, anchor_names))
        # THE ASYMMETRY BETWEEN THE TWO RULES. Two polities co-named in one
        # row's own prose is already strong evidence of one object — the
        # Russia/Ukraine war rows share no title term and are still one thing —
        # so a two-anchor key is admitted on the pair alone. A ONE-anchor key is
        # much weaker: "same country, same geo tag" is a DESK, not a story, and
        # admitting it unguarded announced "Israeli parties submit candidate
        # lists", "Trains to run on special Rosh Hashanah schedule" and
        # "Israel sees 58% drop in returning citizens" as one object on the live
        # 2026-09-08 window. So the weaker rule pays for itself: it needs a term
        # at least two members actually share.
        if len(anchor_names) < 2:
            if not terms:
                continue
            # …and the terms bind the MEMBERS too, not just the cluster. A key
            # is a bucket; a header is a claim about what is in it. Without this
            # the live 2026-09-09 "Australia · geo AU · police, coerce" cluster
            # carried "Returning Rams superstar to miss MCG NFL clash" as its
            # fourth "row", which is the false-connective defect this whole
            # program exists to stop — manufactured by the selector this time
            # rather than by the voice.
            wanted = set(terms)
            ordered = [
                r for r in ordered
                if wanted & set(normalize_prose(_row_title(r)).split())
            ]
            if len(ordered) < _JOURNAL_CLUSTER_MIN_MEMBERS:
                continue
        sources = len({r.get("source_id") for r in ordered if r.get("source_id")})
        clusters.append(SliceCluster(
            key=key,
            anchors=anchor_names,
            geo=geo,
            members=tuple(ordered),
            mass=len(ordered) * max(sources, 1),
            sources=sources,
            max_magnitude=max(magnitude_of(r.get("salience")) for r in ordered),
            terms=terms,
        ))
    clusters.sort(key=lambda c: (-c.mass, -c.max_magnitude, c.key))
    return clusters


def _cluster_rows(cluster: SliceCluster, k: int) -> list[dict[str, Any]]:
    """The cluster's contribution: its lead + up to ``k`` SOURCE-DIVERSE members.

    The admit-then-backfill shape is
    ``actor_substrate_slice._diversify_by_source``'s, at a per-source cap of one:
    inside ONE story a second row from a source already represented adds an angle
    the narrator has, while a new source adds corroboration it does not. A
    single-source cluster still contributes ``k`` members (the backfill), so the
    block is never smaller than the plain cut."""
    picked = [cluster.lead]
    seen = {cluster.lead.get("source_id")}
    overflow: list[dict[str, Any]] = []
    for row in cluster.members[1:]:
        if len(picked) > k:
            break
        sid = row.get("source_id")
        if sid not in seen:
            picked.append(row)
            seen.add(sid)
        else:
            overflow.append(row)
    need = (k + 1) - len(picked)
    if need > 0:
        picked.extend(overflow[:need])
    return picked


def _fill_remainder(rows: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    """``_select_journal_slice``'s own body, at a reduced cap.

    Every semantic is preserved: consequence leads, the FRESHNESS FLOOR keeps its
    full ``_JOURNAL_FRESH_RESERVE`` tail slots for the newest delivered rows, and
    a pool with nothing scored (or one that fits) returns the delivered order
    untouched."""
    from .journal_slice import (
        _JOURNAL_FRESH_RESERVE,
        _salience_ordered,
        _slice_recency_key,
    )
    from .signal_salience import magnitude_of

    if budget <= 0:
        return []
    ordered = _salience_ordered(rows)
    n_scored = sum(1 for r in rows if magnitude_of(r.get("salience")) >= 0.0)
    if n_scored == 0 or len(ordered) <= budget:
        return ordered[:budget]
    head_n = max(0, budget - _JOURNAL_FRESH_RESERVE)
    head = ordered[:head_n]
    head_ids = {id(r) for r in head}
    tail: list[dict[str, Any]] = []
    for row in sorted(rows, key=_slice_recency_key, reverse=True):
        if id(row) in head_ids:
            continue
        tail.append(row)
        if len(tail) >= budget - head_n:
            break
    return head + tail


def cluster_first_window(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The flag-ON window: cluster block first, then the existing magnitude tail.

    Mutates the chosen LEAD rows with a ``journal_cluster_header`` string (the
    same in-place stamping idiom ``_labeled_journal_slice`` uses for
    ``journal_label``) and CLEARS the key everywhere else first, so a row that
    stops being a lead never renders a stale header."""
    from .journal_slice import _JOURNAL_RENDER_CAP

    for row in inputs:
        row.pop(CLUSTER_HEADER_KEY, None)

    block: list[dict[str, Any]] = []
    taken: set[int] = set()
    for cluster in cluster_pool(inputs)[:_JOURNAL_CLUSTER_N]:
        if len(block) >= _JOURNAL_RENDER_CAP:
            break
        rows = [r for r in _cluster_rows(cluster, _JOURNAL_CLUSTER_K)
                if id(r) not in taken]
        if not rows:
            continue
        rows[0][CLUSTER_HEADER_KEY] = cluster.header()
        for row in rows:
            taken.add(id(row))
        block.extend(rows)
    block = block[:_JOURNAL_RENDER_CAP]
    remainder = [r for r in inputs if id(r) not in taken]
    return block + _fill_remainder(remainder, _JOURNAL_RENDER_CAP - len(block))


def journal_window(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """THE selector — the one place ``LEGBA_JOURNAL_CLUSTER_FIRST`` is read.

    Flag OFF: ``_select_journal_slice(inputs)``, called and returned, so the
    shipped default is the pre-T2.2 function itself rather than a re-derivation
    of it (B0's byte-identity proof keeps covering the live path). Flag ON: the
    cluster block leads and the magnitude tail fills the rest."""
    from .journal_slice import _select_journal_slice

    if not cluster_first_enabled():
        return _select_journal_slice(inputs)
    return cluster_first_window(inputs)


def cluster_header_line(row: Mapping[str, Any]) -> str:
    """The header the render loops print BEFORE a cluster's lead row (or ``""``)."""
    header = row.get(CLUSTER_HEADER_KEY)
    return header if isinstance(header, str) and header else ""


#: The one prompt line the cluster block needs. Says what a ``>`` line IS (a
#: deterministic grouping, not an editorial judgement), what it licenses (naming
#: the object once and weighing it by its mass) and what it does NOT license
#: (citing the header — it carries no ref of its own).
CLUSTER_PREAMBLE = (
    "GROUPED ROWS — a line beginning '▸' is not a signal: it is a DETERMINISTIC "
    "grouping of the rows listed under it, keyed on the polities their own text "
    "names, with its member count, its distinct-source count and its "
    "highest-consequence member as the lead. It is the OBJECT those rows are "
    "about. Name that object ONCE and weigh it by its mass — many moderate rows "
    "from many sources about one event is a bigger fact than any one of them — "
    "and cite the member rows' [[ref:...]] for anything you assert. The '▸' line "
    "itself carries NO ref and is never a citation."
)


# ---------------------------------------------------------------------------
# The context-only desk roster (P5's second half)
# ---------------------------------------------------------------------------

#: How many desk ids the roster block names per line-group before it truncates.
_ROSTER_MAX_NAMES = 40


def _coverage_sections(body: str) -> tuple[list[str], list[str]]:
    """The ``## Coverage`` and ``## Not carried`` bullet lines of a world read."""
    coverage: list[str] = []
    dropped: list[str] = []
    current: list[str] | None = None
    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            heading = stripped[3:].strip().lower()
            current = (
                coverage if heading == "coverage"
                else dropped if heading == "not carried"
                else None
            )
            continue
        if current is not None and stripped.startswith("- "):
            current.append(stripped[2:].strip())
    return coverage, dropped


def _desk_id(line: str) -> str:
    """The ``(country_g20_gb)`` desk id inside a coverage bullet, else the line."""
    if "(" in line and ")" in line:
        inner = line[line.index("(") + 1: line.index(")")].strip()
        if inner:
            return inner
    head = line.split(":", 1)[0].strip()
    return head or line.strip()


def coverage_roster_block(body: str) -> str:
    """The trailing "Desks today" block, or ``""`` when the read has no roster.

    CONTEXT, not evidence — the block says so in its own header and carries no
    ``[[ref:]]`` anywhere, which is the whole point: the journal may say what the
    desks carried and what they dropped without laundering a desk's voice into
    its own citations (the audit's §2 "do not double-count one voice through
    another")."""
    coverage, dropped = _coverage_sections(body)
    if not coverage and not dropped:
        return ""
    in_basis = [_desk_id(line) for line in coverage if "in basis" in line.lower()]
    other = [_desk_id(line) for line in coverage if "in basis" not in line.lower()]
    lines = [
        "",
        "--- Desks today (CONTEXT ONLY — the newest world read's own coverage "
        "roster. No line here carries a citable ref, so nothing in this block "
        "may be cited or quoted. Use it to say what the desks carried and what "
        "they dropped; cite signals for the facts themselves) ---",
        f"in basis ({len(in_basis)}): " + ", ".join(in_basis[:_ROSTER_MAX_NAMES])
        + (" ..." if len(in_basis) > _ROSTER_MAX_NAMES else ""),
    ]
    if other:
        lines.append(
            f"carried otherwise ({len(other)}): "
            + ", ".join(other[:_ROSTER_MAX_NAMES])
            + (" ..." if len(other) > _ROSTER_MAX_NAMES else "")
        )
    lines.append(
        f"not carried ({len(dropped)})"
        + (": " + "; ".join(d[:120] for d in dropped[:_ROSTER_MAX_NAMES])
           if dropped else "")
    )
    return "\n".join(lines)
