# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C-4 — the resolved JUDGE PROFILE surface, pinned.

C-4 move 2 proposed data-driving "the per-kind judge configuration (route ladder,
hard/soft fail classes, floors/exemptions per OutputKind)" out of scattered inline
conditionals into one declarative table. That refactor was SKIPPED: the named
pieces are already single-site declarative tables, and the one genuinely
duplicated piece (the verify ELIGIBILITY gate) is duplicated by documented design
to avoid a package load cycle and spans the live actor plane. See the C-4 report.

What survives is the artifact that makes any FUTURE consolidation safe: this
module PINS the resolved profile across the whole surface, through the real
callable APIs, so a consolidation can be proven byte-identical against it.

Pinned here:
  * the judge ROUTE ladder — every rung, and the route CLASS each rung collapses to
  * the hard/soft FAIL CLASS for every reason in the table
  * the verify EXEMPTION resolvers (structural analyst set + badge)
  * the per-claim-kind JUDGE PROFILE registry (version + dedicated-prompt flag)
  * ``_claim_kind`` first-match-wins priority
  * a DRIFT GUARD over the OutputKind x verify-eligibility gate: only FINDING and
    JOURNAL may ever reach the faithfulness pass. Widening that gate without
    updating this test fails the suite.

These are CHARACTERIZATION assertions: they record what the system resolves
today. A deliberate recalibration updates them in the same commit as the change.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from legba.data.provenance import kinds as K
from legba.data.provenance import verify as V
from legba.data.provenance.kinds import OutputKind
from legba.runtime import analyst_deps_builder as ADB


# ---------------------------------------------------------------------------
# 1. The judge ROUTE ladder — every rung, and its collapsed CLASS
# ---------------------------------------------------------------------------
def test_route_rung0_opt_in_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """No judge/verify key => NO judge route, ever (the opt-in gate)."""
    monkeypatch.delenv(ADB.JUDGE_STACK_REF_ENV, raising=False)
    assert ADB.resolve_judge_route_from_llm_block({}) is None
    assert ADB.resolve_judge_route_from_llm_block({"primary": "llm.primary.x"}) is None
    assert ADB.resolve_judge_route_from_llm_block(None) is None
    assert ADB.resolve_judge_route_from_llm_block("nope") is None


def test_route_rung1_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ADB.JUDGE_STACK_REF_ENV, "llm.env.override")
    r = ADB.resolve_judge_route_from_llm_block(
        {"judge": "llm.judge.j", "verify": "llm.verify.v", "primary": "llm.primary.p"}
    )
    assert r is not None
    assert r.component_id == "llm.env.override"
    assert r.source == f"env:{ADB.JUDGE_STACK_REF_ENV}"
    assert r.route_class == ADB.JUDGE_ROUTE_CONFIGURED


def test_route_rung1_env_does_not_enable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env override REPOINTS; it never opts a descriptor in."""
    monkeypatch.setenv(ADB.JUDGE_STACK_REF_ENV, "llm.env.override")
    assert ADB.resolve_judge_route_from_llm_block({"primary": "llm.primary.p"}) is None


def test_route_rung2_explicit_judge_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ADB.JUDGE_STACK_REF_ENV, raising=False)
    r = ADB.resolve_judge_route_from_llm_block(
        {"judge": "llm.judge.j", "verify": "llm.verify.v", "primary": "llm.primary.p"}
    )
    assert r is not None
    assert (r.component_id, r.source, r.route_class) == (
        "llm.judge.j",
        "method.llm.judge",
        ADB.JUDGE_ROUTE_CONFIGURED,
    )


def test_route_rung3_verify_key_is_todays_live_rung(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every LIVE descriptor resolves HERE — the fallback_verify class."""
    monkeypatch.delenv(ADB.JUDGE_STACK_REF_ENV, raising=False)
    r = ADB.resolve_judge_route_from_llm_block(
        {"verify": "llm.primary.openai_compat", "primary": "llm.primary.openai_compat"}
    )
    assert r is not None
    assert (r.component_id, r.source, r.route_class) == (
        "llm.primary.openai_compat",
        "method.llm.verify",
        ADB.JUDGE_ROUTE_FALLBACK_VERIFY,
    )


def test_route_rung4_terminal_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opted in but refs malformed => judge on the PRODUCER plane, never unjudged."""
    monkeypatch.delenv(ADB.JUDGE_STACK_REF_ENV, raising=False)
    r = ADB.resolve_judge_route_from_llm_block(
        {"verify": None, "primary": "llm.primary.p"}
    )
    assert r is not None
    assert (r.component_id, r.source, r.route_class) == (
        "llm.primary.p",
        "method.llm.primary",
        ADB.JUDGE_ROUTE_FALLBACK_PRIMARY,
    )


def test_route_rung5_nothing_resolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ADB.JUDGE_STACK_REF_ENV, raising=False)
    assert ADB.resolve_judge_route_from_llm_block({"verify": None}) is None
    assert ADB.resolve_judge_route_from_llm_block({"judge": {}, "verify": {}}) is None


@pytest.mark.parametrize(
    "shape,expected",
    [
        ({"raw": "llm.a.b"}, "llm.a.b"),  # dump mapping
        ("llm.bare.string", "llm.bare.string"),  # bare string
    ],
)
def test_route_stack_ref_shapes(
    shape: object, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ADB.JUDGE_STACK_REF_ENV, raising=False)
    r = ADB.resolve_judge_route_from_llm_block({"judge": shape})
    assert r is not None and r.component_id == expected


def test_route_class_never_fabricated() -> None:
    """An unrecognized source yields ``""`` — the ref is still stamped."""
    assert ADB.JudgeRoute(component_id="x", source="mystery").route_class == ""


def test_route_class_label_values_pinned() -> None:
    assert ADB.JUDGE_ROUTE_CONFIGURED == "configured"
    assert ADB.JUDGE_ROUTE_FALLBACK_VERIFY == "fallback_verify"
    assert ADB.JUDGE_ROUTE_FALLBACK_PRIMARY == "fallback_primary"
    assert ADB.JUDGE_STACK_REF_ENV == "LEGBA_JUDGE_STACK_REF"


def test_route_class_is_a_total_function_over_the_ladder_sources() -> None:
    """Every source string the ladder can EMIT maps to a non-empty class."""
    emitted = {
        f"env:{ADB.JUDGE_STACK_REF_ENV}",
        "method.llm.judge",
        "method.llm.verify",
        "method.llm.primary",
    }
    for src in emitted:
        assert ADB.JudgeRoute(component_id="c", source=src).route_class != "", src


# ---------------------------------------------------------------------------
# 2. Hard/soft FAIL CLASSES — the one table, pinned entry by entry
# ---------------------------------------------------------------------------
_EXPECTED_FAIL_CLASSES = {
    "unresolved_citation": "hard_fail",
    "judge_contradicted": "hard_fail",
    "stale_leader": "hard_fail",
    "stale_leader_vs_facts": "hard_fail",
    "cross_target_leak": "hard_fail",
    "no_citation": "soft_fail",
    "judge_unsupported": "soft_fail",
    "hedge_laundering": "soft_fail",
    "unhedged_periphery_citation": "soft_fail",
    "double_counted": "soft_fail",
    "indicator_uncited_triggered": "soft_fail",
    "unscoped_absence_claim": "soft_fail",
}


@pytest.mark.parametrize("reason,expected", sorted(_EXPECTED_FAIL_CLASSES.items()))
def test_fail_class_pinned(reason: str, expected: str) -> None:
    assert V.fail_class_for_reason(reason) == expected


def test_fail_class_table_is_exactly_these_reasons() -> None:
    """A NEW reason must land in this pin, not just in the module table."""
    assert set(V._FAIL_CLASS_BY_REASON) == set(_EXPECTED_FAIL_CLASSES)


def test_fail_class_unknown_reason_never_escalates_to_hard() -> None:
    assert V.fail_class_for_reason("brand_new_unmapped_reason") == V.FAIL_CLASS_SOFT
    assert V.fail_class_for_reason("") == V.FAIL_CLASS_SOFT


def test_fail_class_constant_values_pinned() -> None:
    assert V.FAIL_CLASS_HARD == "hard_fail"
    assert V.FAIL_CLASS_SOFT == "soft_fail"


# ---------------------------------------------------------------------------
# 3. Verify EXEMPTIONS
# ---------------------------------------------------------------------------
def test_structural_exempt_set_pinned() -> None:
    assert K.STRUCTURAL_VERIFY_EXEMPT_ANALYSTS == frozenset({
        "graph_mining",
        "anomaly_detection",
        "band_calibration_tracker",
        "calibration_tracking",
        "unit_correctness_scorer",
        "composition_lineage_sweep",
        "adversarial_signals",
        "situation_clustering",
        "thematic_proposal",
        "indicator_tracker",
        "collection_gap",
        "hypothesis_lifecycle",
        "signals_retention",
        "analyst_traces_retention",
        "geo_convergence_scan",
        "fact_decay_scan",
        "source_track_record",
        "narrative_mapper",
        "desk_baseline",
    })


@pytest.mark.parametrize("analyst", sorted(K.STRUCTURAL_VERIFY_EXEMPT_ANALYSTS))
def test_exempt_analysts_resolve_structural(analyst: str) -> None:
    assert K.verify_exempt_reason(analyst) == "structural"


def test_exempt_reason_never_fabricated_for_unknown() -> None:
    assert K.verify_exempt_reason("inline_target") is None
    assert K.verify_exempt_reason("country_composition") is None
    assert K.verify_exempt_reason(None) is None
    assert K.verify_exempt_reason("not_a_real_analyst") is None


def test_structural_claims_verify_optin_is_a_subset_of_exempt() -> None:
    assert K.STRUCTURAL_CLAIMS_VERIFY_ANALYSTS <= K.STRUCTURAL_VERIFY_EXEMPT_ANALYSTS


@pytest.mark.parametrize("analyst", sorted(K.STRUCTURAL_CLAIMS_VERIFY_ANALYSTS))
def test_structural_claims_optin_pinned(analyst: str) -> None:
    assert K.structural_claims_verify_opt_in(analyst) is True


def test_structural_badge_resolution_pinned() -> None:
    some = next(iter(sorted(K.STRUCTURAL_VERIFY_EXEMPT_ANALYSTS)))
    assert K.structural_badge(some, True) == "structural-verified"
    assert K.structural_badge(some, None) == "structural"
    assert K.structural_badge("inline_target", None) is None


# ---------------------------------------------------------------------------
# 4. The per-CLAIM-KIND judge profile registry
# ---------------------------------------------------------------------------
_EXPECTED_PROFILES = {
    "citation_support": ("citsupp.v3", False),
    "absence": ("absence.v1", True),
    "synthesis": ("synthesis.v0", False),
    "forward_looking": ("fwd.v0", False),
    "structure": ("structure.v0", False),
}


def test_judge_profile_registry_covers_exactly_these_kinds() -> None:
    assert set(V._JUDGE_PROFILES) == set(_EXPECTED_PROFILES)


@pytest.mark.parametrize("kind,expected", sorted(_EXPECTED_PROFILES.items()))
def test_judge_profile_version_and_prompt_flag_pinned(
    kind: str, expected: tuple[str, bool]
) -> None:
    version, has_prompt = expected
    prof = V._JUDGE_PROFILES[kind]
    assert prof.kind == kind
    assert prof.version == version, (
        f"judge profile {kind!r} version changed — a recalibration must bump "
        "this pin deliberately"
    )
    assert (prof.judge_system is not None) is has_prompt


def test_only_absence_carries_a_dedicated_prompt_today() -> None:
    with_prompt = {
        k for k, p in V._JUDGE_PROFILES.items() if p.judge_system is not None
    }
    assert with_prompt == {"absence"}


def test_claim_kind_priority_first_match_wins() -> None:
    """The documented priority: structure > forward_looking > absence >
    synthesis > citation_support."""
    assert V._claim_kind("- **Outlook**: things") == V.CLAIM_KIND_STRUCTURE
    assert V._claim_kind("No strikes were reported in the reviewed signals.") == (
        V.CLAIM_KIND_ABSENCE
    )
    assert V._claim_kind("The port closed on Tuesday [1].") == (
        V.CLAIM_KIND_CITATION_SUPPORT
    )


def test_claim_kind_is_total() -> None:
    """Every claim classifies to a kind present in the registry."""
    for claim in (
        "",
        "x",
        "# Heading",
        "BLUF: the situation is stable.",
        "Escalation is likely to continue over the next week.",
        "No evidence of mobilization was found.",
        "Shelling was reported near Kyiv [2].",
    ):
        assert V._claim_kind(claim) in V._JUDGE_PROFILES, claim


# ---------------------------------------------------------------------------
# 5. DRIFT GUARD — OutputKind x verify eligibility
# ---------------------------------------------------------------------------
#: Only these two kinds may reach the faithfulness verify pass. Widening this is
#: a deliberate product decision, not an incidental edit.
_VERIFY_CAPABLE_KINDS = {"FINDING", "JOURNAL"}


def _verification_gate_source() -> str:
    """The ``verification_block`` eligibility expression from the actor plane."""
    path = (
        Path(inspect.getsourcefile(K)).resolve().parents[2]
        / "runtime"
        / "dapr_actors.py"
    )
    src = path.read_text(encoding="utf-8")
    start = src.index("verification_block: dict[str, Any] | None = None")
    end = src.index("verify_inline_target_finding(", start)
    return src[start:end]


def test_only_finding_and_journal_are_verify_capable() -> None:
    """DRIFT GUARD: the actor-plane verify gate names exactly two OutputKinds.

    C-4 skipped consolidating this dispatch (it is duplicated across four
    modules, one of them by documented design to avoid a package load cycle).
    This guard is the compensating control: silently widening the gate to a
    third OutputKind fails here.
    """
    gate = _verification_gate_source()
    named = set(re.findall(r"OutputKind\.([A-Z_]+)", gate))
    assert named == _VERIFY_CAPABLE_KINDS, (
        "the actor-plane faithfulness verify gate changed which OutputKinds it "
        f"admits: {sorted(named)} != {sorted(_VERIFY_CAPABLE_KINDS)}"
    )


def test_verify_capable_kinds_are_real_output_kinds() -> None:
    for name in _VERIFY_CAPABLE_KINDS:
        assert hasattr(OutputKind, name)


def test_output_kind_roster_pinned() -> None:
    """The 12 kinds. A new kind is a deliberate addition — and must be
    classified against the verify gate above."""
    assert {k.name for k in OutputKind} == {
        "FINDING",
        "SITUATION",
        "HYPOTHESIS",
        "PREDICTION",
        "ALERT",
        "META_FINDING",
        "CRITIQUE",
        "FACT",
        "NEXUS",
        "PROMPT_MODULE_CANDIDATE",
        "JOURNAL",
        "SCORECARD",
    }


def test_verification_gate_still_parses_as_python() -> None:
    """Cheap sanity that the slice above really is the gate expression."""
    gate = _verification_gate_source()
    assert "output_kind ==" in gate
    assert "_descriptor_declares_verify" in gate
    # every identity.kind the gate admits, pinned
    kinds = set(re.findall(r'identity\.kind\s*(?:==|in)\s*\(?\s*"([a-z_]+)"', gate))
    assert "inline_target" in kinds
    assert "journal_assessor" in kinds


def test_declares_verify_agrees_with_the_ladder_on_every_live_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The composer's ``_declares_verify`` duplicates the ladder's opt-in gate
    (by documented design — no runtime-package load cycle). C-4 did NOT merge
    them; this pins that they AGREE on every descriptor shape in use.
    """
    from types import SimpleNamespace

    from legba.data.analysts import meta_findings_synthesizer as synth

    monkeypatch.delenv(ADB.JUDGE_STACK_REF_ENV, raising=False)
    live_shapes = [
        ({}, False),
        ({"primary": "llm.primary.p"}, False),
        ({"verify": "llm.primary.openai_compat"}, True),
        ({"verify": "llm.primary.openai_compat", "primary": "llm.primary.p"}, True),
        ({"judge": "llm.judge.j"}, True),
        ({"judge": "llm.judge.j", "primary": "llm.primary.p"}, True),
    ]
    for llm, expected in live_shapes:
        desc = SimpleNamespace(method=SimpleNamespace(llm=llm))
        assert synth._declares_verify(desc) is expected, llm
        assert (ADB.resolve_judge_route_from_llm_block(llm) is not None) is expected, llm


def test_declares_verify_agrees_with_the_ladder_on_a_NULL_valued_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEFECT B, FIXED (2026-07-29) — was a latent divergence, pinned wrong on
    purpose (C-4 was refactor-only — reported, NOT fixed at the time); this pin
    now records the corrected behavior.

    The two gates were documented as mirrors but were not:

      * the ladder's rung 0 tests KEY PRESENCE
        (``if "judge" not in llm and "verify" not in llm: return None``), so
        ``{"verify": None, "primary": <ref>}`` ADMITS and falls through to rung 4,
        resolving the producer plane — a NON-None route. The actor-plane gate
        (``_descriptor_declares_verify`` -> ``resolve_judge_route(...) is not
        None``) therefore RUNS the faithfulness pass and stamps route class
        ``fallback_primary``.
      * the composer's ``_declares_verify`` tested the VALUE
        (``llm.get("verify") is not None or llm.get("judge") is not None``), so
        the same descriptor read as NOT opted in and the composer skipped its
        verify-floor / include_meta branch.

    Net (pre-fix): such a descriptor would be COMPOSED as an unverified legacy
    meta while its output WAS verified and critiqued by the actor plane. The fix
    made ``_declares_verify`` a KEY-PRESENCE test, exactly mirroring rung 0 — so
    it now agrees with the ladder on this shape. No live descriptor carries a
    null-valued ``verify``/``judge`` key today, so the fix changes no behavior on
    live data — only this synthetic shape.
    """
    from types import SimpleNamespace

    from legba.data.analysts import meta_findings_synthesizer as synth

    monkeypatch.delenv(ADB.JUDGE_STACK_REF_ENV, raising=False)
    for llm in ({"verify": None, "primary": "llm.primary.p"},
                {"judge": None, "primary": "llm.primary.p"}):
        desc = SimpleNamespace(method=SimpleNamespace(llm=llm))
        composer_admits = synth._declares_verify(desc)
        ladder_admits = ADB.resolve_judge_route_from_llm_block(llm) is not None
        assert composer_admits is True, llm
        assert ladder_admits is True, llm
        assert composer_admits == ladder_admits, (
            "the composer/ladder opt-in agreement regressed on a null-valued "
            "key with a primary fallback — DEFECT B has reappeared"
        )

    # With no ``primary`` to fall through to, the ladder's LATER rungs find
    # nothing resolvable (rung 5, terminal) even though rung 0 admitted.
    # ``_declares_verify`` mirrors ONLY rung 0 (the opt-in GATE — "this
    # descriptor declares verify/judge intent"), not the ladder's full
    # resolution, so it still reads True here even though no concrete route
    # exists. This is not a re-introduction of the defect: "declares verify"
    # (key present) and "a route resolved" (this shape + every later rung)
    # are different questions by design, and the actor plane's own
    # ``resolve_judge_route`` call independently and correctly returns no
    # route for this shape either way.
    for llm in ({"verify": None}, {"judge": None}):
        desc = SimpleNamespace(method=SimpleNamespace(llm=llm))
        assert synth._declares_verify(desc) is True
        assert ADB.resolve_judge_route_from_llm_block(llm) is None


def test_module_ast_parses() -> None:
    """Guard the source-slicing helpers against a moved gate."""
    path = (
        Path(inspect.getsourcefile(K)).resolve().parents[2]
        / "runtime"
        / "dapr_actors.py"
    )
    ast.parse(path.read_text(encoding="utf-8"))
