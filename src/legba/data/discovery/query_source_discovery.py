# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``query_source_discovery`` (P-13) — one source per entry in a queried list.

The reference source-discovery kind: given a *list* of source specs (a curated
feed list, an OPML export, a substrate query result, or — at L3 — an operator's
inline bootstrap list), emit one :class:`CandidateSource` per entry. The
candidate carries the feed URL as its ``natural_key`` and the
source-instance kind (``rss`` by default) so the validate-before-register probe
can build a real handler and trial-pull it.

This is the "query" flavor of source-discovery (the task's
"crawl OR query" choice). It is the source-side analogue of
``country_list_discovery``: a deterministic, list-driven materialiser. A future
"crawl" flavor (firecrawl an index page, walk a CT-log, enumerate a Shodan org)
plugs in as a sibling kind exposing the same :class:`SourceDiscoveryKind`
Protocol — no changes here.

List sources
------------

* ``inline:<json>`` — a JSON list of ``{"url": ..., "kind": "rss", ...}``
  dicts. The escape hatch for tests + L3 bootstrap. ``kind`` defaults to the
  config-level ``default_source_kind``.
* ``substrate:source_credibility`` — query the ``source_credibility`` registry
  table (migration 0014) for known-good feed hosts, resolved via the
  actor-bound Postgres dep (the same G20 fix seam as country_list — NOT
  ``ctx.stack_resolve``). Reserved unless ``deps.postgres`` is declared.

Each emitted candidate's ``label_set`` exposes ``url`` / ``source_kind`` /
``feed_title`` so a source-template's relabel chain can rewrite them into
``identity.id`` / ``config.url`` / ``scope.tags`` for the materialised
:class:`~legba.data.schemas.source.SourceDescriptor`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, ClassVar, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._contract import DiscoveryContext, DiscoveryEvidence
from .source_contract import CandidateSource, SourceDiscoveryHealth

logger = logging.getLogger(__name__)


KIND_NAME = "query_source_discovery"
SCHEMA_VERSION = "legba/source_discovery/query/1.0.0"

_INLINE_PREFIX = "inline:"
_SUBSTRATE_CREDIBILITY = "substrate:source_credibility"


class QuerySourceDiscoveryConfig(BaseModel):
    """Config for :class:`QuerySourceDiscovery`.

    ``list_source``
        ``"inline:<json>"`` (a JSON list of source spec dicts) or
        ``"substrate:source_credibility"`` (query the source_credibility
        registry via the actor-bound Postgres dep).
    ``default_source_kind``
        The source-handler kind candidates default to when a spec omits
        ``kind`` (``rss``).
    ``filter_predicate``
        Optional Starlark/Python predicate over each spec
        (``url`` / ``source_kind`` / ``feed_title`` / ``host``). Empty = no
        filter.
    """

    model_config = ConfigDict(extra="forbid")

    list_source: str = Field(..., min_length=1, max_length=65536)
    default_source_kind: str = Field(default="rss", min_length=1, max_length=64)
    filter_predicate: str = Field(default="", max_length=4096)

    @field_validator("list_source")
    @classmethod
    def _check_list_source(cls, v: str) -> str:
        if v.startswith(_INLINE_PREFIX):
            payload = v[len(_INLINE_PREFIX):]
            try:
                parsed = json.loads(payload)
            except Exception as exc:
                raise ValueError(
                    f"query_source_discovery inline list is not valid JSON: {exc}"
                ) from exc
            if not isinstance(parsed, list):
                raise ValueError(
                    "query_source_discovery inline list must be a JSON list of "
                    "source spec dicts"
                )
            return v
        if v == _SUBSTRATE_CREDIBILITY:
            return v
        raise ValueError(
            f"unrecognised list_source {v!r}; expected 'inline:<json>' or "
            f"'{_SUBSTRATE_CREDIBILITY}'"
        )


CONFIG_SCHEMA = QuerySourceDiscoveryConfig


class _Spec(BaseModel):
    """One source spec from the list."""

    model_config = ConfigDict(extra="ignore")

    url: str = Field(..., min_length=1)
    kind: str | None = None
    feed_title: str = ""
    tags: list[str] = Field(default_factory=list)


def _eval_filter(predicate: str, spec: _Spec, host: str) -> bool:
    if not predicate or not predicate.strip():
        return True
    from .relabel import _safe_python_eval

    bindings = {
        "url": spec.url,
        "source_kind": spec.kind or "",
        "feed_title": spec.feed_title,
        "host": host,
        "tags": list(spec.tags),
    }
    return bool(_safe_python_eval(predicate, bindings, None))


async def _load_specs_from_substrate(
    resolved: Any,
) -> tuple[list[_Spec], str]:
    """Query the source_credibility registry via the actor-bound Postgres dep.

    Uses the SAME actor-resolved-dep seam as country_list_discovery — the
    descriptor declares ``deps.postgres: true`` and the actor binds a resolved
    bundle onto the handler. NOT ``ctx.stack_resolve``.
    """
    if resolved is None:
        raise RuntimeError(
            "query_source_discovery list_source='substrate:source_credibility' "
            "requires the actor to bind resolved postgres deps "
            "(declare deps.postgres: true on the discovery descriptor)"
        )

    pool = resolved.require_postgres()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT host, default_credibility FROM source_credibility "
            "ORDER BY host"
        )
    specs = [
        _Spec(
            url=f"https://{r['host']}/",
            kind="rss",
            feed_title=r["host"],
            tags=["discovered"],
        )
        for r in rows
    ]
    return specs, f"source_credibility@n={len(specs)}"


class QuerySourceDiscovery:
    """``query_source_discovery`` source-discovery kind.

    One candidate source per entry in the configured list. Satisfies the
    :class:`legba.data.discovery.source_contract.SourceDiscoveryKind` Protocol.
    """

    kind: ClassVar[str] = KIND_NAME
    family: ClassVar[Literal["source_discovery"]] = "source_discovery"
    schema_version: ClassVar[str] = SCHEMA_VERSION
    config_schema: ClassVar[type[BaseModel]] = QuerySourceDiscoveryConfig

    def __init__(self) -> None:
        self._resolved_deps: Any | None = None
        self._last_emitted = 0
        self._last_error: str | None = None

    def bind_resolved_deps(self, resolved: Any) -> None:
        """Bind the actor-resolved dep bundle (same G20 seam as country_list)."""
        self._resolved_deps = resolved

    async def discover(
        self, ctx: DiscoveryContext
    ) -> AsyncIterator[CandidateSource]:
        cfg = ctx.config
        if not isinstance(cfg, QuerySourceDiscoveryConfig):
            cfg = QuerySourceDiscoveryConfig.model_validate(
                cfg.model_dump() if isinstance(cfg, BaseModel) else cfg
            )

        try:
            specs, source_version = await self._resolve_specs(ctx, cfg)
        except Exception as exc:
            self._last_error = f"resolve_specs: {type(exc).__name__}: {exc}"
            ctx.logger.exception(
                "query_source_discovery.resolve_specs_failed list_source=%s",
                cfg.list_source,
            )
            raise

        emitted = 0
        for idx, spec in enumerate(specs):
            host = urlparse(spec.url).netloc or spec.url
            if not _eval_filter(cfg.filter_predicate, spec, host):
                continue
            source_kind = spec.kind or cfg.default_source_kind
            candidate = CandidateSource(
                natural_key=spec.url,
                source_kind=source_kind,
                label_set={
                    "url": spec.url,
                    "source_kind": source_kind,
                    "feed_title": spec.feed_title,
                    "host": host,
                    "tags": list(spec.tags),
                },
                source_metadata={
                    "list_source": cfg.list_source,
                    "list_source_version": source_version,
                    "row_index": idx,
                },
                probe_config={"url": spec.url},
                evidence=DiscoveryEvidence(
                    source_id=f"source_discovery.query.{cfg.list_source[:64]}",
                    source_version=source_version,
                    row_index=idx,
                ),
            )
            emitted += 1
            yield candidate

        self._last_emitted = emitted
        self._last_error = None
        ctx.logger.info(
            "query_source_discovery.cycle_complete list_source=%s "
            "specs_in=%d emitted=%d",
            cfg.list_source, len(specs), emitted,
        )

    async def _resolve_specs(
        self, ctx: DiscoveryContext, cfg: QuerySourceDiscoveryConfig
    ) -> tuple[list[_Spec], str]:
        if cfg.list_source.startswith(_INLINE_PREFIX):
            parsed = json.loads(cfg.list_source[len(_INLINE_PREFIX):])
            specs = [_Spec.model_validate(item) for item in parsed]
            return specs, f"inline@n={len(specs)}"
        if cfg.list_source == _SUBSTRATE_CREDIBILITY:
            return await _load_specs_from_substrate(self._resolved_deps)
        raise ValueError(
            f"unrecognised list_source at discovery time: {cfg.list_source!r}"
        )

    async def healthcheck(
        self, ctx: DiscoveryContext
    ) -> SourceDiscoveryHealth:
        state: Literal["healthy", "degraded", "unhealthy"] = (
            "unhealthy" if self._last_error else "healthy"
        )
        return SourceDiscoveryHealth(
            state=state,
            last_error=self._last_error,
            candidates_24h=self._last_emitted,
            detail={"kind": KIND_NAME},
        )


HANDLER = QuerySourceDiscovery
DISCOVERY_HANDLER = QuerySourceDiscovery


async def discover(ctx: DiscoveryContext) -> AsyncIterator[CandidateSource]:
    handler = QuerySourceDiscovery()
    async for c in handler.discover(ctx):
        yield c


async def healthcheck(ctx: DiscoveryContext) -> SourceDiscoveryHealth:
    return await QuerySourceDiscovery().healthcheck(ctx)


__all__ = [
    "CONFIG_SCHEMA",
    "DISCOVERY_HANDLER",
    "HANDLER",
    "KIND_NAME",
    "SCHEMA_VERSION",
    "QuerySourceDiscovery",
    "QuerySourceDiscoveryConfig",
    "discover",
    "healthcheck",
]
