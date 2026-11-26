# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime wiring for the contract-v2 desk SHADOW.

:func:`build_rows_shadow` is called by the ``inline_target`` deps builder for
every desk. It returns ``None`` — and the desk runs exactly as before — unless
``LEGBA_DESK_ROWS_SHADOW`` names this analyst. When it does, it resolves, ONCE
per deps build:

* the two seat prompts from the registry's copies of the draft descriptors
  ``desk_rows_v2_seat`` (the row contract) and ``statement_verifier_seat``
  (the runtime image carries no ``descriptors/``), the row contract's
  ``<<BOUNDED_QUESTION>>`` filled from the desk's own ``method.bounded_question``;
* the seat handler on ``llm.primary.openai_compat`` UNBOUND (each shadow call
  states its own thinking choice), refused unless it is the unpriced core
  route (:func:`statement_seats.assert_core_route`).

Any failure — the switch names a non-desk, a seat descriptor is not registered,
the route is not the core plane — is LOGGED at warning with its reason and
yields ``None``: the shadow is off for that desk and the desk is untouched.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from ..data.desk_rows_shadow import (
    SEAT_ID,
    DeskRowsShadow,
    ShadowPrompts,
    rows_system_prompt,
    shadow_enabled_for,
)
from ..data.statement_seats import SEAT_COMPONENT, VERIFIER_SEAT_ID, RouteFacts, assert_core_route

logger = logging.getLogger(__name__)


def _prompt(row: dict[str, Any] | None) -> str | None:
    body = (row or {}).get("body") if isinstance(row, dict) else None
    body = body if isinstance(body, dict) else (row if isinstance(row, dict) else {})
    method = body.get("method") if isinstance(body.get("method"), dict) else {}
    text = method.get("system_prompt")
    return text if isinstance(text, str) and text.strip() else None


async def build_rows_shadow(
    descriptor: Any,
    *,
    registry_client: Any,
    resolve_component: Callable[[str], Awaitable[Any]],
    pg_pool: Any,
) -> DeskRowsShadow | None:
    """The desk's shadow, or ``None`` (switch off, or unavailable — logged)."""
    analyst_id = str(descriptor.identity.id)
    if not shadow_enabled_for(analyst_id):
        return None
    reason = None
    try:
        if str(getattr(descriptor.identity.kind, "value", descriptor.identity.kind)) != "inline_target":
            reason = "not_a_desk"
        elif pg_pool is None or registry_client is None:
            reason = "no_pool_or_registry"
        else:
            rows = _prompt(await registry_client.get_descriptor(SEAT_ID, family="analyst"))
            verifier = _prompt(await registry_client.get_descriptor(VERIFIER_SEAT_ID, family="analyst"))
            if rows is None or verifier is None:
                reason = "seat_descriptor_not_registered"
            else:
                llm = await resolve_component(SEAT_COMPONENT)
                from .analyst_deps_builder import infer_llm_subprovider

                cfg = getattr(llm, "_cfg", None)
                endpoint = str(getattr(getattr(cfg, "api_endpoint", None), "raw", "") or "")
                assert_core_route(RouteFacts(
                    component_id=SEAT_COMPONENT,
                    subprovider=infer_llm_subprovider(SEAT_COMPONENT, endpoint=endpoint),
                    model_name=str(getattr(getattr(cfg, "model_name", None), "raw", "") or ""),
                    component_price=llm._component_price() if hasattr(llm, "_component_price") else None,
                    price_table_entries=len(getattr(type(llm), "PRICE_TABLE", {}) or {}),
                ))
                question = getattr(descriptor.method, "bounded_question", None)
                return DeskRowsShadow(llm, pg_pool, ShadowPrompts(rows_system_prompt(rows, question), verifier),
                                      analyst_id, question)
    except Exception as exc:  # noqa: BLE001 — the desk must never notice
        reason = f"{type(exc).__name__}"
    logger.warning("desk_rows_shadow.unavailable analyst=%s reason=%s — the switch names this desk but "
                   "its shadow is OFF", analyst_id, reason)
    return None


__all__ = ["build_rows_shadow"]
