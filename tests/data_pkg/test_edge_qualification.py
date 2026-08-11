# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-G2 — the qualification bar.

The bar exists to keep the graph sparse and evidentiary. Two properties carry
that and are pinned hardest here:

  * **syndication cannot buy qualification** — one wire story on nine wires is
    one unit of support, not nine;
  * **the single-source floor is not purchasable** — no amount of salience or
    desk relevance lets a one-source co-mention through.
"""

from __future__ import annotations

import pytest

from legba.data.analysts.edge_qualification import (
    MIN_INDEPENDENT_SOURCES,
    POOL_SCORING_SQL,
    RECOMMENDED_BAR,
    RETENTION_STALE_DAYS,
    CandidateEvidence,
    components,
    desk_relevance_score,
    multi_source_score,
    qualification_score,
    qualifies,
    retention_verdict,
    salience_score,
    source_diversity_score,
)


def ev(**kw) -> CandidateEvidence:
    return CandidateEvidence(**kw)


# ---------------------------------------------------------------------------
# multi_source — the load-bearing component
# ---------------------------------------------------------------------------


def test_single_source_scores_zero_not_a_little():
    # 92.6 % of the live pending pool is single-sourced; if one source scored
    # even slightly positive the bar would drown in it.
    assert multi_source_score(ev(independent_sources=1)) == 0.0
    assert multi_source_score(ev(independent_sources=0)) == 0.0


def test_multi_source_rises_and_saturates():
    s2 = multi_source_score(ev(independent_sources=2))
    s3 = multi_source_score(ev(independent_sources=3))
    s4 = multi_source_score(ev(independent_sources=4))
    assert 0 < s2 < s3 < s4 == 1.0
    # Past saturation nothing more is earned.
    assert multi_source_score(ev(independent_sources=40)) == 1.0


def test_syndication_does_not_inflate_support():
    """One story on nine wires must score as ONE source.

    The SQL collapses on content_key before counting sources; this is the
    scorer-side statement of the same contract: raw_signals is reporting-only
    and never reaches the score.
    """
    syndicated = ev(independent_sources=1, source_families=1, raw_signals=9)
    genuine = ev(independent_sources=3, source_families=3, raw_signals=3)
    assert multi_source_score(syndicated) == 0.0
    assert multi_source_score(genuine) > 0.0
    assert qualification_score(syndicated) < qualification_score(genuine)


def test_raw_signals_is_not_a_scoring_input():
    a = qualification_score(ev(independent_sources=2, source_families=2, raw_signals=2))
    b = qualification_score(ev(independent_sources=2, source_families=2, raw_signals=99))
    assert a == b


# ---------------------------------------------------------------------------
# source_diversity
# ---------------------------------------------------------------------------


def test_same_family_sources_score_lower_than_distinct_families():
    clustered = ev(independent_sources=2, source_families=1)  # two Al Jazeera feeds
    spread = ev(independent_sources=2, source_families=2)     # AJ + Reuters
    assert source_diversity_score(clustered) < source_diversity_score(spread)
    assert source_diversity_score(spread) == 1.0


def test_diversity_is_zero_without_corroboration():
    assert source_diversity_score(ev(independent_sources=1, source_families=1)) == 0.0
    assert source_diversity_score(ev(independent_sources=0)) == 0.0


def test_families_cannot_exceed_sources():
    # Defensive: a miscounted family must never manufacture >1.0.
    assert source_diversity_score(ev(independent_sources=2, source_families=9)) == 1.0


# ---------------------------------------------------------------------------
# salience
# ---------------------------------------------------------------------------


def test_salience_uses_the_weaker_endpoint():
    """'Trump ↔ <noise token>' must not qualify on Trump's mention count."""
    lopsided = ev(subject_mentions=50_000, object_mentions=1)
    balanced = ev(subject_mentions=200, object_mentions=200)
    assert salience_score(lopsided) < salience_score(balanced)


def test_salience_is_log_damped_and_bounded():
    assert salience_score(ev(subject_mentions=0, object_mentions=0)) == 0.0
    s = [salience_score(ev(subject_mentions=n, object_mentions=n))
         for n in (1, 10, 100, 500, 5000)]
    assert s == sorted(s)
    assert all(0.0 <= x <= 1.0 for x in s)
    # Damping: a 10x mention increase must not be a 10x score increase.
    assert s[2] < 10 * s[1]


# ---------------------------------------------------------------------------
# desk relevance
# ---------------------------------------------------------------------------


def test_desk_entity_hit_outranks_geo_hit_outranks_nothing():
    assert desk_relevance_score(ev(desk_entity_hit=True)) == 1.0
    assert desk_relevance_score(ev(desk_geo_hit=True)) == 0.6
    assert desk_relevance_score(ev()) == 0.0
    # An entity hit dominates regardless of geo.
    assert desk_relevance_score(ev(desk_entity_hit=True, desk_geo_hit=True)) == 1.0


# ---------------------------------------------------------------------------
# The bar
# ---------------------------------------------------------------------------


def test_score_is_bounded_and_weighted():
    assert qualification_score(ev()) == 0.0
    perfect = ev(independent_sources=4, source_families=4,
                 subject_mentions=5000, object_mentions=5000,
                 desk_entity_hit=True)
    assert qualification_score(perfect) == pytest.approx(1.0)


def test_single_source_candidate_is_capped_by_the_weights():
    """A one-source pair cannot score above the multi-source + diversity gap.

    ``multi_source`` (0.45) and ``source_diversity`` (0.20) are BOTH zero at one
    source, so the ceiling is salience + desk relevance = 0.35 of the weight —
    below :data:`RECOMMENDED_BAR`. Stated as a test because it is the reason
    the recommended bar sits where it does: at 0.42 the weights alone already
    exclude every single-sourced candidate.
    """
    loaded = ev(independent_sources=1, source_families=1,
                subject_mentions=100_000, object_mentions=100_000,
                desk_entity_hit=True)
    assert qualification_score(loaded) == pytest.approx(0.35)
    assert qualification_score(loaded) < RECOMMENDED_BAR


def test_single_source_floor_cannot_be_bought_even_at_a_low_bar():
    """The floor is a gate, not a weight — this is the anti-sludge rule.

    It is what keeps a lowered bar honest: drop the bar to 0.20 to widen the
    queue and the weights would start admitting single-sourced pairs; the floor
    does not move with it.
    """
    loaded = ev(independent_sources=1, source_families=1,
                subject_mentions=100_000, object_mentions=100_000,
                desk_entity_hit=True)
    assert qualification_score(loaded) > 0.20
    assert qualifies(loaded, bar=0.20) is False
    # Removing the floor is what would let it through — proving the floor,
    # not the bar, is the thing doing the work here.
    assert qualifies(loaded, bar=0.20, min_sources=1) is True


def test_two_independent_sources_plus_relevance_qualifies():
    good = ev(independent_sources=2, source_families=2,
              subject_mentions=300, object_mentions=250,
              desk_geo_hit=True)
    assert good.independent_sources >= MIN_INDEPENDENT_SOURCES
    assert qualifies(good) is True


def test_bar_is_monotone_in_the_bar_setting():
    c = ev(independent_sources=2, source_families=2,
           subject_mentions=100, object_mentions=100, desk_geo_hit=True)
    score = qualification_score(c)
    assert qualifies(c, bar=score - 0.01) is True
    assert qualifies(c, bar=score + 0.01) is False


def test_components_sum_to_the_score_under_default_weights():
    from legba.data.analysts.edge_qualification import DEFAULT_WEIGHTS
    c = ev(independent_sources=3, source_families=2,
           subject_mentions=400, object_mentions=90, desk_geo_hit=True)
    comp = components(c)
    manual = sum(comp[k] * DEFAULT_WEIGHTS[k] for k in comp) / sum(DEFAULT_WEIGHTS.values())
    assert qualification_score(c) == pytest.approx(manual)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def test_above_bar_candidates_are_always_kept():
    good = ev(independent_sources=3, source_families=3,
              subject_mentions=500, object_mentions=500,
              desk_entity_hit=True, age_days=9999)
    assert retention_verdict(good).action == "keep"
    assert retention_verdict(good).reason == "above_bar"


def test_below_bar_but_fresh_is_kept_to_let_support_accrue():
    fresh = ev(independent_sources=1, age_days=RETENTION_STALE_DAYS - 1)
    v = retention_verdict(fresh)
    assert v.action == "keep"
    assert v.reason == "below_bar_still_fresh"


def test_below_bar_and_stale_is_retired():
    stale = ev(independent_sources=1, age_days=RETENTION_STALE_DAYS + 1)
    v = retention_verdict(stale)
    assert v.action == "retire"
    assert v.reason == "below_bar_stale"


def test_retirement_is_the_only_action_besides_keep():
    # Policy must stay binary — nothing here may imply a DELETE.
    for c in (ev(), ev(independent_sources=9, source_families=9, age_days=1e6),
              ev(independent_sources=1, age_days=1e6)):
        assert retention_verdict(c).action in ("keep", "retire")


# ---------------------------------------------------------------------------
# The measurement query
# ---------------------------------------------------------------------------


def test_pool_sql_collapses_syndication_before_counting_sources():
    sql = POOL_SCORING_SQL
    # The dedup CTE must run on content, and sources must be counted AFTER it.
    assert "content_key" in sql
    assert "DISTINCT ON (cid, content_key)" in sql
    dedup_at = sql.index("dedup AS")
    count_at = sql.index("count(DISTINCT d.source_id)")
    assert dedup_at < count_at


def test_pool_sql_takes_a_status_filter_and_is_read_only():
    formatted = POOL_SCORING_SQL.format(status_filter="pe.status = 'pending'")
    assert "pe.status = 'pending'" in formatted
    lowered = formatted.lower()
    for forbidden in ("insert ", "update ", "delete ", "drop ", "alter ", "truncate"):
        assert forbidden not in lowered


def test_pool_sql_folds_publisher_families_on_the_right_segment():
    # source_id is 'source.<publisher>.<feed>' — segment 2 is the publisher.
    assert "split_part(d.source_id, '.', 2)" in POOL_SCORING_SQL


# ---------------------------------------------------------------------------
# The SQL rendering must BE the Python function
# ---------------------------------------------------------------------------
#
# Three callers score whole tables in SQL (the reifier's selection scan, the
# governance age-out sweep, the retirement migration). The SQL is generated from
# the same constants the Python reads — these tests are the proof that the
# generation is faithful, executed on a real Postgres over a grid that includes
# every branch of every component.


import itertools  # noqa: E402

import asyncpg  # noqa: E402
import pytest_asyncio  # noqa: E402

from legba.data.analysts.edge_qualification import (  # noqa: E402
    DEFAULT_WEIGHTS,
    QUALIFICATION_SCORE_EXPR,
    RETIREMENT_SELECT_SQL,
    weights_are_normalised,
)
from legba.data.config import PostgresConfig  # noqa: E402


@pytest_asyncio.fixture
async def qual_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=2)
    yield pool
    await pool.close()


def test_weights_are_normalised_so_the_sql_may_omit_the_divisor():
    """``qualification_score`` divides by the weight total; the SQL does not,
    because the total is 1.0. If a re-tune breaks that, the SQL silently
    diverges — so the assumption is a test, not a comment."""
    assert weights_are_normalised()
    assert abs(sum(DEFAULT_WEIGHTS.values()) - 1.0) < 1e-9


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sql_score_expression_matches_the_python_function(qual_pool):
    grid = list(itertools.product(
        [0, 1, 2, 3, 4, 5, 9],        # independent sources (incl. past saturation)
        [0, 1, 2, 4, 9],              # source families (incl. > sources)
        [0, 1, 7, 120, 500, 5000],    # weaker-endpoint mentions (incl. saturated)
        [False, True],                # desk geo hit
        [False, True],                # desk entity hit
    ))
    rows = ", ".join(
        f"({n}::int, {f}::int, {m}::int, {str(g).lower()}::bool, "
        f"{str(e).lower()}::bool)"
        for n, f, m, g, e in grid
    )
    sql = (
        "SELECT independent_sources, source_families, weaker_mentions, "
        f"desk_geo_hit, desk_entity_hit, {QUALIFICATION_SCORE_EXPR} AS score "
        f"FROM (VALUES {rows}) AS t(independent_sources, source_families, "
        "weaker_mentions, desk_geo_hit, desk_entity_hit)"
    )
    async with qual_pool.acquire() as conn:
        got = await conn.fetch(sql)

    assert len(got) == len(grid)
    for r in got:
        expected = qualification_score(CandidateEvidence(
            independent_sources=r["independent_sources"],
            source_families=r["source_families"],
            # the SQL is handed the ALREADY-weaker endpoint; give Python the
            # same number on both sides so min() is the identity
            subject_mentions=r["weaker_mentions"],
            object_mentions=r["weaker_mentions"],
            desk_geo_hit=r["desk_geo_hit"],
            desk_entity_hit=r["desk_entity_hit"],
        ))
        assert abs(float(r["score"]) - expected) < 1e-6, (
            f"SQL and Python disagree at {dict(r)}: python={expected}"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sql_qualification_gate_matches_the_python_gate(qual_pool):
    """Not just the score — the two-gate verdict (floor AND bar) must agree."""
    grid = list(itertools.product([0, 1, 2, 3, 4], [0, 1, 2, 4], [0, 30, 900]))
    rows = ", ".join(
        f"({n}::int, {f}::int, {m}::int, false::bool, false::bool)"
        for n, f, m in grid
    )
    sql = (
        "SELECT independent_sources, source_families, weaker_mentions, "
        f"(independent_sources >= {MIN_INDEPENDENT_SOURCES} "
        f"AND {QUALIFICATION_SCORE_EXPR} >= {RECOMMENDED_BAR}) AS q "
        f"FROM (VALUES {rows}) AS t(independent_sources, source_families, "
        "weaker_mentions, desk_geo_hit, desk_entity_hit)"
    )
    async with qual_pool.acquire() as conn:
        got = await conn.fetch(sql)
    for r in got:
        expected = qualifies(CandidateEvidence(
            independent_sources=r["independent_sources"],
            source_families=r["source_families"],
            subject_mentions=r["weaker_mentions"],
            object_mentions=r["weaker_mentions"],
        ))
        assert bool(r["q"]) is bool(expected), f"gate disagrees at {dict(r)}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retirement_select_is_read_only_and_executes(qual_pool):
    """The one statement a migration and a recurring sweep both use. It must
    parse and run against the real schema, and it must never write."""
    lowered = RETIREMENT_SELECT_SQL.lower()
    for verb in ("insert ", "update ", "delete ", "drop ", "truncate "):
        assert verb not in lowered
    async with qual_pool.acquire() as conn:
        rows = await conn.fetch(
            RETIREMENT_SELECT_SQL, RECOMMENDED_BAR, MIN_INDEPENDENT_SOURCES,
            RETENTION_STALE_DAYS,
        )
    assert isinstance(rows, list)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retirement_never_selects_a_qualifying_candidate(qual_pool):
    """The queue is the work; retirement must only ever touch what is NOT it."""
    async with qual_pool.acquire() as conn:
        rows = await conn.fetch(
            RETIREMENT_SELECT_SQL, RECOMMENDED_BAR, MIN_INDEPENDENT_SOURCES, 0,
        )
    for r in rows:
        assert not qualifies(CandidateEvidence(
            independent_sources=r["independent_sources"],
        )) or float(r["qual_score"]) < RECOMMENDED_BAR
