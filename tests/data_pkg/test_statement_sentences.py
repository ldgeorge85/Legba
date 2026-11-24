# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S2 — cited-sentence extraction from a persisted finding
(``data/statement_sentences.py``) and the content readers G2 uses
(``data/statement_content.py``). Pure."""
from __future__ import annotations

import pytest

from legba.data import statement_content as sc
from legba.data.statement_sentences import (
    citation_index,
    extract_from_finding,
    handed_from_citations,
)

CITATIONS = [
    {"marker": "[1]", "signal_id": "S1", "source_id": "src-1", "title": "Strike hits depot"},
    {"marker": "[2]", "ordinal": 2, "ref_kind": "observation", "ref_id": "O2", "grounding": "history"},
    {"marker": "[3]", "ordinal": 3, "ref_kind": "prior_read", "ref_id": "F3", "grounding": "prior_read"},
    {"marker": "[4]", "ordinal": 4, "ref_kind": "ledger", "grounding": "ledger"},
]
BODY = (
    "## Key developments\n"
    "The U.S. struck a fuel depot on 3 May [1]. Prices rose 4% 【2】.\n"
    "Nothing here is cited. The earlier read said the port was shut [3][9].\n"
)


def test_sentences_resolve_markers_through_persisted_citations():
    out = extract_from_finding(BODY, {"citations": CITATIONS})
    assert [s.text for s in out] == [
        "The U.S. struck a fuel depot on 3 May [1].",
        "Prices rose 4% [2].",
        "The earlier read said the port was shut [3][9].",
    ]
    first, second, third = out
    assert first.plain == "The U.S. struck a fuel depot on 3 May."
    assert [(r.ref_kind, r.ref_id, r.evidence_eligible) for r in first.refs] == [("signal", "S1", True)]
    assert second.evidence_refs[0].ref_kind == "observation"
    # a prior read is the desk's own conclusion: carried, never evidence
    assert third.refs[0].ref_kind == "prior_read" and not third.refs[0].evidence_eligible
    assert third.evidence_refs == () and third.unresolved == (9,)


def test_missing_or_malformed_citations_leave_every_marker_unresolved():
    out = extract_from_finding(BODY, {"citations": "garbage"})
    assert all(not s.refs for s in out)
    assert extract_from_finding(None, None) == []


def test_index_first_entry_wins_and_entries_without_ids_are_not_evidence():
    idx = citation_index([*CITATIONS, {"marker": "[1]", "signal_id": "OTHER"}])
    assert idx[1].ref_id == "S1"
    assert idx[4].ref_id is None and not idx[4].evidence_eligible


def test_handed_set_from_a_findings_citations():
    handed = handed_from_citations(CITATIONS, run_id="r")
    assert "s1" in handed and "O2" in handed and "F3" not in handed
    assert handed.run_id == "r"


@pytest.mark.parametrize("text,labels", [
    ("on 13 November 2026", ["2026-11-13"]),
    ("on November 13", ["????-11-13"]),
    ("since September 2025", ["2025-09"]),
    ("dated 2026-11-05", ["2026-11-05"]),
    ("on 3rd of May, 2024", ["2024-05-03"]),
])
def test_date_reader(text, labels):
    assert [d.label() for d in sc.find_dates(text)] == labels


def test_number_reader_skips_names_and_reads_magnitudes():
    nums = sc.find_numbers("G20 F-16 COVID-19 killed 1,200 and $3.5bn in 2027, 5th time")
    assert [(n.raw.strip(), n.value) for n in nums] == [
        ("1,200", 1200.0), ("3.5bn", 3.5e9), ("2027", 2027.0), ("5th", 5.0),
    ]


def test_statement_side_does_not_read_one_as_a_count():
    assert sc.find_numbers("one report says five died") [0].value == 5.0
    assert len(sc.find_numbers("one report says five died")) == 1
    assert {n.value for n in sc.find_numbers("one dead", words="evidence")} == {1.0}


def test_strip_markers_removes_every_marker_form():
    assert sc.strip_markers("Five died 【3】 [4-6] [[ref:abc]] [no citation].") == "Five died."
