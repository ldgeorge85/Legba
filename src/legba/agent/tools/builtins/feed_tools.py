"""
RSS/Atom Feed Tools

Fetch and parse syndication feeds. Uses httpx for async HTTP and
feedparser for RSS/Atom parsing. HTML content in feed entries is
cleaned via trafilatura (readability extraction) with a regex fallback.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import feedparser
import httpx

from ....shared.schemas.tools import ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from ...memory.structured import StructuredStore
    from ...tools.registry import ToolRegistry

_USER_AGENT = "Legba-SA/1.0 (autonomous research agent; +https://github.com/ldgeorge85/legba)"
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_MAX_SUMMARY_CHARS = 800


def _clean_html(raw: str) -> str:
    """Extract readable text from HTML. Trafilatura first, regex fallback."""
    if not raw or "<" not in raw:
        return raw
    try:
        import trafilatura
        text = trafilatura.extract(raw, include_links=False, include_images=False,
                                   include_tables=False, no_fallback=False)
        if text and len(text.strip()) > 20:
            return text.strip()
    except Exception:
        pass
    # Fallback: strip tags and collapse whitespace
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"&#?\w+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------

FEED_PARSE_DEF = ToolDefinition(
    name="feed_parse",
    description="Fetch and parse an RSS or Atom feed URL. Returns structured entries "
                "with title, link, summary, published date, authors, and tags.",
    parameters=[
        ToolParameter(name="url", type="string",
                      description="The RSS/Atom feed URL to fetch and parse"),
        ToolParameter(name="limit", type="number",
                      description="Max entries to return (default 20, max 100)",
                      required=False),
        ToolParameter(name="timeout", type="number",
                      description="HTTP timeout in seconds (default 30)",
                      required=False),
        ToolParameter(name="source_id", type="string",
                      description="UUID of the registered source (enables automatic reliability tracking)",
                      required=False),
    ],
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(registry: ToolRegistry, *, structured: StructuredStore | None = None, **_deps) -> None:
    """Register feed tools."""

    async def feed_parse_handler(args: dict) -> str:
        url = args.get("url", "")
        if not url:
            return "Error: url is required"

        limit = min(int(args.get("limit", 20)), 100)
        timeout = min(int(args.get("timeout", 30)), 60)
        source_id_str = args.get("source_id", "")

        # Parse source_id for reliability tracking
        track_source_id = None
        if source_id_str and structured:
            try:
                from uuid import UUID
                track_source_id = UUID(source_id_str)
            except ValueError:
                pass

        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                headers={"User-Agent": _USER_AGENT},
                follow_redirects=True,
            ) as client:
                resp = await client.get(url)
                # On 403/405, retry once with a browser User-Agent
                if resp.status_code in (403, 405):
                    async with httpx.AsyncClient(
                        timeout=timeout,
                        headers={"User-Agent": _BROWSER_USER_AGENT},
                        follow_redirects=True,
                    ) as browser_client:
                        resp = await browser_client.get(url)
                resp.raise_for_status()
                raw = resp.text
        except (httpx.TimeoutException, httpx.HTTPStatusError, Exception) as e:
            # Record fetch failure
            if track_source_id:
                await structured.record_source_fetch(track_source_id, success=False)
            if isinstance(e, httpx.TimeoutException):
                return f"Error: Feed fetch timed out after {timeout}s"
            elif isinstance(e, httpx.HTTPStatusError):
                return f"Error: HTTP {e.response.status_code} fetching feed"
            return f"Error: Failed to fetch feed: {e}"

        try:
            feed = feedparser.parse(raw)
        except Exception as e:
            return f"Error: Failed to parse feed: {e}"

        if feed.bozo and not feed.entries:
            return f"Error: Malformed feed — {feed.bozo_exception}"

        entries = []
        for entry in feed.entries[:limit]:
            summary = _clean_html(entry.get("summary", ""))
            if len(summary) > _MAX_SUMMARY_CHARS:
                summary = summary[:_MAX_SUMMARY_CHARS] + "..."
            entries.append({
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "summary": summary,
                "published": entry.get("published", ""),
                "authors": [a.get("name", "") for a in entry.get("authors", [])],
                "tags": [t.get("term", "") for t in entry.get("tags", [])],
            })

        # Record successful fetch
        if track_source_id:
            await structured.record_source_fetch(track_source_id, success=True)

        result = {
            "feed_title": feed.feed.get("title", ""),
            "feed_link": feed.feed.get("link", ""),
            "entry_count": len(entries),
            "total_available": len(feed.entries),
            "entries": entries,
        }
        if source_id_str:
            result["source_id"] = source_id_str
        return json.dumps(result, indent=2, default=str)

    registry.register(FEED_PARSE_DEF, feed_parse_handler)
