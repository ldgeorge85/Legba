# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One byte path on the REAL driver (isolated test stack only).

* migration 0266 is idempotent (applied twice) and admits role ``archived``;
* the poll's link writer persists the item ``locator`` (single and batched);
* the archive register: a dry run counts and writes nothing; ``apply`` writes
  bounded batches — a version per (address, sha256) pointing at the archive's
  EXISTING object (no byte copied: the file is untouched), the archiver's own
  ref-less row reused and given the object, the signal linked ``archived``;
  a second run writes nothing; challenge / failed / orphan rows are never
  registered; a corrupted object is reported by the hash check, never fixed;
* :func:`evidence_states` says exactly one of the three labels;
* :func:`signal_item_text` returns the item as the feed carried it, from the
  stored bytes — for a located link and for a link written before locators;
* a registered object counts as referenced: the receipt pruner holds it and
  the cold tier's age for it is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from legba.data import archive_register as reg
from legba.data import byte_path as bp
from legba.data import fetch_receipt_retention as ret
from legba.data.archive import ARCHIVE_ROOT_ENV, cas_object_ref, cas_path
from legba.data.archive_store import EvidenceStore
from legba.data.config import PostgresConfig
from legba.data.sources.fetch_capture import FetchCapture
from legba.runtime.fetch_receipts import ReceiptRecorder, RecordLink, VersionWriter

pytestmark = pytest.mark.asyncio

SRC = "source.test.b1_bytes"
HOST = "b1.example.org"
NOW = datetime.now(tz=timezone.utc)
_MIGRATION = Path(__file__).resolve().parents[2] / "src" / "legba" / "data" / "migrations" / "0266_one_byte_path.sql"


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    async with p.acquire() as c:
        await _clean(c)
    yield p
    async with p.acquire() as c:
        await _clean(c)
    await p.close()


async def _clean(c) -> None:
    await c.execute("ALTER TABLE fetch_version_records DISABLE TRIGGER fetch_version_records_insert_only")
    await c.execute("DELETE FROM fetch_version_records WHERE version_id IN "
                    "(SELECT id FROM fetch_versions WHERE url LIKE $1)", f"%{HOST}%")
    await c.execute("ALTER TABLE fetch_version_records ENABLE TRIGGER fetch_version_records_insert_only")
    await c.execute("DELETE FROM fetch_version_tombstones WHERE version_id IN "
                    "(SELECT id FROM fetch_versions WHERE url LIKE $1)", f"%{HOST}%")
    await c.execute("DELETE FROM fetch_versions WHERE url LIKE $1", f"%{HOST}%")
    # The register scans the whole archive: every row another file left goes.
    await c.execute("DELETE FROM evidence_archive")
    await c.execute("DELETE FROM signals WHERE source_id = $1", SRC)


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv(ARCHIVE_ROOT_ENV, str(tmp_path))
    return tmp_path


def _put(root: Path, body: bytes) -> str:
    sha = hashlib.sha256(body).hexdigest()
    path = cas_path(root, sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return sha


async def _signal(c, *, payload: dict | None = None, prov: dict | None = None, url: str | None = None) -> uuid.UUID:
    sid = uuid.uuid4()
    await c.execute(
        "INSERT INTO signals (id, source_id, canonical_url, payload, raw_provenance, content_hash)"
        " VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6)",
        sid, SRC, url or f"https://{HOST}/a/{sid}", json.dumps(payload or {}), json.dumps(prov or {}),
        uuid.uuid4().hex,
    )
    return sid


async def _archive(c, root: Path, sid, body: bytes | None, *, status: str = "archived",
                   url: str | None = None, age_days: int = 3) -> str | None:
    sha = _put(root, body) if body is not None else None
    await c.execute(
        "INSERT INTO evidence_archive (signal_id, status, object_ref, sha256, size_bytes, content_type,"
        " fetched_url, archived_at, created_at, updated_at)"
        " VALUES ($1, $2, $3, $4, $5, 'text/html', $6, $7, $7, $7)",
        sid, status, cas_object_ref(sha) if sha else None, sha, len(body) if body else None,
        url or f"https://{HOST}/a/{sid}", NOW - timedelta(days=age_days),
    )
    return sha


async def test_migration_0266_is_idempotent_and_admits_archived(pool) -> None:
    sql = _MIGRATION.read_text()
    assert "BEGIN;" not in sql.upper() and "COMMIT;" not in sql.upper()
    async with pool.acquire() as c:
        await c.execute(sql)
        await c.execute(sql)
        cols = {r["column_name"]: r["data_type"] for r in await c.fetch(
            "SELECT table_name || '.' || column_name AS column_name, data_type FROM information_schema.columns"
            " WHERE (table_name, column_name) IN (('fetch_version_records', 'locator'),"
            " ('fetch_version_records', 'register_batch'), ('fetch_versions', 'registered'))")}
        defs = [r["d"] for r in await c.fetch(
            "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint"
            " WHERE conrelid = 'public.fetch_version_records'::regclass AND contype = 'c'"
            " AND pg_get_constraintdef(oid) LIKE '%role%'")]
    assert cols == {"fetch_version_records.locator": "jsonb", "fetch_version_records.register_batch": "text",
                    "fetch_versions.registered": "jsonb"}
    assert len(defs) == 1 and "'archived'" in defs[0] and "'inbound'" in defs[0]


async def test_the_link_writer_persists_the_locator(pool, root) -> None:
    rec = ReceiptRecorder(source_id=SRC, writer=VersionWriter(root=root), enabled=True)
    body = b"<rss><channel><item><guid>g-1</guid><title>Port shut</title></item></channel></rss>"
    rec.log.add(FetchCapture(url=f"https://{HOST}/feed", method="GET", status=200, content_type="application/rss+xml",
                             content_encoding=None, role="response", fetched_at=NOW, complete=True, body=body,
                             sha256=hashlib.sha256(body).hexdigest(), size=len(body)))
    async with pool.acquire() as c:
        a, b = await _signal(c), await _signal(c)
        await rec.link(c, "signal", a, RecordLink(0, locator={"item_id": "g-1"}))
        await rec.link_many(c, "signal", [(b, RecordLink(0, locator={"item_id": "g-1"}))])
        rows = await c.fetch("SELECT record_id, locator FROM fetch_version_records WHERE record_id = ANY($1::uuid[])",
                             [a, b])
        assert {r["record_id"]: json.loads(r["locator"]) for r in rows} == {a: {"item_id": "g-1"}, b: {"item_id": "g-1"}}
        state, item = await bp.signal_item_text(c, a, root=root)
    assert state.label == bp.RECEIVED_BYTES and state.locator == {"item_id": "g-1"}
    assert item is not None and item.basis == "feed_item" and item.text == "g-1 Port shut"


async def test_a_link_written_before_locators_derives_its_item_from_the_signal(pool, root) -> None:
    body = b'{"items": [{"id": "x-1", "title": "First"}, {"id": "x-2", "title": "Grain exports halted"}]}'
    writer = VersionWriter(root=root)
    cap = FetchCapture(url=f"https://{HOST}/api", method="GET", status=200, content_type="application/json",
                       content_encoding=None, role="response", fetched_at=NOW, complete=True, body=body,
                       sha256=hashlib.sha256(body).hexdigest(), size=len(body))
    async with pool.acquire() as c:
        vid, _ = await writer.write(c, cap, source_id=SRC)
        sid = await _signal(c, payload={"external_id": "x-2"})
        await c.execute("INSERT INTO fetch_version_records (record_kind, record_id, role, version_id, sha256)"
                        " VALUES ('signal', $1, 'response', $2, $3)", sid, vid, cap.sha256)
        state, item = await bp.signal_item_text(c, sid, root=root)
    assert state.label == bp.RECEIVED_BYTES and state.locator is None
    assert item is not None and "Grain exports halted" in item.text and "First" not in item.text


async def test_register_dry_run_apply_idempotent_and_honest(pool, root) -> None:
    async with pool.acquire() as c:
        a = await _signal(c)
        sha_a = await _archive(c, root, a, b"<html>page A</html>")
        b = await _signal(c)                       # same bytes, same address as A? no: own URL
        await _archive(c, root, b, b"<html>page B</html>")
        shared_url = f"https://{HOST}/shared"
        d1, d2 = await _signal(c), await _signal(c)
        sha_d = await _archive(c, root, d1, b"<html>shared</html>", url=shared_url)
        await _archive(c, root, d2, b"<html>shared</html>", url=shared_url, age_days=1)
        # The archiver's own N-L version of A's page: same address + hash, no object_ref.
        await c.execute(
            "INSERT INTO fetch_versions (address, url, sha256, hash_basis, role, status, size_bytes,"
            " store_status, first_seen_at, last_seen_at, extracted_text)"
            " VALUES ($1, $1, $2, 'entity', 'archiver', 200, 19, 'not_stored', $3, $3, 'page A')",
            f"https://{HOST}/a/{a}", sha_a, NOW)
        challenge = await _signal(c)
        await _archive(c, root, challenge, b"<html>Just a moment...</html>", status="blocked_challenge")
        failed = await _signal(c)
        await _archive(c, root, failed, None, status="failed")
        orphan = uuid.uuid4()                      # an archive row whose signal is gone
        await _archive(c, root, orphan, b"<html>orphan</html>")
        before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}

        dry = await reg.register(c, apply=False, batch_size=2, store=EvidenceStore(root), verify_per_batch=5)
        assert await c.fetchval("SELECT count(*) FROM fetch_version_records WHERE role = 'archived'") == 0
        assert (dry.signals_to_link, dry.no_signal, dry.objects) == (4, 1, 3)
        assert (dry.versions_new, dry.versions_ref_filled, dry.versions_existing) == (2, 1, 0)
        assert dry.verify_mismatch == 0 and dry.verified >= 3 and dry.complete

        run = await reg.register(c, apply=True, batch_size=10, max_batches=50, store=EvidenceStore(root))
        assert run.links_written == 4 and (run.versions_new, run.versions_ref_filled) == (2, 1)
        again = await reg.register(c, apply=True, batch_size=2, max_batches=50)
        assert again.links_written == 0 and again.already_linked == 4 and again.versions_new == 0

        rows = await c.fetch(
            "SELECT r.record_id, r.register_batch, v.role, v.object_ref, v.store_status, v.registered,"
            " v.first_seen_at, v.last_seen_at, v.extracted_text, v.sha256"
            " FROM fetch_version_records r JOIN fetch_versions v ON v.id = r.version_id WHERE r.role = 'archived'")
        by = {r["record_id"]: r for r in rows}
        assert set(by) == {a, b, d1, d2}
        assert by[d1]["sha256"] == by[d2]["sha256"] == sha_d   # one version, two signals
        assert by[d1]["first_seen_at"] < by[d1]["last_seen_at"]
        assert all(r["role"] == "archiver" and r["object_ref"] == cas_object_ref(r["sha256"]) for r in rows)
        assert all(r["register_batch"] == run.batch for r in rows)
        assert json.loads(by[a]["registered"])["action"] == "object_ref_filled"
        assert by[a]["extracted_text"] == "page A" and by[a]["store_status"] == "existed"
        reg_b = json.loads(by[b]["registered"])
        assert reg_b["from"] == "evidence_archive" and reg_b["action"] == "inserted" and reg_b["status_inferred"]

        states = await bp.evidence_states(c, [a, challenge, failed])
        assert states[str(a)].label == bp.ARCHIVED_LATER and states[str(a)].link_role == "archived"
        assert states[str(challenge)].label == bp.NO_BYTES_KEPT
        assert states[str(failed)].label == bp.NO_BYTES_KEPT
        _, page = await bp.signal_item_text(c, a, root=root)
        assert page is not None and page.basis == "page_text" and page.text == "page A"
        archive_rows = await c.fetchval("SELECT count(*) FROM evidence_archive")
    # Not a byte written, moved or deleted; the archive table untouched.
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert archive_rows == 7


async def test_register_resumes_and_reports_a_corrupt_object(pool, root) -> None:
    async with pool.acquire() as c:
        ids = sorted([await _signal(c) for _ in range(3)])
        shas = [await _archive(c, root, s, f"<html>{s}</html>".encode()) for s in ids]
        cas_path(root, shas[1]).write_bytes(b"tampered")
        first = await reg.register(c, apply=True, batch_size=1, max_batches=1, store=EvidenceStore(root),
                                   verify_per_batch=1)
        assert first.links_written == 1 and not first.complete and first.last_signal_id == str(ids[0])
        rest = await reg.register(c, apply=True, batch_size=1, max_batches=10, after=first.last_signal_id,
                                  store=EvidenceStore(root), verify_per_batch=1)
        assert rest.links_written == 2 and rest.complete
        assert rest.verify_mismatch == 1 and rest.mismatch_sample == [shas[1]]
    assert cas_path(root, shas[1]).read_bytes() == b"tampered"   # reported, never repaired


async def test_a_registered_object_counts_as_referenced(pool, root) -> None:
    async with pool.acquire() as c:
        sid = await _signal(c)
        await _archive(c, root, sid, b"<html>old page</html>", age_days=200)
        await reg.register(c, apply=True, batch_size=10, max_batches=5)
        vid = await c.fetchval("SELECT version_id FROM fetch_version_records WHERE record_id = $1", sid)
        held = await c.fetch(ret.REFERENCED_SQL, [vid], 30)
        assert [r["id"] for r in held] == [vid], "the receipt pruner never prunes a registered object"
        m = await reg.measure_states(c)
    assert m.signals_total == m.received_bytes + m.archived_later_after_register + m.no_bytes_kept_after_register
