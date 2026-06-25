# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Filter/enrichment-kind handler contract (L-102 §3) — minimal Protocol surface.

The full runtime context type lives in L-103 (not yet landed). Until then
this module declares the structural-typing surface that filter handlers
depend on:

  * :class:`FilterContext` — what a handler receives at ``transform`` /
    ``health_check`` / lifecycle hooks: descriptor identity + scope hints
    + bound logger.
  * :class:`FilterHealth` — health-probe return.
  * :class:`StreamHandler` — runtime-checkable Protocol for the kind.

This mirrors the sibling :mod:`legba.data.sources._contract` module: a
single context type satisfies both the descriptor identity AND the
scope hints (``scope_geo`` / ``scope_languages``) needed by filters that
want to apply per-target restrictions (e.g., language allow-listing).

All shapes are Pydantic / Protocol — no ABC inheritance — so third-party
handler packages don't need to import a Legba base class to satisfy the
contract.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Mapping, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..sources._contract import Signal


# ---------------------------------------------------------------------------
# FilterContext
# ---------------------------------------------------------------------------


class FilterContext(BaseModel):
    """Per-transform context handed to a filter/enrichment handler.

    Carries the descriptor identity, the target scope hints needed for
    per-target filtering (e.g., :attr:`scope_languages` for language
    allow-lists), and a bound logger.

    When L-103 runtime types land, an adapter will construct one of these
    from the runtime's :class:`RuntimeContext` + :class:`TargetContext` so
    handlers don't need to care which executor they're running under.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    target_id: str
    target_version: str = ""
    filter_id: str = ""
    logger: logging.Logger = Field(
        default_factory=lambda: logging.getLogger("legba.filter"),
        exclude=True,
    )

    # Scope hints — present on the L-102 TargetContext; carried here for
    # handlers that want to filter or restrict by language / geo.
    scope_geo: list[str] = Field(default_factory=list)
    scope_languages: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class FilterHealth(BaseModel):
    """Filter-handler health probe result. Same shape as L-102's
    ``FilterHealth``.

    ``detail`` is kind-specific (language_detect records language-mix
    histograms; dedupe records duplicate-rate; etc.).
    """

    model_config = ConfigDict(extra="forbid")

    state: str = Field(default="healthy")     # healthy | degraded | unhealthy
    last_success_at: datetime | None = None
    last_error: str | None = None
    signals_in_24h: int = 0
    signals_out_24h: int = 0
    signals_dropped_24h: int = 0
    detail: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# StreamHandler protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class StreamHandler(Protocol):
    """Structural-typing surface for filter/enrichment kinds. L-102 §3.

    A concrete handler exposes:

      * ``kind``: ClassVar[str] — registered kind name.
      * ``family``: ClassVar[str] — always ``"filter"``.
      * ``schema_version``: ClassVar[str] — handler schema version.
      * ``config_schema``: ClassVar[type[BaseModel]] — pydantic config type.
      * ``output_contract``: ClassVar[Mapping[str, type]] — names → expected
        Python types the filter adds (or requires) on
        ``Signal.payload``. Used by the registry at pipeline-registration
        time to surface composition errors before activation.
      * ``transform(signal, ctx) -> Signal | None``: yield the mutated
        signal or ``None`` to drop it from the stream.
      * ``health_check(ctx) -> FilterHealth``.

    Lifecycle hooks (``on_configure`` / ``on_activate`` / etc.) are runtime-
    optional; default no-op when omitted.
    """

    kind: str
    family: str
    schema_version: str
    config_schema: type[BaseModel]
    output_contract: Mapping[str, type]

    async def transform(
        self,
        signal: Signal,
        ctx: FilterContext,
    ) -> Signal | None: ...

    async def health_check(self, ctx: FilterContext) -> FilterHealth: ...


__all__ = [
    "FilterContext",
    "FilterHealth",
    "StreamHandler",
]
