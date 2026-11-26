# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Actors as subjects, the verifier's "partly" class, feed-item vs page
text, and the three non-statement outcomes (assessment / absence /
restatement). Pure: no DB, no model (the link reader runs on a fake conn).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from legba.data import statement_gate as g
from legba.data.statement_actor_subjects import (
    LINKED_ENTITIES_SQL,
    LinkedEntity,
    actor_candidates,
    fetch_linked_entities,
    linked_from_rows,
    named_only_in_passing,
)
from legba.data.statement_piece_typing import (
    ABSENCE,
    ASSESSMENT,
    ESTABLISHED_ELIGIBLE,
    FAILED,
    RESTATEMENT,
    assemble_pieces,
    build_piece_prompt,
    offer_subjects,
    tally_pieces,
)
from legba.data.statement_sentences import CitedRef, CitedSentence
from legba.data.statement_shapes import (
    GATE_VERSION,
    EvidenceTexts,
    HandedSlice,
    StatementDraft,
    SupportRow,
)
from legba.data.statement_split import split_sentence, verify_narrowing
from legba.data.statement_subject_names import entity_name_forms, resolve_subject_names
from legba.data.statement_typing import (
    EvidenceRef,
    SubjectCandidate,
    TypingInput,
    evidence_texts,
    quote_in_context,
    stamp_support,
)
from legba.data.statement_verifier import (
    NOT_APPLICABLE,
    build_verifier_prompt,
    verify_support,
)

UTC = timezone.utc
FRI = datetime(2026, 11, 13, 18, 0, tzinfo=UTC)
READ = datetime(2026, 11, 14, 3, 0, tzinfo=UTC)

GZ = {
    "target_slug_names": {"xa": ["arlandia"], "xb": ["borovia"]},
    "desk_countries": {"xa": ["arlandia"]},
    "demonym_map": {"arlandian": "Arlandia", "borovian": "Borovia"},
}
SUBJECTS = {"country_watch_xa": {"name": "Watch — Arlandia", "geo": ["XA"], "domain": "geo"}}
KW = {"gazetteer": GZ, "metonyms": (), "subjects": SUBJECTS}

DESK = SubjectCandidate("target", "country_watch_xa",
                        resolve_subject_names("target", "country_watch_xa", **KW))

FLEET = LinkedEntity("e-fleet", "Northern Fleet Command", "organization",
                     ("the Northern Fleet", "NFC"), ("sig-1",))
VOSS = LinkedEntity("e-voss", "Admiral Petra Voss", "person", ("Voss", "Petra Voss's"), ("sig-1",))
HERALD = LinkedEntity("e-herald", "Harbour Herald", "organization", (), ("sig-1",))
PORT = LinkedEntity("e-port", "Kessel Port", "location", (), ("sig-1",))
SUMMIT = LinkedEntity("e-summit", "Kessel Summit", "event", (), ("sig-1",))
GOV = LinkedEntity("e-gov", "government", "entity", ("the government",), ("sig-1",))
DESK_ENT = LinkedEntity("e-xa", "Arlandia", "country", (), ("sig-1",))
BOROVIA = LinkedEntity("e-xb", "Borovia", "country", (), ("sig-1",))
ELSEWHERE = LinkedEntity("e-other", "Customs Board", "organization", (), ("sig-9",))

ROW1 = ("The Northern Fleet seized a Borovian tanker near Kessel Port on Friday, the Harbour Herald "
        "reported. Admiral Petra Voss said the ship had entered closed waters. Officials expect more seizures.")


def _ev(text: str = ROW1, **kw) -> EvidenceRef:
    base = {"published_at": FRI, "source_id": "harbourherald", "access_class": "public",
            "source_name": "Harbour Herald"}
    return EvidenceRef(1, "signal", "sig-1", text, **{**base, **kw})


def _item(text: str, *, linked=(), refs=None, evidence=None, subjects=(DESK,)) -> TypingInput:
    refs = refs or (CitedRef(1, "signal", "sig-1", "harbourherald", None, True),)
    sentence = CitedSentence(0, text, g.strip_markers(text), refs)
    return TypingInput(sentence, evidence if evidence is not None else (_ev(),), subjects,
                       read_at=READ, linked_entities=tuple(linked))


HANDED = HandedSlice.of(["sig-1", "sig-2"], run_id="run-1", collection="country_watch_xa",
                        source_ids=["harbourherald"], window_start=datetime(2026, 11, 13, 6, tzinfo=UTC),
                        window_end=datetime(2026, 11, 14, 3, tzinfo=UTC))


def _entry(subject: str = "S2", kind: str = "event", quote: str = "The Northern Fleet seized a Borovian tanker",
           **extra) -> dict:
    return {"piece": 1, "kind": kind, "subject": subject, "valid_from": None,
            "valid_from_precision": "unknown", "slots": extra.pop("slots", {}),
            "support": [{"row": 1, "quote": quote}] if quote else [],
            "relation": {"action": "new", "candidate": None}, **extra}


def _cand(ent: LinkedEntity) -> SubjectCandidate:
    return actor_candidates(ent.canonical_name, (_ev(),), [ent], offered=(DESK,), **KW).offered[0]


# ---------------------------------------------------------------------------
# 1 — actors as subject candidates
# ---------------------------------------------------------------------------


def test_entity_subject_names_come_from_the_profile_cleaned():
    names = resolve_subject_names("entity", "e-voss", entity_profiles={"e-voss": VOSS.profile()}, **KW)
    usable = {n.name: (n.kind, n.source) for n in names.usable}
    assert usable["Admiral Petra Voss"] == ("entity", "entity:canonical_name")
    assert usable["Petra Voss"] == ("entity", "entity:merged_aliases")      # possessive dropped
    assert "Voss" in usable
    # a stored name with its closing parenthesis lost yields base + acronym
    assert entity_name_forms("Zarn Nuclear Plant (ZNP") == [("Zarn Nuclear Plant", "entity"), ("ZNP", "abbreviation")]
    # a form that is not a proper name is kept unusable: folded, it is "in" every quote
    gov = resolve_subject_names("entity", "e-gov", entity_profiles={"e-gov": GOV.profile()}, **KW)
    assert not gov.usable and {n.kind for n in gov.names} == {"entity_generic"}


def test_gate_subject_check_works_for_entity_subjects_as_for_countries():
    names = resolve_subject_names("entity", "e-fleet", entity_profiles={"e-fleet": FLEET.profile()}, **KW)
    named = SupportRow("evidence", "signal", "sig-1", quote="The Northern Fleet seized a tanker", published_at=FRI)
    assert g.check_subject_named([named], names).passed
    acr = SupportRow("evidence", "signal", "sig-1", quote="NFC ships seized a tanker", published_at=FRI)
    assert g.check_subject_named([acr], names).detail["name"] == "NFC"
    lower = SupportRow("evidence", "signal", "sig-1", quote="the nfc said nothing more", published_at=FRI)
    assert not g.check_subject_named([lower], names).passed               # acronym is case-sensitive
    other = SupportRow("evidence", "signal", "sig-1", quote="Admiral Petra Voss said the ship", published_at=FRI)
    assert g.check_subject_named([other], names).reason == "subject_not_named"


def test_actor_candidates_offer_named_actors_and_decline_what_is_not_a_subject():
    piece = ("The Northern Fleet seized a tanker near Kessel Port, the Harbour Herald reported, "
             "as the government and Arlandia watched the Kessel Summit")
    offer = actor_candidates(piece, (_ev(),), [FLEET, VOSS, HERALD, PORT, SUMMIT, GOV, DESK_ENT, ELSEWHERE],
                             offered=(DESK,), **KW)
    assert [c.key for c in offer.offered] == ["e-fleet"]
    c = offer.offered[0]
    assert (c.kind, c.entity_id, c.canonical_name, c.entity_type) == ("entity", "e-fleet", "Northern Fleet Command",
                                                                      "organization")
    assert dict(offer.declined) == {
        "e-voss": "not_named_in_piece", "e-herald": "publisher", "e-port": "place_in_passing",
        "e-summit": "event_not_actor", "e-gov": "no_proper_name", "e-xa": "desk_itself"}
    # an entity linked only to a row this piece does not cite is not considered at all
    assert "e-other" not in dict(offer.declined)


def test_a_place_that_acts_is_a_subject_and_a_country_becomes_its_place_subject():
    assert named_only_in_passing(_cand(PORT).names, "ships waited in Kessel Port")
    assert not named_only_in_passing(_cand(PORT).names, "Kessel Port closed its gates")
    offer = actor_candidates("Kessel Port closed its gates", (_ev(),), [PORT], offered=(DESK,), **KW)
    assert [c.key for c in offer.offered] == ["e-port"]
    bor = actor_candidates("Borovia protested the seizure", (_ev(),), [BOROVIA], offered=(DESK,), **KW).offered
    assert [(c.kind, c.key) for c in bor] == [("place", "XB")]
    assert "borovian" in {n.name for n in bor[0].names.usable}            # the gazetteer's names, as for a desk


def test_actor_ranking_prefers_names_in_the_cited_rows_then_link_count_and_caps():
    a = LinkedEntity("e-a", "Alder Group", "organization", (), ("sig-1",))
    b = LinkedEntity("e-b", "Birch Group", "organization", (), ("sig-1", "sig-2"))
    ev2 = EvidenceRef(2, "signal", "sig-2", "Birch Group shares fell.", published_at=FRI, source_id="x")
    text = "Alder Group and Birch Group and the Northern Fleet"
    offer = actor_candidates(text, (_ev(), ev2), [a, b, FLEET], offered=(DESK,), limit=2, **KW)
    # in-row names first (Birch in row 2, Fleet in row 1), then more cited rows linking it
    assert [c.key for c in offer.offered] == ["e-b", "e-fleet"] and offer.detail["cut_by_limit"] == 1


def test_the_seat_picks_an_offered_actor_or_declines_never_invents():
    text = "The Northern Fleet seized a Borovian tanker on Friday [1]."
    item = _item(text, linked=[FLEET, VOSS])
    offer = offer_subjects(item, split_sentence(text))
    assert [s.key for s in offer.item.subjects] == ["country_watch_xa", "e-fleet"]
    prompt = build_piece_prompt(offer.item, split_sentence(text), offer.allowed)
    assert "S2 · entity:organization · Northern Fleet Command" in prompt
    out = assemble_pieces(item, {"pieces": [_entry("S2")]}, handed=HANDED)
    p = out.pieces[0]
    assert p.outcome == ESTABLISHED_ELIGIBLE, p.why()
    assert (p.result.draft.subject_kind, p.result.draft.subject_key) == ("entity", "e-fleet")
    assert p.result.detail["subject"]["canonical_name"] == "Northern Fleet Command"
    # an id not offered (S3 does not exist), or null: subject_not_offered, never a guess
    for bad in ("S3", None):
        q = assemble_pieces(item, {"pieces": [_entry(bad)]}, handed=HANDED).pieces[0]
        assert q.outcome == FAILED and "subject_not_offered" in q.why()


def test_actors_are_offered_per_piece_and_a_piece_cannot_take_another_pieces_actor():
    ev2 = EvidenceRef(2, "signal", "sig-2", "Admiral Petra Voss resigned on 13 November.", published_at=FRI,
                      source_id="wire", access_class="public")
    voss = LinkedEntity("e-voss", "Admiral Petra Voss", "person", ("Voss",), ("sig-2",))
    text = "The Northern Fleet seized 2 tankers [1], and Admiral Petra Voss resigned on 13 November [2]."
    assert len(split_sentence(text)) == 2
    refs = (CitedRef(1, "signal", "sig-1", "harbourherald", None, True), CitedRef(2, "signal", "sig-2", "wire", None, True))
    item = _item(text, linked=[FLEET, voss], refs=refs, evidence=(_ev(), ev2))
    offer = offer_subjects(item, split_sentence(text))
    assert offer.allowed == {0: frozenset({1, 2}), 1: frozenset({1, 3})}
    reply = {"pieces": [_entry("S3"), {**_entry("S3"), "piece": 2,
                                       "support": [{"row": 2, "quote": "Admiral Petra Voss resigned on 13 November"}]}]}
    out = assemble_pieces(item, reply, handed=HANDED)
    assert "subject_not_offered" in out.pieces[0].why()            # S3 is piece 2's actor
    assert out.pieces[1].outcome == ESTABLISHED_ELIGIBLE, out.pieces[1].why()


def test_a_relation_is_subject_plus_counterpart_and_the_counterpart_is_checked():
    text = "The Northern Fleet seized a Borovian tanker on Friday [1]."
    item = _item(text, linked=[FLEET, BOROVIA])
    offer = offer_subjects(item, split_sentence(text))
    bor_id = next(f"S{i}" for i, s in enumerate(offer.item.subjects, 1) if s.key in {"XB", "e-xb"})
    ok = assemble_pieces(item, {"pieces": [_entry("S2", slots={"counterpart": bor_id})]}, handed=HANDED).pieces[0]
    assert ok.outcome == ESTABLISHED_ELIGIBLE, ok.why()
    assert ok.result.detail["counterpart"]["key"] in {"XB", "e-xb"}
    assert any(c.check == "counterpart_named" and c.passed for c in ok.result.gate.checks)
    # the quote must name the counterpart too
    bare = assemble_pieces(item, {"pieces": [_entry("S2", quote="The Northern Fleet seized a tanker near",
                                                    slots={"counterpart": bor_id})]}, handed=HANDED).pieces[0]
    assert "G2.counterpart_named:counterpart_not_named" in bare.why()
    same = assemble_pieces(item, {"pieces": [_entry("S2", slots={"counterpart": "S2"})]}, handed=HANDED).pieces[0]
    assert "counterpart_is_subject" in same.why()
    free = assemble_pieces(item, {"pieces": [_entry("S2", slots={"counterpart": "the Zed Navy"})]},
                           handed=HANDED).pieces[0]
    assert "counterpart_not_in_sentence" in free.why()


def test_linked_entities_are_read_by_signal_id_and_folded_to_keepers():
    class Conn:
        def __init__(self):
            self.calls = []

        async def fetch(self, sql, ids):
            self.calls.append((sql, ids))
            return [{"signal_id": "SIG-1", "entity_id": "k1", "canonical_name": "Northern Fleet Command",
                     "entity_class": "organization", "aliases": '["the Northern Fleet"]',
                     "linked_name": "Northern Fleet"},
                    {"signal_id": "sig-2", "entity_id": "k1", "canonical_name": "Northern Fleet Command",
                     "entity_class": "organization", "aliases": None, "linked_name": "Northern Fleet Command"}]

    conn = Conn()
    got = asyncio.run(fetch_linked_entities(conn, ["sig-2", "SIG-1", None]))
    assert conn.calls == [(LINKED_ENTITIES_SQL, ["sig-1", "sig-2"])]
    assert "l.signal_id = ANY" in LINKED_ENTITIES_SQL and "merged_into" in LINKED_ENTITIES_SQL
    assert got == [LinkedEntity("k1", "Northern Fleet Command", "organization",
                                ("the Northern Fleet", "Northern Fleet"), ("sig-1", "sig-2"))]
    assert asyncio.run(fetch_linked_entities(conn, [])) == [] and len(conn.calls) == 1
    assert linked_from_rows([]) == []


# ---------------------------------------------------------------------------
# 2 — the "partly" class
# ---------------------------------------------------------------------------


def _one_piece(text: str):
    return split_sentence(text)[0]


def test_narrowing_to_the_supported_clause_is_gated_on_the_narrowed_text():
    text = "The Northern Fleet seized a Borovian tanker on Friday, a step that will choke Borovian trade [1]."
    kept = "The Northern Fleet seized a Borovian tanker on Friday"
    item = _item(text, linked=[FLEET])
    reply = {"pieces": [_entry("S2", narrow={"start": 0, "end": len(kept), "text": kept})]}
    # the SEAT's narrowing, on the split (code splits this tail by
    # itself — asserted below)
    p = assemble_pieces(item, reply, handed=HANDED, pieces=split_sentence(text)).pieces[0]
    assert p.outcome == ESTABLISHED_ELIGIBLE, p.why()
    assert p.result.draft.text == kept and p.typed_text == kept
    assert p.detail["narrow"]["dropped"][1][0] == len(kept)          # the remainder is recorded, not typed
    # the whole piece, typed without narrowing, still says more than its quote:
    # code passes it (no number or date is wrong) — the verifier's "beyond_quote" class
    whole = assemble_pieces(item, {"pieces": [_entry("S2")]}, handed=HANDED,
                            pieces=split_sentence(text)).pieces[0]
    assert whole.result.draft.text.rstrip(".").endswith("choke Borovian trade")
    # By default code splits the judgement tail off and types it an
    # assessment resting on the head; the seat types only the head
    coded = assemble_pieces(item, {"pieces": [_entry("S2")]}, handed=HANDED).pieces
    assert [c.outcome for c in coded] == [ESTABLISHED_ELIGIBLE, "assessment"]
    assert coded[0].result.draft.text == kept and coded[1].detail["typed_by"] == "code_tail"


def test_narrowing_never_drops_an_attribution_negation_or_condition():
    cases = {
        "The tanker entered closed waters, officials said [1].": ("The tanker entered closed waters",
                                                                 "narrow_drops_scope"),
        "According to the Herald, the tanker entered closed waters [1].": ("the tanker entered closed waters",
                                                                         "narrow_drops_scope"),
        "Officials denied that the tanker entered closed waters [1].": ("the tanker entered closed waters",
                                                                       "narrow_starts_mid_clause"),
        "If confirmed, the tanker entered closed waters [1].": ("the tanker entered closed waters",
                                                               "narrow_drops_scope"),
        "The tanker entered closed waters, which the navy disputed [1].": ("The tanker entered closed waters",
                                                                         "narrow_drops_scope"),
        "The tanker entered closed waters near the port [1].": ("The tanker entered closed",
                                                                "narrow_ends_mid_clause"),
        "The tanker entered closed waters [1].": ("The tanker entered closed waters", "narrow_is_whole_piece"),
    }
    for text, (kept, reason) in cases.items():
        got, why, _ = verify_narrowing(text, _one_piece(text), {"text": kept})
        assert (got, why) == (None, reason), text
    text = "The tanker entered closed waters [1]."
    assert verify_narrowing(text, _one_piece(text), {"text": "The ship entered closed waters"})[1] == \
        "narrow_text_not_in_piece"                                    # never a rewrite


def test_narrowing_away_a_figure_records_it_as_dropped():
    text = "The fleet seized 3 tankers on Friday amid a 40% rise in port fees [1]."
    assert len(split_sentence(text)) == 1
    got, why, detail = verify_narrowing(text, _one_piece(text), {"start": 0, "text": "The fleet seized 3 tankers on Friday"})
    assert why is None and got.ordinals == (1,) and got.split_by == "narrowed"
    assert detail["dropped_figures"] == ["40"] and detail["offsets_corrected"] == 0


def test_several_quotes_from_one_row_and_g1_still_enforces_verbatim():
    text = "The Northern Fleet seized a Borovian tanker on Friday, and Admiral Petra Voss said it had entered closed waters [1]."
    item = _item(text, linked=[FLEET])
    two = _entry("S2", kind="attributed")
    two["support"] = [{"row": 1, "quote": "The Northern Fleet seized a Borovian tanker near Kessel Port on Friday"},
                      {"row": 1, "quote": "Admiral Petra Voss said the ship had entered closed waters"}]
    pieces = assemble_pieces(item, {"pieces": [two]}, handed=HANDED).pieces
    assert len(pieces[0].result.support) == 2 and "G1.quote:quote_absent" not in pieces[0].why()
    stretched = _entry("S2")
    stretched["support"] = [{"row": 1, "quote": "The Northern Fleet seized a Borovian tanker near Kessel Port on Friday "
                                                "after a long chase"}]
    bad = assemble_pieces(item, {"pieces": [stretched]}, handed=HANDED).pieces[0]
    assert "G1.quote:quote_absent" in bad.why()


def test_the_verifier_sees_the_quote_and_its_publication_date_only_context_is_recorded():
    row = SupportRow("evidence", "signal", "sig-1", quote="seized a Borovian tanker", published_at=FRI,
                     title="SECRET TITLE", source_id="harbourherald")
    texts = evidence_texts([_ev()])
    (stamped,) = stamp_support([row], texts)
    assert stamped.quote == "seized a Borovian tanker"                       # the quote itself never changes
    assert stamped.quote_context == ("The Northern Fleet seized a Borovian tanker near Kessel Port on Friday, "
                                     "the Harbour Herald reported.")
    st = StatementDraft("The fleet seized a tanker on 13 November.", "event", "entity", "e-fleet")
    prompt = build_verifier_prompt(st, [stamped])
    # measured: the sentence around a quote loosened the check, so by default
    # the verifier reads the quote alone (with its publication date)
    assert '  Q1 (published 2026-11-13): "seized a Borovian tanker"' in prompt
    for hidden in ("SECRET TITLE", "harbourherald", "Admiral Petra Voss", "expect more", "Northern Fleet"):
        assert hidden not in prompt
    assert '"The Northern Fleet seized' in build_verifier_prompt(st, [stamped], show_context=True)
    # context is recorded, never checked: a figure only in the context is no support for G2
    st2 = StatementDraft("Kessel Port saw 7 seizures.", "event", "entity", "e-fleet")
    assert not g.check_numbers(st2, [stamped], texts).passed
    assert quote_in_context("A b c. D e f g. H i.", "e f") == "D e f g."
    assert quote_in_context("one long sentence", "nowhere") is None


def test_a_planted_quote_swap_never_keeps_the_true_quotes_context():
    from legba.data import statement_planted as sp
    from legba.data.statement_seat_canary import CANARY

    base = sp.from_canary(CANARY[0])
    base = sp.SeatItem(base.item_id, base.truth, base.statement, stamp_support(base.support, base.texts),
                       base.texts, base.subject_names, base.base_id)
    assert base.support[0].quote_context
    for kind in ("claim_as_fact", "other_version_quote"):
        planted = sp.plant(base, kind)
        if planted is not None:
            assert planted.support[0].quote_context is None, kind


def test_a_publication_day_date_and_the_verifier_label_agree():
    # "on Friday" in a quote published Friday 13 November: the gate's rule (a)
    # accepts "13 November"; the verifier is shown the same day as metadata
    st = StatementDraft("The fleet seized a tanker on 13 November.", "event", "entity", "e-fleet")
    row = SupportRow("evidence", "signal", "sig-1", quote="The Northern Fleet seized a Borovian tanker near Kessel Port on Friday",
                     published_at=FRI)
    assert g.check_dates(st, [row], read_at=READ).passed
    assert "(published 2026-11-13)" in build_verifier_prompt(st, [row])


# ---------------------------------------------------------------------------
# 3 — feed item vs page text
# ---------------------------------------------------------------------------


def _feed_texts(page: str | None):
    by_fv = {7: page} if page is not None else {}
    return EvidenceTexts(by_fetch_version=by_fv,
                         feed_item_by_signal={"sig-1": ("Fleet seizes tanker off Kessel",
                                                        "The Northern Fleet seized a tanker on Friday.")})


def test_a_quote_only_in_the_feed_item_passes_recorded_and_not_checkable():
    page = "A different page body: the navy confirmed an inspection."
    row = SupportRow("evidence", "signal", "sig-1", quote="The Northern Fleet seized a tanker",
                     fetch_version_id=7, access_class="public", published_at=FRI)
    res = g.check_quote(row, _feed_texts(page))
    assert res.passed and res.detail["quote_found_in"] == "feed_item"
    assert res.detail["checkability"] == "feed_item_only" and res.detail["text_origin"] == "fetch_version"
    (stamped,) = stamp_support([row], _feed_texts(page))
    assert stamped.quote_found_in == "feed_item" and stamped.checkable is False
    rep = g.evaluate_code_gates(StatementDraft("The fleet seized a tanker.", "event", "entity", "e"),
                                [stamped], texts=_feed_texts(page), subject_names=["Northern Fleet"],
                                handed=HandedSlice.of(["sig-1"]))
    assert rep.independent_groups == 1 and rep.checkable_groups == 0 and rep.gate_version == GATE_VERSION == \
        "statement_gate.v4"


def test_page_text_both_and_neither_are_told_apart():
    both = SupportRow("evidence", "signal", "sig-1", quote="The Northern Fleet seized a tanker", fetch_version_id=7)
    page_only = SupportRow("evidence", "signal", "sig-1", quote="the navy confirmed an inspection", fetch_version_id=7)
    t = _feed_texts("Report: The Northern Fleet seized a tanker. Then the navy confirmed an inspection.")
    assert g.check_quote(both, t).detail["quote_found_in"] == "both"
    assert g.check_quote(page_only, t).detail["quote_found_in"] == "page_text"
    assert "checkability" not in g.check_quote(page_only, t).detail
    nowhere = SupportRow("evidence", "signal", "sig-1", quote="the fleet sank three ships", fetch_version_id=7)
    assert g.check_quote(nowhere, t).reason == "quote_absent"
    # title and summary are separate texts: a quote may not straddle them
    straddle = SupportRow("evidence", "signal", "sig-1", quote="off Kessel The Northern Fleet", fetch_version_id=7)
    assert g.check_quote(straddle, _feed_texts("unrelated page")).reason == "quote_absent"
    # no feed item resolved: nothing is recorded (behaviour)
    assert "quote_found_in" not in g.check_quote(both, EvidenceTexts(by_fetch_version={7: "The Northern Fleet seized a tanker"})).detail
    # page text unavailable but the feed item carries it
    gone = g.check_quote(both, _feed_texts(None))
    assert gone.passed and gone.detail["page_text"] == "unavailable"


def test_evidence_refs_carry_the_feed_item_into_the_gate_texts():
    ev = _ev("page text without the line", fetch_version_id=7,
             feed_text=("Fleet seizes tanker", "The Northern Fleet seized a tanker on Friday."))
    texts = evidence_texts([ev])
    assert texts.feed_item_by_signal == {"sig-1": ("Fleet seizes tanker", "The Northern Fleet seized a tanker on Friday.")}


# ---------------------------------------------------------------------------
# 4 — assessments, absences, restatements
# ---------------------------------------------------------------------------


FACT = "The Northern Fleet seized a Borovian tanker on Friday"
JUDGE = "which likely signals a wider blockade"


def test_an_assessment_rests_only_on_typed_pieces_and_never_on_a_quote():
    text = "The Northern Fleet seized a Borovian tanker on Friday [1], which likely signals a wider blockade [1]."
    item = _item(text, linked=[FLEET])
    assert len(split_sentence(text)) == 1                          # the model splits it
    reply = {"pieces": [{**_entry("S2"), "text": FACT},
                        {**_entry("S1", kind="assessment"), "piece": 2, "premises": ["P1"], "text": JUDGE}]}
    out = assemble_pieces(item, reply, handed=HANDED)
    fact, judgement = out.pieces
    assert fact.outcome == ESTABLISHED_ELIGIBLE and judgement.outcome == ASSESSMENT, judgement.why()
    assert judgement.result.detail["quotes_dropped"] == 1
    (prem,) = judgement.result.support
    assert (prem.role, prem.support_kind, prem.ref_id, prem.ref_statement_kind) == (
        "premise", "statement", "piece:0:1", "event")
    assert not judgement.passed                                   # never established-eligible
    rep = asyncio.run(verify_support(None, judgement.result.draft, judgement.result.support,
                                     texts=EvidenceTexts(), system_prompt="S"))
    assert (rep.verdict, rep.decided_by, rep.establishes) == (NOT_APPLICABLE, "code", False)
    # a premise that is not another typed piece is refused
    bad = assemble_pieces(item, {"pieces": [{**_entry("S2"), "text": FACT},
                                            {**_entry("S1", kind="assessment"), "piece": 2, "premises": ["P2"],
                                             "text": JUDGE}]}, handed=HANDED).pieces[1]
    assert bad.outcome == FAILED and "premise_not_a_typed_piece" in bad.why()


def test_an_assessment_with_null_subject_is_the_desks_and_evidence_rows_are_not_premises():
    text = "The blockade risk is rising [1]."
    out = assemble_pieces(_item(text), {"pieces": [_entry(None, kind="assessment")]}, handed=HANDED).pieces[0]
    assert out.outcome == ASSESSMENT and out.result.detail["subject_defaulted"] == "desk"
    st = StatementDraft(text, "assessment", "target", "country_watch_xa")
    ev = SupportRow("evidence", "signal", "sig-1", quote="x y z w")
    assert g.evaluate_assessment_gates(st, [ev]).failures[0].reason == "assessment_support_not_premise"


def test_an_absence_is_about_the_collection_and_scoped_from_the_handed_slice():
    text = "No signal in this window reports a Borovian naval deployment [1]."
    out = assemble_pieces(_item(text), {"pieces": [_entry("S1", kind="absence")]}, handed=HANDED).pieces[0]
    assert out.outcome == ABSENCE, out.why()
    scope = next(c for c in out.result.gate.checks if c.check == "absence_scope").detail
    assert scope["collection"] == "country_watch_xa" and scope["source_ids"] == ["harbourherald"]
    assert scope["window_start"].startswith("2026-11-13") and scope["run_id"] == "run-1"
    assert out.result.support == () and out.result.detail["quotes_dropped"] == 1
    # a slice that does not say what it was cannot scope an absence
    bare = assemble_pieces(_item(text), {"pieces": [_entry("S1", kind="absence")]},
                           handed=HandedSlice.of(["sig-1"])).pieces[0]
    assert bare.outcome == FAILED and "G8.absence_scope:absence_scope_unknown" in bare.why()
    # a row the run was not handed is still refused
    outside = assemble_pieces(_item(text), {"pieces": [_entry("S1", kind="absence")]},
                              handed=HandedSlice.of(["sig-9"], collection="c", window_start=FRI,
                                                    window_end=READ)).pieces[0]
    assert "G3.cited_ids:not_in_run_slice" in outside.why()


def test_a_piece_typed_absence_must_state_one():
    # a watch item or a list of what the slice DOES hold cannot escape the
    # verifier by being typed "absence"
    for text in ("Watch for follow-on seizures of Borovian tankers [1].",
                 "This window's signals are port reporting and a tariff story [1]."):
        p = assemble_pieces(_item(text), {"pieces": [_entry("S1", kind="absence")]}, handed=HANDED).pieces[0]
        assert p.outcome == FAILED and "G8.absence_stated:absence_not_stated" in p.why(), text
    assert g.absence_stated("None of the three signals reports a deployment.")
    assert g.absence_stated("The slice doesn't carry a single protest item.")


def test_a_restatement_links_what_it_restates_and_makes_no_new_piece():
    text = "The fleet's blockade posture is unchanged [3]."
    refs = (CitedRef(3, "prior_read", "read-42", None, None, False),)
    out = assemble_pieces(_item(text, refs=refs, evidence=()), {"pieces": [_entry("S1")]}, handed=HANDED)
    p = out.pieces[0]
    assert p.outcome == RESTATEMENT and p.result is None
    assert p.detail["restates"] == [{"ordinal": 3, "ref_kind": "prior_read", "ref_id": "read-42"}]


def test_tally_counts_each_outcome_apart():
    text = "The Northern Fleet seized a Borovian tanker on Friday [1], which likely signals a wider blockade [1]."
    a = assemble_pieces(_item(text, linked=[FLEET]), {"pieces": [
        {**_entry("S2"), "text": FACT},
        {**_entry("S1", kind="assessment"), "piece": 2, "premises": ["P1"], "text": JUDGE}]}, handed=HANDED)
    b = assemble_pieces(_item("No signal in this window reports a deployment [1]."),
                        {"pieces": [_entry("S1", kind="absence")]}, handed=HANDED)
    c = assemble_pieces(_item("Posture is unchanged [3].", refs=(CitedRef(3, "prior_read", "r", None, None, False),),
                              evidence=()), {"pieces": []}, handed=HANDED)
    d = assemble_pieces(_item("The fleet sank a ship [1]."), {"pieces": [_entry("S1", quote="the fleet sank a ship")]},
                        handed=HANDED)
    t = tally_pieces([a, b, c, d])
    assert t["outcomes"] == {"established_eligible": 1, "assessment": 1, "absence": 1, "coverage": 0,
                             "restatement": 1, "failed": 1}
    assert t["sentences_with_a_supported_piece"] == 1 and t["pieces_passed"] == 1
    assert t["established_eligible_subject_kinds"] == {"entity": 1}
    assert "G1.quote:quote_absent" in t["reasons"] and "assessment" not in str(t["reasons"])
