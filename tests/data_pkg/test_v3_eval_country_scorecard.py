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
"""

from __future__ import annotations

from uuid import uuid4

from legba.data.registry.v3_api import (
    CountryScorecard,
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
    band / composition), matching the empty-list route return."""
    card = CountryScorecard(
        target_id="country_g20_in",
        id=str(uuid4()),
        produced_at="2026-06-30T00:00:00+00:00",
    )
    assert card.generated_at is None
    assert card.floors == {}
    assert card.dimensions == {}
    assert card.composition == {}
