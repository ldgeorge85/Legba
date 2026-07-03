# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""collection_gap handler — S3-T3 collection-requirements aggregation.

Pure-logic coverage of the aggregation core (group scorecard rows by desk;
decide which desk×dimension cells are starved from the CURRENT card; aggregate
the insufficient reasons over the window; rank so an all-insufficient desk tops
the list; name the plausible feed source classes) + the synthetic (deps=None)
handler path, including the force_trace_only contract: a no-gap sweep is
suppressed from the feed (the findings-feed dedup lesson).
"""
from __future__ import annotations

import json
from uuid import uuid4

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
)
from legba.data.analysts.deterministic_handlers import collection_gap as cg
from legba.data.analysts.deterministic_handlers import scorecard_banding
from legba.runtime.analyst_method import AnalystMethodResult


# ---------------------------------------------------------------------------
# Fixtures — a scorecard row in either the "direct dimensions" test shape or the
# persisted `data->data->bands->dimensions` shape.
# ---------------------------------------------------------------------------


def _dim(band: str, reason: str = "") -> dict:
    return {"band": band, "reason": reason, "basis": []}


def _insufficient(reason: str = "no-finding") -> dict:
    return _dim(scorecard_banding.INSUFFICIENT, reason)


def _banded(band: str = "watch") -> dict:
    return _dim(band, "qualified")


def _card(*, desk: str, produced_at, dimensions: dict, persisted: bool = False) -> dict:
    row = {"id": str(uuid4()), "target_id": desk, "produced_at": produced_at}
    if persisted:
        row["data"] = {"data": {"bands": {"dimensions": dimensions}}}
    else:
        row["dimensions"] = dimensions
    return row


def _all_insufficient(reason: str = "no-finding") -> dict:
    return {d: _insufficient(reason) for d in scorecard_banding.DIMENSIONS}


def _all_banded() -> dict:
    return {d: _banded() for d in scorecard_banding.DIMENSIONS}


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_registered_in_dispatch_table():
    assert "collection_gap" in SUB_HANDLERS
    assert OUTPUT_KIND_BY_SUB_HANDLER["collection_gap"].value == "finding"


def test_descriptor_validates():
    """The shipped descriptor round-trips the real AnalystDescriptor schema
    (the same validation the registrar runs at bringup)."""
    import pathlib

    import yaml

    from legba.data.schemas.analyst import AnalystDescriptor

    root = pathlib.Path(__file__).resolve().parents[2]
    body = yaml.safe_load(
        (root / "descriptors" / "analyst_collection_gap.yaml").read_text()
    )
    body.setdefault("identity", {})["version"] = "0" * 16
    desc = AnalystDescriptor.model_validate(body, strict=False)
    assert desc.identity.id == "collection_gap"
    assert desc.identity.kind == "deterministic"
    assert desc.method.sub_handler == "collection_gap"
    # META — no per-target fan-out.
    assert desc.subscription.targets is None


def test_source_class_doctrine_covers_every_dimension():
    """Every banded dimension has a plausible-feed source-class list (no cell
    can ever surface with an empty collection recommendation)."""
    for d in scorecard_banding.DIMENSIONS:
        assert cg.SOURCE_CLASSES_BY_DIMENSION.get(d), d
    # narrative_coordination leads with state_media (framing evidence).
    assert cg.SOURCE_CLASSES_BY_DIMENSION["narrative_coordination"][0] == "state_media"
    # energy_security / leadership_transition / military_posture lead official.
    assert cg.SOURCE_CLASSES_BY_DIMENSION["energy_security"][0] == "official"


# ---------------------------------------------------------------------------
# Aggregation core — aggregate_gaps
# ---------------------------------------------------------------------------


def test_single_starved_cell():
    rows = [
        _card(
            desk="country_g20_de",
            produced_at=1,
            dimensions={
                **_all_banded(),
                "energy_security": _insufficient("below-floor"),
            },
        )
    ]
    gaps, stats = cg.aggregate_gaps(rows)
    assert len(gaps) == 1
    g = gaps[0]
    assert g["desk"] == "country_g20_de"
    assert g["dimension"] == "energy_security"
    assert g["reason"] == "below-floor"
    assert g["source_classes"] == list(
        cg.SOURCE_CLASSES_BY_DIMENSION["energy_security"]
    )
    assert g["desk_starved_dims"] == 1
    assert stats["desks_seen"] == 1


def test_banded_cell_is_not_a_gap():
    rows = [_card(desk="d", produced_at=1, dimensions=_all_banded())]
    gaps, _ = cg.aggregate_gaps(rows)
    assert gaps == []


def test_us_all_insufficient_tops_the_list():
    """ACCEPTANCE: an all-insufficient US desk sorts ALL of its cells above a
    desk starved in a single dimension."""
    rows = [
        _card(desk="country_g20_us", produced_at=1, dimensions=_all_insufficient()),
        _card(
            desk="country_g20_de",
            produced_at=1,
            dimensions={
                **_all_banded(),
                "escalation": _insufficient("verify-failed"),
            },
        ),
    ]
    gaps, stats = cg.aggregate_gaps(rows)
    # US contributes ALL dimensions; DE contributes one.
    assert len(gaps) == len(scorecard_banding.DIMENSIONS) + 1
    # Every one of the US desk's cells tops the ranking.
    top = gaps[: len(scorecard_banding.DIMENSIONS)]
    assert {g["desk"] for g in top} == {"country_g20_us"}
    assert all(
        g["desk_starved_dims"] == len(scorecard_banding.DIMENSIONS) for g in top
    )
    # The single DE cell trails.
    assert gaps[-1]["desk"] == "country_g20_de"
    assert gaps[-1]["dimension"] == "escalation"
    # starved_desks summary ranks US first (starved in every dimension).
    assert stats["starved_desks"][0] == {
        "desk": "country_g20_us",
        "starved_dim_count": len(scorecard_banding.DIMENSIONS),
    }


def test_current_card_decides_starvation_not_history():
    """A cell insufficient last month but banded in the CURRENT card is NOT a
    current gap — the latest-in-window card decides membership."""
    rows = [
        _card(
            desk="d",
            produced_at=1,
            dimensions={**_all_banded(), "escalation": _insufficient("no-finding")},
        ),
        _card(desk="d", produced_at=2, dimensions=_all_banded()),  # current: fed
    ]
    gaps, _ = cg.aggregate_gaps(rows)
    assert gaps == []


def test_reasons_and_persistence_aggregated_over_window():
    """A cell insufficient in the current card carries the window aggregate:
    every insufficient occurrence + the distinct reasons."""
    rows = [
        _card(
            desk="d",
            produced_at=1,
            dimensions={**_all_banded(), "escalation": _insufficient("verify-failed")},
        ),
        _card(
            desk="d",
            produced_at=2,
            dimensions={**_all_banded(), "escalation": _insufficient("no-finding")},
        ),
    ]
    gaps, _ = cg.aggregate_gaps(rows)
    assert len(gaps) == 1
    g = gaps[0]
    # current (latest) reason:
    assert g["reason"] == "no-finding"
    assert g["insufficient_count"] == 2
    assert g["window_scorecards"] == 2
    assert g["persistence"] == 1.0
    assert g["reasons"] == {"verify-failed": 1, "no-finding": 1}


def test_persisted_data_shape_extracted():
    """The real persisted shape: dimensions at data->data->bands->dimensions."""
    rows = [
        _card(
            desk="d",
            produced_at=1,
            persisted=True,
            dimensions={
                **_all_banded(),
                "military_posture": _insufficient("no-severity-tag"),
            },
        )
    ]
    gaps, _ = cg.aggregate_gaps(rows)
    assert len(gaps) == 1
    assert gaps[0]["dimension"] == "military_posture"
    assert gaps[0]["reason"] == "no-severity-tag"


def test_persisted_jsonb_string_data_extracted():
    """asyncpg can hand the jsonb column back as a str — _parse_data tolerates it."""
    row = {
        "id": str(uuid4()),
        "target_id": "d",
        "produced_at": 1,
        "data": json.dumps(
            {
                "data": {
                    "bands": {
                        "dimensions": {
                            **_all_banded(),
                            "internal_stability": _insufficient("below-floor"),
                        }
                    }
                }
            }
        ),
    }
    gaps, _ = cg.aggregate_gaps([row])
    assert len(gaps) == 1
    assert gaps[0]["dimension"] == "internal_stability"


def test_by_dimension_rollup_counts_starved_desks():
    rows = [
        _card(
            desk="a",
            produced_at=1,
            dimensions={**_all_banded(), "escalation": _insufficient()},
        ),
        _card(
            desk="b",
            produced_at=1,
            dimensions={**_all_banded(), "escalation": _insufficient()},
        ),
    ]
    _, stats = cg.aggregate_gaps(rows)
    assert stats["by_dimension"]["escalation"]["desks_starved"] == 2
    assert stats["by_dimension"]["escalation"]["source_classes"] == list(
        cg.SOURCE_CLASSES_BY_DIMENSION["escalation"]
    )
    # dimensions with no starved desk are absent from the rollup.
    assert "energy_security" not in stats["by_dimension"]


# ---------------------------------------------------------------------------
# Handler — synthetic path + the force_trace_only contract
# ---------------------------------------------------------------------------


async def test_handle_gap_emits_one_collection_finding():
    """ACCEPTANCE: current-style scorecards with starved cells → exactly one
    collection-requirements finding, emitted to the feed (synthetic path has no
    prior to dedup against)."""
    rows = [
        _card(desk="country_g20_us", produced_at=1, dimensions=_all_insufficient()),
        _card(
            desk="country_g20_de",
            produced_at=1,
            dimensions={**_all_banded(), "narrative_coordination": _insufficient()},
        ),
    ]
    result = await cg.handle(rows, {"analyst_id": "collection_gap"}, None)
    assert isinstance(result, AnalystMethodResult)
    data = result.finding.data
    assert data["sub_handler"] == "collection_gap"
    assert data["gap_count"] == len(scorecard_banding.DIMENSIONS) + 1
    assert data["starved_desk_count"] == 2
    # US tops the gap list.
    assert data["gaps"][0]["desk"] == "country_g20_us"
    assert result.finding.kind_marker == "finding"
    assert "collection_gap" in result.finding.tags
    assert result.force_trace_only is False
    assert result.usage == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
    }


async def test_handle_no_gap_is_trace_only():
    """ACCEPTANCE: a fully-fed roster → force_trace_only (no feed row) so an
    idempotent monthly re-run doesn't repeat 'nothing starved'."""
    rows = [
        _card(desk="a", produced_at=1, dimensions=_all_banded()),
        _card(desk="b", produced_at=1, dimensions=_all_banded()),
    ]
    result = await cg.handle(rows, {"analyst_id": "collection_gap"}, None)
    assert result.finding.data["gap_count"] == 0
    assert result.force_trace_only is True
    assert "no starved cells" in result.finding.title.lower()


async def test_handle_empty_inputs_is_trace_only():
    result = await cg.handle([], {"analyst_id": "collection_gap"}, None)
    assert result.finding.data["gap_count"] == 0
    assert result.force_trace_only is True
