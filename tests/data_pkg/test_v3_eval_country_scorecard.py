# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the P4-T3 banded country-scorecard read route on the v3 API.

Covers the registry-side inline-SQL route added to
:mod:`legba.data.registry.v3_api`:

  * ``GET /api/v1/v3/eval/country_scorecard`` -> ``list[CountryScorecard]``

Mirrors ``test_v3_eval_calibration``: ``build_v3_router`` only touches ``deps``
lazily inside the async handler, so the router registers against a trivial stub
and the path is introspected without a live substrate. The load-bearing contract
(the route PROJECTS persisted ``data.bands`` — no re-banding, no
scorecard_banding import; an empty result is a first-class honest state, NOT a
404; a distinct path from the critic-rollup ``/eval/scorecard``) is asserted on
the model + the path set.

B0-5 (audit W6) — the scorecard↔composition DISAGREEMENT surface: the pure
reducers (``_composition_usages`` / ``_scorecard_disagreements``) are exercised
with real inputs (the ``test_v3_escalations`` precedent), and the handler is
driven end-to-end through a stubbed pg pool to prove the wiring over the
LIVE-VERIFIED JSONB shapes (``data.data.citations[*].ref_id``/``source`` + the
``derived_from`` column) AND that a reconciliation-query failure degrades to
``disagreements: []`` at HTTP 200, never a 500.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from legba.data.registry.v3_api import (
    CountryScorecard,
    ScorecardDisagreement,
    _composition_usages,
    _scorecard_disagreements,
    build_v3_router,
)


def test_country_scorecard_route_registered_on_distinct_path() -> None:
    """The banded route registers under a DISTINCT path from the critic rollup —
    /eval/country_scorecard must not shadow the existing /eval/scorecard."""
    router = build_v3_router(deps=object())  # type: ignore[arg-type]
    paths = {r.path for r in router.routes}  # type: ignore[attr-defined]
    assert "/eval/country_scorecard" in paths
    # The pre-existing cross-analyst critic rollup is untouched + still distinct.
    assert "/eval/scorecard" in paths
    assert "/eval/country_scorecard" != "/eval/scorecard"


def test_country_scorecard_registry_slim_no_handler_import() -> None:
    """The route stays registry-slim: v3_api must NOT import the runtime banding
    handler (scorecard_banding / deterministic) — it only projects data.bands."""
    import legba.data.registry.v3_api as v3

    src = v3.__file__
    with open(src, "r", encoding="utf-8") as fh:
        text = fh.read()
    # Only IMPORT lines are load-bearing for image slimness — the docstring may
    # mention the handler by name. A runtime-handler import would fatten the
    # registry image (the slim-image rule).
    import_lines = "\n".join(
        ln for ln in text.splitlines()
        if ln.strip().startswith(("import ", "from "))
    )
    assert "scorecard_banding" not in import_lines
    assert "deterministic" not in import_lines


def test_country_scorecard_model_projects_bands_shape() -> None:
    """The response model carries the projected banded verdict — floors +
    per-dimension bands (with the basis ids the UI drills) + the composition."""
    basis = str(uuid4())
    card = CountryScorecard(
        target_id="country_g20_us",
        id=str(uuid4()),
        produced_at="2026-06-30T00:00:00+00:00",
        generated_at="2026-06-30T00:00:00+00:00",
        floors={"conf_floor": 0.35, "conf_confident": 0.60, "faith_floor": 0.50},
        dimensions={
            "escalation": {
                "band": "high", "basis": [basis], "reason": "qualified",
                "eval": {"faithfulness": 0.88, "correctness_vs_reference": 0.71,
                         "n_labeled": 12, "faithfulness_flagged": False},
            },
            "energy_security": {
                "band": "insufficient-evidence", "basis": [], "reason": "no-finding",
            },
        },
        composition={"present": True, "basis": [basis]},
    )
    assert card.target_id == "country_g20_us"
    assert card.floors["faith_floor"] == 0.50
    # A banded dimension NAMES its basis id (the drill target).
    assert card.dimensions["escalation"]["basis"] == [basis]
    # An insufficient dimension carries an empty-but-explicit basis (never fake).
    assert card.dimensions["energy_security"]["basis"] == []
    assert card.composition["present"] is True


def test_persisted_payload_nests_bands_under_data_data() -> None:
    """REGRESSION: the row's ``data`` column holds the WHOLE ScorecardPayload
    dump, so the product bands live one level deeper — ``data['data']['bands']``,
    NOT ``data['bands']``. The read route MUST project through that extra level
    (a route reading ``data.get('bands')`` returns empty dimensions live even
    though the persisted row is correct — the bug this test locks out)."""
    from legba.data.provenance import ScorecardPayload

    bands = {
        "target_id": "country_g20_us",
        "floors": {"conf_floor": 0.35, "faith_floor": 0.50},
        "dimensions": {"escalation": {"band": "high", "basis": [str(uuid4())]}},
        "composition": {"present": False, "basis": []},
    }
    payload = ScorecardPayload(
        title="t", body="b", confidence=1.0, evidence=[],
        tags=["scorecard"], data={"sub_handler": "scorecard_producer", "bands": bands},
    )
    # This dict IS what write_analyst_output persists into analyst_outputs.data.
    row_data = payload.model_dump()
    # The route's projection: the load-bearing nesting the live route must walk.
    assert "bands" not in row_data, "bands must NOT be at the top level"
    projected = (row_data.get("data") or {}).get("bands") or {}
    assert projected.get("dimensions", {}).get("escalation", {}).get("band") == "high"
    assert projected.get("floors", {}).get("faith_floor") == 0.50


def test_country_scorecard_empty_defaults_are_honest() -> None:
    """The empty-state defaults are first-class honest states (no fabricated
    band / composition / disagreement), matching the empty-list route return."""
    card = CountryScorecard(
        target_id="country_g20_in",
        id=str(uuid4()),
        produced_at="2026-06-30T00:00:00+00:00",
    )
    assert card.generated_at is None
    assert card.floors == {}
    assert card.dimensions == {}
    assert card.composition == {}
    # B0-5: the reconciled state is an EMPTY list by default — never fabricated.
    assert card.disagreements == []


# ---------------------------------------------------------------------------
# B0-5 — the scorecard↔composition disagreement surface (audit W6).
# ---------------------------------------------------------------------------


def _dims(
    *,
    excluded_reason: str = "low-faithfulness",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A banded-dimensions fixture: one excluded dim + one qualified dim."""
    dims: dict[str, Any] = {
        "leadership_transition": {
            "band": "insufficient-evidence",
            "basis": [],
            "reason": excluded_reason,
        },
        "escalation": {
            "band": "high",
            "basis": [str(uuid4())],
            "reason": "qualified",
        },
    }
    if extra:
        dims.update(extra)
    return dims


def test_disagreement_when_composition_cites_an_excluded_finding() -> None:
    """The W6 US case: the scorecard excluded the leadership_transition claim
    (R1b low-faithfulness, empty basis) while the composition head CITES that
    exact finding — exactly one disagreement row, fully attributed."""
    excluded_id = str(uuid4())
    citations = [
        {"ref_id": excluded_id, "ref_kind": "finding",
         "source": "leadership_transition", "marker": "[[ref:7]]", "ordinal": 7},
    ]
    usages = _composition_usages(citations, [excluded_id], {})
    rows = _scorecard_disagreements(_dims(), usages)
    assert len(rows) == 1
    d = rows[0]
    assert isinstance(d, ScorecardDisagreement)
    assert d.finding_id == excluded_id
    assert d.dimension == "leadership_transition"
    assert d.scorecard_verdict == "excluded:low-faithfulness"
    # Cited in the prose is the stronger usage claim than bare lineage.
    assert d.composition_usage == "cited"
    assert excluded_id in d.note and "low-faithfulness" in d.note


def test_disjoint_sets_yield_no_disagreement() -> None:
    """The normal reconciled state: the composition only uses findings from
    QUALIFIED dimensions (or non-dimension analysts) → empty list, never padded."""
    ok_id = str(uuid4())
    citations = [
        # A qualified dimension's finding — banded, not excluded.
        {"ref_id": ok_id, "ref_kind": "finding", "source": "escalation"},
        # A non-dimension analyst (live shape: proliferation_watch is cited by
        # the composition but is NOT a scorecard dimension) — never a match.
        {"ref_id": str(uuid4()), "ref_kind": "finding",
         "source": "proliferation_watch"},
    ]
    usages = _composition_usages(citations, [ok_id], {})
    assert _scorecard_disagreements(_dims(), usages) == []


def test_lineage_only_usage_reads_derived_from_via_lookup() -> None:
    """A derived_from-only id (no covering citation) is attributed through the
    id→analyst_id lookup and reads composition_usage='derived_from'."""
    lineage_id = str(uuid4())
    usages = _composition_usages(
        [], [lineage_id], {lineage_id: "leadership_transition"},
    )
    assert usages == {lineage_id: ("leadership_transition", "derived_from")}
    rows = _scorecard_disagreements(_dims(excluded_reason="no-finding"), usages)
    assert len(rows) == 1
    assert rows[0].composition_usage == "derived_from"
    assert rows[0].scorecard_verdict == "excluded:no-finding"


def test_cited_wins_over_derived_from_no_duplicate_rows() -> None:
    """A finding both cited AND in derived_from (the live norm) emits ONE row,
    attributed 'cited'; malformed / non-finding citation elements are skipped."""
    excluded_id = str(uuid4())
    citations = [
        "not-a-dict",
        {"ref_id": str(uuid4()), "ref_kind": "signal", "source": "rss"},
        {"ref_id": excluded_id, "ref_kind": "finding",
         "source": "leadership_transition"},
    ]
    usages = _composition_usages(
        citations, [excluded_id], {excluded_id: "leadership_transition"},
    )
    rows = _scorecard_disagreements(_dims(), usages)
    assert [(r.finding_id, r.composition_usage) for r in rows] == [
        (excluded_id, "cited"),
    ]


def test_reducer_is_defensive_on_malformed_band_shapes() -> None:
    """Non-dict verdicts / missing reasons never raise; a reason-less excluded
    dim still reads a legible verdict."""
    fid = str(uuid4())
    dims = {
        "leadership_transition": {"band": "insufficient-evidence"},  # no reason
        "energy_security": "garbage",  # non-dict verdict — skipped
    }
    usages = {fid: ("leadership_transition", "cited")}
    rows = _scorecard_disagreements(dims, usages)
    assert len(rows) == 1
    assert rows[0].scorecard_verdict == "excluded:insufficient-evidence"
    assert _composition_usages(None, [], {}) == {}


# --- The handler end-to-end over a stubbed pg pool (wiring + degradation). ---


class _StubConn:
    """Scripted conn: each fetch pops the next result (or raises it)."""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.queries: list[str] = []

    async def fetch(self, sql: str, *args: Any) -> Any:
        self.queries.append(sql)
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _StubPool:
    def __init__(self, conn: _StubConn) -> None:
        self._conn = conn

    def acquire(self) -> Any:
        conn = self._conn

        class _Ctx:
            async def __aenter__(self) -> _StubConn:
                return conn

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Ctx()


class _StubDeps:
    def __init__(self, conn: _StubConn) -> None:
        class _Reg:
            pass

        self.descriptor_registry = _Reg()
        self.descriptor_registry.pg = _StubPool(conn)  # type: ignore[attr-defined]


def _endpoint(conn: _StubConn) -> Any:
    router = build_v3_router(deps=_StubDeps(conn))  # type: ignore[arg-type]
    return next(
        r for r in router.routes  # type: ignore[attr-defined]
        if r.path == "/eval/country_scorecard"
    ).endpoint


def _scorecard_row(target_id: str, dims: dict[str, Any]) -> dict[str, Any]:
    """One kind='scorecard' head row as the route's first query returns it."""
    return {
        "target_id": target_id,
        "id": str(uuid4()),
        "produced_at": datetime(2026, 7, 10, tzinfo=timezone.utc),
        "data": {
            "title": "t", "body": "b",
            "data": {
                "sub_handler": "scorecard_producer",
                "bands": {
                    "target_id": target_id,
                    "generated_at": "2026-07-10T00:00:00+00:00",
                    "floors": {"faith_floor": 0.50},
                    "dimensions": dims,
                    "composition": {"present": True, "basis": [str(uuid4())]},
                },
            },
        },
    }


def test_route_emits_disagreement_over_live_verified_jsonb_shapes() -> None:
    """End-to-end through the handler: the composition head's LIVE shapes
    (data.data.citations[*].ref_id/source + the derived_from column) reconcile
    against the scorecard's excluded dims — one cited + one lineage-only row."""
    excluded_id = str(uuid4())
    lineage_id = str(uuid4())
    ok_id = str(uuid4())
    dims = _dims(extra={
        "internal_stability": {
            "band": "insufficient-evidence", "basis": [], "reason": "no-finding",
        },
    })
    comp_row = {
        "target_id": "country_g20_us",
        "derived_from": [excluded_id, ok_id, lineage_id],
        "data": {
            "title": "t", "body": "b",
            "data": {
                "citations": [
                    {"ref_id": excluded_id, "ref_kind": "finding",
                     "source": "leadership_transition"},
                    {"ref_id": ok_id, "ref_kind": "finding",
                     "source": "escalation"},
                ],
            },
        },
    }
    lookup_rows = [{"id": lineage_id, "analyst_id": "internal_stability"}]
    conn = _StubConn([
        [_scorecard_row("country_g20_us", dims)],  # scorecard heads
        [comp_row],                                # composition heads
        lookup_rows,                               # lineage id→analyst lookup
    ])
    cards = asyncio.run(_endpoint(conn)(target_id=None, principal="test"))
    assert len(cards) == 1
    got = [
        (d.dimension, d.finding_id, d.composition_usage, d.scorecard_verdict)
        for d in cards[0].disagreements
    ]
    # Deterministic order: (dimension, finding_id).
    assert got == [
        ("internal_stability", lineage_id, "derived_from", "excluded:no-finding"),
        ("leadership_transition", excluded_id, "cited",
         "excluded:low-faithfulness"),
    ]


def test_route_reconciliation_failure_degrades_to_empty_at_200() -> None:
    """A reconciliation-query failure NEVER breaks the scorecard read: the
    handler returns the cards (HTTP 200) with disagreements=[] — the
    system_escalations degradation precedent."""
    conn = _StubConn([
        [_scorecard_row("country_g20_us", _dims())],  # scorecard heads: OK
        RuntimeError("substrate offline"),            # composition query: BOOM
    ])
    cards = asyncio.run(_endpoint(conn)(target_id=None, principal="test"))
    assert len(cards) == 1
    assert cards[0].target_id == "country_g20_us"
    # The banded product itself is intact...
    assert cards[0].floors == {"faith_floor": 0.50}
    # ...and the disagreement surface degrades to the honest empty state.
    assert cards[0].disagreements == []
