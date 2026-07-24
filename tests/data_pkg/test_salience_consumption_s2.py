# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S-2 / S-1d (MASTER_PLAN 2026-07-10, Phase S) — salience CONSUMPTION + PROPAGATION.

S-1 made consequence DATA (``signals.salience``); this suite locks the consumers:

  * S-1d propagation helpers (signal_salience.magnitude_of / max_salience /
    build_signal_finding_salience) — the max-up-the-tower primitive + the unit
    finding stamp builder.
  * S-2a the JOURNAL slice ordering (journal_assessor._salience_ordered /
    _salience_tag / _render_user_prompt) — THE Graham test: a head-of-state
    funeral (higher magnitude, OLDER) must lead a senator's death (lower
    magnitude, NEWER); an unscored row sorts last; authority breaks a magnitude
    tie.
  * S-2b the COMPOSITION input ordering (meta_findings_synthesizer._orient /
    _extract_input_salience / _input_salience_magnitude / _render_salience_lead_block)
    — salience-primary sort over the FindingPayload envelope (``data->data->
    salience``), the byte-for-byte fallback when NOTHING is scored yet, and the
    cap keeping the highest-consequence input.
"""
from __future__ import annotations

from datetime import datetime, timezone

from legba.data.analysts import signal_salience as ss
from legba.data.analysts import journal_assessor as ja
from legba.data.analysts import meta_findings_synthesizer as mfs


# ---------------------------------------------------------------------------
# S-1d propagation primitives (signal_salience)
# ---------------------------------------------------------------------------


def test_magnitude_of_missing_and_degraded_sort_last() -> None:
    assert ss.magnitude_of({"magnitude": 0.9}) == 0.9
    assert ss.magnitude_of({"magnitude": 0.0}) == 0.0
    # missing / non-mapping / non-numeric all collapse to -1.0 (sorts LAST)
    assert ss.magnitude_of(None) == -1.0
    assert ss.magnitude_of({}) == -1.0
    assert ss.magnitude_of("not a dict") == -1.0
    assert ss.magnitude_of({"magnitude": "high"}) == -1.0


def test_max_salience_picks_highest_and_copies() -> None:
    a = {"magnitude": 0.3, "event_class": "market_move"}
    b = {"magnitude": 0.9, "event_class": "kinetic_strike"}
    c = {"magnitude": 0.5, "event_class": "sanctions_economic"}
    win = ss.max_salience([a, b, c])
    assert win is not None and win["magnitude"] == 0.9
    assert win["event_class"] == "kinetic_strike"
    # returns a COPY — mutating the result never corrupts the input row
    win["magnitude"] = 0.0
    assert b["magnitude"] == 0.9


def test_max_salience_none_when_empty_or_all_unscored() -> None:
    assert ss.max_salience([]) is None
    assert ss.max_salience([None, {}, {"magnitude": "x"}]) is None


def test_build_signal_finding_salience_carries_leaf_identity() -> None:
    rows = [
        {"id": "s-low", "title": "routine arms deal",
         "salience": {"magnitude": 0.2, "event_class": "procurement_routine",
                      "actor_rank": "state_organ", "authority": "reporting"}},
        {"id": "s-top", "title": "US missile strike hits IRGC position",
         "salience": {"magnitude": 0.9, "event_class": "kinetic_strike",
                      "actor_rank": "state_organ", "authority": "official"}},
        {"id": "s-none", "title": "no salience here"},  # unscored → not the max
    ]
    out = ss.build_signal_finding_salience(rows)
    assert out is not None
    assert out["magnitude"] == 0.9
    assert out["event_class"] == "kinetic_strike"
    assert out["authority"] == "official"
    assert out["top_signal_id"] == "s-top"
    assert out["top_title"] == "US missile strike hits IRGC position"
    assert out["source"] == "signals"
    # n_scored counts only the rows that carried a usable magnitude (2 of 3)
    assert out["n_scored"] == 2


def test_build_signal_finding_salience_none_when_unscored() -> None:
    rows = [{"id": "a", "title": "x"}, {"id": "b", "salience": {}}]
    assert ss.build_signal_finding_salience(rows) is None
    assert ss.build_signal_finding_salience([]) is None


def test_build_signal_finding_salience_title_from_payload() -> None:
    # no top-level title column → fall back to payload.title
    rows = [{"id": "p", "payload": {"title": "Bangkok bar fire kills 27"},
             "salience": {"magnitude": 0.8, "event_class": "mass_casualty"}}]
    out = ss.build_signal_finding_salience(rows)
    assert out is not None and out["top_title"] == "Bangkok bar fire kills 27"


# ---------------------------------------------------------------------------
# S-2a — the JOURNAL slice ordering (THE Graham test)
# ---------------------------------------------------------------------------


def _sig(sid: str, title: str, mag: float | None, when: str,
         authority: str = "reporting", event_class: str = "other") -> dict:
    row: dict = {"id": sid, "title": title, "produced_at": when}
    if mag is not None:
        row["salience"] = {"magnitude": mag, "authority": authority,
                           "event_class": event_class}
    return row


def test_journal_graham_test_consequence_beats_recency() -> None:
    """A head-of-state funeral (0.95, OLDER) must outrank a senator's death
    (0.30, NEWER) — the exact j4/j5 failure the salience layer exists to fix."""
    funeral = _sig("khamenei", "Supreme Leader Khamenei buried", 0.95,
                   "2026-07-12T18:00:00+00:00", "state_media", "leader_death")
    senator = _sig("graham", "Senator Graham dies at 70", 0.30,
                   "2026-07-13T08:00:00+00:00", "reporting", "other")
    ordered = ja._salience_ordered([senator, funeral])
    assert [r["id"] for r in ordered] == ["khamenei", "graham"]


def test_journal_authority_breaks_magnitude_tie() -> None:
    official = _sig("off", "wire strike report", 0.9,
                    "2026-07-12T00:00:00+00:00", "official", "kinetic_strike")
    state_media = _sig("sm", "adversary strike claim", 0.9,
                       "2026-07-13T00:00:00+00:00", "state_media", "kinetic_strike")
    ordered = ja._salience_ordered([state_media, official])
    # equal magnitude → the more AUTHORITATIVE source leads (anti-tabloid guard)
    assert [r["id"] for r in ordered] == ["off", "sm"]


def test_journal_unscored_rows_sort_last() -> None:
    scored = _sig("scored", "a real event", 0.2, "2026-07-10T00:00:00+00:00")
    fresh_unscored = _sig("unscored", "newest but unscored", None,
                          "2026-07-14T00:00:00+00:00")
    ordered = ja._salience_ordered([fresh_unscored, scored])
    # a scored 0.2 outranks an UNSCORED row even though the latter is newest
    assert [r["id"] for r in ordered] == ["scored", "unscored"]


def test_journal_salience_tag_format() -> None:
    assert ja._salience_tag({"magnitude": 0.95, "event_class": "leader_death"}) \
        == "(salience 0.95·leader_death) "
    # 'other' class is not appended (adds no information)
    assert ja._salience_tag({"magnitude": 0.30, "event_class": "other"}) \
        == "(salience 0.30) "
    # unscored / missing → NO tag
    assert ja._salience_tag(None) == ""
    assert ja._salience_tag({}) == ""


def test_journal_order_is_stable_preserves_delivery_when_unscored() -> None:
    """Review fix (diversity guard): the reader delivers a per-source
    DIVERSITY-CAPPED order (NOT pure recency) — an older geopolitical row can be
    promoted above a newer firehose row. With nothing scored, the stable salience
    sort must NOT reshuffle by recency (which would re-let the firehose win)."""
    delivered = [
        _sig("geo-old", "reuters strike report", None, "2026-07-10T00:00:00+00:00"),
        _sig("fire-new", "nws flood warning", None, "2026-07-14T00:00:00+00:00"),
        _sig("geo-old2", "aljazeera analysis", None, "2026-07-11T00:00:00+00:00"),
    ]
    out = ja._salience_ordered(delivered)
    # delivered order UNCHANGED (a recency re-sort would hoist fire-new to front)
    assert [r["id"] for r in out] == ["geo-old", "fire-new", "geo-old2"]


def test_journal_select_slice_unchanged_when_nothing_scored() -> None:
    """Review fix: nothing scored → byte-for-byte the delivered order, plain
    [:cap] cut, NO fresh-floor perturbation (nothing is buried below a score)."""
    delivered = [_sig(f"r{i}", f"row {i}", None,
                      f"2026-07-{10 + (i % 5):02d}T00:00:00+00:00") for i in range(80)]
    out = ja._select_journal_slice(delivered)
    assert [r["id"] for r in out] == [r["id"] for r in delivered[:ja._JOURNAL_RENDER_CAP]]


def test_journal_fresh_floor_rescues_breaking_unscored() -> None:
    """Review fix (fresh floor): a window of >cap scored-but-ROUTINE (0.2) rows
    must not truncate a fresh, unscored, breaking signal out of the render — the
    recency-starvation inverse the salience layer must not introduce."""
    rows = [_sig(f"routine{i}", f"routine {i}", 0.2, "2026-07-10T00:00:00+00:00")
            for i in range(65)]
    rows.append(_sig("breaking", "fresh unscored strike", None,
                     "2026-07-14T23:00:00+00:00"))
    out = ja._select_journal_slice(rows)
    ids = [r["id"] for r in out]
    assert len(out) == ja._JOURNAL_RENDER_CAP
    # the fresh unscored breaking row survived (reserved tail), not truncated
    assert "breaking" in ids
    # consequence still LEADS — a scored row is first, not the unscored breaking one
    assert out[0]["id"].startswith("routine")


def test_journal_render_leads_with_top_magnitude() -> None:
    funeral = _sig("khamenei", "Supreme Leader Khamenei buried", 0.95,
                   "2026-07-12T18:00:00+00:00", "state_media", "leader_death")
    senator = _sig("graham", "Senator Graham dies at 70", 0.30,
                   "2026-07-13T08:00:00+00:00")
    rendered = ja._render_user_prompt([senator, funeral])
    slice_body = rendered.split("--- recent signal slice ---", 1)[1]
    lines = [ln for ln in slice_body.splitlines() if ln.startswith("- ")]
    # the funeral line is FIRST and carries its magnitude tag + citable ref
    assert "Khamenei" in lines[0]
    assert "(salience 0.95·leader_death)" in lines[0]
    assert "[[ref:khamenei]]" in lines[0]
    assert lines[0] != lines[1]  # senator did not also lead


# ---------------------------------------------------------------------------
# T-1b / T-2b — title_en preference + the [untranslated:<lang>] tag in BOTH
# render loops (entry + chronicle).
# ---------------------------------------------------------------------------


def _ar_sig(sid: str, raw_title: str, *, title_en=None, mag: float = 0.9) -> dict:
    """An Arabic signal row as the slice reader shapes it: full payload under
    ``data`` (title/title_en/language), top-level title = payload title."""
    payload: dict = {"title": raw_title, "language": "ar"}
    if title_en is not None:
        payload["title_en"] = title_en
    return {
        "id": sid,
        "title": raw_title,
        "language": "ar",
        "data": payload,
        "produced_at": "2026-07-22T00:00:00+00:00",
        "salience": {"magnitude": mag, "event_class": "escalation"},
    }


def _slice_lines(rendered: str) -> list[str]:
    body = rendered.split("--- recent signal slice ---", 1)[1]
    return [ln for ln in body.splitlines() if ln.startswith("- ")]


def test_entry_render_prefers_title_en_over_raw() -> None:
    row = _ar_sig("s1", "روبيو: إيران تتوسل", title_en="Rubio: Iran is begging")
    line = _slice_lines(ja._render_user_prompt([row]))[0]
    assert "Rubio: Iran is begging" in line       # English shown
    assert "روبيو" not in line                     # raw Arabic NOT shown
    assert "[untranslated" not in line             # has a translation → no tag


def test_entry_render_untranslated_tag_when_no_title_en() -> None:
    row = _ar_sig("s2", "روبيو: إيران تتوسل")       # no title_en
    line = _slice_lines(ja._render_user_prompt([row]))[0]
    assert "[untranslated:ar]" in line             # hazard is visible
    assert "روبيو" in line                          # raw title still rendered


def test_chronicle_render_prefers_title_en_and_tags_untranslated() -> None:
    translated = _ar_sig("c1", "عنوان عربي", title_en="An Arabic headline")
    raw = _ar_sig("c2", "عنوان بدون ترجمة", mag=0.5)   # no title_en
    lines = _slice_lines(ja._render_user_prompt([translated, raw], tier="chronicle"))
    joined = "\n".join(lines)
    assert "An Arabic headline" in joined
    assert "[untranslated:ar]" in joined
    # the translated row carries NO untranslated tag on its own line
    tr_line = next(ln for ln in lines if "An Arabic headline" in ln)
    assert "[untranslated" not in tr_line


def test_english_row_never_untranslated_tagged() -> None:
    row = {"id": "e1", "title": "Plain English headline", "language": "en",
           "data": {"title": "Plain English headline", "language": "en"},
           "produced_at": "2026-07-22T00:00:00+00:00",
           "salience": {"magnitude": 0.5, "event_class": "other"}}
    line = _slice_lines(ja._render_user_prompt([row]))[0]
    assert "[untranslated" not in line


# ---------------------------------------------------------------------------
# T-4(c) — _derive_title accepts a leading BOLD-only line, preferred over the
# first ATX section header (the chronicle emits a bold title but ### sections).
# ---------------------------------------------------------------------------


def test_derive_title_bold_wins_over_first_atx_header() -> None:
    # The chronicle shape: a bold headline, then '### The Gate' as the first
    # section. The bold headline must win, not "The Gate".
    body = (
        "**Trump Signals Iran Ground Operation; Gaza Escalates**\n\n"
        "### The Gate\n\nThe account opens...\n"
    )
    assert ja._derive_title(body, "fallback") == (
        "Trump Signals Iran Ground Operation; Gaza Escalates"
    )


def test_derive_title_atx_still_works_without_bold() -> None:
    body = "# A Plain ATX Title\n\nbody text\n"
    assert ja._derive_title(body, "fallback") == "A Plain ATX Title"


def test_derive_title_bold_deep_in_body_does_not_win() -> None:
    # A bold span appearing only AFTER the first ATX header is not the title.
    body = "# Real Title\n\nsome prose with a **bold phrase** inside\n"
    assert ja._derive_title(body, "fallback") == "Real Title"


def test_derive_title_falls_back_to_first_line() -> None:
    body = "just a first line, no headings\n\nmore\n"
    assert ja._derive_title(body, "fallback") == "just a first line, no headings"


# ---------------------------------------------------------------------------
# T-4(d) — an [[instrument]] perspective span is recorded distinctly in the
# reflect audit (instrument_perspective_span), not left indistinguishable from
# ordinary perspective; the claim itself stays kind='perspective'.
# ---------------------------------------------------------------------------


def test_reflect_instrument_span_flagged_distinctly() -> None:
    body = "My betweenness centrality ticked up this cycle [[instrument]]."
    claims, cited, flags = ja._reflect_claims(body)
    assert "instrument_perspective_span" in flags
    # the claim stays perspective (the honest minimal representation) + exempt
    assert len(claims) == 1
    assert claims[0].kind == "perspective"
    assert claims[0].refs == []


def test_reflect_plain_perspective_not_instrument_flagged() -> None:
    body = "I wonder whether this quiet means something."
    claims, cited, flags = ja._reflect_claims(body)
    assert "instrument_perspective_span" not in flags
    assert claims[0].kind == "perspective"


def test_reflect_instrument_on_world_span_still_downgrades() -> None:
    # An [[instrument]] span wearing WORLD proper nouns AND a factual hint is a
    # citation dodge — it is NOT an honest instrument read; the world-span guard
    # still fires and it falls through to an uncited fact (unchanged behavior). The
    # span needs a factual hint ("was"/a number) or _span_is_factual keeps it
    # perspective on its own (voice-preservation tie-break).
    body = "Israel struck Iran near Hormuz; 40 were killed [[instrument]]."
    claims, cited, flags = ja._reflect_claims(body)
    assert "instrument_marker_on_world_span" in flags
    assert "instrument_perspective_span" not in flags
    assert claims[0].kind == "fact"


# ---------------------------------------------------------------------------
# S-2b — the COMPOSITION input ordering (over the FindingPayload envelope)
# ---------------------------------------------------------------------------


def _finding(fid: str, when: str, mag: float | None) -> dict:
    """A composition-input finding row: salience lives at data->data->salience
    (the FindingPayload envelope), NOT at the top level like a signal row."""
    inner: dict = {"meta": False}
    if mag is not None:
        inner["salience"] = {"magnitude": mag, "event_class": "kinetic_strike",
                             "top_signal_id": f"leaf-of-{fid}"}
    return {
        "id": fid,
        "produced_at": datetime.fromisoformat(when),
        "analyst_id": f"unit-{fid}",
        "data": {"data": inner, "tags": ["meta"]},
    }


def test_extract_input_salience_reads_envelope_nesting() -> None:
    row = _finding("f1", "2026-07-13T00:00:00+00:00", 0.7)
    sal = mfs._extract_input_salience(row)
    assert sal is not None and sal["magnitude"] == 0.7
    assert sal["top_signal_id"] == "leaf-of-f1"
    # unstamped / malformed inputs → None (contribute nothing)
    assert mfs._extract_input_salience(_finding("f2", "2026-07-13T00:00:00+00:00", None)) is None
    assert mfs._extract_input_salience({"data": "not a dict"}) is None
    assert mfs._extract_input_salience({}) is None
    assert mfs._input_salience_magnitude(row) == 0.7
    assert mfs._input_salience_magnitude({}) == -1.0


def test_orient_sorts_by_salience_then_recency() -> None:
    # high-salience but OLDER must beat low-salience NEWER
    hi_old = _finding("hi", "2026-07-12T00:00:00+00:00", 0.9)
    lo_new = _finding("lo", "2026-07-14T00:00:00+00:00", 0.2)
    trimmed, derived, _ = mfs._orient([lo_new, hi_old])
    assert [r["id"] for r in trimmed] == ["hi", "lo"]
    # derived_from follows the same (salience) order
    assert [str(u) for u in derived][:2] == []  # ids are non-UUID → skipped, no crash


def test_orient_byte_for_byte_recency_when_nothing_scored() -> None:
    # before S-1d stamps anything, ALL magnitudes are -1.0 → pure newest-first
    old = _finding("old", "2026-07-10T00:00:00+00:00", None)
    new = _finding("new", "2026-07-14T00:00:00+00:00", None)
    mid = _finding("mid", "2026-07-12T00:00:00+00:00", None)
    trimmed, _, _ = mfs._orient([old, new, mid])
    assert [r["id"] for r in trimmed] == ["new", "mid", "old"]


def test_orient_cap_keeps_highest_salience() -> None:
    rows = [
        _finding("a", "2026-07-14T00:00:00+00:00", 0.1),
        _finding("b", "2026-07-13T00:00:00+00:00", 0.95),  # top consequence
        _finding("c", "2026-07-12T00:00:00+00:00", 0.4),
    ]
    trimmed, _, _ = mfs._orient(rows, cap=1)
    # the cap keeps the highest-consequence input, not the newest
    assert [r["id"] for r in trimmed] == ["b"]


def test_salience_lead_block_present_only_when_scored() -> None:
    scored = [_finding("s", "2026-07-13T00:00:00+00:00", 0.8)]
    block = mfs._render_salience_lead_block(scored)
    assert "SALIENCE ORDERING" in block and "[[ref:1]]" in block
    # nothing scored yet → no directive (byte-for-byte compose)
    unscored = [_finding("u", "2026-07-13T00:00:00+00:00", None)]
    assert mfs._render_salience_lead_block(unscored) == ""


# ---------------------------------------------------------------------------
# S-3 — the advisory salience judge (lead vs top-consequence input)
# ---------------------------------------------------------------------------

_W = "2026-07-13T00:00:00+00:00"


def test_s3_lead_on_top_passes() -> None:
    # inputs are salience-ordered, so ref 1 IS the top; a lead citing ref 1 passes
    sliced = [_finding("top", _W, 0.9), _finding("low", _W, 0.2)]
    sc = mfs._build_salience_check({"magnitude": 0.9, "top_title": "US strikes Iran"},
                                   sliced, [1, 2])
    assert sc["pass"] is True and sc["lead_ref"] == 1 and sc["gap"] == 0.0
    assert sc["top_title"] == "US strikes Iran"


def test_s3_buried_lead_fails() -> None:
    # body cites ref 2 (magnitude 0.2) FIRST while ref 1 is a 0.9 event → burial
    sliced = [_finding("top", _W, 0.9), _finding("low", _W, 0.2)]
    sc = mfs._build_salience_check({"magnitude": 0.9}, sliced, [2, 1])
    assert sc["pass"] is False and sc["lead_ref"] == 2
    assert sc["gap"] == 0.7 and "burial" in sc["reason"]


def test_s3_lead_within_threshold_passes() -> None:
    # gap 0.25 <= _SALIENCE_LEAD_GAP (0.30) → not flagged (ordinary hedging)
    sliced = [_finding("top", _W, 0.9), _finding("mid", _W, 0.65)]
    sc = mfs._build_salience_check({"magnitude": 0.9}, sliced, [2])
    assert sc["pass"] is True and abs(sc["gap"] - 0.25) < 1e-9


def test_s3_uncited_is_not_judgeable() -> None:
    sliced = [_finding("top", _W, 0.9)]
    sc = mfs._build_salience_check({"magnitude": 0.9}, sliced, [])
    assert sc["pass"] is None and "not judgeable" in sc["reason"]


def test_s3_unscored_composition_no_stamp() -> None:
    sliced = [_finding("top", _W, 0.9)]
    assert mfs._build_salience_check({"magnitude": -1.0}, sliced, [1]) is None
    assert mfs._build_salience_check({}, sliced, [1]) is None


def test_s3_lead_on_unscored_input_passes_unjudged() -> None:
    # lead cites an input with no salience → can't compare → pass (advisory,
    # never over-flags), with an explicit reason
    sliced = [_finding("top", _W, 0.9), _finding("unscored", _W, None)]
    sc = mfs._build_salience_check({"magnitude": 0.9}, sliced, [2])
    assert sc["pass"] is True and sc["lead_magnitude"] is None
    assert "no salience" in sc["reason"]


def test_s3_out_of_range_lead_ref_does_not_crash() -> None:
    sliced = [_finding("top", _W, 0.9)]
    sc = mfs._build_salience_check({"magnitude": 0.9}, sliced, [99])
    assert sc["pass"] is True and sc["lead_magnitude"] is None
