# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The drift classifier, the cited-span check and the slice mark's
render (pure: no database).

Every class of the closed list from a fixture pair; numbers, names, dates and
negations changing are always at least ``substantive_edit``; the span check is
exact folded matching; the slice render is byte-identical for an unmarked row
and carries one line (plus one legend line) for a marked one."""
from __future__ import annotations

import pytest

from legba.data.evidence_drift_classify import (
    DRIFT_CLASSES,
    FLAGGING_CLASSES,
    UNCLASSIFIABLE,
    check_cited_span,
    classify_pair,
    extract_title,
)
from tests.data_pkg import _drift_fixtures as fx


def test_the_imports_resolve_to_this_tree() -> None:
    import legba.data.evidence_drift_classify as m

    assert "/src/legba/data/evidence_drift_classify.py" in m.__file__


@pytest.mark.parametrize("expected", sorted(fx.PAIRS))
def test_each_class_from_its_fixture_pair(expected: str) -> None:
    earlier, later = fx.PAIRS[expected]
    got = classify_pair(earlier, later)
    assert got.drift_class == expected, (got.rule, got.counts, got.critical)


def test_identical_text_title_changed_gone_and_unclassifiable() -> None:
    assert classify_pair(fx.EARLIER, fx.EARLIER).drift_class == "identical_text"
    t = classify_pair(fx.EARLIER, fx.EARLIER.replace("123 comments", "124 comments"),
                      earlier_title=fx.TITLE, later_title="Swat bombing toll revised as police update figures")
    assert (t.drift_class, t.rule, t.title_changed) == ("title_changed", "title_differs", True)
    assert classify_pair(fx.EARLIER, None, later_gone=True, later_status=404).drift_class == "gone"
    assert classify_pair(fx.EARLIER, None).rule == "later_kept_no_article_text"
    assert classify_pair(None, None).drift_class == UNCLASSIFIABLE
    assert UNCLASSIFIABLE not in DRIFT_CLASSES and len(DRIFT_CLASSES) == 8


@pytest.mark.parametrize("old,new,kind", [
    ("rose to 19 on Monday", "rose to 17 on Monday", "numbers"),
    ("police said", "police did not say", "negations"),
    ("Police spokesperson Nasir Karimi said", "Police spokesperson Ali Khan said", "names"),
    ("on Monday, police said", "on Tuesday, police said", "dates"),
])
def test_numbers_names_dates_negations_are_always_substantive(old: str, new: str, kind: str) -> None:
    body = list(fx.BODY)
    idx = next(i for i, s in enumerate(body) if old in s)
    body[idx] = body[idx].replace(old, new)
    got = classify_pair(fx.page(fx.BODY), fx.page(body))
    assert got.drift_class == "substantive_edit" and got.rule == "critical_token_changed", got.critical
    assert kind in got.critical


def test_an_inflected_name_is_not_a_name_change() -> None:
    a = "Iran said Israel had struck the facility on the coast near the port city overnight."
    b = "Iran said Israeli jets had struck the facility on the coast near the port city overnight."
    got = classify_pair(fx.page([a, *fx.BODY[1:]]), fx.page([b, *fx.BODY[1:]]))
    assert got.drift_class == "minor_edit", got.critical


def test_a_removed_sentence_is_substantive_and_a_correction_notice_with_an_edit_is_retracted() -> None:
    got = classify_pair(fx.page(fx.BODY), fx.page(fx.BODY[:2] + fx.BODY[3:]))
    assert (got.drift_class, got.rule) == ("substantive_edit", "sentence_removed")
    corrected = [fx.BODY[0].replace("rose to 19", "rose to 17"), *fx.BODY[1:],
                 "Correction: An earlier version of this article misstated the death toll."]
    got = classify_pair(fx.page(fx.BODY), fx.page(corrected))
    assert got.drift_class == "retracted_or_replaced" and "correction_notice" in got.markers
    retracted = [*fx.BODY, "This article has been retracted."]
    assert classify_pair(fx.page(fx.BODY), fx.page(retracted)).rule == "retraction_marker"


def test_the_evidence_names_what_changed() -> None:
    earlier, later = fx.PAIRS["substantive_edit"]
    ev = classify_pair(earlier, later).evidence()
    assert ev["critical"]["numbers"] == ["-19", "+17"]
    assert ev["changed"][0]["earlier"].startswith("The death toll") and "17" in ev["changed"][0]["later"]
    assert ev["counts"]["reworded"] == 1 and ev["counts"]["removed"] == 0


def test_flagging_classes_are_the_four_named() -> None:
    assert FLAGGING_CLASSES == {"substantive_edit", "title_changed", "retracted_or_replaced", "gone"}


def test_the_cited_span_check_is_exact_folded_matching() -> None:
    earlier, later = fx.PAIRS["substantive_edit"]
    gone = check_cited_span(fx.CITED_SPAN, earlier, later)
    assert (gone.span_in_earlier, gone.span_in_later, gone.checked) == (True, False, 1)
    kept = check_cited_span(fx.BODY[2].replace("'", "’"), earlier, later)  # curly quote folds
    assert (kept.span_in_earlier, kept.span_in_later) == (True, True)
    elsewhere = check_cited_span("A sentence from the feed summary that the page never carried at all.",
                                 earlier, later)
    assert (elsewhere.span_in_earlier, elsewhere.span_in_later) == (False, None)
    joined = check_cited_span(fx.BODY[2] + "… " + fx.BODY[0], earlier, later)
    assert (joined.checked, joined.in_earlier, joined.in_later) == (2, 2, 1)
    assert check_cited_span(None, earlier, later).span_in_later is None


def test_extract_title_reads_og_title_then_title() -> None:
    assert extract_title(b"<html><head><title>A &amp; B</title></head></html>") == "A & B"
    assert extract_title(b'<meta property="og:title" content="OG one"><title>T</title>') == "OG one"
    assert extract_title(None) is None and extract_title(b"<p>no title</p>") is None


# -- the slice mark's render ----------------------------------------------------

_ROW = {
    "id": "x", "geo": ["OM"], "source_url": "https://example.test/a",
    "title": "Port of Duqm expands",
    "produced_at": "2026-11-13 08:00:00+00:00",
    "data": {"title": "Port of Duqm expands", "summary": "The port grew.",
             "geo": {"country_iso2": "OM"}},
}
_GOLDEN = (
    "[1] Port of Duqm expands\n"
    "    ingested=2026-11-13 08:00:00+00:00 source=https://example.test/a\n"
    "    snippet=The port grew."
)


def test_an_unmarked_row_renders_byte_identically() -> None:
    from legba.data.analysts.slice_render import _render_signal, _render_user_prompt

    assert _render_signal(1, _ROW) == _GOLDEN
    prompt = _render_user_prompt([_ROW], "country_om", run_date="2026-11-13")
    assert "source text changed" not in prompt and prompt.endswith(_GOLDEN)


def test_a_marked_row_carries_one_line_and_the_header_one_legend() -> None:
    from legba.data.analysts.slice_render import _render_signal, _render_user_prompt
    from legba.data.evidence_drift_slice import SOURCE_DRIFT_LEGEND, SOURCE_DRIFT_ROW_KEY

    row = {**_ROW, SOURCE_DRIFT_ROW_KEY: {"date": "2026-11-10", "class": "substantive_edit"}}
    lines = _render_signal(1, row).splitlines()
    assert lines[2] == "    source text changed on 2026-11-10: substantive edit"
    assert "\n".join(lines[:2] + lines[3:]) == _GOLDEN
    prompt = _render_user_prompt([row, _ROW], "country_om", run_date="2026-11-13")
    assert prompt.count(SOURCE_DRIFT_LEGEND.strip()) == 1
    assert prompt.count("source text changed on") == 1


def test_the_slice_mark_switch_is_off_by_default(monkeypatch) -> None:
    from legba.data.evidence_drift_slice import SLICE_DRIFT_MARK_ENV, slice_drift_mark_enabled

    monkeypatch.delenv(SLICE_DRIFT_MARK_ENV, raising=False)
    assert slice_drift_mark_enabled() is False
    monkeypatch.setenv(SLICE_DRIFT_MARK_ENV, "1")
    assert slice_drift_mark_enabled() is True


def test_the_alert_kind_is_closed_listed_with_a_topic_and_never_paged_by_the_rule() -> None:
    from legba.data.alerts.kinds import KINDS
    from legba.data.alerts.page_rule import load_rule
    from legba.data.alerts.topics import TOPIC_BY_KIND

    kind = KINDS["evidence_drift"]
    assert (kind.subject_kind, kind.lifecycle, kind.quiet_close_hours) == ("source", "event", 24)
    assert TOPIC_BY_KIND["evidence_drift"] == "engine"
    assert all(e.kind != "evidence_drift" for e in load_rule().entries)
