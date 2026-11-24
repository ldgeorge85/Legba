# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S2 — gate G7 (``data/statement_independence.py``): corroboration counts
origins, not articles. Planted: twenty syndicated copies count once; a
paraphrase from a second owner counts twice. Property: the count is
order-free, never exceeds the distinct keys, and a copy never raises it."""
from __future__ import annotations

import random

from legba.data import statement_independence as ind
from legba.data.statement_shapes import EvidenceTexts, SupportRow

STORY = (
    "Government forces and rebel fighters clashed overnight near the northern border "
    "crossing, leaving at least twelve soldiers dead according to a military spokesman, "
    "who said reinforcements had been sent to secure the road and that the crossing "
    "would remain closed to civilian traffic until further notice."
)
PARAPHRASE = (
    "A military spokesman said twelve soldiers died when rebels attacked troops at the "
    "northern frontier post late on Tuesday; the army has closed the crossing to "
    "civilians and moved extra units to guard the highway."
)


def _row(i, *, text_key=None, group=None, source=None, role="evidence", **kw):
    return SupportRow(role=role, support_kind="signal", ref_id=f"sig-{i}",
                      quote="twelve soldiers dead", fetch_version_id=text_key,
                      source_id=source if source is not None else f"src-{i}",
                      origin_group_id=group, **kw)


def test_planted_twenty_syndicated_copies_count_as_one():
    texts = {}
    rows = []
    for i in range(20):
        # each outlet adds its own dateline and footer around the wire text
        texts[i] = f"OUTLET {i} — {STORY} Copyright outlet {i}."
        rows.append(_row(i, text_key=i))
    count = ind.count_groups(rows, EvidenceTexts(by_fetch_version=texts))
    assert count.groups == 1
    assert len(count.collapsed_by_text) == 19
    assert count.basis == ind.BASIS_FALLBACK  # no origin groups declared


def test_paraphrase_from_a_second_owner_counts_as_two_and_says_its_limit():
    texts = EvidenceTexts(by_fetch_version={1: STORY, 2: PARAPHRASE})
    count = ind.count_groups([_row(1, text_key=1, group="g-wire"),
                              _row(2, text_key=2, group="g-other")], texts)
    assert count.groups == 2
    body = count.as_dict()
    assert body["basis"] == ind.BASIS_GROUPS
    assert "paraphrases" in body["limit"]


def test_declared_origin_group_collapses_distinct_texts():
    texts = EvidenceTexts(by_fetch_version={1: STORY, 2: PARAPHRASE})
    count = ind.count_groups([_row(1, text_key=1, group="g"), _row(2, text_key=2, group="g")], texts)
    assert count.groups == 1


def test_fallback_is_each_source_its_own_group():
    texts = EvidenceTexts(by_fetch_version={1: STORY, 2: PARAPHRASE})
    same_source = ind.count_groups([_row(1, text_key=1, source="s"), _row(2, text_key=2, source="s")], texts)
    assert same_source.groups == 1 and same_source.basis == ind.BASIS_FALLBACK


def test_checkable_groups_need_a_checkable_row():
    texts = EvidenceTexts(by_fetch_version={1: STORY, 2: PARAPHRASE})
    rows = [_row(1, text_key=1, access_class="public"), _row(2, text_key=2, access_class="restricted")]
    count = ind.count_groups(rows, texts)
    assert (count.groups, count.checkable_groups) == (2, 1)
    rows = [_row(1, text_key=1, checkable=False, access_class="public"),
            _row(2, text_key=2, access_class=None)]
    assert ind.count_groups(rows, texts).checkable_groups == 0


def test_only_evidence_rows_count_and_contradictions_are_separate():
    texts = EvidenceTexts(by_fetch_version={1: STORY, 2: PARAPHRASE, 3: "Officials denied any soldiers died in the clash at the crossing on Tuesday night, calling reports false."})
    rows = [_row(1, text_key=1), _row(2, text_key=2, role="background"),
            _row(3, text_key=3, role="contradicts")]
    assert ind.count_groups(rows, texts).groups == 1
    assert ind.contradicting_groups(rows, texts) == 1
    # a contradiction from the evidence's own origin is not counted
    rows_same = [_row(1, text_key=1, group="g"), _row(3, text_key=3, group="g", role="contradicts")]
    assert ind.contradicting_groups(rows_same, texts) == 0


def test_support_word_is_derived_in_code():
    assert ind.derive_support_word(0) == "unclear"
    assert ind.derive_support_word(1) == "single_source"
    assert ind.derive_support_word(3) == "corroborated"
    assert ind.derive_support_word(3, 1) == "corroborated"   # one contradicting outlet changes nothing
    assert ind.derive_support_word(3, 2) == "contested"
    assert set(ind.SUPPORT_WORDS) == {"corroborated", "single_source", "contested", "unclear"}


def test_short_texts_collapse_only_on_equality():
    a, b = ind.shingles("twelve soldiers dead"), ind.shingles("twelve soldiers were killed")
    assert not ind.near_identical(a, b)
    assert ind.near_identical(a, ind.shingles("Twelve  SOLDIERS dead"))


def test_property_order_free_bounded_and_copies_never_raise_the_count():
    rng = random.Random(77)
    bodies = [STORY, PARAPHRASE,
              "Floods closed three schools in the capital on Monday after heavy rain fell for two days straight across the valley.",
              "The central bank left its policy rate unchanged at its meeting, citing slowing inflation and weak growth in exports."]
    for _ in range(200):
        n = rng.randrange(1, 8)
        texts = {}
        rows = []
        for i in range(n):
            texts[i] = rng.choice(bodies)
            rows.append(_row(i, text_key=i, group=rng.choice([None, "g1", "g2"]),
                             source=rng.choice(["s1", "s2", "s3"])))
        et = EvidenceTexts(by_fetch_version=texts)
        base = ind.count_groups(rows, et).groups
        shuffled = rows[:]
        rng.shuffle(shuffled)
        assert ind.count_groups(shuffled, et).groups == base
        assert 1 <= base <= len({ind.group_key(r)[0] for r in rows})
        copy_src = rng.choice(rows)
        texts2 = dict(texts)
        texts2[100] = texts[copy_src.fetch_version_id]
        more = [*rows, _row(100, text_key=100, source="s-new")]
        assert ind.count_groups(more, EvidenceTexts(by_fetch_version=texts2)).groups == base
