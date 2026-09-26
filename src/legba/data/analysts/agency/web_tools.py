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

Both egress EXCLUSIVELY through :func:`fetch_client` (the SSRF-guarded
client, or the flag-gated impersonating one) — the SAME
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
from dataclasses import dataclass, field, replace as _dc_replace
from typing import Any

import httpx

from ...schemas.action_pack import ActionPack
from ...schemas.properties import Property
from ...schemas.stack import SearchProviderConfig
from ...sources._egress import (
    EgressBlockedError,
    fetch_client,
    guarded_async_client,
)
from ...stack.search import (
    DEFAULT_LIVENESS_CACHE,
    HardSearchFailure,
    LivenessVerdict,
    SearchHandlerContext,
    SearchProviderUnresolved,
    SearchStatus,
    SearxngSearchHandler,
    TransientSearchFailure,
    apply_liveness,
    compute_deferral,
    resolve_tool_search_route,
    verify_engine_liveness,
)
from .search_cost import check_paid_rung_budget
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
#: Matches ``stack.search.base.MAX_RESULTS_CAP``. See its docstring for why
#: 10 was measured to be throwing away two thirds of every search.
_MAX_SEARCH_RESULTS = 30
_DEFAULT_TIMEOUT_SECONDS = 15.0
_USER_AGENT = "legba-web-tools/1.0 (+https://github.com/ldgeorge85/legba)"

#: Extra attempts a TIMED-OUT fetch gets. ONE, and on timeout only.
#:
#: The measured failure: a 07:23Z reference build spent fifteen fetch records
#: for one usable page, and four of them went to ``dfat.gov.au`` and
#: ``defence.gov.au`` URLs that were probed afterwards and found to be REAL
#: pages — one dated inside the build's own window. They had answered
#: ``httpx.ReadTimeout`` with an EMPTY message, which surfaced as
#: ``web_fetch.http_error … err=`` and reached the model as a blocked host. A
#: France build read 0 pages the same way. An intermittent hang is the one
#: failure class where the same request a second later is genuinely likely to
#: work, and it is also the one this tool could not tell apart from a refusal.
#:
#: NOT retried: 4xx/5xx (``web_fetch`` returns those as a completed fetch of
#: whatever the host served — the caller reads ``status_code``), a
#: challenge/paywall stub (a served page; see ``_challenge_detect``), and an
#: SSRF refusal. None of those change on a second identical request; retrying
#: them would spend the caller's fetch budget to be told the same thing twice.
_FETCH_TIMEOUT_RETRIES = 1

#: Outcome vocabulary stamped on every ``web_fetch`` result, so a caller can
#: TALLY fetches by what actually happened rather than inferring it from an
#: error string. ``ok`` is "the host served something" — 200 or 404 alike;
#: whether the body is an article, a stub or a challenge page is a question
#: about the BODY and belongs to the caller that parses it.
FETCH_OUTCOME_OK = "ok"
FETCH_OUTCOME_TIMED_OUT = "timed_out"
FETCH_OUTCOME_BLOCKED = "blocked"
FETCH_OUTCOME_ERROR = "error"

#: The UA ``web_fetch`` actually sends, published for callers that gate this
#: path on robots.txt. A robots decision is evaluated PER USER-AGENT, so a
#: caller that asks ``robots.py`` about ``legba-research/1.0`` (its default)
#: while this tool sends ``legba-web-tools/1.0`` is obeying rules written for a
#: different agent than the one that shows up in the publisher's log. Exported
#: so the two can never drift apart silently.
WEB_FETCH_USER_AGENT = _USER_AGENT

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
    attempts = 0
    response: httpx.Response | None = None
    last_timeout: httpx.TimeoutException | None = None
    while attempts <= _FETCH_TIMEOUT_RETRIES:
        attempts += 1
        try:
            async with fetch_client(
                # ``guarded=`` hands ``fetch_client`` THIS module's own
                # ``guarded_async_client``, so the flag-off path is the exact
                # call this site made before, through the exact name the
                # existing e2e suites monkeypatch. See _egress.fetch_client.
                guarded=guarded_async_client,
                follow_redirects=True,
                timeout=timeout,
                headers={"User-Agent": _USER_AGENT},
            ) as client:
                response = await client.get(url)
            break
        except EgressBlockedError as exc:
            # The SSRF guard refused a non-public target (or a redirect to
            # one). Clean tool failure — the planner sees it and stops, the run
            # survives. NEVER retried: the guard's verdict is deterministic.
            logger.warning("web_fetch.egress_blocked url=%s err=%s", url, exc)
            return ToolResult(
                status="failed", error=f"egress_blocked: {exc!s}",
                output={"url": url, "fetch_outcome": FETCH_OUTCOME_BLOCKED,
                        "attempts": attempts},
            )
        except httpx.TimeoutException as exc:
            # MUST precede the HTTPError arm — every timeout class is an
            # HTTPError subclass, and catching the parent first is exactly how
            # this path used to swallow a retryable hang as a dead host.
            last_timeout = exc
            logger.warning(
                "web_fetch.timeout url=%s attempt=%d/%d cls=%s timeout=%.1fs",
                url, attempts, _FETCH_TIMEOUT_RETRIES + 1,
                type(exc).__name__, timeout,
            )
            continue
        except httpx.HTTPError as exc:
            logger.warning("web_fetch.http_error url=%s err=%s", url, exc)
            return ToolResult(
                status="failed", error=f"fetch_failed: {exc!s}",
                output={"url": url, "fetch_outcome": FETCH_OUTCOME_ERROR,
                        "attempts": attempts},
            )

    if response is None:
        # Every attempt timed out. Reported under its OWN name and with the
        # class named, because ``httpx.ReadTimeout`` carries an EMPTY message
        # and the old ``fetch_failed: `` read as "this host refused us".
        detail = str(last_timeout or "") or "no message"
        logger.warning(
            "web_fetch.timed_out url=%s attempts=%d cls=%s",
            url, attempts, type(last_timeout).__name__ if last_timeout else "?",
        )
        return ToolResult(
            status="failed",
            error=(
                f"fetch_timed_out: {type(last_timeout).__name__ if last_timeout else 'timeout'}"
                f" after {attempts} attempt(s) at {timeout:.0f}s ({detail}). "
                "The host did not answer in time — this is NOT a refusal and "
                "NOT evidence the page is missing."
            ),
            output={"url": url, "fetch_outcome": FETCH_OUTCOME_TIMED_OUT,
                    "attempts": attempts},
        )

    body = _decode_body(response)
    return ToolResult(
        status="completed",
        output={
            "url": str(response.url),
            "status_code": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            "body": body,
            "truncated": len(response.text) > _MAX_FETCH_BYTES,
            "fetch_outcome": FETCH_OUTCOME_OK,
            "attempts": attempts,
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
            ["searxng", "json", "firecrawl", "jina", "tavily", "brave",
             "serper", "agent"],
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


# ---------------------------------------------------------------------------
# THE PROVIDER LADDER (R-C)
# ---------------------------------------------------------------------------


@dataclass
class _Rung:
    """One provider the ladder may try, in order.

    ``handler`` is ``None`` for a rung that was DECLARED on the ToolSpec but
    could not be built this run. That is deliberately not the same as "absent":
    a declared-but-unbuildable rung is reported by name, so an operator sees
    WHICH provider was missing instead of a ladder that silently got shorter.
    """

    handler: Any | None
    label: str
    route: Any | None = None

    @property
    def cost_usd_per_query(self) -> float:
        try:
            return max(0.0, float(getattr(self.handler, "cost_usd_per_query", 0.0)))
        except (TypeError, ValueError):
            return 0.0

    @property
    def metered(self) -> bool:
        return self.cost_usd_per_query > 0.0


@dataclass
class _RungOutcome:
    """What one rung did. ``answered`` is the only thing the loop branches on."""

    result: ToolResult | None
    answered: bool
    reason: str
    cost_usd: float = 0.0
    #: Real upstream queries this rung issued (the search itself plus any
    #: liveness control probe). The probe is a REAL query on a metered
    #: provider — pretending otherwise would under-report the bill by up to
    #: 100%.
    queries: int = 0
    detail: str = ""


@dataclass
class _LadderLog:
    """Per-rung record, attached to the output ONLY when a ladder exists."""

    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(self, rung: "_Rung", reason: str, *, detail: str = "",
               queries: int = 0, cost_usd: float = 0.0) -> None:
        self.entries.append({
            "provider": rung.label,
            "route": getattr(rung.route, "source", "") or "",
            "outcome": reason,
            "queries": queries,
            "cost_usd": round(cost_usd, 6),
            **({"detail": detail} if detail else {}),
        })


def _rung_matches(pin: str, rung: _Rung) -> bool:
    """Does ``pin`` name THIS rung? Component id, or its subprovider segment.

    ``search.serper.paid`` is matched by its full id, by ``serper`` (the dotted
    segment), and by the handler's own ``subprovider``. Nothing else: a pin is
    a SELECTION among rungs the operator already declared, never a way to name
    a component that is not on the ladder.
    """
    label = str(rung.label or "")
    if not pin:
        return False
    if pin == label:
        return True
    if pin in label.split("."):
        return True
    return str(getattr(rung.handler, "subprovider", "") or "") == pin


def _build_ladder(ctx: ToolContext, first: _Rung) -> list[_Rung]:
    """Rung 0 plus the runtime-bound ``config.fallback_providers`` rungs.

    ``ctx.search_fallbacks`` is a list of ``(handler, route)`` pairs the runtime
    resolved from the ToolSpec. Absent or empty ⇒ a one-element ladder, which is
    the shipped state and the reason the searxng path is byte-identical.
    """
    rungs = [first]
    seen = {first.label}
    for entry in (getattr(ctx, "search_fallbacks", None) or []):
        handler, route = (
            entry if isinstance(entry, (tuple, list)) and len(entry) == 2
            else (entry, None)
        )
        label = (
            getattr(route, "component_id", "")
            or getattr(handler, "component_id", "")
            or ""
        )
        if not label or label in seen:
            # A rung is attempted AT MOST ONCE per run — re-entering a provider
            # that just failed is exactly the hammering the deferral ladder
            # exists to prevent.
            continue
        seen.add(label)
        rungs.append(_Rung(handler=handler, label=label, route=route))
    return rungs


async def _run_rung(
    rung: _Rung,
    *,
    query: str,
    limit: int,
    extra_params: Any,
    cache: Any,
) -> _RungOutcome:
    """Run ONE provider and classify the outcome.

    Every ToolResult this returns — success, degraded-empty, unverified-empty,
    transient, hard — is the SAME shape the single-provider path has always
    returned, error strings included. The ladder adds a choice about what to do
    next; it does not restate the honesty contract.
    """
    handler = rung.handler
    probes_before = int(getattr(cache, "probes", 0) or 0)
    try:
        queries = 1
        response = await handler.search(query, limit=limit, params=extra_params)
    except SearchProviderUnresolved as exc:
        # Refused BEFORE egress (no key bound, endpoint host fenced). Nothing
        # was spent and nothing was asked.
        logger.warning(
            "web_search.provider_unresolved provider=%s err=%s", rung.label, exc,
        )
        return _RungOutcome(
            result=ToolResult(status="failed", error=str(exc)),
            answered=False, reason="search_provider_unresolved",
            queries=0, detail=str(exc),
        )
    except TransientSearchFailure as exc:
        # Retryable class (timeout / 429 / upstream 5xx). One fallback attempt
        # is the caller's choice, never a loop — with a ladder configured that
        # choice is the NEXT rung, and it is taken once.
        logger.warning("web_search.transient provider=%s err=%s", rung.label, exc)
        advice = compute_deferral(
            "search_unavailable", provider_key=rung.label, cache=cache,
            detail=str(exc),
        )
        return _RungOutcome(
            result=ToolResult(
                status="failed", error=f"search_unavailable: {exc!s}",
                output={"deferral": advice.to_dict()},
            ),
            answered=False, reason="search_unavailable",
            cost_usd=rung.cost_usd_per_query * queries, queries=queries,
            detail=str(exc),
        )
    except HardSearchFailure as exc:
        # Already carries its own classified prefix (egress_blocked / HTTP nnn /
        # "search response not JSON" / misconfiguration). NO deferral: this
        # class is never retried by contract, and waiting cannot fix a
        # misconfiguration — it needs an operator.
        logger.warning("web_search.hard_failure provider=%s err=%s", rung.label, exc)
        return _RungOutcome(
            result=ToolResult(status="failed", error=str(exc)),
            answered=False, reason="search_hard_failure",
            cost_usd=rung.cost_usd_per_query * queries, queries=queries,
            detail=str(exc),
        )

    # ---- empty is SUSPECT: measure the engine set before believing it ----
    # Only a zero-result response that admitted NO degradation needs this. One
    # bounded control probe through the SAME provider, cached per provider for
    # a short TTL so N empties in a run cost 1 probe.
    if response.status is SearchStatus.EMPTY:
        verdict, detail = await verify_engine_liveness(
            handler, provider_key=rung.label, cache=cache,
        )
        apply_liveness(response, verdict, detail)
    queries += max(0, int(getattr(cache, "probes", 0) or 0) - probes_before)
    spent = rung.cost_usd_per_query * queries

    output = response.to_tool_output()
    output["provider"] = rung.label
    if rung.route is not None:
        output["provider_route"] = rung.route.source
        output["provider_route_class"] = rung.route.route_class

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
            reason, rung.label, response.liveness.value,
            response.degraded_detail,
        )
        advice = compute_deferral(
            reason, provider_key=rung.label, cache=cache,
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
        return _RungOutcome(
            result=ToolResult(
                status="failed", error=error, output=output, units=1,
            ),
            answered=False, reason=reason, cost_usd=spent, queries=queries,
            detail=response.degraded_detail,
        )
    if response.status is SearchStatus.EMPTY:
        # Defensive: an UNVERIFIED empty must never leave as a `completed`
        # count-0 result, which is exactly the shape a planner summarizes as
        # "no results found". Unreachable while the probe above runs; kept so
        # no future path can reintroduce the assumed-absence default.
        logger.warning(
            "web_search.empty_unverified provider=%s — liveness was not "
            "measured; refusing to report an unverified empty as a completion",
            rung.label,
        )
        advice = compute_deferral(
            "search_liveness_unverified", provider_key=rung.label,
            cache=cache, detail="liveness was never measured",
        )
        output["deferral"] = advice.to_dict()
        return _RungOutcome(
            result=ToolResult(
                status="failed",
                error=(
                    "search_liveness_unverified: the search returned zero results "
                    "and engine-set liveness was NOT measured — over a multi-engine "
                    "meta-search that shape usually means BROKEN, not absent. This "
                    "is UNKNOWN, not absence."
                ),
                output=output,
                units=1,
            ),
            answered=False, reason="search_liveness_unverified",
            cost_usd=spent, queries=queries,
        )
    if response.degraded:
        logger.warning(
            "web_search.degraded provider=%s count=%d detail=%s",
            rung.label, response.count, response.degraded_detail,
        )
    # A served search — results, or a liveness-VERIFIED empty. Either way the
    # provider answered, so the deferral ladder resets.
    cache.record_success(rung.label)
    return _RungOutcome(
        result=ToolResult(status="completed", output=output, units=1),
        answered=True, reason="ok", cost_usd=spent, queries=queries,
    )


async def web_search_tool(
    call: ToolCall, pack: ActionPack, ctx: ToolContext
) -> ToolResult:
    """Run one query through the resolved provider LADDER; return top results.

    ``args``:
      * ``query`` (required) — the search query string (planner-supplied).
      * ``limit`` (optional) — max results, clamped to ``_MAX_SEARCH_RESULTS``.
      * ``provider`` (optional) — PIN this call to ONE rung of the ladder, by
        component id (``search.serper.paid``) or by its subprovider segment
        (``serper``).

    Provider selection and the honest outcomes are documented in the module
    docstring. The provider is OPERATOR-owned at every rung; the planner
    supplies only the query.

    ``provider`` DOES NOT WEAKEN THAT. It selects among the rungs the operator
    already declared on the ToolSpec and can reach nothing else: a pin naming
    no declared rung is a clean, loud failure, never a silent fall-back to
    rung 0 and never a way to name an arbitrary component. It exists for the
    one caller that must escalate on a rung 0 that ANSWERED — the external
    audit's absence-claim path, where SearXNG's empty is exactly the thing
    that cannot be believed, and where the generic fallback ladder (which
    only moves when a rung FAILS) structurally cannot help.
    """
    query = str(call.args.get("query", "")).strip()
    if not query:
        return ToolResult(status="failed", error="web_search requires a 'query' arg")

    cfg = _tool_config(pack, "web_search")
    limit = max(1, min(_MAX_SEARCH_RESULTS, int(call.args.get("limit", 5))))
    pin = str(call.args.get("provider", "") or "").strip()

    # The liveness/deferral cache. Injectable off the ToolContext for tests and
    # for a caller that wants an isolated probe budget; the process-wide default
    # otherwise, so every analyst shares ONE verdict and ONE probe.
    cache = getattr(ctx, "search_liveness", None) or DEFAULT_LIVENESS_CACHE

    # ---- resolve rung 0 (the ladder in the module docstring) ----
    route = resolve_tool_search_route(cfg)
    bound = getattr(ctx, "search", None)
    fallbacks = list(getattr(ctx, "search_fallbacks", None) or [])
    if route is not None and bound is None and not fallbacks:
        # A DECLARED route the runtime did not bind, with nothing below it.
        # Loud, never empty: the planner must not read "provider missing" as
        # "the web has nothing".
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
    if bound is not None or (route is not None and fallbacks):
        provider_label = (
            route.component_id if route is not None
            else getattr(bound, "component_id", "") or "runtime-bound"
        )
        rung0 = _Rung(handler=bound, label=provider_label, route=route)
    else:
        built = await _legacy_endpoint_handler(cfg, limit=limit)
        if isinstance(built, ToolResult):
            return built
        legacy_handler, provider_label = built
        rung0 = _Rung(handler=legacy_handler, label=provider_label, route=None)

    rungs = _build_ladder(ctx, rung0)
    if pin:
        pinned = [r for r in rungs if _rung_matches(pin, r)]
        if not pinned:
            # LOUD, never a silent fall-back. A caller that asked for the paid
            # rung and got rung 0's answer back would believe it had bought a
            # verifiable empty when it had not — which is the precise error the
            # rung was bought to prevent.
            declared = [r.label for r in rungs]
            logger.warning(
                "web_search.pin_not_declared pin=%s declared=%s", pin, declared,
            )
            return ToolResult(
                status="failed",
                error=(
                    f"search_provider_not_declared: web_search was pinned to "
                    f"{pin!r}, which is not a declared rung of this pack's "
                    f"ladder (declared: {declared}) — NO query was issued. "
                    "This is not an empty result set. Add the component to the "
                    "web_search ToolSpec's fallback_providers."
                ),
                output={"pinned_provider": pin, "declared_rungs": declared},
            )
        rungs = pinned
    # A LADDER exists only when an operator declared one. With a single rung the
    # loop below runs exactly the single-provider path, and the two ladder-only
    # output keys are never emitted — that is the byte-identity contract.
    laddered = len(rungs) > 1
    log = _LadderLog()
    budget_account = (
        str(getattr(ctx, "search_budget_account", "") or "")
        or str(getattr(call, "budget_account", "") or "system")
    )

    extra_params = cfg.get("params") if isinstance(cfg.get("params"), dict) else None
    total_cost = 0.0
    last: _RungOutcome | None = None
    for rung in rungs:
        if rung.handler is None:
            detail = (
                f"declared at {getattr(rung.route, 'source', '?')}, not bound "
                "on this run"
            )
            logger.warning(
                "web_search.rung_unresolved component=%s source=%s",
                rung.label, getattr(rung.route, "source", ""),
            )
            log.record(rung, "unresolved", detail=detail)
            # The contract (module docstring, "DEFERRAL, NOT RETRY"): an
            # unresolved DECLARED provider carries a deferral block, on the
            # single-rung path and on the ladder alike — a rung below rung 0
            # (2026-09-20: the serper absence rung) must not strip it.
            advice = compute_deferral(
                "search_provider_unresolved", provider_key=rung.label,
                cache=cache, detail=detail,
            )
            last = _RungOutcome(
                result=ToolResult(
                    status="failed",
                    error=(
                        f"search_provider_unresolved: the web_search ToolSpec "
                        f"routes to {rung.label!r} "
                        f"({getattr(rung.route, 'source', '?')}) but no search "
                        "provider is bound on this run — NO query was issued. "
                        "This is not an empty result set."
                    ),
                    output={"deferral": advice.to_dict()},
                ),
                answered=False, reason="search_provider_unresolved", detail=detail,
            )
            continue
        # DEFERRAL HOLDS ACROSS RUNGS. A provider inside its backoff window is
        # SKIPPED, not re-queried — hammering engines that are already refusing
        # worsens the ban, and that rule does not stop applying just because
        # there is somewhere else to go. Applied only when a ladder exists: with
        # one rung there is nowhere to fall to, so skipping would turn "try and
        # fail honestly" into "do not try", which is strictly worse.
        if laddered:
            until = cache.deferred_until(rung.label)
            if until is not None:
                detail = f"deferred until {until.isoformat()}"
                logger.info(
                    "web_search.rung_deferred provider=%s until=%s",
                    rung.label, until.isoformat(),
                )
                log.record(rung, "deferred", detail=detail)
                continue
        # A METERED rung is priced BEFORE it is allowed to spend. Free rungs
        # (every self-hosted one) never touch the ledger.
        if rung.metered:
            decision = await check_paid_rung_budget(
                pack=pack,
                ledger=getattr(ctx, "search_cost_ledger", None),
                budget_account=budget_account,
                cost_usd=rung.cost_usd_per_query,
                component_id=rung.label,
            )
            if not decision.admitted:
                logger.warning(
                    "web_search.paid_rung_refused provider=%s cause=%s detail=%s",
                    rung.label, decision.cause, decision.detail,
                )
                log.record(rung, f"cost_{decision.cause}", detail=decision.detail)
                last = _RungOutcome(
                    result=ToolResult(
                        status="failed",
                        error=(
                            f"search_cost_{decision.cause}: the paid rung "
                            f"{rung.label!r} was NOT queried — {decision.detail} "
                            "This is a budget refusal, NOT an empty result set: "
                            "we did not look, so nothing was found or ruled out."
                        ),
                    ),
                    answered=False, reason=f"search_cost_{decision.cause}",
                    detail=decision.detail,
                )
                continue

        outcome = await _run_rung(
            rung, query=query, limit=limit, extra_params=extra_params, cache=cache,
        )
        total_cost += outcome.cost_usd
        log.record(
            rung, outcome.reason, detail=outcome.detail,
            queries=outcome.queries, cost_usd=outcome.cost_usd,
        )
        last = outcome
        if outcome.answered and outcome.result is not None:
            result = outcome.result
            if laddered:
                result.output["provider_used"] = rung.label
                result.output["ladder"] = log.entries
            # ToolResult is frozen; a free ladder produces the IDENTICAL object
            # the single-provider path always returned (no rebuild at all).
            return (
                _dc_replace(result, cost_usd=round(total_cost, 6))
                if total_cost else result
            )

    # Nothing answered. Report the LAST rung's failure verbatim — its error
    # string is the one the pack rules and the planner already know how to
    # read — and attach the ladder so an operator can see every rung that was
    # tried, skipped or refused, and why.
    if last is None or last.result is None:  # pragma: no cover — rungs is non-empty
        return ToolResult(
            status="failed",
            error=(
                "search_provider_unresolved: no search rung was runnable on this "
                "run — NO query was issued. This is not an empty result set."
            ),
        )
    result = last.result
    if laddered:
        result.output["provider_used"] = ""
        result.output["ladder"] = log.entries
    return (
        _dc_replace(result, cost_usd=round(total_cost, 6))
        if total_cost else result
    )


def register_web_access_tools(registry: "Any") -> None:
    """Register the two external handlers (called by ``default_tool_registry``)."""
    registry.register("web_fetch", web_fetch_tool)
    registry.register("web_search", web_search_tool)


__all__ = [
    "WEB_ACCESS_PACK_ID",
    "WEB_ACCESS_TOOLS",
    "register_web_access_tools",
    "FETCH_OUTCOME_BLOCKED",
    "FETCH_OUTCOME_ERROR",
    "FETCH_OUTCOME_OK",
    "FETCH_OUTCOME_TIMED_OUT",
    "WEB_FETCH_USER_AGENT",
    "web_fetch_tool",
    "web_search_tool",
]
