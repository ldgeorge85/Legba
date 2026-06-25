# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Action-pack allow-list resolution (P-11 / PIVOT §4.8).

Effective agency = the THREE-WAY intersection, gated by each pack's governor:

    analyst.action_packs  ∩  target.allowed_action_packs  ∩  pack.applicability

Each leg is a separate, independently-failing gate so the operator (and the
hard-gate test) can see EXACTLY which rail denied a call:

  * GRANT     — the analyst declares the pack in ``action_packs``. (An inline
                analyst inherits the target's ``allowed_action_packs`` and so
                has an implicit grant — handled by the caller passing the
                target's allow-list as the analyst grant set.)
  * ALLOW     — the target/domain context permits the pack in
                ``allowed_action_packs``.
  * APPLICABLE — the pack's own ``applies_to_tags`` / ``applicability_predicate``
                say it is relevant to THIS target's scope.

The result is a :class:`PackResolution` per pack-id: ``effective`` is True only
when all three legs pass. The governor check (rate/budget) is a SEPARATE,
runtime gate (see :mod:`.governor`) — resolution answers *may this pack ever
run here*, the governor answers *may it run RIGHT NOW under budget*.

Pure, dependency-light: takes already-typed descriptor objects + does not touch
the DB. The orchestrator (:mod:`.agency`) loads descriptors from the registry
and hands them here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ...schemas.action_pack import ActionPack, ActionPackRef, PackGovernor
from ...predicates import (
    TARGET_SCOPE_APPLICABILITY_CTX,
    PredicateError,
    PredicateSurface,
    compile_predicate,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PackResolution:
    """The three-way resolution decision for one pack id, for one target.

    ``effective`` is the AND of the three legs. When False, exactly one of the
    ``*_denied`` reasons is set so the caller can stamp the precise cause into a
    governor BLOCK event.
    """

    pack_id: str
    granted: bool          # analyst.action_packs leg
    allowed: bool          # target.allowed_action_packs leg
    applicable: bool       # pack.applies_to_tags / applicability_predicate leg
    reason: str            # human-readable; "ok" when effective
    # The merged (tightening-only) governor for the effective grant — the
    # pack's own governor with any per-binding override applied. None when the
    # pack carries no governor (uncapped).
    governor: PackGovernor | None = None

    @property
    def effective(self) -> bool:
        return self.granted and self.allowed and self.applicable


@dataclass(frozen=True)
class TargetScopeView:
    """The slice of a target a pack's applicability predicate reasons over.

    Built from a :class:`legba.data.schemas.target.TargetDescriptor` by
    :func:`scope_view_from_target` — kept as a flat struct so resolution stays
    decoupled from the (heavy) descriptor import graph in hot paths.
    """

    target_id: str
    tags: list[str] = field(default_factory=list)
    geo: list[str] = field(default_factory=list)
    entity_classes: list[str] = field(default_factory=list)
    domain: str = ""
    abstraction_level: str = "L1"


def scope_view_from_target(target: Any) -> TargetScopeView:
    """Project a TargetDescriptor (or a dict body) into a TargetScopeView."""
    if isinstance(target, dict):
        scope = target.get("scope") or {}
        ident = target.get("identity") or {}
        return TargetScopeView(
            target_id=str(ident.get("id", "")),
            tags=list(scope.get("tags") or []),
            geo=list(scope.get("geo") or []),
            entity_classes=list(scope.get("entity_classes") or []),
            domain=str(scope.get("domain", "")),
            abstraction_level=str(ident.get("abstraction_level", "L1")),
        )
    scope = target.scope
    return TargetScopeView(
        target_id=target.identity.id,
        tags=list(getattr(scope, "tags", []) or []),
        geo=list(getattr(scope, "geo", []) or []),
        entity_classes=list(getattr(scope, "entity_classes", []) or []),
        domain=getattr(scope, "domain", ""),
        abstraction_level=str(getattr(target.identity, "abstraction_level", "L1")),
    )


def _refs_to_map(refs: list[ActionPackRef] | list[dict] | None) -> dict[str, ActionPackRef]:
    """Index a list of ActionPackRef (or their dict form) by pack_id."""
    out: dict[str, ActionPackRef] = {}
    for r in refs or []:
        ref = r if isinstance(r, ActionPackRef) else ActionPackRef.model_validate(r)
        out[ref.pack_id] = ref
    return out


def _merge_governor(
    base: PackGovernor | None,
    override: PackGovernor | None,
) -> PackGovernor | None:
    """Apply a per-binding governor override (TIGHTENING ONLY) onto the pack's.

    Per the schema contract (``ActionPackRef.governor_override`` — "tightening
    only"): for each cap the EFFECTIVE limit is the MORE RESTRICTIVE of the two
    (the smaller numeric cap; an override may not loosen a pack's own cap). A
    None on one side defers to the other. ``budget_account`` follows the
    override when set (re-targeting the ledger account is not a loosening).
    """
    if base is None and override is None:
        return None
    if override is None:
        return base
    if base is None:
        # Nothing to tighten against — the override stands as the only cap.
        return override

    def _tighter(a: int | float | None, b: int | float | None):
        vals = [v for v in (a, b) if v is not None]
        return min(vals) if vals else None

    return PackGovernor(
        budget_account=override.budget_account or base.budget_account,
        max_invocations_per_hour=_tighter(
            base.max_invocations_per_hour, override.max_invocations_per_hour
        ),
        max_cost_usd_per_day=_tighter(
            base.max_cost_usd_per_day, override.max_cost_usd_per_day
        ),
        max_sources_per_window=_tighter(
            base.max_sources_per_window, override.max_sources_per_window
        ),
        crawl_max_depth=_tighter(base.crawl_max_depth, override.crawl_max_depth),
        crawl_max_pages=_tighter(base.crawl_max_pages, override.crawl_max_pages),
        api_rate_per_minute=_tighter(
            base.api_rate_per_minute, override.api_rate_per_minute
        ),
    )


def _is_applicable(pack: ActionPack, scope: TargetScopeView) -> tuple[bool, str]:
    """Evaluate the pack's applicability against a target scope.

    Two sub-gates, ANDed only when BOTH are declared:
      * ``applies_to_tags`` — at least one tag must overlap the target's scope
        tags (a tag-scoped pack with no overlap is not applicable). An empty
        ``applies_to_tags`` means "no tag constraint".
      * ``applicability_predicate`` — a Starlark predicate over the target
        scope (same TARGET_SCOPE surface the schema compiles it against). Fails
        CLOSED on a runtime error (an un-evaluable predicate denies — the
        hard-gate must never fail open).

    A pack with neither constraint is universally applicable.
    """
    if pack.applies_to_tags:
        if not (set(pack.applies_to_tags) & set(scope.tags)):
            return False, (
                f"pack tags {sorted(pack.applies_to_tags)} do not overlap "
                f"target scope tags {sorted(scope.tags)}"
            )

    if pack.applicability_predicate:
        ctx = {
            "target": {
                "id": scope.target_id,
                "scope_geo": scope.geo,
                "scope_entity_classes": scope.entity_classes,
                "tags": scope.tags,
                "domain": scope.domain,
                "abstraction_level": scope.abstraction_level,
            }
        }
        try:
            # Compile against the ctx this site actually feeds (the
            # target-side keys above; declared as
            # predicates.TARGET_SCOPE_APPLICABILITY_CTX) so a signal-helper
            # predicate is a loud refusal here, not a silent never-match.
            compiled = compile_predicate(
                pack.applicability_predicate,
                PredicateSurface.TARGET_SCOPE,
                ctx_contract=TARGET_SCOPE_APPLICABILITY_CTX,
            )
            if not compiled.evaluate(ctx):
                return False, "applicability_predicate evaluated false"
        except PredicateError as exc:
            # Fail closed — an un-compilable or un-evaluable applicability
            # gate denies (PredicateError covers compile refusals, budget
            # breaches, and runtime helper errors alike).
            logger.warning(
                "action_pack %s applicability predicate failed for target %s: %s",
                pack.identity.id, scope.target_id, exc,
            )
            return False, f"applicability_predicate raised: {exc}"

    return True, "ok"


def resolve_pack(
    *,
    pack: ActionPack,
    analyst_grants: list[ActionPackRef] | list[dict] | None,
    target_allows: list[ActionPackRef] | list[dict] | None,
    scope: TargetScopeView,
) -> PackResolution:
    """Resolve ONE pack's effective agency for one analyst×target context.

    See module docstring for the three legs. The governor returned on an
    effective resolution is the pack's governor merged with the more-restrictive
    of the analyst-grant and target-allow per-binding overrides.
    """
    pack_id = pack.identity.id
    grants = _refs_to_map(analyst_grants)
    allows = _refs_to_map(target_allows)

    granted = pack_id in grants
    allowed = pack_id in allows
    applicable, app_reason = _is_applicable(pack, scope)

    if not granted:
        return PackResolution(
            pack_id=pack_id, granted=False, allowed=allowed,
            applicable=applicable,
            reason=f"analyst does not grant pack {pack_id!r}",
        )
    if not allowed:
        return PackResolution(
            pack_id=pack_id, granted=True, allowed=False,
            applicable=applicable,
            reason=f"target does not allow pack {pack_id!r}",
        )
    if not applicable:
        return PackResolution(
            pack_id=pack_id, granted=True, allowed=True, applicable=False,
            reason=f"pack {pack_id!r} not applicable: {app_reason}",
        )

    # Effective. Merge governor: pack's own → tighten by target override →
    # tighten by analyst override (both are tightening-only per the schema).
    gov = pack.governor
    gov = _merge_governor(gov, allows[pack_id].governor_override)
    gov = _merge_governor(gov, grants[pack_id].governor_override)
    return PackResolution(
        pack_id=pack_id, granted=True, allowed=True, applicable=True,
        reason="ok", governor=gov,
    )


__all__ = [
    "PackResolution",
    "TargetScopeView",
    "resolve_pack",
    "scope_view_from_target",
]
