# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exemplar-shelf loader + id-resolver tests (W1-E cull, decision D-5).

Two halves:

  * **Schema half** — runs everywhere, against the tracked schema doc
    ``seeds/exemplar_shelf.example.yaml`` plus inline malformed shelves. Every
    integrity rule that keeps ids stable and non-dangling fails LOUD.
  * **Curated half** — runs only where the gitignored curated shelf
    ``seeds/exemplar_shelf.yaml`` is present (skipped in a public clone, the
    ``test_seed_sipri`` precedent). Pins the cull itself: 20 offered, the two
    merged ids resolving to their survivors, the two killed ids resolving to
    retired records, and NONE of the four offered.

The load-bearing assertion, stated once: a culled id must **resolve** and must
**not be offered**. A dangling id (resolves to nothing) and a zombie id (still
offered) are the two failures the cull's aliases/retired machinery exists to
prevent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from legba.data.seed.exemplar_shelf import (
    DEFAULT_YAML,
    RESOLVED_ACTIVE,
    RESOLVED_MERGED,
    RESOLVED_RETIRED,
    STATUS_HOLD,
    ExemplarShelfError,
    load_shelf,
    parse_shelf,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_YAML = REPO_ROOT / "seeds" / "exemplar_shelf.example.yaml"
CURATED_YAML = REPO_ROOT / "seeds" / "exemplar_shelf.yaml"

#: The cull (EXEMPLAR_CULL_RECOMMENDATION_2026-08-03 / roadmap D-5): 20 keep,
#: 2 merge, 2 kill. Merged id -> the survivor it folded into.
MERGED_INTO = {
    "exercise_as_cover": "prewar_mobilization_warning",
    "passportization": "protection_of_nationals",
}
#: Killed outright — resolvable through ``retired``, never offered.
KILLED = ("hybrid_composite", "hostage_diplomacy")
#: Every id the cull removed from the active shelf.
CULLED_IDS = (*MERGED_INTO, *KILLED)

#: 24 drafted slots − 2 merged (4, 21) − 2 killed (22, 24). Slot 19 carries
#: TWO sub-patterns (19a/19b, which curation forbids merging with each other),
#: so the shelf offers 21 patterns across 20 slots — "20 keep" counts slots.
KEPT_SLOT_COUNT = 20
OFFERED_COUNT = 21
RETIRED_COUNT = 2
#: #12 export_control_minerals_cascade waits on a stalled-negative instance.
HELD_IDS = ("export_control_minerals_cascade",)


def _minimal_pattern(**overrides):
    row = {
        "id": "pattern_one",
        "slot": "1",
        "title": "Pattern one",
        "doctrine_source": "Example doctrine",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Schema half — the tracked example doc and the loud-failure rules
# ---------------------------------------------------------------------------


def test_example_yaml_parses_and_exercises_every_route():
    """The tracked schema doc covers active / hold / merged / retired."""
    shelf = load_shelf(EXAMPLE_YAML)

    assert shelf.version == 1
    assert shelf.offered_ids() == (
        "example_standing_pattern",
        "example_absorbing_pattern",
        "example_gated_pattern",
    )
    assert shelf.held_ids() == ("example_gated_pattern",)
    assert shelf.pattern("example_gated_pattern").authoring_gate
    assert shelf.retired_ids() == ("example_retired_pattern",)
    # The declared cull arithmetic is verified against the rows on load, so
    # parsing at all proves the example's own counts are honest.
    assert shelf.cull["kept_slots"] == len(shelf.kept_slots()) == 3


def test_example_merged_id_resolves_to_its_survivor_and_is_not_offered():
    shelf = load_shelf(EXAMPLE_YAML)

    resolution = shelf.resolve("example_merged_pattern")
    assert resolution is not None
    assert resolution.status == RESOLVED_MERGED
    assert resolution.offered is False
    assert resolution.pattern_id == "example_absorbing_pattern"
    assert resolution.variant is not None
    assert resolution.variant["id"] == "example_variant"
    assert resolution.reason  # the merge rationale travels with the id
    assert "example_merged_pattern" not in shelf.offered_ids()


def test_example_retired_id_resolves_to_a_reason_and_is_not_offered():
    shelf = load_shelf(EXAMPLE_YAML)

    resolution = shelf.resolve("example_retired_pattern")
    assert resolution is not None
    assert resolution.status == RESOLVED_RETIRED
    assert resolution.offered is False
    assert resolution.pattern_id is None
    assert resolution.retired.reason
    assert resolution.retired.disposition == "killed"
    assert "example_retired_pattern" not in shelf.offered_ids()


def test_active_id_resolves_as_offered_and_unknown_id_is_none():
    shelf = load_shelf(EXAMPLE_YAML)

    active = shelf.resolve("example_standing_pattern")
    assert active.status == RESOLVED_ACTIVE
    assert active.offered is True
    assert active.pattern_id == "example_standing_pattern"

    assert shelf.resolve("no_such_pattern_anywhere") is None


def test_missing_file_degrades_to_an_empty_shelf(tmp_path, caplog):
    """House seed rule: absent curated DATA warns, never crashes, and never
    invents patterns."""
    shelf = load_shelf(tmp_path / "not_here.yaml")

    assert shelf.offered_ids() == ()
    assert shelf.retired_ids() == ()
    assert shelf.resolve("anything") is None


def test_default_path_points_at_the_gitignored_curated_shelf():
    assert DEFAULT_YAML.name == "exemplar_shelf.yaml"
    assert DEFAULT_YAML.parent.name == "seeds"


@pytest.mark.parametrize(
    ("shelf_doc", "expected"),
    [
        pytest.param(
            {"version": 99, "patterns": []},
            "unsupported shelf version",
            id="version",
        ),
        pytest.param({"version": 1}, "no 'patterns' key", id="no-patterns"),
        pytest.param(
            {"version": 1, "patterns": [_minimal_pattern(), _minimal_pattern()]},
            "duplicate pattern id",
            id="duplicate-id",
        ),
        pytest.param(
            {
                "version": 1,
                "patterns": [
                    _minimal_pattern(),
                    _minimal_pattern(id="pattern_two"),
                ],
            },
            "duplicate slot",
            id="duplicate-slot",
        ),
        pytest.param(
            {
                "version": 1,
                "patterns": [_minimal_pattern(status=STATUS_HOLD)],
            },
            "requires authoring_gate",
            id="hold-without-gate",
        ),
        pytest.param(
            {
                "version": 1,
                "patterns": [
                    _minimal_pattern(merged_from=[{"id": "gone_pattern", "slot": "9"}])
                ],
            },
            "is not in aliases",
            id="merged-id-not-aliased",
        ),
        pytest.param(
            {
                "version": 1,
                "patterns": [
                    _minimal_pattern(aliases=["shared_alias"]),
                    _minimal_pattern(
                        id="pattern_two", slot="2", aliases=["shared_alias"]
                    ),
                ],
            },
            "claimed by both",
            id="alias-collision",
        ),
        pytest.param(
            {
                "version": 1,
                "patterns": [_minimal_pattern(aliases=["pattern_one"])],
            },
            "collides with an active pattern id",
            id="alias-shadows-active-id",
        ),
        pytest.param(
            {
                "version": 1,
                "patterns": [_minimal_pattern()],
                "retired": [
                    {
                        "id": "pattern_one",
                        "slot": "7",
                        "title": "T",
                        "disposition": "killed",
                        "reason": "r",
                    }
                ],
            },
            "is also an active pattern",
            id="retired-and-active",
        ),
        pytest.param(
            {
                "version": 1,
                "patterns": [_minimal_pattern(aliases=["folded_pattern"])],
                "retired": [
                    {
                        "id": "folded_pattern",
                        "slot": "7",
                        "title": "T",
                        "disposition": "killed",
                        "reason": "r",
                    }
                ],
            },
            "merged or retired, never both",
            id="retired-and-aliased",
        ),
        pytest.param(
            {
                "version": 1,
                "patterns": [_minimal_pattern()],
                "retired": [
                    {
                        "id": "gone_pattern",
                        "slot": "1",
                        "title": "T",
                        "disposition": "killed",
                        "reason": "r",
                    }
                ],
            },
            "slots are never reused",
            id="retired-slot-reused",
        ),
        pytest.param(
            {
                "version": 1,
                "patterns": [_minimal_pattern(must_stay_split_from=["absent_pattern"])],
            },
            "not an active pattern",
            id="split-target-missing",
        ),
        pytest.param(
            {"version": 1, "patterns": [{"slot": "1", "title": "T"}]},
            "missing required field 'id'",
            id="missing-id",
        ),
        pytest.param(
            {
                "version": 1,
                "cull": {"offered_patterns": 7},
                "patterns": [_minimal_pattern()],
            },
            "cull.offered_patterns says 7 but the shelf has 1",
            id="cull-arithmetic-disagrees",
        ),
    ],
)
def test_malformed_shelf_fails_loud(shelf_doc, expected):
    with pytest.raises(ExemplarShelfError) as excinfo:
        parse_shelf(shelf_doc)
    assert expected in str(excinfo.value)


# ---------------------------------------------------------------------------
# Curated half — the cull itself (skipped where the curated data is absent)
# ---------------------------------------------------------------------------

curated_only = pytest.mark.skipif(
    not CURATED_YAML.exists(),
    reason="curated shelf not bundled (operator-provided); see seeds/README.md",
)


@curated_only
def test_curated_shelf_carries_the_culled_twenty():
    shelf = load_shelf(CURATED_YAML)

    assert len(shelf.kept_slots()) == KEPT_SLOT_COUNT
    assert len(shelf.offered_ids()) == OFFERED_COUNT
    assert len(shelf.retired_ids()) == RETIRED_COUNT
    assert shelf.held_ids() == HELD_IDS
    assert len(shelf.ready_ids()) == OFFERED_COUNT - len(HELD_IDS)
    # A held pattern says what unblocks it — a hold is never a quiet drop.
    assert shelf.pattern(HELD_IDS[0]).authoring_gate
    # The shelf's own stated arithmetic (checked against the rows on load).
    assert shelf.cull["kept_slots"] == KEPT_SLOT_COUNT
    assert shelf.cull["merged_slots"] == len(MERGED_INTO)
    assert shelf.cull["killed_slots"] == len(KILLED)


@curated_only
@pytest.mark.parametrize("culled_id", CULLED_IDS)
def test_every_culled_id_resolves_and_is_not_offered(culled_id):
    """THE cull invariant: resolvable, never offered."""
    shelf = load_shelf(CURATED_YAML)

    resolution = shelf.resolve(culled_id)
    assert resolution is not None, f"{culled_id} dangles — it resolves to nothing"
    assert resolution.offered is False
    assert culled_id not in shelf.offered_ids()
    assert shelf.is_offered(culled_id) is False
    assert resolution.reason, "a culled id carries the cull's own reason"


@curated_only
@pytest.mark.parametrize(("merged_id", "survivor_id"), sorted(MERGED_INTO.items()))
def test_merged_ids_land_on_their_survivor_as_a_named_variant(merged_id, survivor_id):
    shelf = load_shelf(CURATED_YAML)

    resolution = shelf.resolve(merged_id)
    assert resolution.status == RESOLVED_MERGED
    assert resolution.pattern_id == survivor_id

    variant = resolution.variant
    assert variant is not None, "a merge folds in a NAMED variant, not just an alias"
    assert variant.get("framing"), "the variant carries its distinctive framing"
    assert variant.get("distinctive_indicators"), "…and its distinctive indicators"
    assert variant.get("scoring_rule"), "…and how it is scored against the parent"


@curated_only
@pytest.mark.parametrize("killed_id", KILLED)
def test_killed_ids_resolve_to_a_retired_record_with_a_quoted_reason(killed_id):
    shelf = load_shelf(CURATED_YAML)

    resolution = shelf.resolve(killed_id)
    assert resolution.status == RESOLVED_RETIRED
    assert resolution.pattern_id is None
    assert resolution.retired.disposition == "killed"
    assert len(resolution.retired.reason) > 80, "the cull's reason, quoted"
    assert resolution.retired.reversal, "a kill states how it is reversed"


@curated_only
def test_slots_are_stable_and_the_cull_left_holes():
    """Ids are stable: culling removes slots, it never renumbers the rest."""
    shelf = load_shelf(CURATED_YAML)

    active_slots = {p.slot for p in shelf.patterns}
    retired_slots = {r.slot for r in shelf.retired}

    # The merged slots (4, 21) and the killed slots (22, 24) are gone from the
    # active shelf; the surviving numbers did NOT shift down to close the gaps.
    assert {"4", "21", "22", "24"}.isdisjoint(active_slots)
    assert retired_slots == {"22", "24"}
    assert {"1", "2", "3", "23"} <= active_slots
    assert max(int(s) for s in active_slots if s.isdigit()) == 23


@curated_only
def test_the_two_that_must_stay_split_are_still_split():
    """19a/19b have opposite base rates — the draft forbids merging them."""
    shelf = load_shelf(CURATED_YAML)

    assert shelf.is_offered("evacuation_drawdown_cascade")
    assert shelf.is_offered("expulsion_tit_for_tat")
    assert shelf.pattern("evacuation_drawdown_cascade").must_stay_split_from == (
        "expulsion_tit_for_tat",
    )
    assert shelf.pattern("expulsion_tit_for_tat").must_stay_split_from == (
        "evacuation_drawdown_cascade",
    )


@curated_only
def test_curated_shelf_is_the_index_stage_and_names_its_provenance():
    raw = yaml.safe_load(CURATED_YAML.read_text(encoding="utf-8"))
    shelf = parse_shelf(raw, source_path=CURATED_YAML)

    assert shelf.stage == "culled", "threads are authored in a later pass"
    assert shelf.decision, "the shelf names the decision that culled it"
    assert shelf.sources.get("recommendation")
    # Every offered pattern names where it comes from — the shelf's own
    # "doctrine-derived, not invented" build rule.
    assert all(p.doctrine_source for p in shelf.patterns)
