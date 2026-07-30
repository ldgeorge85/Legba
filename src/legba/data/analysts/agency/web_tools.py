# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``web_access`` pack's external tool handlers (S6 — external evidence).

Two tools let an agentic assessor reach OUTSIDE the substrate for evidence:

  * ``web_fetch``  — GET one operator-or-planner supplied URL and return its
                     (size-capped) text body + status.
  * ``web_search`` — run a query through the ``search_provider`` stack family
                     (:mod:`legba.data.stack.search`) and return the top result
                     titles + URLs + snippets, PLUS an explicit degradation
                     verdict.

Both egress EXCLUSIVELY through :func:`guarded_async_client` — the SAME
``SsrfGuardedTransport`` every ingress fetcher uses (``sources/_egress.py``).
A URL that resolves to a private / loopback / link-local / metadata address is
REFUSED before connect; the guard re-runs on every redirect hop. There is no
bare ``httpx`` client anywhere in this module — a web tool that tried to reach
``127.0.0.1`` / ``169.254.169.254`` / RFC-1918 is blocked exactly as an
ingress fetcher would be, and the block is classified as a CLEAN tool failure
(``ToolResult(status="failed")``) rather than a crash, so the GATHER loop folds
the error back to the planner instead of dropping the run.

Provider provenance (web_search) — a four-rung ladder, all rungs OPERATOR-owned:

  1. the ``web_search`` ToolSpec's ``config['provider']`` ``StackRef``
     (``factory_kind: stack_ref``) resolved by
     :func:`legba.data.stack.search.resolve_tool_search_route`, bound by the
     runtime into ``ctx.search``;
  2. a handler the runtime bound with no descriptor ref (``ctx.search`` alone);
  3. the LEGACY operator-pinned endpoint — ``config['endpoint']``, else
     ``LEGBA_WEB_SEARCH_ENDPOINT``. Unchanged and fully supported: it is the
     zero-code-change way to point at a SearXNG instance, and it now runs
     through the SAME ``searxng`` handler (which is where
     ``_parse_search_results`` moved to), so it gains the degradation read for
     free;
  4. nothing configured → a clean failure NAMING THE SEAM. Never a silent
     empty result.

  A declared route that the runtime did not bind is rung-4-shaped too: it fails
  loudly rather than returning zero hits, because "the provider is missing" and
  "the web has nothing" must never share a wire shape.

  The planner supplies only the query string; it can never point search at an
  arbitrary internal JSON API, and even a hostile endpoint is bounded by the
  egress guard.

DEGRADATION IS LOUD (the reason this tool stopped hand-rolling its own parse):
  A meta-search instance whose upstream engines are CAPTCHA'd / rate-limited /
  banned still answers **HTTP 200** — with a shorter, or completely empty,
  ``results[]`` and the refusing engines named in ``unresponsive_engines``. A
  live probe of the deployed instance returned 200 with 20 results while
  ``brave: too many requests``, ``duckduckgo: CAPTCHA`` and
  ``startpage: CAPTCHA`` sat in that field. Had every engine refused, the same
  200 would have carried ``results: []`` — and an analyst reading a bare empty
  list writes "no reporting on X exists", manufacturing FALSE ABSENCE EVIDENCE.

EMPTY IS SUSPECT BY DEFAULT — absence is MEASURED, not assumed:
  Reading ``unresponsive_engines`` catches only the degradation the provider
  ADMITS. Over a 5-engine meta-search a genuinely empty result set for a real
  query is close to impossible — even a nonsense query returns unrelated noise
  — so in practice a clean-looking empty means BROKEN (every engine banned, an
  encoding bug, a network fault), not "the web contains nothing".

  So a zero-result response with NO admitted degradation is NOT trusted. This
  tool issues ONE bounded CONTROL PROBE through the same provider — a fixed,
  deliberately high-yield query — and decides from the outcome
  (:mod:`legba.data.stack.search.liveness`, which is also the single
  control-query canary; there is no second one). The verdict is cached per
  provider for a short TTL, so a run with several empties costs ONE probe.

  Five outcomes reach the planner in the ToolResult itself:

    ``status=completed``, ``degraded=false``, ``count>0``
        results, fully served.
    ``status=completed``, ``count=0``, output ``status="empty_verified"``,
    ``supports_absence_claim=true``
        The control probe proved the engine set is answering, so the empty is
        real FOR THIS QUERY. ``absence_statement`` carries the ONLY licensed
        phrasing — a SCOPED absence ("these engines returned nothing for this
        query"), never "X does not exist".
    ``status=completed``, ``degraded=true``, ``count>0``
        partial service: usable hits PLUS the named unresponsive engines and
        ``supports_absence_claim=false`` (the missing engines could have
        carried the contradicting evidence). Per the spec this does NOT retry
        into a fallback — that would hide the ban and double-count the query
        against engines already unhappy with us.
    ``status=failed``, ``error="search_degraded_no_results: …"``
        EVERY result was lost to degradation the provider ADMITTED.
        Deliberately a clean tool FAILURE rather than an empty success: a
        ``completed`` result with ``count=0`` is exactly the shape that gets
        summarized as "no results found", and this state is UNKNOWN, not
        absence. The GATHER loop folds the error back to the planner and the
        run survives.
    ``status=failed``, ``error="search_liveness_unverified: …"``
        Zero results, no admitted degradation, and the control probe ALSO came
        back empty (or could not run). The plane is broken; same failure class,
        same reasoning.

DEFERRAL, NOT RETRY (the ``deferral`` block on a failed result):
  Every deferrable failure — degraded-empty, a dead/failed control probe, an
  unresolved declared provider, a transient — carries a ``deferral`` block:
  ``{defer, reason, retry_after_seconds, not_before, consecutive_failures,
  escalate}``, an exponential per-provider backoff capped at an hour.

  A caller (e.g. the corpus researcher draining the standing-question backlog)
  consumes it in three rules — read it back with
  :func:`legba.data.stack.search.deferral_from_tool_output`:

    1. do NOT retry inside this run (hammering engines that are already
       refusing worsens the ban and double-counts against them — the existing
       no-retry-on-degraded rule, preserved);
    2. leave the work item OPEN and untouched (a standing question is never
       silently closed, no flag is flipped, nothing is consumed);
    3. let the analyst's OWN next cadence tick re-attempt it, no earlier than
       ``not_before``. The cadence IS the requeue — there is no new queue table,
       because the backlog is re-read and priority-ordered every tick anyway.

  ``escalate=true`` means the ladder is exhausted: an operator is needed, not
  more waiting. A HARD failure (misconfiguration, auth, non-JSON body) carries
  NO deferral on purpose — waiting cannot fix it.

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
from ...schemas.properties import Property
from ...schemas.stack import SearchProviderConfig
from ...sources._egress import EgressBlockedError, guarded_async_client
from ...stack.search import (
    DEFAULT_LIVENESS_CACHE,
    HardSearchFailure,
    LivenessVerdict,
    SearchHandlerContext,
    SearchStatus,
    SearxngSearchHandler,
    TransientSearchFailure,
    apply_liveness,
    compute_deferral,
    resolve_tool_search_route,
    verify_engine_liveness,
)
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

# Env fallback for the search endpoint when a pack's web_search ToolSpec pins
# neither a `provider` stack_ref nor an `endpoint`. Unset by default —
# web_search then returns a clean failure naming the missing endpoint (no
# silent empty result).
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


async def _legacy_endpoint_handler(
    cfg: dict[str, Any], *, limit: int,
) -> tuple[Any, str] | ToolResult:
    """Build a ``searxng`` handler over the LEGACY operator-pinned endpoint.

    Rung 3 of the provenance ladder, retained verbatim in behaviour: the
    endpoint comes from the pack's ``web_search`` ToolSpec ``config['endpoint']``
    and falls back to ``LEGBA_WEB_SEARCH_ENDPOINT``; nothing configured returns
    the SAME clean failure text it always did. What changed is only WHERE the
    parse lives — the searxng handler, so the legacy path inherits the
    ``unresponsive_engines`` degradation read it never had.

    Returns ``(handler, provider_label)`` or a terminal ``ToolResult``.
    """
    endpoint = str(cfg.get("endpoint") or "").strip()
    label = "legacy:web_access.web_search.config.endpoint"
    if not endpoint:
        endpoint = str(os.environ.get(_SEARCH_ENDPOINT_ENV, "")).strip()
        label = f"legacy:env:{_SEARCH_ENDPOINT_ENV}"
    if not endpoint:
        return ToolResult(
            status="failed",
            error=(
                "web_search has no endpoint configured — set the web_access "
                f"pack's web_search config['endpoint'] or {_SEARCH_ENDPOINT_ENV} "
                "(or give the ToolSpec a config['provider'] stack_ref)"
            ),
        )
    if not (endpoint.startswith("http://") or endpoint.startswith("https://")):
        return ToolResult(
            status="failed",
            error=f"web_search endpoint must be http(s), got {endpoint!r}",
        )

    timeout = float(cfg.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS)
    config = SearchProviderConfig(
        subprovider=Property.Dropdown.Static.of(
            "searxng",
            ["searxng", "json", "firecrawl", "jina", "tavily", "brave", "agent"],
        ),
        endpoint=Property.Text.of(endpoint),
        timeout_seconds=Property.Number.of(timeout, minimum=1, maximum=300),
        max_results=Property.Number.of(limit, minimum=1, maximum=50),
    )
    handler = SearxngSearchHandler()
    await handler.on_configure(
        SearchHandlerContext(instance_id=label, config=config)
    )
    return handler, label


async def web_search_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    """Run one query through the resolved search provider; return top results.

    ``args``:
      * ``query`` (required) — the search query string (planner-supplied).
      * ``limit`` (optional) — max results, clamped to ``_MAX_SEARCH_RESULTS``.

    Provider selection and the four honest outcomes are documented in the module
    docstring. The provider is OPERATOR-owned at every rung; the planner
    supplies only the query.
    """
    query = str(call.args.get("query", "")).strip()
    if not query:
        return ToolResult(status="failed", error="web_search requires a 'query' arg")

    cfg = _tool_config(pack, "web_search")
    limit = max(1, min(_MAX_SEARCH_RESULTS, int(call.args.get("limit", 5))))

    # The liveness/deferral cache. Injectable off the ToolContext for tests and
    # for a caller that wants an isolated probe budget; the process-wide default
    # otherwise, so every analyst shares ONE verdict and ONE probe.
    cache = getattr(ctx, "search_liveness", None) or DEFAULT_LIVENESS_CACHE

    # ---- resolve the provider (the ladder in the module docstring) ----
    route = resolve_tool_search_route(cfg)
    bound = getattr(ctx, "search", None)
    if route is not None and bound is None:
        # A DECLARED route the runtime did not bind. Loud, never empty: the
        # planner must not read "provider missing" as "the web has nothing".
        logger.warning(
            "web_search.provider_unresolved component=%s source=%s",
            route.component_id, route.source,
        )
        advice = compute_deferral(
            "search_provider_unresolved",
            provider_key=route.component_id,
            cache=cache,
            detail=f"declared at {route.source}, not bound on this run",
        )
        return ToolResult(
            status="failed",
            error=(
                f"search_provider_unresolved: the web_search ToolSpec routes to "
                f"{route.component_id!r} ({route.source}) but no search provider "
                "is bound on this run — NO query was issued. This is not an "
                "empty result set."
            ),
            output={"deferral": advice.to_dict()},
        )
    if bound is not None:
        handler = bound
        provider_label = (
            route.component_id if route is not None
            else getattr(bound, "component_id", "") or "runtime-bound"
        )
    else:
        built = await _legacy_endpoint_handler(cfg, limit=limit)
        if isinstance(built, ToolResult):
            return built
        handler, provider_label = built

    # ---- run it ----
    extra_params = cfg.get("params") if isinstance(cfg.get("params"), dict) else None
    try:
        response = await handler.search(query, limit=limit, params=extra_params)
    except TransientSearchFailure as exc:
        # Retryable class (timeout / 429 / upstream 5xx). One fallback attempt
        # is the caller's choice, never a loop; today there is no second
        # provider bound, so this is terminal AND explicit — but DEFERRABLE:
        # the same query may well succeed on a later tick.
        logger.warning("web_search.transient provider=%s err=%s", provider_label, exc)
        advice = compute_deferral(
            "search_unavailable", provider_key=provider_label, cache=cache,
            detail=str(exc),
        )
        return ToolResult(
            status="failed", error=f"search_unavailable: {exc!s}",
            output={"deferral": advice.to_dict()},
        )
    except HardSearchFailure as exc:
        # Already carries its own classified prefix (egress_blocked / HTTP nnn /
        # "search response not JSON" / misconfiguration). NO deferral: this
        # class is never retried by contract, and waiting cannot fix a
        # misconfiguration — it needs an operator.
        logger.warning("web_search.hard_failure provider=%s err=%s", provider_label, exc)
        return ToolResult(status="failed", error=str(exc))

    # ---- empty is SUSPECT: measure the engine set before believing it ----
    # Only a zero-result response that admitted NO degradation needs this. One
    # bounded control probe through the SAME provider, cached per provider for
    # a short TTL so N empties in a run cost 1 probe.
    if response.status is SearchStatus.EMPTY:
        verdict, detail = await verify_engine_liveness(
            handler, provider_key=provider_label, cache=cache,
        )
        apply_liveness(response, verdict, detail)

    output = response.to_tool_output()
    output["provider"] = provider_label
    if route is not None:
        output["provider_route"] = route.source
        output["provider_route_class"] = route.route_class

    if response.status is SearchStatus.DEGRADED_EMPTY:
        # Every hit was lost — either to degradation the provider ADMITTED, or
        # to a control probe that found the engine set dead/unverifiable. NOT an
        # empty success (see the module docstring); the detail is carried so the
        # planner can say WHY, and the deferral tells the caller to come back
        # later rather than hammer engines that are already refusing.
        probe_failed = response.liveness in (
            LivenessVerdict.DEAD, LivenessVerdict.PROBE_FAILED,
        )
        reason = (
            "search_liveness_unverified" if probe_failed
            else "search_degraded_no_results"
        )
        logger.warning(
            "web_search.%s provider=%s liveness=%s detail=%s",
            reason, provider_label, response.liveness.value,
            response.degraded_detail,
        )
        advice = compute_deferral(
            reason, provider_key=provider_label, cache=cache,
            detail=response.degraded_detail,
        )
        output["deferral"] = advice.to_dict()
        error = (
            (
                "search_liveness_unverified: the search returned zero results "
                "and a control probe could not show the engine set answering "
                f"({response.liveness_detail or 'no detail'}) — this is "
                "UNKNOWN, not absence. Do NOT conclude that no evidence exists."
            )
            if probe_failed else
            (
                "search_degraded_no_results: the provider reported PARTIAL "
                f"service ({response.degraded_detail or 'no detail'}) and "
                "returned zero results — this is UNKNOWN, not absence. Do NOT "
                "conclude that no evidence exists."
            )
        )
        return ToolResult(
            status="failed", error=error, output=output, units=1,
        )
    if response.status is SearchStatus.EMPTY:
        # Defensive: an UNVERIFIED empty must never leave as a `completed`
        # count-0 result, which is exactly the shape a planner summarizes as
        # "no results found". Unreachable while the probe above runs; kept so
        # no future path can reintroduce the assumed-absence default.
        logger.warning(
            "web_search.empty_unverified provider=%s — liveness was not "
            "measured; refusing to report an unverified empty as a completion",
            provider_label,
        )
        advice = compute_deferral(
            "search_liveness_unverified", provider_key=provider_label,
            cache=cache, detail="liveness was never measured",
        )
        output["deferral"] = advice.to_dict()
        return ToolResult(
            status="failed",
            error=(
                "search_liveness_unverified: the search returned zero results "
                "and engine-set liveness was NOT measured — over a multi-engine "
                "meta-search that shape usually means BROKEN, not absent. This "
                "is UNKNOWN, not absence."
            ),
            output=output,
            units=1,
        )
    if response.degraded:
        logger.warning(
            "web_search.degraded provider=%s count=%d detail=%s",
            provider_label, response.count, response.degraded_detail,
        )
    # A served search — results, or a liveness-VERIFIED empty. Either way the
    # provider answered, so the deferral ladder resets.
    cache.record_success(provider_label)
    return ToolResult(status="completed", output=output, units=1)


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
