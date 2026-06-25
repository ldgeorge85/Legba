# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Worker-id / fan-out helpers — extracted from dapr_actors.py (#93), behavior-preserving move."""

from __future__ import annotations

import os
from typing import Any, Callable

from .lifecycle import ACTIVE, RETIRED


# ---------------------------------------------------------------------------
# A2 concurrency — per-(analyst, target) worker actor ids + fan-out
# ---------------------------------------------------------------------------
#
# The PRIMARY analyst actor (id ``analyst::<descriptor_id>::<ver16>``) keeps
# the cadence reminder. On each tick it FANS OUT one run per matched target to
# a per-target WORKER actor (id ``analyst::<descriptor_id>::<target_id>``).
# Distinct id ⇒ distinct Dapr virtual actor ⇒ own turn-queue ⇒ concurrent
# per-target runs, instead of ~19 countries serializing through the primary's
# single queue.
#
# The worker carries the SAME segment-1 (descriptor_id) as the primary, so the
# analyst deps fallback resolver (``split("::", 2)[1]`` → head descriptor)
# reconstructs the analyst's deps with NO new registration. Workers
# lazy-activate inside ``run`` and register NO reminder (only the primary does).
#
# Fan-out is bounded-concurrent (chunked at ``_FANOUT_CHUNK``) to avoid a
# 19-wide LLM thundering herd against the budget envelope + provider limits.

_FANOUT_CHUNK = 5

# Critic tier-2 fan-out cap (L-175): newest-N ungraded analyzed-analyst findings
# graded per cadence tick. The per-analyst BudgetEnforcer (budget.py — keyed on
# analyst_id, NOT per-target) is the hard daily ceiling regardless of this width;
# this only bounds the number of worker actors spawned per tick so a large
# ungraded backlog drains steadily instead of all at once.
#
# The old hardcoded 4/tick was a real under-capacity: country_assessor produces
# ~60 findings/day and a 400+ ungraded backlog had built up while the critic
# cadence was dormant, so 4/tick could never drain it. Raised the default and
# made it env-tunable (LEGBA_CRITIC_FANOUT_MAX) so an operator can dial the
# backlog-drain rate. NOTE: the analyst's method.budget_tokens_per_day (the
# descriptor field) remains the true daily coverage governor — raising this cap
# front-loads the day's budget onto the backlog; it does NOT raise total
# grades/day. To cover a larger share of findings, raise the token budget.
_DEFAULT_CRITIC_FANOUT_MAX = 12


def _critic_fanout_max() -> int:
    """Max ungraded findings the critic fans out to grade per cadence tick.
    Env LEGBA_CRITIC_FANOUT_MAX; default 12. The per-analyst daily token budget
    is the hard ceiling regardless — this only paces backlog drain per tick."""
    raw = os.getenv("LEGBA_CRITIC_FANOUT_MAX")
    if raw and raw.strip():
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            pass
    return _DEFAULT_CRITIC_FANOUT_MAX


def _worker_actor_id(descriptor_id: str, target_id: str) -> str:
    """Per-(analyst, target) worker actor id — ``analyst::<id>::<target_id>``.

    Authoritative constructor for the A2 worker id. Shares the
    ``kind::id::*`` grammar of :func:`legba.runtime.reconcile._default_actor_id`
    but carries the target_id in the third (version) slot. Mirror constructor
    lives in :func:`legba.runtime.source_first_runtime.worker_actor_id`.
    """
    return f"analyst::{descriptor_id}::{target_id}"


def _split_actor_id(actor_id: str) -> tuple[str, str, str | None]:
    """Decompose ``kind::descriptor_id::tail`` → (kind, descriptor_id, tail).

    ``tail`` is the third ``::`` segment — a content-hash for a primary actor,
    or a target_id for a worker actor — and is ``None`` when absent.
    """
    parts = actor_id.split("::", 2)
    kind = parts[0] if parts else ""
    descriptor_id = parts[1] if len(parts) >= 2 else actor_id
    tail = parts[2] if len(parts) >= 3 else None
    return kind, descriptor_id, tail


def reminder_guard_decision(
    *,
    record_lifecycle: str | None,
    own_tail: str | None,
    head_version: str | None,
) -> str:
    """Belt-and-braces gate for durable-reminder fires (A-1/G1).

    Reminders outlive their actors: a version bump mints a new actor_id and
    the old id's reminder keeps firing until something unregisters it, and a
    missed retire/pause propagation leaves a parked actor with a live
    reminder. Every ``receive_reminder`` consults this BEFORE running.

    Returns one of:
      * ``"unregister"`` — the reminder is provably stale (this actor's
        version is no longer the descriptor head, or the actor is retired).
        Caller unregisters its own reminder and skips the run.
      * ``"skip"`` — not safe to run (paused/error lifecycle) but not
        provably stale; leave the reminder alone.
      * ``"run"`` — proceed.

    Pure + conservative: unknown head (registry unreachable) or a missing
    record never kills a reminder — the run path's own lifecycle gates stay
    the second line of defense.
    """
    if head_version and own_tail is not None:
        head_tail = head_version[:16] or "0" * 16
        if own_tail != head_tail:
            return "unregister"
    if record_lifecycle == RETIRED:
        return "unregister"
    if record_lifecycle is not None and record_lifecycle != ACTIVE:
        return "skip"
    return "run"


def _worker_proxy_factory() -> Callable[[str], Any]:
    """Return a callable ``worker_actor_id -> ActorProxy`` for AnalystActor.

    Extracted so tests can monkeypatch the fan-out's proxy creation without a
    live daprd sidecar. The import is local to keep ``dapr.actor.ActorProxy``
    out of the module-import path (it's only needed at fan-out time).
    """
    from dapr.actor import ActorId, ActorProxy

    from .dapr_actors import AnalystActorInterface

    # Share the trigger-dispatch invoke budget: the cadence fan-out drives the
    # same per-(analyst, target) worker ``run`` as the reactive path, so the
    # heaviest analysts (cross_source_dedup on the busy G20 targets) would hit
    # the same 60s SDK-default round-trip timeout here too. Build the factory
    # once per fan-out (one DaprActorHttpClient reused across the chunked
    # workers). Local import keeps the (source_first_runtime <-> dapr_actors)
    # dependency lazy / one-directional at module-load time.
    from .source_first_runtime import _actor_proxy_factory

    proxy_factory = _actor_proxy_factory()

    def _make(worker_id: str) -> Any:
        return ActorProxy.create(
            "AnalystActor", ActorId(worker_id), AnalystActorInterface,
            actor_proxy_factory=proxy_factory,
        )

    return _make
