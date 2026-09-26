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
  * the descriptor YAMLs validate on the shared kind, declare verify, carry the
    staggered crons + journal_read-only grants;
  * ``get_lens_reads`` is registered in the pack (the four-surface guard).

2026-09-21 — the VOICES LEANS (planning/VOICES_LEAN_PRIORS_2026-09-21.md; operator
decision D-4 accepted the six priors as drafts) add SIX stance-typed ids on the
same kind: lens_left / lens_right / lens_centre / lens_pragmatist /
lens_militarist / lens_isolationist. They ride the same plumbing (entry_kind
'lens', V1 verify, journal_read-only, one declared prior per persona module) on a
DAILY 09:00–11:30 UTC band, and they ship ``state: draft`` — the orchestrator
promotes. The chorus diff's roster is fenced at the four FUNCTION-typed faculties
(``LENS_DIFF_ROSTER_IDS``) so the leans landing cannot silently re-scope an active
analyst whose persona declares a four-prior aperture verbatim.
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
    LENS_DIFF_ROSTER_IDS,
    LENS_FACULTY_ANALYST_IDS,
    LENS_LEAN_ANALYST_IDS,
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
    # the 2026-09-21 stance-typed leans
    "lens_left": "descriptors/analyst_lens_left.yaml",
    "lens_right": "descriptors/analyst_lens_right.yaml",
    "lens_centre": "descriptors/analyst_lens_centre.yaml",
    "lens_pragmatist": "descriptors/analyst_lens_pragmatist.yaml",
    "lens_militarist": "descriptors/analyst_lens_militarist.yaml",
    "lens_isolationist": "descriptors/analyst_lens_isolationist.yaml",
}

# The staggered crons. The four faculties + the diff (VOICES_BUILD_DESIGN §4.1)
# run WEEKLY, all AFTER the chronicle's `0 6 * * 1`, 30-min spaced. The six leans
# (VOICES_LEAN_PRIORS_2026-09-21) run DAILY in the 09:00–11:30 UTC band, 30-min
# spaced and clear of the faculties' Monday 06:30–08:30 window.
_EXPECTED_CRONS = {
    "lens_trend": "30 6 * * 1",
    "lens_baserate": "0 7 * * 1",
    "lens_capability": "30 7 * * 1",
    "lens_intent": "0 8 * * 1",
    "lens_diff": "30 8 * * 1",
    "lens_left": "0 9 * * *",
    "lens_right": "30 9 * * *",
    "lens_centre": "0 10 * * *",
    "lens_pragmatist": "30 10 * * *",
    "lens_militarist": "0 11 * * *",
    "lens_isolationist": "30 11 * * *",
}

# The leans ship as DRAFTS (the orchestrator promotes); the LV-1 five are live.
_EXPECTED_STATES = {
    lens_id: ("draft" if lens_id in LENS_LEAN_ANALYST_IDS else "active")
    for lens_id in _LENS_DESCRIPTORS
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


def test_lens_prompt_empty_slice_renders_tower_fallback() -> None:
    """E-1 (2026-07-27 sweep): an EMPTY lens slice must not prime a blank —
    the render redirects the faculty at the verified tower top explicitly
    (the chronicle/consolidation behavior on the same bad-input cycle)."""
    from legba.prompts.lens_capability import LENS_PRIOR_BLOCK

    out = _render_user_prompt([], tier="lens", lens_prior_block=LENS_PRIOR_BLOCK)
    assert "the signal slice is EMPTY this cycle" in out
    assert "VERIFIED TOWER TOP" in out
    assert "get_assessments" in out
    # honesty stays: never fabricate
    assert "never fabricate" in out
    # rows without renderable titles count as empty too
    out2 = _render_user_prompt(
        [{"id": None, "title": "", "source_id": "graph_metrics"}],
        tier="lens", lens_prior_block=LENS_PRIOR_BLOCK,
    )
    assert "the signal slice is EMPTY this cycle" in out2
    # a populated slice renders rows, NOT the fallback line
    populated = _render_user_prompt(
        _ROWS, tier="lens", lens_prior_block=LENS_PRIOR_BLOCK
    )
    assert "the signal slice is EMPTY this cycle" not in populated
    assert "[[ref:x1]]" in populated


def test_empty_slice_fallback_is_lens_only() -> None:
    """The other tiers keep their pre-existing empty-slice renders unchanged
    (the diary/chronicle/diff prompts never carry the lens fallback line)."""
    for tier in ("entry", "consolidation", "chronicle", "lens_diff"):
        out = _render_user_prompt([], tier=tier)
        assert "the signal slice is EMPTY this cycle" not in out, tier


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
    assert set(m["analyst_ids_seen"]) == set(LENS_DIFF_ROSTER_IDS)
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
    assert set(mn["analyst_ids_missing"]) == set(LENS_DIFF_ROSTER_IDS)


def test_lens_diff_roster_is_fenced_to_the_four_faculties() -> None:
    """The 2026-09-21 leans ride the same kind and write the same entry_kind, but
    the chorus diff's matrix stays the FOUR function-typed faculties: its persona
    declares a four-prior aperture verbatim, so a lean must be neither 'seen' nor
    counted 'missing' by the roster."""
    assert LENS_DIFF_ROSTER_IDS == LENS_FACULTY_ANALYST_IDS
    assert set(LENS_DIFF_ROSTER_IDS).isdisjoint(LENS_LEAN_ANALYST_IDS)

    leans = [
        {"analyst_id": aid, "produced_at": "2026-09-21T09:00:00+00:00"}
        for aid in LENS_LEAN_ANALYST_IDS
    ]
    m = _lens_diff_roster_from_reads(leans)
    assert m["analyst_ids_seen"] == []
    assert set(m["analyst_ids_missing"]) == set(LENS_FACULTY_ANALYST_IDS)

    # a mixed cycle: the lean rows are dropped, the faculty row is kept
    mixed = _lens_diff_roster_from_reads(
        leans + [{"analyst_id": "lens_trend",
                  "produced_at": "2026-09-21T09:05:00+00:00"}]
    )
    assert mixed["analyst_ids_seen"] == ["lens_trend"]
    for aid in LENS_LEAN_ANALYST_IDS:
        assert aid not in mixed["analyst_ids_missing"]


@pytest.mark.asyncio
async def test_get_lens_reads_passes_the_fenced_diff_roster() -> None:
    """Through the REAL registered handler: the pack dispatch hands the port the
    DIFF roster, not every lens id on the kind — otherwise the leans landing
    would silently widen what lens_diff reads without touching its persona."""
    from legba.data.analysts.agency.journal_read import register_journal_read_tools
    from legba.data.analysts.agency.tools import (
        ToolCall,
        ToolContext,
        ToolRegistry,
    )

    seen: dict[str, object] = {}

    class _Port:
        async def get_lens_reads(self, *, lens_analyst_ids, since=None, limit=20):
            seen["ids"] = list(lens_analyst_ids)
            return {"rows": []}

    reg = ToolRegistry()
    register_journal_read_tools(reg)
    handler = reg.handler_for("get_lens_reads")
    assert handler is not None

    out = await handler(
        ToolCall(pack_id="journal_read", tool_name="get_lens_reads", args={}),
        None,
        ToolContext(substrate=_Port()),
    )
    assert out.status == "completed"
    assert seen["ids"] == list(LENS_DIFF_ROSTER_IDS)
    for aid in LENS_LEAN_ANALYST_IDS:
        assert aid not in seen["ids"]


@pytest.mark.parametrize("lens_id", list(_LENS_DESCRIPTORS))
def test_lens_descriptor_yaml_validates(lens_id: str) -> None:
    from legba.data.schemas.analyst import AnalystDescriptor

    body = yaml.safe_load((_REPO / _LENS_DESCRIPTORS[lens_id]).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    desc = AnalystDescriptor.model_validate(body, strict=False)
    assert desc.identity.id == lens_id
    assert desc.identity.kind == "journal_assessor"      # shared kind module
    assert desc.identity.state.value == _EXPECTED_STATES[lens_id]
    # the V1 lens gate must be declared
    assert "verify" in body["method"]["llm"]
    # the beat, staggered off the burst window
    assert body["cadence"]["fallback_schedule"] == _EXPECTED_CRONS[lens_id]
    # cooldown sits below the cadence interval (weekly for LV-1, daily for the leans)
    interval = 86400 if lens_id in LENS_LEAN_ANALYST_IDS else 7 * 86400
    assert int(body["cadence"]["cooldown_seconds"]) < interval
    # tower-output-only: journal_read ONLY (no substrate_read, no propose, no sink)
    packs = {p["pack_id"] for p in body["action_packs"]}
    assert packs == {"journal_read"}
    assert body["outputs"] == []
    # grounding off by default (§4.3)
    assert body.get("grounding", {}).get("enabled") is False
    # house rules: $0 core plane + model-card sampling on every LLM route
    assert int(body["method"]["budget_tokens_per_day"]) == 0
    assert float(body["method"]["llm"]["temperature"]) == 1.0
    # the leans declare NO max_tokens — the core plane never caps model output
    # (the LV-1 five carry an inert legacy 12288; it is not re-introduced here)
    if lens_id in LENS_LEAN_ANALYST_IDS:
        assert "max_tokens" not in body["method"]["llm"]


def test_lens_roster_constants_partition_cleanly() -> None:
    """``LENS_ANALYST_IDS`` is faculties THEN leans, with no overlap and no
    duplicates — it is what drives entry_kind distillation, the persona map and
    the declared-prior lookup, so a stray id here is a silently mis-tiered run."""
    assert LENS_ANALYST_IDS == LENS_FACULTY_ANALYST_IDS + LENS_LEAN_ANALYST_IDS
    assert len(set(LENS_ANALYST_IDS)) == len(LENS_ANALYST_IDS)
    assert set(LENS_FACULTY_ANALYST_IDS).isdisjoint(LENS_LEAN_ANALYST_IDS)
    assert LENS_LEAN_ANALYST_IDS == (
        "lens_left", "lens_right", "lens_centre",
        "lens_pragmatist", "lens_militarist", "lens_isolationist",
    )
    assert LENS_DIFF_ANALYST_ID not in LENS_ANALYST_IDS
    # every lens id has a descriptor and a persona-module path
    from legba.data.analysts.journal_assessor import LENS_PROMPT_MODULE_PATHS

    for aid in LENS_ANALYST_IDS:
        assert aid in _LENS_DESCRIPTORS
        assert LENS_PROMPT_MODULE_PATHS[aid] == f"legba.prompts.{aid}:LENS_SYSTEM"


def test_faculty_prompt_module_resolves_prior_and_id() -> None:
    """Each lens module (faculty AND lean) exports the SAME three names; the prior
    block resolves, names ITS OWN id, and is non-trivial (the persona RENDERS it;
    the user prompt echoes it)."""
    import importlib

    for aid in LENS_ANALYST_IDS:
        mod = importlib.import_module(f"legba.prompts.{aid}")
        assert mod.LENS_ID == aid
        assert isinstance(mod.LENS_PRIOR_BLOCK, str) and "DECLARED PRIOR" in mod.LENS_PRIOR_BLOCK
        assert "BLIND SPOT" in mod.LENS_PRIOR_BLOCK  # the load-bearing field
        # the block is the id's OWN prior, not a neighbour's copied across
        assert aid in mod.LENS_PRIOR_BLOCK, aid
        assert mod.LENS_PRIOR_BLOCK.rstrip().endswith("--- END DECLARED PRIOR ---")
        # the composed system prompt carries the prior + the shared no-new-fact stance
        assert mod.LENS_PRIOR_BLOCK in mod.LENS_SYSTEM
        assert "you never assert a new fact" in mod.LENS_SYSTEM
        assert set(mod.__all__) == {"LENS_SYSTEM", "LENS_PRIOR_BLOCK", "LENS_ID"}


def test_every_lens_system_carries_the_shared_persona_and_its_own_prior() -> None:
    """``compose_lens_system`` is the ONE place the lens register lives: every id
    must carry the shared LENS_PERSONA / LENS_TASK / narrate contract verbatim,
    plus its own prior and NOBODY else's. A lean that hand-rolled its system
    prompt (or imported the wrong prior) fails here."""
    import importlib

    from legba.prompts.lens_common import (
        LENS_NARRATE_PREAMBLE,
        LENS_PERSONA,
        LENS_TASK,
    )

    priors = {
        aid: importlib.import_module(f"legba.prompts.{aid}").LENS_PRIOR_BLOCK
        for aid in LENS_ANALYST_IDS
    }
    for aid in LENS_ANALYST_IDS:
        system = importlib.import_module(f"legba.prompts.{aid}").LENS_SYSTEM
        assert LENS_PERSONA in system, aid
        assert LENS_TASK in system, aid
        assert LENS_NARRATE_PREAMBLE in system, aid
        assert priors[aid] in system, aid
        # exactly ONE declared prior per read — no other lens's block rides along
        assert system.count("--- DECLARED PRIOR") == 1, aid
        for other, block in priors.items():
            if other != aid:
                assert block not in system, (aid, other)
        # the voice sketch renders the prior; it never re-opens the diary
        assert "YOUR VOICE" in system, aid
        assert "the apparatus is your POSTSCRIPT" not in system, aid


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
