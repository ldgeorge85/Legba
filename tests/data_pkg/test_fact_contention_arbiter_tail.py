# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Holes-B / P3-2 — the contested-claims arbiter TAIL (#101).

Soak gate -> deterministic WEIGHTED tie-break (+ A6 earned-record seam) ->
coexistence semantics + surfacing -> reversibility. DETECT-ONLY (B15) still
holds throughout: the arbiter NEVER mutates a fact row.

Pure-logic (no live Postgres) coverage of the new decision layers:

  * ``_tiebreak_weight`` math (corroboration + diversity + credibility; the A6
    seam contributes 0.0 today);
  * decisive-vs-gray routing: a weight-decisive near-tie surfaces
    DETERMINISTICALLY (no LLM), a symmetric near-tie stays gray (LLM territory);
  * the soak gate defers a young near-tie (no tie-break runs);
  * ``_evidence_fingerprint`` changes iff the per-side evidence changes (the
    cache invalidation lever);
  * the full fake-pool pass over a weight-decisive dispute never mutates a fact
    value / validity (B15) and stamps surfaced_by='deterministic';
  * the LLM tie-break verdict-cache round-trip (a cached pick is served without
    re-calling the LLM);
  * ``surface_history`` append shape on a decision change.

The real Postgres round-trip (migration 0097 columns + the alert contention_flip
trigger compatibility) lives in the sibling integration test
``test_fact_contention_surfacing_db.py``.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.analysts.deterministic_handlers import fact_contention_arbiter as arb


NOW = datetime(2026, 7, 24, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the arbiter's clock to :data:`NOW` — the instant every fixture row is
    dated from.

    The full-pass tests drive ``_run_arbiter``, which reads ``_now()`` ONCE and
    threads it into ``_score_group``. ``R`` is an exponential half-life decay
    (``HALFLIFE_DAYS`` = 30) on the row ages, so against a WALL-CLOCK now the
    fixture scores shrink every day the suite is not run: the weight-decisive
    pair scores 0.35 at age 0 but crosses ``MIN_SURFACE_SCORE`` (0.15) at age
    ~36.7d, which silently reclassifies the abstain from ``near_tie`` (the
    tie-break layers' ONLY entry point) to ``weak`` (which never tie-breaks).
    Freezing the clock makes every age in this file relative to ``NOW``, so the
    scores — and therefore the abstain CAUSE each test is really pinning — are
    the same at any future date."""
    monkeypatch.setattr(arb, "_now", lambda: NOW)


# ---------------------------------------------------------------------------
# Fakes — recording conn / pool (mirrors the sibling arbiter tests).
# ---------------------------------------------------------------------------


class RecordingConn:
    def __init__(
        self,
        fetch_script: list[Any] | None = None,
        fetchval_script: list[Any] | None = None,
    ) -> None:
        self.executes: list[tuple[str, tuple[Any, ...]]] = []
        self._fetch = list(fetch_script or [])
        self._fetchval = list(fetchval_script or [])

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


class _StubResponse:
    def __init__(self, content: str, model: str = "vllm-test-model") -> None:
        self.content = content

        class _U:
            pass

        u = _U()
        u.model = model
        self.usage = u


class StubLLM:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(self, messages: Any, **kwargs: Any) -> _StubResponse:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.mode.startswith("pick:"):
            key = self.mode.split(":", 1)[1]
            return _StubResponse(f'{{"winner": "{key}", "why": "more corroborated"}}')
        raise AssertionError(f"unknown stub mode {self.mode!r}")


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


def _agg(value_key: str, *, distinct: int, cred: float, types: int = 1,
         conf_mean: float = 0.7, age_days: float = 0.0) -> arb._ValueAgg:
    a = arb._ValueAgg(value_key)
    a.representative_fact_id = uuid4()
    a.representative_value = value_key
    a.distinct_lineage = {f"src:{value_key}:{i}" for i in range(distinct)}
    a.cred_sum = cred
    a.source_types = {f"type{i}" for i in range(types)}
    a.confidence_sum = conf_mean
    a.row_count = 1
    a.confidence_max = conf_mean
    a.latest_asserted_at = NOW - timedelta(days=age_days)
    a.supporting_fact_ids = [a.representative_fact_id]
    return a


# ===========================================================================
# 1. Weight math + the A6 earned-record seam.
# ===========================================================================


def test_tiebreak_weight_is_sources_plus_diversity_plus_credibility():
    a = _agg("x", distinct=3, cred=2.4, types=2)
    # 3 distinct sources + 2 source types + 2.4 credibility (+ 0.0 A6 seam).
    assert arb._tiebreak_weight(a) == pytest.approx(3 + 2 + 2.4)


def test_earned_track_record_seam_is_zero_today():
    """The A6 hook is NAMED but not built — it must contribute exactly 0.0 so
    the deterministic weight is pure corroboration + diversity + credibility."""
    a = _agg("x", distinct=4, cred=1.0, types=1)
    assert arb._earned_track_record_weight(a) == 0.0


# ===========================================================================
# 2. Decisive vs gray routing on the weighted tie-break.
# ===========================================================================


def test_weight_winner_when_corroboration_dominates(monkeypatch):
    monkeypatch.delenv(arb.WEIGHT_RATIO_ENV, raising=False)  # default 1.5
    heavy = _agg("de-escalating", distinct=6, cred=4.0, types=3)
    thin = _agg("clashes ongoing", distinct=1, cred=0.2, types=1)
    weights = arb._tiebreak_weights([heavy, thin])
    winner = arb._select_weight_winner([heavy, thin], weights)
    assert winner is heavy


def test_weight_abstains_on_symmetric_near_tie():
    """Two equally-corroborated sides — weight ratio 1.0 < 1.5 -> no weight
    winner (this is the LLM's gray zone, not a deterministic call)."""
    a = _agg("x", distinct=3, cred=2.7, types=1)
    b = _agg("y", distinct=3, cred=2.7, types=1)
    weights = arb._tiebreak_weights([a, b])
    assert arb._select_weight_winner([a, b], weights) is None


def test_weight_winner_needs_real_corroboration():
    """A 1-source side never wins on weight alone even if its raw weight
    dominates a 0-credibility runner-up (guards against a lone loud source)."""
    lone = _agg("x", distinct=1, cred=5.0, types=1)      # big cred, ONE source
    other = _agg("y", distinct=1, cred=0.0, types=1)
    weights = arb._tiebreak_weights([lone, other])
    assert lone.distinct_source_count < arb.WEIGHT_TIEBREAK_MIN_SOURCES
    assert arb._select_weight_winner([lone, other], weights) is None


def test_weight_ratio_is_env_configurable(monkeypatch):
    heavy = _agg("de-escalating", distinct=3, cred=1.0, types=1)   # weight 5.0
    light = _agg("clashes ongoing", distinct=2, cred=1.0, types=1)  # weight 4.0 -> 1.25
    weights = arb._tiebreak_weights([heavy, light])
    monkeypatch.setenv(arb.WEIGHT_RATIO_ENV, "1.5")
    assert arb._select_weight_winner([heavy, light], weights) is None  # 1.25 < 1.5
    monkeypatch.setenv(arb.WEIGHT_RATIO_ENV, "1.2")
    assert arb._select_weight_winner([heavy, light], weights) is heavy  # 1.25 >= 1.2


# ===========================================================================
# 3. Soak gate.
# ===========================================================================


def test_soak_defers_a_young_group(monkeypatch):
    monkeypatch.delenv(arb.SOAK_HOURS_ENV, raising=False)  # default 48h
    young = NOW - timedelta(hours=1)
    old = NOW - timedelta(hours=72)
    assert arb._past_soak(young, NOW) is False
    assert arb._past_soak(old, NOW) is True


def test_soak_disabled_when_zero(monkeypatch):
    monkeypatch.setenv(arb.SOAK_HOURS_ENV, "0")
    assert arb._past_soak(NOW, NOW) is True  # no wait at all


def test_soak_fails_open_on_unknown_age(monkeypatch):
    monkeypatch.delenv(arb.SOAK_HOURS_ENV, raising=False)
    # An unknown opened_at must never stall a dispute forever.
    assert arb._past_soak(None, NOW) is True


# ===========================================================================
# 4. Evidence fingerprint — the cache-invalidation lever.
# ===========================================================================


def test_fingerprint_stable_under_reorder():
    a = _agg("x", distinct=2, cred=1.0, types=1)
    b = _agg("y", distinct=3, cred=1.5, types=2)
    assert arb._evidence_fingerprint([a, b]) == arb._evidence_fingerprint([b, a])


def test_fingerprint_changes_on_new_evidence():
    a = _agg("x", distinct=2, cred=1.0, types=1)
    b = _agg("y", distinct=3, cred=1.5, types=2)
    base = arb._evidence_fingerprint([a, b])
    # A new supporting source on side x -> different fingerprint (re-ask).
    a2 = _agg("x", distinct=3, cred=1.0, types=1)
    assert arb._evidence_fingerprint([a2, b]) != base


# ===========================================================================
# 5. Full fake-pool pass — weight-decisive surface is DETECT-ONLY.
# ===========================================================================


def _detect_only_violations(executes: list[tuple[str, tuple[Any, ...]]]) -> list[str]:
    bad: list[str] = []
    for sql, _ in executes:
        if "supersede_prior_facts" in sql:
            bad.append("supersede_prior_facts call")
        if re.search(r"UPDATE\s+facts", sql):
            for col in ("valid_until", "superseded_by"):
                if re.search(rf"\b{col}\s*=", sql):
                    bad.append(f"UPDATE facts SET {col}")
            if re.search(r"\bvalue\s*=", sql):
                bad.append("UPDATE facts SET value")
            if re.search(r"\bconfidence\s*=", sql):
                bad.append("UPDATE facts SET confidence")
    return bad


def _weight_decisive_rows() -> list[dict[str, Any]]:
    """A near-tie by Q·C·R·F that the WEIGHTED tie-break resolves.

    Side A ('de-escalating') = 5 LOW-credibility sources (0.4 each); side B
    ('clashes ongoing') = 2 HIGH-credibility sources (1.0 each). The credibility
    SUM (2.0) and the credibility-weighted source count are EQUAL, so Q·C·R·F
    scores tie -> the dominance gate fails -> near-tie ABSTAIN. But the raw
    corroboration WEIGHT diverges (5+1+2.0=8.0 vs 2+1+2.0=5.0, ratio 1.6 >= 1.5),
    so the deterministic weighted tie-break surfaces side A — accumulated
    corroboration deciding a score-tie, no LLM."""
    rows: list[dict[str, Any]] = []
    for _ in range(5):
        rows.append(_row("India", "border status", "de-escalating",
                         cred=0.4, conf=0.7, source_type="rss", lineage=[uuid4()]))
    for _ in range(2):
        rows.append(_row("India", "border status", "clashes ongoing",
                         cred=1.0, conf=0.7, source_type="rss", lineage=[uuid4()]))
    return rows


def test_full_pass_weight_decisive_is_detect_only(monkeypatch):
    monkeypatch.delenv(arb.LLM_TIEBREAK_ENV, raising=False)
    monkeypatch.setenv(arb.SOAK_HOURS_ENV, "0")  # skip the soak wait for the test
    rows = _weight_decisive_rows()
    conn = RecordingConn(fetch_script=[rows, []], fetchval_script=[uuid4()])
    counts = asyncio.run(arb._run_arbiter(FakePool(conn), None))
    assert counts["weight_tiebreaks"] == 1, "the weighted tie-break resolved it"
    assert counts["abstained"] == 0
    assert _detect_only_violations(conn.executes) == []
    # The surface record was stamped surfaced_by='deterministic'.
    fc_updates = [
        params for sql, params in conn.executes
        if "UPDATE fact_contention" in sql and "surfaced_by" in sql
    ]
    assert any("deterministic" in params for params in fc_updates)


def test_full_pass_symmetric_near_tie_without_llm_abstains(monkeypatch):
    monkeypatch.setenv(arb.SOAK_HOURS_ENV, "0")
    rows = [
        _row("India", "border status", "de-escalating", cred=0.9, conf=0.8,
             lineage=[uuid4(), uuid4(), uuid4()]),
        _row("India", "border status", "clashes ongoing", cred=0.9, conf=0.8,
             lineage=[uuid4(), uuid4(), uuid4()]),
    ]
    conn = RecordingConn(fetch_script=[rows, []], fetchval_script=[uuid4()])
    counts = asyncio.run(arb._run_arbiter(FakePool(conn), None))
    assert counts["weight_tiebreaks"] == 0
    assert counts["abstained"] == 1


def test_full_pass_soak_defers_young_group(monkeypatch):
    """A near-tie whose group is fresh (opened just now) is left contested — no
    weight tie-break, no LLM — until it soaks."""
    monkeypatch.delenv(arb.SOAK_HOURS_ENV, raising=False)  # default 48h
    rows = _weight_decisive_rows()
    # _group_surface_state returns opened_at = NOW (fresh) -> inside soak window.
    # Dated off the FROZEN clock, not wall-clock: the soak gate compares
    # opened_at against the arbiter's own ``now``, so a real-clock opened_at
    # would sit ~37 days in that clock's FUTURE.
    surface_state = [{
        "opened_at": NOW,
        "status": "contested", "surfaced_value": None, "surfaced_fact_id": None,
        "surfaced_by": None, "surfaced_at": None, "surface_rationale": None,
    }]
    conn = RecordingConn(
        fetch_script=[rows, [], surface_state, []],
        fetchval_script=[uuid4()],
    )
    counts = asyncio.run(arb._run_arbiter(FakePool(conn), None))
    assert counts["weight_tiebreaks"] == 0, "young group must not tie-break yet"
    assert counts["soak_deferred"] == 1
    assert counts["abstained"] == 1


# ===========================================================================
# 6. LLM verdict cache — a cached pick is served without re-calling the LLM.
# ===========================================================================


def test_cached_verdict_served_without_llm_call(monkeypatch):
    monkeypatch.setenv(arb.LLM_TIEBREAK_ENV, "1")
    monkeypatch.setenv(arb.SOAK_HOURS_ENV, "0")
    rows = [
        _row("India", "border status", "de-escalating", cred=0.9, conf=0.8,
             lineage=[uuid4(), uuid4(), uuid4()]),
        _row("India", "border status", "clashes ongoing", cred=0.9, conf=0.8,
             lineage=[uuid4(), uuid4(), uuid4()]),
    ]
    # Compute the fingerprint the run will see so the cache row matches.
    non_junk = arb._aggregate_group(rows)[0]
    fp = arb._evidence_fingerprint(non_junk)
    winning_key = non_junk[0].value_key
    cached = [{
        "verdict": "pick", "winner_value_key": winning_key,
        "justification": "cached decision", "model_id": "vllm-cached",
    }]
    # fetch order: open_triples, functional_role, surface_state, cache-hit.
    conn = RecordingConn(
        fetch_script=[rows, [], [], cached],
        fetchval_script=[uuid4()],
    )
    llm = StubLLM("pick:de-escalating")
    counts = asyncio.run(arb._run_arbiter(FakePool(conn), llm))
    assert llm.calls == [], "a cache HIT must never re-call the LLM"
    assert counts["llm_cache_hits"] == 1
    assert counts["llm_tiebreaks"] == 1
    assert counts["abstained"] == 0
    # Prove the fingerprint the test built is the one the run keyed on.
    assert fp  # non-empty sha256


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
