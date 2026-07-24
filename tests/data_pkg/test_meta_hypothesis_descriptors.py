# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Piece 3 — the new analyst descriptors validate + the bringup wires them.

Covers Tasks B / C / D:
  * analyst_meta_synthesizer.yaml      (meta_findings_synthesizer)
  * analyst_cross_correlator.yaml      (cross_analyst_correlator)
  * analyst_hypothesis_lifecycle.yaml  (deterministic / hypothesis_lifecycle)

Each must:
  1. validate against the REAL AnalystDescriptor pydantic schema (via the exact
     bringup ``_load`` path — this is the gate the registry runs);
  2. be present in scripts/bringup_register_analysts.ANALYST_FILES so it
     registers on bringup;
  3. carry an identity.kind the runtime can actually dispatch.

Also covers the Task-A3 source-analyst resolution CONTRACT: the resolution the
actor performs when injecting ``options['source_analyst_ids']`` for the two meta
kinds is the same ``subscription.other_analysts[].id`` read the kinds' READ_SLICE
uses — so a descriptor's declared sources resolve identically on both paths.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from legba.data.schemas.analyst import AnalystDescriptor

_DESCRIPTORS_DIR = pathlib.Path(__file__).resolve().parents[2] / "descriptors"

# Descriptors that must validate AND be in the bringup ANALYST_FILES set.
_NEW_FILES = {
    # The facts-table maintenance sweep — same deterministic-sub_handler pattern;
    # wires the built-but-uninvoked fact_decay handler.
    "analyst_fact_decay.yaml": ("fact_decay", "deterministic"),
    # PIECE C — the ACH competing-hypotheses producer + the Brier-calibration
    # feedback loop.
    "analyst_competing_hypotheses.yaml": ("competing_hypotheses", "competing_hypotheses"),
    "analyst_calibration_tracking.yaml": ("calibration_tracking", "deterministic"),
}

# RETIRED from bringup: descriptor files KEPT on disk (they must still validate
# so the handler/leg can be re-enabled) but intentionally NOT in ANALYST_FILES.
#   * hypothesis_lifecycle (PIECE C): the situation-gated producer is SUPERSEDED
#     by the competing_hypotheses ACH kind (it emitted 0 rows — gated on active
#     situations that go dormant); left out so it isn't a duplicate forward-claim
#     producer.
#   * meta_synthesizer (2026-07-02): the LEGACY standalone cross-analyst
#     synthesizer is SUPERSEDED by the composition spine (country -> region ->
#     escalation -> world) and reads the now-retired country_assessor as input →
#     retired live; left out so a fresh deploy cannot re-create it over a dead
#     input. See test_meta_synthesizer_subscription_sources for the file contract.
#   * cross_correlator (2026-07-09, Piece 3 Task C): drifted into a coverage-gap
#     detector reading RETIRED analyst outputs (0 downstream consumers, emitted
#     a live "score 0.00" faithfulness head) — commented out of ANALYST_FILES in
#     scripts/bringup_register_analysts.py. The descriptor keeps receiving edits
#     post-retirement (its other_analysts list has since grown to 10 entries,
#     entirely different from the 3-entry set this file used to compare it
#     against) — TEST_DEBT_RECON.md §3 flags this as an open question (dead vs.
#     being prepped for reactivation) for whoever's been editing the YAML; this
#     file only asserts it still validates, per the retired-but-kept-on-disk
#     contract, and does not presume either way.
_RETIRED_BUT_VALIDATES = {
    "analyst_hypothesis_lifecycle.yaml": ("hypothesis_lifecycle", "deterministic"),
    "analyst_meta_synthesizer.yaml": ("meta_synthesizer", "meta_findings_synthesizer"),
    "analyst_cross_correlator.yaml": ("cross_correlator", "cross_analyst_correlator"),
}


def _load(name: str) -> AnalystDescriptor:
    """Exact mirror of scripts/bringup_register_analysts._load."""
    body = yaml.safe_load((_DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return AnalystDescriptor.model_validate(body, strict=False)


@pytest.mark.parametrize(
    "name,expected",
    sorted({**_NEW_FILES, **_RETIRED_BUT_VALIDATES}.items()),
)
def test_new_descriptor_validates(name: str, expected: tuple[str, str]):
    """Every descriptor (registered OR retired-but-kept) must still validate
    against the real schema — the retired one is kept on disk so it can be
    re-enabled as a feeder."""
    exp_id, exp_kind = expected
    desc = _load(name)
    assert desc.identity.id == exp_id
    assert desc.identity.kind == exp_kind


def _bringup_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_bringup_register_analysts",
        pathlib.Path(__file__).resolve().parents[2]
        / "scripts"
        / "bringup_register_analysts.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_new_descriptors_in_bringup_set():
    """The registered descriptors must be in ANALYST_FILES so bringup registers
    them (the predictor precedent — without this they register against 0 rows)."""
    mod = _bringup_module()
    for name in _NEW_FILES:
        assert name in mod.ANALYST_FILES, f"{name} missing from bringup ANALYST_FILES"


def test_retired_hypothesis_lifecycle_not_in_bringup():
    """Retired-from-bringup disposition: hypothesis_lifecycle (SUPERSEDED by
    competing_hypotheses) and meta_synthesizer (SUPERSEDED by the composition
    spine; reads the retired country_assessor) are kept on disk for re-enable but
    must NOT be registered — else a fresh deploy resurrects a dead producer.
    """
    mod = _bringup_module()
    for name in _RETIRED_BUT_VALIDATES:
        assert name not in mod.ANALYST_FILES, (
            f"{name} is retired and must NOT be in bringup ANALYST_FILES"
        )


def test_meta_synthesizer_subscription_sources():
    """The synthesizer reads country_assessor + world_assessor findings."""
    desc = _load("analyst_meta_synthesizer.yaml")
    ids = [a.id for a in desc.subscription.other_analysts]
    assert ids == ["country_assessor", "world_assessor"]
    # META analyst → no targets block (single global run per tick).
    assert desc.subscription.targets is None


# test_cross_correlator_widens_input_set_and_staggers_cadence was REMOVED
# 2026-07-23 (TEST_DEBT_RECON.md Bucket A): it asserted a subscription-widening
# CONTRACT between meta_synthesizer and cross_correlator (correlator strictly
# wider, incl. country_predictor) that predates cross_correlator's 2026-07-09
# retirement from bringup. The live descriptor's other_analysts list was
# rewritten post-retirement to a 10-entry set that shares no overlap with the
# old 3-entry comparison set at all — cross_correlator is retired-but-kept-on-
# disk (see _RETIRED_BUT_VALIDATES above) and is being edited as dead/parked
# content, not live-synced against meta_synthesizer. A test can't meaningfully
# gate a subscription contract against something not wired to anything; see
# TEST_DEBT_RECON.md §3 for the open "dead vs. reactivation-prep" question.


def test_competing_hypotheses_is_dispatchable_meta_kind():
    """PIECE C — the ACH kind routes through its own analyst kind (META: global
    sweep, direct substrate queries, no other_analysts) and the runtime can
    actually dispatch it (the kind is discovered + has a deps-builder branch)."""
    desc = _load("analyst_competing_hypotheses.yaml")
    assert desc.identity.kind == "competing_hypotheses"
    # META: global sweep, direct substrate queries, no targets/other_analysts.
    assert desc.subscription.targets is None
    assert desc.subscription.other_analysts == []
    # The kind is discoverable by the runtime registry (dispatch on identity.kind).
    from legba.data.analysts import discover_analyst_kinds

    registry = discover_analyst_kinds()
    assert "competing_hypotheses" in registry
    handler = registry["competing_hypotheses"]
    # "Findings as a real output type" cleanup: this META kind is TRACE_ONLY —
    # its REAL product is the HYPOTHESIS rows it side-writes via
    # write_hypothesis; the per-run summary is audited in analyst_traces, NOT
    # emitted as a redundant FINDING receipt in analyst_outputs.
    from legba.data.provenance.kinds import TRACE_ONLY

    assert handler.output_kind is TRACE_ONLY
    # The enum carries it (closed-enum dispatch parity).
    from legba.data.schemas.analyst import AnalystKind

    assert AnalystKind.COMPETING_HYPOTHESES.value == "competing_hypotheses"


def test_calibration_tracking_is_deterministic_sub_handler_and_staggered():
    """PIECE C — the Brier-calibration loop routes through the deterministic
    dispatcher (sub_handler=calibration_tracking), is a META global sweep, and is
    cadence-offset from the other deterministic maintenance analysts."""
    desc = _load("analyst_calibration_tracking.yaml")
    assert desc.identity.kind == "deterministic"
    assert desc.method.sub_handler == "calibration_tracking"
    assert desc.subscription.other_analysts == []
    from legba.data.analysts.deterministic import SUB_HANDLERS

    assert "calibration_tracking" in SUB_HANDLERS
    # Staggered off hypothesis_lifecycle + fact_decay so they don't co-fire.
    fd = _load("analyst_fact_decay.yaml")
    assert desc.cadence.fallback_schedule != fd.cadence.fallback_schedule


def test_hypothesis_lifecycle_is_deterministic_sub_handler():
    """The hypotheses producer routes through the deterministic dispatcher."""
    desc = _load("analyst_hypothesis_lifecycle.yaml")
    assert desc.identity.kind == "deterministic"
    assert desc.method.sub_handler == "hypothesis_lifecycle"
    # META: global sweep, direct substrate queries, no other_analysts.
    assert desc.subscription.other_analysts == []
    # The dispatch table must carry the sub-handler.
    from legba.data.analysts.deterministic import SUB_HANDLERS

    assert "hypothesis_lifecycle" in SUB_HANDLERS


def test_fact_decay_is_deterministic_sub_handler_and_staggered():
    """The facts-table decay sweep routes through the deterministic dispatcher
    (sub_handler=fact_decay), is a META global sweep, and is cadence-offset
    from the other deterministic maintenance analysts so they don't collide."""
    desc = _load("analyst_fact_decay.yaml")
    assert desc.identity.kind == "deterministic"
    assert desc.method.sub_handler == "fact_decay"
    assert desc.subscription.other_analysts == []
    # The dispatch table must carry the sub-handler (else it never fires).
    from legba.data.analysts.deterministic import SUB_HANDLERS

    assert "fact_decay" in SUB_HANDLERS
    # Cadence offset from the sibling maintenance analysts.
    hyp = _load("analyst_hypothesis_lifecycle.yaml")
    assert desc.cadence.fallback_schedule != hyp.cadence.fallback_schedule


@pytest.mark.parametrize(
    "name,expected_ids",
    [
        ("analyst_meta_synthesizer.yaml", ["country_assessor", "world_assessor"]),
        # analyst_cross_correlator.yaml case REMOVED 2026-07-23 (TEST_DEBT_RECON.md
        # Bucket A): hardcoded expected_ids=[country_assessor, world_assessor,
        # country_predictor] against a descriptor whose live other_analysts list
        # is now a 10-entry set post-retirement (see _RETIRED_BUT_VALIDATES above)
        # — same root cause as the removed
        # test_cross_correlator_widens_input_set_and_staggers_cadence.
    ],
)
def test_actor_source_analyst_injection_matches_descriptor(name, expected_ids):
    """Task A3 CONTRACT — the actor injects options['source_analyst_ids'] by
    reading subscription.other_analysts[].id. Assert that resolution (mirrored
    here exactly as in dapr_actors) yields the descriptor's declared sources,
    AND that it equals the kind's own READ_SLICE id resolution (one surface,
    two readers, no drift)."""
    from legba.data.analysts.meta_findings_synthesizer import _resolve_other_analyst_ids

    desc = _load(name)
    sub = desc.subscription
    others = sub.other_analysts or []
    injected = [str(a.id) for a in others if a.id]
    assert injected == expected_ids
    # The READ_SLICE resolver reads the SAME surface — must agree.
    assert _resolve_other_analyst_ids(desc) == expected_ids
