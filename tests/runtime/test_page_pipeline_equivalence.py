# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lane I-2 — a page fetched ON REQUEST lands as a normal document.

Three proofs over the recording pool (the database half — the real tables,
collapse / fill-in, enrichment, geocoding, the desk slice — is in
tests/data_pkg/test_page_pipeline_db.py):

1. an ADDRESS request under a ``page`` pipeline writes exactly the rows a
   poll of that page writes (same signal INSERT, same fetch receipt and
   link, same NATS message) — no second write path;
2. FIELD BY FIELD against the same article arriving through an RSS feed
   that carries its full text: every payload key a feed document carries is
   carried, populated the same way, and the same signal columns are set;
3. the run's provenance: the request (and its parent signal) on the
   document, the page's outcome on the request's result, a refused page as a
   finished request that says why, and a refusing host never fetched.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from legba.data.sources import page as page_mod
from legba.runtime import intake_run
from legba.runtime.deps import StandardDeps
from legba.runtime.source_actor import SourceCore, SourceDeps

from tests.data_pkg import _page_fixtures as P
from tests.runtime import _ingest_fakes as F

PAGE = "https://news.example.org/2026/11/13/port-strike-widens"
PIPELINE = "source.page.default"


@pytest.fixture
def web(tmp_path, monkeypatch) -> P.Web:
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("LEGBA_FETCH_RECEIPTS", "1")
    w = P.Web()
    monkeypatch.setattr(page_mod, "guarded_async_client", lambda **kw: P.client(w, **kw))
    return w


def _page_desc(url: str | None = None) -> Any:
    return F.descriptor(PIPELINE, "page", {"url": url} if url else {})


def _req(address: str, **kw: Any) -> Any:
    base = dict(id=uuid.uuid4(), target_kind="address", address=address, pipeline_source_id=PIPELINE,
                sink="document", depth=1, window_start=None, version_id=None,
                reason="followed_reference", requested_by="source::source.test.feed",
                on_behalf_of=None, link_kind="article", parent_request_id=None,
                parent_signal_id=uuid.uuid4())
    base.update(kw)
    return SimpleNamespace(**base)


def _own_archive() -> None:
    import os
    import tempfile

    os.environ["LEGBA_ARCHIVE_ROOT"] = tempfile.mkdtemp(prefix="i2_eq_")


async def _poll(desc: Any) -> tuple[F.RecordingPool, F.NatsRecorder, dict]:
    _own_archive()
    pool, nats = F.RecordingPool(), F.NatsRecorder()
    core = SourceCore(f"source::{desc.identity.id}::{desc.identity.version[:16]}",
                      SourceDeps(descriptor=desc, deps=StandardDeps(pg_pool=pool, nats_publish=nats)))
    return pool, nats, await core.pull_once()


async def _intake(address: str, **kw: Any) -> tuple[F.RecordingPool, F.NatsRecorder, Any]:
    _own_archive()
    pool, nats = F.RecordingPool(), F.NatsRecorder()
    deps = StandardDeps(pg_pool=pool, nats_publish=nats)
    desc = _page_desc()

    async def _resolve(sid: str) -> Any:
        return SourceDeps(descriptor=desc, deps=deps)

    out = await intake_run.run_request(_req(address, **kw), pool=pool, resolve=_resolve)
    return pool, nats, out


_TABLES = re.compile(r"\b(signals|signal_aliases|fetch_versions|fetch_version_records)\b")


def _acquisition(pool: F.RecordingPool) -> list:
    return [c for c in pool.calls if _TABLES.search(c[1])
            and not c[1].startswith("SELECT id FROM fetch_versions WHERE address")
            and "fetch_drift_observations" not in c[1]]


def _signal_insert(pool: F.RecordingPool) -> tuple:
    return next(c for c in pool.calls if c[1].startswith("INSERT INTO signals"))


def _strip_intake(obj: Any) -> Any:
    """The request provenance is the ONE deliberate difference on the row."""
    import json

    if isinstance(obj, str) and obj.startswith("{") and '"intake"' in obj:
        d = json.loads(obj)
        d.pop("intake", None)
        return json.dumps(d)
    if isinstance(obj, str) and obj.startswith("{") and '"intake_request_id"' in obj:
        d = json.loads(obj)
        d["intake_request_id"] = None
        return json.dumps(d)
    if isinstance(obj, (list, tuple)):
        return type(obj)(_strip_intake(x) for x in obj)
    return obj


async def test_an_address_request_lands_the_rows_a_poll_of_the_page_lands(web) -> None:
    web.put(PAGE, P.article())
    now = datetime.now(tz=timezone.utc)
    p_pool, p_nats, p_res = await _poll(_page_desc(PAGE))
    i_pool, i_nats, i_out = await _intake(PAGE)
    assert p_res["signals_written"] == 1, p_res
    assert i_out.error is None, i_out.error
    poll = F.normalise({"sql": _acquisition(p_pool)}, now=now)
    intake = F.normalise({"sql": _strip_intake(_acquisition(i_pool))}, now=now)
    assert intake == poll
    assert any("fetch_version_records" in c[1] for c in _acquisition(i_pool)), "the page version is linked"
    assert i_out.result["signals_written"] == 1 and i_out.result["address_status"] == 200
    assert i_out.result["page"]["refusal"] is None
    assert i_out.result["landing"] == [{"action": "new", "existing_signal_id": None}]
    assert len(i_nats.messages) == len(p_nats.messages) == 1


async def test_field_by_field_a_page_equals_the_same_article_from_a_full_text_feed(web, monkeypatch) -> None:
    import json

    from legba.data.sources import rss as rss_mod

    body = P.article()
    web.put(PAGE, body)
    from legba.data.sources.page_text import extract_main_text

    text = extract_main_text(body, "utf-8", max_chars=200_000)
    feed = (
        "<?xml version='1.0'?><rss version='2.0' xmlns:content='http://purl.org/rss/1.0/modules/content/'"
        " xmlns:dc='http://purl.org/dc/elements/1.1/'><channel><title>Example News</title>"
        "<link>https://news.example.org/</link><description>x</description>"
        "<item><title>Port strike widens to Antwerp</title>"
        f"<link>{PAGE}</link><guid>{PAGE}</guid>"
        "<pubDate>Fri, 13 Nov 2026 08:00:00 GMT</pubDate><dc:creator>A. Writer</dc:creator>"
        "<description>Dockworkers extend the stoppage.</description>"
        f"<content:encoded><![CDATA[{text}]]></content:encoded></item></channel></rss>"
    ).encode()

    real_init = rss_mod.RSSSourceHandler.__init__

    def _init(self, config, *a, **kw):
        kw["http_client"] = F.client_for(lambda r: httpx.Response(200, content=feed))
        real_init(self, config, *a, **kw)

    monkeypatch.setattr(rss_mod.RSSSourceHandler, "__init__", _init)
    f_pool, _, f_res = await _poll(F.descriptor("source.test.i2_feed", "rss", {"url": "https://news.example.org/feed"}))
    i_pool, _, i_out = await _intake(PAGE)
    assert f_res["signals_written"] == 1 and i_out.error is None

    f_args, i_args = _signal_insert(f_pool)[2], _signal_insert(i_pool)[2]
    fp, ip = json.loads(f_args[14]), json.loads(i_args[14])
    # every key a feed document carries, the page document carries
    assert set(fp) - set(ip) == set(), set(fp) - set(ip)
    for key in ("title", "link", "author", "summary", "published_at", "external_id"):
        assert ip[key] == fp[key], (key, ip[key], fp[key])
    assert ip["raw_body"] == text == fp["raw_body"], "the same article text"
    assert ip["tags"] == fp["tags"] == []
    # the same signal columns are set: canonical_url (16), content_hash (24),
    # modality (8), produced_by_kind (5), canonical_url_hash (30)
    for idx in (4, 7, 15, 29):
        assert (i_args[idx] is None) == (f_args[idx] is None), idx
    assert i_args[15] == f_args[15] == PAGE and i_args[23] and f_args[23]
    # what only the page carries: how it was fetched and by whom
    assert {"page", "publisher", "references", "intake"} <= set(ip) - set(fp)
    assert ip["page"]["published_basis"] == "json_ld:datePublished"


async def test_the_document_records_the_request_and_its_parent(web) -> None:
    import json

    web.put(PAGE, P.article())
    parent = uuid.uuid4()
    pool, _, out = await _intake(PAGE, parent_signal_id=parent)
    payload = json.loads(_signal_insert(pool)[2][14])
    intake = payload["intake"]
    assert intake["reason"] == "followed_reference" and intake["parent_signal_id"] == str(parent)
    assert intake["requested_by"] == "source::source.test.feed" and intake["link_kind"] == "article"
    assert intake["request_id"] == payload["intake"]["request_id"]
    prov = json.loads(_signal_insert(pool)[2][17])
    assert prov["fetch_kind"] == "page" and prov["intake_request_id"] == intake["request_id"]
    assert payload["page"]["credibility"]["basis"] == "declared_default"
    assert _signal_insert(pool)[2][22] == 0.3, "unknown host: the declared default, flagged"


async def test_a_refused_page_is_a_finished_request_that_says_why(web) -> None:
    web.put(PAGE, P.soft_404())
    pool, nats, out = await _intake(PAGE)
    assert out.error is None and out.records == []
    assert out.result["signals_written"] == 0 and nats.messages == []
    assert out.result["page"]["refusal"] == "soft_404"
    assert any(c[1].startswith("INSERT INTO fetch_versions") for c in pool.calls), "the receipt is kept"
    assert not any(c[1].startswith("INSERT INTO signals") for c in pool.calls)


async def test_robots_disallowed_fails_permanently_a_challenge_is_not_gone(web) -> None:
    web.robots["news.example.org"] = (200, "User-agent: *\nDisallow: /\n")
    web.put(PAGE, P.article())
    _, _, out = await _intake(PAGE)
    assert out.error.startswith("robots_disallowed") and out.permanent
    web.robots.clear()
    web.put(PAGE, P.challenge_page(), status=403)
    _, _, out = await _intake(PAGE)
    assert out.error is None and out.result["blocked"] == "http_403" and "gone" not in out.result


async def test_a_host_the_licence_ledger_refuses_is_never_fetched(web, monkeypatch) -> None:
    from legba.runtime import page_landing

    async def _prep(pool, address):
        return page_landing.PagePrep(refusal="license_refused: host news.example.org is 'anti_ai_walled'")

    monkeypatch.setattr(page_landing, "prepare", _prep)
    web.put(PAGE, P.article())
    _, _, out = await _intake(PAGE)
    assert out.error.startswith("license_refused") and out.permanent
    assert web.hits == [], "not even robots.txt"
