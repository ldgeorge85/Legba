# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""§2.2 — per-target worker deps must re-resolve after a descriptor edit.

Per-(analyst, target) WORKER actor ids are version-LESS by design (the
target_id occupies the slot the primary fills with the descriptor
content-hash). So a descriptor edit does NOT change a worker's id, and the
otherwise-forever ``_ANALYST_DEPS[worker_id]`` cache keeps serving the OLD
prompt/method/budget/gates until a full runtime restart — the new primary
fans out to the same worker ids carrying stale deps.

``evict_analyst_deps_for_descriptor`` (called by the reconcile executor on the
new-version CREATE_ACTOR) drops the descriptor's primary + every worker entry
so the next fire re-resolves head. This test proves the bug (stale cache hit)
AND the fix (eviction → re-resolve), and that an unrelated descriptor's cache
is untouched.

Pure unit — type-erased deps, no daprd/registry needed.
"""

from __future__ import annotations

from legba.runtime import dapr_actors


class _SentinelDeps:
    """Type-erased stand-in (the resolver layer is ``Any``)."""

    def __init__(self, tag: str) -> None:
        self.tag = tag


async def test_evict_worker_deps_forces_reresolve_after_version_bump():
    dapr_actors.clear_deps_registry()
    try:
        head = {"v": "v1"}
        calls: list[str] = []

        async def resolver(actor_id: str):
            calls.append(actor_id)
            return _SentinelDeps(tag=head["v"])

        dapr_actors.register_analyst_deps_resolver(resolver)

        worker_a = "analyst::country_assessor::BR"
        worker_b = "analyst::country_assessor::US"
        other = "analyst::weather::JP"  # different descriptor — must survive

        # Prime the cache: resolver runs once per id, then reads hit the cache.
        assert (await dapr_actors._resolve_analyst_deps(worker_a)).tag == "v1"
        assert (await dapr_actors._resolve_analyst_deps(worker_b)).tag == "v1"
        assert (await dapr_actors._resolve_analyst_deps(other)).tag == "v1"
        assert calls == [worker_a, worker_b, other]

        # Descriptor edited → head is now v2. WITHOUT eviction the version-less
        # worker keeps serving stale v1 from the cache (this IS the bug).
        head["v"] = "v2"
        assert (await dapr_actors._resolve_analyst_deps(worker_a)).tag == "v1"
        assert calls == [worker_a, worker_b, other]  # resolver NOT re-run

        # The fix: evict this descriptor's entries (what CREATE_ACTOR(active)
        # now does on a version bump). Only country_assessor's two workers go.
        evicted = dapr_actors.evict_analyst_deps_for_descriptor("country_assessor")
        assert evicted == 2

        # Evicted descriptor re-resolves head → v2...
        assert (await dapr_actors._resolve_analyst_deps(worker_a)).tag == "v2"
        assert (await dapr_actors._resolve_analyst_deps(worker_b)).tag == "v2"
        # ...the unrelated descriptor's cache survived (still v1, no re-resolve).
        assert (await dapr_actors._resolve_analyst_deps(other)).tag == "v1"
        assert calls == [worker_a, worker_b, other, worker_a, worker_b]
    finally:
        dapr_actors.clear_deps_registry()


def test_evict_is_a_noop_for_unknown_descriptor():
    dapr_actors.clear_deps_registry()
    assert dapr_actors.evict_analyst_deps_for_descriptor("never_registered") == 0
