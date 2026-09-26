# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""B1 (T1.2) / B2 (T1.4) — JOURNAL_CONNECTIVE_AUDIT_PROPOSAL_2026-09-09 §3
P4/P6 — slice row provenance labelling (``'instrument'`` / ``'routine'``) and
the deterministic REFLECT honesty flags it feeds (``instrument_as_report`` /
``routine_as_signal``). No DB — the labelling + flag detectors are pure
functions; the full-arc "flags reach a written entry's honesty_flags" proof
lives in ``tests/journal_w1/test_journal_arc.py`` beside its sibling forced-
honesty-flag tests.
"""

from __future__ import annotations

from uuid import uuid4

from legba.data.analysts import journal_assessor as ja
from legba.data.analysts import journal_reflect as jr
from legba.data.analysts import journal_slice as js
from legba.data.provenance.models import JournalClaim

# ---------------------------------------------------------------------------
# B1 (T1.2) — instrument row labelling
# ---------------------------------------------------------------------------


def test_instrument_row_labeled_by_gdelt_source_id() -> None:
    rows = [{"id": "s1", "source_id": "source.gdelt.files", "title": "x"}]
    out = js._labeled_journal_slice(rows)
    assert out[0]["journal_label"] == "instrument"


def test_instrument_row_labeled_by_cameo_pseudo_title_with_arrow() -> None:
    rows = [{
        "id": "s1", "source_id": "source.reuters.rss",
        "title": "POLICE <-> GOVERNMENT: assault",
    }]
    out = js._labeled_journal_slice(rows)
    assert out[0]["journal_label"] == "instrument"


def test_instrument_row_labeled_by_cameo_pseudo_title_no_arrow() -> None:
    rows = [{
        "id": "s1", "source_id": "source.reuters.rss",
        "title": "PRISON: coerce in Kansas",
    }]
    out = js._labeled_journal_slice(rows)
    assert out[0]["journal_label"] == "instrument"


def test_ordinary_row_stays_unlabeled() -> None:
    rows = [{
        "id": "s1", "source_id": "source.reuters.rss",
        "title": "Senate passes new spending bill",
    }]
    out = js._labeled_journal_slice(rows)
    assert "journal_label" not in out[0]


def test_labeled_journal_slice_never_reorders_the_selector() -> None:
    """Labelling composes OVER _select_journal_slice — it must never change
    which rows are picked or their order (B0's byte-identity proof for the
    selector alone must keep holding forever)."""
    rows = [
        {"id": "a", "source_id": "source.gdelt.files", "title": "x"},
        {"id": "b", "source_id": "source.reuters.rss", "title": "y"},
    ]
    selected_ids = [r["id"] for r in js._select_journal_slice(rows)]
    labeled_ids = [r["id"] for r in js._labeled_journal_slice(rows)]
    assert labeled_ids == selected_ids


def test_labeled_journal_slice_is_idempotent() -> None:
    rows = [{"id": "a", "source_id": "source.gdelt.files", "title": "x"}]
    first = js._labeled_journal_slice(rows)
    second = js._labeled_journal_slice(rows)
    assert first[0]["journal_label"] == second[0]["journal_label"] == "instrument"


# ---------------------------------------------------------------------------
# window rendering — the narrator's window line shows the [instrument] tag
# ---------------------------------------------------------------------------


def test_render_user_prompt_shows_instrument_tag() -> None:
    rows = [{
        "id": "s1", "source_id": "source.gdelt.files",
        "title": "POLICE <-> GOVERNMENT: assault",
    }]
    rendered = ja._render_user_prompt(rows)
    slice_body = rendered.split("--- recent signal slice ---", 1)[1]
    assert "[instrument]" in slice_body


def test_render_user_prompt_omits_tag_for_ordinary_row() -> None:
    rows = [{
        "id": "s1", "source_id": "source.reuters.rss",
        "title": "Senate passes new spending bill",
    }]
    rendered = ja._render_user_prompt(rows)
    slice_body = rendered.split("--- recent signal slice ---", 1)[1]
    assert "[instrument]" not in slice_body


def test_render_chronicle_prompt_also_shows_instrument_tag() -> None:
    """Labelling is composed at every tier's render call site, not just the
    diary's — the chronicle/lens/lens_diff windows share the same tag."""
    rows = [{
        "id": "s1", "source_id": "source.gdelt.files",
        "title": "POLICE <-> GOVERNMENT: assault",
    }]
    rendered = ja._render_user_prompt(rows, tier="chronicle")
    slice_body = rendered.split("--- recent signal slice ---", 1)[1]
    assert "[instrument]" in slice_body


# ---------------------------------------------------------------------------
# REFLECT — the deterministic instrument_as_report flag (flag, never strip)
# ---------------------------------------------------------------------------


def test_flag_instrument_as_report_fires_on_majority_share_no_marker() -> None:
    ref = uuid4()
    claim = JournalClaim(
        text_span=f"legal-security reports trace a pattern [[ref:{ref}]]",
        refs=[ref], kind="fact",
    )
    flags = jr._flag_instrument_as_report([claim], {str(ref): "instrument"})
    assert flags == [jr.INSTRUMENT_AS_REPORT_FLAG]
    # never strips — the claim's own text/refs are untouched by the detector.
    assert claim.refs == [ref]
    assert "[[ref:" in claim.text_span


def test_flag_instrument_as_report_silent_when_marked() -> None:
    ref = uuid4()
    claim = JournalClaim(
        text_span=f"[[instrument]] a pattern of coercive encounters [[ref:{ref}]]",
        refs=[ref], kind="fact",
    )
    assert jr._flag_instrument_as_report([claim], {str(ref): "instrument"}) == []


def test_flag_instrument_as_report_silent_below_half_share() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    claim = JournalClaim(
        text_span=f"pattern [[ref:{a}]] [[ref:{b}]] [[ref:{c}]]",
        refs=[a, b, c], kind="fact",
    )
    labels = {str(a): "instrument"}  # 1/3 — below the 0.5 threshold
    assert jr._flag_instrument_as_report([claim], labels) == []


def test_flag_instrument_as_report_silent_on_empty_ref_labels() -> None:
    ref = uuid4()
    claim = JournalClaim(text_span=f"x [[ref:{ref}]]", refs=[ref], kind="fact")
    assert jr._flag_instrument_as_report([claim], {}) == []


def test_flag_instrument_as_report_ignores_perspective_claims() -> None:
    claim = JournalClaim(text_span="I wonder about the pattern", refs=[], kind="perspective")
    assert jr._flag_instrument_as_report([claim], {"x": "instrument"}) == []


# ---------------------------------------------------------------------------
# B2 (T1.4) — routine product labelling
# ---------------------------------------------------------------------------


def test_routine_allowlist_floor_is_present() -> None:
    assert "source.nws.active_alerts" in js.ROUTINE_PRODUCT_SOURCES


def test_routine_row_labeled_by_allowlisted_source_id() -> None:
    rows = [{"id": "s1", "source_id": "source.nws.active_alerts", "title": "Heat advisory"}]
    out = js._labeled_journal_slice(rows)
    assert out[0]["journal_label"] == "routine"


def test_routine_row_labeled_press_release_wire() -> None:
    rows = [{"id": "s1", "source_id": "source.rbi.press_releases", "title": "RBI notice"}]
    out = js._labeled_journal_slice(rows)
    assert out[0]["journal_label"] == "routine"


def test_ordinary_source_not_labeled_routine() -> None:
    rows = [{"id": "s1", "source_id": "source.reuters.rss", "title": "Senate vote"}]
    out = js._labeled_journal_slice(rows)
    assert "journal_label" not in out[0]


def test_instrument_check_wins_over_routine_when_both_could_apply() -> None:
    """An instrument-shaped title on a (hypothetically) allowlisted source_id
    stays 'instrument' — the CAMEO shape is checked first."""
    rows = [{
        "id": "s1", "source_id": "source.nws.active_alerts",
        "title": "POLICE <-> GOVERNMENT: assault",
    }]
    out = js._labeled_journal_slice(rows)
    assert out[0]["journal_label"] == "instrument"


def test_render_user_prompt_shows_routine_tag() -> None:
    rows = [{"id": "s1", "source_id": "source.nws.active_alerts", "title": "Heat advisory"}]
    rendered = ja._render_user_prompt(rows)
    slice_body = rendered.split("--- recent signal slice ---", 1)[1]
    assert "[routine]" in slice_body


def test_flag_routine_as_signal_fires_on_change_connective() -> None:
    ref = uuid4()
    claim = JournalClaim(
        text_span=f"extreme heat compounds the region's water stress [[ref:{ref}]]",
        refs=[ref], kind="fact",
    )
    flags = jr._flag_routine_as_signal([claim], {str(ref): "routine"})
    assert flags == [jr.ROUTINE_AS_SIGNAL_FLAG]
    # never strips
    assert claim.refs == [ref]


def test_flag_routine_as_signal_silent_without_change_cue() -> None:
    ref = uuid4()
    claim = JournalClaim(
        text_span=f"the weather service issued a heat advisory [[ref:{ref}]]",
        refs=[ref], kind="fact",
    )
    assert jr._flag_routine_as_signal([claim], {str(ref): "routine"}) == []


def test_flag_routine_as_signal_silent_on_non_routine_ref() -> None:
    ref = uuid4()
    claim = JournalClaim(
        text_span=f"the compounds driving the market amid new rules [[ref:{ref}]]",
        refs=[ref], kind="fact",
    )
    # ref is unlabelled (not routine) — the connective alone never fires it.
    assert jr._flag_routine_as_signal([claim], {}) == []
    assert jr._flag_routine_as_signal([claim], {str(ref): "instrument"}) == []


def test_flag_routine_as_signal_ignores_perspective_claims() -> None:
    claim = JournalClaim(text_span="amid the stress I wonder", refs=[], kind="perspective")
    assert jr._flag_routine_as_signal([claim], {"x": "routine"}) == []
