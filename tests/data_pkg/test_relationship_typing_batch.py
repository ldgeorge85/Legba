# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-G2 — the batch typing harness.

The batch layer's whole job is to keep N candidates' verdicts correlated to the
right pairs while staying exactly as strict as the single-candidate reifier
path. These tests pin both halves: correlation (idx matching, truncation
salvage, spurious/duplicate detection) and strictness (the reifier's coercion
still governs what is accepted).
"""

from __future__ import annotations

import json

import pytest

from legba.data.analysts.relationship_typing_batch import (
    BATCH_SYSTEM_PROMPT,
    BatchCandidate,
    build_batch_user_prompt,
    extract_batch_objects,
    max_tokens_for_batch,
    parse_batch_response,
)


def _cand(idx: int, a: str = "Iran", b: str = "Israel", **kw) -> BatchCandidate:
    kw.setdefault("evidence_text", "Iran struck Israeli positions overnight.")
    return BatchCandidate(idx=idx, source=a, target=b, ref=f"ref-{idx}", **kw)


def _verdict(idx: int, **over) -> dict:
    base = {
        "idx": idx,
        "related": True,
        "subject": "Iran",
        "object": "Israel",
        "intermediary": None,
        "rel_type": "HostileTo",
        "intent": "hostile",
        "channel": "direct",
        "confidence": 0.7,
        "rationale": "strike reported",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def test_batch_prompt_states_vocabulary_once_and_numbers_every_candidate():
    cands = [_cand(0), _cand(1, "Russia", "Ukraine"), _cand(2, "China", "Taiwan")]
    prompt = build_batch_user_prompt(cands)
    # The allowed-type list is the bulk of the per-call overhead the single
    # path repeats; batching must state it exactly once.
    assert prompt.count("Allowed rel_type values:") == 1
    for c in cands:
        assert f"--- CANDIDATE {c.idx} ---" in prompt
        assert c.source in prompt and c.target in prompt
    assert "array of 3" in prompt


def test_batch_prompt_truncates_evidence_to_budget():
    long_evidence = "x" * 5000
    prompt = build_batch_user_prompt(
        [_cand(0, evidence_text=long_evidence)], evidence_chars=100
    )
    assert "x" * 100 in prompt
    assert "x" * 101 not in prompt


def test_batch_prompt_scopes_intermediaries_to_their_own_candidate():
    cands = [
        _cand(0, intermediaries=("Hezbollah",)),
        _cand(1, "Russia", "Ukraine", intermediaries=("Wagner",)),
    ]
    prompt = build_batch_user_prompt(cands)
    # Each offered set must sit inside its own candidate block, so a model
    # cannot borrow candidate 1's cut-out for candidate 0.
    block0 = prompt.split("--- CANDIDATE 1 ---")[0]
    assert "Hezbollah" in block0
    assert "Wagner" not in block0


def test_system_prompt_carries_the_reject_freely_instruction():
    # The graph is sparse-by-design; the batch prompt must say so or a model
    # will type every co-mention it is shown.
    assert "Reject freely" in BATCH_SYSTEM_PROMPT
    assert "idx" in BATCH_SYSTEM_PROMPT


def test_max_tokens_scales_with_batch():
    assert max_tokens_for_batch(1) < max_tokens_for_batch(12) < max_tokens_for_batch(24)
    # Linear in N — truncation is the costly failure, so the budget tracks it.
    d1 = max_tokens_for_batch(13) - max_tokens_for_batch(12)
    d2 = max_tokens_for_batch(25) - max_tokens_for_batch(24)
    assert d1 == d2


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_extract_plain_array():
    raw = json.dumps([_verdict(0), _verdict(1)])
    objs, truncated = extract_batch_objects(raw)
    assert len(objs) == 2 and not truncated


def test_extract_handles_fenced_and_prefixed_output():
    body = json.dumps([_verdict(0)])
    for raw in (
        f"```json\n{body}\n```",
        f"Here are the verdicts:\n{body}",
        f"```\n{body}\n```",
    ):
        objs, truncated = extract_batch_objects(raw)
        assert len(objs) == 1, raw[:40]
        assert not truncated


def test_extract_handles_object_wrapped_array():
    raw = json.dumps({"verdicts": [_verdict(0), _verdict(1)]})
    objs, _ = extract_batch_objects(raw)
    assert len(objs) == 2


def test_extract_salvages_truncated_array():
    # The dominant large-N failure: budget runs out mid-object. Every COMPLETE
    # object before the cut must still be recovered.
    full = json.dumps([_verdict(0), _verdict(1), _verdict(2)])
    cut = full[: full.rindex("{")] + '{"idx": 2, "related": tr'
    objs, truncated = extract_batch_objects(cut)
    assert truncated is True
    assert [o["idx"] for o in objs] == [0, 1]


def test_extract_is_string_aware():
    # A brace inside a rationale string must not desynchronise the scanner.
    v = _verdict(0, rationale="odd } brace { inside")
    raw = "prefix noise " + json.dumps([v])
    objs, truncated = extract_batch_objects(raw)
    assert len(objs) == 1
    assert objs[0]["rationale"] == "odd } brace { inside"


def test_extract_empty_and_garbage():
    assert extract_batch_objects("") == ([], False)
    assert extract_batch_objects("no json here") == ([], False)


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------


def test_verdicts_match_by_idx_not_position():
    cands = [_cand(0, "Iran", "Israel"), _cand(1, "Russia", "Ukraine")]
    # Model returns them REVERSED. Matching by position would swap the pairs.
    raw = json.dumps([
        _verdict(1, subject="Russia", object="Ukraine"),
        _verdict(0, subject="Iran", object="Israel"),
    ])
    res = parse_batch_response(raw, cands)
    by_idx = {v.idx: v for v in res.verdicts}
    assert by_idx[0].source == "Iran"
    assert by_idx[1].source == "Russia"
    assert res.parse_ok


def test_missing_idx_is_reported_not_guessed():
    cands = [_cand(0), _cand(1, "Russia", "Ukraine"), _cand(2, "China", "Taiwan")]
    raw = json.dumps([_verdict(0), _verdict(2)])
    res = parse_batch_response(raw, cands)
    assert res.missing_idx == [1]
    assert res.recovered == 2
    assert not res.parse_ok
    assert res.recovery_rate == pytest.approx(2 / 3)


def test_unexpected_and_duplicate_idx_are_flagged():
    cands = [_cand(0), _cand(1, "Russia", "Ukraine")]
    raw = json.dumps([_verdict(0), _verdict(0), _verdict(1), _verdict(99)])
    res = parse_batch_response(raw, cands)
    assert res.duplicate_idx == [0]
    assert res.unexpected_idx == [99]
    assert not res.parse_ok
    # First wins for the duplicate; both real candidates still answered.
    assert sorted(v.idx for v in res.verdicts) == [0, 1]


def test_positional_fallback_only_when_unambiguous():
    cands = [_cand(0), _cand(1, "Russia", "Ukraine")]
    no_idx = [
        {k: v for k, v in _verdict(0).items() if k != "idx"},
        {k: v for k, v in _verdict(1, subject="Russia", object="Ukraine").items()
         if k != "idx"},
    ]
    res = parse_batch_response(json.dumps(no_idx), cands)
    assert res.recovered == 2
    by_idx = {v.idx: v for v in res.verdicts}
    assert by_idx[1].source == "Russia"

    # Count mismatch => refuse to guess; mis-assignment is worse than loss.
    res2 = parse_batch_response(json.dumps(no_idx[:1]), cands)
    assert res2.recovered == 0
    assert res2.missing_idx == [0, 1]


def test_truncated_batch_still_yields_completed_verdicts():
    cands = [_cand(i) for i in range(3)]
    full = json.dumps([_verdict(0), _verdict(1), _verdict(2)])
    cut = full[: full.rindex("{")] + '{"idx": 2, "rel'
    res = parse_batch_response(cut, cands)
    assert res.truncated
    assert res.recovered == 2
    assert res.missing_idx == [2]


# ---------------------------------------------------------------------------
# Strictness — the reifier's own validation still governs
# ---------------------------------------------------------------------------


def test_model_reject_is_recorded_as_unaccepted():
    res = parse_batch_response(json.dumps([_verdict(0, related=False)]), [_cand(0)])
    v = res.verdicts[0]
    assert v.accepted is False
    assert v.reject_reason == "model_reject"
    assert v.payload is None


def test_off_list_rel_type_is_refused_by_coercion():
    raw = json.dumps([_verdict(0, rel_type="VibesWith")])
    res = parse_batch_response(raw, [_cand(0)])
    v = res.verdicts[0]
    assert v.accepted is False
    assert v.reject_reason == "coercion_reject"


def test_demonym_self_loop_is_refused():
    # 'Iranian' canonicalises to 'Iran' -> subject == object -> not a relationship.
    raw = json.dumps([_verdict(0, subject="Iran", object="Iranian")])
    res = parse_batch_response(raw, [_cand(0, "Iran", "Iranian")])
    assert res.verdicts[0].accepted is False
    assert res.verdicts[0].reject_reason == "coercion_reject"


def test_unoffered_intermediary_is_dropped_not_accepted_verbatim():
    raw = json.dumps([_verdict(0, intermediary="Hezbollah", channel="proxy")])
    # No intermediaries offered for this candidate -> must be nulled, and a
    # proxy channel without a cut-out collapses to direct.
    res = parse_batch_response(raw, [_cand(0)])
    v = res.verdicts[0]
    assert v.accepted is True
    assert v.intermediary is None
    assert v.channel == "direct"


def test_offered_intermediary_survives():
    raw = json.dumps([_verdict(0, intermediary="Hezbollah", channel="proxy",
                               rel_type="SuppliesWeaponsTo")])
    res = parse_batch_response(raw, [_cand(0, intermediaries=("Hezbollah",))])
    v = res.verdicts[0]
    assert v.accepted is True
    assert v.intermediary == "Hezbollah"
    assert v.channel == "proxy"


def test_polarity_is_deterministic_from_intent_not_from_the_model():
    # The reifier stopped trusting the model's free polarity integer; the batch
    # path must inherit that. A hostile intent signs -1 even if the model says +1.
    raw = json.dumps([_verdict(0, intent="hostile", polarity=1)])
    res = parse_batch_response(raw, [_cand(0)])
    assert res.verdicts[0].polarity == -1


def test_sports_gate_applies_through_the_batch_path():
    cands = [_cand(0, "Spain", "Morocco", evidence_text="World Cup group stage draw")]
    raw = json.dumps([_verdict(0, subject="Spain", object="Morocco",
                               intent="hostile", rel_type="HostileTo")])
    res = parse_batch_response(raw, cands)
    v = res.verdicts[0]
    # A fixture must not poison the signed graph with fake antagonism.
    assert v.polarity == 0
    assert v.intent == "neutral"


def test_sports_gate_uses_wider_gate_text_when_supplied():
    cands = [_cand(0, "Spain", "Morocco", evidence_text="Spain faced Morocco.")]
    raw = json.dumps([_verdict(0, subject="Spain", object="Morocco",
                               intent="hostile", rel_type="HostileTo")])
    res = parse_batch_response(
        raw, cands, sports_gate_text={0: "Spain faced Morocco. World Cup quarter-final"}
    )
    assert res.verdicts[0].polarity == 0


def test_conflict_vocabulary_blocks_the_sports_downgrade():
    # A real hostile dyad whose lineage happens to include sports vocab must
    # NOT be erased.
    cands = [_cand(0, "Russia", "Ukraine",
                   evidence_text="World Cup ban; 225 killed in shelling on the front line")]
    raw = json.dumps([_verdict(0, subject="Russia", object="Ukraine",
                               intent="hostile", rel_type="HostileTo")])
    res = parse_batch_response(raw, cands)
    assert res.verdicts[0].polarity == -1


def test_ref_is_carried_through_for_worksheet_join():
    res = parse_batch_response(json.dumps([_verdict(0)]), [_cand(0)])
    assert res.verdicts[0].ref == "ref-0"


def test_empty_response_reports_every_candidate_missing():
    cands = [_cand(i) for i in range(5)]
    res = parse_batch_response("", cands)
    assert res.missing_idx == [0, 1, 2, 3, 4]
    assert res.recovered == 0
    assert res.recovery_rate == 0.0
