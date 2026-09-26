"""D-2 — THE ASSEMBLER. `planning/DEMOTION_D1_SPEC_2026-09-04.md` §1.

What this file proves, in the spec's own order:

  * §1.2 / §3.6 / F-12 — a span is byte-identical to its origin, and a TAMPERED
    span CANNOT be constructed. That is the hard gate, and it lives here rather
    than at verify time because the assembler owns a publish decision and verify
    has no withhold seam to reject into.
  * §1.3 — ``|blocks| == |distinct [[ref:N]] markers|`` and
    ``|derived_from| - |blocks| == drops``.
  * §1.4b — selection is a strict prefix of a TOTAL order.
  * §1.5.2 — the repaired key discriminates where the max-pool cannot.
  * §1.5.3 — the earned-lead test, all three outcomes, and the bars.
  * §1.7 — the drop ledger's closed why-class enum.
  * §1.8 / F-9 — the render is byte-stable, every quoted sentence is a span
    verbatim, the connective vocabulary is closed, the title is deterministic.
  * §5.2 — flag off, the legacy path is unchanged apart from the one field §5.2
    itself mandates on every row.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from legba.data.analysts import assembly_payload as ap
from legba.data.analysts import assembly_render as ar
from legba.data.analysts import assembly_spans as asp
from legba.data.analysts import signal_salience as ss
from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.provenance.composition_integrity import (
    has_collection_denominator_scope,
)
from legba.data.provenance.text_fold import fold_equal, normalize_for_match

# The live shape, U+2011 and em dashes and curly quotes included. Every fixture
# below is cut from the 2026-09-03 narrative_coordination head for Iran, because
# a synthetic ASCII body cannot fail the way the real ones do.
DESK_BODY = (
    "*As of 2026-09-03; slice covers the trailing 72h to that date; 120 signals.*\n"
    "\n"
    "**BLUF:** A coordinated escalation narrative is emerging that links recent "
    "U.S. strikes on Iran—including the deadly wedding attack—to Iranian "
    "retaliatory messaging and President Donald Trump’s hard‑line "
    "threats [35] [40].\n"
    "\n"
    "## What changed\n"
    "- On 2 September the United States “now controls the Strait of "
    "Hormuz” [61].\n"
)

SCOPED_BODY = (
    "**BLUF:** No new infrastructure incidents appear in this desk's collection "
    "for the trailing window.\n"
)


def _row(
    uid: str,
    *,
    analyst_id: str = "narrative_coordination",
    target_id: str | None = "country_watch_ir",
    body: str = DESK_BODY,
    severity: str | None = "high",
    produced_at: str = "2026-09-03T10:01:00+00:00",
    signal_ids: tuple[str, ...] = ("s1", "s2"),
    faithfulness: float | None = 0.90,
    effective: float | None = 0.80,
) -> dict:
    return {
        "id": uid,
        "analyst_id": analyst_id,
        "target_id": target_id,
        "title": f"head {uid}",
        "body": body,
        "severity": severity,
        "produced_at": produced_at,
        "faithfulness_score": faithfulness,
        "effective_confidence": effective,
        "claim_verdicts": [
            {"verdict": "supported"}, {"verdict": "unsupported"},
        ],
        "data": {
            "data": {
                "citations": [
                    {
                        "marker": f"[{i}]",
                        "signal_id": s,
                        "source_id": f"source.{s}",
                        "title": f"signal {s}",
                        "source": f"https://example.test/{s}",
                    }
                    for i, s in enumerate(signal_ids, start=1)
                ]
            }
        },
    }


MAGS = {"s1": 0.95, "s2": 0.70, "s3": 0.55, "s4": 0.10}


def _assembly(rows, *, cap=None, **kw):
    ap.attach_cited_salience(rows, MAGS)
    ordered = sorted(rows, key=ap.order_key)
    carried = ordered[: (cap if cap is not None else ap.BLOCK_CAP)]
    return ordered, ap.build_assembly(
        tier=ap.TIER_COUNTRY,
        as_of="2026-09-03T12:00:00+00:00",
        candidates=ordered,
        carried=carried,
        **kw,
    )


# ---------------------------------------------------------------------------
# §3.1 / F-13 — the shared fold, and the U+2011 lesson as a mechanic
# ---------------------------------------------------------------------------


def test_the_shared_fold_covers_what_the_seven_hand_rolled_ones_missed() -> None:
    """MECH-6 in one assertion, plus the three characters no existing table had.

    ``_UNICODE_HYPHENS`` covers U+2010-2013 only — no U+2014 em dash, no U+2212
    minus — and ``judge_quote_rules`` folds no dash at all. 58.2% of claims
    graded under ``2026-08-28/1`` contain U+2011.
    """
    assert fold_equal("weakly‑supported", "weakly-supported")
    assert fold_equal("Iran—including", "Iran-including")
    assert fold_equal("a−b", "a-b")
    assert fold_equal("so­ft", "soft")          # soft hyphen deleted
    assert fold_equal("【7】", "[7]")        # core-plane full-width
    assert fold_equal("“quoted”", '"quoted"')
    assert normalize_for_match("MASSEẞ") == normalize_for_match("masseß")


def test_the_fold_is_for_matching_only_and_is_not_length_preserving() -> None:
    """The reason storage must never use it: an offset taken against folded text
    does not index the body it claims to describe."""
    raw = "so­ft 【7】"
    assert len(normalize_for_match(raw)) != len(raw)


# ---------------------------------------------------------------------------
# §1.2 / §1.4d — the span, its offsets, its digest and its roles
# ---------------------------------------------------------------------------


def test_lead_span_is_the_bluf_sentence_with_byte_exact_offsets() -> None:
    span = asp.extract_lead_span(_row("a"), head_id="a")
    assert span["role"] == asp.SPAN_ROLE_BLUF
    assert span["text"].startswith("A coordinated escalation narrative")
    assert span["text"].endswith("threats [35] [40].")
    raw = DESK_BODY.encode("utf-8")
    o = span["origin"]
    assert raw[o["start"]:o["end"]].decode("utf-8") == span["text"]
    assert o["head_id"] == "a"
    assert o["body_len"] == len(raw)
    assert o["body_sha256"] == asp.body_sha256(DESK_BODY)


def test_the_sentence_terminator_does_not_cut_at_an_abbreviation() -> None:
    """The measured failure a naive split produces on the first live head read
    for this train: ``recent U.`` is nine words that assert nothing."""
    span = asp.extract_lead_span(_row("a"), head_id="a")
    assert "U.S. strikes" in span["text"]
    # Titles, month abbreviations and single-letter initials are all part of the
    # token, not the end of the sentence.
    one = "Filed by Dr. Ross on 2 Sept. 2026 for the E.U. desk in Q3."
    assert asp.first_sentence_end(one) == len(one)
    two = "The corridor stayed open. Freight volumes held."
    assert asp.first_sentence_end(two) == len("The corridor stayed open.")


def test_span_markers_canonicalise_both_bracket_spellings() -> None:
    assert asp.span_markers("cites [35] and 【40】 and [35] again") == [
        "[35]", "[40]",
    ]


def test_scope_token_is_recorded_when_the_source_sentence_bounds_its_negative() -> None:
    span = asp.extract_lead_span(
        _row("a", body=SCOPED_BODY),
        head_id="a",
        scope_predicate=has_collection_denominator_scope,
    )
    assert span["scope_tokens"] == ["collection_denominator"]


def test_the_locator_fallbacks_stamp_which_one_fired() -> None:
    no_bluf = "## What changed\n- Rail freight fell by a third this week [4].\n"
    assert asp.extract_lead_span({"body": no_bluf}, head_id="x")["role"] == (
        asp.SPAN_ROLE_WHAT_CHANGED
    )
    prose = (
        "# Heading\n\n*As of 2026-09-03*\n\n"
        "The northern corridor stayed open for the whole of the trailing week.\n"
    )
    span = asp.extract_lead_span({"body": prose}, head_id="x")
    assert span["role"] == asp.SPAN_ROLE_BODY
    # The `*As of ...*` stamp is metadata, never a claim — it must not be quoted.
    assert "As of" not in span["text"]


# ---------------------------------------------------------------------------
# §3.6 / F-12 — THE HARD GATE AT ASSEMBLE TIME
# ---------------------------------------------------------------------------


def test_a_span_cannot_be_built_outside_its_origin_body() -> None:
    """F-12's first half. There is no degraded span to publish: offsets that do
    not cut real text out of the origin body raise, and so does an unknown role
    (the vocabulary is closed)."""
    with pytest.raises(asp.SpanConstructionError):
        asp.build_span(
            role=asp.SPAN_ROLE_BLUF, body=DESK_BODY,
            start_char=len(DESK_BODY) + 10, end_char=len(DESK_BODY) + 50,
            head_id="a",
        )
    with pytest.raises(asp.SpanConstructionError):
        asp.build_span(
            role="editorial", body=DESK_BODY,
            start_char=0, end_char=20, head_id="a",
        )


def test_a_tampered_span_cannot_pass_the_gate() -> None:
    """F-12's second half, and the reason the gate is worth having: take a real
    assembly, change ONE character of a quoted span, and it stops verifying
    against the head that wrote it. The tamper is impossible to introduce
    through :func:`build_span` at all — the text is DERIVED from the body — so
    this is the audit an already-persisted payload has to survive."""
    _, payload = _assembly([_row("a")])
    span = payload["blocks"][0]["spans"][0]
    o = span["origin"]
    assert asp.verify_span(DESK_BODY, o["start"], o["end"], span["text"]) is None

    tampered = span["text"].replace("escalation", "de-escalation", 1)
    assert tampered != span["text"]
    why = asp.verify_span(DESK_BODY, o["start"], o["end"], tampered)
    assert why is not None and "not byte-identical" in why
    # And it does not even fold equal, so the message names it a CONTENT bug
    # rather than the unicode-storage one.
    assert "do not even fold equal" in why


def test_a_span_stored_from_NORMALISED_text_is_caught_and_named() -> None:
    """The U+2011 storage bug, deliberately committed, and the diagnosis the
    error message has to give: they fold EQUAL, so this is a storage bug and not
    a content one — which is the difference between a five-minute fix and a day."""
    start = DESK_BODY.index("hard‑line")
    end = start + len("hard‑line")
    raw = DESK_BODY.encode("utf-8")
    bstart = len(DESK_BODY[:start].encode("utf-8"))
    bend = bstart + len(DESK_BODY[start:end].encode("utf-8"))
    assert raw[bstart:bend].decode("utf-8") == "hard‑line"
    why = asp.verify_span(DESK_BODY, bstart, bend, "hard-line")
    assert why is not None and "fold EQUAL" in why


def test_a_block_whose_span_fails_takes_the_run_down_loudly() -> None:
    """No silent degradation and no empty read (§3.6, the no-stubs rule)."""
    with pytest.raises(ap.AssemblyConstructionError):
        _assembly([_row("a", body="   \n\n   ")])
    with pytest.raises(ap.AssemblyConstructionError):
        ap.build_assembly(
            tier=ap.TIER_WORLD, as_of="2026-09-03T12:00:00+00:00",
            candidates=[], carried=[],
        )


# ---------------------------------------------------------------------------
# §1.5.2 — the salience repair
# ---------------------------------------------------------------------------


def test_cited_mass_discriminates_where_the_max_pool_cannot() -> None:
    """The measured defect: composition-tier magnitude sd 0.024 across 315 rows.
    Two desks that cite different things must not score the same."""
    loud = ss.cited_salience(
        [{"signal_id": "s1"}, {"signal_id": "s2"}], MAGS
    )
    quiet = ss.cited_salience([{"signal_id": "s4"}], MAGS)
    assert loud["cited_mass"] > quiet["cited_mass"]
    assert loud["cited_max"] == 0.95 and loud["n_above"] == 2
    # s4 sits UNDER the background floor, so it contributes no mass at all.
    assert quiet["cited_mass"] == 0.0 and quiet["n_cited_scored"] == 1


def test_an_unscored_signal_contributes_nothing_rather_than_zero() -> None:
    """1.8% of live signals are unscored. Counting one as 0.0 would say the desk
    cited something inconsequential, which is a different and false claim."""
    r = ss.cited_salience([{"signal_id": "unknown"}], MAGS)
    assert r["n_cited"] == 1 and r["n_cited_scored"] == 0


def test_a_repeated_marker_is_one_piece_of_evidence() -> None:
    once = ss.cited_salience([{"signal_id": "s1"}], MAGS)
    twice = ss.cited_salience([{"signal_id": "s1"}, {"signal_id": "s1"}], MAGS)
    assert once == twice


def test_pool_salience_keeps_magnitude_byte_identical_to_max_salience() -> None:
    """The compat contract that keeps `salience_sort_key`, `_build_salience_check`
    and every unit-tier consumer untouched (§1.5.2, blast radius)."""
    kids = [{"magnitude": 0.9, "cited_mass": 1.0}, {"magnitude": 0.95, "cited_mass": 2.0}]
    assert ss.pool_salience(kids)["magnitude"] == ss.max_salience(kids)["magnitude"]
    assert ss.pool_salience(kids)["version"] == "cited_mass.v1"


def test_a_composition_candidate_is_scored_on_its_children_not_on_zero() -> None:
    """The world tier's candidates cite FINDINGS, so they carry no signal_id at
    all. Scoring them directly returns 0.0 for every candidate — a flat pool and
    a permanently unearned lead, silently."""
    parent = {"id": "p", "data": {"data": {"citations": [{"ref_id": "x"}]}}}
    ids, source = ap.row_signal_ids(parent, {"p": ["s1", "s2"]})
    assert source == "pooled_child_signals" and ids == ["s1", "s2"]
    assert ss.salience_over_ids(ids, MAGS, source=source)["cited_mass"] > 0.0


# ---------------------------------------------------------------------------
# §1.5.1 / §1.4b — a TOTAL, reproducible order and a strict prefix
# ---------------------------------------------------------------------------


def test_the_order_is_total_and_never_decided_by_clock_jitter() -> None:
    """VOICE §2.c measured the 09-01 world headline decided by a FIVE-SECOND
    produced_at gap between two blocks tied at 0.95."""
    a = _row("a", produced_at="2026-09-03T10:00:00+00:00", signal_ids=("s1",))
    b = _row("b", produced_at="2026-09-03T10:00:05+00:00", signal_ids=("s1",))
    ap.attach_cited_salience([a, b], MAGS)
    assert ap.order_key(a) != ap.order_key(b)
    # Recency DESCENDS: the newer head leads its tie band.
    assert [r["id"] for r in sorted([a, b], key=ap.order_key)] == ["b", "a"]
    # Same inputs, same order, every time — what the A/B replay needs.
    assert [r["id"] for r in sorted([a, b], key=ap.order_key)] == \
           [r["id"] for r in sorted([b, a], key=ap.order_key)]
    # …and the LAST tiebreak is the finding id ASCENDING, so nothing is ever
    # left to insertion order.
    c = _row("z", produced_at="2026-09-03T10:00:00+00:00", signal_ids=("s1",))
    ap.attach_cited_salience([c], MAGS)
    assert [r["id"] for r in sorted([c, a], key=ap.order_key)] == ["a", "z"]


def test_severity_outranks_mass_and_mass_outranks_recency() -> None:
    weak_but_critical = _row("a", severity="critical", signal_ids=("s4",))
    loud_but_low = _row("b", severity="low", signal_ids=("s1", "s2"))
    ap.attach_cited_salience([weak_but_critical, loud_but_low], MAGS)
    assert [r["id"] for r in sorted(
        [loud_but_low, weak_but_critical], key=ap.order_key
    )] == ["a", "b"]


def test_selection_is_a_strict_prefix_of_the_order() -> None:
    rows = [
        _row(f"h{i}", signal_ids=("s1",) if i < 3 else ("s4",))
        for i in range(10)
    ]
    ordered, payload = _assembly(rows)
    carried = [b["finding_id"] for b in payload["blocks"]]
    assert carried == [r["id"] for r in ordered[: ap.BLOCK_CAP]]
    dropped_ranks = [d["rank"] for d in payload["drops"]["shown_not_carried"]]
    assert min(dropped_ranks) > len(carried)


# ---------------------------------------------------------------------------
# §1.3 — the three numbers collapse to one
# ---------------------------------------------------------------------------


def test_blocks_equal_distinct_ref_markers_in_the_rendered_body() -> None:
    import re

    rows = [_row(f"h{i}") for i in range(5)]
    _, payload = _assembly(rows)
    body = ar.render_assembly_body(payload)
    markers = {int(m) for m in re.findall(r"\[\[ref:(\d+)\]\]", body)}
    assert markers == {b["ordinal"] for b in payload["blocks"]}


def test_derived_from_minus_blocks_equals_the_drop_count() -> None:
    rows = [_row(f"h{i}") for i in range(11)]
    ordered, payload = _assembly(rows)
    counts = payload["drops"]["counts"]
    assert len(ordered) - len(payload["blocks"]) == (
        counts["shown_not_carried"] + counts["trimmed"]
    )


def test_every_drop_carries_a_why_from_the_closed_enum() -> None:
    rows = [_row(f"h{i}") for i in range(11)]
    _, payload = _assembly(rows)
    drops = payload["drops"]
    seen = {
        d["why"]
        for grain in ("shown_not_carried", "not_selected", "trimmed", "below_floor")
        for d in drops[grain]
    }
    assert seen and seen <= set(ap.WHY_CLASSES)
    assert drops["counts"]["invisible_heads"] is None  # never a fabricated zero


# ---------------------------------------------------------------------------
# §1.5.3 — the earned-lead test
# ---------------------------------------------------------------------------


def test_the_earned_lead_bars_are_exactly_the_specced_two_numbers() -> None:
    assert (ap.RATIO_BAR, ap.SHARE_BAR, ap.MIN_LEAD_CANDIDATES) == (1.50, 0.15, 8)


def test_earned_when_and_only_when_both_bars_clear() -> None:
    earned = ap.earned_lead([10.0] + [1.0] * 9)
    assert earned["earned"] and earned["ratio_12"] == 10.0
    assert earned["top_share"] >= ap.SHARE_BAR
    # Concentrated enough on ratio, not on share → NOT earned. This is the pair
    # of bars doing its job: a big gap over a long flat tail is not a driver.
    thin = ap.earned_lead([2.0, 1.0] + [1.0] * 30)
    assert thin["ratio_12"] >= ap.RATIO_BAR and not thin["earned"]
    # A pool too small to have a shape cannot crown one.
    assert not ap.earned_lead([10.0, 0.1])["earned"]


def test_ratio_is_null_not_infinity_when_the_runner_up_scores_zero() -> None:
    """An infinity is not round-trippable through JSONB and a sentinel float
    would be read as a measurement."""
    assert ap.earned_lead([3.0] + [0.0] * 9)["ratio_12"] is None


def test_all_three_lead_outcomes_are_reachable_and_stamped() -> None:
    kinds = set()
    for masses in ([9.0] + [0.5] * 9, [1.0, 0.9, 0.85] + [0.05] * 9, [1.0] * 10):
        rows = [
            _row(f"h{i}", signal_ids=())
            for i in range(len(masses))
        ]
        ap.attach_cited_salience(rows, {})
        for row, m in zip(rows, masses):
            row[ap.CITED_SALIENCE_ROW_KEY]["cited_mass"] = m
        ordered = sorted(rows, key=ap.order_key)
        payload = ap.build_assembly(
            tier=ap.TIER_COUNTRY, as_of="2026-09-03T12:00:00+00:00",
            candidates=ordered, carried=ordered[: ap.BLOCK_CAP],
        )
        kinds.add(payload["lead"]["kind"])
        # The reason is on the ROW whichever branch fired — never in a log.
        assert payload["lead"]["test"]["key"] == "cited_mass.v1"
        assert set(payload["lead"]["test"]) >= {
            "top_share", "ratio_12", "bar_share", "bar_ratio", "earned",
            "n_candidates",
        }
    assert kinds == {ap.LEAD_EARNED_SINGLE, ap.LEAD_CO_LEADS, ap.LEAD_NONE}


# ---------------------------------------------------------------------------
# §1.6 — tensions, and the CHECKED NEGATIVE
# ---------------------------------------------------------------------------


def test_a_no_conflict_declaration_always_says_what_it_checked() -> None:
    """M-11 in a new costume is the failure this prevents: a `## Tension` section
    asserting unanimity with no statement of scope. The live degenerate form was
    13.8% of world bodies."""
    _, payload = _assembly([_row("a"), _row("b")])
    checked = payload["tension_checked"]
    assert checked["pairs_examined"] == 1 and checked["pairs_found"] == 0
    assert checked["scope_note"] == "BLUF-grain; ambivalent pairs declined"
    body = ar.render_assembly_body(payload)
    assert "no conflicting pair was detected among the 2 shown blocks" in body
    assert "pairs examined" in body


def test_a_detected_pair_is_declared_and_never_explained() -> None:
    up = _row("a", body="**BLUF:** Oil exports through the corridor rose sharply.\n")
    down = _row(
        "b", analyst_id="energy_security",
        body="**BLUF:** Oil exports through the corridor fell sharply.\n",
    )
    _, payload = _assembly([up, down])
    assert payload["tension_checked"]["pairs_found"] == 1
    t = payload["tensions"][0]
    assert t["statement_source"] == "template"
    assert t["detector"] == "direction_conflict"
    assert "point in opposite directions" in t["statement"]
    # DECLARE, never EXPLAIN — the template may not offer a mechanism.
    for banned in ("because", "which drives", "indicating", "underpinning"):
        assert banned not in t["statement"]


# ---------------------------------------------------------------------------
# W-1 (2026-09-06) — a CROSS-TIER pair is not a conflict the record published
# ---------------------------------------------------------------------------
#
# The live defect, from the 12:00Z world read: its ONE declared tension paired a
# CARRIED ``country_g20_us`` head with an UNCARRIED ``country_g20_cn`` one, and
# the template statement was symmetric — "country_composition on country_g20_us
# and country_composition on country_g20_cn describe the same dimension in the
# same window and point in opposite directions". Nothing in that sentence, in
# ``tension_checked``, or in the arithmetic the Assessment is handed said the
# second side was never carried. The 12:15Z Assessment narrated it as a live
# conflict and the judge could only grade the sentence unsupported.


def _up_down_pair():
    """One head pointing UP and one pointing DOWN on the same dimension."""
    up = _row(
        "carried",
        target_id="country_g20_us",
        body="**BLUF:** Oil exports through the corridor rose sharply.\n",
    )
    down = _row(
        "dropped",
        analyst_id="energy_security",
        target_id="country_g20_cn",
        body="**BLUF:** Oil exports through the corridor fell sharply.\n",
    )
    return up, down


def test_a_carried_pair_statement_is_byte_identical_to_the_pre_w1_template() -> None:
    """The repair moves the CROSS-TIER arm and nothing else. Two carried blocks
    are symmetric and their sentence stays symmetric with them, to the byte."""
    up, down = _up_down_pair()
    _, payload = _assembly([up, down])
    t = payload["tensions"][0]
    assert t["kind"] == "carried_pair"
    assert t["b_carried"] is True and t["b_why"] is None
    assert t["statement"] == (
        "narrative_coordination on country_g20_us and energy_security on "
        "country_g20_cn describe the same dimension in the same window and "
        "point in opposite directions."
    )


def test_a_tension_against_an_uncarried_head_says_so_in_its_own_statement() -> None:
    """W-1. The pair is still DECLARED (D-1 §1.6 rule 3 — a tension whose other
    half was dropped is the sharpest argument for the drop ledger). What it may
    no longer do is READ like a conflict between two published reads."""
    up, down = _up_down_pair()
    _, payload = _assembly([up, down], cap=1)

    t = payload["tensions"][0]
    assert t["kind"] == "carried_vs_dropped"
    # STRUCTURAL, so a reader never has to parse the prose to learn it.
    assert t["b_carried"] is False
    assert t["b_why"] == ap.WHY_SHOWN_NOT_SELECTED
    # ...and in the sentence itself, because the rendered body is what the
    # Assessment voice actually reads.
    assert "did NOT carry" in t["statement"]
    assert ap.WHY_SHOWN_NOT_SELECTED in t["statement"]
    assert "not with a read this record published" in t["statement"]
    # DECLARE, never EXPLAIN survives the repair.
    for banned in ("because", "which drives", "indicating", "underpinning"):
        assert banned not in t["statement"]
    # The uncarried side is exactly the head the ledger dropped.
    assert t["b_ref"]["target_id"] == "country_g20_cn"
    assert any(
        d["finding_id"] == "dropped" for d in payload["drops"]["shown_not_carried"]
    )


def test_the_checked_negative_splits_its_two_populations() -> None:
    """``pairs_found`` alone was the number that misled: it told the voice "a
    tension was declared" and nothing about which population it came from. Split,
    the same fact is quotable AND gradeable — the split rides the record's own
    arithmetic, which travels in every citation's evidence map."""
    up, down = _up_down_pair()

    _, both_carried = _assembly([up, down])
    checked = both_carried["tension_checked"]
    assert checked["pairs_found"] == 1
    assert checked["pairs_found_carried"] == 1
    assert checked["pairs_found_uncarried"] == 0

    _, cross_tier = _assembly([up, down], cap=1)
    checked = cross_tier["tension_checked"]
    assert checked["pairs_found"] == 1
    assert checked["pairs_found_carried"] == 0
    assert checked["pairs_found_uncarried"] == 1
    # The split always closes against the total it splits.
    assert (
        checked["pairs_found_carried"] + checked["pairs_found_uncarried"]
        == checked["pairs_found"]
    )


def test_the_record_arithmetic_hands_the_voice_the_split() -> None:
    """The end of the chain W-1 exists to repair. The arithmetic block is one of
    the three things the Assessment is handed AND is minted into every citation's
    ``evidence_text``, so a sentence resting on it is gradeable. Before the
    split, "1 declared" was the only fact available and no honest sentence about
    it could be supported."""
    from legba.data.analysts import assessment_prompts as apr

    up, down = _up_down_pair()
    _, cross_tier = _assembly([up, down], cap=1)
    text = apr.record_arithmetic(cross_tier)
    assert "0 between two carried reads" in text
    assert "1 against a read this record did NOT carry" in text
    assert "NOT a conflict this record published" in text


def test_a_pre_w1_record_renders_its_arithmetic_byte_for_byte() -> None:
    """The guard is PRESENCE of the split keys, not their value: every record
    written before W-1 replays through the prompt builder unchanged, which is
    what keeps the byte-identity proof over the shipped rows honest."""
    from legba.data.analysts import assessment_prompts as apr

    up, down = _up_down_pair()
    _, payload = _assembly([up, down], cap=1)
    legacy = json.loads(json.dumps(payload))
    legacy["tension_checked"].pop("pairs_found_carried")
    legacy["tension_checked"].pop("pairs_found_uncarried")

    assert "- tensions: 1 declared out of 1 pairs examined" in apr.record_arithmetic(
        legacy
    )
    assert "between two carried reads" not in apr.record_arithmetic(legacy)


# ---------------------------------------------------------------------------
# §1.8 / F-9 — the render and the title
# ---------------------------------------------------------------------------


def test_the_render_is_byte_stable() -> None:
    _, payload = _assembly([_row(f"h{i}") for i in range(4)])
    assert ar.render_assembly_body(payload) == ar.render_assembly_body(payload)


def test_every_quoted_sentence_in_the_body_is_a_span_verbatim() -> None:
    from legba.data.analysts.assembly_spans import quoted_spans

    _, payload = _assembly([_row(f"h{i}") for i in range(4)])
    body = ar.render_assembly_body(payload)
    for b in payload["blocks"]:
        # P3-A: the fixture is a COUNTRY assembly, so each block also carries its
        # origin head in FULL under ``context_body``. That span is deliberately
        # NOT in the body — it is handed to the fenced reader, never rendered —
        # and the assertion below pins exactly that. The QUOTATIONS are unchanged.
        for span in quoted_spans(b):
            assert ar.quoted_text(span) in body
            # And the span itself is byte-identical to its origin.
            o = span["origin"]
            raw = DESK_BODY.encode("utf-8")
            assert raw[o["start"]:o["end"]].decode("utf-8") == span["text"]
        for span in b["spans"]:
            # Byte-identity to the origin holds for EVERY span, context included
            # — that is what makes the context span a span rather than a copy.
            o = span["origin"]
            raw = DESK_BODY.encode("utf-8")
            assert raw[o["start"]:o["end"]].decode("utf-8") == span["text"]


def test_no_connective_in_the_vocabulary_asserts_a_state_of_the_world() -> None:
    """The pin, in the shape ``BANNED_PHRASE_MARKERS`` uses. Every entry
    describes the READ; none describes the world."""
    for c in ar.CONNECTIVES:
        low = c.lower()
        for verb in (" rises", " rising", " escalat", " threat", " risk is",
                     " worsen", " improv", " likely", " suggests"):
            assert verb not in low, f"connective {c!r} asserts a state of the world"


def test_the_title_is_deterministic_in_all_three_lead_shapes() -> None:
    _, earned = _assembly([_row("a", signal_ids=("s1", "s2"))] + [
        _row(f"h{i}", signal_ids=()) for i in range(9)
    ])
    assert earned["lead"]["kind"] == ap.LEAD_EARNED_SINGLE
    t = ar.assembly_title(earned)
    assert t == ar.assembly_title(earned)
    assert "“" in t and "A coordinated escalation narrative" in t
    assert len(t) <= 200

    _, flat = _assembly([_row(f"h{i}", signal_ids=()) for i in range(9)])
    assert flat["lead"]["kind"] == ap.LEAD_NONE
    assert ar.assembly_title(flat).endswith("verified desk reads, 1 not carried")


def test_the_title_carries_no_authored_claim_only_a_quotation() -> None:
    """F-9: the assembly's title needs no rubric because it makes no claim of its
    own — its only content is text a desk already wrote and the record quotes."""
    _, payload = _assembly([_row("a", signal_ids=("s1", "s2"))] + [
        _row(f"h{i}", signal_ids=()) for i in range(9)
    ])
    fragment = ar.assembly_title(payload).split("“", 1)[1].rstrip("”…")
    assert fragment.split("…")[0] in payload["blocks"][0]["spans"][0]["text"]


def test_a_child_tiers_ref_markers_are_defused_in_the_render_only() -> None:
    """A span cut from a lower COMPOSITION carries its own ``[[ref:N]]``, which
    would collide with this tier's ordinal space. The payload keeps the raw
    bytes; only the render defuses."""
    child = "**BLUF:** The corridor stayed open all week [[ref:3]].\n"
    _, payload = _assembly([_row("a", body=child)])
    span = payload["blocks"][0]["spans"][0]
    assert "[[ref:3]]" in span["text"]
    assert "(child ref 3)" in ar.render_assembly_body(payload)


# ---------------------------------------------------------------------------
# §1.1 / §1.4c — severity, confidence and the persisted coverage ledger
# ---------------------------------------------------------------------------


def test_severity_reaches_its_read_column_as_the_max_over_carried_blocks() -> None:
    """NULL on 100% of composition rows today (0 of 1,108 in 14 days). It travels
    as a tag the write path lifts to the column, which is what finally makes the
    dead severity dot in the world history list render."""
    rows = [_row("a", severity="elevated"), _row("b", severity="critical")]
    _, payload = _assembly(rows)
    assert ap.assembly_severity(payload["blocks"]) == "critical"
    assert "severity:critical" in ap.assembly_tags(payload)
    from legba.data.provenance.models import severity_from_tags

    assert severity_from_tags(ap.assembly_tags(payload)) == "critical"


def test_evidence_age_is_real_and_an_unknown_age_stays_unknown() -> None:
    """FRAME-1's rule, inherited rather than re-derived: `None` never 0.0, because
    a zero reads as "composed just now"."""
    _, dated = _assembly([_row("a")])
    assert dated["blocks"][0]["evidence_age_h"] > 0
    _, undated = _assembly([_row("b", produced_at=None)])
    assert undated["blocks"][0]["evidence_age_h"] is None


def test_confidence_is_the_weakest_carried_block() -> None:
    rows = [_row("a", effective=0.9), _row("b", effective=0.4)]
    _, payload = _assembly(rows)
    assert ap.assembly_confidence(payload) == 0.4


def test_the_coverage_ledger_is_persisted_verbatim() -> None:
    """§1.4c — computed today, rendered to the prompt and THROWN AWAY. Persisting
    it makes `metadata_mismatch` impossible rather than merely detectable."""
    ledger = [
        {"unit": "escalation", "status": "in_basis", "age_h": 2.0},
        {"unit": "energy_security", "status": "no_head_in_horizon"},
    ]
    _, payload = _assembly([_row("a")], coverage=ledger)
    assert payload["coverage"] == ledger
    assert payload["drops"]["no_head"] == [
        {"unit": "energy_security", "why": "no_head_in_horizon"}
    ]
    assert "energy_security: no read inside the horizon" in ar.render_assembly_body(
        payload
    )


# ---------------------------------------------------------------------------
# F-3 — the bounded question, and its honest fallback
# ---------------------------------------------------------------------------


def test_the_bounded_question_falls_back_and_stamps_that_it_did() -> None:
    """No descriptor field holds one today (F-3). The fallback rate has to be
    COUNTABLE, not invisible, so the parallel descriptor train can be measured."""
    _, without = _assembly([_row("a")])
    b = without["blocks"][0]
    assert b["question"] == "Narrative coordination"
    assert b["question_source"] == "fallback_desk_name"

    _, with_q = _assembly(
        [_row("a")],
        questions={"narrative_coordination": "Is a coordinated narrative emerging?"},
    )
    b = with_q["blocks"][0]
    assert b["question"] == "Is a coordinated narrative emerging?"
    assert b["question_source"] == "descriptor"


# ---------------------------------------------------------------------------
# §5.2 — the flag, and the one field that rides every row regardless
# ---------------------------------------------------------------------------


def test_the_flag_is_off_by_default_and_takes_a_per_analyst_allowlist(
    monkeypatch,
) -> None:
    monkeypatch.delenv(ap.ASSEMBLY_ENV, raising=False)
    assert not ap.assembly_enabled("world_assessor")
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "0")
    assert not ap.assembly_enabled("world_assessor")
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "1")
    assert ap.assembly_enabled("world_assessor")
    # The rollout walks the tiers without a deploy.
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "world_assessor")
    assert ap.assembly_enabled("world_assessor")
    assert not ap.assembly_enabled("country_composition")


def test_the_regime_label_rides_every_row_even_with_the_flag_off() -> None:
    """§5.2 / F-4. The stamp splits on CODE and this cutover is a runtime FLAG,
    so without this field the A/B boundary is invisible inside one stamp — the
    08-12 shape, where a stamp pooled a judge outage with a working period."""
    legacy = ap.legacy_regime_stamp()
    assert legacy == {"schema": "assembly.v1", "regime": "legacy"}
    _, payload = _assembly([_row("a")])
    assert payload["regime"] == "assembly"


def test_the_extra_signal_query_is_not_issued_with_the_flag_off(monkeypatch) -> None:
    from legba.data.analysts import assembly_salience as asal

    monkeypatch.delenv(ap.ASSEMBLY_ENV, raising=False)
    assert not asal.assembly_any_enabled()
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "world_assessor")
    assert asal.assembly_any_enabled()


def test_the_synthesizer_re_exports_the_moved_and_new_names() -> None:
    """The seam extraction is invisible to every importer (the house rule the
    module-size gate states in its own failure message)."""
    for name in (
        "REGION_MODE_GAP", "THEMATIC_MODE_PRESENT", "_is_region_target",
        "_assemble_world_region_slice", "_assemble_thematic_unit_slice",
        "READ_SLICE", "COUNTRY_COMPOSITION_ANALYST_ID",
        "assembly_enabled", "build_assembly", "render_assembly_body",
        "assembly_title", "legacy_regime_stamp", "BLOCK_CAP",
    ):
        assert hasattr(synth, name), name


# ---------------------------------------------------------------------------
# THE REAL BINDING PATH — `_run` itself, both arms
#
# The house discipline (`feedback-tests-must-traverse-real-binding`): audit the
# path the runtime actually takes, not the intent. Everything above tests the
# leaves; this tests that `_run` reaches them, and that with the flag off it
# does not.
# ---------------------------------------------------------------------------


class _CannedLLM:
    subprovider = "d2_test_double"

    def __init__(self) -> None:
        self.calls = 0

    async def chat_complete(self, messages, **kwargs):  # noqa: ANN001
        self.calls += 1

        class _U:
            prompt_tokens = 10
            completion_tokens = 5
            reasoning_tokens = 0

        class _R:
            content = json.dumps({
                "title": "Legacy prose title",
                "body": "BLUF: the units point in one direction [[ref:1]].",
                "confidence": 0.55,
                "tags": ["composition"],
            })
            usage = _U()

        return _R()


class _NeverCalledLLM:
    subprovider = "never_called"

    async def chat_complete(self, *a, **k):  # pragma: no cover
        raise AssertionError(
            "the assembly path is DETERMINISTIC — it must not call the model"
        )


class _Deps:
    def __init__(self, llm) -> None:  # noqa: ANN001
        self.llm = llm


def _slice_row(uid, analyst_id, **kw):  # noqa: ANN001
    row = _row(str(uid), analyst_id=analyst_id, target_id="country_g20_in", **kw)
    row.update({
        "id": uid,
        "kind": "finding",
        "evidence": [],
        "confidence": 0.7,
        "target_version": None,
        "analyst_version": "vtest",
        "derived_from": [],
        "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
        "run_id": uuid4(),
    })
    return row


_OPTIONS = {
    "analyst_id": "country_composition",
    "target_id": "country_g20_in",
    "composition": True,
}


@pytest.mark.asyncio
async def test_flag_off_the_legacy_path_runs_and_only_the_regime_field_is_new(
    monkeypatch,
) -> None:
    """§5.2 — the old path stays runnable for the whole program, and that is a
    test rather than a hope. The ONE payload difference is the field §5.2 itself
    mandates on every composition row from D-2's merge."""
    monkeypatch.delenv(ap.ASSEMBLY_ENV, raising=False)
    llm = _CannedLLM()
    rows = [
        _slice_row(uuid4(), "leadership_transition"),
        _slice_row(uuid4(), "energy_security"),
    ]
    result = await synth.run_method(list(rows), dict(_OPTIONS), _Deps(llm))
    assert llm.calls == 1, "flag off must still call the model"
    assert result.finding.title == "Legacy prose title"
    assert result.finding.data["assembly"] == {
        "schema": "assembly.v1", "regime": "legacy",
    }
    assert "blocks" not in result.finding.data["assembly"]


@pytest.mark.asyncio
async def test_flag_on_the_run_assembles_and_never_calls_the_model(
    monkeypatch,
) -> None:
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "country_composition")
    ids = [uuid4() for _ in range(3)]
    rows = [
        _slice_row(ids[0], "leadership_transition", signal_ids=("s4",)),
        _slice_row(ids[1], "energy_security", signal_ids=("s1", "s2")),
        _slice_row(ids[2], "escalation", signal_ids=("s3",)),
    ]
    ap.attach_cited_salience(rows, MAGS)
    result = await synth.run_method(
        list(rows), dict(_OPTIONS), _Deps(_NeverCalledLLM())
    )

    payload = result.finding.data["assembly"]
    assert payload["schema"] == "assembly.v1"
    assert payload["regime"] == "assembly"
    assert payload["tier"] == "country"
    assert [b["ordinal"] for b in payload["blocks"]] == [1, 2, 3]

    # §1.3 — ordinal N of the assembly IS entry N of derived_from, which is what
    # makes the drop ledger computable. The order is the ASSEMBLY order, not the
    # arrival order: the loudest desk leads.
    assert [str(u) for u in result.derived_from] == [
        b["finding_id"] for b in payload["blocks"]
    ]
    assert payload["blocks"][0]["desk"] == "energy_security"

    # §1.3 — the three numbers collapse to one, through the EXISTING cite path.
    cites = result.finding.data["citations"]
    assert len(cites) == len(payload["blocks"])
    assert [c["ref_id"] for c in cites] == [
        b["finding_id"] for b in payload["blocks"]
    ]

    # §1.1 — severity reaches the row for the first time in this tier's life.
    assert "severity:high" in result.finding.tags
    # The body is the render, and the title is the deterministic one.
    assert result.finding.body == ar.render_assembly_body(payload)
    assert result.finding.title == ar.assembly_title(payload)
    assert "## The record" in result.finding.body
    # Zero tokens: the tier stopped writing.
    assert result.usage == {"prompt_tokens": 0, "completion_tokens": 0}
    assert any(s.get("phase") == "assemble" for s in result.intermediate_steps)
    # …and no field claims a model wrote something. `raw_llm_response` is
    # documented as "the LLM's raw JSON for audit"; a row where no LLM ran must
    # not carry one, however convenient the 8,000 spare characters would be.
    assert "raw_llm_response" not in result.finding.data
    assert not any(s.get("kind") == "llm_call" for s in result.intermediate_steps)


@pytest.mark.asyncio
async def test_the_assembly_run_is_reproducible_byte_for_byte(monkeypatch) -> None:
    """What the A/B arm needs and what the legacy path could never offer: the
    same candidate set yields the same read."""
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "1")
    ids = [uuid4() for _ in range(4)]

    def _rows():
        rows = [
            _slice_row(ids[i], f"desk_{i}", signal_ids=("s1",) if i else ("s4",))
            for i in range(4)
        ]
        ap.attach_cited_salience(rows, MAGS)
        return rows

    a = await synth.run_method(_rows(), dict(_OPTIONS), _Deps(_NeverCalledLLM()))
    b = await synth.run_method(
        list(reversed(_rows())), dict(_OPTIONS), _Deps(_NeverCalledLLM())
    )
    assert a.finding.body == b.finding.body
    assert a.finding.title == b.finding.title
    assert a.derived_from == b.derived_from


# ---------------------------------------------------------------------------
# D-2b — THE OWED SMALLS (D-2 §10 handoff, D-3 §2, R4 PREREG E-3)
#
# Two producer-side fields the arms were written against and the row did not
# carry. Until they land, D-3's ARM 3 counts
# ``assembly_attribution_date_unverifiable`` on every block and ARM 4(a) counts
# ``assembly_coverage_roster_absent`` on every read — so R4's H-G7 zero would be
# a statement about 14 of 16 reason codes rather than 16. Both are ADDITIVE:
# ``assembly.v1`` gains fields and nothing that existed changes shape.
#
# The third owed small, P0c, is ALREADY CLOSED (17b8567f): the capture ceiling
# equals the render ceiling and ``test_composition_evidence_window.py`` pins them
# equal (``test_the_capture_ceiling_cannot_drift_from_the_render_ceiling``).
# Nothing is added for it here.
# ---------------------------------------------------------------------------


def test_d2b_the_citation_bridge_carries_the_origin_heads_timestamp() -> None:
    """ARM 3 compares ``blocks[].produced_at`` against the CITATION's. Both are
    now stamped from the same row by the same helper, so a disagreement is a
    generator defect rather than a formatting artefact."""
    row = _row(str(uuid4()), produced_at="2026-09-03T10:01:00+00:00")
    citation = synth._build_composition_citation(1, row)
    assert citation is not None
    assert citation["produced_at"] == "2026-09-03T10:01:00+00:00"

    _, payload = _assembly([row])
    assert payload["blocks"][0]["produced_at"] == citation["produced_at"]


def test_d2b_a_datetime_row_stamps_the_same_instant_the_block_does() -> None:
    """The live row hands back a ``datetime``, not a string. The two capture
    paths must agree on THAT shape too — it is the one the fleet runs."""
    from datetime import datetime, timezone

    when = datetime(2026, 9, 3, 10, 1, tzinfo=timezone.utc)
    row = _row(str(uuid4()))
    row["produced_at"] = when
    citation = synth._build_composition_citation(1, row)
    assert citation is not None
    _, payload = _assembly([row])
    assert citation["produced_at"] == payload["blocks"][0]["produced_at"]
    assert citation["produced_at"] == when.isoformat()


def test_d2b_an_undated_row_declines_rather_than_inventing_a_date() -> None:
    """Guarded like ``title``/``effective_confidence``. An absent stamp stays
    absent so ARM 3 counts ``assembly_attribution_date_unverifiable`` — undecided,
    never passed — instead of grading a block against a fabricated instant."""
    row = _row(str(uuid4()))
    row["produced_at"] = None
    citation = synth._build_composition_citation(1, row)
    assert citation is not None
    assert "produced_at" not in citation


def test_d2b_the_coverage_roster_is_persisted_as_the_ledgers_denominator() -> None:
    """§1.4c's other half. D-2 persisted the ledger; the ROSTER it is the
    denominator OF stayed unpersisted, and ARM 4(a) refuses to reconstruct one
    from the rows that ARRIVED — that reconstruction is the original defect."""
    roster = ["narrative_coordination", "escalation", "energy_security"]
    ledger = [
        {"unit": "narrative_coordination", "status": "in_basis", "age_h": 2.0},
        {"unit": "escalation", "status": "below_floor", "age_h": 9.0},
        {"unit": "energy_security", "status": "no_head_in_horizon"},
    ]
    _, payload = _assembly([_row("a")], coverage=ledger, coverage_roster=roster)
    assert payload["coverage_roster"] == roster
    assert [c["unit"] for c in payload["coverage"]] == roster
    # …and the ledger it was persisted beside is untouched.
    assert payload["coverage"] == ledger


def test_d2b_the_roster_travels_with_the_ledger_or_not_at_all() -> None:
    """D-2 §10: ARM 4(a) is BLIND above the country tier, where the roster-based
    ledger is not computed. A roster published beside an EMPTY ledger would make
    every declared unit read as missing — a fabricated fire, which is worse than
    the honest blindness. So the default is ``[]``, which the arm declines on."""
    _, payload = _assembly([_row("a")])
    assert payload["coverage"] == []
    assert payload["coverage_roster"] == []


def test_d2b_the_two_fields_un_decline_the_two_arms_they_were_owed_by() -> None:
    """The point of the item, in the arms' own counters: with both fields on the
    row the two checks RUN. These are the counters PREREG E-3 names as the reason
    G7's zero would otherwise cover 14 of 16 reason codes."""
    from legba.data.provenance import assembly_arms as AA

    roster = ["narrative_coordination"]
    ledger = [{"unit": "narrative_coordination", "status": "in_basis", "age_h": 2.0}]
    row = _row(str(uuid4()))
    _, payload = _assembly([row], coverage=ledger, coverage_roster=roster)
    citations = [synth._build_composition_citation(1, row)]

    res = AA.audit(payload, citations)
    assert res.counters["assembly_attribution_blocks_checked"] == 1
    assert "assembly_attribution_date_unverifiable" not in res.counters
    assert "assembly_coverage_roster_absent" not in res.counters
    assert res.findings == [], [f.reason for f in res.findings]


def test_d2b_a_roster_unit_the_ledger_forgot_is_now_visible() -> None:
    """The falsifiability half: a check that cannot fire is not a check. With the
    denominator on the row, a declared unit missing from the ledger fires
    ``coverage_unit_missing`` — the code PREREG E-3 lists as un-fireable at T0."""
    from legba.data.provenance import assembly_arms as AA

    roster = ["narrative_coordination", "energy_security"]
    ledger = [{"unit": "narrative_coordination", "status": "in_basis", "age_h": 2.0}]
    _, payload = _assembly([_row("a")], coverage=ledger, coverage_roster=roster)
    res = AA.selection_honesty(payload)
    assert [f.reason for f in res.findings] == [AA.COVERAGE_UNIT_MISSING]
    assert res.counters["assembly_coverage_unit_missing"] == 1


@pytest.mark.asyncio
async def test_d2b_the_real_binding_path_carries_both_fields(monkeypatch) -> None:
    """``feedback-tests-must-traverse-real-binding``. Everything above tests the
    leaves; this proves ``_run`` reaches them — the roster resolved from
    ``options['source_analyst_ids']`` lands on the row as ``coverage_roster``, and
    every stamped citation dates itself from its own origin head."""
    from legba.data.provenance import assembly_arms as AA

    monkeypatch.setenv(ap.ASSEMBLY_ENV, "country_composition")
    ids = [uuid4() for _ in range(3)]
    rows = [
        _slice_row(ids[0], "leadership_transition", signal_ids=("s4",)),
        _slice_row(ids[1], "energy_security", signal_ids=("s1", "s2")),
        _slice_row(ids[2], "escalation", signal_ids=("s3",)),
    ]
    ap.attach_cited_salience(rows, MAGS)
    # The SUBSCRIPTION-RESOLVED roster, carrying one unit that produced no head
    # — the case the ledger exists for, and the one a denominator reconstructed
    # from the arrivals would silently erase.
    roster = [
        "energy_security", "leadership_transition", "escalation",
        "narrative_coordination",
    ]
    options = dict(_OPTIONS, source_analyst_ids=list(roster))
    result = await synth.run_method(list(rows), options, _Deps(_NeverCalledLLM()))

    payload = result.finding.data["assembly"]
    # (a) the roster reaches the row, in the order the ledger was built in.
    assert payload["coverage_roster"] == roster
    assert [c["unit"] for c in payload["coverage"]] == roster
    assert payload["drops"]["no_head"] == [
        {"unit": "narrative_coordination", "why": "no_head_in_horizon"}
    ]

    # (b) every citation dates itself, and agrees with its block.
    by_ref = {c["ref_id"]: c for c in result.finding.data["citations"]}
    produced = {str(r["id"]): r["produced_at"] for r in rows}
    for block in payload["blocks"]:
        cite = by_ref[block["finding_id"]]
        assert cite["produced_at"] == block["produced_at"]
        assert cite["produced_at"] == produced[block["finding_id"]]

    # (c) and both arms now RUN rather than decline.
    res = AA.audit(payload, result.finding.data["citations"])
    assert res.counters["assembly_attribution_blocks_checked"] == 3
    assert "assembly_attribution_date_unverifiable" not in res.counters
    assert "assembly_coverage_roster_absent" not in res.counters
    assert res.counters["assembly_coverage_units_checked"] == len(roster)
    assert res.findings == [], [f.reason for f in res.findings]


@pytest.mark.asyncio
async def test_d2b_the_persisted_roster_is_the_one_the_run_was_shown(
    monkeypatch,
) -> None:
    """``coverage_roster`` must be the roster the run was SHOWN, not a second
    list that happens to look like it. Same options, flag OFF: the ONE
    ``_ledger_roster`` feeds the rendered HEAD WINDOW block, so the units named
    in the prompt are exactly the units persisted on the assembled row."""
    ids = [uuid4() for _ in range(2)]
    rows = [
        _slice_row(ids[0], "energy_security", signal_ids=("s1",)),
        _slice_row(ids[1], "escalation", signal_ids=("s3",)),
    ]
    ap.attach_cited_salience(rows, MAGS)
    roster = ["energy_security", "escalation", "narrative_coordination"]
    options = dict(_OPTIONS, source_analyst_ids=list(roster))

    monkeypatch.setenv(ap.ASSEMBLY_ENV, "country_composition")
    assembled = await synth.run_method(
        list(rows), dict(options), _Deps(_NeverCalledLLM())
    )

    monkeypatch.delenv(ap.ASSEMBLY_ENV, raising=False)
    seen: list = []

    class _RecordingLLM(_CannedLLM):
        async def chat_complete(self, messages, **kwargs):  # noqa: ANN001
            seen.append(messages)
            return await super().chat_complete(messages, **kwargs)

    await synth.run_method(list(rows), dict(options), _Deps(_RecordingLLM()))

    prompt = "\n".join(
        str(m.get("content", "")) for m in seen[0] if isinstance(m, dict)
    )
    assert "COVERAGE LEDGER" in prompt
    for unit in assembled.finding.data["assembly"]["coverage_roster"]:
        assert f"- {unit}:" in prompt, unit
