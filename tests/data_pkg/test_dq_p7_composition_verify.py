# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DQ Phase 7 — composition-layer verify residual + alert-gate + delivery-honesty
+ forecast-void tests.

The load-bearing surface is verify.py: the deterministic floor must stop crushing
composition critiques on bold headings, split-off citation markers, judgment/
assumption synthesis lines, and forward-looking watch bullets — WITHOUT over-
crediting a genuinely uncited factual claim (the balance the fix must hold). The
remaining tests pin the P7-F4 alert-gate semantics, the P7-F5 delivery-audit
honesty, and the P7-F6 forecast-void guard.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from uuid import uuid4

import pytest

from legba.data.provenance.verify import (
    _correlated_components,
    _deterministic_floor,
    _deterministic_floor_subclaim,
    _is_fact_asserting,
    _is_judgeable_claim,
    _ordinal_source_map,
    _segment_claims,
    verify_finding_faithfulness,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _unit_citation(n: int, sid: str) -> dict:
    return {"marker": f"[{n}]", "signal_id": sid}


def _comp_citation(ordinal: int, *, eff=None, derived=None, source="leadership_transition") -> dict:
    c = {
        "marker": f"[[ref:{ordinal}]]",
        "ordinal": ordinal,
        "ref_id": str(uuid4()),
        "ref_kind": "finding",
        "source": source,
        "title": "sub-claim",
        "evidence_text": "the unit found X",
        "derived_from": [str(x) for x in (derived or [])],
    }
    if eff is not None:
        c["effective_confidence"] = float(eff)
    return c


# ---------------------------------------------------------------------------
# P7-F1(1) — bold heading lines are structure, not facts
# ---------------------------------------------------------------------------


def test_bold_only_heading_not_a_fact_for_floor_or_judge():
    for line in ("**Key points**", "- **Drivers**", "**Indicators to watch:**"):
        assert _is_fact_asserting(line) is False, line
        assert _is_judgeable_claim(line) is False, line


def test_bold_heading_body_floors_only_the_real_uncited_claim():
    # ``**Key points**`` (heading) + one cited bullet + one uncited fact. Only the
    # uncited fact should be an unsupported claim; the heading is not counted.
    body = (
        "**Key points**\n"
        "- Ankara deployed 56,000 police ahead of the summit [[ref:1]].\n"
        "- The junta seized the national bank overnight.\n"
    )
    citations = [_comp_citation(1, eff=0.6, derived=["s1"])]
    rep = _deterministic_floor_subclaim(body, citations)
    # 2 checkable claims (the heading dropped), 1 supported, 1 uncited.
    assert rep.checkable_claims == 2
    assert rep.supported_claims == 1
    assert [s.reason for s in rep.unsupported_spans if s.reason == "no_citation"] == [
        "no_citation"
    ]


# ---------------------------------------------------------------------------
# P7-F1(2) — a citation marker trailing a sentence period is not orphaned
# ---------------------------------------------------------------------------


def test_marker_after_period_is_merged_not_no_citation_composition():
    body = (
        "Rebels seized the airfield. [[ref:1]]\n"
        "The garrison retreated overnight. [[ref:2]]\n"
    )
    citations = [
        _comp_citation(1, eff=0.6, derived=["s1"]),
        _comp_citation(2, eff=0.6, derived=["s2"]),
    ]
    rep = _deterministic_floor_subclaim(body, citations)
    assert rep.faithfulness_score == 1.0
    assert not [s for s in rep.unsupported_spans if s.reason == "no_citation"]


def test_marker_after_period_is_merged_not_no_citation_unit():
    sid1, sid2 = str(uuid4()), str(uuid4())
    body = "The strike hit the depot. [1]\nA second blast followed. [2]\n"
    citations = [_unit_citation(1, sid1), _unit_citation(2, sid2)]
    rep = _deterministic_floor(body, citations)
    assert rep.faithfulness_score == 1.0
    assert not [s for s in rep.unsupported_spans if s.reason == "no_citation"]


def test_segment_pulls_trailing_marker_inside_sentence():
    spans = _segment_claims("Rebels seized the airfield. [[ref:1]] Next thing happened.")
    # The marker must ride with the first clause, not stand alone / lead the next.
    assert any("[[ref:1]]" in s and "airfield" in s for s in spans)


# ---------------------------------------------------------------------------
# P7-F1(3) — judgment/assumption synthesis lines are floor-exempt, judge-graded
# ---------------------------------------------------------------------------


def test_judgment_assumption_floor_exempt_but_judge_grades():
    for line in ("JUDGMENT: the regime remains stable", "Assumption: no external intervention"):
        assert _is_fact_asserting(line) is False, line
        assert _is_judgeable_claim(line) is True, line


# ---------------------------------------------------------------------------
# P7-F1(5) — forward-looking watch bullets exempt for BOTH floor and judge
# ---------------------------------------------------------------------------


def test_forward_looking_watch_bullet_exempt_both():
    line = "Official announcements of fuel rationing would confirm a supply crisis"
    assert _is_fact_asserting(line) is False
    assert _is_judgeable_claim(line) is False


def test_present_absence_still_judged_h1_preserved():
    # A present-tense absence read is NOT forward-looking — the judge must still
    # grade it (H1: the judge, not the floor, catches a fabricated absence).
    line = "No evidence of troop movements was found in the signal window"
    assert _is_fact_asserting(line) is False          # floor exempts absence
    assert _is_judgeable_claim(line) is True           # judge still grades it


# ---------------------------------------------------------------------------
# BALANCE — a genuinely uncited factual claim is STILL caught (no over-credit)
# ---------------------------------------------------------------------------


def test_uncited_factual_claim_still_caught_composition():
    body = "The president was assassinated in the capital yesterday.\n"
    rep = _deterministic_floor_subclaim(body, [_comp_citation(1, eff=0.6, derived=["s1"])])
    assert rep.faithfulness_score == 0.0
    assert any(s.reason == "no_citation" for s in rep.unsupported_spans)


def test_uncited_factual_claim_still_caught_unit():
    body = "A coup deposed the government at dawn.\n"
    rep = _deterministic_floor(body, [_unit_citation(1, str(uuid4()))])
    assert rep.faithfulness_score == 0.0
    assert any(s.reason == "no_citation" for s in rep.unsupported_spans)


# ---------------------------------------------------------------------------
# P7-F1(6) — shared-lineage double-count keyed on SAME source
# ---------------------------------------------------------------------------


def test_double_count_same_source_shared_lineage_flagged():
    body = "Leadership is contested [[ref:1]].\nThe transition is unstable [[ref:2]].\n"
    citations = [
        _comp_citation(1, eff=0.5, derived=["shared", "a"], source="escalation"),
        _comp_citation(2, eff=0.5, derived=["shared", "b"], source="escalation"),
    ]
    rep = _deterministic_floor_subclaim(body, citations)
    assert any(s.reason == "double_counted" for s in rep.unsupported_spans)


def test_different_source_shared_lineage_not_double_counted():
    # The country_composition case: 7 units read the SAME desk slice (shared
    # lineage) but answer DIFFERENT bounded questions — NOT double-counting.
    body = "Leadership is contested [[ref:1]].\nEnergy supply is strained [[ref:2]].\n"
    citations = [
        _comp_citation(1, eff=0.7, derived=["shared", "a"], source="leadership_transition"),
        _comp_citation(2, eff=0.5, derived=["shared", "b"], source="energy_security"),
    ]
    rep = _deterministic_floor_subclaim(body, citations)
    assert not any(s.reason == "double_counted" for s in rep.unsupported_spans)
    # Ceiling is the strongest independent unit, uncrushed.
    assert rep.confidence_ceiling == pytest.approx(0.7)


def test_correlated_components_source_discriminated():
    # shared lineage, different sources -> two components
    comps = _correlated_components([1, 2], {1: {"x"}, 2: {"x"}}, {1: "a", 2: "b"})
    assert sorted(len(c) for c in comps) == [1, 1]
    # shared lineage, same source -> one component
    comps = _correlated_components([1, 2], {1: {"x"}, 2: {"x"}}, {1: "a", 2: "a"})
    assert sorted(len(c) for c in comps) == [2]
    # no group map -> lineage-only (legacy) -> one component
    comps = _correlated_components([1, 2], {1: {"x"}, 2: {"x"}})
    assert sorted(len(c) for c in comps) == [2]


def test_ordinal_source_map_reads_source():
    cits = [_comp_citation(1, source="escalation"), _comp_citation(2, source="military_posture")]
    assert _ordinal_source_map(cits) == {1: "escalation", 2: "military_posture"}


# ---------------------------------------------------------------------------
# P7-F4 — alert gate semantics
# ---------------------------------------------------------------------------


def test_alert_gate_downweights_low_and_info():
    from legba.data.analysts.agency.binding import escalation_gate_decision

    # A confident-but-boring low/info finding no longer pages.
    assert escalation_gate_decision(severity="low", confidence=0.88) is False
    assert escalation_gate_decision(severity="info", confidence=0.99) is False
    # Moderate is a legitimate page.
    assert escalation_gate_decision(severity="moderate", confidence=0.90) is True
    # A high-severity VERIFIED finding escalates on less confidence (weight > 1).
    assert escalation_gate_decision(severity="high", confidence=0.75) is True


def test_alert_gate_suppresses_absence_titles():
    from legba.data.analysts.agency.binding import (
        escalation_gate_decision,
        is_absence_or_negative_title,
    )

    assert is_absence_or_negative_title("Argentina – Low leadership transition risk") is True
    assert is_absence_or_negative_title("No material escalation observed") is True
    assert is_absence_or_negative_title("Iran strikes Hormuz shipping lane") is False
    # Even a high-severity, high-confidence ABSENCE title is suppressed on the
    # confidence×severity leg.
    assert escalation_gate_decision(
        severity="high", confidence=0.95, title="No credible escalation this window"
    ) is False
    # A real high-severity event with a non-absence title still escalates.
    assert escalation_gate_decision(
        severity="high", confidence=0.9, title="Refinery struck by missile"
    ) is True


# ---------------------------------------------------------------------------
# P7-F5(a) — delivery audit honesty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_logged_only_when_no_publisher_wired():
    from legba.data.analysts.agency.tools import ChannelEmitter
    from legba.data.schemas.action_pack import Channel

    emitter = ChannelEmitter()  # no nats_publish, no pool
    record = await emitter.emit(
        Channel(name="escalations", kind="alert", config={"subject": "channels.escalations"}),
        {"severity": "critical"},
    )
    # A log-only emit went NOWHERE — it must not claim delivery.
    assert record["delivered"] is False
    assert record["status"] == "logged_only"


# ---------------------------------------------------------------------------
# P7-F6 — forecast void is DURABLE (resolver + pull exclude voided rows)
# ---------------------------------------------------------------------------


def test_forecast_void_prefix_and_guards():
    from legba.data.analysts.deterministic_handlers import forecast_acute

    assert forecast_acute.VOID_PREFIX == "voided:"
    resolver_src = inspect.getsource(forecast_acute.resolve_open_acute_forecasts)
    pull_src = inspect.getsource(forecast_acute.pull_resolved_acute_forecasts)
    # Both queries must exclude a withdrawn row so the void cannot be overwritten
    # by the resolver, nor scored by the pilot Brier.
    assert "voided:%" in resolver_src
    assert "voided:%" in pull_src


# ---------------------------------------------------------------------------
# migration scope guards (against accidental scope-broadening)
# ---------------------------------------------------------------------------

_MIG_DIR = Path(__file__).resolve().parents[2] / "src" / "legba" / "data" / "migrations"


def test_migration_0075_scopes_exactly_the_pre_clamp_batch():
    sql = (_MIG_DIR / "0075_void_pre_clamp_acute_forecasts.sql").read_text()
    for pin in (
        "voided:pre_clamp_degenerate",
        "recent_rate_poisson",
        "2026-06-29",
        "2026-07-06",
        "p IN (0, 1)",
        "resolved_by IS NULL",
    ):
        assert pin in sql, pin
    # never a hard delete
    assert "DELETE FROM acute_forecasts" not in sql.upper().replace("  ", " ")


def test_migration_0074_is_world_scoped_and_non_destructive():
    sql = (_MIG_DIR / "0074_world_head_signature_repair.sql").read_text()
    assert "world_assessor" in sql
    assert "signature repair" in sql
    assert "_dq0074_prior_target_id" in sql       # reversibility stash
    assert "_dq0074_prior_superseded_by" in sql
    # close/null/annotate only — no row delete
    assert "DELETE FROM analyst_outputs" not in sql.upper()


# ===========================================================================
# P7 r2 — BLOCKING: forward-looking / bold exemption is FLOOR-anchored, the
# JUDGE still grades every PRESENT-FACT clause (H1 invariant restored)
# ===========================================================================


def test_present_fact_with_conditional_tail_is_judge_graded():
    # 'Tehran resumed enrichment, which would confirm a breakout' — a PRESENT-FACT
    # main clause + a conditional TAIL. The round-1 unanchored substring match
    # ('would confirm') wrongly hid it from the judge; anchored, it is NOT forward-
    # looking, so BOTH the floor counts it and the judge grades it (H1).
    clause = (
        "Tehran resumed uranium enrichment at Fordow, which would confirm a "
        "breakout posture."
    )
    assert _is_fact_asserting(clause) is True       # floor counts it
    assert _is_judgeable_claim(clause) is True       # judge grades it


def test_present_fact_with_conditional_tail_uncited_scores_no_citation_both_floors():
    body = (
        "Tehran resumed uranium enrichment at Fordow, which would confirm a "
        "breakout posture.\n"
    )
    # unit floor
    rep_u = _deterministic_floor(body, [_unit_citation(1, str(uuid4()))])
    assert rep_u.faithfulness_score == 0.0
    assert any(s.reason == "no_citation" for s in rep_u.unsupported_spans)
    # composition (sub-claim) floor
    rep_c = _deterministic_floor_subclaim(body, [_comp_citation(1, eff=0.6, derived=["s1"])])
    assert rep_c.faithfulness_score == 0.0
    assert any(s.reason == "no_citation" for s in rep_c.unsupported_spans)


def test_pure_watch_bullet_would_signal_exempt_both():
    # A PURE forward-looking signpost (bare 'X would signal Y', no present fact) is
    # exempt from BOTH the floor and the judge — nothing exists to cite.
    line = "Official announcements of fuel rationing would signal escalation"
    assert _is_fact_asserting(line) is False
    assert _is_judgeable_claim(line) is False


def test_watch_section_list_item_dropped_before_gates():
    # A bullet UNDER an 'Indicators to watch' heading is a pre-registered indicator:
    # _segment_claims drops the whole section, so it never reaches either gate.
    body = (
        "**Indicators to watch**\n"
        "- Border troop concentrations exceed two divisions.\n"
    )
    segs = _segment_claims(body)
    assert not any("Border troop" in s for s in segs)
    rep = _deterministic_floor_subclaim(body, [_comp_citation(1, eff=0.6, derived=["s1"])])
    # No checkable claim survives the section drop -> vacuously faithful.
    assert rep.checkable_claims == 0
    assert rep.faithfulness_score == 1.0


def test_bold_factual_sentence_is_not_a_heading():
    # A whole-line bold that ASSERTS a fact is NOT a heading — the judge grades it
    # and the floor counts it (only a short titley bold LABEL is exempt).
    from legba.data.provenance.verify import _is_bold_heading

    assert _is_bold_heading("**Key points**") is True
    assert _is_bold_heading("**Indicators to watch**") is True
    assert _is_bold_heading("**Tehran resumed enrichment**") is False
    assert _is_fact_asserting("**Tehran resumed enrichment**") is True
    assert _is_judgeable_claim("**Tehran resumed enrichment**") is True


def test_plain_uncited_fact_still_caught_after_anchor():
    # The anchor must not over-exempt: a plain uncited fact is still flagged.
    body = "Government forces shelled the eastern district overnight.\n"
    rep = _deterministic_floor_subclaim(body, [_comp_citation(1, eff=0.6, derived=["s1"])])
    assert rep.faithfulness_score == 0.0
    assert any(s.reason == "no_citation" for s in rep.unsupported_spans)


# ===========================================================================
# P7 r2 — MAJOR: alert absence-title suppression no longer silences real
# escalations, and a moderate+/high finding is never gagged by a heuristic
# ===========================================================================


def test_alert_gate_pages_real_escalations_with_negative_titles():
    from legba.data.analysts.agency.binding import (
        escalation_gate_decision,
        is_absence_or_negative_title,
    )

    # These read as escalation, not boredom — the round-1 heuristic wrongly gagged
    # them (bare 'no ' / 'routine' substring). They must NOT be absence titles ...
    for t in (
        "No off-ramp as Iran-Israel exchange widens",
        "Routine patrol ambushed; 12 soldiers killed",
        "Non-routine mobilization near border",
        "No ceasefire despite strikes",
    ):
        assert is_absence_or_negative_title(t) is False, t
    # ... and a high-severity finding with such a title PAGES.
    for t in (
        "No off-ramp as Iran-Israel exchange widens",
        "Routine patrol ambushed; 12 soldiers killed",
        "Non-routine mobilization near border",
    ):
        assert escalation_gate_decision(severity="high", confidence=0.9, title=t) is True, t


def test_alert_gate_still_suppresses_genuine_absence_verdicts():
    from legba.data.analysts.agency.binding import (
        escalation_gate_decision,
        is_absence_or_negative_title,
    )

    # A whole-title absence / low-risk verdict (even behind a '<subject> – ' prefix)
    # is boredom at ANY severity — suppressed.
    assert is_absence_or_negative_title("Argentina – Low leadership transition risk") is True
    assert is_absence_or_negative_title(
        "United States – No observable WMD proliferation activity"
    ) is True
    assert escalation_gate_decision(
        severity="low", confidence=0.9, title="Argentina – Low leadership transition risk"
    ) is False
    assert escalation_gate_decision(
        severity="info", confidence=0.99,
        title="United States – No observable WMD proliferation activity",
    ) is False


def test_alert_gate_bare_negative_lead_gated_on_severity():
    from legba.data.analysts.agency.binding import escalation_gate_decision

    # A bare 'No <qualifier>' lead that is NOT a recognized verdict is treated as
    # absence ONLY for a sub-moderate finding; a moderate+/high finding still pages.
    title = "No major developments this week"
    assert escalation_gate_decision(severity="low", confidence=0.99, title=title) is False
    assert escalation_gate_decision(severity="high", confidence=0.9, title=title) is True
    assert escalation_gate_decision(severity="moderate", confidence=0.9, title=title) is True


# ===========================================================================
# P7 r2 — MAJOR: cadence cooldown is stamped on EVERY organic trigger (incl.
# the reactive 'coalesced_fire'), NOT the manual/forced 'method' run
# ===========================================================================


def test_organic_trigger_includes_coalesced_fire_excludes_method():
    from legba.runtime.dapr_actors import _FORCED_TRIGGERS, _is_organic_trigger

    # The production reactive per-target dispatch stamps the cooldown ...
    assert _is_organic_trigger("coalesced_fire") is True
    assert _is_organic_trigger("cadence") is True
    assert _is_organic_trigger("reminder") is True
    # ... a manual/forced 'method' run does NOT (can't steal the next organic tick).
    assert _is_organic_trigger("method") is False
    assert "method" in _FORCED_TRIGGERS
    assert "coalesced_fire" not in _FORCED_TRIGGERS


def test_region_composition_alias_preserved():
    from legba.data.analysts import meta_findings_synthesizer as synth

    # The constant was renamed REGIONAL; the back-compat alias stays identical so
    # the region-run dispatch and existing references resolve to the same prompt.
    assert synth._REGION_COMPOSITION_SYSTEM is synth._WORLD_COMPOSITION_SYSTEM
    assert "REGIONAL COMPOSITION" in synth._REGION_COMPOSITION_SYSTEM
