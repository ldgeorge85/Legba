# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Holes-B Wave 2b — LLM tie-break on a NEAR-TIE abstain (#101, decision #2).

The deterministic ``Q·C·R·F`` arbiter (Wave 2) stays the default. An LLM may
break a tie ONLY on a NEAR-TIE abstain (two non-junk clusters that both clear
``MIN_SURFACE_SCORE`` but neither dominates the other by ``DOMINANCE_RATIO``) —
NEVER on a genuinely weak abstain (best < ``MIN_SURFACE_SCORE``) and never when
the deterministic scorer already has a clear winner.

These tests use a STUB LLM handler (no network) and assert the four contract
cases:

  * clear deterministic winner       -> deterministic, ZERO llm calls;
  * near-tie, flag OFF                -> abstain, ZERO llm calls;
  * near-tie, flag ON, stub picks one -> that value is SURFACED (one llm call);
  * near-tie, flag ON, stub raises/garbles -> ABSTAIN (degrade-not-break).

Plus: a WEAK abstain (flag ON) never calls the LLM; the per-pass cap bounds the
number of calls; DETECT-ONLY (B15) holds for the LLM-surfaced winner too.

Reuses the RecordingConn / FakePool / _row patterns from the sibling
test_fact_contention_arbiter.py (a real Postgres round-trip lives in the
integration suite).
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


# ---------------------------------------------------------------------------
# Fakes — fake pool/conn (mirrors the sibling test) + a STUB llm handler.
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


class _StubResponse:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = None


class StubLLM:
    """A no-network LLM handler exposing ``chat_complete``.

    ``mode``:
      * ``"pick:<value_key>"`` — returns a JSON winner object for that key;
      * ``"abstain"``          — returns a JSON ABSTAIN object;
      * ``"garbage"``          — returns unparsable prose;
      * ``"raise"``            — raises (simulates an upstream failure);
      * ``"hang"``             — sleeps past the tie-break timeout.
    Records every call so the tests can assert call COUNT (and that OFF/weak
    paths make ZERO calls)."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(self, messages: Any, **kwargs: Any) -> _StubResponse:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.mode == "raise":
            raise RuntimeError("stub upstream failure")
        if self.mode == "hang":
            await asyncio.sleep(arb.LLM_TIEBREAK_TIMEOUT_SECONDS + 5)
            return _StubResponse('{"winner": "x"}')
        if self.mode == "garbage":
            return _StubResponse("I think it is probably the first one, honestly.")
        if self.mode == "abstain":
            return _StubResponse('{"winner": "ABSTAIN"}')
        if self.mode.startswith("pick:"):
            key = self.mode.split(":", 1)[1]
            return _StubResponse(f'```json\n{{"winner": "{key}"}}\n```')
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


def _near_tie_rows() -> list[dict[str, Any]]:
    """A genuine NEAR-TIE: two equally-credible, equally-recent, well-corroborated
    values that both clear MIN_SURFACE_SCORE but neither dominates — the ONLY case
    the LLM tie-break may run."""
    return [
        _row("India", "border status", "de-escalating", cred=0.9, conf=0.8,
             lineage=[uuid4(), uuid4(), uuid4()]),
        _row("India", "border status", "clashes ongoing", cred=0.9, conf=0.8,
             lineage=[uuid4(), uuid4(), uuid4()]),
    ]


def _clear_winner_rows() -> list[dict[str, Any]]:
    """A CLEAR deterministic winner: one value richly corroborated + credible, the
    other thin + low-credibility -> the dominance gate passes -> no LLM needed."""
    return [
        _row("India", "border status", "de-escalating", cred=0.95, conf=0.9,
             lineage=[uuid4(), uuid4(), uuid4(), uuid4(), uuid4()]),
        _row("India", "border status", "clashes ongoing", cred=0.1, conf=0.4,
             age_days=90, lineage=[uuid4()]),
    ]


def _run(rows: list[dict[str, Any]], llm: Any | None) -> tuple[RecordingConn, dict[str, int]]:
    conn = RecordingConn(fetch_script=[rows, []], fetchval_script=[uuid4()])
    counts = asyncio.run(arb._run_arbiter(FakePool(conn), llm))
    return conn, counts


def _surfaced_winner_keys(conn: RecordingConn) -> list[str]:
    """The value_key(s) the run surfaced, read off the fact_contention_values
    INSERTs whose surfaced_winner positional arg is True."""
    keys: list[str] = []
    for sql, params in conn.executes:
        if "INSERT INTO fact_contention_values" in sql and True in params:
            # value_key is positional arg index 1 (contention_id, value_key, ...).
            keys.append(params[1])
    return keys


# ===========================================================================
# Sanity: the fixtures land in the abstain CAUSES the contract names.
# ===========================================================================


def test_fixtures_classify_as_expected_causes():
    near = arb._aggregate_group(_near_tie_rows())[0]
    near_scores = arb._score_group(near, NOW)
    assert arb._select_winner(near, near_scores) is None
    assert arb._abstain_cause(near, near_scores) == "near_tie"

    clear = arb._aggregate_group(_clear_winner_rows())[0]
    clear_scores = arb._score_group(clear, NOW)
    assert arb._select_winner(clear, clear_scores) is not None
    assert arb._abstain_cause(clear, clear_scores) is None


# ===========================================================================
# 1. clear deterministic winner -> deterministic, ZERO llm calls.
# ===========================================================================


def test_clear_winner_uses_deterministic_and_never_calls_llm(monkeypatch):
    monkeypatch.setenv(arb.LLM_TIEBREAK_ENV, "1")
    llm = StubLLM("pick:de-escalating")
    conn, counts = _run(_clear_winner_rows(), llm)
    assert llm.calls == [], "a clear deterministic winner must not call the LLM"
    assert counts["llm_tiebreaks"] == 0
    assert counts["abstained"] == 0  # the deterministic winner surfaced
    assert "de-escalating" in _surfaced_winner_keys(conn)


# ===========================================================================
# 2. near-tie, flag OFF -> abstain, ZERO llm calls (byte-for-byte unchanged).
# ===========================================================================


def test_near_tie_flag_off_abstains_with_zero_llm(monkeypatch):
    monkeypatch.delenv(arb.LLM_TIEBREAK_ENV, raising=False)
    # Even if a handler were somehow passed, the run gates on the flag at the
    # handle() boundary; here we exercise _run_arbiter with llm=None (what the
    # builder threads when the flag is off).
    conn, counts = _run(_near_tie_rows(), None)
    assert counts["abstained"] == 1
    assert counts["llm_tiebreaks"] == 0
    assert _surfaced_winner_keys(conn) == []


def test_handle_off_path_resolves_no_llm(monkeypatch):
    """The handle() boundary: flag OFF -> _resolve_tiebreak_llm returns None even
    when a handler sits on deps.extras."""
    monkeypatch.delenv(arb.LLM_TIEBREAK_ENV, raising=False)

    class _Deps:
        extras = {arb.LLM_DEPS_EXTRA_KEY: StubLLM("pick:de-escalating")}

    assert arb._resolve_tiebreak_llm(_Deps()) is None
    monkeypatch.setenv(arb.LLM_TIEBREAK_ENV, "1")
    assert arb._resolve_tiebreak_llm(_Deps()) is not None


# ===========================================================================
# 3. near-tie, flag ON, stub picks a value -> that value is SURFACED.
# ===========================================================================


def test_near_tie_flag_on_stub_picks_surfaces_winner(monkeypatch):
    monkeypatch.setenv(arb.LLM_TIEBREAK_ENV, "1")
    llm = StubLLM("pick:clashes ongoing")
    conn, counts = _run(_near_tie_rows(), llm)
    assert len(llm.calls) == 1, "the near-tie group triggers exactly one LLM call"
    assert counts["llm_tiebreaks"] == 1
    assert counts["abstained"] == 0  # the LLM resolved the tie
    assert "clashes ongoing" in _surfaced_winner_keys(conn)
    # The LLM call was BOUNDED (token cap + temperature 0).
    kw = llm.calls[0]["kwargs"]
    assert kw["max_tokens"] == arb.LLM_TIEBREAK_MAX_TOKENS
    assert kw["temperature"] == 0.0


def test_stub_abstain_reply_keeps_abstain(monkeypatch):
    monkeypatch.setenv(arb.LLM_TIEBREAK_ENV, "1")
    llm = StubLLM("abstain")
    conn, counts = _run(_near_tie_rows(), llm)
    assert len(llm.calls) == 1
    assert counts["llm_tiebreaks"] == 0
    # Observability: the LLM WAS consulted (one call) even though it abstained,
    # so ``llm_tiebreak_calls`` separates "consulted + abstained" from "never
    # consulted" — the ``llm_tiebreaks`` (successful-pick) counter alone hides it.
    assert counts["llm_tiebreak_calls"] == 1
    assert counts["abstained"] == 1
    assert _surfaced_winner_keys(conn) == []


# ===========================================================================
# 4. near-tie, flag ON, stub raises / garbles / hangs -> ABSTAIN.
# ===========================================================================


@pytest.mark.parametrize("mode", ["raise", "garbage", "hang"])
def test_stub_failure_degrades_to_abstain(monkeypatch, mode):
    monkeypatch.setenv(arb.LLM_TIEBREAK_ENV, "1")
    # Shrink the timeout so the "hang" case returns fast.
    monkeypatch.setattr(arb, "LLM_TIEBREAK_TIMEOUT_SECONDS", 0.05)
    llm = StubLLM(mode)
    conn, counts = _run(_near_tie_rows(), llm)
    assert len(llm.calls) == 1, "the call was attempted"
    assert counts["llm_tiebreaks"] == 0, "no resolution credited on failure"
    assert counts["abstained"] == 1, "the near-tie abstain stands (degrade-not-break)"
    assert _surfaced_winner_keys(conn) == []


def test_hallucinated_value_key_is_rejected(monkeypatch):
    """A winner the LLM names that is NOT a listed cluster value_key is rejected
    -> ABSTAIN (no hallucinated value can ever be surfaced)."""
    monkeypatch.setenv(arb.LLM_TIEBREAK_ENV, "1")
    llm = StubLLM("pick:something the model invented")
    conn, counts = _run(_near_tie_rows(), llm)
    assert len(llm.calls) == 1
    assert counts["llm_tiebreaks"] == 0
    assert counts["abstained"] == 1


# ===========================================================================
# 5. WEAK abstain (flag ON) never calls the LLM.
# ===========================================================================


def test_weak_abstain_never_calls_llm(monkeypatch):
    """Both values thin + old -> best < MIN_SURFACE_SCORE (cause 1). The LLM is
    NEVER consulted; the weak abstain stands."""
    monkeypatch.setenv(arb.LLM_TIEBREAK_ENV, "1")
    rows = [
        _row("India", "border status", "de-escalating", cred=0.05, conf=0.1, age_days=200),
        _row("India", "border status", "clashes ongoing", cred=0.05, conf=0.1, age_days=300),
    ]
    # Confirm the fixture is a WEAK abstain, not a near-tie.
    aggs = arb._aggregate_group(rows)[0]
    scores = arb._score_group(aggs, NOW)
    assert arb._abstain_cause(aggs, scores) == "weak"

    llm = StubLLM("pick:de-escalating")
    conn, counts = _run(rows, llm)
    assert llm.calls == [], "a WEAK abstain must never call the LLM"
    assert counts["llm_tiebreaks"] == 0
    assert counts["abstained"] == 1


# ===========================================================================
# 6. Per-pass cap bounds the number of LLM calls.
# ===========================================================================


def test_per_pass_cap_bounds_llm_calls(monkeypatch):
    monkeypatch.setenv(arb.LLM_TIEBREAK_ENV, "1")
    monkeypatch.setattr(arb, "MAX_LLM_TIEBREAKS", 2)
    # Three distinct near-tie groups; the cap should stop at 2 calls.
    rows: list[dict[str, Any]] = []
    for subj in ("India", "Brazil", "Egypt"):
        rows.append(_row(subj, "border status", "de-escalating", cred=0.9, conf=0.8,
                         lineage=[uuid4(), uuid4(), uuid4()]))
        rows.append(_row(subj, "border status", "clashes ongoing", cred=0.9, conf=0.8,
                         lineage=[uuid4(), uuid4(), uuid4()]))
    # The run upserts one group per subject; supply enough group ids.
    conn = RecordingConn(fetch_script=[rows, []], fetchval_script=[uuid4(), uuid4(), uuid4()])
    llm = StubLLM("pick:de-escalating")
    counts = asyncio.run(arb._run_arbiter(FakePool(conn), llm))
    assert len(llm.calls) == 2, "the per-pass cap bounds LLM calls to MAX_LLM_TIEBREAKS"
    assert counts["llm_tiebreaks"] == 2
    # The third near-tie group got no call -> it abstained.
    assert counts["abstained"] == 1


# ===========================================================================
# 7. DETECT-ONLY (B15) holds for the LLM-surfaced winner too.
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


def test_llm_surfaced_winner_is_detect_only(monkeypatch):
    monkeypatch.setenv(arb.LLM_TIEBREAK_ENV, "1")
    llm = StubLLM("pick:clashes ongoing")
    conn, counts = _run(_near_tie_rows(), llm)
    assert counts["llm_tiebreaks"] == 1
    violations = _detect_only_violations(conn.executes)
    assert violations == [], f"DETECT-ONLY violated by the LLM path: {violations}"
    # It DID stamp the contested marker via the same path.
    assert any(
        re.search(r"UPDATE\s+facts", sql) and "contested" in sql
        for sql, _ in conn.executes
    )


# ===========================================================================
# 8. Strict parser unit coverage.
# ===========================================================================


def test_parse_tiebreak_winner_strict():
    keys = {"de-escalating", "clashes ongoing"}
    assert arb._parse_tiebreak_winner('{"winner": "de-escalating"}', keys) == "de-escalating"
    assert arb._parse_tiebreak_winner('```json\n{"winner":"clashes ongoing"}\n```', keys) == "clashes ongoing"
    # ABSTAIN, unlisted, missing key, prose, empty -> None.
    assert arb._parse_tiebreak_winner('{"winner": "ABSTAIN"}', keys) is None
    assert arb._parse_tiebreak_winner('{"winner": "made up"}', keys) is None
    assert arb._parse_tiebreak_winner('{"x": 1}', keys) is None
    assert arb._parse_tiebreak_winner("no json here", keys) is None
    assert arb._parse_tiebreak_winner("", keys) is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
