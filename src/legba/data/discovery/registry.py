# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discovery-kind registry — mirrors :func:`legba.data.analysts.discover_analyst_kinds`.

Per L-241 the runtime needs a uniform discovery-style surface for every
kind family (sources, filters, analysts, outputs, discovery) so the
runtime host can wire all five at startup with the same shape.

This module owns the discovery-side equivalent:

  * :class:`DiscoveryHandlerBundle` — what the discovery dispatcher
    indexes by kind name. Mirrors :class:`legba.data.analysts.KindHandler`
    in spirit; trimmed to discovery-relevant fields.
  * :func:`discover_discovery_kinds` — walks the first-party discovery
    modules, returns a ``dict[kind_name, DiscoveryHandlerBundle]``.
  * Sentinel registration of the static-target shortcut so dispatchers
    can ask "what handles a descriptor without a discovery block?"
    and receive a typed answer pointing at
    :func:`legba.data.discovery.static.materialize_static`.

Wave B's L-181 (``country_list_discovery``) and L-182
(``file_sd_discovery``) will land as new modules under
:mod:`legba.data.discovery` exposing ``KIND_NAME`` + a ``DiscoveryKind``
class. This walker picks them up automatically — no edits to this file
required for the two reference kinds. The walker is intentionally
defensive about partial waves (same shape the analyst-kind walker has):
a kind module that fails to import is logged and skipped, the registry
returns the other kinds.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any, Callable

from . import static as static_mod
from ._contract import (
    CandidateTarget,
    DiscoveryContext,
    DiscoveryHealth,
    DiscoveryKind,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DiscoveryHandlerBundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveryHandlerBundle:
    """Discovered discovery-kind metadata.

    Attributes
    ----------
    kind_name:
        Globally unique within the discovery-kind namespace
        (``country_list_discovery``, ``file_sd_discovery``,
        ``static_target_shortcut``).
    schema_version:
        Iglu-style version pinned to the handler shape.
    discover:
        Async iterator factory ``(ctx) -> AsyncIterator[CandidateTarget]``.
        ``None`` for the static-target shortcut (no async iterator —
        materialization is synchronous identity).
    materialize_static:
        Synchronous identity materializer for the static path. ``None``
        for non-static kinds. Mutually exclusive with :attr:`discover`.
    healthcheck:
        ``(ctx) -> DiscoveryHealth`` callable. ``None`` for the static
        path (no health surface — static targets are always-healthy).
    config_schema:
        Pydantic config type the handler expects. Used by the
        registration-time validator to parse the descriptor's
        ``discovery.config`` block.
    module:
        The Python module object — callers may need module-level
        constants the kind exposes (e.g., ``DEFAULT_REFRESH_INTERVAL``).
    is_static:
        Convenience flag for the dispatcher.
    """

    kind_name: str
    schema_version: str
    discover: Callable[..., Any] | None
    materialize_static: Callable[..., Any] | None
    healthcheck: Callable[..., Any] | None
    config_schema: Any
    module: Any
    is_static: bool = False


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


_KIND_MODULE_NAMES: tuple[str, ...] = (
    # L-181 (Wave B) — first-party discovery kinds register here as they
    # land. The walker is defensive about missing modules; this tuple is
    # the *intended* set, not a guarantee.
    "country_list_discovery",
    "file_sd_discovery",
)
"""First-party discovery-kind modules under :mod:`legba.data.discovery`.

The static-target shortcut is registered as a sentinel below — it has no
``DiscoveryKind`` class and intentionally lives outside this tuple."""


def discover_discovery_kinds() -> dict[str, DiscoveryHandlerBundle]:
    """Walk the package, import every discovery-kind module, return a
    registry.

    The static-target shortcut (:data:`static_mod.STATIC_KIND_NAME`) is
    always present. First-party kinds (L-181 / L-182) register as they
    land. Modules that fail to import are logged + skipped — the
    registry still returns the kinds that did import (same defensive
    shape :func:`legba.data.analysts.discover_analyst_kinds` has).

    Returns
    -------
    dict[str, DiscoveryHandlerBundle]
        Keyed by ``KIND_NAME``.
    """
    registry: dict[str, DiscoveryHandlerBundle] = {}

    # Sentinel: static-target shortcut.
    registry[static_mod.STATIC_KIND_NAME] = DiscoveryHandlerBundle(
        kind_name=static_mod.STATIC_KIND_NAME,
        schema_version="legba/discovery/static/1.0.0",
        discover=None,
        materialize_static=static_mod.materialize_static,
        healthcheck=None,
        config_schema=None,
        module=static_mod,
        is_static=True,
    )

    for mod_name in _KIND_MODULE_NAMES:
        try:
            module = importlib.import_module(f"{__name__.rsplit('.', 1)[0]}.{mod_name}")
        except Exception as exc:                                # pragma: no cover
            # Wave B modules: not yet present is the expected state.
            logger.info(
                "discovery.discover.import_skipped module=%s err=%s",
                mod_name, exc,
            )
            continue

        kind_name = getattr(module, "KIND_NAME", None)
        if not kind_name:
            logger.warning(
                "discovery.discover.skip module=%s reason=missing_KIND_NAME",
                mod_name,
            )
            continue

        handler_cls = getattr(module, "HANDLER", None) or getattr(
            module, "DISCOVERY_HANDLER", None
        )
        # Some kinds expose the class via a factory; fall back to module-
        # level ``discover`` + ``healthcheck`` callables if no class is
        # surfaced.
        discover_fn = getattr(module, "discover", None)
        healthcheck_fn = getattr(module, "healthcheck", None) or getattr(
            module, "health_check", None
        )
        config_schema = getattr(module, "CONFIG_SCHEMA", None) or getattr(
            module, "config_schema", None
        )
        if handler_cls is not None:
            # Pull from the class if module level didn't define them.
            discover_fn = discover_fn or getattr(handler_cls, "discover", None)
            healthcheck_fn = healthcheck_fn or getattr(handler_cls, "healthcheck", None)
            config_schema = config_schema or getattr(handler_cls, "config_schema", None)

        if discover_fn is None:
            logger.warning(
                "discovery.discover.skip module=%s reason=missing_discover_callable",
                mod_name,
            )
            continue

        registry[str(kind_name)] = DiscoveryHandlerBundle(
            kind_name=str(kind_name),
            schema_version=str(
                getattr(module, "SCHEMA_VERSION", None)
                or getattr(handler_cls, "schema_version", "0.0.0")
            ),
            discover=discover_fn,
            materialize_static=None,
            healthcheck=healthcheck_fn,
            config_schema=config_schema,
            module=module,
            is_static=False,
        )

    return registry


__all__ = [
    "CandidateTarget",
    "DiscoveryContext",
    "DiscoveryHandlerBundle",
    "DiscoveryHealth",
    "DiscoveryKind",
    "discover_discovery_kinds",
]
