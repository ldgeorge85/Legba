# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W5 SRC-4 -- per-stage enrichment latency counters.

The wrap records (and counts errors without changing behaviour), the ring is
bounded, the per-poll summary lands on the source's ``cursor`` row in
``actor_filter_state`` (read back with SQL on the real driver) and is carried
forward by a poll that enriched nothing, the slow-stage log line fires, and the
fleet read + the ``/v3/source-quality`` typed field show absence as absence.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from legba.data.registry.source_quality_api import (
    AssertedQualityOut,
    ComputedQualityOut,
    EnrichmentLastPollOut,
    SourceQualityRow,
    _attach_enrichment,
)
from legba.data.sources._contract import InMemoryStateStore, Signal
from legba.runtime import enrichment_timing as et
from legba.runtime.deps import StandardDeps
from legba.runtime.pipeline import PipelineRunner
from legba.runtime.source_actor import SourceCore, SourceDeps
from tests.runtime.test_keyword_gate import (
    _PG,
    _core,
    _descriptor,
    _entry,
    _StubHandler,
)


class _Handler:
    """A filter-handler double: ``transform`` sleeps, optionally raises."""

    marker = "delegated"

    def __init__(self, delay_s: float = 0.0, boom: bool = False) -> None:
        self.delay_s = delay_s
        self.boom = boom

    async def transform(self, signal, ctx):
        await asyncio.sleep(self.delay_s)
        if self.boom:
            raise RuntimeError("stage blew up")
        return signal


class _Ctx:
    class logger:  # noqa: N801 -- mirrors FilterContext.logger
        @staticmethod
        def exception(*a, **k):
            return None


@pytest.fixture(autouse=True)
def _clean():
    et._ring.clear()
    et._poll.clear()
    yield
    et._ring.clear()
    et._poll.clear()


@pytest.mark.asyncio
async def test_the_wrap_records_duration_calls_and_max():
    sid = "source.test.timing"
    h = et.timed_stage("geocode", _Handler(0.02), sid)
    sig = Signal(source_id=sid, modality="text", payload={})
    assert await h.transform(sig, None) is sig
    await h.transform(sig, None)
    summary = et.cursor_fields(sid, None)["enrichment_last_poll"]["geocode"]
    assert summary["calls"] == 2 and summary["errors"] == 0
    assert summary["total_ms"] >= 35 and summary["max_ms"] >= 18
    assert summary["max_ms"] <= summary["total_ms"]
    assert len(et.ring_ms("geocode")) == 2
    # the accumulator drained: the next poll starts from zero
    assert "enrichment_last_poll" not in et.cursor_fields(sid, None)


@pytest.mark.asyncio
async def test_errors_are_counted_and_re_raised_and_other_attrs_delegate():
    sid = "source.test.timing_err"
    h = et.timed_stage("ner_multilingual", _Handler(boom=True), sid)
    assert h.marker == "delegated"  # health_check / gate / ... stay reachable
    with pytest.raises(RuntimeError, match="stage blew up"):
        await h.transform(Signal(source_id=sid, modality="text", payload={}), None)
    s = et.cursor_fields(sid, None)["enrichment_last_poll"]["ner_multilingual"]
    assert s["calls"] == 1 and s["errors"] == 1


@pytest.mark.asyncio
async def test_a_cancelled_stage_is_recorded_as_an_error():
    sid = "source.test.timing_cancel"
    h = et.timed_stage("classify", _Handler(5.0), sid)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            h.transform(Signal(source_id=sid, modality="text", payload={}), None), 0.05,
        )
    s = et.cursor_fields(sid, None)["enrichment_last_poll"]["classify"]
    assert s["calls"] == 1 and s["errors"] == 1


@pytest.mark.asyncio
async def test_the_pipeline_runner_still_drops_on_error_and_the_error_is_counted():
    sid = "source.test.timing_runner"
    runner = PipelineRunner(
        stages=[("geocode", et.timed_stage("geocode", _Handler(), sid)),
                ("ner", et.timed_stage("ner", _Handler(boom=True), sid))],
        ctx_factory=lambda fid: _Ctx(),
    )

    async def _gen():
        yield Signal(source_id=sid, modality="text", payload={})

    out = [s async for s in runner.run(_gen())]
    assert out == []  # PipelineRunner's own error handling is unchanged
    fields = et.cursor_fields(sid, None)["enrichment_last_poll"]
    assert fields["geocode"]["errors"] == 0 and fields["ner"]["errors"] == 1


def test_the_ring_is_bounded_per_kind():
    for i in range(et.RING_SIZE + 25):
        et._record(None, "geocode", float(i), error=False)
    ring = et.ring_ms("geocode")
    assert len(ring) == et.RING_SIZE and ring[-1] == float(et.RING_SIZE + 24)
    assert et.ring_ms("never_ran") == []
    assert "enrichment_last_poll" not in et.cursor_fields("nobody", None)


def test_a_poll_that_enriched_nothing_carries_the_prior_summary_forward():
    prior = {
        "enrichment_last_poll": {"geocode": {"calls": 3, "total_ms": 300.0, "max_ms": 120.0, "errors": 0}},
        "enrichment_polled_at": "2026-10-04T09:00:00+00:00",
    }
    assert et.cursor_fields("s", prior) == prior
    # never measured -> both keys absent (not a zero, not an empty dict)
    assert et.cursor_fields("s", {"rows_pulled": 3}) == {}
    assert et.cursor_fields("s", {"enrichment_last_poll": {}}) == {}
    # a measured poll replaces the carried summary and re-stamps the as-of
    et._record("s", "geocode", 40.0, error=False)
    got = et.cursor_fields("s", prior)
    assert got["enrichment_last_poll"]["geocode"]["calls"] == 1
    assert got["enrichment_polled_at"] != prior["enrichment_polled_at"]


def test_slow_stage_logs_one_info_line_per_poll(caplog):
    et._record("source.slow", "ner_multilingual", 11_000.0, error=False)
    et._record("source.slow", "geocode", 300.0, error=False)
    with caplog.at_level(logging.INFO, logger=et.logger.name):
        et.cursor_fields("source.slow", None)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("enrichment.slow")]
    assert lines == ["enrichment.slow source_id=source.slow kind=ner_multilingual total_ms=11000"]


@pytest.mark.asyncio
async def test_the_cursor_carries_the_stage_summary_through_pull_once():
    """SourceCore + a real PipelineRunner over timed stages: the summary rides
    the cursor next to the keyword counters; an empty second poll keeps it."""
    sid = f"source.test.kw_en_{uuid4().hex[:8]}"
    sd = _descriptor(sid)
    runner = PipelineRunner(
        stages=[("geocode", et.timed_stage("geocode", _Handler(0.01), sid)),
                ("ner_multilingual", et.timed_stage("ner_multilingual", _Handler(0.02), sid))],
        ctx_factory=lambda fid: _Ctx(),
    )

    async def _stage(signal, ctx):
        out = signal

        async def _gen():
            yield signal

        async for s in runner.run(_gen()):
            out = s
        return out

    base = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    store = InMemoryStateStore()
    core, _ = _core(sd, _StubHandler([_entry(f"t{i}", base + timedelta(minutes=i)) for i in range(3)]), store)
    core.sd.enrichment_stage = _stage
    result = await core.pull_once()
    assert result["signals_written"] == 3
    cur = await store.get("cursor")
    stages = cur["enrichment_last_poll"]
    assert set(stages) == {"geocode", "ner_multilingual"}
    assert stages["geocode"]["calls"] == 3 and stages["ner_multilingual"]["calls"] == 3
    assert stages["ner_multilingual"]["total_ms"] >= 50
    assert cur["enrichment_polled_at"]

    core.sd.handler = _StubHandler([])  # an empty poll: summary + as-of unchanged
    await core.pull_once()
    cur2 = await store.get("cursor")
    assert cur2["enrichment_last_poll"] == stages
    assert cur2["enrichment_polled_at"] == cur["enrichment_polled_at"]


@pytest.mark.asyncio
async def test_cursor_row_and_fleet_read_on_the_real_driver():
    """The summary lands in ``actor_filter_state`` (read back with SQL), the
    fleet read returns it, and a source with no summary is absent -- the
    ``/v3/source-quality`` typed field is null for it, not a 0."""
    import asyncpg

    from legba.runtime.state import SCHEMA as STATE_SCHEMA

    try:
        pool = await asyncpg.create_pool(**_PG)
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover
        pytest.skip(f"test Postgres unavailable: {exc}")
    sid = f"source.test.kw_enpg_{uuid4().hex[:8]}"
    bare = f"source.test.kw_enpg_bare_{uuid4().hex[:8]}"
    try:
        async with pool.acquire() as c:
            await c.execute(STATE_SCHEMA)
        sd = _descriptor(sid)
        actor_id = f"source::{sid}::enpg"
        core = SourceCore(actor_id, SourceDeps(
            descriptor=sd, deps=StandardDeps(pg_pool=pool, nats_publish=None),
        ))

        async def _stage(signal, ctx):
            return await et.timed_stage("geocode", _Handler(0.01), sid).transform(signal, ctx)

        core.sd.enrichment_stage = _stage
        base = datetime.now(tz=timezone.utc) - timedelta(minutes=10)
        core.sd.handler = _StubHandler([_entry("a", base), _entry("b", base + timedelta(minutes=1))])
        result = await core.pull_once()
        assert result["signals_written"] == 2

        async with pool.acquire() as c:
            raw = await c.fetchval(
                "SELECT value FROM public.actor_filter_state "
                "WHERE actor_id=$1 AND filter_id=$2 AND state_key='cursor'",
                actor_id, f"source:{sid}",
            )
        row = raw if isinstance(raw, dict) else json.loads(raw)
        assert row["enrichment_last_poll"]["geocode"]["calls"] == 2
        assert row["enrichment_polled_at"]
        assert "rows_pulled" in row  # the same row as the SRC-3 counters

        got = await et.fetch_enrichment_last_poll(pool, [sid, bare])
        assert set(got) == {sid}  # `bare` has no cursor row: NOT MEASURED
        assert got[sid]["stages"]["geocode"]["calls"] == 2

        def _row(source_id: str) -> SourceQualityRow:
            return SourceQualityRow(
                source_id=source_id, registered=True, declared_state="active",
                asserted=AssertedQualityOut(), computed=ComputedQualityOut(),
            )

        rows = [_row(sid), _row(bare)]
        await _attach_enrichment(pool, rows)
        assert isinstance(rows[0].computed.enrichment_last_poll, EnrichmentLastPollOut)
        assert rows[0].computed.enrichment_last_poll.stages["geocode"].calls == 2
        assert rows[1].computed.enrichment_last_poll is None
        # a non-active head is never measured
        inactive = _row(sid)
        inactive.declared_state = "paused"
        await _attach_enrichment(pool, [inactive])
        assert inactive.computed.enrichment_last_poll is None
    finally:
        async with pool.acquire() as c:
            await c.execute(
                "DELETE FROM public.actor_filter_state WHERE filter_id LIKE "
                "'source:source.test.kw_enpg_%'"
            )
            await c.execute(
                "DELETE FROM public.source_poll_outcomes WHERE source_id LIKE "
                "'source.test.kw_enpg_%'"
            )
            await c.execute(
                "DELETE FROM signals WHERE source_id LIKE 'source.test.kw_enpg_%'"
            )
        await pool.close()


from tests.runtime.test_source_staleness import _seed_active_source  # noqa: E402
from tests.runtime.test_substrate_query_port import pg_pool  # noqa: E402,F401


@pytest.mark.asyncio
async def test_get_source_health_rows_carry_enrichment_last_poll_or_none(pg_pool) -> None:
    from legba.runtime.substrate_source_health import get_source_health

    timed = f"source.enr.timed.{uuid4().hex[:8]}"
    untimed = f"source.enr.untimed.{uuid4().hex[:8]}"
    for sid in (timed, untimed):
        await _seed_active_source(
            pg_pool, source_id=sid, cron="7 * * * *", signal_ages_h=[1.0],
            poll_outcome="success",
        )
    cursor = {
        "source_id": timed, "rows_pulled": 4,
        "enrichment_last_poll": {
            "ner_multilingual": {"calls": 3, "total_ms": 1200.0, "max_ms": 500.0, "errors": 0},
        },
        "enrichment_polled_at": "2026-10-04T09:00:00+00:00",
    }
    from legba.runtime.state import SCHEMA as STATE_SCHEMA

    async with pg_pool.acquire() as conn:
        await conn.execute(STATE_SCHEMA)  # the bare test DB may lack the table
        await conn.execute(
            "INSERT INTO public.actor_filter_state "
            "(actor_id, filter_id, state_key, value) VALUES ($1, $2, 'cursor', $3::jsonb)",
            f"source::{timed}::x", f"source:{timed}", json.dumps(cursor),
        )
    try:
        health = await get_source_health(pg_pool, silent_hours=48)
        rows = {r["source_id"]: r for r in health["rows"]}
        got = rows[timed]["enrichment_last_poll"]
        assert got["stages"]["ner_multilingual"]["calls"] == 3
        assert got["polled_at"] == "2026-10-04T09:00:00+00:00"
        assert rows[untimed]["enrichment_last_poll"] is None  # not measured, not 0
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM public.actor_filter_state WHERE filter_id = $1", f"source:{timed}",
            )
