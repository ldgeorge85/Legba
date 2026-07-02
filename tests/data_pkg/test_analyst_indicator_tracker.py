# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""indicator_tracker handler — S3-T2 structured-I&W run-over-run diff.

Pure-logic coverage of the diff core (join two runs of a unit-stream by the
stable indicator `id` slug; report status FLIPS, esp. not_observed→triggered) +
the synthetic (deps=None) handler path, including the two force_trace_only
contracts: a NO-flip sweep and an unchanged re-sweep are suppressed from the feed
(the findings-feed dedup lesson). The live read (the two most-recent
indicator-bearing findings per (target_id, analyst_id) via deps.pg_pool) is
exercised against the running stack.
"""
from __future__ import annotations

import json
from uuid import uuid4

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
)
from legba.data.analysts.deterministic_handlers import indicator_tracker as it
from legba.runtime.analyst_method import AnalystMethodResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ind(id_: str, status: str, *, statement: str = "signpost", citations=None) -> dict:
    return {
        "id": id_,
        "statement": statement,
        "status": status,
        "citations": citations if citations is not None else [],
    }


def _run(*, target: str | None, analyst: str, produced_at, indicators, data=None) -> dict:
    row = {
        "id": str(uuid4()),
        "target_id": target,
        "analyst_id": analyst,
        "produced_at": produced_at,
    }
    if data is not None:
        row["data"] = data
    else:
        row["indicators"] = indicators
    return row


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_registered_in_dispatch_table():
    assert "indicator_tracker" in SUB_HANDLERS
    assert OUTPUT_KIND_BY_SUB_HANDLER["indicator_tracker"].value == "finding"


def test_descriptor_validates():
    """The shipped descriptor round-trips the real AnalystDescriptor schema
    (the same validation the registrar runs at bringup)."""
    import pathlib

    import yaml

    from legba.data.schemas.analyst import AnalystDescriptor

    root = pathlib.Path(__file__).resolve().parents[2]
    body = yaml.safe_load((root / "descriptors" / "analyst_indicator_tracker.yaml").read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    desc = AnalystDescriptor.model_validate(body, strict=False)
    assert desc.identity.id == "indicator_tracker"
    assert desc.identity.kind == "deterministic"
    assert desc.method.sub_handler == "indicator_tracker"
    # META — no per-target fan-out.
    assert desc.subscription.targets is None


# ---------------------------------------------------------------------------
# Diff core — collect_flips
# ---------------------------------------------------------------------------


def test_flip_detected_joining_by_id():
    """A not_observed→triggered transition on a shared id is one activation flip."""
    rows = [
        _run(target="country_g20_de", analyst="escalation", produced_at=1,
             indicators=[_ind("reservist-mobilization", "not_observed")]),
        _run(target="country_g20_de", analyst="escalation", produced_at=2,
             indicators=[_ind("reservist-mobilization", "triggered", citations=[2])]),
    ]
    flips, groups = it.collect_flips(rows)
    assert groups == 1
    assert len(flips) == 1
    f = flips[0]
    assert f["indicator_id"] == "reservist-mobilization"
    assert f["from_status"] == "not_observed"
    assert f["to_status"] == "triggered"
    assert f["activation"] is True
    assert f["target_id"] == "country_g20_de"
    assert f["source_analyst_id"] == "escalation"


def test_unchanged_status_is_not_a_flip():
    rows = [
        _run(target="t", analyst="a", produced_at=1, indicators=[_ind("x", "not_observed")]),
        _run(target="t", analyst="a", produced_at=2, indicators=[_ind("x", "not_observed")]),
    ]
    flips, groups = it.collect_flips(rows)
    assert flips == []
    assert groups == 0


def test_newly_introduced_indicator_is_not_a_flip():
    """An id present only in the latest run has no prior status → not a flip
    (a unit first populating its indicators never floods the feed)."""
    rows = [
        _run(target="t", analyst="a", produced_at=1, indicators=[_ind("old", "not_observed")]),
        _run(target="t", analyst="a", produced_at=2,
             indicators=[_ind("old", "not_observed"), _ind("brand-new", "triggered")]),
    ]
    flips, _ = it.collect_flips(rows)
    assert flips == []


def test_only_two_most_recent_runs_compared():
    """Three runs: the flip is between the latest two (not_observed→triggered);
    a spurious run1→run3 comparison (triggered→triggered) would find nothing."""
    rows = [
        _run(target="t", analyst="a", produced_at=1, indicators=[_ind("x", "triggered", citations=[1])]),
        _run(target="t", analyst="a", produced_at=2, indicators=[_ind("x", "not_observed")]),
        _run(target="t", analyst="a", produced_at=3, indicators=[_ind("x", "triggered", citations=[3])]),
    ]
    flips, groups = it.collect_flips(rows)
    assert groups == 1
    assert len(flips) == 1
    assert flips[0]["from_status"] == "not_observed"
    assert flips[0]["to_status"] == "triggered"


def test_streams_isolated_by_target_and_analyst():
    """A flip is diffed WITHIN one (target_id, analyst_id) stream — a different
    analyst's run of the same target does not cross-contaminate the diff."""
    rows = [
        # escalation@DE stream: flips.
        _run(target="de", analyst="escalation", produced_at=1, indicators=[_ind("x", "not_observed")]),
        _run(target="de", analyst="escalation", produced_at=2, indicators=[_ind("x", "triggered", citations=[1])]),
        # energy@DE stream: no change (must not be joined to escalation's runs).
        _run(target="de", analyst="energy_security", produced_at=1, indicators=[_ind("x", "not_observed")]),
        _run(target="de", analyst="energy_security", produced_at=2, indicators=[_ind("x", "not_observed")]),
    ]
    flips, groups = it.collect_flips(rows)
    assert groups == 1
    assert {f["source_analyst_id"] for f in flips} == {"escalation"}


def test_single_run_stream_is_skipped():
    rows = [_run(target="t", analyst="a", produced_at=1, indicators=[_ind("x", "triggered", citations=[1])])]
    flips, groups = it.collect_flips(rows)
    assert flips == []
    assert groups == 0


def test_activations_ordered_first():
    rows = [
        # a non-activation flip (triggered→expired).
        _run(target="t", analyst="a", produced_at=1, indicators=[_ind("hz", "triggered", citations=[1])]),
        _run(target="t", analyst="a", produced_at=2, indicators=[_ind("hz", "expired")]),
        # an activation flip (not_observed→triggered) on a different stream.
        _run(target="t", analyst="b", produced_at=1, indicators=[_ind("act", "not_observed")]),
        _run(target="t", analyst="b", produced_at=2, indicators=[_ind("act", "triggered", citations=[2])]),
    ]
    flips, _ = it.collect_flips(rows)
    assert len(flips) == 2
    assert flips[0]["activation"] is True  # activation sorts first
    assert flips[0]["indicator_id"] == "act"


# ---------------------------------------------------------------------------
# Extraction — the persisted analyst_outputs.data shape
# ---------------------------------------------------------------------------


def test_extract_from_persisted_nested_data_dict():
    """The real persisted shape: indicators at data->'data'->'indicators'."""
    prev = _run(target="t", analyst="a", produced_at=1,
                data={"data": {"indicators": [_ind("x", "not_observed")]}}, indicators=None)
    curr = _run(target="t", analyst="a", produced_at=2,
                data={"data": {"indicators": [_ind("x", "triggered", citations=[1])]}}, indicators=None)
    flips, _ = it.collect_flips([prev, curr])
    assert len(flips) == 1
    assert flips[0]["to_status"] == "triggered"


def test_extract_from_jsonb_string_data():
    """asyncpg can hand back the jsonb column as a str — _parse_data tolerates it."""
    prev = _run(target="t", analyst="a", produced_at=1,
                data=json.dumps({"data": {"indicators": [_ind("x", "not_observed")]}}), indicators=None)
    curr = _run(target="t", analyst="a", produced_at=2,
                data=json.dumps({"data": {"indicators": [_ind("x", "triggered", citations=[1])]}}), indicators=None)
    flips, _ = it.collect_flips([prev, curr])
    assert len(flips) == 1


def test_malformed_indicator_entries_dropped():
    rows = [
        _run(target="t", analyst="a", produced_at=1,
             indicators=[_ind("x", "not_observed"), {"no_id": True}, "garbage", {"id": "y", "status": "bogus"}]),
        _run(target="t", analyst="a", produced_at=2,
             indicators=[_ind("x", "triggered", citations=[1])]),
    ]
    flips, _ = it.collect_flips(rows)
    assert len(flips) == 1
    assert flips[0]["indicator_id"] == "x"


# ---------------------------------------------------------------------------
# Handler — synthetic path + the two force_trace_only contracts
# ---------------------------------------------------------------------------


async def test_handle_two_run_flip_emits_exactly_one_flip_finding():
    """ACCEPTANCE: a flipped-fixture two-run sequence → exactly one flip finding,
    emitted to the feed (synthetic path has no prior to dedup against)."""
    rows = [
        _run(target="country_g20_de", analyst="escalation", produced_at=1,
             indicators=[_ind("reservist-mobilization", "not_observed")]),
        _run(target="country_g20_de", analyst="escalation", produced_at=2,
             indicators=[_ind("reservist-mobilization", "triggered", citations=[2])]),
    ]
    result = await it.handle(rows, {"analyst_id": "indicator_tracker"}, None)
    assert isinstance(result, AnalystMethodResult)
    data = result.finding.data
    assert data["sub_handler"] == "indicator_tracker"
    assert data["flip_count"] == 1
    assert data["activation_count"] == 1
    assert len(data["flips"]) == 1
    assert result.finding.kind_marker == "finding"
    assert "indicator_triggered" in result.finding.tags
    assert result.force_trace_only is False
    # zero token usage — deterministic, no LLM.
    assert result.usage == {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}


async def test_handle_no_change_run_is_trace_only():
    """ACCEPTANCE: a no-change run → force_trace_only (no feed row) so an
    idempotent re-run doesn't repeat 'no changes' every cadence tick."""
    rows = [
        _run(target="t", analyst="a", produced_at=1, indicators=[_ind("x", "not_observed")]),
        _run(target="t", analyst="a", produced_at=2, indicators=[_ind("x", "not_observed")]),
    ]
    result = await it.handle(rows, {"analyst_id": "indicator_tracker"}, None)
    assert result.finding.data["flip_count"] == 0
    assert result.force_trace_only is True
    assert "no indicator status changes" in result.finding.title.lower()


async def test_handle_empty_inputs_is_trace_only():
    result = await it.handle([], {"analyst_id": "indicator_tracker"}, None)
    assert result.finding.data["flip_count"] == 0
    assert result.force_trace_only is True
