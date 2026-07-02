# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P0-T2 / P0-T3 — faithfulness verify pass + the (now-active) confidence gate.

These tests are DETERMINISTIC: the optional LLM judge is OFF by default
(``LEGBA_VERIFY_LLM_JUDGE`` unset → the code default-off floor) or mocked. No
test depends on a live LLM call.

Coverage maps to the task's ACCEPTANCE list:

  1. Cited finding, all citations resolve → faithfulness high, the gate fold
     leaves effective_confidence ~= confidence.
  2. A PLANTED fabricated claim (cites a non-existent signal id, or asserts a
     fact with no citation) → flagged unsupported by the DETERMINISTIC floor
     (judge OFF), and the gate fold demotes effective_confidence = min(confidence,
     faithfulness) < confidence.
  3. Legacy uncited finding → effective_confidence == confidence, no fabricated
     verification block.
  4. The findings API hydration carries the verification block naming the
     unsupported spans for a verified finding.
"""

from __future__ import annotations

import os
from typing import Any, Mapping
from uuid import uuid4

import pytest

from legba.data.provenance.models import CritiquePayload
from legba.data.provenance.verify import (
    FaithfulnessReport,
    UnsupportedSpan,
    build_faithfulness_critique_payload,
    verify_finding_faithfulness,
    _canon_ref,
    _deterministic_floor,
    _deterministic_floor_subclaim,
    _is_fact_asserting,
    _llm_judge_enabled,
    _uses_subclaim_convention,
)


# ---------------------------------------------------------------------------
# Fixtures / doubles
# ---------------------------------------------------------------------------


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    reasoning_tokens = 0


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = _Usage()


class _StubJudge:
    """Minimal judge handler — returns a canned strict-JSON verdict list."""

    subprovider = "vllm:llama-3.1-8b"

    def __init__(self, verdicts_json: str | None = None, raise_exc: bool = False):
        self._json = verdicts_json
        self._raise = raise_exc
        self.calls = 0

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None, system=None, **kw):
        self.calls += 1
        if self._raise:
            raise RuntimeError("judge transport down")
        return _Response(self._json or '{"verdicts": []}')


def _cited_body(sid1: str) -> tuple[str, list[dict[str, Any]]]:
    body = (
        "## Key developments\n"
        "- Itaipu hydro upgrade completed this month [1].\n"
        "- Wind capacity hit a record across the northeast [2].\n"
        "## Assessment\n"
        "The grid is diversifying toward renewables under current policy [1].\n"
        "## Indicators to watch\n"
        "- Any reversal in subsidy policy would break this assessment.\n"
    )
    citations = [
        {"marker": "[1]", "signal_id": sid1},
        {"marker": "[2]", "signal_id": str(uuid4())},
    ]
    return body, citations


def test_marker_to_evidence_feeds_signal_text_not_uuid():
    """The unit judge must receive the cited signal's TEXT (title) so it can
    verify a claim — not an opaque signal_id. Falls back to source, then id."""
    from legba.data.provenance.verify import _marker_to_evidence

    cits = [
        {"marker": "[3]", "signal_id": "s-3", "title": "US Navy helicopter emergency landing"},
        {"marker": "[7]", "signal_id": "s-7", "source": "https://ex.com/a"},  # no title
        {"marker": "[9]", "signal_id": "s-9"},  # no title/source
    ]
    ev = _marker_to_evidence(cits)
    assert ev[3] == "US Navy helicopter emergency landing"  # title wins
    assert ev[7] == "https://ex.com/a"  # source fallback
    assert ev[9] == "s-9"  # last-resort id (never fabricated)


# ---------------------------------------------------------------------------
# Deterministic floor — judge OFF
# ---------------------------------------------------------------------------


async def test_all_citations_resolve_high_faithfulness(monkeypatch):
    """ACCEPTANCE 1: every fact-asserting claim cites a resolving signal_id →
    faithfulness 1.0, no unsupported spans, labelled judge-unavailable:flag_off."""
    # The live deploy dir's .env sets LEGBA_VERIFY_LLM_JUDGE=1 (load_dotenv injects
    # it into the pytest session); this test asserts the flag-OFF label, so clear
    # it here (mirrors tests/test_p0_loop_harness.py) — env hygiene, not P3 logic.
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    sid = str(uuid4())
    body, citations = _cited_body(sid)
    rep = await verify_finding_faithfulness(body=body, citations=citations)
    assert rep.faithfulness_score == 1.0
    assert rep.checkable_claims == 3
    assert rep.supported_claims == 3
    assert rep.unsupported_spans == []
    # Judge OFF by default → labelled, never a fabricated judge number.
    assert rep.judge_status == "deterministic"
    assert rep.judge_unavailable_reason == "flag_off"


async def test_planted_fabricated_claim_flagged_by_floor():
    """ACCEPTANCE 2: a claim citing a NON-EXISTENT id and one with NO citation
    are both flagged unsupported by the deterministic floor; score < 1.0."""
    sid = str(uuid4())
    body = (
        "## Key developments\n"
        "- Itaipu hydro upgrade completed this month [1].\n"
        "- A secret nuclear program was launched last week [9].\n"  # [9] not cited
        "- The president resigned amid scandal.\n"                   # no citation
    )
    citations = [{"marker": "[1]", "signal_id": sid}]
    rep = await verify_finding_faithfulness(body=body, citations=citations)
    assert rep.checkable_claims == 3
    assert rep.supported_claims == 1
    assert rep.faithfulness_score == pytest.approx(1 / 3)
    reasons = sorted(s.reason for s in rep.unsupported_spans)
    assert reasons == ["no_citation", "unresolved_citation"]
    # The unresolved one names the marker it falsely cited.
    unresolved = next(s for s in rep.unsupported_spans if s.reason == "unresolved_citation")
    assert unresolved.markers == [9]


async def test_finding_with_no_citations_scores_every_claim_unsupported():
    """A finding that asserts facts but carries an EMPTY citations bridge → every
    fact-asserting claim is an unsupported (no_citation) span; honest, not a pass
    crash."""
    body = "## Key developments\n- Major flood displaced thousands in the region.\n"
    rep = await verify_finding_faithfulness(body=body, citations=[])
    assert rep.checkable_claims == 1
    assert rep.supported_claims == 0
    assert rep.faithfulness_score == 0.0
    assert rep.unsupported_spans[0].reason == "no_citation"


async def test_no_checkable_claims_is_vacuously_faithful():
    """A body that is all forward-looking / structural → no checkable claims →
    score 1.0 with checkable_claims=0 (we never invent a defect)."""
    body = "## Indicators to watch\n- Watch for a policy reversal that would break this.\n"
    rep = await verify_finding_faithfulness(body=body, citations=[])
    assert rep.checkable_claims == 0
    assert rep.faithfulness_score == 1.0
    assert rep.unsupported_spans == []


def test_is_fact_asserting_skips_headings_and_scaffolding():
    assert _is_fact_asserting("The president signed the bill [1].")
    assert not _is_fact_asserting("## Key developments")
    assert not _is_fact_asserting("   ")
    assert not _is_fact_asserting("- —")  # no real words
    assert not _is_fact_asserting("## Indicators to watch")


# ---------------------------------------------------------------------------
# Optional LLM judge — flag-gated + soft-fail (mocked, never live)
# ---------------------------------------------------------------------------


async def test_judge_off_by_default_is_floor(monkeypatch):
    # Clear the live deploy dir's .env LEGBA_VERIFY_LLM_JUDGE=1 so the CODE default
    # (OFF) is what's asserted — env hygiene, mirrors test_p0_loop_harness.
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    assert _llm_judge_enabled() is False  # code default OFF


async def test_judge_engaged_refines_and_can_only_tighten(monkeypatch):
    """With the flag ON and a mocked judge marking one floor-passed claim
    'contradicted', the judge ADDS a semantic span and can only LOWER the score
    (min(floor, judge)). judge_status flips to 'llm'."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    sid = str(uuid4())
    body, citations = _cited_body(sid)
    # 3 claims; judge contradicts the 2nd → 2/3 supported by the judge.
    judge = _StubJudge('{"verdicts": ["supported", "contradicted", "supported"]}')
    rep = await verify_finding_faithfulness(body=body, citations=citations, judge_llm=judge)
    assert judge.calls == 1
    assert rep.judge_status == "llm"
    assert rep.judge_unavailable_reason is None
    # min(floor=1.0, judge=2/3) = 2/3.
    assert rep.faithfulness_score == pytest.approx(2 / 3)
    assert any(s.reason == "judge_contradicted" for s in rep.unsupported_spans)


async def test_judge_error_soft_fails_to_floor(monkeypatch):
    """Flag ON but the judge errors → degrade to the deterministic floor,
    labelled judge-unavailable:judge_error. NEVER fabricates a score."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    sid = str(uuid4())
    body, citations = _cited_body(sid)
    judge = _StubJudge(raise_exc=True)
    rep = await verify_finding_faithfulness(body=body, citations=citations, judge_llm=judge)
    assert rep.judge_status == "deterministic"
    assert rep.judge_unavailable_reason == "judge_error"
    assert rep.faithfulness_score == 1.0  # the floor's result, not invented


async def test_flag_on_but_no_judge_component_labelled(monkeypatch):
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    sid = str(uuid4())
    body, citations = _cited_body(sid)
    rep = await verify_finding_faithfulness(body=body, citations=citations, judge_llm=None)
    assert rep.judge_status == "deterministic"
    assert rep.judge_unavailable_reason == "no_judge_component"


# ---------------------------------------------------------------------------
# #116(b/c/d) — verify polish: labeled-scaffold floor exemption, tally
# reconciliation, fence/prose-tolerant + length-honest judge parsing.
# ---------------------------------------------------------------------------


def test_floor_does_not_penalize_labeled_scaffold_lines():
    """#116(b): a bolded label:value line (**Severity:** High) is scaffolding, not
    a citable fact — the FLOOR must not score it no_citation. The judge still
    grades it (floor-only exemption)."""
    from legba.data.provenance.verify import _is_judgeable_claim

    body = (
        "The lira fell three percent against the dollar today [1].\n"
        "**Severity:** High\n"
        "**Confidence:** Moderate\n"
    )
    citations = [{"marker": "[1]", "signal_id": str(uuid4())}]
    rep = _deterministic_floor(body, citations)
    # Only the one cited factual claim is checkable; the labeled lines are exempt.
    assert rep.checkable_claims == 1
    assert rep.supported_claims == 1
    assert rep.faithfulness_score == 1.0
    assert not any("Severity" in s.text for s in rep.unsupported_spans)
    assert not any("Confidence" in s.text for s in rep.unsupported_spans)
    # FLOOR-only: the judge still SEES the label lines (H1 must not re-widen).
    assert _is_fact_asserting("**Severity:** High") is False
    assert _is_judgeable_claim("**Severity:** High") is True
    # A plain (unbolded) "Foo: bar" sentence stays a checkable claim.
    assert _is_fact_asserting("The bill passed: the senate voted 60-40.") is True


async def test_judge_tally_reconciles_when_floor_and_judge_flag_same_clause(monkeypatch):
    """#116(c): when the deterministic floor AND the judge flag the SAME clause,
    the span is deduped so supported + (non-advisory) unsupported never exceeds
    checkable on the 'llm' path."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    sid = str(uuid4())
    body = (
        "The central bank raised rates by fifty basis points [1].\n"
        "Inflation has spiralled completely out of control nationwide.\n"
    )
    citations = [{"marker": "[1]", "signal_id": sid}]
    # Two claims; the uncited 2nd is flagged by BOTH the floor (no_citation) and
    # the judge (unsupported).
    judge = _StubJudge('{"verdicts": ["supported", "unsupported"]}')
    rep = await verify_finding_faithfulness(body=body, citations=citations, judge_llm=judge)
    assert rep.judge_status == "llm"
    non_advisory = [
        s for s in rep.unsupported_spans
        if s.reason not in ("double_counted", "hedge_laundering")
    ]
    # The shared clause appears ONCE, not twice.
    assert len(non_advisory) == 1
    assert rep.supported_claims + len(non_advisory) <= rep.checkable_claims


async def test_judge_parses_fenced_json_verdicts(monkeypatch):
    """#116(d): a reasoning-class judge that wraps verdicts in a ```json fence
    (and emits prose around it) still parses — no fence-intolerant floor fallback."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    sid = str(uuid4())
    body, citations = _cited_body(sid)  # 3 judgeable claims
    fenced = (
        "Here is my assessment of the three claims.\n"
        "```json\n"
        '{"verdicts": ["supported", "contradicted", "supported"]}\n'
        "```\n"
    )
    judge = _StubJudge(fenced)
    rep = await verify_finding_faithfulness(body=body, citations=citations, judge_llm=judge)
    assert rep.judge_status == "llm"
    assert rep.judge_unavailable_reason is None
    assert rep.faithfulness_score == pytest.approx(2 / 3)
    assert any(s.reason == "judge_contradicted" for s in rep.unsupported_spans)


async def test_judge_short_verdict_list_is_judge_error_not_silent_pass(monkeypatch):
    """#116(d): a verdict list SHORTER than the graded claims must fail HONESTLY
    to the floor labelled judge_error — never a silent zip-truncated partial pass."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    sid = str(uuid4())
    body, citations = _cited_body(sid)  # 3 judgeable claims
    judge = _StubJudge('{"verdicts": ["supported"]}')  # only ONE verdict
    rep = await verify_finding_faithfulness(body=body, citations=citations, judge_llm=judge)
    assert rep.judge_status == "deterministic"
    assert rep.judge_unavailable_reason == "judge_error"
    # The floor's honest result over _cited_body (all citations resolve) — 1.0,
    # NOT a fabricated pass off one verdict.
    assert rep.faithfulness_score == 1.0


def test_extract_json_objects_is_fence_and_prose_tolerant():
    """#116(d): the JSON extractor unwraps a ```json fence, skips leading reasoning
    prose, and ignores trailing text — returning the verdict object."""
    from legba.data.provenance.verify import _extract_json_objects

    # Fenced.
    objs = _extract_json_objects('```json\n{"verdicts": ["supported"]}\n```')
    assert any(o.get("verdicts") == ["supported"] for o in objs)
    # Prose before + after a bare object.
    objs = _extract_json_objects(
        'Let me think... {"verdicts": ["unsupported", "supported"]} done.'
    )
    picked = next(o for o in objs if "verdicts" in o)
    assert picked["verdicts"] == ["unsupported", "supported"]
    # A stray prose brace does not shadow the real verdict object.
    objs = _extract_json_objects('{note: bad} then {"verdicts": ["supported"]}')
    assert any(o.get("verdicts") == ["supported"] for o in objs)


def test_marker_to_evidence_combines_title_and_snippet():
    """#116(e): the unit judge's evidence is title + snippet when both present, so
    a properly-cited clause is graded against the signal's CONTENT, not a terse
    headline. Title-only / source / id fallbacks are byte-preserved."""
    from legba.data.provenance.verify import _marker_to_evidence

    cits = [
        {
            "marker": "[1]",
            "signal_id": "s-1",
            "title": "Fed holds rates",
            "snippet": "The FOMC left the target range unchanged at 5.25-5.50%.",
        },
        {"marker": "[2]", "signal_id": "s-2", "snippet": "Only a snippet here."},
        {"marker": "[3]", "signal_id": "s-3", "title": "Title only"},
    ]
    ev = _marker_to_evidence(cits)
    assert ev[1] == "Fed holds rates — The FOMC left the target range unchanged at 5.25-5.50%."
    assert ev[2] == "Only a snippet here."  # snippet-only fallback
    assert ev[3] == "Title only"  # title-only preserved


# ---------------------------------------------------------------------------
# Critique payload — the gate input + the verification detail block
# ---------------------------------------------------------------------------


def test_critique_payload_carries_gate_keys_and_verification():
    """The built payload validates as a CritiquePayload and DUMPS overall_score
    + analyzed_output_id to the TOP LEVEL (the gate JOIN keys) with the
    verification detail nested under data."""
    fid = uuid4()
    rep = FaithfulnessReport(
        faithfulness_score=0.4,
        checkable_claims=5,
        supported_claims=2,
        unsupported_spans=[UnsupportedSpan(text="uncited claim", reason="no_citation")],
        judge_status="deterministic",
        judge_unavailable_reason="flag_off",
    )
    payload = build_faithfulness_critique_payload(
        rep, analyzed_output_id=fid, analyzed_analyst_id="country_assessor",
    )
    cp = CritiquePayload.model_validate(payload)
    assert cp.analyzed_output_id == fid
    assert cp.overall_score == 0.4
    dumped = cp.model_dump(mode="json")
    # Gate JOIN reads data->>'overall_score' + data->>'analyzed_output_id' off the
    # analyst_outputs row — these MUST be top-level after the whole payload is
    # model_dumped into the JSONB data column.
    assert dumped["overall_score"] == 0.4
    assert dumped["analyzed_output_id"] == str(fid)
    # The API reads data->'data'->'verification'.
    verification = dumped["data"]["verification"]
    assert verification["faithfulness_score"] == 0.4
    assert verification["unsupported_spans"][0]["reason"] == "no_citation"


# ---------------------------------------------------------------------------
# P0-T3 — the gate fold + the findings API verification block
# ---------------------------------------------------------------------------


def _hydration_row(confidence: float, critic_score: Any, verification: Any) -> dict[str, Any]:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return {
        "id": uuid4(),
        "kind": "finding",
        "title": "t",
        "body": "b",
        "confidence": confidence,
        "severity": None,
        "data": {},
        "target_id": "country_g20_br",
        "target_version": "1",
        "analyst_id": "country_assessor",
        "analyst_version": "1",
        "produced_at": now,
        "derived_from": [],
        "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
        "run_id": None,
        "created_at": now,
        "critic_score": critic_score,
        "verification": verification,
    }


# ---------------------------------------------------------------------------
# P3-T3 / P3-T7 — the COMPOSITION ([[ref:N]] → sub-claim) verify branch.
#
# The SAME verify pass self-detects the composition bridge (a citation carrying
# ``ref_kind='finding'`` / a ``[[ref:`` marker) and switches to the ordinal-keyed
# sub-claim floor + the T7 anti-double-counting + hedge-laundering guards. The
# unit ([N]) path stays byte-identical (proven by the tests above still passing).
# ---------------------------------------------------------------------------


def _comp_citation(
    ordinal,
    *,
    ref_id=None,
    eff=None,
    derived=None,
    source="leadership_transition",
    title="sub-claim title",
    evidence_text="the unit found X",
):
    """A composition citation as stamped by the synth CITE block (kind-aware,
    ordinal-keyed): ``marker='[[ref:N]]'`` + ``ordinal=N`` (the resolution key) +
    ``ref_id=<finding uuid>`` (the drill target) + ``ref_kind='finding'``."""
    c = {
        "marker": f"[[ref:{ordinal}]]",
        "ordinal": ordinal,
        "ref_id": ref_id or str(uuid4()),
        "ref_kind": "finding",
        "source": source,
        "title": title,
        "evidence_text": evidence_text,
        "derived_from": [str(x) for x in (derived or [])],
    }
    if eff is not None:
        c["effective_confidence"] = float(eff)
    return c


def test_discriminator_selects_convention():
    """``ref_kind='finding'`` / ``[[ref:N]]`` markers → subclaim convention;
    ``[N]`` + signal_id → signal (default)."""
    u = uuid4()
    assert _uses_subclaim_convention([_comp_citation(1)]) is True
    # ref_kind alone (no marker) still routes to the sub-claim floor.
    assert _uses_subclaim_convention([{"ref_kind": "finding", "ref_id": str(u)}]) is True
    # A legacy stored uuid-marker composition still routes to the sub-claim floor
    # via the ``[[ref:`` prefix (back-compat).
    assert _uses_subclaim_convention([{"marker": f"[[ref:{u}]]", "signal_id": str(u)}]) is True
    assert _uses_subclaim_convention([{"marker": "[1]", "signal_id": str(u)}]) is False
    assert _uses_subclaim_convention([]) is False
    assert _uses_subclaim_convention(None) is False


def test_canon_ref_canonicalizes_case_and_rejects_junk():
    u = uuid4()
    assert _canon_ref(str(u).upper()) == str(u)
    assert _canon_ref(u) == str(u)
    assert _canon_ref("not-a-uuid") is None
    assert _canon_ref(None) is None


async def test_composition_all_clauses_cite_resolved_subclaims_high():
    """Every composed clause cites a resolved sub-claim → faithfulness 1.0, no
    unsupported spans, uses the subclaim floor (ceiling set from eff)."""
    body = (
        "Leadership looks stable as of the latest sweep [[ref:1]].\n"
        "Energy supply is adequate under current policy [[ref:2]].\n"
    )
    citations = [
        _comp_citation(1, eff=0.7, derived=["sig-a"]),
        _comp_citation(2, eff=0.6, derived=["sig-b"]),
    ]
    rep = await verify_finding_faithfulness(body=body, citations=citations)
    assert rep.checkable_claims == 2
    assert rep.supported_claims == 2
    assert rep.faithfulness_score == 1.0
    assert [s for s in rep.unsupported_spans if s.reason != "double_counted"] == []
    # Independent evidence (disjoint derived_from) → ceiling is the strongest.
    assert rep.confidence_ceiling == pytest.approx(0.7)


async def test_composition_unsupported_clause_flagged_by_floor():
    """A composed clause citing an ordinal NOT in the cited set → unresolved_citation;
    a fact-asserting clause with NO marker → no_citation. Score < 1.0."""
    body = (
        "Leadership is contested [[ref:1]].\n"
        "A secret escalation is underway [[ref:9]].\n"  # ordinal 9 not cited
        "The economy collapsed overnight.\n"  # no marker
    )
    citations = [_comp_citation(1, eff=0.5, derived=["sig-a"])]
    rep = await verify_finding_faithfulness(body=body, citations=citations)
    assert rep.checkable_claims == 3
    assert rep.supported_claims == 1
    assert rep.faithfulness_score == pytest.approx(1 / 3)
    reasons = sorted(
        s.reason for s in rep.unsupported_spans if s.reason != "double_counted"
    )
    assert reasons == ["no_citation", "unresolved_citation"]
    unresolved = next(s for s in rep.unsupported_spans if s.reason == "unresolved_citation")
    assert unresolved.markers == [9]  # INT ordinal markers now


async def test_composition_double_counted_ceiling_is_max_not_sum():
    """Two cited sub-claims sharing a derived_from signal → one component →
    flagged double_counted, and the ceiling is the component MAX (0.5), NEVER the
    naive sum (1.0) — correlated evidence cannot inflate the ceiling."""
    body = (
        "Leadership is contested [[ref:1]].\n"
        "The transition is unstable [[ref:2]].\n"
    )
    # Both sub-claims rest on the SAME underlying signal → correlated.
    citations = [
        _comp_citation(1, eff=0.5, derived=["shared-sig", "sig-x"]),
        _comp_citation(2, eff=0.5, derived=["shared-sig", "sig-y"]),
    ]
    rep = await verify_finding_faithfulness(body=body, citations=citations)
    assert rep.faithfulness_score == 1.0  # both clauses cite resolved sub-claims
    dc = [s for s in rep.unsupported_spans if s.reason == "double_counted"]
    assert len(dc) == 1
    assert sorted(dc[0].markers) == [1, 2]
    # THE T7 INVARIANT: ceiling is the max within the single component, not a sum.
    assert rep.confidence_ceiling == pytest.approx(0.5)
    assert rep.confidence_ceiling < 1.0


async def test_composition_hedge_laundering_flagged_and_capped():
    """A clause asserting confidence 0.9 over a cited sub-claim with
    effective_confidence 0.5 → flagged hedge_laundering AND overall_score capped
    at <= 0.5 (the payload folds min(faithfulness, ceiling))."""
    body = "The country is on the brink of collapse [[ref:1]].\n"
    citations = [_comp_citation(1, eff=0.5, derived=["sig-a"])]
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, finding_confidence=0.9
    )
    assert any(s.reason == "hedge_laundering" for s in rep.unsupported_spans)
    assert rep.confidence_ceiling == pytest.approx(0.5)
    # The gate score is capped by the ceiling.
    fid = uuid4()
    payload = build_faithfulness_critique_payload(rep, analyzed_output_id=fid)
    assert payload["overall_score"] == pytest.approx(0.5)
    assert payload["overall_score"] <= 0.5
    verification = payload["data"]["verification"]
    assert verification["confidence_ceiling"] == pytest.approx(0.5)


async def test_composition_no_resolvable_subclaims_floors_low_not_faked():
    """HONESTY: a composition asserting facts but whose markers resolve to NO
    cited sub-claim (all fabricated) floors low, never a faked pass."""
    body = (
        "Leadership collapsed [[ref:9]].\n"  # ordinal 9 — out of the cited set
        "The government fell overnight.\n"
    )
    # The bridge cites a DIFFERENT (real) sub-claim (ordinal 1); the body's markers
    # don't match.
    citations = [_comp_citation(1, eff=0.4, derived=["sig-a"])]
    rep = await verify_finding_faithfulness(body=body, citations=citations)
    assert rep.faithfulness_score == 0.0
    assert rep.supported_claims == 0


async def test_composition_missing_eff_no_ceiling_no_hedge_flag():
    """HONESTY: citations without effective_confidence never fabricate a cap or a
    hedge flag; ceiling is None so the payload overall == faithfulness."""
    body = "Leadership is contested [[ref:1]].\n"
    citations = [_comp_citation(1, eff=None, derived=["sig-a"])]  # no eff
    rep = await verify_finding_faithfulness(
        body=body, citations=citations, finding_confidence=0.99
    )
    assert rep.confidence_ceiling is None
    assert not any(s.reason == "hedge_laundering" for s in rep.unsupported_spans)
    fid = uuid4()
    payload = build_faithfulness_critique_payload(rep, analyzed_output_id=fid)
    assert payload["overall_score"] == pytest.approx(rep.faithfulness_score)


async def test_unit_path_byte_identical_when_finding_confidence_passed():
    """Passing finding_confidence on the UNIT ([N]) path is inert — the subclaim
    guard never fires (disjoint regex), so the score is unchanged and no ceiling."""
    sid = str(uuid4())
    body, citations = _cited_body(sid)
    rep_no_conf = await verify_finding_faithfulness(body=body, citations=citations)
    rep_with_conf = await verify_finding_faithfulness(
        body=body, citations=citations, finding_confidence=0.99
    )
    assert rep_with_conf.faithfulness_score == rep_no_conf.faithfulness_score == 1.0
    assert rep_with_conf.confidence_ceiling is None
    assert not any(
        s.reason in ("double_counted", "hedge_laundering")
        for s in rep_with_conf.unsupported_spans
    )


def test_gate_fold_high_faithfulness_keeps_confidence():
    """ACCEPTANCE 1 (gate): a high faithfulness critic_score leaves
    effective_confidence ~= confidence."""
    from legba.data.registry.substrate_reads_api import _hydrate_finding

    row = _hydration_row(confidence=0.85, critic_score=1.0, verification={
        "faithfulness_score": 1.0, "unsupported_spans": [], "judge_status": "deterministic",
    })
    fr = _hydrate_finding(row)
    assert fr.effective_confidence == 0.85
    assert fr.verification["faithfulness_score"] == 1.0


def test_gate_fold_low_faithfulness_demotes_confidence():
    """ACCEPTANCE 2 (gate): a poor faithfulness score demotes
    effective_confidence = min(confidence, faithfulness) < confidence, and the
    verification block names the unsupported spans."""
    from legba.data.registry.substrate_reads_api import _hydrate_finding

    spans = [{"text": "secret program [9]", "reason": "unresolved_citation", "markers": [9]}]
    row = _hydration_row(confidence=0.85, critic_score=0.33, verification={
        "faithfulness_score": 0.33, "unsupported_spans": spans, "judge_status": "deterministic",
    })
    fr = _hydrate_finding(row)
    assert fr.confidence == 0.85
    assert fr.critic_score == pytest.approx(0.33)
    assert fr.effective_confidence == pytest.approx(0.33)
    assert fr.effective_confidence < fr.confidence
    # ACCEPTANCE 4: the verification block names WHY it was demoted.
    assert fr.verification["unsupported_spans"][0]["reason"] == "unresolved_citation"


def test_legacy_unverified_finding_no_regression_no_block():
    """ACCEPTANCE 3: a finding with NO faithfulness critique (NULL critic_score,
    NULL verification) → effective_confidence == confidence and NO fabricated
    verification block."""
    from legba.data.registry.substrate_reads_api import _hydrate_finding

    row = _hydration_row(confidence=0.7, critic_score=None, verification=None)
    fr = _hydrate_finding(row)
    assert fr.effective_confidence == 0.7
    assert fr.critic_score is None
    assert fr.verification is None
