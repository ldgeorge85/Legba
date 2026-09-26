# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Brave Search API handler — the first PAID rung of the provider ladder.

SearXNG stays rung 0: free, self-hosted, $0/query, and the thing that has been
answering. Brave is the rung that takes over when rung 0 DEGRADES — the exact
failure the ``searxng`` module documents (upstream engines CAPTCHA / rate-limit
/ ban the instance, HTTP 200 comes back with a short or empty ``results[]``).
It is a first-party index rather than a metasearch, so it does not share
SearXNG's ban surface: that independence is the entire reason it is worth
paying for, and the reason a ladder is the right shape rather than a swap.

WHY THIS IS A MODULE AND NOT A ``search.json.*`` CONFIG
-------------------------------------------------------
``GenericJsonSearchHandler`` is the deliberate "slot in any provider without
writing code" escape hatch, so the first question was whether Brave could ride
it on config alone. It cannot, for four independent reasons — each of which
would have been a SILENT wrong answer rather than a loud one:

1. **The auth header.** The base sends ``Authorization: Bearer <key>``; Brave
   authenticates with ``X-Subscription-Token``. A Bearer-only request is
   rejected, and the rejection (401/422) arrives downstream looking like a
   provider problem rather than a wiring problem. Adding a
   "which header carries the key" CONFIG field was the alternative and is
   worse: it makes the credential's destination header a per-component string,
   so one typo ships a paid subscription token under the wrong name.
   :meth:`~..base.SearchProviderHandler._auth_headers` is a code seam instead.
2. **Two endpoints behind one component.** Web results live at
   ``/res/v1/web/search`` nested under ``web.results``; news lives at
   ``/res/v1/news/search`` with the hits at top-level ``results``.
   ``SearchProviderConfig`` carries ONE ``endpoint`` and ONE ``results_key``,
   so a config-only Brave serves exactly one of the two corpora.
3. **The result-count parameter.** Brave pages with ``count``; the generic
   handler sends no count at all, so every query would take Brave's default
   page and truncate it client-side — paying full price for results thrown
   away.
4. **The degradation read.** This whole package exists because an empty result
   set is suspect. Brave has no ``unresponsive_engines``-shaped field, so the
   generic handler's degradation scan finds nothing and reports every empty as
   CLEAN. For a single first-party index that is actually the RIGHT answer
   (unlike a metasearch, Brave really can just have nothing) — but it has to be
   an argued position in a handler that also knows Brave's ``ErrorResponse``
   envelope, not a coincidence of an unrelated key list.

UNBOUND KEY IS A REFUSAL, NEVER AN EMPTY
-----------------------------------------
:meth:`BraveSearchHandler.on_configure` requires a resolvable, non-empty
``api_key`` and raises :class:`~..base.SearchProviderUnresolved` naming the
vault id and the env var when it cannot get one. The message says NO query was
issued, because the failure this guards against is the one the package was
built for: a keyless handler that quietly returns zero results, and an analyst
that reads zero results as "no reporting on X exists".

CREDENTIAL FENCE
----------------
:attr:`BraveSearchHandler.allowed_endpoint_hosts` pins the endpoint host to
``api.search.brave.com``. The SSRF egress guard cannot do this job: it refuses
NON-PUBLIC targets, and any other PUBLIC host passes it cleanly — with the paid
subscription token attached. Note the corollary, because it is easy to get
backwards: ``api.search.brave.com`` needs NO ``LEGBA_EGRESS_ALLOW_HOSTS``
entry. That allowlist exists to permit RFC-1918 sidecars (``searxng``,
``rsshub``) that the guard would otherwise refuse; a public host is already
permitted, and adding one there would be a no-op that teaches a false model.

WIRE SHAPES (both documented, both parsed)
-------------------------------------------
Web (``GET /res/v1/web/search?q=…&count=…``)::

    {"type": "search",
     "query": {"original": …},
     "web": {"type": "search",
             "results": [{"title", "url", "description", "page_age", "age",
                          "language", "meta_url": {…}, "extra_snippets": [...]}, …]},
     "news": {…}, "videos": {…}}

News (``GET /res/v1/news/search?q=…&count=…``)::

    {"type": "news",
     "results": [{"type": "news_result", "title", "url", "description",
                  "age", "page_age", "breaking", "meta_url": {…}}, …]}

Error envelope, which Brave can serve under a 2xx as well as a 4xx::

    {"type": "ErrorResponse",
     "error": {"id": …, "status": 422, "code": "VALIDATION", "detail": …}}

Field map (normalized ← Brave):

    ==================  ==========================================
    ``url``             ``url``
    ``title``           ``title``                          (≤512)
    ``snippet``         ``description``                    (≤1024)
    ``published_at``    ``page_age``, else ``age``
    ``engine``          ``"brave_web"`` / ``"brave_news"``
    ``score``           — (Brave publishes no relevance score)
    ``rank``            position in the corpus's own ordering
    ``extracted_text``  — always ``None``; see below
    ``degraded``        an ``ErrorResponse`` body, or a structural surprise
    ==================  ==========================================

``extracted_text`` stays ``None`` on every hit. ``extra_snippets`` is a list of
additional SNIPPETS — fragments the index selected — not clean main text, and
joining them would manufacture a document that was never retrieved. Retrieval
stays on the ``web_fetch`` → evidence-archive → Trafilatura path, which is why
this handler advertises ``{"search"}`` only.

COST
----
Metered. The price lives in ``config.cost_usd_per_query`` (list price ≈ $0.005
for the Data-for-Search tier as of 2026-09), never as a constant here — a
provider's price is a contract term that moves without a code change. The
ladder reads it off the handler to decide whether a governor budget check is
required before the rung may issue a query.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar, Mapping

from .base import (
    MAX_RESULTS_CAP,
    SearchProviderHandler,
    SearchProviderUnresolved,
    SearchResponse,
    SearchResult,
    clamp_snippet,
    clamp_title,
)

logger = logging.getLogger(__name__)

#: The one host this handler will send its subscription token to.
BRAVE_API_HOST = "api.search.brave.com"

#: Default component endpoint (the WEB corpus).
BRAVE_WEB_ENDPOINT = f"https://{BRAVE_API_HOST}/res/v1/web/search"

#: The NEWS corpus, derived from the web endpoint by path swap so ONE
#: registered component reaches both corpora.
BRAVE_NEWS_ENDPOINT = f"https://{BRAVE_API_HOST}/res/v1/news/search"

#: The env var an operator sets; promoted into the vault under
#: ``search.brave.api_key`` by ``scripts/bringup_vault_load.py``.
BRAVE_API_KEY_ENV = "LEGBA_BRAVE_SEARCH_API_KEY"

#: Vault id the component's ``api_key`` Secret points at.
BRAVE_API_KEY_SECRET_ID = "search.brave.api_key"

#: Brave's own ceiling on ``count`` for the web corpus. Asking for more is a
#: 422, so clamp rather than let a max_results of 50 hard-fail every query.
BRAVE_MAX_COUNT = 20

_MODE_WEB = "web"
_MODE_NEWS = "news"


def _mode_from(value: Any) -> str:
    """Normalize a caller/config corpus selector to ``web`` | ``news``."""
    text = str(value or "").strip().lower()
    return _MODE_NEWS if text in ("news", "brave_news", "news_search") else _MODE_WEB


def _published(item: Mapping[str, Any]) -> str | None:
    """``page_age`` is an ISO timestamp; ``age`` is prose ("3 days ago").

    Prefer the machine-readable one and fall through rather than dropping the
    weaker signal — a relative age is still better provenance than nothing, and
    the field is documented as free text at the schema level.
    """
    for key in ("page_age", "age"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def parse_brave_payload(
    payload: Any,
    *,
    query: str,
    limit: int = MAX_RESULTS_CAP,
    mode: str = _MODE_WEB,
) -> SearchResponse:
    """Coerce a Brave web/news JSON body into the normalized response.

    Pure — so both corpora's field maps AND the error-envelope handling are
    unit-testable with no network. Every non-result shape is reported as
    DEGRADED with an explicit detail, never as a clean "found nothing": for a
    tool whose whole contract is that absence must be measured, a structural
    surprise and a genuine empty must not share a wire shape.
    """
    if not isinstance(payload, Mapping):
        return SearchResponse(
            query=query, results=[], degraded=True,
            degraded_detail=(
                f"brave response was {type(payload).__name__}, not a JSON object "
                "— cannot distinguish 'no results' from a malformed reply"
            ),
        )

    # Brave serves this envelope under 2xx as well as 4xx, so a status-code
    # check alone does not catch it.
    if str(payload.get("type") or "") == "ErrorResponse":
        error = payload.get("error")
        detail = ""
        if isinstance(error, Mapping):
            detail = " ".join(
                str(error.get(k)) for k in ("code", "status", "detail")
                if error.get(k) not in (None, "")
            )
        return SearchResponse(
            query=query, results=[], degraded=True,
            degraded_detail=(
                "brave returned its ErrorResponse envelope"
                + (f": {detail}" if detail else "")
            ),
            unresponsive_engines=["brave"],
        )

    mode = _mode_from(mode)
    if mode == _MODE_NEWS:
        raw_results = payload.get("results")
        where = "results"
    else:
        web = payload.get("web")
        raw_results = web.get("results") if isinstance(web, Mapping) else None
        where = "web.results"

    if not isinstance(raw_results, list):
        return SearchResponse(
            query=query, results=[], degraded=True,
            degraded_detail=(
                f"brave {mode} response carried no list at {where!r} "
                "— cannot distinguish 'no results' from a malformed reply"
            ),
        )

    engine = f"brave_{mode}"
    results: list[SearchResult] = []
    for item in raw_results:
        if len(results) >= limit:
            break
        if not isinstance(item, Mapping):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        results.append(SearchResult(
            url=url,
            title=clamp_title(item.get("title")),
            snippet=clamp_snippet(item.get("description")),
            published_at=_published(item),
            engine=engine,
            # Brave publishes no per-result relevance score. Leaving this None
            # is the honest answer; a synthesized rank-derived float would look
            # comparable to SearXNG's real scores and is not.
            score=None,
            rank=len(results) + 1,
            # NEVER populated: `extra_snippets` is more snippets, not main
            # text. Retrieval stays on web_fetch → archive → Trafilatura.
            extracted_text=None,
            extract_source=None,
            # A search hit carries NO license verdict (see SearchResult).
            license_class=None,
            raw=dict(item),
        ))

    # No `degraded` here on purpose. Brave is a single first-party index with
    # no partial-service channel, so a well-formed empty body IS a clean empty
    # — and the liveness control probe (which runs through this same handler)
    # is what decides whether that empty is admissible as a scoped absence.
    return SearchResponse(query=query, results=results)


class BraveSearchHandler(SearchProviderHandler):
    """``search.brave.*`` — the paid, first-party discovery index."""

    subprovider: ClassVar[str] = "brave"
    #: Discovery only. Brave returns snippets, never clean main text (see the
    #: module docstring on ``extra_snippets``), so it advertises neither
    #: "fetch" nor "extract".
    capabilities: ClassVar[frozenset[str]] = frozenset({"search"})
    default_port: ClassVar[int] = 443
    #: The credential fence — see the module docstring.
    allowed_endpoint_hosts: ClassVar[frozenset[str]] = frozenset({BRAVE_API_HOST})

    def __init__(self) -> None:
        super().__init__()
        #: Corpus this component defaults to, from ``config.categories``.
        self._mode: str = _MODE_WEB

    async def on_configure(self, ctx: Any) -> None:
        """Bind config AND require a resolvable key. Refuses, never degrades.

        The base resolves ``api_key`` when the config declares one; this
        override makes the key MANDATORY for this subprovider and converts
        every way it can be missing into one :class:`SearchProviderUnresolved`
        that names the vault id, the env var and the registrar — because the
        alternative failure mode is a handler that binds cleanly and then
        answers every query with zero results.
        """
        cfg = self._extract_config(ctx)
        if cfg.api_key is None:
            raise SearchProviderUnresolved(
                f"brave component {getattr(ctx, 'instance_id', '') or '<unknown>'!r} "
                "declares no api_key — the Brave Search API is METERED and "
                "keyless queries are rejected. NO query was issued. Point the "
                f"component's config.api_key at vault id "
                f"{BRAVE_API_KEY_SECRET_ID!r} (set {BRAVE_API_KEY_ENV} in .env "
                "and run scripts/bringup_vault_load.py)."
            )
        try:
            await super().on_configure(ctx)
        except SearchProviderUnresolved:
            raise
        except Exception as exc:
            raise SearchProviderUnresolved(
                f"brave api_key {cfg.api_key.raw!r} could not be resolved "
                f"({type(exc).__name__}: {exc}) — NO query was issued. Load it "
                f"with {BRAVE_API_KEY_ENV} in .env + "
                "scripts/bringup_vault_load.py."
            ) from exc
        if not (self._api_key or "").strip():
            raise SearchProviderUnresolved(
                f"brave api_key {cfg.api_key.raw!r} resolved EMPTY — NO query "
                "was issued. An empty subscription token is rejected by the "
                "API, and a rejected query must never reach a caller as an "
                f"empty result set. Re-load the vault entry from "
                f"{BRAVE_API_KEY_ENV}."
            )
        self._mode = _mode_from(
            next(
                (c for c in (cfg.categories.raw or []) if _mode_from(c) == _MODE_NEWS),
                _MODE_WEB,
            )
        )

    # ---- the two endpoints -------------------------------------------------

    def _endpoint_for(self, endpoint: str, **opts: Any) -> str:
        """Web endpoint by default; the NEWS sibling when this call asks.

        Selection order: an explicit per-call ``mode`` opt, else the component's
        ``config.categories`` (``["news"]`` ⇒ the news corpus). The news URL is
        derived by path swap rather than by a second config field, so ONE
        registered component reaches both corpora and an operator who wants a
        news-pinned component just registers a second id.
        """
        mode = _mode_from(opts.get("mode") or self._mode)
        if mode != _MODE_NEWS:
            return endpoint
        if "/web/search" in endpoint:
            return endpoint.replace("/web/search", "/news/search")
        return BRAVE_NEWS_ENDPOINT

    def _auth_headers(self) -> dict[str, str]:
        """Brave's scheme: ``X-Subscription-Token``, never a Bearer."""
        return {
            "X-Subscription-Token": self._api_key or "",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }

    # ---- params + parse ----------------------------------------------------

    def _build_params(self, query: str, *, limit: int, **opts: Any) -> dict[str, str]:
        cfg = self._require_configured()
        # Clamp to Brave's own ceiling: `count` above it is a 422, and a
        # rejected query is worse than a shorter page.
        params: dict[str, str] = {
            "q": query,
            "count": str(max(1, min(int(limit), BRAVE_MAX_COUNT))),
        }
        language = str(cfg.language.raw or "").strip()
        if language:
            params["search_lang"] = language
        # Operator escape hatch, last so it wins (mirrors searxng/json).
        extra = opts.get("params")
        if isinstance(extra, Mapping):
            params.update({str(k): str(v) for k, v in extra.items()})
        return params

    def _parse_payload(
        self, payload: Any, *, query: str, limit: int,
    ) -> SearchResponse:
        # The corpus is read back off the BODY, not off the request, so a
        # payload can be parsed correctly in a test with no handler state:
        # Brave stamps `type` ("search" for web, "news" for news) and the two
        # bodies nest their hits differently. Fall back to the configured mode
        # when the marker is absent.
        declared = str(payload.get("type") or "") if isinstance(payload, Mapping) else ""
        mode = _MODE_NEWS if declared == "news" else (
            _MODE_WEB if declared == "search" else self._mode
        )
        return parse_brave_payload(payload, query=query, limit=limit, mode=mode)

    async def search(self, query: str, *, limit: int = 5, **opts: Any) -> SearchResponse:
        """Defence in depth: refuse without a key even if configure was bypassed.

        :meth:`on_configure` is the real gate. This second check exists because
        the cost of the gate being bypassed (a handler constructed directly, a
        future lifecycle hook that clears state) is a paid provider silently
        answering nothing, and that is exactly the class of failure this whole
        package is built to make impossible.
        """
        if not (self._api_key or "").strip():
            raise SearchProviderUnresolved(
                "brave handler has no bound api_key — NO query was issued. "
                "This is not an empty result set."
            )
        return await super().search(query, limit=limit, **opts)


__all__ = [
    "BRAVE_API_HOST",
    "BRAVE_API_KEY_ENV",
    "BRAVE_API_KEY_SECRET_ID",
    "BRAVE_MAX_COUNT",
    "BRAVE_NEWS_ENDPOINT",
    "BRAVE_WEB_ENDPOINT",
    "BraveSearchHandler",
    "parse_brave_payload",
]
