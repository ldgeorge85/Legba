# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``web_access`` pack's external tool handlers (S6 — external evidence).

Two tools let an agentic assessor reach OUTSIDE the substrate for evidence:

  * ``web_fetch``  — GET one operator-or-planner supplied URL and return its
                     (size-capped) text body + status.
  * ``web_search`` — query an operator-configured search endpoint (a SearXNG
                     ``/search?format=json`` instance, or any JSON search API
                     with a compatible ``results[]`` shape) and return the top
                     result titles + URLs + snippets.

Both egress EXCLUSIVELY through :func:`guarded_async_client` — the SAME
``SsrfGuardedTransport`` every ingress fetcher uses (``sources/_egress.py``).
A URL that resolves to a private / loopback / link-local / metadata address is
REFUSED before connect; the guard re-runs on every redirect hop. There is no
bare ``httpx`` client anywhere in this module — a web tool that tried to reach
``127.0.0.1`` / ``169.254.169.254`` / RFC-1918 is blocked exactly as an
ingress fetcher would be, and the block is classified as a CLEAN tool failure
(``ToolResult(status="failed")``) rather than a crash, so the GATHER loop folds
the error back to the planner instead of dropping the run.

Endpoint provenance (web_search):
  The search endpoint is NOT planner-controlled. It comes from the pack's
  ``web_search`` ToolSpec ``config['endpoint']`` (operator-authored in the
  ``web_access`` descriptor), falling back to ``LEGBA_WEB_SEARCH_ENDPOINT``.
  The planner supplies only the query string. This keeps the search surface
  an operator decision — the planner cannot point the search at an arbitrary
  internal JSON API (and even if the endpoint were hostile, the egress guard
  still bounds where the request can land).

A handler is ``async (call, pack, ctx) -> ToolResult`` — it NEVER decides
agency; resolution + the governor have already admitted the call. Read-only:
neither tool writes to the substrate (that is ``write_tools.py``); they return
fetched text the assessor can cite, with the originating URL echoed so the
finding's provenance can record where the external evidence came from.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from ...schemas.action_pack import ActionPack
from ...sources._egress import EgressBlockedError, guarded_async_client
from .tools import ToolCall, ToolContext, ToolResult

logger = logging.getLogger(__name__)

WEB_ACCESS_PACK_ID = "web_access"

WEB_ACCESS_TOOLS = (
    "web_fetch",
    "web_search",
)

# Defensive caps — a guarded client still returns whatever the public host
# serves; bound the body we read into the LLM conversation so a multi-MB page
# can't blow the context window or pin memory. The planner's GATHER round
# already truncates tool output, but cap at the source too.
_MAX_FETCH_BYTES = 200_000
_MAX_SEARCH_RESULTS = 10
_DEFAULT_TIMEOUT_SECONDS = 15.0
_USER_AGENT = "legba-web-tools/1.0 (+https://github.com/ldgeorge85/legba)"

# Env fallback for the search endpoint when a pack's web_search ToolSpec does
# not pin one in config. Unset by default — web_search then returns a clean
# failure naming the missing endpoint (no silent empty result).
_SEARCH_ENDPOINT_ENV = "LEGBA_WEB_SEARCH_ENDPOINT"


def _tool_config(pack: ActionPack, tool_name: str) -> dict[str, Any]:
    for t in pack.tools:
        if t.name == tool_name:
            return dict(t.config)
    return {}


def _decode_body(response: httpx.Response) -> str:
    """Best-effort text decode, capped at ``_MAX_FETCH_BYTES``.

    ``response.text`` honors the charset; we slice the DECODED text so the cap
    is a character bound on what enters the conversation (a byte slice could
    split a multi-byte char). The slice marker is explicit so the planner knows
    the body was truncated.
    """
    text = response.text
    if len(text) > _MAX_FETCH_BYTES:
        return text[:_MAX_FETCH_BYTES] + "\n…[truncated by web_fetch cap]"
    return text


async def web_fetch_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    """GET one URL through the SSRF-guarded transport; return its text body.

    ``args``:
      * ``url`` (required) — the absolute http(s) URL to fetch.

    Every connection (including redirect hops) is validated by
    :class:`SsrfGuardedTransport`. A non-public target raises
    :class:`EgressBlockedError`, which we report as a ``failed`` ToolResult
    (``error="egress_blocked: …"``) — the realistic SSRF vector (a planner
    pointing the tool at an internal address) is refused, not crashed on.
    """
    url = str(call.args.get("url", "")).strip()
    if not url:
        return ToolResult(status="failed", error="web_fetch requires a 'url' arg")
    if not (url.startswith("http://") or url.startswith("https://")):
        return ToolResult(
            status="failed",
            error=f"web_fetch refuses non-http(s) url {url!r}",
        )

    cfg = _tool_config(pack, "web_fetch")
    timeout = float(cfg.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS)
    try:
        async with guarded_async_client(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            response = await client.get(url)
    except EgressBlockedError as exc:
        # The SSRF guard refused a non-public target (or a redirect to one).
        # Clean tool failure — the planner sees it and stops, the run survives.
        logger.warning("web_fetch.egress_blocked url=%s err=%s", url, exc)
        return ToolResult(status="failed", error=f"egress_blocked: {exc!s}")
    except httpx.HTTPError as exc:
        logger.warning("web_fetch.http_error url=%s err=%s", url, exc)
        return ToolResult(status="failed", error=f"fetch_failed: {exc!s}")

    body = _decode_body(response)
    return ToolResult(
        status="completed",
        output={
            "url": str(response.url),
            "status_code": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            "body": body,
            "truncated": len(response.text) > _MAX_FETCH_BYTES,
        },
        units=1,
    )


def _parse_search_results(payload: Any, limit: int) -> list[dict[str, Any]]:
    """Coerce a SearXNG-/JSON-search-style payload into ``[{title,url,snippet}]``.

    Accepts the SearXNG ``{"results": [...]}`` shape (the reference endpoint);
    each result's ``title`` / ``url`` / ``content`` map onto our flat shape.
    A non-dict payload, or one with no ``results`` list, yields ``[]`` (the
    caller reports a clean failure rather than fabricating hits).
    """
    if not isinstance(payload, dict):
        return []
    raw = payload.get("results")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:limit]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        out.append({
            "title": str(item.get("title") or "")[:512],
            "url": url,
            "snippet": str(item.get("content") or item.get("snippet") or "")[:1024],
        })
    return out


async def web_search_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    """Query an operator-configured search endpoint; return the top results.

    ``args``:
      * ``query`` (required) — the search query string (planner-supplied).
      * ``limit`` (optional) — max results, clamped to ``_MAX_SEARCH_RESULTS``.

    The endpoint is OPERATOR-pinned (pack ``web_search`` ToolSpec
    ``config['endpoint']``, else ``LEGBA_WEB_SEARCH_ENDPOINT``) — never
    planner-supplied. The request egresses through the SAME guarded transport
    as ``web_fetch``; an endpoint resolving non-public is refused, not crashed.
    With no endpoint configured the tool returns a clean failure naming the
    seam (no silent empty result, no fabricated hits).
    """
    query = str(call.args.get("query", "")).strip()
    if not query:
        return ToolResult(status="failed", error="web_search requires a 'query' arg")

    cfg = _tool_config(pack, "web_search")
    endpoint = str(cfg.get("endpoint") or os.environ.get(_SEARCH_ENDPOINT_ENV, "")).strip()
    if not endpoint:
        return ToolResult(
            status="failed",
            error=(
                "web_search has no endpoint configured — set the web_access "
                f"pack's web_search config['endpoint'] or {_SEARCH_ENDPOINT_ENV}"
            ),
        )
    if not (endpoint.startswith("http://") or endpoint.startswith("https://")):
        return ToolResult(
            status="failed",
            error=f"web_search endpoint must be http(s), got {endpoint!r}",
        )

    limit = max(1, min(_MAX_SEARCH_RESULTS, int(call.args.get("limit", 5))))
    timeout = float(cfg.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS)
    # SearXNG-compatible params; operators can override via config['params'].
    params: dict[str, str] = {"q": query, "format": "json"}
    extra = cfg.get("params")
    if isinstance(extra, dict):
        params.update({str(k): str(v) for k, v in extra.items()})

    try:
        async with guarded_async_client(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            response = await client.get(endpoint, params=params)
    except EgressBlockedError as exc:
        logger.warning("web_search.egress_blocked endpoint=%s err=%s", endpoint, exc)
        return ToolResult(status="failed", error=f"egress_blocked: {exc!s}")
    except httpx.HTTPError as exc:
        logger.warning("web_search.http_error endpoint=%s err=%s", endpoint, exc)
        return ToolResult(status="failed", error=f"search_failed: {exc!s}")

    if response.status_code >= 400:
        return ToolResult(
            status="failed",
            error=f"search endpoint returned HTTP {response.status_code}",
        )
    try:
        # httpx raises json.JSONDecodeError (a ValueError subclass) on a
        # non-JSON body — caught by the ValueError clause below.
        payload = response.json()
    except ValueError as exc:
        return ToolResult(status="failed", error=f"search response not JSON: {exc!s}")

    results = _parse_search_results(payload, limit)
    return ToolResult(
        status="completed",
        output={"query": query, "results": results, "count": len(results)},
        units=1,
    )


def register_web_access_tools(registry: "Any") -> None:
    """Register the two external handlers (called by ``default_tool_registry``)."""
    registry.register("web_fetch", web_fetch_tool)
    registry.register("web_search", web_search_tool)


__all__ = [
    "WEB_ACCESS_PACK_ID",
    "WEB_ACCESS_TOOLS",
    "register_web_access_tools",
    "web_fetch_tool",
    "web_search_tool",
]
