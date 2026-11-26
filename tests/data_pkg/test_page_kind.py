# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``page`` source kind, without a database.

* the kind is registered (plug-in declaration) and has an intake address
  adapter; the example scraper is not the production path;
* ONE page → ONE document shaped like a feed entry (the RSS handler's payload
  keys), with what the page states and nothing invented: ``published_at``
  only when declared (JSON-LD / meta / time tag), a same-site canonical only,
  language, author, site name, the page's own description as ``summary``,
  outbound references classified in the follow vocabulary;
* ``raw_body`` is the archiver's extraction, byte for byte (the text the
  fetch receipt stores for the version);
* every quality gate refuses with its reason (and the bytes still reach the
  capture log — the receipt);
* robots, the size cap, PDFs, redirects, the host fetch policy's
  impersonation escalation;
* a heartbeat: extracting a large page never holds the event loop.
"""

from __future__ import annotations

from tests._lifecycle_rows import lifecycle_text
import asyncio
import time
from typing import Any

import httpx
import pytest

from legba.data.sources import page as page_mod
from legba.data.sources._contract import SourceContext
from legba.data.sources.fetch_capture import CaptureLog, capture_scope
from legba.data.sources.page import PageConfig, PageSourceHandler

from tests.data_pkg import _page_fixtures as P

URL = "https://news.example.org/2026/11/13/port-strike-widens"


class _State:
    def __init__(self) -> None:
        self.d: dict[str, Any] = {}

    async def get(self, k: str) -> Any:
        return self.d.get(k)

    async def set(self, k: str, v: Any) -> None:
        self.d[k] = v


def _ctx() -> SourceContext:
    return SourceContext(target_id="source.page.default", target_version="0" * 16,
                         source_id="source.page.default", config=PageConfig(), state_store=_State())


@pytest.fixture
def web(monkeypatch) -> P.Web:
    w = P.Web()
    monkeypatch.setattr(page_mod, "guarded_async_client", lambda **kw: P.client(w, **kw))
    return w


async def _pull(url: str, log: CaptureLog | None = None, **cfg: Any) -> tuple[list, PageSourceHandler]:
    h = PageSourceHandler(PageConfig(url=url, **cfg))
    with capture_scope(log):
        out = [s async for s in h.pull(_ctx())]
    return out, h


# ---------------------------------------------------------------------------
# registration + the intake
# ---------------------------------------------------------------------------


def test_the_page_kind_is_declared_and_has_an_address_adapter() -> None:
    from legba.data.intake_contract import ADDRESS_ADAPTERS, DEFAULT_PAGE_PIPELINE, address_config
    from legba.data.sources.kinds import declared_source_kinds
    from legba.runtime.source_factory import build_source_handler, discover_source_kinds

    assert ("page", "PageSourceHandler") in declared_source_kinds()
    assert discover_source_kinds()["page"] is PageSourceHandler
    assert "page" in ADDRESS_ADAPTERS and DEFAULT_PAGE_PIPELINE == "source.page.default"
    cfg = address_config("page", {"min_text_chars": 500}, URL)
    assert cfg == {"min_text_chars": 500, "url": URL}
    handler = build_source_handler("page", {**cfg, "intake": {"request_id": "r"}})
    assert isinstance(handler, PageSourceHandler)


def test_the_shipped_default_pipeline_parses_and_is_a_page_pipeline() -> None:
    import yaml

    from legba.data.schemas.source import SourceDescriptor
    from tests.runtime._ingest_fakes import REPO

    body = yaml.safe_load((REPO / "descriptors" / "source_page_pipeline_default.yaml").read_text())
    desc = SourceDescriptor.model_validate(body, strict=False)
    assert desc.identity.kind == "page" and desc.identity.state.value == "draft"
    assert desc.cadence is None, "a page pipeline has no schedule of its own"
    assert "url" not in desc.config, "the address comes from each request"
    PageConfig(**{k: (v.get("raw") if isinstance(v, dict) else v) for k, v in desc.config.items()})
    lifecycle = lifecycle_text(REPO / "descriptors")
    assert "| source | source.page.default | configured | draft |" in lifecycle


def test_nothing_live_depends_on_the_example_scraper() -> None:
    from tests.runtime._ingest_fakes import REPO

    import re

    hits = [p.name for p in (REPO / "descriptors").glob("*.yaml") if "example_news" in p.read_text()]
    assert hits == [], hits
    uses = re.compile(r"(?:from|import)\s+[\w.]*example_news|example_news[:.]ExampleNewsScraper")
    src = [p for p in (REPO / "src" / "legba").rglob("*.py")
           if uses.search(p.read_text()) and "scrapers" not in p.parts]
    assert src == [], src


# ---------------------------------------------------------------------------
# one page -> one document, shaped like a feed entry
# ---------------------------------------------------------------------------


async def test_a_page_becomes_one_document_with_the_feed_payload_shape(web) -> None:
    from legba.data.sources.page_text import extract_main_text

    body = P.article(canonical="https://www.news.example.org/2026/11/13/port-strike-widens")
    web.put(URL, body)
    log = CaptureLog()
    out, h = await _pull(URL, log)
    assert len(out) == 1, h.last_outcome
    sig = out[0]
    rss_keys = {"external_id", "published_at", "title", "link", "summary", "author", "tags",
                "raw_body", "source_url"}
    assert rss_keys <= set(sig.payload), rss_keys - set(sig.payload)
    p = sig.payload
    assert p["title"] == "Port strike widens to Antwerp" and p["author"] == "A. Writer"
    assert p["published_at"] == "2026-11-13T08:00:00+00:00"
    assert p["page"]["published_basis"] == "json_ld:datePublished"
    assert sig.canonical_url == p["link"] == "https://www.news.example.org/2026/11/13/port-strike-widens"
    assert p["page"]["canonical_basis"] == "declared_same_site"
    assert sig.language_hint == "en" and p["summary"] == "Dockworkers extend the stoppage."
    assert p["publisher"]["site_name"] == "Example News"
    assert p["publisher"]["host"] == "www.news.example.org"
    # raw_body IS the archiver's extraction (the text the receipt stores).
    assert p["raw_body"] == extract_main_text(body, "utf-8", max_chars=200_000)
    assert len(p["raw_body"]) > 1500 and "Paragraph 11" in p["raw_body"]
    assert "Section 3" not in p["raw_body"], "navigation is not the body"
    refs = {r["url"]: r["kind"] for r in p["references"]}
    assert refs["https://other.example.com/files/report.pdf"] == "document"
    assert refs["https://news.example.org/2026/11/10/story-0"] == "anchor"
    assert not any("/section" in u for u in refs), "nav links are not references"
    assert sig.raw_provenance["fetch_kind"] == "page"
    # the receipt: the page, as role 'page', complete, the bytes received
    caps = [c for c in log.captures if c.role == "page"]
    assert len(caps) == 1 and caps[0].complete and caps[0].body == body
    assert p["page"]["body_sha256"] == caps[0].sha256
    assert not any("robots.txt" in c.url for c in log.captures), "robots.txt is not a receipt"


@pytest.mark.parametrize("where,basis", [("meta", "meta:article:published_time"), ("time", "time:article")])
async def test_published_at_from_meta_or_time_tag(web, where, basis) -> None:
    web.put(URL, P.article(published="2026-11-12T21:30:00+01:00", published_where=where))
    out, _ = await _pull(URL)
    assert out[0].payload["published_at"] == "2026-11-12T20:30:00+00:00"
    assert out[0].payload["page"]["published_basis"] == basis


async def test_no_stated_date_is_absent_never_invented(web) -> None:
    # A date in the URL path and in the prose is NOT a declared date.
    url = "https://news.example.org/2026/11/13/undated"
    web.put(url, P.article(published=None))
    out, _ = await _pull(url)
    p = out[0].payload
    assert p["published_at"] is None and "_published_at_dt" not in p
    assert p["page"]["published_basis"] == "absent"
    assert p["page"]["fetched_at"], "the fetch time is recorded — as the fetch time"


async def test_a_cross_site_canonical_is_ignored_and_flagged(web) -> None:
    web.put(URL, P.article(canonical="https://syndicator.example.net/copy/123"))
    out, _ = await _pull(URL)
    assert out[0].canonical_url == URL
    assert out[0].payload["page"]["canonical_basis"] == "declared_cross_site_ignored"
    assert "cross_site_canonical_ignored" in out[0].payload["page"]["flags"]


async def test_a_redirect_is_followed_within_the_bound_and_the_final_url_kept(web) -> None:
    short = "https://news.example.org/s/abc"
    web.redirects[short] = URL
    web.put(URL, P.article())
    out, h = await _pull(short)
    assert out and out[0].payload["page"]["final_url"] == URL and out[0].canonical_url == URL
    hop = "https://news.example.org/loop"
    web.redirects[hop] = hop
    out, h = await _pull(hop)
    assert out == [] and h.last_outcome["refusal"] == "transport_error"


FR_URL = "https://www.federalregister.gov/documents/2026/11/04/2026-22472/determination-with-respect-to-yemen"
FR_WALL = "https://unblock.federalregister.gov/"


async def test_a_cross_host_redirect_to_a_wall_is_refused_and_its_bytes_kept(web) -> None:
    """The Federal Register answered with a 200 redirect to its
    'Request Access' CAPTCHA page on another host; it passed every gate and
    landed as a document under the wall's address."""
    web.redirects[FR_URL] = FR_WALL
    web.put(FR_WALL, P.federal_register_wall())
    log = CaptureLog()
    out, h = await _pull(FR_URL, log)
    assert out == [], out and out[0].payload["page"]
    assert h.last_outcome["refusal"] == "blocked_by_challenge", h.last_outcome
    assert h.last_outcome["permanent"] is True
    assert h.last_outcome["final_url"] == FR_WALL
    assert [c for c in log.captures if c.role == "page" and c.body == P.federal_register_wall()]


async def test_a_wall_host_is_refused_even_without_a_known_tell(web) -> None:
    web.redirects[FR_URL] = "https://captcha.federalregister.gov/check"
    web.put("https://captcha.federalregister.gov/check", P.article(title="Please wait"))
    out, h = await _pull(FR_URL)
    assert out == [] and h.last_outcome["refusal"] == "blocked_by_challenge"
    assert "wall host: captcha.federalregister.gov" in h.last_outcome["detail"]


async def test_a_cross_host_redirect_to_an_article_mentioning_a_wall_phrase_is_refused(web) -> None:
    """Off the requested host, a wall phrase is not furniture: the page is not
    the article that was asked for. (On the same host the length gate rules.)"""
    other = "https://gate.example.net/landing"
    web.redirects[URL] = other
    web.put(other, P.article(extra_body="<p>Please verify you are human to continue.</p>"))
    out, h = await _pull(URL)
    assert out == [] and h.last_outcome["refusal"] == "blocked_by_challenge"
    assert h.last_outcome["detail"].startswith("cross-host redirect to gate.example.net")
    web.redirects.clear()
    web.put(URL, P.article(extra_body="<p>Please verify you are human to continue.</p>"))
    out, h = await _pull(URL)
    assert len(out) == 1, h.last_outcome


async def test_a_cross_host_redirect_to_a_real_article_lands_under_its_final_url(web) -> None:
    short = "https://lnk.example.com/x1"
    web.redirects[short] = URL
    web.put(URL, P.article())
    out, h = await _pull(short)
    assert len(out) == 1 and out[0].canonical_url == URL


def test_a_document_never_takes_a_wall_address_as_its_own() -> None:
    from legba.data.sources.page_meta import parse_page_meta
    from legba.data.sources.page_quality import wall_host

    assert wall_host("https://unblock.federalregister.gov") == "unblock.federalregister.gov"
    assert wall_host("https://captcha.example.org/x") == "captcha.example.org"
    assert wall_host("https://unblock.org/") is None          # a registered name, not a sub-domain
    assert wall_host("https://www.federalregister.gov/d/1") is None
    h = PageSourceHandler(PageConfig(url=FR_URL))
    out = page_mod.PageOutcome(url=FR_URL, final_url="https://unblock.federalregister.gov", status=200)
    meta = parse_page_meta(P.federal_register_wall().decode(), "https://unblock.federalregister.gov")
    with pytest.raises(ValueError, match="wall address"):
        h._to_signal(_ctx(), "text", meta, out, page_mod.datetime.now(tz=page_mod.timezone.utc))


# ---------------------------------------------------------------------------
# quality gates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body,reason", [
    (P.soft_404(), "soft_404"),
    (P.login_page(), "login_wall"),
    (P.consent_page(), "consent_wall"),
    (P.paywall_page(), "paywall"),
    (P.challenge_page(), "blocked_by_challenge"),
    (P.thin_page(), "too_little_text"),
    (P.section_front(), "mostly_navigation"),
])
async def test_each_gate_refuses_with_its_reason_and_keeps_the_receipt(web, body, reason) -> None:
    web.put(URL, body)
    log = CaptureLog()
    out, h = await _pull(URL, log)
    assert out == [], (reason, out and out[0].payload["page"])
    assert h.last_outcome["refusal"] == reason, h.last_outcome
    assert h.last_outcome["permanent"] is True
    assert [c for c in log.captures if c.role == "page" and c.body == body], "the bytes are kept"


async def test_a_long_article_that_mentions_cookies_login_and_not_found_is_kept(web) -> None:
    extra = ("<p>The minister said the documents could not be found, that staff must sign in "
             "and accept cookies on the portal, and that the page you requested was gone.</p>")
    web.put(URL, P.article(title="Missing files not found, ministry says", extra_body=extra))
    out, h = await _pull(URL)
    assert len(out) == 1, h.last_outcome


async def test_a_declared_paywall_serving_full_text_lands_flagged(web) -> None:
    web.put(URL, P.article(paywall_declared=True))
    out, _ = await _pull(URL)
    assert out and "paywall_declared" in out[0].payload["page"]["flags"]


async def test_status_verdicts_402_403_and_404(web) -> None:
    for status, reason in ((402, "paywall"), (403, "blocked_by_challenge"), (404, "http_status")):
        web.put(URL, b"<html><body>x</body></html>", status=status)
        out, h = await _pull(URL)
        assert out == [] and h.last_outcome["refusal"] == reason and h.last_outcome["status"] == status


async def test_a_pdf_is_refused_with_what_a_document_family_would_need(web) -> None:
    web.put(URL, b"%PDF-1.7\n...", ctype="application/pdf")
    out, h = await _pull(URL)
    assert out == [] and h.last_outcome["refusal"] == "unsupported_content_type"
    assert "pypdf" in h.last_outcome["detail"]


async def test_the_size_cap_refuses_too_large(web) -> None:
    web.put(URL, P.article(paras=900))
    out, h = await _pull(URL, max_bytes=64 * 1024)
    assert out == [] and h.last_outcome["refusal"] == "too_large"


async def test_robots_disallow_is_honoured_and_never_fetches_the_page(web) -> None:
    web.robots["news.example.org"] = (200, "User-agent: *\nDisallow: /2026/\n")
    web.put(URL, P.article())
    out, h = await _pull(URL)
    assert out == [] and h.last_outcome["refusal"] == "robots_disallowed"
    assert URL not in web.hits
    out, h = await _pull(URL, respect_robots=False)
    assert len(out) == 1


async def test_no_address_yields_nothing() -> None:
    h = PageSourceHandler(PageConfig())
    assert [s async for s in h.pull(_ctx())] == []
    assert h.last_outcome["refusal"] == "no_address"


# ---------------------------------------------------------------------------
# host fetch policy: the impersonation escalation (the archiver's rule)
# ---------------------------------------------------------------------------


class _ImpClient:
    """Stands in for the impersonated client: an httpx client on another
    in-memory web, entered as an async context manager."""

    def __init__(self, web: P.Web) -> None:
        self._client = httpx.AsyncClient(transport=httpx.MockTransport(web), follow_redirects=True)

    async def __aenter__(self) -> httpx.AsyncClient:
        return self._client

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.aclose()


async def test_a_policy_host_is_escalated_to_impersonation_once_and_its_receipt_kept(web, monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_FETCH_IMPERSONATE", raising=False)
    url = "https://www.timesofisrael.com/liveblog-entry/port-strike"
    web.put(url, P.challenge_page(), status=403)
    browser = P.Web()
    browser.put(url, P.article())
    h = PageSourceHandler(PageConfig(url=url), impersonated_client=_ImpClient(browser))
    log = CaptureLog()
    with capture_scope(log):
        out = [s async for s in h.pull(_ctx())]
    assert len(out) == 1 and out[0].payload["page"]["transport"] == "impersonated"
    assert [c.status for c in log.captures if c.role == "page"] == [403, 200]
    # a host without a policy is NOT escalated: the edge's answer stands
    other = "https://blocked.example.org/a"
    web.put(other, P.challenge_page(), status=403)
    h2 = PageSourceHandler(PageConfig(url=other), impersonated_client=_ImpClient(browser))
    assert [s async for s in h2.pull(_ctx())] == [] and h2.last_outcome["refusal"] == "blocked_by_challenge"
    # the fleet-wide kill switch beats the host row
    monkeypatch.setenv("LEGBA_FETCH_IMPERSONATE", "off")
    h3 = PageSourceHandler(PageConfig(url=url), impersonated_client=_ImpClient(browser))
    assert [s async for s in h3.pull(_ctx())] == [] and h3.last_outcome["transport"] == "guarded"


def test_the_archiver_and_the_page_kind_read_one_host_policy_registry() -> None:
    import inspect

    from legba.data.analysts.deterministic_handlers import _intake_route, evidence_archiver
    from legba.data.sources import _fetch_policy

    assert page_mod.fetch_policy_for is _fetch_policy.fetch_policy_for
    assert page_mod.impersonate_kwarg is _fetch_policy.impersonate_kwarg
    assert "fetch_policy_for" in inspect.getsource(_intake_route)
    assert "impersonate_kwarg" in inspect.getsource(evidence_archiver)


def test_one_extraction_for_the_archiver_the_receipts_and_the_page_kind() -> None:
    from legba.data.analysts.deterministic_handlers import evidence_archiver as ea
    from legba.data.sources import page_text

    assert ea._extract_text is page_text.extract_main_text
    assert ea._match_wall_pattern is page_text.match_wall_pattern
    assert ea._is_textual is page_text.is_textual
    body = P.article()
    from legba.runtime.fetch_receipts import extract_page_text

    assert extract_page_text(body, "text/html; charset=utf-8") == page_text.extract_main_text(
        body, "utf-8", max_chars=200_000)


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


async def test_extracting_large_pages_never_holds_the_loop(web, monkeypatch) -> None:
    """Four ~600 KB pages pulled concurrently while a heartbeat ticks every
    10 ms. Extraction and the markup reading run in worker threads; what the
    loop can still feel is lxml holding the GIL inside them.

    Two checks. (1) Structural, deterministic: every ``_analyse`` call runs on
    a thread other than the loop's. (2) Timing: each heartbeat gap is charged
    only the CPU this process burned during it, ``min(wall, process CPU)``,
    bounded at 0.5 s. A gap is the loop being held only if this process was
    doing work for it; a gap in which the process barely ran is the host not
    scheduling it. The wall-clock bound this replaces (1.0 s) failed the
    shuffled nightly of 2026-11-23 (seed 537394567) at 1.91 s over a 3.49 s
    pull on a loaded host; 32 CPU hogs beside this test reproduce it (wall gap
    1.37 s, process CPU in that gap 0.23 s). Measured in the test container:
    off-loop the charged gap is 0.10-0.22 s, with or without the hogs; with
    the extraction moved onto the loop (``to_thread`` run inline) it is
    0.97-0.99 s, which the old 1.0 s wall bound would only just have caught."""
    import threading

    loop_thread = threading.get_ident()
    analyse_threads: list[int] = []
    real_analyse = page_mod._analyse

    def _spy(*a: Any, **kw: Any) -> Any:
        analyse_threads.append(threading.get_ident())
        return real_analyse(*a, **kw)

    monkeypatch.setattr(page_mod, "_analyse", _spy)
    warm = "https://warm.example.org/a"
    web.put(warm, P.article(paras=40))
    await _pull(warm)
    urls = [web.put(f"https://big{i}.example.org/a", P.article(paras=2500)) for i in range(4)]
    gaps: list[tuple[float, float]] = []          # (wall, process CPU) per tick
    stop = asyncio.Event()

    async def _heartbeat() -> None:
        last_wall, last_cpu = time.monotonic(), time.process_time()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now_wall, now_cpu = time.monotonic(), time.process_time()
            gaps.append((now_wall - last_wall, now_cpu - last_cpu))
            last_wall, last_cpu = now_wall, now_cpu

    hb = asyncio.create_task(_heartbeat())
    t0 = time.monotonic()
    results = await asyncio.gather(*(_pull(u) for u in urls))
    took = time.monotonic() - t0
    stop.set()
    await hb
    assert all(len(out) == 1 for out, _ in results)
    assert len(analyse_threads) == 5 and loop_thread not in analyse_threads, analyse_threads
    wall_max = max(w for w, _ in gaps)
    held = max(min(w, c) for w, c in gaps)
    print(f"\n[heartbeat] {took:.2f}s over 4 pages of {len(P.article(paras=2500))} bytes; "
          f"max wall gap {wall_max:.3f}s, max held gap {held:.3f}s")
    assert held < 0.5, (held, wall_max)
