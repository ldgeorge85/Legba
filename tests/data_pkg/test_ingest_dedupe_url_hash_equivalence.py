# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-S — the indexed tier-1 lookup decides exactly what the full scan decided.

The FROZEN pre-N-S engine (``tests/fixtures/ingestion/ingest_dedupe_3482c09b.py``,
the module exactly as it stood at 3482c09b: every non-empty ``canonical_url``
row of the tenant read and canonicalised in Python per probe) and today's
engine (one indexed equality on ``signals.canonical_url_hash`` plus a bounded
scan of rows not yet hashed) are run against the SAME Postgres rows with the
SAME probes, and every result is compared field by field: duplicate or not,
tier, reason, matched row, resolved canonical.

The corpus is ~2,500 stored URLs built from realistic variant classes —
tracking parameters, query order, empty-valued parameters, trailing slashes,
host case, ``www.``, fragments, default ports, http/https, percent-encoding,
IDN hosts in both spellings, publisher article-id slugs, whitespace, and
degenerate strings — with deliberately colliding ``fetched_at`` values so the
``(fetched_at, id)`` tie-break is exercised, across two tenants. It is
compared in three backfill states: every row hashed (after the backfill),
no row hashed (the moment the migration lands), and a seeded half (the
backfill in progress — where an un-hashed EARLIER row must still win over a
hashed later one).

The one deliberate difference — a probe whose only URL is
``payload.source_url`` (the fetch endpoint) no longer probes tier 1 — is
excluded from the equivalence and asserted on its own below.
"""

from __future__ import annotations

import importlib.util
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest

from legba.data._url_canon import canonical_url, stored_canonical_url_hash
from legba.data.config import PostgresConfig
from legba.data.filters.ingest_dedupe import IngestDedupe
from legba.data.sources._contract import Signal

FROZEN = Path(__file__).resolve().parents[1] / "fixtures" / "ingestion" / "ingest_dedupe_3482c09b.py"


def _load_frozen() -> Any:
    name = "legba_frozen_ingest_dedupe_3482c09b"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, FROZEN)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod   # @dataclass resolves its module through sys.modules
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# The URL corpus
# ---------------------------------------------------------------------------

_HOSTS = [
    "reuters.com", "apnews.com", "bbc.co.uk", "dawn.com", "lefaso.net",
    "aljazeera.com", "theguardian.com", "earthquake.usgs.gov", "eonet.gsfc.nasa.gov",
    "news.example.org", "www2.example.org", "bücher.example", "xn--bcher-kva.example",
    "españa.example", "sub.domain.example.co", "yonhapnews.co.kr",
]

_PATHS = [
    "/world/{n}", "/news/{n}/some-slug", "/news/{n}/other-slug", "/article/{n}.html",
    "/2026/11/{d}/story-{n}", "/live/{n}/", "/spip.php", "/a/b/c-{n}", "/caf%C3%A9/{n}",
    "/caf%c3%a9/{n}", "/~user/{n}", "/%7Euser/{n}", "/news/20261113/{n}", "/{n}",
]

_QUERIES = [
    "", "?utm_source=x&utm_medium=y", "?id={n}", "?b=2&a=1", "?a=1&b=2", "?article{n}",
    "?id={n}&utm_campaign=z", "?ref=", "?x=1&x=2", "?q=%20space",
]


def _variants(url: str, rng: random.Random) -> list[str]:
    """Spellings of ``url`` that may or may not canonicalise together."""
    scheme, rest = url.split("://", 1)
    host, _, tail = rest.partition("/")
    tail = "/" + tail
    out = [
        url,
        f"{'http' if scheme == 'https' else 'https'}://{rest}",
        f"{scheme}://{host.upper()}{tail}",
        f"{scheme}://www.{host}{tail}" if not host.startswith("www") else url,
        url + "#section-2",
        f"{scheme}://{host}:{443 if scheme == 'https' else 80}{tail}",
        f"{scheme}://{host}:8443{tail}",
        url.rstrip("/") if url.endswith("/") else url + "/",
        f"  {url}  ",
        (url + ("&" if "?" in url else "?") + "utm_source=rss"),
    ]
    rng.shuffle(out)
    return out[: rng.randint(2, 6)]


def _corpus(rng: random.Random, n_docs: int) -> list[str]:
    urls: list[str] = []
    for i in range(n_docs):
        host = rng.choice(_HOSTS)
        n = rng.choice([str(rng.randint(100000, 9999999)), str(rng.randint(1, 999))])
        path = rng.choice(_PATHS).format(n=n, d=rng.randint(1, 28))
        query = rng.choice(_QUERIES).format(n=n)
        base = f"{rng.choice(['https', 'http'])}://{host}{path}{query}"
        urls.extend(_variants(base, rng))
    urls += ["not a url", "http://[broken", "   ", "mailto:desk@example.org",
             "https://example.org", "HTTPS://EXAMPLE.ORG/"]
    return urls


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


async def _seed(conn: asyncpg.Connection, tenant: str, other: str, urls: list[str],
                rng: random.Random) -> list[dict]:
    rows = []
    for url in urls:
        rows.append({
            "id": uuid4(),
            "tenant": tenant if rng.random() < 0.85 else other,
            "url": url,
            # 40 distinct instants for ~2,500 rows: ties are common.
            "fetched_at": T0 + timedelta(minutes=rng.randint(0, 39)),
            "canonical_signal_id": None,
        })
    # Some rows are already aliases of an earlier row (transitive resolution).
    for r in rng.sample(rows, k=len(rows) // 10):
        r["canonical_signal_id"] = rng.choice(rows)["id"]
    await conn.executemany(
        """INSERT INTO signals (id, source_id, owner_tenant, modality, payload,
                                canonical_url, content_hash, fetched_at,
                                canonical_signal_id, canonical_url_hash)
           VALUES ($1, 'source.ns.equiv', $2, 'text', '{}'::jsonb, $3, '', $4, $5, $6)""",
        [(r["id"], r["tenant"], r["url"], r["fetched_at"], r["canonical_signal_id"],
          stored_canonical_url_hash(r["url"])) for r in rows],
    )
    return rows


async def _set_backfill_state(conn: asyncpg.Connection, rows: list[dict], state: str,
                              rng: random.Random) -> None:
    ids = [r["id"] for r in rows]
    if state == "all":
        await conn.execute(
            "UPDATE signals SET canonical_url_hash = NULL WHERE id = ANY($1::uuid[])", ids)
        await conn.executemany(
            "UPDATE signals SET canonical_url_hash = $2 WHERE id = $1",
            [(r["id"], stored_canonical_url_hash(r["url"])) for r in rows])
    elif state == "none":
        await conn.execute(
            "UPDATE signals SET canonical_url_hash = NULL WHERE id = ANY($1::uuid[])", ids)
    else:  # mixed: a seeded half un-hashed
        await _set_backfill_state(conn, rows, "all", rng)
        half = [r["id"] for r in rows if rng.random() < 0.5]
        await conn.execute(
            "UPDATE signals SET canonical_url_hash = NULL WHERE id = ANY($1::uuid[])", half)


def _probes(rows: list[dict], urls: list[str], rng: random.Random, n: int) -> list[Signal]:
    """Probe signals: stored spellings, fresh variants, unseen URLs, URLs in
    payload.url / payload.link, and probes that ARE a stored row (self-exclusion)."""
    out: list[Signal] = []
    for i in range(n):
        kind = i % 6
        if kind == 0:
            url = rng.choice(urls)
        elif kind == 1:
            url = rng.choice(_variants(rng.choice(urls).strip() or "https://x.example/", rng))
        elif kind == 2:
            url = f"https://unseen-{rng.randint(0, 10**9)}.example/{i}"
        else:
            url = rng.choice(urls)
        sid = uuid4()
        if kind == 5:
            sid = rng.choice(rows)["id"]   # the probe's own row already exists
        if kind == 3:
            out.append(Signal(source_id="source.ns.probe", signal_id=sid, payload={"url": url}))
        elif kind == 4:
            out.append(Signal(source_id="source.ns.probe", signal_id=sid, payload={"link": url}))
        else:
            out.append(Signal(source_id="source.ns.probe", signal_id=sid, canonical_url=url))
    return out


def _cmp(r: Any) -> tuple:
    return (r.is_duplicate, r.tier, r.reason, r.canonical_signal_id, r.matched_signal_id)


@pytest.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    try:
        yield c
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# The equivalence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state,n_probes", [("mixed", 1500), ("all", 500), ("none", 500)])
async def test_indexed_lookup_matches_full_scan(conn, state: str, n_probes: int) -> None:
    rng = random.Random(f"N-S-{state}")
    tenant, other = f"ns_eq_{uuid4().hex[:8]}", f"ns_eq_other_{uuid4().hex[:8]}"
    urls = _corpus(rng, 650)
    rows = await _seed(conn, tenant, other, urls, rng)
    assert len(rows) >= 2000
    # The corpus must actually exercise collisions, not just distinct URLs.
    canon_groups: dict[str, int] = {}
    for u in urls:
        c = canonical_url(u)
        canon_groups[c] = canon_groups.get(c, 0) + 1
    assert sum(1 for v in canon_groups.values() if v > 1) > 300
    await _set_backfill_state(conn, rows, state, rng)

    old_mod = _load_frozen()
    probes = _probes(rows, urls, rng, n_probes)
    hits = 0
    try:
        for owner in (tenant, None):
            old = old_mod.IngestDedupe(tiers=frozenset({1}), owner_tenant=owner)
            new = IngestDedupe(tiers=frozenset({1}), owner_tenant=owner)
            sample = probes if owner is not None else probes[:200]
            for sig in sample:
                o = await old.resolve(conn, sig)
                n = await new.resolve(conn, sig)
                assert _cmp(n) == _cmp(o), (state, owner, sig.canonical_url, sig.payload)
                hits += o.is_duplicate and owner is not None
    finally:
        await conn.execute("DELETE FROM signals WHERE owner_tenant = ANY($1::text[])",
                           [tenant, other])
    # Both hits and misses are well represented.
    assert hits > n_probes // 4
    assert hits < n_probes


async def test_unhashed_earlier_row_beats_hashed_later_row(conn) -> None:
    """Mid-backfill: the earliest row wins even when only a later one is hashed."""
    tenant = f"ns_order_{uuid4().hex[:8]}"
    early, late = uuid4(), uuid4()
    url = "https://www.Example.org/a?b=2&a=1#x"
    try:
        await conn.executemany(
            """INSERT INTO signals (id, source_id, owner_tenant, payload, canonical_url,
                                    fetched_at, canonical_url_hash)
               VALUES ($1, 's', $2, '{}'::jsonb, $3, $4, $5)""",
            [(early, tenant, url, T0, None),
             (late, tenant, "https://example.org/a?a=1&b=2", T0 + timedelta(hours=1),
              stored_canonical_url_hash("https://example.org/a?a=1&b=2"))])
        probe = Signal(source_id="p", canonical_url="https://EXAMPLE.org/a?a=1&b=2")
        res = await IngestDedupe(tiers=frozenset({1}), owner_tenant=tenant).resolve(conn, probe)
        assert res.is_duplicate and res.matched_signal_id == early
        # ...and once the early row is hashed too, the answer is the same.
        await conn.execute("UPDATE signals SET canonical_url_hash = $2 WHERE id = $1",
                           early, stored_canonical_url_hash(url))
        res = await IngestDedupe(tiers=frozenset({1}), owner_tenant=tenant).resolve(conn, probe)
        assert res.matched_signal_id == early
    finally:
        await conn.execute("DELETE FROM signals WHERE owner_tenant = $1", tenant)


# ---------------------------------------------------------------------------
# The endpoint URL (payload.source_url) — the one deliberate change
# ---------------------------------------------------------------------------


async def test_endpoint_url_never_collapses_distinct_items(conn) -> None:
    """UKMTO shape: a JSON API with no per-item URL — every item carries the
    API address as ``payload.source_url`` and no URL of its own. Tier 1 must
    not key on it (it names no document); tier 2 (the content hash, which
    folds in the item's own id) decides. Even a stored row whose
    canonical_url IS the endpoint — the one case the old scan would have
    matched every item against — collapses nothing."""
    tenant = f"ns_api_{uuid4().hex[:8]}"
    api = "https://sccd.royalnavy.mod.uk/api/ukmto/all"
    trap = uuid4()
    try:
        await conn.execute(
            """INSERT INTO signals (id, source_id, owner_tenant, payload, canonical_url,
                                    fetched_at, canonical_url_hash)
               VALUES ($1, 'source.other', $2, '{}'::jsonb, $3, $4, $5)""",
            trap, tenant, api, T0, stored_canonical_url_hash(api))
        eng = IngestDedupe(tiers=frozenset({1, 2}), owner_tenant=tenant)
        old = _load_frozen().IngestDedupe(tiers=frozenset({1, 2}), owner_tenant=tenant)
        for i in range(4):
            item = Signal(source_id="source.ukmto.incidents", content_hash=f"incident-{i}",
                          payload={"link": "", "source_url": api, "external_id": f"id-{i}"})
            assert IngestDedupe.url_hash(item) is None
            res = await eng.resolve(conn, item)
            assert res.is_duplicate is False
            # The frozen scan WOULD have collapsed every incident into the trap.
            assert (await old.resolve(conn, item)).matched_signal_id == trap
    finally:
        await conn.execute("DELETE FROM signals WHERE owner_tenant = $1", tenant)


def test_document_url_still_wins_over_endpoint() -> None:
    """An item with its own link keeps tier 1 on that link, never the endpoint."""
    sig = Signal(source_id="s", payload={"link": "https://a.example/1",
                                         "source_url": "https://a.example/feed"})
    assert IngestDedupe.probe_url(sig) == ("https://a.example/1", "link")
    assert IngestDedupe.url_hash(sig) == stored_canonical_url_hash("https://a.example/1")


# ---------------------------------------------------------------------------
# Persisted rows through the real insert path
# ---------------------------------------------------------------------------


async def test_insert_path_stores_the_key_and_tier1_links_on_it(conn) -> None:
    from legba.runtime.sinks.signal import write_canonical_signal

    tenant = f"ns_ins_{uuid4().hex[:8]}"
    first = Signal(source_id="source.ns.a", canonical_url="https://www.dawn.com/news/2020547/first-slug?utm_source=x",
                   content_hash=f"a-{uuid4().hex}", fetched_at=T0, payload={"title": "a"})
    second = Signal(source_id="source.ns.b", canonical_url="https://DAWN.com/news/2020547/re-headlined?utm_source=x#top",
                    content_hash=f"b-{uuid4().hex}", fetched_at=T0 + timedelta(hours=1),
                    payload={"title": "b"})
    nourl = Signal(source_id="source.ns.c", canonical_url=None, content_hash=f"c-{uuid4().hex}",
                   fetched_at=T0, payload={"title": "c", "source_url": "https://api.example/all"})
    blank = Signal(source_id="source.ns.d", canonical_url="   ", content_hash=f"d-{uuid4().hex}",
                   fetched_at=T0, payload={"title": "d"})
    # A malformed port used to make canonical_url() raise: on the write path
    # that would now lose the signal, so the key must be total.
    badport = Signal(source_id="source.ns.e", canonical_url="https://e.example:443:8443/x",
                     content_hash=f"e-{uuid4().hex}", fetched_at=T0, payload={"title": "e"})
    try:
        for s in (first, second, nourl, blank, badport):
            assert await write_canonical_signal(conn, s, source_version="v", owner_tenant=tenant) == s.signal_id
        stored = {r["id"]: r for r in await conn.fetch(
            "SELECT id, canonical_url, canonical_url_hash FROM signals WHERE owner_tenant = $1", tenant)}
        assert stored[first.signal_id]["canonical_url_hash"] == stored_canonical_url_hash(first.canonical_url)
        assert stored[second.signal_id]["canonical_url_hash"] == stored[first.signal_id]["canonical_url_hash"]
        assert stored[nourl.signal_id]["canonical_url_hash"] is None
        # A blank-canonical URL gets the unmatchable key (never "pending" forever).
        assert stored[blank.signal_id]["canonical_url_hash"] == stored_canonical_url_hash("   ")
        assert stored[badport.signal_id]["canonical_url_hash"] == stored_canonical_url_hash(
            "https://e.example:443:8443/x")
        assert await conn.fetchval(
            "SELECT count(*) FROM signals WHERE owner_tenant = $1 AND canonical_url_hash IS NULL "
            "AND canonical_url IS NOT NULL AND canonical_url <> ''", tenant) == 0

        eng = IngestDedupe(owner_tenant=tenant, produced_by=f"ingest_dedupe:{tenant}")
        res = await eng.apply(conn, second)
        assert res.is_duplicate and res.tier == 1 and res.canonical_signal_id == first.signal_id
        link = await conn.fetchrow(
            "SELECT reason FROM signal_aliases WHERE alias_signal_id = $1", second.signal_id)
        assert link["reason"] == "ingest_url"
        assert await conn.fetchval("SELECT canonical_signal_id FROM signals WHERE id = $1",
                                   second.signal_id) == first.signal_id
    finally:
        await conn.execute("DELETE FROM signal_aliases WHERE produced_by = $1", f"ingest_dedupe:{tenant}")
        await conn.execute("DELETE FROM signals WHERE owner_tenant = $1", tenant)


async def test_lookup_uses_the_index(conn) -> None:
    """At 20k rows the planner reads the equality through the hash index."""
    tenant = f"ns_plan_{uuid4().hex[:8]}"
    try:
        await conn.execute(
            """INSERT INTO signals (id, source_id, owner_tenant, payload, canonical_url,
                                    fetched_at, canonical_url_hash)
               SELECT gen_random_uuid(), 's', $1, '{}'::jsonb, 'https://e.example/' || g,
                      $2::timestamptz + g * interval '1 second', md5(g::text) || md5(g::text)
               FROM generate_series(1, 20000) g""", tenant, T0)
        await conn.execute("ANALYZE signals")
        plan = "\n".join(r[0] for r in await conn.fetch(
            "EXPLAIN SELECT id, canonical_signal_id, fetched_at FROM signals "
            "WHERE canonical_url_hash = $2 AND id <> $1 AND owner_tenant = $3 "
            "ORDER BY fetched_at ASC, id ASC LIMIT 1", uuid4(), "0" * 64, tenant))
        assert "idx_signals_canonical_url_hash" in plan, plan
        plan = "\n".join(r[0] for r in await conn.fetch(
            "EXPLAIN SELECT id, canonical_signal_id, canonical_url, fetched_at FROM signals "
            "WHERE canonical_url_hash IS NULL AND canonical_url IS NOT NULL AND canonical_url <> '' "
            "AND id <> $1 AND owner_tenant = $2 ORDER BY fetched_at ASC, id ASC", uuid4(), tenant))
        assert "idx_signals_canonical_url_unhashed" in plan, plan
    finally:
        await conn.execute("DELETE FROM signals WHERE owner_tenant = $1", tenant)
