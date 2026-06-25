# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Polymorphic discovery materialiser entry (P-13) — target AND source flavors.

This is the single seam the discovery actor / host calls per cycle. It owns the
*actor-resolved dependency* path (the G20 fix) and dispatches between the two
discovery flavors:

  * **target flavor** — a discovery descriptor whose kind is a
    :class:`~legba.data.discovery._contract.DiscoveryKind`
    (``country_list_discovery`` / ``file_sd_discovery``). It emits
    :class:`CandidateTarget` and reconciles into ``target_descriptors`` via
    :func:`legba.data.registry.discovered_materializer.reconcile_discovered_targets`.

  * **source flavor** — a discovery descriptor whose kind is a
    :class:`~legba.data.discovery.source_contract.SourceDiscoveryKind`
    (``query_source_discovery``). It emits
    :class:`~legba.data.discovery.source_contract.CandidateSource`, runs
    validate-before-register, and reconciles into ``source_descriptors`` via
    :func:`legba.data.discovery.source_materializer.reconcile_discovered_sources`
    (which also triggers selector auto-wire).

The G20 fix in one place
------------------------

``run_target_discovery_cycle`` is the function the actor uses to materialise the
G20 country targets. It:

  1. Resolves the descriptor's declared :class:`SourceDeps`
     (``deps.postgres: true``) into a :class:`ResolvedDiscoveryDeps` bundle
     ONCE, via :func:`resolve_discovery_deps`.
  2. Constructs the discovery handler and binds the resolved bundle
     (``handler.bind_resolved_deps(...)``).
  3. Runs ``handler.discover(ctx)`` — which reads ``iso_countries`` via the
     resolved Postgres pool, NOT ``ctx.stack_resolve('postgres')``.
  4. Feeds the emitted candidates through the target reconcile loop.

The old per-target ``ctx.stack_resolve`` plumbing is gone from this path —
``ctx.stack_resolve`` is left ``None``.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

import asyncpg

from ..sources._contract import InMemoryStateStore
from ._contract import CandidateTarget, DiscoveryContext
from .deps_resolver import ResolvedDiscoveryDeps, resolve_discovery_deps
from .source_contract import CandidateSource

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Handler construction + dep binding
# ---------------------------------------------------------------------------


def _build_target_handler(kind: str) -> Any:
    """Instantiate a target-discovery handler by kind via the discovery registry."""
    from .registry import discover_discovery_kinds

    bundle = discover_discovery_kinds().get(kind)
    if bundle is None or bundle.is_static:
        raise ValueError(
            f"unknown target-discovery kind {kind!r}; "
            f"known: {sorted(discover_discovery_kinds())}"
        )
    handler_cls = getattr(bundle.module, "HANDLER", None) or getattr(
        bundle.module, "DISCOVERY_HANDLER", None
    )
    if handler_cls is None:
        raise ValueError(
            f"target-discovery kind {kind!r} has no HANDLER class"
        )
    return handler_cls()


def _build_source_discovery_handler(kind: str) -> Any:
    """Instantiate a source-discovery handler by kind."""
    import importlib

    # Source-discovery kinds live as first-party modules under this package.
    # Map the registered kind name to its module (the only first-party kind is
    # query_source_discovery for now; the firecrawl/CT crawl flavor plugs in
    # the same way).
    module_by_kind = {
        "query_source_discovery": "query_source_discovery",
    }
    mod_name = module_by_kind.get(kind)
    if mod_name is None:
        raise ValueError(
            f"unknown source-discovery kind {kind!r}; "
            f"known: {sorted(module_by_kind)}"
        )
    module = importlib.import_module(f"{__name__.rsplit('.', 1)[0]}.{mod_name}")
    handler_cls = getattr(module, "HANDLER", None)
    if handler_cls is None:
        raise ValueError(f"source-discovery kind {kind!r} has no HANDLER class")
    return handler_cls()


def _bind_deps(handler: Any, resolved: ResolvedDiscoveryDeps) -> None:
    """Bind resolved deps onto a handler that supports it (the G20 seam)."""
    if hasattr(handler, "bind_resolved_deps"):
        handler.bind_resolved_deps(resolved)


def _build_config(handler: Any, raw_config: Mapping[str, Any]) -> Any:
    """Parse the descriptor's discovery.config into the handler's config_schema."""
    schema = getattr(handler, "config_schema", None)
    if schema is None:
        raise ValueError(
            f"discovery handler {type(handler).__name__} has no config_schema"
        )
    return schema.model_validate(dict(raw_config))


# ---------------------------------------------------------------------------
# Target flavor — the G20 entry
# ---------------------------------------------------------------------------


async def run_target_discovery_cycle(
    conn: asyncpg.Connection,
    discovery_descriptor: Any,
    deps: Any,
    *,
    declared_deps: Any = None,
    config_override: Mapping[str, Any] | None = None,
    template_body: Mapping[str, Any] | None = None,
    lookup_tables: Mapping[str, Mapping[str, Any]] | None = None,
    dlq: Any = None,
    nats_publish: Any = None,
    qdrant: Any = None,
    embedding: Any = None,
    object_store: Any = None,
) -> Any:
    """Run one TARGET-discovery cycle end-to-end, with actor-resolved deps.

    This is the G20-fix entry. ``deps`` is a
    :class:`legba.runtime.deps.StandardDeps` carrying the substrate pool +
    secret resolver. ``declared_deps`` is the descriptor's declared
    :class:`~legba.data.schemas.source.SourceDeps` (``deps.postgres: true`` for
    ``iso_3166``); when omitted we derive it from the discovery descriptor's
    ``discovery.config.deps`` block if present, else assume postgres for the
    builtin country list.

    Returns the
    :class:`legba.data.registry.discovered_materializer.ReconcileResult`.
    """
    block = discovery_descriptor.discovery
    if block is None:
        raise ValueError(
            f"run_target_discovery_cycle called on descriptor "
            f"{discovery_descriptor.identity.id!r} with no discovery block"
        )

    # 1. Resolve declared deps ONCE (the actor's job).
    declared = declared_deps
    if declared is None:
        declared = _infer_declared_deps(block)
    resolved = await resolve_discovery_deps(
        declared,
        deps,
        qdrant=qdrant,
        embedding=embedding,
        object_store=object_store,
    )

    # 2. Build the handler + bind the resolved bundle (G20 seam).
    handler = _build_target_handler(block.kind)
    _bind_deps(handler, resolved)

    # 3. Build the config + context (note: stack_resolve stays None).
    raw_config = dict(config_override or block.config or {})
    if not raw_config.get("list_source") and getattr(block, "list_source", ""):
        raw_config["list_source"] = block.list_source
    config = _build_config(handler, raw_config)
    ctx = DiscoveryContext(
        discovery_id=discovery_descriptor.identity.id,
        discovery_version=discovery_descriptor.identity.version,
        config=config,
        state_store=InMemoryStateStore(),
        secrets_resolve=getattr(deps, "secrets_resolve", None),
        stack_resolve=None,  # retired — the G20 blocker is gone
    )

    # 4. Emit candidates (reads iso_countries via resolved.postgres).
    candidates: list[CandidateTarget] = []
    async for cand in handler.discover(ctx):
        candidates.append(cand)

    # 5. Reconcile into target_descriptors via the registry materialiser.
    from ..registry.discovered_materializer import reconcile_discovered_targets

    return await reconcile_discovered_targets(
        conn,
        discovery_descriptor,
        candidates,
        template_body=template_body,
        lookup_tables=lookup_tables,
        dlq=dlq,
        nats_publish=nats_publish,
    )


def _infer_declared_deps(block: Any) -> Any:
    """Infer the declared SourceDeps for a discovery block.

    A discovery block may carry an explicit ``config.deps`` mapping
    (``{"postgres": true}``); otherwise the builtin ``iso_3166`` /
    ``substrate:`` list sources imply ``postgres: true`` and inline/url sources
    imply none.
    """
    from ..schemas.source import SourceDeps

    cfg = getattr(block, "config", {}) or {}
    if isinstance(cfg, Mapping) and "deps" in cfg:
        return SourceDeps.model_validate(cfg["deps"])

    list_source = (
        cfg.get("list_source") if isinstance(cfg, Mapping) else ""
    ) or getattr(block, "list_source", "")
    needs_pg = (
        list_source in ("iso_3166", "substrate:source_credibility")
        or str(list_source).startswith("substrate:")
    )
    return SourceDeps(postgres=needs_pg)


# ---------------------------------------------------------------------------
# Source flavor entry
# ---------------------------------------------------------------------------


async def run_source_discovery_cycle(
    conn: asyncpg.Connection,
    discovery_descriptor: Any,
    deps: Any,
    *,
    declared_deps: Any = None,
    config_override: Mapping[str, Any] | None = None,
    template_body: Mapping[str, Any] | None = None,
    lookup_tables: Mapping[str, Mapping[str, Any]] | None = None,
    source_registry: Any = None,
    probe_handler: Any = None,
    dlq: Any = None,
    auto_wire: bool = True,
    nats_publish: Any = None,
) -> Any:
    """Run one SOURCE-discovery cycle end-to-end, with actor-resolved deps.

    ``discovery_descriptor`` is a
    :class:`~legba.data.schemas.source.SourceDescriptor` carrying a
    :class:`~legba.data.schemas.source.SourceDiscoveryBlock`. Returns the
    :class:`legba.data.discovery.source_materializer.ReconcileSourceResult`.
    """
    block = discovery_descriptor.discovery
    if block is None:
        raise ValueError(
            f"run_source_discovery_cycle called on descriptor "
            f"{discovery_descriptor.identity.id!r} with no discovery block"
        )

    declared = declared_deps if declared_deps is not None else _infer_declared_deps(block)
    resolved = await resolve_discovery_deps(declared, deps)

    handler = _build_source_discovery_handler(block.kind)
    _bind_deps(handler, resolved)

    raw_config = dict(config_override or block.config or {})
    if not raw_config.get("list_source") and getattr(block, "list_source", ""):
        raw_config["list_source"] = block.list_source
    config = _build_config(handler, raw_config)
    ctx = DiscoveryContext(
        discovery_id=discovery_descriptor.identity.id,
        discovery_version=discovery_descriptor.identity.version,
        config=config,
        state_store=InMemoryStateStore(),
        secrets_resolve=getattr(deps, "secrets_resolve", None),
        stack_resolve=None,
    )

    candidates: list[CandidateSource] = []
    async for cand in handler.discover(ctx):
        candidates.append(cand)

    from .source_materializer import reconcile_discovered_sources

    return await reconcile_discovered_sources(
        conn,
        discovery_descriptor,
        candidates,
        template_body=template_body,
        lookup_tables=lookup_tables,
        secrets_resolve=getattr(deps, "secrets_resolve", None),
        source_registry=source_registry,
        probe_handler=probe_handler,
        dlq=dlq,
        auto_wire=auto_wire,
        nats_publish=nats_publish,
    )


__all__ = [
    "run_target_discovery_cycle",
    "run_source_discovery_cycle",
]
