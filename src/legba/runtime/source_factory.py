# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source-kind factory — production generalization of the spike's hardcoded
``if kind == "rss": return rss_handler`` shim.

The Dapr-runtime's :class:`legba.runtime.dapr_actors._TargetDeps` carries a
``source_factory: Callable[[str, Mapping[str, Any]], SourceHandler]`` field
that the actor invokes per :class:`SourceBinding` on every pull cycle.  The
spike wired a single-kind closure inline; the production runtime needs a
generic mapping from descriptor-side ``binding.kind`` (one of ``rss`` /
``gdelt_query`` / ``acled`` / ...) to the matching first-party handler
class plus a parsed instance of that handler's pydantic ``config_schema``.

This module owns that mapping.  It mirrors the analyst-side discovery
pattern in :func:`legba.data.analysts.discover_analyst_kinds` — walk the
``legba.data.sources`` package, defensive-import each handler module,
collect ``(handler.kind -> handler_class)`` pairs.  A missing optional
runtime dep (e.g. ``google-cloud-bigquery`` for GDELT, ``followthemoney``
for OpenSanctions) shouldn't poison the whole registry — the offending
module is logged + skipped, the rest stay reachable.

Construction rules per kind
---------------------------

Source handlers have non-uniform ``__init__`` signatures — some take just
``(config, *, http_client=None)`` (RSS, scraper, common_crawl); a few
require a credential resolver (MediaCloud, Firecrawl, Discord, Telegram,
GDELT); a couple are zero-arg (``ACLEDSourceHandler()`` carries config via
:class:`SourceContext` at pull time).  The factory inspects each handler's
``__init__`` parameter set and only threads in dependencies the handler
declares; this keeps the call site uniform without forcing every handler
to grow the same constructor surface.

The ``secrets_resolve`` callable threaded through this factory matches
:class:`legba.runtime.deps.StandardDeps.secrets_resolve` —
``async (vault_id) -> bytes``.  Handlers whose ``__init__`` expects a
resolver receive it directly; handlers that read secrets via
:class:`SourceContext` (e.g. ``GDELTBigQuerySourceHandler`` looks for
``ctx.secrets_resolve`` first and falls back to the constructor-injected
resolver) get it both ways for belt-and-braces.

Runtime-isolation constraints
-----------------------------

This module is imported by the Dapr host AND the Temporal-worker sandbox;
neither may pull in dapr-sdk-only or httpx-LLM-stack imports.  All handler
imports are kept under :func:`discover_source_kinds`'s defensive try/except
so a missing optional package never crashes the runtime bring-up.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Awaitable, Callable, ClassVar, Mapping, Protocol, cast

from pydantic import BaseModel

from ..data.sources._contract import SourceHandler


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


# Tuple of (module_name, attr_name) pairs.  ``attr_name`` is the handler
# class to look up inside the module after a successful import.  Modules
# whose import fails (missing optional dep, mid-wave merge) are skipped
# silently with a warning — mirrors the analysts-package pattern.
#
# Ordering is informational only — the returned registry is a plain dict
# keyed by ``handler.kind``.  A leading entry is no priority signal.
_SOURCE_MODULE_TABLE: tuple[tuple[str, str], ...] = (
    ("rss",            "RSSSourceHandler"),
    ("geojson",        "GeoJSONSourceHandler"),
    ("json_api",       "JsonApiSourceHandler"),
    ("generic_webhook", "GenericWebhookSourceHandler"),
    ("acled",          "ACLEDSourceHandler"),
    ("ucdp",           "UCDPSourceHandler"),
    ("mediacloud",     "MediaCloudSourceHandler"),
    ("opensanctions",  "OpenSanctionsSourceHandler"),
    ("gdelt",          "GDELTBigQuerySourceHandler"),
    ("gdelt_files",    "GDELTFilesSourceHandler"),
    ("scraper",        "ScraperSourceHandler"),
    ("firecrawl",      "FirecrawlSourceHandler"),
    ("discord",        "DiscordWebhookSourceHandler"),
    ("telegram",       "TelegramChannelSourceHandler"),
    ("common_crawl",   "CommonCrawlNewsSourceHandler"),
    ("intelmq",        "IntelMQCollectorBridge"),
)


# Async callable shape the runtime threads through for vault-secret
# resolution.  Matches :attr:`legba.runtime.deps.StandardDeps.secrets_resolve`
# — handlers that need it either take it via ``__init__`` (MediaCloud,
# Firecrawl, ...) or read it off the :class:`SourceContext` at pull time
# (GDELT, Discord).
SecretsResolveFn = Callable[[str], Awaitable[bytes]]


# ---------------------------------------------------------------------------
# Property-factory dict unwrapper (mirror of dapr_actors._unwrap_factory_dict)
# ---------------------------------------------------------------------------
#
# The descriptor body stores property-factory values as small dicts —
# ``{"raw": "...", "ui_hint": {}, "regex": None, ...}`` — rather than the
# bare value the handler's ``config_schema`` expects.  We have to unwrap
# them here too because the factory is sometimes invoked from code paths
# that don't go through ``_make_source_context`` (e.g. the temporal-side
# pre-flight ``_parse_source_config`` check).  Keeping a local copy avoids
# a runtime->runtime import cycle.


_FACTORY_KEY_HINTS: frozenset[str] = frozenset({
    "ui_hint", "regex", "max_length", "minimum", "maximum", "options",
    "fetcher", "expected_family", "schema_fetcher",
    "factory_kind", "item_kind", "key_kind", "value_kind",
})


def _unwrap_factory_dict(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a Property-factory-shaped dict to the bare-value dict the
    handler's ``config_schema`` constructor expects.

    See :func:`legba.runtime.dapr_actors._unwrap_factory_dict` for the
    canonical version — this is a local copy to keep the factory's import
    graph independent of the actor module.
    """
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if isinstance(v, Mapping):
            siblings = set(v.keys()) - {"raw", "ui_hint"}
            looks_like_factory = "raw" in v and (
                not siblings or siblings.issubset(_FACTORY_KEY_HINTS)
            )
            if looks_like_factory:
                out[k] = v["raw"]
            else:
                out[k] = _unwrap_factory_dict(v)
        elif isinstance(v, list):
            out[k] = [
                _unwrap_factory_dict(i) if isinstance(i, Mapping) else i
                for i in v
            ]
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class _HandlerCls(Protocol):
    """Structural typing surface for the handler-class side of the registry.

    The runtime keeps the class itself (not an instance) so the factory
    can introspect its ``__init__`` signature per call and pass only the
    parameters the handler declares.
    """

    kind: ClassVar[str]
    config_schema: ClassVar[type[BaseModel]]

    def __call__(self, *args: Any, **kwargs: Any) -> SourceHandler: ...


def discover_source_kinds() -> dict[str, type[SourceHandler]]:
    """Walk the ``legba.data.sources`` package and return a kind -> class map.

    Each handler module exposes a class with ``kind: ClassVar[str]`` and
    ``config_schema: ClassVar[type[BaseModel]]`` (per L-102 §2).

    Returns a dict keyed by the handler's ``.kind`` string mapping to
    the handler class (NOT an instance).  Callers construct an instance
    via :func:`build_source_handler`.

    K-3: the table names BOTH a module and a class, so it can be wrong in two
    ways, and both used to be a warning-and-skip. The consequence is worse
    here than for analysts because the *key* is the class's ``kind``
    attribute, not the module name — three handlers already dispatch under a
    kind that differs from their filename (``discord`` →
    ``discord_webhook``, ``common_crawl`` → ``common_crawl_news``,
    ``telegram`` → ``telegram_channel``). A skipped module removes a source
    kind, and every descriptor naming it fails to build a handler with no
    boot-time explanation. Note the optional-dependency worry does not apply:
    every handler that needs an optional package (``intelmq``, GDELT's
    BigQuery client) imports it lazily inside a method, so all 16 modules
    import with the base dependency set.

    Raises
    ------
    KindDiscoveryError
        Any declared module failed to import, lacks the named handler class,
        or that class has no non-empty ``kind``.
    """
    from ..data.kind_discovery import (
        DiscoveryFailure, import_declared_module, raise_if_failed,
    )

    registry: dict[str, type[SourceHandler]] = {}
    failures: list[DiscoveryFailure] = []
    for mod_name, cls_name in _SOURCE_MODULE_TABLE:
        dotted = f"legba.data.sources.{mod_name}"
        module = import_declared_module("sources", dotted, failures)
        if module is None:
            continue

        cls = getattr(module, cls_name, None)
        if cls is None:
            failures.append(DiscoveryFailure(
                registry="sources",
                module=dotted,
                reason="missing_contract",
                detail=f"module has no handler class {cls_name!r}",
            ))
            continue

        kind = getattr(cls, "kind", None)
        if not isinstance(kind, str) or not kind:
            failures.append(DiscoveryFailure(
                registry="sources",
                module=dotted,
                reason="missing_contract",
                detail=f"{cls_name}.kind is absent or not a non-empty str",
            ))
            continue

        if not hasattr(cls, "config_schema"):
            failures.append(DiscoveryFailure(
                registry="sources",
                module=dotted,
                reason="missing_contract",
                detail=f"{cls_name} has no config_schema",
            ))
            continue

        if kind in registry:
            # Two classes claiming one kind is ambiguous, not degraded:
            # "keeping the first registration" made the winner depend on
            # tuple order, so a reorder would silently swap which handler
            # every descriptor of that kind runs.
            failures.append(DiscoveryFailure(
                registry="sources",
                module=dotted,
                reason="duplicate_kind",
                detail=(
                    f"kind={kind!r} already registered by "
                    f"{registry[kind].__name__}; {cls.__name__} collides"
                ),
            ))
            continue

        registry[kind] = cast(type[SourceHandler], cls)

    raise_if_failed(failures)
    return registry


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


# Per-handler ``__init__`` parameter names that map to the secrets-resolve
# callable.  Different kind modules picked different names historically
# (``secret_resolver``, ``credential_resolver``); the factory accepts any
# of them and threads ``secrets_resolve`` through to the matching slot.
_SECRET_PARAM_ALIASES: tuple[str, ...] = (
    "secret_resolver",
    "credential_resolver",
    "secrets_resolve",
)


def _select_init_kwargs(
    cls: type[SourceHandler],
    config: BaseModel,
    *,
    secrets_resolve: SecretsResolveFn | None,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Build the ``__init__`` call args for ``cls``.

    Inspects the handler class's constructor signature and selects the
    arguments the handler actually declares:

      * ``config`` (positional or keyword) — passed when the handler
        takes a config parameter.  A few handlers (``ACLEDSourceHandler``,
        ``TelegramChannelSourceHandler``, ``IntelMQCollectorBridge`` with
        default-None) accept config only via :class:`SourceContext` at
        pull time, so the factory omits it.
      * ``secret_resolver`` / ``credential_resolver`` / ``secrets_resolve``
        — receive the ``secrets_resolve`` callable when the handler
        declares one of these parameters.

    All other keyword-only parameters are left to their declared defaults
    — production runtime substitutes in HTTP clients / stack resolvers via
    the lifecycle hooks (:meth:`on_configure`) the handler advertises.
    """
    try:
        sig = inspect.signature(cls.__init__)
    except (TypeError, ValueError):                             # pragma: no cover
        # Builtin / C-extension constructors that don't expose a signature
        # are best-effort: pass config positionally and hope.
        return ((config,), {})

    params = sig.parameters
    args: list[Any] = []
    kwargs: dict[str, Any] = {}

    # The "config" slot — accept either ``config`` (the canonical name
    # used across all first-party handlers) or fall through to omit when
    # the handler takes no config parameter (ACLED / Telegram / IntelMQ
    # with None default).
    if "config" in params:
        cfg_param = params["config"]
        if cfg_param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            args.append(config)
        else:
            kwargs["config"] = config

    # Secrets resolver — thread it through to the named slot when present.
    if secrets_resolve is not None:
        for alias in _SECRET_PARAM_ALIASES:
            if alias in params:
                kwargs[alias] = secrets_resolve
                break

    return tuple(args), kwargs


def build_source_handler(
    kind: str,
    config: Mapping[str, Any],
    *,
    secrets_resolve: SecretsResolveFn | None = None,
    registry: Mapping[str, type[SourceHandler]] | None = None,
) -> SourceHandler:
    """Construct a :class:`SourceHandler` for ``kind`` from a raw config dict.

    Production callable bound into
    :attr:`legba.runtime.dapr_actors._TargetDeps.source_factory`.  Steps:

      1. Look up the handler class via :func:`discover_source_kinds`
         (or the passed-in registry for tests).
      2. Unwrap descriptor-side property-factory shapes
         (``{"raw": ..., "ui_hint": ...}``) into bare values so the
         handler's pydantic ``config_schema`` validates.
      3. Instantiate the ``config_schema`` from the unwrapped dict.  A
         schema mismatch raises ``pydantic.ValidationError``, which the
         actor surfaces as a permanent-fail on that source binding.
      4. Inspect the handler's ``__init__`` and call it with only the
         parameters it declares — config (when present) plus the
         ``secrets_resolve`` callable in the named slot the handler uses
         (``secret_resolver`` / ``credential_resolver`` / ``secrets_resolve``).

    Parameters
    ----------
    kind:
        The ``SourceBinding.kind`` string from the descriptor body.
    config:
        Mapping from the descriptor's ``SourceBinding.config`` block.
        Property-factory wrapper shapes are unwrapped before being
        passed to the handler's config_schema.
    secrets_resolve:
        Async ``(vault_id) -> bytes`` callable for credential resolution.
        Threaded into the handler's constructor only when the handler
        declares a matching parameter.  Otherwise left for the runtime
        to inject via :class:`SourceContext` at pull time.
    registry:
        Optional pre-built kind -> handler-class mapping.  When omitted,
        :func:`discover_source_kinds` is invoked.  Tests pass a frozen
        registry to avoid the per-call discovery cost; production hosts
        either pass a cached registry or accept the discovery walk on
        every binding (typically <10 per descriptor).

    Raises
    ------
    ValueError
        When ``kind`` isn't registered.  The message lists the known
        kinds for the operator's benefit.
    """
    if registry is None:
        registry = discover_source_kinds()

    cls = registry.get(kind)
    if cls is None:
        known = sorted(registry.keys())
        raise ValueError(
            f"unknown source kind {kind!r}; known: {known}"
        )

    schema = getattr(cls, "config_schema", None)
    if schema is None:                                          # pragma: no cover
        raise TypeError(
            f"source handler {cls.__name__} has no config_schema; "
            "cannot instantiate generically",
        )

    unwrapped = _unwrap_factory_dict(config)
    parsed_config = schema(**unwrapped)

    args, kwargs = _select_init_kwargs(
        cls, parsed_config, secrets_resolve=secrets_resolve,
    )
    return cls(*args, **kwargs)


__all__ = [
    "SecretsResolveFn",
    "build_source_handler",
    "discover_source_kinds",
]
