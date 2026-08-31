# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wire the ``web_access`` action pack onto the ``standing_auditor`` sub-handler.

WHY THIS IS A MODULE AND NOT TEN LINES IN THE BUILDER. ``dapr_host`` already
builds a ``web_access`` :class:`AgencyToolBinding`, but only for an in-actor
GATHER kind — the gate is ``method.kind == llm_planner`` over
``_GATHER_KINDS``, because a GATHER binding exists to feed a model's tool loop.
The standing auditor is a ``deterministic`` sub-handler that calls exactly ONE
pack tool from code, on a fixed schedule, with a code-built query. It wants the
pack, not the loop. Generalizing ``_GATHER_KINDS`` to admit a deterministic kind
would hand every future deterministic analyst a planner-shaped surface it has no
use for; adding the wiring inline in ``analyst_deps_builder`` would spend most of
that module's remaining headroom under the size gate. So the binding is built
here, next to its rationale, and the builder calls it in one branch — the same
shape as ``_wire_corpus_indexer_os`` / ``_wire_signal_embedder``.

WHAT IT PRESERVES. Everything the GATHER path enforces, because it is the same
object: the analyst's ``action_packs`` GRANT is passed as ``analyst_grants``, so
a descriptor that does not grant ``web_access`` gets no binding at all; the
governor's budget account is the analyst id; the invocation ledger and the
``action_pack_invocations`` receipt land exactly as they do for any other pack
caller. What it does NOT do is invent an allow leg: a META analyst has no
running target, so the binding runs under ``GLOBAL_SCOPE`` with
``target_allows=None`` — identical to the META GATHER path's self-allow
(``actor_output_emit._gather_binding_for_target``, the journal_assessor and
corpus_researcher precedent).

DEGRADE-NOT-BREAK, WITH ONE EXCEPTION IN SPIRIT. Every failure here returns
``deps`` unmodified and logs at WARNING (a missing agency plane, an unregistered
pack, a registry hiccup). It does NOT fail deps-build. That looks like the
silent bypass this codebase usually refuses, and it is not: the handler cannot
run a single search without the binding, so the gap surfaces LOUDLY one layer
down as a heartbeat row carrying ``degraded_reason="no web_access binding
wired"`` — an observable, operator-actionable state that outlives the log line a
build failure would produce.
"""

from __future__ import annotations

import logging
from dataclasses import replace as _dc_replace
from typing import Any

logger = logging.getLogger(__name__)


async def wire_standing_auditor_web_pack(
    descriptor: Any,
    deps: Any,
    *,
    registry_client: Any,
) -> Any:
    """Merge a ``web_access`` :class:`AgencyToolBinding` into ``deps.extras``.

    Returns the (replaced) deps, or ``deps`` unchanged on any gap. Imports are
    function-local for the same reason the host's are: the agency plane pulls in
    the whole action-pack stack, and a slim image that never binds an analyst
    must not pay for it at import time.
    """
    from ..data.analysts.agency.binding import AgencyToolBinding, fetch_action_pack
    from ..data.analysts.agency.tools import ToolContext
    from ..data.analysts.agency.web_tools import WEB_ACCESS_PACK_ID
    from ..data.analysts.deterministic_handlers.standing_auditor import (
        WEB_BINDING_DEPS_EXTRA_KEY,
    )
    from ..data.schemas.action_pack import ActionPackRef
    from .source_first_runtime import AGENCY_HOLDER

    analyst_id = getattr(getattr(descriptor, "identity", None), "id", "?")

    # The GRANT leg. A descriptor that does not declare the pack gets nothing —
    # silently and correctly, since not granting it is a valid configuration.
    grants = [
        r.model_dump() if hasattr(r, "model_dump") else r
        for r in (getattr(descriptor, "action_packs", None) or [])
    ]
    if not any(
        (g.get("pack_id") if isinstance(g, dict) else getattr(g, "pack_id", None))
        == WEB_ACCESS_PACK_ID
        for g in grants
    ):
        return deps

    agency = AGENCY_HOLDER.get("agency")
    base_ctx = AGENCY_HOLDER.get("tool_context")
    pool = getattr(deps, "pg_pool", None)
    if agency is None or base_ctx is None or pool is None:
        logger.warning(
            "external_audit_binding.unbindable analyst=%r agency=%s ctx=%s "
            "pool=%s — the auditor grants web_access but the agency plane is "
            "not up; it will run and report an unaudited heartbeat",
            analyst_id, agency is not None, base_ctx is not None,
            pool is not None,
        )
        return deps

    try:
        pack = await fetch_action_pack(registry_client, WEB_ACCESS_PACK_ID)
    except Exception as exc:
        logger.warning(
            "external_audit_binding.pack_fetch_failed analyst=%r err=%s",
            analyst_id, exc,
        )
        return deps
    if pack is None:
        logger.warning(
            "external_audit_binding.pack_missing analyst=%r pack=%s — register "
            "it (scripts/bringup_register_action_packs.py) or drop the grant",
            analyst_id, WEB_ACCESS_PACK_ID,
        )
        return deps

    binding = AgencyToolBinding(
        agency=agency,
        pack=pack,
        pg_pool=pool,
        # The search PROVIDER + its liveness cache are carried straight off the
        # process-wide tool context the host assembled at bring-up, so this
        # analyst shares ONE provider, ONE control-probe budget and ONE deferral
        # ladder with every other search caller — never its own second stack.
        tool_context=ToolContext(
            queue=getattr(base_ctx, "queue", None),
            emit=getattr(base_ctx, "emit", None),
            search=getattr(base_ctx, "search", None),
            search_route=getattr(base_ctx, "search_route", None),
            search_liveness=getattr(base_ctx, "search_liveness", None),
        ),
        analyst_grants=grants,
        # META: there is no running target, so there is no
        # ``allowed_action_packs`` list to intersect the grant with — and
        # ``target_allows=None`` resolves to allowed=False, which would deny
        # every call at the allow leg and leave the auditor silently toolless.
        # So the binding SELF-ALLOWS its own pack under the default
        # GLOBAL_SCOPE, exactly as the META GATHER path does
        # (``actor_output_emit._gather_binding_for_target``, target_id=None —
        # the journal_assessor / corpus_researcher precedent). The GRANT leg
        # stays real: an analyst that does not declare web_access returned
        # above, before this line.
        target_allows=[ActionPackRef(pack_id=WEB_ACCESS_PACK_ID)],
        requested_by=f"analyst::{analyst_id}",
        budget_account=str(analyst_id),
    )
    merged = {**dict(getattr(deps, "extras", None) or {}),
              WEB_BINDING_DEPS_EXTRA_KEY: binding}
    logger.info(
        "external_audit_binding.wired analyst=%r pack=%s search_bound=%s",
        analyst_id, WEB_ACCESS_PACK_ID,
        getattr(base_ctx, "search", None) is not None,
    )
    return _dc_replace(deps, extras=merged)


__all__ = ["wire_standing_auditor_web_pack"]
