# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P4-T1 — the banded-verdict rule engine.

Covers, mostly as PURE functions (no DB):

  * the demote ladder + boundary constants (0.60 confident, 0.35 floor):
    critical@0.4 -> demoted 'high' (damped), elevated@0.62 -> 'elevated'
    (undamped), and the exact-boundary rows (0.60 stands, 0.35 damps,
    0.3499 falls below the floor);
  * the R0-R4 rule table: no-finding / verify-failed (None) / verify-failed
    (coerce_failed tag) / below-floor / no-severity-tag, each returning
    band='insufficient-evidence' with an EMPTY explicit basis and all-null
    numerics — never a fabricated band, never a synthesized basis id;
  * the HONESTY invariant: a real band ALWAYS names basis=[finding_id] (the id
    that drove it) and insufficient ALWAYS names basis=[];
  * band_target over the four fixed dimensions (keyed off analyst_id, not the
    topic tag) + the composition surfaced as its own node, never a fabricated
    overall band;
  * the async gather_and_band run-entry over a fake pool (verify fold →
    band_target), incl. the LEFT-join verify-absent → verify-failed path.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from legba.data.analysts.deterministic_handlers import scorecard_banding as sb


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _claim(
    *,
    analyst_id="escalation",
    confidence=0.9,
    faithfulness=0.9,
    severity="high",
    extra_tags=(),
    finding_id=None,
    produced_at="2026-06-30T00:00:00+00:00",
):
    """A gathered Claim with a severity:<level> tag (unless severity=None)."""
    tags = list(extra_tags)
    if severity is not None:
        tags.append(f"severity:{severity}")
    return sb.Claim(
        finding_id=finding_id or str(uuid4()),
        analyst_id=analyst_id,
        confidence=confidence,
        faithfulness_score=faithfulness,
        tags=tuple(tags),
        produced_at=produced_at,
    )


# ---------------------------------------------------------------------------
# Constants / helper units
# ---------------------------------------------------------------------------


def test_constants_are_the_locked_rule_table():
    assert sb.CONF_FLOOR == 0.35
    assert sb.CONF_CONFIDENT == 0.60
    assert sb.BAND_LADDER == ("low", "watch", "elevated", "high", "critical")
    assert sb.SEVERITY_TO_BAND == {
        "low": "low",
        "moderate": "watch",
        "elevated": "elevated",
        "high": "high",
        "critical": "critical",
    }
    # Dimensions are the unit analyst_ids, not topic tags. internal_stability
    # (S1-T4) + military_posture (S1-T5) join the original four P2 units.
    assert sb.DIMENSIONS == (
        "leadership_transition",
        "energy_security",
        "escalation",
        "narrative_coordination",
        "internal_stability",
        "military_posture",
    )
    assert sb.COMPOSITION_ANALYST_ID == "country_composition"


def test_demote_one_walks_down_and_clamps_at_low_never_promotes():
    assert sb.demote_one("critical") == "high"
    assert sb.demote_one("high") == "elevated"
    assert sb.demote_one("watch") == "low"
    assert sb.demote_one("low") == "low"  # clamped, never below the ladder


def test_severity_from_tags_parses_only_valid_levels():
    assert sb._severity_from_tags(["severity:critical", "topic:x"]) == "critical"
    assert sb._severity_from_tags(["escalation", "target:foo"]) is None
    assert sb._severity_from_tags(["severity:bogus"]) is None
    # Bare 'escalation' topic tag must never be read as a severity.
    assert sb._severity_from_tags(["escalation"]) is None


# ---------------------------------------------------------------------------
# R4 — the band assignment + boundaries
# ---------------------------------------------------------------------------


def test_confident_claim_bands_at_severity_undamped():
    fid = str(uuid4())
    v = sb.band_dimension(_claim(severity="elevated", faithfulness=0.62,
                                 confidence=0.9, finding_id=fid))
    assert v.band == "elevated"
    assert v.damped is False
    assert v.reason == "qualified"
    # basis names the exact finding id that drove the band.
    assert v.basis == [fid]
    assert v.severity_tag == "elevated"
    assert v.effective_confidence == pytest.approx(0.62)
    assert v.critic_score == pytest.approx(0.62)  # folded faithfulness


def test_low_effective_confidence_demotes_one_rung_and_flags_damped():
    # critical @ effective 0.4 (min(0.4, 0.9)) -> demoted to 'high'. The demote is
    # driven by CONFIDENCE in the damped band; faithfulness stays ABOVE the P4-T5
    # faith floor (0.50) so the dedicated low-faithfulness exclusion does NOT fire.
    fid = str(uuid4())
    v = sb.band_dimension(_claim(severity="critical", confidence=0.4,
                                 faithfulness=0.9, finding_id=fid))
    assert v.band == "high"
    assert v.damped is True
    assert v.reason == "damped"
    assert v.basis == [fid]
    assert v.effective_confidence == pytest.approx(0.4)


def test_confident_boundary_060_stands_undamped():
    v = sb.band_dimension(_claim(severity="high", confidence=0.6, faithfulness=0.9))
    assert v.effective_confidence == pytest.approx(0.60)
    assert v.band == "high"
    assert v.damped is False


def test_floor_boundary_035_damps_not_dropped():
    v = sb.band_dimension(_claim(severity="high", confidence=0.35, faithfulness=0.9))
    assert v.effective_confidence == pytest.approx(0.35)
    assert v.band == "elevated"  # demote_one('high')
    assert v.damped is True


def test_low_severity_damped_clamps_at_low():
    v = sb.band_dimension(_claim(severity="low", confidence=0.4, faithfulness=0.9))
    assert v.band == "low"  # demote_one('low') clamps
    assert v.damped is True


# ---------------------------------------------------------------------------
# R0-R3 — the insufficient-evidence honesty (empty explicit basis)
# ---------------------------------------------------------------------------


def _assert_insufficient(v, reason):
    assert v.band == sb.INSUFFICIENT == "insufficient-evidence"
    assert v.basis == []  # empty but EXPLICIT — never fabricated
    assert v.reason == reason
    # every numeric field is null in an insufficient verdict
    assert v.severity_tag is None
    assert v.effective_confidence is None
    assert v.confidence is None
    assert v.critic_score is None
    assert v.damped is False


def test_r0_no_finding_when_claim_absent():
    _assert_insufficient(sb.band_dimension(None), "no-finding")


def test_r1_verify_failed_when_no_faithfulness_folded():
    # effective_confidence is None because faithfulness never folded.
    v = sb.band_dimension(_claim(severity="critical", confidence=0.9,
                                 faithfulness=None))
    _assert_insufficient(v, "verify-failed")


def test_r1_verify_failed_drops_coerce_fallback_even_if_scored():
    # A vacuously-faithful coerce_failed body scores fine but must be dropped.
    v = sb.band_dimension(_claim(severity="critical", confidence=0.9,
                                 faithfulness=0.95, extra_tags=("coerce_failed",)))
    _assert_insufficient(v, "verify-failed")
    v2 = sb.band_dimension(_claim(severity="high", faithfulness=0.9,
                                  extra_tags=("unstructured",)))
    _assert_insufficient(v2, "verify-failed")


def test_r2_below_floor_when_effective_under_035():
    v = sb.band_dimension(_claim(severity="critical", confidence=0.3499,
                                 faithfulness=0.9))
    _assert_insufficient(v, "below-floor")


# ---------------------------------------------------------------------------
# R1b (P4-T5) — the dedicated low-faithfulness exclusion
# ---------------------------------------------------------------------------


def test_faith_floor_is_the_locked_constant():
    assert sb.FAITH_FLOOR == 0.50


def test_r1b_low_faithfulness_excludes_claim_with_dedicated_reason():
    # conf .9 / faith .45 — the DISTINCT low-faithfulness state (NOT below-floor):
    # effective_confidence=min(.9,.45)=.45 is ABOVE the 0.35 conf floor, so
    # without the dedicated guard this would band; the faith floor excludes it.
    v = sb.band_dimension(_claim(severity="critical", confidence=0.9,
                                 faithfulness=0.45))
    _assert_insufficient(v, "low-faithfulness")


def test_r1b_faith_at_floor_050_is_not_excluded():
    # AT the floor (0.50) is NOT below it — the claim bands (damped, since
    # effective 0.50 < the 0.60 confident boundary).
    v = sb.band_dimension(_claim(severity="high", confidence=0.9,
                                 faithfulness=0.50))
    assert v.band == "elevated"  # demote_one('high'), damped
    assert v.reason == "damped"
    assert v.critic_score == pytest.approx(0.50)


def test_r1b_runs_after_verify_failed_and_before_below_floor():
    # verify-failed (faith None) still wins over the faith floor.
    v = sb.band_dimension(_claim(severity="critical", confidence=0.9,
                                 faithfulness=None))
    _assert_insufficient(v, "verify-failed")
    # A claim with BOTH low faith (.4) and a sub-floor effective would read
    # low-faithfulness (R1b) — R1b precedes R2 (below-floor).
    v2 = sb.band_dimension(_claim(severity="critical", confidence=0.9,
                                  faithfulness=0.40))
    _assert_insufficient(v2, "low-faithfulness")


def test_faith_floor_is_overridable_and_threaded_through_band_target():
    # Lowering the floor lets a mediocre-faithfulness claim band again.
    fid = str(uuid4())
    claims = {"escalation": _claim(analyst_id="escalation", severity="high",
                                   confidence=0.9, faithfulness=0.45,
                                   finding_id=fid)}
    verdict = sb.band_target("target:usa", claims, faith_floor=0.40)
    esc = verdict["dimensions"]["escalation"]
    assert esc["band"] != sb.INSUFFICIENT
    assert esc["basis"] == [fid]
    # The threaded floor is surfaced in floors.
    assert verdict["floors"]["faith_floor"] == 0.40


def test_floors_carries_faith_floor_by_default():
    verdict = sb.band_target("target:usa", {})
    assert verdict["floors"] == {
        "conf_floor": 0.35,
        "conf_confident": 0.60,
        "faith_floor": 0.50,
    }


def test_r3_no_severity_tag():
    v = sb.band_dimension(_claim(severity=None, confidence=0.9, faithfulness=0.9))
    _assert_insufficient(v, "no-severity-tag")


def test_r3_invalid_severity_level_is_no_severity_tag():
    v = sb.band_dimension(_claim(severity="moderate-ish", confidence=0.9,
                                 faithfulness=0.9))
    _assert_insufficient(v, "no-severity-tag")


def test_rule_precedence_verify_failed_before_below_floor_and_severity():
    # None effective wins over a missing severity tag and a sub-floor value.
    v = sb.band_dimension(_claim(severity=None, confidence=0.1, faithfulness=None))
    _assert_insufficient(v, "verify-failed")


# ---------------------------------------------------------------------------
# band_target — four dimensions + composition node
# ---------------------------------------------------------------------------


def test_band_target_reports_all_four_dimensions_even_when_absent():
    fid = str(uuid4())
    claims = {"escalation": _claim(analyst_id="escalation", severity="high",
                                   confidence=0.9, faithfulness=0.9,
                                   finding_id=fid)}
    verdict = sb.band_target("target:usa", claims)

    assert verdict["target_id"] == "target:usa"
    assert set(verdict["dimensions"]) == set(sb.DIMENSIONS)
    assert verdict["floors"] == {
        "conf_floor": 0.35, "conf_confident": 0.60, "faith_floor": 0.50,
    }

    # The one present unit bands + names its basis id.
    esc = verdict["dimensions"]["escalation"]
    assert esc["band"] == "high"
    assert esc["basis"] == [fid]
    # The three absent units are honest insufficient with empty basis.
    for unit in ("leadership_transition", "energy_security", "narrative_coordination"):
        dim = verdict["dimensions"][unit]
        assert dim["band"] == "insufficient-evidence"
        assert dim["basis"] == []


def test_band_target_composition_is_its_own_node_never_a_fabricated_overall():
    comp_id = str(uuid4())
    comp = _claim(analyst_id="country_composition", confidence=0.8,
                  faithfulness=0.8, severity="high", finding_id=comp_id)
    verdict = sb.band_target("target:usa", {}, comp)

    node = verdict["composition"]
    assert node["present"] is True
    assert node["basis"] == [comp_id]
    # There is NO fabricated overall band anywhere in the verdict.
    assert "overall" not in verdict
    assert "band" not in verdict


def test_band_target_absent_composition_is_present_false_empty_basis():
    node = sb.band_target("target:usa", {})["composition"]
    assert node["present"] is False
    assert node["basis"] == []


# ---------------------------------------------------------------------------
# gather_and_band — the async run-entry over a fake pool
# ---------------------------------------------------------------------------


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, rows):
        self._rows = rows

    def acquire(self):
        return _AcquireCtx(_FakeConn(self._rows))


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, sql, *args):
        # Sanity: the gather query is the verify-folded per-analyst DISTINCT ON.
        assert "DISTINCT ON (f.analyst_id)" in sql
        assert "Faithfulness verify%" in sql
        return self._rows


def _row(analyst_id, finding_id, confidence, faithfulness, tags):
    return {
        "finding_id": finding_id,
        "analyst_id": analyst_id,
        "confidence": confidence,
        "faithfulness_score": faithfulness,
        "tags": tags,  # a JSON list; the coercer also accepts a JSON string
        "produced_at": None,
    }


@pytest.mark.asyncio
async def test_gather_and_band_folds_verify_and_bands_over_fake_pool():
    lead_id, esc_id, comp_id = str(uuid4()), str(uuid4()), str(uuid4())
    rows = [
        # confident high -> stands
        _row("leadership_transition", lead_id, 0.9, 0.9, ["severity:high"]),
        # verify absent (faithfulness None) -> verify-failed, dropped from band
        _row("escalation", esc_id, 0.9, None, ["severity:critical"]),
        # composition present -> own node, tags as a JSON string exercises coercion
        _row("country_composition", comp_id, 0.8, 0.8, '["severity:high"]'),
    ]
    verdict = await sb.gather_and_band(_FakePool(rows), "target:usa")

    dims = verdict["dimensions"]
    assert dims["leadership_transition"]["band"] == "high"
    assert dims["leadership_transition"]["basis"] == [lead_id]
    # escalation had no folded verify -> honest verify-failed, empty basis
    assert dims["escalation"]["band"] == "insufficient-evidence"
    assert dims["escalation"]["reason"] == "verify-failed"
    assert dims["escalation"]["basis"] == []
    # energy_security never appeared -> no-finding
    assert dims["energy_security"]["reason"] == "no-finding"
    # composition surfaced as its own node with its real id
    assert verdict["composition"] == {
        "present": True,
        "basis": [comp_id],
        "effective_confidence": pytest.approx(0.8),
        "produced_at": None,
    }


def test_coerce_tags_handles_list_json_string_and_none():
    assert sb._coerce_tags(["a", "b"]) == ("a", "b")
    assert sb._coerce_tags('["a", "b"]') == ("a", "b")
    assert sb._coerce_tags(None) == ()
    assert sb._coerce_tags("not json") == ()
