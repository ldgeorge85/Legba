# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Holes-B Wave 2 — the contested-claims arbiter (DETECT-ONLY, #101).

Pure-logic (no live Postgres) tests for the deterministic arbiter:

  * the junk gate excludes the live Poland -> {Berlin, Russian} `located in` case
    (Berlin is an inverted relation -> only one non-junk cluster -> NO dispute);
  * the Q·C·R·F factor functions + the abstain gate (winner only when
    score >= MIN_SURFACE_SCORE AND >= DOMINANCE_RATIO * runner-up);
  * the full pass over a fake pool: a genuine same-tier dispute opens a group,
    marks facts contested, surfaces (or abstains);
  * INVARIANT B15 — DETECT-ONLY: the arbiter NEVER writes facts.valid_until /
    superseded_by / value / confidence and never calls supersede_prior_facts,
    asserted by capturing every SQL the run issues against a RecordingConn;
  * idempotency: two passes over identical inputs issue the same facts-marker
    writes and surface the same winner.

The DB round-trip (a real same-tier dispute producing a fact_contention group
with 2 open rows + one surfaced winner, the abstain path, the Poland case
opening 0 genuine groups) lives in an integration test needing a migrated DB
(see the module-end note).
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.analysts.deterministic_handlers import fact_contention_arbiter as arb


NOW = datetime(2026, 6, 29, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the arbiter's clock to :data:`NOW`, the instant the fixtures date
    from — the tail file's fix applied here too. This file's full-pass test
    only asserts a cause-independent invariant, so it never FAILED as the rows
    aged past the recency floor — it just silently stopped exercising the
    near-tie routing it was written for (a MEANING bomb, not a failure bomb;
    see ARBITER_TAIL_FIX.md item 3)."""
    monkeypatch.setattr(arb, "_now", lambda: NOW)


# ---------------------------------------------------------------------------
# Fake pool / conn — captures every execute + scripts fetch/fetchval results.
# ---------------------------------------------------------------------------


class RecordingConn:
    def __init__(self, fetch_script: list[Any], fetchval_script: list[Any]) -> None:
        self.executes: list[tuple[str, tuple[Any, ...]]] = []
        self._fetch = list(fetch_script)
        self._fetchval = list(fetchval_script)

    async def fetch(self, sql: str, *params: Any) -> Any:
        if self._fetch:
            return self._fetch.pop(0)
        return []

    async def fetchval(self, sql: str, *params: Any) -> Any:
        if self._fetchval:
            return self._fetchval.pop(0)
        return None

    async def execute(self, sql: str, *params: Any) -> str:
        self.executes.append((sql, params))
        return "UPDATE 0"


class _Acquire:
    def __init__(self, conn: RecordingConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> RecordingConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class FakePool:
    def __init__(self, conn: RecordingConn) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


def _row(subject: str, predicate: str, value: str, *, cred: float | None = 0.5,
         conf: float = 0.6, age_days: float = 0.0, lineage: list[UUID] | None = None,
         source_type: str = "ingestion") -> dict[str, Any]:
    return {
        "id": uuid4(),
        "subject": subject,
        "predicate": predicate,
        "value": value,
        "confidence": conf,
        "source_type": source_type,
        "source_credibility": cred,
        "produced_at": NOW - timedelta(days=age_days),
        "derived_from": list(lineage or [uuid4()]),
    }


# ===========================================================================
# Junk gate — the live Poland -> {Berlin, Russian} `located in` case
# ===========================================================================


def test_poland_berlin_junk_gated_out():
    """Berlin trips the inverted-relation gate (a country located in a city);
    Russia/Russian is the lone non-junk value -> < 2 clusters -> NO dispute."""
    rows = [
        _row("Poland", "located in", "Berlin"),
        _row("Poland", "located in", "Russian"),
    ]
    non_junk, junk = arb._aggregate_group(rows)
    assert len(non_junk) == 1, "only one non-junk value remains"
    assert len(junk) == 1
    _, reason = junk[0]
    assert reason == "inverted_relation"


def test_junk_reason_labels():
    assert arb._junk_reason("Poland", "located in", "Berlin") == "inverted_relation"
    assert arb._junk_reason("US", "located in", "United States") == "reflexive_after_canon"
    # a genuine pair of disputed values is NOT junk
    assert arb._junk_reason("India", "border status", "de-escalating") is None


# ===========================================================================
# Q·C·R·F factor functions + abstain gate
# ===========================================================================


def test_quorum_log_damped_normalized():
    # the most-corroborated value scores 1.0; a zero-source value scores 0.
    assert arb._quorum(5, 5) == pytest.approx(1.0)
    assert arb._quorum(0, 5) == pytest.approx(0.0)
    # diminishing returns: 2nd source adds more than the 5th.
    g2 = arb._quorum(2, 5) - arb._quorum(1, 5)
    g5 = arb._quorum(5, 5) - arb._quorum(4, 5)
    assert g2 > g5


def test_credibility_is_group_share():
    assert arb._credibility_share(2.7, 3.7) == pytest.approx(2.7 / 3.7)
    assert arb._credibility_share(0.0, 0.0) == 0.0  # NULL/unknown-only group


def test_recency_halflife():
    assert arb._recency(NOW, NOW) == pytest.approx(1.0)
    assert arb._recency(NOW - timedelta(days=arb.HALFLIFE_DAYS), NOW) == pytest.approx(0.5)
    assert arb._recency(None, NOW) == 0.0


def test_score_is_multiplicative_zero_axis_kills():
    # a value with no credible source (C=0) cannot win on confidence alone.
    assert arb._arbiter_score(1.0, 0.0, 1.0, 1.0) == 0.0
    assert arb._arbiter_score(1.0, 1.0, 1.0, 1.0) == pytest.approx(1.0)


def _agg(value_key: str, *, distinct: int, cred: float, conf_mean: float,
         age_days: float = 0.0) -> arb._ValueAgg:
    a = arb._ValueAgg(value_key)
    a.representative_fact_id = uuid4()
    a.distinct_lineage = {f"src{i}" for i in range(distinct)}
    a.cred_sum = cred
    a.confidence_sum = conf_mean
    a.row_count = 1
    a.confidence_max = conf_mean
    a.latest_asserted_at = NOW - timedelta(days=age_days)
    a.supporting_fact_ids = [a.representative_fact_id]
    return a


def test_winner_surfaced_when_clears_floor_and_dominates():
    winner = _agg("x", distinct=5, cred=3.0, conf_mean=0.9)
    loser = _agg("y", distinct=1, cred=0.3, conf_mean=0.5)
    aggs = [winner, loser]
    scores = arb._score_group(aggs, NOW)
    sel = arb._select_winner(aggs, scores)
    assert sel is winner
    assert scores["x"] >= arb.MIN_SURFACE_SCORE
    assert scores["x"] >= arb.DOMINANCE_RATIO * scores["y"]


def test_abstains_on_near_tie():
    """Two equally-credible, equally-recent values -> the dominance gate fails
    -> abstain (no surfaced winner). An honest deadlock, not a coin-flip."""
    a = _agg("x", distinct=3, cred=1.5, conf_mean=0.8)
    b = _agg("y", distinct=3, cred=1.5, conf_mean=0.8)
    aggs = [a, b]
    scores = arb._score_group(aggs, NOW)
    assert arb._select_winner(aggs, scores) is None


def test_abstains_when_best_below_floor():
    """All values weak (old + thin credibility) -> best < MIN_SURFACE_SCORE."""
    a = _agg("x", distinct=1, cred=0.1, conf_mean=0.2, age_days=120)
    b = _agg("y", distinct=1, cred=0.1, conf_mean=0.2, age_days=200)
    aggs = [a, b]
    scores = arb._score_group(aggs, NOW)
    assert max(scores.values()) < arb.MIN_SURFACE_SCORE
    assert arb._select_winner(aggs, scores) is None


# ===========================================================================
# DETECT-ONLY invariant (B15) — over a full fake-pool pass
# ===========================================================================


def _detect_only_violations(executes: list[tuple[str, tuple[Any, ...]]]) -> list[str]:
    """Return any SQL that would mutate a fact's value / validity (forbidden)."""
    bad: list[str] = []
    for sql, _ in executes:
        if "supersede_prior_facts" in sql:
            bad.append("supersede_prior_facts call")
        if re.search(r"UPDATE\s+facts", sql):
            forbidden = ("valid_until", "superseded_by")
            # a facts UPDATE may set value/confidence only via these exact words;
            # the marker UPDATEs set contested/contention_id/surfaced_winner only.
            for col in forbidden:
                if re.search(rf"\b{col}\s*=", sql):
                    bad.append(f"UPDATE facts SET {col}")
            # `value =` / `confidence =` as an assignment (not `value_count`).
            if re.search(r"\bvalue\s*=", sql):
                bad.append("UPDATE facts SET value")
            if re.search(r"\bconfidence\s*=", sql):
                bad.append("UPDATE facts SET confidence")
    return bad


def _run_full_pass(rows: list[dict[str, Any]]) -> RecordingConn:
    """Drive one arbiter pass with a fake pool returning ``rows`` as the open set.

    fetch script: [open_triples rows, stale-groups (empty)].
    fetchval script: [group id for the upsert] (one genuine group here).
    """
    cid = uuid4()
    conn = RecordingConn(
        fetch_script=[rows, []],            # _open_triples, then stale-group scan
        fetchval_script=[cid],              # _upsert_group RETURNING id
    )
    pool = FakePool(conn)
    asyncio.run(arb._run_arbiter(pool))
    return conn


def test_full_pass_never_mutates_fact_value_or_validity():
    """A genuine same-tier dispute (India border status: two values) opens a
    group + marks facts contested, but issues ZERO writes that touch a fact's
    value / confidence / valid_until / superseded_by (invariant B15)."""
    rows = [
        _row("India", "border status", "de-escalating", cred=0.9, conf=0.9, lineage=[uuid4(), uuid4()]),
        _row("India", "border status", "de-escalating", cred=0.9, conf=0.8, lineage=[uuid4()]),
        _row("India", "border status", "clashes ongoing", cred=0.5, conf=0.6, lineage=[uuid4()]),
    ]
    conn = _run_full_pass(rows)
    violations = _detect_only_violations(conn.executes)
    assert violations == [], f"DETECT-ONLY violated: {violations}"
    # It DID stamp the contested marker (proof it ran the detection write path).
    marker_writes = [
        sql for sql, _ in conn.executes
        if re.search(r"UPDATE\s+facts", sql) and "contested" in sql
    ]
    assert marker_writes, "expected a facts.contested marker UPDATE"
    # It DID write the sidecar group + value rows.
    assert any("INSERT INTO fact_contention_values" in sql for sql, _ in conn.executes)


def test_full_pass_is_idempotent_in_facts_marker_writes():
    """Two passes over identical inputs issue the same SHAPE of facts-marker
    writes (same set of UPDATE-facts statements) — the recompute is stable."""
    rows = [
        _row("India", "border status", "de-escalating", cred=0.9, conf=0.9),
        _row("India", "border status", "clashes ongoing", cred=0.5, conf=0.6),
    ]
    c1 = _run_full_pass(rows)
    c2 = _run_full_pass(rows)

    def marker_shapes(conn: RecordingConn) -> set[str]:
        return {
            sql.strip()
            for sql, _ in conn.executes
            if re.search(r"UPDATE\s+facts", sql)
        }

    assert marker_shapes(c1) == marker_shapes(c2)
    # And neither pass mutated value/validity.
    assert _detect_only_violations(c1.executes) == []
    assert _detect_only_violations(c2.executes) == []


def test_aggregate_distinct_source_count_is_lineage_not_rows():
    """A single chatty source (one lineage id, many rows) counts ONCE; two
    rows with distinct lineage count as two distinct sources."""
    lineage = uuid4()
    chatty = [
        _row("India", "border status", "de-escalating", lineage=[lineage]),
        _row("India", "border status", "de-escalating", lineage=[lineage]),
        _row("India", "border status", "de-escalating", lineage=[lineage]),
        _row("India", "border status", "clashes ongoing", lineage=[uuid4()]),
    ]
    non_junk, _ = arb._aggregate_group(chatty)
    by_key = {a.value_key: a for a in non_junk}
    assert by_key["de-escalating"].distinct_source_count == 1   # 3 rows, ONE lineage
    assert by_key["clashes ongoing"].distinct_source_count == 1


def test_null_credibility_skipped_not_zero():
    """A NULL source_credibility is UNKNOWN — skipped from the sum, never
    counted as 0 (which would unfairly penalize the value)."""
    rows = [
        _row("India", "border status", "de-escalating", cred=None),
        _row("India", "border status", "de-escalating", cred=0.8),
    ]
    non_junk, _ = arb._aggregate_group(rows)
    agg = non_junk[0]
    assert agg.cred_sum == pytest.approx(0.8)  # only the scored row contributes


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
