# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reconcile control loop (per legba_runtime_spec.md §3).

The reconciler is the bridge between the descriptor registry (desired
state) and the actor runtime (observed state). It is a SINGLETON loop —
it must run on exactly one replica. Single-node: it just runs. Multi-node:
:class:`legba.runtime.leader.LeaderLease` (Postgres advisory lock) gates the
loop + the NATS informer to the elected leader; standby replicas run the hot
actor/fan-out path but NOT this loop. ``start()`` is re-startable so a promoted
standby can take it over after the prior leader dies.

Pattern: informer (subscribed to NATS descriptor events + periodic resync)
→ work queue → per-kind reconciler → action executor.

The per-kind reconcilers are **pure** — they take (observed, desired) and
return a :class:`ReconcileAction`. The executor is the only thing that
mutates actor state, mounts cron triggers, etc. Makes reconcilers
trivially testable.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol

from .lifecycle import (
    ACTIVE,
    CONFIGURED,
    DRAFT,
    ERROR,
    PAUSED,
    RETIRED,
    LifecycleEvent,
    LifecycleFSM,
    Transition,
)
from .state import ActorStateRecord, ActorStateStore

logger = logging.getLogger(__name__)


class ActionKind(str, Enum):
    NOOP = "noop"
    CREATE_ACTOR = "create_actor"
    ENSURE_ACTIVE = "ensure_active"
    TRANSITION_LIFECYCLE = "transition_lifecycle"
    UPDATE_MAILBOX = "update_mailbox"
    RESTART_ACTOR = "restart_actor"
    RETIRE_ACTOR = "retire_actor"
    BACKOFF = "backoff"
    ERROR = "error"


@dataclass
class ReconcileAction:
    """One reconcile output. The executor consumes these to mutate."""

    kind: ActionKind
    actor_id: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ObservedState:
    """The runtime's view of one actor — populated by the informer."""

    actor_id: str
    state_record: ActorStateRecord | None
    cron_mounted: bool = False


@dataclass
class DesiredState:
    """The registry's view of one descriptor — populated from the API."""

    descriptor_id: str
    descriptor_kind: str       # "target" | "analyst"
    descriptor_version: str    # content hash
    lifecycle_target: str      # what the registry says the lifecycle should be
    body: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Per-kind reconcilers (pure)
# ---------------------------------------------------------------------------


class Reconciler(Protocol):
    """Pure reconciler. Returns a ReconcileAction; never mutates."""

    async def reconcile(
        self,
        observed: ObservedState,
        desired: DesiredState,
    ) -> ReconcileAction: ...


class TargetReconciler:
    """Default reconciler for target descriptors.

    Logic:

      * No actor row + descriptor is non-retired → ``CREATE_ACTOR``.
      * Actor exists, lifecycle differs from descriptor target →
        ``TRANSITION_LIFECYCLE``.
      * Actor exists, descriptor_version differs from observed → identity
        drift → ``RESTART_ACTOR`` (new content hash; old actor will be
        drained on a later iteration).
      * Otherwise → ``NOOP``.
    """

    async def reconcile(
        self,
        observed: ObservedState,
        desired: DesiredState,
    ) -> ReconcileAction:
        return _common_reconcile(observed, desired)


class AnalystReconciler:
    """Default reconciler for analyst descriptors. Same shape as targets."""

    async def reconcile(
        self,
        observed: ObservedState,
        desired: DesiredState,
    ) -> ReconcileAction:
        return _common_reconcile(observed, desired)


def _common_reconcile(
    observed: ObservedState,
    desired: DesiredState,
) -> ReconcileAction:
    rec = observed.state_record

    # Every actionable detail carries the descriptor coordinates so the
    # executor can write the observed-state row after the proxy call
    # succeeds (G1 fix: lifecycle actions must land in `actor_state`,
    # otherwise reconcile is forever blind and re-CREATEs everything).
    base_detail: dict[str, Any] = {
        "descriptor_id": desired.descriptor_id,
        "descriptor_kind": desired.descriptor_kind,
        "descriptor_version": desired.descriptor_version,
    }

    # Retired descriptor + observed actor → retire.
    if desired.lifecycle_target == RETIRED:
        if rec is None or rec.lifecycle == RETIRED:
            return ReconcileAction(
                kind=ActionKind.NOOP,
                actor_id=observed.actor_id,
                detail={"reason": "already_retired_or_absent"},
            )
        return ReconcileAction(
            kind=ActionKind.RETIRE_ACTOR,
            actor_id=observed.actor_id,
            detail={**base_detail, "from_state": rec.lifecycle},
        )

    # No actor row → create one in the descriptor's target lifecycle.
    if rec is None:
        return ReconcileAction(
            kind=ActionKind.CREATE_ACTOR,
            actor_id=observed.actor_id,
            detail={
                **base_detail,
                "target_lifecycle": desired.lifecycle_target,
            },
        )

    # Identity drift — descriptor's content hash changed under us.
    if rec.descriptor_version != desired.descriptor_version:
        return ReconcileAction(
            kind=ActionKind.RESTART_ACTOR,
            actor_id=observed.actor_id,
            detail={
                **base_detail,
                "old_version": rec.descriptor_version,
                "new_version": desired.descriptor_version,
            },
        )

    # Lifecycle drift.
    if rec.lifecycle != desired.lifecycle_target:
        return ReconcileAction(
            kind=ActionKind.TRANSITION_LIFECYCLE,
            actor_id=observed.actor_id,
            detail={
                **base_detail,
                "from": rec.lifecycle,
                "to": desired.lifecycle_target,
            },
        )

    # Active analyst/source, version + lifecycle in sync. Re-assert the
    # durable reminder on each periodic resync so a silently-dropped Dapr
    # reminder self-heals within resync_interval — rather than stalling
    # indefinitely once the actor idles out (actorIdleTimeout 30m) and
    # nothing re-activates it (the reminder that would is the thing that
    # went silent). This is the durability backstop for the 2026-06-05
    # 06:00→16:13 cadence stall. `activate()` is idempotent on both actor
    # kinds (deps cached, reminder re-anchored to the next cron boundary).
    #
    # Sources are included deliberately: before the G1 fix the observed row
    # was never written, so every resync fell into CREATE_ACTOR and healed
    # source poll reminders *by accident*. Once observed state exists that
    # path goes NOOP — ENSURE_ACTIVE keeps the heal on purpose. Targets are
    # passive subscribers (no reminder) → NOOP.
    if (
        desired.descriptor_kind in ("analyst", "source")
        and desired.lifecycle_target == ACTIVE
    ):
        return ReconcileAction(
            kind=ActionKind.ENSURE_ACTIVE,
            actor_id=observed.actor_id,
            detail=dict(base_detail),
        )

    return ReconcileAction(
        kind=ActionKind.NOOP,
        actor_id=observed.actor_id,
    )


# ---------------------------------------------------------------------------
# Reconcile loop
# ---------------------------------------------------------------------------


# Resolver: descriptor_id -> DesiredState | None.
DesiredResolver = Callable[[str], Awaitable[DesiredState | None]]
# All-descriptors lister (for periodic resync).
DesiredLister = Callable[[], Awaitable[list[DesiredState]]]
# Executor: takes a ReconcileAction and applies it.
ActionExecutor = Callable[[ReconcileAction], Awaitable[None]]


class ReconcileLoop:
    """Single-process reconcile loop.

    Drives convergence between observed and desired by running per-kind
    reconcilers off the work queue. Periodic resync every
    ``resync_interval`` walks the full registry; events from the informer
    enqueue per-descriptor reconciles.
    """

    def __init__(
        self,
        *,
        state_store: ActorStateStore,
        desired_resolver: DesiredResolver,
        desired_lister: DesiredLister,
        action_executor: ActionExecutor,
        reconcilers: dict[str, Reconciler] | None = None,
        resync_interval: timedelta = timedelta(minutes=5),
        # ENSURE_ACTIVE pokes the head actor (idempotent activate) via a
        # turn-based Dapr proxy, so it QUEUES behind whatever turn the actor is
        # mid-running. The heaviest analysts (e.g. cross_source_dedup, whose
        # cadence run sweeps a large + growing finding pool) can hold their turn
        # past a 30s bound, so the periodic-resync poke timed out every cycle —
        # a benign skip+retry, but a recurring WARNING. 90s comfortably clears
        # the busy turn while still bounding a genuinely wedged reconcile.
        run_once_timeout: timedelta = timedelta(seconds=90),
        actor_id_fn: Callable[[str, str, str], str] | None = None,
        # Optional orphan-reminder GC, invoked once per periodic resync after
        # the registry walk. Signature ``() -> Awaitable[Any]``. Wired by
        # bring_up_production_runtime to call reminder_gc.sweep_orphan_reminders;
        # left None in tests / embedded host (no daprd scheduler to GC).
        reminder_gc: Callable[[], Awaitable[Any]] | None = None,
        # Task #236 RIDER: optional stale-pack-deps WARNING sweep, invoked on
        # the SAME periodic cadence as reminder_gc (best-effort, never blocks
        # reconcile). ``action_pack`` descriptors have no actor lifecycle of
        # their own (they're outside ``_FAMILIES`` in dapr_host's
        # desired_resolver), so a pack PUT never reaches an analyst's cached
        # deps eviction — this diagnoses that drift instead of silently
        # serving a stale tool grant. Signature ``() -> Awaitable[Any]``.
        # Wired by bring_up_production_runtime to
        # dapr_actors.warn_stale_pack_deps; left None in tests / embedded host.
        pack_staleness_check: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self._state = state_store
        self._resolve = desired_resolver
        self._list = desired_lister
        self._execute = action_executor
        self._reminder_gc = reminder_gc
        self._pack_staleness_check = pack_staleness_check
        self._reconcilers = reconcilers or {
            "target": TargetReconciler(),
            "analyst": AnalystReconciler(),
        }
        self._resync_interval = resync_interval
        self._run_once_timeout = run_once_timeout
        self._actor_id_fn = actor_id_fn or _default_actor_id
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._stopped = False
        self._tasks: list[asyncio.Task] = []
        self._last_run_at: dict[str, datetime] = {}

    def enqueue(self, descriptor_id: str, reason: str = "event") -> None:
        """Schedule a reconcile for `descriptor_id`. Idempotent via the queue."""
        self._queue.put_nowait((descriptor_id, reason))

    async def start(self) -> None:
        # Re-startable: a leader-election demotion stops the loop (sets
        # _stopped), and a later re-promotion calls start() again. Clear the
        # flag so the re-launched tasks actually run instead of exiting on the
        # first iteration. A fresh loop already has _stopped=False, so this is a
        # no-op on the cold-boot path.
        self._stopped = False
        self._tasks.append(asyncio.create_task(self._main_loop(), name="legba-reconcile"))
        self._tasks.append(asyncio.create_task(self._resync_loop(), name="legba-resync"))

    async def stop(self) -> None:
        self._stopped = True
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def run_once(self, descriptor_id: str) -> ReconcileAction:
        """Run one reconcile pass synchronously — drives a single descriptor.

        Returns the action that was executed (or NOOP).
        """
        desired = await self._resolve(descriptor_id)
        if desired is None:
            return ReconcileAction(
                kind=ActionKind.NOOP,
                actor_id=descriptor_id,
                detail={"reason": "desired_state_not_found"},
            )
        actor_id = self._actor_id_fn(
            desired.descriptor_kind,
            desired.descriptor_id,
            desired.descriptor_version,
        )

        # Version-drift sweep (G1): the actor_id embeds content_hash[:16],
        # so a descriptor edit mints a NEW id — nothing else ever retires
        # the old one, leaving it double-running (duplicate polls/cadence).
        # Retire every live sibling of this descriptor before reconciling
        # the head id. Best-effort: a failed sibling retire is logged and
        # must not block the head action (the reminder guard in the actors
        # is the belt-and-braces for missed sweeps).
        try:
            siblings = await self._state.list_live_siblings(
                actor_kind=desired.descriptor_kind,
                descriptor_id=desired.descriptor_id,
                exclude_actor_id=actor_id,
            )
        except Exception as exc:  # pragma: no cover — store outage
            logger.warning(
                "reconcile.sibling_sweep.list_failed descriptor_id=%s err=%s",
                desired.descriptor_id, exc,
            )
            siblings = []
        for sib in siblings:
            retire = ReconcileAction(
                kind=ActionKind.RETIRE_ACTOR,
                actor_id=sib.actor_id,
                detail={
                    "descriptor_id": desired.descriptor_id,
                    "descriptor_kind": desired.descriptor_kind,
                    "descriptor_version": sib.descriptor_version,
                    "from_state": sib.lifecycle,
                    "reason": "version_drift",
                    "head_version": desired.descriptor_version,
                },
            )
            logger.info(
                "reconcile.version_drift.retire actor_id=%s head=%s",
                sib.actor_id, desired.descriptor_version[:16],
            )
            try:
                await self._execute(retire)
            except Exception as exc:  # pragma: no cover — executor failure
                logger.warning(
                    "reconcile.version_drift.retire_failed actor_id=%s err=%s",
                    sib.actor_id, exc,
                )

        record = await self._state.get(actor_id)
        observed = ObservedState(actor_id=actor_id, state_record=record)
        reconciler = self._reconcilers.get(desired.descriptor_kind)
        if reconciler is None:
            return ReconcileAction(
                kind=ActionKind.ERROR,
                actor_id=actor_id,
                detail={
                    "error": f"no reconciler for kind {desired.descriptor_kind!r}",
                },
            )
        action = await reconciler.reconcile(observed, desired)
        # Visibility for state-changing reconcile actions. Steady-state
        # NOOP / ENSURE_ACTIVE stay quiet (they fire for every active head on
        # every resync — logging them would flood), but a CREATE / RETIRE /
        # TRANSITION / RESTART / ERROR is a convergence event worth seeing —
        # so a head that never converges is diagnosable from the logs rather
        # than silent.
        if action.kind not in (ActionKind.NOOP, ActionKind.ENSURE_ACTIVE):
            logger.info(
                "reconcile.action descriptor_id=%s kind=%s actor_id=%s observed=%s",
                desired.descriptor_id,
                action.kind.value,
                actor_id,
                "present" if record is not None else "absent",
            )
        await self._execute(action)
        return action

    async def _main_loop(self) -> None:
        try:
            while not self._stopped:
                try:
                    descriptor_id, reason = await asyncio.wait_for(
                        self._queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                try:
                    # Head-of-line protection: a single descriptor whose
                    # reconcile blocks (a wedged actor activation, a slow
                    # registry / DB call) must NEVER starve the rest of the
                    # queue — otherwise a newly-registered active head behind it
                    # silently never goes live and the durability re-asserts
                    # stall. Bound each pass; a timed-out descriptor is
                    # re-enqueued by the next resync (reconcile is idempotent).
                    await asyncio.wait_for(
                        self.run_once(descriptor_id),
                        timeout=self._run_once_timeout.total_seconds(),
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "reconcile.run_once.timeout descriptor_id=%s reason=%s "
                        "timeout=%.0fs — skipped, re-tried next resync",
                        descriptor_id,
                        reason,
                        self._run_once_timeout.total_seconds(),
                    )
                except Exception as exc:  # pragma: no cover
                    logger.exception(
                        "reconcile.failed descriptor_id=%s reason=%s err=%s",
                        descriptor_id,
                        reason,
                        exc,
                    )
        except asyncio.CancelledError:
            return

    async def _resync_loop(self) -> None:
        try:
            while not self._stopped:
                await asyncio.sleep(self._resync_interval.total_seconds())
                if self._stopped:
                    return
                try:
                    descriptors = await self._list()
                except Exception as exc:  # pragma: no cover
                    logger.warning("reconcile.resync.list_failed err=%s", exc)
                    continue
                for d in descriptors:
                    self.enqueue(d.descriptor_id, reason="periodic_resync")
                # Orphan-reminder GC piggybacks on the resync cadence: after
                # the desired-state walk has (re-)enqueued the live set, sweep
                # any daprd reminders still owned by RETIRED actors. Failure is
                # logged-and-continued — a GC outage must never stall reconcile.
                if self._reminder_gc is not None:
                    try:
                        await self._reminder_gc()
                    except Exception as exc:  # pragma: no cover — best-effort
                        logger.warning("reconcile.reminder_gc.failed err=%s", exc)
                # Task #236 RIDER: same best-effort, logged-and-continued shape
                # as reminder_gc above — a stale-pack-deps sweep failure must
                # never stall reconcile.
                if self._pack_staleness_check is not None:
                    try:
                        await self._pack_staleness_check()
                    except Exception as exc:  # pragma: no cover — best-effort
                        logger.warning(
                            "reconcile.pack_staleness_check.failed err=%s", exc,
                        )
        except asyncio.CancelledError:
            return


def _default_actor_id(descriptor_kind: str, descriptor_id: str, version: str) -> str:
    """Per spec §2.1 + post-bring-up hardening: ``kind::id::content_hash[:16]``.

    Widened from 8-char to 16-char in 2026-05 (Phase 5 hardening item 7).
    Rationale: two descriptor versions whose content hashes share an
    8-character prefix would collide on actor_id; bumping to 16 gives the
    spike >10^19 distinct identities per (kind, id) tuple, eliminating
    the practical collision risk while keeping ids short enough for daprd
    placement / logging.

    Callers MUST use this helper (or :class:`ReconcileLoop`'s injected
    ``actor_id_fn``) so the grammar stays consistent across construction
    sites. The mirror in ``runtime/dapr_actors.py`` module docstring is
    the authoritative grammar description; this function is the only
    implementation.
    """
    short = (version or "")[:16] or "0" * 16
    return f"{descriptor_kind}::{descriptor_id}::{short}"


__all__ = [
    "ActionExecutor",
    "ActionKind",
    "AnalystReconciler",
    "DesiredLister",
    "DesiredResolver",
    "DesiredState",
    "ObservedState",
    "ReconcileAction",
    "ReconcileLoop",
    "Reconciler",
    "TargetReconciler",
]
