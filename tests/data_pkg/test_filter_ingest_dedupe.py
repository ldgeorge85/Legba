# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for source-side ingest dedupe (tiers 1+2, alias/canonical).

Two layers, mirroring ``test_analyst_cross_source_dedup.py``:

  * **Unit** — a process-local fake asyncpg connection drives the engine's
    resolve/apply/link logic with no substrate. Asserts the alias/canonical
    semantics (raw rows kept, canonical stamped self-canonical, alias linked,
    transitive canonical resolution, idempotent re-link) and the
    ``ingestion_filters`` → engine construction.

  * **Live pivot-DB** (env-gated, ``legba_pivot_test``) — inserts two raw
    signals sharing a content hash via two sources, runs the engine on the
    second at ingest, and asserts: BOTH rows survive, the alias points at the
    canonical, and a ``canonical_only`` subscription query sees exactly one.

The engine reuses :meth:`Dedupe4TierHandler.canonical_url` /
``normalized_content`` for hashing, so the ingest keys match the target-side
filter + the periodic analyst byte-for-byte.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.filters.ingest_dedupe import (
    IngestDedupe,
    IngestDedupeResult,
    ingest_dedupe_from_stages,
)
from legba.data.sources._contract import Signal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signal(
    *,
    source_id: str = "source.test",
    title: str = "",
    body: str = "",
    url: str | None = None,
    content_hash: str = "",
    canonical_signal_id: UUID | None = None,
    signal_id: UUID | None = None,
) -> Signal:
    payload: dict[str, Any] = {}
    if title:
        payload["title"] = title
    if body:
        payload["body"] = body
    kwargs: dict[str, Any] = dict(
        source_id=source_id,
        payload=payload,
        content_hash=content_hash,
        canonical_signal_id=canonical_signal_id,
    )
    if url is not None:
        kwargs["canonical_url"] = url
    if signal_id is not None:
        kwargs["signal_id"] = signal_id
    return Signal(**kwargs)


class FakeConn:
    """Minimal asyncpg-connection fake backed by an in-memory signals table.

    Models exactly the surface :class:`IngestDedupe` uses: ``fetchrow`` for the
    tier lookups, ``fetchval`` for the alias INSERT ... RETURNING, and
    ``execute`` for the canonical / alias UPDATEs. The in-memory ``signals``
    list + ``aliases`` set let a unit test assert the alias/canonical writes
    without a real DB.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        # rows: [{"id", "canonical_url", "content_hash", "fetched_at",
        #         "owner_tenant", "canonical_signal_id"}]
        self.signals = rows
        self.aliases: set[tuple[str, str]] = set()
        self.alias_rows: list[dict[str, Any]] = []

    async def fetch(self, sql: str, *params: Any):
        # Tier-1 canonical-URL scan (the canonicalise-BOTH-sides lookup): returns
        # every tenant row carrying a non-empty canonical_url (excluding self),
        # ordered fetched_at ASC, id ASC — IngestDedupe canonicalises each
        # stored value in Python and picks the first match.
        exclude_id = params[0]
        tenant = params[1] if len(params) > 1 else None
        cands = [
            r for r in self.signals
            if str(r["id"]) != str(exclude_id)
            and r.get("canonical_url")
            and (tenant is None or r.get("owner_tenant") == tenant)
        ]
        cands.sort(key=lambda r: (r["fetched_at"], str(r["id"])))
        return [
            {
                "id": r["id"],
                "canonical_signal_id": r.get("canonical_signal_id"),
                "canonical_url": r.get("canonical_url"),
            }
            for r in cands
        ]

    async def fetchrow(self, sql: str, *params: Any):
        exclude_id = params[0]
        if "canonical_url = $2" in sql:
            canon = params[1]
            tenant = params[2] if len(params) > 2 else None
            cands = [
                r for r in self.signals
                if str(r["id"]) != str(exclude_id)
                and r.get("canonical_url") == canon
                and (tenant is None or r.get("owner_tenant") == tenant)
            ]
        elif "content_hash = $2" in sql:
            ch = params[1]
            tenant = params[2] if len(params) > 2 else None
            cands = [
                r for r in self.signals
                if str(r["id"]) != str(exclude_id)
                and r.get("content_hash") == ch
                and r.get("content_hash")
                and (tenant is None or r.get("owner_tenant") == tenant)
            ]
        else:  # pragma: no cover
            return None
        if not cands:
            return None
        cands.sort(key=lambda r: (r["fetched_at"], str(r["id"])))
        top = cands[0]
        return {"id": top["id"], "canonical_signal_id": top.get("canonical_signal_id")}

    async def fetchval(self, sql: str, *params: Any):
        if "INSERT INTO signal_aliases" in sql:
            alias_id, canon_id = str(params[0]), str(params[1])
            key = (alias_id, canon_id)
            if key in self.aliases:
                return None  # ON CONFLICT DO NOTHING
            self.aliases.add(key)
            self.alias_rows.append({
                "alias_signal_id": params[0],
                "canonical_signal_id": params[1],
                "reason": params[2],
                "score": params[3],
                "produced_by": params[4],
            })
            return params[0]
        return None  # pragma: no cover

    async def execute(self, sql: str, *params: Any):
        if "SET canonical_signal_id = id" in sql:
            cid = str(params[0])
            for r in self.signals:
                if str(r["id"]) == cid:
                    r["canonical_signal_id"] = r["id"]
        elif "SET canonical_signal_id = $2" in sql:
            alias_id, canon_id = str(params[0]), params[1]
            for r in self.signals:
                if str(r["id"]) == alias_id:
                    r["canonical_signal_id"] = canon_id
        return "OK"


# ---------------------------------------------------------------------------
# Construction from ingestion_filters
# ---------------------------------------------------------------------------


def test_from_stages_builds_both_tiers():
    stages = [
        {"kind": "dedupe_tier_1", "config": {}},
        {"kind": "dedupe_tier_2", "config": {}},
    ]
    eng = ingest_dedupe_from_stages(stages, owner_tenant="t1")
    assert eng is not None
    assert eng.is_tier_active(1)
    assert eng.is_tier_active(2)
    assert eng.owner_tenant == "t1"


def test_from_stages_single_tier():
    eng = ingest_dedupe_from_stages([{"kind": "dedupe_tier_2"}])
    assert eng is not None
    assert not eng.is_tier_active(1)
    assert eng.is_tier_active(2)


def test_from_stages_none_when_no_dedupe():
    assert ingest_dedupe_from_stages([{"kind": "language_detect"}]) is None
    assert ingest_dedupe_from_stages([]) is None
    assert ingest_dedupe_from_stages(None) is None


def test_from_stages_ignores_tier3_tier4():
    """Tiers 3/4 aren't ingest-side — they're ignored here even if declared."""
    eng = ingest_dedupe_from_stages(
        [{"kind": "dedupe_tier_3"}, {"kind": "dedupe_tier_4"}]
    )
    assert eng is None


def test_from_stages_accepts_filterstage_models():
    """Works with FilterStage pydantic models, not just dicts."""
    from legba.data.schemas.source import FilterStage

    stages = [FilterStage(kind="dedupe_tier_1"), FilterStage(kind="dedupe_tier_2")]
    eng = ingest_dedupe_from_stages(stages)
    assert eng is not None and eng.is_tier_active(1) and eng.is_tier_active(2)


# ---------------------------------------------------------------------------
# Hashing parity with the target-side handler
# ---------------------------------------------------------------------------


def test_url_hash_canonicalizes():
    a = _signal(url="https://example.com/x?b=2&a=1")
    b = _signal(url="HTTPS://EXAMPLE.COM/x?a=1&b=2#frag")
    assert IngestDedupe.url_hash(a) == IngestDedupe.url_hash(b)


def test_url_hash_none_without_url():
    assert IngestDedupe.url_hash(_signal(title="no url")) is None


def test_content_hash_prefers_baseline_column():
    s = _signal(content_hash="precomputed", title="ignored")
    assert IngestDedupe.content_hash(s) == "precomputed"


def test_content_hash_falls_back_to_body():
    s = _signal(title="Quake", body="A quake hit the region")
    h = IngestDedupe.content_hash(s)
    assert h and len(h) == 64  # sha256 hex


# ---------------------------------------------------------------------------
# resolve / apply — alias/canonical semantics (unit, fake conn)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_miss_when_pool_empty():
    eng = IngestDedupe(owner_tenant="t1")
    conn = FakeConn([])
    s = _signal(url="https://a.example/1", content_hash="H1")
    res = await eng.apply(conn, s)
    assert res.is_duplicate is False
    assert s.canonical_signal_id is None  # implicit canonical (NULL)
    assert conn.alias_rows == []


@pytest.mark.asyncio
async def test_tier2_content_hash_links_alias_to_existing_canonical():
    """Same content via 2 sources => new row aliases to the earlier one."""
    earlier_id = uuid4()
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [{
        "id": earlier_id,
        "canonical_url": "https://reuters.example/quake",
        "content_hash": "HQUAKE",
        "fetched_at": t0,
        "owner_tenant": "t1",
        "canonical_signal_id": None,
    }]
    conn = FakeConn(rows)
    eng = IngestDedupe(owner_tenant="t1", produced_by="ingest_dedupe:src")

    new_id = uuid4()
    rows.append({
        "id": new_id,
        "canonical_url": "https://ap.example/quake",   # different URL
        "content_hash": "HQUAKE",                        # same content
        "fetched_at": t0 + timedelta(minutes=3),
        "owner_tenant": "t1",
        "canonical_signal_id": None,
    })
    new_sig = _signal(
        signal_id=new_id, source_id="source.ap",
        url="https://ap.example/quake", content_hash="HQUAKE",
    )

    res = await eng.apply(conn, new_sig)
    assert res.is_duplicate is True
    assert res.tier == 2
    assert res.reason == "content_hash"
    assert res.canonical_signal_id == earlier_id
    # alias row's in-memory copy was mutated
    assert new_sig.canonical_signal_id == earlier_id
    # one alias link written
    assert len(conn.alias_rows) == 1
    assert conn.alias_rows[0]["alias_signal_id"] == new_id
    assert conn.alias_rows[0]["canonical_signal_id"] == earlier_id
    assert conn.alias_rows[0]["produced_by"] == "ingest_dedupe:src"
    # canonical row stamped self-canonical; both raw rows still present
    earlier_row = next(r for r in rows if r["id"] == earlier_id)
    assert earlier_row["canonical_signal_id"] == earlier_id
    assert len(rows) == 2  # NEVER destructive collapse


@pytest.mark.asyncio
async def test_tier1_url_match_links_alias():
    earlier_id = uuid4()
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [{
        "id": earlier_id,
        "canonical_url": "https://x.example/a",
        "content_hash": "HA",
        "fetched_at": t0,
        "owner_tenant": "t1",
        "canonical_signal_id": None,
    }]
    conn = FakeConn(rows)
    eng = IngestDedupe(owner_tenant="t1")

    new_id = uuid4()
    rows.append({
        "id": new_id, "canonical_url": "https://x.example/a",
        "content_hash": "HB", "fetched_at": t0 + timedelta(minutes=1),
        "owner_tenant": "t1", "canonical_signal_id": None,
    })
    new_sig = _signal(signal_id=new_id, url="https://x.example/a", content_hash="HB")
    res = await eng.apply(conn, new_sig)
    assert res.is_duplicate and res.tier == 1 and res.reason == "ingest_url"
    assert new_sig.canonical_signal_id == earlier_id


@pytest.mark.asyncio
async def test_transitive_canonical_resolution():
    """A new row matching an ALIAS links to the alias's canonical, not the alias."""
    root_id = uuid4()
    alias_id = uuid4()
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [
        {"id": root_id, "canonical_url": "https://r.example/1", "content_hash": "H",
         "fetched_at": t0, "owner_tenant": "t1", "canonical_signal_id": root_id},
        # alias already points at root
        {"id": alias_id, "canonical_url": "https://a.example/1", "content_hash": "H",
         "fetched_at": t0 + timedelta(minutes=1), "owner_tenant": "t1",
         "canonical_signal_id": root_id},
    ]
    conn = FakeConn(rows)
    eng = IngestDedupe(owner_tenant="t1")

    # New row by content hash matches the EARLIEST (root) by ordering, but the
    # transitive resolver guarantees it links to a true canonical regardless.
    new_id = uuid4()
    rows.append({
        "id": new_id, "canonical_url": "https://c.example/1", "content_hash": "H",
        "fetched_at": t0 + timedelta(minutes=2), "owner_tenant": "t1",
        "canonical_signal_id": None,
    })
    new_sig = _signal(signal_id=new_id, url="https://c.example/1", content_hash="H")
    res = await eng.apply(conn, new_sig)
    assert res.is_duplicate
    assert res.canonical_signal_id == root_id  # never an alias
    assert new_sig.canonical_signal_id == root_id


@pytest.mark.asyncio
async def test_relink_is_idempotent():
    earlier_id = uuid4()
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [{
        "id": earlier_id, "canonical_url": "https://x.example/a", "content_hash": "H",
        "fetched_at": t0, "owner_tenant": "t1", "canonical_signal_id": None,
    }]
    conn = FakeConn(rows)
    eng = IngestDedupe(owner_tenant="t1")
    new_id = uuid4()
    new_row = {
        "id": new_id, "canonical_url": "https://x.example/a", "content_hash": "H",
        "fetched_at": t0 + timedelta(minutes=1), "owner_tenant": "t1",
        "canonical_signal_id": None,
    }
    rows.append(new_row)
    new_sig = _signal(signal_id=new_id, url="https://x.example/a", content_hash="H")

    await eng.apply(conn, new_sig)
    # Re-run over the same pool: link already exists -> no new alias row.
    await eng.apply(conn, new_sig)
    assert len(conn.alias_rows) == 1


@pytest.mark.asyncio
async def test_tenant_scoping_prevents_cross_tenant_link():
    earlier_id = uuid4()
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [{
        "id": earlier_id, "canonical_url": "https://x.example/a", "content_hash": "H",
        "fetched_at": t0, "owner_tenant": "tenant_A", "canonical_signal_id": None,
    }]
    conn = FakeConn(rows)
    eng = IngestDedupe(owner_tenant="tenant_B")  # different tenant
    new_id = uuid4()
    rows.append({
        "id": new_id, "canonical_url": "https://x.example/a", "content_hash": "H",
        "fetched_at": t0 + timedelta(minutes=1), "owner_tenant": "tenant_B",
        "canonical_signal_id": None,
    })
    new_sig = _signal(signal_id=new_id, url="https://x.example/a", content_hash="H")
    res = await eng.apply(conn, new_sig)
    assert res.is_duplicate is False  # tenant A row is invisible to tenant B
    assert conn.alias_rows == []


# ---------------------------------------------------------------------------
# Live pivot-DB acceptance (env-gated) — real signal_aliases + canonical_only
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
        ok = await conn.fetchval("SELECT to_regclass('signal_aliases')")
        has_canon = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='signals' AND column_name='canonical_signal_id'"
        )
    if not ok or not has_canon:
        await pool.close()
        pytest.skip("pivot substrate (signal_aliases / canonical_signal_id) not present")
    yield pool
    await pool.close()


async def test_live_ingest_dedupe_links_cross_source_alias(pivot_pool):
    """Ingest-time analogue of the P-09 acceptance: same content via 2 sources
    => 1 canonical + 1 alias, both raw rows preserved, canonical_only sees 1."""
    import json

    tenant = f"ingdedup_{uuid4().hex[:8]}"
    content_hash = f"ing_{uuid4().hex}"
    sig_a, sig_b = uuid4(), uuid4()
    t0 = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)

    eng = IngestDedupe(
        owner_tenant=tenant, produced_by=f"ingest_dedupe:test_{tenant}",
    )
    produced_by = f"ingest_dedupe:test_{tenant}"

    try:
        async with pivot_pool.acquire() as conn:
            # First source row lands (earlier) — no existing match -> miss.
            await conn.execute(
                """INSERT INTO signals
                       (id, source_id, owner_tenant, modality, payload,
                        canonical_url, content_hash, fetched_at)
                   VALUES ($1,$2,$3,'text',$4::jsonb,$5,$6,$7)""",
                sig_a, "source.reuters", tenant,
                json.dumps({"title": "Quake hits region"}),
                "https://reuters.example/quake", content_hash, t0,
            )
            sig_a_obj = _signal(
                signal_id=sig_a, source_id="source.reuters",
                url="https://reuters.example/quake", content_hash=content_hash,
            )
            r_a = await eng.apply(conn, sig_a_obj)
            assert r_a.is_duplicate is False

            # Second source row lands (later, same content) — tier-2 hit.
            await conn.execute(
                """INSERT INTO signals
                       (id, source_id, owner_tenant, modality, payload,
                        canonical_url, content_hash, fetched_at)
                   VALUES ($1,$2,$3,'text',$4::jsonb,$5,$6,$7)""",
                sig_b, "source.ap", tenant,
                json.dumps({"title": "Quake hits region"}),
                "https://ap.example/quake", content_hash,
                t0 + timedelta(minutes=3),
            )
            sig_b_obj = _signal(
                signal_id=sig_b, source_id="source.ap",
                url="https://ap.example/quake", content_hash=content_hash,
            )
            r_b = await eng.apply(conn, sig_b_obj)
            assert r_b.is_duplicate is True
            assert r_b.tier == 2
            assert r_b.canonical_signal_id == sig_a  # earliest = canonical

            # BOTH raw rows survive.
            raw = await conn.fetchval(
                "SELECT count(*) FROM signals WHERE owner_tenant=$1", tenant)
            assert raw == 2

            # alias points at the canonical; canonical points at itself.
            aliases = await conn.fetch(
                "SELECT alias_signal_id, canonical_signal_id, reason, score "
                "FROM signal_aliases WHERE produced_by=$1", produced_by)
            assert len(aliases) == 1
            assert str(aliases[0]["alias_signal_id"]) == str(sig_b)
            assert str(aliases[0]["canonical_signal_id"]) == str(sig_a)
            assert aliases[0]["reason"] == "content_hash"

            ca = await conn.fetchval(
                "SELECT canonical_signal_id FROM signals WHERE id=$1", sig_a)
            cb = await conn.fetchval(
                "SELECT canonical_signal_id FROM signals WHERE id=$1", sig_b)
            assert str(ca) == str(sig_a)
            assert str(cb) == str(sig_a)

            # canonical_only subscription sees exactly 1.
            canon_only = await conn.fetchval(
                "SELECT count(*) FROM signals WHERE owner_tenant=$1 "
                "AND (canonical_signal_id = id OR canonical_signal_id IS NULL)",
                tenant)
            assert canon_only == 1

            # Re-apply on B is idempotent (no second alias row).
            await eng.apply(conn, sig_b_obj)
            assert await conn.fetchval(
                "SELECT count(*) FROM signal_aliases WHERE produced_by=$1",
                produced_by) == 1
    finally:
        async with pivot_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM signal_aliases WHERE produced_by=$1", produced_by)
            await conn.execute(
                "DELETE FROM signals WHERE owner_tenant=$1", tenant)
