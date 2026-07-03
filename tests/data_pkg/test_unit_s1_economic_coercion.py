# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S1-T7 — the economic_coercion bounded UNIT (7th PMESII dimension) + wiring.

economic_coercion assesses SANCTIONS / TRADE / CURRENCY coercion for a country —
sanctions designations & regimes, punitive trade restrictions & tariffs,
currency/reserve pressure, capital controls, and commodity leverage used
coercively — while separating COERCIVE economic pressure from ordinary macro
conditions. It is a bounded inline_target UNIT (the validated T1 unit-factory: NO
new Python kind). This asserts:

  1. the descriptor validates against the REAL AnalystDescriptor schema (via the
     exact bringup ``_load`` path) and passes the P2-T7 unit drift guard;
  2. it is wired into scripts/bringup ANALYST_FILES so bringup registers it;
  3. the bounded-question contract: exactly-one-severity guidance + [N] citation
     instruction + the topic tag + grounding block + a FRESH staggered cadence
     (disjoint from the taken slots + the compose slot) with cooldown BELOW the
     fire interval;
  4. the HIDDEN wirings without which a 7th dimension is INVISIBLE in the product —
     scorecard_banding.DIMENSIONS (now 7), the two sync-linked producer lists,
     the collection-gap source-class doctrine map, and
     country_composition.other_analysts;
  5. a FIXTURE run through the inline_target kind emits a cited finding carrying a
     severity tag AND the structured I&W indicators block.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
from uuid import UUID, uuid4

import pytest
import yaml

from legba.data.schemas.analyst import AnalystDescriptor
from legba.data.analysts.deterministic_handlers import scorecard_banding as sb
from legba.data.analysts.deterministic_handlers import unit_correctness_scorer as ucs
from legba.data.analysts.deterministic_handlers import collection_gap as cg
from legba.data.analysts.inline_target import InlineTargetDeps, run_method
from legba.runtime.substrate_query_port import _ASSESSMENT_PRODUCER_ANALYSTS

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DESCRIPTORS_DIR = _ROOT / "descriptors"

_UNIT_FILE = "analyst_economic_coercion.yaml"
_UNIT_ID = "economic_coercion"

# Hour slots TAKEN by the six existing units + the composition compose.
_TAKEN_UNIT_HOURS = {1, 13, 2, 14, 4, 16, 5, 17, 7, 19, 10, 22}
_COMPOSE_HOURS = {11, 23}
_FIRE_INTERVAL_S = 12 * 3600


def _load(name: str) -> AnalystDescriptor:
    """Exact mirror of scripts/bringup_register_analysts._load."""
    body = yaml.safe_load((_DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return AnalystDescriptor.model_validate(body, strict=False)


def _raw_body(name: str) -> dict:
    return yaml.safe_load((_DESCRIPTORS_DIR / name).read_text())


def _bringup_module():
    spec = importlib.util.spec_from_file_location(
        "_bringup_register_analysts",
        _ROOT / "scripts" / "bringup_register_analysts.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cron_hours(schedule: str) -> set[int]:
    fields = schedule.split()
    assert len(fields) == 5, schedule
    return {int(h) for h in fields[1].split(",")}


# ---------------------------------------------------------------------------
# 1. Descriptor validates + passes the bringup drift guard
# ---------------------------------------------------------------------------


def test_descriptor_validates():
    desc = _load(_UNIT_FILE)
    assert desc.identity.id == _UNIT_ID
    assert desc.identity.kind == "inline_target"
    assert desc.identity.state == "active"


def test_passes_drift_guard():
    """inline_target + inline system_prompt ⇒ a bounded UNIT that MUST carry both
    eval.rubric and method.llm.verify. The exact live bringup guard accepts it."""
    mod = _bringup_module()
    body = mod._load_body(_UNIT_FILE)
    assert mod._is_bounded_unit(body) is True
    has_rubric, has_verify = mod._unit_eval_coverage(body)
    assert has_rubric and has_verify
    mod._assert_unit_eval_coverage([(_UNIT_FILE, body)])  # no raise == clean


# ---------------------------------------------------------------------------
# 2. Wired into bringup ANALYST_FILES
# ---------------------------------------------------------------------------


def test_in_bringup_set():
    mod = _bringup_module()
    assert _UNIT_FILE in mod.ANALYST_FILES


# ---------------------------------------------------------------------------
# 3. Bounded-question contract + grounding + cadence
# ---------------------------------------------------------------------------


def test_prompt_contract_severity_and_citation():
    prompt = _raw_body(_UNIT_FILE)["method"]["system_prompt"]
    assert "EXACTLY ONE severity" in prompt
    for level in ("severity:low", "severity:moderate", "severity:elevated",
                  "severity:high", "severity:critical"):
        assert level in prompt
    assert f"topic:{_UNIT_ID}" in prompt
    assert "[N]" in prompt
    assert "STRICT JSON" in prompt
    assert "EXACTLY ONE FINDING" in prompt
    # The bounded question DISTINGUISHES coercion from ordinary macro conditions.
    assert "ordinary macro" in prompt.lower()
    # And draws the boundary against the energy_security unit's energy-supply lane.
    assert "energy_security" in prompt


def test_grounding_block_on_and_scoped():
    g = _raw_body(_UNIT_FILE)["grounding"]
    assert g["enabled"] is True
    assert g["scope"] == ["target_geo", "slice_entities"]
    assert set(g["sources"]) == {"substrate", "situations", "graph_structure"}


def test_broad_unit_runs_on_blanket_predicate():
    targets = _raw_body(_UNIT_FILE)["subscription"]["targets"]
    assert targets["predicate"] == 'has_tag("g20") or has_tag("watch")'
    assert targets["data_types"] == ["signal"]


def test_verify_declared_on_the_stack_ref_core_plane():
    llm = _raw_body(_UNIT_FILE)["method"]["llm"]
    assert llm["verify"]["factory_kind"] == "stack_ref"
    assert llm["primary"]["factory_kind"] == "stack_ref"


def test_cadence_slot_is_fresh_staggered_and_disjoint():
    """The unit takes a FRESH hour slot colliding with neither the six taken unit
    fires nor the compose slot; fires exactly 2×/day; cooldown BELOW 12h."""
    cad = _raw_body(_UNIT_FILE)["cadence"]
    hours = _cron_hours(cad["fallback_schedule"])
    assert len(hours) == 2, "must fire 2x/day"
    assert hours.isdisjoint(_TAKEN_UNIT_HOURS), "collides with a taken unit slot"
    assert hours.isdisjoint(_COMPOSE_HOURS), "collides with the compose slot"
    assert cad["cooldown_seconds"] < _FIRE_INTERVAL_S


# ---------------------------------------------------------------------------
# 4. HIDDEN wiring — the 7th dimension + the sync-linked lists
# ---------------------------------------------------------------------------


def test_scorecard_dimensions_now_has_seven_including_economic_coercion():
    assert _UNIT_ID in sb.DIMENSIONS
    assert len(sb.DIMENSIONS) == 7
    # The original six are still present.
    for prior in ("leadership_transition", "energy_security", "escalation",
                  "narrative_coordination", "internal_stability",
                  "military_posture"):
        assert prior in sb.DIMENSIONS


def test_producer_and_scorer_lists_stay_in_sync():
    assert _UNIT_ID in ucs._DEFAULT_UNITS
    assert _UNIT_ID in _ASSESSMENT_PRODUCER_ANALYSTS
    # The unit set stays identical across the banding + correctness-scorer lists.
    assert set(ucs._DEFAULT_UNITS) == set(sb.DIMENSIONS)
    assert set(sb.DIMENSIONS) <= set(_ASSESSMENT_PRODUCER_ANALYSTS)


def test_collection_gap_doctrine_covers_economic_coercion():
    """Every banded dimension needs a plausible-feed source-class list so a
    starved economic_coercion cell surfaces a non-empty collection recommendation
    — official designations lead."""
    classes = cg.SOURCE_CLASSES_BY_DIMENSION.get(_UNIT_ID)
    assert classes, "economic_coercion missing from the source-class doctrine map"
    assert classes[0] == "official"
    # And the doctrine map covers EVERY dimension (no cell without a feed).
    for d in sb.DIMENSIONS:
        assert cg.SOURCE_CLASSES_BY_DIMENSION.get(d), d


def test_country_composition_fuses_the_seventh_unit():
    desc = _load("analyst_country_composition.yaml")
    ids = [a.id for a in desc.subscription.other_analysts]
    assert _UNIT_ID in ids
    assert len(ids) == 7


def test_banding_reports_the_new_dimension_end_to_end():
    """band_target reports EVERY dimension always — economic_coercion reads honest
    insufficient (no-finding) when nothing fired, with an empty explicit basis."""
    verdict = sb.band_target("target:usa", {})
    dim = verdict["dimensions"][_UNIT_ID]
    assert dim["band"] == sb.INSUFFICIENT
    assert dim["basis"] == []
    assert dim["reason"] == "no-finding"


def test_a_banded_economic_coercion_dimension_names_its_basis():
    fid = str(uuid4())
    claim = sb.Claim(finding_id=fid, analyst_id=_UNIT_ID,
                     confidence=0.9, faithfulness_score=0.9,
                     tags=("severity:high",))
    verdict = sb.band_target("target:usa", {_UNIT_ID: claim})
    dim = verdict["dimensions"][_UNIT_ID]
    assert dim["band"] == "high"
    assert dim["basis"] == [fid]


# ---------------------------------------------------------------------------
# 5. A fixture run emits a cited finding with a severity tag + indicators
# ---------------------------------------------------------------------------


class _FixtureLLM:
    """Typed LLM double returning a canned economic-coercion finding JSON."""

    subprovider = "openai"

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict] = []

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None,
                            system=None, **kwargs):
        self.calls.append({"messages": messages, "system": system})

        class _Usage:
            prompt_tokens = 100
            completion_tokens = 50
            reasoning_tokens = 0

        class _Response:
            pass

        resp = _Response()
        resp.content = self._content
        resp.usage = _Usage()
        return resp


def _signal_row(*, id_: UUID, title: str, produced_at: str) -> dict:
    return {
        "id": id_,
        "title": title,
        "produced_at": produced_at,
        "source_url": "https://example.com/econ",
        "data": {"summary": f"{title} — detail."},
    }


@pytest.mark.asyncio
async def test_fixture_run_emits_cited_finding_with_severity_and_indicators():
    """ACCEPTANCE: over a FIXTURE synthesized response whose prose cites [1]/[2]
    against a KNOWN signal ordering, run_method persists data['citations'] (real
    fixture ids), carries the topic + severity tags, and lands the structured
    data['indicators'] block."""
    # ORIENT sorts produced_at DESC → [1] = newest = sig_ids[0].
    sig_ids = [uuid4() for _ in range(2)]
    inputs = [
        _signal_row(id_=sig_ids[0], produced_at="2026-06-30T12:00:00+00:00",
                    title="Treasury adds new sanctions designations"),
        _signal_row(id_=sig_ids[1], produced_at="2026-06-29T09:00:00+00:00",
                    title="Central-bank reserves fall sharply"),
    ]
    body = (
        "**BLUF:** Intensifying sanctions coercion; the country is the target.\n"
        "## Key points\n"
        "- Treasury imposed fresh designations on state banks [1].\n"
        "- FX reserves fell double-digits amid the defense [2].\n"
        "## Assessment\n"
        "Confirmed designations outweigh threats; trajectory intensifying.\n"
        "## Indicators to watch\n"
        "- Any secondary-sanctions extension to third-country banks.\n"
    )
    fixture = {
        "title": "Country X: intensifying sanctions coercion (target)",
        "body": body,
        "confidence": 0.72,
        "evidence": ["1", "2"],
        "tags": ["topic:economic_coercion", "severity:high"],
        "indicators": [
            {"id": "new-sanctions-designation",
             "statement": "New or tightened sanctions designation naming the country",
             "status": "triggered", "horizon_date": "2026-08-15",
             "first_seen": "2026-07-02", "citations": [1]},
            {"id": "reserve-or-currency-shock",
             "statement": "Sharp FX-reserve drawdown or currency crisis",
             "status": "triggered", "horizon_date": "2026-07-20",
             "first_seen": "2026-07-02", "citations": [2]},
            {"id": "punitive-trade-measure",
             "statement": "Imposed export control or punitive tariff used as leverage",
             "status": "not_observed", "horizon_date": "2026-07-30",
             "first_seen": "2026-07-02", "citations": []},
        ],
    }
    llm = _FixtureLLM(json.dumps(fixture))
    result = await run_method(
        inputs,
        {"target_id": "country_g20_xx", "analyst_id": _UNIT_ID},
        InlineTargetDeps(llm=llm),
    )

    # Cited finding — real fixture ids, correct [N] → signal ordering.
    citations = result.finding.data.get("citations")
    assert isinstance(citations, list) and citations
    fixture_ids = {str(i) for i in sig_ids}
    for c in citations:
        assert c["signal_id"] in fixture_ids
    by_marker = {c["marker"]: c["signal_id"] for c in citations}
    assert by_marker["[1]"] == str(sig_ids[0])
    assert by_marker["[2]"] == str(sig_ids[1])

    # Severity + topic tags carried through onto the finding.
    assert "severity:high" in result.finding.tags
    assert "topic:economic_coercion" in result.finding.tags

    # Structured I&W indicators landed in data['indicators'].
    indicators = result.finding.data.get("indicators")
    assert isinstance(indicators, list) and len(indicators) == 3
    ind_ids = {i["id"] for i in indicators}
    assert "new-sanctions-designation" in ind_ids
    triggered = [i for i in indicators if i["status"] == "triggered"]
    assert triggered and all(i["citations"] for i in triggered)

    # Lineage: both signals carried through derived_from.
    assert set(result.derived_from) == set(sig_ids)
