# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generic JSON search handler — any endpoint with a compatible ``results[]``.

The escape hatch that makes "slot in any provider" true today rather than after
someone writes a bespoke module: point it at any HTTP endpoint that answers a
query with a JSON object containing a list of hits, and it normalizes them.

Tolerated shapes, in resolution order (all configurable, none guessed twice):

* the results list lives at ``config.results_key`` (default ``"results"``);
  a dotted path is honoured (``"data.web"`` for a Firecrawl-shaped body);
* per hit, ``url`` ← ``url`` | ``link`` | ``href``;
* ``title`` ← ``title`` | ``name`` | ``heading``;
* ``snippet`` ← ``content`` | ``snippet`` | ``description`` | ``abstract`` |
  ``summary``;
* ``extracted_text`` ← ``markdown`` | ``raw_content`` | ``text`` — ONLY when the
  provider actually sent it, and then ``extract_source`` is stamped
  ``"provider"``. Never synthesized from the snippet.

The query parameter name is ``config.query_param`` (default ``"q"``), so a
``?query=`` or ``?search=`` API needs config, not code.

DEGRADATION HONESTY FOR AN UNKNOWN PROVIDER
--------------------------------------------
A generic endpoint has no standard way to say "I served you a partial result".
This handler therefore does two things and refuses to guess a third:

1. It READS a degradation signal when one is present under any of the known
   spellings (``unresponsive_engines``, ``warning``, ``warnings``, ``errors``,
   ``partial``) and maps it onto ``degraded`` / ``degraded_detail``.
2. When the body is not a JSON object, or carries no list at the configured
   results key, that is a STRUCTURAL surprise — reported as degraded with an
   explicit detail, never as a clean "found nothing".

What it does NOT do is invent confidence: a well-formed body with an empty
``results[]`` and no degradation signal is reported as
:attr:`~..base.SearchStatus.EMPTY`, i.e. TRUE absence. If a provider can serve
partial results without saying so, that provider needs its own handler that
knows how to detect it — that is precisely what :mod:`.searxng` is.
"""

from __future__ import annotations

from typing import Any, ClassVar, Mapping

from .base import (
    MAX_RESULTS_CAP,
    SearchProviderHandler,
    SearchResponse,
    SearchResult,
    clamp_snippet,
    clamp_title,
    coerce_engine_names,
)

_URL_KEYS = ("url", "link", "href")
_TITLE_KEYS = ("title", "name", "heading")
_SNIPPET_KEYS = ("content", "snippet", "description", "abstract", "summary")
_TEXT_KEYS = ("markdown", "raw_content", "extracted_text", "text")
_PUBLISHED_KEYS = ("publishedDate", "published_date", "published_at", "date")
#: Keys under which a provider may admit partial service.
_DEGRADED_KEYS = (
    "unresponsive_engines", "warning", "warnings", "errors", "partial",
)


def _first(item: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def _dig(payload: Mapping[str, Any], path: str) -> Any:
    """Resolve a dotted path (``"data.web"``) inside a nested JSON object."""
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def parse_generic_payload(
    payload: Any,
    *,
    query: str,
    limit: int = MAX_RESULTS_CAP,
    results_key: str = "results",
) -> SearchResponse:
    """Coerce a generic JSON search body into the normalized response.

    Pure, so both the mapping and the degradation contract are unit-testable
    without a network.
    """
    if not isinstance(payload, Mapping):
        return SearchResponse(
            query=query, results=[], degraded=True,
            degraded_detail=(
                f"search response was {type(payload).__name__}, not a JSON object "
                "— cannot distinguish 'no results' from a malformed reply"
            ),
        )

    degraded_names: list[str] = []
    for key in _DEGRADED_KEYS:
        if key in payload and payload[key]:
            names = coerce_engine_names(payload[key])
            degraded_names.extend(n for n in names if n not in degraded_names)

    raw_results = _dig(payload, results_key)
    if not isinstance(raw_results, list):
        return SearchResponse(
            query=query, results=[], degraded=True,
            degraded_detail=(
                f"search response carried no list at {results_key!r} "
                "— cannot distinguish 'no results' from a malformed reply"
            ),
            unresponsive_engines=degraded_names,
        )

    results: list[SearchResult] = []
    for item in raw_results:
        if len(results) >= limit:
            break
        if not isinstance(item, Mapping):
            continue
        url = str(_first(item, _URL_KEYS) or "").strip()
        if not url:
            continue
        score = item.get("score")
        try:
            score_value = float(score) if score is not None else None
        except (TypeError, ValueError):
            score_value = None
        text = _first(item, _TEXT_KEYS)
        published = _first(item, _PUBLISHED_KEYS)
        engine = item.get("engine") or item.get("provider") or item.get("source")
        results.append(SearchResult(
            url=url,
            title=clamp_title(_first(item, _TITLE_KEYS)),
            snippet=clamp_snippet(_first(item, _SNIPPET_KEYS)),
            published_at=str(published) if published else None,
            engine=str(engine) if engine else None,
            score=score_value,
            rank=len(results) + 1,
            # Stamped ONLY when the provider genuinely sent clean text.
            extracted_text=str(text) if text else None,
            extract_source="provider" if text else None,
            license_class=None,
            raw=dict(item),
        ))

    degraded = bool(degraded_names)
    detail = "provider reported: " + ", ".join(degraded_names) if degraded else ""
    return SearchResponse(
        query=query,
        results=results,
        degraded=degraded,
        degraded_detail=detail,
        unresponsive_engines=degraded_names,
    )


class GenericJsonSearchHandler(SearchProviderHandler):
    """``search.json.*`` — any JSON search API with a compatible ``results[]``."""

    subprovider: ClassVar[str] = "json"
    #: "extract" is advertised because the handler PROPAGATES provider-supplied
    #: clean text when it is present; it never manufactures it. A body without
    #: such a field simply yields ``extracted_text=None`` and the caller falls
    #: through to the existing retrieval path.
    capabilities: ClassVar[frozenset[str]] = frozenset({"search", "extract"})
    default_port: ClassVar[int] = 443

    def _build_params(self, query: str, *, limit: int, **opts: Any) -> dict[str, str]:
        cfg = self._require_configured()
        param_name = str(cfg.query_param.raw or "q").strip() or "q"
        params: dict[str, str] = {param_name: query}
        language = str(cfg.language.raw or "").strip()
        if language:
            params["language"] = language
        extra = opts.get("params")
        if isinstance(extra, Mapping):
            params.update({str(k): str(v) for k, v in extra.items()})
        return params

    def _parse_payload(
        self, payload: Any, *, query: str, limit: int,
    ) -> SearchResponse:
        cfg = self._require_configured()
        return parse_generic_payload(
            payload, query=query, limit=limit,
            results_key=str(cfg.results_key.raw or "results"),
        )


__all__ = ["GenericJsonSearchHandler", "parse_generic_payload"]
