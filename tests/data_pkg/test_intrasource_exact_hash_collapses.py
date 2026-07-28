# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S-4 — intra-source exact-hash duplicate collapse at ingest.

A source that re-lists an unchanged item every poll (hazard feeds re-serve
active events; NWS/NASA/USGS re-publish) used to spawn a fresh ``signals`` row
per poll — 41% of a 7-day window's rows were exact content-hash duplicates, ALL
intra-source (the "M 5.5 Chupaca, Peru" quake was stored 194x). Because
``content_hash`` is a deterministic hash of content (never the fetch time), an
exact (source_id, content_hash) match is byte-identical content — safe to
collapse. :func:`legba.runtime.source_actor.write_canonical_signal` now, before
inserting, bumps the freshest existing same-(source_id, content_hash) row's
``fetched_at`` (recency preserved) and SKIPS the insert.

Two layers, mirroring ``test_filter_ingest_dedupe.py``:

  * **Unit** — a process-local fake asyncpg connection backing an in-memory
    signals table drives ``write_canonical_signal`` with no substrate. Asserts:
    identical (source, hash) collapses to ONE row with ``fetched_at`` advanced;
    a different content_hash inserts a second row; a different SOURCE sharing the
    hash inserts a second row (intra-source ONLY); an empty content_hash is never
    a dedup key; the flag off disables the collapse; ``fetched_at`` never moves
    backward (GREATEST); and the ``dedup_stats`` counter tracks collapses.

  * **Live pivot-DB** (env-gated, ``legba_pivot_test``) — exercises the REAL
    ``UPDATE ... RETURNING`` bump SQL against Postgres for the same scenarios
    plus the lookback-window boundary.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.sources._contract import Signal
from legba.runtime import source_actor
from legba.runtime.source_actor import (
    _intrasource_dedup_enabled,
    _intrasource_dedup_window_hours,
    write_canonical_signal,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sig(
    *,
    source_id: str = "source.test",
    content_hash: str = "",
    fetched_at: datetime | None = None,
    title: str = "item",
) -> Signal:
    return Signal(
        source_id=source_id,
        payload={"title": title},
        content_hash=content_hash,
        # A score short-circuits the host-credibility lookup so the fake conn
        # never needs to model the source_credibility probe.
        source_credibility=0.5,
        fetched_at=fetched_at or datetime.now(tz=timezone.utc),
    )


class FakeSignalsConn:
    """In-memory ``signals`` table modeling exactly the surface
    :func:`write_canonical_signal` touches:

      * ``fetchval`` — the S-4 ``UPDATE signals ... GREATEST(fetched_at, $4) ...
        RETURNING id`` bump (find the freshest same-(source_id, content_hash,
        owner_tenant) row, advance its ``fetched_at``, return its id — or None).
      * ``fetchrow`` — the ``INSERT INTO signals ... RETURNING id`` (append a row,
        return its id).
      * ``fetch`` — the credibility probe (returns no hosts; unused here since the
        test signals carry a score).

    The lookback window is time-relative in the real SQL; the unit test uses
    recent timestamps only and the fake treats every candidate as in-window (the
    window boundary itself is exercised by the live-DB test).
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def fetch(self, sql: str, *params: Any):  # pragma: no cover - unused
        return []

    async def fetchval(self, sql: str, *params: Any):
        assert "UPDATE signals" in sql and "GREATEST(fetched_at" in sql, sql
        source_id, content_hash, owner_tenant, incoming = params
        if not content_hash:
            return None
        cands = [
            r for r in self.rows
            if r["source_id"] == source_id
            and r["content_hash"] == content_hash
            and r["owner_tenant"] == owner_tenant
        ]
        if not cands:
            return None
        cands.sort(key=lambda r: (r["fetched_at"], str(r["id"])))
        top = cands[-1]  # most-recent (ORDER BY fetched_at DESC, id DESC LIMIT 1)
        if incoming is not None and incoming > top["fetched_at"]:
            top["fetched_at"] = incoming  # GREATEST(fetched_at, $4)
        top["bumps"] = top.get("bumps", 0) + 1
        return top["id"]

    async def fetchrow(self, sql: str, *params: Any):
        assert "INSERT INTO signals" in sql, sql
        # Param order per _INSERT_SIGNAL: $1 id, $2 source_id, $6 fetched_at,
        # $7 owner_tenant, $24 content_hash, $25 canonical_signal_id.
        row = {
            "id": params[0],
            "source_id": params[1],
            "fetched_at": params[5],
            "owner_tenant": params[6],
            "content_hash": params[23],
            "canonical_signal_id": params[24],
        }
        self.rows.append(row)
        return {"id": params[0]}

    async def execute(self, *a: Any, **k: Any):  # pragma: no cover - unused
        return None


async def _write(conn, sig, *, tenant="t1", stats=None):
    return await write_canonical_signal(
        conn, sig, source_version="v" * 16, owner_tenant=tenant, dedup_stats=stats,
    )


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def test_dedup_enabled_default_on(monkeypatch):
    monkeypatch.delenv(source_actor._INTRASOURCE_DEDUP_ENV, raising=False)
    assert _intrasource_dedup_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "OFF", " Off "])
def test_dedup_flag_off_values(monkeypatch, val):
    monkeypatch.setenv(source_actor._INTRASOURCE_DEDUP_ENV, val)
    assert _intrasource_dedup_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "anything"])
def test_dedup_flag_on_values(monkeypatch, val):
    monkeypatch.setenv(source_actor._INTRASOURCE_DEDUP_ENV, val)
    assert _intrasource_dedup_enabled() is True


def test_window_default_and_overrides(monkeypatch):
    monkeypatch.delenv(source_actor._INTRASOURCE_DEDUP_WINDOW_ENV, raising=False)
    assert _intrasource_dedup_window_hours() == 168
    monkeypatch.setenv(source_actor._INTRASOURCE_DEDUP_WINDOW_ENV, "720")
    assert _intrasource_dedup_window_hours() == 720
    # garbage / non-positive degrade to the default
    for bad in ("0", "-5", "abc", ""):
        monkeypatch.setenv(source_actor._INTRASOURCE_DEDUP_WINDOW_ENV, bad)
        assert _intrasource_dedup_window_hours() == 168


# ---------------------------------------------------------------------------
# Unit — collapse semantics via write_canonical_signal + fake conn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identical_source_hash_collapses_and_bumps(monkeypatch):
    """Two ingests of an identical (source_id, content_hash) => ONE row, and the
    surviving row's fetched_at is advanced to the later poll's timestamp."""
    monkeypatch.setenv(source_actor._INTRASOURCE_DEDUP_ENV, "1")
    conn = FakeSignalsConn()
    t0 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=5)

    id1 = await _write(conn, _sig(source_id="s", content_hash="H", fetched_at=t0))
    assert id1 is not None
    assert len(conn.rows) == 1
    assert conn.rows[0]["fetched_at"] == t0

    stats: dict[str, int] = {}
    id2 = await _write(
        conn, _sig(source_id="s", content_hash="H", fetched_at=t1), stats=stats,
    )
    assert id2 is None                      # collapsed — no new row id
    assert len(conn.rows) == 1              # NO second row
    assert conn.rows[0]["fetched_at"] == t1  # recency bumped forward
    assert stats == {"deduped": 1}          # counted as a productive collapse


@pytest.mark.asyncio
async def test_different_content_hash_inserts_second_row(monkeypatch):
    monkeypatch.setenv(source_actor._INTRASOURCE_DEDUP_ENV, "1")
    conn = FakeSignalsConn()
    id1 = await _write(conn, _sig(source_id="s", content_hash="H1"))
    stats: dict[str, int] = {}
    id2 = await _write(conn, _sig(source_id="s", content_hash="H2"), stats=stats)
    assert id1 is not None and id2 is not None
    assert len(conn.rows) == 2
    assert stats.get("deduped", 0) == 0


@pytest.mark.asyncio
async def test_different_source_same_hash_inserts_second_row(monkeypatch):
    """Intra-source ONLY: the SAME content_hash from a DIFFERENT source_id is
    NOT collapsed (it may be genuine cross-source corroboration)."""
    monkeypatch.setenv(source_actor._INTRASOURCE_DEDUP_ENV, "1")
    conn = FakeSignalsConn()
    id1 = await _write(conn, _sig(source_id="s1", content_hash="H"))
    id2 = await _write(conn, _sig(source_id="s2", content_hash="H"))
    assert id1 is not None and id2 is not None
    assert len(conn.rows) == 2


@pytest.mark.asyncio
async def test_empty_content_hash_never_dedups(monkeypatch):
    """The empty-string content_hash is the schema default ('no hash') — never a
    dedup key, so two hash-less rows both land."""
    monkeypatch.setenv(source_actor._INTRASOURCE_DEDUP_ENV, "1")
    conn = FakeSignalsConn()
    await _write(conn, _sig(source_id="s", content_hash=""))
    await _write(conn, _sig(source_id="s", content_hash=""))
    assert len(conn.rows) == 2


@pytest.mark.asyncio
async def test_flag_off_disables_collapse(monkeypatch):
    monkeypatch.setenv(source_actor._INTRASOURCE_DEDUP_ENV, "0")
    conn = FakeSignalsConn()
    await _write(conn, _sig(source_id="s", content_hash="H"))
    stats: dict[str, int] = {}
    await _write(conn, _sig(source_id="s", content_hash="H"), stats=stats)
    assert len(conn.rows) == 2              # both inserted — collapse disabled
    assert stats.get("deduped", 0) == 0


@pytest.mark.asyncio
async def test_fetched_at_never_moves_backward(monkeypatch):
    """A late-arriving OLDER duplicate collapses but must not rewind recency."""
    monkeypatch.setenv(source_actor._INTRASOURCE_DEDUP_ENV, "1")
    conn = FakeSignalsConn()
    t_new = datetime(2026, 7, 2, tzinfo=timezone.utc)
    t_old = t_new - timedelta(hours=3)
    await _write(conn, _sig(source_id="s", content_hash="H", fetched_at=t_new))
    id2 = await _write(
        conn, _sig(source_id="s", content_hash="H", fetched_at=t_old),
    )
    assert id2 is None
    assert len(conn.rows) == 1
    assert conn.rows[0]["fetched_at"] == t_new   # GREATEST held the newer time


# ---------------------------------------------------------------------------
# Live pivot-DB acceptance (env-gated) — the REAL bump SQL against Postgres
# ---------------------------------------------------------------------------


_PIVOT_DB = {
    "host": os.environ.get("LEGBA_PIVOT_PG_HOST", "127.0.0.1"),
    "port": int(os.environ.get("LEGBA_PIVOT_PG_PORT", "5432")),
    "user": os.environ.get("LEGBA_PIVOT_PG_USER", "legba"),
    "password": os.environ.get("LEGBA_PIVOT_PG_PASSWORD", "legba"),
    "database": os.environ.get("LEGBA_PIVOT_PG_DB", "legba_pivot_test"),
}


@pytest.fixture
async def pivot_pool():
    asyncpg = pytest.importorskip("asyncpg")
    try:
        pool = await asyncpg.create_pool(min_size=1, max_size=4, **_PIVOT_DB)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"legba_pivot_test unreachable: {exc}")
    async with pool.acquire() as conn:
        ok = await conn.fetchval("SELECT to_regclass('signals')")
    if not ok:
        await pool.close()
        pytest.skip("pivot substrate (signals) not present")
    yield pool
    await pool.close()


async def _count(conn, tenant: str) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM signals WHERE owner_tenant=$1", tenant)


@pytest.mark.asyncio
async def test_live_intrasource_collapse_and_window(pivot_pool, monkeypatch):
    monkeypatch.setenv(source_actor._INTRASOURCE_DEDUP_ENV, "1")
    tenant = f"s4_{uuid4().hex[:8]}"
    src = f"source.s4.{uuid4().hex[:6]}"
    other = f"source.s4o.{uuid4().hex[:6]}"
    h = f"s4hash_{uuid4().hex}"
    # Seed WITHIN the default 168h window (relative to the real NOW) so the
    # collapse is eligible; the window-boundary case is scenario 4 below.
    now = datetime.now(tz=timezone.utc)
    t0 = now - timedelta(minutes=10)
    try:
        async with pivot_pool.acquire() as conn:
            # 1) identical (source, hash) => ONE row, fetched_at bumped forward.
            id1 = await _write(
                conn, _sig(source_id=src, content_hash=h, fetched_at=t0),
                tenant=tenant,
            )
            assert id1 is not None
            later = datetime.now(tz=timezone.utc)
            id2 = await _write(
                conn, _sig(source_id=src, content_hash=h, fetched_at=later),
                tenant=tenant,
            )
            assert id2 is None
            assert await _count(conn, tenant) == 1
            bumped = await conn.fetchval(
                "SELECT fetched_at FROM signals WHERE id=$1", id1)
            assert bumped > t0  # advanced off the seed time (GREATEST)

            # 2) different content_hash => a second row lands.
            await _write(
                conn, _sig(source_id=src, content_hash=h + "x", fetched_at=later),
                tenant=tenant,
            )
            assert await _count(conn, tenant) == 2

            # 3) different SOURCE, same hash => a second row lands (intra only).
            await _write(
                conn, _sig(source_id=other, content_hash=h, fetched_at=later),
                tenant=tenant,
            )
            assert await _count(conn, tenant) == 3

            # 4) window boundary: a same-(source, hash) row OUTSIDE the lookback
            # window is not a collapse target — a fresh row lands. Use a 1-hour
            # window and a seed row well outside it.
            monkeypatch.setenv(
                source_actor._INTRASOURCE_DEDUP_WINDOW_ENV, "1")
            oldsrc = f"source.s4old.{uuid4().hex[:6]}"
            oldh = f"s4old_{uuid4().hex}"
            old_t = datetime.now(tz=timezone.utc) - timedelta(hours=48)
            await conn.execute(
                """INSERT INTO signals
                       (id, source_id, owner_tenant, modality, payload,
                        content_hash, fetched_at)
                   VALUES ($1,$2,$3,'text',$4::jsonb,$5,$6)""",
                uuid4(), oldsrc, tenant, "{}", oldh, old_t,
            )
            # same (source, hash) but the only existing row is 48h old > 1h window
            id_fresh = await _write(
                conn,
                _sig(source_id=oldsrc, content_hash=oldh,
                     fetched_at=datetime.now(tz=timezone.utc)),
                tenant=tenant,
            )
            assert id_fresh is not None  # inserted (old row out of window)
            n_old = await conn.fetchval(
                "SELECT count(*) FROM signals WHERE owner_tenant=$1 "
                "AND source_id=$2 AND content_hash=$3",
                tenant, oldsrc, oldh)
            assert n_old == 2
    finally:
        async with pivot_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM signals WHERE owner_tenant=$1", tenant)
