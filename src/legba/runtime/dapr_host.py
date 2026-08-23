# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba-runtime Dapr-host — Phase 5a daprd validation surface.

This is the actor-host process daprd routes ActorProxy invocations to.
It owns:

  * A FastAPI app on port 6090 (default; override via env).
  * The Dapr Actor wiring (``DaprActor(app)``) which mounts the magic
    ``/actors/{type}/{id}/method/...`` endpoints that daprd hits.
  * The :func:`bring_up_for_test` helper used by the integration test to
    register actor types + populate the dependency registry against an
    already-running daprd sidecar.

It is intentionally separate from :mod:`legba.runtime.host` (the
embedded-mode host). The embedded host runs the actor classes
in-process; the Dapr host runs them through daprd. Once Phase 6 fan-out
lands and the embedded host can be retired, these will collapse to one
entry point — but for the validation we want them distinct so the
spike's tests can clearly target one or the other.

CLI entry: ``legba-runtime-dapr``. Run after ``docker compose --profile
dapr up -d``; daprd will then route to this process's port 6090.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal as signalmod
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import uvicorn
from dapr.actor import ActorRuntime
from dapr.ext.fastapi import DaprActor
from fastapi import FastAPI

from .actor_turn import (
    HealBreaker,
    TurnBudgetExceeded,
    bounded_turn_op,
    heal_breaker_trips,
    heal_timeout_seconds,
)
from .dapr_actors import AnalystActor, TargetActor

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Optional NATS-event informer
# ---------------------------------------------------------------------------
#
# When a :class:`ReconcileLoop` is brought up alongside this host, the caller
# can attach the L-103 NATS informer via :func:`attach_reconcile_informer`
# below. The informer subscribes to ``descriptor.>`` on
# ``LEGBA_DESCRIPTOR_EVENTS`` and calls ``loop.enqueue`` per event, so the
# 5-minute periodic resync is no longer the only path that picks up
# descriptor mutations.
#
# We don't auto-wire the informer in :func:`build_dapr_host_app` because the
# spike integration test brings up the FastAPI surface WITHOUT a reconcile
# loop attached (the test invokes actors directly via ActorProxy). The
# attach helper lets production bring-up code opt in.


# Module-level holder threading the L-193 A2ASkillRegistry from main()
# (where it's constructed) down to the action_executor closure (defined
# inside bring_up_production_runtime, which can't reach main()'s ``app``).
# The closure reads ``A2A_SKILL_REGISTRY_HOLDER["registry"]`` at call
# time; main() populates it before the reconcile loop fires.
A2A_SKILL_REGISTRY_HOLDER: dict[str, Any] = {}

# Same pattern for the trigger engine (§2.1): the action_executor closure tears
# down a retired analyst's trigger registrations, but the engine is constructed
# later in source-first bring-up. Populated once source_first is up (before the
# reconcile loop starts dispatching retires); read at call time.
TRIGGER_ENGINE_HOLDER: dict[str, Any] = {}

# G1: the lifecycle states the periodic resync re-asserts. Non-active heads
# (paused/retired) are included so a retire/pause lost in its single-NATS-event
# delivery window self-heals on the next resync (reconcile is idempotent —
# already-converged heads NOOP). draft/configured are excluded: they map to no
# live-actor action and would trip the executor's unknown-lifecycle path.
RECONCILABLE_LIFECYCLE_STATES: frozenset[str] = frozenset({"active", "paused", "retired"})


# ---------------------------------------------------------------------------
# Reconcile action executor (module-level, dependency-injected)
# ---------------------------------------------------------------------------
#
# Extracted from the bring_up_production_runtime closure (2026-06, A-1/G1):
# the closure was untestable, and that is precisely where the lifecycle
# plane silently died — nothing ever wrote observed state, CREATE_ACTOR
# ignored target_lifecycle (pausing a descriptor re-ACTIVATED it), and
# version bumps left the old actor double-running. The executor now
# (a) honors the commanded lifecycle on CREATE, and (b) writes the
# observed-state row after every successful proxy call so reconcile can
# finally see reality (RETIRE/TRANSITION/ENSURE become reachable).


# ---------------------------------------------------------------------------
# §4.9 — GATHER-gate generalization. The agentic GATHER binding was hard-gated
# on `kind == "inline_target"` + the hardcoded SUBSTRATE_READ_PACK_ID. These two
# helpers generalize it to a capability check over the in-actor GATHER kind set +
# the kind's declared read pack, so a NEW llm_planner kind (journal_assessor →
# journal_read) gets its tools instead of silently running with none.
# ---------------------------------------------------------------------------

# The in-actor llm_planner kinds whose run_method drives a bounded GATHER loop.
# inline_target is the original; journal_assessor (plan §4.8) reuses the same
# GATHER machinery (inline_target._gather) on its own run_method. Membership
# here — NOT an opt-in flag — is what makes the binding wire; the three-way
# agency gate still decides whether each tool call is admitted at run time.
_GATHER_KINDS: frozenset[str] = frozenset({"inline_target", "journal_assessor"})

# Per-kind default READ pack the GATHER loop fetches. The descriptor must still
# GRANT the pack via `action_packs` (the grant leg is checked separately) — this
# only names WHICH read pack a given kind's GATHER loop reaches for.
_GATHER_READ_PACK_BY_KIND: dict[str, str] = {
    "journal_assessor": "journal_read",
    # inline_target falls through to the SUBSTRATE_READ_PACK_ID default below.
}


def _gather_kind_engages(ad: Any) -> bool:
    """True when this analyst is an in-actor GATHER kind (§4.9). The capability
    check that replaced the hard-coded `kind == "inline_target"` gates."""
    return getattr(ad.identity, "kind", None) in _GATHER_KINDS


def _gather_read_pack_for(ad: Any, default_pack_id: str) -> str | None:
    """The READ pack id this kind's GATHER loop fetches, or None when the kind is
    not a GATHER kind. inline_target → ``default_pack_id`` (substrate_read);
    journal_assessor → ``journal_read`` (§4.9)."""
    kind = getattr(ad.identity, "kind", None)
    if kind not in _GATHER_KINDS:
        return None
    return _GATHER_READ_PACK_BY_KIND.get(kind, default_pack_id)


async def _write_observed_state(
    state_store: Any,
    action: Any,
    lifecycle: str,
) -> None:
    """Upsert the observed-state row for a just-executed lifecycle action.

    Read-modify-write so fields we don't own (source_cursors, extras) are
    preserved. Best-effort: a write failure is logged, and the actor falls
    back to the idempotent CREATE-on-next-resync behavior — never blocks
    the proxy call that already succeeded.
    """
    if state_store is None:
        return
    from .state import ActorStateRecord

    detail = action.detail or {}
    actor_id = action.actor_id
    parts = actor_id.split("::", 2)
    actor_kind = detail.get("descriptor_kind") or parts[0]
    descriptor_id = detail.get("descriptor_id") or (
        parts[1] if len(parts) > 1 else actor_id
    )
    version = detail.get("descriptor_version") or detail.get("new_version") or ""
    try:
        rec = await state_store.get(actor_id)
        if rec is None:
            rec = ActorStateRecord(
                actor_id=actor_id,
                actor_kind=actor_kind,
                descriptor_id=descriptor_id,
                descriptor_version=version
                or (parts[2] if len(parts) > 2 else ""),
                lifecycle=lifecycle,
            )
        else:
            rec.lifecycle = lifecycle
            if version:
                rec.descriptor_version = version
        rec.last_outcome = f"reconcile:{action.kind.value}"
        await state_store.upsert(rec)
    except Exception as exc:
        logger.warning(
            "action_executor.observed_state.write_failed actor_id=%s err=%s",
            actor_id, exc,
        )


#: S-6 — process-wide breaker for the reconciler's per-actor durability heal.
#: Module-level because the reconcile loop is a singleton (leader-gated in
#: multi-node) and the streak must survive across resync passes; a breaker
#: rebuilt per action would count to one forever.
_HEAL_BREAKER = HealBreaker()


async def execute_reconcile_action(
    action: Any,
    *,
    proxy_for: Any,
    state_store: Any | None = None,
    remember_analyst: Any | None = None,
    forget_analyst: Any | None = None,
    register_a2a_skills: Any | None = None,
    unregister_a2a_skills: Any | None = None,
    unregister_triggers: Any | None = None,
    breaker: HealBreaker | None = None,
) -> None:
    """Apply one :class:`ReconcileAction` to the actor runtime.

    ``proxy_for(actor_kind, actor_id)`` returns the lifecycle proxy —
    injected so tests can assert the exact calls without daprd. All hook
    params are optional; production wires them in
    :func:`bring_up_production_runtime`'s thin closure.

    S-6: every proxy lifecycle call runs under
    :func:`~legba.runtime.actor_turn.heal_timeout_seconds`, well below the 90 s
    ``run_once`` bound. On 2026-08-01 a hung ``activate()`` consumed that whole
    bound per descriptor and, because the reconcile loop is strictly serial,
    dragged the queue to one descriptor per 90 s while the actor plane
    turn-poisoned itself behind it. A timed-out call here is now a short,
    logged SKIP: the observed-state row is deliberately NOT written (the
    lifecycle was never confirmed, and recording it as confirmed is how the
    reconciler goes blind), and the next resync retries — reconcile is
    idempotent. Repeated timeouts on the same actor open ``breaker`` so a
    wedged fleet stops costing the queue anything at all.
    """
    from .reconcile import ActionKind

    detail = action.detail or {}
    actor_kind = detail.get("descriptor_kind") or action.actor_id.split("::", 1)[0]
    descriptor_id = detail.get("descriptor_id", "")
    brk = breaker if breaker is not None else _HEAL_BREAKER
    try:
        proxy = proxy_for(actor_kind, action.actor_id)
    except ValueError as exc:
        logger.warning("action_executor.unknown_kind %s", exc)
        return

    async def _call(method: str) -> bool:
        """One bounded proxy lifecycle call. True = the actor confirmed it.

        False means the deadline blew. Callers MUST treat that as "did not
        happen" and skip both the observed-state write and the live-set hooks —
        a heal we only *attempted* must not be recorded as a heal that landed.
        """
        try:
            await bounded_turn_op(
                getattr(proxy, method)(),
                op=f"reconcile.{method}",
                actor_id=action.actor_id,
                timeout=heal_timeout_seconds(),
            )
        except TurnBudgetExceeded:
            count = brk.record_timeout(action.actor_id)
            logger.warning(
                "action_executor.deadline kind=%s method=%s actor_id=%s "
                "consecutive=%d — skipped, retried next resync",
                action.kind.name, method, action.actor_id, count,
            )
            return False
        brk.record_success(action.actor_id)
        return True

    # ENSURE_ACTIVE fires for every active analyst/source on every periodic
    # resync (the durability heal) — keep it at debug so it doesn't flood.
    _log = logger.debug if action.kind == ActionKind.ENSURE_ACTIVE else logger.info
    _log(
        "action_executor.invoke kind=%s actor_id=%s detail=%s",
        action.kind.name, action.actor_id, detail,
    )

    async def _analyst_went_live() -> None:
        if actor_kind != "analyst":
            return
        if remember_analyst is not None:
            remember_analyst(descriptor_id, action.actor_id)

    async def _analyst_retired() -> None:
        if actor_kind != "analyst":
            return
        if unregister_a2a_skills is not None:
            removed = unregister_a2a_skills(descriptor_id)
            if removed:
                logger.info(
                    "action_executor.a2a_skill.unregistered analyst_id=%s count=%d",
                    descriptor_id, removed,
                )
        if forget_analyst is not None:
            forget_analyst(descriptor_id, action.actor_id)
        if unregister_triggers is not None:
            removed = unregister_triggers(descriptor_id)
            if removed:
                logger.info(
                    "action_executor.triggers.unregistered analyst_id=%s count=%d",
                    descriptor_id, removed,
                )

    async def _analyst_paused() -> None:
        # §2.1: pause forgets the analyst from the dispatch live-set (the
        # forget_analyst hook) so the trigger gate NOOPs its reactive fires —
        # symmetric with the cadence reminder that A-1 already unregisters on
        # pause. Reversible: TRANSITION_LIFECYCLE→active re-remembers via
        # _analyst_went_live. We do NOT tear down trigger registrations here
        # (that's the terminal retire cleanup) — the gate suffices and avoids
        # a re-wire on resume.
        if actor_kind != "analyst":
            return
        if forget_analyst is not None:
            forget_analyst(descriptor_id, action.actor_id)

    if action.kind == ActionKind.CREATE_ACTOR:
        # Honor the descriptor's commanded lifecycle (G1: this branch used
        # to call activate() unconditionally, so pausing a descriptor whose
        # observed row was missing — i.e. always, pre-fix — re-activated it).
        target_lc = (detail.get("target_lifecycle") or "active").lower()
        if target_lc == "active":
            if actor_kind == "analyst" and descriptor_id:
                # New head version landing → drop stale per-target worker deps
                # (version-less ids cache forever; see
                # dapr_actors.evict_analyst_deps_for_descriptor). The next fire
                # re-resolves the new head's prompt/method/budget/gates.
                from .dapr_actors import evict_analyst_deps_for_descriptor

                evict_analyst_deps_for_descriptor(descriptor_id)
            if not await _call("activate"):
                return
            await _analyst_went_live()
            if actor_kind == "analyst" and register_a2a_skills is not None:
                await register_a2a_skills(
                    descriptor_id, detail.get("descriptor_version", ""),
                )
            await _write_observed_state(state_store, action, "active")
        elif target_lc == "paused":
            # Dapr runs _on_activate before any method (creating the
            # actor's own record); pause() then parks it and (post-A-1)
            # unregisters its reminder. Never activate().
            if not await _call("pause"):
                return
            await _analyst_paused()
            await _write_observed_state(state_store, action, "paused")
        elif target_lc == "retired":
            # Defensive — the reconciler resolves retired descriptors to
            # RETIRE_ACTOR/NOOP before ever emitting CREATE.
            logger.info(
                "action_executor.create.skip_retired actor_id=%s",
                action.actor_id,
            )
        else:
            # draft/configured — no live actor wanted; nothing to observe.
            logger.info(
                "action_executor.create.skip_lifecycle actor_id=%s lifecycle=%s",
                action.actor_id, target_lc,
            )
    elif action.kind == ActionKind.ENSURE_ACTIVE:
        # Durability heal — re-assert the durable reminder, and reactivate
        # the actor if it idled out (Dapr activates it to deliver the call).
        # activate() is idempotent on analysts AND sources: deps are cached
        # and the reminder re-anchors to the next cron boundary.
        #
        # S-6: the breaker is consulted ONLY here. ENSURE_ACTIVE is re-emitted
        # for every active analyst and source on every resync, so an actor that
        # is not answering would otherwise be re-poked forever — at 217 active
        # actors a fleet-wide wedge burns 217 x the deadline per cycle, every
        # cycle, which is the sum the per-call deadline alone does not bound.
        # Skipping is safe precisely because this action re-runs next resync.
        # CREATE / RETIRE / TRANSITION are NOT breaker-gated: those are one-shot
        # convergence steps, and skipping one means a descriptor never reaching
        # its declared state.
        if brk.should_skip(action.actor_id):
            logger.warning(
                "action_executor.heal_suppressed actor_id=%s — %d consecutive "
                "deadline misses; retrying after the cooloff",
                action.actor_id, heal_breaker_trips(),
            )
            return
        if not await _call("activate"):
            return
        await _analyst_went_live()
        # Re-populate a2a skills after a restart: the resync re-asserts active
        # analysts via ENSURE_ACTIVE (not CREATE), so without this the in-memory
        # A2ASkillRegistry stays empty after a runtime restart. Guarded by
        # has_analyst_version → a no-op fetch-skip once registered.
        if actor_kind == "analyst" and register_a2a_skills is not None:
            await register_a2a_skills(
                descriptor_id, detail.get("descriptor_version", ""),
            )
        await _write_observed_state(state_store, action, "active")
    elif action.kind == ActionKind.RETIRE_ACTOR:
        if not await _call("retire"):
            return
        # The actor is gone — stop tracking it, so a retired wedge cannot keep
        # an entry alive in the breaker forever.
        brk.forget(action.actor_id)
        await _analyst_retired()
        await _write_observed_state(state_store, action, "retired")
    elif action.kind == ActionKind.RESTART_ACTOR:
        # Dapr re-activates from persisted state; calling activate()
        # again is idempotent and re-reads the descriptor body. A
        # full retire-then-activate would lose source cursors, so
        # restart-on-content-hash-change is a soft restart only.
        if not await _call("activate"):
            return
        await _write_observed_state(state_store, action, "active")
    elif action.kind == ActionKind.TRANSITION_LIFECYCLE:
        target_lc = (detail.get("to") or "").lower()
        if target_lc == "active":
            # PAUSED → active is a RESUME: re-register the cadence reminder
            # that pause() unregistered (proxy.resume() mirrors pause()).
            # Any other source state (incl. a rollback-restored retired head)
            # routes through activate(), which re-runs _on_activate and
            # resurrects a parked record to active.
            from_lc = (detail.get("from") or "").lower()
            if not await _call("resume" if from_lc == "paused" else "activate"):
                return
            await _analyst_went_live()
            if actor_kind == "analyst" and register_a2a_skills is not None:
                await register_a2a_skills(
                    descriptor_id, detail.get("descriptor_version", ""),
                )
            await _write_observed_state(state_store, action, "active")
        elif target_lc == "paused":
            if not await _call("pause"):
                return
            await _analyst_paused()
            await _write_observed_state(state_store, action, "paused")
        elif target_lc == "retired":
            if not await _call("retire"):
                return
            brk.forget(action.actor_id)
            await _analyst_retired()
            await _write_observed_state(state_store, action, "retired")
        else:
            logger.warning(
                "action_executor.unknown_lifecycle target=%s actor_id=%s",
                target_lc, action.actor_id,
            )
    # NOOP / ERROR / BACKOFF — nothing to do on the actor side.


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def build_dapr_host_app(
    *,
    app_port: int | None = None,
    a2a_registry: Any | None = None,
    a2a_identity: Any | None = None,
    a2a_fetch_latest_outputs: Any | None = None,
    a2a_trusted_keys: Any | None = None,
    a2a_prefix: str = "/a2a/skills",
) -> FastAPI:
    """Build the FastAPI app daprd talks to.

    Side-effect: registers ``TargetActor`` and ``AnalystActor`` types
    with the global :class:`ActorRuntime` (single-process, fine to call
    multiple times — the registration is idempotent on the type name).

    The L-193 A2A skill router is mounted when ``a2a_registry``,
    ``a2a_identity``, and ``a2a_fetch_latest_outputs`` are all supplied.
    When any of them is ``None`` the router is omitted — the spike's
    existing tests bring the host up without the descriptor / signing
    infra threaded through (see ``test_spike_integration.py``).
    Integration tests that exercise the L-193 surface pass the trio in
    explicitly.
    """
    if app_port is None:
        app_port = int(os.getenv("LEGBA_RUNTIME_HTTP_PORT", "6090"))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Register actor types with the SDK at startup. SourceActor joins the
        # Target/Analyst types per P-CUT (source-first acquisition host).
        from .source_actor import SourceActor
        await ActorRuntime.register_actor(TargetActor)
        await ActorRuntime.register_actor(AnalystActor)
        if SourceActor is not None:
            await ActorRuntime.register_actor(SourceActor)
        logger.info(
            "dapr_host.actor_types.registered types=%s",
            list(ActorRuntime.get_registered_actor_types()),
        )
        yield
        logger.info("dapr_host.shutdown")

    app = FastAPI(title="Legba Runtime (Dapr)", version="0.1.0", lifespan=lifespan)
    # DaprActor mounts the magic actor endpoints daprd will hit.
    DaprActor(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "actor_types": list(ActorRuntime.get_registered_actor_types()),
        }

    # Mount L-193 A2A skill router when the caller supplied the wiring.
    if (
        a2a_registry is not None
        and a2a_identity is not None
        and a2a_fetch_latest_outputs is not None
    ):
        attach_a2a_skill_router(
            app,
            registry=a2a_registry,
            identity=a2a_identity,
            fetch_latest_outputs=a2a_fetch_latest_outputs,
            trusted_keys=a2a_trusted_keys,
            prefix=a2a_prefix,
        )
        logger.info(
            "dapr_host.a2a_skill_router.mounted prefix=%s",
            a2a_prefix,
        )

    return app


# B-2 fail-closed posture for the production A2A mount (main()).
A2A_ENABLED_ENV = "LEGBA_A2A_ENABLED"
A2A_TRUSTED_KEYS_ENV = "LEGBA_A2A_TRUSTED_KEYS"


def resolve_a2a_mount() -> Any | None:
    """Decide whether main() may mount the A2A skill surface (B-2).

    Returns the :class:`~legba.data.outputs.a2a_skill.TrustedKeyDirectory`
    to mount with, or ``None`` when the surface must stay UNMOUNTED — the
    default. Previously main() mounted unconditionally with
    ``trusted_keys=None``, which left the unauthenticated GET endpoints
    (and any ``auth_required=False`` skill) open to every caller.

      * ``LEGBA_A2A_ENABLED`` != "1" → ``None`` (mount disabled; default).
      * Enabled + ``LEGBA_A2A_TRUSTED_KEYS`` non-empty → directory of
        ``did=hex`` caller verify-keys.
      * Enabled + empty allowlist → RuntimeError (refuse activation)
        UNLESS ``LEGBA_DEV_MODE=1`` is explicitly set, in which case an
        empty directory is returned (auth-required skills still reject
        every caller; only explicitly-public skills answer).
    """
    from ..data.outputs.a2a_skill import TrustedKeyDirectory

    if os.getenv(A2A_ENABLED_ENV, "").strip() != "1":
        return None
    directory = TrustedKeyDirectory.from_env(A2A_TRUSTED_KEYS_ENV)
    if not directory.keys and os.getenv("LEGBA_DEV_MODE", "").strip() != "1":
        raise RuntimeError(
            f"{A2A_ENABLED_ENV}=1 but {A2A_TRUSTED_KEYS_ENV} is empty and "
            "LEGBA_DEV_MODE=1 is not set — refusing to mount the A2A skill "
            "surface without an explicit caller allowlist (B-2 fail-closed)."
        )
    return directory


# L-193 — A2A skill output kind route registration helper.
# Kept additive so the integration pass (which wires the real
# `fetch_latest_outputs` against the descriptor registry + Postgres) can
# call this without touching the rest of `build_dapr_host_app`. The helper
# does not mount anything on import — callers must invoke it after the
# substrate is up.
def attach_a2a_skill_router(
    app: FastAPI,
    *,
    registry: Any,
    identity: Any,
    fetch_latest_outputs: Any,
    trusted_keys: Any | None = None,
    prefix: str = "/a2a/skills",
) -> Any:
    """Wire the L-193 A2A skill output kind onto the dapr-host FastAPI app.

    Thin re-export of :func:`legba.data.outputs.a2a_skill.register_a2a_skill_route`
    so callers don't need to import the kind module directly. Returns the
    constructed APIRouter.
    """
    from ..data.outputs.a2a_skill import register_a2a_skill_route

    return register_a2a_skill_route(
        app,
        registry=registry,
        identity=identity,
        fetch_latest_outputs=fetch_latest_outputs,
        trusted_keys=trusted_keys,
        prefix=prefix,
    )


@dataclass
class _RuntimeHandles:
    """Owned handles from :func:`bring_up_production_runtime`.

    Holds the live substrate connections + the reconcile loop + the
    NATS informer so the FastAPI lifespan can shut them down cleanly
    on signal.
    """

    pg_store: Any
    nats_store: Any
    state_store: Any
    reconcile_loop: Any
    informer: Any
    registry_client: Any
    leader_lease: Any = None

    async def stop(self) -> None:
        # Stop in reverse order — leader lease first (release the advisory lock
        # so a sibling replica promotes fast), then informer (stop pulling new
        # events), then the reconcile loop drains its queue, then close the
        # connections. Releasing the lock BEFORE closing the pool keeps the
        # dedicated lock-connection valid for the explicit unlock.
        lease = getattr(self, "leader_lease", None)
        if lease is not None:
            try:
                await lease.stop()
            except Exception as exc:                            # pragma: no cover
                logger.warning("leader_lease.stop err=%s", exc)
        try:
            await self.informer.stop()
        except Exception as exc:                                # pragma: no cover
            logger.warning("informer.stop err=%s", exc)
        # S-2: the stack-component informer + its cache registration. Stored
        # via getattr (added incrementally — not a formal field), and
        # unregistered so the module-level sweep set doesn't retain a dict
        # belonging to a runtime that has shut down.
        stack_informer = getattr(self, "stack_informer", None)
        if stack_informer is not None:
            try:
                await stack_informer.stop()
            except Exception as exc:                            # pragma: no cover
                logger.warning("stack_informer.stop err=%s", exc)
        # RUST-5: the vault-rotation informer. Same getattr posture as the
        # stack informer above — added incrementally, not a formal field.
        vault_informer = getattr(self, "vault_informer", None)
        if vault_informer is not None:
            try:
                await vault_informer.stop()
            except Exception as exc:                            # pragma: no cover
                logger.warning("vault_informer.stop err=%s", exc)
        handler_cache = getattr(self, "llm_handler_cache", None)
        if handler_cache is not None:
            from .llm_handler_cache import unregister_handler_cache

            unregister_handler_cache(handler_cache)
        # P-CUT: stop the source-first planes (trigger engine + job worker
        # pool) before tearing down the reconcile loop + closing NATS/PG.
        # Stored via getattr (added incrementally — not a formal field).
        source_first = getattr(self, "source_first", None)
        if source_first is not None:
            try:
                await source_first.stop()
            except Exception as exc:                            # pragma: no cover
                logger.warning("source_first.stop err=%s", exc)
        # P-2: stop audit checkpointer before reconcile loop so its
        # last tick can still INSERT into audit_checkpoints. Stored
        # via getattr (added incrementally — not a formal field).
        checkpointer = getattr(self, "audit_checkpointer", None)
        if checkpointer is not None:
            try:
                await checkpointer.stop()
            except Exception as exc:                            # pragma: no cover
                logger.warning("audit_checkpointer.stop err=%s", exc)
        try:
            await self.reconcile_loop.stop()
        except Exception as exc:                                # pragma: no cover
            logger.warning("reconcile_loop.stop err=%s", exc)
        try:
            await self.registry_client.close()
        except Exception as exc:                                # pragma: no cover
            logger.warning("registry_client.close err=%s", exc)
        # W-1 / #235: close qdrant + embedding service if built. Both stored
        # via getattr as LAZY HOLDERS (not raw clients) — formal
        # _RuntimeHandles fields list doesn't yet carry them. Reading
        # `.cached` (rather than a boot-time snapshot local) means a client
        # that resolved LATE via lazy re-resolution (after the boot-time
        # attempt failed) still gets closed here — no leak on the recovery
        # path.
        qdrant_holder = getattr(self, "qdrant_client_holder", None)
        qdrant = qdrant_holder.cached if qdrant_holder is not None else None
        if qdrant is not None:
            try:
                await qdrant.close()
            except Exception as exc:                            # pragma: no cover
                logger.warning("qdrant_client.close err=%s", exc)
        embedding_holder = getattr(self, "embedding_service_holder", None)
        embedding = embedding_holder.cached if embedding_holder is not None else None
        if embedding is not None:
            try:
                await embedding.aclose()
            except Exception as exc:                            # pragma: no cover
                logger.warning("embedding_service.aclose err=%s", exc)
        output_http = getattr(self, "output_http_client", None)
        if output_http is not None:
            try:
                await output_http.aclose()
            except Exception as exc:                            # pragma: no cover
                logger.warning("output_http_client.aclose err=%s", exc)
        # Optimizer Dapr-Workflow worker — stop the in-process WorkflowRuntime
        # (best-effort). Replaces the old temporalio client, which had no
        # explicit close (gRPC channel was GC'd).
        wf = getattr(self, "workflow_runtime", None)
        if wf is not None:
            try:
                wf.shutdown()
            except Exception as exc:                            # pragma: no cover
                logger.warning("optimizer_workflow.shutdown err=%s", exc)
        try:
            await self.nats_store.close()
        except Exception as exc:                                # pragma: no cover
            logger.warning("nats_store.close err=%s", exc)
        try:
            await self.pg_store.close()
        except Exception as exc:                                # pragma: no cover
            logger.warning("pg_store.close err=%s", exc)


async def bring_up_production_runtime() -> _RuntimeHandles:
    """Wire the reconcile loop + NATS informer + action executor for live runtime.

    What this fills in vs :func:`build_dapr_host_app`:

      * Substrate connections (Postgres pool + NATS JetStream client).
      * :class:`ActorStateStore` against ``public.actor_state``.
      * :class:`RegistryHTTPClient` used by the desired-resolver + lister.
      * :class:`ReconcileLoop` with an ``action_executor`` that translates
        :class:`ReconcileAction` instances into Dapr Actor invocations via
        :class:`ActorProxy`.
      * :class:`NatsReconcileInformer` subscribing to ``descriptor.>`` for
        live propagation (the registry publishes on every descriptor
        lifecycle event).
      * One synchronous initial resync — enqueues every active descriptor
        so its actor gets activated through Dapr on bring-up. This is what
        the 2026-05-22 host-mode runtime did inline and what the L-205
        retirement dropped; without it the substrate stays idle even when
        descriptors exist.

    Returns a :class:`_RuntimeHandles` for the lifespan handler to stop on
    shutdown.
    """
    # Late imports inside the function to keep the dapr_host module
    # importable without yanking the full substrate/runtime closure into
    # callers that only need ``build_dapr_host_app`` (e.g. tests).
    from dapr.actor import ActorId, ActorProxy
    from ..data.config import NatsConfig, PostgresConfig
    from ..data.nats import NatsStore
    from ..data.postgres import PostgresStore
    from .dapr_actors import AnalystActorInterface, TargetActorInterface
    from .nats_informer import (
        NatsReconcileInformer,
        NatsStackComponentInformer,
        NatsVaultRotationInformer,
    )
    from .reconcile import (
        AnalystReconciler,
        DesiredState,
        ReconcileLoop,
        TargetReconciler,
        _default_actor_id,
    )
    from .leader import LeaderLease, assert_singleton_safe
    from .registry_client import RegistryHTTPClient
    from .source_actor import SourceActorInterface
    from .state import ActorStateStore

    # ---------- single-replica fail-loud guard (scaling-multinode) ------
    #
    # Refuse to boot if the operator declared >1 replica without leader
    # election — running >1 replica would silently double-run the singleton
    # control-plane loops (reconcile resync + descriptor informer) on every
    # replica. Raises SingletonSafetyError BEFORE any substrate connection so a
    # misconfigured multi-replica deploy fails loud + fast. See docs/SEAMS.md.
    assert_singleton_safe()

    # ---------- substrate connections ----------------------------------
    pg_store = PostgresStore(PostgresConfig.from_env())
    await pg_store.connect()
    nats_store = NatsStore(NatsConfig.from_env())
    await nats_store.connect()

    # Leader lease for the singleton control-plane loops. When
    # LEGBA_LEADER_ELECTION is off this is a no-op leader (single-node: the one
    # replica is leader by definition); when on, a Postgres session-level
    # advisory lock elects exactly one leader across replicas. Started (and the
    # singleton loops gated on it) below, after the loop + informer are built.
    leader_lease = LeaderLease(pg_store)

    state_store = ActorStateStore(pg_store.pool)
    await state_store.ensure_schema()

    registry_client = RegistryHTTPClient()  # reads LEGBA_REGISTRY_API_URL etc.

    # ---------- desired-state lookups ----------------------------------
    #
    # The registry's REST surface is the canonical view. The runtime
    # never reads descriptor rows out of Postgres directly — going
    # through HTTP picks up the registry's auth + audit hooks, and the
    # registry container is the source of truth for content-hash heads.

    def _to_desired(row: dict[str, Any]) -> DesiredState:
        return DesiredState(
            descriptor_id=row["descriptor_id"],
            descriptor_kind=row["family"],
            descriptor_version=row["version"],
            lifecycle_target=row["state"],
            body=row.get("body") or {},
        )

    # Per-id family cache for the resolver below. A descriptor_id belongs to
    # exactly ONE family for life, but the id alone doesn't carry it — so the
    # first resolve probes the families in order and every MISS is a registry
    # 404. The periodic resync re-resolves the SAME live set every interval, so
    # without a memo each pass re-pays those cross-family 404s (~5k/6h observed:
    # every source id costs a target+analyst 404, every analyst id a target
    # 404). Remembering an id's resolved family makes the resync hit the right
    # family FIRST — steady-state cross-family 404s drop to ~0.
    _resolver_family_cache: dict[str, str] = {}

    async def desired_resolver(descriptor_id: str) -> DesiredState | None:
        # Descriptor family isn't carried in the actor_id alone; try each.
        # (All but one 404 and that's fine.) ``source`` joins target/analyst
        # per P-CUT so the reconcile loop can drive SourceActor lifecycle.
        # Probe the previously-resolved family FIRST (see cache note above) so a
        # re-resolve of a known id costs one lookup, not a full cross-family walk.
        _FAMILIES = ("target", "analyst", "source")
        cached = _resolver_family_cache.get(descriptor_id)
        probe_order = (
            (cached, *(f for f in _FAMILIES if f != cached)) if cached else _FAMILIES
        )
        for family in probe_order:
            row = await registry_client.get_descriptor(descriptor_id, family=family)
            if row is not None:
                _resolver_family_cache[descriptor_id] = family
                return _to_desired(row)
        # Not found under any family (retired/deleted head) — drop any stale
        # memo so a re-created id re-probes cleanly rather than pinning a family.
        _resolver_family_cache.pop(descriptor_id, None)
        return None

    async def desired_lister() -> list[DesiredState]:
        # Direct HTTP — RegistryHTTPClient only has single-descriptor
        # GET helpers today. List is one shot per resync, so this stays
        # a plain httpx call to the registry's /descriptors endpoint.
        # P-CUT: the UNFILTERED /descriptors lister only unions target +
        # analyst families, so the ``source`` family must be enumerated
        # explicitly — otherwise the periodic resync never sees source
        # descriptors and no SourceActor is ever created. Mirrors the
        # family loop in ``desired_resolver``.
        import httpx
        base_url = os.environ.get(
            "LEGBA_REGISTRY_API_URL", "http://localhost:8090",
        ).rstrip("/")
        token = os.environ.get("LEGBA_REGISTRY_API_TOKEN") or "dev"
        rows: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=15) as c:
            for family in ("target", "analyst", "source"):
                r = await c.get(
                    f"{base_url}/api/v1/registry/descriptors",
                    params={"family": family, "head_only": "true", "limit": 500},
                    headers={"Authorization": f"Bearer {token}"},
                )
                r.raise_for_status()
                rows.extend(r.json())
        # G1: enumerate non-active heads too (paused + retired), not just
        # active. Retire/pause arrives as exactly ONE NATS event via the
        # informer; if that delivery is lost (crash/restart in the window,
        # stream max_age 1h) this periodic resync is the only re-assertion —
        # filtering to active here is what made a lost retire/pause stick until
        # a manual touch. Reconcile is idempotent: an already-converged
        # paused/retired head → NOOP; a missed transition (observed still
        # active) → the RETIRE / TRANSITION→paused the lost event would have
        # produced. draft/configured stay excluded — they map to no live-actor
        # action and would hit the executor's unknown-lifecycle path.
        return [
            _to_desired(row)
            for row in rows
            if row.get("state") in RECONCILABLE_LIFECYCLE_STATES
        ]

    # ---------- action executor → ActorProxy invocations ---------------
    #
    # ReconcileLoop emits actions; the executor maps each action to the
    # Dapr Actor lifecycle method that effects the change. Dapr's actor
    # framework activates the actor on first invocation and persists its
    # reminders/cron via the dapr-scheduler — so calling ``activate()``
    # is both "create" and "ensure-registered."

    def _proxy_for(actor_kind: str, actor_id: str):
        if actor_kind == "target":
            return ActorProxy.create(
                "TargetActor", ActorId(actor_id), TargetActorInterface,
            )
        if actor_kind == "analyst":
            return ActorProxy.create(
                "AnalystActor", ActorId(actor_id), AnalystActorInterface,
            )
        if actor_kind == "source":
            # P-CUT: the reconcile executor drives SourceActor lifecycle
            # (CREATE/RETIRE/TRANSITION) just like target/analyst. SourceActor
            # owns acquisition; activate() provisions upstream + registers the
            # poll Reminder (or binds the push handler).
            return ActorProxy.create(
                "SourceActor", ActorId(actor_id), SourceActorInterface,
            )
        raise ValueError(f"unknown actor kind {actor_kind!r} for {actor_id}")

    async def _maybe_register_a2a_skills(
        descriptor_id: str, descriptor_version: str,
    ) -> None:
        """K-4 wiring: when an analyst with ``outputs.a2a_skill`` activates,
        the L-193 router needs its ``A2ASkillRegistry`` populated.  The
        comment on app.state.a2a_skill_registry promised the executor
        would do this — wire it now.

        ``A2A_SKILL_REGISTRY_HOLDER`` is a module-level dict that
        ``main()`` populates after constructing the registry.  The
        ``action_executor`` closure can't reach ``app`` directly because
        it lives in :func:`bring_up_production_runtime`, not in
        :func:`main`, so the registry is threaded module-wide.

        Idempotent: re-registering the same skill_id replaces the prior
        entry.  Failures are logged and swallowed (skill registration is
        non-blocking; the actor activation is what matters).
        """
        registry = A2A_SKILL_REGISTRY_HOLDER.get("registry")
        if registry is None:
            return
        # Already registered at this version (steady-state resync) → skip the
        # descriptor re-fetch + replace. Re-registers after a restart (registry
        # empty) or a version bump (stored version differs) — see
        # A2ASkillRegistry.has_analyst_version. This is what makes calling the
        # hook on every ENSURE_ACTIVE cheap rather than a fetch-per-resync.
        if registry.has_analyst_version(descriptor_id, descriptor_version):
            return
        import httpx
        base_url = os.environ.get(
            "LEGBA_REGISTRY_API_URL", "http://localhost:8090",
        ).rstrip("/")
        token = os.environ.get("LEGBA_REGISTRY_API_TOKEN") or "dev"
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(
                    f"{base_url}/api/v1/registry/descriptors/analyst/"
                    f"{descriptor_id}/typed",
                    headers={"Authorization": f"Bearer {token}"},
                )
                r.raise_for_status()
                body = r.json()
        except Exception as exc:
            logger.warning(
                "action_executor.a2a_skill.fetch_failed descriptor_id=%s %s",
                descriptor_id, exc,
            )
            return
        outputs = body.get("outputs") or []
        if not any(o.get("kind") == "a2a_skill" for o in outputs):
            return
        try:
            regs = registry.register_from_descriptor(
                analyst_id=descriptor_id,
                analyst_version=descriptor_version,
                descriptor_id=descriptor_id,
                outputs=outputs,
                type_signature=(body.get("identity") or {}).get("type_signature"),
            )
            for reg in regs:
                logger.info(
                    "action_executor.a2a_skill.registered "
                    "skill_id=%s analyst_id=%s",
                    reg.skill_id, reg.analyst_id,
                )
        except Exception as exc:
            logger.warning(
                "action_executor.a2a_skill.register_failed "
                "descriptor_id=%s %s",
                descriptor_id, exc,
            )

    def _unregister_a2a_skills(descriptor_id: str) -> int:
        registry = A2A_SKILL_REGISTRY_HOLDER.get("registry")
        if registry is None:
            return 0
        return registry.unregister_by_analyst(descriptor_id)

    def _unregister_triggers(descriptor_id: str) -> int:
        engine = TRIGGER_ENGINE_HOLDER.get("engine")
        if engine is None:
            return 0
        return engine.unregister(descriptor_id)

    async def action_executor(action: Any) -> None:
        # Thin wrapper: all lifecycle semantics live in the module-level
        # execute_reconcile_action (testable; see its docstring for the
        # G1 history). This closure only supplies the production deps.
        from .source_first_runtime import (
            forget_analyst_actor_id,
            remember_analyst_actor_id,
        )
        await execute_reconcile_action(
            action,
            proxy_for=_proxy_for,
            state_store=state_store,
            remember_analyst=remember_analyst_actor_id,
            forget_analyst=forget_analyst_actor_id,
            register_a2a_skills=_maybe_register_a2a_skills,
            unregister_a2a_skills=_unregister_a2a_skills,
            unregister_triggers=_unregister_triggers,
        )

    # ---------- shared dependency closures for the resolvers -----------
    #
    # Built once and threaded into both target + analyst resolvers so we
    # don't reconnect Redis / re-instantiate LLM handlers per activation.

    from ..data.config import RedisConfig
    from ..data.filters._contract import FilterContext
    from ..data.redis import RedisStore
    from ..data.registry.credentials import CredentialVault
    from ..data.schemas.analyst import AnalystDescriptor
    from ..data.schemas.target import TargetDescriptor
    from ..data.stack.nlp_service import NlpServiceClient
    from ..data.provenance.verify import _llm_judge_enabled
    from .analyst_deps_builder import (
        AnalystDepsBuildError,
        build_analyst_run_method,
        build_llm_handler_from_stack_component,
        build_search_handler_from_stack_component,
        resolve_judge_route,
    )
    from .audit_checkpointer_wiring import start_audit_checkpointer
    from .budget import BudgetEnforcer
    from .dapr_actors import (
        _AnalystDeps,
        _TargetDeps,
        register_analyst_deps_resolver,
        register_target_deps_resolver,
    )
    from .deps import StandardDeps
    from .embedding_factory import EmbeddingFactoryError
    from .llm_handler_cache import (
        evict_all_llm_handlers,
        evict_llm_handler,
        register_handler_cache,
        registered_cache_labels,
    )
    from .nlp_client_factory import (
        DEFAULT_NLP_COMPONENT_ID,
        LazyNlpClient,
        NlpClientFactoryError,
    )
    from .pipeline import PipelineRunner, build_filter_handler
    from .qdrant_factory import QdrantFactoryError
    from .source_factory import _unwrap_factory_dict, build_source_handler
    from .substrate_singleton_factory import (
        LazyEmbeddingService,
        LazyQdrantClient,
        LazySubstrateQueryPort,
    )
    from .dapr_workflow.client import build_dapr_workflow_client
    from .dapr_workflow.worker import build_workflow_runtime

    # Substrate-side connections shared across all actor activations.
    redis_store = RedisStore(RedisConfig.from_env())
    await redis_store.connect()
    redis_client = redis_store.client

    # Credential vault — shares the pg_pool. LEGBA_DATA_MASTER_KEY must
    # be set in the runtime container's env (compose .env wires it).
    vault = CredentialVault(pg_store)

    async def _secrets_resolve(secret_id: str) -> bytes:
        return await vault.resolve(secret_id)

    # NATS publish closure for substrate-write events. The actors stamp
    # subjects per L-191 (``analyst.<id>.<channel>`` / ``target.<id>.<channel>``).
    async def _nats_publish(subject: str, payload: bytes) -> None:
        # K-1 finding (2026-05-29): NatsStore exposes `publish_json` but
        # not `publish`. The signature on publish_json takes raw bytes
        # despite the name — the actor's analyst publish path already
        # canonical_json-encodes the dict before calling. Bridge here
        # so all actors actually emit on substrate-write events instead
        # of silently AttributeError'ing.
        #
        # DQ-C2 (2026-06-21): `legba.alerts.*` is an interest-only fan-out
        # with NO JetStream stream by design, so publish_json (which awaits a
        # stream ack) raises NoStreamResponseError and EVERY alert was silently
        # dropped (alert_sink_deliveries delivered 0). Route alert subjects to
        # core publish; durable substrate-write subjects (analyst.* / target.*
        # / legba.signals.*) keep JetStream.
        #
        # D1 (W4 remediation): the STIX output sink emits on
        # `legba.outputs.stix.<target_id>` (stix_bundle.NATS_SUBJECT_PATTERN),
        # which — exactly like alerts — is an interest-only TAXII fan-out with
        # NO JetStream stream provisioned. It therefore hit the same
        # NoStreamResponseError ("no-stream") and every STIX bundle was silently
        # dropped. Mirror the alert sink: route the STIX output subjects to core
        # publish too (no new stream). Durable substrate-write subjects keep
        # JetStream.
        if subject.startswith("legba.alerts.") or subject.startswith(
            "legba.outputs.stix."
        ):
            await nats_store.publish_core(subject, payload)
        else:
            await nats_store.publish_json(subject, payload)

    # Shared outbound HTTP client for emit-capable output kinds (webhook /
    # stix_bundle's TAXII 2.1 push). One long-lived client so connection
    # pooling is reused across runs; closed on shutdown via stop(). Threaded
    # through StandardDeps.extras so the analyst-run dispatch can lift it onto
    # the per-emit OutputDeps.http port without widening the StandardDeps shape.
    # The transport is the same SSRF egress guard the ingress fetchers use:
    # a webhook descriptor's ``cfg.url`` (or a TAXII push target) is operator-
    # supplied and could point at 127.0.0.1 / 169.254.169.254 / RFC-1918, so we
    # REFUSE non-public targets here exactly as on the acquisition side.
    from legba.data.sources._egress import guarded_async_client

    output_http_client = guarded_async_client(timeout=15.0)

    standard_deps = StandardDeps(
        pg_pool=pg_store.pool,
        nats_publish=_nats_publish,
        secrets_resolve=_secrets_resolve,
        extras={"output_http_client": output_http_client},
    )

    # ---- LLM handler factory --------------------------------------
    # Cache per-component-id so an analyst's repeat run_method calls
    # reuse one configured handler (httpx client opens only on the
    # actor's on_activate per L-102).
    #
    # S-2: the cache is REGISTERED with legba.runtime.llm_handler_cache so the
    # stack-component informer (below) can evict a component_id when the
    # registry publishes a change for it. Before that wiring the cache keyed by
    # component_id alone, never by version and never invalidated, so a
    # stack-component PUT changing timeout/endpoint/model/max_tokens was
    # invisible until a container recreate — 3.5 h of the 2026-08-01 incident
    # went to exactly that (timeout 60→240 PUT at 16:00Z, live at the 19:31Z
    # recreate). The caching win is untouched: with no event, this is still a
    # plain dict hit.
    _llm_handler_cache: dict[str, Any] = {}
    register_handler_cache("dapr_host.llm_handler", _llm_handler_cache)

    async def _llm_handler_factory(component_id: str) -> Any:
        existing = _llm_handler_cache.get(component_id)
        if existing is not None:
            return existing
        handler = await build_llm_handler_from_stack_component(
            component_id,
            registry_client=registry_client,
            secrets_resolve=_secrets_resolve,
        )
        _llm_handler_cache[component_id] = handler
        return handler

    # ---- Search-provider handler factory (R-3d) -------------------
    # The DISCOVERY leg's twin of _llm_handler_factory. Caches a SUCCESS per
    # component id (a search handler holds no persistent client — every query
    # opens its own guarded client — so one instance is freely shareable) and
    # NEVER caches a failure, mirroring the Lazy* holders' #235 lesson: a
    # component registered (or a registry recovered) minutes after boot must
    # heal on the NEXT deps build, not on the next restart.
    #
    # Returns None rather than raising, and that is NOT a silent degradation:
    # the ONLY consumer binds it into ToolContext.search, and `web_search`
    # turns a DECLARED-but-unbound route into a loud `search_provider_unresolved`
    # tool failure that explicitly says NO query was issued. Raising here would
    # instead take the whole analyst's deps build down (return None from the
    # resolver ⇒ the actor never activates), which is a much worse failure for
    # a capability that is additive to the analyst's substrate work.
    _search_handler_cache: dict[str, Any] = {}

    async def _search_handler_factory(component_id: str) -> Any | None:
        existing = _search_handler_cache.get(component_id)
        if existing is not None:
            return existing
        try:
            handler = await build_search_handler_from_stack_component(
                component_id,
                registry_client=registry_client,
                secrets_resolve=_secrets_resolve,
            )
        except Exception as exc:
            logger.error(
                "dapr_host.search_provider.unresolved component=%s err=%s — "
                "the web_search ToolSpec DECLARES this route; every web_search "
                "call will fail loudly with search_provider_unresolved (never "
                "an empty result set) until it resolves. Register the component "
                "(scripts/bringup_register_stack.py) or drop the ref. Retried "
                "on the NEXT analyst deps build.",
                component_id, exc,
            )
            return None
        logger.info(
            "dapr_host.search_provider.bound component=%s subprovider=%s",
            component_id, getattr(handler, "subprovider", "?"),
        )
        _search_handler_cache[component_id] = handler
        return handler

    # ---- Pre-built service clients --------------------------------
    # Construct stack-component-backed clients at bootstrap so filters +
    # analyst kinds don't race or RuntimeError on first activation.

    # W-4 NLP service client — LAZILY resolved; ner_multilingual + classify
    # (and the fact_extractor /extract fallback) filters call into this through
    # _lazy_nlp_client.get() at handler-build time.
    #
    # #91 §2.3: this used to be built ONCE at bootstrap and cached as a possibly-
    # permanent None. Boot-before-seed (the nlp.local.legba_models stack row not
    # yet registered) or a transient models-host outage at boot left the client
    # None/degraded for the WHOLE process lifetime — every later filter build
    # silently fell back to the unenriched path with no way to recover short of
    # a restart. LazyNlpClient resolves on FIRST use and RE-resolves on every
    # subsequent build attempt while no client is cached, so a late seed / a
    # recovered host heals the filter set on the next source-deps resolution. A
    # failed attempt is NEVER cached as a sticky None — the next call retries —
    # and a genuine hard failure still raises loud (NlpClientFactoryError) at
    # the build site rather than degrading silently.
    _lazy_nlp_client = LazyNlpClient(
        registry_client=registry_client,
        secrets_resolve=_secrets_resolve,
        component_id=DEFAULT_NLP_COMPONENT_ID,
    )

    # W-1 / #235 Qdrant client + hosted embedding service — LAZILY resolved.
    # dedupe_tier_3, signal_embedder, cross_source_coalesce/dedup, the
    # grounding RAG, and the SubstrateQueryPort (below) all consume these.
    #
    # #235 (2026-07-23 ~18:54 outage): these used to be built ONCE at
    # bootstrap inside a try/except that swallowed a ConnectError and cached
    # `None` — permanently. A deploy that recreated the registry and the
    # runtime simultaneously raced the ONE-SHOT lookup here against the
    # registry's readiness; the runtime lost the race, pinned both clients
    # None for the rest of the process's lifetime, and — because
    # substrate_query_port (below) was built ONLY when qdrant_client was
    # already non-None at THIS moment — every consult_on_demand activation
    # 502'd "no deps registered for actor_id" for ~3h until an operator
    # restarted the runtime. LazyQdrantClient / LazyEmbeddingService mirror
    # LazyNlpClient exactly (#91 §2.3, just above): resolve on FIRST use,
    # RE-resolve on every subsequent call while unresolved (backed off to at
    # most once per DEFAULT_RETRY_COOLDOWN_SECONDS so a burst of callers
    # during an outage doesn't hammer the registry), cache a SUCCESS
    # permanently, NEVER cache a FAILURE. Every consumer below reads through
    # `.get()` (or a resolved-per-deps-build snapshot pulled from `.get()`)
    # rather than a closed-over boot-time local, so a registry that recovers
    # moments after this function returns heals the vector plane on the next
    # USE — no restart required.
    _lazy_qdrant_client = LazyQdrantClient(
        registry_client=registry_client,
        component_id=os.environ.get(
            "LEGBA_DATA_DEFAULT_VECTOR_STORE", "vector.qdrant.cluster_main",
        ),
    )
    _lazy_embedding_service = LazyEmbeddingService(
        registry_client=registry_client,
        secrets_resolve=_secrets_resolve,
        component_id=os.environ.get(
            "LEGBA_DATA_DEFAULT_EMBEDDING", "embed.primary.openai_compat",
        ),
    )

    async def _resolve_qdrant_client() -> Any | None:
        """Best-effort snapshot of the qdrant client for THIS deps build.

        Never raises — an unresolved/unreachable vector plane degrades the
        caller (dedupe_tier_3 refuses loud at filter-build time per its own
        contract; every other consumer treats ``None`` as "vector plane
        absent this run" and falls back to its documented unavailable
        shape). The point of going through ``.get()`` here (rather than
        reading a boot-time local) is that EVERY call retries while
        unresolved, so a recovered registry heals on the next analyst/source
        deps build without a restart.
        """
        try:
            return await _lazy_qdrant_client.get()
        except QdrantFactoryError as exc:
            logger.warning(
                "dapr_host.qdrant_client.unavailable err=%s "
                "(attempt=%d; dedupe_tier_3 + vector-backed reads degrade "
                "this build — retried on the NEXT deps resolution)",
                exc, _lazy_qdrant_client.attempt_count,
            )
            return None

    async def _resolve_embedding_service() -> Any | None:
        """Best-effort snapshot of the embedding client for THIS deps build.

        Mirrors :func:`_resolve_qdrant_client` — never raises, retried on
        every subsequent deps build while unresolved.
        """
        try:
            return await _lazy_embedding_service.get()
        except EmbeddingFactoryError as exc:
            logger.warning(
                "dapr_host.embedding_service.unavailable err=%s "
                "(attempt=%d; dedupe_tier_3 + semantic-correlator + "
                "grounding-RAG analysts degrade this build — retried on "
                "the NEXT deps resolution)",
                exc, _lazy_embedding_service.attempt_count,
            )
            return None

    # Resolve ONCE now so boot behaviour is unchanged when the registry is
    # reachable (the common case — this succeeds on the first try exactly as
    # the old eager build did) and so `standard_deps.extras` starts wired.
    # `_analyst_deps_resolver` (below) re-resolves + re-stamps `extras` on
    # EVERY analyst deps build (cache miss), so a failure here is not sticky.
    qdrant_client = await _resolve_qdrant_client()
    embedding_service = await _resolve_embedding_service()

    # Thread the substrate-wide vector ports onto the analyst deps bundle so
    # deterministic-kind analysts that coalesce/correlate over embeddings can
    # reach them (P2 cross_source_coalesce reuses these; cross_source_dedup's
    # best-effort semantic pass also reads deps.extras['qdrant']). The bundle's
    # ``extras`` is the same dict object every deterministic-kind actor's
    # cached `kind_deps` refers to (mutated in place, not replaced) — so a
    # LATER successful lazy resolution (stamped by `_analyst_deps_resolver`
    # on its next call, per #235) is visible to an ALREADY-cached actor's next
    # scheduled run too, not just to brand-new actor activations. Both may be
    # None on this first attempt — the coalesce handler degrades-not-drops +
    # refuses loud (SEAM #19) when a required port is missing.
    if isinstance(standard_deps.extras, dict):
        standard_deps.extras["qdrant"] = qdrant_client
        standard_deps.extras["embedding_service"] = embedding_service

    # Optimizer durable-execution client — Dapr Workflow (replaces Temporal,
    # P-16 / L-205). The in-process WorkflowRuntime worker registers the GEPA
    # optimizer workflow + activities against the Dapr sidecar; the client is
    # passed into build_analyst_run_method's optimizer slot (the field is still
    # named ``temporal_client`` — it's the durable-workflow handle slot, now
    # backed by Dapr). Graceful: a None client (dapr.ext.workflow absent or
    # LEGBA_OPTIMIZER_IN_PROCESS=1) keeps the optimizer's in-process GEPA
    # fallback, and a worker-start failure never sinks the host.
    temporal_client: Any | None = None   # slot retained: OptimizerDeps field name
    deep_consult_client: Any | None = None  # anchor §5 PIECE 4 — detached submit
    workflow_runtime: Any | None = None
    try:
        temporal_client = build_dapr_workflow_client()
        # Deep-consult detached-submit client rides the SAME daprd sidecar gRPC
        # channel + the SAME embedded WorkflowRuntime (which now registers BOTH
        # workflows). Env-gated identically; None when dapr.ext.workflow absent.
        from .dapr_workflow.deep_consult_client import (
            build_deep_consult_workflow_client,
        )
        deep_consult_client = build_deep_consult_workflow_client()
        if deep_consult_client is not None:
            logger.info("dapr_host.deep_consult_workflow.ready (detached-submit client built)")
        else:
            logger.info(
                "dapr_host.deep_consult_workflow.unavailable "
                "(dapr.ext.workflow absent; deep_consult submit will 503)",
            )
        if temporal_client is not None:
            # Execute the GEPA workflow in-process ONLY when no external worker
            # owns it. The dspy GEPA worker (docker/Dockerfile.worker, compose
            # service legba-dapr-workflow-worker) runs the compile off-box on a
            # dspy-bearing image; the runtime image is deliberately dspy-free,
            # so an in-process compile here can only ever fall back to the naive
            # candidate search. Set LEGBA_EMBED_WORKFLOW_WORKER=0 when the
            # external worker is running so the activity lands there instead.
            # Default "1" keeps the embedded worker (safe when no external
            # worker is deployed — the optimizer still produces candidates,
            # just via the naive path).
            if os.environ.get("LEGBA_EMBED_WORKFLOW_WORKER", "1") != "0":
                workflow_runtime = build_workflow_runtime()
                workflow_runtime.start()
                logger.info("dapr_host.optimizer_workflow.ready (embedded Dapr Workflow worker started)")
            else:
                logger.info(
                    "dapr_host.optimizer_workflow.dispatch_only "
                    "(LEGBA_EMBED_WORKFLOW_WORKER=0 — external dspy worker executes the GEPA compile)",
                )
        else:
            logger.info(
                "dapr_host.optimizer_workflow.in_process "
                "(dapr.ext.workflow absent or in-process mode; optimizer uses in-process GEPA)",
            )
    except Exception as exc:
        logger.warning(
            "dapr_host.optimizer_workflow.unavailable err=%s "
            "(optimizer falls back to in-process GEPA; other kinds OK)", exc,
        )
        temporal_client = None
        deep_consult_client = None
        workflow_runtime = None

    # W-3 / #235 SubstrateQueryPort — pg+qdrant backing for consult_on_demand.
    #
    # #235: this used to be built ONCE, gated on `qdrant_client is not None`
    # AT THIS EXACT MOMENT — the same one-shot snapshot problem as the
    # qdrant/embedding clients above (and the direct cause of the outage:
    # `qdrant_client` was `None` here, so the port was never built, so every
    # consult_on_demand activation failed closed for ~3h). `LazySubstrateQueryPort`
    # re-attempts qdrant resolution (via `_lazy_qdrant_client`, already lazy)
    # on every `.get()` until it succeeds, THEN constructs the port once —
    # threaded (not the possibly-None snapshot) into `_analyst_deps_resolver`
    # below so a registry that recovers seconds after this function returns
    # still heals consult_on_demand on the actor's next activation.
    from ..data.config import QdrantConfig
    _qdrant_cfg = QdrantConfig.from_env()
    # Stage 1 — the OpenSearch full-text corpus store for search_corpus /
    # read_document. Built GUARDED (opensearch-py may be absent, or the
    # single-node index unprovisioned); None keeps the honest
    # no_corpus_wired fallback (mirrors the embedder's seam #11 contract).
    # Unlike qdrant/embedding this is not registry-backed (OpenSearchStore.
    # from_env() reads only local env vars), so it isn't subject to the same
    # boot-race and doesn't need its own lazy holder.
    try:
        from ..data.opensearch import OpenSearchStore
        _os_store = OpenSearchStore.from_env()
    except Exception:
        _os_store = None

    _lazy_substrate_query_port = LazySubstrateQueryPort(
        pg_pool=pg_store.pool,
        qdrant_holder=_lazy_qdrant_client,
        embedding_holder=_lazy_embedding_service,
        world_context_collection=_qdrant_cfg.world_context_collection,
        tradecraft_collection=_qdrant_cfg.tradecraft_collection,
        opensearch_store=_os_store,
    )

    async def _resolve_substrate_query_port() -> Any | None:
        """Best-effort snapshot of the substrate port for THIS deps build.

        Never raises — mirrors :func:`_resolve_qdrant_client`. A consumer
        that receives ``None`` here (``consult_on_demand``'s deps builder)
        already fails loud on ``None`` per its own no-stubs contract
        (:class:`AnalystDepsBuildError`); the retry lives in re-attempting
        THIS resolution on the actor's next activation, not in silently
        downgrading consult's own port-required contract.
        """
        try:
            return await _lazy_substrate_query_port.get()
        except QdrantFactoryError as exc:
            logger.warning(
                "dapr_host.substrate_query_port.unavailable err=%s "
                "(attempt=%d; consult_on_demand activations fail loud this "
                "build — retried on the NEXT deps resolution)",
                exc, _lazy_substrate_query_port.attempt_count,
            )
            return None

    # Resolve ONCE now — unchanged boot behaviour when the registry answers
    # on the first try. `_analyst_deps_resolver` re-resolves on every
    # analyst deps build (cache miss) via the SAME holder, so a first-attempt
    # failure here is not sticky.
    substrate_query_port: Any | None = await _resolve_substrate_query_port()
    if substrate_query_port is None:
        logger.warning(
            "dapr_host.substrate_query_port.unavailable "
            "(no qdrant_client; consult_on_demand uninstalliable this build)",
        )

    # #235 — durable visibility for "consult_on_demand is uninstallable
    # because the substrate port is absent". Before this, the ONLY trace of
    # the 2026-07-23 outage was a per-request 502 + an
    # `analyst_deps_resolver.build_failed` log line — nothing durable, so
    # "is consult broken right now" was unanswerable without live-tailing
    # runtime logs (exactly the operator-visibility gap the liveness
    # watchdog's B0-12 fix closed for pipeline stalls). Mirrors
    # liveness_watchdog._record_stall_delivery: ONE `alert_sink_deliveries`
    # row (status='logged_only' — recorded, not externally delivered) per
    # BOOT, not per request — a 502 storm during an outage must not spam the
    # table. The in-process flag resets on every restart (intentional: a
    # restart is itself operator-visible + a fresh boot deserves a fresh
    # signal if the condition recurs).
    _consult_uninstallable_alerted = False

    async def _record_consult_uninstallable_once(detail: str) -> None:
        nonlocal _consult_uninstallable_alerted
        if _consult_uninstallable_alerted:
            return
        _consult_uninstallable_alerted = True
        try:
            async with pg_store.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO alert_sink_deliveries (
                        channel_name, sink_kind, sink_target, severity,
                        attempt_number, status, payload_summary
                    ) VALUES ($1, $2, $3, $4, 1, $5, $6::jsonb)
                    """,
                    "consult_uninstallable",   # channel_name
                    "runtime",                 # sink_kind
                    "operator",                # sink_target
                    "high",                    # severity
                    "logged_only",             # status: durable, not externally delivered
                    json.dumps(
                        {
                            "kind": "consult_uninstallable",
                            "detail": detail[:2000],
                        },
                        separators=(",", ":"),
                    ),
                )
            logger.error(
                "dapr_host.consult_uninstallable.recorded detail=%s", detail,
            )
        except Exception as exc:  # pragma: no cover — never crash the resolver
            logger.warning(
                "dapr_host.consult_uninstallable.record_failed err=%s", exc,
            )

    # L-175 — tools registry for analyst kinds with a tools_whitelist.
    # The mapping is name → async callable taking the LLM-emitted args
    # dict and returning a JSON-serializable result.  The L-175 critic
    # kind's ReAct loop dispatches against it.
    #
    # L-211 mnemosyne_trust_query — we wire it lazily: callers must
    # supply ``LEGBA_MNEMOSYNE_BASE_URL`` and the runtime's instance
    # signing key (already used by the A2A server).  When either is
    # absent we omit the tool from the registry so descriptors that
    # whitelist it surface a clean "tool unresolved" warning rather
    # than crashing.  The tool itself returns the
    # ``{"error": "transport_error"}`` shape when Mnemosyne is
    # unreachable, so the analyst loop sees a usable result either way.
    tools_registry: dict[str, Any] = {}
    try:
        from ..data.tools import (
            MNEMOSYNE_TRUST_QUERY_TOOL_NAME,
            MnemosyneTrustQueryDeps,
            mnemosyne_trust_query,
        )
        mnemosyne_base_url = os.environ.get("LEGBA_MNEMOSYNE_BASE_URL", "").strip()
        if mnemosyne_base_url:
            from ..ui.agent_card import (
                get_instance_signing_key,
                public_key_to_did,
            )
            import httpx as _httpx
            signing_key = get_instance_signing_key()
            if signing_key is None:
                logger.warning(
                    "dapr_host.tools_registry.mnemosyne_skipped "
                    "(instance signing key unavailable)"
                )
            else:
                signer_did = public_key_to_did(bytes(signing_key.verify_key))
                mnemosyne_http_client = _httpx.AsyncClient(timeout=10.0)
                mnemosyne_deps = MnemosyneTrustQueryDeps(
                    base_url=mnemosyne_base_url,
                    signing_key=signing_key,
                    signer_did=signer_did,
                    http_client=mnemosyne_http_client,
                )

                async def _mnemosyne_trust_query_bound(args: dict[str, Any]) -> dict[str, Any]:
                    return await mnemosyne_trust_query(args, mnemosyne_deps)

                tools_registry[MNEMOSYNE_TRUST_QUERY_TOOL_NAME] = (
                    _mnemosyne_trust_query_bound
                )
                logger.info(
                    "dapr_host.tools_registry.bound tool=%s base_url=%s",
                    MNEMOSYNE_TRUST_QUERY_TOOL_NAME, mnemosyne_base_url,
                )
        else:
            logger.info(
                "dapr_host.tools_registry.mnemosyne_skipped "
                "(LEGBA_MNEMOSYNE_BASE_URL unset)"
            )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "dapr_host.tools_registry.mnemosyne_unavailable err=%s", exc,
        )

    # P-2 Audit checkpointer — per-analyst chain-head Ed25519 signing
    # against `audit_checkpoints`. Mnemosyne D5 alignment.
    audit_checkpointer = await start_audit_checkpointer(pg_store.pool)
    logger.info("dapr_host.audit_checkpointer.started")

    # ---- deps resolvers --------------------------------------------
    def _parse_actor_id(actor_id: str) -> tuple[str, str] | None:
        parts = actor_id.split("::", 2)
        if len(parts) < 2:
            return None
        return parts[0], parts[1]

    def _unwrap_descriptor_config(raw: Mapping[str, Any]) -> dict[str, Any]:
        """Recursively unwrap FactoryValue dicts in a config blob.

        Descriptor bodies serialize ``{"raw": ..., "factory_kind": ...}``
        for every config field. The handler builders want plain values.
        """
        out: dict[str, Any] = {}
        for k, v in raw.items():
            if isinstance(v, dict) and "factory_kind" in v:
                inner = v.get("raw")
                if isinstance(inner, dict):
                    out[k] = _unwrap_descriptor_config(inner)
                else:
                    out[k] = inner
            elif isinstance(v, dict):
                out[k] = _unwrap_descriptor_config(v)
            else:
                out[k] = v
        return out

    async def _target_deps_resolver(actor_id: str) -> "_TargetDeps | None":
        parsed = _parse_actor_id(actor_id)
        if parsed is None or parsed[0] != "target":
            return None
        descriptor_id = parsed[1]
        try:
            typed = await registry_client.get_descriptor_typed(
                descriptor_id, family="target",
            )
        except Exception as exc:                                # pragma: no cover
            logger.error(
                "target_deps_resolver.fetch_failed actor_id=%s err=%s",
                actor_id, exc,
            )
            return None
        if typed is None:
            return None
        try:
            # strict=False mirrors descriptor.get_typed — the typed
            # endpoint returns model_dump(mode="json") so re-validation
            # needs the same coercion path the registry uses internally.
            td = TargetDescriptor.model_validate(typed, strict=False)
        except Exception as exc:                                # pragma: no cover
            logger.error(
                "target_deps_resolver.parse_failed actor_id=%s err=%s",
                actor_id, exc,
            )
            return None

        # Source-first (L-205 / B5): the legacy target-owned source +
        # pipeline factories were retired with the TargetActor pull loop —
        # SourceActor owns acquisition and signals are target-agnostic, so a
        # TargetActor's deps are just its descriptor + the StandardDeps bundle.
        return _TargetDeps(
            descriptor=td,
            deps=standard_deps,
        )

    async def _analyst_deps_resolver(actor_id: str) -> "_AnalystDeps | None":
        parsed = _parse_actor_id(actor_id)
        if parsed is None or parsed[0] != "analyst":
            return None
        descriptor_id = parsed[1]
        try:
            typed = await registry_client.get_descriptor_typed(
                descriptor_id, family="analyst",
            )
        except Exception as exc:                                # pragma: no cover
            logger.error(
                "analyst_deps_resolver.fetch_failed actor_id=%s err=%s",
                actor_id, exc,
            )
            return None
        if typed is None:
            return None
        try:
            ad = AnalystDescriptor.model_validate(typed, strict=False)
        except Exception as exc:                                # pragma: no cover
            logger.error(
                "analyst_deps_resolver.parse_failed actor_id=%s err=%s",
                actor_id, exc,
            )
            return None

        # #235: re-resolve the lazy qdrant/embedding/substrate-port singletons
        # on EVERY deps build (this resolver runs on every actor cache-miss —
        # a brand-new actor, an actor evicted by a descriptor-version bump, or
        # the FIRST activation attempt after a boot race). Shadowing the outer
        # `qdrant_client` / `embedding_service` / `substrate_query_port` names
        # here means every site below this point (the consult/GATHER agency
        # bindings' ToolContext.substrate, the grounding hook's embedder/qdrant,
        # and the build_analyst_run_method kwargs) sees the FRESH snapshot for
        # THIS build, not the frozen boot-time value — the direct fix for the
        # 2026-07-23 outage (a consult actor that failed to activate during the
        # race gets a real port on its NEXT activation attempt, no restart).
        # Also re-stamp `standard_deps.extras` in place (same dict object every
        # already-cached deterministic-kind actor's `kind_deps` refers to), so
        # a recovered vector plane reaches cross_source_coalesce/dedup + the
        # signal_embedder sweep on their next scheduled tick too.
        qdrant_client = await _resolve_qdrant_client()
        embedding_service = await _resolve_embedding_service()
        substrate_query_port = await _resolve_substrate_query_port()
        if isinstance(standard_deps.extras, dict):
            standard_deps.extras["qdrant"] = qdrant_client
            standard_deps.extras["embedding_service"] = embedding_service

        # A-3a (review G2): the consult kind's tool calls route through the
        # agency plane. Build the substrate_read binding BEFORE the kind
        # build — for consult it is mandatory (fail loud, never a silent
        # ungoverned bypass: that silent-bypass shape is exactly how the
        # review found the agency plane orphaned).
        consult_agency_binding = None
        if ad.identity.kind == "consult_on_demand":
            from ..data.analysts.agency.binding import (
                AgencyToolBinding,
                GLOBAL_SCOPE,
                fetch_action_pack,
            )
            from ..data.analysts.agency.substrate_read import (
                SUBSTRATE_READ_PACK_ID,
            )
            from .source_first_runtime import AGENCY_HOLDER

            agency = AGENCY_HOLDER.get("agency")
            base_ctx = AGENCY_HOLDER.get("tool_context")
            pack = await fetch_action_pack(registry_client, SUBSTRATE_READ_PACK_ID)
            if agency is None or base_ctx is None or pack is None:
                logger.error(
                    "analyst_deps_resolver.consult_agency_unavailable "
                    "actor_id=%s agency=%s ctx=%s pack=%s — register the "
                    "substrate_read action pack (bringup_register_action_packs) "
                    "and bring up the agency plane before binding consult",
                    actor_id, agency is not None, base_ctx is not None,
                    pack is not None,
                )
                return None
            from ..data.analysts.agency.tools import ToolContext
            from ..data.schemas.action_pack import ActionPackRef

            consult_agency_binding = AgencyToolBinding(
                agency=agency,
                pack=pack,
                pg_pool=pg_store.pool,
                tool_context=ToolContext(
                    queue=base_ctx.queue,
                    emit=base_ctx.emit,
                    substrate=substrate_query_port,
                ),
                analyst_grants=[
                    r.model_dump() if hasattr(r, "model_dump") else r
                    for r in (ad.action_packs or [])
                ],
                # The global consult surface has no target in context; the
                # read-only pack is allowed by construction (documented in
                # the pack descriptor). Target-bound packs (escalate etc.)
                # resolve their allow leg per run instead.
                target_allows=[ActionPackRef(pack_id=SUBSTRATE_READ_PACK_ID)],
                scope=GLOBAL_SCOPE,
                requested_by=f"analyst::{ad.identity.id}",
                budget_account=ad.identity.id,
            )

        # S5: the agentic inline_target GATHER binding. Built iff the analyst is
        # an inline_target assessor AND it grants `substrate_read` via
        # action_packs (the grant leg of the three-way gate). target_allows is
        # left None here — the run path re-points it to the RUNNING target's
        # allow-list per run (mirroring the escalation binding), so GATHER
        # engages ONLY when the (assessor, target) read pack is EFFECTIVE. A
        # granted-but-unbindable pack is FAIL-LOUD (return None) — the same
        # silent-bypass guard the consult/escalation legs use.
        gather_binding = None
        from ..data.analysts.agency.binding import grants_include as _grants_include
        from ..data.analysts.agency.substrate_read import (
            SUBSTRATE_READ_PACK_ID,
        )

        _inline_grant_dicts = [
            r.model_dump() if hasattr(r, "model_dump") else r
            for r in (ad.action_packs or [])
        ]
        # §4.9 — the agentic GATHER binding was hard-gated on
        # `kind == "inline_target"` + the hardcoded SUBSTRATE_READ_PACK_ID.
        # Generalize to a CAPABILITY check (method.kind == llm_planner over the
        # in-actor GATHER kind set) + the DESCRIPTOR's declared read pack, so a
        # new llm_planner kind (journal_assessor → journal_read) gets its tools
        # instead of silently running with none. `inline_target` keeps
        # substrate_read as its default read pack (back-compat).
        _gather_read_pack_id = _gather_read_pack_for(ad, SUBSTRATE_READ_PACK_ID)
        if _gather_kind_engages(ad) and _gather_read_pack_id is not None and (
            _grants_include(_inline_grant_dicts, _gather_read_pack_id)
        ):
            from ..data.analysts.agency.binding import (
                AgencyToolBinding,
                fetch_action_pack,
            )
            from ..data.analysts.agency.tools import ToolContext
            from .source_first_runtime import AGENCY_HOLDER

            g_agency = AGENCY_HOLDER.get("agency")
            g_base_ctx = AGENCY_HOLDER.get("tool_context")
            g_pack = await fetch_action_pack(
                registry_client, _gather_read_pack_id
            )
            if g_agency is None or g_base_ctx is None or g_pack is None:
                logger.error(
                    "analyst_deps_resolver.gather_unbindable actor_id=%s "
                    "kind=%s read_pack=%s agency=%s ctx=%s pack=%s — the "
                    "assessor grants its read pack; register the pack "
                    "(bringup_register_action_packs) and bring up the agency "
                    "plane, or remove the grant",
                    actor_id, ad.identity.kind, _gather_read_pack_id,
                    g_agency is not None, g_base_ctx is not None,
                    g_pack is not None,
                )
                return None
            gather_binding = AgencyToolBinding(
                agency=g_agency,
                pack=g_pack,
                pg_pool=pg_store.pool,
                tool_context=ToolContext(
                    queue=g_base_ctx.queue,
                    emit=g_base_ctx.emit,
                    substrate=substrate_query_port,
                ),
                analyst_grants=_inline_grant_dicts,
                # Re-pointed per run from the target's allowed_action_packs.
                target_allows=None,
                requested_by=f"analyst::{ad.identity.id}",
                budget_account=ad.identity.id,
            )

        # SEAM #22: the external (web_access) + write-back (propose_facts) GATHER
        # bindings. Built ONLY for an inline_target assessor that ALSO grants the
        # pack via `action_packs` (grant leg) — AND only when the base
        # substrate_read GATHER is itself wired (gather_binding is not None), so a
        # write/web grant is never bound without the read loop that drives it.
        # Each binding carries the assessor-constant legs; the actor re-points the
        # allow leg per run and, for the write pack, injects the per-run
        # WritebackContext (copy-on-write). A granted-but-unbindable pack is
        # FAIL-LOUD (return None) — the same silent-bypass guard the other legs
        # use. pg_pool is threaded onto each ToolContext so the write tools have
        # the connection source the WritebackContext needs (seam22-2).
        # §4.9 — generalized to the GATHER kind set (was `== "inline_target"`).
        # The journal_assessor is read-only in Wave 0 (it grants no web/write
        # pack), so this inner loop is a no-op for it; the gate stays open for any
        # llm_planner GATHER kind that DOES grant web_access/propose_facts later.
        gather_write_bindings: dict[str, Any] | None = None
        if gather_binding is not None and _gather_kind_engages(ad):
            from ..data.analysts.agency.binding import (
                AgencyToolBinding as _GWBinding,
                fetch_action_pack as _gw_fetch,
            )
            from ..data.analysts.agency.tools import ToolContext as _GWToolContext
            from ..data.analysts.agency.web_tools import (
                WEB_ACCESS_PACK_ID,
                WEB_ACCESS_TOOLS,
            )
            from ..data.analysts.agency.journal_propose import (
                JOURNAL_PROPOSE_PACK_ID,
                JOURNAL_PROPOSE_TOOLS,
            )
            from ..data.analysts.agency.write_tools import (
                WRITE_PACK_ID,
                WRITE_TOOLS,
            )
            from .source_first_runtime import AGENCY_HOLDER as _GW_HOLDER

            _gw_agency = _GW_HOLDER.get("agency")
            _gw_base_ctx = _GW_HOLDER.get("tool_context")
            _bindings: dict[str, Any] = {}
            _web_fragments: list[str] | None = None
            _write_fragments: list[str] | None = None
            # Each write/web pack the GATHER kind grants. journal_propose (plan §7
            # / Wave 4) is a WRITE pack like propose_facts — each tool writes ONLY
            # a pending journal_proposals row, so it needs the per-run
            # WritebackContext injection (_is_write=True) exactly like propose_facts
            # (the connection source + run identity; NO provenance writer). A pack
            # the analyst does not grant is skipped (`_grants_include` below).
            for _pack_id, _tool_names, _is_write in (
                (WEB_ACCESS_PACK_ID, WEB_ACCESS_TOOLS, False),
                (WRITE_PACK_ID, WRITE_TOOLS, True),
                (JOURNAL_PROPOSE_PACK_ID, JOURNAL_PROPOSE_TOOLS, True),
            ):
                if not _grants_include(_inline_grant_dicts, _pack_id):
                    continue
                _pack = await _gw_fetch(registry_client, _pack_id)
                if _gw_agency is None or _gw_base_ctx is None or _pack is None:
                    logger.error(
                        "analyst_deps_resolver.gather_write_unbindable "
                        "actor_id=%s pack=%s agency=%s ctx=%s pack_loaded=%s — "
                        "the assessor grants %s; register the pack "
                        "(bringup_register_action_packs) and bring up the agency "
                        "plane, or remove the grant",
                        actor_id, _pack_id, _gw_agency is not None,
                        _gw_base_ctx is not None, _pack is not None, _pack_id,
                    )
                    return None
                # R-3d — THE SEARCH BINDING. `ToolContext.search` /
                # `search_route` were DECLARED by R-3b and bound by nothing,
                # which is what kept the discovery leg inert: `web_search`
                # could not reach a provider at all. Resolve the pack's own
                # `web_search` ToolSpec `config.provider` StackRef through the
                # SAME ladder the tool documents (`resolve_tool_search_route`,
                # which also honours the LEGBA_SEARCH_STACK_REF global repoint)
                # and bind the configured handler here, next to the other
                # stack-backed capabilities (substrate port, LLM handlers).
                #
                # Rung 0 of that ladder is an OPT-IN GATE: no `provider` key on
                # the ToolSpec ⇒ no route ⇒ nothing bound ⇒ `web_search` falls
                # through to the legacy operator-pinned endpoint exactly as
                # before. A DECLARED route we could not build binds None, and
                # the tool then fails LOUDLY (`search_provider_unresolved`,
                # "NO query was issued") rather than returning zero hits — an
                # unresolved provider and an empty web must never share a wire
                # shape. Re-resolved on every deps build, so an operator PUT
                # that adds the ref takes effect on the actor's next build.
                _search_handler = None
                _search_route = None
                if _pack_id == WEB_ACCESS_PACK_ID:
                    from ..data.stack.search import (
                        resolve_tool_search_route as _resolve_search_route,
                    )

                    _ws_cfg = next(
                        (dict(_t.config) for _t in _pack.tools
                         if _t.name == "web_search"),
                        {},
                    )
                    _search_route = _resolve_search_route(_ws_cfg)
                    if _search_route is not None:
                        _search_handler = await _search_handler_factory(
                            _search_route.component_id
                        )
                        logger.info(
                            "analyst_deps_resolver.search_route actor_id=%s "
                            "component=%s source=%s bound=%s",
                            actor_id, _search_route.component_id,
                            _search_route.source, _search_handler is not None,
                        )
                # The base ToolContext carries the read substrate + queue/emit;
                # the per-run WritebackContext (write pack) is injected by the
                # actor, NOT pinned here (it needs the run's AnalystContext).
                _ctx = _GWToolContext(
                    queue=_gw_base_ctx.queue,
                    emit=_gw_base_ctx.emit,
                    substrate=substrate_query_port,
                    search=_search_handler,
                    search_route=_search_route,
                )
                _binding = _GWBinding(
                    agency=_gw_agency,
                    pack=_pack,
                    pg_pool=pg_store.pool,
                    tool_context=_ctx,
                    analyst_grants=_inline_grant_dicts,
                    target_allows=None,  # re-pointed per run from the target
                    requested_by=f"analyst::{ad.identity.id}",
                    budget_account=ad.identity.id,
                )
                for _tn in _tool_names:
                    _bindings[_tn] = _binding
                _frags = [str(f) for f in (_pack.prompt_fragments or [])]
                _frags += [str(r) for r in (_pack.rules or [])]
                if _is_write:
                    _write_fragments = _frags
                else:
                    _web_fragments = _frags
            if _bindings:
                gather_write_bindings = {
                    "bindings": _bindings,
                    "web_fragments": _web_fragments,
                    "write_fragments": _write_fragments,
                }

        try:
            (
                run_method,
                kind_deps,
                output_kind,
                receipt_chain,
                read_slice,
            ) = await build_analyst_run_method(
                ad,
                deps=standard_deps,
                registry_client=registry_client,
                pg_pool=pg_store.pool,
                llm_handler_factory=_llm_handler_factory,
                temporal_client=temporal_client,    # W-2
                deep_consult_client=deep_consult_client,  # anchor §5 PIECE 4
                substrate_query_port=substrate_query_port,  # W-3
                embedding_service=embedding_service,  # L-114 — grounding embedder
                qdrant_client=qdrant_client,  # S5-T3 — vector:world_context RAG
                nlp_client=_lazy_nlp_client,  # reenrich_ner — NER-backfill sweep source
                tools_registry=tools_registry,      # L-175
                consult_agency_binding=consult_agency_binding,
                # S5: the GATHER binding is passed so the runner engages the
                # bounded tool-call phase. Default-off (None) when not granted.
                inline_target_agency_binding=gather_binding,
            )
        except AnalystDepsBuildError as exc:
            logger.error(
                "analyst_deps_resolver.build_failed actor_id=%s err=%s",
                actor_id, exc,
            )
            # #235: the specific "consult_on_demand is uninstallable because
            # the substrate port is absent" case deserves a DURABLE, once-
            # per-boot operator signal — every other AnalystDepsBuildError
            # (a malformed LLM ref, an unknown kind, ...) is an operator-
            # authored descriptor problem the existing ERROR log already
            # covers, so we scope this narrowly rather than durably
            # recording every deps-build failure.
            if ad.identity.kind == "consult_on_demand" and substrate_query_port is None:
                await _record_consult_uninstallable_once(
                    f"actor_id={actor_id} err={exc}"
                )
            return None

        # Pull budget params from the descriptor's method block. Default
        # to None (unbounded) when the field is unset — BudgetEnforcer
        # treats None as "no envelope check."
        method_body = ad.method.model_dump() if ad.method else {}
        tokens_per_day = method_body.get("budget_tokens_per_day")
        # A-5/G5: real provider+model (PRICE_TABLE dispatch keys, so USD
        # cost observability works) + a real per-run token estimate (so the
        # forward-looking throttle is reachable). Best-effort resolver —
        # degrades to ("", "", est) rather than blocking deps build.
        from .analyst_deps_builder import resolve_llm_budget_params

        provider, model, estimated_tokens = await resolve_llm_budget_params(
            ad, registry_client=registry_client,
        )
        budget = BudgetEnforcer(
            analyst_id=ad.identity.id,
            analyst_version=ad.identity.version,
            budget_tokens_per_day=tokens_per_day,
            provider=provider,
            model=model,
            estimated_tokens_per_run=estimated_tokens,
        )

        # A-3c: bind the escalate_finding pack iff the analyst grants it.
        # A declared grant that cannot bind is FAIL-LOUD (return None →
        # activation refuses) — silently skipping a granted capability is
        # the exact silent-bypass shape the review's G2 documented.
        escalation = None
        from ..data.analysts.agency.binding import grants_include

        grant_dicts = [
            r.model_dump() if hasattr(r, "model_dump") else r
            for r in (ad.action_packs or [])
        ]
        if grants_include(grant_dicts, "escalate_finding"):
            from ..data.analysts.agency.binding import (
                AgencyToolBinding,
                EscalationBinding,
                fetch_action_pack,
                resolve_escalation_action,
            )
            from .source_first_runtime import AGENCY_HOLDER

            esc_agency = AGENCY_HOLDER.get("agency")
            esc_ctx = AGENCY_HOLDER.get("tool_context")
            esc_pack = await fetch_action_pack(registry_client, "escalate_finding")
            if esc_agency is None or esc_ctx is None or esc_pack is None:
                logger.error(
                    "analyst_deps_resolver.escalation_unbindable actor_id=%s "
                    "agency=%s ctx=%s pack=%s — the analyst grants "
                    "escalate_finding; register the pack "
                    "(bringup_register_action_packs) and bring up the agency "
                    "plane, or remove the grant",
                    actor_id, esc_agency is not None, esc_ctx is not None,
                    esc_pack is not None,
                )
                return None
            esc_tool_cfg: dict[str, Any] = {}
            for t in esc_pack.tools:
                if t.name == "escalate":
                    esc_tool_cfg = dict(t.config or {})
                    break
            # Stage 1 — WHICH tool the crossing invokes. Read from the same
            # DB-sourced tool config the gates come from, validated against
            # this pack's live tool list. Absent → "escalate", byte-identical
            # to the string literal this replaced. An action naming no tool on
            # the pack degrades LOUD (error log + a note that follows every
            # emit onto alert_sink_deliveries) rather than taking the operator's
            # escalation edge offline over a typo.
            esc_action, esc_action_note = resolve_escalation_action(
                esc_tool_cfg, esc_pack, log_context=ad.identity.id
            )
            escalation = EscalationBinding(
                binding=AgencyToolBinding(
                    agency=esc_agency,
                    pack=esc_pack,
                    pg_pool=pg_store.pool,
                    tool_context=esc_ctx,
                    analyst_grants=grant_dicts,
                    target_allows=None,  # resolved per run from the target
                    requested_by=f"analyst::{ad.identity.id}",
                    budget_account=ad.identity.id,
                ),
                severity_gate=str(esc_tool_cfg.get("severity_gate", "high")),
                confidence_gate=float(esc_tool_cfg.get("confidence_gate", 0.85)),
                action_tool=esc_action,
                action_degraded=esc_action_note,
            )

        # P0-T2 faithfulness verify — resolve the OPTIONAL judge LLM for the
        # MANDATORY post-finding verify pass. The deterministic citation-presence
        # floor runs regardless; this wires the judge ONLY when BOTH hold:
        #   * the descriptor opts into a JUDGE ROUTE (P2-4 ladder:
        #     ``LEGBA_JUDGE_STACK_REF`` env override → ``method.llm.judge`` →
        #     ``method.llm.verify`` → ``method.llm.primary``; the opt-in gate is
        #     the judge/verify key — see resolve_judge_route); AND
        #   * the ``LEGBA_VERIFY_LLM_JUDGE`` flag gates the judge ON (the verify
        #     seam's OWN helper is reused so the flag semantics are IDENTICAL).
        # SOFT-FAIL: any resolution error → verify_judge=None + a warning; the
        # floor still runs (labelled 'judge-unavailable'). NEVER raises into deps
        # build. The handler is built through the SAME cached _llm_handler_factory
        # → build_llm_handler_from_stack_component path every other LLM uses, so
        # it is an LLMProviderHandler exposing chat_complete (verify.py's
        # contract). No route / flag off → None → the floor stands. The resolved
        # ref rides along as ``verify_judge_ref`` so the critique row can stamp
        # ``judge_llm_ref`` (which model judged — provenance, forever).
        verify_judge: Any = None
        verify_judge_ref: str = ""
        # W-3d — the coarse route CLASS (configured|fallback_verify|
        # fallback_primary) riding along with the ref so the critique row can
        # stamp ``judge_route`` (the UI badge's configured-vs-fell-back signal).
        verify_judge_route: str = ""
        judge_route = resolve_judge_route(ad)
        if judge_route is not None and _llm_judge_enabled():
            try:
                verify_judge = await _llm_handler_factory(judge_route.component_id)
                verify_judge_ref = judge_route.component_id
                verify_judge_route = judge_route.route_class
                logger.info(
                    "analyst_deps_resolver.verify_judge_wired actor_id=%s "
                    "analyst=%s judge_ref=%s source=%s route=%s",
                    actor_id, ad.identity.id,
                    judge_route.component_id, judge_route.source,
                    verify_judge_route,
                )
            except Exception as exc:  # noqa: BLE001 — soft-fail, floor still runs
                verify_judge = None
                verify_judge_ref = ""
                verify_judge_route = ""
                logger.warning(
                    "analyst_deps_resolver.verify_judge_resolve_failed "
                    "actor_id=%s analyst=%s component_id=%s source=%s err=%s — "
                    "the faithfulness verify degrades to its deterministic floor",
                    actor_id, ad.identity.id, judge_route.component_id,
                    judge_route.source, exc,
                )

        return _AnalystDeps(
            descriptor=ad,
            deps=standard_deps,
            run_method=run_method,
            kind_deps=kind_deps,
            output_kind=output_kind,
            budget=budget,
            receipt_chain=receipt_chain,
            read_slice=read_slice,
            escalation=escalation,
            gather_binding=gather_binding,
            gather_write_bindings=gather_write_bindings,
            verify_judge=verify_judge,
            verify_judge_ref=verify_judge_ref,
            verify_judge_route=verify_judge_route,
        )

    register_target_deps_resolver(_target_deps_resolver)
    register_analyst_deps_resolver(_analyst_deps_resolver)
    logger.info("dapr_host.deps_resolvers.registered")

    # ---------- assemble the reconcile loop ----------------------------
    #
    # P-CUT: the loop reconciles the ``source`` descriptor family in addition
    # to target/analyst. The source reconciler reuses the family-agnostic
    # CREATE/RETIRE/TRANSITION logic (``_common_reconcile``) — the executor's
    # ``_proxy_for`` maps the ``source`` actor kind to SourceActor.
    #
    # Orphan-reminder GC (release-engineering / REVIEW §3.4): a reminder whose
    # owning actor RETIRED but which never fires again (idled out / scheduler
    # etcd lost the recurrence) is an orphan the on-fire self-disarm guard
    # never reaches — the documented "fires once then silent" stall whose only
    # other fix is a full scheduler-data wipe. This sweep unregisters only
    # reminders owned by RETIRED actor_state rows (provably-orphan; never a
    # live actor) via the idempotent daprd sidecar reminder DELETE.
    from .reminder_gc import (
        build_sidecar_reminder_deleter,
        sweep_orphan_reminders,
    )

    _reminder_deleter = build_sidecar_reminder_deleter()

    async def _reminder_gc_sweep() -> Any:
        return await sweep_orphan_reminders(
            state_store=state_store,
            delete_reminder=_reminder_deleter,
            alert_publish=nats_store.publish_json,
        )

    # Task #236 RIDER: ``action_pack`` descriptors have no actor lifecycle
    # (excluded from desired_resolver's ``_FAMILIES`` above), so a pack PUT
    # never triggers dapr_actors.evict_analyst_deps_for_descriptor for the
    # analysts that merely GRANT the pack — their cached deps keep serving
    # the pre-PUT tool set until their OWN descriptor head also bumps (the
    # live 2026-07-24 lens_diff "Tool get_lens_reads not found" incident).
    # This WARNS on the drift every resync rather than silently serving it;
    # see dapr_actors.warn_stale_pack_deps for the fuller recipe toward a
    # real fix (a pack_id -> dependent-analyst reverse index).
    async def _pack_version(pack_id: str) -> str | None:
        from ..data.analysts.agency.binding import fetch_action_pack

        pack = await fetch_action_pack(registry_client, pack_id)
        return pack.identity.version if pack is not None else None

    async def _pack_staleness_sweep() -> Any:
        from .dapr_actors import warn_stale_pack_deps

        return await warn_stale_pack_deps(_pack_version)

    reconcile_loop = ReconcileLoop(
        state_store=state_store,
        desired_resolver=desired_resolver,
        desired_lister=desired_lister,
        action_executor=action_executor,
        reconcilers={
            "target": TargetReconciler(),
            "analyst": AnalystReconciler(),
            # Same pure reconcile logic as targets/analysts; the actor kind
            # discriminator lives in the desired-state family + the executor.
            "source": TargetReconciler(),
        },
        reminder_gc=_reminder_gc_sweep,
        pack_staleness_check=_pack_staleness_sweep,
    )
    # NOTE: the loop is CONSTRUCTED here but STARTED below, AFTER the
    # source-first planes populate AGENCY_HOLDER. A consult analyst's deps
    # resolver fail-closes (returns None → activation refused) when the
    # agency plane isn't up yet; starting the reconcile loop before
    # bring-up would open a window where a reconcile could refuse a consult
    # actor that would otherwise bind fine. Nothing enqueues a reconcile
    # between construction and start (the informer + bootstrap resync are
    # wired after start), so deferring start is pure ordering safety.

    # ---------- source-first planes (P-CUT) ----------------------------
    #
    # Job worker pool (P-07) + subscription/fan-out engine (P-08) + coalescing
    # trigger engine (P-10) + action-pack agency (P-11) + the SourceActor deps
    # resolver. This is the source-first runtime the host boots ON; the legacy
    # E2 target-owned pull path is NOT wired here (L-205).
    from .source_first_runtime import bring_up_source_first_planes

    # Source baseline NLP-enrichment factory — builds the descriptor's
    # ``pipeline.enrichment`` chain (language_detect/geocode/ner_multilingual/
    # classify) the SAME way as the target ingestion pipeline above
    # (build_filter_handler + PipelineRunner), threading the host's nlp/qdrant/
    # embedding clients. The source deps resolver calls this per source so the
    # SourceActor enriches each pulled signal (geo/tags/entity_classes/language)
    # — the coarse axes the subscription fan-out + per-target scoping match on.
    # Without it, signals land raw (tier-1 structured enrichment only).
    # Filter kinds that consume the hosted-NLP client. ner_multilingual +
    # classify REQUIRE it (build_filter_handler raises without the factory);
    # fact_extractor uses it only as an optional /extract fallback.
    _NLP_REQUIRED_KINDS = {"ner_multilingual", "classify"}
    _NLP_OPTIONAL_KINDS = {"fact_extractor"}
    #: The only enrichment kind that hard-requires BOTH qdrant + embedding
    #: (pipeline.py raises building it without them) — see pipeline.py's
    #: "dedupe_tier_3" branch.
    _VECTOR_REQUIRED_KINDS = {"dedupe_tier_3"}

    async def _source_enrichment_factory(sd: Any):
        enrichment = list(getattr(sd.pipeline, "enrichment", []) or [])
        if not enrichment:
            return None

        # #91 §2.3: lazily (re-)resolve the hosted-NLP client only if THIS
        # descriptor's chain needs it. A late seed or a recovered models-host
        # heals the filter set on the next source-deps resolution — boot-time
        # unavailability no longer pins the process degraded for its lifetime.
        kinds = {getattr(s, "kind", None) for s in enrichment}
        needs_nlp = bool(kinds & (_NLP_REQUIRED_KINDS | _NLP_OPTIONAL_KINDS))
        resolved_nlp_client: NlpServiceClient | None = None
        if needs_nlp:
            try:
                resolved_nlp_client = await _lazy_nlp_client.get()
            except NlpClientFactoryError as exc:
                if kinds & _NLP_REQUIRED_KINDS:
                    # A descriptor genuinely requiring NER must fail loud, not
                    # degrade silently. The async resolver above is NOT cached
                    # on failure, so the NEXT source-deps resolution retries.
                    raise
                # Only the optional /extract fallback wanted it — degrade to
                # the on-signal GLiREL triples path rather than dropping the
                # whole enrichment chain.
                logger.warning(
                    "source_enrichment.nlp_optional_unavailable source=%s "
                    "err=%s (fact_extractor falls back to signal triples)",
                    getattr(getattr(sd, "identity", None), "id", "?"), exc,
                )

        # #235: mirror the NLP pattern above for the qdrant/embedding pair —
        # lazily (re-)resolve ONLY if this descriptor's chain needs them
        # (dedupe_tier_3). Every other source's enrichment chain (the vast
        # majority — language_detect/geocode/ner_multilingual/classify never
        # touch the vector plane) pays zero extra registry round-trips and
        # rides the boot-time snapshot unchanged (byte-for-byte the pre-#235
        # behaviour for every non-vector chain). Named distinctly from the
        # outer `qdrant_client` / `embedding_service` (rather than shadowing
        # them) so there is no risk of an UnboundLocalError on the no-vectors
        # path — Python would otherwise treat a conditionally-assigned same-
        # named local as local for the WHOLE function body.
        needs_vectors = bool(kinds & _VECTOR_REQUIRED_KINDS)
        source_qdrant_client = qdrant_client
        source_embedding_service = embedding_service
        if needs_vectors:
            source_qdrant_client = await _resolve_qdrant_client()
            source_embedding_service = await _resolve_embedding_service()
            if source_qdrant_client is None or source_embedding_service is None:
                # A descriptor genuinely requiring dedupe_tier_3 must fail
                # loud (build_filter_handler itself raises without both) —
                # NOT degrade silently. Neither resolve call above caches a
                # failure, so the NEXT source-deps resolution retries.
                raise RuntimeError(
                    "source_enrichment: dedupe_tier_3 requires BOTH a "
                    f"qdrant_client (got {source_qdrant_client is not None}) "
                    "and an embedding_service "
                    f"(got {source_embedding_service is not None}) — neither "
                    "was reachable when this source's enrichment chain was "
                    "built. A later source-deps resolution will retry once "
                    "the vector plane heals."
                )

        def _nlp_client_factory() -> NlpServiceClient:
            if resolved_nlp_client is None:
                raise RuntimeError(
                    "NlpServiceClient not available — the "
                    "nlp.local.legba_models stack component was not "
                    "reachable when this source's enrichment chain was built. "
                    "A later source-deps resolution will retry once it heals.",
                )
            return resolved_nlp_client

        built: list[tuple[str, Any]] = []
        for stage in enrichment:
            cfg = _unwrap_descriptor_config(getattr(stage, "config", {}) or {})
            handler = build_filter_handler(
                kind=stage.kind,
                config=cfg,
                redis_client=redis_client,
                pg_pool=pg_store.pool,
                nlp_client_factory=(
                    _nlp_client_factory
                    if resolved_nlp_client is not None
                    else None
                ),
                qdrant_client=source_qdrant_client,
                embedding_service=source_embedding_service,
                secrets_resolve=_secrets_resolve,
                # PIECE 2: the fact_extractor 'llm' backend routes through the
                # analyst LLM plane; the relation backend ignores both. AGE
                # edges (emit_graph_edges) ride the PostgresStore cypher() port.
                llm_handler_factory=_llm_handler_factory,
                graph_store=pg_store,
            )
            built.append((stage.kind, handler))

        def _ctx_factory(filter_id: str) -> FilterContext:
            return FilterContext(
                target_id=sd.identity.id,
                target_version=sd.identity.version,
                filter_id=filter_id,
                scope_geo=list(getattr(sd.scope, "geo", []) or []),
                scope_languages=list(getattr(sd.scope, "languages", []) or []),
            )

        runner = PipelineRunner(stages=built, ctx_factory=_ctx_factory)

        async def _stage(signal: Any, ctx: Any):
            async def _gen():
                yield signal
            out = None
            try:
                async for s in runner.run(_gen()):
                    out = s
            except Exception as exc:                            # pragma: no cover
                logger.warning(
                    "source_enrichment.stage_error source=%s err=%s",
                    sd.identity.id, exc,
                )
                return signal
            # Enrichment is annotate-only + best-effort: a filter error or
            # empty yield must NOT drop the signal (unlike ingestion_filters).
            # Fall back to the unenriched signal so acquisition never loses data.
            sig = out if out is not None else signal
            # Promote payload enrichment → the indexed structured-filter
            # columns. The geocode/ner/language_detect handlers annotate
            # ``payload`` only (payload.geo / payload.entities / payload.
            # language), but the subscription fan-out + per-target scoping
            # match on the typed ``geo`` / ``entity_classes`` / ``language``
            # columns. Lift them across so a signal about Germany lands
            # geo=['DE'] and routes to country_g20_de.
            try:
                pl = sig.payload if isinstance(sig.payload, dict) else {}
                geo_block = pl.get("geo")
                if isinstance(geo_block, dict):
                    iso2 = geo_block.get("country_iso2")
                    if iso2 and iso2 not in sig.geo:
                        sig.geo.append(iso2)
                lang = pl.get("language")
                if lang and not sig.language:
                    sig.language = lang
                ents = pl.get("entities")
                if isinstance(ents, list):
                    for e in ents:
                        cls = e.get("class") if isinstance(e, dict) else None
                        if cls and cls not in sig.entity_classes:
                            sig.entity_classes.append(cls)
            except Exception as exc:                            # pragma: no cover
                logger.debug(
                    "source_enrichment.promote_skip source=%s err=%s",
                    sd.identity.id, exc,
                )
            return sig

        return _stage

    source_first: Any | None
    try:
        source_first = await bring_up_source_first_planes(
            pg_store=pg_store,
            nats_store=nats_store,
            standard_deps=standard_deps,
            registry_client=registry_client,
            enrichment_factory=_source_enrichment_factory,
        )
        logger.info(
            "dapr_host.source_first.ready targets_wired=%d trigger_regs=%d",
            len(source_first.registered_targets),
            source_first.trigger_registrations,
        )
        # §2.1: expose the trigger engine to the action_executor's retire hook
        # so RETIRE tears down the analyst's trigger registrations at the source.
        TRIGGER_ENGINE_HOLDER["engine"] = source_first.trigger_engine
    except Exception as exc:                                    # pragma: no cover
        logger.exception(
            "dapr_host.source_first.bringup_failed err=%s — the actor surface "
            "+ reconcile loop are up, but fan-out / job / trigger planes are "
            "NOT running. Resolve the rig (NATS/PG) + restart.", exc,
        )
        source_first = None

    # ---------- the NATS informers (built now, started by the leader) ---
    informer = NatsReconcileInformer(nats_store, reconcile_loop)
    # S-2 — the stack-component twin. Bound to LEGBA_STACK_EVENTS
    # (``stack.component.>``) with its OWN durable, it evicts the built-handler
    # caches for a component the operator just PUT so the next LLM call rebuilds
    # from the live row. Not leader-gated in principle — every replica holds its
    # own handler cache and each would need its own eviction — but it is started
    # alongside the descriptor informer below because today's deployment is
    # single-replica and the multi-replica story (per-replica durables, see
    # C-9) is one decision, not two.
    stack_informer = NatsStackComponentInformer(
        nats_store, evict=evict_llm_handler,
    )
    # RUST-5 — the vault-rotation third. Bound to LEGBA_VAULT_EVENTS
    # (``vault.secret.>``) with its own durable; on any message it drops
    # EVERY cached LLM handler (no per-secret targeting — see
    # nats_informer.py's module docstring) so a rotated credential is
    # re-resolved on the next call instead of staying cached until a
    # container recreate.
    vault_informer = NatsVaultRotationInformer(
        nats_store, evict_all=evict_all_llm_handlers,
    )

    # ---------- singleton control-plane loops — LEADER ONLY -------------
    #
    # scaling-multinode: the reconcile loop (resync + queue drain) + the
    # descriptor informer are singletons — running them on every replica would
    # double-run control-plane mutation (each resync re-issues CREATE/RETIRE
    # actions; the informer's per-instance durable fans every event to every
    # replica). The leader lease gates them to exactly ONE replica. The hot
    # path (Dapr-placed actors, CAS-guarded coalescer, shared-durable trigger
    # engine) is already replica-safe and runs on ALL replicas regardless — it
    # was brought up above, outside this gate.
    #
    # Deferred from construction so consult actors never reconcile into a
    # fail-closed agency-binding refusal during bring-up (AGENCY_HOLDER is up
    # by now).
    async def _start_singleton_loops() -> None:
        await reconcile_loop.start()
        logger.info("dapr_host.reconcile_loop.started (leader)")
        await informer.start()
        logger.info(
            "dapr_host.informer.started (leader) consumer=%s",
            informer.consumer_name,
        )
        # S-2: never let a stack-informer bind failure take the control plane
        # down with it. Its absence degrades to the pre-fix behaviour (config
        # PUTs need a recreate) and says so LOUDLY — it does not stop the
        # reconcile loop, which is the thing that keeps the engine running.
        try:
            await stack_informer.start()
            logger.info(
                "dapr_host.stack_informer.started (leader) consumer=%s "
                "caches=%s",
                stack_informer.consumer_name,
                ",".join(registered_cache_labels()) or "none",
            )
        except Exception as exc:  # pragma: no cover — bind failure is rig-level
            logger.error(
                "dapr_host.stack_informer.start_failed err=%s — stack-component "
                "PUTs will NOT invalidate cached LLM handlers in this process; "
                "a config change needs a container recreate until this binds. "
                "Check the LEGBA_STACK_EVENTS stream exists (the registry "
                "provisions it via ensure_runtime_event_streams).", exc,
            )
        # RUST-5: same never-take-the-control-plane-down posture as the stack
        # informer above — its absence degrades to the pre-fix behaviour (a
        # rotated secret needs a container recreate) and says so loudly.
        try:
            await vault_informer.start()
            logger.info(
                "dapr_host.vault_informer.started (leader) consumer=%s",
                vault_informer.consumer_name,
            )
        except Exception as exc:  # pragma: no cover — bind failure is rig-level
            logger.error(
                "dapr_host.vault_informer.start_failed err=%s — vault secret "
                "rotations will NOT invalidate cached LLM handlers in this "
                "process; a rotation needs a container recreate until this "
                "binds. Check the LEGBA_VAULT_EVENTS stream exists (the "
                "registry provisions it via ensure_runtime_event_streams).",
                exc,
            )
        # Initial resync — enqueue every active descriptor so its actor gets
        # activated. Idempotent (reconcile converges), so a standby that later
        # promotes re-runs this cleanly on acquire.
        initial = await desired_lister()
        for d in initial:
            reconcile_loop.enqueue(d.descriptor_id, reason="bootstrap_resync")
        logger.info("dapr_host.initial_resync.enqueued count=%d", len(initial))
        # Crash-recovery ledger hygiene (leader-gated → single-writer): a process
        # that died mid-dispatch left its pack-tool invocation row stuck
        # `admitted` (the per-call timeout/settle in run_pack_tool never ran).
        # Settle rows older than the stale threshold to `failed` so the ledger
        # reflects reality. Best-effort — never block leadership acquire.
        from ..data.analysts.agency.governor import (
            PackGovernorEnforcer,
            pack_tool_stale_reconcile_seconds,
        )
        try:
            async with pg_store.pool.acquire() as conn:
                swept = await PackGovernorEnforcer.reconcile_stale_admitted(
                    conn, older_than_seconds=pack_tool_stale_reconcile_seconds(),
                )
            if swept:
                logger.info(
                    "dapr_host.pack_invocation_reconcile settled=%d stale_admitted",
                    swept,
                )
        except Exception as exc:  # pragma: no cover — best-effort hygiene
            logger.warning("dapr_host.pack_invocation_reconcile.err=%s", exc)

    async def _stop_singleton_loops() -> None:
        # Demotion (lost the lock to a new leader) — stop driving the
        # control-plane so the new leader owns it exclusively. Best-effort.
        try:
            await informer.stop()
        except Exception as exc:                                # pragma: no cover
            logger.warning("dapr_host.demote.informer_stop err=%s", exc)
        try:
            await stack_informer.stop()
        except Exception as exc:                                # pragma: no cover
            logger.warning("dapr_host.demote.stack_informer_stop err=%s", exc)
        try:
            await vault_informer.stop()
        except Exception as exc:                                # pragma: no cover
            logger.warning("dapr_host.demote.vault_informer_stop err=%s", exc)
        try:
            await reconcile_loop.stop()
        except Exception as exc:                                # pragma: no cover
            logger.warning("dapr_host.demote.reconcile_stop err=%s", exc)
        logger.warning(
            "dapr_host.leadership.lost — singleton loops stopped; standing by",
        )

    await leader_lease.start(
        on_acquire=_start_singleton_loops,
        on_lose=_stop_singleton_loops,
    )
    if not leader_lease.is_leader:
        logger.info(
            "dapr_host.standby — leader election held by another replica; "
            "actor/fan-out/trigger planes run here, control-plane loops do not",
        )

    handles = _RuntimeHandles(
        pg_store=pg_store,
        nats_store=nats_store,
        state_store=state_store,
        reconcile_loop=reconcile_loop,
        informer=informer,
        registry_client=registry_client,
        leader_lease=leader_lease,
    )
    # Stash extra handles for shutdown — redis, qdrant, embedding,
    # temporal, audit_checkpointer. None of these are formal fields on
    # _RuntimeHandles (they were added incrementally); the lifespan +
    # SIGTERM path closes them best-effort via getattr() lookups.
    #
    # #235: stash the LAZY HOLDERS, not the boot-time qdrant_client /
    # embedding_service snapshot locals. If the boot-time attempt failed
    # (snapshot is None) but a LATER lazy resolution inside
    # _analyst_deps_resolver succeeded, the snapshot locals stay stuck at
    # None for the rest of bring_up_production_runtime's scope — reading
    # them here would silently skip closing a client that IS live by the
    # time the process shuts down (a connection/socket leak on every
    # lazy-recovery event). `.cached` on each holder always reflects
    # whatever actually got built, whenever it got built.
    handles.redis_store = redis_store  # type: ignore[attr-defined]
    handles.qdrant_client_holder = _lazy_qdrant_client  # type: ignore[attr-defined]
    handles.embedding_service_holder = _lazy_embedding_service  # type: ignore[attr-defined]
    handles.temporal_client = temporal_client  # type: ignore[attr-defined]
    handles.workflow_runtime = workflow_runtime  # type: ignore[attr-defined]
    handles.audit_checkpointer = audit_checkpointer  # type: ignore[attr-defined]
    handles.output_http_client = output_http_client  # type: ignore[attr-defined]
    # P-CUT source-first planes — stopped before the substrate closes (the
    # _RuntimeHandles.stop() path tears them down first via getattr lookup).
    handles.source_first = source_first  # type: ignore[attr-defined]
    # S-2 stack-component informer + the cache it evicts through. Stopped and
    # UNREGISTERED on shutdown so a second bring-up in the same process (the
    # test rig) never evicts through a torn-down runtime's dict.
    handles.stack_informer = stack_informer  # type: ignore[attr-defined]
    handles.llm_handler_cache = _llm_handler_cache  # type: ignore[attr-defined]
    # RUST-5 vault-rotation informer. Same shutdown posture as the stack
    # informer — stopped via the getattr lookup in ``_RuntimeHandles.stop()``.
    handles.vault_informer = vault_informer  # type: ignore[attr-defined]
    return handles


def main() -> None:
    # Structured JSON logging with run/correlation-id threading (W-1b §3).
    # JSON by default; LEGBA_LOG_FORMAT=text keeps the old human format.
    from .logging_setup import configure_structured_logging

    configure_structured_logging()
    port = int(os.getenv("LEGBA_RUNTIME_HTTP_PORT", "6090"))

    handles_holder: dict[str, _RuntimeHandles] = {}

    @asynccontextmanager
    async def production_lifespan(_app: FastAPI):
        # Bring up the actor-host wiring before yielding so daprd can
        # immediately dispatch into a hot runtime.
        #
        # P-CUT: register the NEW source-first SourceActor alongside the
        # Target/Analyst actors. SourceActor OWNS acquisition (poll Reminder /
        # push webhook → canonical signal → publish); the old E2 target-owned
        # pull path is no longer the live acquisition mechanism (L-205).
        # TargetActor stays registered only as the discovery-materialiser host
        # + the subscriber identity the fan-out delivers to.
        from dapr.actor import ActorRuntime
        from .dapr_actors import AnalystActor, TargetActor
        from .source_actor import SourceActor
        await ActorRuntime.register_actor(TargetActor)
        await ActorRuntime.register_actor(AnalystActor)
        if SourceActor is not None:
            await ActorRuntime.register_actor(SourceActor)
        logger.info(
            "dapr_host.actor_types.registered types=%s",
            list(ActorRuntime.get_registered_actor_types()),
        )
        try:
            handles = await bring_up_production_runtime()
            handles_holder["h"] = handles
        except Exception as exc:                                # pragma: no cover
            logger.exception(
                "dapr_host.bringup.failed err=%s — runtime will serve "
                "the actor surface but the reconcile loop / informer "
                "are NOT running; descriptors will sit unread.", exc,
            )
        yield
        h = handles_holder.get("h")
        if h is not None:
            await h.stop()
        logger.info("dapr_host.shutdown")

    # Build a fresh app with the production lifespan (not the
    # build_dapr_host_app default, which is geared at the spike test).
    app = FastAPI(
        title="Legba Runtime (Dapr)",
        version="0.1.0",
        lifespan=production_lifespan,
    )
    DaprActor(app)

    # Mount the L-193 A2A skill router on the production app — B-2: only
    # when explicitly enabled, and only with an explicit caller allowlist
    # (or LEGBA_DEV_MODE=1). O-1 (`c6da066`) found this surface was wired
    # in `build_dapr_host_app()` but never on the production `main()` path;
    # the first wiring passed `trusted_keys=None`, which meant accept-all —
    # `resolve_a2a_mount()` is the fail-closed replacement. Disabled mount
    # → `A2A_SKILL_REGISTRY_HOLDER` stays empty and the action_executor's
    # skill (un)registration paths no-op.
    _a2a_trusted = resolve_a2a_mount()
    if _a2a_trusted is not None:
        from ..data.outputs.a2a_skill import (
            A2ASkillRegistry,
            register_a2a_skill_route,
        )
        from ..data.registry.signing import load_default_identity

        _a2a_registry = A2ASkillRegistry()
        _a2a_identity = load_default_identity()

        async def _fetch_latest_outputs(
            *,
            analyst_ids: list[str],
            limit: int = 5,
            target_filter: str | None = None,
        ) -> list[dict[str, Any]]:
            """Lazy-deferred read of recent analyst outputs for A2A skill
            responses. Returns [] until the bootstrap completes. Signature
            matches `LatestOutputFetcher` — the router calls it with
            `analyst_ids=[...], limit=..., target_filter=...`.
            """
            h = handles_holder.get("h")
            if h is None:
                return []
            query = """
                SELECT id, kind, title, body, data, produced_at,
                       target_id, analyst_id, analyst_version, schema_uri
                  FROM analyst_outputs
                 WHERE analyst_id = ANY($1::text[])
            """
            args: list[Any] = [list(analyst_ids)]
            if target_filter is not None:
                args.append(target_filter)
                query += f" AND target_id = ${len(args)}"
            args.append(limit)
            query += f" ORDER BY produced_at DESC LIMIT ${len(args)}"
            async with h.pg_store.pool.acquire() as conn:
                rows = await conn.fetch(query, *args)
            return [dict(r) for r in rows]

        register_a2a_skill_route(
            app,
            registry=_a2a_registry,
            identity=_a2a_identity,
            fetch_latest_outputs=_fetch_latest_outputs,
            trusted_keys=_a2a_trusted,
            prefix="/a2a/skills",
        )
        # Stash the registry on app.state for runtime introspection AND in
        # the module-level holder so the action_executor closure (which lives
        # in bring_up_production_runtime, not in main()) can pick it up.
        app.state.a2a_skill_registry = _a2a_registry
        A2A_SKILL_REGISTRY_HOLDER["registry"] = _a2a_registry
        logger.info(
            "dapr_host.a2a.mounted trusted_dids=%d",
            len(_a2a_trusted.keys),
        )
    else:
        logger.info(
            "dapr_host.a2a.mount_disabled set %s=1 (plus %s) to enable",
            A2A_ENABLED_ENV, A2A_TRUSTED_KEYS_ENV,
        )
        # Fail-LOUD seam (SEAMS #15): when the A2A surface is gated off, a bare
        # GET /a2a/skills would 404 — indistinguishable from "route was never
        # built". Mount a tiny stub that answers 503 with a self-documenting
        # body so an operator (or the xfail-tracked e2e) sees the surface is
        # DELIBERATELY disabled, not silently missing. No skill logic, no
        # signing identity — purely an honest "operator-gated, set the flag".
        @app.api_route(
            "/a2a/skills", methods=["GET", "POST"], include_in_schema=False,
        )
        @app.api_route(
            "/a2a/skills/{skill_id}",
            methods=["GET", "POST"],
            include_in_schema=False,
        )
        async def _a2a_disabled(skill_id: str | None = None) -> Any:
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=503,
                content={
                    "error": "a2a_skill_surface_disabled",
                    "detail": (
                        f"The A2A skill surface is operator-gated OFF (B-2 "
                        f"fail-closed). Set {A2A_ENABLED_ENV}=1 plus "
                        f"{A2A_TRUSTED_KEYS_ENV}=<did=hex,...> (or "
                        f"LEGBA_DEV_MODE=1) and restart to mount it. See "
                        f"docs/SEAMS.md #15."
                    ),
                },
            )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        from dapr.actor import ActorRuntime
        from .leader import leader_election_enabled, replica_count

        h = handles_holder.get("h")
        lease = getattr(h, "leader_lease", None) if h is not None else None
        is_leader = bool(getattr(lease, "is_leader", False)) if lease is not None else None
        # Singleton loops run only on the leader; report reconcile_running as
        # True only when this replica IS the leader and its loop is live.
        reconcile_running = (
            h is not None
            and is_leader is True
            and not h.reconcile_loop._stopped
        )
        return {
            "status": "ok",
            "actor_types": list(ActorRuntime.get_registered_actor_types()),
            "reconcile_running": reconcile_running,
            "leader": is_leader,
            "leader_election": "on" if leader_election_enabled() else "off",
            "replica_count": replica_count(),
        }

    cfg = uvicorn.Config(
        app,
        host=os.getenv("LEGBA_RUNTIME_HTTP_HOST", "0.0.0.0"),
        port=port,
        log_level=os.getenv("LEGBA_LOG_LEVEL", "info").lower(),
        log_config=None,
    )
    server = uvicorn.Server(cfg)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _signal_handler(*_: Any) -> None:
        logger.info("legba-runtime-dapr received shutdown signal")
        loop.call_soon_threadsafe(loop.stop)

    for sig in (signalmod.SIGINT, signalmod.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:  # pragma: no cover — Windows
            pass

    try:
        loop.run_until_complete(server.serve())
    finally:
        loop.close()


async def attach_reconcile_informer(
    nats_store: Any,
    reconcile_loop: Any,
    *,
    consumer_label: str = "informer",
) -> Any:
    """Start a :class:`NatsReconcileInformer` against ``nats_store`` + ``reconcile_loop``.

    Returns the informer instance — caller is responsible for calling
    ``await informer.stop()`` during shutdown. The informer is reentrant:
    on a restart-after-crash the durable consumer name is the same so the
    process picks up where it left off (no replay across restarts; events
    that arrive while the runtime is down land via the 5-min periodic
    resync's catchup pass).
    """
    from .nats_informer import NatsReconcileInformer

    informer = NatsReconcileInformer(
        nats_store, reconcile_loop, consumer_label=consumer_label,
    )
    await informer.start()
    logger.info(
        "dapr_host.informer.started consumer=%s", informer.consumer_name,
    )
    return informer


__all__ = [
    "attach_a2a_skill_router",
    "attach_reconcile_informer",
    "build_dapr_host_app",
    "main",
    "resolve_a2a_mount",
]


if __name__ == "__main__":  # pragma: no cover
    main()
