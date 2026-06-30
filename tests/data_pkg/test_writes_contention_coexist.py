# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure-logic (no-DB) tests for the Holes-B Wave 4 write-path COEXISTENCE leg
(#101, decision #1), the one behavioral change of contested-claims.

``supersede_prior_facts`` gains an opt-in mode behind ``LEGBA_FACT_CONTENTION``
(default OFF). With the flag OFF it must run the single blind UPDATE byte-for-
byte as before — ZERO extra queries on the hot path. With the flag ON, a
SAME-TIER open prior whose value is FUZZY-DISTINCT from the incoming value must
COEXIST (NOT close) so the detect-only ``fact_contention_arbiter`` opens a group
next cadence; everything else (differing-value same-tier fuzzy-SAME, lower-tier,
higher-tier) closes / is-skipped exactly as today.

These run WITHOUT a live Postgres: a hand-rolled ``CoexistConn`` records the
SQL + params and returns scripted ``fetch`` rows, so we assert the control flow
(one UPDATE OFF; a FETCH + a scoped UPDATE ON) and the close set.

The fuzzy clusterer (``provenance.value_clustering``) is a sibling module built
by a parallel wave and is absent at this branch point, so we inject a faithful
stub into ``sys.modules`` mirroring its documented two-stage contract (canon-
fold "Russian" -> "Russia", then a tight close-distance merge) before importing
the helper — the production import is a lazy ``from .value_clustering import
cluster_values`` inside the ON branch only, so the OFF path never needs it.
"""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest


# ---------------------------------------------------------------------------
# Faithful stub of provenance.value_clustering (the parallel Wave-2 module).
# ---------------------------------------------------------------------------
#
# The real cluster_values is "canon-fold then tight Levenshtein": values whose
# canonical key is identical (or within a tight distance) land in ONE cluster,
# genuinely-different values open separate clusters. We only need the
# one-cluster-vs-many distinction the Wave-4 carve-out keys on. This stub folds
# a couple of demonyms ("russian" -> "russia") + lower-cases/strips, then groups
# by exact canonical equality — enough to exercise:
#   * "Russian" vs "Russia"  -> ONE cluster  (fuzzy-SAME  -> still closes)
#   * "France"  vs "Germany" -> TWO clusters (fuzzy-distinct -> coexists)
_CANON_FOLD = {
    "russian": "russia",
    "american": "united states",
    "usa": "united states",
}


def _stub_cluster_values(values: list[str]) -> list[Any]:
    @dataclass
    class _Cluster:
        key: str
        members: list[int] = field(default_factory=list)

    clusters: list[_Cluster] = []
    by_key: dict[str, _Cluster] = {}
    for idx, raw in enumerate(values):
        folded = (raw or "").strip().lower()
        ckey = _CANON_FOLD.get(folded, folded)
        existing = by_key.get(ckey)
        if existing is None:
            existing = _Cluster(key=ckey, members=[])
            by_key[ckey] = existing
            clusters.append(existing)
        existing.members.append(idx)
    return clusters


_stub_module = types.ModuleType("legba.data.provenance.value_clustering")
_stub_module.cluster_values = _stub_cluster_values  # type: ignore[attr-defined]
sys.modules.setdefault("legba.data.provenance.value_clustering", _stub_module)


from legba.data.provenance.writes import (  # noqa: E402  (after the stub inject)
    _fact_contention_enabled,
    supersede_prior_facts,
)


# ---------------------------------------------------------------------------
# Fake conn — records execute + serves scripted fetch rows.
# ---------------------------------------------------------------------------


class _Row(dict):
    """asyncpg.Record stand-in — supports row["col"] access."""


class CoexistConn:
    """Minimal asyncpg.Connection stand-in for the coexistence path.

    ``fetch_rows`` is the canned candidate-prior set served to the ON-path
    SELECT. ``execute`` records SQL+params and returns a scripted close-count
    string so ``supersede_prior_facts`` parses a real count back.
    """

    def __init__(
        self,
        fetch_rows: list[_Row] | None = None,
        *,
        update_reply: str = "UPDATE 0",
    ) -> None:
        self.executes: list[tuple[str, tuple[Any, ...]]] = []
        self.fetches: list[tuple[str, tuple[Any, ...]]] = []
        self._fetch_rows = list(fetch_rows or [])
        self._update_reply = update_reply

    async def execute(self, sql: str, *params: Any) -> str:
        self.executes.append((sql, params))
        return self._update_reply

    async def fetch(self, sql: str, *params: Any) -> list[_Row]:
        self.fetches.append((sql, params))
        return list(self._fetch_rows)

    # --- assertion helpers -------------------------------------------------

    @property
    def update_facts_execs(self) -> list[tuple[str, tuple[Any, ...]]]:
        return [
            (sql, p)
            for sql, p in self.executes
            if "UPDATE facts" in sql and "superseded_by" in sql
        ]


def _row(fid: UUID, value: str, source_type: str) -> _Row:
    return _Row(id=fid, value=value, source_type=source_type)


# ===========================================================================
# Flag plumbing
# ===========================================================================


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("LEGBA_FACT_CONTENTION", raising=False)
    assert _fact_contention_enabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", "  On "])
def test_flag_on_values(monkeypatch, raw):
    monkeypatch.setenv("LEGBA_FACT_CONTENTION", raw)
    assert _fact_contention_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "", "frobnicate"])
def test_flag_off_values(monkeypatch, raw):
    monkeypatch.setenv("LEGBA_FACT_CONTENTION", raw)
    assert _fact_contention_enabled() is False


# ===========================================================================
# Flag OFF — byte-for-byte the single blind UPDATE (no extra queries).
# ===========================================================================


def test_off_is_single_blind_update(monkeypatch):
    """With the flag OFF, supersede_prior_facts issues EXACTLY ONE execute (the
    historical UPDATE) and NEVER a SELECT — the hot path is unchanged."""
    monkeypatch.delenv("LEGBA_FACT_CONTENTION", raising=False)
    conn = CoexistConn(update_reply="UPDATE 1")
    closed = asyncio.run(
        supersede_prior_facts(
            conn,
            subject="Iran",
            predicate="leader of",
            value="New Leader",
            new_fact_id=uuid4(),
            incoming_source_type="agent",
        )
    )
    assert closed == 1
    assert len(conn.executes) == 1, "OFF path must issue exactly one query"
    assert conn.fetches == [], "OFF path must NOT fetch candidate priors"
    sql, params = conn.executes[0]
    # The historical SQL contract (the A1 tier ladder + value-differs predicate)
    # is intact, including the `$5::int` rank bind = 1 for an 'agent' incoming.
    assert "UPDATE facts" in sql and "superseded_by = $4" in sql
    assert "lower(value)    <> lower($3)" in sql
    assert "$5::int IS NULL" in sql
    assert "<= $5::int" in sql
    assert params[4] == 1, params  # 'agent' -> machine rank


def test_off_unchanged_when_flag_explicitly_disabled(monkeypatch):
    """An explicit LEGBA_FACT_CONTENTION=0 is the same single-UPDATE OFF path."""
    monkeypatch.setenv("LEGBA_FACT_CONTENTION", "0")
    conn = CoexistConn()
    asyncio.run(
        supersede_prior_facts(
            conn, subject="A", predicate="p", value="v",
            new_fact_id=uuid4(), incoming_source_type="seed",
        )
    )
    assert len(conn.executes) == 1
    assert conn.fetches == []


# ===========================================================================
# Flag ON — fetch candidates, decide per-row, close the right set.
# ===========================================================================


def test_on_same_tier_fuzzy_distinct_coexists(monkeypatch):
    """ON + same-tier + fuzzy-DISTINCT prior -> NOT closed (coexist). The two
    open rows survive so the detect-only arbiter groups them next cadence."""
    monkeypatch.setenv("LEGBA_FACT_CONTENTION", "1")
    prior = _row(uuid4(), "Germany", "agent")  # same tier (agent==agent)
    conn = CoexistConn(fetch_rows=[prior])
    closed = asyncio.run(
        supersede_prior_facts(
            conn,
            subject="X",
            predicate="allied with",
            value="France",            # fuzzy-DISTINCT from "Germany"
            new_fact_id=uuid4(),
            incoming_source_type="agent",
        )
    )
    assert closed == 0, "same-tier fuzzy-distinct prior must COEXIST"
    # It fetched candidates and then closed NOTHING (no UPDATE facts issued,
    # because the close set was empty).
    assert len(conn.fetches) == 1
    assert conn.update_facts_execs == [], "nothing to close -> no UPDATE"


def test_on_same_tier_fuzzy_same_still_closes(monkeypatch):
    """ON + same-tier + fuzzy-SAME ("Russian" vs "Russia") -> CLOSED, exactly as
    today. Fuzzy-SAME is a spelling variant, NOT contention."""
    monkeypatch.setenv("LEGBA_FACT_CONTENTION", "1")
    pid = uuid4()
    prior = _row(pid, "Russian", "agent")
    conn = CoexistConn(fetch_rows=[prior], update_reply="UPDATE 1")
    new_id = uuid4()
    closed = asyncio.run(
        supersede_prior_facts(
            conn,
            subject="X",
            predicate="nationality",
            value="Russia",            # fuzzy-SAME as "Russian" (one cluster)
            new_fact_id=new_id,
            incoming_source_type="agent",
        )
    )
    assert closed == 1, "same-tier fuzzy-SAME variant closes as today"
    assert len(conn.update_facts_execs) == 1
    sql, params = conn.update_facts_execs[0]
    # ON path closes via the id = ANY($ids) scoped UPDATE.
    assert "id = ANY($2::uuid[])" in sql
    assert params[0] == new_id          # superseded_by pointer
    assert params[1] == [pid]           # exactly the fuzzy-same prior


def test_on_lower_tier_prior_closes(monkeypatch):
    """ON + a LOWER-tier prior (the incoming outranks) -> CLOSED even when the
    value is fuzzy-distinct: the coexistence carve-out is SAME-tier only; a
    seed/curated incoming retires a lower agent/ingestion prior outright."""
    monkeypatch.setenv("LEGBA_FACT_CONTENTION", "1")
    pid = uuid4()
    prior = _row(pid, "Germany", "agent")   # rank 1, fuzzy-distinct from France
    conn = CoexistConn(fetch_rows=[prior], update_reply="UPDATE 1")
    new_id = uuid4()
    closed = asyncio.run(
        supersede_prior_facts(
            conn,
            subject="X",
            predicate="allied with",
            value="France",
            new_fact_id=new_id,
            incoming_source_type="seed",    # rank 2 outranks the agent prior
        )
    )
    assert closed == 1, "a lower-tier prior closes regardless of fuzziness"
    assert params_of(conn) == [pid]


def test_on_higher_tier_prior_not_closed_a1(monkeypatch):
    """ON + a HIGHER-tier prior (seed) vs a machine incoming (agent) -> NOT
    closed (A1 guard). The carve-out never even runs; A1 excludes it first."""
    monkeypatch.setenv("LEGBA_FACT_CONTENTION", "1")
    prior = _row(uuid4(), "Real Leader", "seed")   # rank 2
    conn = CoexistConn(fetch_rows=[prior])
    closed = asyncio.run(
        supersede_prior_facts(
            conn,
            subject="Iran",
            predicate="leader of",
            value="Wrong Leader",
            new_fact_id=uuid4(),
            incoming_source_type="agent",          # rank 1, must not retire seed
        )
    )
    assert closed == 0, "A1 — a machine fact must not close a seed prior"
    assert conn.update_facts_execs == [], "nothing closes -> no UPDATE"


def test_on_mixed_priors_closes_only_the_right_ones(monkeypatch):
    """ON with a MIX of priors, incoming = seed (rank 2), closes exactly: the
    same-tier (seed) fuzzy-SAME variant and the lower-tier (agent/ingestion)
    priors; spares the same-tier (seed) fuzzy-distinct one (coexist).

    seed/curated are EQUAL rank, and ingestion/agent are EQUAL rank and BOTH
    below seed — so against a seed incoming every ingestion/agent prior is
    strictly lower and closes outright (the carve-out is same-tier only)."""
    monkeypatch.setenv("LEGBA_FACT_CONTENTION", "1")
    seed_variant = _row(uuid4(), "Russian", "seed")    # same tier, fuzzy-SAME  -> close
    seed_coexist = _row(uuid4(), "Germany", "curated") # same tier (2), fuzzy-distinct -> spare
    lower_agent = _row(uuid4(), "Older", "agent")      # rank 1 < 2 -> close (outranked)
    lower_ing = _row(uuid4(), "Oldest", "ingestion")   # rank 1 < 2 -> close (outranked)
    conn = CoexistConn(
        fetch_rows=[seed_variant, seed_coexist, lower_agent, lower_ing],
        update_reply="UPDATE 3",
    )
    new_id = uuid4()
    closed = asyncio.run(
        supersede_prior_facts(
            conn,
            subject="X",
            predicate="p",
            value="Russia",                 # fuzzy-SAME as "Russian", distinct from Germany
            new_fact_id=new_id,
            incoming_source_type="seed",    # rank 2
        )
    )
    assert closed == 3
    assert len(conn.update_facts_execs) == 1
    _, params = conn.update_facts_execs[0]
    assert params[0] == new_id
    assert set(params[1]) == {
        seed_variant["id"], lower_agent["id"], lower_ing["id"]
    }
    # The same-tier fuzzy-distinct row is the ONLY one spared (coexist).
    assert seed_coexist["id"] not in params[1]


def test_on_none_source_type_closes_everything_unconditionally(monkeypatch):
    """ON + incoming_source_type=None (the operator-correction caller) closes
    EVERY differing-value prior unconditionally — no A1 guard, no coexistence
    carve-out — exactly as the OFF path's `$5::int IS NULL` short-circuit."""
    monkeypatch.setenv("LEGBA_FACT_CONTENTION", "1")
    seed_prior = _row(uuid4(), "Germany", "seed")      # higher tier, fuzzy-distinct
    agent_prior = _row(uuid4(), "Russian", "agent")
    conn = CoexistConn(
        fetch_rows=[seed_prior, agent_prior], update_reply="UPDATE 2"
    )
    new_id = uuid4()
    closed = asyncio.run(
        supersede_prior_facts(
            conn,
            subject="X",
            predicate="p",
            value="France",
            new_fact_id=new_id,
            incoming_source_type=None,      # operator correction — maximal authority
        )
    )
    assert closed == 2, "operator correction closes every differing-value prior"
    _, params = conn.update_facts_execs[0]
    assert set(params[1]) == {seed_prior["id"], agent_prior["id"]}


def test_on_no_candidates_returns_zero_no_update(monkeypatch):
    """ON with no open differing-value priors -> fetch returns nothing, no
    UPDATE issued, returns 0 (first assertion of the subject+predicate)."""
    monkeypatch.setenv("LEGBA_FACT_CONTENTION", "1")
    conn = CoexistConn(fetch_rows=[])
    closed = asyncio.run(
        supersede_prior_facts(
            conn, subject="X", predicate="p", value="v",
            new_fact_id=uuid4(), incoming_source_type="agent",
        )
    )
    assert closed == 0
    assert len(conn.fetches) == 1
    assert conn.update_facts_execs == []


# ---------------------------------------------------------------------------
# small helper used by a couple of single-prior assertions
# ---------------------------------------------------------------------------


def params_of(conn: CoexistConn) -> list[UUID]:
    """The id-array bound onto the ON-path close UPDATE (params[1])."""
    assert len(conn.update_facts_execs) == 1
    return list(conn.update_facts_execs[0][1][1])
