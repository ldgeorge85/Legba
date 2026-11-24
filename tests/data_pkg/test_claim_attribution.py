# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C-2 — the ``claim_as_fact`` attribution check, MEASURED (pure; no model).

The labelled set (``fixtures/claim_attribution_set.psv``, 220 invented
sentences: 166 the rule was tuned on, 54 written after tuning) pins the
confusion table of the C-1 rule and of the C-2 clause rule. The C-2 rule must
miss NO sentence that states the claimed event as fact; the wrongly-flagged
fair sentences are pinned at their measured count so a change that raises
them shows up here.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from legba.data import claim_attribution as CA
from legba.data import claim_support as CS

SET = pathlib.Path(__file__).parent / "fixtures" / "claim_attribution_set.psv"
CLAIMS = {
    "G": {"source_class": "claim", "claimant": "Nightjar", "aliases": ["Nightjar Team", "NJ Team"]},
    "T": {"source_class": "claim", "claimant": "Example Tracker (leak-site tracker)"},
}


def _rows() -> list[tuple[str, str, str, str, str]]:
    out = []
    for line in SET.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            split, cat, label, cl, sentence = line.split("|", 4)
            out.append((split, cat, label, cl, sentence))
    return out


# The C-1 rule, verbatim (claimant substring + any verb anywhere), kept here
# only to pin the BEFORE half of the table.
_C1_VERBS = ("claim", "claimed", "claims", "claiming", "post", "posted", "posts", "posting",
             "list", "listed", "lists", "listing", "said", "says", "stated", "asserted",
             "alleged", "alleges", "announced", "announces", "took responsibility",
             "takes responsibility")
_C1_RE = re.compile(r"\b(" + "|".join(re.escape(v) for v in sorted(_C1_VERBS, key=len, reverse=True))
                    + r")\b|\baccording to\b", re.IGNORECASE)


def _c1(sentence: str, claim: dict) -> bool:
    claimant = claim["claimant"]
    if claimant.lower() not in sentence.lower():
        head = re.split(r"\s*[(,;]", claimant, maxsplit=1)[0].strip()
        if not head or head.lower() not in sentence.lower():
            return False
    return _C1_RE.search(sentence) is not None


def _table(fn, split: str | None = None) -> dict[str, int]:
    t = {"fact_flagged": 0, "fact_missed": 0, "fair_flagged": 0, "fair_passed": 0}
    for sp, _cat, label, cl, sentence in _rows():
        if split and sp != split:
            continue
        flagged = not fn(sentence, CLAIMS[cl])
        if label == "fact":
            t["fact_flagged" if flagged else "fact_missed"] += 1
        else:
            t["fair_flagged" if flagged else "fair_passed"] += 1
    return t


def test_the_set_is_large_and_covers_every_category():
    rows = _rows()
    assert len(rows) >= 150
    cats = {c for _s, c, _l, _cl, _x in rows}
    assert {"attributed", "fact", "mixed", "hedged", "quoted", "negated", "alias", "foreign",
            "factive", "role", "tracker", "wrong_claimant"} <= cats
    assert {s for s, *_ in rows} == {"dev", "holdout"}


def test_c1_rule_confusion_table_before():
    assert _table(_c1) == {"fact_flagged": 52, "fact_missed": 40, "fair_flagged": 51, "fair_passed": 77}


def test_c2_rule_misses_no_event_stated_as_fact():
    t = _table(CS.claim_sentence_attributed)
    assert t["fact_missed"] == 0
    assert t == {"fact_flagged": 92, "fact_missed": 0, "fair_flagged": 4, "fair_passed": 124}
    assert _table(CS.claim_sentence_attributed, "holdout") == {
        "fact_flagged": 27, "fact_missed": 0, "fair_flagged": 1, "fair_passed": 26}


@pytest.mark.parametrize("sentence", [
    "Nightjar claimed the attack, and Acme Widgets' plant was shut down for a week [1].",
    "Echo Mills confirmed the breach that Nightjar claimed [1].",
    "Nightjar posted the stolen Delta Freight files [1].",
    "Nightjar confirmed it breached Garnet Foods [1].",
])
def test_c1_passed_what_c2_refuses(sentence: str):
    assert _c1(sentence, CLAIMS["G"]) or "confirmed" in sentence
    assert not CS.claim_sentence_attributed(sentence, CLAIMS["G"])


def test_a_stamp_without_a_claimant_grounds_nothing():
    assert not CS.claim_sentence_attributed("Nightjar claimed it [1].", {"source_class": "claim"})
    assert not CS.claim_sentence_attributed("", CLAIMS["G"])
    assert not CS.claim_sentence_attributed("Nightjar claimed it [1].", None)


def test_hop2_stamp_accepts_any_of_its_claimants():
    stamp = {"source_class": "claim", "claimant": "Nightjar", "claimants": ["Nightjar", "Corvid"]}
    assert CS.claim_sentence_attributed("Corvid claimed an attack on Acme Widgets [[ref:1]].", stamp)


def test_severity_is_a_declared_setting(monkeypatch):
    from legba.data.provenance import verify as V

    monkeypatch.delenv(CA.CLAIM_AS_FACT_SEVERITY_ENV, raising=False)
    assert V.fail_class_for_reason("claim_as_fact") == V.FAIL_CLASS_SOFT
    monkeypatch.setenv(CA.CLAIM_AS_FACT_SEVERITY_ENV, "hard")
    assert V.fail_class_for_reason("claim_as_fact") == V.FAIL_CLASS_HARD
    monkeypatch.setenv(CA.CLAIM_AS_FACT_SEVERITY_ENV, "fatal")     # unknown -> soft
    assert V.fail_class_for_reason("claim_as_fact") == V.FAIL_CLASS_SOFT
    # The setting reaches only this reason.
    monkeypatch.setenv(CA.CLAIM_AS_FACT_SEVERITY_ENV, "hard")
    assert V.fail_class_for_reason("carried_unsupported") == V.FAIL_CLASS_SOFT


def test_hard_severity_reaches_the_span_on_the_floor(monkeypatch):
    from legba.data.provenance import verify as V

    monkeypatch.setenv(CA.CLAIM_AS_FACT_SEVERITY_ENV, "hard")
    cits = [{"marker": "[1]", "signal_id": "s1", "source_id": "source.test.claims",
             "claim": CLAIMS["G"]}]
    rep = V._deterministic_floor("Nightjar breached Acme Widgets [1].", cits)
    assert [s.reason for s in rep.unsupported_spans] == ["claim_as_fact"]
    assert rep.unsupported_spans[0].as_dict()["fail_class"] == V.FAIL_CLASS_HARD
