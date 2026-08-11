# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Registry of process-lifetime LLM-handler caches + their invalidation (S-2).

Why this module exists
----------------------

Two long-lived caches in the runtime process key a built
:class:`~legba.data.stack.llm.base.LLMProviderHandler` by ``component_id``
alone — no version, no invalidation:

  * ``dapr_host._llm_handler_cache`` — every analyst's generation handler AND
    the verify judge (``_llm_handler_factory``);
  * ``bearing_gate._GATE_CLIENT_CACHE`` — ``claim_watch``'s 8B bearing gate.

A handler owns a long-lived ``httpx.AsyncClient`` constructed ONCE from the
component body (``httpx.Timeout(cfg.timeout_seconds.raw)`` in
``data/stack/llm/base.py``), so a stack-component PUT that changes *anything* —
timeout, endpoint, model name, max_tokens, credentials — was invisible to the
running process until a container recreate.

That is not theoretical. On 2026-08-01 the operator PUT
``llm.primary.openai_compat`` timeout 60→240 at 16:00:16Z during an incident;
it did not take effect until the 19:31Z recreate. Three and a half hours of
incident response spent on a stale cache, with nothing in the logs to say the
config had not landed.

The fix, and why it is a registry rather than one dict
-----------------------------------------------------

The eviction IDIOM already existed one module over —
:func:`legba.runtime.dapr_actors.evict_analyst_deps_for_descriptor` does exactly
this for analyst descriptors, driven off the ``descriptor.>`` NATS events the
registry publishes. The registry publishes the twin event class for stack
components (``stack.component.>``, see
:mod:`legba.data.registry.stack_events`); nothing consumed it.
:class:`legba.runtime.stack_informer.NatsStackComponentInformer` now does, and
calls :func:`evict_llm_handler` here.

This module deliberately does NOT merge the two caches into one dict. They are
built through different call paths with different lifetimes (the gate builds its
client against a throwaway Postgres store it closes immediately after), and
collapsing them would silently start sharing a handler instance between the
analyst plane and the gate — a behaviour change smuggled in under a cache fix.
Instead each cache REGISTERS itself and eviction sweeps every registered cache,
so a component id is dropped everywhere it is held.

What eviction does NOT do
-------------------------

It does not close the evicted handler's HTTP client. A handler reference can be
held by an in-flight ``chat_complete`` (the analyst plane is concurrent), and
``aclose()``-ing a client out from under a live request would convert an
operator config change into a request failure — strictly worse than the stale
config it fixes. The evicted handler is dropped from the cache, serves out
whatever calls already hold it, and is collected when the last reference goes.
Stack-component PUTs are rare operator actions, so the transient duplicate
connection pool is not a leak worth trading a live-call failure for. Same
posture as ``evict_analyst_deps_for_descriptor``, which also drops without
tearing down.

Failure posture: eviction is best-effort instrumentation of the control plane.
It never raises into the informer's message loop.
"""

from __future__ import annotations

import logging
from typing import Any, MutableMapping

logger = logging.getLogger(__name__)

#: Registered caches as ``(label, cache)``. Small and append-rare (two entries
#: in the live runtime), so a list scanned linearly is the right shape — a dict
#: keyed by label would silently drop a second cache sharing a label.
_CACHES: list[tuple[str, MutableMapping[str, Any]]] = []


def register_handler_cache(
    label: str, cache: MutableMapping[str, Any],
) -> None:
    """Register ``cache`` so :func:`evict_llm_handler` sweeps it.

    Idempotent by cache IDENTITY — registering the same object twice is a
    no-op, so a re-entrant bring-up cannot accumulate duplicate sweeps over one
    dict. ``label`` is diagnostic only (it names the cache in the eviction log
    line); two caches may legitimately share one.
    """
    for _, existing in _CACHES:
        if existing is cache:
            return
    _CACHES.append((label, cache))
    logger.debug("llm_handler_cache.registered label=%s", label)


def unregister_handler_cache(cache: MutableMapping[str, Any]) -> None:
    """Drop ``cache`` from the sweep set. Safe to call on an unregistered cache.

    The bring-up path owns a per-process cache dict and unregisters it on
    shutdown so a second bring-up in the same process (the test rig) never
    evicts through a dict belonging to a torn-down runtime.
    """
    for i, (_, existing) in enumerate(_CACHES):
        if existing is cache:
            del _CACHES[i]
            return


def registered_cache_labels() -> tuple[str, ...]:
    """Labels of every registered cache, in registration order (ops/tests)."""
    return tuple(label for label, _ in _CACHES)


def evict_llm_handler(component_id: str) -> int:
    """Drop every cached handler for ``component_id``. Returns entries evicted.

    The next :func:`~legba.runtime.dapr_host._llm_handler_factory` call (or
    bearing-gate build) rebuilds from the LIVE stack component, which is the
    whole point: the operator's PUT takes effect on the next call instead of
    the next container recreate.

    A component id nobody has built yet evicts 0 and logs nothing — the common
    case, since the runtime caches only the handful of components its analysts
    actually route to.
    """
    if not component_id:
        return 0
    evicted = 0
    for label, cache in _CACHES:
        if cache.pop(component_id, None) is not None:
            evicted += 1
            logger.info(
                "llm_handler_cache.evicted component_id=%s cache=%s — the next "
                "build re-reads the live stack component (config PUTs no longer "
                "need a container recreate)",
                component_id, label,
            )
    return evicted


def evict_all_llm_handlers() -> int:
    """Drop every cached handler in every registered cache. Returns the count.

    Not on any event path — the ops escape hatch (and the reset the test rig
    uses between bring-ups).
    """
    evicted = 0
    for label, cache in _CACHES:
        n = len(cache)
        if n:
            cache.clear()
            evicted += n
            logger.info(
                "llm_handler_cache.evicted_all cache=%s count=%d", label, n,
            )
    return evicted


def clear_handler_cache_registry() -> None:
    """Test hook — forget every registered cache (does NOT clear the caches)."""
    _CACHES.clear()


__all__ = [
    "clear_handler_cache_registry",
    "evict_all_llm_handlers",
    "evict_llm_handler",
    "register_handler_cache",
    "registered_cache_labels",
    "unregister_handler_cache",
]
