# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ingestion intake, on the real path against a migrated
Postgres (the data_pkg session database, isolated test stack only).

Covered: every admission refusal (recorded and counted); a request satisfied
from a stored version / stored signal without a fetch; depth and fan-out
caps; budgets resetting at the UTC day; claim order and not-before; per-host
politeness at claim; crash safety (a dead worker's claim is reclaimed once,
not twice, and the dead worker cannot complete it); the retry policy and
expiry; the drain end to end (a fetched page lands as a signal linked to the
request and its parent, and a later poll of the same page collapses into it
— the database half of the equivalence proof in
tests/runtime/test_intake_equivalence.py); the follow step; the drift
re-check batch; the agent tool through the REAL binding (action_pack_
invocations); the archiver's routed re-fetch; the counts; a heartbeat test
that a drain tick over large pages does not stall the event loop; and the
query plans at 100,000 requests (printed with -s; inside a rolled-back
transaction).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio
import yaml

from legba.data.intake_contract import IntakeLimits
from legba.data.intake_store import (
    SubmitRequest,
    claim_requests,
    complete_request,
    expire_requests,
    fail_request,
    get_request,
    reclaim_expired_leases,
    submit_request,
)
from legba.data.intake_visibility import intake_counts
from legba.runtime.deps import StandardDeps
from legba.runtime.intake_drain import IntakeDrain
from legba.runtime.intake_run import run_request
from legba.runtime.source_actor import SourceCore, SourceDeps

from tests.runtime._ingest_fakes import REPO, NatsRecorder, client_for, descriptor

pytestmark = pytest.mark.asyncio

PAGES = "source.test.i1_pages"
FEED = "source.test.i1_feed"
NOFOLLOW = "source.test.i1_nofollow"
PUSH = "source.test.i1_push"
RETIRED = "source.test.i1_retired"
IMPL = "legba.data.sources.scrapers.example_news:ExampleNewsScraper"
HOST = "news.i1.example.org"


def _article(title: str, n: int = 12, extra: str = "") -> bytes:
    paras = "".join(
        f"<p>Paragraph {i}: {title} — dockworkers in Rotterdam and Antwerp extended the stoppage "
        f"on Thursday as talks over automation and pay broke down, the union said ({i}).</p>"
        for i in range(n)
    )
    return (f"<html><head><title>{title}</title></head><body><article><h1>{title}</h1>"
            f"{paras}{extra}</article></body></html>").encode()


class _Web:
    """A tiny web: path -> (status, body). robots.txt 404 (no rules)."""

    def __init__(self) -> None:
        self.pages: dict[str, tuple[int, bytes]] = {}
        self.hits: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.hits.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(404, content=b"")
        status, body = self.pages.get(str(request.url), (404, b"not here"))
        return httpx.Response(status, content=body, headers={"content-type": "text/html; charset=utf-8"})


@pytest.fixture
def web(monkeypatch, tmp_path) -> _Web:
    from legba.data.sources import scraper as scraper_mod

    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("LEGBA_FETCH_RECEIPTS", "1")
    w = _Web()
    monkeypatch.setattr(scraper_mod, "guarded_async_client", lambda **_kw: client_for(w))
    return w


def _desc(source_id: str, kind: str, config: dict, *, follow: dict | None = None,
          acquisition: str = "poll") -> Any:
    d = descriptor(source_id, kind, config)
    upd: dict[str, Any] = {"acquisition": acquisition}
    if follow is not None:
        from legba.data.schemas.source_follow import SourceFollow

        upd["follow"] = SourceFollow.model_validate(follow, strict=False)
    return d.model_copy(update=upd)


def _pages_desc() -> Any:
    return _desc(PAGES, "scraper", {"impl": IMPL, "seed_urls": [], "max_depth": 0, "respect_robots": True})


FOLLOW_POLICY = {"pipeline": PAGES, "same_host": True, "kinds": ["article", "anchor", "document"],
                 "max_depth": 1, "max_per_document": 3}


def _feed_desc() -> Any:
    return _desc(FEED, "rss", {"url": f"https://{HOST}/feed"}, follow=FOLLOW_POLICY)


async def _put(conn, desc: Any, state: str) -> None:
    body = desc.model_dump(mode="json")
    await conn.execute(
        "INSERT INTO source_descriptors (descriptor_id, version, schema_uri, is_head, kind, state, "
        "owner, name, body) VALUES ($1, $2, $3, TRUE, $4, $5, 'test:i1', $6, $7::jsonb)",
        desc.identity.id, desc.identity.version, desc.identity.schema_uri, desc.identity.kind,
        state, desc.identity.name, json.dumps(body),
    )


_TEST_IDS = (PAGES, FEED, NOFOLLOW, PUSH, RETIRED)


async def _scrub(c) -> None:
    """Remove every row this module writes (run before AND after each test)."""
    await c.execute("DELETE FROM ingestion_request_records")
    await c.execute("DELETE FROM ingestion_requests")
    await c.execute("ALTER TABLE fetch_version_records DISABLE TRIGGER fetch_version_records_insert_only")
    await c.execute("DELETE FROM fetch_version_records")
    await c.execute("ALTER TABLE fetch_version_records ENABLE TRIGGER fetch_version_records_insert_only")
    await c.execute("DELETE FROM fetch_drift_observations")
    await c.execute("DELETE FROM fetch_version_tombstones")
    await c.execute("DELETE FROM fetch_versions")
    await c.execute("DELETE FROM evidence_archive WHERE signal_id IN "
                    "(SELECT id FROM signals WHERE source_id = ANY($1::text[]))", list(_TEST_IDS))
    await c.execute("DELETE FROM signal_aliases WHERE alias_signal_id IN "
                    "(SELECT id FROM signals WHERE source_id = ANY($1::text[]))", list(_TEST_IDS))
    await c.execute("DELETE FROM signals WHERE source_id = ANY($1::text[])", list(_TEST_IDS))
    await c.execute("DELETE FROM source_poll_outcomes WHERE source_id = ANY($1::text[])", list(_TEST_IDS))
    await c.execute("DELETE FROM actor_filter_state WHERE actor_id LIKE 'intake::%' "
                    "OR filter_id = ANY($1::text[])", [f"source:{s}" for s in _TEST_IDS])
    await c.execute("DELETE FROM source_descriptors WHERE descriptor_id = ANY($1::text[])", list(_TEST_IDS))


@pytest_asyncio.fixture
async def pool(migrated_pg):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=8)
    async with p.acquire() as c:
        from legba.runtime.state import SCHEMA

        await c.execute(SCHEMA)
        await _scrub(c)
        await _put(c, _pages_desc(), "paused")
        await _put(c, _feed_desc(), "active")
        await _put(c, _desc(NOFOLLOW, "rss", {"url": f"https://{HOST}/other"}), "active")
        await _put(c, _desc(PUSH, "generic_webhook", {"shared_secret": "s"}, acquisition="push"), "active")
        await _put(c, _desc(RETIRED, "scraper", {"impl": IMPL}), "retired")
    try:
        yield p
    finally:
        # Leave the session database as found (nightly shuffled seed
        # 734222642: these five source heads, left behind, matched the desk
        # reference test's wanted classes and failed its pollution guard).
        try:
            async with p.acquire() as c:
                await _scrub(c)
        finally:
            await p.close()


def _resolver(pool) -> Any:
    deps = StandardDeps(pg_pool=pool, nats_publish=NatsRecorder())
    descs = {PAGES: _pages_desc(), FEED: _feed_desc()}

    async def _resolve(sid: str) -> Any:
        return SourceDeps(descriptor=descs[sid], deps=deps) if sid in descs else None

    return _resolve


def _drain(pool, **kw) -> IntakeDrain:
    resolve = _resolver(pool)

    async def _runner(req):
        return await run_request(req, pool=pool, resolve=resolve)

    return IntakeDrain(pool, runner=_runner, limits=kw.pop("limits", IntakeLimits(host_min_interval_s=0)), **kw)


def _addr(path: str, host: str = HOST) -> str:
    return f"https://{host}/{path}"


def _req(address: str | None = None, **kw: Any) -> SubmitRequest:
    base: dict[str, Any] = dict(target_kind="address", address=address, pipeline_source_id=PAGES,
                                reason="operator_request", requested_by="test:i1")
    base.update(kw)
    return SubmitRequest(**base)


# ---------------------------------------------------------------------------
# admission
# ---------------------------------------------------------------------------


async def test_every_refusal_is_recorded_with_its_reason_and_counted(pool) -> None:
    lim = IntakeLimits(fanout_per_parent=2, host_daily=1000, requester_daily=1000,
                       class_daily={k: 1000 for k in ("routine", "follow", "agent", "drift", "backfill", "operator")})
    parent = uuid.uuid4()
    cases = [
        ("egress_refused", _req("http://127.0.0.1/admin")),
        ("egress_refused", _req("ftp://files.example.org/x")),
        ("egress_refused", _req("http://metadata.internal/latest")),
        ("egress_refused", _req("https://user:pw@news.example.org/a")),
        ("unknown_pipeline", _req(_addr("a1"), pipeline_source_id="source.test.i1_nope")),
        ("pipeline_not_runnable", _req(_addr("a2"), pipeline_source_id=RETIRED)),
        ("pipeline_not_runnable", SubmitRequest(target_kind="source_window", source_id=PUSH,
                                                window_start=datetime.now(timezone.utc),
                                                reason="operator_request", requested_by="test:i1")),
        ("pipeline_not_address_capable", _req(_addr("a3"), pipeline_source_id=PUSH)),
        ("sink_mismatch", _req(_addr("a4"), sink="observation")),
        ("unknown_version", SubmitRequest(target_kind="stored_version", version_id=987654321,
                                          pipeline_source_id=PAGES, reason="drift_recheck",
                                          requested_by="test:i1")),
        ("depth_exceeded", _req(_addr("a5"), depth=3)),
        ("follow_not_allowed", _req(_addr("a6", "elsewhere.example.com"), reason="followed_reference",
                                    follow_source_id=FEED, follow_document_host=HOST,
                                    link_kind="anchor", depth=1)),
        ("follow_not_allowed", _req(_addr("a7"), reason="followed_reference", follow_source_id=NOFOLLOW,
                                    follow_document_host=HOST, link_kind="anchor", depth=1)),
        ("depth_exceeded", _req(_addr("a8"), reason="followed_reference", follow_source_id=FEED,
                                follow_document_host=HOST, link_kind="anchor", depth=2)),
    ]
    for want, req in cases:
        res = await submit_request(pool, req, limits=lim)
        assert (res.state, res.refusal_reason) == ("refused", want), (want, req, res)
    # duplicate pending
    first = await submit_request(pool, _req(_addr("dup")), limits=lim)
    second = await submit_request(pool, _req(_addr("dup")), limits=lim)
    assert first.state == "queued"
    assert (second.refusal_reason, second.duplicate_of) == ("duplicate_pending", first.id)
    # fan-out per parent (cap 2)
    outs = [await submit_request(pool, _req(_addr(f"child{i}"), parent_signal_id=parent), limits=lim)
            for i in range(3)]
    assert [o.state for o in outs] == ["queued", "queued", "refused"]
    assert outs[2].refusal_reason == "fanout_exceeded"
    # host / requester / class budgets
    tight = IntakeLimits(host_daily=1, requester_daily=1000, class_daily={**lim.class_daily})
    assert (await submit_request(pool, _req(_addr("h1", "budget.example.org")), limits=tight)).state == "queued"
    assert (await submit_request(pool, _req(_addr("h2", "budget.example.org")), limits=tight)).refusal_reason == "host_budget"
    tight = IntakeLimits(requester_daily=1, class_daily={**lim.class_daily})
    assert (await submit_request(pool, _req(_addr("r1"), requested_by="test:i1:r"), limits=tight)).state == "queued"
    assert (await submit_request(pool, _req(_addr("r2"), requested_by="test:i1:r"), limits=tight)).refusal_reason == "requester_budget"
    tight = IntakeLimits(class_daily={**lim.class_daily, "agent": 1})
    assert (await submit_request(pool, _req(_addr("c1"), reason="agent_request", requested_by="test:i1:c1"),
                                 limits=tight)).state == "queued"
    assert (await submit_request(pool, _req(_addr("c2"), reason="agent_request", requested_by="test:i1:c2"),
                                 limits=tight)).refusal_reason == "class_budget"

    async with pool.acquire() as conn:
        counts = await intake_counts(conn)
        stored = await conn.fetchval("SELECT count(*) FROM ingestion_requests WHERE state = 'refused'")
    assert counts["refusals"] == {
        "egress_refused": 4, "unknown_pipeline": 1, "pipeline_not_runnable": 2,
        "pipeline_not_address_capable": 1, "sink_mismatch": 1, "unknown_version": 1,
        "depth_exceeded": 2, "follow_not_allowed": 2, "duplicate_pending": 1, "fanout_exceeded": 1,
        "host_budget": 1, "requester_budget": 1, "class_budget": 1,
    }
    assert stored == sum(counts["refusals"].values())
    assert counts["queue"]["queued"] == 6
    assert counts["oldest_queued_age_s"] is not None


async def test_a_page_fetched_recently_is_served_from_the_stored_copy(pool, web) -> None:
    url = _addr("fresh")
    web.pages[url] = (200, _article("Fresh page"))
    # A poll of the page under the page pipeline lands a signal + a page version.
    core = SourceCore(f"source::{PAGES}::x", SourceDeps(
        descriptor=_desc(PAGES, "scraper", {"impl": IMPL, "seed_urls": [url], "max_depth": 0}),
        deps=StandardDeps(pg_pool=pool, nats_publish=NatsRecorder())))
    assert (await core.pull_once())["signals_written"] == 1
    hits = len(web.hits)

    receipt = await submit_request(pool, _req(url, sink="receipt"))
    document = await submit_request(pool, _req(url))
    assert (receipt.state, receipt.satisfied_from) == ("done", "stored_version")
    assert receipt.result_version_id is not None
    assert (document.state, document.satisfied_from) == ("done", "stored_signal")
    # Outside the freshness window it is a fetch again.
    stale = await submit_request(pool, _req(url, sink="receipt"),
                                 now=datetime.now(timezone.utc) + timedelta(hours=7))
    assert (stale.state, stale.satisfied_from) == ("queued", None)
    assert len(web.hits) == hits, "satisfying a request fetches nothing"
    row = await get_request(pool, document.id)
    assert row["result"]["stored_signal_id"] and row["finished_at"] is not None


async def test_daily_budgets_reset_at_the_utc_day(pool) -> None:
    lim = IntakeLimits(requester_daily=2)
    yesterday = datetime.now(timezone.utc).replace(hour=12, minute=0) - timedelta(days=1)
    for i in range(2):
        assert (await submit_request(pool, _req(_addr(f"y{i}"), requested_by="test:i1:day"),
                                     limits=lim, now=yesterday)).state == "queued"
    over = await submit_request(pool, _req(_addr("y2"), requested_by="test:i1:day"), limits=lim, now=yesterday)
    assert over.refusal_reason == "requester_budget"
    today = await submit_request(pool, _req(_addr("t0"), requested_by="test:i1:day"), limits=lim)
    assert today.state == "queued", "yesterday's spend does not count today"
    # refused and satisfied rows never spend
    assert (await submit_request(pool, _req(_addr("t1"), requested_by="test:i1:day"), limits=lim)).state == "queued"


# ---------------------------------------------------------------------------
# the queue
# ---------------------------------------------------------------------------


async def test_claim_order_priority_then_not_before_then_age(pool) -> None:
    now = datetime.now(timezone.utc)
    low = await submit_request(pool, _req(_addr("o1", "a.example.org"), priority=10))
    high = await submit_request(pool, _req(_addr("o2", "b.example.org"), priority=90))
    later = await submit_request(pool, _req(_addr("o3", "c.example.org"), priority=99,
                                            not_before=now + timedelta(hours=1)))
    mid = await submit_request(pool, _req(_addr("o4", "d.example.org"), priority=50))
    claimed = await claim_requests(pool, worker="t", limit=10, limits=IntakeLimits(host_min_interval_s=0))
    assert [c.id for c in claimed] == [high.id, mid.id, low.id]
    assert later.id not in {c.id for c in claimed}, "not before its not_before"


async def test_per_host_politeness_at_claim(pool) -> None:
    a1 = await submit_request(pool, _req(_addr("p1", "polite.example.org")))
    a2 = await submit_request(pool, _req(_addr("p2", "polite.example.org")))
    b1 = await submit_request(pool, _req(_addr("p3", "other.example.org")))
    lim = IntakeLimits(host_concurrency=1, host_min_interval_s=3600)
    first = await claim_requests(pool, worker="t", limit=10, limits=lim)
    assert {c.id for c in first} == {a1.id, b1.id}, "one per host"
    await complete_request(pool, first[0].id, first[0].claim_token, result={})
    await complete_request(pool, first[1].id, first[1].claim_token, result={})
    assert await claim_requests(pool, worker="t", limit=10, limits=lim) == [], "min interval holds"
    again = await claim_requests(pool, worker="t", limit=10, limits=IntakeLimits(host_min_interval_s=0))
    assert [c.id for c in again] == [a2.id]


async def test_a_dead_workers_claim_is_reclaimed_once_not_twice(pool) -> None:
    sub = await submit_request(pool, _req(_addr("crash")))
    lim = IntakeLimits(lease_s=0.05, host_min_interval_s=0)
    (dead,) = await claim_requests(pool, worker="dead", limit=1, limits=lim)
    await asyncio.sleep(0.2)
    got = await asyncio.gather(*(reclaim_expired_leases(pool) for _ in range(4)))
    assert sum(len(g) for g in got) == 1 and [i for g in got for i in g] == [sub.id]
    assert await reclaim_expired_leases(pool) == []
    # The dead worker wakes and tries to finish: refused (its claim was lost).
    assert await complete_request(pool, dead.id, dead.claim_token, result={"late": True}) is False
    assert await fail_request(pool, dead.id, dead.claim_token, error="late") is None
    (live,) = await claim_requests(pool, worker="live", limit=1, limits=IntakeLimits(host_min_interval_s=0))
    assert live.id == sub.id and live.claim_token != dead.claim_token
    assert await complete_request(pool, live.id, live.claim_token, result={"ok": True}) is True
    row = await get_request(pool, sub.id)
    assert (row["state"], row["attempts"], row["result"]) == ("done", 2, {"ok": True})


async def test_retry_policy_then_failed_and_expiry(pool) -> None:
    lim = IntakeLimits(max_attempts=2, host_min_interval_s=0, retry_base_s=30)
    sub = await submit_request(pool, _req(_addr("flaky")), limits=lim)
    (c,) = await claim_requests(pool, worker="t", limit=1, limits=lim)
    assert await fail_request(pool, c.id, c.claim_token, error="timeout", limits=lim) == "queued"
    row = await get_request(pool, sub.id)
    assert row["not_before"] > datetime.now(timezone.utc) + timedelta(seconds=20), "backed off"
    async with pool.acquire() as conn:
        await conn.execute("UPDATE ingestion_requests SET not_before = now() WHERE id = $1", sub.id)
    (c,) = await claim_requests(pool, worker="t", limit=1, limits=lim)
    assert await fail_request(pool, c.id, c.claim_token, error="timeout", limits=lim) == "failed"
    # permanent failure: no retry
    perm = await submit_request(pool, _req(_addr("perm")), limits=lim)
    (c,) = await claim_requests(pool, worker="t", limit=1, limits=lim)
    assert c.id == perm.id
    assert await fail_request(pool, c.id, c.claim_token, error="x", permanent=True, limits=lim) == "failed"
    # expiry
    old = await submit_request(pool, _req(_addr("old")), limits=IntakeLimits(expire_hours=0.0001))
    await asyncio.sleep(0.5)
    assert await expire_requests(pool) == 1
    assert (await get_request(pool, old.id))["state"] == "expired"


# ---------------------------------------------------------------------------
# the drain, end to end
# ---------------------------------------------------------------------------


async def test_the_drain_lands_a_page_as_the_poll_would_and_links_it(pool, web) -> None:
    url = _addr("e2e")
    web.pages[url] = (200, _article("End to end"))
    parent = uuid.uuid4()
    sub = await submit_request(pool, _req(url, parent_signal_id=parent, note="why"))
    drain = _drain(pool)
    assert await drain.tick() == 1
    assert (drain.stats.done, drain.stats.failed) == (1, 0)
    row = await get_request(pool, sub.id)
    assert row["state"] == "done", row
    assert row["result"]["signals_written"] == 1 and row["result"]["address_status"] == 200
    assert row["result_version_id"] is not None and row["parent_signal_id"] == parent
    (link,) = row["records"]
    async with pool.acquire() as conn:
        sig = await conn.fetchrow("SELECT source_id, canonical_url, payload FROM signals WHERE id = $1",
                                  uuid.UUID(link["id"]))
        vlink = await conn.fetchval("SELECT version_id FROM fetch_version_records WHERE record_id = $1 "
                                    "AND role = 'page'", uuid.UUID(link["id"]))
        state_rows = await conn.fetchval("SELECT count(*) FROM actor_filter_state WHERE actor_id = $1",
                                         f"intake::{sub.id}")
        outcomes = await conn.fetchval("SELECT count(*) FROM source_poll_outcomes WHERE source_id = $1", PAGES)
    assert sig["source_id"] == PAGES and sig["canonical_url"] == url
    assert vlink == row["result_version_id"]
    assert state_rows == 0, "the request's cursor/handler state is removed"
    assert outcomes == 0, "a request is not the source's scheduled poll"
    # A poll of the same page under the same source collapses into that row
    # (S-4 intra-source exact hash): the intake wrote THE row a poll writes.
    core = SourceCore(f"source::{PAGES}::x", SourceDeps(
        descriptor=_desc(PAGES, "scraper", {"impl": IMPL, "seed_urls": [url], "max_depth": 0}),
        deps=StandardDeps(pg_pool=pool, nats_publish=NatsRecorder())))
    res = await core.pull_once()
    assert res["signals_written"] == 0
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM signals WHERE source_id = $1", PAGES) == 1


async def test_a_404_is_done_and_gone_a_blocked_fetch_is_retried(pool, web, monkeypatch) -> None:
    gone = await submit_request(pool, _req(_addr("gone")))
    drain = _drain(pool)
    await drain.tick()
    row = await get_request(pool, gone.id)
    assert row["state"] == "done" and row["result"]["gone"] is True and row["result"]["address_status"] == 404
    # robots disallow: nothing fetched -> a transient failure under the retry policy
    from legba.data.sources import scraper as scraper_mod

    url = _addr("private", "robots.example.org")

    def _serve(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt" and request.url.host == "robots.example.org":
            return httpx.Response(200, content=b"User-agent: *\nDisallow: /\n")
        return web(request)

    monkeypatch.setattr(scraper_mod, "guarded_async_client", lambda **_kw: client_for(_serve))
    blocked = await submit_request(pool, _req(url))
    await drain.tick()
    row = await get_request(pool, blocked.id)
    assert row["state"] == "queued" and row["last_error"].startswith("not_fetched"), row
    assert url not in web.hits, "robots posture unchanged: the page is never fetched"


# ---------------------------------------------------------------------------
# producers
# ---------------------------------------------------------------------------


def _feed_xml() -> bytes:
    now = datetime.now(timezone.utc)
    items = (
        f"<item><title>Strike talks</title><link>https://{HOST}/a/1</link><guid>https://{HOST}/a/1</guid>"
        "<description><![CDATA[Short summary. See <a href='/a/2'>the earlier piece</a>, "
        f"<a href='https://{HOST}/docs/ruling.pdf'>the ruling</a> and "
        "<a href='https://offsite.example.com/x'>an offsite page</a>.]]></description>"
        f"<pubDate>{now.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>"
    )
    return (f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title><link>https://{HOST}</link>'
            f"<description>d</description>{items}</channel></rss>").encode()


async def _poll_feed(pool, monkeypatch) -> dict:
    from legba.data.sources.rss import RSSConfig, RSSSourceHandler

    body = _feed_xml()
    handler = RSSSourceHandler(RSSConfig(url=f"https://{HOST}/feed"), http_client=client_for(
        lambda r: httpx.Response(200, content=body, headers={"content-type": "application/rss+xml"})))
    core = SourceCore(f"source::{FEED}::x", SourceDeps(descriptor=_feed_desc(), handler=handler,
                      deps=StandardDeps(pg_pool=pool, nats_publish=NatsRecorder())))
    return await core.pull_once()


async def test_the_follow_step_submits_the_references_the_policy_allows(pool, web, monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_INTAKE_FOLLOW", raising=False)
    assert (await _poll_feed(pool, monkeypatch))["signals_written"] == 1
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM ingestion_requests") == 0, "switch off: nothing"
        await conn.execute("DELETE FROM signals WHERE source_id = $1", FEED)
        await conn.execute("DELETE FROM actor_filter_state WHERE filter_id = $1", f"source:{FEED}")
    monkeypatch.setenv("LEGBA_INTAKE_FOLLOW", "1")
    assert (await _poll_feed(pool, monkeypatch))["signals_written"] == 1
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT address, link_kind, state, depth, reason, pipeline_source_id, "
                                "requested_by, parent_signal_id FROM ingestion_requests ORDER BY created_at")
        sig = await conn.fetchval("SELECT id FROM signals WHERE source_id = $1", FEED)
    got = [(r["address"], r["link_kind"], r["state"]) for r in rows]
    assert got == [
        (f"https://{HOST}/a/1", "article", "queued"),
        (f"https://{HOST}/a/2", "anchor", "queued"),
        (f"https://{HOST}/docs/ruling.pdf", "document", "queued"),
    ], "the offsite anchor is outside the policy (same host only) and is not submitted"
    assert {(r["depth"], r["reason"], r["pipeline_source_id"], r["requested_by"], r["parent_signal_id"])
            for r in rows} == {(1, "followed_reference", PAGES, f"source::{FEED}", sig)}
    # A followed page's own references would be depth 2 > the policy's 1.
    deep = await submit_request(pool, _req(f"https://{HOST}/a/9", reason="followed_reference",
                                           follow_source_id=FEED, follow_document_host=HOST,
                                           link_kind="anchor", depth=2))
    assert deep.refusal_reason == "depth_exceeded"


async def test_the_follow_policy_is_validated_at_save() -> None:
    from pydantic import ValidationError

    from legba.data.schemas.source import SourceDescriptor

    body = _feed_desc().model_dump(mode="json")
    assert SourceDescriptor.model_validate(body, strict=False).follow.pipeline == PAGES
    for bad, msg in [
        ({**FOLLOW_POLICY, "kinds": ["image"]}, "declared but not built"),
        ({**FOLLOW_POLICY, "same_host": False, "hosts": []}, "follows nothing"),
        ({**FOLLOW_POLICY, "hosts": ["https://x.org/path"]}, "bare host"),
        ({**FOLLOW_POLICY, "max_depth": 9}, "less than or equal"),
        ({**FOLLOW_POLICY, "kinds": []}, "at least 1"),
        ({**FOLLOW_POLICY, "surprise": 1}, "Extra inputs"),
    ]:
        with pytest.raises(ValidationError, match=msg):
            SourceDescriptor.model_validate({**body, "follow": bad}, strict=False)
    # absent = hash-neutral: no key in the dump
    plain = descriptor("source.test.i1_plain", "rss", {"url": "https://x.org/f"})
    assert "follow" not in plain.model_dump(mode="json")


async def test_the_drift_batch_rechecks_cited_pages_and_records_the_change(pool, web, monkeypatch) -> None:
    url = _addr("cited")
    web.pages[url] = (200, _article("Original wording"))
    core = SourceCore(f"source::{PAGES}::x", SourceDeps(
        descriptor=_desc(PAGES, "scraper", {"impl": IMPL, "seed_urls": [url], "max_depth": 0}),
        deps=StandardDeps(pg_pool=pool, nats_publish=NatsRecorder())))
    await core.pull_once()
    async with pool.acquire() as conn:
        sig = await conn.fetchval("SELECT id FROM signals WHERE source_id = $1", PAGES)
        await conn.execute("INSERT INTO evidence_archive (signal_id, status) VALUES ($1, 'archived')", sig)
        old = datetime.now(timezone.utc) - timedelta(days=8)
        await conn.execute("UPDATE fetch_versions SET first_seen_at = $1, last_seen_at = $1", old)
        version_id = await conn.fetchval("SELECT id FROM fetch_versions WHERE role = 'page'")
    from legba.runtime import intake_drift

    monkeypatch.setattr(intake_drift, "_last_batch_at", None)
    monkeypatch.setenv("LEGBA_INTAKE_DRIFT_PIPELINE", PAGES)
    assert await intake_drift.drift_batch_if_due(pool) is True
    assert await intake_drift.drift_batch_if_due(pool) is False, "once per cadence"
    async with pool.acquire() as conn:
        (req,) = await conn.fetch("SELECT id, target_kind, version_id, sink, reason, budget_class "
                                  "FROM ingestion_requests")
    assert (req["target_kind"], req["version_id"], req["sink"], req["reason"], req["budget_class"]) == \
        ("stored_version", version_id, "receipt", "drift_recheck", "drift")
    web.pages[url] = (200, _article("Revised wording after publication"))
    drain = _drain(pool)
    assert await drain.tick() == 1
    row = await get_request(pool, req["id"])
    assert row["state"] == "done", row
    assert row["result"]["changed"] is True and row["result"]["drift_observed"] == 1
    async with pool.acquire() as conn:
        drift = await conn.fetchrow("SELECT earlier_version_id, later_version_id, later_gone "
                                    "FROM fetch_drift_observations")
        n_signals = await conn.fetchval("SELECT count(*) FROM signals WHERE source_id = $1", PAGES)
    assert (drift["earlier_version_id"], drift["later_version_id"], drift["later_gone"]) == \
        (version_id, row["result_version_id"], False)
    assert n_signals == 1, "a receipt re-check writes no record"


def _pack():
    from legba.data.schemas.action_pack import ActionPack

    body = yaml.safe_load((REPO / "descriptors" / "action_pack_ingestion_intake.yaml").read_text())
    body["identity"]["version"] = "b" * 16
    return ActionPack.model_validate(body, strict=False)


def _binding(pool, analyst_id: str):
    from legba.data.analysts.agency import Agency, AgencyToolBinding, ToolContext
    from legba.data.analysts.agency.binding import GLOBAL_SCOPE
    from legba.data.analysts.agency.tools import WritebackContext
    from legba.data.provenance import AnalystContext

    wb = WritebackContext(pg_pool=pool, analyst_ctx=AnalystContext(
        analyst_id=analyst_id, analyst_version="0" * 16, run_id=uuid.uuid4()))
    return AgencyToolBinding(
        agency=Agency(), pack=_pack(), pg_pool=pool, tool_context=ToolContext(writeback=wb),
        analyst_grants=[{"pack_id": "ingestion_intake"}], target_allows=[{"pack_id": "ingestion_intake"}],
        scope=GLOBAL_SCOPE, requested_by=f"analyst::{analyst_id}", budget_account="ingestion_intake",
    )


async def test_the_agent_tool_through_the_real_binding(pool, monkeypatch) -> None:
    from legba.data.analysts.agency.intake_tools import INTAKE_TOOLS, register_intake_tools

    class _Reg:
        names: list[str] = []

        def register(self, name, _fn):
            self.names.append(name)

    reg = _Reg()
    register_intake_tools(reg)
    assert tuple(reg.names) == INTAKE_TOOLS == tuple(t.name for t in _pack().tools)

    async with pool.acquire() as conn:
        marker = {r["id"] for r in await conn.fetch("SELECT id FROM action_pack_invocations")}
    binding = _binding(pool, "test_i1_analyst")
    monkeypatch.delenv("LEGBA_INTAKE_AGENT_REQUESTS", raising=False)
    off = await binding.run_tool("request_ingestion", {"address": _addr("agent"), "pipeline": PAGES})
    assert off.tool_result.status == "failed" and "intake_agent_requests_off" in off.tool_result.error
    monkeypatch.setenv("LEGBA_INTAKE_AGENT_REQUESTS", "1")
    ok = await binding.run_tool("request_ingestion", {"address": _addr("agent"), "pipeline": PAGES,
                                                     "note": "the ruling the desk cites"})
    assert ok.admitted is True and ok.tool_result.status == "completed", ok.tool_result.error
    rid = ok.tool_result.output["id"]
    assert ok.tool_result.output["status"] == "queued"
    refused = await binding.run_tool("request_ingestion", {"address": "http://10.0.0.5/x", "pipeline": PAGES})
    assert refused.tool_result.output["status"] == "refused"
    assert refused.tool_result.output["refusal_reason"] == "egress_refused"
    status = await binding.run_tool("ingestion_request_status", {"id": rid})
    assert status.tool_result.output["state"] == "queued"
    assert status.tool_result.output["reason"] == "agent_request"
    other = _binding(pool, "test_i1_other")
    fenced = await other.run_tool("ingestion_request_status", {"id": rid})
    assert fenced.tool_result.status == "failed", "another analyst cannot read it"
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT requested_by, on_behalf_of, budget_class FROM ingestion_requests "
                                  "WHERE id = $1", uuid.UUID(rid))
        invs = await conn.fetch("SELECT id, pack_id, tool_name, outcome FROM action_pack_invocations "
                                "ORDER BY occurred_at, id")
    assert (row["requested_by"], row["on_behalf_of"], row["budget_class"]) == \
        ("analyst::test_i1_analyst", None, "agent")
    new = [(r["pack_id"], r["tool_name"], r["outcome"]) for r in invs if r["id"] not in marker]
    assert ("ingestion_intake", "request_ingestion", "completed") in new
    assert ("ingestion_intake", "ingestion_request_status", "completed") in new


async def test_the_archivers_refetch_routed_through_the_intake(pool, web, tmp_path) -> None:
    from legba.data.analysts.deterministic_handlers._intake_route import intake_copy_or_defer
    from legba.data.archive import archive_root

    url = _addr("cited-by-a-finding")
    web.pages[url] = (200, _article("Archived via the intake"))
    sig = uuid.uuid4()
    root = archive_root()
    verdict, rid = await intake_copy_or_defer(pool, sig, url, pipeline=PAGES, root=root)
    assert verdict == "defer"
    again, rid2 = await intake_copy_or_defer(pool, sig, url, pipeline=PAGES, root=root)
    assert (again, rid2) == ("defer", rid), "one request per signal, not one per tick"
    await _drain(pool).tick()
    verdict, copy = await intake_copy_or_defer(pool, sig, url, pipeline=PAGES, root=root)
    assert verdict == "copy" and copy[0] == web.pages[url][1] and copy[1].startswith("text/html")
    row = await get_request(pool, rid)
    assert (row["reason"], row["sink"], row["requested_by"], row["parent_signal_id"]) == \
        ("backfill", "receipt", "evidence_archiver", sig)
    # an impersonation host keeps the archiver's own two-leg fetch
    v, why = await intake_copy_or_defer(pool, uuid.uuid4(), "https://www.timesofisrael.com/x", pipeline=PAGES,
                                        root=root)
    assert v == "fallback" and "fetch policy" in why
    # pending past the wait: the archiver fetches itself
    stuck = uuid.uuid4()
    await intake_copy_or_defer(pool, stuck, _addr("stuck"), pipeline=PAGES, root=root)
    v, _ = await intake_copy_or_defer(pool, stuck, _addr("stuck"), pipeline=PAGES, root=root,
                                      now=datetime.now(timezone.utc) + timedelta(hours=2))
    assert v == "fallback"


# ---------------------------------------------------------------------------
# the event loop
# ---------------------------------------------------------------------------


async def test_a_drain_tick_over_large_pages_never_holds_the_loop(pool, web) -> None:
    """Four ~250 KB article pages (the large end of a real article's HTML)
    run concurrently through one tick while a heartbeat ticks every 10 ms.

    A warm-up request first: the first run in a process imports every source
    kind and trafilatura (seconds, once — the actor plane has done it by the
    time a production drain starts). Page-text extraction runs in worker
    threads (the scraper kind's and the receipt writer's); what the loop can
    still feel is lxml holding the GIL inside those threads — measured at
    about 0.1 s for a 100 KB page and about 0.5 s for a 650 KB one, against
    the live loop-lag monitor's 2 s warning threshold."""
    warm = _addr("warm", "warm.example.org")
    web.pages[warm] = (200, _article("Warm-up", n=50))
    await submit_request(pool, _req(warm, sink="receipt"))
    drain = _drain(pool, batch=4)
    assert await drain.tick() == 1
    for i in range(4):
        url = _addr(f"big{i}", f"big{i}.example.org")
        web.pages[url] = (200, _article(f"Big page {i}", n=1500))
        await submit_request(pool, _req(url))
    gaps: list[float] = []
    stop = asyncio.Event()

    async def _heartbeat() -> None:
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    hb = asyncio.create_task(_heartbeat())
    t0 = time.monotonic()
    assert await drain.tick() == 4
    took = time.monotonic() - t0
    stop.set()
    await hb
    assert drain.stats.done == 5, drain.stats
    print(f"\n[heartbeat] tick {took:.2f}s over 4 pages of {len(web.pages[_addr('big0', 'big0.example.org')][1])} "
          f"bytes; max loop gap {max(gaps):.3f}s over {len(gaps)} beats")
    assert max(gaps) < 0.5, f"the loop was held {max(gaps):.2f}s"


# ---------------------------------------------------------------------------
# plans at a realistic size (inside a rolled-back transaction)
# ---------------------------------------------------------------------------

_PLAN_QUERIES = {
    "claim": ("SELECT r.* FROM ingestion_requests r WHERE r.state = 'queued' AND r.not_before <= now() "
              "AND (r.expires_at IS NULL OR r.expires_at > now()) AND (r.host IS NULL OR ("
              "(SELECT count(*) FROM ingestion_requests c WHERE c.state = 'claimed' AND c.host = r.host) < 1 "
              "AND COALESCE((SELECT max(d.claimed_at) FROM ingestion_requests d WHERE d.host = r.host "
              "AND d.claimed_at IS NOT NULL), '-infinity') <= now() - make_interval(secs => 10))) "
              "ORDER BY r.priority DESC, r.not_before, r.created_at LIMIT 1 FOR UPDATE OF r SKIP LOCKED"),
    "pending_duplicate": ("SELECT id FROM ingestion_requests WHERE dedupe_key = 'address|https://h7.example.org/p/5|"
                          "source.test.i1_pages|document' AND state IN ('queued', 'claimed') LIMIT 1"),
    "requester_budget": ("SELECT count(*) FROM ingestion_requests WHERE requested_by = 'analyst::a3' "
                         "AND created_at >= date_trunc('day', now()) AND state <> 'refused' AND satisfied_from IS NULL"),
    "class_budget": ("SELECT count(*) FROM ingestion_requests WHERE budget_class = 'agent' "
                     "AND created_at >= date_trunc('day', now()) AND state <> 'refused' AND satisfied_from IS NULL"),
    "host_budget": ("SELECT count(*) FROM ingestion_requests WHERE host = 'h7.example.org' "
                    "AND created_at >= date_trunc('day', now()) AND state <> 'refused' AND satisfied_from IS NULL"),
    "fanout": ("SELECT count(*) FROM ingestion_requests WHERE state <> 'refused' AND parent_signal_id = "
               "'00000000-0000-0000-0000-000000000007'"),
    "reclaim": "SELECT id FROM ingestion_requests WHERE state = 'claimed' AND lease_expires_at < now()",
    "expire": "SELECT id FROM ingestion_requests WHERE state = 'queued' AND expires_at <= now()",
}


async def test_query_plans_at_100k_requests(pool) -> None:
    from legba.data import intake_visibility as V

    plans: dict[str, str] = {}
    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute("""
                INSERT INTO ingestion_requests (id, target_kind, address, address_canonical, host,
                    pipeline_source_id, sink, reason, budget_class, requested_by, parent_signal_id, depth,
                    priority, not_before, expires_at, dedupe_key, state, refusal_reason, claim_token,
                    lease_expires_at, claimed_at, created_at, updated_at)
                SELECT gen_random_uuid(), 'address', 'https://h' || (g % 500) || '.example.org/p/' || g,
                       'https://h' || (g % 500) || '.example.org/p/' || g, 'h' || (g % 500) || '.example.org',
                       'source.test.i1_pages', 'document',
                       (ARRAY['scheduled','followed_reference','agent_request','drift_recheck','backfill'])[1 + g % 5],
                       (ARRAY['routine','follow','agent','drift','backfill'])[1 + g % 5],
                       'analyst::a' || (g % 50),
                       CASE WHEN g % 3 = 0 THEN ('00000000-0000-0000-0000-' || lpad((g % 5000)::text, 12, '0'))::uuid END,
                       g % 3, g % 100, now() - make_interval(secs => g), now() + interval '1 day',
                       'k' || g,
                       CASE WHEN g % 10 < 6 THEN 'done' WHEN g % 10 < 8 THEN 'refused'
                            WHEN g % 10 = 8 THEN 'queued' ELSE 'failed' END,
                       CASE WHEN g % 10 IN (6, 7) THEN 'host_budget' END, NULL, NULL,
                       CASE WHEN g % 10 < 6 THEN now() - make_interval(secs => g) END,
                       now() - make_interval(secs => g * 3), now()
                  FROM generate_series(1, 100000) g
            """)
            await conn.execute("ANALYZE ingestion_requests")
            for name, sql in _PLAN_QUERIES.items():
                rows = await conn.fetch("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) " + sql)
                plans[name] = "\n".join(r[0] for r in rows)
            for name, sql in (("visibility_states", V._STATE_SQL), ("visibility_hosts", V._HOST_SQL)):
                args = (24,) if name == "visibility_states" else (24, 20)
                rows = await conn.fetch("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) " + sql, *args)
                plans[name] = "\n".join(r[0] for r in rows)
            t0 = time.monotonic()
            counts = await V.intake_counts(conn)
            plans["intake_counts_ms"] = f"{(time.monotonic() - t0) * 1000:.1f} ms; queue={counts['queue']}"
        finally:
            await tx.rollback()
    for name, plan in plans.items():
        print(f"\n===== {name} =====\n{plan}")
    assert "ingestion_requests_claim_idx" in plans["claim"]
    assert "ingestion_requests_host_claimed_idx" in plans["claim"] and "Seq Scan" not in plans["claim"]
    assert "ingestion_requests_pending_key" in plans["pending_duplicate"]
    for budget in ("requester_budget", "class_budget", "host_budget"):
        assert "Seq Scan" not in plans[budget], plans[budget]
    assert "ingestion_requests_requester_idx" in plans["requester_budget"]
    assert "ingestion_requests_host_created_idx" in plans["host_budget"]
    assert "ingestion_requests_parent_signal_idx" in plans["fanout"]
    assert "ingestion_requests_lease_idx" in plans["reclaim"]
    assert "ingestion_requests_expiry_idx" in plans["expire"]


async def test_the_read_route_over_the_real_table(pool) -> None:
    from types import SimpleNamespace

    from fastapi import FastAPI

    from legba.data.registry.api import require_bearer
    from legba.data.registry.intake_api import build_intake_router

    await submit_request(pool, _req(_addr("route1", "r1.example.org")))
    await submit_request(pool, _req("http://127.0.0.1/x"))
    app = FastAPI()
    app.include_router(build_intake_router(SimpleNamespace(descriptor_registry=SimpleNamespace(pg=pool))),
                       prefix="/api/v1/v3")
    app.dependency_overrides[require_bearer] = lambda: "test"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        body = (await client.get("/api/v1/v3/ingestion/intake?window_hours=6")).json()
    assert body["available"] is True and body["window_hours"] == 6
    assert body["queue"] == {"queued": 1, "refused": 1}
    assert body["by_reason"] == {"operator_request": {"queued": 1, "refused": 1}}
    assert body["refusals"] == {"egress_refused": 1}
    assert {h["host"] for h in body["hosts"]} == {"r1.example.org", "127.0.0.1"}
    assert body["oldest_queued_age_s"] >= 0
