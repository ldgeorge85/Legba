# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lane N-R — the observation sink follow-ups, against a real, freshly
migrated Postgres (the data_pkg session DB):

* batched inserts + batched receipt links keep the acceptance behaviour (an
  unchanged re-poll writes 0 rows, a revision exactly 1, a conflict is still
  reported) across batch boundaries, and are timed against the pre-lane
  row-by-row path on an 8,036-row PortWatch-shaped fixture;
* every row carries ``license_class`` + ``attribution`` in ``provenance``,
  read back from the PERSISTED row;
* rows written before the stamp resolve licence, attribution and declared
  subject kind at read time (``observation_attribution``) — nothing rewritten;
* PortWatch rows persist ``subject_kind='entity'``;
* an observation-only poll logs ``source_actor.pull.done``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
import yaml

from legba.data.observation_attribution import fetch_attributed
from legba.data.schemas.source import SourceDescriptor
from legba.data.sources.fetch_capture import CaptureLog, FetchCapture
from legba.data.sources.portwatch import COUNT_FIELDS, PortWatchConfig, PortWatchSourceHandler
from legba.data.sources.records import ObservationRecord
from legba.runtime.deps import StandardDeps
from legba.runtime.fetch_receipts import ReceiptRecorder, RecordLink
from legba.runtime.sinks import observation as obs_sink
from legba.runtime.sinks.observation import ObservationSink, live_collection_id
from legba.runtime.source_actor import SourceCore, SourceDeps
from legba.runtime.source_factory import _unwrap_factory_dict

from tests.runtime._ingest_fakes import REPO, NatsRecorder, client_for

SOURCE = "source.imf.portwatch_chokepoints"
ATTRIBUTION = "Source: IMF PortWatch (portwatch.imf.org)"
_BENCH_SOURCES = ("source.bench.n_r_rowwise", "source.bench.n_r_batched")


@pytest_asyncio.fixture
async def pool(migrated_pg):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    async with p.acquire() as c:
        from legba.runtime.state import SCHEMA

        await c.execute(SCHEMA)
        await c.execute("ALTER TABLE fetch_version_records DISABLE TRIGGER fetch_version_records_insert_only")
        await c.execute("DELETE FROM fetch_version_records")
        await c.execute("ALTER TABLE fetch_version_records ENABLE TRIGGER fetch_version_records_insert_only")
        await c.execute("DELETE FROM fetch_drift_observations")
        await c.execute("DELETE FROM fetch_version_tombstones")
        await c.execute("DELETE FROM fetch_versions")
        await c.execute("ALTER TABLE observations DISABLE TRIGGER observations_live_append_only")
        await c.execute("DELETE FROM observations WHERE origin_class = 'live'")
        await c.execute("ALTER TABLE observations ENABLE TRIGGER observations_live_append_only")
        await c.execute("DELETE FROM actor_filter_state WHERE filter_id = $1", f"source:{SOURCE}")
        await c.execute("DELETE FROM source_descriptors WHERE descriptor_id = $1", SOURCE)
    try:
        yield p
    finally:
        # The licence-at-read-time test writes two active `official` heads for
        # SOURCE; left behind, they join every global candidate-source read in
        # the session DB (test_collection_requirements' pollution guard named
        # source.imf.portwatch_chokepoints and errored all 15 of its tests).
        async with p.acquire() as c:
            await c.execute("DELETE FROM source_descriptors WHERE descriptor_id = $1", SOURCE)
            await c.execute("DELETE FROM actor_filter_state WHERE filter_id = $1", f"source:{SOURCE}")
        await p.close()


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    r = tmp_path / "archive"
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(r))
    monkeypatch.setenv("LEGBA_FETCH_RECEIPTS", "1")
    return r


def _body() -> dict[str, Any]:
    body = yaml.safe_load((REPO / "descriptors" / "source_imf_portwatch_chokepoints.yaml").read_text())
    body["identity"]["state"] = "active"
    body["identity"]["version"] = "c" * 16
    return body


def _descriptor(*, n_total_only: bool = True, source_id: str = SOURCE) -> SourceDescriptor:
    body = _body()
    body["identity"]["id"] = source_id
    if n_total_only:
        body["sink"]["config"]["series"] = [s for s in body["sink"]["config"]["series"]
                                            if s["field"] == "n_total"]
    return SourceDescriptor.model_validate(body, strict=False)


def _handler_config(**over: Any) -> PortWatchConfig:
    """The handler config the ACTIVATION path builds from the descriptor."""
    return PortWatchConfig(**{**_unwrap_factory_dict(_descriptor().config), **over})


class _Layer:
    def __init__(self, values: dict[tuple[str, str], int]) -> None:
        self.values = dict(values)

    def __call__(self, request):
        import httpx

        where = request.url.params.get("where", "")
        start = where.split("DATE '")[1][:10]
        rows = sorted((d, p, v) for (d, p), v in self.values.items() if d >= start)
        off = int(request.url.params.get("resultOffset", "0"))
        size = int(request.url.params.get("resultRecordCount", "1000"))
        feats = [{"attributes": {"date": d, "portid": p, "portname": f"Port {p}", "ObjectId": i, "n_total": v}}
                 for i, (d, p, v) in enumerate(rows[off:off + size])]
        body: dict[str, Any] = {"features": feats}
        if off + size < len(rows):
            body["exceededTransferLimit"] = True
        return httpx.Response(200, content=json.dumps(body).encode(),
                              headers={"content-type": "application/json"})


async def _poll(pool, layer: _Layer) -> dict[str, Any]:
    handler = PortWatchSourceHandler(_handler_config(fields=["n_total"], page_size=50),
                                     http_client=client_for(layer))
    core = SourceCore(f"source::{SOURCE}::{'c' * 16}",
                      SourceDeps(descriptor=_descriptor(), deps=StandardDeps(pg_pool=pool,
                                                                            nats_publish=NatsRecorder()),
                                 handler=handler))
    return await core.pull_once()


def _days(n: int) -> list[str]:
    today = datetime.now(tz=timezone.utc).date()
    return [(today - timedelta(days=5 + i)).isoformat() for i in range(n)]


# ---------------------------------------------------------------------------
# Item 3/4/5/6 through the real actor poll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batched_poll_keeps_acceptance_and_stamps_licence_on_the_persisted_row(
        pool, root, monkeypatch, caplog) -> None:
    # Small batches so 80 rows cross several statement boundaries.
    monkeypatch.setattr(obs_sink, "INSERT_BATCH_ROWS", 7)
    import legba.runtime.fetch_receipts as fr

    monkeypatch.setattr(fr, "LINK_BATCH_ROWS", 11)
    days = _days(40)
    layer = _Layer({(d, p): 40 + (i % 3) for i, d in enumerate(days) for p in ("chokepoint1", "chokepoint4")})

    caplog.set_level(logging.INFO, logger="legba.runtime.source_actor")
    first = await _poll(pool, layer)
    assert first["sink_counts"]["observation"]["written"] == 80
    done = [r.getMessage() for r in caplog.records if "source_actor.pull.done" in r.getMessage()]
    assert done and "written=0" in done[-1]
    assert "sinks=observation:written=80,revised=0,unchanged=0,rejected=0" in done[-1]

    async with pool.acquire() as c:
        rows = await c.fetch("SELECT id, subject_kind, provenance FROM observations WHERE source_id = $1", SOURCE)
        assert len(rows) == 80
        for r in rows:
            prov = json.loads(r["provenance"])
            assert prov["license_class"] == "api_terms"
            assert prov["attribution"] == ATTRIBUTION
            assert prov["field"] == "n_total" and prov["portname"].startswith("Port ")
            assert r["subject_kind"] == "entity"
        linked = await c.fetchval(
            "SELECT count(DISTINCT record_id) FROM fetch_version_records WHERE record_kind = 'observation'")
        assert linked == 80
        assert await c.fetchval(
            "SELECT count(*) FROM observations o JOIN fetch_version_records l ON l.record_id = o.id "
            "JOIN fetch_versions v ON v.id = l.version_id WHERE o.fetch_sha256 <> v.sha256") == 0

    # Unchanged re-poll (twice: the trailing window, then the identical request): 0 rows.
    assert (await _poll(pool, layer))["sink_counts"]["observation"]["written"] == 0
    third = await _poll(pool, layer)
    assert third["sink_counts"]["observation"] == {"written": 0, "unchanged": 20, "revised": 0, "rejected": 0}
    # A revision: exactly one new row, linked once.
    layer.values[(days[8], "chokepoint1")] = 999
    fourth = await _poll(pool, layer)
    assert fourth["sink_counts"]["observation"] == {"written": 1, "unchanged": 19, "revised": 1, "rejected": 0}
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM observations WHERE source_id = $1", SOURCE) == 81
        assert await c.fetchval(
            "SELECT count(*) FROM fetch_version_records WHERE record_kind = 'observation'") == 81


@pytest.mark.asyncio
async def test_a_conflicting_row_in_a_batch_is_reported_and_the_rest_written(pool, root) -> None:
    core = type("Core", (), {"descriptor": _descriptor()})()
    sink = ObservationSink(core)
    sha = "ab" * 32
    polled = datetime.now(tz=timezone.utc)
    recs = [ObservationRecord(series_id="imf_portwatch.chokepoint.n_total", subject_kind="entity",
                              subject="chokepoint1", valid_from=date(2026, 10, d), valid_to=date(2026, 10, d),
                              value=Decimal(d), unit="vessels", methodology_version="portwatch-daily-chokepoints/1",
                              source_url="https://x.invalid/q", fetch_sha256=sha) for d in (1, 2, 3)]
    async with pool.acquire() as c:
        # Occupy day 2's identity at the exact record_time the flush will use.
        await c.execute(
            "INSERT INTO observations (collection_id, series_id, subject_kind, subject, valid_from, valid_to, "
            "record_time, value, unit, source_url, sha256, origin_class, provenance, source_id, "
            "methodology_version, fetch_sha256, period, record_time_basis) VALUES "
            "($1, 'imf_portwatch.chokepoint.n_total', 'entity', 'chokepoint1', '2026-10-02', '2026-10-02', "
            "$2, 77, 'vessels', 'u', $3, 'live', '{}', $4, 'portwatch-daily-chokepoints/1', $3, 'day', "
            "'first_fetch')", live_collection_id(SOURCE), polled, sha, SOURCE)
        for r in recs:
            sink.accept(r)
        result = await sink.flush(c, polled_at=polled)
    # Day 2 differs from the held vintage (77 != 2) so it is attempted, and conflicts.
    assert result.written == 2 and result.revised == 0
    assert result.rejected == ["identity_conflict:imf_portwatch.chokepoint.n_total:chokepoint1:2026-10-02"]
    assert sorted(rec.valid_from.day for _, rec in result.written_ids) == [1, 3]


# ---------------------------------------------------------------------------
# Item 4/5: read-time attribution for rows written BEFORE the stamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_stamp_rows_resolve_licence_attribution_and_kind_at_read_time(pool, root) -> None:
    old_head = _body()
    old_head["scope"].pop("attribution")                 # the head the live rows were written under
    for s in old_head["sink"]["config"]["series"]:
        s["subject_kind"] = "global"
    new_head = _body()
    sha = "cd" * 32
    async with pool.acquire() as c:
        ins = ("INSERT INTO source_descriptors (descriptor_id, version, schema_uri, is_head, kind, state, owner, "
               "name, body, created_at) VALUES ($1, $2, 'legba/source/1.0.0', $3, 'portwatch', 'active', 'o', "
               "'PortWatch', $4::jsonb, $5)")
        t0 = datetime.now(tz=timezone.utc) - timedelta(hours=2)
        await c.execute(ins, SOURCE, "old", False, json.dumps(old_head), t0)
        # A pre-lane row: no licence, no attribution, subject_kind global.
        await c.execute(
            "INSERT INTO observations (collection_id, series_id, subject_kind, subject, valid_from, valid_to, "
            "record_time, value, unit, source_url, sha256, origin_class, provenance, source_id, "
            "methodology_version, fetch_sha256, period, record_time_basis, loaded_at) VALUES "
            "($1, 'imf_portwatch.chokepoint.n_total', 'global', 'chokepoint6', '2026-10-05', '2026-10-05', "
            "$2, 12, 'vessels', 'u', $3, 'live', $4::jsonb, $5, 'portwatch-daily-chokepoints/1', $3, 'day', "
            "'first_fetch', $2)",
            live_collection_id(SOURCE), t0 + timedelta(minutes=45), sha,
            json.dumps({"field": "n_total", "portname": "Strait of Hormuz", "object_id": 1}), SOURCE)
        await c.execute(ins, SOURCE, "new", True, json.dumps(new_head), t0 + timedelta(hours=1))
        before = await c.fetchval("SELECT provenance FROM observations WHERE source_id = $1", SOURCE)

        rows = await fetch_attributed(c, SOURCE, series_id="imf_portwatch.chokepoint.n_total")
        assert len(rows) == 1
        row = rows[0]
        assert row["license_class"] == "api_terms" and row["licence_basis"] == "descriptor"
        assert row["attribution"] == ATTRIBUTION            # only the newer head declares it
        assert row["written_under_version"] == "old"
        assert (row["stored_subject_kind"], row["subject_kind"]) == ("global", "entity")
        # Nothing was rewritten.
        assert await c.fetchval("SELECT provenance FROM observations WHERE source_id = $1", SOURCE) == before

        # The dry-run script reports the same, read-only, and writes nothing.
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "n_r_backfill", REPO / "scripts" / "backfill_observation_attribution.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        async with c.transaction(readonly=True):
            assert await mod.run(c, [SOURCE]) == 0
        report = mod.summarise(rows)
        assert report["no_licence"] == 0 and report["no_attribution"] == 0
        assert report["subject_kind_stored_to_declared"] == {"global->entity": 1}


# ---------------------------------------------------------------------------
# Item 3: before/after timing on an 8,036-row PortWatch-shaped fixture
# ---------------------------------------------------------------------------

_ROWWISE_INSERT_SQL = """
INSERT INTO observations (
    collection_id, series_id, subject_kind, subject, valid_from, valid_to,
    record_time, value, unit, value_text, source_url, sha256, origin_class,
    provenance, source_id, methodology_version, fetch_sha256, period,
    record_time_basis
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, 'live',
    $13::jsonb, $14, $15, $12, $16, $17
)
ON CONFLICT DO NOTHING
RETURNING id
"""


def _fixture_records(sha: str) -> list[ObservationRecord]:
    """28 chokepoints x 7 series x 41 days = 8,036 — the first live poll."""
    start = date(2026, 9, 29)
    return [
        ObservationRecord(series_id=f"imf_portwatch.chokepoint.{field}", subject_kind="entity",
                          subject=f"chokepoint{p}", valid_from=start + timedelta(days=d),
                          valid_to=start + timedelta(days=d), value=Decimal(10 + (p * d) % 50),
                          unit="vessels", methodology_version="portwatch-daily-chokepoints/1",
                          source_url="https://services9.arcgis.com/x/FeatureServer/0/query", fetch_sha256=sha,
                          provenance={"portname": f"Port {p}", "object_id": p * 100 + d, "field": field})
        for p in range(1, 29) for field in COUNT_FIELDS for d in range(41)
    ]


def _receipts(source_id: str, body: bytes) -> ReceiptRecorder:
    log = CaptureLog()
    log.captures.append(FetchCapture(
        url=f"https://bench.invalid/{source_id}", method="GET", status=200, content_type="application/json",
        content_encoding=None, role="response", fetched_at=datetime.now(tz=timezone.utc), complete=True,
        body=body, sha256=hashlib.sha256(body).hexdigest(), size=len(body)))
    rec = ReceiptRecorder(source_id=source_id, enabled=True)
    rec.log = log
    return rec


@pytest.mark.asyncio
async def test_eight_thousand_rows_batched_vs_row_by_row(pool, root, capsys) -> None:
    timings: dict[str, dict[str, float]] = {}
    for source_id in _BENCH_SOURCES:
        body = f'{{"bench": "{source_id}"}}'.encode()
        sha = hashlib.sha256(body).hexdigest()
        records = _fixture_records(sha)
        assert len(records) == 8036
        receipts = _receipts(source_id, body)
        polled = datetime.now(tz=timezone.utc)
        async with pool.acquire() as c:
            t0 = time.perf_counter()
            if source_id.endswith("rowwise"):
                written = []
                for rec in records:   # the pre-lane loop, statement for statement
                    new_id = await c.fetchval(
                        _ROWWISE_INSERT_SQL, live_collection_id(source_id), rec.series_id, rec.subject_kind,
                        rec.subject, rec.valid_from, rec.valid_to, polled, rec.value, rec.unit, rec.value_text,
                        rec.source_url, rec.fetch_sha256, json.dumps(rec.provenance, default=str), source_id,
                        rec.methodology_version, rec.period, "first_fetch")
                    written.append((new_id, rec))
                t1 = time.perf_counter()
                for new_id, _rec in written:
                    await receipts.link(c, "observation", new_id, RecordLink(0))
            else:
                desc = _descriptor(n_total_only=False, source_id=source_id)
                sink = ObservationSink(type("Core", (), {"descriptor": desc})())
                for rec in records:
                    sink.accept(rec)
                result = await sink.flush(c, polled_at=polled)
                assert result.written == 8036 and not result.rejected
                written = result.written_ids
                t1 = time.perf_counter()
                await receipts.link_many(c, "observation", [(i, RecordLink(0)) for i, _ in written])
            t2 = time.perf_counter()
            assert await c.fetchval("SELECT count(*) FROM observations WHERE source_id = $1", source_id) == 8036
            assert await c.fetchval(
                "SELECT count(*) FROM fetch_version_records l JOIN observations o ON o.id = l.record_id "
                "WHERE o.source_id = $1", source_id) == 8036
            timings[source_id] = {"insert_s": t1 - t0, "link_s": t2 - t1}
    with capsys.disabled():
        print(f"\nN-R 8,036-row timings: {json.dumps(timings, indent=1)}")
    old, new = (timings[s] for s in _BENCH_SOURCES)
    assert new["insert_s"] < old["insert_s"] and new["link_s"] < old["link_s"]
