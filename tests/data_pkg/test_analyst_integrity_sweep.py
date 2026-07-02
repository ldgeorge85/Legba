# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the ``integrity_sweep`` deterministic sub-handler (DIRECTION §9).

Covers the two properties that distinguish it from the 2.4-deleted
``integrity_verification`` predecessor: it **refuses loud** (a missing relation
or absent pool raises rather than zeroing) and it emits an **honest** summary
(a 0-issue finding only ever means the checks genuinely ran clean).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
)
from legba.data.analysts.deterministic_handlers import integrity_sweep
from legba.runtime.analyst_method import AnalystMethodResult


class _FakeConn:
    def __init__(
        self,
        values: list[Any],
        sample_rows: list[dict[str, Any]] | None = None,
        probe_rows: dict[str, list[dict[str, Any]]] | None = None,
    ):
        self._values = list(values)
        self.calls = 0
        self._sample_rows = sample_rows
        # P1-T8: per-probe rows, routed by the SQL comment marker so the dangling
        # C5 sample and the three reachable-click-path probes never cross-feed.
        self._probe_rows = probe_rows or {}
        self.fetch_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        v = self._values[self.calls]
        self.calls += 1
        if isinstance(v, Exception):
            raise v
        return v

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.fetch_calls.append((sql, args))
        if "probe:dangling_finding" in sql:
            rows = self._probe_rows.get("dangling_finding", [])
        elif "probe:bodyless_root" in sql:
            rows = self._probe_rows.get("bodyless_root", [])
        elif "probe:orphan_receipt" in sql:
            rows = self._probe_rows.get("orphan_receipt", [])
        else:  # the C5 dangling-derived_from sample (has min(ao.id) AS sample_output_id)
            rows = self._sample_rows or []
        # Honor the LIMIT $1 contract the same way pg would.
        cap = args[0] if args else len(rows)
        return rows[:cap]


class _NoFetchConn(_FakeConn):
    """A connection that cannot run a multi-row fetch (older probe). The sample
    must degrade to empty rather than fabricating one."""

    fetch = None  # type: ignore[assignment]


class _FakeAcquire:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self.conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self.conn)


class _FakeDeps:
    def __init__(self, pool: Any):
        self.pg_pool = pool


def _run(
    values: list[Any] | None,
    options: dict[str, Any] | None = None,
    *,
    sample_rows: list[dict[str, Any]] | None = None,
    probe_rows: dict[str, list[dict[str, Any]]] | None = None,
    conn_cls: type[_FakeConn] = _FakeConn,
):
    pool = None
    if values is not None:
        pool = _FakePool(
            conn_cls(values, sample_rows=sample_rows, probe_rows=probe_rows)
        )
    deps = _FakeDeps(pool)
    return asyncio.run(integrity_sweep.handle([], options or {}, deps))


def test_registered_in_dispatch_tables() -> None:
    assert SUB_HANDLERS.get("integrity_sweep") is integrity_sweep.handle
    assert "integrity_sweep" in OUTPUT_KIND_BY_SUB_HANDLER


def test_clean_sweep_emits_honest_zero_finding() -> None:
    res = _run([0, 0, 0, 0, 0, 0, 0])  # all seven checks clean
    assert isinstance(res, AnalystMethodResult)
    assert res.finding.data["total_issues"] == 0
    assert "integrity_clean" in res.finding.tags
    assert "integrity_issues_present" not in res.finding.tags
    assert len(res.finding.data["issues"]) == 7  # every check ran
    assert res.usage["completion_tokens"] == 0  # deterministic: zero LLM spend


def test_issues_surface_with_counts_and_tag() -> None:
    res = _run([0, 0, 7, 24, 0, 0, 0])  # the live proposed_edges orphan counts
    assert res.finding.data["total_issues"] == 31
    assert res.finding.data["issues"]["orphan_proposed_edges_source"] == 7
    assert res.finding.data["issues"]["orphan_proposed_edges_target"] == 24
    assert "integrity_issues_present" in res.finding.tags
    assert "integrity_clean" not in res.finding.tags


def test_dangling_derived_from_check_registered_and_counted() -> None:
    """D23/D10: the dangling-derived_from audit is the LAST check and its count
    surfaces under the documented issue key."""
    # Check order: signal/signal, signal/entity, pe/source, pe/target,
    # facts_no_evidence, broken_supersession, dangling_derived_from.
    res = _run([0, 0, 0, 0, 0, 0, 101506])
    issues = res.finding.data["issues"]
    assert "dangling_analyst_output_derived_from" in issues
    assert issues["dangling_analyst_output_derived_from"] == 101506
    assert res.finding.data["total_issues"] == 101506
    assert "integrity_issues_present" in res.finding.tags


def test_dangling_derived_from_check_is_last_check_key() -> None:
    """Pin the new check's identity + position so ordering drift is caught."""
    keys = [k for k, _sql in integrity_sweep._CHECKS]
    assert keys[-1] == "dangling_analyst_output_derived_from"
    assert len(keys) == 7
    # The SQL references analyst_outputs.derived_from + the lineage tables.
    sql = dict(integrity_sweep._CHECKS)["dangling_analyst_output_derived_from"]
    assert "unnest(ao.derived_from)" in sql
    for tbl in ("signals", "analyst_outputs", "facts", "entity_profiles"):
        assert tbl in sql


def test_no_pool_refuses_loud() -> None:
    # No live substrate → raise, never a zeroed finding.
    with pytest.raises(RuntimeError, match="requires a live deps.pg_pool"):
        _run(None)


def test_missing_relation_refuses_loud() -> None:
    # A failing check (e.g. a dropped relation) MUST propagate, not be swallowed
    # into a zeroed clean finding — the exact predecessor bug this re-home fixes.
    boom = RuntimeError('relation "proposed_edges" does not exist')
    with pytest.raises(RuntimeError, match="does not exist"):
        _run([0, 0, boom, 0, 0, 0, 0])


# ---------------------------------------------------------------------------
# P0-T5: dangling derived_from capped SAMPLE (read-only) + prune migration
# ---------------------------------------------------------------------------


def test_dangling_sample_surfaces_when_count_positive() -> None:
    """When the dangling count is >0 the finding carries a capped sample of the
    dead refs (ref + an owning output id) under the documented data key + body."""
    rows = [
        {"ref": "11111111-1111-1111-1111-111111111111",
         "sample_output_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
        {"ref": "22222222-2222-2222-2222-222222222222",
         "sample_output_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"},
    ]
    res = _run([0, 0, 0, 0, 0, 0, 2], sample_rows=rows)
    sample = res.finding.data["dangling_derived_from_sample"]
    assert [s["ref"] for s in sample] == [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]
    assert sample[0]["sample_output_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert res.finding.data["dangling_derived_from_sample_cap"] == \
        integrity_sweep._DANGLING_SAMPLE_CAP
    # The sample appears in the human-readable body too.
    assert "dangling_derived_from_sample" in res.finding.body
    assert "11111111-1111-1111-1111-111111111111" in res.finding.body


def test_no_sample_probe_when_count_is_zero() -> None:
    """The capped sample only runs when there IS dead debt — a clean dangling
    count must NOT pay for the sample query (no fetch call, empty sample)."""
    pool = _FakePool(_FakeConn([0, 0, 0, 0, 0, 0, 0], sample_rows=[{"ref": "x"}]))
    deps = _FakeDeps(pool)
    res = asyncio.run(integrity_sweep.handle([], {}, deps))
    assert res.finding.data["dangling_derived_from_sample"] == []
    # The C5 dangling-derived_from SAMPLE (count 0) is skipped entirely; the only
    # fetch with the C5 sample SQL must be absent. (The P1-T8 reachable-click-path
    # probe always runs — those fetches are routed by a different marker.)
    c5_calls = [c for c in pool.conn.fetch_calls if "sample_output_id" in c[0]]
    assert c5_calls == []


def test_sample_probe_passes_cap_as_limit_param() -> None:
    """The sample probe binds the cap as the LIMIT $1 parameter (not inlined)."""
    rows = [{"ref": f"{i}", "sample_output_id": f"o{i}"} for i in range(3)]
    pool = _FakePool(_FakeConn([0, 0, 0, 0, 0, 0, 3], sample_rows=rows))
    deps = _FakeDeps(pool)
    asyncio.run(integrity_sweep.handle([], {}, deps))
    # Filter to the C5 dangling SAMPLE call (the P1-T8 probe also fetches).
    c5_calls = [c for c in pool.conn.fetch_calls if "sample_output_id" in c[0]]
    assert len(c5_calls) == 1
    sql, args = c5_calls[0]
    assert args == (integrity_sweep._DANGLING_SAMPLE_CAP,)
    assert "LIMIT $1" in sql
    # Probe catalog must mirror the COUNT check's four lineage tables exactly.
    for tbl in ("signals", "analyst_outputs", "facts", "entity_profiles"):
        assert tbl in sql
    assert "unnest(ao.derived_from)" in sql


def test_sample_degrades_to_empty_when_fetch_unavailable() -> None:
    """Honesty: a connection with no multi-row fetch degrades to an EMPTY sample
    rather than fabricating one — the audit count is unaffected, refuse loud is
    only for the count, the sample is best-effort diagnostic."""
    res = _run([0, 0, 0, 0, 0, 0, 5], conn_cls=_NoFetchConn)
    assert res.finding.data["dangling_derived_from_sample"] == []
    # The count still surfaced.
    assert res.finding.data["issues"]["dangling_analyst_output_derived_from"] == 5


# ---------------------------------------------------------------------------
# P1-T8: reachable-click-path PROBE — confirm zero dead-ends on the click path
# ---------------------------------------------------------------------------

_PROBE_KEYS = (
    "dangling_finding_derived_from",
    "bodyless_finding_roots",
    "orphaned_receipt_links",
)


def _run_probe(
    probe_rows: dict[str, list[dict[str, Any]]] | None = None,
    *,
    cap: int | None = None,
    conn_cls: type[_FakeConn] = _FakeConn,
):
    conn = conn_cls([], probe_rows=probe_rows)
    pool = _FakePool(conn)
    kwargs = {} if cap is None else {"cap": cap}
    result = asyncio.run(
        integrity_sweep.probe_reachable_click_path(pool, **kwargs)
    )
    return result, conn


def test_probe_clean_returns_three_zero_counts() -> None:
    """A clean substrate (no planted dead-ends) → every class count 0, sample []."""
    result, _conn = _run_probe({})
    assert set(result) == set(_PROBE_KEYS)
    for k in _PROBE_KEYS:
        assert result[k] == {"count": 0, "sample": []}


def test_probe_dangling_finding_planted_named_list() -> None:
    """A finding whose derived_from points at a non-existent row surfaces as a
    NAMED (finding_id, dangling_ref) sample; the count is the TRUE window total,
    independent of the capped sample length."""
    rows = [
        {"finding_id": "f1", "dangling_ref": "d1", "total": 3},
        {"finding_id": "f2", "dangling_ref": "d2", "total": 3},
    ]
    result, _conn = _run_probe({"dangling_finding": rows})
    entry = result["dangling_finding_derived_from"]
    assert entry["count"] == 3  # true total > sampled (cap simulated)
    assert entry["sample"] == [
        {"finding_id": "f1", "dangling_ref": "d1"},
        {"finding_id": "f2", "dangling_ref": "d2"},
    ]
    # The other two classes stay clean.
    assert result["bodyless_finding_roots"] == {"count": 0, "sample": []}
    assert result["orphaned_receipt_links"] == {"count": 0, "sample": []}


def test_probe_bodyless_root_planted_named_list() -> None:
    """A body-less LIVE finding root surfaces as a NAMED finding_id sample."""
    rows = [{"finding_id": "blank-1", "total": 1}]
    result, _conn = _run_probe({"bodyless_root": rows})
    entry = result["bodyless_finding_roots"]
    assert entry["count"] == 1
    assert entry["sample"] == [{"finding_id": "blank-1"}]
    assert result["dangling_finding_derived_from"]["count"] == 0


def test_probe_orphaned_receipt_planted_named_list() -> None:
    """A run whose prev_receipt_hash has no predecessor surfaces as a NAMED
    (run_id, analyst_id, prev_receipt_hash) sample."""
    rows = [
        {
            "run_id": "run-9",
            "analyst_id": "country_assessor",
            "prev_receipt_hash": "deadbeef",
            "total": 1,
        }
    ]
    result, _conn = _run_probe({"orphan_receipt": rows})
    entry = result["orphaned_receipt_links"]
    assert entry["count"] == 1
    assert entry["sample"] == [
        {
            "run_id": "run-9",
            "analyst_id": "country_assessor",
            "prev_receipt_hash": "deadbeef",
        }
    ]


def test_probe_binds_cap_and_excludes_genesis_zero_hash() -> None:
    """Each probe binds the cap as LIMIT $1; the receipt probe also binds the
    genesis ZERO_HASH ($2) and EXCLUDES it (genesis is not an orphan)."""
    _result, conn = _run_probe({}, cap=7)
    assert len(conn.fetch_calls) == 3
    by_marker: dict[str, tuple[str, tuple[Any, ...]]] = {}
    for sql, args in conn.fetch_calls:
        if "probe:dangling_finding" in sql:
            by_marker["dangling"] = (sql, args)
        elif "probe:bodyless_root" in sql:
            by_marker["bodyless"] = (sql, args)
        elif "probe:orphan_receipt" in sql:
            by_marker["orphan"] = (sql, args)
    assert set(by_marker) == {"dangling", "bodyless", "orphan"}
    # cap bound as the first param of every probe.
    assert by_marker["dangling"][1] == (7,)
    assert by_marker["bodyless"][1] == (7,)
    assert by_marker["orphan"][1] == (7, integrity_sweep.ZERO_HASH)
    for sql, _args in by_marker.values():
        assert "LIMIT $1" in sql
        assert "count(*) OVER ()" in sql  # true full count, not sample length
    # The dangling probe is FINDING-scoped over the seven-table superset catalog
    # (mirrors migration 0056 so it only flags a TRUE dead-end).
    dsql = by_marker["dangling"][0]
    assert "kind = 'finding'" in dsql
    for tbl in ("signals", "analyst_outputs", "facts", "situations",
                "hypotheses", "entity_profiles", "nexuses"):
        assert tbl in dsql
    # The receipt probe excludes the genesis hash via the bound $2 param.
    osql = by_marker["orphan"][0]
    assert "prev_receipt_hash <> $2" in osql
    assert "analyst_traces" in osql


def test_probe_degrades_to_zero_when_fetch_unavailable() -> None:
    """Honesty: a connection that cannot multi-row fetch degrades to all-zero
    counts rather than fabricating a dead-end (no fetch attempted)."""
    result, conn = _run_probe({"dangling_finding": [{"finding_id": "x",
                                                     "dangling_ref": "y",
                                                     "total": 9}]},
                              conn_cls=_NoFetchConn)
    for k in _PROBE_KEYS:
        assert result[k] == {"count": 0, "sample": []}
    assert conn.fetch_calls == []  # never attempted the multi-row fetch


def test_handle_clean_path_confirms_zero_dead_ends() -> None:
    """A clean sweep CONFIRMS the navigable read: finding data carries the three
    probe classes at 0 and the click_path_clean tag."""
    res = _run([0, 0, 0, 0, 0, 0, 0])  # all checks clean, no probe rows
    probe = res.finding.data["reachable_click_path"]
    assert set(probe) == set(_PROBE_KEYS)
    for k in _PROBE_KEYS:
        assert probe[k] == {"count": 0, "sample": []}
    assert res.finding.data["reachable_click_path_dead_ends"] == 0
    assert "click_path_clean" in res.finding.tags
    assert "click_path_dead_ends" not in res.finding.tags


def test_handle_surfaces_probe_dead_ends_in_data_and_body() -> None:
    """When the click path WOULD dead-end the finding names the offending nodes in
    both data['reachable_click_path'] and the human-readable body, and is tagged
    click_path_dead_ends."""
    probe_rows = {
        "dangling_finding": [
            {"finding_id": "FIND-A", "dangling_ref": "GONE-REF", "total": 1}
        ],
        "bodyless_root": [{"finding_id": "BLANK-B", "total": 1}],
        "orphan_receipt": [
            {"run_id": "RUN-C", "analyst_id": "world_assessor",
             "prev_receipt_hash": "ORPHAN-H", "total": 1}
        ],
    }
    res = _run([0, 0, 0, 0, 0, 0, 0], probe_rows=probe_rows)
    probe = res.finding.data["reachable_click_path"]
    assert probe["dangling_finding_derived_from"]["count"] == 1
    assert probe["bodyless_finding_roots"]["count"] == 1
    assert probe["orphaned_receipt_links"]["count"] == 1
    assert res.finding.data["reachable_click_path_dead_ends"] == 3
    assert "click_path_dead_ends" in res.finding.tags
    assert "click_path_clean" not in res.finding.tags
    # The named nodes appear in the body so an operator can pinpoint each.
    body = res.finding.body
    assert "FIND-A" in body and "GONE-REF" in body
    assert "BLANK-B" in body
    assert "RUN-C" in body and "ORPHAN-H" in body


def test_handle_probe_runs_even_when_other_checks_clean() -> None:
    """The probe is INDEPENDENT of the seven integrity checks — a fully clean
    integrity sweep still pays for + reports the reachable-click-path probe."""
    probe_rows = {"bodyless_root": [{"finding_id": "blank", "total": 4}]}
    res = _run([0, 0, 0, 0, 0, 0, 0], probe_rows=probe_rows)
    # integrity total is 0 but the click-path probe still found a dead-end.
    assert res.finding.data["total_issues"] == 0
    assert "integrity_clean" in res.finding.tags
    assert res.finding.data["reachable_click_path"]["bodyless_finding_roots"][
        "count"
    ] == 4
    assert "click_path_dead_ends" in res.finding.tags


# ---- the prune migration (authored, not run) ----

import pathlib  # noqa: E402

_MIGRATIONS_DIR = (
    pathlib.Path(integrity_sweep.__file__).resolve()
    .parents[2]  # .../legba/data
    / "migrations"
)
_PRUNE_V2 = _MIGRATIONS_DIR / "0056_prune_dangling_derived_from_v2.sql"
_PRUNE_V1 = _MIGRATIONS_DIR / "0051_prune_dangling_derived_from.sql"


def test_prune_v2_migration_exists_with_next_free_number() -> None:
    assert _PRUNE_V2.exists(), f"missing prune migration: {_PRUNE_V2}"
    # 0056 was the next free number when this prune migration was added (head
    # 0055 then). Later migrations (0057+) legitimately follow, so assert 0056
    # exists and followed 0055 with no gap — NOT that it is the GLOBAL head,
    # which regressed this test every time a new migration landed.
    nums = sorted(
        int(p.name[:4])
        for p in _MIGRATIONS_DIR.glob("*.sql")
        if p.name[:4].isdigit()
    )
    assert 56 in nums
    assert 55 in nums


def test_prune_v2_follows_0051_guarded_idempotent_style() -> None:
    body = _PRUNE_V2.read_text(encoding="utf-8")
    # NULL-OUT via array filter, NOT a row delete.
    assert "UPDATE analyst_outputs" in body
    assert "DELETE FROM analyst_outputs" not in body
    # Idempotency guards: only touch rows with a dangling element + non-empty.
    assert "array_length(ao.derived_from, 1) IS NOT NULL" in body
    assert "EXISTS" in body
    assert "COALESCE" in body and "'{}'::uuid[]" in body  # NOT-NULL contract
    # The seven-table superset catalog (>= the sweep's four-table audit) — so the
    # prune can only KEEP more edges than the audit counts resolvable.
    for tbl in ("signals", "analyst_outputs", "facts", "situations",
                "hypotheses", "entity_profiles", "nexuses"):
        assert tbl in body
    # SPDX header present, like 0051.
    assert "SPDX-License-Identifier: AGPL-3.0-or-later" in body


def test_prune_v2_update_predicate_matches_0051() -> None:
    """The actual repair logic is byte-stable with 0051 (the established pattern):
    same UPDATE ... SET derived_from = COALESCE(... array_agg ...) body."""
    v2 = _PRUNE_V2.read_text(encoding="utf-8")
    v1 = _PRUNE_V1.read_text(encoding="utf-8")

    def _statement(text: str) -> str:
        # The single SQL statement: from the first UPDATE to its terminating ';'.
        start = text.index("UPDATE analyst_outputs")
        end = text.index(";", start)
        # Collapse whitespace so comment-formatting differences don't matter.
        return " ".join(text[start:end].split())

    assert _statement(v2) == _statement(v1)
