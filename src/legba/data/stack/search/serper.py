# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""serper.dev — the PAID SERP rung the external audit escalates to.

WHY A MODULE AND NOT A CONFIGURED ``json`` COMPONENT. The generic handler
(:mod:`.json_generic`) already maps serper's result shape: ``link`` is in its
URL key list, ``snippet`` in its snippet list, ``date`` in its published list,
and ``results_key`` would be ``"organic"``. It cannot reach the API at all,
though, for two reasons that are both transport and neither of which is
configurable:

  * serper is **POST-only** with the query in a JSON body; every shipped
    handler before this one issued ``GET`` with a query string;
  * it authenticates with ``X-API-KEY``, while the base's default scheme is
    ``Authorization: Bearer`` (see ``_auth_headers``).

So this module is exactly what brave.py is: the two wire facts a generic
endpoint cannot express, plus the credential fence. Everything else — the SSRF
guard, the transient/hard failure split, the liveness contract, the cost
metering — is inherited unchanged.

Wire shape (``POST https://google.serper.dev/search``, ``X-API-KEY: <key>``,
body ``{"q": …, "num": …}``)::

    {"searchParameters": {"q": …, "type": "search", "num": …},
     "organic": [{"title", "link", "snippet", "date", "position", …}, …],
     "answerBox": {...}, "knowledgeGraph": {...}, "peopleAlsoAsk": [...],
     "relatedSearches": [...], "credits": 1}

Field map (normalized ← serper):

    ==================  ============================
    ``url``             ``link``
    ``title``           ``title``            (≤512)
    ``snippet``         ``snippet``          (≤1024)
    ``published_at``    ``date``             (free text: "3 days ago")
    ``engine``          ``"serper"``
    ``score``           — (serper publishes none)
    ``rank``            ``position``, else list order
    ``extracted_text``  — (serper returns snippets, never main text)
    ==================  ============================

NO DEGRADATION CHANNEL, DELIBERATELY UNINVENTED. serper is a single first-party
proxy onto one index; it has no partial-service signal, so a well-formed empty
body IS a clean empty — and it is precisely that property the audit buys. The
liveness control probe (which runs through this same handler) is what decides
whether that empty is admissible as a scoped absence, and that is the whole
reason this rung exists: SearXNG cannot verify its own emptiness at volume, so
16-41% of every read was ungradable behind it.

⚠ METERED. ``cost_usd_per_query`` is CONFIG, never a constant here — a
provider's price is a contract term that moves without a code change. The
ladder reads it off the handler to decide whether the pack governor's
``max_cost_usd_per_day`` must be consulted before the rung may issue a query.
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

#: The one host this handler will send its API key to.
SERPER_API_HOST = "google.serper.dev"

#: Default component endpoint (the WEB corpus).
SERPER_WEB_ENDPOINT = f"https://{SERPER_API_HOST}/search"

#: The NEWS corpus, derived from the web endpoint by path swap so ONE
#: registered component reaches both — the same shape brave.py uses.
SERPER_NEWS_ENDPOINT = f"https://{SERPER_API_HOST}/news"

#: The env var an operator sets; promoted into the vault under
#: ``search.serper.api_key`` by ``scripts/bringup_vault_load.py``.
SERPER_API_KEY_ENV = "LEGBA_SERPER_API_KEY"

#: Vault id the component's ``api_key`` Secret points at.
SERPER_API_KEY_SECRET_ID = "search.serper.api_key"

#: serper's own page ceiling on ``num``. Above it the API silently serves its
#: maximum rather than erroring, so clamping here keeps the request honest
#: about what can come back.
SERPER_MAX_NUM = 100

_MODE_WEB = "web"
_MODE_NEWS = "news"

#: Where each corpus nests its hits.
_RESULTS_KEY = {_MODE_WEB: "organic", _MODE_NEWS: "news"}


def _mode_from(value: Any) -> str:
    """Normalize a caller/config corpus selector to ``web`` | ``news``."""
    text = str(value or "").strip().lower()
    return _MODE_NEWS if text in ("news", "serper_news", "news_search") else _MODE_WEB


def parse_serper_payload(
    payload: Any,
    *,
    query: str,
    limit: int = MAX_RESULTS_CAP,
    mode: str = _MODE_WEB,
) -> SearchResponse:
    """Coerce a serper web/news JSON body into the normalized response.

    Pure — so the field map AND the error-envelope handling are unit-testable
    with no network. Every non-result shape is reported as DEGRADED with an
    explicit detail, never as a clean "found nothing": for a rung whose entire
    job is to make an absence measurable, a structural surprise and a genuine
    empty must not share a wire shape.
    """
    if not isinstance(payload, Mapping):
        return SearchResponse(
            query=query, results=[], degraded=True,
            degraded_detail=(
                f"serper response was {type(payload).__name__}, not a JSON "
                "object — cannot distinguish 'no results' from a malformed reply"
            ),
        )

    # serper reports a refused/erroring call as a top-level ``message``, under
    # a 4xx that ``_get_json`` has usually already turned into a
    # HardSearchFailure. This catches the 2xx spelling of the same thing.
    message = payload.get("message")
    if message and "organic" not in payload and "news" not in payload:
        return SearchResponse(
            query=query, results=[], degraded=True,
            degraded_detail=f"serper returned an error body: {str(message)[:200]}",
            unresponsive_engines=["serper"],
        )

    mode = _mode_from(mode)
    where = _RESULTS_KEY[mode]
    raw_results = payload.get(where)
    if not isinstance(raw_results, list):
        return SearchResponse(
            query=query, results=[], degraded=True,
            degraded_detail=(
                f"serper {mode} response carried no list at {where!r} "
                "— cannot distinguish 'no results' from a malformed reply"
            ),
        )

    results: list[SearchResult] = []
    for item in raw_results:
        if len(results) >= limit:
            break
        if not isinstance(item, Mapping):
            continue
        url = str(item.get("link") or "").strip()
        if not url:
            continue
        date = item.get("date")
        results.append(SearchResult(
            url=url,
            title=clamp_title(item.get("title")),
            snippet=clamp_snippet(item.get("snippet")),
            # Free text ("3 days ago") as often as an ISO date. Carried as the
            # provider sent it rather than parsed into a false precision; the
            # window gate downstream reads the PAGE's date, not this one.
            published_at=str(date) if date not in (None, "") else None,
            engine="serper",
            # serper publishes no per-result relevance score. None is the
            # honest answer; a rank-derived float would look comparable to
            # SearXNG's real scores and is not.
            score=None,
            rank=len(results) + 1,
            # NEVER populated: serper returns snippets, never clean main text.
            # Retrieval stays on web_fetch -> archive -> Trafilatura.
            extracted_text=None,
            extract_source=None,
            # A search hit carries NO license verdict (see SearchResult).
            license_class=None,
            raw=dict(item),
        ))

    # No `degraded` here on purpose — see the module docstring: serper has no
    # partial-service channel, so a well-formed empty body IS a clean empty,
    # and the liveness probe is what licenses an absence claim over it.
    return SearchResponse(query=query, results=results)


class SerperSearchHandler(SearchProviderHandler):
    """``search.serper.*`` — the paid SERP rung, POST + ``X-API-KEY``."""

    subprovider: ClassVar[str] = "serper"
    #: Discovery only. serper returns snippets, never clean main text, so it
    #: advertises neither "fetch" nor "extract".
    capabilities: ClassVar[frozenset[str]] = frozenset({"search"})
    default_port: ClassVar[int] = 443
    #: The credential fence — see the module docstring.
    allowed_endpoint_hosts: ClassVar[frozenset[str]] = frozenset({SERPER_API_HOST})
    #: POST-only API. This is the fact json_generic could not express.
    http_method: ClassVar[str] = "POST"
    #: METERED: one page is one charge, and a second page is a second charge.
    #: The audit spends this rung on claims SearXNG could not decide, one query
    #: at a time; paging it would double the bill for the same claim.
    max_extra_pages: ClassVar[int] = 0

    def __init__(self) -> None:
        super().__init__()
        #: Corpus this component defaults to, from ``config.categories``.
        self._mode: str = _MODE_WEB

    async def on_configure(self, ctx: Any) -> None:
        """Bind config AND require a resolvable key. Refuses, never degrades.

        Identical posture to :class:`~.brave.BraveSearchHandler`, and for the
        identical reason: a metered provider that binds cleanly without a key
        answers every query with zero results, and a zero-result search is
        what an analyst reads as "no reporting on X exists".
        """
        cfg = self._extract_config(ctx)
        if cfg.api_key is None:
            raise SearchProviderUnresolved(
                f"serper component {getattr(ctx, 'instance_id', '') or '<unknown>'!r} "
                "declares no api_key — the serper.dev API is METERED and "
                "keyless queries are rejected. NO query was issued. Point the "
                f"component's config.api_key at vault id "
                f"{SERPER_API_KEY_SECRET_ID!r} (set {SERPER_API_KEY_ENV} in "
                ".env and run scripts/bringup_vault_load.py)."
            )
        try:
            await super().on_configure(ctx)
        except SearchProviderUnresolved:
            raise
        except Exception as exc:
            raise SearchProviderUnresolved(
                f"serper api_key {cfg.api_key.raw!r} could not be resolved "
                f"({type(exc).__name__}: {exc}) — NO query was issued. Load it "
                f"with {SERPER_API_KEY_ENV} in .env + "
                "scripts/bringup_vault_load.py."
            ) from exc
        if not (self._api_key or "").strip():
            raise SearchProviderUnresolved(
                f"serper api_key {cfg.api_key.raw!r} resolved EMPTY — NO query "
                "was issued. An empty key is rejected by the API, and a "
                "rejected query must never reach a caller as an empty result "
                f"set. Re-load the vault entry from {SERPER_API_KEY_ENV}."
            )
        self._mode = _mode_from(
            next(
                (c for c in (cfg.categories.raw or []) if _mode_from(c) == _MODE_NEWS),
                _MODE_WEB,
            )
        )

    # ---- the two endpoints -------------------------------------------------

    def _endpoint_for(self, endpoint: str, **opts: Any) -> str:
        """Web endpoint by default; the NEWS sibling when this call asks."""
        mode = _mode_from(opts.get("mode") or self._mode)
        if mode != _MODE_NEWS:
            return endpoint
        if endpoint.rstrip("/").endswith("/search"):
            return endpoint.rstrip("/")[: -len("/search")] + "/news"
        return SERPER_NEWS_ENDPOINT

    def _auth_headers(self) -> dict[str, str]:
        """serper's scheme: ``X-API-KEY``, never a Bearer."""
        return {
            "X-API-KEY": self._api_key or "",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ---- params + body + parse ---------------------------------------------

    def _build_params(self, query: str, *, limit: int, **opts: Any) -> dict[str, str]:
        """NOTHING on the query string — serper carries everything in the body."""
        return {}

    def _build_body(
        self, query: str, *, limit: int, **opts: Any,
    ) -> dict[str, Any]:
        cfg = self._require_configured()
        body: dict[str, Any] = {
            "q": query,
            "num": max(1, min(int(limit), SERPER_MAX_NUM)),
        }
        language = str(cfg.language.raw or "").strip()
        if language:
            body["hl"] = language
        # Operator escape hatch, last so it wins (mirrors searxng/json/brave).
        extra = opts.get("params")
        if isinstance(extra, Mapping):
            body.update({str(k): v for k, v in extra.items()})
        return body

    def _parse_payload(
        self, payload: Any, *, query: str, limit: int,
    ) -> SearchResponse:
        # The corpus is read back off the BODY, not off the request, so a
        # payload parses correctly in a test with no handler state.
        declared = self._mode
        if isinstance(payload, Mapping):
            if isinstance(payload.get("news"), list):
                declared = _MODE_NEWS
            elif isinstance(payload.get("organic"), list):
                declared = _MODE_WEB
        return parse_serper_payload(payload, query=query, limit=limit, mode=declared)

    async def search(self, query: str, *, limit: int = 5, **opts: Any) -> SearchResponse:
        """Defence in depth: refuse without a key even if configure was bypassed.

        :meth:`on_configure` is the real gate. This second check exists because
        the cost of the gate being bypassed is a paid provider silently
        answering nothing — exactly the class of failure this package exists to
        make impossible.
        """
        if not (self._api_key or "").strip():
            raise SearchProviderUnresolved(
                "serper handler has no bound api_key — NO query was issued. "
                "This is not an empty result set."
            )
        return await super().search(query, limit=limit, **opts)


__all__ = [
    "SERPER_API_HOST",
    "SERPER_API_KEY_ENV",
    "SERPER_API_KEY_SECRET_ID",
    "SERPER_MAX_NUM",
    "SERPER_NEWS_ENDPOINT",
    "SERPER_WEB_ENDPOINT",
    "SerperSearchHandler",
    "parse_serper_payload",
]
