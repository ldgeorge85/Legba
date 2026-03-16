"""Source fetcher — RSS/Atom + JSON API + GeoJSON.

Extracted from agent feed_tools.py, adapted for standalone use.
No LLM, no tool registry — just async fetch and parse.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import feedparser
import httpx

logger = logging.getLogger(__name__)

_USER_AGENT = "Legba-SA/1.0 (autonomous research agent; +https://github.com/ldgeorge85/legba)"
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_MAX_SUMMARY_CHARS = 800

# OAuth2 token cache: {cache_key: (token, expires_at_epoch)}
_token_cache: dict[str, tuple[str, float]] = {}


def _resolve_env(value: str) -> str:
    """Resolve a $ENV_VAR reference to its value, or return as-is."""
    if isinstance(value, str) and value.startswith("$"):
        return os.getenv(value[1:], "")
    return value


async def _get_oauth_token(auth_config: dict) -> str:
    """Get OAuth2 token via client_credentials or password grant. Caches until expiry."""
    token_url = auth_config.get("token_url", "")
    if not token_url:
        return ""

    cache_key = token_url

    # Check cache (with 60s buffer before expiry)
    if cache_key in _token_cache:
        token, expires_at = _token_cache[cache_key]
        if time.time() < expires_at - 60:
            return token

    grant_type = auth_config.get("grant_type", "client_credentials")

    data = {"grant_type": grant_type}

    if grant_type == "password":
        # Resource Owner Password Credentials (ROPC) — used by ACLED etc.
        username = _resolve_env(auth_config.get("username", ""))
        password = _resolve_env(auth_config.get("password", ""))
        client_id = _resolve_env(auth_config.get("client_id", ""))
        if not username or not password:
            logger.warning("OAuth2 password grant: username or password not resolved for %s", token_url)
            return ""
        data["username"] = username
        data["password"] = password
        if client_id:
            data["client_id"] = client_id
    else:
        # Client Credentials grant (default)
        client_id = _resolve_env(auth_config.get("client_id", ""))
        client_secret = _resolve_env(auth_config.get("client_secret", ""))
        if not client_id or not client_secret:
            logger.warning("OAuth2 client_id or client_secret not resolved for %s", token_url)
            return ""
        data["client_id"] = client_id
        data["client_secret"] = client_secret

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(token_url, data=data)
            if resp.status_code >= 400:
                # Log response body for debugging auth errors
                body = resp.text[:300] if resp.text else "(empty)"
                logger.error(
                    "OAuth2 token exchange failed for %s: HTTP %d — %s",
                    token_url, resp.status_code, body,
                )
                return ""
            resp_data = resp.json()
    except Exception as e:
        logger.error("OAuth2 token exchange failed for %s: %s", token_url, e)
        return ""

    token = resp_data.get("access_token", "")
    if not token:
        logger.error("OAuth2 response missing access_token from %s", token_url)
        return ""

    expires_in = resp_data.get("expires_in", 3600)
    _token_cache[cache_key] = (token, time.time() + expires_in)
    logger.info("OAuth2 token acquired from %s (expires in %ds, grant=%s)", token_url, expires_in, grant_type)
    return token

# Retry config for transient HTTP errors (429, 502, 503)
_RETRY_STATUS_CODES = {429, 502, 503}
_MAX_RETRIES = 3
_BACKOFF_SCHEDULE = [5, 15, 45]  # seconds — exponential-ish
_MAX_RETRY_AFTER = 60  # cap Retry-After header at 60s

_JSON_COLLECTION_KEYS = (
    "articles", "results", "data", "items", "events", "reports",
    "entries", "records", "stories", "hits", "feed", "posts",
    "documents", "content", "values", "rows", "features",
    "vulnerabilities",  # NVD + CISA KEV
    "value",  # OData (WHO, etc.)
    "observations",  # FRED
)


@dataclass
class FetchedEntry:
    """A single entry extracted from a feed or API response."""

    title: str = ""
    link: str = ""
    guid: str = ""
    summary: str = ""
    published: str = ""
    authors: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    # For GeoJSON / structured APIs — pass through raw fields the normalizer needs
    raw_data: dict = field(default_factory=dict)


@dataclass
class FetchResult:
    """Result of fetching a single source."""

    success: bool = False
    entries: list[FetchedEntry] = field(default_factory=list)
    feed_title: str = ""
    parse_mode: str = ""  # "rss", "json_api", "geojson", "csv"
    error: str = ""
    http_status: int = 0
    fetch_duration_ms: int = 0


def _clean_html(raw: str) -> str:
    """Extract readable text from HTML. Trafilatura first, regex fallback."""
    if not raw or "<" not in raw:
        return raw
    try:
        import trafilatura
        text = trafilatura.extract(
            raw, include_links=False, include_images=False,
            include_tables=False, no_fallback=False,
        )
        if text and len(text.strip()) > 20:
            return text.strip()
    except Exception:
        pass
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"&#?\w+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_json_items(data: object, limit: int) -> list[dict] | None:
    """Extract a list of item dicts from a JSON response."""
    if isinstance(data, list):
        items = [i for i in data if isinstance(i, dict)]
        return items[:limit] if items else None

    if not isinstance(data, dict):
        return None

    # Top-level known key
    for key in _JSON_COLLECTION_KEYS:
        val = data.get(key)
        if isinstance(val, list) and val and isinstance(val[0], dict):
            return val[:limit]

    # One level nested
    for val in data.values():
        if isinstance(val, dict):
            for key in _JSON_COLLECTION_KEYS:
                inner = val.get(key)
                if isinstance(inner, list) and inner and isinstance(inner[0], dict):
                    return inner[:limit]

    return None


def _json_item_to_entry(item: dict) -> FetchedEntry:
    """Map a JSON item dict to a FetchedEntry."""
    def _first(*keys: str, default: str = "") -> str:
        for k in keys:
            v = item.get(k)
            if v and isinstance(v, str):
                return v
        return default

    title = _first("title", "headline", "name", "subject")
    link = _first("link", "url", "href", "source_url", "web_url")
    summary_raw = _first(
        "summary", "description", "abstract", "body",
        "content", "text", "snippet", "excerpt", "lead",
    )
    published = _first(
        "published", "publishedAt", "published_at", "pubDate",
        "dateTime", "datetime", "date_time",
        "seendate", "seen_date",
        "event_date", "eventDate",
        "date", "timestamp", "time",
        "created_at", "createdAt",
        "updated_at", "updatedAt",
    )
    guid = _first("id", "guid", "_id", "uuid") or link

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

    return FetchedEntry(
        title=title,
        link=link,
        guid=guid,
        summary=summary,
        published=published,
        authors=authors,
        tags=tags,
        raw_data=item,
    )


def _parse_geojson_features(data: dict, limit: int) -> list[FetchedEntry]:
    """Extract entries from GeoJSON FeatureCollection."""
    features = data.get("features", [])
    entries = []
    for feat in features[:limit]:
        props = feat.get("properties", {})
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates", [])

        entry = _json_item_to_entry(props)
        # Preserve geometry for geo resolution
        if coords:
            entry.raw_data["_geometry"] = geom
        entries.append(entry)
    return entries


def _build_url(source_url: str, query_template: str, last_fetch: datetime | None) -> str:
    """Build fetch URL from template with placeholder substitution.

    Supports:
      - {timespan}, {since_iso}, {date_today}, {date_yesterday} — dynamic time values
      - $ENV_VAR — resolved from environment, URL-encoded for safe embedding in query strings
    """
    if not query_template:
        return source_url

    import os
    from urllib.parse import quote

    url = query_template
    now = datetime.now(timezone.utc)

    from datetime import timedelta
    default_since = now - timedelta(days=7)

    replacements = {
        "{since_iso}": (last_fetch or default_since).isoformat(),
        "{timespan}": "24h" if not last_fetch else "15min",
        "{date_today}": now.strftime("%Y-%m-%d"),
        "{date_yesterday}": (now - timedelta(days=1)).strftime("%Y-%m-%d"),
    }

    # Compute timespan from last fetch
    if last_fetch:
        delta_minutes = int((now - last_fetch).total_seconds() / 60)
        if delta_minutes < 60:
            replacements["{timespan}"] = f"{max(delta_minutes, 15)}min"
        elif delta_minutes < 1440:
            replacements["{timespan}"] = f"{delta_minutes // 60}h"
        else:
            replacements["{timespan}"] = f"{min(delta_minutes // 1440, 30)}d"

    for placeholder, value in replacements.items():
        url = url.replace(placeholder, str(value))

    # Resolve $ENV_VAR references (URL-encode values for query string safety)
    import re as _re
    for match in _re.findall(r'\$([A-Z_][A-Z0-9_]*)', url):
        env_val = os.getenv(match, "")
        if env_val:
            url = url.replace(f"${match}", quote(env_val, safe=""))
        else:
            logger.warning("Env var $%s not set in query template", match)

    return url


async def fetch_source(
    url: str,
    *,
    source_type: str = "rss",
    query_template: str = "",
    auth_config: dict | None = None,
    last_fetch: datetime | None = None,
    timeout: int = 30,
    limit: int = 50,
) -> FetchResult:
    """Fetch and parse a single source. Returns structured entries.

    Args:
        url: Source URL (base URL if query_template is set)
        source_type: "rss", "api", "geojson", "static_json", "csv"
        query_template: URL pattern with {placeholders}
        auth_config: {"type": "api_key", "header": "X-Api-Key", "value": "..."}
                     or {"type": "query_param", "key": "api_key", "value": "..."}
                     or {"type": "bearer", "token": "..."} (static token)
                     or {"type": "bearer", "token_url": "...", "client_id": "...", "client_secret": "..."} (OAuth2 client credentials)
                     or {"type": "bearer", "token_url": "...", "grant_type": "password", "username": "...", "password": "...", "client_id": "..."} (OAuth2 ROPC)
        last_fetch: Last successful fetch time (for incremental queries)
        timeout: HTTP timeout in seconds
        limit: Max entries to return
    """
    start = datetime.now(timezone.utc)
    fetch_url = _build_url(url, query_template, last_fetch)

    # Build headers
    headers = {"User-Agent": _USER_AGENT}
    params = {}

    # Skip query_param auth when query_template already resolved env vars in the URL.
    # Header-based auth (api_key type) always applies since it's not in the URL.
    if auth_config and not (query_template and auth_config.get("type") == "query_param"):
        auth_type = auth_config.get("type", "")
        if auth_type == "api_key" and auth_config.get("header"):
            headers[auth_config["header"]] = auth_config.get("value", "")
        elif auth_type == "query_param" and auth_config.get("key"):
            # Append to URL
            sep = "&" if "?" in fetch_url else "?"
            fetch_url = f"{fetch_url}{sep}{auth_config['key']}={auth_config.get('value', '')}"
        elif auth_type == "bearer":
            # Static token or OAuth2 client credentials exchange
            token = _resolve_env(auth_config.get("token", ""))
            if not token and auth_config.get("token_url"):
                token = await _get_oauth_token(auth_config)
            if token:
                headers["Authorization"] = f"Bearer {token}"

    result = FetchResult()

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            headers=headers,
            follow_redirects=True,
        ) as client:
            resp = await client.get(fetch_url)
            # UA retry on 403/405
            if resp.status_code in (403, 405):
                headers["User-Agent"] = _BROWSER_USER_AGENT
                async with httpx.AsyncClient(
                    timeout=timeout,
                    headers=headers,
                    follow_redirects=True,
                ) as browser_client:
                    resp = await browser_client.get(fetch_url)

            # Retry on transient errors (429 rate-limit, 502/503 server errors)
            if resp.status_code in _RETRY_STATUS_CODES:
                for attempt in range(_MAX_RETRIES):
                    wait = _BACKOFF_SCHEDULE[attempt]
                    # Honour Retry-After header if present (429 often includes it)
                    retry_after = resp.headers.get("retry-after")
                    if retry_after:
                        try:
                            wait = min(int(retry_after), _MAX_RETRY_AFTER)
                        except ValueError:
                            # Retry-After can be an HTTP-date — ignore, use backoff
                            pass

                    logger.warning(
                        "HTTP %d from %s — retry %d/%d in %ds",
                        resp.status_code, fetch_url, attempt + 1, _MAX_RETRIES, wait,
                    )
                    await asyncio.sleep(wait)

                    resp = await client.get(fetch_url)
                    if resp.status_code not in _RETRY_STATUS_CODES:
                        break

            result.http_status = resp.status_code
            resp.raise_for_status()
            raw = resp.text
            content_type = resp.headers.get("content-type", "")

            # Handle empty responses (e.g. GDELT returns 200 with empty body)
            if not raw or not raw.strip():
                result.error = "Empty response body"
                result.fetch_duration_ms = _elapsed_ms(start)
                return result

    except httpx.TimeoutException:
        result.error = f"Timeout after {timeout}s"
        result.fetch_duration_ms = _elapsed_ms(start)
        return result
    except httpx.HTTPStatusError as e:
        result.error = f"HTTP {e.response.status_code}"
        result.http_status = e.response.status_code
        result.fetch_duration_ms = _elapsed_ms(start)
        return result
    except Exception as e:
        result.error = str(e)
        result.fetch_duration_ms = _elapsed_ms(start)
        return result

    is_json_response = "json" in content_type

    # --- CSV path (FIRMS etc.) ---
    if source_type == "csv":
        try:
            import csv
            import io
            reader = csv.DictReader(io.StringIO(raw))
            rows = []
            for row in reader:
                rows.append(row)
                if len(rows) >= limit:
                    break
            if rows:
                result.entries = [_json_item_to_entry(r) for r in rows]
                result.parse_mode = "csv"
                result.success = True
            else:
                result.error = "CSV response has no data rows"
        except Exception as e:
            result.error = f"CSV parse error: {e}"
        result.fetch_duration_ms = _elapsed_ms(start)
        return result

    # --- GeoJSON path ---
    if source_type == "geojson":
        try:
            data = json.loads(raw)
            if data.get("type") == "FeatureCollection":
                result.entries = _parse_geojson_features(data, limit)
                result.parse_mode = "geojson"
                result.success = True
            else:
                # Try as regular JSON
                items = _extract_json_items(data, limit)
                if items:
                    result.entries = [_json_item_to_entry(i) for i in items]
                    result.parse_mode = "json_api"
                    result.success = True
                else:
                    result.error = "GeoJSON response has no FeatureCollection or extractable items"
        except json.JSONDecodeError as e:
            result.error = f"JSON parse error: {e}"
        result.fetch_duration_ms = _elapsed_ms(start)
        return result

    # --- JSON API path ---
    if source_type in ("api", "static_json") or is_json_response:
        try:
            data = json.loads(raw)
            items = _extract_json_items(data, limit)
            if items:
                result.entries = [_json_item_to_entry(i) for i in items]
                result.parse_mode = "json_api"
                result.success = True
            elif isinstance(data, dict) and source_type in ("api", "static_json"):
                # Single-object API (e.g. Frankfurter exchange rates) —
                # wrap the entire response as one entry for the normalizer
                result.entries = [_json_item_to_entry(data)]
                result.parse_mode = "json_api"
                result.success = True
            else:
                result.error = "Could not extract items from JSON response"
        except json.JSONDecodeError as e:
            result.error = f"JSON parse error: {e}"

        if result.success:
            result.fetch_duration_ms = _elapsed_ms(start)
            return result

        # If explicit API type, don't fall through to RSS
        if source_type in ("api", "static_json"):
            result.fetch_duration_ms = _elapsed_ms(start)
            return result

    # --- RSS/Atom path ---
    try:
        feed = feedparser.parse(raw)
    except Exception as e:
        result.error = f"Feed parse error: {e}"
        result.fetch_duration_ms = _elapsed_ms(start)
        return result

    if feed.bozo and not feed.entries:
        # JSON fallback for malformed feeds
        if is_json_response or raw.lstrip().startswith(("{", "[")):
            try:
                data = json.loads(raw)
                items = _extract_json_items(data, limit)
                if items:
                    result.entries = [_json_item_to_entry(i) for i in items]
                    result.parse_mode = "json_api"
                    result.success = True
                    result.fetch_duration_ms = _elapsed_ms(start)
                    return result
            except json.JSONDecodeError:
                pass
        result.error = f"Malformed feed: {feed.bozo_exception}"
        result.fetch_duration_ms = _elapsed_ms(start)
        return result

    result.feed_title = feed.feed.get("title", "")
    result.parse_mode = "rss"

    for entry in feed.entries[:limit]:
        summary = _clean_html(entry.get("summary", ""))
        if len(summary) > _MAX_SUMMARY_CHARS:
            summary = summary[:_MAX_SUMMARY_CHARS] + "..."
        entry_guid = entry.get("id", "") or entry.get("guid", "") or entry.get("link", "")

        # Capture feedparser's pre-parsed struct_time fields for the normalizer.
        # These are more reliable than string parsing since feedparser handles
        # many date formats internally.
        raw_data: dict = {}
        for ts_field in ("published_parsed", "updated_parsed", "created_parsed"):
            ts_val = entry.get(ts_field)
            if ts_val is not None:
                raw_data[ts_field] = ts_val

        result.entries.append(FetchedEntry(
            title=entry.get("title", ""),
            link=entry.get("link", ""),
            guid=entry_guid,
            summary=summary,
            published=entry.get("published", ""),
            authors=[a.get("name", "") for a in entry.get("authors", [])],
            tags=[t.get("term", "") for t in entry.get("tags", [])],
            raw_data=raw_data,
        ))

    # Last-resort JSON fallback for RSS with 0 entries
    if not result.entries and (is_json_response or raw.lstrip().startswith(("{", "["))):
        try:
            data = json.loads(raw)
            items = _extract_json_items(data, limit)
            if items:
                result.entries = [_json_item_to_entry(i) for i in items]
                result.parse_mode = "json_api"
        except json.JSONDecodeError:
            pass

    result.success = True
    result.fetch_duration_ms = _elapsed_ms(start)
    return result


def _elapsed_ms(start: datetime) -> int:
    return int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
