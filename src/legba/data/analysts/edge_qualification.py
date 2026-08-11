# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``edge_qualification`` — the bar a co-mention must clear to be worth typing (K-G2).

The graph is **sparse and evidentiary**: a typed edge is an earned point, not a
full-text hit. Today `relationship_reifier` orders its candidate window by
``proposed_edges.confidence`` alone, and that column is a poor proxy for
"earned" — it is accumulated co-mention weight, so a single wire story
syndicated across nine outlets can lift a pair as hard as nine independent
newsrooms reporting the same relationship. This module is the replacement
ranking: four measured components, combined into one score, with a documented
bar.

**The four components** (each normalised to 0..1, all computed from the live
substrate — no model in the loop):

``multi_source`` (weight 0.45)
    Distinct INDEPENDENT sources backing the pair. Independence is counted over
    ``signals.source_id`` after collapsing near-duplicate content: the same
    story re-published under nine ``signals`` rows sharing one ``content_hash``
    (or one ``canonical_signal_id``) is **one** unit of support, not nine. This
    is the component that carries the "evidentiary" requirement and it is
    weighted highest on purpose.

``source_diversity`` (weight 0.20)
    Whether that support is spread or clustered. Two sources from the same
    ``source_dossiers`` family (two Reuters feeds, two Telegram channels of one
    network) are less independent than two unrelated outlets. Measured as the
    distinct-family count relative to the distinct-source count.

``salience`` (weight 0.20)
    How load-bearing the endpoints are in the corpus, from
    ``signal_entity_links`` mention volume. Damped logarithmically and taken as
    the WEAKER endpoint's salience: a pair is only as interesting as its less
    prominent side, which stops "Trump ↔ <any noise token>" from qualifying on
    Trump's mention count alone.

``desk_relevance`` (weight 0.15)
    Whether the pair sits inside a live desk's remit — the union of the active
    L1 target descriptors' ``scope.geo`` matched against the backing signals'
    ``geo`` codes, plus a direct endpoint match against desk country names.
    A relationship nobody has a desk for is real but not yet worth GPU time.

The weights are a starting position, stated as data (:data:`DEFAULT_WEIGHTS`)
so they can be re-tuned against the measured pool without touching logic.

**Retention.** Below-bar candidates must age out rather than fester — the queue
is 174,595 pending rows today and grows ~9,941/day. :func:`retention_verdict`
implements the policy: a candidate that has not cleared the bar and has not
gained new support within :data:`RETENTION_STALE_DAYS` is retired. Retiring is
a STATUS change, never a delete — the co-mention evidence stays addressable and
a pair that re-earns support is revived by the normal producer path.

Nothing in this module writes. It scores, and it tells the caller what the
policy would say.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

HARNESS_VERSION: str = "0.1.0"

DEFAULT_WEIGHTS: Mapping[str, float] = {
    "multi_source": 0.45,
    "source_diversity": 0.20,
    "salience": 0.20,
    "desk_relevance": 0.15,
}

#: Independent-source count at which ``multi_source`` saturates. Four unrelated
#: outlets is corroboration; a fifth adds little to the DECISION to spend a
#: typing call on the pair.
MULTI_SOURCE_SATURATION: int = 4

#: Mention count at which ``salience`` saturates (log-damped below it).
SALIENCE_SATURATION: int = 500

#: Days without new supporting evidence after which a below-bar candidate is
#: retired. Chosen against the live arrival rate: at ~9,941/day a 30-day window
#: bounds the standing queue near 300k worst-case while giving a slow-burning
#: story a month to accumulate a second source.
RETENTION_STALE_DAYS: int = 30

#: The recommended bar. See docs/TYPING_BAKEOFF_2026-08-03.md for the measured
#: pool size at this and neighbouring settings.
RECOMMENDED_BAR: float = 0.42

#: A hard floor applied ahead of the weighted score: a pair resting on a single
#: independent source is not evidentiary, whatever its other components say.
#: This is the "never full-text-search sludge" rule in one line.
MIN_INDEPENDENT_SOURCES: int = 2


@dataclass
class CandidateEvidence:
    """The measured substrate facts about one candidate co-mention pair.

    Populated by :data:`POOL_SCORING_SQL` (live) or by hand (tests). Every field
    is a count or a flag — no judgement has happened yet.
    """

    #: Distinct ``signals.source_id`` backing the pair, AFTER collapsing rows
    #: that share a ``content_hash`` / ``canonical_signal_id``.
    independent_sources: int = 0
    #: Distinct source FAMILIES among those sources (a family groups feeds of
    #: one publisher/network). Never exceeds ``independent_sources``.
    source_families: int = 0
    #: Raw backing-signal count, including duplicates. Only used for reporting
    #: the syndication ratio — it is deliberately NOT part of the score.
    raw_signals: int = 0
    #: Corpus mention counts for each endpoint.
    subject_mentions: int = 0
    object_mentions: int = 0
    #: Any backing signal's geo intersects an active desk's ``scope.geo``.
    desk_geo_hit: bool = False
    #: An endpoint IS an active desk's subject country/lane.
    desk_entity_hit: bool = False
    #: Days since the newest backing signal.
    age_days: float = 0.0


def multi_source_score(ev: CandidateEvidence) -> float:
    """0..1 in the distinct INDEPENDENT source count, saturating at 4.

    One source scores 0.0, not a small positive: a single-sourced co-mention is
    the null hypothesis (two names in one article), and 92.6 % of the live
    pending pool is exactly that."""
    n = max(0, int(ev.independent_sources))
    if n <= 1:
        return 0.0
    return min(1.0, (n - 1) / (MULTI_SOURCE_SATURATION - 1))


def source_diversity_score(ev: CandidateEvidence) -> float:
    """0..1 — how much of the support comes from DISTINCT publisher families.

    Undefined (0.0) with no sources. With sources but no family metadata the
    caller passes ``source_families == independent_sources`` and this reduces to
    1.0, i.e. diversity is assumed until a family says otherwise — the fold is
    evidence AGAINST independence, never for it."""
    n = max(0, int(ev.independent_sources))
    if n <= 0:
        return 0.0
    fams = max(0, min(int(ev.source_families), n))
    if n == 1:
        return 0.0
    return fams / n


def salience_score(ev: CandidateEvidence) -> float:
    """0..1 from the WEAKER endpoint's corpus mention count, log-damped."""
    weaker = min(max(0, int(ev.subject_mentions)), max(0, int(ev.object_mentions)))
    if weaker <= 0:
        return 0.0
    return min(1.0, math.log1p(weaker) / math.log1p(SALIENCE_SATURATION))


def desk_relevance_score(ev: CandidateEvidence) -> float:
    """0..1 — 1.0 when an endpoint IS a desk subject, 0.6 on a geo overlap."""
    if ev.desk_entity_hit:
        return 1.0
    if ev.desk_geo_hit:
        return 0.6
    return 0.0


def components(ev: CandidateEvidence) -> dict[str, float]:
    """All four normalised components, for reporting and for tuning."""
    return {
        "multi_source": multi_source_score(ev),
        "source_diversity": source_diversity_score(ev),
        "salience": salience_score(ev),
        "desk_relevance": desk_relevance_score(ev),
    }


def qualification_score(
    ev: CandidateEvidence, *, weights: Mapping[str, float] | None = None
) -> float:
    """The weighted 0..1 qualification score."""
    w = dict(weights or DEFAULT_WEIGHTS)
    comp = components(ev)
    total = sum(w.get(k, 0.0) for k in comp)
    if total <= 0:
        return 0.0
    return sum(comp[k] * w.get(k, 0.0) for k in comp) / total


def qualifies(
    ev: CandidateEvidence,
    *,
    bar: float = RECOMMENDED_BAR,
    min_sources: int = MIN_INDEPENDENT_SOURCES,
    weights: Mapping[str, float] | None = None,
) -> bool:
    """Does this candidate earn a typing call?

    Two gates, both must pass: the hard independent-source floor, then the
    weighted score against the bar. The floor is not expressible as a weight —
    a single-sourced pair with a huge salience score would otherwise buy its way
    in, which is exactly the sludge the graph must not accumulate."""
    if int(ev.independent_sources) < int(min_sources):
        return False
    return qualification_score(ev, weights=weights) >= float(bar)


@dataclass
class RetentionVerdict:
    action: str  # "keep" | "retire"
    reason: str


def retention_verdict(
    ev: CandidateEvidence,
    *,
    bar: float = RECOMMENDED_BAR,
    min_sources: int = MIN_INDEPENDENT_SOURCES,
    stale_days: int = RETENTION_STALE_DAYS,
    weights: Mapping[str, float] | None = None,
) -> RetentionVerdict:
    """What the retention policy says about a below-bar candidate.

    Above-bar candidates are always kept (they are the work queue). A below-bar
    candidate is kept while it is still young enough to plausibly gain a second
    source, and retired once it has gone ``stale_days`` without doing so."""
    if qualifies(ev, bar=bar, min_sources=min_sources, weights=weights):
        return RetentionVerdict("keep", "above_bar")
    if ev.age_days < stale_days:
        return RetentionVerdict("keep", "below_bar_still_fresh")
    return RetentionVerdict("retire", "below_bar_stale")


# ---------------------------------------------------------------------------
# The live measurement query
# ---------------------------------------------------------------------------

#: Read-only. Materialises every component of :class:`CandidateEvidence` for the
#: pending pool in one pass so the bar can be swept against real numbers.
#:
#: Placeholders: ``{status_filter}`` — a SQL predicate over ``pe``.
#:
#: Independence is enforced in ``dedup``: backing signals are collapsed on
#: ``coalesce(nullif(content_hash,''), canonical_signal_id::text, id::text)``
#: BEFORE sources are counted, so syndication cannot inflate support.
POOL_SCORING_SQL = """
WITH cand AS (
    SELECT pe.id, pe.source_entity, pe.target_entity, pe.confidence,
           pe.produced_at, pe.derived_from, pe.status
      FROM proposed_edges pe
     WHERE {status_filter}
), expanded AS (
    SELECT c.id AS cid, s.id AS sid, s.source_id, s.geo, s.fetched_at,
           coalesce(nullif(s.content_hash, ''), s.canonical_signal_id::text,
                    s.id::text) AS content_key
      FROM cand c
      CROSS JOIN LATERAL unnest(c.derived_from) AS d(sig_id)
      JOIN signals s ON s.id = d.sig_id
), dedup AS (
    -- one row per (candidate, distinct content) — syndication collapsed
    SELECT DISTINCT ON (cid, content_key)
           cid, content_key, source_id, geo, fetched_at
      FROM expanded
     ORDER BY cid, content_key, fetched_at ASC
), support AS (
    SELECT d.cid,
           count(DISTINCT d.source_id) AS independent_sources,
           -- source_id is 'source.<publisher>.<feed>'; the PUBLISHER is segment
           -- 2. Folding on it makes source.aljazeera.arabic +
           -- source.aljazeera.world one family, which is the point.
           count(DISTINCT split_part(d.source_id, '.', 2)) AS source_families,
           max(d.fetched_at) AS newest_signal_at,
           bool_or(d.geo && (SELECT coalesce(array_agg(DISTINCT g), '{{}}')
                               FROM target_descriptors td
                               CROSS JOIN LATERAL jsonb_array_elements_text(
                                   coalesce(td.body->'scope'->'geo', '[]'::jsonb)) AS g
                              WHERE td.is_head AND td.state = 'active'
                                AND td.abstraction_level = 'L1')) AS desk_geo_hit
      FROM dedup d GROUP BY d.cid
), rawcount AS (
    SELECT cid, count(*) AS raw_signals FROM expanded GROUP BY cid
), mentions AS (
    SELECT lower(ep.canonical_name) AS name, count(*) AS n
      FROM signal_entity_links sel
      JOIN entity_profiles ep ON ep.id = sel.entity_id
     GROUP BY 1
)
SELECT c.id, c.source_entity, c.target_entity, c.confidence, c.status,
       c.produced_at,
       coalesce(sp.independent_sources, 0) AS independent_sources,
       coalesce(sp.source_families, 0)     AS source_families,
       coalesce(rc.raw_signals, 0)         AS raw_signals,
       coalesce(ms.n, 0)                   AS subject_mentions,
       coalesce(mo.n, 0)                   AS object_mentions,
       coalesce(sp.desk_geo_hit, false)    AS desk_geo_hit,
       EXTRACT(EPOCH FROM (now() - coalesce(sp.newest_signal_at, c.produced_at)))
           / 86400.0                       AS age_days
  FROM cand c
  LEFT JOIN support sp  ON sp.cid = c.id
  LEFT JOIN rawcount rc ON rc.cid = c.id
  LEFT JOIN mentions ms ON ms.name = lower(c.source_entity)
  LEFT JOIN mentions mo ON mo.name = lower(c.target_entity)
"""


# ---------------------------------------------------------------------------
# The SAME score, in SQL
# ---------------------------------------------------------------------------
#
# Three callers need this score against whole tables rather than one candidate
# at a time — the reifier's per-run selection, the governance age-out sweep, and
# the one-shot retirement migration. Scoring 175,000 rows in Python to keep ONE
# implementation would be the wrong trade; scoring them in hand-written SQL that
# drifts from :func:`qualification_score` would be worse.
#
# So the SQL is GENERATED from the same constants the Python reads. Change a
# weight or a saturation point and both move together, because there is only one
# place either is written down. ``tests/data_pkg/test_edge_qualification.py``
# pins the two against each other over a grid of evidence, executed on a real
# Postgres — if they ever disagree by more than float noise, that test is red.
#
# The expression expects these column names in scope:
#   ``independent_sources`` int, ``source_families`` int,
#   ``weaker_mentions`` int, ``desk_geo_hit`` bool, ``desk_entity_hit`` bool.

_W = DEFAULT_WEIGHTS

#: SQL rendering of :func:`qualification_score`. Weights sum to 1.0, so the
#: Python's ``/ total`` normalisation is the identity and is not restated here —
#: :func:`weights_are_normalised` guards that assumption.
QUALIFICATION_SCORE_EXPR: str = (
    f"({_W['multi_source']} * (CASE WHEN independent_sources <= 1 THEN 0.0 "
    f"ELSE least(1.0, (independent_sources - 1)::numeric "
    f"/ {MULTI_SOURCE_SATURATION - 1}.0) END)"
    f" + {_W['source_diversity']} * (CASE WHEN independent_sources <= 1 THEN 0.0 "
    f"ELSE least(source_families, independent_sources)::numeric "
    f"/ independent_sources END)"
    f" + {_W['salience']} * (CASE WHEN weaker_mentions <= 0 THEN 0.0 "
    f"ELSE least(1.0, ln(1.0 + weaker_mentions) "
    f"/ ln({SALIENCE_SATURATION + 1}.0)) END)"
    f" + {_W['desk_relevance']} * (CASE WHEN desk_entity_hit THEN 1.0 "
    f"WHEN desk_geo_hit THEN 0.6 ELSE 0.0 END))"
)


def weights_are_normalised(weights: Mapping[str, float] | None = None) -> bool:
    """Whether the weights sum to 1.0 — the assumption :data:`QUALIFICATION_
    SCORE_EXPR` relies on when it omits the normalising divisor."""
    total = sum((weights or DEFAULT_WEIGHTS).values())
    return abs(total - 1.0) < 1e-9


#: The active desks, as the score needs them. ``desk_names`` mirrors the Python
#: derivation used by the K-G2 measurement: an L1 desk's subject is the part of
#: its ``name`` after the em-dash ('G20 — Germany' → 'Germany'), plus the leading
#: form of a comma-qualified ISO name ('Korea, Republic of' → 'Korea').
DESK_CTES: str = """
desk_geo AS (
    SELECT coalesce(array_agg(DISTINCT g), '{}') AS codes
      FROM target_descriptors td
      CROSS JOIN LATERAL jsonb_array_elements_text(
          coalesce(td.body->'scope'->'geo', '[]'::jsonb)) AS g
     WHERE td.is_head AND td.state = 'active' AND td.abstraction_level = 'L1'
), desk_subject AS (
    SELECT lower(btrim(
             CASE WHEN td.name LIKE '%—%'
                  THEN reverse(split_part(reverse(td.name), '—', 1))
                  ELSE td.name END)) AS subject
      FROM target_descriptors td
     WHERE td.is_head AND td.state = 'active' AND td.abstraction_level = 'L1'
), desk_names AS (
    SELECT subject FROM desk_subject WHERE subject <> ''
    UNION
    SELECT btrim(split_part(subject, ',', 1)) FROM desk_subject
     WHERE subject LIKE '%,%' AND btrim(split_part(subject, ',', 1)) <> ''
)
"""

#: Per-candidate evidence, materialised for scoring. ``{status_filter}`` is a
#: predicate over ``pe``; ``{extra_columns}`` lets a caller pull row payload it
#: needs without forking the CTE chain.
#:
#: Independence is enforced in ``dedup`` exactly as :data:`POOL_SCORING_SQL`
#: does it: backing signals collapse on
#: ``coalesce(nullif(content_hash,''), canonical_signal_id::text, id::text)``
#: BEFORE sources are counted, so syndication cannot inflate support.
EVIDENCE_CTES: str = """
cand AS (
    SELECT pe.id, pe.source_entity, pe.target_entity, pe.confidence,
           pe.produced_at, pe.derived_from
      FROM proposed_edges pe
     WHERE {status_filter}
), expanded AS (
    SELECT c.id AS cid, s.source_id, s.geo, s.fetched_at,
           coalesce(nullif(s.content_hash, ''), s.canonical_signal_id::text,
                    s.id::text) AS content_key
      FROM cand c
      CROSS JOIN LATERAL unnest(c.derived_from) AS d(sig_id)
      JOIN signals s ON s.id = d.sig_id
), dedup AS (
    SELECT DISTINCT ON (cid, content_key)
           cid, content_key, source_id, geo, fetched_at
      FROM expanded ORDER BY cid, content_key, fetched_at ASC
), support AS (
    SELECT d.cid,
           count(DISTINCT d.source_id) AS independent_sources,
           -- source_id is 'source.<publisher>.<feed>'; the PUBLISHER is
           -- segment 2, so aljazeera.arabic + aljazeera.world fold to one.
           count(DISTINCT split_part(d.source_id, '.', 2)) AS source_families,
           max(d.fetched_at) AS newest_signal_at,
           bool_or(d.geo && (SELECT codes FROM desk_geo)) AS desk_geo_hit
      FROM dedup d GROUP BY d.cid
), mentions AS (
    SELECT lower(ep.canonical_name) AS name, count(*) AS n
      FROM signal_entity_links sel
      JOIN entity_profiles ep ON ep.id = sel.entity_id
     GROUP BY 1
), evidence AS (
    SELECT c.id, c.source_entity, c.target_entity, c.confidence, c.produced_at,
           coalesce(sp.independent_sources, 0)  AS independent_sources,
           coalesce(sp.source_families, 0)      AS source_families,
           least(coalesce(ms.n, 0), coalesce(mo.n, 0)) AS weaker_mentions,
           coalesce(sp.desk_geo_hit, false)     AS desk_geo_hit,
           (lower(c.source_entity) IN (SELECT subject FROM desk_names)
            OR lower(c.target_entity) IN (SELECT subject FROM desk_names))
                                                AS desk_entity_hit,
           EXTRACT(EPOCH FROM (now() - coalesce(sp.newest_signal_at,
                                                c.produced_at))) / 86400.0
                                                AS age_days
      FROM cand c
      LEFT JOIN support sp  ON sp.cid = c.id
      LEFT JOIN mentions ms ON ms.name = lower(c.source_entity)
      LEFT JOIN mentions mo ON mo.name = lower(c.target_entity)
)
"""


def scored_pool_sql(*, status_filter: str) -> str:
    """One SELECT giving every candidate its evidence AND its ``qual_score``.

    Read-only. ``status_filter`` is a predicate over ``pe``, supplied by the
    caller so one CTE chain serves the reifier's selection, the governance
    age-out sweep and the retirement migration alike."""
    return (
        "WITH " + DESK_CTES.strip()
        + ", " + EVIDENCE_CTES.strip().format(status_filter=status_filter)
        + f"\nSELECT e.*, {QUALIFICATION_SCORE_EXPR} AS qual_score\n  FROM evidence e"
    )


#: The retirement set: below the bar AND stale. ONE statement, so a one-shot
#: migration and a recurring sweep can never disagree about who retires.
#: ``$1`` bar, ``$2`` min independent sources, ``$3`` stale-days.
#:
#: Selects ids (plus the numbers that justify the verdict) — this module never
#: writes; the caller decides what a retirement means.
RETIREMENT_SELECT_SQL: str = (
    "SELECT s.id, s.source_entity, s.target_entity, s.independent_sources,\n"
    "       s.qual_score, s.age_days\n"
    "  FROM (\n"
    + scored_pool_sql(status_filter="pe.status = 'pending'")
    + "\n) AS s\n"
    " WHERE NOT (s.independent_sources >= $2 AND s.qual_score >= $1)\n"
    "   AND s.age_days >= $3\n"
)


#: The terminal ``proposed_edges.status`` a retired candidate carries.
#:
#: A NEW value beside the existing four (``pending`` / ``promoted`` /
#: ``rejected`` / ``orphaned``), following the ``entity_gc`` precedent: mint a
#: terminal status outside the pending work-set, flip it, stamp ``reviewed_at``,
#: never delete. Deliberately NOT ``'rejected'`` — that means a human or the
#: governance pass refused the pair. Retirement means something different and
#: weaker: the pair never earned enough independent support to be worth a GPU
#: call, and stopped accruing. The distinction is the whole audit value; folding
#: it into ``rejected`` would erase it.
#:
#: The co-mention evidence stays addressable, and a pair that later re-earns
#: support returns through the normal producer path.
RETIRED_STATUS: str = "retired"


def retirement_update_sql(
    *,
    bar: float = RECOMMENDED_BAR,
    min_sources: int = MIN_INDEPENDENT_SOURCES,
    stale_days: int = RETENTION_STALE_DAYS,
    limit: int | None = None,
) -> str:
    """The statement that retires the below-bar, stale remainder.

    ONE generator for TWO callers that must never disagree: the one-shot
    migration that clears the standing backlog, and the recurring governance
    sweep that keeps it clear. A migration cannot bind query parameters or
    import Python, so the thresholds are rendered as LITERALS — coerced through
    ``float()``/``int()`` here, which is what makes that safe.

    ``limit`` bounds one sweep's work (oldest first, so the sweep converges);
    ``None`` is the unbounded migration form.

    Idempotent by construction: the UPDATE re-asserts ``status = 'pending'``,
    and the inner select only ever looks at pending rows, so a second run
    matches zero rows.
    """
    sel = (
        RETIREMENT_SELECT_SQL
        .replace("$1", repr(float(bar)))
        .replace("$2", str(int(min_sources)))
        .replace("$3", str(int(stale_days)))
    )
    inner = f"SELECT r.id FROM (\n{sel}\n) AS r"
    if limit is not None:
        inner += f"\n ORDER BY r.age_days DESC\n LIMIT {int(limit)}"
    return (
        "UPDATE proposed_edges\n"
        f"   SET status = '{RETIRED_STATUS}', reviewed_at = now()\n"
        " WHERE status = 'pending'\n"
        "   AND id IN (\n"
        f"{inner}\n"
        ")"
    )


__all__ = [
    "HARNESS_VERSION",
    "DEFAULT_WEIGHTS",
    "RETIRED_STATUS",
    "retirement_update_sql",
    "QUALIFICATION_SCORE_EXPR",
    "DESK_CTES",
    "EVIDENCE_CTES",
    "scored_pool_sql",
    "RETIREMENT_SELECT_SQL",
    "weights_are_normalised",
    "RECOMMENDED_BAR",
    "MIN_INDEPENDENT_SOURCES",
    "RETENTION_STALE_DAYS",
    "CandidateEvidence",
    "RetentionVerdict",
    "components",
    "qualification_score",
    "qualifies",
    "retention_verdict",
    "multi_source_score",
    "source_diversity_score",
    "salience_score",
    "desk_relevance_score",
    "POOL_SCORING_SQL",
]
