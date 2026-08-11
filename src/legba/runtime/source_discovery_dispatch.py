"""The SOURCE-flavor discovery-cycle dispatch (P-13), extracted from
``source_actor`` at the regrowth-gate seam.

This is the source-flavor twin of ``dapr_actors.TargetActor._run_discovery_cycle``.
Until it existed the source flavor had NO actor-plane entry at all:
``discovery.materializer.run_source_discovery_cycle`` — the function that
resolves the descriptor's declared deps, builds the handler, drains
``discover(ctx)`` and reconciles into ``source_descriptors`` — had no caller
anywhere outside its own tests, so a source-discovery descriptor could not fire
even at ``state: active``. The kind, the materialiser and the
validate-before-register path were all built and tested; only the binding was
missing.

A discovery template does NOT pull (the schema's cadence validator exempts it,
and its materialised CHILDREN are the things that poll), so this dispatch
replaces the acquisition path rather than joining it.

Errors propagate to the caller (``SourceActor.run``), which records a
``hard_fail`` on the actor record with the message — a discovery template that
stopped materialising must be visible in ``get_state``, not just in the log.
(``pull_once`` differs: it absorbs its own failures and RETURNS an outcome, so
``run`` never needed to catch.)
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .source_actor import SourceCore

logger = logging.getLogger(__name__)


async def run_discovery_cycle(core: "SourceCore") -> dict[str, Any]:
    """Run one SOURCE-discovery cycle for ``core``'s descriptor."""
    block = core.descriptor.discovery
    if block is None:  # pragma: no cover
        raise ValueError(
            f"run_discovery_cycle called on source "
            f"{core.descriptor.identity.id!r} with no discovery block"
        )
    from ..data.discovery.materializer import run_source_discovery_cycle

    async with core.deps.pg_pool.acquire() as conn:
        result = await run_source_discovery_cycle(
            conn,
            core.descriptor,
            core.deps,
            # `probe_handler`/`source_registry` stay unset on purpose:
            # validate-before-register then builds the real handler via
            # `runtime.source_factory.build_source_handler` (a genuine
            # liveness + trial-pull probe). Injecting one is the TEST seam.
            dlq=getattr(core.deps, "descriptor_dlq", None),
            nats_publish=getattr(core.deps, "nats_publish", None),
        )

    logger.info(
        "source_actor.discovery.cycle actor_id=%s discovery=%s kind=%s "
        "candidates=%d registered=%d rejected=%d dropped=%d dlq=%d",
        core.actor_id, core.descriptor.identity.id, block.kind,
        result.candidates_in, result.registered_count,
        result.rejected_count, result.dropped_count, result.dlq_count,
    )
    return {
        "outcome": "success" if result.registered_count else "noop",
        "discovery_cycle": True,
        "discovery_kind": block.kind,
        "candidates_in": result.candidates_in,
        "registered": result.registered_count,
        "rejected": result.rejected_count,
        "dropped": result.dropped_count,
        "dlq": result.dlq_count,
    }
