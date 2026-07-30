# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Supply-chain pack — register the 10 desks + the 1 bounded unit (all draft).

Registers the analysis half of the supply-chain exemplar domain per
planning/SUPPLY_CHAIN_PACK_PLAN_2026-07-29.md (the collection half — 8 source
descriptors — shipped separately via
``scripts/bringup_register_supply_chain_sources.py``):

  * 10 TargetDescriptors — 6 Tier-A desks (§1.1) + 4 Tier-B desks (§1.2), two new
    functional families: ``lane_*`` (a geo-anchored physical corridor) and
    ``flow_*`` (a geo-anchored commodity/product flow).
  * 1 AnalystDescriptor — ``disruption_status`` (§2), the pack's ONE new bounded
    reasoning unit.

Mirrors ``scripts/bringup_register_supply_chain_sources.py``'s structure
(idempotent direct-DB registration through ``_p17_registrar``).

Ships INERT / activation is the operator's:
  * EVERY descriptor here ships ``identity.state: draft``, so bulk registration
    creates NO live actor and NO fan-out. The reconciler boot-wires only
    ``active`` targets (``runtime/source_first_runtime.py:901``) and
    ``AnalystActor._cadence_targets`` (``runtime/dapr_actors.py:1591-1650``)
    evaluates a unit's subscription predicate against ``is_head AND
    state='active'`` targets only — so a draft desk cannot be fanned onto and a
    draft unit cannot fire.
  * TIER-A ACTIVATION IS GATED ON THE PREFLIGHT. Run
    ``scripts/preflight_supply_chain_lanes.py`` (read-only) FIRST: every Tier-A
    desk must land under the 360-row SQL pre-filter with zero predicate hits
    dropped and no single source over ~30% of the lane. A mis-sized desk's
    findings rest on a silently truncated slice (plan §0.2, §6.5 kill
    criterion 4).
  * TIER-B DESKS STAY DRAFT until their own collection gate is met — each gate is
    written into that descriptor's header (lane_panama >= 40 titles/30d,
    flow_critical_minerals >= 100 + a precision spot-check,
    flow_container_freight >= 150, lane_baltic_north_sea >= 80). As of 2026-07-29
    NONE of the four gates is met; do not flip them.

NO VOCABULARY SEED IS NEEDED — checked, not assumed:
  * The unit's ``identity.kind`` is ``inline_target``, which is a BUILT-IN
    ``AnalystKind`` enum member (``src/legba/data/schemas/analyst.py:33-34``,
    folded into ``_BUILTIN_KINDS``). The registry validates a kind from the
    closed enum UNION the ``vocabulary_entries`` (family='analyst_kind') rows it
    mirrors, and built-ins are always present — so ``disruption_status`` is a new
    INSTANCE of an EXISTING kind, not a new kind. This is exactly the
    proliferation_watch / energy_security situation: the T1 unit-factory pattern
    adds NO Python kind module, NO ``_KIND_MODULE_NAMES`` entry, NO
    ``_NEW_ANALYST_KINDS`` entry (contrast ``journal_assessor`` /
    ``entity_researcher`` / ``signal_salience`` in
    ``scripts/bringup_register_analysts.py``, which DO need the row and get it
    from ``_register_new_analyst_kinds``).
  * Target-side vocabulary: the registry vocab-validates ONLY
    ``scope.entity_classes`` and ``scope.relationship_types``
    (``registry/descriptor.py:1024-1031``). Every value used by the 10 desks is
    already in the live ``vocabulary_entries`` seed (verified by read-only SELECT
    on 2026-07-29: entity_class carries country/organization/corporation/
    infrastructure/commodity/location/international_org/event_series/person/
    military_unit/armed_group/concept; relationship_type carries TradesWith/
    SanctionsAgainst/HostileTo/OperatesIn/LocatedIn/PartOf/Targets/
    MilitaryPresenceIn/PartnersWith/CompetesWith/SubsidiaryOf).
    ``scope.tags`` — including the pack's new ``supply_chain`` fan-out key — is
    NOT vocabulary-backed (free-form snake_case per ``target.py::_ScopeTag``), so
    it needs no seed either.

RUNTIME REQUIREMENT — ``_p17_registrar.py`` MUST SIT ALONGSIDE THIS FILE. This
script does ``sys.path.insert(0, <its own dir>)`` and imports ``_p17_registrar``
from there. If you stage it into a container as ``/tmp/scripts`` (the usual
pattern when the image's baked ``scripts/`` is older than the worktree), copy
BOTH files and run from that directory, e.g.::

    docker cp scripts/bringup_register_supply_chain_pack.py \\
        legba-legba-registry-1:/tmp/scripts/
    docker cp scripts/_p17_registrar.py legba-legba-registry-1:/tmp/scripts/
    docker exec -e LEGBA_DATA_PG_DB=legba legba-legba-registry-1 \\
        python /tmp/scripts/bringup_register_supply_chain_pack.py

...and the ``descriptors/`` directory must be reachable at
``<parent of the script dir>/descriptors`` — so with the /tmp/scripts layout,
mount or copy the 11 descriptor files to ``/tmp/descriptors/``.

DB selection: direct-DB via DescriptorRegistry (default ``legba_pivot_test`` on
the dev rig — override with ``LEGBA_DATA_PG_DB=legba`` for production, exactly as
the other bring-up scripts).

REGISTRATION STEP (main session), once the preflight is clean and reviewed:
    docker exec -e LEGBA_DATA_PG_DB=legba legba-legba-registry-1 \\
        python scripts/bringup_register_supply_chain_pack.py
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
from legba.data.schemas.target import TargetDescriptor  # noqa: E402

DESCRIPTORS_DIR = pathlib.Path(__file__).resolve().parent.parent / "descriptors"

# TIER A — the 6 volume-backed desks the plan activates at launch (§1.1). Order
# is the plan's ranking, so the print-out reads like the plan's table.
TIER_A_TARGET_FILES = [
    "target_lane_hormuz.yaml",                    # 1 — strongest measured corridor signal
    "target_lane_red_sea.yaml",                   # 2 — cleanest disruption-vs-baseline contrast
    "target_lane_malacca_south_china_sea.yaml",   # 3 — densest corridor + best cross-domain demo
    "target_lane_black_sea.yaml",                 # 4 — the only food-trade lane with volume
    "target_flow_semiconductors.yaml",            # 5 — KR/TW-centric, geo anchor verified
    "target_flow_energy_shipping.yaml",           # 6 — producer-side complement to lane_hormuz
]

# TIER B — declared so the topology is visible and the collection gap is NAMED;
# each stays draft until the gate in its own descriptor header is met (§1.2).
TIER_B_TARGET_FILES = [
    "target_lane_panama.yaml",                    # 7 — gate: >= 40 titles/30d (slot M-2 landed)
    "target_flow_critical_minerals.yaml",         # 8 — gate: >= 100 + precision (slot C-1 landed)
    "target_flow_container_freight.yaml",         # 9 — gate: >= 150 (slot F-1 landed); source-pinned
    "target_lane_baltic_north_sea.yaml",          # 10 — gate: >= 80 (slot M-1 STILL UNFILLED)
]

TARGET_FILES = TIER_A_TARGET_FILES + TIER_B_TARGET_FILES

# The ONE new bounded unit. `inline_target` + an inline method.system_prompt +
# eval.rubric + method.llm.verify (the T1 unit-factory pattern).
ANALYST_FILES = [
    "analyst_disruption_status.yaml",
]


def _load_target(name: str) -> TargetDescriptor:
    """Mirror scripts/bringup_register_situation_targets._load — yaml +
    placeholder version + strict=False validation against the real schema (which
    also COMPILES scope.predicate, target.py:78-95)."""
    body = yaml.safe_load((DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return TargetDescriptor.model_validate(body, strict=False)


def _load_analyst(name: str) -> AnalystDescriptor:
    body = yaml.safe_load((DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return AnalystDescriptor.model_validate(body, strict=False)


def _assert_unit_eval_coverage(name: str) -> None:
    """The P2-T7 bounded-unit drift guard, applied on THIS path.

    ``scripts/bringup_register_analysts.py`` runs this guard over its own
    ANALYST_FILES set; this pack registers its unit through its own registrar
    (the convention every post-P2 analyst wave follows), so the guard is
    re-asserted here rather than skipped. A bounded unit (inline_target + an
    inline ``method.system_prompt``) that lacks ``eval.rubric`` makes the critic
    HARD-FAIL, and one that lacks ``method.llm.verify`` silently falls back to
    the deterministic citation floor with no faithfulness judge. Both are
    register-time refusals, not run-time surprises.
    """
    body = yaml.safe_load((DESCRIPTORS_DIR / name).read_text())
    method = body.get("method") or {}
    is_unit = (
        (body.get("identity") or {}).get("kind") == "inline_target"
        and bool(str(method.get("system_prompt") or "").strip())
    )
    if not is_unit:
        return
    missing: list[str] = []
    if not str(((body.get("eval") or {}).get("rubric")) or "").strip():
        missing.append("eval.rubric (the critic HARD-FAILS without it)")
    if not (method.get("llm") or {}).get("verify"):
        missing.append(
            "method.llm.verify (the faithfulness judge; silently falls back to "
            "the deterministic floor without it)"
        )
    if missing:
        raise SystemExit(
            f"unit drift guard: {name} is a bounded inline_target UNIT but is "
            "missing " + " AND ".join(missing)
        )


async def main() -> int:
    # Fail LOUD before any DB connection: parse + validate every descriptor and
    # re-assert the bounded-unit eval coverage.
    for fname in ANALYST_FILES:
        _assert_unit_eval_coverage(fname)

    pg, reg = await open_registry()
    try:
        target_results: list[RegisterResult] = []
        for fname in TARGET_FILES:
            desc = _load_target(fname)
            target_results.append(
                await register_descriptor(pg, reg, family=Family.TARGET, descriptor=desc)
            )
        analyst_results: list[RegisterResult] = []
        for fname in ANALYST_FILES:
            desc = _load_analyst(fname)
            analyst_results.append(
                await register_descriptor(pg, reg, family=Family.ANALYST, descriptor=desc)
            )
        failures = print_results(
            f"Supply-chain pack — desks ({len(target_results)} targets, all draft):",
            target_results,
        )
        failures += print_results(
            f"Supply-chain pack — bounded unit ({len(analyst_results)} analyst, draft):",
            analyst_results,
        )
        print(
            "\nAll 11 descriptors are registered DRAFT — nothing is wired and "
            "nothing fires yet.\nNEXT (in order, main session):\n"
            "  1. scripts/preflight_supply_chain_lanes.py   (read-only; every "
            "Tier-A desk must be clean)\n"
            "  2. flip lane_hormuz + lane_red_sea + lane_malacca_south_china_sea "
            "draft -> configured -> active\n"
            "  3. flip disruption_status draft -> configured -> active\n"
            "  4. force one live run per desk; require >= 3 [N] markers and a "
            "non-empty data.indicators array\n"
            "  5. one 7-day watch cycle + the verify readout (plan §6.5) BEFORE "
            "activating the remaining 3 Tier-A desks\n"
            "  Tier-B desks stay draft until their per-descriptor collection "
            "gate is met."
        )
    finally:
        await close_registry(pg, reg)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
