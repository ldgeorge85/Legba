# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W-4 — THE EXTERNAL GRADER ROUTE and the three-way family fence.

Extracted from :mod:`analyst_deps_builder` for the module-size gate: the builder
stood at its ceiling and the grader route is a cohesive, self-contained unit
(the fence, the family map, the resolution ladder) with no inbound dependency on
the builder's internals. The builder imports these names ONE WAY and re-exports
them, so ``analyst_deps_builder.resolve_grader_route`` resolves unchanged for
every caller and test. This module is a LEAF — it reimplements the two two-line
helpers it needs rather than importing the builder, so the dependency never
runs back.

THE RECORD THAT MADE THIS CODE INSTEAD OF A COMMENT. From the R2 correction:

  "The R2 'cross-family independence' lane was VOID, not open. It graded with
   `nvidia/nemotron-3-super-120b` — the same model family as the production
   judge … The independence property the lane exists to test was therefore
   never tested."

An external auditor that grades with the writer's model is measuring the
writer's self-consistency; one that grades with the JUDGE's model is measuring
what the platform already measures 84% of the time. Either way the number is not
what its label says, and NOTHING about the descriptor would look wrong. So the
property is enforced where it can fail loudly: three refusals, in code, each with
its own test.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, NamedTuple

logger = logging.getLogger(__name__)

#: The deployment-wide judge override, read to enforce refusal (c). Mirrored
#: from ``analyst_deps_builder.JUDGE_STACK_REF_ENV`` (a bare string constant, so
#: mirroring it is behaviour-neutral) to keep this module a leaf.
JUDGE_STACK_REF_ENV = "LEGBA_JUDGE_STACK_REF"


def _is_anthropic_component(component_id: str | None) -> bool:
    """True if a stack-component id names the Anthropic plane (``llm.anthropic.*``).

    A two-line mirror of the builder's predicate of the same name — Anthropic is
    consult-only and never scheduled, and a scheduled grader may never route onto
    it. Mirrored rather than imported so this module stays a leaf.
    """
    return bool(component_id) and "anthropic" in component_id.lower()


def _stack_ref_raw(value: Any) -> str | None:
    """Shape-tolerant StackRef extraction: dump mapping / live StackRef / bare
    string -> the component id, else ``None``. The same variants the builder's
    ``_primary_llm_component_id`` / ``_verify_llm_component_id`` accept."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        raw = value.get("raw")
        return str(raw) if isinstance(raw, str) and raw else None
    raw = getattr(value, "raw", None)
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(value, str) and value:
        return value
    return None



# ---------------------------------------------------------------------------
# THE GRADER ROUTE (W-4) — a THIRD family, fenced from the writer and the judge
# ---------------------------------------------------------------------------
#
# THE RECORD THAT MADE THIS CODE INSTEAD OF A COMMENT. From the R2 correction:
#
#   "The R2 'cross-family independence' lane was VOID, not open. It graded with
#    `nvidia/nemotron-3-super-120b` — the same model family as the production
#    judge … The independence property the lane exists to test was therefore
#    never tested."
#
# An external auditor that grades with the writer's model is measuring the
# writer's self-consistency; one that grades with the JUDGE's model is measuring
# what the platform already measures 84% of the time. Either way the number is
# not what its label says, and NOTHING about the descriptor would look wrong. So
# the property is enforced where it can fail loudly: three refusals, in code,
# each with its own test.
#
#   (a) ANTHROPIC — a HARD standing rule, not a preference: Anthropic is
#       consult-only and never scheduled. Reuses ``_is_anthropic_component``, the
#       same predicate the deterministic LLM wiring already refuses on.
#   (b) THE WRITER — the descriptor's own ``method.llm.primary``. The live core
#       plane is the self-hosted gpt-oss-120b writer; the writer cannot grade itself.
#   (c) THE JUDGE — the live value of ``LEGBA_JUDGE_STACK_REF``, read at
#       resolution time rather than captured at import, because the operator
#       repoints the whole deployment's judging with that one variable and a
#       fence that cached it would fence yesterday's judge.
#
# The comparison is by component id AND, when both ids are registered in
# :data:`GRADER_FAMILY_BY_COMPONENT`, by FAMILY — so pointing the grader at a
# different component of the same family is refused too, which is precisely the
# mistake R2 made. Unknown ids degrade to id equality rather than to a guess: a
# family map that hallucinates a family is worse than one that admits it does
# not know.

#: The deployment-wide grader override. Separate from ``LEGBA_JUDGE_STACK_REF``
#: on purpose — the R4 freeze forbids touching the judge route, and these two
#: planes must be repointable independently or the fence at (c) is unenforceable.
GRADER_STACK_REF_ENV = "LEGBA_EXTERNAL_GRADER_STACK_REF"

#: Component id → model FAMILY. Hand-maintained and deliberately small: it holds
#: exactly the components this deployment has registered, because a family
#: inferred from a substring is a fence that fails open the day somebody names a
#: component well. An id absent from this map has family ``""`` and the fence
#: falls back to id equality — see :func:`grader_fence_refusal`.
GRADER_FAMILY_BY_COMPONENT: dict[str, str] = {
    # the WRITER's plane (the self-hosted gpt-oss-120b core plane)
    "llm.primary.openai_compat": "openai_oss",
    # the JUDGE's plane
    "llm.judge.openrouter_nemotron120b": "nvidia",
    "llm.judge.nemotron3_super.openai_compat": "nvidia",
    "llm.judge.nemotron3_ultra": "nvidia",
    # the RULED grader — third family, already registered active, already priced.
    # UNFUNDED as of 2026-09-05: the R4 reachability probe got HTTP 402 and
    # completed 0 of 12 graded calls. The entry stays — a component that once
    # graded must keep its family declared, or a later repoint back onto it
    # would be fenced by id equality alone.
    "llm.judge.cerebras_gemma4_31b": "google_gemma",
    "llm.judge.cerebras_gemma4_31b.openai_compat": "google_gemma",
    # the THIRD family that actually completes — Mistral Large 3 on OpenRouter,
    # 12 of 12 probe calls at rate. This is the slot the Cerebras Gemma refs
    # move to (descriptors/stack_component_llm_judge_openrouter_mistral_large
    # .yaml). Distinct from the writer (openai_oss), the judge (nvidia) and
    # anthropic, which is the whole property the grader route exists to hold.
    "llm.judge.openrouter_mistral_large": "mistral",
    "llm.judge.openrouter_mistral_large.openai_compat": "mistral",
    # the FOURTH family — the audit rater
    "llm.audit.openrouter_llama33_70b.openai_compat": "meta_llama",
    "llm.verify.slm_8b": "meta_llama",
    # consult-only, never scheduled
    "llm.anthropic.opus_4_7": "anthropic",
}

GRADER_REFUSE_ANTHROPIC = "anthropic_is_consult_only"
GRADER_REFUSE_WRITER_FAMILY = "same_family_as_the_writer"
GRADER_REFUSE_JUDGE_FAMILY = "same_family_as_the_judge"


def grader_family_for_component(component_id: str | None) -> str:
    """The registered model family for a component id, or ``""`` when unknown.

    ``""`` is an honest answer and it is treated as one everywhere: it never
    matches another family, so an unregistered component is fenced by id
    equality alone rather than by a guess.
    """
    if not component_id:
        return ""
    ref = str(component_id).strip()
    family = GRADER_FAMILY_BY_COMPONENT.get(ref)
    if family:
        return family
    if _is_anthropic_component(ref):
        return "anthropic"
    return ""


def _same_plane(a: str | None, b: str | None) -> bool:
    """Is ``a`` the same model plane as ``b``? Id equality, then family."""
    if not a or not b:
        return False
    if str(a).strip() == str(b).strip():
        return True
    fam_a = grader_family_for_component(a)
    fam_b = grader_family_for_component(b)
    return bool(fam_a) and fam_a == fam_b


class GraderRoute(NamedTuple):
    """A resolved external-grader route: the component id + the rung that won."""

    component_id: str
    #: ``env:LEGBA_EXTERNAL_GRADER_STACK_REF`` | ``method.llm.grader``
    source: str

    @property
    def family(self) -> str:
        return grader_family_for_component(self.component_id)


def grader_fence_refusal(
    component_id: str | None,
    *,
    primary_component_id: str | None,
    judge_component_id: str | None,
) -> str:
    """``""`` when this component may grade, else the reason it may not.

    Three refusals, evaluated in the order of how badly each one would corrupt
    the number: an Anthropic grader breaks a HARD standing rule about spend and
    scheduling; the writer's family makes the audit a self-consistency check;
    the judge's family makes it a second copy of the instrument the platform
    already runs.
    """
    if not component_id:
        return ""
    if _is_anthropic_component(component_id):
        return GRADER_REFUSE_ANTHROPIC
    if _same_plane(component_id, primary_component_id):
        return GRADER_REFUSE_WRITER_FAMILY
    if _same_plane(component_id, judge_component_id):
        return GRADER_REFUSE_JUDGE_FAMILY
    return ""


def resolve_grader_route_from_llm_block(llm: Any) -> GraderRoute | None:
    """Resolve the grader route over a raw ``method.llm`` mapping.

    Ladder, first hit wins:

      0. OPT-IN GATE — no ``grader`` key ⇒ ``None``. The env var REPOINTS a
         grader; it never turns grading on, the same contract
         ``LEGBA_JUDGE_STACK_REF`` has. An analyst that never opted in cannot be
         conscripted into paying for one.
      1. ``LEGBA_EXTERNAL_GRADER_STACK_REF`` — the deployment-wide override.
      2. ``method.llm.grader`` — the per-descriptor ref.

    There is deliberately NO terminal fallback to ``method.llm.primary``. The
    judge ladder has one because an opted-in analyst judging on the producer's
    plane still beats not judging; here the same rung would land the grader on
    the WRITER, which is refusal (b) — so an unresolvable grader ref yields
    ``None`` and the run degrades to an observable heartbeat instead.
    """
    if not isinstance(llm, Mapping):
        return None
    if "grader" not in llm:
        return None
    env_ref = (os.getenv(GRADER_STACK_REF_ENV) or "").strip()
    if env_ref:
        return GraderRoute(
            component_id=env_ref, source=f"env:{GRADER_STACK_REF_ENV}"
        )
    grader_ref = _stack_ref_raw(llm.get("grader"))
    if grader_ref:
        return GraderRoute(component_id=grader_ref, source="method.llm.grader")
    return None


def resolve_grader_route(descriptor: Any) -> GraderRoute | None:
    """The FENCED grader route for a descriptor (``None`` = no grader).

    Refusal is not an error and not a fallback: it returns ``None`` and logs at
    WARNING with the reason, so the auditor runs, writes a heartbeat naming the
    gap, and grades nothing — which is the honest outcome. A fence that silently
    substituted a different model would produce numbers under a label that no
    longer described them, which is the failure this whole fence exists to stop.
    """
    llm = getattr(descriptor.method, "llm", None) or {}
    route = resolve_grader_route_from_llm_block(llm)
    if route is None:
        return None
    refusal = grader_fence_refusal(
        route.component_id,
        primary_component_id=_stack_ref_raw(
            llm.get("primary") if isinstance(llm, Mapping) else None
        ),
        judge_component_id=(os.getenv(JUDGE_STACK_REF_ENV) or "").strip() or None,
    )
    if refusal:
        logger.warning(
            "analyst_deps_builder.grader_refused analyst=%r grader=%r source=%s "
            "reason=%s — the external audit grades with a THIRD family or it "
            "does not grade; refusing and staying no-grader",
            descriptor.identity.id, route.component_id, route.source, refusal,
        )
        return None
    return route



async def wire_external_grader(
    descriptor,
    deps,
    *,
    registry_client,
    secrets_resolve,
    handler_builder,
):
    """Merge the fenced grader — and the fourth-family audit rater — into deps.

    Extracted from ``analyst_deps_builder`` with the route itself, so the whole
    W-4 plane lives in one leaf. ``handler_builder`` is injected
    (``build_llm_handler_from_stack_component``) rather than imported, which is
    what keeps this module a leaf: it depends on nothing in the builder, so the
    builder -> external_grader_route edge never runs back.

    BOTH ARE OPTIONAL AND BOTH DEGRADE. No grader ⇒ the auditor runs, writes a
    heartbeat naming the gap and grades nothing; no audit rater ⇒ it grades and
    reports its instrument overlap as UNMEASURED, which is not the same as
    reporting it as agreement. That distinction is the whole reason
    ``instrument.band`` has an ``unmeasured`` value.

    The audit rater is resolved from ``method.llm.audit_rater`` and passes the
    SAME fence as the primary grader, plus one more that is implicit in the
    arithmetic: it is pointless to double-grade with the primary's own family,
    so a rater that resolves to the grader's family is refused here too.
    """
    from dataclasses import replace as _dc_replace

    from ..data.analysts.deterministic_handlers._external_audit_grader import (
        AUDIT_RATER_DEPS_EXTRA_KEY,
        GRADER_DEPS_EXTRA_KEY,
    )

    route = resolve_grader_route(descriptor)
    if route is None:
        return deps
    if secrets_resolve is None:
        logger.warning(
            "analyst_deps_builder.grader_no_secrets analyst=%r — cannot build "
            "the grader handler without a secrets resolver; staying no-grader",
            descriptor.identity.id,
        )
        return deps

    merged = dict(deps.extras)
    try:
        merged[GRADER_DEPS_EXTRA_KEY] = await handler_builder(
            route.component_id,
            registry_client=registry_client,
            secrets_resolve=secrets_resolve,
        )
        merged[f"{GRADER_DEPS_EXTRA_KEY}_ref"] = route.component_id
        merged[f"{GRADER_DEPS_EXTRA_KEY}_family"] = route.family
        logger.info(
            "analyst_deps_builder.grader_wired analyst=%r grader=%r family=%r "
            "source=%s",
            descriptor.identity.id, route.component_id, route.family, route.source,
        )
    except Exception as exc:
        logger.warning(
            "analyst_deps_builder.grader_resolve_failed analyst=%r grader=%r "
            "err=%s — the audit degrades to an observable heartbeat",
            descriptor.identity.id, route.component_id, exc,
        )
        return deps

    llm_block = getattr(descriptor.method, "llm", None) or {}
    rater_ref = _stack_ref_raw(
        llm_block.get("audit_rater") if isinstance(llm_block, Mapping) else None
    )
    if rater_ref:
        refusal = grader_fence_refusal(
            rater_ref,
            primary_component_id=_stack_ref_raw(
                llm_block.get("primary") if isinstance(llm_block, Mapping) else None
            ),
            judge_component_id=(os.getenv(JUDGE_STACK_REF_ENV) or "").strip() or None,
        ) or (
            GRADER_REFUSE_WRITER_FAMILY
            if _same_plane(rater_ref, route.component_id) else ""
        )
        if refusal:
            logger.warning(
                "analyst_deps_builder.audit_rater_refused analyst=%r rater=%r "
                "reason=%s — the double-grade needs a FOURTH family; the "
                "instrument overlap will report UNMEASURED",
                descriptor.identity.id, rater_ref, refusal,
            )
        else:
            try:
                merged[AUDIT_RATER_DEPS_EXTRA_KEY] = await handler_builder(
                    rater_ref,
                    registry_client=registry_client,
                    secrets_resolve=secrets_resolve,
                )
                merged[f"{AUDIT_RATER_DEPS_EXTRA_KEY}_ref"] = rater_ref
                merged[f"{AUDIT_RATER_DEPS_EXTRA_KEY}_family"] = (
                    grader_family_for_component(rater_ref)
                )
                logger.info(
                    "analyst_deps_builder.audit_rater_wired analyst=%r rater=%r",
                    descriptor.identity.id, rater_ref,
                )
            except Exception as exc:
                logger.warning(
                    "analyst_deps_builder.audit_rater_resolve_failed analyst=%r "
                    "rater=%r err=%s — the instrument overlap reports UNMEASURED",
                    descriptor.identity.id, rater_ref, exc,
                )
    return _dc_replace(deps, extras=merged)


__all__ = [
    "GRADER_FAMILY_BY_COMPONENT",
    "GRADER_REFUSE_ANTHROPIC",
    "GRADER_REFUSE_JUDGE_FAMILY",
    "GRADER_REFUSE_WRITER_FAMILY",
    "GRADER_STACK_REF_ENV",
    "GraderRoute",
    "_same_plane",
    "grader_family_for_component",
    "grader_fence_refusal",
    "resolve_grader_route",
    "resolve_grader_route_from_llm_block",
    "wire_external_grader",
]
