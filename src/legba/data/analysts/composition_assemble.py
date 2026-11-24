# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE DETERMINISTIC READ — the assembly and rollup arms of the
synthesizer's ``_run``, in one call.

Split out of ``meta_findings_synthesizer.py`` (the module-size gate's own named
seam: "the rollup-assembly call site around ``_rollup.assemble_region_rollup``")
so the floor that W4 F-3 adds to the world read has somewhere to land that is
not a module at its ceiling. PURE MOVE: the lines below are ``_run``'s ASSEMBLE
section, with the run's locals passed in as keyword arguments and the outputs
handed back as a :class:`DeterministicRead` rather than bound in ``_run``'s
frame. The finding coercion arrives as ``coerce`` — the synthesizer's
``_coerce_finding`` injected, the same idiom ``region_rollup.assemble_region_
rollup`` takes, so this leaf never imports back into its host.

Two arms, and the caller enters this function only when the assembly regime is
on for the run (``_assembling``):

  * REGION (``region_scoped``) — D-5 §4.1: the region tier stops writing and
    starts adding up (``region_rollup.assemble_region_rollup``).
  * everything else — D-2: the composition tier CARRIES its inputs' words
    (``assembly_payload.build_assembly``) and the body is the render.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping, NamedTuple, Sequence

from . import composition_slice as _slice
from . import off_roster_activity as _off_roster
from . import region_rollup as _rollup
from .assembly_payload import (
    DESK_QUESTIONS_OPTION,
    assembly_confidence,
    assembly_magnitudes,
    assembly_shared_signals,
    assembly_tags,
    build_assembly,
    now_iso as assembly_now_iso,
    select_carried as assembly_select_carried,
)
from ..provenance.assembly_floor_arm import floor_disclosure
from .assembly_carry import WORLD_CARRY_ROW_KEY
from .assembly_floor import (
    FloorSelection,
    floor_select,
    floor_stale_hours,
    floor_step,
    stamp_floor,
)
from .assembly_render import assembly_title, render_assembly_body
from .assembly_world import FLOOR_EXTRA_ROW_KEY, stamp_world_carry, world_order_key
from .contrary_tension import merge_contrary_tension
from .world_block_evidence import stamp_balance_dates

__all__ = ["DeterministicRead", "FLOOR_WITHDRAWN_KEY", "assemble_deterministic_read"]

logger = logging.getLogger(__name__)

#: ``data.data.assembly.floor_withdrawn`` — N-T (2026-11-13). The floor's own
#: identities (ARM 4(c), ``assembly_floor_arm.floor_disclosure``) are checked
#: BEFORE publication, so a broken floor ledger is never published: the read
#: falls back to the strict prefix of the pure world order (no subject-group
#: floor, no carry plan) and this key states why, with the broken identities.
FLOOR_WITHDRAWN_KEY: str = "floor_withdrawn"


class DeterministicRead(NamedTuple):
    """What the deterministic arm hands back to ``_run``.

    Exactly one of ``assembly_payload`` / ``rollup_payload`` is set. ``usage``
    is the zero-token receipt (no model ran), and ``steps`` are appended to the
    run's own step list in order.
    """

    assembly_payload: dict[str, Any] | None
    rollup_payload: dict[str, Any] | None
    finding: Any
    usage: dict[str, int]
    steps: list[dict[str, Any]]
    #: N-T — the published candidate order when the floor was WITHDRAWN before
    #: publication (the caller re-mints ``derived_from`` from it); ``None``
    #: when the order the caller passed in stands.
    order: list[Mapping[str, Any]] | None = None


async def assemble_deterministic_read(
    *,
    sliced: Sequence[Mapping[str, Any]],
    options: Mapping[str, Any],
    inputs: Sequence[Mapping[str, Any]],
    tier: str,
    region_scoped: bool,
    horizon_hours: int | None,
    periphery: Sequence[Mapping[str, Any]],
    trimmed: Sequence[Mapping[str, Any]],
    head_ledger: Sequence[Mapping[str, Any]],
    ledger_roster: Sequence[str],
    lead_test_v2: bool,
    contributing_analysts: Sequence[str],
    coerce: Any,
    pg: Any,
    floor: FloorSelection | None = None,
    carry_plan: Mapping[str, Any] | None = None,
) -> DeterministicRead:
    """The ASSEMBLE section of ``_run`` (D-2 / D-5), moved verbatim.

    ``sliced`` is already in the assembly order (the caller re-sorts it before
    minting ``derived_from`` from it, so ordinal N of the payload IS entry N of
    ``derived_from``). ``tier`` is the assembly tier the caller resolved once;
    it is not read on the region arm.

    ``floor`` is the caller's :func:`assembly_floor.floor_select` result for
    this ``sliced`` (W4 F-3): its ``carried`` IS the carry, and its ledger —
    ``None`` everywhere but a grouped world read — is stamped onto the payload.
    Absent (a direct caller), the carry is the tier-aware strict prefix.

    ``carry_plan`` is the world read's ``assembly_world.plan_world_carry``
    ledger (W1) — ``via_unit`` and the restraint-shift stamps; ``None`` on every
    other tier, which leaves the payload untouched.
    """
    steps: list[dict[str, Any]] = []
    _order: list[Mapping[str, Any]] | None = None
    # --- ASSEMBLE (D-2) — the deterministic path, INSTEAD of the model -----
    # Under the assembly the composition tier does not write prose about its
    # inputs; it CARRIES their words. Selection is the only remaining
    # discretionary act and it is a strict prefix of a total order, so there is
    # nothing left for a model to decide at this tier — the interpretive voice
    # moves to the Assessment channel (D-6), which is a separate row with its
    # own badge and its own population.
    #
    # The prompt is still RENDERED by ``_run`` and simply not sent. That is
    # deliberate for the flag-gated build: it keeps the nine block renderers,
    # their splice order and the prompt receipts exercised on both arms while
    # both arms are live, so ``test_composer_prompt_block_equivalence`` stays a
    # real proof rather than a proof about a path nobody takes. It costs CPU
    # over rows already in memory and no tokens. Once the flag is default-on,
    # short-circuiting it is a one-line follow-on.
    _assembly_payload: dict[str, Any] | None = None
    _rollup_payload: dict[str, Any] | None = None
    # D-5 §4.1 — the REGION tier stops writing and starts adding up. No LLM, no
    # prompt, no judge: it CARRIES its member country assemblies' lead blocks
    # forward unchanged and states the arithmetic of the membership. Checked
    # BEFORE the assembly branch because a region run satisfies both.
    if region_scoped:
        _rollup_payload, finding, _rollup_steps = _rollup.assemble_region_rollup(
            sliced,
            region_id=options.get("target_id"),
            horizon_hours=horizon_hours,
            as_of=assembly_now_iso(),
            # Option WINS over LEGBA_ROLLUP_MASS_FLOOR — the house `_coerce`
            # idiom. Default 0.0, which is byte-identical to carrying any block
            # that has mass at all.
            mass_floor=_rollup.rollup_mass_floor(options),
            coerce=coerce,
            contributing_analysts=contributing_analysts,
        )
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        steps.extend(_rollup_steps)
    else:
        # The spec's tier enum is country | world | thematic and has no
        # REGION value, deliberately: §4 retires the region generative path
        # outright and the world assembler carries country blocks forward.
        # Until D-5 lands, a region run is the multi-country, world-shaped
        # read it has always been, so it assembles as `world` rather than
        # inventing a fourth tier the reader and the arms would have to
        # learn and then unlearn. (``tier`` is resolved by the caller.)
        def _build(
            cands: Sequence[Mapping[str, Any]],
            carried: Sequence[Mapping[str, Any]],
            floor_ledger: Mapping[str, Any] | None,
            plan: Mapping[str, Any] | None,
        ) -> dict[str, Any]:
            payload = build_assembly(
                tier=tier,
                as_of=assembly_now_iso(),
                candidates=cands,
                # W4 F-3 — the carry is the caller's ``floor_select``: the
                # tier-aware strict prefix (country/thematic 8, world 16) on every
                # tier, plus ONE block per subject group on a grouped world read,
                # selected so the published order keeps ARM 4(b)'s prefix property
                # (``assembly_floor``'s banner). No selection handed in ⇒ the
                # strict prefix, as shipped.
                carried=carried,
                periphery=periphery,
                trimmed=trimmed,
                coverage=head_ledger,
                coverage_roster=ledger_roster,
                magnitudes=assembly_magnitudes(cands),
                also_cited_by=assembly_shared_signals(cands),
                questions=options.get(DESK_QUESTIONS_OPTION),
                # Amendment 7f — THE PARAMETER THAT WAS NEVER PASSED. It has
                # been on `build_assembly` since D-2 and every caller left it
                # `None`, so `target_name` has been the slug on every block and
                # every drop row of every live record — which is how the voice
                # came to write "Pakistan" for `country_watch_kp` and be failed
                # for it. `composition_slice` resolves the map once, on the
                # slice it already reads; absent (a legacy slice, a direct
                # caller, a tier that stamps none) this is `{}` and the payload
                # is byte-for-byte the one that shipped.
                target_names=_slice.unit_names_of(inputs),
                invisible_heads=options.get("invisible_heads"),
                lead_test_v2=lead_test_v2,
                # STEP E — the COUNTRY VOICE, world tier only; `unit_names_of` idiom.
                context_heads=_slice.country_assessment_context_of(inputs),
            )
            # W4 F-3 — the floor's audit line (``floor``, ``order_rank``, the
            # carried_by_floor / displaced_by_floor stamps). ``ledger=None`` —
            # every tier but a grouped world read — returns the payload untouched.
            payload = stamp_floor(
                payload, floor_ledger, stale_hours=floor_stale_hours(options),
            )
            return stamp_world_carry(payload, plan)

        _floor_ledger = floor.ledger if floor is not None else None
        _assembly_payload = _build(
            sliced,
            floor.carried if floor is not None
            else assembly_select_carried(sliced, tier=tier),
            _floor_ledger,
            carry_plan,
        )
        # N-T — the floor's identities are checked BEFORE publication. A broken
        # constructor is never published: the read falls back to the strict
        # prefix of the pure order and says so (``floor_withdrawn``).
        _broken = (
            floor_disclosure(_assembly_payload).findings
            if (_floor_ledger or carry_plan) else []
        )
        if _broken:
            _detail = "; ".join(f.detail for f in _broken)
            logger.error(
                "composition_assemble.floor_withdrawn detail=%s — the floor ledger "
                "failed its own identities; the strict prefix of the pure order is "
                "published instead", _detail,
            )
            _pure_rows = [r for r in sliced if not r.get(FLOOR_EXTRA_ROW_KEY)]
            for _r in _pure_rows:
                _r.pop(WORLD_CARRY_ROW_KEY, None)  # type: ignore[attr-defined]
            _pure = floor_select(
                _pure_rows, tier=tier, apply_floor=False, key=world_order_key,
            )
            _assembly_payload = _build(_pure.order, _pure.carried, None, None)
            _assembly_payload[FLOOR_WITHDRAWN_KEY] = {
                "why": _broken[0].reason,
                "detail": _detail,
                "fallback": "strict_prefix_pure_order",
            }
            _floor_ledger = None
            _order = list(_pure.order)
            steps.append({
                "phase": "assemble", "kind": FLOOR_WITHDRAWN_KEY,
                "why": _broken[0].reason,
            })
        # W5 — the off-roster countries the world slice stamped, persisted for
        # the aperture (``off_roster_activity``); no stamp ⇒ the payload as is.
        _assembly_payload = _off_roster.with_off_roster(_assembly_payload, inputs)
        # 7a — the CONTRARY leg of the tension rule. Splices LIVE,
        # polarity-derived contradictions for the blocks this payload carries.
        # No pool, no rows, or an unapplied migration 0221 each return the
        # payload UNCHANGED — today's body, byte for byte.
        _assembly_payload = await merge_contrary_tension(_assembly_payload, pg)
        # N-C — the newest date on each side of a declared tension's evidence
        # balance (read-only SELECT on signals; no pool or any error leaves the
        # dates unknown and the counts standing).
        _assembly_payload = await stamp_balance_dates(_assembly_payload, pg)
        finding = coerce(
            json.dumps({
                "title": assembly_title(_assembly_payload),
                "body": render_assembly_body(_assembly_payload),
                "confidence": assembly_confidence(_assembly_payload),
                "tags": assembly_tags(_assembly_payload),
            }),
            fallback_title="Assembled read",
            contributing_analysts=contributing_analysts,
        )
        # `_coerce_finding` stamps ``data.raw_llm_response`` — "the LLM's raw
        # JSON for audit". On this path NO LLM RAN, and the value would be an
        # 8,000-char copy of the body we just rendered under a field name that
        # says a model wrote it. A field whose NAME is false is the small
        # dishonesty this whole program is about, so it is dropped rather than
        # filled. The audit trail loses nothing: the assembly is reproducible
        # from `data.assembly` by construction, which is strictly more than a
        # raw response ever gave.
        finding.data.pop("raw_llm_response", None)
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        steps.append({
            "phase": "assemble",
            "kind": "assembly.v1",
            "blocks": len(_assembly_payload["blocks"]),
            "candidates": len(sliced),
            "dropped": _assembly_payload["drops"]["counts"]["shown_not_carried"],
            "lead": _assembly_payload["lead"]["kind"],
            "earned": _assembly_payload["lead"]["test"]["earned"],
        })
        if _floor_ledger:
            steps.append(floor_step(_floor_ledger))
        steps.append({
            "phase": "reflect",
            "kind": "coerce_finding",
            "confidence": finding.confidence,
            "evidence_count": len(finding.evidence),
            "structured": "unstructured" not in finding.tags,
        })
    return DeterministicRead(
        assembly_payload=_assembly_payload,
        rollup_payload=_rollup_payload,
        finding=finding,
        usage=usage,
        steps=steps,
        order=_order,
    )
