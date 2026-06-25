# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.runtime — Phase 5a vertical-slice runtime (L-002a).

The runtime turns registered descriptors into running actors. Per
``plans/design/legba_runtime_spec.md``:

  * **TargetActor** — owns one target descriptor. Has source-pull cadence
    (cron + reminder) and per-source cursors in persistent state. Runs
    sources -> pipeline -> substrate-write.

  * **AnalystActor** — owns one analyst descriptor. Reads its substrate
    slice on cadence, invokes the configured LLM, writes one or more
    outputs (findings, situations, etc.) into substrate with provenance.

  * **Reconciler** — informer + work queue. Watches descriptor.registered
    / .retired NATS events, plus periodic resync. Drives the lifecycle
    FSM. Per-kind reconcilers are pure; an action executor mutates.

  * **Budget** — pre-call envelope check + post-call ledger record.

  * **Lifecycle** — draft/configured/active/paused/retired/error FSM.

  * **Pipeline** — composes filter handlers from
    :mod:`legba.data.filters` into a stream that the target actor's pull
    feeds.

Dapr usage:

  * Actors are Dapr virtual actors — single-activation per descriptor id;
    state via the ``legba-actor-state`` Postgres state component;
    reminders provide durable cadence across sidecar restarts. The
    embedded asyncio host (``runtime.host`` / ``runtime.actors``) retired
    under L-205 once the production daprd path stabilized.
"""

# NB: this package previously re-exported every submodule's public
# surface here (TargetActor, AnalystActor, ReconcileLoop, BudgetEnforcer,
# the lifecycle FSM, etc.). The re-exports had no in-tree consumers
# (verified by `grep -rn "from legba.runtime import "`; every hit
# targets a submodule name like `dapr_actors`, not a re-exported
# symbol). They were dropped 2026-05-27 so this package can be imported
# without eagerly loading dapr_actors → dapr SDK + the source-kind
# handler set (rss.py → httpx) — keeping the package import cheap and
# side-effect free for minimal environments.
#
# Consumers continue to import submodules directly:
#     from legba.runtime.dapr_actors import TargetActor
#     from legba.runtime.reconcile import ReconcileLoop
#
# which load the deps they actually need — no more, no less.

__all__: list[str] = []
