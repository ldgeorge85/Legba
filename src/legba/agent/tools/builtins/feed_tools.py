"""
RSS/Atom Feed Tools + JSON API Fallback

Fetch and parse syndication feeds. Uses httpx for async HTTP and
feedparser for RSS/Atom parsing. HTML content in feed entries is
cleaned via trafilatura (readability extraction) with a regex fallback.

For JSON API sources (source_type "api"), or when feedparser returns no
entries and the response is JSON, attempts to extract items from common
JSON structures (root array, or keys like "articles", "results", "data",
"items", "events", "reports").
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

# Common JSON keys that contain item arrays in news/data APIs
_JSON_COLLECTION_KEYS = ("articles", "results", "data", "items", "events", "reports",
                         "entries", "records", "stories", "hits", "feed", "posts",
                         "documents", "content", "values", "rows")


def _extract_json_items(data: object, limit: int) -> list[dict] | None:
    """Try to extract a list of item dicts from a JSON response.

    Handles:
      1. Root-level list of objects
      2. Object with a known collection key whose value is a list
      3. Nested one level (e.g. {"data": {"articles": [...]}})

    Returns None if no item list can be identified.
    """
    # Case 1: root is a list of dicts
    if isinstance(data, list):
        items = [i for i in data if isinstance(i, dict)]
        return items[:limit] if items else None

    if not isinstance(data, dict):
        return None

    # Case 2: top-level known key
    for key in _JSON_COLLECTION_KEYS:
        val = data.get(key)
        if isinstance(val, list) and val and isinstance(val[0], dict):
            return val[:limit]

    # Case 3: one level of nesting (e.g. {"response": {"results": [...]}})
    for val in data.values():
        if isinstance(val, dict):
            for key in _JSON_COLLECTION_KEYS:
                inner = val.get(key)
                if isinstance(inner, list) and inner and isinstance(inner[0], dict):
                    return inner[:limit]

    return None


def _json_item_to_entry(item: dict) -> dict:
    """Map a JSON item dict to the same shape as an RSS entry.

    Tries common field names for title, link, summary, published.
    """
    def _first(*keys: str, default: str = "") -> str:
        for k in keys:
            v = item.get(k)
            if v and isinstance(v, str):
                return v
        return default

    title = _first("title", "headline", "name", "subject")
    link = _first("link", "url", "href", "source_url", "web_url")
    summary_raw = _first("summary", "description", "abstract", "body",
                         "content", "text", "snippet", "excerpt", "lead")
    published = _first("published", "publishedAt", "published_at", "pubDate",
                       "date", "created_at", "createdAt", "timestamp", "time",
                       "updated_at", "updatedAt")
    guid = _first("id", "guid", "_id", "uuid") or link

    # Nested source objects (e.g. {"source": {"name": "..."}})
    authors: list[str] = []
    author_val = item.get("author") or item.get("authors") or item.get("creator")
    if isinstance(author_val, str):
        authors = [author_val]
    elif isinstance(author_val, list):
        authors = [a if isinstance(a, str) else a.get("name", "") for a in author_val]

    tags: list[str] = []
    tags_val = item.get("tags") or item.get("categories") or item.get("keywords")
    if isinstance(tags_val, list):
        tags = [t if isinstance(t, str) else t.get("term", t.get("name", "")) for t in tags_val]
    elif isinstance(tags_val, str):
        tags = [t.strip() for t in tags_val.split(",") if t.strip()]

    summary = _clean_html(summary_raw) if summary_raw else ""
    if len(summary) > _MAX_SUMMARY_CHARS:
        summary = summary[:_MAX_SUMMARY_CHARS] + "..."

    return {
        "title": title,
        "link": link,
        "guid": guid,
        "summary": summary,
        "published": published,
        "authors": authors,
        "tags": tags,
    }


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


def _try_parse_json(raw: str, limit: int) -> list[dict] | None:
    """Attempt to parse raw text as JSON and extract items.

    Returns a list of entry dicts (same shape as RSS entries) on success,
    or None if parsing fails or no items can be identified.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None

    items = _extract_json_items(data, limit)
    if not items:
        return None

    return [_json_item_to_entry(item) for item in items]


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------

FEED_PARSE_DEF = ToolDefinition(
    name="feed_parse",
    description="Fetch and parse an RSS/Atom feed or JSON API endpoint. Returns structured entries "
                "with title, link, summary, published date, authors, and tags. For JSON API sources "
                "(source_type 'api'), automatically extracts items from common JSON structures. "
                "Also falls back to JSON parsing when RSS/Atom parsing yields no entries.",
    parameters=[
        ToolParameter(name="url", type="string",
                      description="The RSS/Atom feed URL or JSON API endpoint to fetch and parse"),
        ToolParameter(name="limit", type="number",
                      description="Max entries to return (default 20, max 100)",
                      required=False),
        ToolParameter(name="timeout", type="number",
                      description="HTTP timeout in seconds (default 30)",
                      required=False),
        ToolParameter(name="source_id", type="string",
                      description="UUID of the registered source (enables automatic reliability tracking)",
                      required=False),
        ToolParameter(name="source_type", type="string",
                      description="Source type hint: 'rss' (default) or 'api'. When 'api', skips "
                                  "RSS parsing and goes straight to JSON extraction.",
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

        content_type = ""
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
                content_type = resp.headers.get("content-type", "")
        except (httpx.TimeoutException, httpx.HTTPStatusError, Exception) as e:
            # Record fetch failure
            if track_source_id:
                await structured.record_source_fetch(track_source_id, success=False)
            if isinstance(e, httpx.TimeoutException):
                return f"Error: Feed fetch timed out after {timeout}s"
            elif isinstance(e, httpx.HTTPStatusError):
                return f"Error: HTTP {e.response.status_code} fetching feed"
            return f"Error: Failed to fetch feed: {e}"

        source_type = args.get("source_type", "").lower()
        is_json_response = "json" in content_type

        # ----- JSON API path -----
        # Use JSON parsing when explicitly api type, or as fallback
        entries = []
        feed_title = ""
        feed_link = url
        total_available = 0
        used_json = False

        if source_type == "api" or is_json_response:
            # Try JSON first for api sources or JSON content-type
            json_entries = _try_parse_json(raw, limit)
            if json_entries is not None:
                entries = json_entries
                total_available = len(entries)
                used_json = True
            elif source_type == "api":
                # Explicit API source but couldn't extract items — return helpful error
                # Include a truncated preview so the agent can adapt
                preview = raw[:500] + ("..." if len(raw) > 500 else "")
                if track_source_id:
                    await structured.record_source_fetch(track_source_id, success=False)
                return (f"Error: Could not extract items from JSON API response. "
                        f"Content-Type: {content_type}. "
                        f"Use http_request to fetch and parse manually.\n"
                        f"Response preview: {preview}")

        # ----- RSS/Atom path -----
        if not used_json and source_type != "api":
            try:
                feed = feedparser.parse(raw)
            except Exception as e:
                return f"Error: Failed to parse feed: {e}"

            if feed.bozo and not feed.entries:
                # Before giving up, try JSON fallback if response looks like JSON
                if is_json_response or raw.lstrip().startswith(("{", "[")):
                    json_entries = _try_parse_json(raw, limit)
                    if json_entries is not None:
                        entries = json_entries
                        total_available = len(entries)
                        used_json = True
                    else:
                        return f"Error: Malformed feed — {feed.bozo_exception}"
                else:
                    return f"Error: Malformed feed — {feed.bozo_exception}"

            if not used_json:
                feed_title = feed.feed.get("title", "")
                feed_link = feed.feed.get("link", "") or url
                total_available = len(feed.entries)

                for entry in feed.entries[:limit]:
                    summary = _clean_html(entry.get("summary", ""))
                    if len(summary) > _MAX_SUMMARY_CHARS:
                        summary = summary[:_MAX_SUMMARY_CHARS] + "..."
                    entry_guid = entry.get("id", "") or entry.get("guid", "") or entry.get("link", "")

                    entries.append({
                        "title": entry.get("title", ""),
                        "link": entry.get("link", ""),
                        "guid": entry_guid,
                        "summary": summary,
                        "published": entry.get("published", ""),
                        "authors": [a.get("name", "") for a in entry.get("authors", [])],
                        "tags": [t.get("term", "") for t in entry.get("tags", [])],
                    })

        # ----- Last-resort JSON fallback for RSS path with 0 entries -----
        if not entries and not used_json and (is_json_response or raw.lstrip().startswith(("{", "["))):
            json_entries = _try_parse_json(raw, limit)
            if json_entries is not None:
                entries = json_entries
                total_available = len(entries)
                used_json = True

        # Record successful fetch
        if track_source_id:
            await structured.record_source_fetch(track_source_id, success=True)

        result = {
            "feed_title": feed_title,
            "feed_link": feed_link,
            "entry_count": len(entries),
            "total_available": total_available,
            "entries": entries,
        }
        if used_json:
            result["parse_mode"] = "json_api"
        if source_id_str:
            result["source_id"] = source_id_str
        return json.dumps(result, indent=2, default=str)

    registry.register(FEED_PARSE_DEF, feed_parse_handler)
