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
        delivery_rows: list[dict[str, Any]] | None = None,
    ):
        self._values = list(values)
        self.calls = 0
        self._sample_rows = sample_rows
        # P1-T8: per-probe rows, routed by the SQL comment marker so the dangling
        # C5 sample and the three reachable-click-path probes never cross-feed.
        self._probe_rows = probe_rows or {}
        # W1-T3: delivery-failure canary rows, routed by their own marker.
        self._delivery_rows = delivery_rows
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
        elif "canary:delivery_failures" in sql:
            rows = self._delivery_rows or []
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
    delivery_rows: list[dict[str, Any]] | None = None,
    conn_cls: type[_FakeConn] = _FakeConn,
):
    pool = None
    if values is not None:
        pool = _FakePool(
            conn_cls(
                values,
                sample_rows=sample_rows,
                probe_rows=probe_rows,
                delivery_rows=delivery_rows,
            )
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


# ---------------------------------------------------------------------------
# S8-T5(a): the critique analyzed_output_id partial expression index (0059).
# Static (no-DB) shape checks — the live idempotency/apply is exercised by the
# integration migration suite (tests/data_pkg/test_migrations.py, DB-gated).
# ---------------------------------------------------------------------------

_CRITIQUE_IDX = _MIGRATIONS_DIR / "0059_critique_analyzed_output_id_index.sql"


def test_critique_index_migration_exists() -> None:
    assert _CRITIQUE_IDX.exists(), f"missing critique-index migration: {_CRITIQUE_IDX}"
    # Renumbered to 0059 at integration (0058 was taken by the composition-
    # supersession fold, landed in the same batch). Assert 0059 exists and 0058
    # precedes it with no gap — NOT that it is the GLOBAL head.
    nums = sorted(
        int(p.name[:4])
        for p in _MIGRATIONS_DIR.glob("*.sql")
        if p.name[:4].isdigit()
    )
    assert 59 in nums
    assert 58 in nums


def test_critique_index_migration_is_idempotent_partial_expression_index() -> None:
    body = _CRITIQUE_IDX.read_text(encoding="utf-8")
    # Idempotent, additive, forward-only — CREATE INDEX IF NOT EXISTS only.
    assert "CREATE INDEX IF NOT EXISTS" in body
    assert "DROP INDEX" not in body
    assert "DELETE" not in body and "UPDATE " not in body  # no data migration
    # Targets the analyst_outputs table, the analyzed_output_id JSONB expression,
    # and is PARTIAL to the critique subset (matches the join's kind filter).
    assert "public.analyst_outputs" in body
    assert "(data->>'analyzed_output_id')" in body
    assert "WHERE kind = 'critique'" in body
    # Not built CONCURRENTLY (the runner wraps each migration in a transaction).
    assert "CREATE INDEX CONCURRENTLY" not in body
    # SPDX header present, like the neighbouring migrations.
    assert "SPDX-License-Identifier: AGPL-3.0-or-later" in body


# ---------------------------------------------------------------------------
def test_dangling_sample_sql_casts_uuid_for_min() -> None:
    """min(uuid) has no Postgres aggregate — the dangling sample must aggregate
    over the text cast, else the WHOLE sweep hard-fails ('function min(uuid) does
    not exist') whenever a dangling ref exists. Pin the cast (the fake-conn tests
    can't catch a real-SQL planning error)."""
    sql = integrity_sweep._DANGLING_SAMPLE_SQL
    assert "min(ao.id::text)::uuid" in sql
    assert "min(ao.id) " not in sql and "min(ao.id)\n" not in sql


# ---------------------------------------------------------------------------
# W1-T3: delivery-failure CANARY — goes LOUD when alert deliveries fail, stays
# silent (no false positive) when the 24h window is clean.
# ---------------------------------------------------------------------------


def test_delivery_query_shape_targets_nondelivery_24h_window() -> None:
    """Static SQL shape: the canary counts NON-DELIVERED alerts (failed +
    logged_only) in a 24h window, grouped by (sink_kind, status), most-failing
    first, over the audit table."""
    sql = integrity_sweep._DELIVERY_FAILURE_SQL
    assert "alert_sink_deliveries" in sql
    assert "status IN ('failed', 'logged_only')" in sql
    assert "interval '24 hours'" in sql
    assert "attempted_at > now()" in sql
    assert "GROUP BY sink_kind, status" in sql
    assert "ORDER BY n DESC" in sql
    assert "canary:delivery_failures" in sql  # routable marker
    assert integrity_sweep._DELIVERY_FAILURE_WINDOW_HOURS == 24


def test_delivery_severity_scales_with_volume() -> None:
    """A handful reads low; a sink failing wholesale (pushover 552) reads high."""
    assert integrity_sweep._delivery_severity(1) == "low"
    assert integrity_sweep._delivery_severity(9) == "low"
    assert integrity_sweep._delivery_severity(10) == "elevated"
    assert integrity_sweep._delivery_severity(99) == "elevated"
    assert integrity_sweep._delivery_severity(100) == "high"
    assert integrity_sweep._delivery_severity(552) == "high"


def test_delivery_severity_split_floors_logged_only_at_low() -> None:
    """Hard 'failed' rows drive the band; a logged-only-only window floors at
    'low' (a by-design log-only channel never alone escalates); all-clean → None."""
    assert integrity_sweep._delivery_severity_split(0, 0) is None
    assert integrity_sweep._delivery_severity_split(0, 999) == "low"  # went-nowhere only
    assert integrity_sweep._delivery_severity_split(5, 0) == "low"
    assert integrity_sweep._delivery_severity_split(40, 500) == "elevated"  # failed drives it
    assert integrity_sweep._delivery_severity_split(200, 1) == "high"


def test_clean_delivery_window_emits_no_canary() -> None:
    """Zero recent failed deliveries → NO delivery-health tag/section/data (no
    false positive), while the rest of the sweep is unaffected."""
    res = _run([0, 0, 0, 0, 0, 0, 0], delivery_rows=[])
    tags = res.finding.tags
    assert "topic:delivery_health" not in tags
    assert not any(t.startswith("severity:") for t in tags)
    assert "alert_delivery_failures" not in res.finding.data
    assert "alert_delivery_failures_total" not in res.finding.data
    assert "alert_delivery_failures" not in res.finding.body
    # The finding still emits its honest integrity summary.
    assert "integrity_clean" in tags


def test_old_and_delivered_rows_do_not_trip_canary() -> None:
    """Rows outside the 24h window or with a non-'failed' status never reach the
    handler (the SQL filters them in the DB); the fake returns only the recent
    FAILED set, so an empty set → no canary. This pins the 'no false positive'
    contract at the handler boundary."""
    # The DB-side WHERE would have excluded old/delivered rows; the fake models a
    # window that filtered them all out.
    res = _run([0, 0, 0, 0, 0, 0, 0], delivery_rows=[])
    assert "topic:delivery_health" not in res.finding.tags


def test_wholesale_sink_failure_goes_loud_high_severity() -> None:
    """A sink failing wholesale (pushover 552/552 — the live-audit bug) surfaces
    LOUD: high severity tag, delivery-health topic tag, named breakdown in body
    and data, and the delivery-failure condition in the title."""
    rows = [
        {"sink_kind": "pushover", "status": "failed", "n": 552, "sample_err": "HTTP 400 invalid token"},
        {"sink_kind": "webhook", "status": "failed", "n": 3, "sample_err": "connection refused"},
    ]
    res = _run([0, 0, 0, 0, 0, 0, 0], delivery_rows=rows)
    tags = res.finding.tags
    assert "topic:delivery_health" in tags
    assert "severity:high" in tags  # 555 failed → high
    # Title names the delivery-failure condition + severity so it is identifiable.
    assert res.finding.title.startswith("ALERT DELIVERY FAILURE")
    assert "severity:high" in res.finding.title
    # Data carries the full breakdown + rollups (failed / logged_only split).
    data = res.finding.data
    assert data["alert_delivery_failures_total"] == 555
    assert data["alert_delivery_failures_failed"] == 555
    assert data["alert_delivery_failures_logged_only"] == 0
    assert data["alert_delivery_failures_severity"] == "high"
    assert data["alert_delivery_failures_window_hours"] == 24
    assert data["alert_delivery_failures"] == rows
    # Body names each failing sink + status + count + a sample error.
    body = res.finding.body
    assert "sink_kind=pushover status=failed n=552" in body
    assert "HTTP 400 invalid token" in body
    assert "sink_kind=webhook status=failed n=3" in body


def test_handful_of_failures_reads_low_severity() -> None:
    """A small transient failure count reads low, still surfacing the canary."""
    rows = [{"sink_kind": "webhook", "status": "failed", "n": 2, "sample_err": "timeout"}]
    res = _run([0, 0, 0, 0, 0, 0, 0], delivery_rows=rows)
    assert "topic:delivery_health" in res.finding.tags
    assert "severity:low" in res.finding.tags
    assert res.finding.data["alert_delivery_failures_total"] == 2


def test_delivery_canary_independent_of_integrity_issues() -> None:
    """The canary is orthogonal to the seven referential checks: it fires on
    delivery failures even when integrity is clean, and does not inflate
    total_issues (delivery failures are not referential-integrity issues)."""
    rows = [{"sink_kind": "pushover", "status": "failed", "n": 40, "sample_err": "boom"}]
    res = _run([0, 0, 0, 0, 0, 0, 0], delivery_rows=rows)
    assert res.finding.data["total_issues"] == 0  # NOT inflated by delivery count
    assert "integrity_clean" in res.finding.tags
    assert "topic:delivery_health" in res.finding.tags
    assert "severity:elevated" in res.finding.tags  # 40 → elevated


def test_logged_only_went_nowhere_surfaces_at_low_severity() -> None:
    """The silent 'went nowhere' case (emit logged_only — no publisher wired), the
    exact failure the canary exists for, surfaces — but FLOORS at low even at
    volume (a log-only channel can be a config choice, not an active break)."""
    rows = [{"sink_kind": "webhook", "status": "logged_only", "n": 300, "sample_err": None}]
    res = _run([0, 0, 0, 0, 0, 0, 0], delivery_rows=rows)
    tags = res.finding.tags
    assert "topic:delivery_health" in tags
    assert "severity:low" in tags  # logged-only floors at low despite 300 volume
    assert "severity:high" not in tags
    data = res.finding.data
    assert data["alert_delivery_failures_failed"] == 0
    assert data["alert_delivery_failures_logged_only"] == 300
    assert data["alert_delivery_failures_total"] == 300
    assert "went-nowhere" in res.finding.title
    assert "status=logged_only" in res.finding.body


def test_mixed_failed_and_logged_only_failed_drives_severity() -> None:
    """A mixed window: hard 'failed' drives the severity band, 'logged_only' is
    still surfaced (count + title), and neither inflates total_issues."""
    rows = [
        {"sink_kind": "pushover", "status": "failed", "n": 40, "sample_err": "boom"},
        {"sink_kind": "stix_bundle", "status": "logged_only", "n": 5, "sample_err": None},
    ]
    res = _run([0, 0, 0, 0, 0, 0, 0], delivery_rows=rows)
    data = res.finding.data
    assert data["alert_delivery_failures_failed"] == 40
    assert data["alert_delivery_failures_logged_only"] == 5
    assert data["alert_delivery_failures_total"] == 45
    assert "severity:elevated" in res.finding.tags  # 40 failed → elevated
    assert "40 failed + 5 went-nowhere" in res.finding.title


def test_delivery_canary_and_integrity_issues_both_present() -> None:
    """Both nonzero integrity issues AND delivery failures: the title composes
    both, and the delivery count does NOT inflate total_issues (orthogonal)."""
    rows = [{"sink_kind": "pushover", "status": "failed", "n": 5, "sample_err": "boom"}]
    res = _run([1, 0, 0, 0, 0, 0, 0], delivery_rows=rows)  # one integrity issue
    data = res.finding.data
    assert data["total_issues"] == 1  # unchanged by the delivery count
    assert data["alert_delivery_failures_total"] == 5
    # Title carries BOTH the delivery-failure prefix and the integrity summary tail.
    assert res.finding.title.startswith("ALERT DELIVERY FAILURE")
    assert "Integrity sweep: 1 issue(s)" in res.finding.title
    assert "integrity_issues_present" in res.finding.tags
    assert "topic:delivery_health" in res.finding.tags


def test_delivery_canary_refuses_loud_on_broken_read() -> None:
    """Fail-loud: a broken/missing alert_sink_deliveries read propagates rather
    than silently reporting a clean delivery window (the 552/552-silent bug)."""
    class _BoomConn(_FakeConn):
        async def fetch(self, sql: str, *args: Any):  # type: ignore[override]
            if "canary:delivery_failures" in sql:
                raise RuntimeError('relation "alert_sink_deliveries" does not exist')
            return await super().fetch(sql, *args)

    with pytest.raises(RuntimeError, match="alert_sink_deliveries"):
        _run([0, 0, 0, 0, 0, 0, 0], conn_cls=_BoomConn)
