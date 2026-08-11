# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-G4 — the graph-walk read surface (`/graph/ego`, `/graph/edge/{id}`).

Most of these run against a STUB pool rather than a migrated database,
deliberately. The shapes under test are the ones a fixture cannot lie about:
parameter numbering in the dynamically-assembled SQL, the filter fragment's
placement in both arms of the ego union, neighbour de-duplication, truncation
disclosure, and every fail-loud branch.

The highest-risk failure in that half of the module is a `$n` that does not
line up with its argument, because asyncpg reports it as an opaque runtime
error far from the cause. `_assert_params_consistent` makes that a test failure
instead.

**The edge-budget section is the exception and runs against real Postgres.** A
stub cannot check an ORDER BY, because it never executes one — and the K-G4
screenshot pass found a defect that lives entirely in the ordering: switching
the co-mention family on evicted every asserted edge from the live `United
States` ego instead of adding density to it. Those tests seed the measured
family imbalance into a migrated database and run the real `_ego_sql` through
a real planner, including a counter-example test that proves the seed still
reproduces the old behaviour.
"""
from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data.registry.api import require_bearer
from legba.data.registry.graph_walk_api import (
    EDGE_FAMILIES,
    MAX_KNOWN,
    _build_edge_filter,
    _ego_sql,
    _family_precedence_sql,
    _no_evidence_detail,
    _split_csv,
    _stitch_sql,
    _validate_direction,
    _validate_families,
    _validate_limit,
    _validate_polarity,
    build_graph_walk_router,
)

ANCHOR = "66c795b7-73ba-44a3-9cfe-60cc2b7dfbb9"
IRAN = "8e7c0a9e-4950-40af-8ced-06c55c4923ae"
RUSSIA = "1f64f392-0751-45cf-96e4-a8d9d94762dd"
EDGE_A = "dde48dd1-32aa-4172-a0fb-61912161882a"
EDGE_B = "80ff0eb6-683a-4a11-bd83-b116bb3ab6ae"
SIGNAL = "36ccb56e-3e70-4299-b573-2d052e49c49c"


# ---- the stub pool ----


def _max_placeholder(sql: str) -> int:
    nums = [int(n) for n in re.findall(r"\$(\d+)", sql)]
    return max(nums) if nums else 0


class _FakeConn:
    """Routes by SQL shape, records every (sql, args) pair for assertions."""

    def __init__(self, data: dict[str, Any], calls: list[tuple[str, tuple[Any, ...]]]):
        self.data = data
        self.calls = calls

    def _route(self, sql: str) -> str:
        if "match_count" in sql:
            return "ego"
        if "FILTER (WHERE polarity" in sql:
            return "facets"
        if "AS degree" in sql:
            return "degree"
        if "e.src_id = ANY($2::uuid[])" in sql:
            return "stitch"
        if "e.evidence_set, e.source_signal_ids" in sql:
            return "evidence_edge"
        if "FROM signals" in sql:
            return "signals"
        if "WHERE id = ANY($1::uuid[])" in sql:
            return "endpoints"
        return "anchor"

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.calls.append((sql, args))
        return list(self.data.get(self._route(sql), []) or [])

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        rows = self.data.get(self._route(sql))
        if isinstance(rows, list):
            return rows[0] if rows else None
        return rows


class _Acquire:
    def __init__(self, conn: _FakeConn):
        self.conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self.conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakePool:
    def __init__(self, data: dict[str, Any]):
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.conn = _FakeConn(data, self.calls)

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


def _app(data: dict[str, Any]) -> tuple[FastAPI, _FakePool]:
    pool = _FakePool(data)
    deps = SimpleNamespace(descriptor_registry=SimpleNamespace(pg=pool))
    app = FastAPI()
    app.include_router(build_graph_walk_router(deps), prefix="/api/v1")
    app.dependency_overrides[require_bearer] = lambda: "test-principal"
    return app, pool


async def _get(app: FastAPI, path: str, **params: Any) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get(path, params=params)


def _assert_params_consistent(pool: _FakePool) -> None:
    """Every executed statement must bind exactly as many params as it names."""
    for sql, args in pool.calls:
        highest = _max_placeholder(sql)
        assert highest == len(args), (
            f"SQL names ${highest} but {len(args)} args were bound — a "
            f"placeholder/argument mismatch is the defect this catches.\n{sql}"
        )


# ---- fixtures for the happy path ----


def _anchor_row() -> dict[str, Any]:
    return {
        "id": UUID(ANCHOR),
        "canonical_name": "United States",
        "entity_class": "country",
        "entity_type": "country",
        "geo_country": "US",
    }


def _edge_row(
    *,
    edge_id: str,
    other: str,
    direction: str,
    family: str = "relation",
    edge_type: str = "hostile to",
    polarity: int = -1,
    confidence: float = 0.9,
    has_evidence: bool = True,
    match_count: int = 2,
    name: str | None = "Iran",
) -> dict[str, Any]:
    src = UUID(ANCHOR) if direction == "out" else UUID(other)
    dst = UUID(other) if direction == "out" else UUID(ANCHOR)
    return {
        "id": UUID(edge_id),
        "src_id": src,
        "dst_id": dst,
        "other_id": UUID(other),
        "direction": direction,
        "edge_family": family,
        "edge_type": edge_type,
        "polarity": polarity,
        "confidence": confidence,
        "observed_count": 3,
        "intent": "hostile",
        "channel": "direct",
        "source_type": "agent",
        "valid_from": None,
        "first_seen_at": None,
        "last_seen_at": None,
        "has_evidence": has_evidence,
        "signal_count": 4,
        "match_count": match_count,
        "canonical_name": name,
        "entity_class": "country" if name else None,
        "entity_type": "country" if name else None,
        "geo_country": None,
    }


def _happy_data() -> dict[str, Any]:
    return {
        "anchor": _anchor_row(),
        "ego": [
            _edge_row(edge_id=EDGE_A, other=IRAN, direction="out"),
            _edge_row(
                edge_id=EDGE_B,
                other=IRAN,
                direction="in",
                edge_type="targets",
                confidence=0.7,
            ),
        ],
        "facets": [
            {
                "edge_family": "cooccurrence",
                "edge_type": "co occurs with",
                "n": 583,
                "negative": 0,
                "neutral": 583,
                "positive": 0,
            },
            {
                "edge_family": "relation",
                "edge_type": "hostile to",
                "n": 31,
                "negative": 30,
                "neutral": 1,
                "positive": 0,
            },
        ],
        "degree": [
            {"eid": UUID(ANCHOR), "degree": 693},
            {"eid": UUID(IRAN), "degree": 595},
        ],
    }


# ---- pure helpers ----


def test_families_accept_repeated_and_csv_forms() -> None:
    assert _validate_families(["relation", "reference"]) == ["relation", "reference"]
    assert _validate_families(["relation,cooccurrence"]) == ["relation", "cooccurrence"]
    assert _validate_families(["RELATION"]) == ["relation"]
    assert _validate_families(None) == []
    # de-duplicated, order preserved
    assert _validate_families(["relation", "relation"]) == ["relation"]


def test_unknown_family_is_a_400_not_a_silently_empty_walk() -> None:
    """A typo'd family must not read as 'this actor has no such edges'."""
    with pytest.raises(Exception) as err:
        _validate_families(["relatoin"])
    assert getattr(err.value, "status_code", None) == 400
    assert "relatoin" in str(err.value.detail)


def test_every_declared_family_validates() -> None:
    for fam in EDGE_FAMILIES:
        assert _validate_families([fam]) == [fam]


def test_polarity_validation() -> None:
    assert _validate_polarity([-1, 0, 1]) == [-1, 0, 1]
    assert _validate_polarity(None) == []
    with pytest.raises(Exception) as err:
        _validate_polarity([2])
    assert getattr(err.value, "status_code", None) == 400


def test_direction_and_limit_validation() -> None:
    assert _validate_direction("OUT") == "out"
    assert _validate_direction("") == "both"
    with pytest.raises(Exception) as err:
        _validate_direction("sideways")
    assert getattr(err.value, "status_code", None) == 400
    assert _validate_limit(0) == 60
    assert _validate_limit(-5) == 60
    assert _validate_limit(10_000) == 400


def test_split_csv_dedupes_and_trims() -> None:
    assert _split_csv(["a, b", "b", " c "]) == ["a", "b", "c"]
    assert _split_csv(None) == []


def test_no_evidence_detail_names_the_reason_per_source_type() -> None:
    assert "seed" in _no_evidence_detail("seed")
    assert "manually" in _no_evidence_detail("manual")
    # the generic branch still says something operator-readable
    assert _no_evidence_detail("agent")


# ---- SQL assembly ----


def test_filter_fragment_numbers_params_in_append_order() -> None:
    args: list[Any] = ["anchor"]
    frag = _build_edge_filter(
        args,
        families=["relation"],
        edge_types=["hostile to"],
        polarity=[-1],
        min_confidence=0.3,
        since=None,
        until=None,
    )
    assert "$2::text[]" in frag and "$3::text[]" in frag
    assert "$4::smallint[]" in frag and "e.confidence >= $5" in frag
    assert len(args) == 5


def test_empty_filter_fragment_is_empty() -> None:
    args: list[Any] = ["anchor"]
    frag = _build_edge_filter(
        args, families=[], edge_types=[], polarity=[], min_confidence=0.0,
        since=None, until=None,
    )
    assert frag == ""
    assert len(args) == 1


def test_ego_sql_applies_the_filter_to_BOTH_arms() -> None:
    """A filter on only one arm silently returns unfiltered inbound edges."""
    args: list[Any] = ["anchor"]
    frag = _build_edge_filter(
        args, families=["relation"], edge_types=[], polarity=[],
        min_confidence=0.0, since=None, until=None,
    )
    args.append(60)
    sql = _ego_sql(frag, direction="both", limit_param=len(args))
    assert sql.count("e.edge_family = ANY($2::text[])") == 2
    assert sql.count("e.src_id = $1") == 1
    assert sql.count("e.dst_id = $1") == 1
    assert _max_placeholder(sql) == len(args)


def test_ego_sql_carries_the_open_predicate_on_every_arm() -> None:
    """An edge that has been closed or superseded is not part of the walk."""
    sql = _ego_sql("", direction="both", limit_param=2)
    assert sql.count("e.valid_until IS NULL AND e.superseded_by IS NULL") == 2


@pytest.mark.parametrize(
    ("direction", "expect_src", "expect_dst"),
    [("both", 1, 1), ("out", 1, 0), ("in", 0, 1)],
)
def test_ego_sql_direction_selects_arms(direction, expect_src, expect_dst) -> None:
    sql = _ego_sql("", direction=direction, limit_param=2)
    assert sql.count("WHERE e.src_id = $1") == expect_src
    assert sql.count("WHERE e.dst_id = $1") == expect_dst


def test_ego_sql_ranks_each_family_independently() -> None:
    """The budget must be spent per family, not on raw confidence.

    Ordering the whole neighbourhood on ``confidence DESC`` hands the entire
    limit to whichever family scores highest — measured live, 585 co-mentions
    at minimum confidence 0.500 evicted all 89 relation and all 22 reference
    edges of the graph's highest-degree actor. These two facts are what stop
    that: each family is ranked in its OWN partition, and the outer ordering
    leads with that rank rather than with confidence.
    """
    sql = _ego_sql("", direction="both", limit_param=2)
    assert "PARTITION BY nb.edge_family" in sql
    order_by = sql.split("ORDER BY")[-1]
    lead = order_by.strip().splitlines()[0]
    assert "family_rank" in lead, (
        "the outer ordering must LEAD with the per-family rank; leading with "
        f"confidence is the defect itself. Got: {lead!r}"
    )


def test_ego_sql_counts_matches_before_the_limit_is_applied() -> None:
    """Per-family ranking must not disturb the truncation denominator.

    ``degree_matched`` is what the viewer discloses as "80 of 111 matching";
    if the window that computes it moved inside the per-family partition it
    would silently start counting one family instead of the neighbourhood.
    """
    sql = _ego_sql("", direction="both", limit_param=2)
    assert "count(*) OVER () AS match_count" in sql
    # over the WHOLE set — an empty OVER (), never the family partition
    assert "count(*) OVER (PARTITION" not in sql


def test_family_precedence_orders_claims_before_statistics() -> None:
    """Ties inside one rank tier go to the assertion, not the statistic."""
    frag = _family_precedence_sql("x.edge_family")
    positions = [frag.index(f"'{fam}'") for fam in EDGE_FAMILIES]
    assert positions == sorted(positions), "precedence must follow EDGE_FAMILIES"
    assert frag.index("'relation'") < frag.index("'cooccurrence'")
    # nothing caller-supplied reaches this string
    assert all(f"'{fam}'" in frag for fam in EDGE_FAMILIES)


def test_stitch_sql_excludes_the_anchor_and_binds_its_limit() -> None:
    args: list[Any] = ["anchor", "canvas"]
    frag = _build_edge_filter(
        args, families=["relation"], edge_types=[], polarity=[],
        min_confidence=0.0, since=None, until=None,
    )
    args.append(400)
    sql = _stitch_sql(frag, limit_param=len(args))
    assert "e.src_id <> $1 AND e.dst_id <> $1" in sql
    assert "LIMIT $4" in sql
    assert _max_placeholder(sql) == len(args)


# ---- the edge budget, executed against real Postgres ----
#
# The stub above cannot check an ORDER BY, because it never executes one. The
# defect this section pins is PURELY an ordering defect, so it is the one thing
# in this module that has to run against a real planner: the K-G4 screenshot
# pass measured the live `United States` ego collapsing to `{cooccurrence: 80,
# relation: 0, reference: 0}` the moment the co-mention chip was switched on.

#: Seeded rows carry this analyst id so the fixture cleans up by ownership
#: rather than truncating a table in a SESSION-scoped database.
_BUDGET_ANALYST = "test.graph_walk_budget"

#: The live shape, scaled down. What matters is reproduced exactly: the noisy
#: family is an order of magnitude larger AND uniformly higher-confidence than
#: the two asserted families, whose confidences do not overlap it at all.
_BUDGET_MIX: tuple[tuple[str, str, int, float], ...] = (
    ("cooccurrence", "co occurs with", 60, 0.90),
    ("relation", "hostile to", 12, 0.55),
    ("reference", "member of", 6, 0.30),
)
_BUDGET_LIMIT = 20


@pytest_asyncio.fixture
async def budget_conn(migrated_pg):
    """A real neighbourhood with the measured live family imbalance."""
    conn = await asyncpg.connect(
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database,
    )

    async def _cleanup() -> None:
        await conn.execute(
            "DELETE FROM entity_edges WHERE analyst_id = $1", _BUDGET_ANALYST
        )
        await conn.execute(
            "DELETE FROM entity_profiles WHERE analyst_id = $1", _BUDGET_ANALYST
        )

    try:
        await _cleanup()
        anchor = uuid4()
        await conn.execute(
            "INSERT INTO entity_profiles (id, data, canonical_name, entity_class,"
            " entity_type, analyst_id) VALUES ($1, '{}'::jsonb, $2, 'country',"
            " 'country', $3)",
            anchor, "Budget Anchor", _BUDGET_ANALYST,
        )
        # One distinct neighbour per edge: `uq_entity_edges_open` is unique on
        # (src, dst, edge_type), and a real ego has one edge per counterpart.
        for family, edge_type, n, confidence in _BUDGET_MIX:
            for i in range(n):
                other = uuid4()
                await conn.execute(
                    "INSERT INTO entity_profiles (id, data, canonical_name,"
                    " entity_class, entity_type, analyst_id) VALUES ($1,"
                    " '{}'::jsonb, $2, 'country', 'country', $3)",
                    other, f"{family}-{i}", _BUDGET_ANALYST,
                )
                await conn.execute(
                    "INSERT INTO entity_edges (src_id, dst_id, edge_type,"
                    " edge_family, confidence, analyst_id)"
                    " VALUES ($1, $2, $3, $4, $5, $6)",
                    anchor, other, edge_type, family, confidence, _BUDGET_ANALYST,
                )
        yield conn, anchor
    finally:
        await _cleanup()
        await conn.close()


async def _run_ego(conn, anchor, families: list[str], limit: int) -> dict[str, int]:
    """Execute the real ego SQL and return the per-family slice of the budget."""
    args: list[Any] = [anchor]
    frag = _build_edge_filter(
        args, families=families, edge_types=[], polarity=[], min_confidence=0.0,
        since=None, until=None,
    )
    args.append(limit)
    rows = await conn.fetch(
        _ego_sql(frag, direction="both", limit_param=len(args)), *args
    )
    drawn: dict[str, int] = {}
    for row in rows:
        drawn[row["edge_family"]] = drawn.get(row["edge_family"], 0) + 1
    return drawn


async def test_cooccurrence_cannot_evict_the_asserted_families(budget_conn) -> None:
    """Turning the co-mention family ON must ADD density, never replace it.

    This is the K-G4 screenshot defect, executed. Every co-mention here
    outranks every relation and reference edge on confidence, and there are
    five times as many of them as the budget can hold — so the pre-fix ordering
    (`ORDER BY confidence DESC LIMIT n`) spent all 20 slots on co-mentions and
    drew zero asserted edges. A filter chip that removes the very edges the
    operator is reading the graph for is worse than one that does nothing.
    """
    conn, anchor = budget_conn
    drawn = await _run_ego(
        conn, anchor, ["relation", "reference", "cooccurrence"], _BUDGET_LIMIT
    )

    assert drawn.get("relation", 0) > 0, (
        "the asserted family was evicted by co-mention volume — this is the "
        f"defect the per-family budget exists to prevent. Got {drawn}"
    )
    assert drawn.get("reference", 0) > 0, f"reference edges were evicted: {drawn}"
    # ...and the budget is still fully spent: a per-family floor must not turn
    # into wasted slots.
    assert sum(drawn.values()) == _BUDGET_LIMIT


async def test_the_pre_fix_ordering_really_does_starve_them(budget_conn) -> None:
    """Prove the fixture reproduces the defect, so the pin above cannot rot.

    A guarantee test is only worth the counter-example it is measured against:
    if this seed were ever weakened to the point that plain confidence ordering
    already returned asserted edges, the test above would pass for the wrong
    reason and stop defending anything.
    """
    conn, anchor = budget_conn
    rows = await conn.fetch(
        """
        SELECT e.edge_family
          FROM entity_edges e
         WHERE (e.src_id = $1 OR e.dst_id = $1)
           AND e.valid_until IS NULL AND e.superseded_by IS NULL
         ORDER BY e.confidence DESC, e.last_seen_at DESC NULLS LAST
         LIMIT $2
        """,
        anchor, _BUDGET_LIMIT,
    )
    families = {r["edge_family"] for r in rows}
    assert families == {"cooccurrence"}, (
        "the seeded mix no longer reproduces the confidence-ordering defect, "
        f"so the guarantee test is no longer proving anything. Got {families}"
    )


async def test_each_family_gets_a_floor_of_the_budget(budget_conn) -> None:
    """No family may lose slots to another family's sheer volume.

    With ``f`` families matching, each is guaranteed
    ``min(its matches, floor(limit / f))`` — the round-robin over per-family
    ranks. Reference has only 6 edges and gets all 6; the other two split what
    is left instead of one of them taking it whole.
    """
    conn, anchor = budget_conn
    drawn = await _run_ego(
        conn, anchor, ["relation", "reference", "cooccurrence"], _BUDGET_LIMIT
    )
    floor = _BUDGET_LIMIT // len(_BUDGET_MIX)
    for family, _type, available, _conf in _BUDGET_MIX:
        assert drawn.get(family, 0) >= min(available, floor), (
            f"{family} got {drawn.get(family, 0)}, below its guaranteed share "
            f"of {min(available, floor)}. Full split: {drawn}"
        )
    # reference has fewer edges than its share, so it is drawn WHOLE
    assert drawn["reference"] == 6


async def test_a_single_family_request_is_still_pure_confidence_order(
    budget_conn,
) -> None:
    """With one family the partition is the whole set — nothing changes.

    The per-family budget must not quietly reorder the common case. Asking for
    ``relation`` alone still walks that family's best edges first.
    """
    conn, anchor = budget_conn
    args: list[Any] = [anchor]
    frag = _build_edge_filter(
        args, families=["relation"], edge_types=[], polarity=[],
        min_confidence=0.0, since=None, until=None,
    )
    args.append(5)
    rows = await conn.fetch(
        _ego_sql(frag, direction="both", limit_param=len(args)), *args
    )
    assert len(rows) == 5
    assert {r["edge_family"] for r in rows} == {"relation"}
    confidences = [float(r["confidence"]) for r in rows]
    assert confidences == sorted(confidences, reverse=True)
    # truncation still reports the whole matching set, not the family slice
    assert rows[0]["match_count"] == 12


async def test_unfiltered_walk_still_fills_the_budget(budget_conn) -> None:
    """No family filter at all is the widest case — it must not waste slots."""
    conn, anchor = budget_conn
    drawn = await _run_ego(conn, anchor, [], _BUDGET_LIMIT)
    assert sum(drawn.values()) == _BUDGET_LIMIT
    assert set(drawn) == {"relation", "reference", "cooccurrence"}


# ---- route registration ----


async def test_routes_register_without_colliding_with_graph_structure() -> None:
    from legba.data.registry.graph_structure_api import build_graph_structure_router

    deps = SimpleNamespace(descriptor_registry=SimpleNamespace(pg=None))
    walk = {r.path for r in build_graph_walk_router(deps).routes}
    structure = {r.path for r in build_graph_structure_router(deps).routes}
    assert walk == {"/graph/ego", "/graph/edge/{edge_id}"}
    assert not (walk & structure), "graph-walk paths must not shadow graph-structure"


# ---- /graph/ego ----


async def test_ego_returns_neighbourhood_with_facets_and_degree() -> None:
    app, pool = _app(_happy_data())
    res = await _get(app, "/api/v1/graph/ego", entity_id=ANCHOR)
    assert res.status_code == 200
    body = res.json()

    assert body["anchor"]["canonical_name"] == "United States"
    assert body["anchor"]["degree"] == 693
    # Two edges between the same pair collapse to ONE node carrying both.
    assert len(body["edges"]) == 2
    assert len(body["nodes"]) == 1
    assert body["nodes"][0]["canonical_name"] == "Iran"
    assert body["nodes"][0]["degree"] == 595
    assert {e["direction"] for e in body["edges"]} == {"out", "in"}
    _assert_params_consistent(pool)


async def test_ego_facets_are_unfiltered_so_the_view_discloses_what_it_hides() -> None:
    """The facet denominator must not shrink when the caller filters."""
    app, _ = _app(_happy_data())
    res = await _get(
        app, "/api/v1/graph/ego", entity_id=ANCHOR, family="relation", min_confidence=0.8
    )
    body = res.json()
    fams = {f["edge_family"]: f for f in body["facets"]}
    # cooccurrence is filtered OUT of the walk but still reported in facets,
    # which is the whole point: 583 hidden edges stay visible as a number.
    assert fams["cooccurrence"]["count"] == 583
    assert fams["relation"]["negative"] == 30
    assert body["filters"]["family"] == ["relation"]
    assert body["filters"]["min_confidence"] == 0.8


async def test_ego_discloses_truncation() -> None:
    data = _happy_data()
    data["ego"] = [
        _edge_row(edge_id=EDGE_A, other=IRAN, direction="out", match_count=693)
    ]
    app, _ = _app(data)
    body = (await _get(app, "/api/v1/graph/ego", entity_id=ANCHOR, limit=1)).json()
    assert body["degree_matched"] == 693
    assert body["truncated"] is True
    assert len(body["edges"]) == 1


async def test_ego_untruncated_when_everything_fits() -> None:
    app, _ = _app(_happy_data())
    body = (await _get(app, "/api/v1/graph/ego", entity_id=ANCHOR)).json()
    assert body["degree_matched"] == 2
    assert body["truncated"] is False


async def test_ego_reports_true_degree_even_when_filters_match_nothing() -> None:
    """'Exists but nothing matches your filter' must stay distinguishable.

    The anchor's degree comes from an UNFILTERED count, so a view that draws
    no edges still tells the operator the actor has 693 of them — the filter
    is the reason the canvas is empty, and the panel can say so.
    """
    data = _happy_data()
    data["ego"] = []
    app, pool = _app(data)
    body = (
        await _get(
            app, "/api/v1/graph/ego", entity_id=ANCHOR, family="structural"
        )
    ).json()
    assert body["edges"] == []
    assert body["nodes"] == []
    assert body["degree_total"] == 693
    assert body["degree_matched"] == 0
    assert body["truncated"] is False
    # the facets still name what IS there, unfiltered
    assert {f["edge_family"] for f in body["facets"]} == {"cooccurrence", "relation"}
    _assert_params_consistent(pool)


async def test_ego_unknown_anchor_is_404_not_an_empty_neighbourhood() -> None:
    """'No such actor' and 'this actor is isolated' are different answers."""
    app, _ = _app({"anchor": None})
    res = await _get(app, "/api/v1/graph/ego", entity_id=str(uuid4()))
    assert res.status_code == 404
    assert "no entity profile" in res.json()["detail"]


async def test_ego_rejects_a_non_uuid_anchor() -> None:
    app, _ = _app(_happy_data())
    res = await _get(app, "/api/v1/graph/ego", entity_id="United States")
    assert res.status_code == 400
    assert "must be a uuid" in res.json()["detail"]


async def test_ego_rejects_an_unknown_family() -> None:
    app, _ = _app(_happy_data())
    res = await _get(app, "/api/v1/graph/ego", entity_id=ANCHOR, family="friendship")
    assert res.status_code == 400


async def test_ego_rejects_an_inverted_time_window() -> None:
    app, _ = _app(_happy_data())
    res = await _get(
        app,
        "/api/v1/graph/ego",
        entity_id=ANCHOR,
        since="2026-08-01T00:00:00Z",
        until="2026-07-01T00:00:00Z",
    )
    assert res.status_code == 400
    assert "since must not be after until" in res.json()["detail"]


async def test_ego_unresolved_neighbour_stays_visible_rather_than_vanishing() -> None:
    """A dangling endpoint is reported, not dropped by an inner join."""
    data = _happy_data()
    data["ego"] = [
        _edge_row(edge_id=EDGE_A, other=IRAN, direction="out", name=None, match_count=1)
    ]
    data["degree"] = []
    app, _ = _app(data)
    body = (await _get(app, "/api/v1/graph/ego", entity_id=ANCHOR)).json()
    assert len(body["nodes"]) == 1
    assert body["nodes"][0]["resolved"] is False
    assert body["nodes"][0]["canonical_name"].startswith("unresolved:")


async def test_ego_without_known_does_not_run_the_stitch_query() -> None:
    app, pool = _app(_happy_data())
    body = (await _get(app, "/api/v1/graph/ego", entity_id=ANCHOR)).json()
    assert body["stitch_edges"] == []
    assert not any("$2::uuid[]" in sql for sql, _ in pool.calls)


async def test_ego_with_known_stitches_induced_edges() -> None:
    """Edges among canvas nodes that miss the anchor still reach the viewer."""
    data = _happy_data()
    data["stitch"] = [
        _edge_row(
            edge_id=EDGE_B, other=RUSSIA, direction="out", match_count=1, name="Russia"
        )
    ]
    app, pool = _app(data)
    body = (
        await _get(app, "/api/v1/graph/ego", entity_id=ANCHOR, known=RUSSIA)
    ).json()
    assert len(body["stitch_edges"]) == 1
    assert body["stitch_edges"][0]["direction"] == "stitch"
    _assert_params_consistent(pool)


async def test_stitch_canvas_excludes_the_anchor_and_is_capped() -> None:
    data = _happy_data()
    data["stitch"] = []
    app, pool = _app(data)
    many = ",".join(str(uuid4()) for _ in range(MAX_KNOWN + 50))
    await _get(app, "/api/v1/graph/ego", entity_id=ANCHOR, known=f"{ANCHOR},{many}")
    stitch = [args for sql, args in pool.calls if "$2::uuid[]" in sql]
    assert stitch, "expected the stitch query to run"
    canvas = stitch[0][1]
    assert len(canvas) <= MAX_KNOWN
    assert UUID(ANCHOR) not in canvas
    _assert_params_consistent(pool)


async def test_ego_filters_reach_the_sql_as_bound_params() -> None:
    app, pool = _app(_happy_data())
    await _get(
        app,
        "/api/v1/graph/ego",
        entity_id=ANCHOR,
        family="relation",
        edge_type="hostile to",
        polarity=-1,
        min_confidence=0.5,
        direction="out",
    )
    ego = [(sql, args) for sql, args in pool.calls if "match_count" in sql]
    assert len(ego) == 1
    sql, args = ego[0]
    assert ["relation"] in args and ["hostile to"] in args
    assert [-1] in args and 0.5 in args
    # direction=out ⇒ only the outbound arm is present
    assert "WHERE e.dst_id = $1" not in sql
    _assert_params_consistent(pool)


# ---- /graph/edge/{id} ----


def _evidence_row(*, source_type: str, evidence: Any, signals: list[Any]) -> dict[str, Any]:
    return {
        "id": UUID(EDGE_A),
        "src_id": UUID(ANCHOR),
        "dst_id": UUID(IRAN),
        "edge_family": "cooccurrence",
        "edge_type": "co occurs with",
        "polarity": 0,
        "confidence": 1.0,
        "observed_count": 2,
        "intent": "neutral",
        "channel": "direct",
        "source_type": source_type,
        "valid_from": None,
        "first_seen_at": None,
        "last_seen_at": None,
        "has_evidence": evidence is not None,
        "signal_count": len(signals),
        "evidence_set": evidence,
        "source_signal_ids": signals,
        "derived_from": [],
        "analyst_id": "proposed_edge_governance",
        "run_id": None,
        "produced_at": None,
    }


async def test_edge_evidence_returns_snippet_and_resolved_signals() -> None:
    app, pool = _app(
        {
            "evidence_edge": _evidence_row(
                source_type="agent",
                evidence={
                    "evidence_text": "Ex-DP leader Jung wins primary",
                    "promoted_from_proposed_edge": EDGE_B,
                },
                signals=[UUID(SIGNAL)],
            ),
            "endpoints": [
                _anchor_row(),
                {
                    "id": UUID(IRAN),
                    "canonical_name": "Iran",
                    "entity_class": "country",
                    "entity_type": "country",
                    "geo_country": None,
                },
            ],
            "signals": [
                {
                    "id": UUID(SIGNAL),
                    "title": "Ex-DP leader Jung wins primary",
                    "url": "https://en.yna.co.kr/view/AEN20260802003800315",
                    "source_id": "source.yonhap.english",
                    "fetched_at": None,
                    "language": "en",
                }
            ],
        }
    )
    res = await _get(app, f"/api/v1/graph/edge/{EDGE_A}")
    assert res.status_code == 200
    body = res.json()
    assert body["evidence_available"] is True
    assert body["detail"] == ""
    assert body["evidence_text"].startswith("Ex-DP leader Jung")
    assert body["promoted_from_proposed_edge"] == EDGE_B
    assert len(body["signals"]) == 1
    assert body["signals"][0]["source_id"] == "source.yonhap.english"
    assert body["src"]["canonical_name"] == "United States"
    assert body["dst"]["canonical_name"] == "Iran"
    assert body["unresolved_signal_ids"] == []
    _assert_params_consistent(pool)


async def test_edge_evidence_parses_a_json_string_evidence_set() -> None:
    """asyncpg may hand back jsonb as text depending on codec registration."""
    app, _ = _app(
        {
            "evidence_edge": _evidence_row(
                source_type="agent",
                evidence='{"evidence_text": "as text"}',
                signals=[],
            ),
            "endpoints": [],
        }
    )
    body = (await _get(app, f"/api/v1/graph/edge/{EDGE_A}")).json()
    assert body["evidence_text"] == "as text"
    assert body["evidence_available"] is True


async def test_seed_edge_says_why_it_has_no_evidence() -> None:
    """An empty evidence panel must never read as a failed lookup."""
    app, _ = _app(
        {
            "evidence_edge": _evidence_row(source_type="seed", evidence=None, signals=[]),
            "endpoints": [],
        }
    )
    body = (await _get(app, f"/api/v1/graph/edge/{EDGE_A}")).json()
    assert body["evidence_available"] is False
    assert "seed import" in body["detail"]
    assert body["signals"] == []


async def test_edge_evidence_reports_signal_ids_that_no_longer_resolve() -> None:
    """A vanished source is a fact about the evidence, not a rendering detail."""
    missing = uuid4()
    app, _ = _app(
        {
            "evidence_edge": _evidence_row(
                source_type="agent",
                evidence={"evidence_text": "snippet"},
                signals=[UUID(SIGNAL), missing],
            ),
            "endpoints": [],
            "signals": [
                {
                    "id": UUID(SIGNAL),
                    "title": "kept",
                    "url": None,
                    "source_id": "s",
                    "fetched_at": None,
                    "language": None,
                }
            ],
        }
    )
    body = (await _get(app, f"/api/v1/graph/edge/{EDGE_A}")).json()
    assert body["unresolved_signal_ids"] == [str(missing)]
    assert body["signal_count"] == 2


async def test_unknown_edge_is_404() -> None:
    app, _ = _app({"evidence_edge": None})
    res = await _get(app, f"/api/v1/graph/edge/{uuid4()}")
    assert res.status_code == 404


async def test_non_uuid_edge_id_is_400() -> None:
    app, _ = _app({})
    res = await _get(app, "/api/v1/graph/edge/not-a-uuid")
    assert res.status_code == 400


# ---- the endpoints an unauthenticated caller must not reach ----


async def test_both_routes_are_bearer_gated() -> None:
    """The guard is declared on every handler, not just the first one."""
    import inspect

    deps = SimpleNamespace(descriptor_registry=SimpleNamespace(pg=None))
    for route in build_graph_walk_router(deps).routes:
        params = inspect.signature(route.endpoint).parameters
        assert "principal" in params, f"{route.path} is missing the bearer guard"
