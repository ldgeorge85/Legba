"""
HTTP request tool: http_request

HTML responses are automatically cleaned via trafilatura (readability
extraction) with a regex fallback to strip tags.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from ....shared.schemas.tools import ToolDefinition, ToolParameter

# Module-level default, overridden by register() with config value
_max_timeout: int = 30

_DEFAULT_USER_AGENT = "Legba-SA/1.0 (autonomous research agent; +https://github.com/ldgeorge85/legba)"
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_MAX_BODY_CHARS = 12000

# Within-cycle cache for GET responses. Cleared at cycle start.
_get_cache: dict[str, str] = {}


def _extract_content(body: str, content_type: str) -> str:
    """Extract readable text from HTML responses. Pass through non-HTML."""
    if "html" not in content_type:
        return body
    try:
        import trafilatura
        text = trafilatura.extract(body, include_links=True, include_images=False,
                                   include_tables=True, no_fallback=False)
        if text and len(text.strip()) > 50:
            return text.strip()
    except Exception:
        pass
    # Fallback: strip tags and collapse whitespace
    text = re.sub(r"<script[^>]*>.*?</script>", " ", body, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", body, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"&#?\w+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clear_http_cache() -> None:
    """Clear the within-cycle HTTP cache. Called at WAKE phase."""
    _get_cache.clear()


def get_definitions() -> list[tuple[ToolDefinition, Any]]:
    return [(HTTP_DEF, http_request)]


def register(registry, *, agent_config=None, **_deps) -> None:
    """Register HTTP tools. Accepts agent_config for timeout ceiling."""
    global _max_timeout
    if agent_config is not None:
        _max_timeout = agent_config.http_timeout
        HTTP_DEF.parameters[4].description = (
            f"Timeout in seconds (default {_max_timeout}, max {_max_timeout})"
        )
    registry.register(HTTP_DEF, http_request)


HTTP_DEF = ToolDefinition(
    name="http_request",
    description="Make an HTTP request",
    parameters=[
        ToolParameter(name="url", type="string", description="The URL to request"),
        ToolParameter(name="method", type="string", description="HTTP method (GET, POST, PUT, DELETE, etc.)", required=False),
        ToolParameter(name="headers", type="object", description="Request headers", required=False),
        ToolParameter(name="body", type="string", description="Request body", required=False),
        ToolParameter(name="timeout", type="number", description=f"Timeout in seconds (default {_max_timeout}, max {_max_timeout})", required=False),
    ],
)


async def http_request(args: dict) -> str:
    url = args.get("url", "")
    if not url:
        return "Error: No URL provided"

    method = args.get("method", "GET").upper()
    headers = args.get("headers", {})
    body = args.get("body")
    requested = int(args.get("timeout", _max_timeout))
    timeout = min(requested, _max_timeout)

    # Cache check for GET requests
    if method == "GET" and url in _get_cache:
        return f"[CACHED from earlier this cycle]\n{_get_cache[url]}"

    # Inject default User-Agent if not already set
    if "User-Agent" not in headers and "user-agent" not in headers:
        headers["User-Agent"] = _DEFAULT_USER_AGENT

    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            response = await client.request(
                method=method,
                url=url,
                content=body,
            )

            # On 403/405 GET requests with bot UA, retry with browser UA
            if (response.status_code in (403, 405)
                    and method == "GET"
                    and headers.get("User-Agent") == _DEFAULT_USER_AGENT):
                retry_headers = dict(headers)
                retry_headers["User-Agent"] = _BROWSER_USER_AGENT
                async with httpx.AsyncClient(timeout=timeout, headers=retry_headers) as retry_client:
                    response = await retry_client.request(method=method, url=url)

            content_type = response.headers.get("content-type", "")
            body = _extract_content(response.text, content_type)
            if len(body) > _MAX_BODY_CHARS:
                body = body[:_MAX_BODY_CHARS] + "\n... (truncated)"

            result = f"HTTP {response.status_code} {response.reason_phrase}\n"
            result += f"Content-Type: {content_type}\n"
            result += f"Body:\n{body}"

            # Cache successful GET responses for this cycle
            if method == "GET" and response.status_code < 400:
                _get_cache[url] = result

            return result

    except httpx.TimeoutException:
        return f"Error: Request timed out after {timeout}s"
    except Exception as e:
        return f"Error: HTTP request failed: {e}"
