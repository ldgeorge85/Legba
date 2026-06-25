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
    "analyst_country_assessor.yaml",
    "analyst_world_assessor.yaml",
    "analyst_country_critic.yaml",
    "analyst_country_optimizer.yaml",
    "analyst_consult_default.yaml",
    "analyst_country_predictor.yaml",
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
    "analyst_deep_consult.yaml",              # NEW — Piece 4 (deep-consult workflow bridge)
    "analyst_relationship_reifier.yaml",      # NEW — PIECE A (the reified typed-Nexus producer)
    "analyst_structural_balance.yaml",        # NEW — PIECE A consumer (signed-triad balance over nexuses)
    "analyst_graph_mining.yaml",              # NEW — PIECE A consumer (proxy-chain sign products over nexuses)
    "analyst_nexus_decay.yaml",               # NEW — PIECE A (nexuses-table temporal-lifecycle maintenance)
    "analyst_proposed_edge_governance.yaml",  # NEW — Phase D (promote/reject the proposed_edges queue; P3-1)
    "analyst_thematic_proposal.yaml",        # NEW — Phase 5b (propose thematic frames for uncovered hot situations)
    "analyst_journal_assessor.yaml",         # NEW — Journal Assessor Wave 0 (the 11th OutputKind producer; entry tier)
    "analyst_journal_consolidator.yaml",     # NEW — Journal Wave 2 (consolidation tier: SAME kind, distinct id, daily beat)
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


def _load(name: str) -> AnalystDescriptor:
    body = yaml.safe_load((DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return AnalystDescriptor.model_validate(body, strict=False)


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
    pg, reg = await open_registry()
    try:
        await _register_new_analyst_kinds(pg)
        results: list[RegisterResult] = []
        for fname in ANALYST_FILES:
            desc = _load(fname)
            results.append(
                await register_descriptor(pg, reg, family=Family.ANALYST, descriptor=desc)
            )
        failures = print_results("Source-first analyst set:", results)
    finally:
        await close_registry(pg, reg)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
