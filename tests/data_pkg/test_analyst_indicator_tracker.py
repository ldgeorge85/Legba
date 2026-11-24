# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""indicator_tracker handler — S3-T2 structured-I&W flips, debounced (W11-F55).

Pure-logic coverage of the sweep core (follow each unit-stream's indicators run
over run by the stable `id` slug, else the normalised statement; record every
consecutive-run change as RAW; count a change — DEBOUNCED — only once the new
status has held) and of the synthetic (deps=None) handler path, including the
force_trace_only contract: only a debounced flip reaches the feed. The live
path (the watermark store, migration 0237, the reader) is covered against a
real database in ``test_indicator_flip_debounce.py``.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
)
from legba.data.analysts.deterministic_handlers import indicator_tracker as it
from legba.data.analysts.deterministic_handlers._indicator_debounce import (
    StreamState,
    advance_stream,
)
from legba.data.provenance.kinds import (
    STRUCTURAL_CLAIMS_VERIFY_ANALYSTS,
    STRUCTURAL_VERIFY_EXEMPT_ANALYSTS,
)
from legba.data.provenance.verify import verify_structural_claims
from legba.runtime.analyst_method import AnalystMethodResult

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


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


def _at(i: int) -> datetime:
    return T0 + timedelta(hours=11 * i)


def _run(*, target: str | None, analyst: str, produced_at, indicators, data=None) -> dict:
    row = {
        "id": str(uuid4()),
        "target_id": target,
        "analyst_id": analyst,
        "produced_at": _at(produced_at) if isinstance(produced_at, int) else produced_at,
    }
    if data is not None:
        row["data"] = data
    else:
        row["indicators"] = indicators
    return row


def _stream(statuses: list[str], *, target="t", analyst="a", slug="x") -> list[dict]:
    return [
        _run(target=target, analyst=analyst, produced_at=i, indicators=[_ind(slug, s)])
        for i, s in enumerate(statuses)
    ]


N, T = "not_observed", "triggered"


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


def test_hysteresis_knobs_are_declared_in_the_option_catalog():
    """A knob the handler reads must be declared before a descriptor sets it."""
    from legba.data.option_catalog.handler_options_catalog import HANDLER_OPTIONS

    names = {spec.name for spec in HANDLER_OPTIONS["indicator_tracker"]}
    assert {"lookback_days", "hysteresis_runs", "hysteresis_hours"} <= names


# ---------------------------------------------------------------------------
# Raw flips — the consecutive-run diff (the audit record)
# ---------------------------------------------------------------------------


def test_raw_flip_joining_by_id():
    """A not_observed→triggered change on a shared id is one raw activation."""
    sweep = it.collect_flips(_stream([N, T], target="country_g20_de", analyst="escalation",
                                     slug="reservist-mobilization"))
    assert len(sweep.raw) == 1
    f = sweep.raw[0]
    assert f["grade"] == "raw"
    assert f["indicator_id"] == "reservist-mobilization"
    assert (f["from_status"], f["to_status"]) == (N, T)
    assert f["activation"] is True
    assert f["match"] == "id"
    assert f["target_id"] == "country_g20_de"
    assert f["source_analyst_id"] == "escalation"


def test_fuzzy_statement_join_when_ids_re_minted():
    """DQ P6 — when two runs share NO ids (re-minted slugs), the lineage is
    followed by the normalized STATEMENT text, so a status change on a
    re-slugged-but-same signpost is still seen."""
    stmt = "Reservist call-up or mobilization orders issued by the target state"
    rows = [
        _run(target="de", analyst="escalation", produced_at=0,
             indicators=[_ind("reservist-mobilization", N, statement=stmt)]),
        _run(target="de", analyst="escalation", produced_at=1,
             indicators=[_ind("mobilization-order-issued", T,
                              statement=stmt.upper() + " .", citations=[2])]),
    ]
    sweep = it.collect_flips(rows)
    assert len(sweep.raw) == 1
    f = sweep.raw[0]
    assert f["match"] == "statement"
    assert f["indicator_id"] == "mobilization-order-issued"  # the current slug
    assert f["citations"] == [2]


def test_no_flip_when_ids_and_statements_both_differ():
    rows = [
        _run(target="t", analyst="a", produced_at=0,
             indicators=[_ind("aaa", N, statement="alpha signpost")]),
        _run(target="t", analyst="a", produced_at=1,
             indicators=[_ind("bbb", T, statement="beta signpost")]),
    ]
    sweep = it.collect_flips(rows)
    assert sweep.raw == [] and sweep.debounced == []


def test_unchanged_status_is_not_a_flip():
    sweep = it.collect_flips(_stream([N, N, N]))
    assert sweep.raw == [] and sweep.debounced == []


def test_newly_introduced_indicator_is_not_a_flip():
    """An id with no prior run in its lineage has no prior status → no flip."""
    rows = [
        _run(target="t", analyst="a", produced_at=0, indicators=[_ind("old", N)]),
        _run(target="t", analyst="a", produced_at=1,
             indicators=[_ind("old", N), _ind("brand-new", T)]),
        _run(target="t", analyst="a", produced_at=2,
             indicators=[_ind("old", N), _ind("brand-new", T)]),
    ]
    sweep = it.collect_flips(rows)
    assert sweep.raw == [] and sweep.debounced == []


def test_streams_isolated_by_target_and_analyst():
    rows = [
        *_stream([N, T, T], target="de", analyst="escalation"),
        *_stream([N, N, N], target="de", analyst="energy_security"),
    ]
    sweep = it.collect_flips(rows)
    assert {f["source_analyst_id"] for f in sweep.raw + sweep.debounced} == {"escalation"}


def test_extract_from_persisted_nested_data_dict_and_jsonb_string():
    rows = [
        _run(target="t", analyst="a", produced_at=0, indicators=None,
             data={"data": {"indicators": [_ind("x", N)]}}),
        _run(target="t", analyst="a", produced_at=1, indicators=None,
             data=json.dumps({"data": {"indicators": [_ind("x", T)]}})),
    ]
    sweep = it.collect_flips(rows)
    assert len(sweep.raw) == 1 and sweep.raw[0]["to_status"] == T


def test_malformed_indicator_entries_dropped():
    rows = [
        _run(target="t", analyst="a", produced_at=0,
             indicators=[_ind("x", N), {"no_id": True}, "garbage", {"id": "y", "status": "bogus"}]),
        _run(target="t", analyst="a", produced_at=1, indicators=[_ind("x", T)]),
    ]
    sweep = it.collect_flips(rows)
    assert [f["indicator_id"] for f in sweep.raw] == ["x"]


# ---------------------------------------------------------------------------
# Debounce — the hysteresis rule
# ---------------------------------------------------------------------------


def test_eleven_oscillations_make_eleven_raw_and_one_debounced_flip():
    """DONE TEST (W11-F55): a stream that oscillates 11 times between two held
    stretches yields 11 raw records and ONE debounced flip, which says how many
    raw toggles it took to settle."""
    statuses = [N, N] + [T, N] * 5 + [T, T]
    sweep = it.collect_flips(_stream(statuses))
    assert len(sweep.raw) == 11
    assert len(sweep.debounced) == 1
    d = sweep.debounced[0]
    assert (d["from_status"], d["to_status"]) == (N, T)
    assert d["raw_flips_between"] == 11
    assert d["held_runs"] == 2 and d["confirmed_by"] == "runs"


def test_pure_oscillation_never_debounces():
    """Toggling every run, nothing ever holds: raw only, zero debounced."""
    sweep = it.collect_flips(_stream([N, N] + [T, N] * 6))
    assert len(sweep.raw) == 12
    assert sweep.debounced == []


def test_a_clean_change_debounces_once_at_the_second_run():
    sweep = it.collect_flips(_stream([N, N, T, T, T, T]))
    assert len(sweep.raw) == 1 and len(sweep.debounced) == 1
    d = sweep.debounced[0]
    assert d["raw_flips_between"] == 1
    # onset = the first triggered run; confirmed = the second.
    assert d["evidence_at"] == _at(2) and d["confirmed_at"] == _at(3)


def test_a_blip_and_back_is_not_a_change():
    """N N T N N: the T never held, and the indicator is back where it was."""
    sweep = it.collect_flips(_stream([N, N, T, N, N]))
    assert len(sweep.raw) == 2
    assert sweep.debounced == []


def test_the_first_held_status_is_a_baseline_not_a_flip():
    sweep = it.collect_flips(_stream([T, T, T]))
    assert sweep.debounced == []


def test_change_and_change_back_are_two_debounced_flips():
    sweep = it.collect_flips(_stream([N, N, T, T, N, N]))
    assert [(d["from_status"], d["to_status"]) for d in sweep.debounced] == [(N, T), (T, N)]


def test_hysteresis_runs_is_tunable():
    statuses = [N, N, N, T, T, N, T, T, T]
    assert len(it.collect_flips(_stream(statuses), hysteresis_runs=2).debounced) == 1
    # Three runs: the first T T never held, the later T T T did.
    three = it.collect_flips(_stream(statuses), hysteresis_runs=3).debounced
    assert len(three) == 1 and three[0]["raw_flips_between"] == 3
    # One run = no debounce: every raw change counts.
    assert len(it.collect_flips(_stream(statuses), hysteresis_runs=1).debounced) == 3


def test_hours_leg_holds_a_status_whose_runs_span_one_cadence():
    """hysteresis_runs=3 alone would need three runs; two runs 11 h apart
    already span one 11 h cadence, so the hours leg confirms."""
    rows = _stream([N, N, N, T, T])
    assert it.collect_flips(rows, hysteresis_runs=3).debounced == []
    via_cadence = it.collect_flips(rows, hysteresis_runs=3, cadence_hours={"a": 11.0}).debounced
    assert len(via_cadence) == 1 and via_cadence[0]["confirmed_by"] == "hours"
    # An explicit 0 turns the hours leg off, whatever the cadence.
    assert it.collect_flips(rows, hysteresis_runs=3, hysteresis_hours=0,
                            cadence_hours={"a": 11.0}).debounced == []


def test_hours_leg_ignores_a_rerun_minutes_apart():
    """Two runs two minutes apart span 2 min, not one cadence."""
    rows = _stream([N, N, N, T])
    rows.append(_run(target="t", analyst="a", produced_at=_at(3) + timedelta(minutes=2),
                     indicators=[_ind("x", T)]))
    assert it.collect_flips(rows, hysteresis_runs=3, hysteresis_hours=11).debounced == []


def test_incremental_walk_equals_one_walk_over_the_history():
    """The watermark carries everything the walk needs: run-by-run sweeps from
    the saved state find exactly the flips one sweep over all runs finds (the
    first-sight sweep's flips are the stream's backfill)."""
    statuses = [N, N, T, N, T, T, T, N, N, T, N, N, T, T]
    rows = _stream(statuses)
    whole = it.collect_flips(rows)
    states: dict = {}
    raw, deb = [], []
    for i in range(len(rows)):
        sweep = it.collect_flips(rows[: i + 1], states=states)
        states = {**states, **sweep.states}
        raw += sweep.raw
        deb += sweep.debounced

    def key(f):
        return (f["indicator_id"], f["from_status"], f["to_status"], f["evidence_finding_id"])

    assert sorted(map(key, raw)) == sorted(map(key, whole.raw))
    assert sorted(map(key, deb)) == sorted(map(key, whole.debounced))
    assert all(f["backfill"] for f in whole.raw + whole.debounced)
    assert not any(f["backfill"] for f in deb)  # every later sweep is live


def test_a_rerun_from_the_saved_state_finds_nothing():
    rows = _stream([N, N, T, T])
    first = it.collect_flips(rows)
    again = it.collect_flips(rows, states=first.states)
    assert again.raw == [] and again.debounced == [] and again.states == {}


def test_state_round_trips_through_json():
    rows = _stream([N, N, T])
    st, _, _ = advance_stream(StreamState(), [
        {"id": r["id"], "produced_at": r["produced_at"], "indicators": r["indicators"]}
        for r in rows
    ], target_id="t", analyst_id="a")
    again = StreamState.from_json(json.loads(json.dumps(st.to_json())))
    assert again.to_json() == st.to_json()
    assert again.lineages[0].confirmed == N and again.lineages[0].status == T


# ---------------------------------------------------------------------------
# Handler — synthetic path + the feed contract
# ---------------------------------------------------------------------------


async def test_handle_feeds_only_the_debounced_flip():
    """The receipt (the feed row) carries the debounced flip, never the raw
    toggles; the raw count rides along as an audit number."""
    statuses = [N, N] + [T, N] * 5 + [T, T]
    result = await it.handle(_stream(statuses, target="country_g20_de", analyst="escalation"),
                             {"analyst_id": "indicator_tracker"}, None)
    assert isinstance(result, AnalystMethodResult)
    data = result.finding.data
    assert data["flip_grade"] == "debounced"
    assert data["flip_count"] == 1 and data["activation_count"] == 1
    assert data["raw_flips_recorded"] == 11
    assert len(data["flips"]) == 1
    assert data["flips"][0]["raw_flips_between"] == 11
    assert result.force_trace_only is False
    assert "indicator_triggered" in result.finding.tags
    assert result.usage == {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}


async def test_handle_raw_only_sweep_is_trace_only():
    """A sweep whose changes never held writes no feed row."""
    result = await it.handle(_stream([N, T]), {"analyst_id": "indicator_tracker"}, None)
    assert result.finding.data["flip_count"] == 0
    assert result.finding.data["raw_flips_recorded"] == 1
    assert result.force_trace_only is True


async def test_handle_no_change_run_is_trace_only():
    result = await it.handle(_stream([N, N]), {"analyst_id": "indicator_tracker"}, None)
    assert result.finding.data["flip_count"] == 0
    assert result.force_trace_only is True
    assert "no indicator status changes" in result.finding.title.lower()


async def test_handle_empty_inputs_is_trace_only():
    result = await it.handle([], {"analyst_id": "indicator_tracker"}, None)
    assert result.finding.data["flip_count"] == 0
    assert result.force_trace_only is True


async def test_handle_reads_the_hysteresis_options():
    rows = _stream([N, N, T])
    assert (await it.handle(rows, {}, None)).force_trace_only is True
    one = await it.handle(rows, {"hysteresis_runs": 1}, None)
    assert one.force_trace_only is False and one.finding.data["flip_count"] == 1


# ---------------------------------------------------------------------------
# A2 / C2b — real citations, derived_from, structural claims
# ---------------------------------------------------------------------------


def test_registered_in_both_verify_exempt_registries():
    assert "indicator_tracker" in STRUCTURAL_VERIFY_EXEMPT_ANALYSTS
    assert "indicator_tracker" in STRUCTURAL_CLAIMS_VERIFY_ANALYSTS


def test_flip_finding_ids_dedupes_and_skips_unresolvable():
    id_a, id_b, id_c = uuid4(), uuid4(), uuid4()
    flips = [
        {"source_finding_ids": {"prev": str(id_a), "curr": str(id_b)}},
        {"source_finding_ids": {"prev": str(id_b), "curr": str(id_c)}},
        {"source_finding_ids": {"prev": None, "curr": None}},
        {},
    ]
    ids = it._flip_finding_ids(flips)
    assert ids == [id_a, id_b, id_c]
    assert all(isinstance(u, UUID) for u in ids)


async def test_receipt_cites_the_old_status_run_and_the_confirming_run():
    """A2: the receipt cites the last run in the old status and the run that
    confirmed the new one — rows the sweep read — and derived_from matches."""
    rows = _stream([N, N, T, T])
    result = await it.handle(rows, {}, None)
    flip = result.finding.data["flips"][0]
    assert flip["source_finding_ids"] == {"prev": rows[1]["id"], "curr": rows[3]["id"]}
    cited = {c["ref_id"] for c in result.finding.data["citations"]}
    assert cited == {rows[1]["id"], rows[3]["id"]}
    assert {str(u) for u in result.derived_from} == cited


def test_build_finding_citations_and_derived_from_empty_together():
    finding = it._build_finding([], groups_compared=0)
    assert finding.data["citations"] == []
    assert it._flip_finding_ids([]) == []


async def test_receipt_structural_claims_re_derive_supported():
    rows = [
        _run(target="de", analyst="escalation", produced_at=i,
             indicators=[_ind("x", a), _ind("y", b, statement="other")])
        for i, (a, b) in enumerate([(N, T), (N, T), (T, N), (T, N)])
    ] + _stream([N, N, T, T], target="fr", analyst="energy_security")
    result = await it.handle(rows, {}, None)
    data = result.finding.data
    assert data["flip_count"] == 3 and data["groups_compared"] == 2
    report = verify_structural_claims(data=data)
    assert report.had_claims is True
    assert report.structural_verified is True
    assert report.miscount == 0
    assert report.unverifiable == 0
