# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Supply-chain pack — the 10 lane/flow desks + the disruption_status unit.

planning/SUPPLY_CHAIN_PACK_PLAN_2026-07-29.md. The pack is the platform's SECOND
exemplar domain and its whole point is that it adds no infrastructure: 10 target
descriptors, 1 inline_target unit descriptor, and exactly ONE line of Python (the
desk-roster tag literal). This suite locks the properties that make that true —
each one is a plan-level decision that a later edit could quietly undo.

  1. TARGETS — thematic domain with a real SQL-pushdown discriminator (scope.geo,
     or an explicit source_id pin for the one honestly non-geo desk), the
     contains_any predicate only ever a REFINER, NO ``US`` in any geo set, NO
     g20/watch tag, NO inert inline ``analyst:`` block, and all 10 shipping
     ``state: draft``.
  2. TIER B — each of the 4 declared-but-draft desks carries its concrete
     activation gate in its header.
  3. THE UNIT — narrow ``has_tag("supply_chain")`` fan-out, the MEASURED 24h
     window (not 72h — the 360-row pre-filter), the free 06/18 cadence slot,
     RAG-off grounding, the default verify profile (rubric + verify judge), no
     action-pack grant, and an indicators array the live indicator_tracker can
     actually consume.
  4. NARROW-UNIT DISCIPLINE — deliberately OFF the fixed scorecard DIMENSIONS
     tuple (a fixed dimension would render insufficient-evidence on all 32
     country desks where the question does not apply).
  5. THE ONE CODE TOUCH — ``supply_chain`` joins the desk-coverage roster so a
     supply-chain desk with no head is NAMED as a gap instead of silently
     missing, and the three sibling roster SQLs are deliberately NOT widened.
"""

from __future__ import annotations

import json
import pathlib
import re
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml

from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.analysts.deterministic_handlers import alert_trigger_scan as ats
from legba.data.analysts.deterministic_handlers import desk_baseline as db
from legba.data.analysts.deterministic_handlers import indicator_tracker as it
from legba.data.analysts.deterministic_handlers import scorecard_banding as sb
from legba.data.analysts.deterministic_handlers import scorecard_producer as sp
from legba.data.analysts.deterministic_handlers import unit_correctness_scorer as ucs
from legba.data.schemas.analyst import AnalystDescriptor
from legba.data.schemas.target import TargetDescriptor

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DESCRIPTORS_DIR = _ROOT / "descriptors"

_UNIT_FILE = "analyst_disruption_status.yaml"
_UNIT_ID = "disruption_status"
_FAN_OUT_TAG = "supply_chain"

# Tier A — activated at launch after the preflight (plan §1.1).
_TIER_A: list[tuple[str, str]] = [
    ("target_lane_hormuz.yaml", "lane_hormuz"),
    ("target_lane_red_sea.yaml", "lane_red_sea"),
    ("target_lane_malacca_south_china_sea.yaml", "lane_malacca_south_china_sea"),
    ("target_lane_black_sea.yaml", "lane_black_sea"),
    ("target_flow_semiconductors.yaml", "flow_semiconductors"),
    ("target_flow_energy_shipping.yaml", "flow_energy_shipping"),
]
# Tier B — declared, draft, each gated on a source slot (plan §1.2).
_TIER_B: list[tuple[str, str]] = [
    ("target_lane_panama.yaml", "lane_panama"),
    ("target_flow_critical_minerals.yaml", "flow_critical_minerals"),
    ("target_flow_container_freight.yaml", "flow_container_freight"),
    ("target_lane_baltic_north_sea.yaml", "lane_baltic_north_sea"),
]
_ALL_DESKS = _TIER_A + _TIER_B

# The ONE desk that is honestly non-geo: its pushdown is the explicit source pin
# (plan §5.3 — the slice reader resolves source_id, never a selector).
_SOURCE_PINNED = "flow_container_freight"

# The slice reader's SQL pre-filter: max(200, LEGBA_SLICE_ROW_CAP * 3).
_FETCH_LIMIT = 360
# The interval between the two staggered daily fires.
_FIRE_INTERVAL_S = 12 * 3600
# Hour slots TAKEN by the live 2x/day units + the compose/world runs. 06/18 is
# the only free pair (plan §2.1, measured against the live cadence table).
_TAKEN_HOURS = {
    1, 13,    # leadership_transition
    2, 14,    # internal_stability
    3, 15,    # proliferation_watch (+ corpus_researcher :37)
    4, 16,    # energy_security
    5, 17,    # military_posture (+ cross_doc_corroborator :17)
    7, 19,    # escalation
    8, 20,    # escalation_composition :30 / escalation_dyad :45
    9, 21,    # economic_coercion
    10, 22,   # narrative_coordination
    11, 23,   # country_composition :30 / region_composition :45
    0, 12,    # world_assessor + journal_assessor
}


def _raw(name: str) -> dict:
    return yaml.safe_load((_DESCRIPTORS_DIR / name).read_text())


def _text(name: str) -> str:
    return (_DESCRIPTORS_DIR / name).read_text()


def _target(name: str) -> TargetDescriptor:
    """Mirror scripts/bringup_register_supply_chain_pack._load_target (which also
    COMPILES scope.predicate via the schema validator)."""
    body = _raw(name)
    body.setdefault("identity", {})["version"] = "0" * 16
    return TargetDescriptor.model_validate(body, strict=False)


def _unit() -> AnalystDescriptor:
    body = _raw(_UNIT_FILE)
    body.setdefault("identity", {})["version"] = "0" * 16
    return AnalystDescriptor.model_validate(body, strict=False)


def _prompt(name: str = _UNIT_FILE) -> str:
    return _raw(name)["method"]["system_prompt"]


def _prompt_flat(name: str = _UNIT_FILE) -> str:
    """The prompt with all whitespace collapsed — the descriptor's system_prompt is
    a literal block scalar, so a hand-wrapped PHRASE carries a newline mid-phrase.
    Flatten before asserting on multi-word phrases, never on single tokens."""
    return re.sub(r"\s+", " ", _prompt(name))


def _cron_hours(schedule: str) -> set[int]:
    fields = schedule.split()
    assert len(fields) == 5, schedule
    return {int(h) for h in fields[1].split(",")}


# ---------------------------------------------------------------------------
# 1. The 10 desks — schema, state, tags, pushdown
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("fname", "desk_id"), _ALL_DESKS)
def test_desk_validates_thematic_and_draft(fname: str, desk_id: str):
    desc = _target(fname)
    assert desc.identity.id == desk_id
    assert desc.scope.domain == "thematic"
    assert desc.identity.abstraction_level.value == "L1"
    # ALL TEN ship draft: bulk registration must create no live actor and no
    # fan-out. Activation is a deliberate FSM step after the lane preflight.
    assert desc.identity.state.value == "draft", (
        f"{fname} must ship state: draft — activation is the operator's FSM step"
    )
    assert desc.identity.owner == "supply_chain_pack"


@pytest.mark.parametrize(("fname", "desk_id"), _ALL_DESKS)
def test_desk_tags_are_the_fan_out_key_plus_family(fname: str, desk_id: str):
    """Exactly [supply_chain, disruption, <family>] (plan §6.1 step 2), and NEVER
    g20/watch: those two are the subscription key for all 7 broad geopolitics
    units, so tagging a lane `watch` would fan all seven onto non-country desks
    (plan §1.3 — 42-70 spurious runs per cycle)."""
    desc = _target(fname)
    family = "lane" if desk_id.startswith("lane_") else "flow"
    assert desc.scope.tags == [_FAN_OUT_TAG, "disruption", family]
    assert "g20" not in desc.scope.tags
    assert "watch" not in desc.scope.tags


@pytest.mark.parametrize(("fname", "desk_id"), _ALL_DESKS)
def test_desk_carries_a_real_sql_pushdown(fname: str, desk_id: str):
    """The plan's single most important rule (§0.2): the slice reader pre-filters
    the newest 360 rows in SQL and applies scope.predicate only AFTER, so every
    desk MUST carry a SQL-pushdown discriminator — scope.geo and/or an explicit
    source_id pin. contains_any is a refiner, never the sole selector."""
    desc = _target(fname)
    geo = list(getattr(desc.scope, "geo", None) or [])
    pinned = [s.source_id for s in desc.sources if s.source_id]
    assert geo or pinned, f"{desk_id} has NO pushdown — it would read the whole pool"
    if desk_id == _SOURCE_PINNED:
        # The honestly non-geo desk: pinned ids ARE the pushdown (plan §5.3).
        assert not geo
        assert len(pinned) == 3
    else:
        # A geo-anchored desk must NOT pin ids: the reader ANDs `source_id = ANY()`
        # with the geo overlap, which would shrink the lane to the pinned feeds.
        assert geo
        assert not pinned, f"{desk_id} pins source ids, which ANDs away its geo lane"
    assert desc.scope.predicate, f"{desk_id} must carry the refiner predicate"


@pytest.mark.parametrize(("fname", "desk_id"), _ALL_DESKS)
def test_no_us_in_any_desk_geo(fname: str, desk_id: str):
    """plan §0.3, hard NO: source.nws.active_alerts is 25.9% of the 30-day corpus,
    and `_diversify_by_source` sits on the `elif` branch — it does NOT run when a
    scope.predicate is present, so a predicate-bearing desk has ZERO firehose
    protection. A measured PA,CR,CO,US lane was 51% US weather alerts."""
    desc = _target(fname)
    assert "US" not in list(getattr(desc.scope, "geo", None) or [])


@pytest.mark.parametrize(("fname", "desk_id"), _ALL_DESKS)
def test_no_predicate_term_is_split_across_lines(fname: str, desk_id: str):
    """A YAML folded scalar (`>-`) keeps the newline on a MORE-indented
    continuation line. That is harmless between tokens but would silently corrupt
    a term if a quoted string were broken across lines — the term would carry an
    embedded newline + padding and never word-boundary match."""
    pred = _target(fname).scope.predicate or ""
    split = [t for t in re.findall(r'"[^"]*"', pred) if "\n" in t]
    assert not split, f"{desk_id}: predicate term(s) split across lines: {split}"


@pytest.mark.parametrize(("fname", "desk_id"), _ALL_DESKS)
def test_no_inert_inline_analyst_block(fname: str, desk_id: str):
    """plan §0.1: TargetDescriptor.analyst is never read by any runtime code —
    target_situation_iran_war.yaml declares one and has produced ZERO
    analyst_outputs rows. Fan-out comes ONLY from the unit's subscription
    predicate. Declaring one here would be a lie about what runs."""
    assert _target(fname).analyst is None
    assert "analyst:" not in _raw(fname)


@pytest.mark.parametrize(("fname", "desk_id"), _ALL_DESKS)
def test_no_action_pack_grant_on_a_supply_chain_desk(fname: str, desk_id: str):
    """plan §4.4: action_pack_escalate declares applies_to_tags [g20, watch], so a
    grant here would deny with a visible governor BLOCK. An empty grant is the
    honest state until the pack is widened (§6 step 7a, operator-gated)."""
    assert _target(fname).allowed_action_packs == []


def test_the_pack_is_exactly_six_plus_four_desks():
    """No thirteenth desk crept in, and no Tier-A/Tier-B file was renamed out of
    the registrar's lists."""
    on_disk = sorted(
        p.name
        for p in _DESCRIPTORS_DIR.glob("target_*.yaml")
        if _raw(p.name).get("identity", {}).get("owner") == "supply_chain_pack"
    )
    assert on_disk == sorted(f for f, _ in _ALL_DESKS)
    assert len(_TIER_A) == 6 and len(_TIER_B) == 4


# ---------------------------------------------------------------------------
# 2. Tier B — the activation gate is written down
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("fname", "desk_id"), _TIER_B)
def test_tier_b_header_states_its_activation_gate(fname: str, desk_id: str):
    """A Tier-B desk exists to make a collection gap VISIBLE. If its gate is not
    written into the file, the desk is just an undocumented dead descriptor and
    the next reader will be tempted to flip it (plan §6.3 item 8)."""
    header = _text(fname).split("identity:")[0]
    assert "ACTIVATION GATE" in header
    assert "ACTIVATE AT:" in header
    assert re.search(r"§5\.2 slot [A-Z]-\d|slot [A-Z]-\d", header), (
        f"{fname} header must name the §5.2 source slot it waits on"
    )


# ---------------------------------------------------------------------------
# 3. The unit — fan-out, window, cadence, verify, indicators
# ---------------------------------------------------------------------------


def test_unit_validates_inline_target_and_draft():
    desc = _unit()
    assert desc.identity.id == _UNIT_ID
    assert desc.identity.kind == "inline_target"
    # A new INSTANCE of a BUILT-IN kind — so no vocabulary_entries seed is needed.
    assert desc.identity.state.value == "draft"


def test_unit_narrow_supply_chain_fan_out():
    """The pack's ONE new fan-out key. Same one-string tag-scoped narrowing
    proliferation_watch uses with has_tag("nuclear_watch") — NOT the blanket
    g20+watch predicate the seven broad units carry."""
    targets = _raw(_UNIT_FILE)["subscription"]["targets"]
    assert targets["predicate"] == f'has_tag("{_FAN_OUT_TAG}")'
    assert targets["data_types"] == ["signal"]
    assert "g20" not in targets["predicate"]
    assert "watch" not in targets["predicate"]


def test_unit_window_is_24h_not_72h():
    """The MEASURED deviation from the proliferation_watch precedent (plan §0.2):
    at 72h the Hormuz/Black Sea/Malacca/fab-belt lanes each exceed the 360-row SQL
    pre-filter and silently lose about half their predicate hits."""
    assert _raw(_UNIT_FILE)["subscription"]["targets"]["time_window"] == "24h"
    prompt_header = _text(_UNIT_FILE).split("identity:")[0]
    assert str(_FETCH_LIMIT) in prompt_header, (
        "the header must state the 360-row pre-filter that forces the 24h window"
    )


def test_unit_cadence_is_the_free_staggered_slot():
    cad = _raw(_UNIT_FILE)["cadence"]
    hours = _cron_hours(cad["fallback_schedule"])
    assert hours == {6, 18}, "06/18 UTC is the only free 2x/day unit slot"
    assert hours.isdisjoint(_TAKEN_HOURS)
    # A cooldown == the interval lands past the next fire and silently halves each
    # desk's cadence.
    assert cad["cooldown_seconds"] < _FIRE_INTERVAL_S


def test_unit_grounding_on_scoped_and_rag_off():
    g = _raw(_UNIT_FILE)["grounding"]
    assert g["enabled"] is True
    assert g["scope"] == ["target_geo", "slice_entities"]
    assert set(g["sources"]) == {"substrate", "situations", "graph_structure"}
    # RAG stays rolled back platform-wide (#176) — no vector:* source.
    assert not any(str(s).startswith("vector:") for s in g["sources"])


def test_unit_takes_the_default_verify_profile_on_the_core_plane():
    """Both refs are the $0 self-hosted core plane, and `verify`'s PRESENCE is
    what arms the faithfulness judge (the bounded-unit drift guard refuses a unit
    without it). No verify exemption is requested anywhere."""
    llm = _raw(_UNIT_FILE)["method"]["llm"]
    for ref in ("primary", "verify"):
        assert llm[ref]["factory_kind"] == "stack_ref"
        assert llm[ref]["raw"] == "llm.primary.openai_compat"
    assert str(_raw(_UNIT_FILE)["eval"]["rubric"]).strip()
    from legba.data.provenance import kinds as pk

    exempt = getattr(pk, "STRUCTURAL_VERIFY_EXEMPT_ANALYSTS", frozenset())
    assert _UNIT_ID not in exempt


def test_unit_ships_no_action_pack_grant():
    assert _raw(_UNIT_FILE)["action_packs"] == []


def test_unit_prompt_carries_the_six_vectors_and_a_direction():
    prompt = _prompt()
    for vector in (
        "throughput",
        "interdiction & physical risk",
        "rerouting",
        "cost & insurance",
        "access & control",
        "downstream shortage",
    ):
        assert vector in prompt, f"missing output vector: {vector}"
    for direction in ("DEGRADING", "HOLDING", "RECOVERING"):
        assert direction in prompt


def test_unit_prompt_scopes_absence_to_the_collection():
    """verify.py:811-814 classifies an unscoped absence as the soft-fail class
    `unscoped_absence_claim`, and a QUIET lane is this unit's NORMAL state — so
    the collection-scoping paragraph is load-bearing, not boilerplate."""
    flat = _prompt_flat()
    assert "SCOPE ABSENCE TO THE COLLECTION" in flat
    assert "not observed in collected reporting" in flat
    assert "ABSENCE-HONEST" in flat


def test_unit_prompt_guards_quantitative_claims():
    """Rates, premia and transit counts are where this unit is most likely to
    fabricate; an uncited magnitude must become a direction word (plan §2.1)."""
    flat = _prompt_flat()
    assert "QUANTITATIVE CLAIMS" in flat
    assert "replace it with a DIRECTION word" in flat
    assert "MUST appear in a signal you cite" in flat


def test_unit_prompt_draws_the_three_lane_boundaries():
    """plan §2.4 — the unit owns the PHYSICAL FLOW and hands off the three
    adjacent questions by name, so one wire item is not double-counted."""
    prompt = _prompt()
    assert "BOUNDARY" in prompt
    for sibling in ("energy_security", "economic_coercion", "escalation"):
        assert sibling in prompt
    assert "double-count" in prompt


def test_unit_prompt_severity_and_citation_contract():
    prompt = _prompt()
    assert "EXACTLY ONE severity" in prompt
    for level in (
        "severity:low",
        "severity:moderate",
        "severity:elevated",
        "severity:high",
        "severity:critical",
    ):
        assert level in prompt
    assert f"topic:{_UNIT_ID}" in prompt
    assert "[N]" in prompt
    assert "STRICT JSON" in prompt
    assert "EXACTLY ONE FINDING" in prompt


def test_unit_indicator_examples_are_consumable_by_indicator_tracker():
    """The structured indicators are the whole reason indicator_tracker (live,
    deterministic, desk-agnostic) picks this unit up for FREE. Feed the prompt's
    own example objects through the tracker's real cleaner: an entry missing `id`
    or carrying an unknown `status` is DROPPED, so a drifted example silently
    unhooks the tracker."""
    prompt = _prompt()
    entries = [
        json.loads(line.strip().rstrip(","))
        for line in prompt.splitlines()
        if line.strip().startswith('{"id":')
    ]
    assert len(entries) >= 3, "the prompt must show 3-6 example indicators"
    cleaned = it._clean_indicators(entries)
    assert len(cleaned) == len(entries), "an example indicator is not tracker-consumable"
    for e in entries:
        assert e["status"] in it._STATUSES
        assert str(e["id"]).strip() and str(e["statement"]).strip()
        assert "horizon_date" in e and "first_seen" in e and "citations" in e
    # CANONICAL ids: the prompt must tell the model to REUSE them — a re-minted
    # slug is invisible to the tracker's id-join.
    assert "CANONICAL IDS" in prompt and "do NOT re-mint" in prompt


# ---------------------------------------------------------------------------
# 4. NARROW-unit discipline — OFF the fixed scorecard tuple
# ---------------------------------------------------------------------------


def test_unit_kept_off_the_fixed_scorecard_dimensions():
    """plan §2.3: DIMENSIONS is synchronised across FOUR places, and a fixed
    dimension would render a misleading `insufficient-evidence` band on all 32
    country desks where the question does not apply."""
    assert _UNIT_ID not in sb.DIMENSIONS
    assert _UNIT_ID not in ucs._DEFAULT_UNITS
    assert len(sb.DIMENSIONS) == 7
    assert set(ucs._DEFAULT_UNITS) == set(sb.DIMENSIONS)


# ---------------------------------------------------------------------------
# 5. The ONE code touch — desk-coverage roster widening
# ---------------------------------------------------------------------------


def test_desk_roster_sql_includes_supply_chain():
    """plan §3.5 — the pack's only Python edit. Supply-chain desks carry neither
    g20 nor watch, so without this literal a desk that produced no head this
    cycle is SILENTLY MISSING from data.desk_coverage instead of NAMED as a gap.
    Silent coverage is the failure mode this platform exists to refuse."""
    sql = synth._DESK_ROSTER_SQL
    assert "'g20'" in sql and "'watch'" in sql
    assert f"'{_FAN_OUT_TAG}'" in sql


def test_sibling_roster_sqls_are_deliberately_not_widened():
    """plan §3.5 + §4.5: scorecards, baseline_deviation alerts and desk baselines
    stay g20+watch ONLY. Widening them is three coupled edits plus a four-place
    DIMENSIONS sync plus a new banding doctrine, and it lands
    `insufficient-evidence` bands on 32 country desks."""
    for mod, attr in (
        (sp, "_G20_TARGETS_SQL"),
        (ats, "_DESKS_SQL"),
        (db, "_DESKS_SQL"),
    ):
        sql = getattr(mod, attr)
        assert "'g20'" in sql and "'watch'" in sql
        assert f"'{_FAN_OUT_TAG}'" not in sql, (
            f"{mod.__name__}.{attr} was widened — that is out of thin scope"
        )


class _ThematicConn:
    """Fake conn routing READ_SLICE's two queries by SQL content (mirrors
    tests/data_pkg/test_meta_findings_escalation_composition.py)."""

    def __init__(self, *, roster: list[dict[str, Any]], slice_rows: list[dict[str, Any]]):
        self._roster = roster
        self._slice_rows = slice_rows
        self.roster_calls: list[str] = []

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        if "target_descriptors" in query:
            self.roster_calls.append(query)
            return list(self._roster)
        return list(self._slice_rows)


def _head_row(*, uid: UUID, target_id: str, analyst_id: str) -> dict[str, Any]:
    return {
        "id": uid,
        "kind": "finding",
        "title": f"{target_id} read",
        "body": f"{target_id} disruption-status body",
        "confidence": 0.7,
        "effective_confidence": 0.6,
        "faithfulness_score": 0.9,
        "severity": None,
        "data": {"evidence": []},
        "evidence": [],
        "target_id": target_id,
        "target_version": None,
        "analyst_id": analyst_id,
        "analyst_version": "vtest",
        "produced_at": "2026-07-29T00:00:00+00:00",
        "derived_from": [],
        "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
        "run_id": uuid4(),
    }


def _supply_chain_composition_descriptor() -> SimpleNamespace:
    """A thematic composition stub shaped like the plan's §3.1 descriptor: no
    targets block (ONE global run), the disruption_status marker routing to the
    thematic branch, and a verify ref."""
    return SimpleNamespace(
        subscription=SimpleNamespace(
            other_analysts=[
                SimpleNamespace(id=_UNIT_ID, time_window="24h", data_types=[]),
                SimpleNamespace(id="economic_coercion", time_window="24h", data_types=[]),
            ],
            substrate={"direct_queries": False, "thematic_dimension": _UNIT_ID},
            targets=None,
        ),
        method=SimpleNamespace(llm={"verify": {"factory_kind": "stack_ref"}}),
    )


@pytest.mark.asyncio
async def test_supply_chain_desks_appear_in_desk_coverage():
    """THE behavioral assertion for the roster widening: with the supply-chain
    desks on the roster, a desk WITH a disruption_status head reads `present` and
    a desk WITHOUT one is NAMED as a `gap` — the coverage list carries the lane
    ids, so an empty cycle on a lane is visible instead of silent."""
    desc = _supply_chain_composition_descriptor()
    heads = [
        _head_row(uid=uuid4(), target_id="lane_hormuz", analyst_id=_UNIT_ID),
        _head_row(uid=uuid4(), target_id="country_watch_ir", analyst_id="economic_coercion"),
    ]
    conn = _ThematicConn(
        roster=[
            {"descriptor_id": "country_watch_ir", "name": "Iran"},
            {"descriptor_id": "lane_hormuz", "name": "Lane — Strait of Hormuz"},
            {"descriptor_id": "lane_red_sea", "name": "Lane — Red Sea"},
        ],
        slice_rows=heads,
    )
    rows = await synth.READ_SLICE(conn, descriptor=desc, target_filter=None)
    assert rows, "the thematic branch must return the admitted heads"
    coverage = rows[0]["_thematic_coverage"]
    modes = {c["desk_id"]: c["mode"] for c in coverage}
    assert modes["lane_hormuz"] == synth.THEMATIC_MODE_PRESENT
    # The lane with no head this cycle is NAMED, not dropped.
    assert modes["lane_red_sea"] == synth.THEMATIC_MODE_GAP
    # And the cross-domain country desk still rides the same coverage list.
    assert modes["country_watch_ir"] == synth.THEMATIC_MODE_PRESENT
    assert conn.roster_calls, "the thematic branch must resolve the desk roster"


# ---------------------------------------------------------------------------
# 6. The registrar covers every file the pack ships
# ---------------------------------------------------------------------------


def _pack_registrar():
    import importlib.util

    path = _ROOT / "scripts" / "bringup_register_supply_chain_pack.py"
    spec = importlib.util.spec_from_file_location("_bringup_supply_chain_pack", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_registrar_lists_all_eleven_descriptors():
    mod = _pack_registrar()
    assert mod.TIER_A_TARGET_FILES == [f for f, _ in _TIER_A]
    assert mod.TIER_B_TARGET_FILES == [f for f, _ in _TIER_B]
    assert len(mod.TARGET_FILES) == 10
    assert mod.ANALYST_FILES == [_UNIT_FILE]
    for fname in list(mod.TARGET_FILES) + list(mod.ANALYST_FILES):
        assert (_DESCRIPTORS_DIR / fname).is_file(), f"{fname} missing from descriptors/"


def test_registrar_unit_drift_guard_accepts_the_real_unit():
    """The registrar re-asserts the P2-T7 bounded-unit guard on its own path (the
    pack does not ride bringup_register_analysts.ANALYST_FILES), so a unit that
    lost its rubric or verify ref can never register from here."""
    mod = _pack_registrar()
    mod._assert_unit_eval_coverage(_UNIT_FILE)  # must not raise


def test_preflight_script_measures_every_tier_a_lane():
    """The activation gate is only a gate if it measures every launch desk."""
    import importlib.util

    path = _ROOT / "scripts" / "preflight_supply_chain_lanes.py"
    spec = importlib.util.spec_from_file_location("_preflight_supply_chain", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.TIER_A_FILES == [f for f, _ in _TIER_A]
    assert mod.TIER_B_FILES == [f for f, _ in _TIER_B]
    assert mod.DEFAULT_WINDOW_HOURS == 24
    # The cap it reports against must be the slice reader's real over-fetch.
    assert mod._fetch_limit(120) == _FETCH_LIMIT
