# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult kind's PROVIDER pack bindings (C-1b; generalised by C-2a §7).

``runtime/dapr_host.py`` builds the consult kind's ``substrate_read`` binding
inline and sits two lines under its size ceiling, so every further pack
binding is built here and the host makes one call:
:func:`build_provider_bindings` returns ``{pack_id: binding}`` for each tool
provider (``analysts/consult_tool_providers``) whose pack the consult
descriptor GRANTS — ``workstation_drive`` (C-1b, present a case) first.

Each binding mirrors the substrate one exactly — the same agency, the same
global scope, the grant leg from the descriptor's own ``action_packs``, and a
self-allow for THAT pack only (documented in each pack descriptor) — and is
re-pointed per request by :meth:`AgencyToolBinding.for_request`, so every
ledger row an armed turn lands names the request it came from.

Bound only when the consult descriptor GRANTS the pack: a descriptor without
the grant gets no entry and an armed request then fails loud (the provider's
``unavailable`` words, e.g. ``case_unavailable``). A grant whose pack is not
registered is logged at ERROR and also yields no entry — the plain consult must
keep working while the operator registers the pack, and the armed path refuses
by name rather than answering plainly.
"""

from __future__ import annotations

import logging
from typing import Any

from ..data.workstation_vocabulary import WORKSTATION_DRIVE_PACK_ID

logger = logging.getLogger(__name__)

__all__ = ["build_drive_binding", "build_pack_binding", "build_provider_bindings"]


async def build_pack_binding(
    substrate_binding: Any, pack_id: str, *, registry_client: Any, analyst: Any,
) -> Any | None:
    """The ``pack_id`` binding beside ``substrate_binding``, or None."""
    from ..data.analysts.agency.binding import (
        AgencyToolBinding,
        fetch_action_pack,
        grants_include,
    )
    from ..data.schemas.action_pack import ActionPackRef

    if substrate_binding is None:
        return None
    grants = substrate_binding.analyst_grants
    if not grants_include(grants, pack_id):
        return None
    pack = await fetch_action_pack(registry_client, pack_id)
    if pack is None:
        logger.error(
            "consult_bindings.%s_unavailable analyst=%s — the descriptor grants "
            "the pack but it is not registered (bringup_register_action_packs); "
            "turns armed for it will refuse until it is",
            pack_id, getattr(getattr(analyst, "identity", None), "id", "?"),
        )
        return None
    return AgencyToolBinding(
        agency=substrate_binding.agency,
        pack=pack,
        pg_pool=substrate_binding.pg_pool,
        tool_context=_context_for(pack_id, substrate_binding.tool_context, registry_client),
        analyst_grants=grants,
        # The global consult surface has no target in context; the pack is
        # allowed by construction (documented in the pack descriptor).
        target_allows=[ActionPackRef(pack_id=pack_id)],
        scope=substrate_binding.scope,
        requested_by=substrate_binding.requested_by,
        budget_account=substrate_binding.budget_account,
    )


def _context_for(pack_id: str, ctx: Any, registry_client: Any) -> Any:
    """The ToolContext a provider pack's handlers see (the substrate one by default).

    ``consult_write`` (C-2b-1) additionally carries the thin client of the
    registry's write executor — on THAT binding only.
    """
    from ..data.consult_write_vocabulary import CONSULT_WRITE_PACK_ID

    if pack_id != CONSULT_WRITE_PACK_ID:
        return ctx
    from dataclasses import replace

    from .consult_writes_client import ConsultWritesClient

    return replace(ctx, consult_writes=ConsultWritesClient(registry_client))


async def build_drive_binding(
    substrate_binding: Any, *, registry_client: Any, analyst: Any,
) -> Any | None:
    """The ``workstation_drive`` binding beside ``substrate_binding``, or None."""
    return await build_pack_binding(
        substrate_binding, WORKSTATION_DRIVE_PACK_ID,
        registry_client=registry_client, analyst=analyst,
    )


async def build_provider_bindings(
    substrate_binding: Any, *, registry_client: Any, analyst: Any,
) -> dict[str, Any]:
    """``{pack_id: binding}`` for every provider pack the descriptor grants."""
    from ..data.analysts.consult_tool_providers import providers

    out: dict[str, Any] = {}
    for provider in providers():
        binding = await build_pack_binding(
            substrate_binding, provider.pack_id,
            registry_client=registry_client, analyst=analyst,
        )
        if binding is not None:
            out[provider.pack_id] = binding
    return out
