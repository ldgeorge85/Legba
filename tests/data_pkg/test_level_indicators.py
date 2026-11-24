# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X4 LEVEL (W11-F36) — level-shaped indicators re-evidence a standing status.

The defect: the carried-state rule (W11-F14) withholds a unit status none of the
unit's own indicators re-evidenced inside N cadence windows, and the units whose
status is a STANDING condition (energy_security "high" under a sanctions regime,
military_posture on a war footing) had only change-shaped signposts, so their
honest status was withheld forever. The done line, as tests:

  * the option is declared in the kind catalog BEFORE a descriptor sets it, and
    both converted descriptors parse through the runtime schema (the 09-28
    rule) with no rejected key;
  * the prompt names every declared reference verbatim; the profile's
    ``structural`` flag and the descriptors agree both ways;
  * the schema, the pre-coercion shaping, and the demotion of a level resting
    on desk memory alone;
  * a fixture unit with one level indicator observed at level is NOT
    carried_unsupported after 3 windows; a change indicator alone still is —
    pure, through ``run_method``, and on the REAL driver (the SQL predicate);
  * verify holds a level observation to a citation; the I&W tracker never
    reads one as a firing.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

import pytest
import yaml

from legba.data import carried_state as cs
from legba.data import indicator_shape as shape
from legba.data.option_catalog.handler_options import (
    ANALYST_KIND_OPTIONS,
    resolve_kind_options,
)
from legba.data.option_catalog.handler_options_level import LEVEL_INDICATORS_OPTION
from legba.data.schemas.analyst import AnalystDescriptor, IndicatorEntry

REPO = Path(__file__).resolve().parents[2]
DESCRIPTORS = REPO / "descriptors"
PROFILE = REPO / "profiles" / "geopolitics" / "profile.yaml"
NOW = datetime(2026, 10, 13, 16, 0, tzinfo=timezone.utc)
CADENCE_H = 11.0  # energy_security / military_posture cooldown_seconds 39600

CONVERTED = {
    "energy_security": {
        "energy-trade-restriction-in-force",
        "energy-route-disruption-in-force",
        "energy-supply-stress-in-force",
    },
    "military_posture": {"forces-in-active-operations", "heightened-posture-in-force"},
}


def _body(unit: str) -> dict[str, Any]:
    return yaml.safe_load((DESCRIPTORS / f"analyst_{unit}.yaml").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. The option — declared, validated, parsed
# ---------------------------------------------------------------------------


def test_the_option_is_declared_in_the_inline_target_kind_catalog() -> None:
    names = {sp.name for sp in ANALYST_KIND_OPTIONS["inline_target"]}
    assert LEVEL_INDICATORS_OPTION == "level_indicators" and LEVEL_INDICATORS_OPTION in names


@pytest.mark.parametrize(
    "entry",
    [
        "energy-supply-stress-in-force=bar:a signal in this window reports rationing still in effect",
        "crude-imports-at-level=series:eia.crude_oil_imports_tbpd",
        "crude-imports-at-level=series:eia.crude_oil_imports_tbpd above its 2016-2025 range",
    ],
)
def test_the_option_accepts_a_bar_and_a_series(entry: str) -> None:
    res = resolve_kind_options("inline_target", {LEVEL_INDICATORS_OPTION: [entry]})
    assert res.accepted == {LEVEL_INDICATORS_OPTION: [entry]} and not res.rejected


@pytest.mark.parametrize(
    "entry",
    [
        "x=band:last_composition",            # not a reference kind (no band in the prompt)
        "Upper-Case=bar:a report",            # the id is a canonical slug
        "no-reference=bar:",                  # an empty bar
        "two-lines=bar:a report\nand more",   # one line
        "no-equals-bar:a report",
        "x=series:EIA.Upper",                 # a series id is lower-case
        "x=bar:" + "a" * 241,                 # over the 240-character bar
    ],
)
def test_the_option_refuses_a_malformed_entry(entry: str) -> None:
    res = resolve_kind_options("inline_target", {LEVEL_INDICATORS_OPTION: [entry]})
    assert LEVEL_INDICATORS_OPTION not in res.accepted
    assert [r.key for r in res.rejected] == [LEVEL_INDICATORS_OPTION]


def test_the_longest_admissible_reference_fits_the_schema() -> None:
    """A reference the grammar admits must never be dropped by the schema it is
    stamped into (``IndicatorEntry.reference`` max 256)."""
    longest = [
        "a=bar:" + "b" * 240,
        "a=series:" + "s" * 96 + " " + "t" * 140,
    ]
    for entry in longest:
        assert resolve_kind_options("inline_target", {LEVEL_INDICATORS_OPTION: [entry]}).accepted
        [spec] = shape.level_specs({LEVEL_INDICATORS_OPTION: [entry]}).values()
        IndicatorEntry(**_entry(shape="level", reference=spec.reference))
    for too_long in ("a=series:" + "s" * 97, "a=series:s " + "t" * 141):
        assert not resolve_kind_options("inline_target", {LEVEL_INDICATORS_OPTION: [too_long]}).accepted


def test_level_specs_parses_and_keeps_the_first_of_a_repeated_id() -> None:
    specs = shape.level_specs({LEVEL_INDICATORS_OPTION: [
        "a-level=bar:first bar", "b-level=series:wb.gdp_growth_annual_pct", "a-level=bar:second",
        "Bad=bar:x", 7,
    ]})
    assert list(specs) == ["a-level", "b-level"]
    assert specs["a-level"] == shape.LevelSpec("a-level", "bar", "bar:first bar")
    assert specs["b-level"].kind == "series"
    assert shape.level_specs(None) == {} and shape.level_specs({}) == {}


@pytest.mark.parametrize("unit", sorted(CONVERTED))
def test_the_converted_descriptor_parses_through_the_runtime_schema(unit: str) -> None:
    """The 09-28 rule: the PUT body validates through ``AnalystDescriptor`` and
    every ``method.options`` key resolves against the kind catalog, none
    rejected — so the runtime parse cannot strand the analyst."""
    body = _body(unit)
    body.setdefault("identity", {})["version"] = "0" * 16
    desc = AnalystDescriptor.model_validate(body, strict=False)
    kind = str(getattr(desc.identity.kind, "value", desc.identity.kind))
    res = resolve_kind_options(kind, desc.method.options, log_context=f"{unit}@test")
    assert not res.rejected, res.rejected
    assert set(shape.level_specs(res.accepted)) == CONVERTED[unit]


@pytest.mark.parametrize("unit", sorted(CONVERTED))
def test_the_prompt_names_every_reference_verbatim(unit: str) -> None:
    body = _body(unit)
    prompt = body["method"]["system_prompt"]
    for spec in shape.level_specs(body["method"]["options"]).values():
        assert f'"id": "{spec.indicator_id}"' in prompt, spec.indicator_id
        assert f'"shape": "level", "reference": "{spec.reference}"' in prompt, spec.indicator_id
    assert "observed_at_level" in prompt and "LEVEL INDICATORS" in prompt
    # Every example indicator object in the prompt still validates once the
    # placeholders are filled — the model is shown a shape the schema accepts.
    for line in re.findall(r'\{"id": "[^\n]*\}', prompt):
        entry = json.loads(re.sub(r'"<as-of[^"]*>"', '"2026-10-13"', line))
        IndicatorEntry(**entry)


def test_the_profile_structural_flag_and_the_descriptors_agree_both_ways() -> None:
    from legba.data.profile import load_profile

    units = load_profile(PROFILE).rosters.units
    structural = {u.id for u in units if u.structural}
    declaring = {
        u.id for u in units
        if shape.level_specs((_body(u.id).get("method") or {}).get("options") or {})
    }
    assert structural == declaring == set(CONVERTED)


# ---------------------------------------------------------------------------
# 2. The schema and the two handler rules
# ---------------------------------------------------------------------------


def _entry(**kw: Any) -> dict[str, Any]:
    base = {"id": "x-level", "statement": "a standing condition", "status": "not_observed",
            "horizon_date": "2026-10-20", "first_seen": "2026-10-13", "citations": []}
    return {**base, **kw}


def test_the_schema_admits_a_level_observation_only_on_a_level_indicator() -> None:
    ok = IndicatorEntry(**_entry(status="observed_at_level", shape="level",
                                 reference="bar:a report", citations=[2]))
    assert ok.status == "observed_at_level"
    for bad in (
        _entry(status="observed_at_level"),                         # no shape
        _entry(status="observed_at_level", shape="change"),
        _entry(shape="level"),                                      # level without a reference
        _entry(reference="bar:a report"),                           # reference without level
    ):
        with pytest.raises(ValueError):
            IndicatorEntry(**bad)


def test_a_change_indicator_dumps_byte_identically() -> None:
    from legba.data.schemas.analyst import validate_indicators

    [dumped] = validate_indicators([_entry(status="triggered", citations=[1])])
    assert set(dumped) == {"id", "statement", "status", "horizon_date", "first_seen", "citations"}


def test_shaping_stamps_the_declaration_and_strips_the_models() -> None:
    specs = shape.level_specs({LEVEL_INDICATORS_OPTION: ["x-level=bar:the declared bar"]})
    raw = [
        _entry(status="triggered", citations=[1]),                                # declared
        _entry(id="y-change", status="observed_at_level", shape="level",          # undeclared
               reference="bar:the model's own"),
        "not a dict",
    ]
    out = shape.shape_raw_indicators(raw, specs)
    assert out[0]["shape"] == "level" and out[0]["reference"] == "bar:the declared bar"
    assert out[0]["status"] == "observed_at_level"
    assert "shape" not in out[1] and "reference" not in out[1]
    assert raw[0]["status"] == "triggered", "the input is never mutated"
    plain = [_entry(status="triggered", citations=[1])]
    assert shape.shape_raw_indicators(plain, {}) is plain, "no declaration: byte-identical"

    from legba.data.analysts.inline_target import _coerce_indicators

    coerced = _coerce_indicators(raw, specs)
    # The undeclared observed_at_level fails the schema and drops alone.
    assert [e["id"] for e in coerced] == ["x-level"]
    assert coerced[0]["status"] == "observed_at_level"
    # No declaration at all (absent, or rejected at resolve): a shape the model
    # copied from the prompt's example is stripped, so its level status drops.
    copied = [_entry(status="observed_at_level", shape="level", reference="bar:x", citations=[1])]
    assert _coerce_indicators(copied, {}) == []
    assert _coerce_indicators(copied) == []


def _signal_cite(n: int) -> dict[str, Any]:
    return {"marker": f"[{n}]", "signal_id": str(uuid4()), "title": "a report"}


def _prior_read_cite(n: int) -> dict[str, Any]:
    return {"marker": f"[{n}]", "ref_kind": "prior_read", "ref_id": str(uuid4()),
            "grounding": "prior_read", "marker_class": "desk_grounding"}


def test_a_level_resting_on_desk_memory_alone_is_set_back() -> None:
    level = {"shape": "level", "reference": "bar:a report", "status": "observed_at_level"}
    data = {
        "citations": [_signal_cite(1), _prior_read_cite(121)],
        "indicators": [
            _entry(id="on-a-report", citations=[1], **level),
            _entry(id="on-memory", citations=[121], **level),
            _entry(id="on-nothing", citations=[], **level),
            _entry(id="a-change", status="triggered", citations=[121]),
        ],
    }
    out, demoted = shape.demote_unsupported_levels(data)
    assert demoted == ["on-memory", "on-nothing"]
    by_id = {e["id"]: e for e in out["indicators"]}
    assert by_id["on-a-report"]["status"] == "observed_at_level"
    assert by_id["on-memory"]["status"] == "not_observed" and by_id["on-memory"]["citations"] == []
    assert by_id["a-change"]["status"] == "triggered", "change indicators are verify's, not this rule's"
    assert out[shape.DEMOTIONS_DATA_KEY] == ["on-memory", "on-nothing"]
    assert data["indicators"][1]["status"] == "observed_at_level", "the input is never mutated"
    _, none = shape.demote_unsupported_levels({"indicators": [_entry()]})
    assert none == []


# ---------------------------------------------------------------------------
# 3. THE DONE LINE — pure: N windows of reads, level vs change
# ---------------------------------------------------------------------------


def _read(age_h: float, indicators: list[dict[str, Any]], severity: str = "high") -> dict[str, Any]:
    return {
        "id": uuid4(), "analyst_id": "energy_security", "target_id": "country_g20_gb",
        "severity": severity, "produced_at": NOW - timedelta(hours=age_h),
        "data": {"tags": [f"severity:{severity}"], "data": {"indicators": indicators}},
    }


def _level_obs(citations: list[int]) -> dict[str, Any]:
    return _entry(id="energy-supply-stress-in-force", status="observed_at_level",
                  shape="level", reference="bar:a report", citations=citations)


def _verdict_at_head(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The rule over a read history (oldest first): the head's verdict, its
    last evidence chosen by the same predicate the SQL applies."""
    head = history[-1]
    evidencing = [r for r in history if cs.evidences_status(r)]
    last = evidencing[-1] if evidencing else None
    return cs.carry_verdict(
        head,
        last_evidence=({"id": str(last["id"]), "produced_at": last["produced_at"]} if last else None),
        cadence_h=CADENCE_H, now=NOW, windows=3,
    )


def test_a_level_observed_at_level_is_not_carried_after_three_windows() -> None:
    change = _entry(id="pipeline-disruption")
    # A triggered read 4 windows ago, then one read a window for 4 windows.
    start = [_read(4 * CADENCE_H + 1, [_entry(id="pipeline-disruption", status="triggered",
                                              citations=[1])])]
    level_history = start + [
        _read(age, [change, _level_obs([2])]) for age in (3 * CADENCE_H, 2 * CADENCE_H, CADENCE_H, 1.0)
    ]
    change_history = start + [
        _read(age, [change, _entry(id="energy-supply-stress-in-force")])
        for age in (3 * CADENCE_H, 2 * CADENCE_H, CADENCE_H, 1.0)
    ]
    assert _verdict_at_head(level_history) is None
    v = _verdict_at_head(change_history)
    assert v is not None and v["kind"] == cs.CARRIED_UNSUPPORTED
    assert v["last_evidence_age_h"] == pytest.approx(4 * CADENCE_H + 1)
    assert "observed at its level" in cs.verdict_sentence(v)


@pytest.mark.parametrize(
    "indicator",
    [
        _level_obs([]),                                                 # no citation
        {k: v for k, v in _level_obs([2]).items() if k != "shape"},     # not level-shaped
    ],
)
def test_what_a_level_observation_is_not(indicator: dict[str, Any]) -> None:
    head = _read(1.0, [indicator])
    assert not cs.evidences_status(head)
    assert cs.carry_verdict(head, last_evidence=None, cadence_h=CADENCE_H, now=NOW) is not None


# ---------------------------------------------------------------------------
# 4. THE REAL BINDING PATH — run_method with the option, the merge
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    prompt_tokens: int = 100
    completion_tokens: int = 50
    reasoning_tokens: int = 0


@dataclass
class _Response:
    content: str
    usage: _Usage


class _CannedLLM:
    subprovider = "level_indicator_test_double"

    def __init__(self, content: str) -> None:
        self.content = content

    async def chat_complete(self, messages: Any, **kw: Any) -> _Response:
        return _Response(self.content, _Usage())


def _model_output() -> str:
    level = {"horizon_date": "2026-10-20", "first_seen": "2026-10-13"}
    return json.dumps({
        "title": "UK diesel squeeze holds under the Russian product ban",
        "body": "*As of 13 October 2026.*\n\n**BLUF:** The ban still binds [1].\n\n"
                "## What changed\n\nDiesel prices held at a two-year high [2].",
        "confidence": 0.6, "evidence": ["1", "2"],
        "tags": ["severity:high", "severity_delta:steady", "energy_security"],
        "indicators": [
            {"id": "pipeline-disruption", "statement": "Pipeline cut", "status": "not_observed",
             "citations": [], **level},
            # The model says "triggered" on a level id: the handler reads it as a level.
            {"id": "energy-trade-restriction-in-force", "statement": "Ban in force",
             "status": "triggered", "citations": [1], **level},
            # Cited only to a slice position that does not exist: set back.
            {"id": "energy-supply-stress-in-force", "statement": "Prices at a high",
             "status": "observed_at_level", "citations": [99], **level},
        ],
    })


@pytest.mark.asyncio
async def test_run_method_shapes_keeps_and_sets_back_on_the_real_path() -> None:
    from legba.data.analysts.inline_target import InlineTargetDeps, run_method

    signals = [
        {"id": uuid4(), "title": "UK keeps Russian refined-product ban", "produced_at": "2026-10-13T08:00:00+00:00",
         "source_url": "https://example.com/a", "data": {"summary": "The ban remains in force."}},
        {"id": uuid4(), "title": "Diesel at a two-year high", "produced_at": "2026-10-12T08:00:00+00:00",
         "source_url": "https://example.com/b", "data": {"summary": "Pump prices held."}},
    ]
    options = {
        "target_id": "country_g20_gb", "analyst_id": "energy_security",
        LEVEL_INDICATORS_OPTION: _body("energy_security")["method"]["options"][LEVEL_INDICATORS_OPTION],
    }
    result = await run_method(signals, options, InlineTargetDeps(llm=_CannedLLM(_model_output())))
    data = result.finding.data
    by_id = {e["id"]: e for e in data["indicators"]}
    kept = by_id["energy-trade-restriction-in-force"]
    assert kept["status"] == "observed_at_level" and kept["shape"] == "level"
    assert kept["reference"].startswith("bar:a signal in this window reports the measure")
    assert by_id["energy-supply-stress-in-force"]["status"] == "not_observed"
    assert data[shape.DEMOTIONS_DATA_KEY] == ["energy-supply-stress-in-force"]
    assert "shape" not in by_id["pipeline-disruption"]
    [step] = [s for s in result.intermediate_steps if s.get("kind") == "coerce_finding"]
    assert step["level_observations"] == 1 and step["level_demoted"] == ["energy-supply-stress-in-force"]
    # The persisted row this finding becomes re-evidences its status.
    assert cs.evidences_status({"data": {"data": data}})


@pytest.mark.asyncio
async def test_run_method_without_the_option_is_unchanged() -> None:
    """No declaration: a model-written level status cannot sneak through."""
    from legba.data.analysts.inline_target import InlineTargetDeps, run_method

    signals = [{"id": uuid4(), "title": "t", "produced_at": "2026-10-13T08:00:00+00:00",
                "source_url": "https://example.com/a", "data": {"summary": "s"}}]
    result = await run_method(
        signals, {"target_id": "country_g20_gb", "analyst_id": "energy_security"},
        InlineTargetDeps(llm=_CannedLLM(_model_output())),
    )
    ids = [e["id"] for e in result.finding.data["indicators"]]
    # The triggered level id stays a plain triggered change indicator; the
    # model's own observed_at_level (no declaration) drops.
    assert ids == ["pipeline-disruption", "energy-trade-restriction-in-force"]
    assert result.finding.data["indicators"][1]["status"] == "triggered"
    assert all("shape" not in e for e in result.finding.data["indicators"])
    assert shape.DEMOTIONS_DATA_KEY not in result.finding.data


def test_the_runtime_merge_hands_the_option_to_run_method() -> None:
    from legba.runtime.dapr_actors import _merge_descriptor_options

    body = _body("military_posture")
    body.setdefault("identity", {})["version"] = "0" * 16
    descriptor = AnalystDescriptor.model_validate(body, strict=False)
    options: dict[str, Any] = {}
    receipt = _merge_descriptor_options(options, descriptor, actor_id="test")
    assert receipt is not None and not receipt.get("rejected")
    assert set(shape.level_specs(options)) == CONVERTED["military_posture"]


# ---------------------------------------------------------------------------
# 5. Verify and the I&W tracker
# ---------------------------------------------------------------------------


def test_verify_holds_a_level_observation_to_a_citation() -> None:
    from legba.data.provenance import verify

    lvl = {"shape": "level", "reference": "bar:a report", "status": "observed_at_level"}
    checkable, supported, spans = verify._indicator_spans([
        _entry(id="cited", citations=[3], **lvl),
        _entry(id="uncited", statement="Uncited level", citations=[], **lvl),
        _entry(id="quiet"),
    ])
    assert (checkable, supported) == (2, 1)
    assert spans[0].reason == "indicator_uncited_triggered"
    assert spans[0].text.startswith("level indicator observed at level without citation")
    labels = [cv.text for cv in verify._indicator_claim_verdicts([_entry(id="cited", citations=[3], **lvl)])]
    assert labels and labels[0].startswith("level indicator observed at level: ")


def test_the_tracker_never_reads_a_level_observation_as_a_firing() -> None:
    from legba.data.analysts.deterministic_handlers import indicator_tracker as it

    def run(age_h: float, status: str) -> dict[str, Any]:
        ind = _entry(id="energy-supply-stress-in-force", status=status)
        if status == "observed_at_level":
            ind.update(shape="level", reference="bar:a report", citations=[1])
        return {"id": uuid4(), "target_id": "country_g20_gb", "analyst_id": "energy_security",
                "produced_at": NOW - timedelta(hours=age_h), "indicators": [ind]}

    rows = [run(55, "not_observed"), run(44, "observed_at_level"), run(33, "observed_at_level"),
            run(22, "observed_at_level"), run(11, "not_observed")]
    sweep = it.collect_flips(rows, hysteresis_runs=2)
    statuses = {f["to_status"] for f in sweep.raw} | {f["from_status"] for f in sweep.raw}
    assert "observed_at_level" not in statuses
    assert not [f for f in sweep.debounced if f.get("activation")]


# ---------------------------------------------------------------------------
# 6. The predicate on the REAL driver
# ---------------------------------------------------------------------------

_UNIT_LEVEL = "x4_level_fixture_unit"
_UNIT_CHANGE = "x4_change_fixture_unit"
_TARGET = "country_x4_level_fixture"
_OWNER = "test_x4_level"


async def _seed(conn, unit: str, *, age_h: float, indicators: list[dict[str, Any]], now: datetime) -> str:
    rid = uuid4()
    await conn.execute(
        "INSERT INTO public.analyst_outputs "
        "  (id, kind, title, body, confidence, severity, data, target_id, analyst_id, "
        "   produced_at, schema_uri) "
        "VALUES ($1, 'finding', 'x4 fixture', 'x4', 0.6, 'high', $2::jsonb, $3, $4, $5, "
        "        'iglu:legba/finding/jsonschema/1-0-0')",
        rid,
        json.dumps({"tags": ["severity:high"], "data": {"indicators": indicators}}),
        _TARGET, unit, now - timedelta(hours=age_h),
    )
    return str(rid)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_shipped_sql_reads_a_level_observation_as_re_evidence(migrated_pg) -> None:
    from legba.data.postgres import PostgresStore

    store = PostgresStore(migrated_pg)
    await store.connect()
    now = datetime.now(timezone.utc)
    lvl = {"shape": "level", "reference": "bar:a report"}
    try:
        async with store.acquire() as conn:
            await conn.execute("DELETE FROM public.analyst_outputs WHERE target_id = $1", _TARGET)
            await conn.execute("DELETE FROM public.analyst_descriptors WHERE owner = $1", _OWNER)
            for unit in (_UNIT_LEVEL, _UNIT_CHANGE):
                await conn.execute(
                    "INSERT INTO public.analyst_descriptors "
                    "  (descriptor_id, version, schema_uri, is_head, kind, state, owner, name, body) "
                    "VALUES ($1, 'x4v1', 'legba/analyst/2.0.0', TRUE, 'inline_target', "
                    "        'active', $2, $1, $3::jsonb)",
                    unit, _OWNER, json.dumps({"cadence": {"cooldown_seconds": 39600}}),
                )
                await _seed(conn, unit, age_h=336, indicators=[{"id": "c", "status": "triggered"}], now=now)
            for age in (33, 22, 11, 1):
                # The level unit observes its level, cited, every window.
                await _seed(conn, _UNIT_LEVEL, age_h=age, now=now, indicators=[
                    {"id": "c", "status": "not_observed"},
                    {"id": "l", "status": "observed_at_level", "citations": [1], **lvl},
                ])
                # The change unit has nothing but not_observed — and two near
                # misses the predicate must refuse: an uncited level
                # observation and an unshaped one.
                await _seed(conn, _UNIT_CHANGE, age_h=age, now=now, indicators=[
                    {"id": "c", "status": "not_observed"},
                    {"id": "l", "status": "observed_at_level", "citations": [], **lvl},
                    {"id": "m", "status": "observed_at_level", "citations": [1]},
                ])

            verdicts = await cs.unit_carried_verdicts(
                conn, target=_TARGET, units=[_UNIT_LEVEL, _UNIT_CHANGE], now=now,
            )
            assert [v["unit"] for v in verdicts] == [_UNIT_CHANGE]
            assert verdicts[0]["last_evidence_age_h"] == pytest.approx(336.0, abs=0.1)

            # The last-evidence read: the level unit's newest re-evidencing read
            # is its 1 h head; the change unit's is still the 336 h trigger.
            ev = await cs.read_last_evidence(
                conn, [(_UNIT_LEVEL, _TARGET), (_UNIT_CHANGE, _TARGET)], now=now,
            )
            age = {k[0]: (now - v["produced_at"]).total_seconds() / 3600 for k, v in ev.items()}
            assert age[_UNIT_LEVEL] == pytest.approx(1.0, abs=0.1)
            assert age[_UNIT_CHANGE] == pytest.approx(336.0, abs=0.1)

            # The SQL predicate and its Python twin agree, row by row.
            rows = await conn.fetch(
                "SELECT data, jsonb_path_exists(data, $2) AS ev FROM public.analyst_outputs "
                "WHERE target_id = $1",
                _TARGET, shape.RE_EVIDENCE_JSONPATH,
            )
            assert len(rows) == 10
            for r in rows:
                data = r["data"] if isinstance(r["data"], Mapping) else json.loads(r["data"])
                assert bool(r["ev"]) == cs.evidences_status({"data": data})
    finally:
        async with store.acquire() as conn:
            await conn.execute("DELETE FROM public.analyst_outputs WHERE target_id = $1", _TARGET)
            await conn.execute("DELETE FROM public.analyst_descriptors WHERE owner = $1", _OWNER)
        await store.close()
