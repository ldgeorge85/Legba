# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the ``archive_retention`` sub-handler — the evidence archive's
expiry sweep for UNCITED rows, shipped behind ``LEGBA_ARCHIVE_RETENTION`` (OFF).

What is pinned, against a real Postgres and real files on a tmp archive root:

  * the SELECTION — a row cited by ANY of analyst_outputs / facts / events is never
    expired; an uncited row is; the age boundary is strict and a row with no
    ``archived_at`` ages from ``created_at``; rows that hold no bytes
    (``failed`` / ``skipped_*``) are not candidates;
  * FLAG OFF is a no-op — no database read, no file touched;
  * the ACTION — the object is removed, the row KEPT as ``expired_uncited`` with
    its ``object_ref`` / ``sha256`` receipt, ``signals.object_ref`` cleared,
    ``retention_class`` left alone;
  * a content address SHARED with a live row is kept, and removed once the last
    live user expires;
  * the safety order — dry run writes nothing, a row cited between scan and lock
    is skipped, an unusable root expires nothing, a foreign/hostile ref never
    becomes a path, an unlink failure leaves the row expired and marked.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
)
from legba.data.analysts.deterministic_handlers import archive_retention as ar
from legba.data.option_catalog.handler_options import known_option_names
from legba.data.archive import ARCHIVE_ROOT_ENV, cas_object_ref, cas_path
from legba.data.config import PostgresConfig
from legba.data.provenance.kinds import TRACE_ONLY

SUB = "archive_retention"
SRC = "test_w1_sw12_src"
NOW = datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Registration (no DB)
# ---------------------------------------------------------------------------


def test_registered_trace_only_with_declared_options():
    assert SUB in SUB_HANDLERS
    assert OUTPUT_KIND_BY_SUB_HANDLER[SUB] is TRACE_ONLY
    assert {"retention_days", "batch_limit", "dry_run"} <= set(known_option_names(SUB))


def test_descriptor_ships_draft_and_validates():
    import yaml

    from legba.data.schemas.analyst import AnalystDescriptor

    path = Path(__file__).resolve().parents[2] / "descriptors" / "analyst_archive_retention.yaml"
    body = yaml.safe_load(path.read_text())
    assert body["identity"]["state"] == "draft"
    assert body["method"]["sub_handler"] == SUB
    assert "options" not in body["method"]  # shipped defaults only
    AnalystDescriptor.model_validate(body)


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv(ar.ENABLED_ENV, raising=False)
    assert ar.retention_enabled() is False
    for off in ("", "0", "false", "no", "off", "nope"):
        monkeypatch.setenv(ar.ENABLED_ENV, off)
        assert ar.retention_enabled() is False
    for on in ("1", "true", "YES", " on "):
        monkeypatch.setenv(ar.ENABLED_ENV, on)
        assert ar.retention_enabled() is True


def test_the_cited_predicate_reads_every_citing_table():
    for table in ar.CITING_TABLES:
        assert f"unnest(derived_from) AS sid FROM {table}" in ar.SELECT_ELIGIBLE_SQL
        assert f"FROM {table} " in ar.LOCK_ELIGIBLE_SQL
    assert set(ar.CITING_TABLES) == {"analyst_outputs", "facts", "events"}


class _ExplodingPool:
    """Any touch of the database is a test failure."""

    def acquire(self) -> Any:
        raise AssertionError("flag off must not touch the database")


class _Deps:
    def __init__(self, pool: Any) -> None:
        self.pg_pool = pool
        self.extras: dict[str, Any] = {}


async def _handle(pool: Any, **opts: Any) -> dict[str, Any]:
    result = await ar.handle([], {"sub_handler": SUB, **opts}, _Deps(pool))
    return result.finding.data


@pytest.mark.asyncio
async def test_flag_off_is_a_noop_and_never_touches_the_pool(monkeypatch):
    monkeypatch.delenv(ar.ENABLED_ENV, raising=False)
    data = await _handle(_ExplodingPool())
    assert data["flag_off"] == 1
    assert data["expired"] == 0 and data["eligible"] == 0
    assert data["retention_days"] == ar.DEFAULT_RETENTION_DAYS == 45


@pytest.mark.asyncio
async def test_synthetic_no_deps_is_a_zeroed_run(monkeypatch):
    monkeypatch.setenv(ar.ENABLED_ENV, "1")
    result = await ar.handle([], {}, None)
    assert result.finding.data["expired"] == 0
    assert result.usage["prompt_tokens"] == 0


# ---------------------------------------------------------------------------
# Fixtures (real Postgres + a tmp archive root)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def clean_slate(pg_pool):
    """The sweep is a GLOBAL scan of a session-shared DB, so every archive row any
    other file left behind is retired first (same idiom as the archiver's tests)."""
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM evidence_archive")
        await conn.execute("DELETE FROM signals WHERE source_id = $1", SRC)
        await conn.execute("DELETE FROM analyst_outputs WHERE analyst_id LIKE 'test_w1_sw12%'")
        await conn.execute("DELETE FROM facts WHERE analyst_id LIKE 'test_w1_sw12%'")
        await conn.execute("DELETE FROM events WHERE analyst_id LIKE 'test_w1_sw12%'")
    yield


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv(ARCHIVE_ROOT_ENV, str(tmp_path))
    monkeypatch.setenv(ar.ENABLED_ENV, "1")
    return tmp_path


def _put(root: Path, payload: bytes) -> tuple[str, str, Path]:
    """Write one CAS object; return (sha, object_ref, path)."""
    sha = hashlib.sha256(payload).hexdigest()
    path = cas_path(root, sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return sha, cas_object_ref(sha), path


async def _signal(conn, object_ref: str | None) -> Any:
    sid = uuid4()
    await conn.execute(
        "INSERT INTO signals (id, source_id, canonical_url, payload, raw_provenance,"
        " content_hash, object_ref, retention_class)"
        " VALUES ($1, $2, 'http://example.test/a', '{}'::jsonb, '{}'::jsonb, $3, $4,"
        "         'evidence_hold')",
        sid, SRC, uuid4().hex, object_ref,
    )
    return sid


async def _archive_row(
    conn, root: Path, *, age: timedelta, payload: bytes | None = None,
    status: str = "archived", ref: str | None = None, media_payload: bytes | None = None,
    archived_at_null: bool = False,
) -> dict[str, Any]:
    """A signal + its evidence_archive row aged ``age`` + the file on disk."""
    path = None
    sha = None
    if ref is None and payload is not None:
        sha, ref, path = _put(root, payload)
    elif ref is not None and ref.startswith("cas:sha256/"):
        sha = ref[len("cas:sha256/"):]
        path = cas_path(root, sha)
    media_ref = media_path = None
    if media_payload is not None:
        _, media_ref, media_path = _put(root, media_payload)
    sid = await _signal(conn, ref)
    when = NOW - age
    await conn.execute(
        "INSERT INTO evidence_archive (signal_id, status, object_ref, sha256, size_bytes,"
        " media_object_ref, media_size_bytes, fetched_url, archived_at, created_at)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, 'http://example.test/a', $8, $9)",
        sid, status, ref, sha, len(payload) if payload else None, media_ref,
        len(media_payload) if media_payload else None,
        None if archived_at_null else when, when,
    )
    return {"sid": sid, "ref": ref, "path": path, "media_path": media_path, "sha": sha}


async def _cite(conn, kind: str, sid: Any) -> None:
    if kind == "analyst_outputs":
        await conn.execute(
            "INSERT INTO analyst_outputs (id, kind, title, body, confidence, data,"
            " analyst_id, derived_from, schema_uri) VALUES ($1, 'finding', 't', '', 0.9,"
            " '{}'::jsonb, 'test_w1_sw12_unit', $2::uuid[], 'legba/finding/1.0.0')",
            uuid4(), [str(sid)],
        )
    elif kind == "facts":
        await conn.execute(
            "INSERT INTO facts (id, subject, predicate, value, analyst_id, derived_from)"
            " VALUES ($1, $3, 'p', 'v', 'test_w1_sw12_unit', $2::uuid[])",
            uuid4(), [str(sid)], f"sw12-subject-{uuid4().hex[:12]}",
        )
    else:
        await conn.execute(
            "INSERT INTO events (event_signature, analyst_id, title, derived_from)"
            " VALUES ($1, 'test_w1_sw12_unit', 't', $2::uuid[])",
            f"evt:sw12-{uuid4().hex[:8]}#evt:ir", [str(sid)],
        )


async def _status(conn, sid: Any) -> Any:
    return await conn.fetchrow("SELECT * FROM evidence_archive WHERE signal_id = $1", sid)


DAY = timedelta(days=1)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("citing_table", ["analyst_outputs", "facts", "events"])
async def test_a_cited_row_is_never_expired_an_uncited_one_is(
    pg_pool, clean_slate, root, citing_table,
):
    async with pg_pool.acquire() as conn:
        cited = await _archive_row(conn, root, age=60 * DAY, payload=b"cited bytes")
        uncited = await _archive_row(conn, root, age=60 * DAY, payload=b"uncited bytes")
        await _cite(conn, citing_table, cited["sid"])

    data = await _handle(pg_pool)

    async with pg_pool.acquire() as conn:
        assert (await _status(conn, cited["sid"]))["status"] == "archived"
        assert (await _status(conn, uncited["sid"]))["status"] == "expired_uncited"
    assert cited["path"].exists()
    assert not uncited["path"].exists()
    assert data["eligible"] == 1 and data["expired"] == 1
    assert data["objects_removed"] == 1
    assert data["bytes_removed"] == len(b"uncited bytes")


@pytest.mark.asyncio
async def test_age_boundary_is_strict_and_falls_back_to_created_at(
    pg_pool, clean_slate, root,
):
    async with pg_pool.acquire() as conn:
        inside = await _archive_row(conn, root, age=44 * DAY + timedelta(hours=23), payload=b"in")
        outside = await _archive_row(conn, root, age=45 * DAY + timedelta(hours=1), payload=b"out")
        no_archived_at = await _archive_row(
            conn, root, age=50 * DAY, payload=b"old-created", archived_at_null=True,
            status="blocked_challenge",
        )
        young_created = await _archive_row(
            conn, root, age=3 * DAY, payload=b"young", archived_at_null=True,
        )

    await _handle(pg_pool)

    async with pg_pool.acquire() as conn:
        assert (await _status(conn, inside["sid"]))["status"] == "archived"
        assert (await _status(conn, outside["sid"]))["status"] == "expired_uncited"
        assert (await _status(conn, no_archived_at["sid"]))["status"] == "expired_uncited"
        assert (await _status(conn, young_created["sid"]))["status"] == "archived"
    assert inside["path"].exists() and young_created["path"].exists()
    assert not outside["path"].exists() and not no_archived_at["path"].exists()


@pytest.mark.asyncio
async def test_retention_days_option_moves_the_boundary(pg_pool, clean_slate, root):
    async with pg_pool.acquire() as conn:
        row = await _archive_row(conn, root, age=12 * DAY, payload=b"twelve days")

    assert (await _handle(pg_pool))["expired"] == 0
    data = await _handle(pg_pool, retention_days=10)
    assert data["retention_days"] == 10 and data["expired"] == 1
    assert not row["path"].exists()


@pytest.mark.asyncio
async def test_rows_that_hold_no_bytes_are_not_candidates(pg_pool, clean_slate, root):
    async with pg_pool.acquire() as conn:
        failed = await _archive_row(conn, root, age=90 * DAY, status="failed")
        skipped = await _archive_row(conn, root, age=90 * DAY, status="skipped_license")

    data = await _handle(pg_pool)

    assert data["eligible"] == 0
    async with pg_pool.acquire() as conn:
        assert (await _status(conn, failed["sid"]))["status"] == "failed"
        assert (await _status(conn, skipped["sid"]))["status"] == "skipped_license"


# ---------------------------------------------------------------------------
# The action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expiry_keeps_the_row_and_its_receipt_and_clears_the_signal_ref(
    pg_pool, clean_slate, root,
):
    async with pg_pool.acquire() as conn:
        row = await _archive_row(
            conn, root, age=60 * DAY, payload=b"article", media_payload=b"mediabytes",
        )

    data = await _handle(pg_pool)

    assert data["expired"] == 1 and data["objects_removed"] == 2
    assert data["bytes_removed"] == len(b"article") + len(b"mediabytes")
    assert not row["path"].exists() and not row["media_path"].exists()
    async with pg_pool.acquire() as conn:
        ea = await _status(conn, row["sid"])
        sig = await conn.fetchrow(
            "SELECT object_ref, retention_class FROM signals WHERE id = $1", row["sid"]
        )
    assert ea["status"] == "expired_uncited"
    assert ea["object_ref"] == row["ref"] and ea["sha256"] == row["sha"]  # the receipt
    assert ea["fetched_url"] == "http://example.test/a"
    assert sig["object_ref"] is None  # the read surfaces must not claim gone bytes
    assert sig["retention_class"] == "evidence_hold"  # the hold is not ours to lift
    # idempotent: a second run finds nothing
    again = await _handle(pg_pool)
    assert again["eligible"] == 0 and again["expired"] == 0


@pytest.mark.asyncio
async def test_a_shared_object_stays_while_a_live_row_uses_it(pg_pool, clean_slate, root):
    async with pg_pool.acquire() as conn:
        live = await _archive_row(conn, root, age=2 * DAY, payload=b"shared bytes")  # young, kept
        old = await _archive_row(conn, root, age=60 * DAY, ref=live["ref"])

    data = await _handle(pg_pool)

    assert data["expired"] == 1
    assert data["objects_shared_kept"] == 1 and data["objects_removed"] == 0
    assert live["path"].exists()
    async with pg_pool.acquire() as conn:
        assert (await _status(conn, old["sid"]))["status"] == "expired_uncited"
        assert (await _status(conn, live["sid"]))["status"] == "archived"


@pytest.mark.asyncio
async def test_a_shared_object_goes_when_the_last_live_user_expires(
    pg_pool, clean_slate, root,
):
    async with pg_pool.acquire() as conn:
        first = await _archive_row(conn, root, age=70 * DAY, payload=b"twin bytes")
        second = await _archive_row(conn, root, age=60 * DAY, ref=first["ref"])

    data = await _handle(pg_pool)

    assert data["expired"] == 2
    assert data["objects_shared_kept"] == 1 and data["objects_removed"] == 1
    assert not first["path"].exists()


@pytest.mark.asyncio
async def test_an_already_absent_file_is_the_desired_end_state(pg_pool, clean_slate, root):
    async with pg_pool.acquire() as conn:
        row = await _archive_row(conn, root, age=60 * DAY, payload=b"vanished")
    row["path"].unlink()

    data = await _handle(pg_pool)

    assert data["expired"] == 1 and data["objects_missing"] == 1
    assert data["unlink_failed"] == 0


# ---------------------------------------------------------------------------
# Safety order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_with_a_real_pool_leaves_everything_alone(
    pg_pool, clean_slate, root, monkeypatch,
):
    async with pg_pool.acquire() as conn:
        row = await _archive_row(conn, root, age=400 * DAY, payload=b"ancient")
    monkeypatch.delenv(ar.ENABLED_ENV)

    data = await _handle(pg_pool)

    assert data["flag_off"] == 1 and data["expired"] == 0
    assert row["path"].exists()
    async with pg_pool.acquire() as conn:
        assert (await _status(conn, row["sid"]))["status"] == "archived"


@pytest.mark.asyncio
async def test_dry_run_counts_and_writes_nothing(pg_pool, clean_slate, root):
    async with pg_pool.acquire() as conn:
        row = await _archive_row(conn, root, age=60 * DAY, payload=b"dryrun!")

    data = await _handle(pg_pool, dry_run=True)

    assert data["dry_run"] == 1 and data["eligible"] == 1 and data["expired"] == 0
    assert data["bytes_removed"] == len(b"dryrun!")
    assert row["path"].exists()
    async with pg_pool.acquire() as conn:
        assert (await _status(conn, row["sid"]))["status"] == "archived"


@pytest.mark.asyncio
async def test_a_row_cited_between_scan_and_lock_is_skipped(pg_pool, clean_slate, root):
    async with pg_pool.acquire() as conn:
        row = await _archive_row(conn, root, age=60 * DAY, payload=b"late cite")
        await _cite(conn, "analyst_outputs", row["sid"])
    counters = ar._zero_counters(retention_days=45, batch_limit=200, dry_run=False)
    scanned = {
        "signal_id": row["sid"], "object_ref": row["ref"],
        "media_object_ref": None, "size_bytes": 9, "media_size_bytes": None,
    }

    await ar._expire_one(pg_pool, root, scanned, days=45, counters=counters)

    assert counters["skipped_changed"] == 1 and counters["expired"] == 0
    assert row["path"].exists()
    async with pg_pool.acquire() as conn:
        assert (await _status(conn, row["sid"]))["status"] == "archived"


@pytest.mark.asyncio
async def test_an_unusable_root_expires_nothing(pg_pool, clean_slate, root, monkeypatch):
    async with pg_pool.acquire() as conn:
        row = await _archive_row(conn, root, age=60 * DAY, payload=b"unmounted")
    monkeypatch.setenv(ARCHIVE_ROOT_ENV, str(root / "not-mounted"))

    data = await _handle(pg_pool)

    assert data["skipped_no_root"] == 1 and data["expired"] == 0
    async with pg_pool.acquire() as conn:
        assert (await _status(conn, row["sid"]))["status"] == "archived"


@pytest.mark.asyncio
async def test_a_hostile_or_foreign_ref_never_becomes_a_path(pg_pool, clean_slate, root):
    outside = root.parent / "outside_the_archive.txt"
    outside.write_text("keep me")
    async with pg_pool.acquire() as conn:
        traversal = await _archive_row(
            conn, root, age=60 * DAY, ref="cas:sha256/../../outside_the_archive.txt",
        )
        foreign = await _archive_row(conn, root, age=60 * DAY, ref="s3://bucket/key")

    data = await _handle(pg_pool)

    assert data["expired"] == 2 and data["objects_removed"] == 0
    assert outside.read_text() == "keep me"
    async with pg_pool.acquire() as conn:
        assert (await _status(conn, traversal["sid"]))["status"] == "expired_uncited"
        assert (await _status(conn, foreign["sid"]))["status"] == "expired_uncited"


@pytest.mark.asyncio
async def test_an_unlink_failure_leaves_the_row_expired_and_marked(
    pg_pool, clean_slate, root,
):
    async with pg_pool.acquire() as conn:
        row = await _archive_row(conn, root, age=60 * DAY, payload=b"stuck")
    row["path"].unlink()
    row["path"].mkdir()  # unlink() on a directory raises: an orphan, not a lie

    data = await _handle(pg_pool)

    assert data["expired"] == 1 and data["unlink_failed"] == 1
    assert data["objects_removed"] == 0
    async with pg_pool.acquire() as conn:
        ea = await _status(conn, row["sid"])
    assert ea["status"] == "expired_uncited"
    assert "unlink_failed" in ea["last_error"]


@pytest.mark.asyncio
async def test_batch_limit_bounds_a_run_oldest_first(pg_pool, clean_slate, root):
    async with pg_pool.acquire() as conn:
        oldest = await _archive_row(conn, root, age=90 * DAY, payload=b"oldest")
        middle = await _archive_row(conn, root, age=80 * DAY, payload=b"middle")
        newest = await _archive_row(conn, root, age=70 * DAY, payload=b"newest")

    data = await _handle(pg_pool, batch_limit=2)

    assert data["eligible"] == 2 and data["batch_full"] == 1 and data["expired"] == 2
    assert not oldest["path"].exists() and not middle["path"].exists()
    assert newest["path"].exists()


@pytest.mark.asyncio
async def test_the_status_check_still_refuses_an_unknown_status(pg_pool, clean_slate):
    async with pg_pool.acquire() as conn:
        sid = await _signal(conn, None)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO evidence_archive (signal_id, status) VALUES ($1, 'bogus')", sid
            )
        await conn.execute(
            "INSERT INTO evidence_archive (signal_id, status) VALUES ($1, 'expired_uncited')",
            sid,
        )
    assert json.dumps(ar.BYTE_HOLDING_STATUSES)  # the statuses the sweep may act on
    assert "expired_uncited" not in ar.BYTE_HOLDING_STATUSES


# ---------------------------------------------------------------------------
# The other keepers of a content address (fetch receipts, observations)
# ---------------------------------------------------------------------------


async def _fetch_version(conn, sha: str, *, pruned: bool = False, stored: bool = True) -> int:
    return await conn.fetchval(
        "INSERT INTO fetch_versions (address, url, sha256, hash_basis, role, first_source_id,"
        " last_source_id, status, size_bytes, object_ref, store_status, first_seen_at,"
        " last_seen_at, bytes_pruned_at) VALUES ($1, $1, $2, 'wire', 'response', $3, $3, 200,"
        " 10, $4, $5, now() - interval '70 days', now() - interval '60 days', $6) RETURNING id",
        f"http://example.test/c1/{uuid4().hex}", sha, SRC,
        cas_object_ref(sha) if stored else None, "existed" if stored else "not_stored",
        NOW if pruned else None,
    )


@pytest_asyncio.fixture
async def clean_fetch_versions(pg_pool):
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM fetch_version_records WHERE version_id IN"
                           " (SELECT id FROM fetch_versions WHERE first_source_id = $1)", SRC)
        await conn.execute("DELETE FROM fetch_versions WHERE first_source_id = $1", SRC)
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM fetch_versions WHERE first_source_id = $1", SRC)


@pytest.mark.asyncio
async def test_bytes_a_fetch_receipt_still_holds_are_never_unlinked(
    pg_pool, clean_slate, clean_fetch_versions, root,
):
    """The known gap: an uncited archive row and a fetch version share
    one content address; expiring the row must not take the receipt's bytes."""
    async with pg_pool.acquire() as conn:
        row = await _archive_row(conn, root, age=60 * DAY, payload=b"page bytes also a receipt")
        await _fetch_version(conn, row["sha"])

    data = await _handle(pg_pool)

    assert data["expired"] == 1
    assert data["objects_shared_kept"] == 1 and data["objects_removed"] == 0
    assert row["path"].exists()
    async with pg_pool.acquire() as conn:
        assert (await _status(conn, row["sid"]))["status"] == "expired_uncited"


@pytest.mark.asyncio
async def test_a_pruned_or_bytes_less_receipt_does_not_keep_the_file(
    pg_pool, clean_slate, clean_fetch_versions, root,
):
    async with pg_pool.acquire() as conn:
        pruned = await _archive_row(conn, root, age=60 * DAY, payload=b"receipt already pruned")
        await _fetch_version(conn, pruned["sha"], pruned=True)
        hashonly = await _archive_row(conn, root, age=60 * DAY, payload=b"receipt kept no bytes")
        await _fetch_version(conn, hashonly["sha"], stored=False)

    data = await _handle(pg_pool)

    assert data["expired"] == 2 and data["objects_removed"] == 2
    assert not pruned["path"].exists() and not hashonly["path"].exists()


@pytest.mark.asyncio
async def test_the_live_sharer_statement_carries_every_present_sharer(pg_pool):
    async with pg_pool.acquire() as conn:
        sql = await ar.referenced_elsewhere_sql(conn)
        assert "fetch_versions" in sql and "observations" in sql
        # It parses and answers on the migrated schema (no row: not referenced).
        assert await conn.fetchval(sql, "cas:sha256/" + "e" * 64, "e" * 64) is False
        assert await conn.fetchval(sql, "s3://foreign/key", None) is False


@pytest.mark.asyncio
async def test_sharer_checks_are_skipped_when_their_table_is_absent():
    class _Conn:
        async def fetchval(self, sql, *args):
            return False  # pre-0245 schema: no fetch_versions, no observations columns

    sql = await ar.referenced_elsewhere_sql(_Conn())
    assert sql.strip() == ar.REFERENCED_ELSEWHERE_SQL.strip()
