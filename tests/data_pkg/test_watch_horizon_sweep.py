# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W5 WATCH-CLOSE — the ``watch_horizon_sweep`` sub-handler.

Pinned, on a real Postgres (the same driver the live registry uses):

  * a consult watch whose marker horizon has passed closes — status ``expired``,
    ``resolved_at`` / ``resolved_by`` stamped, the marker gains ``closed_reason``
    ``horizon_passed``, ``resolved_outcome`` stays NULL (the 0/1 calibration
    label is never written by a horizon), thesis and evidence kept;
  * a row the fold stamped ``folded_onto`` closes — status ``withdrawn``, reason
    ``folded`` — and the row it folded onto stays open;
  * a live (future-horizon) consult watch is untouched, a NON-consult
    open_question (a desk's own question, a non-consult marker) is untouched, a
    marker whose horizon will not parse is untouched and counted;
  * idempotent: a second tick closes nothing and rewrites nothing;
  * bounded: ``batch_limit`` closes per tick, oldest first, ``batch_full`` set;
  * dry run counts and writes nothing;
  * a closed row is no longer a fold peer, and a fresh mint of the same watch
    after the horizon is not folded onto it;
  * registration: SUB_HANDLERS + TRACE_ONLY, the knobs are declared, the
    descriptor ships ``draft`` and validates through ``AnalystDescriptor``;
  * the dispatcher path (``run_method`` + deps) returns the closed ids in the
    receipt.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts import consult_watch_fold as wf
from legba.data.analysts import consult_watch_mint as wm
from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
    run_method,
)
from legba.data.analysts.deterministic_handlers import watch_horizon_sweep as sweep
from legba.data.config import PostgresConfig
from legba.data.option_catalog.handler_options import known_option_names
from legba.data.provenance.kinds import TRACE_ONLY

SUB = "watch_horizon_sweep"
TAG = "w5_watchclose"


# ---------------------------------------------------------------------------
# Registration and the pure rule (no DB)
# ---------------------------------------------------------------------------


def test_registered_trace_only_with_declared_options():
    assert SUB in SUB_HANDLERS
    assert OUTPUT_KIND_BY_SUB_HANDLER[SUB] is TRACE_ONLY
    assert {"batch_limit", "dry_run"} <= set(known_option_names(SUB))


def test_descriptor_ships_draft_and_validates():
    import yaml

    from legba.data.schemas.analyst import AnalystDescriptor

    path = Path(__file__).resolve().parents[2] / "descriptors" / "analyst_watch_horizon_sweep.yaml"
    body = yaml.safe_load(path.read_text())
    assert body["identity"]["state"] == "draft"
    assert body["method"]["sub_handler"] == SUB
    AnalystDescriptor.model_validate(body)


def test_the_marker_vocabulary_mirrors_the_mint():
    assert sweep._MARKER_KEY == wm.MARKER_KEY == wf._MARKER_KEY
    assert sweep._ORIGIN == wf._ORIGIN == wf._CREATED_BY


def test_closing_reason_is_pure():
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    past = (now - timedelta(days=1)).isoformat()
    future = (now + timedelta(days=1)).isoformat()
    assert sweep.closing_reason({"resolves_by": past}, now) == "horizon_passed"
    assert sweep.closing_reason({"resolves_by": future}, now) is None
    assert sweep.closing_reason({"resolves_by": now.isoformat()}, now) is None  # strict
    # Folded wins, even on a live horizon and even with a passed one.
    assert sweep.closing_reason({"resolves_by": future, "folded_onto": "x"}, now) == "folded"
    assert sweep.closing_reason({"resolves_by": past, "folded_onto": "x"}, now) == "folded"
    # No / unparseable horizon, no fold: left alone.
    assert sweep.closing_reason({}, now) is None
    assert sweep.closing_reason({"resolves_by": "next tuesday"}, now) is None
    # A naive timestamp is read as UTC, a Z suffix parses.
    assert sweep.closing_reason({"resolves_by": "2026-10-09T00:00:00"}, now) == "horizon_passed"
    assert sweep.closing_reason({"resolves_by": "2026-10-09T00:00:00Z"}, now) == "horizon_passed"


# ---------------------------------------------------------------------------
# Real driver
# ---------------------------------------------------------------------------


class _Deps:
    def __init__(self, pool: Any) -> None:
        self.pg_pool = pool


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)

    async def _wipe() -> None:
        async with p.acquire() as c:
            # The sweep is a GLOBAL read of a session-shared DB: retire every
            # consult watch another file left, open or closed, plus our own rows.
            await c.execute(
                "DELETE FROM hypotheses WHERE analyst_id = $1 OR "
                "diagnostic_evidence @> $2::jsonb",
                TAG, json.dumps([{"marker": wm.MARKER_KEY, "origin": "consult"}]),
            )

    await _wipe()
    yield p
    try:
        await _wipe()
    finally:
        await p.close()


NOW = datetime.now(timezone.utc)
PAST = NOW - timedelta(days=2)
FUTURE = NOW + timedelta(days=9)


async def _seed(
    pool: Any, text: str, horizon: Any, *, desk: str | None = "country_w5_watch",
    produced: datetime | None = None, created_by: str = "consult",
    extra: dict[str, Any] | None = None, analyst_id: str = "consult_default",
) -> str:
    marker: dict[str, Any] = {
        "marker": wm.MARKER_KEY, "origin": "consult", "created_by": created_by,
        "source_id": wm.fold_key(text), "resolution_test": text, "candidate": text,
    }
    if horizon is not None:
        marker["resolves_by"] = horizon.isoformat() if isinstance(horizon, datetime) else horizon
    marker.update(extra or {})
    async with pool.acquire() as c:
        return str(await c.fetchval(
            "INSERT INTO hypotheses (thesis, status, target_id, analyst_id, "
            "diagnostic_evidence, produced_at) VALUES ($1, 'open_question', $2, $3, "
            "$4::jsonb, $5) RETURNING id",
            text, desk, analyst_id, json.dumps([marker]), produced or NOW,
        ))


async def _row(pool: Any, hid: str) -> dict[str, Any]:
    async with pool.acquire() as c:
        r = await c.fetchrow(
            "SELECT status, thesis, resolved_outcome, resolved_at, resolved_by, "
            "diagnostic_evidence, updated_at FROM hypotheses WHERE id = $1::uuid", hid,
        )
    d = dict(r)
    ev = json.loads(d["diagnostic_evidence"])
    d["marker"] = ev[0] if ev else {}
    return d


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_lapsed_consult_watch_closes_with_the_right_outcome(pool):
    hid = await _seed(pool, f"naval-buildup indicator observed {uuid4().hex[:6]}", PAST)
    counters = await sweep.sweep(pool, {})
    row = await _row(pool, hid)
    assert row["status"] == "expired"
    assert row["resolved_by"] == "watch_horizon_sweep" and row["resolved_at"] is not None
    assert row["resolved_outcome"] is None  # a horizon is not a 0/1 calibration label
    assert row["marker"]["closed_reason"] == "horizon_passed"
    assert row["marker"]["closed_by"] == "watch_horizon_sweep"
    assert row["marker"]["resolves_by"] == PAST.isoformat()  # evidence kept
    assert row["thesis"].startswith("naval-buildup")
    assert counters["horizon_passed"] == 1 and counters["folded"] == 0
    assert counters["closed_ids"]["horizon_passed"] == [hid]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_folded_row_closes_and_the_row_it_folded_onto_stays_open(pool):
    standing = await _seed(pool, "major-arms-acquisition indicator observed", FUTURE,
                           produced=NOW - timedelta(hours=2))
    dup = await _seed(pool, "detection of a major arms acquisition", FUTURE,
                      produced=NOW - timedelta(hours=1),
                      extra={"folded_onto": standing, "fold_check": "core"})
    counters = await sweep.sweep(pool, {})
    folded, kept = await _row(pool, dup), await _row(pool, standing)
    assert folded["status"] == "withdrawn" and folded["resolved_outcome"] is None
    assert folded["marker"]["closed_reason"] == "folded"
    assert folded["marker"]["folded_onto"] == standing  # the fold stamp survives
    assert kept["status"] == "open_question" and kept["resolved_at"] is None
    assert "closed_reason" not in kept["marker"]
    assert counters["folded"] == 1 and counters["horizon_passed"] == 0
    assert counters["closed_ids"]["folded"] == [dup] and counters["live"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_and_non_consult_rows_are_untouched(pool):
    live = await _seed(pool, "a live watch", FUTURE)
    # A non-consult marker with a PASSED horizon, a desk's own question with no
    # marker at all, and a consult-shaped row whose created_by is not consult.
    other = await _seed(pool, "someone else's marker", PAST, created_by="unit")
    async with pool.acquire() as c:
        own = str(await c.fetchval(
            "INSERT INTO hypotheses (thesis, status, target_id, analyst_id, "
            "diagnostic_evidence) VALUES ('desk question', 'open_question', "
            "'country_w5_watch', $1, '[]'::jsonb) RETURNING id", TAG,
        ))
    bad = await _seed(pool, "a watch with a garbled horizon", "next tuesday")
    before = {h: await _row(pool, h) for h in (live, other, own, bad)}
    counters = await sweep.sweep(pool, {})
    for h, b in before.items():
        a = await _row(pool, h)
        assert a["status"] == "open_question" and a["resolved_at"] is None, h
        assert a["diagnostic_evidence"] == b["diagnostic_evidence"], h
        assert a["updated_at"] == b["updated_at"], h
    assert counters["scanned"] == 2  # the live watch and the garbled one: consult rows only
    assert counters["live"] == 1 and counters["unparseable"] == 1
    assert counters["horizon_passed"] == 0 and counters["folded"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_sweep_is_idempotent(pool):
    lapsed = await _seed(pool, "lapsed one", PAST)
    folded = await _seed(pool, "folded one", FUTURE, extra={"folded_onto": str(uuid4())})
    live = await _seed(pool, "live one", FUTURE)
    first = await sweep.sweep(pool, {})
    assert (first["horizon_passed"], first["folded"]) == (1, 1)
    snap = {h: await _row(pool, h) for h in (lapsed, folded, live)}
    second = await sweep.sweep(pool, {})
    assert (second["horizon_passed"], second["folded"], second["scanned"]) == (0, 0, 1)
    for h, b in snap.items():
        assert await _row(pool, h) == b, h  # nothing rewritten, updated_at included


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_cap_bounds_closes_per_tick_oldest_first(pool):
    ids = [
        await _seed(pool, f"lapsed {i}", PAST - timedelta(hours=7 - i),
                    produced=NOW - timedelta(hours=10 - i))
        for i in range(7)
    ]
    one = await sweep.sweep(pool, {"batch_limit": 3})
    assert one["horizon_passed"] == 3 and one["batch_full"] == 1 and one["batch_limit"] == 3
    assert one["closed_ids"]["horizon_passed"] == ids[:3]  # oldest first
    two = await sweep.sweep(pool, {"batch_limit": 3})
    assert two["horizon_passed"] == 3 and two["batch_full"] == 1
    three = await sweep.sweep(pool, {"batch_limit": 3})
    assert three["horizon_passed"] == 1 and three["batch_full"] == 0
    assert {(await _row(pool, h))["status"] for h in ids} == {"expired"}
    # The default cap is the brief's 500.
    assert sweep.DEFAULT_BATCH_LIMIT == 500
    assert (await sweep.sweep(pool, {}))["horizon_passed"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_dry_run_counts_and_writes_nothing(pool):
    lapsed = await _seed(pool, "lapsed", PAST)
    before = await _row(pool, lapsed)
    counters = await sweep.sweep(pool, {"dry_run": True})
    assert counters["dry_run"] == 1 and counters["horizon_passed"] == 1
    assert await _row(pool, lapsed) == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_row_changed_between_scan_and_lock_is_skipped(pool):
    hid = await _seed(pool, "raced", PAST)
    # The scan saw it lapsed; a re-ask extends the watch before the close runs.
    async with pool.acquire() as c:
        await c.execute(
            "UPDATE hypotheses SET diagnostic_evidence = jsonb_set(diagnostic_evidence, "
            "'{0,resolves_by}', to_jsonb($2::text)) WHERE id = $1::uuid",
            hid, FUTURE.isoformat(),
        )
    counters = sweep._zero_counters(batch_limit=5, dry_run=False)
    await sweep._close_one(pool, hid, "horizon_passed", NOW, counters)
    assert counters["skipped_changed"] == 1 and counters["horizon_passed"] == 0
    assert (await _row(pool, hid))["status"] == "open_question"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_closed_watch_is_no_fold_peer_and_a_reask_mints_afresh(pool):
    desk = f"country_w5_{uuid4().hex[:6]}"
    text = "major-arms-acquisition indicator observed"
    old = await _seed(pool, text, PAST, desk=desk)
    await sweep.sweep(pool, {})
    async with pool.acquire() as c:
        assert await wf.read_fold_peers(c, desk) == []
        # claim_watch's open set is `status = 'open_question'`: the row is gone from it.
        n = await c.fetchval(
            "SELECT count(*) FROM hypotheses WHERE id = $1::uuid AND status = 'open_question'",
            old,
        )
    assert n == 0
    # A re-ask after the horizon mints a fresh watch: the lexical fold probe
    # (open + horizon not passed) finds nothing to fold onto.
    async with pool.acquire() as c:
        hit = await c.fetchval(
            wm._FOLD_PROBE_SQL,
            json.dumps([{"marker": wm.MARKER_KEY, "origin": "consult"}]),
            wm.fold_key(text),
        )
    assert hit is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_dispatcher_path_returns_the_closed_ids_in_the_receipt(pool, caplog):
    lapsed = await _seed(pool, "lapsed via dispatcher", PAST)
    folded = await _seed(pool, "folded via dispatcher", FUTURE,
                         extra={"folded_onto": str(uuid4())})
    with caplog.at_level("INFO", logger=sweep.logger.name):
        result = await run_method([], {"sub_handler": SUB}, _Deps(pool))
    data = result.finding.data
    assert data["sub_handler"] == SUB
    assert data["closed_ids"] == {"horizon_passed": [lapsed], "folded": [folded]}
    assert result.usage["prompt_tokens"] == 0
    assert "closed 2" in result.finding.title
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith(SUB))
    assert "horizon_passed=1" in line and "folded=1" in line


@pytest.mark.asyncio
async def test_a_failing_sweep_is_a_zeroed_receipt_not_a_failed_tick():
    class _Boom:
        def acquire(self) -> Any:
            raise RuntimeError("db down")

    result = await sweep.handle([], {}, _Deps(_Boom()))
    assert result.finding.data["horizon_passed"] == 0 and result.finding.data["folded"] == 0
    none = await sweep.handle([], {}, None)
    assert none.finding.data["scanned"] == 0
