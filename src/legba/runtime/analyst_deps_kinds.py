# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-kind deterministic WIRING for the two OUT-OF-PLANE audit desks:
``standing_auditor`` (D5) and ``desk_reference`` (A-1).

Extracted from :mod:`analyst_deps_builder`'s ``_build_deterministic`` for the
module-size gate: the 09-05 merge wave (W-4 grader + A-1 desk_reference
wiring) pushed the builder 3,035 lines against its 3,040 ceiling, and these
two per-kind branches are a cohesive, self-contained unit — both desks share
the exact same two-leg shape (a self-hosted LLM leg + a ``web_access`` action
pack leg) and neither has an inbound dependency on the builder's internals.
The builder imports these two names ONE WAY and re-exports them, so
``analyst_deps_builder`` still calls them at the same call site with the same
effect on ``deps``, byte-identical to before the split.

This module is a LEAF, mirroring the shape ``external_grader_route`` already
uses: rather than import ``analyst_deps_builder._wire_deterministic_llm`` (and
risk the edge running back), the builder INJECTS it — and, for the standing
auditor's W-4 grader leg, ``build_llm_handler_from_stack_component`` too — as
callables. So this module depends on nothing in the builder, and the
builder -> analyst_deps_kinds edge never runs back.

Both functions assume their caller has already applied the
``is_deterministic and sub_handler == "<kind>"`` gate (unchanged from before
the split); they do the LLM-leg + web-pack-leg wiring the branch used to do
inline and return the (possibly replaced) ``deps``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace as _dc_replace
from typing import Any, Awaitable, Callable, Mapping

from ..data.schemas.analyst import AnalystDescriptor
from ..data.stack.llm.base import LLMProviderHandler
from .deps import StandardDeps
from .external_grader_route import JUDGE_STACK_REF_ENV
from .registry_client import RegistryHTTPClient

logger = logging.getLogger(__name__)

__all__ = [
    "wire_contrary_evidence_kind_deps",
    "wire_correctness_grader_kind_deps",
    "wire_desk_reference_kind_deps",
    "wire_reference_builder_kind_deps",
    "wire_instrument_kind_deps",
    "wire_standing_auditor_kind_deps",
]


async def wire_standing_auditor_kind_deps(
    descriptor: AnalystDescriptor,
    deps: StandardDeps,
    *,
    registry_client: RegistryHTTPClient | None,
    resolve_llm: Callable[[], Awaitable[LLMProviderHandler]],
    component_id: str | None,
    wire_deterministic_llm: Callable[..., Awaitable[StandardDeps]],
    handler_builder: Callable[..., Awaitable[Any]],
) -> StandardDeps:
    """standing_auditor (D5) — the STANDING EXTERNAL-AUDIT plane. Two legs, both
    optional and both degrading to an observable heartbeat rather than a build
    failure (the handler reports the gap; see external_audit_binding):

      * the $0 CORE plane, through the SAME shared helper as
        signal_summarizer, so the Anthropic hard-refuse applies — an external
        auditor is a scheduled analyst and may never route onto the billed
        plane;
      * the web_access ACTION PACK, as a real AgencyToolBinding. Every
        external byte this analyst reads arrives through the registered
        web_search pack tool (SSRF guard + governor + ledger); there is no
        ad-hoc HTTP anywhere in its path.

    ``wire_deterministic_llm`` (``analyst_deps_builder._wire_deterministic_llm``)
    and ``handler_builder`` (``build_llm_handler_from_stack_component``) are
    injected by the caller rather than imported — see the module docstring.
    """
    from ..data.analysts.deterministic_handlers.standing_auditor import (
        LLM_DEPS_EXTRA_KEY as _AUDITOR_LLM_KEY,
    )
    from .external_audit_binding import wire_standing_auditor_web_pack
    from .external_grader_route import wire_external_grader

    if component_id is not None:
        deps = await wire_deterministic_llm(
            descriptor, deps, resolve_llm,
            component_id=component_id,
            extra_key=_AUDITOR_LLM_KEY,
            purpose="standing_auditor",
        )
    if registry_client is not None:
        deps = await wire_standing_auditor_web_pack(
            descriptor, deps, registry_client=registry_client,
        )
        # W-4 — the GRADER plane. A third family, fenced from the writer and
        # the judge (see resolve_grader_route). Wired here rather than in
        # dapr_host's judge branch because this is not a judge: it never
        # touches LEGBA_JUDGE_STACK_REF, never writes a faithfulness row, and
        # must be repointable without moving the verify plane at all. The
        # route + fence + wiring live in the external_grader_route leaf; the
        # handler builder is injected so that module imports nothing here.
        deps = await wire_external_grader(
            descriptor, deps,
            registry_client=registry_client,
            secrets_resolve=getattr(deps, "secrets_resolve", None),
            handler_builder=handler_builder,
        )
    return deps


async def wire_contrary_evidence_kind_deps(
    descriptor: AnalystDescriptor,
    deps: StandardDeps,
    *,
    registry_client: RegistryHTTPClient | None,
    resolve_llm: Callable[[], Awaitable[LLMProviderHandler]],
    component_id: str | None,
    wire_deterministic_llm: Callable[..., Awaitable[StandardDeps]],
) -> StandardDeps:
    """contrary_evidence_pass (7a) — the CONTRARY-EVIDENCE plane. Two legs.

    Structurally the standing auditor's wiring minus the grader: this pass has
    no grader and must not acquire one, because it renders NO VERDICT. Its
    model call returns a SEARCH QUERY and nothing else; the stance is derived in
    code from fetched text. Wiring a grader here would be wiring the one organ
    the design refuses to have.

      * the $0 CORE plane, through the SAME shared helper as signal_summarizer
        and the auditor, so the Anthropic hard-refuse applies — a scheduled
        analyst may never route onto the billed plane, and this one issues one
        bounded call per claim that takes no side in the deterministic polarity
        vocabulary;
      * the web_access ACTION PACK, as a real AgencyToolBinding under this
        pass's OWN extras key. Every search and every page fetch traverses the
        agency gate, the SSRF guard, the governor and the invocation ledger;
        there is no ad-hoc HTTP in the handler's path and no ``httpx`` import
        anywhere in it.

    BOTH DEGRADE, and they degrade DIFFERENTLY. No LLM ⇒ the pass still runs and
    still contends every claim whose negation is deterministic, naming the gap
    on its heartbeat — a real, partial, honest run. No web binding ⇒ every claim
    comes back ``search_failed`` with a reason, which is the ledger saying the
    plane is down rather than the web being quiet. Neither is a build failure:
    an instrument that refuses to start is invisible to everything but a log.
    """
    from ..data.analysts.deterministic_handlers.contrary_evidence_pass import (
        LLM_DEPS_EXTRA_KEY as _CONTRARY_LLM_KEY,
        WEB_BINDING_DEPS_EXTRA_KEY as _CONTRARY_WEB_KEY,
    )
    from .external_audit_binding import wire_standing_auditor_web_pack

    if component_id is not None:
        deps = await wire_deterministic_llm(
            descriptor, deps, resolve_llm,
            component_id=component_id,
            extra_key=_CONTRARY_LLM_KEY,
            purpose="contrary_evidence_pass",
        )
    if registry_client is not None:
        deps = await wire_standing_auditor_web_pack(
            descriptor, deps, registry_client=registry_client,
            extra_key=_CONTRARY_WEB_KEY,
        )
    return deps


async def wire_desk_reference_kind_deps(
    descriptor: AnalystDescriptor,
    deps: StandardDeps,
    *,
    registry_client: RegistryHTTPClient | None,
    resolve_llm: Callable[[], Awaitable[LLMProviderHandler]],
    component_id: str | None,
    wire_deterministic_llm: Callable[..., Awaitable[StandardDeps]],
) -> StandardDeps:
    """desk_reference (A-1, ATTENTION MEASUREMENT) — the OUT-OF-PLANE daily desk
    reference. Same two legs as the standing auditor, and the same
    degrade-not-break posture (the gap reaches an operator as a heartbeat row,
    not as a build failure), with ONE deliberate difference:

      the LLM is NOT the core plane. A gpt-oss-120b reference against
      gpt-oss-120b desks measures the family's shared blind spots and calls
      them agreement, and a nemotron reference would share a family with the
      JUDGE. The shipped descriptor therefore binds a THIRD family
      (llm.judge.cerebras_gemma4_31b). Because that is a DESCRIPTOR field
      resolved through the same shared helper, an operator can re-point it —
      to a self-hosted family on ai1, say — with a PUT and no code edit. The
      Anthropic hard-refuse inside ``wire_deterministic_llm`` still applies: a
      SCHEDULED analyst may never route onto the billed plane.

    No llm block => no wiring => the handler runs, writes a heartbeat naming
    the gap, and references nothing. Same for an ungranted web_access pack.

    ``wire_deterministic_llm`` is injected by the caller — see
    :func:`wire_standing_auditor_kind_deps` and the module docstring.
    """
    from ..data.analysts.deterministic_handlers.desk_reference import (
        LLM_DEPS_EXTRA_KEY as _REFERENCE_LLM_KEY,
        WEB_BINDING_DEPS_EXTRA_KEY as _REFERENCE_WEB_KEY,
    )
    from .external_audit_binding import wire_standing_auditor_web_pack

    if component_id is not None:
        deps = await wire_deterministic_llm(
            descriptor, deps, resolve_llm,
            component_id=component_id,
            extra_key=_REFERENCE_LLM_KEY,
            purpose="desk_reference",
        )
    if registry_client is not None:
        deps = await wire_standing_auditor_web_pack(
            descriptor, deps, registry_client=registry_client,
            extra_key=_REFERENCE_WEB_KEY,
        )
    return deps


async def wire_reference_builder_kind_deps(
    descriptor: AnalystDescriptor,
    deps: StandardDeps,
    *,
    registry_client: RegistryHTTPClient | None,
    resolve_llm: Callable[[], Awaitable[LLMProviderHandler]],
    component_id: str | None,
    wire_deterministic_llm: Callable[..., Awaitable[StandardDeps]],
) -> StandardDeps:
    """reference_builder (R2) — the CORE PLANE and the web pack, under its own keys.

    The same two legs as ``desk_reference`` above, and the opposite choice on
    the first one. A-1 binds a THIRD family deliberately, because an
    out-of-plane reference against in-plane desks is the point of that
    instrument. R2 binds the CORE PLANE, equally deliberately, for a reason
    that is not about blind spots at all:

      **The reference must be free, or the tower cannot afford one per country
      per fortnight.** R1 measured this exact loop at ~1-1.7M prompt tokens and
      ~8 minutes of ai1 per reference; a 33-target roster is ~4-5 hours of
      owned hardware per cycle and $0. The same work on a paid plane is the
      cost question Program 2 exists to answer NO to. And the independence
      property the number rests on is NOT "a different model family" — it is
      "built without reading our substrate", which this lane satisfies by
      construction: it has no substrate read in its path, only the open web.
      A shared blind spot between the builder and the desks would show up as a
      `silent` claim, which the grader already publishes as coverage rather
      than as correctness.

    The Anthropic hard-refuse inside ``wire_deterministic_llm`` still applies,
    and matters more here than anywhere: this is a SCHEDULED analyst that makes
    dozens of model rounds per run, and routing it onto the billed plane would
    be the single most expensive misconfiguration available in the tree.

    EVERY LEG DEGRADES. No llm block => no wiring => the handler records
    ``no_model`` on its receipt and builds nothing. No web pack grant, or an
    agency plane that is down => ``no_web``, and NOTHING is built — which is
    the right refusal rather than a degradation, because a reference built
    without the open web would not be independent of the reads it grades.
    """
    from ..data.analysts.deterministic_handlers.reference_builder import (
        LLM_DEPS_EXTRA_KEY as _BUILDER_LLM_KEY,
        WEB_BINDING_DEPS_EXTRA_KEY as _BUILDER_WEB_KEY,
    )
    from .external_audit_binding import wire_standing_auditor_web_pack

    if component_id is not None:
        deps = await wire_deterministic_llm(
            descriptor, deps, resolve_llm,
            component_id=component_id,
            extra_key=_BUILDER_LLM_KEY,
            purpose="reference_builder",
        )
    if registry_client is not None:
        deps = await wire_standing_auditor_web_pack(
            descriptor, deps, registry_client=registry_client,
            extra_key=_BUILDER_WEB_KEY,
        )
    return deps


async def wire_correctness_grader_kind_deps(
    descriptor: AnalystDescriptor,
    deps: StandardDeps,
    *,
    registry_client: RegistryHTTPClient | None,
    resolve_llm: Callable[[], Awaitable[LLMProviderHandler]],
    component_id: str | None,
    wire_deterministic_llm: Callable[..., Awaitable[StandardDeps]],
    handler_builder: Callable[..., Awaitable[Any]],
) -> StandardDeps:
    """correctness_grader (G1) — THREE grader families, wired under three keys.

    The two out-of-plane audit desks above have ONE model leg. This one has
    three, because the property it measures is that three families reading one
    rubric agree with each other (PREREG_P1 §4): a one-family correctness number
    is a model's opinion, and Program 1 exists because that is not a measurement.

      * **F0** — ``method.llm.primary``, the $0 self-hosted core plane, through
        the SAME shared helper as signal_summarizer, so the Anthropic
        hard-refuse applies. F0 is the PRODUCER family: the model that writes
        the reads grades them here. That is deliberately NOT fenced and it is
        DISCLOSED instead — PREREG_P1 §4 registered it, and VERDICT_P1v4
        measured it not to be the outlier (F0xF3 = 0.90 was the strongest pair).
        Fencing it would delete the only $0 family and with it the whole
        possibility of a correctness number on a deployment that spends nothing.
      * **F2 / F3** — ``method.llm.family_f2`` / ``method.llm.family_f3``, the
        two PAID OpenRouter families, each passed through
        ``external_grader_route.grader_fence_refusal``: never Anthropic (a HARD
        standing rule — consult-only, never scheduled), never the writer's
        family, never the live judge's. F3 is additionally refused if it
        resolves to F2's own plane: two "independent" families that are the same
        family are one family with two names, which is precisely the mistake the
        R2 void was made of.

    EVERY LEG IS OPTIONAL AND EVERY ONE DEGRADES. A family that does not resolve
    is simply absent from ``deps.extras``; the handler grades with what it has,
    records the gap on its receipt, and flags the claims that ended up carrying
    one family's label. A build failure here would take the whole analyst down
    for a missing second opinion, which is a worse outcome than a measured
    number that says how many families produced it.

    NOTHING IS WIRED WITHOUT THE CEILING'S CONSENT EITHER — but that gate is the
    HANDLER's (``LEGBA_GRADER_DAILY_CEILING_USD``, default $0 = F2/F3 never
    called), not this function's: an operator raising the ceiling must not have
    to rebuild deps, and a resolved-but-uncalled handler costs nothing.
    """
    from ..data.analysts.deterministic_handlers.correctness_grader import (
        F0_DEPS_EXTRA_KEY,
        F2_DEPS_EXTRA_KEY,
        F3_DEPS_EXTRA_KEY,
    )
    from .external_grader_route import (
        _same_plane,
        _stack_ref_raw,
        grader_fence_refusal,
    )

    if component_id is not None:
        deps = await wire_deterministic_llm(
            descriptor, deps, resolve_llm,
            component_id=component_id,
            extra_key=F0_DEPS_EXTRA_KEY,
            purpose="correctness_grader_f0",
        )
    if registry_client is None:
        return deps
    secrets_resolve = getattr(deps, "secrets_resolve", None)
    if secrets_resolve is None:
        logger.warning(
            "analyst_deps_builder.correctness_grader_no_secrets analyst=%r — "
            "cannot build the paid grader handlers without a secrets resolver; "
            "the sweep grades with the core plane alone and says so",
            descriptor.identity.id,
        )
        return deps

    llm_block = getattr(descriptor.method, "llm", None) or {}
    judge_ref = (os.getenv(JUDGE_STACK_REF_ENV) or "").strip() or None
    merged = dict(deps.extras)
    resolved: dict[str, str] = {}
    for field, extra_key in (
        ("family_f2", F2_DEPS_EXTRA_KEY), ("family_f3", F3_DEPS_EXTRA_KEY),
    ):
        ref = _stack_ref_raw(
            llm_block.get(field) if isinstance(llm_block, Mapping) else None
        )
        if not ref:
            continue
        refusal = grader_fence_refusal(
            ref, primary_component_id=component_id, judge_component_id=judge_ref,
        ) or next(
            ("same_family_as_the_other_paid_grader" for other in resolved.values()
             if _same_plane(ref, other)),
            "",
        )
        if refusal:
            logger.warning(
                "analyst_deps_builder.correctness_family_refused analyst=%r "
                "field=%s component=%r reason=%s — the family stays UNWIRED and "
                "its claims will be graded by fewer families, flagged as such",
                descriptor.identity.id, field, ref, refusal,
            )
            continue
        try:
            merged[extra_key] = await handler_builder(
                ref, registry_client=registry_client,
                secrets_resolve=secrets_resolve,
            )
        except Exception as exc:  # noqa: BLE001 — a dead family is a finding
            logger.warning(
                "analyst_deps_builder.correctness_family_resolve_failed "
                "analyst=%r field=%s component=%r err=%s — grading degrades to "
                "the families that did resolve",
                descriptor.identity.id, field, ref, exc,
            )
            continue
        merged[f"{extra_key}_ref"] = ref
        resolved[field] = ref
        logger.info(
            "analyst_deps_builder.correctness_family_wired analyst=%r field=%s "
            "component=%r", descriptor.identity.id, field, ref,
        )
    if not resolved:
        return deps
    return _dc_replace(deps, extras=merged)


#: sub_handler -> the wiring function for it, and whether that function takes a
#: ``handler_builder``. A table rather than a chain of ``if`` blocks because the
#: chain is what grew: every instrument that landed added four near-identical
#: lines to the deps builder, which is how that module reached its size ceiling.
_INSTRUMENT_WIRERS: dict[str, tuple[Any, bool]] = {}


async def wire_instrument_kind_deps(
    descriptor: AnalystDescriptor,
    deps: StandardDeps,
    *,
    sub_handler: str | None,
    registry_client: RegistryHTTPClient | None,
    resolve_llm: Callable[[], Awaitable[LLMProviderHandler]],
    component_id: str | None,
    wire_deterministic_llm: Callable[..., Awaitable[StandardDeps]],
    handler_builder: Callable[..., Awaitable[Any]],
) -> StandardDeps:
    """Dispatch to the one instrument wirer ``sub_handler`` names, or pass through.

    Every branch it can take is optional and every one DEGRADES: a sub-handler
    with no entry here (the overwhelming majority) gets ``deps`` back untouched,
    which is what makes adding an instrument free for every analyst that is not
    one.

    Lives here rather than in the deps builder because the alternative was four
    copies of the same six-line call block in a module already at its size
    ceiling — and the fifth instrument would have been a fifth copy. The table
    is populated lazily below so the import graph stays a tree: this module is a
    LEAF, and a module-level reference to each wirer would be fine, but the
    lazy build keeps the declaration next to the functions themselves.
    """
    if not sub_handler:
        return deps
    if not _INSTRUMENT_WIRERS:
        _INSTRUMENT_WIRERS.update({
            "standing_auditor": (wire_standing_auditor_kind_deps, True),
            "desk_reference": (wire_desk_reference_kind_deps, False),
            "correctness_grader": (wire_correctness_grader_kind_deps, True),
            "reference_builder": (wire_reference_builder_kind_deps, False),
            "contrary_evidence_pass": (
                wire_contrary_evidence_kind_deps, False,
            ),
        })
    entry = _INSTRUMENT_WIRERS.get(sub_handler)
    if entry is None:
        return deps
    wirer, wants_builder = entry
    kwargs: dict[str, Any] = {
        "registry_client": registry_client,
        "resolve_llm": resolve_llm,
        "component_id": component_id,
        "wire_deterministic_llm": wire_deterministic_llm,
    }
    if wants_builder:
        kwargs["handler_builder"] = handler_builder
    return await wirer(descriptor, deps, **kwargs)
