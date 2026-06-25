# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Example scraper-impl — generic RSS- / sitemap-driven news scraper.

Demonstrates the :class:`~legba.data.sources.scraper.ScraperImpl` contract
against the common shape of "news site with an RSS feed and/or a
``/sitemap.xml`` index". Not tied to any specific site — the handler's
config supplies the seed URLs.

Behavior:

  * :meth:`discover_urls` at depth 0 treats the seed URL as either an
    RSS/Atom feed (parsed via :mod:`feedparser`) or a sitemap (XML with
    ``<loc>`` elements). It yields the article URLs from those indices.
    At depth >= 1 it yields nothing — articles are leaf nodes.

  * :meth:`extract` runs :func:`trafilatura.extract` over the fetched HTML
    to recover the article body, plus a lightweight metadata grab for
    title / publication date / author from trafilatura's metadata API.
    The resulting :class:`Signal` payload carries
    ``{title, body, url, author, published_at, tags}``.

The impl uses no network calls except through the ``fetch`` callable the
handler provides — this keeps proxy / rate-limit / robots policy honored.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import AsyncIterator
from uuid import uuid4
from xml.etree import ElementTree as ET

import feedparser
import trafilatura

from .._contract import Signal, SourceContext
from ..scraper import HttpFetcher

logger = logging.getLogger(__name__)


# Sitemap XML namespace.
_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
# Crude heuristics — sniff body shape.
_SITEMAP_ROOT_RE = re.compile(
    r"<\s*(urlset|sitemapindex)[\s>]", re.IGNORECASE,
)
_FEED_ROOT_RE = re.compile(
    r"<\s*(rss|feed)[\s>]", re.IGNORECASE,
)


class ExampleNewsScraper:
    """RSS- or sitemap-driven generic news scraper."""

    name: str = "example_news"

    async def discover_urls(
        self,
        seed_url: str,
        depth: int,
        *,
        fetch: HttpFetcher,
    ) -> AsyncIterator[str]:
        if depth >= 1:
            # Articles are leaves — no further discovery.
            return

        try:
            resp = await fetch(seed_url)
        except Exception as exc:
            logger.warning("example_news discovery fetch failed %s: %s",
                           seed_url, exc)
            return
        if resp.status_code >= 400:
            logger.debug("example_news discovery non-2xx %s for %s",
                         resp.status_code, seed_url)
            return

        body = resp.text
        ctype = (resp.headers.get("content-type") or "").lower()

        # Decide format by sniffing the body root tag first (more reliable
        # than content-type — many sitemaps and feeds ship with generic
        # ``application/xml`` or ``text/xml``).
        head = body[:512]
        is_sitemap = bool(_SITEMAP_ROOT_RE.search(head))
        is_feed = bool(_FEED_ROOT_RE.search(head))
        # Content-type as a secondary signal.
        if not is_sitemap and not is_feed:
            if "rss" in ctype or "atom" in ctype or "feed" in ctype:
                is_feed = True
            elif "xml" in ctype:
                is_sitemap = True

        if is_sitemap:
            for url in _parse_sitemap(body):
                yield url
            return

        if is_feed:
            feed = feedparser.parse(body)
            if not feed.entries:
                logger.debug("example_news: %s yielded no entries", seed_url)
                return
            for entry in feed.entries:
                link = getattr(entry, "link", None)
                if not link:
                    continue
                yield link
            return

        # Fallback — try feedparser anyway (lenient on weird shapes).
        feed = feedparser.parse(body)
        for entry in getattr(feed, "entries", []) or []:
            link = getattr(entry, "link", None)
            if link:
                yield link

    async def extract(
        self,
        html: str,
        url: str,
        *,
        ctx: SourceContext,
    ) -> Signal | None:
        # trafilatura.extract returns the article body or None.
        body = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=False,
            with_metadata=False,
            favor_recall=True,
        )
        if not body:
            return None

        meta_obj = trafilatura.extract_metadata(html, default_url=url)
        title: str | None = None
        author: str | None = None
        published_at: str | None = None
        tags: list[str] = []
        if meta_obj is not None:
            title = getattr(meta_obj, "title", None) or None
            author = getattr(meta_obj, "author", None) or None
            published_at = getattr(meta_obj, "date", None) or None
            cats = getattr(meta_obj, "categories", None) or []
            tags = list(cats) if isinstance(cats, (list, tuple)) else []

        canonical_text = "\n".join(
            x for x in (title, body) if x
        ).strip()
        content_hash = hashlib.sha256(
            canonical_text.encode("utf-8")
        ).hexdigest()

        return Signal(
            signal_id=uuid4(),
            # source_id / target_id backfilled by ScraperSourceHandler.
            source_id="",
            modality="text",
            fetched_at=datetime.now(tz=timezone.utc),
            payload={
                "title": title,
                "body": body,
                "url": url,
                "author": author,
                "published_at": published_at,
                "tags": tags,
            },
            content_hash=content_hash,
            canonical_url=url,
            language_hint=None,
        )


def _parse_sitemap(xml_text: str) -> list[str]:
    """Return every ``<loc>`` from a sitemap or sitemap-index document.

    Falls back to a regex sweep if the XML is malformed (publishers ship
    a lot of broken sitemaps).
    """
    out: list[str] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml_text)

    # Try namespaced lookup first, then unprefixed.
    for ns in (_SITEMAP_NS, None):
        if ns is not None:
            locs = root.findall(".//sm:url/sm:loc", ns) or root.findall(
                ".//sm:sitemap/sm:loc", ns
            )
        else:
            locs = root.findall(".//url/loc") or root.findall(".//sitemap/loc")
        if locs:
            for el in locs:
                if el.text:
                    out.append(el.text.strip())
            break
    return out


__all__ = ["ExampleNewsScraper"]
