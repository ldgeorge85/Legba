# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GEO ROUTING v2 — "the desk's own polity is NAMED, but the router filed it elsewhere".

WHY THIS MODULE EXISTS
======================
``signals.geo`` is written once, at ingest, by a ladder that resolves exactly
ONE country and stops: :mod:`legba.data.filters.geocode`'s
*"The first candidate that the backend successfully resolves wins"*
(``geocode.py:81``), promoted onto the column by ``dapr_host``'s
``payload.geo.country_iso2 -> signal.geo`` append. Within the lead zone the
winner is *the first country by POSITION* (``geocode.py:71``). So a wire title
that opens with the ACTOR files under the actor::

    "Russian drone strikes Ukraine security service headquarters in Kyiv"
        -> "Russian" is at offset 0 -> geo = {RU}

and Ukraine — the subject, the place struck, the desk that needs it — is
discarded, because the ladder has already stopped. The desk slice then narrows
on ``geo && $target_geo`` (``actor_substrate_slice.py``), which is the ONE
reader of a desk's reactive window, so the story never reaches the Ukraine
desk at all. On 2026-09-04 that cost Ukraine four of the five reports of the
strike on SBU headquarters — magnitude 0.90-0.94, the window's highest — all
four routed to Russia. Only the TASS copy, whose title opens
"Explosion rocks Ukrainian Security Service headquarters", was tagged ``UA``.

The class is not rare and it is not confined to Ukraine. The same rule sends
nine reports of a US sanctions action against a *Turkish* bank to the US desk
("US sanctions Turkish bank …"), a summit involving India's PM to the venue
country, and a strike inside Lebanon to Israel.

WHAT THIS MODULE DOES, AND WHAT IT REFUSES TO DO
================================================
It answers one question — *does this title NAME the desk's own polity?* — with
the SHARED matcher (:mod:`legba.data._polity_match` /
:class:`legba.data._frame_anchor.PolitySurfaceIndex`), never a second
implementation. It does NOT rewrite ``signals.geo``: the column is read by the
live subscription fan-out (``subscription/filter.py``), the reads API
(``registry/substrate_reads_api.py``), the entity census and the region
rollups, and widening it would silently widen all of them at once. The repair
is applied at SLICE time, where it is contained, reversible, and effective on
the corpus that ALREADY exists — the mis-tagged rows do not need re-ingesting.

THE ISO2 -> POLITY RESOLUTION, and why it is not just ``home_country_name``
==========================================================================
``home_country_name("TR")`` returns "Turkey" and
``entity_surfaces("Turkey")`` is ``{"Turkey", "turkish"}`` — which does not
match the wire's other spelling, "Türkiye", and Anadolu writes "Türkiye".
``home_country_name("GB")`` returns "Britain", whose surface set is the single
literal "Britain" — losing "UK", "British" and "United Kingdom", the three
spellings the wire actually uses. So the resolution here takes the UNION of
what both gazetteers know for the ISO2 (pycountry's names + the canon's
curated ``_ISO2_HOME_ALIASES``), keeps the forms that survive the canon's
``country`` class gate, and adds the curated aliases as literal surfaces.

THE SUBSTRING GUARD, which is the one place this could have gone wrong.
``represented_by`` matches a MULTI-WORD surface as a whole run of tokens (since
Amendment 7g; it was bare containment before), so the alias "Republic of China"
(TW) still sits at token boundaries inside "People's Republic of China" (CN)
and would make every China story name Taiwan. An alias-only surface is therefore
dropped when it is contained in some OTHER country's spelling — and the
haystack has to be BOTH the canon's surfaces and pycountry's official names,
because "People's Republic of China" is only in the second (the canon knows
China as ``{"China", "chinese"}``, so a canon-only guard let the TW alias
straight through — the first version of this function did exactly that).
The guard runs on alias-only surfaces and NOT on canon polities, because
"Congo" is a legitimate canon polity (CG) that is genuinely contained in
"Democratic Republic of the Congo" (CD) — dropping it would break a desk to
protect a different one — and it skips the ISO2's OWN spellings, since a short
alias is usually contained in its country's long name ("Taiwan" in "Taiwan,
Province of China") and that is the alias working, not failing.
``test_geo_routing_v2`` pins every one of those halves.

THE SQL LEG IS A RECALL PREFILTER, NOT THE DECISION
===================================================
Postgres cannot run :func:`~legba.data._polity_match.normalize_prose` (NFKC,
the canon's alias and demonym maps, the word-boundary probe with its optional
plural). So the second slice leg narrows in SQL with a deliberately GENEROUS
``title ILIKE ANY(%probe%)`` and then decides in Python with the exact shared
matcher. The asymmetry is on purpose and it is the safe one: a probe that is
too broad costs one wasted row the matcher then refuses, while a probe that
misses leaves the row exactly where it is today. Every failure mode of the
prefilter therefore degrades toward CURRENT BEHAVIOUR, never toward a false
admission.

THE FLAG
========
``LEGBA_SLICE_GEO_V2`` (env, default OFF). Off, no second query is issued and
no clause changes, so a desk's slice is byte-identical — pinned by
``test_flag_off_issues_exactly_one_query`` and by a read-only replay of five
desks' 2026-09-06 slices against their recorded ``input_row_refs``. The knobs
follow the house ``CoverageFloorConfig._coerce`` contract: env is the base,
a descriptor option wins, and a mistyped knob keeps its predecessor rather
than taking the reader offline.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from ._entity_canon import canonicalize_entity
from ._polity_match import (
    _ISO2_HOME_ALIASES,
    _pycountry_names,
    _SURFACES as _CANON_POLITIES,
    entity_surfaces,
    home_country_name,
    normalize_prose,
)

if TYPE_CHECKING:  # pragma: no cover — import-cycle-free type reference
    from ._frame_anchor import PolitySurfaceIndex

logger = logging.getLogger(__name__)

#: The env flag. Absent or falsey => the program is dark and every read is
#: byte-identical to the pre-program reader.
SLICE_GEO_V2_ENV = "LEGBA_SLICE_GEO_V2"

#: Values that turn the flag ON. Anything else — including the empty string,
#: "0", "off" and a typo — leaves it off, because the safe reading of an
#: ambiguous flag is the one that changes nothing.
_TRUTHY = frozenset({"1", "true", "yes", "on", "enabled"})

OPTION_PREFIX: str = "slice_geo_v2_"
ENV_PREFIX: str = "LEGBA_SLICE_GEO_V2_"

#: Every canonical polity the shared canon knows — the SAME set
#: ``_frame_anchor.DEFAULT_CANDIDATES`` is built from, read from its source
#: (``_polity_match._SURFACES``) rather than through ``_frame_anchor``.
#: Importing the sibling here would close a cycle:
#: ``_frame_anchor -> _frame_content -> _coverage_floor_scan -> _geo_routing``,
#: and the coverage floor is one of this module's two callers.
CANON_POLITIES: tuple[str, ...] = tuple(sorted(_CANON_POLITIES))

#: Canon polities, as a set, for the substring guard below.
_CANON: frozenset[str] = frozenset(CANON_POLITIES)

#: Every canon polity surface, normalized — half the guard's haystack.
_CANON_SURFACES_BY_POLITY: dict[str, frozenset[str]] = {
    name: frozenset(
        n for n in (normalize_prose(s) for s in entity_surfaces(name)) if n
    )
    for name in CANON_POLITIES
}

#: The OTHER half, and the one the first version of this guard was missing:
#: pycountry's OFFICIAL names, per ISO2. "People's Republic of China" is not a
#: canon surface — the canon knows China as {"China", "chinese"} — so a guard
#: that read only canon surfaces let the curated TW alias "Republic of China"
#: through, and every China story would then have named Taiwan. Built lazily:
#: pycountry is an optional dependency at this layer, and a country list is
#: ~250 rows we should not walk at import if nobody asks.
_OFFICIAL_NAMES_BY_ISO2: dict[str, frozenset[str]] | None = None


def _official_names_by_iso2() -> dict[str, frozenset[str]]:
    """ISO2 -> its normalized gazetteer spellings. Memoized."""
    global _OFFICIAL_NAMES_BY_ISO2
    if _OFFICIAL_NAMES_BY_ISO2 is not None:
        return _OFFICIAL_NAMES_BY_ISO2
    built: dict[str, frozenset[str]] = {}
    try:
        import pycountry
    except Exception:  # pragma: no cover — dependency-shape guard
        _OFFICIAL_NAMES_BY_ISO2 = built
        return built
    for record in pycountry.countries:
        code = str(getattr(record, "alpha_2", "") or "").upper()
        if not code:
            continue
        names = {
            normalize_prose(str(n))
            for n in _pycountry_names(code)
            + _ISO2_HOME_ALIASES.get(code, ())
            if n
        }
        built[code] = frozenset(n for n in names if n)
    _OFFICIAL_NAMES_BY_ISO2 = built
    return built


def slice_geo_v2_enabled() -> bool:
    """True when the operator has turned the program on.

    Read on every call rather than cached at import: an operator flipping the
    flag must not have to wait for a process restart, and the read is a dict
    lookup on the hot path's cold edge (once per slice, not once per row).
    """
    raw = os.getenv(SLICE_GEO_V2_ENV)
    return bool(raw) and raw.strip().casefold() in _TRUTHY


@dataclass(frozen=True)
class GeoRoutingConfig:
    """The program's knobs. ``LEGBA_SLICE_GEO_V2_*`` / ``slice_geo_v2_*``.

    ``admit_share`` is the UBIQUITY CEILING, and it is the knob that matters.
    A desk whose polity the whole wire names — the US desk, and on a war week
    the RU and UA desks — would otherwise have its geo-routed core displaced
    wholesale by the recovery leg. Capping the leg at a share of the row cap
    bounds the blast radius to a quarter of the slice by default, and the
    admissions inside that quarter are ordered by MAGNITUDE, because the
    defect this program exists to repair is that the window's *highest*
    magnitude signals were the ones routed away.

    ``min_magnitude`` defaults to 0.0 — deliberately no floor. The India/China
    material the audit named sits at magnitude 0.30, below every floor the
    platform uses elsewhere; a floor tuned to Ukraine's 0.94 strike reports
    would have silently re-lost it.
    """

    #: Max share of the slice's row cap the recovery leg may occupy.
    admit_share: float = 0.25
    #: Magnitude floor for an admitted row. 0.0 = admit at any magnitude.
    min_magnitude: float = 0.0
    #: Max rows the recovery leg's SQL may return before the exact matcher
    #: runs. Mirrors the reader's own over-fetch idiom.
    fetch_multiplier: int = 3
    #: Shortest probe token the SQL prefilter will use. Two-character tokens
    #: match far too much to be worth a scan.
    min_probe_len: int = 3
    #: 1 = also read ``payload.summary`` (falling back to ``distilled_body``),
    #: not just the title. 0/1 rather than a bool because ``_coerce`` is
    #: ``type(fallback)(raw)`` and ``bool("0")`` is True — an env knob whose
    #: OFF value reads as ON is the one shape a flag must never have.
    #:
    #: DEFAULT OFF, and the reason is the platform's own scar tissue: the
    #: geocode ladder demotes whole-field country sweeps below every lead-zone
    #: candidate because "a country named only deep in the body is usually
    #: incidental" — the live R4 sweep filed "Russian scientist beaten in
    #: Yekaterinburg" under AFGHANISTAN off a buried "war in Afghanistan"
    #: clause (``geocode.py:64-72``). A summary is far closer to a lead than
    #: to a body, so this is a real and useful widening — but it is the
    #: operator's to turn on, measured, not the default.
    #:
    #: MEASURED on the 2026-09-01 SCO material the audit named: of the three
    #: items, title-only recovers 0 (none of the three titles names India at
    #: all) and title+summary recovers 1 (``9fd494a9``, whose summary reads
    #: "India's Narendra Modi"). The other two name India ONLY through the
    #: person "Modi", which no polity matcher can see — that recovery needs
    #: the entity graph's person->polity edge, not this rule.
    match_summary: int = 0


def _coerce(name: str, raw: Any, fallback: Any, *, source: str) -> Any:
    """The ``CoverageFloorConfig._coerce`` contract, verbatim in spirit.

    A mistyped knob must degrade the program, never take the slice reader
    offline — a desk that cannot read its own window publishes nothing.
    """
    try:
        return type(fallback)(raw)
    except (TypeError, ValueError):
        logger.info(
            "slice_geo_v2.bad_%s name=%s value=%r — keeping %r",
            source, name, raw, fallback,
        )
        return fallback


def config_from_options(
    options: Mapping[str, Any] | None,
) -> GeoRoutingConfig:
    """Build from ``slice_geo_v2_*`` handler options over env defaults.

    Env supplies the base value (retunable with no deploy); an option, when
    present, wins. The order every other typed option family in this tree
    uses.
    """
    kwargs: dict[str, Any] = {}
    opts = dict(options or {})
    for f in fields(GeoRoutingConfig):
        value = f.default
        env_raw = os.environ.get(ENV_PREFIX + f.name.upper())
        if env_raw is not None:
            value = _coerce(f.name, env_raw, value, source="env")
        opt_key = OPTION_PREFIX + f.name
        if opt_key in opts:
            value = _coerce(f.name, opts[opt_key], value, source="option")
        kwargs[f.name] = value
    return GeoRoutingConfig(**kwargs)


def _alias_surface_is_ambiguous(
    surface: str, keep: Iterable[str], *, iso2: str,
) -> bool:
    """True when this ALIAS-only surface is contained in another COUNTRY's name.

    The exact hazard: "Republic of China" (a curated TW alias) is a substring
    of "People's Republic of China" (China's gazetteer name), and
    ``represented_by``'s multi-word rule is containment — so without this
    guard every China story would name Taiwan. Runs on alias-only surfaces
    only; see the module banner for why canon polities are exempt.

    The comparison EXCLUDES this ISO2's own spellings, because a country's
    short alias is very often contained in its own long name ("Taiwan" in
    "Taiwan, Province of China") and that is the alias doing its job.
    """
    norm = normalize_prose(surface)
    if not norm:
        return True
    kept = set(keep)
    code = str(iso2 or "").upper()
    for polity, surfaces in _CANON_SURFACES_BY_POLITY.items():
        if polity in kept:
            continue
        for other in surfaces:
            if norm != other and norm in other:
                return True
    for other_code, names in _official_names_by_iso2().items():
        if other_code == code:
            continue
        for other in names:
            if norm != other and norm in other:
                return True
    return False


def polity_names_for_iso2(iso2: str) -> tuple[str, ...]:
    """Every polity NAME whose surfaces count as naming this ISO2's country.

    The union of both gazetteers the canon already depends on, class-gated to
    ``country``, with the curated home aliases added as literal surfaces so
    "Türkiye" and "UK" are not lost to the canonical spelling. Falls back to
    :func:`~legba.data._polity_match.home_country_name` when the class gate
    keeps nothing, so an ISO2 the canon does not carry still resolves to
    something rather than to silence.
    """
    code = str(iso2 or "").upper()
    if not code:
        return ()
    aliases = tuple(_ISO2_HOME_ALIASES.get(code, ()))
    raw = list(_pycountry_names(code)) + list(aliases)
    kept: list[str] = []
    for name in raw:
        canonical, cls = canonicalize_entity(str(name), "country")
        if not canonical or cls != "country" or canonical not in _CANON:
            continue
        if canonical not in kept:
            kept.append(canonical)
    for alias in aliases:
        if alias in kept:
            continue
        if _alias_surface_is_ambiguous(alias, kept, iso2=code):
            continue
        kept.append(alias)
    if kept:
        return tuple(kept)
    fallback = home_country_name(code)
    return (fallback,) if fallback else ()


def polity_names_for_iso2s(iso2s: Sequence[str]) -> tuple[str, ...]:
    """:func:`polity_names_for_iso2` over a desk's whole geo scope."""
    out: list[str] = []
    for code in iso2s:
        for name in polity_names_for_iso2(code):
            if name not in out:
                out.append(name)
    return tuple(out)


def surface_index(names: Sequence[str]) -> "PolitySurfaceIndex":
    """The shared batched matcher over these polity names.

    Imported HERE rather than at module scope: ``_frame_anchor`` reaches the
    coverage-floor scan through ``_frame_content``, and the scan imports this
    module — a module-level import would close that cycle. The call happens
    once per slice (or once per scan), so the import cache absorbs it.
    """
    from ._frame_anchor import PolitySurfaceIndex

    return PolitySurfaceIndex(names)


def title_probes(
    names: Sequence[str], *, min_len: int = 3,
) -> tuple[str, ...]:
    """SQL ``ILIKE`` needles that RECALL every title the matcher could accept.

    For each surface: its longest normalized token, plus the raw surface
    lowered. The normalized token is what survives the fold; the raw form is
    what a pre-fold spelling ("Türkiye") looks like in the column. Taking the
    LONGEST token of a multi-word surface keeps the probe as selective as a
    substring test can be while staying a superset of the phrase-or-squeezed
    rule — every token of a matched phrase is present in the folded prose.

    Deliberately a SUPERSET. The exact decision is
    :meth:`PolitySurfaceIndex.names_in`, applied to the rows this returns.
    Amendment 7g narrowed that decision, which can only make this set MORE of a
    superset; the prefilter is unchanged.

    KNOWN RECALL GAP, stated rather than left to be rediscovered. ``min_len``
    drops every probe under three characters, so the two-character surfaces
    ``us`` and ``uk`` never become needles: a title that writes only "US" or
    "UK" — no "America", no "United", no dotted form — is not a candidate for
    the US or GB desk's recovery leg at all. Measured on the live 72 h pool
    (2026-09-09): of the 301 non-``US`` titles the repaired matcher accepts for
    the US desk, **221 are invisible to this prefilter** ("Oil prices breach
    $100 as Iran-US attacks escalate"); for GB it is 59 of 105. AU, IR and RU
    lose nothing — they carry no two-character surface. Closing it means either a
    two-character probe (a very cheap ILIKE over a very expensive fraction of
    the table) or a token-aware SQL clause, and it is a WIDENING with its own
    blast radius — a separate lane, not a side effect of a false-admit repair.
    """
    probes: set[str] = set()
    for name in names:
        for surface in entity_surfaces(name):
            norm = normalize_prose(surface)
            if norm:
                tokens = norm.split()
                if tokens:
                    probes.add(max(tokens, key=len))
            low = str(surface).strip().casefold()
            if low:
                probes.add(low)
    return tuple(sorted(p for p in probes if len(p) >= max(1, int(min_len))))


def admitted_rows(
    rows: Sequence[Mapping[str, Any]],
    index: "PolitySurfaceIndex",
    *,
    config: GeoRoutingConfig,
    row_cap: int,
    title_of: Any = None,
    magnitude_of: Any = None,
    summary_of: Any = None,
) -> list[Mapping[str, Any]]:
    """The recovery leg's admissions: exact-matched, floored, magnitude-first.

    Ordered by magnitude DESC then recency, capped at
    ``int(row_cap * admit_share)`` — the ubiquity ceiling. The cap is SHARED
    with the slice's own row cap rather than added to it: an admitted row
    displaces one the geo leg would have carried, so the slice stays exactly
    the size the desk's prompt budget was sized for.
    """
    get_title = title_of or (lambda r: r.get("title") or "")
    get_mag = magnitude_of or (lambda r: r.get("magnitude"))
    get_summary = summary_of or (lambda r: r.get("summary") or "")
    floor = float(config.min_magnitude)
    read_summary = bool(int(config.match_summary))
    scored: list[tuple[float, Mapping[str, Any]]] = []
    for row in rows:
        prose = str(get_title(row) or "")
        if read_summary:
            prose = f"{prose}\n{str(get_summary(row) or '')}"
        if not prose.strip():
            continue
        if not index.names_in(prose):
            continue
        try:
            mag = float(get_mag(row))
        except (TypeError, ValueError):
            mag = 0.0
        if mag < floor:
            continue
        scored.append((mag, row))
    scored.sort(key=lambda t: t[0], reverse=True)
    cap = max(0, int(int(row_cap) * float(config.admit_share)))
    return [row for _mag, row in scored[:cap]]


# ---------------------------------------------------------------------------
# THE RECEIPT — the same rule, asked of the whole fleet at once
#
# The coverage-floor detector's clause 4 is a SLICE SHARE, so it is
# structurally unreachable for material that never entered the slice: the
# 2026-09-07 audit found India<->China at 0 of 27 and Turkey<->Syria/Israel at
# 0 of 12, and concluded that *the same routing gap that caused the miss hides
# it from the detector*. This counter is that gap, made a line in the scan's
# receipt. It lives HERE rather than in the scan because it is the same
# question the slice leg above asks, and two spellings of one question is how
# two answers start.
# ---------------------------------------------------------------------------

#: Bound on the routed-elsewhere sweep's single fetch. High-magnitude signals
#: in a 14-day window run ~5k live; the bound is a cost ceiling, not a filter,
#: and hitting it is reported rather than swallowed.
MAX_ROUTED_ROWS = 20_000

#: How many desks the routed-elsewhere breakdown names in the receipt. The
#: fleet is 32 desks; the receipt wants the shape of the gap, not a census.
MAX_ROUTED_TARGETS_IN_RECEIPT = 12

#: High-magnitude signals in the window, with the geo the router chose and the
#: title the newsroom wrote. No desk join: the desk × row attribution happens
#: in Python, where the canon's matcher lives.
ROUTED_ELSEWHERE_SQL = """
    SELECT s.geo AS geo, s.payload ->> 'title' AS title
      FROM signals s
     WHERE s.fetched_at > now() - make_interval(days => $1)
       AND (s.salience ->> 'magnitude')::float >= $2
       AND s.payload ->> 'title' IS NOT NULL
       AND s.payload ->> 'title' <> ''
       AND (s.canonical_signal_id IS NULL OR s.canonical_signal_id = s.id)
     LIMIT $3
"""


async def count_routed_elsewhere(
    conn: Any,
    *,
    desks: Sequence[Mapping[str, Any]],
    parse_jsonish: Any,
    window_days: int,
    high_magnitude: float,
    config: GeoRoutingConfig,
    stats: dict[str, Any],
) -> dict[str, int]:
    """Per desk: high-magnitude signals that NAME it and were routed away.

    WHY THIS COUNTER EXISTS. The detector's clause 4 is a SLICE SHARE — a
    cluster has to be a tenth of the desk's own window to breach. That clause
    is unreachable for the failure mode where the material never entered the
    slice at all: the 2026-09-07 audit found India↔China at 0 of 27 and
    Turkey↔Syria/Israel at 0 of 12, so the entity census that feeds the bar
    never counted a single one of them. *The same routing gap that caused the
    miss hides it from the detector.* This number is the gap made visible.

    It REPORTS and does not gate: no watermark, no candidate, no dispatch, no
    threshold. It is a line in the receipt an operator reads next to
    ``breaches``, and nothing downstream branches on it.

    One fold per row for the whole fleet, not one per (row, desk): every
    desk's polities go into ONE shared index and
    :meth:`PolitySurfaceIndex.names_in_folded` is asked once per title — the
    batched entry point that exists for exactly this shape.
    """
    claims: dict[str, list[tuple[str, set[str]]]] = {}
    names: list[str] = []
    for desk_row in desks:
        target_id = str(desk_row["descriptor_id"])
        iso2s = {
            str(g) for g in (parse_jsonish(desk_row["geo"]) or [])
            if isinstance(g, str) and g
        }
        if not iso2s:
            continue
        for polity in polity_names_for_iso2s(sorted(iso2s)):
            if polity not in names:
                names.append(polity)
            claims.setdefault(polity, []).append((target_id, iso2s))
    if not names:
        return {}
    # The bar is the scan's own ``high_magnitude``, raised to the recovery
    # leg's ``min_magnitude`` when an operator has set one. Counting at the
    # SAME floor the flag-on leg would admit at is what makes this number
    # predictive of the flip rather than merely descriptive of the gap.
    bar = max(float(high_magnitude), float(config.min_magnitude))
    rows = await conn.fetch(
        ROUTED_ELSEWHERE_SQL,
        int(window_days),
        bar,
        int(MAX_ROUTED_ROWS),
    )
    if len(rows) >= MAX_ROUTED_ROWS:
        stats["routed_elsewhere_bound_hit"] = 1
        logger.warning(
            "coverage_floor_scan.routed_elsewhere_bound_hit rows=%d — the "
            "count is a FLOOR; nothing else in the scan is affected",
            len(rows),
        )
    index = surface_index(names)
    out: dict[str, int] = {}
    for row in rows:
        title = str(row["title"] or "")
        if not title.strip():
            continue
        hits = index.names_in_folded(normalize_prose(title))
        if not hits:
            continue
        geo = {str(g) for g in (row["geo"] or []) if isinstance(g, str)}
        for polity in hits:
            for target_id, iso2s in claims.get(polity, ()):
                # Routed ELSEWHERE = the desk's own polity is in the title and
                # the router put the row outside the desk's geo scope, so
                # ``geo && target.geo`` — the one clause that builds the desk's
                # slice — cannot admit it.
                if iso2s & geo:
                    continue
                out[target_id] = out.get(target_id, 0) + 1
    return out


__all__ = [
    "CANON_POLITIES",
    "ENV_PREFIX",
    "MAX_ROUTED_ROWS",
    "MAX_ROUTED_TARGETS_IN_RECEIPT",
    "ROUTED_ELSEWHERE_SQL",
    "count_routed_elsewhere",
    "GeoRoutingConfig",
    "OPTION_PREFIX",
    "SLICE_GEO_V2_ENV",
    "admitted_rows",
    "config_from_options",
    "polity_names_for_iso2",
    "polity_names_for_iso2s",
    "slice_geo_v2_enabled",
    "surface_index",
    "title_probes",
]
