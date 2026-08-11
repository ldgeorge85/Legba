# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P2-4 — hard/soft verdict labels + per-claim SUPPORT persistence + the staged
independence-posture judge prompt profile.

Three contract surfaces, all LABELS + PERSISTENCE only (scores / floors /
pass-fail semantics byte-unchanged — the pre-existing verify suites are the
regression proof):

  1. FAIL-CLASS mapping (the Primer taxonomy) — every span reason classifies
     ``hard_fail`` (entity scramble / contradicted-by-source / fabricated
     citation) or ``soft_fail`` (unsupported inference / hedge laundering /
     overclaim). ONE table (``verify._FAIL_CLASS_BY_REASON``) + an AST-level
     DRIFT GUARD: every reason verify.py can emit must be mapped, and every
     mapped reason must actually be emitted.

  2. CLAIM-VERDICT LEDGER — the per-claim record including SUPPORTED verdicts
     (previously recorded NOWHERE: the citation-hover UI had to say
     "claim-level verdict not recorded"). Additive ``claim_verdicts`` on the
     report + ``data.verification.claim_verdicts`` on the critique payload,
     size-bounded with an honest ``claim_verdicts_truncated`` flag.

  3. JUDGE PROMPT PROFILE — ``current`` (default; byte-identical live prompt)
     vs ``independent`` (staged adversarial-reviewer posture, DORMANT).
"""

from __future__ import annotations

import ast
import inspect
from typing import Any
from uuid import uuid4

import legba.data.provenance.judge_input_checks as judge_input_checks
import legba.data.provenance.verify as verify
from legba.data.provenance.models import CritiquePayload
from legba.data.provenance.verify import (
    FAIL_CLASS_HARD,
    FAIL_CLASS_SOFT,
    JUDGE_PROFILE_CURRENT,
    JUDGE_PROFILE_INDEPENDENT,
    VERDICT_SUPPORTED,
    ClaimVerdict,
    UnsupportedSpan,
    _CLAIM_VERDICTS_CAP,
    _FAIL_CLASS_BY_REASON,
    _GENERIC_JUDGE_SYSTEM,
    _INDEPENDENT_JUDGE_SYSTEM,
    build_faithfulness_critique_payload,
    fail_class_for_reason,
    verify_finding_faithfulness,
)

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = None


class _StubJudge:
    """Canned strict-JSON judge that also records the system prompts it saw."""

    subprovider = "stub-judge"

    def __init__(self, verdicts_json: str) -> None:
        self._json = verdicts_json
        self.systems: list[str | None] = []
        self.calls = 0

    async def chat_complete(
        self, messages, *, max_tokens=None, temperature=None, system=None, **kw
    ):
        self.calls += 1
        self.systems.append(system)
        return _Response(self._json)


# Three fact-asserting prose claims: [1] resolves, [9] does not, third uncited.
_THREE_CLAIM_BODY = (
    "Alpha struck Bravo base on Monday [1].\n"
    "Charlie seized the port on Tuesday [9].\n"
    "Delta announced sanctions on Wednesday.\n"
)


def _citations() -> list[dict[str, Any]]:
    return [{"marker": "[1]", "signal_id": str(uuid4())}]


# ---------------------------------------------------------------------------
# 1. Fail-class mapping + drift guard
# ---------------------------------------------------------------------------


def test_fail_class_mapping_table() -> None:
    """The Primer taxonomy, pinned reason-by-reason (the ONE table)."""
    assert _FAIL_CLASS_BY_REASON == {
        "unresolved_citation": FAIL_CLASS_HARD,
        "judge_contradicted": FAIL_CLASS_HARD,
        "stale_leader": FAIL_CLASS_HARD,
        # E-1: the facts-reconciled officeholder guard — same entity-scramble
        # class, DISTINCT reason so calibration can tell it from the heuristic.
        "stale_leader_vs_facts": FAIL_CLASS_HARD,
        "cross_target_leak": FAIL_CLASS_HARD,
        "no_citation": FAIL_CLASS_SOFT,
        "judge_unsupported": FAIL_CLASS_SOFT,
        "hedge_laundering": FAIL_CLASS_SOFT,
        # C-TIER: a composed clause resting ONLY on periphery-tier (below-floor
        # / unverified) sub-claims asserted WITHOUT hedged attribution — the
        # overclaim family, counted on the deterministic floor.
        "unhedged_periphery_citation": FAIL_CLASS_SOFT,
        "double_counted": FAIL_CLASS_SOFT,
        "indicator_uncited_triggered": FAIL_CLASS_SOFT,
        # W31: a world-scoped absence claim with no collection-scoping language
        # — honesty-phrasing defect (overclaim family), not fabrication.
        "unscoped_absence_claim": FAIL_CLASS_SOFT,
        # V-B: a scoped negative contradicted by a row of the analyst's OWN
        # retained input slice — hard, and only when the violating title
        # resolves (the same earned-severity rule as V-D).
        "absence_slice_contradicted": FAIL_CLASS_HARD,
        # V-D: a judge contradiction with no resolvable verbatim evidence quote
        # — still a failure, but the hard-fail severity was never earned.
        "judge_contradicted_unquoted": FAIL_CLASS_SOFT,
        # W2: a judge contradiction whose quote RESOLVES but does not REFUTE (it
        # restates the claim, or comes from the PRIOR READ block the claim diffs
        # against) — a bad refutation rather than a missing one.
        "judge_contradicted_unrefuted": FAIL_CLASS_SOFT,
        # V-G1: the refuting quote is verbatim and real, and lives in an ANALYST
        # FINDING the claim never cited — overwhelmingly this desk's own
        # superseded prior read. An update, not a misstatement of evidence.
        "judge_prior_read_conflict": FAIL_CLASS_SOFT,
        # V-H4: the claim ENUMERATED what it denies and the refuting quote names
        # none of those things in full — real evidence, of something the claim
        # never denied ("business and mortgage defaults" against "sovereign
        # default pressures").
        "judge_contradicted_off_scope": FAIL_CLASS_SOFT,
        # V-I1: the "refuting" quote states the claim's own numbers back to it
        # under numeral/word-number normalization — it CONFIRMS the claim.
        "judge_quote_confirms_claim": FAIL_CLASS_SOFT,
        # V-I4: the refuting quote resolves ONLY inside a GDELT/CAMEO machine
        # coding — real, resolvable, and not testimony. The class the V-B route
        # has excluded 2,109 times a day since W1(c).
        "judge_contradicted_machine_row": FAIL_CLASS_SOFT,
        # V-I5: the V-B continuity router had already routed this claim out of
        # slice checking and the judge hard-failed it anyway. One claim cannot
        # have two authorities.
        "judge_contradicted_route_excluded": FAIL_CLASS_SOFT,
        # V-G5: a markerless claim resting on a historical/structural BASELINE
        # about the world — a premise whose truthmaker no cited row contains.
        "uncited_world_knowledge": FAIL_CLASS_SOFT,
        # V-C: prose misquoting the platform's OWN metadata (an
        # effective_confidence / tier the cited output's captured column
        # contradicts) — an overclaim about provenance, not a fabricated fact.
        "metadata_mismatch": FAIL_CLASS_SOFT,
        # R3: the composition led on an input materially less consequential than
        # its top one. Soft — the facts were right, the ORDER was wrong.
        "buried_lead_salience": FAIL_CLASS_SOFT,
        # R2: the input set asserted P and not-P and the composition did not name
        # the disagreement. Soft (no fabricated fact), and the costliest of them.
        "unsurfaced_input_contradiction": FAIL_CLASS_SOFT,
    }
    # Unknown reasons degrade conservatively (soft, never a fabricated hard).
    assert fail_class_for_reason("some_future_reason") == FAIL_CLASS_SOFT


def _reason_literals(node: ast.AST) -> tuple[set[str], bool]:
    """(literal reason strings under ``node``, saw_dynamic?) — resolves module
    constants (``_STALE_LEADER_REASON``) and both arms of a ternary."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}, False
    if isinstance(node, ast.Name):
        for mod in (verify, judge_input_checks):
            resolved = getattr(mod, node.id, None)
            if isinstance(resolved, str):
                return {resolved}, False
        return set(), True  # a local variable — dynamic (originates elsewhere)
    if isinstance(node, ast.IfExp):
        a, da = _reason_literals(node.body)
        b, db = _reason_literals(node.orelse)
        return a | b, da or db
    return set(), True


def test_fail_class_drift_guard() -> None:
    """EVERY reason verify.py emits (UnsupportedSpan(...) / ClaimVerdict.failed /
    _ClaimOverride(...)) is in the mapping table, and every mapped reason is
    actually emitted — a new span reason cannot land without classifying it, and
    the table cannot carry dead entries. AST-level so multiline / ternary /
    constant forms all count.

    ``_ClaimOverride`` (2026-07-31) is the THIRD reason source: the deterministic
    override seam decides a verdict and ``_apply_claim_overrides`` materializes
    the span/ledger row from it, so the literal reason lives on the override
    construction rather than on the span.

    ``_judge_reason`` (W2, 2026-08-02) is the FOURTH: the judge path's verdict →
    reason mapping was factored into ONE function so the span arm and the ledger
    arm cannot disagree (they did — the ledger collapsed both demotion classes to
    ``judge_unsupported``). Its RETURNS are reason emissions and must count, or
    factoring the duplication out would have silently blinded this guard.
    """
    # R2/R3 (2026-08-05): the guard follows the CODE. Two reasons are now emitted
    # from the judge-subsystem brick that owns the checks (``judge_input_checks``)
    # rather than from verify.py itself, and a guard that only ever parsed one
    # module would have called them DEAD — i.e. the extraction seam the tree is
    # deliberately growing would have silently disabled this test's second half.
    # Any future brick that emits a span reason joins this tuple.
    trees = [
        ast.parse(inspect.getsource(mod))
        for mod in (verify, judge_input_checks)
    ]
    emitted: set[str] = set()
    for tree in trees:
        _collect_emitted_reasons(tree, emitted)
    _assert_reason_tables_agree(emitted)


def _collect_emitted_reasons(tree: ast.Module, emitted: set[str]) -> None:
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not fn.name.endswith("_reason"):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Return) and node.value is not None:
                lits, _ = _reason_literals(node.value)
                emitted |= lits
    for node in ast.walk(tree):
        # ``reason = "judge_contradicted" if ... else "judge_unsupported"`` —
        # the ternary feeding UnsupportedSpan/ClaimVerdict via a local variable.
        if isinstance(node, ast.Assign):
            if any(
                isinstance(t, ast.Name) and t.id == "reason" for t in node.targets
            ):
                lits, _ = _reason_literals(node.value)
                emitted |= lits
            continue
        if not isinstance(node, ast.Call):
            continue
        fname = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else getattr(node.func, "attr", None)
        )
        # ANY call carrying a ``reason=`` keyword counts, not just the three
        # constructors. R2/R3 raise their spans through a shared ``_fold_soft``
        # helper — factoring the duplicated fold out is exactly the refactor this
        # guard must survive, and the same lesson W2 already taught it about
        # ``_judge_reason``. Broadening here can only ever catch MORE emissions,
        # which is the safe direction for a guard.
        if fname not in ("failed",):
            for kw in node.keywords:
                if kw.arg == "reason":
                    lits, _ = _reason_literals(kw.value)
                    emitted |= lits
        elif fname == "failed":  # ClaimVerdict.failed(text, reason, ...)
            if len(node.args) >= 2:
                lits, _ = _reason_literals(node.args[1])
                emitted |= lits
            for kw in node.keywords:
                if kw.arg == "reason":
                    lits, _ = _reason_literals(kw.value)
                    emitted |= lits

def _assert_reason_tables_agree(emitted: set[str]) -> None:
    unmapped = emitted - set(_FAIL_CLASS_BY_REASON)
    assert not unmapped, f"span reasons missing from _FAIL_CLASS_BY_REASON: {unmapped}"
    dead = set(_FAIL_CLASS_BY_REASON) - emitted
    assert not dead, f"mapped reasons never emitted by the verify modules: {dead}"
    assert set(_FAIL_CLASS_BY_REASON.values()) <= {FAIL_CLASS_HARD, FAIL_CLASS_SOFT}


def test_unsupported_span_dict_carries_fail_class() -> None:
    hard = UnsupportedSpan(text="x", reason="judge_contradicted").as_dict()
    soft = UnsupportedSpan(text="y", reason="no_citation").as_dict()
    assert hard["fail_class"] == FAIL_CLASS_HARD
    assert soft["fail_class"] == FAIL_CLASS_SOFT
    # Additive: the pre-existing keys are untouched.
    assert {"text", "reason", "markers"} <= set(soft)


# ---------------------------------------------------------------------------
# 2. Claim-verdict ledger — floor path
# ---------------------------------------------------------------------------


async def test_floor_ledger_supported_and_both_fail_classes(monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=_THREE_CLAIM_BODY, citations=_citations()
    )
    # Scores untouched: 1 of 3 supported, exactly as before this change.
    assert report.checkable_claims == 3
    assert report.supported_claims == 1
    # The ledger reconciles 1:1 with the tally on the floor path.
    assert len(report.claim_verdicts) == 3
    by_verdict = {cv.verdict: cv for cv in report.claim_verdicts}
    sup = by_verdict[VERDICT_SUPPORTED]
    assert "Alpha struck" in sup.text and sup.markers == [1] and sup.reason is None
    hard = by_verdict[FAIL_CLASS_HARD]
    assert hard.reason == "unresolved_citation" and hard.markers == [9]
    soft = by_verdict[FAIL_CLASS_SOFT]
    assert soft.reason == "no_citation" and "Delta announced" in soft.text


async def test_floor_ledger_indicators_and_guards(monkeypatch) -> None:
    """Supported structured indicators land in the ledger (previously recorded
    nowhere); guard hits land as hard_fail rows."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body="Cooperation with former President Trump resumed [1].\n",
        citations=_citations(),
        indicators=[
            {"status": "triggered", "statement": "Border crossings closed", "citations": [1]},
            {"status": "triggered", "statement": "Fuel rationing announced", "citations": []},
            {"status": "not_observed", "statement": "Mobilization", "citations": []},
        ],
    )
    ledger = report.claim_verdicts
    # prose claim + 2 triggered indicators + 1 stale-leader guard hit.
    assert len(ledger) == 4
    ind_sup = [
        cv for cv in ledger
        if cv.verdict == VERDICT_SUPPORTED and "Border crossings" in cv.text
    ]
    assert ind_sup and ind_sup[0].markers == [1]
    ind_fail = [cv for cv in ledger if cv.reason == "indicator_uncited_triggered"]
    assert ind_fail and ind_fail[0].verdict == FAIL_CLASS_SOFT
    guard = [cv for cv in ledger if cv.reason == "stale_leader"]
    assert guard and guard[0].verdict == FAIL_CLASS_HARD
    # Tally coherence on the floor path: ledger rows == checkable claims.
    assert len(ledger) == report.checkable_claims
    assert (
        sum(1 for cv in ledger if cv.verdict == VERDICT_SUPPORTED)
        == report.supported_claims
    )


# ---------------------------------------------------------------------------
# 2. Claim-verdict ledger — judge path
# ---------------------------------------------------------------------------


async def test_judge_ledger_all_three_verdicts(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    # V-D: the contradicted claim carries a VERBATIM quote of the cited evidence,
    # so the hard class is EARNED (an unquotable contradiction demotes to the soft
    # judge_contradicted_unquoted — see test_verify_hardfail_quote.py).
    cites = [
        {
            "marker": "[1]",
            "signal_id": str(uuid4()),
            "title": "Delta held the port and announced no sanctions",
        }
    ]
    judge = _StubJudge(
        '{"verdicts": ["supported", "contradicted", "unsupported"], '
        '"quotes": ["", "Delta held the port and announced no sanctions", ""]}'
    )
    report = await verify_finding_faithfulness(
        body=_THREE_CLAIM_BODY, citations=cites, judge_llm=judge
    )
    assert report.judge_status == "llm"
    ledger = report.claim_verdicts
    assert len(ledger) == 3
    assert ledger[0].verdict == VERDICT_SUPPORTED and ledger[0].markers == [1]
    assert ledger[1].verdict == FAIL_CLASS_HARD
    assert ledger[1].reason == "judge_contradicted"
    assert ledger[2].verdict == FAIL_CLASS_SOFT
    assert ledger[2].reason == "judge_unsupported"
    # Judge-path coherence: supported ledger rows == the judge-supported tally.
    assert (
        sum(1 for cv in ledger if cv.verdict == VERDICT_SUPPORTED)
        == report.supported_claims
        == 1
    )


# ---------------------------------------------------------------------------
# 2. Persistence — the critique payload
# ---------------------------------------------------------------------------


async def test_payload_persists_ledger_and_judge_ref(monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=_THREE_CLAIM_BODY, citations=_citations()
    )
    payload = build_faithfulness_critique_payload(
        report,
        analyzed_output_id=uuid4(),
        judge_model="deterministic-floor",
        judge_llm_ref="llm.primary.openai_compat",
    )
    verification = payload["data"]["verification"]
    # The judge-route provenance stamp, top-level AND in the verification block.
    assert payload["judge_llm_ref"] == "llm.primary.openai_compat"
    assert verification["judge_llm_ref"] == "llm.primary.openai_compat"
    # The full per-claim ledger, including the SUPPORTED verdict.
    rows = verification["claim_verdicts"]
    assert len(rows) == 3
    assert {r["verdict"] for r in rows} == {
        VERDICT_SUPPORTED, FAIL_CLASS_HARD, FAIL_CLASS_SOFT,
    }
    supported_rows = [r for r in rows if r["verdict"] == VERDICT_SUPPORTED]
    assert supported_rows[0]["markers"] == [1]
    assert supported_rows[0]["reason"] is None
    assert verification["claim_verdicts_truncated"] is False
    # Every persisted span carries its fail_class.
    assert all("fail_class" in s for s in verification["unsupported_spans"])
    # The payload still validates against CritiquePayload (extra='forbid'):
    # judge_llm_ref is a real schema field, not a silently-dropped extra.
    validated = CritiquePayload.model_validate(payload)
    assert validated.judge_llm_ref == "llm.primary.openai_compat"


async def test_payload_persists_judge_route_class(monkeypatch) -> None:
    """W-3d: the judge-route CLASS (configured|fallback_verify|fallback_primary)
    is stamped top-level (a real CritiquePayload field) AND into the
    data.verification block the findings API projects wholesale — so the UI
    badge can tell a configured judge from a ladder fallback."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=_THREE_CLAIM_BODY, citations=_citations()
    )
    for route in ("configured", "fallback_verify", "fallback_primary"):
        payload = build_faithfulness_critique_payload(
            report,
            analyzed_output_id=uuid4(),
            judge_llm_ref="llm.primary.openai_compat",
            judge_route=route,
        )
        assert payload["judge_route"] == route
        assert payload["data"]["verification"]["judge_route"] == route
        validated = CritiquePayload.model_validate(payload)
        assert validated.judge_route == route
    # Floor-only / pre-W-3d rows: empty top-level, honest None in the block.
    bare = build_faithfulness_critique_payload(report, analyzed_output_id=uuid4())
    assert bare["judge_route"] == ""
    assert bare["data"]["verification"]["judge_route"] is None
    assert CritiquePayload.model_validate(bare).judge_route == ""


async def test_payload_ledger_truncation_flag(monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    n = _CLAIM_VERDICTS_CAP + 15
    body = "\n".join(
        f"Unit {i} shelled sector {i} on day {i} [1]." for i in range(n)
    )
    report = await verify_finding_faithfulness(body=body, citations=_citations())
    assert len(report.claim_verdicts) == n
    payload = build_faithfulness_critique_payload(
        report, analyzed_output_id=uuid4()
    )
    verification = payload["data"]["verification"]
    assert len(verification["claim_verdicts"]) == _CLAIM_VERDICTS_CAP
    assert verification["claim_verdicts_truncated"] is True
    # Honest cap on the report dict too.
    d = report.as_dict()
    assert len(d["claim_verdicts"]) == _CLAIM_VERDICTS_CAP
    assert d["claim_verdicts_truncated"] is True


def test_ledger_text_is_bounded() -> None:
    row = ClaimVerdict.supported("x" * 10_000, [1]).as_dict()
    assert len(row["text"]) == verify._CLAIM_VERDICT_TEXT_CHARS


async def test_long_claim_survives_ledger_roundtrip_untruncated() -> None:
    """A >300-char claim reaches the persisted ledger WHOLE (2026-08-02).

    The cap was 300, which cut real adjudication claims mid-sentence: an
    Americas row lost the word "Argentina" past char 300, so the ledger row
    named a different dispute than the one the judge graded. This pins the
    REAL round-trip — verify pass → critique payload → validated model — for a
    claim in that length band, and asserts the distinguishing tail token (the
    part the old cap ate) is still there.
    """
    # One claim, comfortably past the old 300 cap, with the disambiguating
    # party named only in the tail — the exact shape that mislabelled live.
    filler = "and the tribunal recorded the submissions of the parties, "
    claim = (
        f"The boundary commission reviewed the {filler * 8}"
        "which the delegation of Argentina formally contested [1]."
    )
    assert len(claim) > verify._CLAIM_VERDICT_TEXT_CHARS * 0.25
    assert len(claim) > 300

    report = await verify_finding_faithfulness(
        body=claim, citations=_citations()
    )
    assert report.claim_verdicts, "the claim must be graded at all"

    verification = build_faithfulness_critique_payload(
        report, analyzed_output_id=uuid4()
    )["data"]["verification"]
    rows = verification["claim_verdicts"]
    assert rows, "the ledger must carry the graded claim"
    assert verification["claim_verdicts_truncated"] is False

    persisted = rows[0]["text"]
    # Whole claim, not a 300-char stub — and the tail token survives.
    assert len(persisted) == len(claim)
    assert "Argentina" in persisted
    assert persisted == claim


# ---------------------------------------------------------------------------
# 3. Judge prompt profile — staged, default current, zero live change
# ---------------------------------------------------------------------------


async def test_profile_default_current_sends_live_prompt(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    monkeypatch.delenv("LEGBA_JUDGE_PROMPT_PROFILE", raising=False)
    judge = _StubJudge('{"verdicts": ["supported", "supported", "supported"]}')
    await verify_finding_faithfulness(
        body=_THREE_CLAIM_BODY, citations=_citations(), judge_llm=judge
    )
    assert judge.systems == [_GENERIC_JUDGE_SYSTEM]


async def test_profile_default_output_identical_to_explicit_current(
    monkeypatch,
) -> None:
    """Zero live behavior change: profile unset ≡ profile 'current', report
    byte-identical."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    monkeypatch.delenv("LEGBA_JUDGE_PROMPT_PROFILE", raising=False)
    canned = '{"verdicts": ["supported", "contradicted", "unsupported"]}'
    j1, j2 = _StubJudge(canned), _StubJudge(canned)
    r_default = await verify_finding_faithfulness(
        body=_THREE_CLAIM_BODY, citations=_citations(), judge_llm=j1
    )
    r_current = await verify_finding_faithfulness(
        body=_THREE_CLAIM_BODY,
        citations=_citations(),
        judge_llm=j2,
        judge_prompt_profile=JUDGE_PROFILE_CURRENT,
    )
    assert j1.systems == j2.systems == [_GENERIC_JUDGE_SYSTEM]
    d1, d2 = r_default.as_dict(), r_current.as_dict()
    assert d1 == d2


async def test_profile_independent_sends_staged_prompt(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge('{"verdicts": ["supported", "supported", "supported"]}')
    report = await verify_finding_faithfulness(
        body=_THREE_CLAIM_BODY,
        citations=_citations(),
        judge_llm=judge,
        judge_prompt_profile=JUDGE_PROFILE_INDEPENDENT,
    )
    assert judge.systems == [_INDEPENDENT_JUDGE_SYSTEM]
    # Never "check your own work": the posture is an independent reviewer of
    # ANOTHER analyst's claims, grounded only in the shown evidence.
    assert "ANOTHER analyst" in _INDEPENDENT_JUDGE_SYSTEM
    assert "INDEPENDENT" in _INDEPENDENT_JUDGE_SYSTEM
    # Same verdict rubric labels — the A/B isolates posture, not the rubric.
    for token in ("SUPPORTED", "UNSUPPORTED", "CONTRADICTED"):
        assert token in _INDEPENDENT_JUDGE_SYSTEM
    assert report.judge_status == "llm"


async def test_profile_env_selects_independent(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    monkeypatch.setenv("LEGBA_JUDGE_PROMPT_PROFILE", "independent")
    judge = _StubJudge('{"verdicts": ["supported", "supported", "supported"]}')
    await verify_finding_faithfulness(
        body=_THREE_CLAIM_BODY, citations=_citations(), judge_llm=judge
    )
    assert judge.systems == [_INDEPENDENT_JUDGE_SYSTEM]


def test_unknown_profile_degrades_to_current() -> None:
    assert verify._judge_prompt_profile("adversarial-typo") == JUDGE_PROFILE_CURRENT
    assert verify._generic_judge_system("adversarial-typo") == _GENERIC_JUDGE_SYSTEM
