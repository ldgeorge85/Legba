# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""H-2 (MASTER_PLAN 2026-07-10 F/H/S, audit W6) — scorecard↔composition
disagreements, extracted to a PURE module + surfaced on the journal instrument.

Two surfaces reconcile with the SAME code now:

  * the ``GET /eval/country_scorecard`` endpoint (``v3_api``), and
  * ``PostgresQdrantSubstrateQueryPort.get_assessments`` (the journal read),
    which surfaces a bounded, fail-safe ``disagreements`` block.

These are pure-shape / fake-pool assertions — no live substrate.
"""
from __future__ import annotations

import pytest

from legba.data.registry.scorecard_reconcile import (
    ScorecardDisagreement,
    composition_usages,
    scorecard_disagreements,
)
from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort


# ---------------------------------------------------------------------------
# The pure reducers (the extracted module's public API)
# ---------------------------------------------------------------------------


def test_composition_usages_citation_beats_lineage() -> None:
    usages = composition_usages(
        [{"ref_id": "F1", "ref_kind": "finding", "source": "leadership_transition"}],
        ["F1", "F2"],
        {"F2": "energy_security"},
    )
    # F1 is cited (stronger claim even though also in lineage); F2 lineage-only.
    assert usages["F1"] == ("leadership_transition", "cited")
    assert usages["F2"] == ("energy_security", "derived_from")


def test_composition_usages_skips_nonfinding_and_malformed() -> None:
    usages = composition_usages(
        [
            {"ref_id": "S1", "ref_kind": "signal", "source": "x"},   # not a finding
            {"ref_id": "", "ref_kind": "finding", "source": "y"},    # empty id
            "garbage",                                                # not a dict
        ],
        [],
        {},
    )
    assert usages == {}


def test_scorecard_disagreements_flags_excluded_dim_the_composition_uses() -> None:
    dims = {
        "leadership_transition": {"band": "insufficient-evidence", "reason": "low-faithfulness"},
        "energy_security": {"band": "moderate"},
    }
    usages = {
        "F1": ("leadership_transition", "cited"),
        "F9": ("energy_security", "derived_from"),  # dim NOT excluded → no row
    }
    out = scorecard_disagreements(dims, usages)
    assert len(out) == 1
    d = out[0]
    assert isinstance(d, ScorecardDisagreement)
    assert d.finding_id == "F1"
    assert d.dimension == "leadership_transition"
    assert d.scorecard_verdict == "excluded:low-faithfulness"
    assert d.composition_usage == "cited"
    assert "scorecard excluded the leadership_transition dimension" in d.note


def test_scorecard_disagreements_empty_when_reconciled() -> None:
    # No dimension banded insufficient-evidence → nothing to reconcile.
    assert scorecard_disagreements({"x": {"band": "moderate"}}, {"F1": ("x", "cited")}) == []


# ---------------------------------------------------------------------------
# get_assessments — the journal instrument's bounded, fail-safe disagreements
# ---------------------------------------------------------------------------


class _RoutingConn:
    """Fake conn routing the H-2 reconciliation queries by SQL substring."""

    def __init__(self, *, scorecards, compositions, lookups=None, raise_on=None) -> None:
        self._scorecards = scorecards
        self._compositions = compositions
        self._lookups = lookups or []
        self._raise_on = raise_on
        self.fetch_calls: list[str] = []

    async def fetch(self, sql: str, *params):
        self.fetch_calls.append(sql)
        if self._raise_on and self._raise_on in sql:
            raise RuntimeError("boom")
        if "kind = 'scorecard'" in sql:
            return list(self._scorecards)
        if "analyst_id = 'country_composition'" in sql:
            return list(self._compositions)
        if "id = ANY($1::uuid[])" in sql:
            return list(self._lookups)
        return []


class _Acquire:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc) -> bool:
        return False


class _Pool:
    def __init__(self, conn) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


def _port(conn) -> PostgresQdrantSubstrateQueryPort:
    return PostgresQdrantSubstrateQueryPort(pg_pool=_Pool(conn), qdrant_client=None)


@pytest.mark.asyncio
async def test_reconcile_surfaces_disagreement_and_extends_refs() -> None:
    scorecards = [
        {
            "target_id": "us",
            "data": {
                "data": {
                    "bands": {
                        "dimensions": {
                            "leadership_transition": {
                                "band": "insufficient-evidence",
                                "reason": "low-faithfulness",
                            }
                        }
                    }
                }
            },
        }
    ]
    compositions = [
        {
            "target_id": "us",
            "derived_from": [],
            "data": {
                "data": {
                    "citations": [
                        {
                            "ref_id": "F1",
                            "ref_kind": "finding",
                            "source": "leadership_transition",
                        }
                    ]
                }
            },
        }
    ]
    conn = _RoutingConn(scorecards=scorecards, compositions=compositions)
    refs: list[str] = ["A0"]
    rows = [{"target_id": "us"}, {"target_id": "world"}, {"target_id": None}]
    out = await _port(conn)._reconcile_scorecard_disagreements(rows, refs)
    assert len(out) == 1
    assert out[0]["finding_id"] == "F1"
    assert out[0]["dimension"] == "leadership_transition"
    assert out[0]["target_id"] == "us"          # target stamped onto the row
    # The contested finding id is now citable by the journal (deduped onto refs).
    assert refs == ["A0", "F1"]
    # "world" and NULL targets are excluded from the country reconciliation.
    assert all("world" not in c for c in conn.fetch_calls if "= ANY($1::text[])" in c)


@pytest.mark.asyncio
async def test_reconcile_no_country_targets_is_noop_no_query() -> None:
    conn = _RoutingConn(scorecards=[], compositions=[])
    out = await _port(conn)._reconcile_scorecard_disagreements(
        [{"target_id": "world"}, {"target_id": None}], []
    )
    assert out == []
    assert conn.fetch_calls == []               # early-return: never touches the pool


@pytest.mark.asyncio
async def test_reconcile_malformed_scorecard_row_skips_that_target_not_whole_block() -> None:
    # us: a MALFORMED scorecard (data is a list, not a dict) — must skip us, not
    # sink the block. gb: a well-formed insufficient-evidence card the composition
    # cites — must still surface. (Review fail-safe-consistency finding.)
    scorecards = [
        {"target_id": "us", "data": ["not", "a", "dict"]},
        {
            "target_id": "gb",
            "data": {
                "data": {
                    "bands": {
                        "dimensions": {
                            "energy_security": {
                                "band": "insufficient-evidence",
                                "reason": "below-floor",
                            }
                        }
                    }
                }
            },
        },
    ]
    compositions = [
        {
            "target_id": "gb",
            "derived_from": [],
            "data": {
                "data": {
                    "citations": [
                        {"ref_id": "F7", "ref_kind": "finding", "source": "energy_security"}
                    ]
                }
            },
        }
    ]
    conn = _RoutingConn(scorecards=scorecards, compositions=compositions)
    out = await _port(conn)._reconcile_scorecard_disagreements(
        [{"target_id": "us"}, {"target_id": "gb"}], []
    )
    # The malformed us row degraded silently; gb's real divergence still surfaced.
    assert [d["target_id"] for d in out] == ["gb"]
    assert out[0]["finding_id"] == "F7"


@pytest.mark.asyncio
async def test_reconcile_is_fail_safe_on_db_error() -> None:
    conn = _RoutingConn(scorecards=[], compositions=[], raise_on="kind = 'scorecard'")
    refs: list[str] = ["A0"]
    out = await _port(conn)._reconcile_scorecard_disagreements([{"target_id": "us"}], refs)
    assert out == []                            # degrades to honest empty, never raises
    assert refs == ["A0"]                       # refs untouched on failure
