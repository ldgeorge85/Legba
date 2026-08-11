# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P4-T2 / T5 — the banded-scorecard producer (deterministic META sweep).

Covers, mostly as PURE functions (no DB), plus one async sweep over a fake pool:

  * T5 eval fold — parse_unit_eval (honest-null on absent / JSON-null / malformed)
    + fold_unit_eval (per-dimension eval block, faithfulness_flagged only when the
    aggregate faithfulness is present AND below the floor, never when unmeasured);
  * basis_uuids_for_verdict — the derived_from UNION names ONLY real basis ids;
    an insufficient dimension (basis=[]) + an absent composition contribute
    NOTHING (zero dangling), and dupes are de-duped;
  * build_scorecard_payload — the all-insufficient row STILL emits (honesty tag
    scorecard_all_insufficient), a mixed row carries no such tag, data.bands is
    the verdict verbatim;
  * the async handle sweep over a fake pool — one kind=scorecard side-write per
    active G20 country, the low-faithfulness exclusion yields an empty basis, and
    the no-pool path degrades to an HONEST empty summary (writes nothing).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from legba.data.analysts.deterministic_handlers import scorecard_banding as sb
from legba.data.analysts.deterministic_handlers import scorecard_producer as sp
from legba.data.provenance.kinds import OutputKind


# ---------------------------------------------------------------------------
# T5 — parse_unit_eval + fold_unit_eval
# ---------------------------------------------------------------------------


def _scorer_data(units: dict) -> dict:
    """The unit_correctness_scorer finding's `data` column shape (nested)."""
    return {"sub_handler": "unit_correctness_scorer", "data": {"units": units}}


def test_parse_unit_eval_reads_nested_units_with_honest_null():
    data = _scorer_data({
        "escalation": {"faithfulness": 0.88, "correctness_vs_reference": 0.71,
                       "n_labeled": 12},
        # JSON-null values → honest None (never 0.0), n_labeled absent → 0.
        "energy_security": {"faithfulness": None, "correctness_vs_reference": None},
    })
    ev = sp.parse_unit_eval(data)
    assert ev["escalation"] == {
        "faithfulness": 0.88,
        "judge_pipeline_version": None,
        "correctness_operator": None,
        "n_operator_scored": 0,
        "operator_sufficient": False,
        "correctness_vs_reference": 0.71,
        "n_labeled": 12,
    }
    assert ev["energy_security"] == {
        "faithfulness": None,
        "judge_pipeline_version": None,
        "correctness_operator": None,
        "n_operator_scored": 0,
        "operator_sufficient": False,
        "correctness_vs_reference": None,
        "n_labeled": 0,
    }


def test_parse_unit_eval_carries_the_operator_axis_and_its_judge_population():
    """M-1/M-2 — the operator gold-set axis rides as its OWN keys, and the
    faithfulness figure names the judge that produced it."""
    data = _scorer_data({
        "escalation": {
            "faithfulness": 0.92,
            "faithfulness_population": {"judge_pipeline_version": "2026-08-03/1"},
            "correctness_operator": 0.5,
            "n_operator_scored": 2,
            "operator_sufficient": False,
            "correctness_vs_reference": None,
            "n_labeled": 0,
        },
    })
    rec = sp.parse_unit_eval(data)["escalation"]
    assert rec["correctness_operator"] == 0.5
    assert rec["n_operator_scored"] == 2
    assert rec["operator_sufficient"] is False
    assert rec["judge_pipeline_version"] == "2026-08-03/1"
    # Highly faithful AND only half right — the two axes must be readable
    # side by side, never averaged into one "quality" number.
    assert rec["faithfulness"] == 0.92
    assert rec["correctness_vs_reference"] is None


def test_parse_unit_eval_accepts_json_string_and_degrades_empty_on_garbage():
    data = _scorer_data({"escalation": {"faithfulness": 0.5, "n_labeled": 3}})
    import json
    assert sp.parse_unit_eval(json.dumps(data))["escalation"]["faithfulness"] == 0.5
    # Malformed / missing → empty map (every unit reads unmeasured, not a stub).
    assert sp.parse_unit_eval("not json") == {}
    assert sp.parse_unit_eval(None) == {}
    assert sp.parse_unit_eval({"data": {}}) == {}
    assert sp.parse_unit_eval({"data": {"units": "bad"}}) == {}


def _verdict_with_dims(**bands):
    """A minimal T1-shaped verdict with the given per-dimension band dicts."""
    dims = {u: {"band": sb.INSUFFICIENT, "basis": [], "reason": "no-finding"}
            for u in sb.DIMENSIONS}
    dims.update(bands)
    return {
        "target_id": "country_g20_us",
        "generated_at": "2026-06-30T00:00:00+00:00",
        "floors": {"conf_floor": 0.35, "conf_confident": 0.60, "faith_floor": 0.50},
        "dimensions": dims,
        "composition": {"present": False, "basis": []},
    }


def test_fold_unit_eval_attaches_block_and_flags_low_aggregate_faithfulness():
    verdict = _verdict_with_dims(
        escalation={"band": "high", "basis": [str(uuid4())], "reason": "qualified"},
    )
    eval_by_unit = {
        "escalation": {"faithfulness": 0.40, "correctness_vs_reference": 0.7,
                       "n_labeled": 5},  # below 0.50 floor → flagged
        # leadership_transition unmeasured (absent from the map)
    }
    sp.fold_unit_eval(verdict, eval_by_unit, faith_floor=0.50)

    esc_eval = verdict["dimensions"]["escalation"]["eval"]
    assert esc_eval["faithfulness"] == 0.40
    assert esc_eval["correctness_vs_reference"] == 0.7
    assert esc_eval["n_labeled"] == 5
    assert esc_eval["faithfulness_flagged"] is True

    # An unmeasured unit reads null everywhere and is NEVER flagged — on BOTH
    # correctness axes as well as faithfulness.
    lead_eval = verdict["dimensions"]["leadership_transition"]["eval"]
    assert lead_eval == {
        "faithfulness": None,
        "judge_pipeline_version": None,
        "correctness_operator": None,
        "n_operator_scored": 0,
        "operator_sufficient": False,
        "correctness_vs_reference": None,
        "n_labeled": 0,
        "faithfulness_flagged": False,
    }


def test_fold_unit_eval_keeps_the_operator_axis_off_the_band():
    """M-1 — the operator axis is DISPLAYED on the card and never demotes it:
    the band is the T1 banding's verdict, and a human's semantic judgement of a
    different finding must not silently move it."""
    verdict = _verdict_with_dims(
        escalation={"band": "high", "basis": [str(uuid4())], "reason": "qualified"},
    )
    sp.fold_unit_eval(
        verdict,
        {"escalation": {"faithfulness": 0.95, "correctness_operator": 0.0,
                        "n_operator_scored": 1, "operator_sufficient": False}},
        faith_floor=0.50,
    )
    dim = verdict["dimensions"]["escalation"]
    assert dim["band"] == "high"                       # untouched
    assert dim["eval"]["correctness_operator"] == 0.0  # displayed
    assert dim["eval"]["n_operator_scored"] == 1
    assert dim["eval"]["operator_sufficient"] is False
    assert dim["eval"]["faithfulness_flagged"] is False

    # ...and the rendered body shows the n beside the number, never alone.
    body = sp.build_scorecard_payload("country_g20_us", verdict).body
    assert "op=0.0(n=1,indicative)" in body


def test_fold_unit_eval_does_not_flag_healthy_faithfulness():
    verdict = _verdict_with_dims()
    sp.fold_unit_eval(
        verdict,
        {"escalation": {"faithfulness": 0.9, "correctness_vs_reference": 0.8,
                        "n_labeled": 4}},
        faith_floor=0.50,
    )
    assert verdict["dimensions"]["escalation"]["eval"]["faithfulness_flagged"] is False


# ---------------------------------------------------------------------------
# basis_uuids_for_verdict — the derived_from union (zero dangling)
# ---------------------------------------------------------------------------


def test_basis_union_names_only_real_ids_and_skips_empty_and_absent():
    a, b, comp = str(uuid4()), str(uuid4()), str(uuid4())
    verdict = _verdict_with_dims(
        escalation={"band": "high", "basis": [a], "reason": "qualified"},
        energy_security={"band": "watch", "basis": [b], "reason": "qualified"},
        # leadership_transition + narrative_coordination stay insufficient (basis=[])
    )
    verdict["composition"] = {"present": True, "basis": [comp]}
    ids = sp.basis_uuids_for_verdict(verdict)
    from uuid import UUID
    assert set(ids) == {UUID(a), UUID(b), UUID(comp)}
    # An insufficient dimension contributed nothing → no dangling.
    assert len(ids) == 3


def test_basis_union_dedupes_and_drops_absent_composition():
    a = str(uuid4())
    verdict = _verdict_with_dims(
        escalation={"band": "high", "basis": [a], "reason": "qualified"},
        energy_security={"band": "watch", "basis": [a], "reason": "qualified"},
    )
    # composition absent (present=False) → contributes nothing.
    ids = sp.basis_uuids_for_verdict(verdict)
    from uuid import UUID
    assert ids == [UUID(a)]  # de-duped, no composition id


def test_basis_union_of_all_insufficient_verdict_is_empty():
    verdict = _verdict_with_dims()  # every dim insufficient, no composition
    assert sp.basis_uuids_for_verdict(verdict) == []


# ---------------------------------------------------------------------------
# build_scorecard_payload — the all-insufficient row still emits, honestly
# ---------------------------------------------------------------------------


def test_all_insufficient_row_still_emits_with_honesty_tag():
    verdict = _verdict_with_dims()  # 0 banded
    payload = sp.build_scorecard_payload("country_g20_br", verdict)
    assert payload.kind_marker == "scorecard"
    assert payload.confidence == 1.0
    assert "scorecard_all_insufficient" in payload.tags
    assert "target:country_g20_br" in payload.tags
    # data.bands is the verdict VERBATIM (the product).
    assert payload.data["bands"] == verdict
    assert payload.data["sub_handler"] == "scorecard_producer"


def test_mixed_row_has_no_all_insufficient_tag():
    verdict = _verdict_with_dims(
        escalation={"band": "high", "basis": [str(uuid4())], "reason": "qualified"},
    )
    payload = sp.build_scorecard_payload("country_g20_us", verdict)
    assert "scorecard_all_insufficient" not in payload.tags
    assert "2 banded" not in payload.title  # exactly 1 banded here
    # 7 fixed dimensions (S1-T4/T5/T7 added internal_stability + military_posture
    # + economic_coercion): 1 banded (escalation) + 6 insufficient.
    assert "1 banded / 6 insufficient" in payload.title


# ---------------------------------------------------------------------------
# The async sweep over a fake pool
# ---------------------------------------------------------------------------


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, targets, eval_row, gather_by_target):
        self._targets = targets
        self._eval_row = eval_row
        self._gather_by_target = gather_by_target

    async def fetchrow(self, sql, *args):
        # Only the per-unit eval read uses fetchrow.
        assert "unit_correctness_scorer" in sql
        return self._eval_row

    async def fetch(self, sql, *args):
        if "FROM target_descriptors" in sql:
            return [{"descriptor_id": t} for t in self._targets]
        if "DISTINCT ON (f.analyst_id)" in sql:
            # scorecard_banding._GATHER_SQL — args[0] is the target id.
            return self._gather_by_target.get(args[0], [])
        raise AssertionError(f"unexpected fetch SQL: {sql[:60]}")

    async def execute(self, sql, *args):
        # The supersede UPDATE. Record nothing; just succeed.
        assert "UPDATE analyst_outputs SET superseded_by" in sql
        return "UPDATE 0"


class _FakePool:
    def __init__(self, targets, eval_row, gather_by_target):
        self._targets = targets
        self._eval_row = eval_row
        self._gather_by_target = gather_by_target

    def acquire(self):
        return _AcquireCtx(
            _FakeConn(self._targets, self._eval_row, self._gather_by_target)
        )


class _Deps:
    def __init__(self, pool):
        self.pg_pool = pool


def _gather_row(analyst_id, finding_id, confidence, faithfulness, tags):
    return {
        "finding_id": finding_id,
        "analyst_id": analyst_id,
        "confidence": confidence,
        "faithfulness_score": faithfulness,
        "tags": tags,
        "produced_at": None,
    }


@pytest.mark.asyncio
async def test_handle_sweeps_g20_and_side_writes_one_scorecard_per_country(
    monkeypatch,
):
    us_lead, br_esc = str(uuid4()), str(uuid4())
    # us: leadership_transition bands high; br: escalation low-faithfulness (excluded).
    gather_by_target = {
        "country_g20_us": [
            _gather_row("leadership_transition", us_lead, 0.9, 0.9, ["severity:high"]),
        ],
        "country_g20_br": [
            # conf .9 / faith .3 → R1b low-faithfulness → excluded, basis=[]
            _gather_row("escalation", br_esc, 0.9, 0.3, ["severity:critical"]),
        ],
    }
    eval_row = {"data": _scorer_data({
        "leadership_transition": {"faithfulness": 0.9,
                                  "correctness_vs_reference": 0.8, "n_labeled": 4},
        "escalation": {"faithfulness": 0.3, "correctness_vs_reference": None,
                       "n_labeled": 2},
    })}
    pool = _FakePool(
        ["country_g20_us", "country_g20_br"], eval_row, gather_by_target
    )

    captured: list[dict] = []

    async def _fake_write(conn, *, analyst_ctx, kind, output_payload,
                          derived_from, **kw):
        captured.append({
            "target_id": analyst_ctx.target_id,
            "kind": kind,
            "payload": output_payload,
            "derived_from": list(derived_from),
        })
        return object(), None  # (row, dead) — truthy row, no DLQ

    monkeypatch.setattr(sp, "write_analyst_output", _fake_write)

    result = await sp.handle(
        [], {"sub_handler": "scorecard_producer", "analyst_id": "scorecard_producer",
             "analyst_version": "0" * 16, "run_id": str(uuid4())},
        _Deps(pool),
    )

    # One side-write per active G20 country.
    assert {c["target_id"] for c in captured} == {"country_g20_us", "country_g20_br"}
    assert all(c["kind"] == OutputKind.SCORECARD for c in captured)

    by_target = {c["target_id"]: c for c in captured}

    # US: leadership_transition banded → derived_from NAMES exactly that basis id.
    us = by_target["country_g20_us"]
    us_bands = us["payload"].data["bands"]
    assert us_bands["dimensions"]["leadership_transition"]["band"] == "high"
    assert us_bands["dimensions"]["leadership_transition"]["basis"] == [us_lead]
    from uuid import UUID
    assert us["derived_from"] == [UUID(us_lead)]
    # The eval fold landed on the dimension.
    assert us_bands["dimensions"]["leadership_transition"]["eval"]["faithfulness"] == 0.9

    # BR: escalation was low-faithfulness → excluded (empty basis) → all-insufficient
    # → no dangling derived_from, and the honesty tag is set.
    br = by_target["country_g20_br"]
    br_bands = br["payload"].data["bands"]
    assert br_bands["dimensions"]["escalation"]["band"] == sb.INSUFFICIENT
    assert br_bands["dimensions"]["escalation"]["reason"] == "low-faithfulness"
    assert br_bands["dimensions"]["escalation"]["basis"] == []
    assert br["derived_from"] == []  # every dim insufficient → zero dangling
    assert "scorecard_all_insufficient" in br["payload"].tags
    # The cross-eval faithfulness (0.3) flags escalation on BR's card.
    assert br_bands["dimensions"]["escalation"]["eval"]["faithfulness_flagged"] is True

    # The returned summary is the receipt.
    assert result.finding.data["written"] == 2
    assert result.finding.data["all_insufficient_countries"] == 1


@pytest.mark.asyncio
async def test_handle_no_pool_is_honest_empty_and_writes_nothing(monkeypatch):
    calls: list = []

    async def _fake_write(*a, **k):
        calls.append(1)
        return object(), None

    monkeypatch.setattr(sp, "write_analyst_output", _fake_write)

    # deps=None → no pool → honest empty summary, no side-write.
    result = await sp.handle([], {"sub_handler": "scorecard_producer"}, None)
    assert calls == []
    assert result.finding.data["written"] == 0
    assert result.finding.data["countries"] == 0
    assert "scorecard_producer.no_pool" in result.finding.data["warnings"]
    # Zero-token deterministic run.
    assert result.usage == {
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
    }
