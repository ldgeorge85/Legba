# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R1-a — THE FRAME ANCHOR. What a frame is ABOUT, read off authored prose.

The module ``planning/R1_FRAME_REPAIR_DESIGN_2026-09-06.md`` row R1-a asks for,
**as amended** by ``planning/R1_FRAME_REPAIR_AMENDMENT_2026-09-06.md`` §2. It is
a pure addition: nothing in the fleet imports it, nothing behaves differently
because it exists, and it buys nothing until R1-b mints against it. It is here
so that the bar can be *built and measured* before it is *bound*.

WHY THE AMENDMENT REBUILT THIS BAR, WHICH IS THE ONLY THING WORTH KNOWING.
The design's anchor counted distinct SIGNALS in a frame's own evidence. That
predicate is not wrong so much as it is not *about the frame*: every open frame
on a desk shares (almost exactly) one evidence pool, because one variable in
``runtime/actor_substrate_slice.py`` becomes both a finding's lineage and its
receipt slice. Live, one IL frame's pool is **98.78%** of the entire desk union,
and the whole spread across IL's eight frames is **four signals** (amendment
§1.2, Q-B/Q-C). So a predicate over a frame's cited signals is a fact about the
DESK wearing a frame-level contract, and every frame-grain consequence drawn
from it is arithmetic on that identity.

The repair stops one hop earlier. ``situations.derived_from`` → the member
finding is genuinely frame-specific; it is the second hop, into signals, that
dissolves into the desk. **Read what the finding SAYS, not what it READ.** The
same eight IL frames name Palestine in 12 / 12 / 7 / 5 / 1 / 0 / 0 / 0 of their
cited findings' bodies, and JP's seven name North Korea in 4 / 2 / 1 / 0 / 0 /
0 / 0. That spread is frame-specific, which the signal pool never was.

WHY ``body`` AND NOT ``title`` ALONE — the result that settles it. At MINT
grain on titles alone, IL/Palestine anchors on **nothing at every threshold**,
K=2 included (amendment §1.4). That is not a tuning miss, it is R-1's founding
case: the repair exists *because* IL's desks do not write Palestine in their
heads. A bar that mints an anchor only where the title already names the polity
is the coverage floor's clause 6 read backwards, and can only ratify coverage
that already exists. So the substrate is ``title`` + ``body``, bounded by
:attr:`FrameAnchorConfig.max_body_chars`.

THE FOUR CLAUSES (amendment §2.1), each of which earns its place:

``class gate``
    ``canonicalize_entity(A, "country")[1] == "country"``. This is what cleanly
    drops ``United Nations``, ``European Union`` and ``NATO`` — the canon calls
    all three ``organization``. Live: **158 of the canon's 160 keys** survive.
``home exclusion``
    A desk's own country is in nearly every one of its findings and can never
    be the missing second story. The test is ``represented_by`` over a prose
    blob of every home spelling, not a fold containment — fold containment gets
    ``Niger``/``Nigeria`` and ``Sudan``/``South Sudan`` wrong in both
    directions. Pinned over all 32 fleet desks by this module's tests.
``recurrence``
    ≥ ``min_anchor_findings`` (5) DISTINCT cited findings naming the polity.
    The live cliff is **between 4 and 5 and it is the negative control's
    cliff**: at K=4 JP mints one North Korea key, at K=5 it mints none, while
    IL/Palestine holds 4 of 9 dimensions across K ∈ {2..5}.
``ubiquity ceiling`` (D-t)
    A polity named by ≥ ``max_anchor_ubiquity`` (0.75) of the DESK's own
    findings is refused. This clause is not optional and it is not a nicety:
    without it ``United States`` anchors **41 of 92** minted keys and takes the
    top slot on nearly every dimension of every desk, because it is named in
    94–97% of every desk's finding bodies. A story every dimension already
    carries is not a missing second story.

WHAT ``evidence_mass`` COUNTS, AND WHY IT HAD TO CHANGE. Findings, not signals
— for the reason the amendment exists. The signal count is desk-uniform to
within four signals across a desk's eight frames, so it cannot ORDER a
register; the finding count can (live over the 61 minted keys: p25 6 / p50 9 /
p75 14, min 5, max 29). It is not comparable ACROSS desks — a desk's finding
count is a cadence artifact of its descriptor — and amendment F-12 forbids
pooling it or thresholding on it.

WHY THE CLOCK IS NOT ``produced_at`` (D-k). ``newest_evidence_at`` is a world
clock the product cannot wind. Dropping the signal walk from the *bar* does not
license substituting the finding's own ``produced_at``, which is exactly the
M-1 bookkeeping-for-evidence defect. So this module never reads a date as an
evidence clock: :func:`anchors_for` takes the clock as an INPUT and passes it
through unchanged, and the cheap ``signals.fetched_at`` read that produces it is
R1-b's to bind. ``produced_at`` is read here for one thing only —
:attr:`Anchor.anchor_days`, a count of distinct authoring days, which is
bookkeeping about bookkeeping and is labelled as such.

THE COST, AND WHY THERE IS A BATCHED ENTRY POINT. A nested
``represented_by(candidate, prose)`` loop over 254 IL findings × 158 candidates
measures **13.26 s**, i.e. ≈823 s/run fleet-wide — 69% of a 20-minute tick, and
a blocker. Folding each prose ONCE and precompiling the surfaces takes the same
work to **0.89 s** (≈55 s/run, 15×) with 705 hits against 705 — exact
agreement. :class:`PolitySurfaceIndex` is that entry point, and
``test_batched_matcher_agrees_with_represented_by`` (amendment P-6) is the pin
that keeps it honest. The equivalence is not merely measured, it is
constructive: see the class's own docstring.

NO LLM, NO PRODUCER SELF-DESCRIPTION (D-n, narrowed by amendment §2.4). This
module may not import an LLM handler and may not read ``data.key_entities`` or
its four self-description siblings. A finding's ``title`` and ``body`` ARE
allowed and are the substrate the bar reads: under the QUOTATION regime the
desk sentence is the product, and authored prose about the world is not a
model's structured label for itself. Pinned by the AST guard in
``tests/data_pkg/test_frame_anchor.py``.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from ._entity_canon import canonicalize_entity
from ._frame_content import DOMESTIC_ANCHOR, signature_anchor
from ._polity_match import (
    _SURFACES as _CANON_POLITIES,
    SQUEEZE_TOKEN_MAX_CHARS,
    _boundary_probe,
    entity_surfaces,
    is_home_country,
    normalize_prose,
)
from .analysts.deterministic_handlers.finding_supersession import (
    _SIGNATURE_EVENT_MARKER,
)

logger = logging.getLogger(__name__)

#: Descriptor-option prefix (the ``coverage_floor_`` / ``frame_content_gauge_``
#: precedent). R1-b declares this family in ``handler_options.py`` when it binds
#: the bar to ``situation_clustering``; declaring it HERE, while nothing reads
#: the knobs on a live path, would put seven knobs in the catalog that an
#: operator can set and that cannot move anything — dead config, which is the
#: exact defect the X-1 guard exists to prevent.
OPTION_PREFIX: str = "register_anchor_"

#: Env-var prefix. Env is the base default; a descriptor option, when set,
#: always wins — the order every other option in this tree uses.
ENV_PREFIX: str = "LEGBA_REGISTER_ANCHOR_"

#: Cap on the anchor token, mirroring ``finding_supersession._DIMENSION_MAX_CHARS``.
#: A bound on what an unvalidated name can do to an indexed text key.
_ANCHOR_MAX_CHARS = 64

#: Everything that is not a slug character collapses to ``_``. This is what
#: makes the token separator-counterfeit-proof: ``#`` and ``|`` are both
#: non-slug, so no polity name can spell a ``#dim:`` or ``#evt:`` marker or an
#: entity-tail ``|`` into the key.
_NON_SLUG = re.compile(r"[^a-z0-9]+")

#: How ``title`` and ``body`` are joined before matching (amendment §2.1). A
#: separator no analyst writes, so a surface cannot straddle the seam and match
#: text that exists in neither field.
PROSE_JOIN: str = " ‖ "


# ---------------------------------------------------------------------------
# Config — 7 knobs, ``LEGBA_REGISTER_ANCHOR_*``, option wins over env
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameAnchorConfig:
    """The anchor bar's seven knobs (amendment §2.3).

    Three knobs from the design RETIRED with the signal walk and are absent by
    intent, not by oversight: ``min_anchor_signals`` and ``min_entity_confidence``
    (the bar reads no signals and no NER spans at all) and ``min_anchor_share``
    (measured INERT — K=5 at share 0% and at share 15% give identical anchor
    sets on all five replay desks, and shipping a knob that cannot move the
    answer is worse than shipping none).
    """

    #: DISTINCT cited findings that must name the polity. The plateau floor:
    #: K ∈ {5,6} give identical answers on SA and TR, and the negative
    #: control's cliff is between 4 and 5 (JP mints one NK key at 4, none at 5).
    min_anchor_findings: int = 5

    #: D-t. A polity named by AT LEAST this share of the desk's own findings is
    #: refused as wire boilerplate. The comparison is ``share < ceiling`` to
    #: PASS — strictly below — which is the reading amendment §2.1 writes and
    #: the one ``_frame_content.mint_floor_twin`` already ships, so R1-d's
    #: pre-flip twin and this bar cannot select different populations.
    #: The only knob that moves the answer, and it has ONE measurement behind
    #: it (amendment F-10): 0.75 sits in the gap between boilerplate (the US at
    #: 94–97%) and a real second story (UA/Russia at 68%, the closest approach).
    max_anchor_ubiquity: float = 0.75

    #: Anchors kept per SCOPE — a dimension at MINT, a frame at KEEP. Named
    #: ``per_scope`` and not ``per_frame`` because the mint scope is a
    #: dimension's whole finding pool.
    max_anchors_per_scope: int = 3

    #: The recurrence window. Matches the coverage floor's own, so the two
    #: instruments cannot disagree about which fortnight they describe.
    window_days: int = 14

    #: Distinct ``produced_at`` DAYS among the naming findings. Re-based from
    #: the design's signal-day count; see :func:`anchors_for` for the note on
    #: why this clause is applied here although amendment §2.1's predicate
    #: line omits it.
    min_anchor_days: int = 5

    #: Bounds the prose projection. Live max finding body is 14,598 B and the
    #: mean 1,822 B; 6,000 holds the 30-day mint read at ~29 MB.
    max_body_chars: int = 6_000

    #: The canon class a candidate must canonicalize to. Design §3.2 refuses to
    #: widen this: an ORG-class anchor without a home-PLACE exclusion and an
    #: ORG fold would mint a "Reuters" frame on every desk.
    class_gate: str = "country"

    @classmethod
    def from_options(
        cls, options: Mapping[str, Any] | None
    ) -> "FrameAnchorConfig":
        kwargs: dict[str, Any] = {}
        opts = dict(options or {})
        for f in fields(cls):
            value = f.default
            env_raw = os.environ.get(ENV_PREFIX + f.name.upper())
            if env_raw is not None:
                value = _coerce(f.name, env_raw, value)
            opt_key = OPTION_PREFIX + f.name
            if opt_key in opts:
                value = _coerce(f.name, opts[opt_key], value)
            kwargs[f.name] = value
        return cls(**kwargs)


def config_from_options(
    options: Mapping[str, Any] | None,
) -> FrameAnchorConfig:
    """The PREFIX-FAMILY reader, in the ``_coverage_floor_scan`` idiom.

    A module-level function rather than only the classmethod because the X-1
    guard proves a prefixed knob REACHABLE by invoking its family's reader with
    a probe value — the stronger form of "no dead config", since no literal
    ``options.get("register_anchor_min_anchor_findings")`` exists to grep for.
    This module's own tests use it that way today; ``handler_options`` picks it
    up when R1-b binds the bar.
    """
    return FrameAnchorConfig.from_options(options)


def _coerce(name: str, raw: Any, fallback: Any) -> Any:
    """Coerce to the field's type, keeping the predecessor on a bad value.

    A mistyped knob must degrade the bar, never take the clusterer offline —
    the ``CoverageFloorConfig._coerce`` contract.
    """
    try:
        return type(fallback)(raw)
    except (TypeError, ValueError):
        logger.warning(
            "frame_anchor.bad_option name=%s raw=%r — keeping %r",
            name, raw, fallback,
        )
        return fallback


# ---------------------------------------------------------------------------
# THE SIGNATURE'S ANCHOR SLOT — the ``dimension_token`` idiom, with a SQL twin
# ---------------------------------------------------------------------------

#: The SQL twin of :func:`anchor_token`, kept beside it so the two are edited
#: together. Migration 0193 appends ``#evt:_domestic`` to every stored frame and
#: any later re-key computes the token in Postgres; a token the two spell
#: differently does not fail loudly — it produces a SECOND frame for the same
#: anchor under ``uq_situations_signature_analyst``, which is the exact
#: duplicate-frame outcome the whole re-key exists to remove. Asserted
#: row-for-row by ``test_anchor_token_python_and_sql_agree``.
#:
#: It is an EXPRESSION over ``$1``, not a statement — that is the form a
#: migration embeds (``UPDATE situations SET … = … || '#evt:' || <expr>``).
#: Wrap it in a ``SELECT`` to execute it on its own.
ANCHOR_TOKEN_SQL: str = """
    COALESCE(
      NULLIF(
        btrim(
          left(
            btrim(
              regexp_replace(
                lower(btrim(COALESCE($1::text, ''))),
                '[^a-z0-9]+', '_', 'g'
              ),
              '_'
            ),
            64
          ),
          '_'
        ),
        ''
      ),
      '_domestic'
    )
"""


def anchor_token(polity: Any) -> str:
    """The signature-safe ``#evt:`` token for an anchor polity.

    Lowercased, every non-slug run folded to ``_`` (so the token can never
    counterfeit ``#dim:``, ``#evt:`` or the entity-tail ``|``), trimmed of
    leading/trailing underscores, capped, and trimmed again so a cut that lands
    mid-separator cannot leave a dangling one.

    An empty/absent/fully-stripped name yields :data:`DOMESTIC_ANCHOR` — the
    residue, not an anchor — which is the same contract
    ``finding_supersession.dimension_token`` gives ``_unattributed``. That
    matters: it means a caller cannot accidentally mint an EMPTY ``#evt:``
    slot, which would parse as neither anchored nor domestic.

    THIS FUNCTION HAS A SQL TWIN — :data:`ANCHOR_TOKEN_SQL`. Keep the two edits
    together.
    """
    token = _NON_SLUG.sub("_", str(polity or "").strip().lower()).strip("_")
    token = token[:_ANCHOR_MAX_CHARS].strip("_")
    return token or DOMESTIC_ANCHOR


def with_anchor(sig: Any, polity: Any = None) -> str:
    """``sig`` keyed to its anchor — idempotent, and safe on any vintage.

    Mirrors :func:`finding_supersession.with_dimension` exactly, one marker
    later in the grammar:

    * only DERIVED (``sig:``) keys take an anchor slot. An explicit ``sit:``
      key is handed to the clusterer by its own producer, already carries that
      producer in its text, and is returned untouched;
    * a signature that ALREADY carries an ``#evt:`` slot is returned untouched,
      so this is safe to apply to a row of unknown vintage — which is exactly
      how a half-migrated fleet would use it;
    * ``polity=None`` mints the ``_domestic`` residue, so the flag-off /
      no-anchor path is the same call with the same idempotence.
    """
    text = str(sig or "")
    if not text.startswith("sig:") or _SIGNATURE_EVENT_MARKER in text:
        return text
    return f"{text}{_SIGNATURE_EVENT_MARKER}{anchor_token(polity)}"


# ---------------------------------------------------------------------------
# THE BATCHED MATCHER — fold each prose ONCE (amendment §2.6)
# ---------------------------------------------------------------------------


class PolitySurfaceIndex:
    """Many candidates against one prose, folded once. Provably exact.

    ``represented_by(name, prose)`` folds ``prose`` on every call and folds
    every one of ``name``'s surfaces on every call, so a nested loop over R
    rows and C candidates does R×C prose folds of ~1.8 kB each. This class does
    R prose folds and C surface folds, once.

    THE EQUIVALENCE IS CONSTRUCTIVE, not merely measured.
    ``normalize_prose`` ends in ``_PUNCT.sub(" ", …).strip()``, so its output
    is a stream of ``[a-z0-9]+`` tokens separated by single spaces. In such a
    stream a regex ``\\b<S>s?\\b`` can only match at a token boundary and can
    only end at one, so for a SINGLE-WORD surface ``S``::

        re.search(r"\\b" + escape(S) + r"s?\\b", prose)  ⟺  S in tokens or S+"s" in tokens

    which is a set lookup. MULTI-WORD surfaces run ``represented_by``'s own
    phrase-and-squeezed rule over the same folded prose, from the same shared
    constants: the phrase leg is the SAME ``_boundary_probe(norm)`` pattern the
    single-word leg uses, and the squeezed leg drops to a whole-token test at
    ``_polity_match.SQUEEZE_TOKEN_MAX_CHARS`` exactly as it does there. The
    classification into single/multi is ``" " in norm`` — the same expression,
    on the same value. So the two implementations agree by construction, and
    ``test_batched_matcher_agrees_with_represented_by`` (P-6) pins it on
    live-shaped rows anyway, because a proof that is not tested is a hope.

    WHY THE MULTI-WORD LEGS ARE BOUNDED. Both were bare containment over a
    token stream until 2026-09-09, and containment cannot tell a name from the
    middle of a longer word. ``United States`` carries the surface ``u.s``,
    which folds to ``u s`` and squeezes to ``us`` — inside *russia*,
    *australia*, *focus*, *thousands* and *Jerusalem*; the phrase form ``u s``
    is inside *Netanyahu says* and *EU sanctions*. On the live 72 h pool the
    index put ``United States`` on 25.1% of titles and ``United Nations`` on
    21.1%, and the ``country_g20_us`` desk's recovery leg spent twenty of its
    thirty admission slots on rows naming no American anything. Bounding both
    legs is a strict NARROWING — nothing newly matches — and it is measured to
    cost no real naming, because every multi-word polity also carries its
    demonym as a single-word surface.

    Deliberately GENEROUS in the same direction as ``represented_by``: every
    REMAINING ambiguity resolves toward "this prose names that polity".
    """

    __slots__ = ("_single", "_multi", "_names")

    def __init__(self, candidates: Iterable[str]) -> None:
        #: normalized single-word surface -> the polities it names
        self._single: dict[str, tuple[str, ...]] = {}
        #: (polity, compiled ``\bphrase s?\b`` probe, squeezed form, is-short)
        self._multi: list[tuple[str, "re.Pattern[str]", str, bool]] = []
        self._names: tuple[str, ...] = tuple(dict.fromkeys(candidates))
        single: dict[str, list[str]] = {}
        for name in self._names:
            for surface in entity_surfaces(name):
                norm = normalize_prose(surface)
                if not norm:
                    continue
                if " " in norm:
                    squeezed = norm.replace(" ", "")
                    self._multi.append((
                        name,
                        _boundary_probe(norm),
                        squeezed,
                        len(squeezed) <= SQUEEZE_TOKEN_MAX_CHARS,
                    ))
                else:
                    bucket = single.setdefault(norm, [])
                    if name not in bucket:
                        bucket.append(name)
        self._single = {k: tuple(v) for k, v in single.items()}

    @property
    def names(self) -> tuple[str, ...]:
        """The candidate polities this index was built over, in order."""
        return self._names

    def names_in(self, prose: str) -> set[str]:
        """Every candidate this prose NAMES — one fold, whatever C is."""
        return self.names_in_folded(normalize_prose(prose))

    def names_in_folded(self, folded: str) -> set[str]:
        """:meth:`names_in` over prose ALREADY through ``normalize_prose``.

        The entry point a caller uses when one prose is matched against several
        indexes, so the fold is paid once rather than once per index.
        """
        hits: set[str] = set()
        tokens = set(folded.split())
        for token in tokens:
            for name in self._single.get(token, ()):
                hits.add(name)
            if token.endswith("s"):
                for name in self._single.get(token[:-1], ()):
                    hits.add(name)
        if self._multi:
            squashed = folded.replace(" ", "")
            for name, probe, squeezed, short in self._multi:
                # ``squeezed in squashed`` is necessary for both legs and
                # sufficient for a long surface — ``_surface_matches``'s own
                # order, so the two spellings refuse in the same place.
                if name in hits or squeezed not in squashed:
                    continue
                if not short:
                    hits.add(name)
                elif (
                    probe.search(folded)
                    or squeezed in tokens
                    or (squeezed + "s") in tokens
                ):
                    hits.add(name)
        return hits


def finding_prose(row: Mapping[str, Any], *, max_body_chars: int) -> str:
    """The substrate the bar reads: ``title`` + ``body``, bounded.

    The bound is applied HERE and not only in R1-b's SQL projection, so a
    caller that hands over a full body gets the same answer as one that hands
    over ``left(body, max_body_chars)`` — otherwise the bar would quietly
    depend on which read produced its rows.
    """
    title = str(row.get("title") or "")
    body = str(row.get("body") or "")
    if max_body_chars >= 0:
        body = body[:max_body_chars]
    return f"{title}{PROSE_JOIN}{body}"


def _finding_key(row: Mapping[str, Any], position: int) -> str:
    """The identity a DISTINCT-finding count counts.

    A row with no ``id`` counts as its own finding rather than being dropped or
    collapsed with its neighbours: an unidentifiable row is a fact about our
    bookkeeping, and neither silently discarding it nor silently merging it is
    honest. Live rows always carry an id.
    """
    fid = str(row.get("id") or "")
    return fid or f"#pos:{position}"


def naming_findings(
    rows: Sequence[Mapping[str, Any]],
    index: PolitySurfaceIndex,
    *,
    max_body_chars: int,
) -> dict[str, set[str]]:
    """polity -> the DISTINCT finding keys whose prose names it.

    The batched entry point amendment §2.6 requires. One fold per row, not one
    per (row, candidate).
    """
    out: dict[str, set[str]] = {}
    for position, row in enumerate(rows):
        folded = normalize_prose(
            finding_prose(row, max_body_chars=max_body_chars)
        )
        key = _finding_key(row, position)
        for name in index.names_in_folded(folded):
            out.setdefault(name, set()).add(key)
    return out


# ---------------------------------------------------------------------------
# THE CANDIDATES — the class gate and the home exclusion
# ---------------------------------------------------------------------------

#: Every canonical polity the shared canon knows, which is the default
#: candidate set. 160 keys, 158 of which survive the class gate (the two
#: casualties are ``European Union`` and ``United Nations``; ``NATO`` is not a
#: canon key and is refused by the gate if a caller offers it). Amendment §2.6
#: recommends a SECOND step that narrows this per desk to the ~10-20 polities
#: the coverage floor already nominates — a caller passes them as
#: ``candidates`` and everything below is unchanged.
DEFAULT_CANDIDATES: tuple[str, ...] = tuple(sorted(_CANON_POLITIES))


def candidate_polities(
    candidates: Iterable[str] | None = None,
    *,
    home_blob: str = "",
    class_gate: str = "country",
) -> tuple[str, ...]:
    """The gated candidate set: canonical, in-class, and not the desk's own.

    The class gate runs on the CANDIDATE, never on a span some producer
    labelled — that is what makes it a clean structural refusal of
    ``United Nations`` / ``European Union`` / ``NATO`` rather than a curated
    stoplist that has to learn each new institution.

    The canonical form the canon returns is what is kept, so a caller may hand
    over ``"US"`` or ``"Iranian"`` and get ``United States`` / ``Iran``; the
    result is de-duplicated with the FIRST spelling's position preserved.
    """
    raw = DEFAULT_CANDIDATES if candidates is None else candidates
    kept: list[str] = []
    seen: set[str] = set()
    for name in raw:
        canonical, cls = canonicalize_entity(str(name or ""), class_gate)
        if not canonical or cls != class_gate:
            continue
        if canonical in seen:
            continue
        if home_blob and is_home_country(canonical, home_blob):
            continue
        seen.add(canonical)
        kept.append(canonical)
    return tuple(kept)


# ---------------------------------------------------------------------------
# THE BAR
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Anchor:
    """One anchor a scope established, and everything a register line needs."""

    #: The canonical polity name — what a rendered ``anchor=`` field prints.
    polity: str
    #: The ``#evt:`` slot value. ``anchor_token(polity)``, carried so a caller
    #: never has to re-derive it (and so cannot re-derive it differently).
    token: str
    #: DISTINCT cited findings in the SCOPE naming this polity. This is
    #: ``data.evidence_mass``. Findings, not signals — amendment §2.5. Never
    #: comparable across desks (F-12).
    evidence_mass: int
    #: Distinct ``produced_at`` days among those findings. Bookkeeping about
    #: bookkeeping, and labelled as such: it is NOT an evidence clock.
    anchor_days: int
    #: The share of the DESK's own findings naming this polity — the D-t
    #: measurement, carried so a receipt can show why a near-miss was kept.
    desk_ubiquity: float | None
    #: The D-k world clock, passed through from the caller's ``signals``
    #: read. ``None`` when the caller supplied none. This module never
    #: substitutes ``produced_at`` for it.
    newest_evidence_at: datetime | None = None


def anchors_for(
    scope_rows: Sequence[Mapping[str, Any]],
    *,
    desk_rows: Sequence[Mapping[str, Any]],
    home_blob: str = "",
    config: FrameAnchorConfig | None = None,
    candidates: Iterable[str] | None = None,
    newest_evidence_at: Mapping[str, datetime] | None = None,
) -> list[Anchor]:
    """The amended anchor bar. PURE and DB-FREE — rows in, anchors out.

    ``scope_rows``
        The cited findings the anchor must be established over. At MINT that is
        the dimension's whole ``window_days`` pool; at KEEP it is the frame's
        own ``situations.derived_from`` members. The design's mint/keep split
        survives the amendment intact — both discriminate at frame grain.
    ``desk_rows``
        THE UBIQUITY DENOMINATOR, and it is REQUIRED — there is deliberately no
        default. A ceiling on the wrong denominator is amendment P-3's named
        failure ("D-t mis-set, or ubiquity on the wrong denominator"), and both
        available defaults fail silently: falling back to ``scope_rows`` makes
        the ceiling measure a dimension's share of itself (every anchor a scope
        establishes reads as 100% ubiquitous, so the bar over-refuses and D-t
        looks like it is working), while skipping the clause when no pool is
        given loses D-t altogether and lets ``United States`` take 41 of 92
        keys. Neither is visible in the output, so the caller states the desk
        or gets a ``TypeError``. It is the desk's own findings over the same
        window — every dimension's, not just this scope's — counted DISTINCT.
    ``newest_evidence_at``
        D-k's clock, polity -> newest ``signals.fetched_at``. An INPUT. This
        function does not read it out of ``scope_rows`` and must not: a clock
        derived from ``produced_at`` is the M-1 defect, and it would be
        invisible in the output.

    ORDER OF OPERATIONS, because it is load-bearing. Gate → count → recurrence
    → days → ubiquity → sort → cap. The ubiquity refusal happens BEFORE the
    cap, so a refused polity does not consume one of the scope's slots; that is
    amendment §2.1's conjunction (every clause is a conjunct of ``anchor``,
    with the cap applied to the survivors). The appendix's Q-A′ prose lists the
    cap before the ceiling; taken literally, ``United States`` would occupy a
    slot on nearly every dimension and then be dropped from it, so §2.2's
    reported "the five named gaps close exactly" could not be the number it
    reports. §2.1 governs.

    ``min_anchor_days`` IS APPLIED, and the amendment is ambiguous about it:
    §2.3 keeps the knob at 5 and re-bases it onto authoring days, while §2.1's
    predicate line and the Q-A′ fold both omit it. A declared knob that gates
    nothing is dead config — the defect X-1 exists to catch — so it gates here,
    and R1-a's replay reports the sweep at BOTH settings so the cost of the
    clause is visible rather than baked in.

    Ordering is WORST-FIRST by evidence mass (the coverage floor's own sense of
    "worst" — the biggest thing nobody is carrying), then by ``anchor_days``,
    then by polity name so a tie is deterministic across runs and processes.
    """
    cfg = config or FrameAnchorConfig()
    pool = list(desk_rows)
    gated = candidate_polities(
        candidates, home_blob=home_blob, class_gate=cfg.class_gate
    )
    if not gated or not scope_rows:
        return []

    index = PolitySurfaceIndex(gated)
    in_scope = naming_findings(
        scope_rows, index, max_body_chars=cfg.max_body_chars
    )
    on_desk = (
        in_scope
        if _same_rows(pool, scope_rows)
        else naming_findings(pool, index, max_body_chars=cfg.max_body_chars)
    )
    denominator = len({_finding_key(r, i) for i, r in enumerate(pool)})

    days_by_polity = _naming_days(scope_rows, index, cfg)
    kept: list[Anchor] = []
    for polity, findings in in_scope.items():
        mass = len(findings)
        if mass < cfg.min_anchor_findings:
            continue
        days = days_by_polity.get(polity, 0)
        if days < cfg.min_anchor_days:
            continue
        ubiquity = (
            len(on_desk.get(polity, ())) / denominator if denominator else None
        )
        if ubiquity is not None and ubiquity >= cfg.max_anchor_ubiquity:
            continue
        kept.append(
            Anchor(
                polity=polity,
                token=anchor_token(polity),
                evidence_mass=mass,
                anchor_days=days,
                desk_ubiquity=(
                    None if ubiquity is None else round(ubiquity, 4)
                ),
                newest_evidence_at=(newest_evidence_at or {}).get(polity),
            )
        )
    kept.sort(key=lambda a: (-a.evidence_mass, -a.anchor_days, a.polity))
    cap = max(int(cfg.max_anchors_per_scope), 0)
    return kept[:cap]


def _same_rows(a: Sequence[Any], b: Sequence[Any]) -> bool:
    """True when two row sequences are the SAME rows, cheaply.

    Only an identity check per element — it exists to skip a second fold of the
    whole desk pool when the caller passed one sequence for both arguments, and
    a false negative merely costs that fold.
    """
    return len(a) == len(b) and all(x is y for x, y in zip(a, b))


def _naming_days(
    rows: Sequence[Mapping[str, Any]],
    index: PolitySurfaceIndex,
    cfg: FrameAnchorConfig,
) -> dict[str, int]:
    """polity -> distinct ``produced_at`` DAYS among the naming findings.

    Rows with no usable ``produced_at`` contribute no day. That is deliberate
    and it is the strict direction: an undated row cannot testify to recurrence
    over time, and inventing a day for it would let a single batch clear a
    clause about spread.
    """
    days: dict[str, set[Any]] = {}
    for position, row in enumerate(rows):
        day = _day_of(row.get("produced_at"))
        if day is None:
            continue
        folded = normalize_prose(
            finding_prose(row, max_body_chars=cfg.max_body_chars)
        )
        for name in index.names_in_folded(folded):
            days.setdefault(name, set()).add(day)
    return {k: len(v) for k, v in days.items()}


def _day_of(value: Any) -> Any:
    """The calendar day of a ``produced_at``, or ``None``."""
    if isinstance(value, datetime):
        return value.date()
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except (TypeError, ValueError):
        return None


__all__ = [
    "ANCHOR_TOKEN_SQL",
    "Anchor",
    "DEFAULT_CANDIDATES",
    "DOMESTIC_ANCHOR",
    "FrameAnchorConfig",
    "OPTION_PREFIX",
    "ENV_PREFIX",
    "PROSE_JOIN",
    "PolitySurfaceIndex",
    "anchor_token",
    "anchors_for",
    "candidate_polities",
    "config_from_options",
    "finding_prose",
    "naming_findings",
    "signature_anchor",
    "with_anchor",
]
