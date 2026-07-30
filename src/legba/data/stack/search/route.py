# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Search-provider route resolution — which component id serves this call.

Deliberately mirrors ``resolve_judge_route_from_llm_block``
(``src/legba/runtime/analyst_deps_builder.py``) INCLUDING its rung-0 opt-in
gate, so both operator-facing routes behave the same way:

===== ==================================== =========================================
Rung  Source                               Behaviour
===== ==================================== =========================================
0     —                                    **OPT-IN GATE.** The block must exist and
                                           carry a ``primary`` or ``fallback`` key.
                                           Absent → ``None`` → no search route ever.
1     ``LEGBA_SEARCH_STACK_REF`` (env)      Global operator repoint. It REPOINTS, it
                                           never ENABLES — an analyst that did not
                                           ask for search cannot be conscripted.
2     ``<block>.primary``                   The explicit per-descriptor choice.
3     ``<block>.fallback``                  The declared degraded provider.
4     —                                    ``None`` → the caller's terminal rung
                                           (the legacy operator-pinned endpoint),
                                           else a clean, loud failure.
===== ==================================== =========================================

The block is any mapping of ``StackRef``-shaped values — ``method.search`` on an
analyst descriptor, or the ``web_search`` ToolSpec's ``config.provider`` block
on the ``web_access`` action pack. One ladder, two entry points.

``SearchRoute.component_id`` is what gets stamped into provenance on any finding
whose evidence came through search — the same discipline as ``judge_llm_ref`` on
the critique row. Without it there is no way to re-audit later which provider
introduced which claims.

NOTE on ``expected_family`` (``src/legba/data/schemas/properties.py``): it is
DOCUMENTATION ONLY. It is stripped at bind time by ``_unwrap_factory_dict()``
(it is a member of ``_FACTORY_KEY_HINTS`` in both ``runtime/source_factory.py``
and ``runtime/dapr_actors.py``) and no code compares it against the resolved
component's kind. Real validation is :func:`assert_search_component`, called at
handler-build time.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, NamedTuple

from .base import HardSearchFailure

#: Global operator repoint. Repoints an ALREADY-opted-in route; never enables.
SEARCH_STACK_REF_ENV = "LEGBA_SEARCH_STACK_REF"

#: The stack family this route must resolve to.
SEARCH_PROVIDER_KIND = "search_provider"


class SearchRoute(NamedTuple):
    """Which component id serves a search call, and WHY that one."""

    component_id: str
    #: ``env:LEGBA_SEARCH_STACK_REF`` | ``<block>.primary`` | ``<block>.fallback``
    source: str

    @property
    def route_class(self) -> str:
        """``"configured"`` for env/primary, ``"fallback"`` for the declared
        degraded provider, ``""`` when unclassifiable. Carried into provenance
        so a finding records that it ran on the fallback."""
        if self.source.endswith(".fallback"):
            return "fallback"
        if self.source:
            return "configured"
        return ""


def stack_ref_raw(value: Any) -> str | None:
    """Shape-tolerant ``StackRef`` extraction → the component id, else ``None``.

    Accepts the dumped mapping (``{"factory_kind": "stack_ref", "raw": …}``), a
    live ``StackRef`` object, or a bare string — the same variants the judge
    route accepts, so a descriptor authored either way resolves identically.
    """
    if value is None:
        return None
    if isinstance(value, Mapping):
        raw = value.get("raw")
        return str(raw) if isinstance(raw, str) and raw else None
    raw = getattr(value, "raw", None)
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(value, str) and value:
        return value
    return None


def resolve_search_route(block: Any, *, block_name: str = "method.search") -> SearchRoute | None:
    """Resolve the search route over a raw ``method.search``-shaped mapping.

    ``block_name`` only labels :attr:`SearchRoute.source` so provenance says
    which descriptor key was read.
    """
    if not isinstance(block, Mapping):
        return None
    # Rung 0 — the opt-in gate. No primary/fallback key ⇒ no search route, ever.
    if "primary" not in block and "fallback" not in block:
        return None
    # Rung 1 — the global operator repoint (repoints, never enables).
    env_ref = (os.getenv(SEARCH_STACK_REF_ENV) or "").strip()
    if env_ref:
        return SearchRoute(
            component_id=env_ref, source=f"env:{SEARCH_STACK_REF_ENV}",
        )
    # Rung 2 — the explicit per-descriptor choice.
    primary = stack_ref_raw(block.get("primary"))
    if primary:
        return SearchRoute(component_id=primary, source=f"{block_name}.primary")
    # Rung 3 — the declared degraded provider.
    fallback = stack_ref_raw(block.get("fallback"))
    if fallback:
        return SearchRoute(component_id=fallback, source=f"{block_name}.fallback")
    # Rung 4 — opted in but every ref malformed. Terminal: the caller falls
    # through to its own last rung (legacy endpoint) or fails loudly.
    return None


def resolve_tool_search_route(tool_config: Any) -> SearchRoute | None:
    """The same ladder over a ``web_search`` ToolSpec ``config`` mapping.

    Accepts both the nested block (``config.provider.primary`` /
    ``config.provider.fallback``) and the single-ref shorthand
    (``config.provider`` = one ``StackRef``), which is the common case.
    """
    if not isinstance(tool_config, Mapping):
        return None
    provider = tool_config.get("provider")
    if provider is None:
        return None
    if isinstance(provider, Mapping) and (
        "primary" in provider or "fallback" in provider
    ):
        return resolve_search_route(provider, block_name="config.provider")
    ref = stack_ref_raw(provider)
    if not ref:
        return None
    # Shorthand still honours the global repoint (rung 1) so one env var can
    # move every search call at once.
    env_ref = (os.getenv(SEARCH_STACK_REF_ENV) or "").strip()
    if env_ref:
        return SearchRoute(
            component_id=env_ref, source=f"env:{SEARCH_STACK_REF_ENV}",
        )
    return SearchRoute(component_id=ref, source="config.provider")


def assert_search_component(schema_uri: str, component_id: str) -> None:
    """The bind-time validation ``expected_family`` does NOT give you.

    Raises :class:`HardSearchFailure` when the resolved component is not a
    ``search_provider`` — mirroring how the LLM path raises a build error
    naming the mismatch rather than silently binding the wrong family.
    """
    # Local import: registry.stack pulls the full stack schema catalog, and the
    # handler package must stay importable from the tool layer without it.
    from ...registry.stack import kind_from_schema_uri

    try:
        kind = kind_from_schema_uri(schema_uri)
    except ValueError as exc:
        raise HardSearchFailure(
            f"stack-component {component_id!r}: unparseable schema_uri "
            f"{schema_uri!r}"
        ) from exc
    if kind != SEARCH_PROVIDER_KIND:
        raise HardSearchFailure(
            f"stack-component {component_id!r} is kind {kind!r}, not "
            f"{SEARCH_PROVIDER_KIND!r} — a search route must resolve to a "
            "search_provider component"
        )


__all__ = [
    "SEARCH_PROVIDER_KIND",
    "SEARCH_STACK_REF_ENV",
    "SearchRoute",
    "assert_search_component",
    "resolve_search_route",
    "resolve_tool_search_route",
    "stack_ref_raw",
]
