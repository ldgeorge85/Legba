# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic sub-handlers for the L-173 ``deterministic`` analyst kind.

One module per sub-handler. The top-level :mod:`legba.data.analysts.deterministic`
dispatcher imports each and routes by ``options.sub_handler``. Sub-handlers
share a single ``async def handle(inputs, options, deps) -> AnalystMethodResult``
contract — see :mod:`.graph_mining` for the canonical reference.

Sub-handlers are intentionally **decoupled from each other** at import time.
Adding a new one means a new module + an entry in
:data:`legba.data.analysts.deterministic.SUB_HANDLERS`; the existing
sub-handlers do not need to know about it.
"""

from __future__ import annotations

from . import (
    adversarial_signals,
    anomaly_detection,
    calibration_tracking,
    cross_source_coalesce,
    cross_source_dedup,
    entity_gc,
    entity_resolution,
    fact_decay,
    finding_supersession,
    graph_mining,
    hypothesis_lifecycle,
    nexus_decay,
    proposed_edge_governance,
    signals_retention,
    structural_balance,
)

__all__ = [
    "adversarial_signals",
    "anomaly_detection",
    "calibration_tracking",
    "cross_source_coalesce",
    "cross_source_dedup",
    "entity_gc",
    "entity_resolution",
    "fact_decay",
    "finding_supersession",
    "graph_mining",
    "hypothesis_lifecycle",
    "nexus_decay",
    "proposed_edge_governance",
    "signals_retention",
    "structural_balance",
]
