# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""VOICES faculty-lens tier (planning/VOICES_BUILD_DESIGN.md §5.2) — the fourth
and fifth ids on the journal_assessor kind.

Mirrors tests/data_pkg/test_chronicle_tier.py's approach. Locks the tier
plumbing:

  * ``_entry_kind_for_analyst`` distills the four faculty ids → ``'lens'`` and
    ``lens_diff`` → ``'lens_diff'`` (append tiers — the consolidation-supersession
    branch keys on ``'consolidation'`` and must never fire for them);
  * the lens user prompt carries the declared prior + collection-health-first +
    the citation mandate, and NONE of the diary's apparatus blocks — the
    entry/consolidation/chronicle render is unchanged;
  * the diff-matrix roster helper is a pure function (seen/missing correct on
    full + partial input, most-recent-per-id dedup);
  * the ``JournalPayload`` Literal admits the new kinds (the today's-chronicle
    stumble: an unwidened Literal rejected the new kind at validation);
  * the five descriptor YAMLs validate on the shared kind, declare verify, carry
    the staggered crons + journal_read-only grants;
  * ``get_lens_reads`` is registered in the pack (the four-surface guard).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from legba.data.analysts.journal_assessor import (
    CONSOLIDATOR_ANALYST_ID,
    CHRONICLE_ANALYST_ID,
    LENS_ANALYST_IDS,
    LENS_DIFF_ANALYST_ID,
    _entry_kind_for_analyst,
    _lens_diff_roster_from_reads,
    _render_user_prompt,
)

_REPO = Path(__file__).resolve().parents[2]

_ROWS = [
    {"id": "x1", "title": "Strikes hit the port", "source_id": "s1",
     "produced_at": "2026-07-21T00:00:00+00:00",
     "salience": {"magnitude": 0.95, "event_class": "escalation"}},
    {"id": "x2", "title": "Ceasefire talks stall", "source_id": "s2",
     "produced_at": "2026-07-21T01:00:00+00:00",
     "salience": {"magnitude": 0.4, "event_class": "other"}},
]

_LENS_DESCRIPTORS = {
    "lens_trend": "descriptors/analyst_lens_trend.yaml",
    "lens_baserate": "descriptors/analyst_lens_baserate.yaml",
    "lens_capability": "descriptors/analyst_lens_capability.yaml",
    "lens_intent": "descriptors/analyst_lens_intent.yaml",
    "lens_diff": "descriptors/analyst_lens_diff.yaml",
}

# The staggered crons (VOICES_BUILD_DESIGN §4.1) — all AFTER the chronicle's
# `0 6 * * 1`, 30-min spaced.
_EXPECTED_CRONS = {
    "lens_trend": "30 6 * * 1",
    "lens_baserate": "0 7 * * 1",
    "lens_capability": "30 7 * * 1",
    "lens_intent": "0 8 * * 1",
    "lens_diff": "30 8 * * 1",
}


def test_entry_kind_distills_all_tiers() -> None:
    # The pre-existing three tiers stay put.
    assert _entry_kind_for_analyst("journal_assessor") == "entry"
    assert _entry_kind_for_analyst(CONSOLIDATOR_ANALYST_ID) == "consolidation"
    assert _entry_kind_for_analyst(CHRONICLE_ANALYST_ID) == "chronicle"
    assert _entry_kind_for_analyst(None) == "entry"
    # All four faculties → 'lens'.
    for aid in LENS_ANALYST_IDS:
        assert _entry_kind_for_analyst(aid) == "lens", aid
    # The diff id → its OWN kind (not folded into 'lens').
    assert _entry_kind_for_analyst(LENS_DIFF_ANALYST_ID) == "lens_diff"


def test_lens_prompt_contains_prior_and_citation_mandate_not_diary() -> None:
    from legba.prompts.lens_trend import LENS_PRIOR_BLOCK

    out = _render_user_prompt(_ROWS, tier="lens", lens_prior_block=LENS_PRIOR_BLOCK)
    # the declared prior is echoed VERBATIM, and FIRST (before the slice header)
    assert "DECLARED PRIOR (lens_trend" in out
    assert out.index("DECLARED PRIOR") < out.index("recent signal slice")
    # the citation rule + the two j7 hardenings are present
    assert "[[ref:<uuid>]]" in out
    assert "COLLECTION HEALTH FIRST" in out
    assert "CONTESTED-SUBSTRATE GUARD" in out
    # the diary's apparatus contract must NOT leak into a lens read
    assert "the apparatus is your POSTSCRIPT" not in out
    assert "INSTRUMENT CITATIONS" not in out
    assert "[[instrument]]" not in out
    assert "DISTINCT wired sources" not in out
    # the slice rows render citable, salience-tagged (shared with every tier)
    assert "[[ref:x1]]" in out and "(salience 0.95" in out


def test_lens_diff_prompt_carries_aperture_and_convergence_guard() -> None:
    out = _render_user_prompt(_ROWS, tier="lens_diff")
    assert "CONVERGENCE GUARD" in out
    assert "WARNING band" in out
    assert "NEVER MERGE THE VOICES" in out
    # the aperture line, verbatim
    assert "These are four declared priors, not the space of priors." in out
    # diff has no prior of its own — no DECLARED PRIOR block
    assert "DECLARED PRIOR" not in out


def test_entry_consolidation_chronicle_render_unchanged() -> None:
    """Regression-guard the pre-existing three dispatch arms against the new
    lens / lens_diff branches."""
    default = _render_user_prompt(_ROWS)
    explicit = _render_user_prompt(_ROWS, tier="entry")
    assert default == explicit
    assert "the apparatus is your POSTSCRIPT" in default
    assert "INSTRUMENT CITATIONS" in default
    # consolidation shares the diary render
    assert _render_user_prompt(_ROWS, tier="consolidation") == default
    # the chronicle arm is untouched (its own public-record disciplines, no diary)
    chron = _render_user_prompt(_ROWS, tier="chronicle")
    assert "the apparatus is your POSTSCRIPT" not in chron
    assert "[[ref:x1]]" in chron


def test_journal_payload_admits_lens_kinds() -> None:
    """THE Literal-widen regression test (today's chronicle stumble): construct a
    JournalPayload with each new entry_kind directly and assert no ValidationError,
    and that the new `data` field round-trips."""
    from legba.data.provenance.models import JournalPayload

    now = datetime.now(timezone.utc)
    lens = JournalPayload(
        entry_kind="lens",
        title="Trend read",
        body="Under this prior, the trajectory holds.",
        period_start=now,
        period_end=now,
        data={"lens_id": "lens_trend"},
    )
    assert lens.entry_kind == "lens"
    assert lens.data == {"lens_id": "lens_trend"}
    diff = JournalPayload(
        entry_kind="lens_diff",
        title="Chorus diff",
        body="They split on what to weight.",
        period_start=now,
        period_end=now,
        data={"matrix": {"analyst_ids_seen": ["lens_trend"], "analyst_ids_missing": []}},
    )
    assert diff.entry_kind == "lens_diff"
    assert diff.data["matrix"]["analyst_ids_seen"] == ["lens_trend"]
    # the pre-existing kinds still validate and default data to {}
    entry = JournalPayload(
        entry_kind="entry", title="t", period_start=now, period_end=now,
    )
    assert entry.data == {}


def test_lens_diff_roster_helper_over_fake_rows() -> None:
    """The deterministic matrix roster (§3.3) is a pure function — seen/missing
    correct on full + partial input, and most-recent-per-id dedup drops a stale
    duplicate."""
    full = [
        {"analyst_id": "lens_trend", "produced_at": "2026-07-21T08:00:00+00:00"},
        {"analyst_id": "lens_baserate", "produced_at": "2026-07-21T08:01:00+00:00"},
        {"analyst_id": "lens_capability", "produced_at": "2026-07-21T08:02:00+00:00"},
        {"analyst_id": "lens_intent", "produced_at": "2026-07-21T08:03:00+00:00"},
    ]
    m = _lens_diff_roster_from_reads(full)
    assert set(m["analyst_ids_seen"]) == set(LENS_ANALYST_IDS)
    assert m["analyst_ids_missing"] == []
    assert m["topics"] == []  # topic alignment is NARRATE's job, not deterministic

    # partial (3 of 4 — intent absent this cycle)
    partial = [r for r in full if r["analyst_id"] != "lens_intent"]
    mp = _lens_diff_roster_from_reads(partial)
    assert "lens_intent" in mp["analyst_ids_missing"]
    assert "lens_intent" not in mp["analyst_ids_seen"]
    assert set(mp["analyst_ids_seen"]) == {"lens_trend", "lens_baserate", "lens_capability"}

    # dedup: two trend rows, the newer wins; still ONE seen entry per id
    dup = [
        {"analyst_id": "lens_trend", "produced_at": "2026-07-21T08:00:00+00:00"},
        {"analyst_id": "lens_trend", "produced_at": "2026-07-14T08:00:00+00:00"},  # stale
    ]
    md = _lens_diff_roster_from_reads(dup)
    assert md["analyst_ids_seen"] == ["lens_trend"]

    # a non-faculty id is ignored entirely (not seen, not counted)
    noise = [{"analyst_id": "journal_assessor", "produced_at": "2026-07-21T08:00:00+00:00"}]
    mn = _lens_diff_roster_from_reads(noise)
    assert mn["analyst_ids_seen"] == []
    assert set(mn["analyst_ids_missing"]) == set(LENS_ANALYST_IDS)


@pytest.mark.parametrize("lens_id", list(_LENS_DESCRIPTORS))
def test_lens_descriptor_yaml_validates(lens_id: str) -> None:
    from legba.data.schemas.analyst import AnalystDescriptor

    body = yaml.safe_load((_REPO / _LENS_DESCRIPTORS[lens_id]).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    desc = AnalystDescriptor.model_validate(body, strict=False)
    assert desc.identity.id == lens_id
    assert desc.identity.kind == "journal_assessor"      # shared kind module
    assert desc.identity.state.value == "active"
    # the V1 lens gate must be declared
    assert "verify" in body["method"]["llm"]
    # weekly beat, staggered off the burst window
    assert body["cadence"]["fallback_schedule"] == _EXPECTED_CRONS[lens_id]
    assert int(body["cadence"]["cooldown_seconds"]) < 7 * 86400
    # tower-output-only: journal_read ONLY (no substrate_read, no propose, no sink)
    packs = {p["pack_id"] for p in body["action_packs"]}
    assert packs == {"journal_read"}
    assert body["outputs"] == []
    # grounding off by default (§4.3)
    assert body.get("grounding", {}).get("enabled") is False


def test_faculty_prompt_module_resolves_prior_and_id() -> None:
    """Each faculty module exports the SAME three names; the prior block resolves
    and is non-trivial (the persona RENDERS it; the user prompt echoes it)."""
    import importlib

    for aid in LENS_ANALYST_IDS:
        mod = importlib.import_module(f"legba.prompts.{aid}")
        assert mod.LENS_ID == aid
        assert isinstance(mod.LENS_PRIOR_BLOCK, str) and "DECLARED PRIOR" in mod.LENS_PRIOR_BLOCK
        assert "BLIND SPOT" in mod.LENS_PRIOR_BLOCK  # the load-bearing field
        # the composed system prompt carries the prior + the shared no-new-fact stance
        assert mod.LENS_PRIOR_BLOCK in mod.LENS_SYSTEM
        assert "you never assert a new fact" in mod.LENS_SYSTEM


def test_lens_diff_persona_carries_verbatim_aperture() -> None:
    from legba.prompts.lens_diff import LENS_DIFF_APERTURE_LINE, LENS_DIFF_SYSTEM

    assert LENS_DIFF_APERTURE_LINE == "These are four declared priors, not the space of priors."
    assert LENS_DIFF_APERTURE_LINE in LENS_DIFF_SYSTEM
    # the referee never adjudicates + never merges
    assert "you referee, you do not adjudicate" in LENS_DIFF_SYSTEM
    assert "NEVER merge the voices".lower() in LENS_DIFF_SYSTEM.lower()


def test_get_lens_reads_tool_registered() -> None:
    """The new tool is in JOURNAL_READ_TOOLS and dispatches to a global handler
    (the four-surface drift guard extended to get_lens_reads)."""
    from legba.data.analysts.agency.journal_read import (
        JOURNAL_READ_TOOLS,
        register_journal_read_tools,
    )
    from legba.data.analysts.agency.tools import ToolRegistry

    assert "get_lens_reads" in JOURNAL_READ_TOOLS
    reg = ToolRegistry()
    register_journal_read_tools(reg)
    assert "get_lens_reads" in set(reg.names)
