# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Task #236 RIDER — the stale-pack-deps WARNING sweep.

``action_pack`` descriptors (e.g. ``journal_read``) have NO actor lifecycle
of their own — ``dapr_host.desired_resolver``'s ``_FAMILIES = ("target",
"analyst", "source")`` never resolves an ``action_pack`` id, so a pack PUT
never reaches ``dapr_actors.evict_analyst_deps_for_descriptor`` for the
analysts that merely GRANT the pack. Their cached ``_AnalystDeps`` entry
(built once, kept forever per ``_resolve_analyst_deps``) keeps serving the
OLD pack snapshot until the analyst's OWN descriptor also bumps — the exact
live shape that broke ``lens_diff`` on 2026-07-24 (``Tool get_lens_reads not
found`` until its own descriptor head was bumped, which is what incidentally
evicted it).

``dapr_actors.warn_stale_pack_deps`` does NOT evict or rebuild anything (a
live eviction needs a pack_id -> dependent-analyst reverse index that does
not exist yet); it WARNS every periodic resync so the drift is diagnosable
instead of silently served. This suite proves the detection logic — pure
unit, duck-typed sentinels (mirrors test_worker_deps_eviction.py's
_SentinelDeps shape), no daprd/registry needed.
"""

from __future__ import annotations

from legba.runtime import dapr_actors


class _Identity:
    def __init__(self, id_: str, version: str) -> None:
        self.id = id_
        self.version = version


class _Pack:
    def __init__(self, id_: str, version: str) -> None:
        self.identity = _Identity(id_, version)


class _Binding:
    def __init__(self, pack: _Pack) -> None:
        self.pack = pack


class _Escalation:
    def __init__(self, binding: _Binding) -> None:
        self.binding = binding


class _SentinelAnalystDeps:
    """Duck-typed stand-in for ``dapr_actors._AnalystDeps`` — only the
    pack-bearing fields (``gather_binding`` / ``gather_write_bindings`` /
    ``escalation``) are load-bearing for ``_cached_pack_refs`` /
    ``warn_stale_pack_deps`` (both are plain functions, not pydantic model
    methods, so no strict-model validation applies to a fixture)."""

    def __init__(
        self,
        *,
        gather_binding: _Binding | None = None,
        gather_write_bindings: dict | None = None,
        escalation: _Escalation | None = None,
    ) -> None:
        self.gather_binding = gather_binding
        self.gather_write_bindings = gather_write_bindings
        self.escalation = escalation


# ---------------------------------------------------------------------------
# _cached_pack_refs — the per-entry {pack_id: cached_version} extraction
# ---------------------------------------------------------------------------


def test_cached_pack_refs_reads_gather_binding():
    deps = _SentinelAnalystDeps(
        gather_binding=_Binding(_Pack("journal_read", "a" * 16)),
    )
    assert dapr_actors._cached_pack_refs(deps) == {"journal_read": "a" * 16}


def test_cached_pack_refs_reads_gather_write_bindings_deduped():
    # Two tool names sharing ONE binding (the real shape — see
    # dapr_host's per-pack loop, one _binding object fans into _bindings[tn]
    # for every tool the pack owns) must yield ONE pack entry, not two.
    binding = _Binding(_Pack("propose_facts", "b" * 16))
    deps = _SentinelAnalystDeps(
        gather_write_bindings={"bindings": {"write_fact": binding, "write_nexus": binding}},
    )
    assert dapr_actors._cached_pack_refs(deps) == {"propose_facts": "b" * 16}


def test_cached_pack_refs_reads_escalation_binding():
    deps = _SentinelAnalystDeps(
        escalation=_Escalation(_Binding(_Pack("escalate_finding", "c" * 16))),
    )
    assert dapr_actors._cached_pack_refs(deps) == {"escalate_finding": "c" * 16}


def test_cached_pack_refs_merges_all_three_sources():
    deps = _SentinelAnalystDeps(
        gather_binding=_Binding(_Pack("journal_read", "a" * 16)),
        gather_write_bindings={"bindings": {"propose_fact": _Binding(_Pack("journal_propose", "b" * 16))}},
        escalation=_Escalation(_Binding(_Pack("escalate_finding", "c" * 16))),
    )
    assert dapr_actors._cached_pack_refs(deps) == {
        "journal_read": "a" * 16,
        "journal_propose": "b" * 16,
        "escalate_finding": "c" * 16,
    }


def test_cached_pack_refs_empty_when_no_bindings():
    assert dapr_actors._cached_pack_refs(_SentinelAnalystDeps()) == {}


def test_cached_pack_refs_skips_a_binding_with_no_identity():
    # Defensive — a binding whose .pack lacks a recognizable .identity (or
    # has no .pack at all) must never raise, only be skipped (this is a
    # diagnostic sweep, not a correctness gate).
    class _WeirdBinding:
        pack = object()  # no .identity at all

    deps = _SentinelAnalystDeps(gather_binding=_WeirdBinding())
    assert dapr_actors._cached_pack_refs(deps) == {}


def test_cached_pack_refs_skips_a_gather_binding_that_is_none():
    # The real byte-for-byte common case — most cached analysts have NO
    # gather_binding at all (None is the _AnalystDeps default).
    deps = _SentinelAnalystDeps(gather_binding=None)
    assert dapr_actors._cached_pack_refs(deps) == {}


# ---------------------------------------------------------------------------
# warn_stale_pack_deps — the full sweep over the process-global cache
# ---------------------------------------------------------------------------


async def test_warn_stale_pack_deps_detects_a_stale_cached_binding():
    """The live lens_diff shape: the analyst's cached deps still carry the
    PRE-PUT pack version; the (injected) head-version fetch reports a NEWER
    version. The sweep must report exactly one stale pair and never raise or
    mutate the cache (WARN-only, per the task's smaller fallback scope)."""
    dapr_actors.clear_deps_registry()
    try:
        dapr_actors._ANALYST_DEPS["analyst::lens_diff::deadbeefdeadbeef"] = (
            _SentinelAnalystDeps(
                gather_binding=_Binding(_Pack("journal_read", "old" + "0" * 13)),
            )
        )

        async def fetch_head(pack_id: str) -> str | None:
            assert pack_id == "journal_read"
            return "new" + "0" * 13  # PUT landed a new head

        stale = await dapr_actors.warn_stale_pack_deps(fetch_head)
        assert stale == 1
        # WARN-only — the cache entry itself is untouched (no eviction).
        assert "analyst::lens_diff::deadbeefdeadbeef" in dapr_actors._ANALYST_DEPS
    finally:
        dapr_actors.clear_deps_registry()


async def test_warn_stale_pack_deps_silent_when_versions_match():
    dapr_actors.clear_deps_registry()
    try:
        dapr_actors._ANALYST_DEPS["analyst::lens_diff::deadbeefdeadbeef"] = (
            _SentinelAnalystDeps(
                gather_binding=_Binding(_Pack("journal_read", "same" + "0" * 12)),
            )
        )

        async def fetch_head(pack_id: str) -> str | None:
            return "same" + "0" * 12  # head == cached — nothing stale

        assert await dapr_actors.warn_stale_pack_deps(fetch_head) == 0
    finally:
        dapr_actors.clear_deps_registry()


async def test_warn_stale_pack_deps_fetches_each_unique_pack_once():
    """Two cached actors both reference journal_read — the head-version
    fetch must be called ONCE for that pack id, not once per referencing
    actor (the whole point of resolving unique_pack_ids first)."""
    dapr_actors.clear_deps_registry()
    try:
        pack = _Pack("journal_read", "v1" + "0" * 14)
        dapr_actors._ANALYST_DEPS["analyst::lens_diff::aaaa"] = _SentinelAnalystDeps(
            gather_binding=_Binding(pack),
        )
        dapr_actors._ANALYST_DEPS["analyst::lens_trend::bbbb"] = _SentinelAnalystDeps(
            gather_binding=_Binding(pack),
        )
        calls: list[str] = []

        async def fetch_head(pack_id: str) -> str | None:
            calls.append(pack_id)
            return "v1" + "0" * 14  # matches — not stale

        assert await dapr_actors.warn_stale_pack_deps(fetch_head) == 0
        assert calls == ["journal_read"]
    finally:
        dapr_actors.clear_deps_registry()


async def test_warn_stale_pack_deps_missing_pack_is_not_reported_stale():
    """A fetch miss (pack retired/unreachable) returns None — that is NOT the
    same claim as "stale"; a diagnostic sweep must not manufacture a false
    positive out of a transient/absent lookup."""
    dapr_actors.clear_deps_registry()
    try:
        dapr_actors._ANALYST_DEPS["analyst::lens_diff::cccc"] = _SentinelAnalystDeps(
            gather_binding=_Binding(_Pack("journal_read", "v1" + "0" * 14)),
        )

        async def fetch_head(pack_id: str) -> str | None:
            return None

        assert await dapr_actors.warn_stale_pack_deps(fetch_head) == 0
    finally:
        dapr_actors.clear_deps_registry()


async def test_warn_stale_pack_deps_one_bad_fetch_does_not_sink_the_sweep():
    """Best-effort per the reminder_gc precedent — a raising fetch for ONE
    pack id must not abort the sweep for other cached entries."""
    dapr_actors.clear_deps_registry()
    try:
        dapr_actors._ANALYST_DEPS["analyst::flaky::aaaa"] = _SentinelAnalystDeps(
            gather_binding=_Binding(_Pack("flaky_pack", "v1" + "0" * 14)),
        )
        dapr_actors._ANALYST_DEPS["analyst::lens_diff::bbbb"] = _SentinelAnalystDeps(
            gather_binding=_Binding(_Pack("journal_read", "old" + "0" * 13)),
        )

        async def fetch_head(pack_id: str) -> str | None:
            if pack_id == "flaky_pack":
                raise RuntimeError("registry unreachable")
            return "new" + "0" * 13

        # The flaky pack's fetch failure is swallowed (logged); journal_read
        # is still correctly evaluated as stale.
        assert await dapr_actors.warn_stale_pack_deps(fetch_head) == 1
    finally:
        dapr_actors.clear_deps_registry()


async def test_warn_stale_pack_deps_no_op_on_empty_cache():
    dapr_actors.clear_deps_registry()
    try:
        calls: list[str] = []

        async def fetch_head(pack_id: str) -> str | None:
            calls.append(pack_id)
            return None

        assert await dapr_actors.warn_stale_pack_deps(fetch_head) == 0
        assert calls == []  # no cached entries -> no fetches at all
    finally:
        dapr_actors.clear_deps_registry()
