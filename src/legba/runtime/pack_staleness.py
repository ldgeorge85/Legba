# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which action-pack versions one cached analyst deps entry binds (RIDER #236,
W11-F161), a leaf beside ``dapr_actors``.

``dapr_actors.warn_stale_pack_deps`` compares these against each pack's head
every periodic resync and WARNs on drift (``dapr_actors.pack_staleness.stale``).
It used to read only the GATHER bindings (``gather_binding`` /
``gather_write_bindings`` / ``escalation``). The consult binding lives on the
kind deps instead (``kind_deps.agency_binding`` plus the per-pack
``kind_deps.provider_bindings``), so on 2026-11-05 ``consult_default`` served
``substrate_read`` 2fc80dc2 for hours after the walker minted 4552f78e and the
sweep never named it. Both are read here now, on ``kind_deps`` and on
``fallback_kind_deps``. ``deep_consult`` is not cached: its workflow activities
fetch the pack fresh per stage (``deep_consult_workflow``), so it cannot drift.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["cached_pack_refs"]


def _stamp(out: dict[str, str], binding: Any) -> None:
    pack = getattr(binding, "pack", None)
    identity = getattr(pack, "identity", None)
    pack_id = getattr(identity, "id", None)
    version = getattr(identity, "version", None)
    if pack_id and version:
        out[str(pack_id)] = str(version)


def _stamp_kind_deps(out: dict[str, str], kind_deps: Any) -> None:
    if kind_deps is None:
        return
    _stamp(out, getattr(kind_deps, "agency_binding", None))
    providers = getattr(kind_deps, "provider_bindings", None)
    if isinstance(providers, Mapping):
        for binding in providers.values():
            _stamp(out, binding)


def cached_pack_refs(deps: Any) -> dict[str, str]:
    """Collect ``{pack_id: cached_version}`` baked into one cached deps entry:
    the GATHER bindings (``gather_binding`` / ``gather_write_bindings`` /
    ``escalation``) and the consult bindings on ``kind_deps`` /
    ``fallback_kind_deps`` (``agency_binding`` + ``provider_bindings``).
    Best-effort attribute probing — a binding shape this function doesn't
    recognize is silently skipped, never raised (this is a diagnostic sweep,
    not a correctness gate)."""
    out: dict[str, str] = {}
    _stamp(out, getattr(deps, "gather_binding", None))
    write = getattr(deps, "gather_write_bindings", None) or {}
    bindings = write.get("bindings") if isinstance(write, Mapping) else None
    for binding in (bindings or {}).values():
        _stamp(out, binding)
    escalation = getattr(deps, "escalation", None)
    if escalation is not None:
        _stamp(out, getattr(escalation, "binding", None))
    _stamp_kind_deps(out, getattr(deps, "kind_deps", None))
    _stamp_kind_deps(out, getattr(deps, "fallback_kind_deps", None))
    return out
