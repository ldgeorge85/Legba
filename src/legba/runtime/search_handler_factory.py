# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ONE process-wide resolver for the ``search_provider`` stack family (#85).

Why this module exists
----------------------

``ToolContext.search`` is bound at TWO independent sites, because two different
kinds of analyst reach the ``web_access`` pack by two different routes:

  * ``dapr_host._analyst_deps_resolver`` — the in-actor GATHER path, for an
    ``llm_planner`` kind whose model drives a tool loop;
  * ``external_audit_binding.wire_standing_auditor_web_pack`` — the
    ``standing_auditor``, a ``deterministic`` sub-handler that calls exactly one
    pack tool from code on a fixed schedule.

Only the FIRST one ever resolved a provider. The second sourced its handler from
``AGENCY_HOLDER["tool_context"]``, the process-wide bring-up ToolContext, which
``source_first_runtime`` builds with ``queue`` + ``emit`` and nothing else — so
``base_ctx.search`` was structurally ``None`` on every run since the component
was registered on 2026-07-28. The pack's rung-1 route still RESOLVED, so every
auditor search took ``web_search``'s declared-but-unbound branch and failed with
``search_provider_unresolved``: claims extracted, every verdict ``UNCHECKED``,
heartbeat honestly ``healthy:false``. The search leg was dead on arrival at the
one analyst built to use it.

The lesson is not "fix that one line". It is that a capability resolved inline
at one binding site is invisible to the next site somebody adds. So the
resolution lives HERE, once, and both sites call it — a third site gets it right
by construction, and ``pack -> route -> handler`` cannot drift between callers
because there is only one implementation of it.

What it preserves
-----------------

The behaviour contract is ``dapr_host``'s, verbatim, because it was correct:

  * a SUCCESS is cached per component id — a search handler holds no persistent
    client (every query opens its own SSRF-guarded one), so one instance is
    freely shareable, and sharing it is what makes the "ONE provider, ONE
    control-probe budget, ONE deferral ladder" claim in ``external_audit_binding``
    true rather than aspirational;
  * a FAILURE is NEVER cached (the ``#235`` Lazy* lesson): a component
    registered — or a registry recovered — minutes after boot must heal on the
    NEXT deps build, not on the next container restart;
  * failure returns ``None`` rather than raising, and that is not a silent
    degradation. The only consumer binds the result into ``ToolContext.search``,
    and ``web_search`` turns a DECLARED-but-unbound route into a LOUD
    ``search_provider_unresolved`` tool failure that says NO query was issued.
    Raising here would take the whole analyst's deps build down (the actor never
    activates) over a capability that is additive to its substrate work.

Rung discipline (the ladder in ``descriptors/action_pack_web_access.yaml``) is
untouched: no ``provider`` key on the ToolSpec ⇒ :func:`resolve_pack_search_route`
returns ``(None, None)`` ⇒ nothing is bound ⇒ ``web_search`` falls through to the
rung-3 operator-pinned endpoint (``config.endpoint`` / ``LEGBA_WEB_SEARCH_ENDPOINT``)
exactly as before.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, MutableMapping

logger = logging.getLogger(__name__)

#: Process-lifetime cache of CONFIGURED handlers, keyed by component id.
#: Successes only — see the module docstring.
_HANDLER_CACHE: dict[str, Any] = {}


def search_handler_cache() -> MutableMapping[str, Any]:
    """The shared cache object (ops/tests; also the eviction-registry handle)."""
    return _HANDLER_CACHE


def clear_search_handler_cache() -> int:
    """Drop every cached handler. Returns the count. Test/ops hook only."""
    n = len(_HANDLER_CACHE)
    _HANDLER_CACHE.clear()
    return n


async def resolve_search_handler(
    component_id: str,
    *,
    registry_client: Any,
    secrets_resolve: Callable[[str], Awaitable[bytes]],
) -> Any | None:
    """Build (or reuse) the configured handler for ``component_id``.

    Returns ``None`` — never raises — when the component cannot be resolved,
    having logged the gap at ERROR with the operator's next action.
    """
    if not component_id:
        return None
    existing = _HANDLER_CACHE.get(component_id)
    if existing is not None:
        return existing

    # Imported here, not at module scope: analyst_deps_builder pulls in the
    # whole stack/handler surface, and this module is imported by the slim
    # binding paths.
    from .analyst_deps_builder import build_search_handler_from_stack_component

    try:
        handler = await build_search_handler_from_stack_component(
            component_id,
            registry_client=registry_client,
            secrets_resolve=secrets_resolve,
        )
    except Exception as exc:
        logger.error(
            "search_handler_factory.unresolved component=%s err=%s — the "
            "web_search ToolSpec DECLARES this route; every web_search call "
            "will fail loudly with search_provider_unresolved (never an empty "
            "result set) until it resolves. Register the component "
            "(scripts/bringup_register_stack.py) or drop the ref. Retried on "
            "the NEXT deps build.",
            component_id, exc,
        )
        return None
    logger.info(
        "search_handler_factory.bound component=%s subprovider=%s",
        component_id, getattr(handler, "subprovider", "?"),
    )
    _HANDLER_CACHE[component_id] = handler
    return handler


def resolve_pack_search_route(pack: Any, *, tool_name: str = "web_search") -> Any | None:
    """The pack's search ToolSpec ``config`` → a ``SearchRoute`` or None.

    Rung 0 of the ladder is an OPT-IN GATE: a ToolSpec with no ``provider`` key
    yields no route, which is how an operator keeps the legacy endpoint rungs in
    charge.

    ``tool_name`` defaults to ``web_search`` (byte-identical for both existing
    callers) and is passed as ``web_evidence`` by the ``research`` pack's
    binding — the two packs route through the SAME ladder, from the same
    implementation, so a repoint can never take effect for one and not the
    other.
    """
    from ..data.stack.search import resolve_tool_search_route

    cfg = next(
        (dict(t.config) for t in (getattr(pack, "tools", None) or [])
         if getattr(t, "name", None) == tool_name),
        {},
    )
    return resolve_tool_search_route(cfg)


def resolve_pack_search_ladder(pack: Any) -> list[Any]:
    """The pack's ``web_search`` ToolSpec ``config`` → the ORDERED rung list.

    Rung 0 is exactly what :func:`resolve_pack_search_route` returns; the rungs
    below it come from the optional ``config.fallback_providers``. Absent (the
    shipped state) the list is rung 0 alone — or empty when the ToolSpec never
    opted in — so this is a strict superset of the single-route resolution and
    the two can never disagree about rung 0.
    """
    from ..data.stack.search import resolve_tool_search_ladder

    cfg = next(
        (dict(t.config) for t in (getattr(pack, "tools", None) or [])
         if getattr(t, "name", None) == "web_search"),
        {},
    )
    return resolve_tool_search_ladder(cfg)


async def resolve_pack_search_fallback_bindings(
    pack: Any,
    *,
    registry_client: Any,
    secrets_resolve: Callable[[str], Awaitable[bytes]] | None,
) -> list[tuple[Any | None, Any]]:
    """Build the rungs BELOW rung 0 — ``[(handler|None, route), …]``.

    Returns ``[]`` when the ToolSpec declares no ``fallback_providers``, which
    is what keeps the whole ladder additive: nothing is bound, ``web_search``
    sees no fallbacks, and the single-provider path runs untouched.

    A rung that cannot be built binds ``(None, route)`` rather than being
    dropped. That distinction is load-bearing and mirrors rung 0's own rule: a
    ladder that silently gets shorter is how "we had no paid provider today"
    becomes indistinguishable from "the paid provider found nothing". The tool
    reports an unbuildable rung BY NAME and moves on to the next one.

    Every rung resolves through :func:`resolve_search_handler`, so a fallback
    shares the SAME process-wide handler cache — and therefore the same
    control-probe budget and deferral ladder — as every other caller of that
    component id.
    """
    rungs = resolve_pack_search_ladder(pack)
    if len(rungs) < 2:
        return []
    out: list[tuple[Any | None, Any]] = []
    for route in rungs[1:]:
        if secrets_resolve is None:
            logger.error(
                "search_handler_factory.no_secrets_resolver_fallback component=%s "
                "source=%s — the fallback rung is DECLARED but this caller has no "
                "secrets resolver to build it with; a KEYED provider (Brave) "
                "cannot be bound at all, and web_search will report the rung "
                "unresolved rather than silently having one fewer.",
                route.component_id, route.source,
            )
            out.append((None, route))
            continue
        handler = await resolve_search_handler(
            route.component_id,
            registry_client=registry_client,
            secrets_resolve=secrets_resolve,
        )
        out.append((handler, route))
    return out


async def resolve_pack_search_binding(
    pack: Any,
    *,
    registry_client: Any,
    secrets_resolve: Callable[[str], Awaitable[bytes]] | None,
) -> tuple[Any | None, Any | None]:
    """``pack`` → ``(handler, route)`` for its ``web_search`` ToolSpec.

    The whole rung-1 resolution both binding sites need, in one call:

      * no route declared            -> ``(None, None)``  (fall through to rung 3)
      * route declared + resolvable  -> ``(handler, route)``
      * route declared + unresolvable-> ``(None, route)``  (``web_search`` then
        fails LOUDLY — an unresolved provider and an empty web must never share
        a wire shape)

    ``secrets_resolve=None`` cannot build a handler, so it is treated as the
    unresolvable case AND logged: a caller that lost its secrets plumbing would
    otherwise look exactly like an operator who never registered the component.
    """
    route = resolve_pack_search_route(pack)
    if route is None:
        return None, None
    if secrets_resolve is None:
        logger.error(
            "search_handler_factory.no_secrets_resolver component=%s source=%s "
            "— the route is DECLARED but this caller has no secrets resolver to "
            "build it with; web_search will fail with "
            "search_provider_unresolved.",
            route.component_id, route.source,
        )
        return None, route
    handler = await resolve_search_handler(
        route.component_id,
        registry_client=registry_client,
        secrets_resolve=secrets_resolve,
    )
    return handler, route


__all__ = [
    "clear_search_handler_cache",
    "resolve_pack_search_binding",
    "resolve_pack_search_fallback_bindings",
    "resolve_pack_search_ladder",
    "resolve_pack_search_route",
    "resolve_search_handler",
    "search_handler_cache",
]
