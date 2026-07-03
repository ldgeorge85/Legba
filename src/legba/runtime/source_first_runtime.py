# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source-first runtime planes — the P-CUT host bring-up wiring.

This module assembles the four source-first runtime planes the production
Dapr host boots ON TOP OF the substrate connections + reconcile loop that
:func:`legba.runtime.dapr_host.bring_up_production_runtime` already owns:

  * **Job plane** (P-07) — :class:`legba.runtime.jobs.JobQueue` +
    :class:`~legba.runtime.jobs.JobWorkerPool`. The work-queue stream + the
    shared durable consumer + the ``process_media`` handler. The pool runs as
    competing consumers; ``process_media`` lands derived signals which re-enter
    the fan-out → trigger path.
  * **Subscription / fan-out plane** (P-08) —
    :class:`legba.runtime.subscription.SubscriptionEngine`. Resolves each active
    target's ``source_refs`` → authorized bindings, enforces source policy, and
    binds ONE per-target aggregated JetStream consumer subject-filtered to its
    coarse axes. Source-published signals (``legba.signals.>``) fan out to the
    targets that subscribe to them.
  * **Trigger plane** (P-10) — :class:`legba.runtime.triggers.TriggerEngine`
    over a :class:`~legba.runtime.triggers.Coalescer`. Consumes the matched-
    signal stream, marks (analyst, target) pairs dirty, and fires on cadence /
    accumulation / severity (clamped by cooldown). A fire dispatches the
    analyst's run path via the injected runner.
  * **Agency plane** (P-11) — :class:`legba.data.analysts.agency.Agency`
    constructed over the live job queue + a NATS governor-event publisher.
    Exposed via :data:`AGENCY_HOLDER` so the analyst run path can resolve →
    govern → dispatch a pack tool (e.g. enqueue ``process_media`` mid-run).

It also installs the **SourceActor deps resolver** so a freshly-routed
``source::<id>::<ver>`` actor reconstructs its :class:`SourceDeps` from the
registry on activation — the source-actor analogue of the target/analyst
resolvers in :mod:`dapr_host`.

L-205 RETIREMENT: acquisition is OWNED BY :class:`SourceActor` here (poll
Reminder / push webhook → ``write_canonical_signal`` → publish to
``source.<id>.signals``). The legacy E2 target-owned pull path (TargetActor
pulling inline ``SourceBinding`` rows and calling ``write_target_signal``) is
NOT wired into the live acquisition chain. TargetActor stays registered only
as (a) the discovery-materialiser host and (b) the subscriber identity the
subscription engine fans out to — it no longer pulls sources itself.

The planes are assembled into a :class:`SourceFirstHandles` the host's lifespan
stops on shutdown (reverse order: trigger engine → worker pool → close the
job-plane resources; the substrate connections are owned + closed by the
caller).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# Module-level holder threading the live :class:`Agency` from bring-up down to
# the analyst run path (which lives in the actor / deps-builder, not here).
# Mirrors ``dapr_host.A2A_SKILL_REGISTRY_HOLDER``. The analyst run path reads
# ``AGENCY_HOLDER["agency"]`` / ``AGENCY_HOLDER["tool_context"]`` at call time;
# bring-up populates both before the reconcile loop activates any analyst.
AGENCY_HOLDER: dict[str, Any] = {}


@dataclass
class SourceFirstHandles:
    """Owned handles from :func:`bring_up_source_first_planes`.

    The job worker pool + trigger engine run as long-lived asyncio tasks; the
    subscription engine + agency are control-plane objects (no loop). ``stop``
    tears the loops down cleanly. The Postgres / NATS stores are NOT owned here
    — :func:`legba.runtime.dapr_host.bring_up_production_runtime` owns + closes
    them.
    """

    job_queue: Any
    worker_pool: Any
    subscription_engine: Any
    trigger_engine: Any
    agency: Any
    trigger_state: Any
    _trigger_task: asyncio.Task | None = None
    registered_targets: list[str] = field(default_factory=list)
    trigger_registrations: int = 0
    # Silent-stall liveness watchdog (W-1b §2) — observes signal + finding
    # traffic and alerts when the pipeline goes quiet. None when run_loops=False.
    liveness_watchdog: Any = None
    # S1 inbound-webhook drain (durable pull consumer over legba.inbound.>).
    # None when run_loops=False or the drain failed to start.
    inbound_drain: Any = None

    async def stop(self) -> None:
        # Liveness watchdog first — stop the check loop + drop its subscriptions
        # before the substrate connection is torn down.
        if self.liveness_watchdog is not None:
            try:
                await self.liveness_watchdog.stop()
            except Exception as exc:                            # pragma: no cover
                logger.warning("source_first.liveness_watchdog.stop err=%s", exc)
        # S1 inbound drain — stop the fetch loop + unsubscribe its pull consumer
        # BEFORE the NATS store is torn down (same as the trigger-engine unbind).
        if self.inbound_drain is not None:
            try:
                await self.inbound_drain.stop()
            except Exception as exc:                            # pragma: no cover
                logger.warning("source_first.inbound_drain.stop err=%s", exc)
        # Trigger engine first — stop consuming + ticking.
        if self.trigger_engine is not None:
            try:
                self.trigger_engine.stop()
            except Exception as exc:                            # pragma: no cover
                logger.warning("source_first.trigger_engine.stop err=%s", exc)
        if self._trigger_task is not None:
            self._trigger_task.cancel()
            try:
                await self._trigger_task
            except (asyncio.CancelledError, Exception):
                pass
        # Best-effort unbind the trigger engine's pull subscription so a later
        # NatsStore.close()/drain() doesn't block on a live consumer binding.
        psub = getattr(self.trigger_engine, "_psub", None)
        if psub is not None:
            try:
                await psub.unsubscribe()
            except Exception:                                   # pragma: no cover
                logger.debug("source_first.trigger_engine.unsubscribe failed")
            try:
                self.trigger_engine._psub = None
            except Exception:                                   # pragma: no cover
                pass
        # Worker pool next — drain in-flight + cancel.
        if self.worker_pool is not None:
            try:
                await self.worker_pool.stop()
            except Exception as exc:                            # pragma: no cover
                logger.warning("source_first.worker_pool.stop err=%s", exc)


# ---------------------------------------------------------------------------
# SourceActor deps resolver
# ---------------------------------------------------------------------------


def build_source_deps_resolver(
    *,
    registry_client: Any,
    standard_deps: Any,
    upstream_client: Any | None = None,
    enrichment_factory: Any | None = None,
):
    """Build the async fallback resolver for :class:`SourceActor` deps.

    Mirrors the target/analyst resolvers in :mod:`dapr_host`: parse the
    ``source::<id>::<ver>`` actor_id, fetch the typed source descriptor from
    the registry, parse it to a :class:`SourceDescriptor`, and assemble a
    :class:`legba.runtime.source_actor.SourceDeps`. Returns ``None`` on a miss
    so the SourceActor surfaces a clean ``no_deps`` rather than crashing daprd.
    """
    from ..data.schemas.source import SourceDescriptor
    from .source_actor import SourceDeps

    def _parse_actor_id(actor_id: str) -> tuple[str, str] | None:
        parts = actor_id.split("::", 2)
        if len(parts) < 2:
            return None
        return parts[0], parts[1]

    async def _resolver(actor_id: str) -> "SourceDeps | None":
        parsed = _parse_actor_id(actor_id)
        if parsed is None or parsed[0] != "source":
            return None
        descriptor_id = parsed[1]
        try:
            typed = await registry_client.get_descriptor_typed(
                descriptor_id, family="source",
            )
        except Exception as exc:                                # pragma: no cover
            logger.error(
                "source_deps_resolver.fetch_failed actor_id=%s err=%s",
                actor_id, exc,
            )
            return None
        if typed is None:
            return None
        try:
            sd = SourceDescriptor.model_validate(typed, strict=False)
        except Exception as exc:                                # pragma: no cover
            logger.error(
                "source_deps_resolver.parse_failed actor_id=%s err=%s",
                actor_id, exc,
            )
            return None

        # Baseline NLP enrichment chain (descriptor.pipeline.enrichment) —
        # built + StackRef-configured by the host (which owns the nlp/qdrant/
        # embedding clients). None when no factory is wired or the source
        # declares no enrichment stages.
        enrichment_stage = None
        if enrichment_factory is not None:
            try:
                enrichment_stage = enrichment_factory(sd)
                # The host's enrichment factory may be a coroutine function
                # so it can LAZILY (re-)resolve the hosted-NLP client on first
                # use — boot-before-seed / a transient models-host outage at
                # boot must not pin the filter set degraded for the process
                # lifetime. Await the awaitable form here, in the resolver's
                # async context.
                if inspect.isawaitable(enrichment_stage):
                    enrichment_stage = await enrichment_stage
            except Exception as exc:                            # pragma: no cover
                logger.warning(
                    "source_deps_resolver.enrichment_build_failed actor_id=%s err=%s",
                    actor_id, exc,
                )
                enrichment_stage = None

        return SourceDeps(
            descriptor=sd,
            deps=standard_deps,
            # poll handlers rebuild per pull from the factory; push handlers
            # are bound by the source-actor host at deps-build time (out of the
            # boot-gate scope — the resolver leaves handler=None so the core
            # builds a fresh poll handler via the source factory).
            handler=None,
            upstream_client=upstream_client,
            enrichment_stage=enrichment_stage,
        )

    return _resolver


# ---------------------------------------------------------------------------
# Trigger runner — bridges a fire to the analyst's actor run path.
# ---------------------------------------------------------------------------

# Actor-invoke (``ActorProxy.run``) round-trip budget for the trigger-dispatch
# + cadence fan-out hot paths. The dapr-python SDK defaults to
# ``DAPR_HTTP_TIMEOUT_SECONDS`` (60s) for an actor ``invoke_method``. That is
# too tight for the heaviest deterministic analysts on the busiest G20 targets:
# ``cross_source_dedup``'s per-target run sweeps a large + growing finding pool
# and, on us/ru/tr, the queued ``run`` turn routinely holds past 60s — so the
# round-trip threw ``asyncio.TimeoutError`` (caught upstream as
# ``trigger.run.failed``) on a ~8% tail of busy-target fires even though the
# analyst itself completed and wrote its output. The work is correct; only the
# client wait was under-budgeted. 180s comfortably clears the slowest observed
# busy-target turn while still bounding a genuinely wedged invoke (the actor's
# own cooldown + the trigger window CAS already dedup a near-simultaneous
# cadence run, so a late completion is not double-counted). Env-overridable for
# operators who need to tune it without a rebuild.
ACTOR_INVOKE_TIMEOUT_ENV = "LEGBA_ACTOR_INVOKE_TIMEOUT_SECONDS"
ACTOR_INVOKE_TIMEOUT_DEFAULT_S = 180


def actor_invoke_timeout_seconds() -> int:
    """Resolve the ActorProxy invoke round-trip budget (env-overridable).

    Falls back to :data:`ACTOR_INVOKE_TIMEOUT_DEFAULT_S` on an unset / malformed
    / non-positive value so a typo never silently disables the budget.
    """
    raw = os.getenv(ACTOR_INVOKE_TIMEOUT_ENV)
    if raw is None:
        return ACTOR_INVOKE_TIMEOUT_DEFAULT_S
    try:
        val = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "actor_invoke_timeout.bad_env value=%r — using default %ds",
            raw, ACTOR_INVOKE_TIMEOUT_DEFAULT_S,
        )
        return ACTOR_INVOKE_TIMEOUT_DEFAULT_S
    if val <= 0:
        logger.warning(
            "actor_invoke_timeout.non_positive value=%d — using default %ds",
            val, ACTOR_INVOKE_TIMEOUT_DEFAULT_S,
        )
        return ACTOR_INVOKE_TIMEOUT_DEFAULT_S
    return val


# ---------------------------------------------------------------------------
# S1 inbound stream sizing (env-overridable).
# ---------------------------------------------------------------------------

# Bounded buffer for the `legba_inbound` WORKQUEUE stream: the max number of
# un-drained envelopes it holds before a publish is refused (front -> 503) and a
# safety TTL after which an un-drained envelope ages out. Defaults are generous
# — the drain normally keeps the stream near-empty — but bound a runaway burst
# or a wedged drain so the stream can't grow without limit.
INBOUND_MAX_MSGS_ENV = "LEGBA_INBOUND_MAX_MSGS"
INBOUND_MAX_MSGS_DEFAULT = 50_000
INBOUND_MAX_AGE_ENV = "LEGBA_INBOUND_MAX_AGE_SECONDS"
INBOUND_MAX_AGE_DEFAULT_S = 3 * 24 * 3600  # 3 days


def _positive_int_env(name: str, default: int) -> int:
    """Resolve a positive-int env var, falling back on unset/malformed/≤0."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        logger.warning("%s.bad_env value=%r — using default %d", name, raw, default)
        return default
    if val <= 0:
        logger.warning("%s.non_positive value=%d — using default %d", name, val, default)
        return default
    return val


def inbound_max_msgs() -> int:
    """Buffer cap for the inbound stream (env :data:`INBOUND_MAX_MSGS_ENV`)."""
    return _positive_int_env(INBOUND_MAX_MSGS_ENV, INBOUND_MAX_MSGS_DEFAULT)


def inbound_max_age_seconds() -> int:
    """Safety TTL for the inbound stream (env :data:`INBOUND_MAX_AGE_ENV`)."""
    return _positive_int_env(INBOUND_MAX_AGE_ENV, INBOUND_MAX_AGE_DEFAULT_S)


def _actor_proxy_factory(timeout_seconds: int | None = None):
    """Build a dapr :class:`ActorProxyFactory` with our invoke timeout budget.

    The default ``ActorProxy.create`` path uses the SDK-global
    ``DAPR_HTTP_TIMEOUT_SECONDS`` (60s); passing an explicit factory lets the
    hot analyst-run paths use the larger :func:`actor_invoke_timeout_seconds`
    budget without mutating SDK globals (which would also widen unrelated
    invokes). Import is local to keep ``dapr`` off the module-import path.
    """
    from dapr.actor.client.proxy import ActorProxyFactory

    return ActorProxyFactory(
        http_timeout_seconds=timeout_seconds
        if timeout_seconds is not None
        else actor_invoke_timeout_seconds(),
    )


def build_trigger_work(action_executor_proxy_for: Any):
    """Build the :class:`DeterministicWork` callable a fire dispatches to.

    A fire routes the (analyst, target) pair to its :class:`AnalystActor` via an
    :class:`dapr.actor.ActorProxy` ``run`` call, target-scoped. The trigger
    plane owns the TRIGGERING; the analyst actor owns EXECUTION (it reads its
    matched slice, runs its method, writes its output). The deterministic runner
    refuses LLM-bearing fires (belt-and-braces) before this callable is reached.
    """
    from dapr.actor import ActorId, ActorProxy

    from ..runtime.dapr_actors import AnalystActorInterface

    # One factory (one DaprActorHttpClient) for every fire this runner
    # dispatches — carries the larger invoke budget so a busy-target analyst
    # run (e.g. cross_source_dedup on us/ru/tr) finishes within the round-trip
    # window instead of throwing asyncio.TimeoutError -> trigger.run.failed.
    proxy_factory = _actor_proxy_factory()

    async def _work(fire) -> dict[str, Any]:
        # A2 concurrency: route the fire to the PER-(analyst, target) WORKER
        # actor — id ``analyst::<analyst_id>::<target_id>`` — NOT the primary
        # ``analyst::<id>::<ver16>``. Each distinct id is a distinct Dapr
        # virtual actor with its own turn-queue, so per-target fires run
        # concurrently instead of serializing through the primary's queue.
        #
        # The worker reconstructs the analyst's deps with NO new registration:
        # the analyst deps fallback resolver keys off segment-1 of the
        # actor_id (``split("::", 2)[1]`` = the descriptor_id) and fetches the
        # head descriptor. The worker LAZY-ACTIVATES inside ``run`` (creates a
        # minimal ACTIVE record inline, registers no reminder).
        #
        # Meta analysts (no subscription.targets) never reach this path with a
        # per-target fire — their fires carry no target_id, so we fall back to
        # the primary actor (global run), unchanged.
        #
        # §2.1 lifecycle gate: NOOP the fire if the analyst is no longer live
        # (retired/paused). The per-target path below routes straight to a
        # version-less worker that lazy-activates + gates only on its OWN
        # record, so without this gate a retired/paused analyst keeps burning
        # reactive LLM budget. Gate BEFORE creating any actor proxy — no
        # activation, no spend. (See _analyst_is_live for the boot-window note.)
        if not _analyst_is_live(fire.analyst_id):
            reason = fire.reason.value if hasattr(fire.reason, "value") else str(fire.reason)
            logger.info(
                "trigger.dispatch.skipped_not_live analyst=%s target=%s reason=%s "
                "(retired/paused — no reactive spend)",
                fire.analyst_id, fire.target_id, reason,
            )
            return {"skipped": "analyst_not_live", "target_id": fire.target_id}
        if fire.target_id:
            actor_id = worker_actor_id(fire.analyst_id, fire.target_id)
        else:
            actor_id = _resolve_analyst_actor_id(fire.analyst_id)
        proxy = ActorProxy.create(
            "AnalystActor", ActorId(actor_id), AnalystActorInterface,
            actor_proxy_factory=proxy_factory,
        )
        result = await proxy.run({
            "trigger_kind": "coalesced_fire",
            "target_filter": fire.target_id,
            "reason": fire.reason.value if hasattr(fire.reason, "value") else str(fire.reason),
            "pending_count": fire.pending_count,
        })
        return {"actor_run": result, "target_id": fire.target_id}

    return _work


def worker_actor_id(analyst_id: str, target_id: str) -> str:
    """Per-(analyst, target) WORKER actor id — ``analyst::<id>::<target_id>``.

    A2 concurrency primitive. The worker shares the primary analyst's
    ``kind::id::*`` grammar (``_default_actor_id``), but carries the
    target_id in the third (version) slot instead of a content hash. Two
    consequences fall out of this for free:

      * The analyst deps fallback resolver (:func:`_analyst_deps_resolver`
        in ``dapr_host``) keys off ``split("::", 2)[1]`` — segment-1 is the
        descriptor_id either way — so the worker reconstructs the analyst's
        deps + head descriptor with NO new registration.
      * A distinct id per target ⇒ a distinct Dapr virtual actor ⇒ each
        per-target run gets its own turn-queue and runs concurrently.

    The worker lazy-activates in :meth:`AnalystActor.run` (minimal ACTIVE
    record, no reminder); the primary keeps the cadence reminder + fans out
    to the workers. Mirror constructor in
    :func:`AnalystActor._is_worker_id` / the cadence fan-out.
    """
    return f"analyst::{analyst_id}::{target_id}"


# Per-process cache of analyst_id → actor_id, populated by the reconcile
# action executor on CREATE_ACTOR so the trigger runner can address the hot
# actor without re-deriving the content hash.
_ANALYST_ACTOR_IDS: dict[str, str] = {}


def remember_analyst_actor_id(analyst_id: str, actor_id: str) -> None:
    """Record an analyst's live actor_id (called from the reconcile executor)."""
    _ANALYST_ACTOR_IDS[analyst_id] = actor_id


def forget_analyst_actor_id(analyst_id: str, actor_id: str) -> None:
    """Evict a retired analyst's actor_id from the dispatch cache.

    Guarded: only evicts when the cache still maps to THIS actor_id, so a
    version-drift sweep (retire old id after the new id was remembered)
    can never clobber the live mapping regardless of executor ordering.
    """
    if _ANALYST_ACTOR_IDS.get(analyst_id) == actor_id:
        del _ANALYST_ACTOR_IDS[analyst_id]


def _resolve_analyst_actor_id(analyst_id: str) -> str:
    cached = _ANALYST_ACTOR_IDS.get(analyst_id)
    if cached is not None:
        return cached
    # Fall back to a best-effort id (version-zeroed). Dapr routes by the full
    # actor_id string; if the reconcile loop activated the analyst with its real
    # content hash this fallback won't match, hence the cache above is primary.
    from .reconcile import _default_actor_id

    return _default_actor_id("analyst", analyst_id, "")


def _analyst_is_live(analyst_id: str) -> bool:
    """True iff the analyst is currently active per the runtime's live set.

    ``_ANALYST_ACTOR_IDS`` is the reconcile executor's view of lifecycle: it
    remembers an analyst on CREATE(active)/ENSURE_ACTIVE and forgets it on
    RETIRE/PAUSE. The trigger dispatch path uses this as the cheap in-memory
    lifecycle gate (§2.1) — a per-target fire routes straight to a version-less
    WORKER actor that lazy-activates and gates ONLY on its own record, never on
    head lifecycle, so without this a retired/paused analyst whose cadence
    reminder is already gone would STILL burn reactive LLM budget on every
    matching signal. Fail-closed: an unknown analyst NOOPs. The window already
    reset on the coalescer's CAS fire-claim before dispatch, so a NOOP'd fire
    during the brief boot settle (triggers wired before the first resync
    remembers analysts) is not lost — it re-fires on the next accumulation or
    the cadence ticker, once the analyst is remembered.
    """
    return analyst_id in _ANALYST_ACTOR_IDS


# ---------------------------------------------------------------------------
# Bring-up
# ---------------------------------------------------------------------------


async def bring_up_source_first_planes(
    *,
    pg_store: Any,
    nats_store: Any,
    standard_deps: Any,
    registry_client: Any,
    media_client: Any | None = None,
    worker_pool_size: int | None = None,
    run_loops: bool = True,
    enrichment_factory: Any | None = None,
    job_queue: Any | None = None,
    trigger_durable: str | None = None,
) -> SourceFirstHandles:
    """Assemble + start the four source-first planes. Returns the handles.

    Parameters
    ----------
    pg_store / nats_store:
        Connected substrate stores (owned by the caller; not closed here).
    standard_deps:
        The :class:`legba.runtime.deps.StandardDeps` bundle the source deps
        resolver threads into reconstructed :class:`SourceDeps`.
    registry_client:
        :class:`legba.runtime.registry_client.RegistryHTTPClient` for the source
        deps resolver + the target-subscription wiring.
    media_client:
        Optional hosted media client for the ``process_media`` job handler. When
        ``None`` the handler builds one from ``LEGBA_MEDIA_API_URL`` and
        REFUSES the job (typed terminal failure) if no endpoint is configured
        — there is no stub edge (A-2).
    worker_pool_size:
        Job worker count. Defaults to ``LEGBA_JOB_WORKERS`` (env) or 2.
    run_loops:
        When True (production) launch the worker-pool + trigger-engine loops as
        background tasks. A test harness can pass False to bring the planes up
        (topology + consumers + registrations) without long-lived loops, then
        drive ``drain_once`` / ``drain_until_empty`` manually.
    job_queue:
        Optional pre-built :class:`legba.runtime.jobs.JobQueue`. Production
        passes ``None`` (the default ``LEGBA_JOBS`` stream + shared durable is
        constructed here). The boot-wiring test rig injects a per-test queue
        (unique stream/durable/subject-prefix) so a suite run on a shared NATS
        never touches the live work-queue topology.
    trigger_durable:
        Optional durable-consumer name for the trigger engine. ``None``
        (production) keeps the engine's default ``legba-trigger-engine``;
        the test rig passes a unique name so ``ensure_consumer`` can never
        rewrite the live engine's filter set on a shared ``legba_signals``
        stream.
    """
    from .jobs import JobQueue, JobWorkerPool, default_dispatch
    from .source_actor import register_source_deps_resolver
    from .subscription import SubscriptionEngine
    from .triggers import (
        ActorTriggerRunner,
        Coalescer,
        TriggerEngine,
        TriggerRegistration,
        TriggerStateStore,
        policy_from_descriptor,
    )

    # ---- SourceActor deps resolver ------------------------------------
    register_source_deps_resolver(
        build_source_deps_resolver(
            registry_client=registry_client,
            standard_deps=standard_deps,
            enrichment_factory=enrichment_factory,
        )
    )
    logger.info("source_first.source_deps_resolver.registered")

    # ---- Job plane (P-07) ---------------------------------------------
    if job_queue is None:
        job_queue = JobQueue(nats_store)
    await job_queue.ensure_topology()
    # Idempotently ensure the job ledger schema.
    from ..data.jobs.store import JobStore

    async with pg_store.acquire() as conn:
        await JobStore.ensure_schema(conn)
    pool_size = (
        worker_pool_size
        if worker_pool_size is not None
        else int(os.getenv("LEGBA_JOB_WORKERS", "2"))
    )
    # The subscription engine is created BEFORE the worker pool so the
    # process_media handler can publish landed derived signals back into
    # fan-out (event_class="derived") — the A-2 media-loop close.
    subscription_engine = SubscriptionEngine(pg_store, nats=nats_store)
    await subscription_engine.ensure_signal_stream()
    worker_pool = JobWorkerPool(
        queue=job_queue,
        pg=pg_store,
        size=pool_size,
        dispatch=default_dispatch(),
        media=media_client,
        subscriptions=subscription_engine,
    )
    logger.info(
        "source_first.job_plane.ready stream=%s durable=%s workers=%d",
        job_queue.stream, job_queue.durable, pool_size,
    )

    # G2: bind JetStream streams for the agency's coarse subjects so escalate /
    # create_incident emits durably LAND (delivered=True → honest status
    # "emitted") and governor decision-audit telemetry is retained for an
    # operator dashboard — instead of vanishing on unbound subjects ("no response
    # from stream"). The DB ledgers (action_pack_invocations + governor_events)
    # remain the source of truth either way; these streams add durable fan-out.
    await nats_store.ensure_stream(
        "legba_channels", ["channels.>"], max_age_seconds=7 * 24 * 3600,
    )
    await nats_store.ensure_stream(
        "legba_governor_events", ["governor.events.>"], max_age_seconds=7 * 24 * 3600,
    )
    logger.info(
        "source_first.agency_streams.ready subjects=channels.>,governor.events.>"
    )

    # ---- S1 inbound accept-and-enqueue front (signals ingestion track) ----
    # NEW `legba_inbound` WORKQUEUE stream captures the RAW push envelopes the
    # webhook front publishes to `legba.inbound.<source_id>`; the single durable
    # InboundWebhookDrain consumes it OFF the request path (ingest ->
    # write_canonical_signal -> legba.signals.>). WORKQUEUE + `max_msgs` gives
    # bounded buffering + backpressure (a full buffer -> publish raises -> the
    # front returns 503, never a silent drop); `max_age` is a safety TTL.
    # ensure_stream is idempotent (stream_info-then-add) so this is safe every
    # boot. Bind the front's publish sink onto the process-wide webhook router
    # (closure over the JetStream publish) UNCONDITIONALLY — the front (mounted
    # on the L-113 server) must be able to enqueue even in a test rig where the
    # drain loop isn't launched.
    from ..data.sources.webhook_router import (
        INBOUND_STREAM_NAME,
        INBOUND_SUBJECT_ROOT,
        default_router as _default_webhook_router,
    )

    await nats_store.ensure_stream(
        INBOUND_STREAM_NAME,
        [f"{INBOUND_SUBJECT_ROOT}.>"],
        retention="workqueue",
        max_msgs=inbound_max_msgs(),
        max_age_seconds=inbound_max_age_seconds(),
        # discard=new → a full buffer REJECTS the new publish (publish_inbound
        # raises → front 503 honest backpressure) instead of silently evicting the
        # oldest already-202'd envelope (DiscardPolicy.OLD default). This is the
        # load-bearing honesty guarantee for the accept-and-enqueue front (S1).
        discard="new",
    )
    webhook_router = _default_webhook_router()
    webhook_router.bind_inbound_sink(nats_store.publish_json)
    logger.info(
        "source_first.inbound_stream.ready stream=%s subject=%s.> max_msgs=%d "
        "max_age_s=%d",
        INBOUND_STREAM_NAME, INBOUND_SUBJECT_ROOT,
        inbound_max_msgs(), inbound_max_age_seconds(),
    )

    # ---- Agency plane (P-11) ------------------------------------------
    from ..data.analysts.agency import Agency
    from ..data.analysts.agency.tools import (
        ChannelEmitter,
        ToolContext,
        default_tool_registry,
    )

    async def _governor_publish(subject: str, payload: bytes) -> None:
        # G2 — report delivery HONESTLY. ``publish_json`` is a JetStream
        # publish; a subject with no bound stream (channels.escalations has
        # none) makes it RAISE rather than silently succeed. Both callers of
        # this hook already wrap it in their own try/except:
        #   * ChannelEmitter.emit (agency/tools.py) sets delivered=False on a
        #     raise — swallowing the error here would let it report
        #     delivered=True for an escalation that vanished;
        #   * record_governor_event (agency/events.py) logs-and-continues —
        #     the NATS publish is best-effort telemetry behind a durable DB row.
        # So we let the exception propagate: neither path is destabilised, and
        # the emitter stops overstating delivery it cannot confirm.
        await nats_store.publish_json(subject, payload)

    agency = Agency(
        tool_registry=default_tool_registry(),
        nats_publish=_governor_publish,
    )
    # The ToolContext carries the live job queue (process_media enqueues a
    # real job) AND the channel emitter (A-3b / review G2: this was None,
    # which made escalate/create_incident return `failed` unconditionally —
    # the headline agency feature could never produce a side effect). The
    # emitter publishes alert/nats_stream channels onto the shared NATS;
    # richer sinks (webhook POST, a2a invoke) are emitter subclasses.
    tool_context = ToolContext(
        queue=job_queue,
        # pg_pool makes the emit DURABLY auditable (migration 0061): every
        # escalate/incident emit writes one alert_sink_deliveries row recording
        # what was delivered + whether the publish confirmed — the durable answer
        # to "who got alerted", beyond the process-local emitted[] list and the
        # NATS edge (which retains nothing queryable per finding).
        emit=ChannelEmitter(
            nats_publish=_governor_publish, pg_pool=pg_store.pool,
        ),
    )
    AGENCY_HOLDER["agency"] = agency
    AGENCY_HOLDER["tool_context"] = tool_context
    logger.info("source_first.agency.ready tools=%s", agency_tool_names(agency))

    # ---- Subscription / fan-out plane (P-08) --------------------------
    # (engine already constructed above, before the worker pool)
    logger.info("source_first.subscription_engine.ready")

    # Wire every active target's subscriptions + build the trigger registry.
    trigger_state = TriggerStateStore(pg_store.pool)
    await trigger_state.ensure_schema()

    # Actor-dispatch runner: routes a coalesced fire to the analyst's actor run
    # for ANY method kind (deterministic + LLM). The per-signal-LLM guard lives
    # upstream in the trigger policy (floored accumulation), so by the time a
    # fire lands here it is already a coalesced batch — safe to run an LLM
    # assessor on. This is what completes the reactive path: an LLM analyst now
    # fires on accumulation/cadence, not only on its Dapr cadence reminder.
    runner = ActorTriggerRunner(build_trigger_work(None))
    # Per-pair policy resolver — built from the analyst descriptors discovered
    # during target wiring below. Defaults to a deterministic policy for an
    # unknown pair (the coalescer never fires an unknown LLM pair because the
    # policy floors accumulation when is_llm).
    _policies: dict[tuple[str, str], Any] = {}

    def _policy_for(analyst_id: str, target_id: str):
        existing = _policies.get((analyst_id, target_id))
        if existing is not None:
            return existing
        # Conservative default: deterministic, accumulate-then-fire on cadence.
        from .triggers.policy import TriggerPolicy

        return TriggerPolicy(
            cadence_seconds=300.0,
            accumulation_threshold=1,
            severity_gate=None,
            cooldown_seconds=0,
            is_llm=False,
        )

    coalescer = Coalescer(
        state=trigger_state, runner=runner, policy_for=_policy_for,
    )
    trigger_engine = TriggerEngine(
        nats=nats_store, coalescer=coalescer, durable=trigger_durable,
    )

    registered_targets, trigger_regs = await _wire_targets_and_triggers(
        subscription_engine=subscription_engine,
        trigger_engine=trigger_engine,
        registry_client=registry_client,
        policies=_policies,
    )

    # Ensure the trigger engine's durable consumer over the union of subject
    # filters of all registered (analyst, target) pairs.
    try:
        await trigger_engine.ensure_consumer()
        logger.info(
            "source_first.trigger_engine.consumer_ready registrations=%d",
            trigger_regs,
        )
    except Exception as exc:                                    # pragma: no cover
        logger.warning(
            "source_first.trigger_engine.ensure_consumer err=%s "
            "(no registrations yet is fine on a cold rig)", exc,
        )

    # ---- Launch the loops ---------------------------------------------
    trigger_task: asyncio.Task | None = None
    liveness_watchdog: Any = None
    inbound_drain: Any = None
    if run_loops:
        await worker_pool.start()
        logger.info("source_first.worker_pool.started workers=%d", pool_size)
        trigger_task = asyncio.create_task(
            trigger_engine.run(), name="legba-trigger-engine",
        )
        logger.info("source_first.trigger_engine.started")

        # S1 inbound drain — durable pull consumer draining `legba.inbound.>`
        # OFF the request path. Best-effort start: a drain failure must not
        # abort the whole runtime bring-up (the front still enqueues durably;
        # the drain catches up once healthy). The stream + sink were provisioned
        # above, unconditionally.
        from .inbound_drain import InboundWebhookDrain

        try:
            inbound_drain = InboundWebhookDrain(nats_store, webhook_router)
            await inbound_drain.start()
            logger.info(
                "source_first.inbound_drain.started consumer=%s",
                inbound_drain.consumer_name,
            )
        except Exception as exc:                                # pragma: no cover
            logger.warning(
                "source_first.inbound_drain.start err=%s "
                "(inbound webhooks will BUFFER on the stream until the drain "
                "recovers — not dropped)", exc,
            )
            inbound_drain = None

        # Silent-stall liveness watchdog (W-1b §2): observe signal + finding
        # traffic and alert if the pipeline goes quiet. Best-effort — a
        # watchdog start failure must not abort the runtime bring-up.
        from .liveness_watchdog import LivenessWatchdog

        try:
            # pg_store enables OBS — the per-analyst cadence-liveness check
            # (alerts when ONE analyst goes dark while the aggregate pipeline
            # still flows; the global stall check above can't see that).
            liveness_watchdog = LivenessWatchdog(nats_store, pg_store=pg_store)
            await liveness_watchdog.start()
        except Exception as exc:                                # pragma: no cover
            logger.warning(
                "source_first.liveness_watchdog.start err=%s "
                "(pipeline-stall alerting will NOT run this boot)", exc,
            )
            liveness_watchdog = None

    return SourceFirstHandles(
        job_queue=job_queue,
        worker_pool=worker_pool,
        subscription_engine=subscription_engine,
        trigger_engine=trigger_engine,
        agency=agency,
        trigger_state=trigger_state,
        _trigger_task=trigger_task,
        registered_targets=registered_targets,
        trigger_registrations=trigger_regs,
        liveness_watchdog=liveness_watchdog,
        inbound_drain=inbound_drain,
    )


def agency_tool_names(agency: Any) -> list[str]:
    """Best-effort list of the agency's registered tool names (for the log)."""
    reg = getattr(agency, "_tools", None)
    if reg is None:
        return []
    names = getattr(reg, "names", None)
    if callable(names):
        try:
            return list(names())
        except Exception:                                       # pragma: no cover
            return []
    handlers = getattr(reg, "_handlers", None)
    if isinstance(handlers, dict):
        return sorted(handlers)
    return []


async def _wire_targets_and_triggers(
    *,
    subscription_engine: Any,
    trigger_engine: Any,
    registry_client: Any,
    policies: dict[tuple[str, str], Any],
) -> tuple[list[str], int]:
    """Resolve every active target's subscriptions + register its analysts'
    coalescing triggers.

    Best-effort + fail-soft per target: a single bad target (vanished source,
    refused policy) is logged and skipped — it never sinks the whole bring-up.
    Returns ``(registered_target_ids, trigger_registration_count)``.
    """
    import httpx

    from ..data.schemas.target import TargetDescriptor
    from .subscription import TargetSubscription
    from .triggers import TriggerRegistration, policy_from_descriptor

    base_url = os.environ.get(
        "LEGBA_REGISTRY_API_URL", "http://localhost:8090",
    ).rstrip("/")
    token = os.environ.get("LEGBA_REGISTRY_API_TOKEN") or "dev"

    # List ACTIVE targets + analysts via the registry. Lifecycle policy:
    #   active                = full participation (boot-wired here + the
    #                           cross-target `state='active'` enumeration).
    #   draft / configured    = reactive-only-until-promoted: per-target workers
    #                           still fan out (discovery materialises drafts and
    #                           promotes on first run), but no boot wiring.
    #   retired               = excluded everywhere.
    # So a target/analyst MUST reach `active` to be boot-wired — the bring-up
    # registrar advances declared-`active` descriptors there on first register.
    async def _list_family(family: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{base_url}/api/v1/registry/descriptors",
                params={"family": family, "head_only": "true", "limit": 500},
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            return [row for row in r.json() if row.get("state") == "active"]

    registered: list[str] = []
    reg_count = 0

    try:
        target_rows = await _list_family("target")
    except Exception as exc:
        logger.warning(
            "source_first.wire.list_targets_failed err=%s "
            "(no fan-out wiring this boot; informer/resync will retry)", exc,
        )
        return registered, reg_count

    # Build analyst index (id → typed descriptor dict) once.
    analysts_by_id: dict[str, dict[str, Any]] = {}
    try:
        for row in await _list_family("analyst"):
            analysts_by_id[row["descriptor_id"]] = row
    except Exception as exc:                                    # pragma: no cover
        logger.warning("source_first.wire.list_analysts_failed err=%s", exc)

    for row in target_rows:
        target_id = row["descriptor_id"]
        try:
            typed = await registry_client.get_descriptor_typed(
                target_id, family="target",
            )
            if typed is None:
                continue
            td = TargetDescriptor.model_validate(typed, strict=False)
        except Exception as exc:
            # An OLD target-owned (E2) descriptor that doesn't satisfy the new
            # source-first TargetDescriptor schema is EXPECTED to be rejected
            # here (L-205) — log a one-line summary, not the full pydantic
            # traceback, so the operator log stays readable.
            summary = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            logger.warning(
                "source_first.wire.target_skipped target=%s reason=%s "
                "(legacy/incompatible descriptor — not wired into fan-out)",
                target_id, summary,
            )
            continue

        # Discovery targets don't subscribe to sources — they materialise them.
        if td.discovery is not None:
            continue
        if not td.sources:
            continue

        # Pivot §9: TargetScope is polymorphic (GeoScope/EstateScope/
        # EntityScope) and no longer carries owner_tenant on the scope itself.
        # Default to the substrate default tenant (matches Signal.owner_tenant
        # default + the subscription policy's source-tenant default).
        tenant = getattr(td.scope, "owner_tenant", None) or "default"
        try:
            sub: TargetSubscription = await subscription_engine.register_target(
                target_id=td.identity.id,
                target_tenant=tenant,
                source_refs=list(td.sources),
                strict_policy=False,   # one refused source must not sink the target
            )
        except Exception as exc:
            logger.warning(
                "source_first.wire.register_target_failed target=%s err=%s",
                target_id, exc,
            )
            continue
        registered.append(td.identity.id)
        logger.info(
            "source_first.wire.target target=%s bindings=%d refused=%d filters=%d",
            td.identity.id, len(sub.bindings), len(sub.refused),
            len(sub.subject_filters),
        )

        # Register one coalescing trigger per analyst that runs over this target.
        analyst_ids = _analyst_ids_for_target(td, analysts_by_id)
        for analyst_id in analyst_ids:
            arow = analysts_by_id.get(analyst_id)
            if arow is None:
                continue
            try:
                atyped = await registry_client.get_descriptor_typed(
                    analyst_id, family="analyst",
                )
                from ..data.schemas.analyst import AnalystDescriptor

                ad = AnalystDescriptor.model_validate(atyped, strict=False)
            except Exception as exc:                            # pragma: no cover
                logger.warning(
                    "source_first.wire.analyst_parse_failed analyst=%s err=%s",
                    analyst_id, exc,
                )
                continue
            policy = policy_from_descriptor(
                cadence=ad.cadence,
                method_kind=str(ad.method.kind),
            )
            policies[(analyst_id, td.identity.id)] = policy
            trigger_engine.register(
                TriggerRegistration(
                    analyst_id=analyst_id,
                    target_id=td.identity.id,
                    tenant=tenant,
                    bindings=list(sub.bindings),
                    subject_filters=list(sub.subject_filters),
                )
            )
            reg_count += 1

    return registered, reg_count


def _analyst_ids_for_target(
    td: Any, analysts_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    """Resolve which standalone analysts run over a target → per-(analyst,
    target) coalescing triggers. Two binding paths:

    1. **analyst_ref** (target → analyst): the target's inline analyst block
       names a standalone analyst via ``analyst_ref``.
    2. **subscription.targets selector** (analyst → targets; PIVOT match-many,
       mirrors the SourceRef-selector model): a standalone analyst declares a
       ``subscription.targets`` block whose optional Starlark predicate
       (ANALYST_SUBSCRIPTION surface — e.g. ``has_tag("g20")``) is evaluated
       against the target's scope. A null predicate = match every target. This
       is how ``country_assessor`` (has_tag("g20")) coalesces over all 19 G20
       country targets without enumerating them.

    Best-effort, fail-soft per analyst: a bad/uncompilable selector is logged
    and skipped, never sinking the whole wiring pass.
    """
    from ..data.predicates import PredicateSurface, compile_predicate

    ids: list[str] = []
    block = getattr(td, "analyst", None)
    if block is not None:
        ref = getattr(block, "analyst_ref", None)
        if ref:
            ids.append(str(ref))

    scope = getattr(td, "scope", None)
    ident = getattr(td, "identity", None)
    target_ctx = {
        "target": {
            "id": getattr(ident, "id", "") or "",
            "kind": str(getattr(ident, "kind", "") or ""),
            "scope_geo": list(getattr(scope, "geo", []) or []),
            "scope_entity_classes": list(getattr(scope, "entity_classes", []) or []),
            "tags": list(getattr(scope, "tags", []) or []),
            "abstraction_level": str(getattr(ident, "abstraction_level", "") or ""),
        }
    }

    for analyst_id, arow in analysts_by_id.items():
        if analyst_id in ids:
            continue
        sub_targets = ((arow.get("body") or {}).get("subscription") or {}).get("targets")
        if not sub_targets:
            continue  # analyst doesn't bind to targets by selector
        pred = sub_targets.get("predicate")
        if not pred:
            ids.append(analyst_id)  # selector present, no predicate → all targets
            continue
        try:
            compiled = compile_predicate(pred, PredicateSurface.ANALYST_SUBSCRIPTION)
            if compiled.evaluate(target_ctx):
                ids.append(analyst_id)
        except Exception as exc:
            logger.warning(
                "source_first.wire.analyst_selector_eval_failed "
                "analyst=%s target=%s err=%s",
                analyst_id, getattr(ident, "id", "?"), exc,
            )
    return ids


__all__ = [
    "ACTOR_INVOKE_TIMEOUT_DEFAULT_S",
    "ACTOR_INVOKE_TIMEOUT_ENV",
    "AGENCY_HOLDER",
    "SourceFirstHandles",
    "actor_invoke_timeout_seconds",
    "bring_up_source_first_planes",
    "build_source_deps_resolver",
    "build_trigger_work",
    "forget_analyst_actor_id",
    "remember_analyst_actor_id",
    "worker_actor_id",
]
