# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The runtime's ``GET /healthz`` (split out of ``dapr_host``, which
re-exports it). Mounted BEFORE ``DaprActor(app)`` — see the function."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from dapr.actor import ActorRuntime
from fastapi import FastAPI

__all__ = ["mount_runtime_healthz"]


def mount_runtime_healthz(app: FastAPI, handles_holder: Mapping[str, Any]) -> None:
    """Mount the runtime's ``GET /healthz`` on ``app``. Call it BEFORE
    ``DaprActor(app)``.

    WHY THE ORDER: ``dapr.ext.fastapi.DaprActor`` registers its own
    ``GET /healthz`` (answering ``{"status": "ok"}``), and Starlette answers a
    path from the FIRST route that matches. Mounted after it, this route was
    dead code: live, ``:6090/healthz`` answered ``{"status":"ok"}``
    with no ``actor_types`` and no T-1 ``llm_gates`` block. The answer keeps
    ``status: ok`` and 200, so daprd's app-health probe reads it as before.
    """

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        from ..data.stack.llm.call_gate import gate_snapshots
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
            # T-1: the core-plane concurrency gate (in flight, queued, waits).
            "llm_gates": gate_snapshots(),
        }
