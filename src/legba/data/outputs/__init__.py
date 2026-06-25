# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.outputs — operator-facing output-kind sinks (L-190 .. L-197+).

Output kinds are the *delivery* counterpart of the analyst-output payload
families in :mod:`legba.data.provenance.models`. Where the provenance
package handles *substrate writes* (rows + NATS event), the outputs
package handles *operator surfaces* — Pushover, XMPP, Matrix, etc.

Each output kind exposes:

  * ``KIND_NAME`` — registered kind name (string).
  * Optional ``async def emit(payload, *, descriptor, deps) -> None``.
    Some kinds (``a2a_skill``, ``mcp_tool``, ``substrate``) expose
    surface helpers instead of a uniform ``emit`` — those are not
    callable through the runtime's analyst-output dispatcher but are
    still discoverable via :func:`discover_output_kinds`.

Sub-output handlers (the actual transport sinks) live under
``alert_sinks/`` etc. and are addressed by name from the kind module so
descriptor-level opt-in/opt-out works at the surface level.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output-handler bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutputHandler:
    """Discovered output-kind metadata.

    Attributes
    ----------
    kind_name:
        The output kind's ``KIND_NAME`` string.
    emit:
        Optional async ``(payload, *, descriptor, deps) -> Any`` callable.
        ``None`` when the kind does not expose a uniform emit surface
        (substrate / a2a_skill / mcp_tool — these expose surface-specific
        wiring functions instead).
    module:
        The Python module object — callers may need ``register_*_route``
        or ``write_*`` helpers exposed by the kind.
    """

    kind_name: str
    emit: Callable[..., Any] | None
    module: Any


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


_OUTPUT_KIND_MODULE_NAMES: tuple[str, ...] = (
    "substrate",
    "nats_stream",
    "a2a_skill",
    "mcp_tool",
    "alert",
    "webhook",
    "stix_bundle",
    "ui_panel",
)


def discover_output_kinds() -> dict[str, OutputHandler]:
    """Walk the package, import every output-kind module, return a registry.

    Each module that exposes a ``KIND_NAME`` is included; the ``emit``
    slot is populated when the kind exposes a module-level
    ``async def emit(payload, *, descriptor, deps)`` (currently the
    ``alert`` and ``nats_stream`` kinds).  Surface-oriented kinds
    (``substrate``, ``a2a_skill``, ``mcp_tool``) carry ``emit = None``.

    Returns a dict keyed by ``KIND_NAME`` mapping to an
    :class:`OutputHandler` bundle.
    """
    registry: dict[str, OutputHandler] = {}
    for mod_name in _OUTPUT_KIND_MODULE_NAMES:
        try:
            module = importlib.import_module(f"{__name__}.{mod_name}")
        except Exception as exc:                                # pragma: no cover
            logger.warning(
                "outputs.discover.import_failed module=%s err=%s",
                mod_name, exc,
            )
            continue

        kind_name = getattr(module, "KIND_NAME", None)
        if not kind_name:
            logger.warning(
                "outputs.discover.skip module=%s reason=missing_KIND_NAME",
                mod_name,
            )
            continue

        emit = getattr(module, "emit", None)
        if emit is not None and not callable(emit):
            emit = None

        registry[str(kind_name)] = OutputHandler(
            kind_name=str(kind_name),
            emit=emit,
            module=module,
        )

    return registry


# ---------------------------------------------------------------------------
# Eager imports (preserve the wave-A import shape)
# ---------------------------------------------------------------------------


# L-190 — substrate-write surface (canonical persistence path).
from . import substrate
# L-191 — NATS stream output kind.
from . import nats_stream
# L-193 — A2A skill output kind (signed-envelope protocol).
from . import a2a_skill
# L-194 — MCP tool output kind (legba-mcp surface).
from . import mcp_tool
# L-197 — alert output kind (severity-aware operator-facing fan-out).
from . import alert
# L-196 — webhook output kind (signed outbound HTTPS POST).
from . import webhook
# L-195 — STIX 2.1 bundle output kind (TAXII-shaped exporter).
from . import stix_bundle
# L-192 — UI panel output kind (descriptor-driven panel registration).
from . import ui_panel

__all__ = [
    "OutputHandler",
    "a2a_skill",
    "alert",
    "discover_output_kinds",
    "mcp_tool",
    "nats_stream",
    "stix_bundle",
    "substrate",
    "ui_panel",
    "webhook",
]
