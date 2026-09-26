# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE STANDING EXTERNAL-TRUTH SURFACE — W-6 publication + W-7 badge (§3.2-3.4).

WHAT THIS MODULE IS. ``round_lineage.py`` holds five frozen numbers per R-round
and is DB-less by design; it must stay so, because a badge that changes
retroactively is a badge nobody can audit. This is its sibling: the accessor and
the arithmetic for the STANDING number — ~470 graded claims a day, living in the
``external_grades`` ledger, aggregated into the block the eval scorecard renders
and the badge the reader sees.

The two modules answer different questions and are deliberately not merged:

  * ``round_lineage`` — *"what did human raters find, in a round, on a
    population that existed then?"* Five numbers, append-only, no SQL.
  * this module      — *"how true is what the reader reads THIS WEEK?"* A rate,
    recomputed daily, with its own instrument health beside it.

FIVE RULES THIS MODULE ENFORCES AS CODE.

1. **The headline is a PAIR, never a sum (F-12).** The record (the desks, quoted
   verbatim under the assembly) and the voice (the Assessment, which writes its
   own sentences) are two different acts of authorship. :func:`headline_pair`
   returns both with both n's, and there is deliberately NO function on this
   module that returns one number for the front page. Summing them would pool
   the thing the demotion program spent five trains separating.
2. **NOT_FOUND is a statement about the search, never about the world.** It is
   excluded from numerator AND denominator, exactly as ``correctness_axis``
   excludes ``unresolvable`` (its ``WEIGHTS`` dict simply has no entry). So are
   UNCHECKED and UNCHECKABLE. ``decided_rate`` publishes how much of the read the
   instrument actually reached, and it travels on every surface the accuracy
   travels on — today's live sweep decided 2 of 6.
3. **Below the floor the value is ``None``, never ``0.0``.** ``0.0`` on a badge
   reads as a measured failure. n per stratum will be small for a long time (the
   world tier makes ~110 claims/week, the Assessment ~56), so most sub-strata are
   honest-null for weeks and say so.
4. **Strata are never pooled.** :data:`AXIS_KEYS` names the keys this axis owns
   and :func:`assert_not_pooled` raises if a faithfulness or calibration
   aggregate carries one — the ``correctness_axis`` idiom, applied to the new
   axis from its first row rather than retrofitted.
5. **An unmeasured number says a WORD.** :func:`standing_state` returns
   ``unmeasured`` / ``instrument_limited`` / ``sampled`` / ``measured``, and the
   reader renders the first three as prose. ``instrument_limited`` means the two
   grader families did not agree with each other often enough this week for the
   number to be reported at all — the D4 stop rule, made continuous.

HONEST-NULL BEFORE THE LEDGER EXISTS. ``external_grades`` (migration 0190) is
written by a different train. Every read here is defensive in the way the
``/eval/correctness`` route's own reads are: a missing table, a missing column or
a broken connection degrades to the empty board — ``available: false``, zero
populations, the honesty note intact — never a fabricated row and never a 500.
:func:`read_external_truth` is tested against a connection that raises.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from pydantic import BaseModel, Field

from .external_span_check import (
    TIER_CLASS_1,
    TIER_CLASS_2,
    TIER_CLASS_3,
    TIER_CLASS_UNKNOWN,
    VERDICT_CONTRADICTED,
    VERDICT_NOT_FOUND,
    VERDICT_SUPPORTED,
    VERDICT_UNCHECKABLE,
    VERDICT_UNCHECKED,
)

# ---------------------------------------------------------------------------
# THE POPULATIONS (§0.4 — three, never pooled)
# ---------------------------------------------------------------------------

#: The RECORD: verbatim desk sentences carried under ``assembly.v1``. Grading it
#: grades the DESKS — the composition authors nothing, so its construction error
#: is 1.0 by arithmetic. Anyone reading this as "the composition tier's accuracy"
#: is reading it wrong, which is why :data:`RECORD_STANDING_NOTE` rides it.
POPULATION_ASSEMBLY_SPAN = "assembly_span"
#: The VOICE: the Assessment channel's own sentences. Its own act of authorship
#: and its own number.
POPULATION_ASSESSMENT_SENTENCE = "assessment_sentence"
#: Flag-off / pre-flip composition prose. Kept so the A/B arm has a control arm
#: and so a number measured before the flip is never pooled with one after.
POPULATION_LEGACY_PROSE = "legacy_prose"

POPULATIONS: tuple[str, ...] = (
    POPULATION_ASSEMBLY_SPAN,
    POPULATION_ASSESSMENT_SENTENCE,
    POPULATION_LEGACY_PROSE,
)

#: Display order and human labels, so three surfaces cannot spell them three ways.
POPULATION_LABELS: Mapping[str, str] = {
    POPULATION_ASSEMBLY_SPAN: "the record (desk sentences, quoted)",
    POPULATION_ASSESSMENT_SENTENCE: "the voice (the Assessment's own sentences)",
    POPULATION_LEGACY_PROSE: "legacy prose (flag-off / pre-flip)",
}

# ---------------------------------------------------------------------------
# THE STRATA (§3.3)
# ---------------------------------------------------------------------------

STRATUM_REGIME = "regime"
STRATUM_RETRIEVAL_ORIGIN = "retrieval_origin"
STRATUM_SEVERITY = "severity"
STRATUM_CLAIM_SHAPE = "claim_shape"
STRATUM_TIER = "tier"
#: F-3, taken on the VISIBLE option: the decisive source's tier class is its own
#: stratum so an unregistered-domain verdict is counted and reported separately
#: rather than silently suppressed into Tier 3.
STRATUM_SOURCE_TIER = "source_tier"

STRATA: tuple[str, ...] = (
    STRATUM_REGIME,
    STRATUM_RETRIEVAL_ORIGIN,
    STRATUM_SEVERITY,
    STRATUM_CLAIM_SHAPE,
    STRATUM_TIER,
    STRATUM_SOURCE_TIER,
)

SOURCE_TIER_CLASSES: tuple[str, ...] = (
    TIER_CLASS_1,
    TIER_CLASS_2,
    TIER_CLASS_3,
    TIER_CLASS_UNKNOWN,
)

#: Desk tier, off ``analyst_id`` (§3.3's example spells the stratum with these
#: values). ``voice`` is the Assessment channel, kept distinct from the record
#: tiers it reads.
_DESK_TIER_BY_ANALYST: Mapping[str, str] = {
    "world_assessor": "world",
    "country_composition": "country",
    "region_composition": "region",
    "escalation_composition": "thematic",
    "world_assessment": "voice",
    # P3 LANE A — the per-country voice. The SAME stratum as the world voice
    # (both author; neither is a record), stratified apart from the country
    # RECORD it reads, which is the distinction this map exists to draw.
    "country_assessment": "voice",
}


def desk_tier_for_analyst(analyst_id: object) -> str:
    """The read's tier, or ``"other"``. Never raises, never invents a tier."""
    return _DESK_TIER_BY_ANALYST.get(str(analyst_id or ""), "other")


# ---------------------------------------------------------------------------
# THE FLOORS AND THE INSTRUMENT BANDS
# ---------------------------------------------------------------------------

#: Below this many DECIDED claims a stratum publishes ``None`` and its n, never a
#: number. Matched to ``correctness_axis.MIN_UNIT_LABELS`` deliberately: the point
#: is that the two judge-independent axes apply the same discipline, not that 10
#: is magic.
MIN_DECIDED = 10

#: The fleet headline's own floor — the same relationship to :data:`MIN_DECIDED`
#: that ``correctness_axis.MIN_FLEET_LABELS`` has to its per-unit floor.
MIN_DECIDED_HEADLINE = 30

#: §2.4's pre-registered bands, applied continuously to the rolling 7-day RAW
#: agreement between the primary grader and the fourth-family audit rater over
#: the byte-identical evidence envelope. R1 published 0.774, R2 0.804, R3 0.7451
#: — and R3 died on exactly this line.
BAND_STANDS = "stands"
BAND_CONTINGENT = "contingent"
BAND_LIMITED = "instrument_limited"
OVERLAP_STANDS_AT = 0.80
OVERLAP_FLOOR = 0.75

#: Below this many double-graded claims the overlap itself is not a measurement,
#: so the band is withheld rather than computed off a handful. R3's overlap n was
#: 51; the standing sample is ~329/week, so this floor binds only in the first
#: days of a pipeline-version bump.
MIN_OVERLAP_N = 30


def overlap_band(overlap_raw: float | None, overlap_n: int) -> str | None:
    """The pre-registered band for a rolling overlap, or ``None`` if unmeasured.

    ``None`` is a real answer and is NOT :data:`BAND_LIMITED`: "the graders have
    not been compared yet" and "the graders disagreed" are different facts, and a
    badge that renders the first as the second would be lying in the direction
    that looks rigorous.
    """
    if overlap_raw is None or overlap_n < MIN_OVERLAP_N:
        return None
    if overlap_raw >= OVERLAP_STANDS_AT:
        return BAND_STANDS
    if overlap_raw >= OVERLAP_FLOOR:
        return BAND_CONTINGENT
    return BAND_LIMITED


# ---------------------------------------------------------------------------
# NEVER POOLED
# ---------------------------------------------------------------------------

AXIS_NAME = "external_truth"

#: Keys this axis owns. Any aggregate that is not the external-truth axis must
#: not carry them — see :func:`assert_not_pooled`.
AXIS_KEYS: tuple[str, ...] = (
    "external_truth",
    "standing_accuracy",
    "n_decided",
    "decided_rate",
    "n_uncheckable",
    "overlap_raw",
    "instrument_limited",
)


def assert_not_pooled(payload: Mapping[str, Any], *, what: str) -> None:
    """Raise if a NON-external-truth aggregate carries an external-truth key.

    The same enforcement point ``correctness_axis.assert_not_pooled`` is, for the
    same reason: the rule was written down for the operator axis and nothing
    enforced it until a review found the weighting reimplemented in SQL. This
    axis gets the guard from its first row.
    """
    leaked = sorted(k for k in AXIS_KEYS if k in payload)
    if leaked:
        raise AssertionError(
            f"{what} carries external-truth key(s) {leaked}: the standing "
            "external-truth axis is web-grounded and judge-independent, and "
            "must never be pooled into a faithfulness / calibration / operator "
            "gold-set aggregate (design §3.3)."
        )


# ---------------------------------------------------------------------------
# THE ARITHMETIC (one implementation, shared by the route and the badge)
# ---------------------------------------------------------------------------

_DECIDED = (VERDICT_SUPPORTED, VERDICT_CONTRADICTED)


def score_verdicts(
    counts: Mapping[str, int], *, min_decided: int = MIN_DECIDED
) -> dict[str, Any]:
    """Score one cell from its verdict counts. The ONE place the arithmetic lives.

    ``accuracy = supported / (supported + contradicted)`` — ``None``, never
    ``0.0``, below ``min_decided``. A real ``0.0`` means every decided claim was
    CONTRADICTED, and that must remain distinguishable from "nothing decided".

    ``decided_rate = decided / searched``, where ``searched`` counts only the
    claims the search plane actually answered about (SUPPORTED + CONTRADICTED +
    NOT_FOUND). UNCHECKED never reached the world and UNCHECKABLE was never the
    kind of thing that has one; both are reported in their own counts and enter
    neither ratio.
    """
    supported = int(counts.get(VERDICT_SUPPORTED, 0) or 0)
    contradicted = int(counts.get(VERDICT_CONTRADICTED, 0) or 0)
    not_found = int(counts.get(VERDICT_NOT_FOUND, 0) or 0)
    unchecked = int(counts.get(VERDICT_UNCHECKED, 0) or 0)
    uncheckable = int(counts.get(VERDICT_UNCHECKABLE, 0) or 0)

    n_decided = supported + contradicted
    n_searched = n_decided + not_found
    sufficient = n_decided >= int(min_decided)
    return {
        "accuracy": (supported / n_decided) if sufficient and n_decided else None,
        "supported": supported,
        "contradicted": contradicted,
        "not_found": not_found,
        "n_decided": n_decided,
        "n_searched": n_searched,
        "n_unchecked": unchecked,
        "n_uncheckable": uncheckable,
        "decided_rate": (n_decided / n_searched) if n_searched else None,
        "sufficient": sufficient,
        "min_decided": int(min_decided),
    }


def describe_population(record: Mapping[str, Any]) -> str:
    """One human line for a population — never a bare ratio.

    ``the record 0.71 (n=1,148 decided of 3,290 searched; decided rate 0.35)``
    or ``the record — unmeasured: 4 decided claims, below the 10 floor``.
    """
    label = POPULATION_LABELS.get(
        str(record.get("population")), str(record.get("population"))
    )
    n_decided = int(record.get("n_decided") or 0)
    n_searched = int(record.get("n_searched") or 0)
    value = record.get("accuracy")
    if value is None:
        return (
            f"{label} — unmeasured: {n_decided} decided claim"
            f"{'' if n_decided == 1 else 's'}, below the "
            f"{int(record.get('min_decided') or MIN_DECIDED)} floor"
        )
    rate = record.get("decided_rate")
    rate_txt = "—" if rate is None else f"{float(rate):.2f}"
    return (
        f"{label} {float(value):.2f} (n={n_decided:,} decided of "
        f"{n_searched:,} searched; decided rate {rate_txt})"
    )


def _empty_counts() -> dict[str, int]:
    return {
        VERDICT_SUPPORTED: 0,
        VERDICT_CONTRADICTED: 0,
        VERDICT_NOT_FOUND: 0,
        VERDICT_UNCHECKED: 0,
        VERDICT_UNCHECKABLE: 0,
    }


def _stratum_keys(row: Mapping[str, Any]) -> dict[str, str | None]:
    """The six stratum values one aggregate row falls into.

    ``retrieval_origin`` is ``None`` today on every live row — the column is NULL
    on all 23,774 signals fetched in the last 7 days — so the stratum ships
    empty and fills itself the day the research lane's write path lands. That is
    exactly what the A/B label was specified for, and an empty stratum is the
    honest render of it.
    """
    return {
        STRATUM_REGIME: _text(row.get("assembly_regime")) or "legacy",
        STRATUM_RETRIEVAL_ORIGIN: _text(row.get("retrieval_origin")),
        STRATUM_SEVERITY: _text(row.get("severity")) or "unstated",
        STRATUM_CLAIM_SHAPE: _text(row.get("claim_shape")) or "positive",
        STRATUM_TIER: _text(row.get("desk_tier")) or "other",
        STRATUM_SOURCE_TIER: _text(row.get("source_tier_class")),
    }


def _text(value: object) -> str | None:
    if value is None:
        return None
    out = str(value).strip()
    return out or None


def score_population_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    min_decided: int = MIN_DECIDED,
) -> dict[str, dict[str, Any]]:
    """Fold :data:`VERDICT_AGGREGATE_SQL` rows into ``{population: record}``.

    Each record carries its own totals plus its ``strata`` — one scored cell per
    stratum value. Cells are computed independently and NEVER summed across
    populations: three populations means three records, and there is no fourth.
    """
    totals: dict[str, dict[str, int]] = {}
    strata: dict[str, dict[str, dict[str, dict[str, int]]]] = {}

    for row in rows:
        population = _text(row.get("population")) or POPULATION_LEGACY_PROSE
        verdict = str(row.get("verdict") or "").upper()
        n = int(row.get("n") or 0)
        if verdict not in _empty_counts():
            continue
        totals.setdefault(population, _empty_counts())[verdict] += n
        pop_strata = strata.setdefault(population, {})
        for axis, value in _stratum_keys(row).items():
            if value is None:
                continue
            cell = pop_strata.setdefault(axis, {}).setdefault(value, _empty_counts())
            cell[verdict] += n

    out: dict[str, dict[str, Any]] = {}
    for population, counts in totals.items():
        record = score_verdicts(counts, min_decided=min_decided)
        record["population"] = population
        record["label"] = POPULATION_LABELS.get(population, population)
        record["strata"] = {
            axis: {
                value: {
                    **score_verdicts(cell, min_decided=min_decided),
                    "stratum": axis,
                    "value": value,
                }
                for value, cell in sorted(values.items())
            }
            for axis, values in sorted(strata.get(population, {}).items())
        }
        # Every stratum the design names is PRESENT even when empty. An absent
        # key reads as "not measured here"; an empty one reads as "measured,
        # nothing in it" — and for retrieval_origin today the second is true.
        for axis in STRATA:
            record["strata"].setdefault(axis, {})
        record["display"] = describe_population(record)
        out[population] = record
    return out


def headline_pair(
    populations: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """F-12 — the world-facing headline as a PAIR, record and voice.

    *record 0.712 (n=1,148) · voice 0.68 (n=56)*. There is deliberately no
    function on this module that sums them: they are two different acts of
    authorship, and one number over both would pool the exact distinction the
    demotion program exists to make. If a front page ever wants one number, that
    is an API-shape decision an operator takes explicitly (F-12's overrule),
    not an arithmetic convenience taken here.
    """
    record = populations.get(POPULATION_ASSEMBLY_SPAN)
    voice = populations.get(POPULATION_ASSESSMENT_SENTENCE)

    def _side(rec: Mapping[str, Any] | None, what: str) -> dict[str, Any]:
        if not rec:
            return {"what": what, "accuracy": None, "n_decided": 0,
                    "display": f"{what} — not graded yet"}
        return {
            "what": what,
            "accuracy": rec.get("accuracy"),
            "n_decided": int(rec.get("n_decided") or 0),
            "decided_rate": rec.get("decided_rate"),
            "display": str(rec.get("display") or ""),
        }

    return {
        "record": _side(record, "record"),
        "voice": _side(voice, "voice"),
        "note": (
            "Two numbers, never one. The record is the DESKS' sentences quoted "
            "verbatim; the voice is the Assessment's own. Summing them would "
            "pool two different acts of authorship."
        ),
    }


# ---------------------------------------------------------------------------
# THE BADGE (§3.4, W-7)
# ---------------------------------------------------------------------------

STANDING_MEASURED = "measured"
STANDING_UNMEASURED = "unmeasured"
STANDING_INSTRUMENT_LIMITED = "instrument_limited"
STANDING_SAMPLED = "sampled"

#: §3.4's own words: the demotion in one line, and the honest thing to print.
#: It answers, on the page, the mock's fourth note — *"a badge that can read 1.00
#: on a page containing a fact its own desk denies is not a badge"*.
RECORD_STANDING_NOTE = (
    "the record's own construction is 1.0 by arithmetic; this number is the "
    "truth of the DESK SENTENCES it quotes"
)

#: The voice's note. The Assessment writes its own sentences, so its standing
#: number is about the channel itself — unlike the borrowed R3 prose-tier number
#: the badge also carries, which is about the tier this program demoted.
VOICE_STANDING_NOTE = (
    "the voice's own population: sentences this channel wrote, graded against "
    "the world by the standing loop"
)

#: The reader's sentence for each non-measured state. A word, not a number — and
#: rendered as prose, because a bare 'instrument_limited: true' beside a figure
#: reads as a footnote on a number that should not be shown at all.
STANDING_STATE_COPY: Mapping[str, str] = {
    STANDING_UNMEASURED: (
        "not graded against the world yet — the standing loop has not decided "
        "enough claims on this population to report a rate"
    ),
    STANDING_INSTRUMENT_LIMITED: (
        "the graders did not agree with each other often enough this week for "
        "this number to be reported"
    ),
    STANDING_SAMPLED: (
        "graded on a sample of the week's claims — the daily budget was "
        "exhausted, so this rate covers part of the read, not all of it"
    ),
}


def standing_state(
    record: Mapping[str, Any] | None,
    *,
    instrument_limited: bool = False,
    sample_fraction: float | None = None,
) -> str:
    """The badge's state WORD. ``unmeasured`` is not ``0.0``.

    Order is deliberate: an instrument that failed its own reliability band is
    reported as limited even when n is plentiful, because a well-powered number
    from two graders who disagree is not a better number — it is a
    better-disguised one (R3's whole stop rule).
    """
    if instrument_limited:
        return STANDING_INSTRUMENT_LIMITED
    if not record or record.get("accuracy") is None:
        return STANDING_UNMEASURED
    if sample_fraction is not None and float(sample_fraction) < 1.0:
        return STANDING_SAMPLED
    return STANDING_MEASURED


def standing_accuracy(
    record: Mapping[str, Any] | None,
    *,
    window_days: int = 7,
    grader_family: str | None = None,
    instrument_limited: bool = False,
    sample_fraction: float | None = None,
) -> dict[str, Any] | None:
    """§3.4's ``standing_accuracy`` block, or ``None`` when there is nothing.

    The same contract ``round_lineage.external_accuracy`` keeps: the value never
    travels without its population, its n and its window, and there is no
    accessor that returns the float alone. ``value`` is ``None`` — with the n
    still attached — whenever the state is not ``measured``, so a reader that
    prints ``value`` unconditionally prints an absence rather than a rate the
    instrument did not earn.
    """
    if not record:
        return None
    state = standing_state(
        record,
        instrument_limited=instrument_limited,
        sample_fraction=sample_fraction,
    )
    value = record.get("accuracy")
    # The number is WITHHELD in exactly two states, and shown in the other two.
    # ``unmeasured`` has no number to show; ``instrument_limited`` has one and
    # must not show it — a well-powered rate from two graders who disagree is
    # not a better number, it is a better-disguised one. ``sampled`` DOES show
    # its rate, with the fraction it covers riding beside it (§3.4: "sampled
    # renders the fraction"), because a partial measurement is still a
    # measurement as long as it says how partial.
    withheld = state in (STANDING_UNMEASURED, STANDING_INSTRUMENT_LIMITED)
    return {
        "value": None if withheld or value is None else round(float(value), 4),
        "state": state,
        "n_decided": int(record.get("n_decided") or 0),
        "n_searched": int(record.get("n_searched") or 0),
        "decided_rate": (
            None if record.get("decided_rate") is None
            else round(float(record["decided_rate"]), 4)
        ),
        "window_days": int(window_days),
        "population": str(record.get("population") or ""),
        "grader_family": grader_family,
        "instrument_limited": bool(instrument_limited),
        "sample_fraction": (
            None if sample_fraction is None else round(float(sample_fraction), 4)
        ),
        "display": str(record.get("display") or ""),
        "state_note": STANDING_STATE_COPY.get(state),
    }


def record_standing_badge(
    standing: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """The RECORD side of §3.4's badge — the spine's number plus its one line.

    Kept separate from the voice's so the pair can never be summed by a reader
    reaching for a single key (F-12). ``None`` in, ``None`` out: the badge then
    carries no record-standing key at all rather than a null pretending to be a
    measurement.
    """
    if standing is None:
        return None
    return {"standing_accuracy": dict(standing), "standing_note": RECORD_STANDING_NOTE}


# ---------------------------------------------------------------------------
# THE ACCESSOR (§3.1 columns, read-only; another train owns the writer)
# ---------------------------------------------------------------------------

#: The verdict / strata aggregate. GROUP BY in SQL, arithmetic in Python — the
#: ``correctness_axis`` split, for the reason that review found: weights
#: hand-rolled in SQL in one place and in prose in another are one divergence
#: away from two different numbers called the same thing.
#:
#: ``source_tier_class`` is DERIVED from ``decisive_source_tier`` rather than
#: stored: NULL on a decisive row means the domain was not in the registry, which
#: is F-3's ``tier_unknown`` — the class that exists so the suppression is
#: visible. ``retrieval_origin`` collapses the mix jsonb to its single key when
#: there is one; today the column is NULL fleet-wide and the stratum ships empty.
VERDICT_AGGREGATE_SQL = """
    SELECT g.population,
           g.verdict,
           COALESCE(NULLIF(g.assembly_regime, ''), 'legacy')      AS assembly_regime,
           COALESCE(NULLIF(g.claim_severity, ''), 'unstated')     AS severity,
           CASE WHEN g.absence_shaped THEN 'absence' ELSE 'positive' END
                                                                  AS claim_shape,
           CASE g.analyst_id
                WHEN 'world_assessor'          THEN 'world'
                WHEN 'country_composition'     THEN 'country'
                WHEN 'region_composition'      THEN 'region'
                WHEN 'escalation_composition'  THEN 'thematic'
                WHEN 'world_assessment'        THEN 'voice'
                WHEN 'country_assessment'      THEN 'voice'
                ELSE 'other'
           END                                                    AS desk_tier,
           CASE
                WHEN g.verdict NOT IN ('SUPPORTED', 'CONTRADICTED') THEN NULL
                WHEN g.decisive_source_tier = 1 THEN 'tier_1'
                WHEN g.decisive_source_tier = 2 THEN 'tier_2'
                WHEN g.decisive_source_tier = 3 THEN 'tier_3'
                ELSE 'tier_unknown'
           END                                                    AS source_tier_class,
           (SELECT CASE WHEN count(*) = 0 THEN NULL
                        WHEN count(*) = 1 THEN min(k)
                        ELSE 'mixed' END
              FROM jsonb_object_keys(g.retrieval_origin_mix) k)   AS retrieval_origin,
           count(*)::int                                          AS n
      FROM public.external_grades g
     WHERE g.rater_role = 'primary'
       AND g.graded_at >= now() - make_interval(days => $1::int)
     GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
"""

#: The instrument + search + sampling aggregate. One row per grader identity ×
#: search condition, so a week when SearXNG lost eight engines is legible as a
#: search condition rather than as the world getting quieter (§3.5.1).
CONDITIONS_AGGREGATE_SQL = """
    SELECT g.population,
           g.grader_family,
           g.grader_model_name,
           g.grader_pipeline_version,
           COALESCE(NULLIF(g.search_provider, ''), 'unknown')  AS search_provider,
           g.search_degraded,
           COALESCE(NULLIF(g.search_liveness, ''), 'unknown')  AS search_liveness,
           min(g.sample_fraction)::float8                      AS min_sample_fraction,
           count(*)::int                                       AS n
      FROM public.external_grades g
     WHERE g.rater_role = 'primary'
       AND g.graded_at >= now() - make_interval(days => $1::int)
     GROUP BY 1, 2, 3, 4, 5, 6, 7
"""

#: §2.4's rolling raw overlap: the primary grader against the fourth-family audit
#: rater, over the byte-identical cached envelope, on the three-verdict decisive
#: vocabulary — the same statistic R1/R2/R3 published, so the standing number is
#: directly comparable to the rounds. Joined on ``grader_pipeline_version``
#: because pooling an overlap across a stamp bump would describe an instrument
#: that never existed.
OVERLAP_SQL = """
    SELECT p.population,
           count(*)::int                                          AS shared,
           count(*) FILTER (WHERE p.verdict = a.verdict)::int      AS agree,
           count(DISTINCT a.grader_family)::int                    AS audit_families
      FROM public.external_grades p
      JOIN public.external_grades a
        ON a.claim_key = p.claim_key
       AND a.rater_role = 'audit'
       AND a.grader_pipeline_version = p.grader_pipeline_version
     WHERE p.rater_role = 'primary'
       AND p.graded_at >= now() - make_interval(days => $1::int)
       AND p.verdict IN ('SUPPORTED', 'CONTRADICTED', 'NOT_FOUND')
       AND a.verdict IN ('SUPPORTED', 'CONTRADICTED', 'NOT_FOUND')
     GROUP BY 1
"""

HONESTY_NOTE = (
    "Standing external truth is WEB-GROUNDED and judge-independent: it grades "
    "the read's claims against sources outside this pipeline, and it is never "
    "pooled with faithfulness, calibration or the operator gold set. NOT_FOUND "
    "is a statement about the SEARCH, never about the world — it is excluded "
    "from numerator and denominator both, and decided_rate publishes how much "
    "of the read the instrument actually reached. Grading the assembly grades "
    "the DESKS: the composition tier authors nothing under assembly.v1, so its "
    "construction error is 1.0 by arithmetic. The three populations are never "
    "summed; the headline is a pair. Below the n floor a stratum publishes null "
    "and its n, never a number, and a week whose two grader families disagreed "
    "publishes a sentence instead of a rate."
)


def _rows(value: object) -> list[Mapping[str, Any]]:
    return [r for r in value if isinstance(r, Mapping)] if isinstance(
        value, (list, tuple)
    ) else []


async def read_external_truth(conn: Any, *, days: int = 7) -> dict[str, Any]:
    """Read the three aggregates. Honest-null on ANY failure; never raises.

    Migration 0190 is written by a different train, so this accessor must be
    correct BEFORE the table exists: an ``UndefinedTableError`` is not an error
    condition here, it is "not measured yet", and the board it produces says so.
    Every read is caught separately so a broken overlap query cannot take the
    verdict counts down with it.
    """
    window = max(1, int(days))
    out: dict[str, Any] = {
        "days": window, "verdicts": [], "conditions": [], "overlap": [],
        "available": False,
    }
    try:
        out["verdicts"] = _rows(list(await conn.fetch(VERDICT_AGGREGATE_SQL, window)))
        out["available"] = True
    except Exception:  # noqa: BLE001 — honest empty board, never a 500
        return out
    try:
        out["conditions"] = _rows(
            list(await conn.fetch(CONDITIONS_AGGREGATE_SQL, window))
        )
    except Exception:  # noqa: BLE001
        out["conditions"] = []
    try:
        out["overlap"] = _rows(list(await conn.fetch(OVERLAP_SQL, window)))
    except Exception:  # noqa: BLE001
        out["overlap"] = []
    return out


# ---------------------------------------------------------------------------
# THE WIRE SHAPE (§3.3) — the models the route attaches
# ---------------------------------------------------------------------------


class ExternalTruthCell(BaseModel):
    """One scored cell: a population total, or one stratum value inside it.

    ``accuracy`` is ``None`` below the floor and the n's stay — which is the
    whole tiny-n contract, and the reason ``display`` is composed server-side.
    """

    accuracy: float | None = None
    supported: int = 0
    contradicted: int = 0
    not_found: int = 0
    n_decided: int = 0
    n_searched: int = 0
    n_unchecked: int = 0
    n_uncheckable: int = 0
    decided_rate: float | None = None
    sufficient: bool = False
    min_decided: int = MIN_DECIDED


class ExternalTruthPopulation(ExternalTruthCell):
    """One of the three populations, with its strata and its conditions.

    ``instrument`` carries the rolling grader-vs-grader overlap and the band it
    falls in; ``search`` carries the provider mix and the degradation share,
    because a number produced through a search plane that lost eight engines is
    a different number and the split key must say so; ``sampled`` carries the
    fraction, so a partial day is never published as a whole one.
    """

    population: str
    label: str = ""
    display: str = ""
    strata: dict[str, dict[str, ExternalTruthCell]] = Field(default_factory=dict)
    instrument: dict[str, Any] = Field(default_factory=dict)
    search: dict[str, Any] = Field(default_factory=dict)
    sampled: dict[str, Any] = Field(default_factory=dict)


class ExternalTruthBoard(BaseModel):
    """The ``external_truth`` block on ``GET /v3/eval/correctness`` (§3.3).

    There is deliberately NO top-level accuracy field. The three populations are
    never summed, and the world-facing headline is the PAIR in :attr:`headline`
    — record and voice, each with its own n. A single number over both would
    pool two different acts of authorship (F-12).
    """

    available: bool = False
    window: dict[str, Any] = Field(default_factory=dict)
    populations: list[ExternalTruthPopulation] = Field(default_factory=list)
    headline: dict[str, Any] = Field(default_factory=dict)
    honesty_note: str = HONESTY_NOTE


def _instrument_block(
    overlap_rows: Sequence[Mapping[str, Any]],
    condition_rows: Sequence[Mapping[str, Any]],
    population: str,
) -> dict[str, Any]:
    shared = agree = 0
    for row in overlap_rows:
        if _text(row.get("population")) == population:
            shared += int(row.get("shared") or 0)
            agree += int(row.get("agree") or 0)
    raw = (agree / shared) if shared else None
    band = overlap_band(raw, shared)
    graders = sorted(
        {
            _text(r.get("grader_model_name")) or _text(r.get("grader_family")) or ""
            for r in condition_rows
            if _text(r.get("population")) == population
        }
        - {""}
    )
    versions = sorted(
        {
            _text(r.get("grader_pipeline_version")) or ""
            for r in condition_rows
            if _text(r.get("population")) == population
        }
        - {""}
    )
    return {
        "overlap_raw": None if raw is None else round(raw, 4),
        "overlap_n": shared,
        "band": band,
        "instrument_limited": band == BAND_LIMITED,
        "graders": graders,
        "pipeline_version": versions[-1] if versions else None,
        "pipeline_versions": versions,
    }


def _search_block(
    condition_rows: Sequence[Mapping[str, Any]], population: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = [r for r in condition_rows if _text(r.get("population")) == population]
    total = sum(int(r.get("n") or 0) for r in rows)
    providers: dict[str, int] = {}
    degraded = live = 0
    fractions: list[float] = []
    for row in rows:
        n = int(row.get("n") or 0)
        providers[_text(row.get("search_provider")) or "unknown"] = (
            providers.get(_text(row.get("search_provider")) or "unknown", 0) + n
        )
        if row.get("search_degraded"):
            degraded += n
        if _text(row.get("search_liveness")) == "verified":
            live += n
        frac = row.get("min_sample_fraction")
        if isinstance(frac, (int, float)) and not isinstance(frac, bool):
            fractions.append(float(frac))
    search = {
        "provider_mix": (
            {k: round(v / total, 4) for k, v in sorted(providers.items())}
            if total else {}
        ),
        "degraded_share": round(degraded / total, 4) if total else None,
        "liveness_verified_share": round(live / total, 4) if total else None,
        "n": total,
    }
    fraction = min(fractions) if fractions else None
    sampled = {
        "sample_fraction": None if fraction is None else round(fraction, 4),
        "reason": (
            None if fraction is None or fraction >= 1.0
            else "daily claim/SERP budget exhausted — graded on a hash-gated sample"
        ),
    }
    return search, sampled


def compose_board(read: Mapping[str, Any]) -> ExternalTruthBoard:
    """Build the wire block from :func:`read_external_truth`'s three aggregates.

    An empty read produces ``available: false`` with the honesty note intact —
    the honest empty board, which is what every surface sees until migration
    0190 lands and the auditor's drain starts writing.
    """
    days = int(read.get("days") or 7)
    verdict_rows = _rows(read.get("verdicts"))
    condition_rows = _rows(read.get("conditions"))
    overlap_rows = _rows(read.get("overlap"))
    scored = score_population_rows(verdict_rows)

    populations: list[ExternalTruthPopulation] = []
    for population in POPULATIONS:
        record = scored.get(population)
        if record is None:
            continue
        instrument = _instrument_block(overlap_rows, condition_rows, population)
        search, sampled = _search_block(condition_rows, population)
        populations.append(
            ExternalTruthPopulation(
                **{k: v for k, v in record.items() if k != "strata"},
                strata={
                    axis: {
                        value: ExternalTruthCell(
                            **{
                                k: v for k, v in cell.items()
                                if k not in ("stratum", "value")
                            }
                        )
                        for value, cell in values.items()
                    }
                    for axis, values in record["strata"].items()
                },
                instrument=instrument,
                search=search,
                sampled=sampled,
            )
        )
    board = ExternalTruthBoard(
        available=bool(populations),
        window={"days": days},
        populations=populations,
        headline=headline_pair(scored),
    )
    # The publication's own never-pool guard, run on the way out: if a future
    # edit ever hangs an axis key off the window or the headline's top level,
    # this is where it turns red rather than in a dashboard six weeks later.
    assert_not_pooled(board.window, what="external_truth.window")
    return board


__all__ = [
    "AXIS_KEYS",
    "AXIS_NAME",
    "BAND_CONTINGENT",
    "BAND_LIMITED",
    "BAND_STANDS",
    "CONDITIONS_AGGREGATE_SQL",
    "HONESTY_NOTE",
    "MIN_DECIDED",
    "MIN_DECIDED_HEADLINE",
    "MIN_OVERLAP_N",
    "OVERLAP_FLOOR",
    "OVERLAP_SQL",
    "OVERLAP_STANDS_AT",
    "POPULATIONS",
    "POPULATION_ASSEMBLY_SPAN",
    "POPULATION_ASSESSMENT_SENTENCE",
    "POPULATION_LABELS",
    "POPULATION_LEGACY_PROSE",
    "RECORD_STANDING_NOTE",
    "SOURCE_TIER_CLASSES",
    "STANDING_INSTRUMENT_LIMITED",
    "STANDING_MEASURED",
    "STANDING_SAMPLED",
    "STANDING_STATE_COPY",
    "STANDING_UNMEASURED",
    "STRATA",
    "STRATUM_CLAIM_SHAPE",
    "STRATUM_REGIME",
    "STRATUM_RETRIEVAL_ORIGIN",
    "STRATUM_SEVERITY",
    "STRATUM_SOURCE_TIER",
    "STRATUM_TIER",
    "VERDICT_AGGREGATE_SQL",
    "VOICE_STANDING_NOTE",
    "ExternalTruthBoard",
    "ExternalTruthCell",
    "ExternalTruthPopulation",
    "assert_not_pooled",
    "compose_board",
    "desk_tier_for_analyst",
    "describe_population",
    "headline_pair",
    "overlap_band",
    "read_external_truth",
    "record_standing_badge",
    "score_population_rows",
    "score_verdicts",
    "standing_accuracy",
    "standing_state",
]
