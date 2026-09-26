# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R-2 — collection requirements: gaps + the standing ``source_request``
backlog become durable, provenance-carrying, reviewable COLLECTION
REQUIREMENT proposals (migration 0113), written by the EXTENDED
``collection_gap`` analyst.

Pure tests (no DB): ISO2 desk-id parsing, gap-cell row shaping
(natural_key / evidence / rationale / cap / skip-if-no-evidence),
source_request row shaping (prefix stripping, missing-desk tolerance).

Ephemeral-DB tests (the ``migrated_pg`` fixture): candidate-source matching
(source_class + geo, ANY lifecycle state, non-active-first ordering, global
sources with no geo tag matching every desk), :func:`_attach_candidates`
fillable/unfillable + ``suggested_fetch_url``, end-to-end ``handle()``
writes for BOTH origins, idempotent re-run (no duplicate row for a
still-starved cell, and no duplicate even after the row was dispositioned
to a terminal status), and the honest unfillable path when nothing in
``source_descriptors`` matches.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers import (
    _reference_gap_dispatch as rgd,
)
from legba.data.analysts.deterministic_handlers import collection_gap as cg
from legba.data.analysts.deterministic_handlers import scorecard_banding
from legba.data.config import PostgresConfig
from legba.runtime.analyst_method import AnalystMethodResult

_ANALYST = "collection_gap_r2_test"


# ---------------------------------------------------------------------------
# Pure — _desk_iso2
# ---------------------------------------------------------------------------


def test_desk_iso2_extracts_from_country_target_id():
    assert cg._desk_iso2("country_g20_ml") == "ML"
    assert cg._desk_iso2("country_watch_ir") == "IR"
    assert cg._desk_iso2("country_g20_us") == "US"


def test_desk_iso2_none_for_non_country_or_malformed():
    assert cg._desk_iso2("thematic_energy") is None
    assert cg._desk_iso2("country_g20") is None       # 'g20' — not 2-letter alpha
    assert cg._desk_iso2("country_g20_usa") is None    # 3-letter trailing token
    assert cg._desk_iso2(None) is None
    assert cg._desk_iso2(123) is None


# ---------------------------------------------------------------------------
# Pure — build_gap_requirement_rows
# ---------------------------------------------------------------------------


def _gap(
    *,
    desk: str = "country_g20_ml",
    dimension: str = "economic_coercion",
    evidence_id: str | None = None,
    reason: str = "no-finding",
    window_scorecards: int = 1,
    insufficient_count: int = 1,
    persistence: float | None = 1.0,
    desk_starved_dims: int = 1,
) -> dict[str, Any]:
    return {
        "desk": desk,
        "dimension": dimension,
        "reason": reason,
        "reasons": {reason: insufficient_count},
        "insufficient_count": insufficient_count,
        "window_scorecards": window_scorecards,
        "persistence": persistence,
        "source_classes": list(cg.SOURCE_CLASSES_BY_DIMENSION[dimension]),
        "latest_scorecard_id": evidence_id,
        "desk_starved_dims": desk_starved_dims,
    }


def test_build_gap_requirement_rows_basic_shape():
    eid = str(uuid4())
    rows = cg.build_gap_requirement_rows([_gap(evidence_id=eid)])
    assert len(rows) == 1
    r = rows[0]
    assert r["natural_key"] == "collection_gap:country_g20_ml:economic_coercion"
    assert r["origin"] == "collection_gap"
    assert r["desk"] == "country_g20_ml"
    assert r["dimension"] == "economic_coercion"
    assert r["evidence_kind"] == "analyst_output"
    assert r["evidence_id"] == eid
    assert r["source_classes_wanted"] == list(
        cg.SOURCE_CLASSES_BY_DIMENSION["economic_coercion"]
    )
    assert r["priority_rank"] == 0
    assert "no-finding" in r["topic"]
    assert "1/1" in r["rationale"]


def test_build_gap_requirement_rows_skips_when_no_evidence():
    """A gap cell whose scorecard id is missing (defensive — real aggregation
    always sets it) never becomes an unprovenanced proposal."""
    rows = cg.build_gap_requirement_rows([_gap(evidence_id=None)])
    assert rows == []


def test_build_gap_requirement_rows_respects_limit_and_preserves_rank():
    gaps = [
        _gap(desk=f"country_g20_{c}", evidence_id=str(uuid4()))
        for c in ("aa", "bb", "cc")
    ]
    rows = cg.build_gap_requirement_rows(gaps, limit=2)
    assert len(rows) == 2
    assert [r["priority_rank"] for r in rows] == [0, 1]
    assert rows[0]["desk"] == "country_g20_aa"
    assert rows[1]["desk"] == "country_g20_bb"


def test_build_gap_requirement_rows_uses_dimension_doctrine_when_gap_lacks_it():
    eid = str(uuid4())
    gap = _gap(evidence_id=eid, dimension="narrative_coordination")
    del gap["source_classes"]
    rows = cg.build_gap_requirement_rows([gap])
    assert rows[0]["source_classes_wanted"] == list(
        cg.SOURCE_CLASSES_BY_DIMENSION["narrative_coordination"]
    )


# ---------------------------------------------------------------------------
# Pure — build_source_request_row
# ---------------------------------------------------------------------------


def test_build_source_request_row_strips_tool_prefix():
    hid = uuid4()
    hyp = {
        "id": hid,
        "thesis": "Source coverage gap: no source covers Kazakhstan energy policy",
        "counter_thesis": "blocked a scorecard read",
        "target_id": "country_g20_kz",
    }
    row = cg.build_source_request_row(hyp, priority_rank=0)
    assert row["natural_key"] == f"source_request:{hid}"
    assert row["origin"] == "source_request"
    assert row["topic"] == "no source covers Kazakhstan energy policy"
    assert row["rationale"] == "blocked a scorecard read"
    assert row["desk"] == "country_g20_kz"
    assert row["dimension"] is None
    assert row["evidence_kind"] == "hypothesis"
    assert row["evidence_id"] == str(hid)
    assert row["source_classes_wanted"] == list(cg._DEFAULT_SOURCE_CLASSES)


def test_build_source_request_row_tolerates_missing_prefix_and_desk():
    hyp = {
        "id": uuid4(),
        "thesis": "no prefix on this one",
        "counter_thesis": "",
        "target_id": None,
    }
    row = cg.build_source_request_row(hyp, priority_rank=3)
    assert row["topic"] == "no prefix on this one"
    assert row["desk"] is None
    assert row["priority_rank"] == 3


def test_build_source_request_row_empty_thesis_is_honest_placeholder():
    hyp = {"id": uuid4(), "thesis": "", "counter_thesis": "", "target_id": None}
    row = cg.build_source_request_row(hyp, priority_rank=0)
    assert row["topic"] == "(no coverage-gap text recorded)"


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


async def _delete_this_files_own_rows(conn: Any) -> None:
    """Retire ONLY the rows this file's own fixtures create (``owner`` /
    ``analyst_id`` == ``_ANALYST``), never a table-wide wipe — the exact
    scoping the ``foreign``-row pin below insists every OTHER file also keep.

    Called from ``clean_slate`` both BEFORE a test (protects this file's own
    tests from a same-file predecessor) and AFTER one (yield + teardown):
    setup-only cleaning left this file's OWN last-in-shuffle-order
    ``source_descriptors`` row (``owner = _ANALYST``, e.g.
    ``test_match_candidate_sources_no_geo_filter_when_desk_has_none``'s
    ``source.r2.ml_only``) sitting in the session-shared DB for every test
    that ran afterward in OTHER files — concretely,
    ``tests/data_pkg/test_desk_reference.py``'s
    ``test_an_uncollected_item_writes_a_requirement_and_opens_a_question``,
    whose target (``country_watch_zz_ref``) has no ISO2-shaped suffix, so
    ``_desk_iso2`` returns ``None`` and its OWN candidate match runs with NO
    geo filter at all — any leftover ``source_class='analysis'`` row here
    (escalation's own wanted-classes set) satisfies it and flips
    ``fillable`` from the expected ``False`` to ``True``. Confirmed via
    ``LEGBA_TEST_SHUFFLE_SEED=987654321`` where this file's shuffled order
    happens to put that test last.
    """
    await conn.execute("DELETE FROM collection_requirements")
    await conn.execute(
        "DELETE FROM hypotheses WHERE status = 'source_request'"
    )
    await conn.execute("DELETE FROM analyst_outputs WHERE kind = 'scorecard'")
    await conn.execute(
        "DELETE FROM analyst_outputs WHERE analyst_id = $1", _ANALYST
    )
    await conn.execute(
        "DELETE FROM source_descriptors WHERE owner = $1", _ANALYST
    )


@pytest_asyncio.fixture
async def clean_slate(pg_pool):
    async with pg_pool.acquire() as conn:
        await _delete_this_files_own_rows(conn)
        # ------------------------------------------------------------------
        # THE PRECONDITION THIS FILE'S CANDIDATE-MATCHING HALF RESTS ON, made
        # explicit (task #23 regression pin).
        #
        # `_match_candidate_sources` reads `source_descriptors` GLOBALLY — any
        # `is_head` row whose `scope.source_class` is one of the wanted classes
        # and whose `scope.geo` is empty or overlaps the desk. The deletes
        # above only retire THIS file's own rows (`owner = _ANALYST`), so a
        # sibling that leaves such a row behind silently joins every candidate
        # set here. That is exactly what
        # `test_source_catalog_bringup::test_catalog_registers_head_rows_and_
        # credibility_rows` did until it grew a scoped teardown: it registered
        # 35-50 ACTIVE catalog heads into the session-shared DB and left them,
        # putting five of this file's tests on the nightly's shared-state
        # allowlist for a week.
        #
        # We do NOT delete them — an unscoped wipe here would just make this
        # file the polluter instead. We NAME them, so the next leak arrives as
        # one legible sentence pointing at the descriptor ids rather than as
        # five `assert True is False` diffs in a shuffled nightly.
        # ------------------------------------------------------------------
        foreign = await conn.fetch(
            "SELECT descriptor_id, owner, "
            "       body -> 'scope' ->> 'source_class' AS source_class "
            "  FROM source_descriptors "
            " WHERE is_head "
            "   AND (body -> 'scope' ->> 'source_class') IS NOT NULL "
            "   AND owner IS DISTINCT FROM $1 "
            " ORDER BY descriptor_id",
            _ANALYST,
        )
        assert not foreign, (
            "a sibling test left candidate-matchable source_descriptors heads "
            "in the session-shared DB; every candidate assertion in this file "
            "is a statement about that whole table, so they are now reading "
            f"{len(foreign)} sources they never seeded. Retire them where they "
            "are written (scoped to the rows that test created), not here: "
            + ", ".join(
                f"{r['descriptor_id']}(owner={r['owner']}, "
                f"class={r['source_class']})"
                for r in foreign[:10]
            )
            + (" …" if len(foreign) > 10 else "")
        )
    yield
    # TEARDOWN — the other half of the fix. Setup-only cleaning left this
    # file's own last-in-shuffle-order row (e.g. a `source_descriptors` row
    # from `test_match_candidate_sources_no_geo_filter_when_desk_has_none`)
    # in the session-shared DB for every OTHER file's test that happened to
    # run afterward, with no subsequent test in THIS file left to clean it.
    # Scoped identically to the setup pass — `owner`/`analyst_id` ==
    # `_ANALYST` only — so this stays this file's own row-set, never a
    # table-wide wipe.
    async with pg_pool.acquire() as conn:
        await _delete_this_files_own_rows(conn)


class _Deps:
    def __init__(self, pool: Any) -> None:
        self.pg_pool = pool


async def _run(pool: Any, **opts: Any) -> AnalystMethodResult:
    options = {"analyst_id": _ANALYST, **opts}
    result = await cg.handle([], options, _Deps(pool))
    assert isinstance(result, AnalystMethodResult)
    return result


# -- insert helpers ----------------------------------------------------------


def _dim(band: str, reason: str = "") -> dict:
    return {"band": band, "reason": reason, "basis": []}


def _insufficient(reason: str = "no-finding") -> dict:
    return _dim(scorecard_banding.INSUFFICIENT, reason)


def _banded() -> dict:
    return _dim("watch", "qualified")


def _all_banded() -> dict:
    return {d: _banded() for d in scorecard_banding.DIMENSIONS}


async def _insert_scorecard(conn: Any, *, desk: str, dimensions: dict) -> str:
    sid = uuid4()
    data = {"data": {"bands": {"dimensions": dimensions}}}
    await conn.execute(
        "INSERT INTO analyst_outputs "
        "  (id, kind, title, body, confidence, data, target_id, analyst_id, "
        "   schema_uri) "
        "VALUES ($1, 'scorecard', $2, '', 1.0, $3::jsonb, $4, $5, "
        "        'iglu:legba/scorecard/jsonschema/1-0-0')",
        sid,
        f"scorecard {desk}",
        json.dumps(data),
        desk,
        _ANALYST,
    )
    return str(sid)


async def _insert_source(
    conn: Any,
    *,
    sid: str,
    state: str,
    source_class: str,
    geo: list[str] | None = None,
    url: str | None = None,
) -> None:
    body: dict[str, Any] = {
        "scope": {"source_class": source_class, "geo": geo or [], "tags": []}
    }
    if url is not None:
        body["config"] = {"url": {"factory_kind": "text", "raw": url}}
    await conn.execute(
        "INSERT INTO source_descriptors "
        "  (descriptor_id, version, schema_uri, is_head, abstraction_level, "
        "   kind, state, owner, name, body) "
        "VALUES ($1, 'v1', 'legba/source/1.0.0', TRUE, 'L1', 'rss', $2, $3, "
        "        $1, $4::jsonb)",
        sid,
        state,
        _ANALYST,
        json.dumps(body),
    )


async def _insert_source_request(
    conn: Any,
    *,
    need: str,
    rationale: str = "",
    target_id: str | None = None,
) -> str:
    hid = uuid4()
    await conn.execute(
        "INSERT INTO hypotheses "
        "  (id, thesis, counter_thesis, status, target_id, analyst_id, "
        "   produced_at) "
        "VALUES ($1, $2, $3, 'source_request', $4, $5, now())",
        hid,
        f"Source coverage gap: {need}",
        rationale,
        target_id,
        _ANALYST,
    )
    return str(hid)


async def _requirements(conn: Any) -> list[Any]:
    return await conn.fetch(
        "SELECT * FROM collection_requirements ORDER BY priority_rank, created_at"
    )


# ---------------------------------------------------------------------------
# _match_candidate_sources / _attach_candidates
# ---------------------------------------------------------------------------


async def test_match_candidate_sources_prefers_non_active_and_respects_geo(
    pg_pool, clean_slate
):
    async with pg_pool.acquire() as conn:
        await _insert_source(
            conn, sid="source.r2.active_global", state="active",
            source_class="official", geo=[],
        )
        await _insert_source(
            conn, sid="source.r2.paused_ml", state="paused",
            source_class="official", geo=["ML"],
        )
        both = await cg._match_candidate_sources(
            conn, ["official"], ["ML"], limit=5
        )
        ids = [c["descriptor_id"] for c in both]
        assert ids == ["source.r2.paused_ml", "source.r2.active_global"]

        # A mismatched geo excludes the geo-scoped candidate but keeps the
        # global (no-geo-tag) one — a global feed matches every desk.
        mismatched = await cg._match_candidate_sources(
            conn, ["official"], ["FR"], limit=5
        )
        assert [c["descriptor_id"] for c in mismatched] == ["source.r2.active_global"]


async def test_match_candidate_sources_no_geo_filter_when_desk_has_none(
    pg_pool, clean_slate
):
    async with pg_pool.acquire() as conn:
        await _insert_source(
            conn, sid="source.r2.ml_only", state="paused",
            source_class="analysis", geo=["ML"],
        )
        rows = await cg._match_candidate_sources(conn, ["analysis"], [], limit=5)
        assert [r["descriptor_id"] for r in rows] == ["source.r2.ml_only"]


async def test_attach_candidates_fillable_with_suggested_fetch_url(
    pg_pool, clean_slate
):
    async with pg_pool.acquire() as conn:
        await _insert_source(
            conn, sid="source.r2.paused_fetchable", state="paused",
            source_class="official", geo=["ML"],
            url="https://example.invalid/ml-fuel-feed.xml",
        )
        row = cg.build_gap_requirement_rows(
            [_gap(desk="country_g20_ml", evidence_id=str(uuid4()))]
        )[0]
        full = await cg._attach_candidates(conn, row)
        assert full["fillable"] is True
        assert full["unfillable_reason"] is None
        assert len(full["candidate_sources"]) == 1
        assert full["candidate_sources"][0]["descriptor_id"] == "source.r2.paused_fetchable"
        assert full["suggested_fetch_url"] == "https://example.invalid/ml-fuel-feed.xml"


async def test_attach_candidates_active_candidate_never_suggests_a_fetch(
    pg_pool, clean_slate
):
    """An ALREADY-ACTIVE match is fillable (informative: the cell is still
    starved despite a running source) but never a fetch suggestion — it's
    already being fetched on cadence."""
    async with pg_pool.acquire() as conn:
        await _insert_source(
            conn, sid="source.r2.active_ml", state="active",
            source_class="official", geo=["ML"],
            url="https://example.invalid/already-active.xml",
        )
        row = cg.build_gap_requirement_rows(
            [_gap(desk="country_g20_ml", evidence_id=str(uuid4()))]
        )[0]
        full = await cg._attach_candidates(conn, row)
        assert full["fillable"] is True
        assert full["suggested_fetch_url"] is None


async def test_attach_candidates_unfillable_when_nothing_matches(
    pg_pool, clean_slate
):
    async with pg_pool.acquire() as conn:
        row = cg.build_gap_requirement_rows(
            [_gap(desk="country_g20_ml", evidence_id=str(uuid4()))]
        )[0]
        full = await cg._attach_candidates(conn, row)
        assert full["fillable"] is False
        assert full["unfillable_reason"] == "no_known_feed"
        assert full["candidate_sources"] == []
        assert full["suggested_fetch_url"] is None


# ---------------------------------------------------------------------------
# End-to-end handle() — gap-origin
# ---------------------------------------------------------------------------


async def test_handle_writes_gap_requirement_end_to_end(pg_pool, clean_slate):
    async with pg_pool.acquire() as conn:
        scorecard_id = await _insert_scorecard(
            conn,
            desk="country_g20_ml",
            dimensions={
                **_all_banded(),
                "economic_coercion": _insufficient("no-finding"),
            },
        )
        await _insert_source(
            conn, sid="source.r2.ml_official", state="paused",
            source_class=cg.SOURCE_CLASSES_BY_DIMENSION["economic_coercion"][0],
            geo=["ML"], url="https://example.invalid/ml.xml",
        )

    result = await _run(pg_pool)
    assert result.finding.data["requirements_proposed"] >= 1

    async with pg_pool.acquire() as conn:
        rows = await _requirements(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["natural_key"] == "collection_gap:country_g20_ml:economic_coercion"
    assert r["origin"] == "collection_gap"
    assert r["evidence_kind"] == "analyst_output"
    assert str(r["evidence_id"]) == scorecard_id
    assert r["fillable"] is True
    assert r["status"] == "proposed"
    candidates = json.loads(r["candidate_sources"]) if isinstance(
        r["candidate_sources"], str
    ) else r["candidate_sources"]
    assert candidates and candidates[0]["descriptor_id"] == "source.r2.ml_official"
    assert r["suggested_fetch_url"] == "https://example.invalid/ml.xml"


async def test_handle_idempotent_rerun_no_duplicate(pg_pool, clean_slate):
    async with pg_pool.acquire() as conn:
        await _insert_scorecard(
            conn,
            desk="country_g20_ml",
            dimensions={
                **_all_banded(),
                "economic_coercion": _insufficient("no-finding"),
            },
        )
    await _run(pg_pool)
    await _run(pg_pool)  # re-sweep — the cell is still starved
    async with pg_pool.acquire() as conn:
        rows = await _requirements(conn)
    assert len(rows) == 1


async def test_handle_never_recreates_a_dispositioned_requirement(
    pg_pool, clean_slate
):
    """Once a natural_key exists (ANY status — including a terminal
    'dismissed'/'registered'), a later sweep never writes a second row for
    the same gap — natural_key is the schema-enforced identity, not just an
    open-status dedup."""
    async with pg_pool.acquire() as conn:
        await _insert_scorecard(
            conn,
            desk="country_g20_ml",
            dimensions={
                **_all_banded(),
                "economic_coercion": _insufficient("no-finding"),
            },
        )
        await conn.execute(
            "INSERT INTO collection_requirements "
            "  (natural_key, origin, topic, evidence_kind, evidence_id, "
            "   status, reviewed_by, reviewed_at, fillable, unfillable_reason) "
            "VALUES ($1, 'collection_gap', 'pre-existing', 'analyst_output', "
            "        $2, 'dismissed', 'operator', now(), false, 'no_known_feed')",
            "collection_gap:country_g20_ml:economic_coercion",
            uuid4(),
        )

    result = await _run(pg_pool)
    assert result.finding.data["requirements_proposed"] == 0

    async with pg_pool.acquire() as conn:
        rows = await _requirements(conn)
    assert len(rows) == 1
    assert rows[0]["status"] == "dismissed"
    assert rows[0]["topic"] == "pre-existing"


# ---------------------------------------------------------------------------
# End-to-end handle() — source_request origin
# ---------------------------------------------------------------------------


async def test_handle_drains_source_request_backlog(pg_pool, clean_slate):
    async with pg_pool.acquire() as conn:
        hid = await _insert_source_request(
            conn,
            need="no source covers Kazakhstan energy policy",
            rationale="blocked a scorecard read",
            target_id="country_g20_kz",
        )

    result = await _run(pg_pool)
    assert result.finding.data["requirements_proposed"] >= 1

    async with pg_pool.acquire() as conn:
        rows = await _requirements(conn)
    matches = [r for r in rows if r["origin"] == "source_request"]
    assert len(matches) == 1
    r = matches[0]
    assert r["natural_key"] == f"source_request:{hid}"
    assert r["evidence_kind"] == "hypothesis"
    assert str(r["evidence_id"]) == hid
    assert r["desk"] == "country_g20_kz"
    assert "Kazakhstan" in r["topic"]
    # No known candidate seeded for this fixture — honestly unfillable.
    assert r["fillable"] is False
    assert r["unfillable_reason"] == "no_known_feed"


async def test_handle_source_request_idempotent_rerun(pg_pool, clean_slate):
    async with pg_pool.acquire() as conn:
        await _insert_source_request(conn, need="no coverage of X")
    await _run(pg_pool)
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        rows = await _requirements(conn)
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# A-4 — the THIRD origin (`reference_gap`), written through THIS module's
# writer. The design's three named properties: natural_key uniqueness under
# re-detection, coexistence with a monthly `collection_gap` row for the same
# desk x dimension, and degrade-not-break on any side-write failure.
# ---------------------------------------------------------------------------


def _candidate(**over: Any) -> dict[str, Any]:
    """One A-4 candidate as ``_reference_diff.reference_gap_candidates`` emits
    it, run through ``select_candidates`` so it carries its own natural_key."""
    base = {
        "reference_id": str(uuid4()),
        "target_id": "country_g20_ml",
        "unit": "economic_coercion",
        "geo": ["ML"],
        "ordinal": 1,
        "headline": "New export controls announced",
        "sentence": "Mali announced export controls on lithium concentrate.",
        "entity_folds": ["china"],
        "urls": ["https://example.invalid/story-1"],
        "materiality": "high",
        "n_slice_rows": 120,
        "arms_failed": ["c1_url", "c2_entity", "c3_prose"],
    }
    base.update(over)
    return rgd.select_candidates([base])[0]


def test_reference_gap_row_shape_is_the_designed_one():
    candidate = _candidate()
    row = rgd.build_requirement_row(candidate, priority_rank=0)
    assert row["natural_key"] == (
        "reference_gap:country_g20_ml:economic_coercion:china"
    )
    assert row["origin"] == "reference_gap"
    assert row["desk"] == "country_g20_ml"
    assert row["dimension"] == "economic_coercion"
    assert row["evidence_kind"] == "unit_reference_label"
    assert row["evidence_id"] == candidate["reference_id"]
    # The doctrine map, REUSED and not re-derived.
    assert row["source_classes_wanted"] == list(
        cg.SOURCE_CLASSES_BY_DIMENSION["economic_coercion"]
    )
    assert row["suggested_fetch_url"] == "https://example.invalid/story-1"
    assert "New export controls announced" in row["topic"]
    assert "present in the world reference" in row["rationale"]
    assert "120-row slice" in row["rationale"]
    assert "c1_url" in row["rationale"]


def test_a_reference_unit_outside_the_doctrine_map_falls_back_never_drops():
    """Two of the nine reference units (``proliferation_watch`` /
    ``disruption_status``) are not scorecard dimensions and so are not in
    ``SOURCE_CLASSES_BY_DIMENSION``. The row is still written, with the
    documented default classes."""
    row = rgd.build_requirement_row(
        _candidate(unit="proliferation_watch"), priority_rank=0
    )
    assert row["dimension"] == "proliferation_watch"
    assert row["source_classes_wanted"] == list(cg._DEFAULT_SOURCE_CLASSES)


def test_the_natural_key_carries_the_unit_so_two_desks_questions_are_two_needs():
    """The same polity surfacing under two bounded units is two COLLECTION
    needs — the source classes that would carry an escalation story are not the
    ones that would carry an energy-security story."""
    a = rgd.build_requirement_row(_candidate(), priority_rank=0)["natural_key"]
    b = rgd.build_requirement_row(
        _candidate(unit="escalation"), priority_rank=0
    )["natural_key"]
    assert a != b


async def test_reference_gap_requirement_lands_through_the_shared_writer(
    pg_pool, clean_slate
):
    """A-4 leg 1 through ``collection_gap``'s OWN writer: the candidate-source
    cross-reference runs for this origin exactly as it does for the other two.
    """
    async with pg_pool.acquire() as conn:
        await _insert_source(
            conn, sid="source.a4.ml_official", state="paused",
            source_class=cg.SOURCE_CLASSES_BY_DIMENSION["economic_coercion"][0],
            geo=["ML"], url="https://example.invalid/ml.xml",
        )
    candidate = _candidate()
    written = await cg.write_requirements(
        pg_pool, [rgd.build_requirement_row(candidate, priority_rank=0)]
    )
    assert written == 1
    async with pg_pool.acquire() as conn:
        rows = await _requirements(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["origin"] == "reference_gap"
    assert r["evidence_kind"] == "unit_reference_label"
    assert str(r["evidence_id"]) == candidate["reference_id"]
    assert r["status"] == "proposed"
    assert r["fillable"] is True
    candidates = json.loads(r["candidate_sources"]) if isinstance(
        r["candidate_sources"], str
    ) else r["candidate_sources"]
    assert candidates and candidates[0]["descriptor_id"] == "source.a4.ml_official"
    # THE ONE DIFFERENCE from the other origins: the reference model handed us
    # the exact page, so the item's own url wins over the feed's root.
    assert r["suggested_fetch_url"] == "https://example.invalid/story-1"


async def test_reference_gap_re_detection_never_creates_a_second_row(
    pg_pool, clean_slate
):
    """The design's own requirement: a still-uncollected item re-detected
    tomorrow — a NEW reference row, a new day — re-derives the same
    ``natural_key`` and writes nothing. Proven twice: through the bulk check
    (the writer's fast path) and against the schema's UNIQUE index."""
    first = _candidate()
    assert await cg.write_requirements(
        pg_pool, [rgd.build_requirement_row(first, priority_rank=0)]
    ) == 1
    stats: dict[str, int] = {}
    tomorrow = _candidate(reference_id=str(uuid4()))
    assert tomorrow["reference_id"] != first["reference_id"]
    assert await cg.write_requirements(
        pg_pool, [rgd.build_requirement_row(tomorrow, priority_rank=0)],
        stats=stats,
    ) == 0
    assert stats["existing"] == 1 and stats["failed"] == 0

    async with pg_pool.acquire() as conn:
        rows = await _requirements(conn)
        assert len(rows) == 1
        # And the schema itself refuses, not just the handler's bulk check.
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO collection_requirements "
                "(natural_key, origin, desk, dimension, topic, evidence_kind, "
                " evidence_id) VALUES ($1,'reference_gap','d','u','t',"
                "'unit_reference_label',$2::uuid)",
                rows[0]["natural_key"], str(uuid4()),
            )


async def test_a_monthly_collection_gap_and_a_daily_reference_gap_coexist(
    pg_pool, clean_slate
):
    """THE COEXISTENCE PROPERTY. Same desk, same dimension, two origins, two
    rows — neither displaces the other, and the research dispatcher reads both
    through one door because they differ on ``origin``, not on identity."""
    async with pg_pool.acquire() as conn:
        scorecard_id = await _insert_scorecard(
            conn,
            desk="country_g20_ml",
            dimensions={
                **_all_banded(),
                "economic_coercion": _insufficient("no-finding"),
            },
        )
    # The monthly analyst's own sweep.
    await _run(pg_pool)
    # A-4's daily side-write for the SAME desk x dimension.
    candidate = _candidate()
    assert await cg.write_requirements(
        pg_pool, [rgd.build_requirement_row(candidate, priority_rank=0)]
    ) == 1

    async with pg_pool.acquire() as conn:
        rows = await _requirements(conn)
    assert len(rows) == 2
    by_origin = {r["origin"]: r for r in rows}
    assert set(by_origin) == {"collection_gap", "reference_gap"}
    for row in rows:
        assert row["desk"] == "country_g20_ml"
        assert row["dimension"] == "economic_coercion"
        assert row["status"] == "proposed"
    assert str(by_origin["collection_gap"]["evidence_id"]) == scorecard_id
    assert by_origin["collection_gap"]["evidence_kind"] == "analyst_output"
    assert str(by_origin["reference_gap"]["evidence_id"]) == (
        candidate["reference_id"]
    )
    assert by_origin["reference_gap"]["evidence_kind"] == "unit_reference_label"

    # And a re-sweep of the monthly analyst still writes nothing new — the
    # reference_gap row did not consume the collection_gap key or vice versa.
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        assert len(await _requirements(conn)) == 2


async def test_reference_gap_write_degrades_and_never_raises(pg_pool, clean_slate):
    """DEGRADE-NOT-BREAK, leg 1: the gauge is ``desk_reference``'s durable
    product and a side-write may not cost the operator its measurement."""

    class _ExplodingPool:
        def acquire(self):
            raise RuntimeError("substrate down")

    stats: dict[str, int] = {}
    row = rgd.build_requirement_row(_candidate(), priority_rank=0)
    assert await cg.write_requirements(_ExplodingPool(), [row], stats=stats) == 0
    assert stats["failed"] == 1
    async with pg_pool.acquire() as conn:
        assert await _requirements(conn) == []


async def test_handle_no_gaps_no_requests_writes_nothing(pg_pool, clean_slate):
    result = await _run(pg_pool)
    assert result.finding.data["requirements_proposed"] == 0
    async with pg_pool.acquire() as conn:
        rows = await _requirements(conn)
    assert rows == []


# ---------------------------------------------------------------------------
# Synthetic (deps=None) path never touches the table
# ---------------------------------------------------------------------------


async def test_synthetic_path_reports_zero_requirements_proposed():
    """The deps=None unit-test path has no table to write to — the field is
    present and honestly zero, never omitted or a crash."""
    rows = [
        {
            "id": str(uuid4()),
            "target_id": "country_g20_ml",
            "produced_at": 1,
            "dimensions": {
                **_all_banded(),
                "economic_coercion": _insufficient("no-finding"),
            },
        }
    ]
    result = await cg.handle(rows, {"analyst_id": "collection_gap"}, None)
    assert result.finding.data["requirements_proposed"] == 0
