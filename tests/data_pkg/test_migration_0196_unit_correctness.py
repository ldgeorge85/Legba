# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0196 — the correctness tables, against a real Postgres.

Everything here runs against real SQL, and that is not ceremony: this
migration's guarantees are all properties of the DATABASE. It must be idempotent
under a runner that re-globs every file; its idempotence index must make a
re-graded head a no-op rather than a second population member; its CHECKs must
make an impossible number unrepresentable rather than merely discouraged; and
the two SHARES must be able to be NULL, because ``NULL`` and ``0.0`` are
different findings about a unit and the whole instrument turns on the
difference. A test that mocked the connection would assert only that the Python
called the SQL it was written to call.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers._correctness_rubric import (
    RUBRIC_SHA256,
)
from legba.data.config import PostgresConfig
from legba.data.migrations import MIGRATIONS_DIR

MIGRATION_NAME = "0196_unit_correctness_grading.sql"

_REF_SQL = """
INSERT INTO unit_references (
    id, target_id, window_start, window_end, built_at, builder, ref_json,
    span_verified_rate, thin_dimensions, sha256
) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9::text[],$10)
RETURNING id
"""

_UC_SQL = """
INSERT INTO unit_correctness (
    id, analyst_id, target_id, head_id, as_of, reference_id, rubric_sha, grain,
    n_claims, n_contains, n_contradicts, n_silent, n_split, n_unparseable,
    n_single_family, correctness_share, coverage_share, families, cost_usd
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,
          $18::jsonb,$19)
RETURNING id
"""

_CLAIM_SQL = """
INSERT INTO unit_correctness_claims (
    id, correctness_id, claim_id, grain, claim_text, label_by_family,
    adjudicated, n_families, single_family, spans
) VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10::jsonb)
RETURNING id
"""

_CAL_SQL = """
INSERT INTO grader_calibrations (
    id, rubric_sha, model_ids, pooled, pairwise, gate_pass, packet_sha, n_atoms
) VALUES ($1,$2,$3::jsonb,$4,$5::jsonb,$6,$7,$8)
RETURNING id
"""


def _migration_sql() -> str:
    return (MIGRATIONS_DIR / MIGRATION_NAME).read_text(encoding="utf-8")


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    yield c
    await c.close()


@pytest_asyncio.fixture
async def scratch(conn):
    """A target id this file exclusively owns, wiped either side."""
    target = f"country_test_{uuid4().hex[:8]}"

    async def _wipe():
        await conn.execute(
            "DELETE FROM unit_correctness WHERE target_id = $1", target
        )
        await conn.execute(
            "DELETE FROM unit_references WHERE target_id = $1", target
        )

    await _wipe()
    yield target
    await _wipe()


async def _ref(conn, target: str, *, sha: str | None = None, **over):
    now = datetime.now(timezone.utc)
    kwargs = {
        "window_start": now - timedelta(days=14),
        "window_end": now + timedelta(days=1),
        "built_at": now,
        "builder": "pytest-lane",
        "span_verified_rate": None,
        "thin_dimensions": ["proliferation_watch"],
    }
    kwargs.update(over)
    return await conn.fetchval(
        _REF_SQL, uuid4(), target, kwargs["window_start"], kwargs["window_end"],
        kwargs["built_at"], kwargs["builder"], json.dumps({"ref_developments": []}),
        kwargs["span_verified_rate"], kwargs["thin_dimensions"],
        sha or uuid4().hex + uuid4().hex,
    )


async def _unit(conn, target: str, ref_id, **over):
    row = {
        "analyst_id": "internal_stability",
        "head_id": uuid4(),
        "as_of": datetime.now(timezone.utc),
        "rubric_sha": RUBRIC_SHA256,
        "grain": "desk",
        "n_claims": 4, "n_contains": 2, "n_contradicts": 1, "n_silent": 1,
        "n_split": 0, "n_unparseable": 0, "n_single_family": 4,
        "correctness_share": Decimal("0.6667"),
        "coverage_share": Decimal("0.75"),
        "families": {"F0": {"n_calls": 4}},
        "cost_usd": Decimal("0"),
    }
    row.update(over)
    return await conn.fetchval(
        _UC_SQL, uuid4(), row["analyst_id"], target, row["head_id"],
        row["as_of"], ref_id, row["rubric_sha"], row["grain"], row["n_claims"],
        row["n_contains"], row["n_contradicts"], row["n_silent"],
        row["n_split"], row["n_unparseable"], row["n_single_family"],
        row["correctness_share"], row["coverage_share"],
        json.dumps(row["families"]), row["cost_usd"],
    )


# ---------------------------------------------------------------------------
# The migration itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_four_tables_exist(conn):
    for table in ("unit_references", "unit_correctness",
                  "unit_correctness_claims", "grader_calibrations"):
        assert await conn.fetchval(
            "SELECT to_regclass($1)", f"public.{table}"
        ), table


@pytest.mark.asyncio
async def test_reapplying_the_migration_is_a_no_op(conn):
    """The runner re-globs every file. A second apply must not raise and must
    not drop anything the first apply created."""
    sql = _migration_sql()
    await conn.execute(sql)
    await conn.execute(sql)
    assert await conn.fetchval("SELECT to_regclass('public.unit_correctness')")
    indexes = {
        r["indexname"] for r in await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename = "
            "'unit_correctness'"
        )
    }
    assert "uq_unit_correctness_run" in indexes


@pytest.mark.asyncio
async def test_the_indexes_the_read_paths_need_exist(conn):
    by_table = {}
    for table in ("unit_references", "unit_correctness",
                  "unit_correctness_claims", "grader_calibrations"):
        by_table[table] = {
            r["indexname"] for r in await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE tablename = $1", table
            )
        }
    assert "idx_unit_references_target_window" in by_table["unit_references"]
    assert "uq_unit_references_target_sha" in by_table["unit_references"]
    assert "idx_unit_correctness_target_as_of" in by_table["unit_correctness"]
    assert "idx_unit_correctness_analyst_target" in by_table["unit_correctness"]
    assert "uq_unit_correctness_run" in by_table["unit_correctness"]
    assert "uq_unit_correctness_claims_claim" in by_table[
        "unit_correctness_claims"
    ]
    assert "uq_grader_calibrations_draw" in by_table["grader_calibrations"]


# ---------------------------------------------------------------------------
# unit_references
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_same_reference_loaded_twice_is_one_row(conn, scratch):
    sha = uuid4().hex + uuid4().hex
    await _ref(conn, scratch, sha=sha)
    with pytest.raises(asyncpg.UniqueViolationError):
        await _ref(conn, scratch, sha=sha)


@pytest.mark.asyncio
async def test_a_backwards_window_is_refused(conn, scratch):
    now = datetime.now(timezone.utc)
    with pytest.raises(asyncpg.CheckViolationError):
        await _ref(conn, scratch, window_start=now, window_end=now - timedelta(1))


@pytest.mark.asyncio
async def test_a_span_rate_outside_the_unit_interval_is_refused(conn, scratch):
    with pytest.raises(asyncpg.CheckViolationError):
        await _ref(conn, scratch, span_verified_rate=Decimal("1.5"))
    # NULL is legal and is NOT zero — the builder reported none.
    ref_id = await _ref(conn, scratch, span_verified_rate=None)
    assert await conn.fetchval(
        "SELECT span_verified_rate FROM unit_references WHERE id = $1", ref_id
    ) is None


@pytest.mark.asyncio
async def test_the_window_lookup_finds_the_reference_covering_a_stamp(
    conn, scratch
):
    """The grader's own query: the reference whose window CONTAINS the stamp."""
    now = datetime.now(timezone.utc)
    inside = await _ref(
        conn, scratch,
        window_start=now - timedelta(days=14), window_end=now + timedelta(days=1),
    )
    await _ref(
        conn, scratch,
        window_start=now - timedelta(days=90), window_end=now - timedelta(days=60),
    )
    got = await conn.fetchval(
        """
        SELECT id FROM unit_references
         WHERE target_id = $1 AND window_start <= $2 AND window_end >= $2
         ORDER BY built_at DESC LIMIT 1
        """,
        scratch, now,
    )
    assert got == inside


# ---------------------------------------------------------------------------
# unit_correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_unit_row_inserts_and_carries_both_shares(conn, scratch):
    ref_id = await _ref(conn, scratch)
    unit_id = await _unit(conn, scratch, ref_id)
    row = await conn.fetchrow(
        "SELECT * FROM unit_correctness WHERE id = $1", unit_id
    )
    assert float(row["correctness_share"]) == pytest.approx(0.6667)
    assert float(row["coverage_share"]) == 0.75
    assert row["rubric_sha"] == RUBRIC_SHA256


@pytest.mark.asyncio
async def test_the_idempotence_index_makes_a_regrade_a_no_op(conn, scratch):
    """Same head, same reference, same rubric = the same measurement. Writing it
    twice would double a population somebody later takes a mean over."""
    ref_id = await _ref(conn, scratch)
    head_id = uuid4()
    await _unit(conn, scratch, ref_id, head_id=head_id)
    with pytest.raises(asyncpg.UniqueViolationError):
        await _unit(conn, scratch, ref_id, head_id=head_id)
    # A DIFFERENT reference for the same head is a different measurement and is
    # allowed — that is a re-grade against better evidence, not a duplicate.
    other_ref = await _ref(conn, scratch)
    assert await _unit(conn, scratch, other_ref, head_id=head_id)


@pytest.mark.asyncio
async def test_a_null_correctness_share_is_legal_and_is_not_zero(conn, scratch):
    """The single most consequential column decision in the file: a unit whose
    reference bore on nothing has NO share, not a zero."""
    ref_id = await _ref(conn, scratch)
    unit_id = await _unit(
        conn, scratch, ref_id,
        n_claims=5, n_contains=0, n_contradicts=0, n_silent=5,
        correctness_share=None, coverage_share=Decimal("0"),
    )
    row = await conn.fetchrow(
        "SELECT correctness_share, coverage_share FROM unit_correctness "
        "WHERE id = $1", unit_id,
    )
    assert row["correctness_share"] is None
    assert float(row["coverage_share"]) == 0.0


@pytest.mark.asyncio
async def test_counts_that_do_not_reconcile_are_unrepresentable(conn, scratch):
    """The labels must partition the claims. A row whose parts do not sum to its
    n is an arithmetic impossibility and the schema refuses it."""
    ref_id = await _ref(conn, scratch)
    with pytest.raises(asyncpg.CheckViolationError):
        await _unit(
            conn, scratch, ref_id,
            n_claims=10, n_contains=2, n_contradicts=1, n_silent=1,
            n_split=0, n_unparseable=0,
        )


@pytest.mark.asyncio
async def test_a_share_outside_the_unit_interval_is_refused(conn, scratch):
    ref_id = await _ref(conn, scratch)
    with pytest.raises(asyncpg.CheckViolationError):
        await _unit(conn, scratch, ref_id, correctness_share=Decimal("1.4"))


@pytest.mark.asyncio
async def test_a_third_grain_is_unrepresentable(conn, scratch):
    ref_id = await _ref(conn, scratch)
    with pytest.raises(asyncpg.CheckViolationError):
        await _unit(conn, scratch, ref_id, grain="world")


@pytest.mark.asyncio
async def test_a_number_cannot_outlive_the_reference_it_rests_on(conn, scratch):
    """ON DELETE RESTRICT: deleting a reference a published share was computed
    against would leave a number nobody can re-argue."""
    ref_id = await _ref(conn, scratch)
    await _unit(conn, scratch, ref_id)
    with pytest.raises(asyncpg.RestrictViolationError):
        await conn.execute("DELETE FROM unit_references WHERE id = $1", ref_id)


# ---------------------------------------------------------------------------
# unit_correctness_claims
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_claim_ledger_cascades_with_its_unit(conn, scratch):
    ref_id = await _ref(conn, scratch)
    unit_id = await _unit(conn, scratch, ref_id)
    await conn.fetchval(
        _CLAIM_SQL, uuid4(), unit_id, "DR-002e675d", "desk", "a claim",
        json.dumps({"F0": "contains"}), "contains", 1, True,
        json.dumps({"F0": {"decisive_span": "x"}}),
    )
    assert await conn.fetchval(
        "SELECT count(*) FROM unit_correctness_claims WHERE correctness_id = $1",
        unit_id,
    ) == 1
    await conn.execute("DELETE FROM unit_correctness WHERE id = $1", unit_id)
    assert await conn.fetchval(
        "SELECT count(*) FROM unit_correctness_claims WHERE correctness_id = $1",
        unit_id,
    ) == 0


@pytest.mark.asyncio
async def test_the_adjudicated_vocabulary_is_closed(conn, scratch):
    """`split` and `unparseable` are OUTCOMES, not labels, and a fourth label is
    unrepresentable rather than merely discouraged."""
    ref_id = await _ref(conn, scratch)
    unit_id = await _unit(conn, scratch, ref_id)
    for allowed in ("contains", "contradicts", "silent", "split", "unparseable"):
        assert await conn.fetchval(
            _CLAIM_SQL, uuid4(), unit_id, f"DR-{allowed[:8]}", "desk", "t",
            json.dumps({}), allowed, 0, False, json.dumps({}),
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.fetchval(
            _CLAIM_SQL, uuid4(), unit_id, "DR-partial", "desk", "t",
            json.dumps({}), "partially_contains", 0, False, json.dumps({}),
        )


@pytest.mark.asyncio
async def test_one_claim_per_unit_row(conn, scratch):
    ref_id = await _ref(conn, scratch)
    unit_id = await _unit(conn, scratch, ref_id)
    args = (unit_id, "DR-002e675d", "desk", "t", json.dumps({}), "silent", 1,
            True, json.dumps({}))
    await conn.fetchval(_CLAIM_SQL, uuid4(), *args)
    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.fetchval(_CLAIM_SQL, uuid4(), *args)


# ---------------------------------------------------------------------------
# grader_calibrations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_pass_with_no_measured_rate_is_unrepresentable(conn):
    """An empty-overlap calibration is UNMEASURED. A gate that passed without a
    number is not a gate."""
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.fetchval(
            _CAL_SQL, uuid4(), RUBRIC_SHA256, json.dumps({"F0": "m"}), None,
            json.dumps([]), True, "b" * 64, 30,
        )
    # A FIRED gate with no number is legal — it is an honest record of a run
    # that could not be scored.
    fired = await conn.fetchval(
        _CAL_SQL, uuid4(), RUBRIC_SHA256, json.dumps({"F0": "m"}), None,
        json.dumps([]), False, "c" * 64, 30,
    )
    assert fired
    await conn.execute("DELETE FROM grader_calibrations WHERE id = $1", fired)


@pytest.mark.asyncio
async def test_the_same_draw_cannot_be_scored_twice(conn):
    """A second roll of the same dice is not independent confirmation."""
    packet_sha = uuid4().hex + uuid4().hex
    models = json.dumps({"F0": "core-120b"})
    first = await conn.fetchval(
        _CAL_SQL, uuid4(), RUBRIC_SHA256, models, Decimal("0.84"),
        json.dumps([]), True, packet_sha, 30,
    )
    try:
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.fetchval(
                _CAL_SQL, uuid4(), RUBRIC_SHA256, models, Decimal("0.90"),
                json.dumps([]), True, packet_sha, 30,
            )
    finally:
        await conn.execute(
            "DELETE FROM grader_calibrations WHERE id = $1", first
        )


@pytest.mark.asyncio
async def test_a_malformed_rubric_sha_is_refused(conn):
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.fetchval(
            _CAL_SQL, uuid4(), "not-a-sha", json.dumps({}), Decimal("0.9"),
            json.dumps([]), True, "d" * 64, 30,
        )
