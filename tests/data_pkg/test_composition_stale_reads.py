# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A composition marks the unit reads it quotes that are stale.

FRESH_EYES_2026-10-05 §5: 19 of 40 compositions quoted unit reads 34-94 h old
and a reader could not tell. ``composition_stale_reads`` marks (never
re-selects): each quoted head and coverage row gains ``cadence_h`` + ``stale``,
the record gains ``stale_unit_reads``. The cadence lookup is proven on the real
driver with a throwaway head descriptor, deleted at teardown.
"""

from __future__ import annotations

import json
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.composition_stale_reads import (
    STALE_READ_CADENCE_MULTIPLE_ENV,
    UNIT_CADENCE_ROW_KEY,
    mark_stale_reads,
    stale_read_cadence_multiple,
    stamp_unit_cadence,
)


def _data(ages: dict[str, float]) -> dict:
    return {
        "head_ages": {
            "max_h": max(ages.values()),
            "heads": [{"analyst_id": u, "target_id": "d", "age_h": a} for u, a in ages.items()],
        },
        "assembly": {
            "coverage": [
                {"unit": u, "status": "in_basis", "age_h": a} for u, a in ages.items()
            ] + [{"unit": "absent_unit", "status": "no_head_in_horizon"}],
        },
    }


def _rows(cadence: dict[str, float | None]) -> list[dict]:
    out = []
    for unit, c in cadence.items():
        row = {"analyst_id": unit}
        if c is not None:
            row[UNIT_CADENCE_ROW_KEY] = c
        out.append(row)
    return out


def test_a_read_older_than_cadence_times_the_multiple_is_marked_and_counted():
    data = _data({"proliferation_watch": 94.29, "escalation": 6.36, "economic_coercion": 17.14})
    rows = _rows({"proliferation_watch": 11.0, "escalation": 11.0, "economic_coercion": 11.0})
    assert mark_stale_reads(data, rows, multiple=3) == 1
    assert data["stale_unit_reads"] == 1
    heads = {h["analyst_id"]: h for h in data["head_ages"]["heads"]}
    assert heads["proliferation_watch"]["stale"] is True
    assert heads["proliferation_watch"]["cadence_h"] == 11.0
    assert heads["escalation"]["stale"] is False
    assert data["head_ages"]["stale_multiple"] == 3
    cov = {c["unit"]: c for c in data["assembly"]["coverage"]}
    assert cov["proliferation_watch"]["stale"] is True
    assert cov["economic_coercion"]["stale"] is False
    # A unit with no read in the horizon carries no age, so no mark.
    assert "stale" not in cov["absent_unit"]


def test_the_boundary_is_strictly_older_than():
    data = _data({"u": 33.0})
    assert mark_stale_reads(data, _rows({"u": 11.0}), multiple=3) == 0
    data = _data({"u": 33.01})
    assert mark_stale_reads(data, _rows({"u": 11.0}), multiple=3) == 1


def test_an_unknown_cadence_is_not_measured_never_fresh():
    data = _data({"u": 94.0, "v": 2.0})
    # No cadence on any row: nothing written at all (absent, never 0).
    assert mark_stale_reads(data, _rows({"u": None, "v": None}), multiple=3) is None
    assert "stale_unit_reads" not in data
    assert all("stale" not in h for h in data["head_ages"]["heads"])
    # Cadence known for one unit only: the other carries no mark.
    data = _data({"u": 94.0, "v": 2.0})
    assert mark_stale_reads(data, _rows({"u": None, "v": 11.0}), multiple=3) == 0
    heads = {h["analyst_id"]: h for h in data["head_ages"]["heads"]}
    assert "stale" not in heads["u"] and heads["v"]["stale"] is False


def test_no_head_ages_stamp_means_no_count():
    assert mark_stale_reads({}, _rows({"u": 11.0}), multiple=3) is None


def test_the_multiple_is_a_tunable_with_a_safe_default(monkeypatch):
    monkeypatch.delenv(STALE_READ_CADENCE_MULTIPLE_ENV, raising=False)
    assert stale_read_cadence_multiple() == 3.0
    monkeypatch.setenv(STALE_READ_CADENCE_MULTIPLE_ENV, "2")
    assert stale_read_cadence_multiple() == 2.0
    for bad in ("0", "-1", "abc", " "):
        monkeypatch.setenv(STALE_READ_CADENCE_MULTIPLE_ENV, bad)
        assert stale_read_cadence_multiple() == 3.0
    monkeypatch.setenv(STALE_READ_CADENCE_MULTIPLE_ENV, "2")
    data = _data({"u": 23.0})
    assert mark_stale_reads(data, _rows({"u": 11.0})) == 1


# ---------------------------------------------------------------------------
# The cadence lookup on the real driver
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pool(migrated_pg):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=2)
    yield p
    await p.close()


@pytest_asyncio.fixture
async def throwaway_units(pool):
    tag = uuid4().hex[:8]
    with_cadence, without = f"w11_stale_unit_{tag}", f"w11_stale_unit_nocad_{tag}"
    async with pool.acquire() as conn:
        for unit, cadence in ((with_cadence, {"cooldown_seconds": 39600}), (without, {})):
            await conn.execute(
                """
                INSERT INTO analyst_descriptors
                    (descriptor_id, version, schema_uri, is_head, kind, state, owner,
                     name, body)
                VALUES ($1, 'v1', 'legba/analyst/1.0.0', TRUE,
                        'inline_target', 'active', 'test', 'W11 stale-read probe', $2::jsonb)
                """,
                unit,
                json.dumps({"identity": {"id": unit, "kind": "inline_target"}, "cadence": cadence}),
            )
    yield with_cadence, without
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM analyst_descriptors WHERE descriptor_id = ANY($1::text[])",
            [with_cadence, without],
        )


@pytest.mark.asyncio
async def test_the_unit_cadence_rides_the_rows_from_the_head_descriptor(pool, throwaway_units):
    with_cadence, without = throwaway_units
    rows = [
        {"analyst_id": with_cadence, "age_h": 40.0},
        {"analyst_id": without, "age_h": 40.0},
        {"analyst_id": "w11_no_such_unit", "age_h": 40.0},
    ]
    async with pool.acquire() as conn:
        await stamp_unit_cadence(conn, rows)
    assert rows[0][UNIT_CADENCE_ROW_KEY] == 11.0
    assert UNIT_CADENCE_ROW_KEY not in rows[1]  # no declared cooldown ⇒ unknown
    assert UNIT_CADENCE_ROW_KEY not in rows[2]
    data = _data({with_cadence: 40.0, without: 40.0})
    assert mark_stale_reads(data, rows, multiple=3) == 1
