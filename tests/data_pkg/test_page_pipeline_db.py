# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The page pipeline on the REAL path against a migrated Postgres
(the data_pkg session database; isolated test stack only).

A page requested through the intake is drained, fetched by the ``page`` kind
(on an in-memory web — synthetic pages, no real site), and lands:

* as a NEW document that went through the keyword gate, the enrichment chain
  (the real geocoder with its network backend stubbed, and the host's
  promote step), the host credibility ledger (by the page's host) and the
  publisher match (a registered feed on the same host lends its
  ``scope.geo`` as ``publisher_origin``), and that a country desk's slice
  reads like any document; linked to its request and its page version;
* or COLLAPSED into a document already holding the full text (a full-text
  feed entry for the same URL): no new row; the request and the page
  version are linked to the existing signal, and the next request inside
  the freshness window is satisfied from it without a fetch;
* or FILLED IN onto a summary-only feed entry: ``payload.archived_text`` on
  the existing row (the content-depth model the evidence archiver uses),
  the feed's own ``raw_body``/``fetched_at`` untouched, the corpus dirty
  marker set, and still one row for the URL.

And: an unknown host gets the declared default credibility, flagged; a
refused page (soft 404) keeps its receipt and writes no signal; a host the
licence ledger refuses is never fetched; a ``configured`` page pipeline takes
address requests but not source-window re-polls.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import asyncpg
import httpx
import pycountry
import pytest
import pytest_asyncio

from legba.data.filters._contract import FilterContext
from legba.data.filters._geo_precedence import promote_payload_columns
from legba.data.filters.geocode import GeocodeConfig, GeocodeHandler, GeocodeResult
from legba.data.intake_contract import IntakeLimits
from legba.data.intake_store import SubmitRequest, get_request, submit_request
from legba.data.schemas.source import FilterStage
from legba.data.sources import page as page_mod
from legba.runtime.actor_substrate_slice import _read_substrate_slice
from legba.runtime.deps import StandardDeps
from legba.runtime.intake_drain import IntakeDrain
from legba.runtime.intake_run import run_request
from legba.runtime.source_actor import SourceCore, SourceDeps

from tests.data_pkg import _page_fixtures as P
from tests.runtime._ingest_fakes import NatsRecorder, client_for, descriptor

pytestmark = pytest.mark.asyncio

PAGES = "source.test.i2_pages"
GATED = "source.test.i2_pages_gated"
FEED = "source.test.i2_feed"
HOST = "news.i2.example.org"
TARGET = "target.test.i2_nl_desk"
_IDS = (PAGES, GATED, FEED)


def _url(path: str, host: str = HOST) -> str:
    return f"https://{host}/2026/11/13/{path}"


def _dutch_article(title: str = "Port strike in the Netherlands widens to Antwerp", **kw: Any) -> bytes:
    return P.article(title=title, **kw)


# ---------------------------------------------------------------------------
# rig
# ---------------------------------------------------------------------------


class _Geo:
    name = "stub"

    async def geocode(self, query):
        try:
            iso = pycountry.countries.lookup(query).alpha_2
        except LookupError:
            return None
        return GeocodeResult(country=query, country_iso2=iso, country_iso3=None, region=None,
                             municipality=None, address=None, lat=52.0, lon=4.5,
                             precision="country", source="stub")

    async def reachable(self):
        return True


async def _enrich(signal, ctx):
    """The enrichment stage the host builds for ``geocode`` (network backend
    stubbed) followed by the host's promote step — what dapr_host's stage
    runs."""
    handler = GeocodeHandler(GeocodeConfig(precision="country"), backend=_Geo())
    out = await handler.transform(signal, FilterContext(
        target_id=ctx.source_id, scope_geo=list(ctx.scope_geo), scope_languages=[], source_class="reporting"))
    promote_payload_columns(out)
    return out


def _desc(source_id: str, kind: str, config: dict, *, gate: list[str] | None = None,
          geo: list[str] | None = None) -> Any:
    d = descriptor(source_id, kind, config)
    if gate:
        d.pipeline.ingestion_filters.append(FilterStage(kind="keyword_gate", config={"include": gate}))
    if geo is not None:
        d = d.model_copy(update={"scope": d.scope.model_copy(update={"geo": geo})})
    return d


def _pages_desc() -> Any:
    return _desc(PAGES, "page", {})


async def _put(conn, desc: Any, state: str) -> None:
    body = desc.model_dump(mode="json")
    body["identity"]["state"] = state
    await conn.execute(
        "INSERT INTO source_descriptors (descriptor_id, version, schema_uri, is_head, kind, state, "
        "owner, name, body) VALUES ($1, $2, $3, TRUE, $4, $5, 'test:i2', $6, $7::jsonb)",
        desc.identity.id, desc.identity.version, desc.identity.schema_uri, desc.identity.kind,
        state, desc.identity.name, json.dumps(body),
    )


@pytest.fixture
def web(monkeypatch, tmp_path) -> P.Web:
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("LEGBA_FETCH_RECEIPTS", "1")
    w = P.Web()
    monkeypatch.setattr(page_mod, "guarded_async_client", lambda **kw: P.client(w, **kw))
    from legba.runtime import page_landing

    page_landing._PUBLISHERS.update(at=0.0, by_host={})   # no cache across tests
    return w


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
    await c.execute("DELETE FROM signal_aliases WHERE alias_signal_id IN "
                    "(SELECT id FROM signals WHERE source_id = ANY($1::text[]))", list(_IDS))
    await c.execute("DELETE FROM signals WHERE source_id = ANY($1::text[])", list(_IDS))
    await c.execute("DELETE FROM source_poll_outcomes WHERE source_id = ANY($1::text[])", list(_IDS))
    await c.execute("DELETE FROM actor_filter_state WHERE actor_id LIKE 'intake::%' "
                    "OR filter_id = ANY($1::text[])", [f"source:{s}" for s in _IDS])
    await c.execute("DELETE FROM source_descriptors WHERE descriptor_id = ANY($1::text[])", list(_IDS))
    await c.execute("DELETE FROM target_descriptors WHERE descriptor_id = $1", TARGET)
    await c.execute("DELETE FROM source_credibility WHERE source_host LIKE '%i2.example.org'")


@pytest_asyncio.fixture
async def pool(migrated_pg):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=8)
    async with p.acquire() as c:
        from legba.runtime.state import SCHEMA

        await c.execute(SCHEMA)
        await _scrub(c)
        await _put(c, _pages_desc(), "configured")
        await _put(c, _desc(GATED, "page", {}, gate=["dockworkers"]), "configured")
        await _put(c, _desc(FEED, "rss", {"url": f"https://{HOST}/feed"}, geo=["NL"]), "active")
        await c.execute(
            "INSERT INTO source_credibility (source_host, score, score_rationale, scored_by, license_class) "
            "VALUES ($1, 0.72, 'i2 test', 'test:i2', NULL), ($2, 0.5, 'i2 test', 'test:i2', 'anti_ai_walled')",
            HOST, "walled.i2.example.org")
        await c.execute(
            "INSERT INTO target_descriptors (descriptor_id, version, schema_uri, is_head, state, owner, "
            "name, body) VALUES ($1, $2, 'legba/target/1.0.0', TRUE, 'active', 'test:i2', 'NL desk', $3::jsonb)",
            TARGET, "c" * 16, json.dumps({"scope": {"geo": ["NL"]}}))
    try:
        yield p
    finally:
        # Leave the session database as found. A drained request keeps its
        # `ingestion_requests.result_version_id` -> `fetch_versions` reference;
        # left behind, every sibling file whose setup resets `fetch_versions`
        # (test_ingestion_followups_n_r, test_series_sources_acceptance) hit
        # the FK and errored at setup (shuffled nightly seeds 890480541,
        # 406219133, 1026012366).
        async with p.acquire() as c:
            await _scrub(c)
        await p.close()


def _resolver(pool) -> Any:
    deps = StandardDeps(pg_pool=pool, nats_publish=NatsRecorder())
    descs = {PAGES: _pages_desc(), GATED: _desc(GATED, "page", {}, gate=["dockworkers"])}

    async def _resolve(sid: str) -> Any:
        return SourceDeps(descriptor=descs[sid], deps=deps, enrichment_stage=_enrich) if sid in descs else None

    return _resolve


async def _drain_one(pool, address: str, *, pipeline: str = PAGES, **kw: Any) -> Any:
    res = await submit_request(pool, SubmitRequest(
        target_kind="address", address=address, pipeline_source_id=pipeline,
        reason=kw.pop("reason", "operator_request"), requested_by="test:i2", **kw))
    if res.state != "queued":
        return res, await get_request(pool, res.id)
    resolve = _resolver(pool)

    async def _runner(req):
        return await run_request(req, pool=pool, resolve=resolve)

    drain = IntakeDrain(pool, runner=_runner, limits=IntakeLimits(host_min_interval_s=0))
    assert await drain.tick() == 1
    return res, await get_request(pool, res.id)


async def _poll_feed(pool, monkeypatch, entry_body: str, link: str) -> Any:
    from legba.data.sources import rss as rss_mod

    feed = (
        "<?xml version='1.0'?><rss version='2.0' xmlns:content='http://purl.org/rss/1.0/modules/content/'>"
        f"<channel><title>i2</title><link>https://{HOST}/</link><description>x</description><item>"
        f"<title>Port strike in the Netherlands widens to Antwerp</title><link>{link}</link>"
        f"<guid>{link}</guid><pubDate>Fri, 13 Nov 2026 08:00:00 GMT</pubDate>"
        f"<description>Dockworkers extend the stoppage.</description>{entry_body}</item></channel></rss>"
    ).encode()
    real_init = rss_mod.RSSSourceHandler.__init__

    def _init(self, config, *a, **kw):
        kw["http_client"] = client_for(lambda r: httpx.Response(200, content=feed))
        real_init(self, config, *a, **kw)

    monkeypatch.setattr(rss_mod.RSSSourceHandler, "__init__", _init)
    core = SourceCore(f"source::{FEED}::x", SourceDeps(
        descriptor=_desc(FEED, "rss", {"url": f"https://{HOST}/feed"}, geo=["NL"]),
        deps=StandardDeps(pg_pool=pool, nats_publish=NatsRecorder())))
    out = await core.pull_once()
    assert out["signals_written"] == 1, out
    return uuid.UUID(out["signal_ids"][0])


async def _signals_for(pool, url: str) -> list:
    from legba.data._url_canon import canonical_url_hash

    async with pool.acquire() as c:
        return await c.fetch("SELECT * FROM signals WHERE canonical_url_hash = $1 ORDER BY fetched_at",
                             canonical_url_hash(url))


def _payload(row) -> dict:
    p = row["payload"]
    return json.loads(p) if isinstance(p, str) else p


# ---------------------------------------------------------------------------
# admission
# ---------------------------------------------------------------------------


async def test_a_configured_page_pipeline_takes_addresses_not_window_repolls(pool) -> None:
    from datetime import datetime, timezone

    ok = await submit_request(pool, SubmitRequest(target_kind="address", address=_url("adm"),
                                                  pipeline_source_id=PAGES, reason="operator_request",
                                                  requested_by="test:i2"))
    assert ok.state == "queued", ok
    window = await submit_request(pool, SubmitRequest(target_kind="source_window", source_id=PAGES,
                                                      window_start=datetime.now(timezone.utc),
                                                      reason="operator_request", requested_by="test:i2"))
    assert (window.state, window.refusal_reason) == ("refused", "pipeline_not_runnable")


# ---------------------------------------------------------------------------
# a new document, end to end
# ---------------------------------------------------------------------------


async def test_a_requested_page_lands_as_a_document_a_desk_reads(pool, web) -> None:
    url = web.put(_url("port-strike"), _dutch_article())
    parent = uuid.uuid4()
    res, row = await _drain_one(pool, url, parent_signal_id=parent)
    assert row["state"] == "done", row
    result = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"])
    assert result["signals_written"] == 1 and result["landing"][0]["action"] == "new"
    sigs = await _signals_for(pool, url)
    assert len(sigs) == 1
    sig = sigs[0]
    p = _payload(sig)
    assert sig["source_id"] == PAGES
    assert p["title"].startswith("Port strike in the Netherlands") and len(p["raw_body"]) > 1500
    assert p["published_at"] == "2026-11-13T08:00:00+00:00"
    # enrichment + geocode: the body's country reached the indexed column
    assert "NL" in list(sig["geo"] or []), (sig["geo"], p.get("geo"))
    # host credibility from the ledger, by the PAGE's host
    assert sig["source_credibility"] == pytest.approx(0.72)
    assert p["page"]["credibility"]["basis"] == "ledger"
    # the publisher: the registered feed on the same host
    assert p["publisher"]["registered_source_id"] == FEED and p["publisher_origin"] == ["NL"]
    # provenance: which request, which parent
    assert p["intake"]["request_id"] == str(res.id) and p["intake"]["parent_signal_id"] == str(parent)
    async with pool.acquire() as c:
        linked = await c.fetch("SELECT record_id FROM ingestion_request_records WHERE request_id = $1", res.id)
        page_link = await c.fetchrow(
            "SELECT r.role, v.role AS vrole, v.extracted_text FROM fetch_version_records r "
            "JOIN fetch_versions v ON v.id = r.version_id WHERE r.record_id = $1", sig["id"])
        rows = await _read_substrate_slice(
            # a non-inline kind, so the desk-grounding leg stays out of the way
            c, descriptor=SimpleNamespace(identity=SimpleNamespace(id="a.test.i2", kind="predictor"),
                                          subscription=SimpleNamespace(
                                              substrate={}, targets=SimpleNamespace(time_window="24h"))),
            target_filter=TARGET)
    assert [r["record_id"] for r in linked] == [sig["id"]]
    assert page_link["role"] == "page" and page_link["vrole"] == "page"
    assert page_link["extracted_text"] == p["raw_body"], "the receipt holds the same text"
    assert sig["id"] in {r["id"] for r in rows}, "the NL desk's slice reads it"


async def test_the_pipelines_keyword_gate_drops_an_off_topic_page(pool, web) -> None:
    on = web.put(_url("on-topic"), _dutch_article())
    off = web.put(_url("off-topic"), P.article(title="Football final", paras=10).replace(
        b"dockworkers", b"supporters"))
    _, row_on = await _drain_one(pool, on, pipeline=GATED)
    _, row_off = await _drain_one(pool, off, pipeline=GATED)
    assert (row_on["state"], row_off["state"]) == ("done", "done")
    assert len(await _signals_for(pool, on)) == 1
    assert await _signals_for(pool, off) == []


async def test_an_unknown_host_gets_the_declared_default_flagged(pool, web) -> None:
    url = web.put(_url("elsewhere", "unknown.i2.example.org"), _dutch_article())
    await _drain_one(pool, url)
    sig = (await _signals_for(pool, url))[0]
    p = _payload(sig)
    assert sig["source_credibility"] == pytest.approx(0.3)
    assert p["page"]["credibility"] == {"basis": "declared_default", "host": "unknown.i2.example.org",
                                        "flag": "unknown_host", "score": 0.3}
    assert "registered_source_id" not in p["publisher"] and "publisher_origin" not in p


# ---------------------------------------------------------------------------
# duplicates and content depth
# ---------------------------------------------------------------------------


async def test_a_page_already_held_in_full_collapses_into_the_existing_row(pool, web, monkeypatch) -> None:
    from legba.data.sources.page_text import extract_main_text

    body = _dutch_article()
    url = web.put(_url("held-in-full"), body)
    text = extract_main_text(body, "utf-8", max_chars=200_000)
    feed_sig = await _poll_feed(pool, monkeypatch, f"<content:encoded><![CDATA[{text}]]></content:encoded>", url)
    res, row = await _drain_one(pool, url)
    result = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"])
    assert row["state"] == "done" and result["signals_written"] == 0
    assert result["landing"][0]["action"] == "collapsed"
    assert result["landing"][0]["existing_signal_id"] == str(feed_sig)
    sigs = await _signals_for(pool, url)
    assert [s["id"] for s in sigs] == [feed_sig], "no duplicate row"
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT record_id FROM ingestion_request_records WHERE request_id = $1",
                                res.id) == feed_sig
        assert await c.fetchval("SELECT count(*) FROM fetch_version_records WHERE record_id = $1 "
                                "AND role = 'page'", feed_sig) == 1
    # inside the freshness window the next request is served from the stored copy
    again = await submit_request(pool, SubmitRequest(target_kind="address", address=url, pipeline_source_id=PAGES,
                                                     reason="operator_request", requested_by="test:i2"))
    assert (again.state, again.satisfied_from) == ("done", "stored_signal")


async def test_a_summary_only_feed_entry_is_filled_in_not_duplicated(pool, web, monkeypatch) -> None:
    url = web.put(_url("teaser"), _dutch_article())
    feed_sig = await _poll_feed(pool, monkeypatch, "", url)
    before = (await _signals_for(pool, url))[0]
    res, row = await _drain_one(pool, url, parent_signal_id=feed_sig)
    result = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"])
    assert result["landing"][0]["action"] == "filled", result
    sigs = await _signals_for(pool, url)
    assert len(sigs) == 1 and sigs[0]["id"] == feed_sig
    after = sigs[0]
    p = _payload(after)
    assert len(p["archived_text"]) > 1500 and p["archived_text_chars"] == len(p["archived_text"])
    assert p["page_fill"]["intake"]["request_id"] == str(res.id)
    assert p["page_fill"]["intake"]["parent_signal_id"] == str(feed_sig)
    assert p["raw_body"] == _payload(before)["raw_body"], "what the feed said is kept"
    assert after["fetched_at"] == before["fetched_at"] and after["content_hash"] == before["content_hash"]
    assert after["indexed_at"] is None and after["updated_at"] >= before["updated_at"]
    # the follow step now sees the article behind the teaser
    from legba.runtime.intake_follow import extract_references

    assert extract_references(SimpleNamespace(payload=p, canonical_url=url), ["article"]) == []


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


async def test_a_refused_page_keeps_its_receipt_and_writes_no_signal(pool, web) -> None:
    url = web.put(_url("gone-soft"), P.soft_404())
    _, row = await _drain_one(pool, url)
    result = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"])
    assert row["state"] == "done" and result["page"]["refusal"] == "soft_404"
    assert await _signals_for(pool, url) == []
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM fetch_versions WHERE url = $1", url) == 1


async def test_a_host_the_licence_ledger_refuses_is_never_fetched(pool, web) -> None:
    url = web.put(_url("walled", "walled.i2.example.org"), _dutch_article())
    _, row = await _drain_one(pool, url)
    assert row["state"] == "failed" and "license_refused" in (row["last_error"] or "")
    assert web.hits == []
