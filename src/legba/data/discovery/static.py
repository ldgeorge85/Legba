# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static-target shortcut (L-183) — descriptors without a ``discovery`` block.

Per L-106 §4.3: **a target descriptor with no `discovery` field is *not*
a degenerate one-candidate discovery.** It bypasses the discovery
pipeline entirely. The registry stores the descriptor as-is; the runtime
materializes exactly one target instance whose body equals the
descriptor itself. No state row, no diff, no relabel.

The right default for:

  * Handcrafted high-value targets
    (``target.legba.south_china_sea_monitor``,
    ``target.travis.acme_corp_perimeter``).
  * L3 deployment-specific instances.
  * One-off investigations / experiments.

This module exists to (a) document the pattern formally and (b) provide
a thin programmatic surface so the registry materialization loop (L-181
/ L-182) can call ``materialize_static(...)`` uniformly with the
``materialize_discovered(...)`` path it builds for discovery kinds. Same
call site, two leaves — keeps the kind-dispatcher pattern symmetric.

Public surface:

  * :func:`is_static_descriptor` — predicate that returns True iff the
    descriptor has no ``discovery`` block. Used by the registry to route.
  * :func:`materialize_static` — pass-through that returns the single
    materialized identity for a static descriptor. Wraps the input in a
    :class:`StaticMaterialization` carrier matching the shape the
    discovery materialization loop returns.

The handler-side ``DiscoveryKind`` Protocol is intentionally *not*
implemented here — static targets sidestep the protocol entirely. The
discovery registry (see :mod:`legba.data.discovery.registry`) carries a
sentinel entry under :data:`STATIC_KIND_NAME` so dispatchers asking
"what kind handles descriptors with no discovery block?" receive a
typed answer rather than a None.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


STATIC_KIND_NAME = "static_target_shortcut"
"""Conventional kind name for the no-op static path. Mirrored in
:data:`legba.data.discovery._contract.RELABEL_ACTIONS`-style style —
the registry refuses to register a descriptor with
``discovery.kind = static_target_shortcut`` because static targets are
exactly those *without* a discovery block. The name exists so the
discovery-kind registry dispatch table can carry a sentinel that
forwards to :func:`materialize_static`.
"""


# ---------------------------------------------------------------------------
# StaticMaterialization
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StaticMaterialization:
    """The single materialized target descriptor a static path emits.

    Parallel to the chain a discovery kind walks: discovery handler →
    CandidateTarget → relabel chain → materialized target. For static
    descriptors the chain collapses to identity:

      * ``natural_key`` is the descriptor id (the operator-chosen
        target id, not a discovery-internal handle).
      * ``materialized_body`` is the descriptor body verbatim — no
        substitution, no relabel.
      * ``dropped`` is always False (no filter step).

    Discovery-side materialization callers can branch on
    ``isinstance(result, StaticMaterialization)`` to apply the no-state
    path (no discovery_state row written, no diff bookkeeping).
    """

    natural_key: str
    descriptor_id: str
    materialized_body: Mapping[str, Any]
    dropped: bool = False
    dropped_reason: str = ""
    # Carried for symmetry with the discovery materialization carrier so
    # registry-side code can write a consistent ``discovered_from`` /
    # ``discovery_id`` column. For static targets ``discovered_from``
    # is None — the target is its own source-of-truth.
    discovered_from: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def kept(self) -> bool:
        return not self.dropped


# ---------------------------------------------------------------------------
# is_static_descriptor
# ---------------------------------------------------------------------------


def is_static_descriptor(descriptor: Any) -> bool:
    """Return True iff ``descriptor`` has no active ``discovery`` block.

    Works against either a pydantic ``TargetDescriptor`` or a raw
    dict body (the two shapes the registry accepts at materialization
    time). The discriminator is the *absence* of a discovery block, or
    a discovery block whose ``kind`` field is missing / empty / equal
    to :data:`STATIC_KIND_NAME`.

    This is intentionally a Python-level predicate, not a schema field:
    the schema's ``discovery: DiscoveryBlock | None = None`` field
    already encodes the surface. Callers shouldn't need to know whether
    the block was omitted or set to ``None``; both are static.
    """
    if descriptor is None:
        return True

    # Pydantic model branch.
    if hasattr(descriptor, "discovery"):
        block = getattr(descriptor, "discovery")
        if block is None:
            return True
        kind = getattr(block, "kind", None)
        if not kind:
            return True
        if kind == STATIC_KIND_NAME:
            return True
        return False

    # Raw dict branch.
    if isinstance(descriptor, Mapping):
        block = descriptor.get("discovery")
        if not block:
            return True
        if not isinstance(block, Mapping):
            return True
        kind = block.get("kind")
        if not kind:
            return True
        if kind == STATIC_KIND_NAME:
            return True
        return False

    # Unknown shape — be conservative: treat as static so the registry
    # falls through to the no-op path rather than crashing the
    # materialization loop. The schema validator catches malformed
    # descriptors before they hit this function in normal flow.
    return True


# ---------------------------------------------------------------------------
# materialize_static
# ---------------------------------------------------------------------------


def materialize_static(descriptor: Any) -> StaticMaterialization:
    """Pass-through materialization for a static target descriptor.

    Returns the single :class:`StaticMaterialization` carrier whose
    ``materialized_body`` equals the descriptor body. The registry
    persists this row identically to a discovery-emitted descriptor
    (with ``discovered_from = None``), so downstream consumers don't
    have to branch on static-vs-discovered when they read.

    The caller is expected to have already validated the descriptor via
    pydantic before calling here; this function does no validation.
    """
    if not is_static_descriptor(descriptor):
        # Programming error — the caller routed a discovery-bearing
        # descriptor into the static path. Raise rather than silently
        # producing a misleading materialization.
        raise ValueError(
            "materialize_static called on a descriptor with an active "
            "discovery block; route through the discovery kind dispatcher "
            "instead"
        )

    # Pydantic-model branch.
    if hasattr(descriptor, "model_dump"):
        body = descriptor.model_dump(mode="json", by_alias=True)
        descriptor_id = getattr(
            getattr(descriptor, "identity", None), "id", None
        ) or body.get("identity", {}).get("id", "")
    elif isinstance(descriptor, Mapping):
        # Defensive deep-copy — callers shouldn't mutate the returned
        # body and expect the descriptor to stay intact.
        body = dict(descriptor)
        descriptor_id = body.get("identity", {}).get("id", "")
    else:
        raise TypeError(
            f"materialize_static expects a TargetDescriptor or dict, "
            f"got {type(descriptor).__name__}"
        )

    if not descriptor_id:
        raise ValueError(
            "static descriptor missing identity.id; cannot materialize"
        )

    return StaticMaterialization(
        natural_key=descriptor_id,
        descriptor_id=descriptor_id,
        materialized_body=body,
        dropped=False,
        discovered_from=None,
    )


__all__ = [
    "STATIC_KIND_NAME",
    "StaticMaterialization",
    "is_static_descriptor",
    "materialize_static",
]
