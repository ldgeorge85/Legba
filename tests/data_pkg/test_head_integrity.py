# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""HEAD INTEGRITY (lane N-A) — the current head of a desk × unit is the newest
VERIFIED read, exactly one, and an old one is never presented as current.

The replay fixtures are shaped on the live CD escalation chain of 2026-11-11
(sweep_quality_1111b WR2): ``6a082ea8`` (11-03 07:04, signature-bearing) →
``bb2ce5fc`` (11-03 19:05, NO key_entities so no signature: the orphan) →
``e916d607`` … ``1423491b`` (11-10 19:02, verified) → ``fae8b3ca`` (11-11 07:02,
parse_failed, conf 0.30).
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from legba.data import carried_state as cs
from legba.data import head_integrity as hi
from legba.data.analysts import composition_window as cw
from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.analysts.deterministic_handlers import finding_supersession as fs

NOW = datetime(2026, 11, 11, 11, 32, tzinfo=timezone.utc)
SIG = "sig:country_watch_cd#dim:escalation"


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _desk_row(rid: str, at: str, *, entities: bool = True, failed: bool = False,
              analyst: str = "escalation", target: str = "country_watch_cd",
              sig: str | None = None) -> dict[str, Any]:
    inner: dict[str, Any] = {"category": target}
    if entities:
        inner["key_entities"] = ["unstructured"] if failed else ["escalation", "severity:elevated"]
    tags = ["unstructured", f"target:{target}"] if failed else ["escalation", f"target:{target}"]
    return {
        "id": rid, "produced_at": _ts(at), "analyst_id": analyst, "target_id": target,
        "situation_signature": sig, "title": rid,
        "data": {"tags": tags, "confidence": 0.3 if failed else 0.7, "data": inner},
    }


# ---------------------------------------------------------------------------
# Rule 1 — a failed parse is recorded, never a head
# ---------------------------------------------------------------------------


def test_pick_head_prefers_the_newest_structured_row():
    verified = _desk_row("1423491b", "2026-11-10T19:02:47")
    failed = _desk_row("fae8b3ca", "2026-11-11T07:02:28", failed=True)
    assert hi.pick_head([verified, failed])["id"] == "1423491b"
    assert fs._pick_latest([failed, verified])["id"] == "1423491b"


def test_pick_head_of_only_failed_rows_is_the_newest_failed_row():
    a = _desk_row("a", "2026-11-10T07:00:00", failed=True)
    b = _desk_row("b", "2026-11-11T07:00:00", failed=True)
    assert hi.pick_head([a, b])["id"] == "b"


def test_is_failed_parse_reads_payload_tags_and_persisted_data_tags():
    assert hi.is_failed_parse({"tags": ["coerce_failed"]})
    assert hi.is_failed_parse(_desk_row("x", "2026-11-11T00:00:00", failed=True))
    assert not hi.is_failed_parse(_desk_row("y", "2026-11-11T00:00:00"))
    assert not hi.is_failed_parse({"data": "not json"})


def test_synthetic_cluster_never_lets_the_failed_parse_supersede_the_verified_head():
    rows = [_desk_row("1423491b", "2026-11-10T19:02:47"),
            _desk_row("fae8b3ca", "2026-11-11T07:02:28", failed=True)]
    _, superseded, _, clusters = fs._resolve_synthetic(rows, sub_handler_fallback=None)
    (cluster,) = [c for c in clusters if c["reason"] == "signature_match"]
    assert cluster["latest_finding_id"] == "1423491b"
    assert cluster["superseded_finding_ids"] == ["fae8b3ca"]
    assert superseded == 1


# ---------------------------------------------------------------------------
# Rule 2 — one open row per key (the orphan close)
# ---------------------------------------------------------------------------


def _with_sigs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for r in rows:
        sig = fs.derive_signature(r["data"], analyst_id=r["analyst_id"])
        if sig:
            r["_sig"] = sig
    return rows


def test_key_fold_closes_a_signature_less_orphan_toward_the_head():
    rows = _with_sigs([
        _desk_row("bb2ce5fc", "2026-11-03T19:05:07", entities=False),
        _desk_row("1423491b", "2026-11-10T19:02:47"),
    ])
    assert "_sig" not in rows[0]  # the orphan's mechanism: no entities → no signature
    links = hi.plan_key_fold(rows)
    assert [(lk.superseded_id, lk.superseding_id, lk.reason) for lk in links] == [
        ("bb2ce5fc", "1423491b", hi.REASON_KEY_FOLD),
    ]


def test_key_fold_makes_a_newer_signature_less_read_the_head():
    """The 11-03 19:21 tick: the orphan was the NEWEST read; it must become the
    head (not be closed toward an older row), and 6a082ea8 closes toward it."""
    rows = _with_sigs([
        _desk_row("6a082ea8", "2026-11-03T07:04:27"),
        _desk_row("bb2ce5fc", "2026-11-03T19:05:07", entities=False),
    ])
    links = hi.plan_key_fold(rows)
    assert [(lk.superseded_id, lk.superseding_id) for lk in links] == [("6a082ea8", "bb2ce5fc")]


def test_key_fold_leaves_metrics_streams_explicit_situations_and_excluded_analysts():
    metrics = [
        {"id": "m1", "analyst_id": "integrity_sweep", "target_id": "t", "produced_at": _ts("2026-11-10T00:00:00"), "data": {}},
        {"id": "m2", "analyst_id": "integrity_sweep", "target_id": "t", "produced_at": _ts("2026-11-11T00:00:00"), "data": {}},
    ]
    explicit = [dict(_desk_row("s1", "2026-11-10T00:00:00"), _sig="sit:situation-a"),
                dict(_desk_row("s2", "2026-11-11T00:00:00"), _sig="sig:x")]
    comp = [dict(_desk_row("c1", "2026-11-10T00:00:00", analyst="country_composition"), _sig="sig:y"),
            dict(_desk_row("c2", "2026-11-11T00:00:00", analyst="country_composition"), _sig="sig:y")]
    assert hi.plan_key_fold(metrics) == []
    assert hi.plan_key_fold(explicit) == []  # s2 alone in its key once s1 is set aside
    assert hi.plan_key_fold(comp, exclude_analysts={"country_composition"}) == []


def test_cd_chain_replay_through_the_handler_write_path():
    """The 11-11 07:21 tick, pre-repair state: three open rows on CD escalation.
    The handler must leave exactly one open — the verified 11-10 head."""
    open_rows = [
        _desk_row("bb2ce5fc", "2026-11-03T19:05:07", entities=False),
        _desk_row("1423491b", "2026-11-10T19:02:47", sig=SIG),
        _desk_row("fae8b3ca", "2026-11-11T07:02:28", failed=True),
    ]
    conn = _PoolConn(open_rows)
    result = _run(fs._resolve_pool(_Pool(conn), produced_by="finding_supersession",
                                   analyst_id=None, owner_tenant=None, lookback_days=30,
                                   sub_handler_fallback=None))
    assert conn.superseded_by == {"fae8b3ca": "1423491b", "bb2ce5fc": "1423491b"}
    assert "1423491b" not in conn.superseded_by
    # The orphan's own signature column is not rewritten by the key fold.
    assert conn.sig_stamps.get("bb2ce5fc") is None
    reasons = {e[0]: e[3] for e in conn.edges}
    assert reasons["bb2ce5fc"] == hi.REASON_KEY_FOLD
    assert result[1] == 2  # superseded_count: one cluster link + one key fold


# ---------------------------------------------------------------------------
# Rule 3 — the gathers fold to the newest live row before the floor
# ---------------------------------------------------------------------------


class _CapturingConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        return []


def test_live_head_predicate_keeps_a_head_closed_by_a_failed_parse():
    pred = hi.live_head_predicate("f")
    assert "f.superseded_by IS NULL OR EXISTS" in pred
    assert "hs.id = f.superseded_by" in pred
    # The cheap column is checked before the superseder's data is detoasted.
    assert pred.index("hs.confidence < 0.31") < pred.index("hs.data -> 'tags'")
    gated = hi.live_head_predicate("f", gated=["X", "Y"])
    assert gated.startswith("(CASE WHEN f.superseded_by IS NULL OR EXISTS")
    assert "THEN (X AND Y) ELSE false END" in gated


@pytest.mark.asyncio
async def test_basis_gather_folds_to_one_row_per_key_inside_the_cte_before_the_floor():
    conn = _CapturingConn()
    await synth.read_other_analyst_findings(
        conn, analyst_ids=["escalation"], time_window_hours=336,
        target_id="country_watch_cd", verify_floor=0.5,
    )
    q = conn.calls[0][0]
    cte = q[: q.index("v AS MATERIALIZED")]
    assert "SELECT DISTINCT ON (f.analyst_id, f.target_id)" in cte
    assert "f.superseded_by IS NULL OR EXISTS" in cte
    # The row-content filters run inside the live-row CASE, not as bare quals.
    assert "THEN ((f.data -> 'data' ->> 'meta') IS DISTINCT FROM 'true' AND " \
           "(f.data -> 'tags' ?| array['unstructured','coerce_failed']) IS NOT TRUE)" in cte
    assert "LEAST(f.confidence, v.faithfulness_score) >=" not in cte


@pytest.mark.asyncio
async def test_theme_filter_applies_after_the_head_fold():
    conn = _CapturingConn()
    await synth.read_other_analyst_findings(
        conn, analyst_ids=["escalation"], time_window_hours=336, verify_floor=0.5,
        dedupe_heads=True, text_pattern="ebola",
    )
    q = conn.calls[0][0]
    cte = q[: q.index("v AS MATERIALIZED")]
    assert "~*" not in cte
    assert "~* $3" in q[q.index("JOIN v ON"):]


@pytest.mark.asyncio
async def test_fallback_gather_is_unchanged_newest_passing_regardless_of_supersession():
    conn = _CapturingConn()
    await synth.read_other_analyst_findings(
        conn, analyst_ids=["escalation"], time_window_hours=336,
        target_id="country_watch_cd", verify_floor=0.5, include_superseded=True,
    )
    q = conn.calls[0][0]
    assert "superseded_by" not in q
    cte = q[: q.index("v AS MATERIALIZED")]
    assert "DISTINCT ON" not in cte


@pytest.mark.asyncio
async def test_unfloored_head_fold_uses_the_live_predicate_without_a_prefold():
    conn = _CapturingConn()
    await synth.read_other_analyst_findings(
        conn, analyst_ids=["escalation"], time_window_hours=336, target_id="country_watch_cd",
    )
    q = conn.calls[0][0]
    assert hi.live_head_predicate("f") in q
    assert "MATERIALIZED" not in q


@pytest.mark.asyncio
async def test_periphery_gather_is_the_same_live_fold():
    conn = _CapturingConn()
    await cw.read_periphery_findings(
        conn, analyst_ids=["escalation"], time_window_hours=336, floor=0.5,
        target_id="country_watch_cd", text_pattern="ebola",
    )
    q = conn.calls[0][0]
    cte = q[: q.index("v AS MATERIALIZED")]
    assert "SELECT DISTINCT ON (f.analyst_id, f.target_id)" in cte
    assert "f.superseded_by IS NULL OR EXISTS" in cte
    assert "~*" not in cte


# ---------------------------------------------------------------------------
# The age bar on every live basis head
# ---------------------------------------------------------------------------


def _basis(rid: str, age_h: float, **extra: Any) -> dict[str, Any]:
    return {"id": rid, "analyst_id": "escalation", "target_id": "country_watch_cd",
            "produced_at": NOW - timedelta(hours=age_h), cs.UNIT_CADENCE_ROW_KEY: 11.0, **extra}


def test_head_age_bar_withholds_a_head_past_n_windows_and_keeps_a_young_one():
    old = _basis("bb2ce5fc", 184.45)
    young = _basis("1423491b", 16.5)
    periphery = _basis("p", 184.0, _evidence_tier="periphery")
    continuity = _basis("c", 184.0, _continuity=True)
    unmeasured = {k: v for k, v in _basis("u", 184.0).items() if k != cs.UNIT_CADENCE_ROW_KEY}
    rows = [old, young, periphery, continuity, unmeasured]
    assert cs.stamp_head_age_bar(rows, now=NOW, windows=3) == 1
    verdict = old[cs.CARRIED_ROW_KEY]
    assert verdict["cause"] == cs.CAUSE_HEAD_TOO_OLD
    assert verdict["limit_h"] == 33.0 and verdict["head_age_h"] == 184.45
    for r in (young, periphery, continuity, unmeasured):
        assert cs.CARRIED_ROW_KEY not in r
    kept, carried = cs.partition_carried([old, young])
    assert [r["id"] for r in kept] == ["1423491b"] and carried == [old]


def test_head_age_bar_keeps_an_existing_carry_verdict():
    row = _basis("x", 100.0, **{cs.CARRIED_ROW_KEY: {"cause": cs.CAUSE_NOT_RE_EVIDENCED}})
    assert cs.stamp_head_age_bar([row], now=NOW, windows=3) == 0
    assert row[cs.CARRIED_ROW_KEY]["cause"] == cs.CAUSE_NOT_RE_EVIDENCED


def test_head_too_old_is_named_with_its_date_never_as_current():
    old = _basis("bb2ce5fc", 184.45)
    cs.stamp_head_age_bar([old], now=NOW, windows=3)
    ledger = cw.build_coverage_ledger(["escalation"], [], [old], now=NOW)
    assert ledger[0]["status"] == cw.COVERAGE_CARRIED
    assert ledger[0]["cause"] == cs.CAUSE_HEAD_TOO_OLD
    text = cw.render_coverage_ledger_block(ledger, horizon_hours=336, floor=0.5, max_age_hours=None)
    assert "newest verified read is 184 h old" in text
    assert "never quoted as current" in text
    assert "withheld" in text


def test_fallback_and_head_bars_are_the_same_bar():
    row = _basis("x", 40.0)
    f = cs.fallback_verdict(row, cadence_h=11.0, now=NOW, windows=3)
    h = cs.head_age_verdict(row, cadence_h=11.0, now=NOW, windows=3)
    assert f is not None and h is not None
    assert {k: v for k, v in f.items() if k != "cause"} == {k: v for k, v in h.items() if k != "cause"}
    assert cs.head_age_verdict(_basis("y", 30.0), cadence_h=11.0, now=NOW, windows=3) is None


def test_absence_route_unit_heads_skip_failed_parse_rows():
    assert "'unstructured','coerce_failed'" in cs.UNIT_HEADS_SQL


# ---------------------------------------------------------------------------
# The one-off repair
# ---------------------------------------------------------------------------


def _load_repair():
    path = Path(__file__).resolve().parents[2] / "scripts" / "repair_desk_heads_once.py"
    spec = importlib.util.spec_from_file_location("repair_desk_heads_once", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cand(rid: str, at: str, *, sup: str | None = None, failed: bool = False,
          sig: str | None = SIG, bearing: bool = True) -> dict[str, Any]:
    return {"id": rid, "analyst_id": "escalation", "target_id": "country_watch_cd",
            "produced_at": _ts(at), "superseded_by": sup, "superseded_at": None,
            "situation_signature": sig,
            "tags": ["unstructured"] if failed else ["escalation"], "key_bearing": bearing}


CD_LIVE = [  # the live CANDIDATES_SQL rows for CD escalation, 2026-11-11
    _cand("bb2ce5fc", "2026-11-03T19:05:07", sig=None),
    _cand("1423491b", "2026-11-10T19:02:47", sup="fae8b3ca"),
    _cand("fae8b3ca", "2026-11-11T07:02:28", failed=True),
]


def test_repair_plan_on_the_cd_chain():
    repair = _load_repair()
    plan = repair.plan_repair(CD_LIVE)
    closes = {(lk.superseded_id, lk.superseding_id, lk.reason) for lk in plan["closes"]}
    assert closes == {("bb2ce5fc", "1423491b", hi.REASON_KEY_FOLD),
                      ("fae8b3ca", "1423491b", hi.REASON_PARSE_FAILED)}
    assert plan["restores"] == [("1423491b", "fae8b3ca")]
    assert plan["keys_multi_open"] == 1


def test_repair_plan_is_idempotent_after_apply():
    repair = _load_repair()
    after = [_cand("1423491b", "2026-11-10T19:02:47")]  # bb2ce5fc / fae8b3ca now closed
    plan = repair.plan_repair(after)
    assert plan["closes"] == [] and plan["restores"] == [] and plan["keys_multi_open"] == 0


def test_repair_plan_never_restores_a_head_a_verified_read_replaced():
    """UA escalation: 6fb1cfff was closed by the failed 7f6280e0, which a newer
    verified read 2ec108b4 has since closed. 6fb1cfff is history, not a head —
    it is not even a candidate (its superseder's superseder is verified)."""
    repair = _load_repair()
    rows = [_cand("2ec108b4", "2026-11-11T18:26:17")]
    assert repair.plan_repair(rows)["restores"] == []


class _RepairConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.executed: list[str] = []
        self.fetchvals: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        return self.rows

    async def fetchval(self, query: str, *params: Any) -> Any:
        self.fetchvals.append((query, params))
        return params[0]

    async def execute(self, query: str, *params: Any) -> str:
        self.executed.append(query)
        return "OK"

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                conn.executed.append("BEGIN")

            async def __aexit__(self, *exc):
                conn.executed.append("COMMIT")
                return False

        return _Tx()


def test_repair_dry_run_writes_nothing_and_names_the_ids():
    repair = _load_repair()
    conn = _RepairConn(CD_LIVE)
    out = _run(repair.repair_once(conn, exclude=["country_composition"], apply=False))
    assert conn.executed == [] and conn.fetchvals == []
    assert out["close_orphans"] == 1 and out["close_parse_failed"] == 1 and out["restore_heads"] == 1
    assert out["named"]["1423491b"].startswith("restore")


def test_repair_apply_backs_up_before_any_write():
    repair = _load_repair()
    conn = _RepairConn(CD_LIVE)
    out = _run(repair.repair_once(conn, exclude=[], apply=True, today=NOW))
    assert out["backup_table"] == "analyst_outputs_head_repair_20261111"
    assert conn.executed[0] == "BEGIN" and conn.executed[-1] == "COMMIT"
    assert "CREATE TABLE IF NOT EXISTS analyst_outputs_head_repair_20261111" in conn.executed[1]
    assert "INSERT INTO analyst_outputs_head_repair_20261111" in conn.executed[2]
    assert out["closed"] == 2 and out["restored"] == 1


# ---------------------------------------------------------------------------
# Fakes for the pool path
# ---------------------------------------------------------------------------


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class _PoolConn:
    """Enough of asyncpg for ``finding_supersession._resolve_pool``."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.superseded_by: dict[str, str] = {}
        self.sig_stamps: dict[str, str] = {}
        self.edges: list[tuple[Any, ...]] = []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        if "FROM analyst_outputs" in query and "superseded_by IS NULL" in query:
            return [dict(r) for r in self.rows]
        return []

    async def fetchval(self, query: str, *params: Any) -> Any:
        if "INSERT INTO finding_supersessions" in query:
            self.edges.append((params[0], params[1], params[2], params[3]))
            return params[0]
        if query.strip().startswith("SELECT situation_signature"):
            return next((r.get("situation_signature") for r in self.rows if r["id"] == params[0]), None)
        if "SET superseded_by = $2, superseded_at = NOW()" in query:
            if params[0] in self.superseded_by:
                return None
            self.superseded_by[params[0]] = params[1]
            return params[0]
        if "COUNT(DISTINCT" in query:
            return 1
        return None

    async def execute(self, query: str, *params: Any) -> str:
        if "INSERT INTO finding_supersessions" in query:
            self.edges.append((params[0], params[1], params[2], params[3]))
        elif "SET superseded_by = $2" in query:
            self.superseded_by[params[0]] = params[1]
            self.sig_stamps[params[0]] = params[2]
        elif "SET situation_signature = $2" in query:
            self.sig_stamps.setdefault(params[0], params[1])
        return "OK"


class _Pool:
    def __init__(self, conn: _PoolConn) -> None:
        self.conn = conn

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()
