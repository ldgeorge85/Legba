# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""I-1 — an intake request lands EXACTLY the rows a poll of the same page
lands (no second write path).

The way the sink lane proved its move (tests/runtime/test_signal_sink_
equivalence.py): the same live-shaped fixture is run twice, each against its
own recording pool and NATS recorder —

* POLL: a ``scraper`` source whose seed list IS the page (``seed_urls=[X]``,
  ``max_depth=0``), through ``SourceCore.pull_once``;
* INTAKE: an ``address`` request for X under a ``scraper`` page pipeline
  (empty seed list), through ``intake_run.run_request`` — the address adapter
  substitutes X and the run is ``pull_once`` on an ``IntakeCore``.

Asserted identical after normalising run-specific uuids and clock reads:
every statement on ``signals`` and ``signal_aliases`` (with its arguments, in
order), every statement on the fetch-receipt tables (the page version and its
link to the signal), and every published NATS message. What differs is
deliberately outside acquisition and asserted as such: the cursor/handler
state lives under ``intake::<request id>`` (and is deleted at the end), and
the per-poll provenance row goes to the request's result instead of
``source_poll_outcomes``.

A second case runs an ``rss`` pipeline (a followed feed address) the same
way against an ``rss`` poll of that feed.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from legba.runtime import intake_run
from legba.runtime.deps import StandardDeps
from legba.runtime.source_actor import SourceCore, SourceDeps

from tests.runtime import _ingest_fakes as F

PAGE = "https://news.example.org/2026/11/13/port-strike-widens"
IMPL = "legba.data.sources.scrapers.example_news:ExampleNewsScraper"

_ARTICLE = (
    "<html><head><title>Port strike widens to Antwerp</title>"
    "<meta property='article:published_time' content='2026-11-13T08:00:00Z'></head><body>"
    "<nav><a href='/'>Home</a></nav><article><h1>Port strike widens to Antwerp</h1>"
    + "".join(
        f"<p>Paragraph {i}: dockworkers in Rotterdam and Antwerp extended the stoppage on "
        f"Thursday as talks over automation and pay broke down for the {i}th time, the "
        "union said, with container traffic backed up along the Scheldt.</p>"
        for i in range(12)
    )
    + "<p>See also <a href='https://news.example.org/2026/11/12/talks-collapse'>the talks</a>"
    " and <a href='https://other.example.com/report.pdf'>the report</a>.</p>"
    "</article></body></html>"
).encode()


def _serve(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/robots.txt":
        return httpx.Response(404, content=b"")
    return httpx.Response(200, content=_ARTICLE, headers={"content-type": "text/html; charset=utf-8"})


@pytest.fixture(autouse=True)
def _rig(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("LEGBA_FETCH_RECEIPTS", "1")
    from legba.data.sources import scraper as scraper_mod

    def _client(**_kw: Any) -> Any:   # the scraper's guarded client, on the fixture transport
        return F.client_for(_serve)

    monkeypatch.setattr(scraper_mod, "guarded_async_client", _client)


def _scraper_desc(seeds: list[str]) -> Any:
    return F.descriptor("source.test.i1_pages", "scraper", {
        "impl": IMPL, "seed_urls": seeds, "max_depth": 0, "respect_robots": True,
    })


def _req(address: str, pipeline: str, *, sink: str = "document") -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(), target_kind="address", address=address, pipeline_source_id=pipeline,
        sink=sink, depth=0, window_start=None, version_id=None,
    )


_TABLES = re.compile(r"\b(signals|signal_aliases|fetch_versions|fetch_version_records)\b")


def _acquisition(pool: F.RecordingPool) -> list:
    """Statements on the record tables and the receipt tables."""
    return [c for c in pool.calls if _TABLES.search(c[1])
            and not c[1].startswith("SELECT id FROM fetch_versions WHERE address")   # run's own read-back
            and "fetch_drift_observations" not in c[1]]


def _comparable(pool, nats, now) -> Any:
    return F.normalise({"sql": _acquisition(pool), "nats": nats.messages}, now=now)


def _own_archive() -> None:
    """Each run stores into its OWN evidence store, so the content-addressed
    store's 'already held' answer cannot differ between the two runs."""
    import os
    import tempfile

    os.environ["LEGBA_ARCHIVE_ROOT"] = tempfile.mkdtemp(prefix="i1_eq_")


async def _poll(desc: Any) -> tuple[F.RecordingPool, F.NatsRecorder, dict]:
    _own_archive()
    pool, nats = F.RecordingPool(), F.NatsRecorder()
    core = SourceCore(f"source::{desc.identity.id}::{desc.identity.version[:16]}",
                      SourceDeps(descriptor=desc, deps=StandardDeps(pg_pool=pool, nats_publish=nats)))
    return pool, nats, await core.pull_once()


async def _intake(pipeline_desc: Any, address: str, **kw: Any) -> tuple[F.RecordingPool, F.NatsRecorder, Any]:
    _own_archive()
    pool, nats = F.RecordingPool(), F.NatsRecorder()
    deps = StandardDeps(pg_pool=pool, nats_publish=nats)

    async def _resolve(sid: str) -> Any:
        assert sid == pipeline_desc.identity.id
        return SourceDeps(descriptor=pipeline_desc, deps=deps)

    out = await intake_run.run_request(_req(address, pipeline_desc.identity.id, **kw), pool=pool, resolve=_resolve)
    return pool, nats, out


@pytest.mark.asyncio
async def test_an_address_request_lands_the_rows_a_poll_of_the_page_lands() -> None:
    now = datetime.now(tz=timezone.utc)
    p_pool, p_nats, p_res = await _poll(_scraper_desc([PAGE]))
    i_pool, i_nats, i_out = await _intake(_scraper_desc([]), PAGE)

    assert p_res["signals_written"] == 1 and len(p_nats.messages) == 1, "the fixture must write"
    assert i_out.error is None, i_out.error
    assert _comparable(i_pool, i_nats, now) == _comparable(p_pool, p_nats, now)
    # The page was kept as a version and linked to the signal on both paths.
    assert any("INSERT INTO fetch_versions" in c[1] for c in _acquisition(i_pool))
    assert any("fetch_version_records" in c[1] for c in _acquisition(i_pool))
    # Outside acquisition, by design: no poll-ledger row for the request, its
    # state under its own actor id, deleted at the end.
    assert any("source_poll_outcomes" in c[1] for c in p_pool.calls)
    assert not any("source_poll_outcomes" in c[1] for c in i_pool.calls)
    intake_state = [c for c in i_pool.calls if "actor_filter_state" in c[1]]
    assert intake_state and all(str(c[2][0]).startswith("intake::") for c in intake_state)
    assert intake_state[-1][1].startswith("DELETE FROM public.actor_filter_state")
    assert i_out.records and i_out.records[0][0] == "signal"
    assert i_out.result["signals_written"] == 1 and i_out.result["address_status"] == 200


@pytest.mark.asyncio
async def test_a_receipt_request_keeps_the_same_version_and_writes_no_record() -> None:
    now = datetime.now(tz=timezone.utc)
    p_pool, _p_nats, _ = await _poll(_scraper_desc([PAGE]))
    i_pool, i_nats, i_out = await _intake(_scraper_desc([]), PAGE, sink="receipt")

    def _versions(pool):
        return F.normalise([c for c in pool.calls if c[1].startswith("INSERT INTO fetch_versions")], now=now)

    assert i_out.error is None, i_out.error
    assert _versions(i_pool) == _versions(p_pool), "the same page version, byte for byte"
    assert not any("INSERT INTO signals" in c[1] for c in i_pool.calls)
    assert i_nats.messages == []
    assert i_out.records == [] and i_out.result["records_seen_not_written"] == 1


@pytest.mark.asyncio
async def test_a_followed_feed_address_under_an_rss_pipeline_equals_an_rss_poll(monkeypatch) -> None:
    from legba.data.sources import rss as rss_mod

    now = datetime.now(tz=timezone.utc)
    feed_body = F._rss_xml(now)

    def _feed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=feed_body, headers={"content-type": "application/rss+xml"})

    real_init = rss_mod.RSSSourceHandler.__init__

    def _init(self, config, *a, **kw):  # every rss handler gets the fixture transport
        kw["http_client"] = F.client_for(_feed)
        real_init(self, config, *a, **kw)

    monkeypatch.setattr(rss_mod.RSSSourceHandler, "__init__", _init)
    url = "https://news.example.org/feed"
    p_pool, p_nats, p_res = await _poll(F.descriptor("source.test.i1_feeds", "rss", {"url": url}))
    i_pool, i_nats, i_out = await _intake(
        F.descriptor("source.test.i1_feeds", "rss", {"url": "https://placeholder.example.org/feed"}), url,
    )
    assert p_res["signals_written"] == 3
    assert i_out.error is None, i_out.error
    assert _comparable(i_pool, i_nats, now) == _comparable(p_pool, p_nats, now)
