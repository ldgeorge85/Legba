# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The three INTEGRITY gauges (R4 + the judge alarm, R-train 2026-08-05).

judge_availability exists because of a specific 26-hour silence: the judge
component returned ``402 payment_required`` from 2026-08-03 19:49Z, every
critique written meanwhile carried a floor-only verdict, fleet mean faithfulness
dropped 0.21, an acceptance panel could measure nothing, and no gauge anywhere
went red. Analysts kept running, so no PRODUCTION loop noticed — the deficit was
in the grader.

descriptor_prompt_drift exists because a bounded unit's system prompt is a
registry DB row, not a tracked file, so a fix can be correct in the tree and
wrong in production indefinitely.

descriptor_state_drift is the same disease one level up, and it was measured:
of 157 descriptors present in both tree and registry, 76 disagree on state, and
68 are `draft` in the tree while `active` live. Any bringup_register_* run
against those would take sixty-eight running descriptors OFF-LINE.

The load-bearing assertion in this file is the FIRST one: a total outage must
land ``critical``. Everything else is calibration around it.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from gen_descriptor_prompt_manifest import prompt_hash  # noqa: E402

from legba.data.analysts.handler_options import HANDLER_OPTIONS
from legba.data.registry.production_gauge import (
    LOOP_CLASSES,
    LOOP_DESCRIPTOR_PROMPT_DRIFT,
    LOOP_DESCRIPTOR_STATE_DRIFT,
    LOOP_JUDGE_AVAILABILITY,
    GaugeConfig,
)
from legba.data.registry.production_gauge_integrity import (
    MANIFEST_PATH,
    descriptor_drift_gauge,
    descriptor_state_gauge,
    judge_availability_gauge,
    load_prompt_manifest,
    load_state_manifest,
    read_descriptor_drift_loops,
    read_descriptor_state_loops,
    read_judge_loops,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
CFG = GaugeConfig()


# ---------------------------------------------------------------------------
# judge_availability
# ---------------------------------------------------------------------------


def test_total_outage_is_critical_and_pages():
    """THE assertion. The one condition that silently invalidates every
    faithfulness number in the substrate must page at the top of the ladder."""
    gauge = judge_availability_gauge(
        {"critiques": 611, "judged": 0, "judge_wired": 611, "last_judged_at": None},
        now=NOW,
        cfg=CFG,
    )
    assert gauge.state == "deficit"
    assert gauge.severity == "critical"
    assert gauge.pages is True
    assert gauge.evidence["unjudged"] == 611
    assert "PROVISIONAL" in gauge.actual


def test_healthy_judge_is_ok_and_silent():
    gauge = judge_availability_gauge(
        {"critiques": 100, "judged": 95, "judge_wired": 100}, now=NOW, cfg=CFG
    )
    assert gauge.state == "ok"
    assert gauge.pages is False
    assert gauge.ratio == 0.0


def test_partial_degradation_is_visible_without_paging():
    """A judge that is up but soft-failing on some calls is a number worth
    reading, not an interruption worth having."""
    gauge = judge_availability_gauge(
        {"critiques": 3264, "judged": 2372, "judge_wired": 3264}, now=NOW, cfg=CFG
    )
    assert gauge.state == "deficit"
    assert gauge.pages is False  # below ALERT_MIN_SEVERITY
    assert gauge.evidence["unjudged"] == 892


def test_severity_climbs_with_the_shortfall():
    seen = []
    for judged in (80, 70, 60, 50, 0):
        g = judge_availability_gauge(
            {"critiques": 100, "judged": judged, "judge_wired": 100},
            now=NOW,
            cfg=CFG,
        )
        seen.append(g.severity)
    # Monotone non-decreasing severity as the adjudicated share falls.
    ladder = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    assert [ladder[s] for s in seen] == sorted(ladder[s] for s in seen)
    assert seen[-1] == "critical"


def test_no_judge_configured_is_quiet_not_a_deficit():
    """Floor-only is a legitimate configuration, not a failure. Gauged and
    visible; never paged."""
    gauge = judge_availability_gauge(
        {"critiques": 50, "judged": 0, "judge_wired": 0}, now=NOW, cfg=CFG
    )
    assert gauge.state == "ungauged"
    assert gauge.quiet_reason == "judge_never_configured"
    assert gauge.pages is False


def test_no_critiques_defers_to_the_production_loops():
    """One condition must not page under two names."""
    gauge = judge_availability_gauge(
        {"critiques": 0, "judged": 0, "judge_wired": 0}, now=NOW, cfg=CFG
    )
    assert gauge.state == "ungauged"
    assert gauge.quiet_reason == "no_critiques_in_window"


async def test_judge_read_degrades_loud_on_a_query_failure():
    """A failed read must never render as a healthy judge."""

    class _Boom:
        async def fetchrow(self, *a, **k):
            raise RuntimeError("connection reset")

    loops = await read_judge_loops(_Boom(), now=NOW, cfg=CFG)
    assert len(loops) == 1
    assert loops[0].state == "ungauged"
    assert loops[0].quiet_reason == "judge_query_failed"
    assert "connection reset" in loops[0].evidence["error"]


# ---------------------------------------------------------------------------
# descriptor_prompt_drift (R4)
# ---------------------------------------------------------------------------

#: The tree side, built through the SHIPPING generator rather than a hand-written
#: hash — if the two hashers ever drift apart, that is the defect this whole loop
#: would otherwise report as fleet-wide prompt drift, forever.
_TREE_PROMPT = "You are an escalation analyst."
_MANIFEST = {
    "escalation": {"sha256": prompt_hash(_TREE_PROMPT), "chars": len(_TREE_PROMPT)}
}


def _row(desc_id: str, prompt: str) -> dict:
    return {"descriptor_id": desc_id, "state": "active", "system_prompt": prompt}


def test_matching_prompt_is_ok():
    gauge = descriptor_drift_gauge(
        [_row("escalation", _TREE_PROMPT)], _MANIFEST, now=NOW, cfg=CFG
    )
    assert gauge.state == "ok"
    assert gauge.pages is False
    assert gauge.evidence["matched"] == 1


def test_a_changed_word_is_drift():
    """The comparison is real, not a rubber stamp."""
    gauge = descriptor_drift_gauge(
        [_row("escalation", "You are a de-escalation analyst.")],
        _MANIFEST,
        now=NOW,
        cfg=CFG,
    )
    assert gauge.state == "deficit"


def test_trailing_whitespace_is_not_drift():
    """A YAML block scalar and a JSON round-trip through the registry disagree
    about trailing whitespace in ways nobody meant as a prompt change. An
    un-normalized hash would report drift on everything forever, which is the
    same as reporting it on nothing."""
    gauge = descriptor_drift_gauge(
        [_row("escalation", _TREE_PROMPT + "   \n\n\n")],
        _MANIFEST,
        now=NOW,
        cfg=CFG,
    )
    assert gauge.state == "ok"


def test_divergence_pages_on_the_first_one():
    gauge = descriptor_drift_gauge(
        [_row("escalation", "You are a COMPLETELY different analyst now.")],
        _MANIFEST,
        now=NOW,
        cfg=CFG,
    )
    assert gauge.state == "deficit"
    assert gauge.pages is True
    assert gauge.evidence["diverged"] == ["escalation"]


def test_untracked_live_prompt_is_reported_separately():
    """Worse than divergence: there is nothing to diff against, so the live
    analytic method is unreviewable."""
    gauge = descriptor_drift_gauge(
        [_row("ghost_unit", "invented in production")], _MANIFEST, now=NOW, cfg=CFG
    )
    assert gauge.state == "deficit"
    assert gauge.evidence["untracked"] == ["ghost_unit"]
    assert gauge.evidence["diverged"] == []


def test_a_tracked_prompt_with_no_live_row_is_not_a_deficit():
    """An un-deployed or paused unit is the cadence loops' business."""
    gauge = descriptor_drift_gauge(
        [], _MANIFEST, now=NOW, cfg=CFG
    )
    assert gauge.state == "ungauged"
    assert gauge.quiet_reason == "no_live_descriptor_prompts"


def test_missing_manifest_is_ungauged_not_clean():
    """A missing manifest and a matching one are not the same fact."""
    gauge = descriptor_drift_gauge(
        [_row("escalation", "x")], {}, now=NOW, cfg=CFG
    )
    assert gauge.state == "ungauged"
    assert gauge.quiet_reason == "prompt_manifest_unavailable"


async def test_drift_read_degrades_loud():
    class _Boom:
        async def fetch(self, *a, **k):
            raise RuntimeError("relation does not exist")

    loops = await read_descriptor_drift_loops(_Boom(), now=NOW, cfg=CFG)
    assert loops[0].state == "ungauged"
    assert loops[0].quiet_reason == "drift_query_failed"


# ---------------------------------------------------------------------------
# descriptor_state_drift (R4, extended) — the DEACTIVATION HAZARD
# ---------------------------------------------------------------------------

_STATE_MANIFEST = {
    "source:source.a": {"state": "draft"},
    "source:source.b": {"state": "active"},
    "analyst:escalation": {"state": "active"},
}


def _srow(family: str, desc_id: str, state: str) -> dict:
    return {"family": family, "descriptor_id": desc_id, "state": state}


def test_tree_draft_over_live_active_is_the_expected_promotion():
    """OPERATOR POLICY 2026-08-11: the tree ships descriptors state:draft by
    design and activation is a live act — draft→live is the designed lifecycle,
    not drift. It reads OK, and the row stays listed as the do-not-re-register
    set for bringup_register_* scripts."""
    gauge = descriptor_state_gauge(
        [_srow("source", "source.a", "active")], _STATE_MANIFEST, now=NOW, cfg=CFG
    )
    assert gauge.state == "ok"
    assert gauge.evidence["expected_promotions"] == [
        "source:source.a tree=draft live=active"
    ]
    assert "expected draft→live promotions" in gauge.actual


def test_agreeing_states_are_ok():
    gauge = descriptor_state_gauge(
        [_srow("source", "source.b", "active"), _srow("analyst", "escalation", "active")],
        _STATE_MANIFEST,
        now=NOW,
        cfg=CFG,
    )
    assert gauge.state == "ok"
    assert gauge.evidence["matched"] == 2


def test_ordinary_divergence_is_reported_but_does_not_escalate():
    """An operator pausing a source on purpose must not be able to push this loop
    to critical and train everyone to ignore it."""
    gauge = descriptor_state_gauge(
        [_srow("source", "source.b", "paused")], _STATE_MANIFEST, now=NOW, cfg=CFG
    )
    assert gauge.evidence["diverged"] == ["source:source.b tree=active live=paused"]
    assert gauge.evidence["deactivation_hazard"] == []
    assert gauge.state == "ok"
    assert gauge.pages is False


def test_hazard_severity_scales_and_a_fleet_wide_one_is_critical():
    """Tree RETIRED + live running is the hazard that survives the 08-11
    policy: the tree declaring a running descriptor dead is a disagreement
    someone must resolve, and a fleet of them is critical."""
    rows = [_srow("source", f"s{i}", "active") for i in range(20)]
    manifest = {f"source:s{i}": {"state": "retired"} for i in range(20)}
    gauge = descriptor_state_gauge(rows, manifest, now=NOW, cfg=CFG)
    assert gauge.severity == "critical"
    assert gauge.pages is True
    assert len(gauge.evidence["deactivation_hazard"]) == 20


def test_a_fleet_of_draft_promotions_never_pages():
    """The same 20 rows under tree=draft — the exact shape that paged CRITICAL
    for a week — is the designed lifecycle under the 08-11 policy."""
    rows = [_srow("source", f"s{i}", "active") for i in range(20)]
    manifest = {f"source:s{i}": {"state": "draft"} for i in range(20)}
    gauge = descriptor_state_gauge(rows, manifest, now=NOW, cfg=CFG)
    assert gauge.state == "ok"
    assert gauge.pages is False
    assert len(gauge.evidence["expected_promotions"]) == 20


def test_family_scoping_prevents_a_name_collision():
    """A source and an analyst may share a bare id; the manifest is keyed
    ``<family>:<id>`` so one can never be graded against the other's state."""
    gauge = descriptor_state_gauge(
        [_srow("analyst", "source.a", "active")], _STATE_MANIFEST, now=NOW, cfg=CFG
    )
    # analyst:source.a is not in the manifest -> live-only, so ZERO comparisons
    # were made, which is "we cannot say" and never "it is fine".
    assert gauge.state == "ungauged"
    assert gauge.quiet_reason == "no_copresent_descriptors"


def test_live_only_and_tree_only_are_not_drift():
    """Live-only is a registration the tree never carried; tree-only is an
    un-deployed file, i.e. the normal state of a repo."""
    gauge = descriptor_state_gauge(
        [_srow("source", "source.never_in_tree", "active")],
        _STATE_MANIFEST,
        now=NOW,
        cfg=CFG,
    )
    assert gauge.state == "ungauged"
    assert gauge.quiet_reason == "no_copresent_descriptors"


def test_state_missing_manifest_is_ungauged():
    gauge = descriptor_state_gauge(
        [_srow("source", "source.a", "active")], {}, now=NOW, cfg=CFG
    )
    assert gauge.state == "ungauged"
    assert gauge.quiet_reason == "prompt_manifest_unavailable"


async def test_state_read_degrades_loud():
    class _Boom:
        async def fetch(self, *a, **k):
            raise RuntimeError("permission denied")

    loops = await read_descriptor_state_loops(_Boom(), now=NOW, cfg=CFG)
    assert loops[0].state == "ungauged"
    assert loops[0].quiet_reason == "state_drift_query_failed"


def test_state_manifest_covers_every_family():
    """Prompt drift only concerns analysts; STATE drift concerns sources most of
    all — that is where the 68 hazard rows live."""
    states = load_state_manifest()
    assert states
    families = {k.split(":", 1)[0] for k in states}
    assert {"analyst", "source", "action_pack"} <= families


# ---------------------------------------------------------------------------
# The manifest itself, and the wiring
# ---------------------------------------------------------------------------


def test_shipped_manifest_matches_the_descriptor_tree():
    """THE staleness guard.

    The manifest is a build output checked into the tree, so it can go stale, so
    the gauge could quietly compare against a fiction — the exact disease R4
    exists to cure. Editing a descriptor's ``method.system_prompt`` without
    regenerating turns this red.
    """
    result = subprocess.run(
        [sys.executable, "scripts/gen_descriptor_prompt_manifest.py", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"{result.stdout}{result.stderr}\n"
        "Run: python3 scripts/gen_descriptor_prompt_manifest.py"
    )


def test_manifest_is_loadable_and_non_empty():
    manifest = load_prompt_manifest()
    assert manifest, "the shipped manifest must carry the tree's inline prompts"
    for desc_id, entry in manifest.items():
        assert isinstance(desc_id, str) and desc_id
        assert len(entry["sha256"]) == 64
        assert entry["chars"] > 0


def test_manifest_ships_inside_the_package():
    """``descriptors/`` is in neither container image. If the manifest is not
    under ``src/legba`` the gauge is dead in production and green in CI, which is
    the worst possible arrangement."""
    assert MANIFEST_PATH.exists()
    assert MANIFEST_PATH.is_relative_to(REPO_ROOT / "src" / "legba")
    json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_every_integrity_loop_is_registered_in_the_enumeration():
    assert LOOP_JUDGE_AVAILABILITY in LOOP_CLASSES
    assert LOOP_DESCRIPTOR_PROMPT_DRIFT in LOOP_CLASSES
    assert LOOP_DESCRIPTOR_STATE_DRIFT in LOOP_CLASSES


@pytest.mark.parametrize(
    "field",
    [
        "judge_window_days",
        "judge_min_adjudicated_share",
        "judge_share_tolerance",
        "drift_severity_divisor",
        "state_drift_severity_divisor",
    ],
)
def test_every_new_threshold_is_an_operator_knob(field):
    """A GaugeConfig field with no matching ``gauge_``-prefixed OptionSpec is
    silently un-tunable: a descriptor setting it is REJECTED."""
    assert hasattr(GaugeConfig(), field)
    names = {spec.name for spec in HANDLER_OPTIONS["alert_trigger_scan"]}
    assert f"gauge_{field}" in names
