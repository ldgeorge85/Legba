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
    # (S1-T4) + military_posture (S1-T5) + economic_coercion (S1-T7) join the
    # original four P2 units.
    assert sb.DIMENSIONS == (
        "leadership_transition",
        "energy_security",
        "escalation",
        "narrative_coordination",
        "internal_stability",
        "military_posture",
        "economic_coercion",
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


def test_low_effective_confidence_no_longer_demotes_and_records_what_it_would_have(
):
    """H3 — the damper is RETIRED from the band path.

    ``critical`` @ effective 0.4 (min(0.4, 0.9)) used to ship ``high``: one rung
    down for weak confidence. Under severity-as-state that subtraction demotes a
    STANDING LEVEL rather than a slice delta, and CORRECTNESS-R2 measured it
    net-negative in six lanes. The band is now the severity tag's band; the
    weakness is named in the ``reason`` and the retired rung is RECORDED, so the
    reversal is auditable from the row instead of from the deploy log.
    """
    fid = str(uuid4())
    v = sb.band_dimension(_claim(severity="critical", confidence=0.4,
                                 faithfulness=0.9, finding_id=fid))
    assert v.band == "critical"                 # the tag's band, undamped
    assert v.damped is False
    assert v.damped_would_have_been == "high"   # what the retired damper did
    assert v.reason == "qualified-low-confidence"
    assert v.basis == [fid]
    assert v.effective_confidence == pytest.approx(0.4)


def test_a_confident_band_records_no_would_have_been():
    """The damper only ever fired between the floor and the confident knee, so
    a confident row has nothing to record. ``None`` (not the band itself) keeps
    "the damper would have fired here" a one-field question."""
    v = sb.band_dimension(_claim(severity="critical", confidence=0.9,
                                 faithfulness=0.9))
    assert v.band == "critical"
    assert v.damped_would_have_been is None
    assert v.reason == "qualified"


def test_confident_boundary_060_stands_undamped():
    v = sb.band_dimension(_claim(severity="high", confidence=0.6, faithfulness=0.9))
    assert v.effective_confidence == pytest.approx(0.60)
    assert v.band == "high"
    assert v.damped is False


def test_floor_boundary_035_bands_at_the_tag_not_dropped_and_not_demoted():
    """AT the floor is IN: 0.35 bands. H3: it bands at the TAG (``high``), where
    it used to ship ``elevated``. The floor still decides admission; it no longer
    decides the rung."""
    v = sb.band_dimension(_claim(severity="high", confidence=0.35, faithfulness=0.9))
    assert v.effective_confidence == pytest.approx(0.35)
    assert v.band == "high"
    assert v.damped is False
    assert v.damped_would_have_been == "elevated"


def test_the_clamped_case_records_low_would_have_been_low():
    """The 10-of-22 case R2 counted separately: ``low`` damped to ``low``. The
    retired damper lost nothing here, and ``damped_would_have_been`` says so by
    naming the same band rather than ``None`` — "it fired and changed nothing"
    and "it did not fire" are different facts about the row."""
    v = sb.band_dimension(_claim(severity="low", confidence=0.4, faithfulness=0.9))
    assert v.band == "low"
    assert v.damped is False
    assert v.damped_would_have_been == "low"  # demote_one('low') clamps
    assert v.reason == "qualified-low-confidence"


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
    # AT the floor (0.50) is NOT below it — the claim bands. Effective 0.50 is
    # under the 0.60 confident knee, so the reason names the weakness (H3: it no
    # longer moves the band down a rung).
    v = sb.band_dimension(_claim(severity="high", confidence=0.9,
                                 faithfulness=0.50))
    assert v.band == "high"
    assert v.reason == "qualified-low-confidence"
    assert v.damped_would_have_been == "elevated"
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


# ---------------------------------------------------------------------------
# H3 (1) — the retired damper, at the verdict + card level
# ---------------------------------------------------------------------------


def test_damping_semantics_is_stamped_on_every_card():
    """The band value alone cannot say which contract produced it. A ``watch``
    under the old damper and a ``watch`` under H3 are the same five characters,
    so the card records the contract the same way FRAME-3's flip does."""
    verdict = sb.band_target("target:usa", {})
    assert verdict["damping_semantics"] == sb.DAMPING_SEMANTICS == "off"
    assert verdict["banding_semantics"] == "standing"  # FRAME-3, untouched


# ---------------------------------------------------------------------------
# H3-GUARD — the semantics-mismatch primitives shared by alert_trigger_scan
# and band_calibration_tracker.
# ---------------------------------------------------------------------------


def test_bands_semantics_reads_the_stamps_off_a_real_card():
    verdict = sb.band_target("target:usa", {})
    assert sb.bands_semantics(verdict) == (sb.BANDING_SEMANTICS, sb.DAMPING_SEMANTICS)


def test_bands_semantics_absent_or_malformed_reads_none_none():
    assert sb.bands_semantics({}) == (None, None)
    assert sb.bands_semantics(None) == (None, None)
    assert sb.bands_semantics("not-a-mapping") == (None, None)
    # Half-stamped (FRAME-3 landed, H3 has not) — the damping key is absent.
    assert sb.bands_semantics({"banding_semantics": "standing"}) == (
        "standing", None,
    )


def test_semantics_changed_false_when_both_absent():
    """Two cards written before either stamp existed carry NO information
    that they differ — reading None==None as a mismatch would reclassify
    every pre-stamp transition in history (and every fixture that never
    bothered stamping one) as a migration event."""
    assert sb.semantics_changed((None, None), (None, None)) is False


def test_semantics_changed_false_when_identical_and_present():
    assert sb.semantics_changed(("standing", "off"), ("standing", "off")) is False


def test_semantics_changed_true_when_prior_predates_a_stamp_now_present():
    """THE H3 shape: damping_semantics is a NEW key. Every pre-H3 card lacks
    it; every post-H3 card carries 'off'."""
    assert sb.semantics_changed(("standing", None), ("standing", "off")) is True
    # Symmetric on the banding side too.
    assert sb.semantics_changed((None, "off"), ("standing", "off")) is True
    assert sb.semantics_changed((None, None), ("standing", "off")) is True


def test_semantics_changed_true_on_a_real_value_mismatch():
    assert sb.semantics_changed(("standing", "off"), ("delta", "off")) is True
    assert sb.semantics_changed(("standing", "off"), ("standing", "demote")) is True


def test_no_band_in_a_whole_card_is_ever_damped():
    """The census the R2 attribution ran (22 of 49 rows ``damped: true``) must
    now return zero over the same shape of input — including the rows that would
    have damped, which say so in ``damped_would_have_been`` instead."""
    claims = {
        unit: _claim(analyst_id=unit, severity=sev, confidence=conf,
                     faithfulness=0.9)
        for unit, sev, conf in (
            ("escalation", "critical", 0.40),
            ("energy_security", "high", 0.55),
            ("internal_stability", "moderate", 0.36),
            ("military_posture", "low", 0.50),
            ("economic_coercion", "elevated", 0.95),
        )
    }
    dims = sb.band_target("target:usa", claims)["dimensions"]
    assert [d["damped"] for d in dims.values()] == [False] * len(sb.DIMENSIONS)
    # the four weak rows record the retired rung; the confident one does not
    assert dims["escalation"]["damped_would_have_been"] == "high"
    assert dims["internal_stability"]["damped_would_have_been"] == "low"
    assert dims["economic_coercion"]["damped_would_have_been"] is None
    # and the bands are the severity tags' bands, not one rung under them
    assert dims["escalation"]["band"] == "critical"
    assert dims["internal_stability"]["band"] == "watch"   # moderate -> watch


def test_the_cd_internal_stability_case_from_r2():
    """The round's sharpest damper case, as a regression lock: a desk that called
    the standing level ``moderate`` AND called the movement ``rose`` shipped
    ``low``. Severity-as-state semantics are untouched — the delta still rides
    beside the band and still moves nothing — but the band is now the desk's."""
    v = sb.band_dimension(
        _claim(analyst_id="internal_stability", severity="moderate",
               confidence=0.55, faithfulness=0.9,
               extra_tags=("severity_delta:rose",))
    )
    assert v.band == "watch"                     # SEVERITY_TO_BAND['moderate']
    assert v.damped_would_have_been == "low"     # what shipped in R2
    assert v.severity_delta == "rose"            # beside the band, not inside it


# ---------------------------------------------------------------------------
# H3 (2) — basis alignment against the composition's consumed heads
# ---------------------------------------------------------------------------


def _consumed(analyst_id, **kw):
    """A head the composition consumed for ``analyst_id``."""
    return _claim(analyst_id=analyst_id, **kw)


def test_no_composition_means_no_alignment_claim_at_all():
    """Without the second artefact on the page there is nothing to diverge from,
    and the state says exactly that rather than an unearned ``aligned``."""
    v = sb.band_dimension(_claim(severity="high"))
    assert v.basis_state == sb.BASIS_NO_COMPOSITION
    assert v.consumed_basis == ()


def test_consumed_heads_are_ignored_when_no_composition_is_present():
    """A consumed set with no composition to consume it would move a band on
    evidence the reader never sees. The guard is positional: it returns before
    any candidate is examined."""
    fresh = _claim(severity="high", confidence=0.1, faithfulness=0.9)
    strong = _consumed("escalation", severity="high", confidence=0.9)
    v = sb.band_dimension(fresh, consumed=[strong], composition_present=False)
    assert v.band == sb.INSUFFICIENT
    assert v.basis_state == sb.BASIS_NO_COMPOSITION


def test_aligned_when_the_composition_consumed_the_very_row_the_card_banded():
    fid = str(uuid4())
    claim = _claim(severity="high", finding_id=fid)
    v = sb.band_dimension(claim, consumed=[claim], composition_present=True)
    assert v.band == "high"
    assert v.basis == [fid]
    assert v.basis_state == sb.BASIS_ALIGNED
    assert v.consumed_basis == (fid,)


def test_banded_unconsumed_records_the_reverse_divergence_without_a_band_move():
    """R2 found the divergence running BOTH ways: BF ``economic_coercion``
    banded at eff 0.40 — above this engine's 0.35 floor, below the composition's
    0.50 admission bar — so the card banded a desk the prose never cited. The
    band is correct under this engine's own rule and is NOT touched; the state
    makes the disagreement visible instead of implicit."""
    banded = _claim(severity="low", confidence=0.40, faithfulness=0.9)
    other = _consumed("escalation", severity="high", confidence=0.9)
    v = sb.band_dimension(banded, consumed=[other], composition_present=True)
    assert v.band == "low"                       # unchanged
    assert v.basis == [banded.finding_id]        # unchanged
    assert v.basis_state == sb.BASIS_UNCONSUMED
    assert v.consumed_basis == (other.finding_id,)


def test_not_consumed_when_the_composition_read_the_desk_no_more_than_the_card(
):
    v = sb.band_dimension(_claim(severity="high"), consumed=[],
                          composition_present=True)
    assert v.basis_state == sb.BASIS_NOT_CONSUMED
    v2 = sb.band_dimension(None, consumed=[], composition_present=True)
    assert v2.band == sb.INSUFFICIENT
    assert v2.reason == "no-finding"
    assert v2.basis_state == sb.BASIS_NOT_CONSUMED


def test_the_bf_energy_security_case_the_card_bands_what_the_prose_consumed():
    """The round's named instance: the freshest head was conf 0.20 (below floor)
    and the composition had consumed a 0.90 head one cycle back tagged
    ``severity:moderate``. The card published ``insufficient-evidence`` beside
    prose asserting all seven desks produced verified reads."""
    fresh = _claim(analyst_id="energy_security", severity="low",
                   confidence=0.20, faithfulness=1.0,
                   produced_at="2026-08-24T16:01:27+00:00")
    passing = _consumed("energy_security", severity="moderate", confidence=0.90,
                        faithfulness=1.0,
                        produced_at="2026-08-23T16:01:34+00:00")
    v = sb.band_dimension(fresh, consumed=[fresh, passing],
                          composition_present=True)

    assert v.band == "watch"                     # SEVERITY_TO_BAND['moderate']
    assert v.basis == [passing.finding_id]       # the row the prose rests on
    assert v.basis_state == sb.BASIS_CONSUMED
    assert v.effective_confidence == pytest.approx(0.90)
    # FRAME-1's periphery rule: the newer head the card could not band is shown,
    # DATED, beside the older one it could.
    assert v.newer_head == {
        "finding_id": fresh.finding_id,
        "reason": "below-floor",
        "produced_at": "2026-08-24T16:01:27+00:00",
    }


def test_the_consumed_walk_takes_the_NEWEST_qualifying_head():
    """FRAME-1's rule verbatim. Two consumed heads both qualify; the newer one
    wins, so realignment never reaches further back than it must."""
    fresh = _claim(severity="low", confidence=0.1, faithfulness=0.9)
    newer = _consumed("escalation", severity="high", confidence=0.9,
                      produced_at="2026-08-24T00:00:00+00:00")
    older = _consumed("escalation", severity="critical", confidence=0.9,
                      produced_at="2026-08-20T00:00:00+00:00")
    v = sb.band_dimension(fresh, consumed=[newer, older],
                          composition_present=True)
    assert v.basis == [newer.finding_id]
    assert v.band == "high"


def test_the_alignment_path_is_never_a_softer_path():
    """Every consumed head runs the IDENTICAL R0-R4 guards. A consumed head that
    fails one is refused exactly as a freshest head would be — the card would
    otherwise launder an unverified read into a band by way of the composition.

    This is the live IL ``energy_security`` shape from the replay: the
    composition consumed a conf 0.20 / faithfulness 0.00 head through its
    periphery tier, and the card still abstains."""
    fresh = _claim(severity="high", confidence=0.1, faithfulness=0.9)
    junk = _consumed("energy_security", severity="low", confidence=0.20,
                     faithfulness=0.0)
    v = sb.band_dimension(fresh, consumed=[junk], composition_present=True)

    assert v.band == sb.INSUFFICIENT
    assert v.basis == []                            # no band ⇒ no basis, still
    assert v.basis_state == sb.BASIS_CONSUMED_UNBANDABLE
    assert v.consumed_basis == (junk.finding_id,)   # but NEVER a silent null
    assert v.consumed_reason == "low-faithfulness"  # the rule that refused it


@pytest.mark.parametrize(
    "severity, tags, expected_reason",
    (
        (None, (), "no-severity-tag"),
        ("high", ("coerce_failed",), "verify-failed"),
    ),
)
def test_consumed_unbandable_names_whichever_rule_refused_the_head(
    severity, tags, expected_reason
):
    fresh = _claim(severity="high", confidence=0.1, faithfulness=0.9)
    bad = _consumed("escalation", severity=severity, extra_tags=tags)
    v = sb.band_dimension(fresh, consumed=[bad], composition_present=True)
    assert v.basis_state == sb.BASIS_CONSUMED_UNBANDABLE
    assert v.consumed_reason == expected_reason


def test_every_dimension_gets_exactly_one_state_from_the_enum():
    """The assignment is exhaustive and has no fallthrough — an unrecognised
    state would mean a dimension whose relation to the prose is undefined."""
    verdict = sb.band_target(
        "target:usa",
        {"escalation": _claim(analyst_id="escalation", severity="high")},
        _claim(analyst_id="country_composition", severity="high"),
    )
    for unit, dim in verdict["dimensions"].items():
        assert dim["basis_alignment"]["state"] in sb.BASIS_ALIGNMENT_STATES, unit
    census = verdict["basis_alignment"]
    assert census["composition_present"] is True
    assert sum(census["states"].values()) == len(sb.DIMENSIONS)


def test_the_card_level_census_counts_realigned_and_unbandable_separately():
    """Two different facts about a country, never one ratio: how many dimensions
    the card recovered from the prose's own basis, and how many still abstain
    beside a composition that read the desk."""
    weak = _claim(analyst_id="escalation", severity="high", confidence=0.1,
                  faithfulness=0.9)
    good = _consumed("escalation", severity="high", confidence=0.9)
    weak2 = _claim(analyst_id="energy_security", severity="high",
                   confidence=0.1, faithfulness=0.9)
    junk = _consumed("energy_security", severity="low", confidence=0.2,
                     faithfulness=0.0)
    verdict = sb.band_target(
        "target:usa",
        {"escalation": weak, "energy_security": weak2},
        _claim(analyst_id="country_composition", severity="high"),
        consumed_by_dim={"escalation": [good], "energy_security": [junk]},
    )
    assert verdict["basis_alignment"]["realigned"] == 1
    assert verdict["basis_alignment"]["unbandable"] == 1
    assert verdict["dimensions"]["escalation"]["band"] == "high"
    assert verdict["dimensions"]["energy_security"]["band"] == sb.INSUFFICIENT


def test_consumed_by_dim_is_ignored_when_the_composition_is_absent():
    """band_target's own guard, matching band_dimension's: no composition node
    on the card ⇒ the consumed set cannot move a band."""
    weak = _claim(analyst_id="escalation", severity="high", confidence=0.1,
                  faithfulness=0.9)
    good = _consumed("escalation", severity="high", confidence=0.9)
    verdict = sb.band_target(
        "target:usa", {"escalation": weak}, None,
        consumed_by_dim={"escalation": [good]},
    )
    assert verdict["dimensions"]["escalation"]["band"] == sb.INSUFFICIENT
    assert verdict["basis_alignment"]["composition_present"] is False


def test_the_band_basis_invariant_holds_in_both_directions_after_h3():
    """P4-T8's honesty invariant, re-checked against the new states: a real band
    ALWAYS names the row that drove it, and an insufficient dimension NEVER
    names one — the consulted-but-unbandable ids ride in ``consumed_basis``,
    which is a different field answering a different question."""
    cases = [
        sb.band_dimension(_claim(severity="high"), composition_present=True),
        sb.band_dimension(None, composition_present=True),
        sb.band_dimension(
            _claim(severity="high", confidence=0.1, faithfulness=0.9),
            consumed=[_consumed("escalation", severity="high", confidence=0.9)],
            composition_present=True,
        ),
        sb.band_dimension(
            _claim(severity="high", confidence=0.1, faithfulness=0.9),
            consumed=[_consumed("escalation", severity=None)],
            composition_present=True,
        ),
    ]
    for v in cases:
        if v.band == sb.INSUFFICIENT:
            assert v.basis == [], v.basis_state
        else:
            assert len(v.basis) == 1 and v.basis[0], v.basis_state


# ---------------------------------------------------------------------------
# H3 — gather_and_band: the consumed-basis wiring + the as-of replay pin
# ---------------------------------------------------------------------------


class _TwoQueryConn:
    """A fake connection that dispatches the gather and the consumed lookup.

    Records every (sql, args) pair so a test can assert WHICH statement ran —
    the production path must execute the legacy gather string verbatim.
    """

    def __init__(self, head_rows, consumed_rows):
        self.head_rows = head_rows
        self.consumed_rows = consumed_rows
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if "f.id = ANY($1::UUID[])" in sql:
            return self.consumed_rows
        return self.head_rows


class _TwoQueryPool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _AcquireCtx(self.conn)


def _row_df(analyst_id, finding_id, confidence, faithfulness, tags,
            derived_from=None, produced_at=None):
    row = _row(analyst_id, finding_id, confidence, faithfulness, tags)
    row["produced_at"] = produced_at
    if derived_from is not None:
        row["derived_from"] = derived_from
    return row


@pytest.mark.asyncio
async def test_gather_and_band_bands_from_the_compositions_consumed_head():
    """The whole H3 chain through the real run-entry: the composition row's
    ``derived_from`` is resolved to rows, those rows are handed to the banding,
    and a dimension that would have abstained bands from the head the prose
    actually used."""
    weak_id, strong_id, comp_id = str(uuid4()), str(uuid4()), str(uuid4())
    heads = [
        _row_df("energy_security", weak_id, 0.20, 1.0, ["severity:low"],
                produced_at="2026-08-24T16:01:27+00:00"),
        _row_df("country_composition", comp_id, 0.8, 0.8, ["severity:high"],
                derived_from=[weak_id, strong_id]),
    ]
    consumed = [
        _row_df("energy_security", weak_id, 0.20, 1.0, ["severity:low"],
                produced_at="2026-08-24T16:01:27+00:00"),
        _row_df("energy_security", strong_id, 0.90, 1.0, ["severity:moderate"],
                produced_at="2026-08-23T16:01:34+00:00"),
    ]
    conn = _TwoQueryConn(heads, consumed)
    verdict = await sb.gather_and_band(_TwoQueryPool(conn), "country_watch_bf")

    dim = verdict["dimensions"]["energy_security"]
    assert dim["band"] == "watch"
    assert dim["basis"] == [strong_id]
    assert dim["basis_alignment"]["state"] == sb.BASIS_CONSUMED
    assert dim["basis_alignment"]["newer_head"]["finding_id"] == weak_id
    # the consumed lookup is scoped to this target + the fixed dimensions
    consumed_call = [c for c in conn.calls if "ANY($1::UUID[])" in c[0]][0]
    assert consumed_call[1][1] == "country_watch_bf"
    assert consumed_call[1][2] == list(sb.DIMENSIONS)


@pytest.mark.asyncio
async def test_no_second_query_fires_when_the_composition_names_no_basis():
    """Cost discipline: a country whose composition has no lineage (or has no
    composition at all) pays exactly the pre-H3 cost — one statement."""
    comp_id = str(uuid4())
    heads = [_row_df("country_composition", comp_id, 0.8, 0.8, ["severity:high"],
                     derived_from=[])]
    conn = _TwoQueryConn(heads, [])
    await sb.gather_and_band(_TwoQueryPool(conn), "target:usa")
    assert len(conn.calls) == 1


@pytest.mark.asyncio
async def test_production_path_executes_the_legacy_gather_string_verbatim():
    """``as_of=None`` is production. The as-of variant is a SEPARATE constant
    precisely so this is assertable rather than argued: the live sweep runs the
    string it ran before H3, character for character."""
    conn = _TwoQueryConn([], [])
    await sb.gather_and_band(_TwoQueryPool(conn), "target:usa")
    sql, args = conn.calls[0]
    assert sql == sb._GATHER_SQL
    assert "NOW() - make_interval" in sql
    assert len(args) == 3               # no as-of parameter is bound


@pytest.mark.asyncio
async def test_the_as_of_pin_reads_the_head_fold_as_it_stood_at_that_instant():
    """The replay pin. ``superseded_by IS NULL`` is a statement about the
    substrate NOW; every past head has since been superseded, so a replay that
    kept it would read an empty country and look like a code defect. The pinned
    query asks the correct question instead — was this row a head AT the pin —
    and bounds the window above so the replay sees what the card saw."""
    conn = _TwoQueryConn([], [])
    pin = "2026-08-25T04:40:00+00:00"
    await sb.gather_and_band(_TwoQueryPool(conn), "target:usa", as_of=pin)
    sql, args = conn.calls[0]
    assert sql == sb._GATHER_SQL_AS_OF
    assert "f.superseded_at > $4" in sql
    assert "f.produced_at <= $4" in sql
    assert "NOW()" not in sql
    assert args[3] == pin


def test_consumed_ids_tolerates_a_row_without_the_lineage_column():
    """A caller on an older projection reports "we did not look" (which the
    alignment layer renders as ``not-consumed``) rather than a divergence it
    never checked for."""
    assert sb._consumed_ids({"finding_id": "x"}) == []
    assert sb._consumed_ids({"derived_from": None}) == []
    assert sb._consumed_ids({"derived_from": ["not-a-uuid"]}) == []
    good = str(uuid4())
    assert sb._consumed_ids({"derived_from": [good]}) == [good]
    assert sb._consumed_ids({"derived_from": f'["{good}"]'}) == [good]
