# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-L acceptance — against a real, freshly migrated Postgres (migrations 0245
+ 0246 applied by the session fixture) and a private evidence-store root.

The lane's definition of done:
  * re-polling unchanged data writes zero observation rows and stores no new
    object;
  * a revised day writes exactly one new row, and an earlier as-of read
    returns the old value;
  * a finding's citation resolves to a stored response whose bytes hash to
    the recorded sha256;
  * no series row reaches ``signals`` or the message bus;
  * live rows are append-only and must carry their source + receipt;
  * page versions: an identical re-fetch stores nothing, changed content adds
    a version and one drift row, a gone page records a drift row with no new
    bytes, and a record's version link never moves;
  * push: one receipt per envelope, a redelivery stores nothing new;
  * the retention runner keeps referenced bytes, prunes the rest with a
    tombstone, and is idempotent.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio
import yaml

from legba.data.archive_store import EvidenceStore
from legba.data.schemas.source import SourceDescriptor
from legba.data.sources.fetch_capture import FetchCapture
from legba.data.sources.portwatch import PortWatchConfig, PortWatchSourceHandler
from legba.runtime.deps import StandardDeps
from legba.runtime.fetch_receipts import InboundEnvelope, VersionWriter, persist_inbound
from legba.runtime.source_actor import SourceCore, SourceDeps

from tests.runtime._ingest_fakes import REPO, NatsRecorder, client_for

SOURCE = "source.imf.portwatch_chokepoints"


@pytest_asyncio.fixture
async def pool(migrated_pg):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    async with p.acquire() as c:
        from legba.runtime.state import SCHEMA

        await c.execute(SCHEMA)   # actor_state / actor_filter_state (not a numbered migration)
        for t in ("fetch_version_tombstones", "fetch_drift_observations"):
            await c.execute(f"DELETE FROM {t}")
        await c.execute("ALTER TABLE fetch_version_records DISABLE TRIGGER fetch_version_records_insert_only")
        await c.execute("DELETE FROM fetch_version_records")
        await c.execute("ALTER TABLE fetch_version_records ENABLE TRIGGER fetch_version_records_insert_only")
        await c.execute("DELETE FROM fetch_versions")
        await c.execute("ALTER TABLE observations DISABLE TRIGGER observations_live_append_only")
        await c.execute("DELETE FROM observations WHERE origin_class = 'live'")
        await c.execute("ALTER TABLE observations ENABLE TRIGGER observations_live_append_only")
        await c.execute("DELETE FROM actor_filter_state WHERE filter_id = $1", f"source:{SOURCE}")
    yield p
    await p.close()


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    r = tmp_path / "archive"
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(r))
    monkeypatch.setenv("LEGBA_FETCH_RECEIPTS", "1")
    return r


def _files(root: Path) -> int:
    return sum(1 for p in root.rglob("*") if p.is_file()) if root.exists() else 0


class _Layer:
    """A PortWatch FeatureServer double: rows by (date, portid) → n_total."""

    def __init__(self, values: dict[tuple[str, str], int]) -> None:
        self.values = dict(values)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        where = request.url.params.get("where", "")
        start = where.split("DATE '")[1][:10]
        rows = sorted((d, p, v) for (d, p), v in self.values.items() if d >= start)
        off = int(request.url.params.get("resultOffset", "0"))
        size = int(request.url.params.get("resultRecordCount", "1000"))
        page = rows[off:off + size]
        feats = [{"attributes": {"date": d, "portid": p, "portname": p, "ObjectId": i, "n_total": v}}
                 for i, (d, p, v) in enumerate(page)]
        body: dict[str, Any] = {"features": feats}
        if off + size < len(rows):
            body["exceededTransferLimit"] = True
        return httpx.Response(200, content=json.dumps(body).encode(),
                              headers={"content-type": "application/json; charset=utf-8"})


def _descriptor() -> SourceDescriptor:
    body = yaml.safe_load((REPO / "descriptors" / "source_imf_portwatch_chokepoints.yaml").read_text())
    body["identity"]["state"] = "active"
    body["identity"]["version"] = "c" * 16
    body["sink"]["config"]["series"] = [s for s in body["sink"]["config"]["series"] if s["field"] == "n_total"]
    return SourceDescriptor.model_validate(body, strict=False)


async def _poll(pool, layer: _Layer, nats: NatsRecorder) -> dict[str, Any]:
    # N-R: the handler's subject_kind must agree with the series registry, as
    # the activation path builds it from the same descriptor.
    handler = PortWatchSourceHandler(PortWatchConfig(fields=["n_total"], page_size=50, subject_kind="entity"),
                                     http_client=client_for(layer))
    core = SourceCore(f"source::{SOURCE}::{'c' * 16}",
                      SourceDeps(descriptor=_descriptor(), deps=StandardDeps(pg_pool=pool, nats_publish=nats),
                                 handler=handler))
    return await core.pull_once()


def _days(n: int) -> list[str]:
    today = datetime.now(tz=timezone.utc).date()
    return [(today - timedelta(days=5 + i)).isoformat() for i in range(n)]


@pytest.mark.asyncio
async def test_series_end_to_end(pool, root) -> None:
    days = _days(40)
    layer = _Layer({(d, p): 40 + (i % 3) + (80 if i < 7 and p == "chokepoint4" else 0)
                    for i, d in enumerate(days) for p in ("chokepoint1", "chokepoint4")})
    nats = NatsRecorder()

    first = await _poll(pool, layer, nats)
    assert first["sink_counts"]["observation"]["written"] == 80          # 40 days x 2 ports, 2 pages
    async with pool.acquire() as c:
        n_obs = await c.fetchval("SELECT count(*) FROM observations WHERE source_id = $1", SOURCE)
        n_ver = await c.fetchval("SELECT count(*) FROM fetch_versions")
        files = _files(root)
        assert n_obs == 80 and n_ver == 2 and files == 2
        assert await c.fetchval("SELECT count(*) FROM signals WHERE source_id = $1", SOURCE) == 0
        outcome = await c.fetchrow("SELECT outcome, signals_written, sink_counts FROM source_poll_outcomes "
                                   "WHERE source_id = $1 ORDER BY occurred_at DESC LIMIT 1", SOURCE)
        assert outcome["outcome"] == "success" and outcome["signals_written"] == 0
        assert json.loads(outcome["sink_counts"])["observation"]["written"] == 80
    assert nats.messages == [], "no series row reaches the message bus"

    # Re-poll, nothing changed upstream: zero rows. (Poll 2 asks for the
    # 14-day trailing window instead of the 45-day backfill — a different
    # request with a different body, so that body is a new version.)
    second = await _poll(pool, layer, nats)
    assert second["sink_counts"]["observation"]["written"] == 0
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM observations WHERE source_id = $1", SOURCE) == 80
        assert await c.fetchval("SELECT count(*) FROM fetch_versions") == 3
    files = _files(root)
    # Poll 3 = the same request, the same body: zero rows AND no new object —
    # the version is re-sighted (fetch_count 2), nothing is stored.
    third = await _poll(pool, layer, nats)
    assert third["sink_counts"]["observation"] == {"written": 0, "unchanged": 20, "revised": 0, "rejected": 0}
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM observations WHERE source_id = $1", SOURCE) == 80
        assert await c.fetchval("SELECT count(*) FROM fetch_versions") == 3
        assert await c.fetchval("SELECT max(fetch_count) FROM fetch_versions") == 2
    assert _files(root) == files

    # A revised day: exactly one new row; an as-of read before it sees the old value.
    between = datetime.now(tz=timezone.utc)
    revised_day = days[8]   # inside the re-read window, a baseline day: a vintage, not a spike
    layer.values[(revised_day, "chokepoint1")] = 999
    fourth = await _poll(pool, layer, nats)
    assert fourth["sink_counts"]["observation"]["written"] == 1
    assert fourth["sink_counts"]["observation"]["revised"] == 1
    asof_sql = ("SELECT DISTINCT ON (valid_from) value FROM observations WHERE source_id = $1 "
                "AND series_id = 'imf_portwatch.chokepoint.n_total' AND subject = 'chokepoint1' "
                "AND valid_from = $2 AND record_time <= $3 ORDER BY valid_from, record_time DESC")
    async with pool.acquire() as c:
        d = date.fromisoformat(revised_day)
        assert await c.fetchval(asof_sql, SOURCE, d, between) != 999
        assert await c.fetchval(asof_sql, SOURCE, d, datetime.now(tz=timezone.utc)) == 999
        # Append-only: a live row cannot be edited or deleted.
        with pytest.raises(asyncpg.exceptions.RestrictViolationError):
            await c.execute("UPDATE observations SET value = 1 WHERE source_id = $1", SOURCE)
        with pytest.raises(asyncpg.exceptions.RestrictViolationError):
            await c.execute("DELETE FROM observations WHERE source_id = $1", SOURCE)
        # A live row without its source/receipt is refused by the pairing CHECK.
        with pytest.raises(asyncpg.exceptions.CheckViolationError, match="observations_origin_class_pairing"):
            await c.execute(
                "INSERT INTO observations (collection_id, series_id, subject_kind, subject, valid_from, "
                "valid_to, record_time, value, unit, source_url, sha256, origin_class) VALUES "
                "('x','s','global','x','2026-01-01','2026-01-01', now(), 1, 'vessels', 'u', 'h', 'live')")

    # The detector: a crossing on chokepoint4, its citation resolves to bytes.
    from legba.data.analysts.deterministic_handlers import series_change

    deps = type("D", (), {"pg_pool": pool})()
    result = await series_change.handle([], {"source_ids": [SOURCE]}, deps)
    data = result.finding.data
    assert data["counts"]["above"] == 1
    crossing = data["crossings"][0]
    assert crossing["subject"] == "chokepoint4" and crossing["status"] == "above"
    cites = data["citations"]
    assert len(cites) == 7 and {c["ref_kind"] for c in cites} == {"observation"}
    assert [str(u) for u in result.derived_from] == [c["ref_id"] for c in cites]
    async with pool.acquire() as c:
        for cite in cites[:3]:
            row = await c.fetchrow("SELECT o.fetch_sha256, v.object_ref FROM observations o "
                                   "JOIN fetch_versions v ON v.sha256 = o.fetch_sha256 WHERE o.id = $1",
                                   __import__("uuid").UUID(cite["ref_id"]))
            assert row["fetch_sha256"] == cite["fetch_sha256"] and row["object_ref"]
            body = EvidenceStore(root).get(cite["fetch_sha256"])
            assert body is not None and hashlib.sha256(body).hexdigest() == cite["fetch_sha256"]
            assert json.loads(body)["features"], "the bytes are the API response the value was parsed from"
        # Every written observation is linked to its response version.
        assert await c.fetchval("SELECT count(*) FROM fetch_version_records WHERE record_kind = 'observation'") == 81


def _page(url: str, html: str, *, role: str = "page") -> FetchCapture:
    body = html.encode()
    return FetchCapture(url=url, method="GET", status=200, content_type="text/html; charset=utf-8",
                        content_encoding=None, role=role, fetched_at=datetime.now(tz=timezone.utc),
                        complete=True, body=body, sha256=hashlib.sha256(body).hexdigest(), size=len(body))


_ARTICLE = ("<html><head><title>t</title></head><body><nav>Home | World</nav><article><h1>Port strike</h1>"
            "<p>Dockworkers in Rotterdam walked out on Tuesday over a pay dispute that has idled cranes "
            "across the terminal for the third time this year, union officials said.</p>"
            "<p>The operator said talks would resume on Thursday and that vessels were being diverted "
            "to Antwerp while the stoppage lasted, adding to congestion along the coast.</p></article>"
            "<footer>(c) {year}</footer></body></html>")


@pytest.mark.asyncio
async def test_page_versions_and_drift(pool, root) -> None:
    w = VersionWriter()
    url = "https://www.news.example.org/2026/11/port-strike?utm_source=x"
    async with pool.acquire() as c:
        v1, ins1 = await w.write(c, _page(url, _ARTICLE.format(year=2025)), source_id="source.s")
        assert ins1
        files = _files(root)
        # Identical re-fetch: same version, nothing stored.
        v1b, ins = await w.write(c, _page(url, _ARTICLE.format(year=2025)), source_id="source.s")
        assert (v1b, ins) == (v1, False) and _files(root) == files
        assert await c.fetchval("SELECT fetch_count FROM fetch_versions WHERE id = $1", v1) == 2
        # Furniture-only change (footer): new bytes = a new version, same text = no drift row.
        v2, ins = await w.write(c, _page(url, _ARTICLE.format(year=2026)), source_id="source.s")
        assert ins and v2 != v1
        assert await c.fetchval("SELECT count(*) FROM fetch_drift_observations") == 0
        # A changed article: a new version and ONE drift row.
        changed = _ARTICLE.format(year=2026).replace("Thursday", "Friday")
        v3, ins = await w.write(c, _page(url, changed), source_id="source.s")
        assert ins
        drift = await c.fetch("SELECT * FROM fetch_drift_observations")
        assert len(drift) == 1 and drift[0]["earlier_version_id"] == v2 and drift[0]["later_version_id"] == v3
        assert drift[0]["earlier_text_sha256"] != drift[0]["later_text_sha256"] and not drift[0]["later_gone"]
        # The address is the platform's canonical URL (legba.data._url_canon).
        from legba.data._url_canon import canonical_url

        assert await c.fetchval("SELECT DISTINCT address FROM fetch_versions") == canonical_url(url)
        assert not canonical_url(url).startswith("https://www.")
        # Gone: one drift row, no new version, no new bytes.
        files = _files(root)
        assert await w.note_gone(c, url=url, status=410, fetched_at=datetime.now(tz=timezone.utc),
                                 source_id=None, observed_by="archiver")
        gone = await c.fetchrow("SELECT * FROM fetch_drift_observations WHERE later_gone")
        assert gone["later_version_id"] is None and gone["later_status"] == 410
        assert await c.fetchval("SELECT count(*) FROM fetch_versions") == 3 and _files(root) == files
        # A signal's version link never moves.
        sid = uuid4()
        await c.execute("INSERT INTO fetch_version_records (record_kind, record_id, role, version_id, sha256) "
                        "SELECT 'signal', $1, 'page', id, sha256 FROM fetch_versions WHERE id = $2", sid, v2)
        with pytest.raises(asyncpg.exceptions.RestrictViolationError):
            await c.execute("UPDATE fetch_version_records SET version_id = $1 WHERE record_id = $2", v3, sid)
        assert await c.fetchval("SELECT version_id FROM fetch_version_records WHERE record_id = $1", sid) == v2


@pytest.mark.asyncio
async def test_push_envelope_receipt(pool, root) -> None:
    body = b'{"id": "evt-1", "level": 7}'
    async with pool.acquire() as c:
        for _ in range(2):
            env = InboundEnvelope(source_id="source.hook", body=body,
                                  headers={"x-webhook-token": "s3cret", "content-type": "application/json"},
                                  received_at=datetime.now(tz=timezone.utc))
            vid = await persist_inbound(c, env)
        row = await c.fetchrow("SELECT * FROM fetch_versions WHERE id = $1", vid)
        assert row["receipt_kind"] == "inbound" and row["address"] is None and row["fetch_count"] == 2
        assert "s3cret" not in row["headers"] and json.loads(row["headers"])["x-webhook-token"].startswith("sha256:")
        assert EvidenceStore(root).get(row["sha256"]) == body


@pytest.mark.asyncio
async def test_retention_keeps_referenced_and_prunes_the_rest(pool, root) -> None:
    from legba.data.fetch_receipt_retention import RetentionPolicy, prune_unreferenced

    w = VersionWriter()
    async with pool.acquire() as c:
        kept, _ = await w.write(c, _page("https://a.example.org/1", "<html>cited feed body</html>", role="response"),
                                source_id="source.s")
        gone, _ = await w.write(c, _page("https://a.example.org/2", "<html>uncited feed body</html>", role="response"),
                                source_id="source.s")
        await c.execute("UPDATE fetch_versions SET first_seen_at = now() - interval '40 days', "
                        "last_seen_at = now() - interval '40 days'")
        sid = uuid4()
        await c.execute("INSERT INTO fetch_version_records (record_kind, record_id, role, version_id, sha256) "
                        "SELECT 'signal', $1, 'response', id, sha256 FROM fetch_versions WHERE id = $2", sid, kept)
        await c.execute(
            "INSERT INTO analyst_outputs (id, kind, title, body, confidence, data, target_id, target_version, "
            "analyst_id, analyst_version, produced_at, derived_from, schema_uri) VALUES "
            "($1, 'finding', 't', 'b', 0.9, $2::jsonb, 't', 'v', 'test_n_l_retention', 'v', now(), '{}', 'u')",
            uuid4(), json.dumps({"data": {"citations": [{"signal_id": str(sid), "marker": "[1]"}]}}))
        policy = RetentionPolicy(unreferenced_after_days=30, batch_size=10, max_batches=5)
        dry = await prune_unreferenced(c, root, policy, apply=False)
        assert (dry.examined, dry.referenced, dry.prunable, dry.pruned) == (2, 1, 1, 0)
        assert _files(root) == 2
        done = await prune_unreferenced(c, root, policy, apply=True)
        assert done.pruned == 1 and _files(root) == 1
        tomb = await c.fetchrow("SELECT * FROM fetch_version_tombstones")
        assert tomb["version_id"] == gone and tomb["policy_version"] == "fetch-receipts-retention/1"
        row = await c.fetchrow("SELECT sha256, bytes_pruned_at FROM fetch_versions WHERE id = $1", gone)
        assert row["sha256"] and row["bytes_pruned_at"] is not None
        again = await prune_unreferenced(c, root, policy, apply=True)
        assert (again.examined, again.pruned) == (1, 0)
        await c.execute("DELETE FROM analyst_outputs WHERE analyst_id = 'test_n_l_retention'")
