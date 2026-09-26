# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""B5 (JOURNAL_CONNECTIVE_AUDIT_PROPOSAL_2026-09-09 §6, the T1.3 reader lane's
live finding on entry dde520d3) — REPAIR a hand-copied ``[[ref:<uuid>]]``
typo against the run's own gathered window, by Hamming distance. No DB —
pure-function tests; the full-arc "flag + data reach a written entry" proof
lives in ``tests/journal_w1/test_journal_arc.py`` beside its siblings.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from legba.data.analysts import journal_reflect as jr


def test_exact_match_left_untouched() -> None:
    ref = uuid4()
    body = f"A claim [[ref:{ref}]]."
    new_body, repairs = jr._repair_ref_markers(body, gathered_ref_ids=[str(ref)])
    assert new_body == body
    assert repairs == []


def test_one_digit_typo_is_repaired_and_recorded() -> None:
    # The live defect: entry dde520d3's police-civilian span cited this typo;
    # the real GDELT "PRISON: coerce in Kansas" row is one hex digit off.
    good = "233bd806-5bee-4e3c-b0c3-1c9e794c52ee"
    typo = "233bd806-5bee-4c3c-b0c3-1c9e794c52ee"
    body = f"legal-security reports trace a pattern [[ref:{typo}]]."
    new_body, repairs = jr._repair_ref_markers(body, gathered_ref_ids=[good])
    assert f"[[ref:{good}]]" in new_body
    assert f"[[ref:{typo}]]" not in new_body
    assert repairs == [{"from": typo, "to": good}]


def test_ambiguous_tie_leaves_the_marker_untouched() -> None:
    typo = "00000000-0000-0000-0000-000000000000"
    candidate_a = "10000000-0000-0000-0000-000000000000"  # distance 1
    candidate_b = "01000000-0000-0000-0000-000000000000"  # distance 1, different digit
    body = f"A claim [[ref:{typo}]]."
    new_body, repairs = jr._repair_ref_markers(
        body, gathered_ref_ids=[candidate_a, candidate_b]
    )
    # never guess between two equally-close candidates — the reader renders
    # this ref "unresolved" honestly rather than this repairing it wrong.
    assert new_body == body
    assert repairs == []


def test_distance_over_two_leaves_the_marker_untouched() -> None:
    typo = "00000000-0000-0000-0000-000000000000"
    far = "abcdefab-cdef-abcd-efab-cdefabcdefab"  # every hex char differs
    body = f"A claim [[ref:{typo}]]."
    new_body, repairs = jr._repair_ref_markers(body, gathered_ref_ids=[far])
    assert new_body == body
    assert repairs == []


def test_none_ref_set_is_byte_identical_to_today() -> None:
    """The default — every pre-B5 caller of _repair_ref_markers, and every
    caller of _reflect_claims (which this never touches) — is a strict
    no-op, with or without explicitly passing None."""
    body = "A claim [[ref:00000000-0000-0000-0000-000000000000]]."
    new_body, repairs = jr._repair_ref_markers(body)
    assert new_body == body
    assert repairs == []
    new_body2, repairs2 = jr._repair_ref_markers(body, gathered_ref_ids=None)
    assert new_body2 == body
    assert repairs2 == []
    new_body3, repairs3 = jr._repair_ref_markers(body, gathered_ref_ids=[])
    assert new_body3 == body
    assert repairs3 == []


def test_multiple_markers_each_repaired_independently() -> None:
    good_a = "11111111-1111-1111-1111-111111111111"
    good_b = "22222222-2222-2222-2222-222222222222"
    typo_a = "11111111-1111-1111-1111-111111111110"  # last char off
    typo_b = "22222222-2222-2222-2222-222222222223"  # last char off
    body = f"First [[ref:{typo_a}]]. Second [[ref:{typo_b}]]."
    new_body, repairs = jr._repair_ref_markers(
        body, gathered_ref_ids=[good_a, good_b]
    )
    assert f"[[ref:{good_a}]]" in new_body
    assert f"[[ref:{good_b}]]" in new_body
    assert {r["from"] for r in repairs} == {typo_a, typo_b}


def test_repaired_ref_survives_into_reflect_claims() -> None:
    """_reflect_claims itself is completely untouched (its own byte-identity
    proof, T1.0, keeps holding) — this proves the repair, run BEFORE
    _reflect_claims, is what makes the fixed UUID show up in the claim."""
    good = uuid4()
    last = str(good)[-1]
    typo = UUID(str(good)[:-1] + ("0" if last != "0" else "1"))
    body = f"A pattern of coercive encounters [[ref:{typo}]]."
    repaired_body, repairs = jr._repair_ref_markers(body, gathered_ref_ids=[str(good)])
    assert repairs
    claims, cited_refs, _ = jr._reflect_claims(repaired_body)
    assert good in cited_refs
    assert any(good in c.refs for c in claims)
