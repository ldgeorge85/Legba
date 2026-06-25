# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SourceRef resolution (P-08 / PIVOT §4.4).

A ``TargetDescriptor.sources`` is a ``list[SourceRef]``. Each ``SourceRef`` is
EITHER:

  * **explicit** — ``source_id`` names one source directly; resolution is a
    single head-row lookup in ``source_descriptors``.
  * **selector** — ``source_selector`` is a coarse query over source-descriptor
    SCOPE (tags / geo / languages / kind / owner_tenant + an optional Starlark
    residual over source metadata). It matches ANY source whose advertised
    ``scope`` satisfies it. This is distinct from ``Subscription`` (which
    filters *signals*): the selector decides which SOURCES a target wires to.

Selector matching is done in two stages, mirroring the signal path:
  1. a SQL pre-narrow over the indexed ``source_descriptors`` columns
     (``kind``/``state``), then a structured in-Python scope match over the
     descriptor ``body.scope`` (tags ⊇, geo ∩, languages ∩, tenant);
  2. an optional Starlark residual (``PredicateSurface.ANALYST_SUBSCRIPTION``
     — a descriptor-scoped surface) over the source's scope metadata.

Tenancy: a selector matches sources in its own tenant or ``shared`` only
(the multi-tenant boundary, PIVOT §8). ``allowlist``/``grant`` sources never
auto-wire by selector — they require explicit opt-in — so selector resolution
drops non-``open`` sources (the policy layer also enforces this at registration,
but we never even *propose* a locked source via a selector).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ...data.predicates import (
    PredicateRuntimeError,
    PredicateSurface,
    compile_predicate,
)
from ...data.schemas.source import SourceRef, SourceSelector, Subscription
from .subjects import ResolvedBinding

logger = logging.getLogger(__name__)


# Tenant value that any tenant may subscribe to (PIVOT §8 shared sources).
SHARED_TENANT = "shared"


@dataclass(frozen=True)
class SourceRow:
    """The slice of a source_descriptors head row resolution needs."""

    source_id: str
    kind: str
    state: str
    owner_tenant: str
    scope_tags: list[str]
    scope_geo: list[str]
    scope_languages: list[str]
    subscription_policy: str
    allowed_targets: list[str]
    allowed_tenants: list[str]


async def _load_source_heads(pg: Any) -> list[SourceRow]:
    """Load every ACTIVE/head source descriptor's scope + policy.

    Head rows only (``is_head``); we resolve against the live descriptor set.
    The ``body`` JSONB carries the full SourceDescriptor; scope + policy are
    read from it (kind/state are denormalised columns).
    """
    import json

    async with pg.acquire() as conn:
        rows = await conn.fetch(
            "SELECT descriptor_id, kind, state, body "
            "FROM source_descriptors WHERE is_head"
        )
    out: list[SourceRow] = []
    for r in rows:
        body = r["body"]
        if isinstance(body, (str, bytes, bytearray)):
            body = json.loads(body)
        scope = (body or {}).get("scope") or {}
        out.append(
            SourceRow(
                source_id=r["descriptor_id"],
                kind=r["kind"],
                state=r["state"],
                owner_tenant=scope.get("owner_tenant", "default"),
                scope_tags=list(scope.get("tags") or []),
                scope_geo=list(scope.get("geo") or []),
                scope_languages=list(scope.get("languages") or []),
                subscription_policy=(body or {}).get("subscription_policy", "open"),
                allowed_targets=list((body or {}).get("allowed_targets") or []),
                allowed_tenants=list((body or {}).get("allowed_tenants") or []),
            )
        )
    return out


def _scope_match(sel: SourceSelector, row: SourceRow, *, target_tenant: str) -> bool:
    """Structured scope match: a source matches a selector iff every
    constraint the selector sets is satisfied by the source's scope.

    Set semantics:
      * ``tags``       — source.scope.tags ⊇ selector.tags (source covers all).
      * ``geo``        — non-empty intersection (source advertises one of them).
      * ``languages``  — non-empty intersection.
      * ``kinds``      — source.kind ∈ selector.kinds.
      * ``owner_tenant`` — exact tenant pin (overrides the default tenancy gate).
    Empty selector fields are unconstrained (match anything).
    """
    # Tenancy gate: a selector matches its own tenant or `shared` only, unless
    # it explicitly pins a tenant.
    if sel.owner_tenant is not None:
        if row.owner_tenant != sel.owner_tenant:
            return False
    else:
        if row.owner_tenant not in (target_tenant, SHARED_TENANT):
            return False

    if sel.tags and not set(sel.tags).issubset(set(row.scope_tags)):
        return False
    if sel.geo and not (set(sel.geo) & set(row.scope_geo)):
        return False
    if sel.languages and not (set(sel.languages) & set(row.scope_languages)):
        return False
    if sel.kinds and row.kind not in sel.kinds:
        return False
    return True


def _residual_match(sel: SourceSelector, row: SourceRow) -> bool:
    """Evaluate the selector's optional Starlark residual over source scope.

    The residual binds on the descriptor-scoped surface (helpers read a
    ``target`` ctx — here we feed the SOURCE's scope into that slot, since the
    selector reasons about the source descriptor). Fails CLOSED on a residual
    error (a source is not wired if its residual can't be evaluated).
    """
    if not sel.predicate:
        return True
    try:
        compiled = compile_predicate(sel.predicate, PredicateSurface.ANALYST_SUBSCRIPTION)
        ctx = {
            "target": {
                "id": row.source_id,
                "kind": row.kind,
                "scope_geo": row.scope_geo,
                "scope_entity_classes": [],
                "tags": row.scope_tags,
                "abstraction_level": "",
            }
        }
        return compiled.evaluate(ctx)
    except PredicateRuntimeError as exc:
        logger.warning(
            "source_selector residual eval failed for source %s: %s",
            row.source_id, exc,
        )
        return False


async def resolve_source_refs(
    pg: Any,
    *,
    target_id: str,
    target_tenant: str,
    source_refs: list[SourceRef],
) -> list[ResolvedBinding]:
    """Resolve a target's ``list[SourceRef]`` into concrete bindings.

    Explicit refs resolve to exactly one source (head row must exist + be
    non-retired). Selector refs resolve to every ``open`` source whose scope
    matches (structured + residual), in the target's tenant or ``shared``.

    A binding carries the per-ref ``Subscription`` (the signal-level slice) so
    later stages (subject planning, SQL builder, residual eval) have it.

    Locked sources (``allowlist``/``grant``) NEVER auto-wire via selector; an
    explicit ref to a locked source is still RESOLVED here (the policy layer
    decides whether the subscription may register).
    """
    heads = await _load_source_heads(pg)
    by_id = {h.source_id: h for h in heads}
    bindings: list[ResolvedBinding] = []
    seen: set[str] = set()

    for ref in source_refs:
        if ref.source_id is not None:
            row = by_id.get(ref.source_id)
            if row is None:
                logger.warning(
                    "target %s explicit SourceRef -> unknown source %s",
                    target_id, ref.source_id,
                )
                continue
            if row.state == "retired":
                logger.info(
                    "target %s explicit SourceRef -> retired source %s (skipped)",
                    target_id, ref.source_id,
                )
                continue
            if row.source_id not in seen:
                seen.add(row.source_id)
                bindings.append(
                    ResolvedBinding(
                        source_id=row.source_id,
                        owner_tenant=row.owner_tenant,
                        subscription=ref.subscription,
                        via_selector=False,
                    )
                )
        else:
            sel = ref.source_selector
            assert sel is not None  # SourceRef validator guarantees exactly one
            for row in heads:
                if row.state == "retired":
                    continue
                # Selector auto-wire is gated by subscription_policy: only
                # `open` sources attract by selector (PIVOT §4.4.1 / §4.7).
                if row.subscription_policy != "open":
                    continue
                if not _scope_match(sel, row, target_tenant=target_tenant):
                    continue
                if not _residual_match(sel, row):
                    continue
                if row.source_id in seen:
                    continue
                seen.add(row.source_id)
                bindings.append(
                    ResolvedBinding(
                        source_id=row.source_id,
                        owner_tenant=row.owner_tenant,
                        subscription=ref.subscription,
                        via_selector=True,
                    )
                )

    return bindings


__all__ = [
    "SHARED_TENANT",
    "SourceRow",
    "resolve_source_refs",
]
