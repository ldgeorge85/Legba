# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Anthropic Messages API leg arms D and E call directly (Legba not involved).

Raw ``httpx`` on purpose, the same wire the platform's own handler speaks
(``legba.data.stack.llm.anthropic``: ``x-api-key``, ``anthropic-version``
``2023-06-01``, ``POST /v1/messages``) — the bake-off adds no SDK dependency
to the tree. The frontier model is whatever the ``llm.anthropic.opus_4_7``
stack component SERVES (``model_name``), so B, C, D and E run one model
(design §2: "the only thing that varies is what the model can read").

"Effort and thinking left at the route's defaults" (§2): the consult route's
handler sends neither ``thinking`` nor an ``effort``; neither do these
requests. No temperature: the model line rejects sampling parameters and the
handler drops it too.

Nothing in this module is called by the G0 lane: :func:`post` is reached only
through a paid gate that refused (``common.PaidGate``).
"""

from __future__ import annotations

from typing import Any

API_VERSION = "2023-06-01"
#: Server-side web tools — the dynamic-filtering variants the Opus 5.5 / 5 /
#: 4.6+ line takes (re-checked against the claude-api reference for
#: ``claude-opus-5-5``; the older ``web_search_20250305`` /
#: ``web_fetch_20250910`` are the basic variants for pre-4.6 models).
WEB_SEARCH_TOOL = "web_search_20260209"
WEB_FETCH_TOOL = "web_fetch_20260209"


def usage_of(response: dict[str, Any]) -> dict[str, int]:
    """Provider-reported usage, every billed dimension (absent → 0)."""
    u = response.get("usage") or {}
    stu = u.get("server_tool_use") or {}
    return {
        "input_tokens": int(u.get("input_tokens") or 0),
        "output_tokens": int(u.get("output_tokens") or 0),
        "cache_read_tokens": int(u.get("cache_read_input_tokens") or 0),
        "cache_write_tokens": int(u.get("cache_creation_input_tokens") or 0),
        "web_search_requests": int(stu.get("web_search_requests") or 0),
        "web_fetch_requests": int(stu.get("web_fetch_requests") or 0),
    }


def text_of(response: dict[str, Any]) -> str:
    return "".join(
        b.get("text") or "" for b in response.get("content") or [] if b.get("type") == "text"
    )


def cited_urls(response: dict[str, Any]) -> list[str]:
    """URLs the answer's text blocks cite (web citations), first-seen order,
    plus bare URLs written in the text."""
    import re

    urls: list[str] = []
    for b in response.get("content") or []:
        if b.get("type") != "text":
            continue
        for c in b.get("citations") or []:
            if c.get("url"):
                urls.append(str(c["url"]))
        urls += re.findall(r"https?://[^\s<>\]\)\"']+[^\s<>\]\)\"'.,;:!?]", b.get("text") or "")
    return list(dict.fromkeys(urls))


async def post(endpoint: str, api_key: str, body: dict[str, Any], *, timeout: float = 900.0) -> dict[str, Any]:
    """One non-streaming Messages call. Reached only past a paid gate."""
    import httpx

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0)) as client:
        r = await client.post(
            f"{endpoint.rstrip('/')}/v1/messages",
            json=body,
            headers={
                "x-api-key": api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
        )
        r.raise_for_status()
        return r.json()
