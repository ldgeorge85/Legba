# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-17 — register the source-first analyst set.

Registers the fresh analyst working set, all granting capability via
``action_packs`` (the retired ``tools_whitelist`` surface is gone):

  * country_assessor   (inline_target) — the G20 situational-awareness analyst,
    scoped by `has_tag("g20")` to every country target.  ONE correctly-scoped
    analyst, NOT a brazil-scoped one fanning out across countries.
    Grants: media_processing + incident_response.
  * country_critic     (critic)        — grades country_assessor's findings.
    Grants: incident_response.  (Replaces the old india_energy_critic whose
    tools_whitelist=[mnemosyne_trust_query] no longer validates.)
  * country_optimizer  (optimizer)     — DSPy/GEPA compile over the assessor's
    trace+critique join window.  Grants: none (operates over prompt modules).
  * consult_default    (consult_on_demand) — on-demand consult.
    Grants: discovery.  (Replaces the legacy legba_consult_default whose
    tools_whitelist no longer validates.)

This re-register is ALSO the fix for the live duplicate-findings issue: we do
NOT register the stray ``india_energy_inline_critic_test`` and we register a
single correctly-scoped assessor instead of the brazil-analyst-on-all-countries.

Direct-DB registration via DescriptorRegistry against the migrated Postgres
(default ``legba_pivot_test``).  Idempotent.  Each descriptor is validated
against the real pydantic AnalystDescriptor schema before it touches the DB.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _p17_registrar import (  # noqa: E402
    Family,
    RegisterResult,
    close_registry,
    open_registry,
    print_results,
    register_descriptor,
)

from legba.data.schemas.analyst import AnalystDescriptor  # noqa: E402

DESCRIPTORS_DIR = pathlib.Path(__file__).resolve().parent.parent / "descriptors"

ANALYST_FILES = [
    # OPERATOR DECISION 2026-07-01: country_assessor (the monolithic per-country
    # one-pager) is RETIRED, not just demoted-to-feeder. P2-T8/SEAMS #35 kept it
    # running as a feeder, but the system (4 bounded units -> country_composition
    # -> scorecard/world_assessor) is the trusted product and NOTHING in the tower
    # reads it — so feeding ~150 untrusted findings/48h into the substrate is exactly
    # the verdict-from-nowhere pollution the tower exists to kill. Like country_predictor
    # (SEAMS #31), nulling its cadence is INSUFFICIENT (it is REACTIVE via
    # subscription.targets has_tag g20), so the live head is `retired` (POST /retire)
    # AND it is removed from bringup so a fresh deploy cannot re-create it. To restore
    # the feeder: un-retire + re-add this line + re-register.
    # "analyst_country_assessor.yaml",
    "analyst_world_assessor.yaml",
    "analyst_country_critic.yaml",
    "analyst_country_optimizer.yaml",
    "analyst_consult_default.yaml",
    # P3-T8 FREEZE COMPLETION: country_predictor (forecast-as-claim) is NOT
    # registered here. Nulling its cadence (P0-T6 / SEAMS #31) did NOT freeze it —
    # it has subscription.targets (g20 signals) + state:active, so it fired
    # REACTIVELY (~1/hr live `prediction` rows). Removed from bringup + retired live
    # so it stays at 0 until forecasting RETURNS at P4-T7 as a precise-question
    # scoreboard (a different design, not this reactive predictor). To restore the
    # OLD leg: re-add this line + re-register.
    # "analyst_country_predictor.yaml",
    "analyst_meta_synthesizer.yaml",          # NEW — Piece 3 (Task B)
    "analyst_cross_correlator.yaml",          # NEW — Piece 3 (Task C, sibling meta producer)
    # PIECE C SUPERSEDED Piece 3 Task D: the situation-gated hypothesis_lifecycle
    # emitted 0 rows (gated on `active` situations that go dormant — see
    # DATA_ANALYSIS_DEEP_REVIEW_2026-06-16.md §1.2). The competing_hypotheses ACH
    # kind below is now the REAL producer (reads facts/findings/nexuses directly,
    # NOT situation-gated). hypothesis_lifecycle is RETIRED from bringup (its
    # file + tests + deterministic-dispatch entry are kept so it can serve as a
    # lifecycle-maintenance feeder if re-enabled) — NOT registered here to avoid
    # a duplicate forward-claim producer over the same situations.
    "analyst_competing_hypotheses.yaml",      # NEW — PIECE C (the ACH hypotheses producer)
    "analyst_calibration_tracking.yaml",      # NEW — PIECE C (Brier-calibration feedback loop)
    "analyst_fact_decay.yaml",                # NEW — facts-table temporal-lifecycle maintenance (audit fix)
    "analyst_fact_contention_arbiter.yaml",   # NEW — Holes-B Wave 2 (#101) — contested-claims arbiter (DETECT-ONLY)
    "analyst_deep_consult.yaml",              # NEW — Piece 4 (deep-consult workflow bridge)
    "analyst_relationship_reifier.yaml",      # NEW — PIECE A (the reified typed-Nexus producer)
    "analyst_structural_balance.yaml",        # NEW — PIECE A consumer (signed-triad balance over nexuses)
    "analyst_graph_mining.yaml",              # NEW — PIECE A consumer (proxy-chain sign products over nexuses)
    "analyst_nexus_decay.yaml",               # NEW — PIECE A (nexuses-table temporal-lifecycle maintenance)
    "analyst_proposed_edge_governance.yaml",  # NEW — Phase D (promote/reject the proposed_edges queue; P3-1)
    "analyst_thematic_proposal.yaml",        # NEW — Phase 5b (propose thematic frames for uncovered hot situations)
    "analyst_indicator_tracker.yaml",        # NEW — S3-T2 (deterministic META: diffs the structured I&W data.indicators run-over-run per unit-stream; emits a summary finding on status FLIPS, esp. not_observed->triggered; trace-only on a no-flip/unchanged sweep)
    "analyst_journal_assessor.yaml",         # NEW — Journal Assessor Wave 0 (the 11th OutputKind producer; entry tier)
    "analyst_journal_consolidator.yaml",     # NEW — Journal Wave 2 (consolidation tier: SAME kind, distinct id, daily beat)
    "analyst_entity_gc.yaml",                # NEW — health remediation D2 (deterministic GC of orphan entities/proposed_edges; drains the integrity_sweep-flagged backlog)
    "analyst_unit_correctness_scorer.yaml",  # NEW — P2-T5 (deterministic meta scorer: each bounded unit's correctness_vs_reference vs the operator gold labels; null when a unit has 0 labels)
    # P2-T2 — the 4 bounded-reasoning UNITS (T1 unit-factory pattern). Each is a
    # topic-scoped inline_target descriptor carrying its OWN method.system_prompt
    # + scope predicate + eval.rubric; NO new Python kind, so NO _NEW_ANALYST_KINDS
    # entry is needed (identity.kind: inline_target is a built-in).
    "analyst_leadership_transition.yaml",    # NEW — P2-T2 unit (leadership-transition risk)
    "analyst_energy_security.yaml",          # NEW — P2-T2 unit (energy-security pressure)
    "analyst_escalation.yaml",               # NEW — P2-T2 unit (escalation risk)
    "analyst_narrative_coordination.yaml",   # NEW — P2-T2 unit (narrative / coordination signals)
    # S1-T4/T5 — two MORE bounded units (same T1 unit-factory pattern). BROAD
    # (blanket g20+watch predicate → every desk), so both are wired as FIXED
    # scorecard dimensions + into country_composition.other_analysts.
    "analyst_internal_stability.yaml",       # NEW — S1-T4 unit (internal-stability / coup-risk)
    "analyst_military_posture.yaml",         # NEW — S1-T5 unit (military-posture shift)
    # P3-T1/T2 — per-country COMPOSITION over the 4 verified units. Same
    # meta_findings_synthesizer kind (no new kind), but a PER-TARGET descriptor
    # (subscription.targets has_tag g20) reading the units as other_analysts, with
    # a verify-floor slice + hedged [[ref:<uuid>]] prompt. Registered AFTER the
    # units it reads (registration order is independent, but this reads clearly).
    "analyst_country_composition.yaml",      # NEW — P3-T1/T2 (per-country composition over verified sub-claims)
    # S2-T2 — per-REGION composition BETWEEN the country reads and the world read.
    # SAME meta_findings_synthesizer kind (no new kind), a PER-TARGET descriptor
    # (subscription.targets has_tag region → one worker per region frame) reading
    # country_composition as other_analysts. The kind's READ_SLICE region branch
    # resolves each frame → its member country desks and reads their
    # country_composition heads as a SET; the run uses the WORLD-shaped prompt.
    "analyst_region_composition.yaml",       # NEW — S2-T2 (per-region composition over verified country reads)
    # S2-T4 — THEMATIC composition PILOT. SAME meta_findings_synthesizer kind, a
    # target-LESS descriptor (no subscription.targets → one global run) that fuses
    # ONE dimension (the `escalation` UNIT, analyst_id) across EVERY g20+watch desk
    # into one global escalation read. The subscription.substrate.thematic_dimension
    # marker routes the kind's READ_SLICE to the thematic branch (one head per desk,
    # post-supersession, verify-floored) instead of the world-over-regions branch;
    # the T7 cross-desk correlation guard de-dupes shared underlying signals.
    "analyst_escalation_composition.yaml",   # NEW — S2-T4 (thematic escalation composition over all desks)
    "analyst_composition_lineage_sweep.yaml", # NEW — P3-T6 (deterministic META sweep: multi-floor lineage-integrity over world+country composition outputs via validate_lineage; read-only audit)
    "analyst_scorecard_producer.yaml",  # NEW — P4-T2 (banded-scorecard producer; deterministic META, one scorecard row per active G20 country; data.bands = the T1 verdict, T5 eval folded)
    "analyst_forecast_scoreboard.yaml",  # NEW — P4-T7 (acute-forecast scoreboard producer; deterministic META, weekly-idempotent driver of the forecast_acute pilot: issue → exogenous-resolve → count. Side-writes acute_forecasts rows only; TRACE_ONLY counts receipt; forecasting surfaces ONLY in the T4 scoreboard, never as a claim)
    # P4-T6 — the SCOPED, MEASURED GEPA return. A SEPARATE optimizer descriptor
    # (analyzed=leadership_transition, fitness_metric=faithfulness) — the frozen
    # country_optimizer monolith stays null-cadence (SEAMS #30). META analyst (no
    # subscription.targets) => ONE run_cadence reminder (flat Dapr job count); the
    # candidate is human_gated with a REAL before/after faithfulness delta and can
    # NEVER promote on a degenerate/absent/non-positive delta.
    "analyst_unit_optimizer.yaml",       # NEW — P4-T6 (bounded-unit GEPA optimizer, faithfulness-measured)
]

# Journal Assessor (plan §4.8 leg 2): `journal_assessor` is a NEW analyst kind,
# NOT in the closed AnalystKind enum. The registry recognizes it via a
# `vocabulary_entries` row (family='analyst_kind'). We (a) register it in the
# in-process ANALYST_KIND_REGISTRY so the descriptor's `model_validate` accepts
# the kind during this bring-up, and (b) upsert the persistent vocabulary row so
# a freshly-booted registry recognizes it too (idempotent).
#
# NOTE (Wave 2): the consolidation tier (`journal_consolidator`,
# analyst_journal_consolidator.yaml above) is the SAME kind — `identity.kind:
# journal_assessor` (§4.7, "the tier is the descriptor, not a mode flag"). It is a
# distinct DESCRIPTOR id, NOT a new analyst kind, so it needs NO new vocabulary
# row here — only its descriptor file in ANALYST_FILES. The single kind below
# covers both tiers' `model_validate`.
_NEW_ANALYST_KINDS: list[str] = ["journal_assessor"]


def _load_body(name: str) -> dict:
    """Parse a descriptor YAML body + stamp the placeholder version.

    Split out of ``_load`` so the P2-T7 unit drift guard (below) can inspect the
    RAW descriptor structure — rubric + verify presence — before pydantic
    validation + DB registration. Pure file parse; no DB, no validation.
    """
    body = yaml.safe_load((DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return body


def _load(name: str) -> AnalystDescriptor:
    return AnalystDescriptor.model_validate(_load_body(name), strict=False)


# ---------------------------------------------------------------------------
# P2-T7 — unit drift guard (fail loud at REGISTER time)
# ---------------------------------------------------------------------------
#
# A bounded reasoning UNIT (the T1/T2 unit-factory pattern) is JUST an
# inline_target DESCRIPTOR carrying its OWN inline ``method.system_prompt`` +
# scope predicate + ``eval.rubric``. Every such unit runs the system
# cited→verified loop, so it MUST declare two pieces of eval coverage or it
# degrades silently:
#
#   (a) ``eval.rubric``        — the critic HARD-FAILS without it ("no
#       eval.rubric"). Catch it here, at register time, instead of at the
#       critic's first run hours later.
#   (b) ``method.llm.verify``  — the cross-family faithfulness judge ref. A unit
#       without it silently FALLS BACK to the deterministic citation-presence
#       floor (no real LLM faithfulness verdict).
#
# UNIT SIGNATURE (how we tell a unit from the country_assessor MONOLITH FEEDER):
# both are ``identity.kind: inline_target``, but a UNIT carries an INLINE
# ``method.system_prompt`` (its bounded prompt VERBATIM in the descriptor) while
# country_assessor is driven by a ``method.prompt_module`` and has NO inline
# system_prompt. So "inline_target + non-empty inline system_prompt" binds the
# guard to the 4 bounded units + any FUTURE inline_target unit while leaving
# country_assessor (and any other prompt_module-driven inline_target) untouched.


class UnitDriftError(RuntimeError):
    """A bounded inline_target UNIT is missing required eval coverage.

    Raised at bringup/register time so a coverage gap fails LOUD with a
    non-zero exit instead of degrading silently at the unit's first run.
    """


def _is_bounded_unit(body: dict) -> bool:
    """True iff this descriptor body is a bounded reasoning UNIT.

    Unit signature: ``identity.kind == 'inline_target'`` AND a non-empty inline
    ``method.system_prompt``. country_assessor (prompt_module, no inline
    system_prompt) is therefore NOT a unit and is exempt from the guard.
    """
    identity = body.get("identity") or {}
    method = body.get("method") or {}
    if identity.get("kind") != "inline_target":
        return False
    system_prompt = method.get("system_prompt")
    return bool(system_prompt and str(system_prompt).strip())


def _unit_eval_coverage(body: dict) -> tuple[bool, bool]:
    """Return ``(has_rubric, has_verify)`` for a unit descriptor body.

    ``has_rubric``  — ``eval.rubric`` present + non-empty.
    ``has_verify``  — ``method.llm.verify`` present + non-empty (the
                      cross-family faithfulness judge ref).
    """
    eval_block = body.get("eval") or {}
    rubric = eval_block.get("rubric")
    has_rubric = bool(rubric and str(rubric).strip())

    llm = (body.get("method") or {}).get("llm") or {}
    has_verify = bool(llm.get("verify"))
    return has_rubric, has_verify


def _unit_coverage_rows(bodies: list[tuple[str, dict]]) -> list[tuple[str, str, bool, bool]]:
    """Coverage rows ``(unit_id, fname, has_rubric, has_verify)`` for each UNIT.

    Non-units (country_assessor + the meta/deterministic analysts) are skipped.
    """
    rows: list[tuple[str, str, bool, bool]] = []
    for fname, body in bodies:
        if not _is_bounded_unit(body):
            continue
        unit_id = (body.get("identity") or {}).get("id", "<unknown>")
        has_rubric, has_verify = _unit_eval_coverage(body)
        rows.append((unit_id, fname, has_rubric, has_verify))
    return rows


def _print_unit_coverage(bodies: list[tuple[str, dict]]) -> None:
    """Enumerate each bounded unit + its {rubric, verify} coverage at register
    time so the operator sees the picture before anything touches the DB."""
    rows = _unit_coverage_rows(bodies)
    print(f"Bounded-unit eval coverage (P2-T7 drift guard) — {len(rows)} unit(s):")
    if not rows:
        print("  (no bounded inline_target units in this set)")
        return
    for unit_id, _fname, has_rubric, has_verify in rows:
        print(
            f"  - {unit_id:<28} rubric={'yes' if has_rubric else 'NO '}  "
            f"verify={'yes' if has_verify else 'NO '}"
        )


def _assert_unit_eval_coverage(bodies: list[tuple[str, dict]]) -> None:
    """Fail LOUD if any bounded inline_target UNIT lacks rubric AND/OR verify.

    Raises :class:`UnitDriftError` (→ non-zero exit via the unhandled
    propagation out of ``main``) listing every offending unit + what it's
    missing. country_assessor and the meta/deterministic analysts are exempt
    (they are not bounded units — see :func:`_is_bounded_unit`).
    """
    problems: list[str] = []
    for unit_id, fname, has_rubric, has_verify in _unit_coverage_rows(bodies):
        missing: list[str] = []
        if not has_rubric:
            missing.append("eval.rubric (the critic HARD-FAILS without it)")
        if not has_verify:
            missing.append(
                "method.llm.verify (the faithfulness judge; silently falls back "
                "to the deterministic floor without it)"
            )
        if missing:
            problems.append(f"  - {unit_id} ({fname}): missing " + " AND ".join(missing))
    if problems:
        raise UnitDriftError(
            "P2-T7 unit drift guard: bounded inline_target unit(s) lack required "
            "eval coverage. Every bounded UNIT (inline_target + an inline "
            "method.system_prompt) MUST declare BOTH eval.rubric AND "
            "method.llm.verify before it can register:\n" + "\n".join(problems)
        )


async def _register_new_analyst_kinds(pg: Any) -> None:
    """Register the NEW (non-builtin) analyst kinds (plan §4.8 leg 2).

    (a) in-process: so the descriptor `model_validate` below accepts the kind.
    (b) persistent: idempotently upsert the `vocabulary_entries` row so a
        freshly-booted registry recognizes the kind too. Without (b) the next
        cold-start would 500 the kind validation again.
    """
    from legba.data.schemas.analyst import register_analyst_kind

    for kind in _NEW_ANALYST_KINDS:
        register_analyst_kind(kind)
        async with pg.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO vocabulary_entries (family, value, introduced)
                VALUES ('analyst_kind', $1, now())
                ON CONFLICT (family, value) DO NOTHING
                """,
                kind,
            )


async def main() -> int:
    # Load the raw descriptor bodies up front so the P2-T7 unit drift guard can
    # inspect their structure and FAIL LOUD before we open a DB connection or
    # register anything. Catching a missing rubric/verify here — the earliest
    # point — beats the critic hard-failing or the verify silently falling back
    # to the deterministic floor at the unit's first run.
    bodies = [(fname, _load_body(fname)) for fname in ANALYST_FILES]
    _print_unit_coverage(bodies)
    _assert_unit_eval_coverage(bodies)

    pg, reg = await open_registry()
    try:
        await _register_new_analyst_kinds(pg)
        results: list[RegisterResult] = []
        for fname, body in bodies:
            desc = AnalystDescriptor.model_validate(body, strict=False)
            results.append(
                await register_descriptor(pg, reg, family=Family.ANALYST, descriptor=desc)
            )
        failures = print_results("Source-first analyst set:", results)
    finally:
        await close_registry(pg, reg)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
