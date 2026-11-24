# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-T (2026-11-13) — the crossroads detectors' own arithmetic carries a
declared provenance the verify pass checks.

Crossroads entry b7a5b41d (12:02 UTC) carried 16 verify marks, 11 of them
``[[number_unsupported]]`` on counts the detectors computed ("carries Pakistan
across 7 distinct desks and 7 verified findings"): the number check compared
them with the CITED TEXT, where a desk count can never appear. Two more were
the "11" and "09" of an ISO timestamp. Now the inquiry kind stamps the block's
computed numbers by row on ``data.instrument_numbers`` and the check accepts a
number only when the sentence cites that row (by ordinal or ref) and the row
computed exactly that number — a different number is still marked.
"""

from __future__ import annotations

import datetime
from datetime import timezone

from legba.data.analysts import crossroads_detectors as cd
from legba.data.analysts.inquiry import resolve_instrument_provenance
from legba.data.analysts.journal_prepublish import (
    INSTRUMENT_NUMBER,
    INSTRUMENT_NUMBERS_KEY,
    NUMBER_UNSUPPORTED,
    apply_prepublish_verdict,
)
from legba.data.analysts.journal_reflect import _reflect_claims
from legba.data.provenance.models import JournalPayload

PK_REF = "7ca7268d-288b-4cc0-b9c6-9d0ccc56a082"
OTHER_REF = "4efcbe8b-864c-4535-91f1-5a3ad524ad64"
NOW = datetime.datetime(2026, 11, 13, 12, 1, 1, tzinfo=timezone.utc)

#: The cited row's text: a composition head that states no desk count.
TEXTS = {
    PK_REF: {"title": "G20 — Pakistan", "text": "Pakistan read across the desks.",
             "publisher": "country_composition"},
    OTHER_REF: {"title": "COP31", "text": "COP31 read.", "publisher": "country_composition"},
}


def _block() -> str:
    rows = [
        cd.CrossroadsRow(
            kind="pattern", subject="pakistan",
            desks=("a", "b", "c", "d", "e", "f", "g"),
            arithmetic=(
                '7 distinct desks (a, b, c, d, e, f, g) carry "pakistan" across 7 '
                "verified findings in this window (threshold: >= 3 desks; verified = "
                "a faithfulness verdict >= 0.5)"
            ),
            refs=(PK_REF,), ledger_kind="observation", ledger_text="x",
            resolution_test='EXPIRED if no desk names "pakistan" in the next 3 cadences.',
        ),
        cd.CrossroadsRow(
            kind="pattern", subject="cop31", desks=("a", "b", "c", "d", "e", "f"),
            arithmetic='6 distinct desks (a, b, c, d, e, f) carry "cop31" across 7 verified findings',
            refs=(OTHER_REF,), ledger_kind="observation", ledger_text="y",
            resolution_test="z",
        ),
    ]
    sub = cd.CrossroadsSubstrate(
        window_start=NOW - datetime.timedelta(hours=24), window_end=NOW,
        reads_ok=("list_findings",),
    )
    return cd.render_crossroads_block(rows, sub)


def _payload(body: str, instrument=None) -> JournalPayload:
    claims, refs, _ = _reflect_claims(body)
    data = {INSTRUMENT_NUMBERS_KEY: instrument} if instrument is not None else {}
    return JournalPayload(
        entry_kind="crossroads", title="Crossroads", body=body, claims=claims,
        cited_substrate_refs=refs, period_start=NOW, period_end=NOW, data=data,
    )


def _apply(body: str, instrument=None):
    return apply_prepublish_verdict(
        _payload(body, instrument), unsupported_spans=[], ref_texts=TEXTS,
        judge_status="llm", reflect=_reflect_claims,
    )


#: Verbatim from b7a5b41d's Patterns paragraph (marks stripped).
PK_SENTENCE = (
    f"[[ref:{PK_REF}]] carries Pakistan across 7 distinct desks and 7 verified "
    "findings (threshold >= 3): economic_coercion, energy_security, escalation."
)


def test_the_block_round_trips_its_numbers_by_row() -> None:
    prov = cd.instrument_provenance(_block())
    assert [p["ordinal"] for p in prov] == [0, 1, 2]
    pk = prov[1]
    assert pk["kind"] == "pattern" and pk["refs"] == [PK_REF]
    assert {7.0, 3.0, 0.5} <= set(pk["numbers"])
    assert prov[2]["numbers"] and 6.0 in prov[2]["numbers"]


def test_before_the_detectors_counts_are_marked_unsupported() -> None:
    out, summary = _apply(PK_SENTENCE)
    assert len(summary[NUMBER_UNSUPPORTED]) >= 2
    assert "[[number_unsupported]]" in out.body


def test_after_the_counts_are_checked_against_the_row_they_came_from() -> None:
    prov = cd.instrument_provenance(_block())
    out, summary = _apply(PK_SENTENCE, prov)
    assert summary[NUMBER_UNSUPPORTED] == []
    assert "[[number_unsupported]]" not in out.body
    accepted = summary[INSTRUMENT_NUMBER]
    assert {a["number"] for a in accepted} >= {"7"}
    assert all(a["ordinal"] == 1 and "distinct desks" in a["arithmetic"] for a in accepted)


def test_a_number_the_row_did_not_compute_is_still_marked() -> None:
    """Not an exemption: "9 distinct desks" over a row that computed 7 fails."""
    prov = cd.instrument_provenance(_block())
    body = PK_SENTENCE.replace("7 distinct desks", "9 distinct desks")
    _, summary = _apply(body, prov)
    assert [n["number"] for n in summary[NUMBER_UNSUPPORTED]] == ["9"]


def test_a_row_the_sentence_does_not_cite_lends_it_nothing() -> None:
    """COP31's row computed 6; a Pakistan sentence citing only Pakistan's row
    may not borrow it."""
    prov = cd.instrument_provenance(_block())
    body = PK_SENTENCE.replace("7 distinct desks", "6 distinct desks")
    _, summary = _apply(body, prov)
    assert [n["number"] for n in summary[NUMBER_UNSUPPORTED]] == ["6"]


def test_a_row_cited_by_ordinal_counts() -> None:
    prov = cd.instrument_provenance(_block())
    body = f"COP31 sits across 6 desks [2] [[ref:{PK_REF}]]."
    _, summary = _apply(body, prov)
    assert summary[NUMBER_UNSUPPORTED] == []


def test_an_iso_timestamp_is_a_date_not_three_numbers() -> None:
    body = (
        "the narrative slice reporting no coordinated narrative, collected "
        f"2026-11-13T09:56:26 [[ref:{PK_REF}]]."
    )
    out, summary = _apply(body)
    assert summary[NUMBER_UNSUPPORTED] == []
    assert out.body == body


def test_the_inquiry_kind_resolves_the_declared_provenance() -> None:
    opts = {"pre_pass_module": "legba.data.analysts.crossroads_detectors:pre_pass_block"}
    assert resolve_instrument_provenance(opts, _block())[1]["refs"] == [PK_REF]
    assert resolve_instrument_provenance({}, _block()) == []
    assert resolve_instrument_provenance(
        {"pre_pass_module": "json:loads"}, _block(),
    ) == [], "a module that declares no provenance yields none"
