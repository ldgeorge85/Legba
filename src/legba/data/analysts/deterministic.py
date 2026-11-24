# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.analysts.deterministic — L-173 deterministic analyst kind.

Realizes the L-006 B/A/C/D sub-splits (see ``plans/design/legba_analysis_subsplit.md``)
as four sub-handlers under one analyst kind. The kind itself is the
dispatcher; per-run, the bound descriptor's ``options.sub_handler`` selects
which sub-handler runs.

Contract (per ``plans/design/legba_kind_contracts.md`` §5):

  * ``KIND_NAME = "deterministic"`` — registered against the analyst-kind
    namespace at host start.
  * ``async def run_method(inputs, options, deps) -> AnalystMethodResult`` —
    the entry point the runtime calls per analyst-actor run. The runtime
    in :mod:`legba.runtime.dapr_actors` invokes the bound ``run_method`` as
    ``await deps_bundle.run_method(inputs, options)``; the host wraps this
    function with the activation-time ``deps`` (a ``StandardDeps`` bundle)
    via ``functools.partial`` so the wire-level signature stays 2-arg while
    the kind-internal signature carries deps explicitly.

Differentiating this from LLM-bearing kinds (``inline_target``,
``cross_target_raw``, ``meta_findings_synthesizer``, etc.):

  * **No LLM calls.** All work is pure Python over already-materialized
    substrate slices: networkx for graph mining, scipy/numpy for anomaly
    stats. Token usage is always zeroed.
  * **Structured outputs.** Each sub-handler emits a typed payload whose
    ``data`` field carries the structured result (community ids, anomaly
    scores, Brier values, etc.). ``body`` is a short human-readable
    summary, never a model-generated narrative.

Sub-handlers
------------

================== ==================================================
``graph_mining``    Community detection, structural-balance triads
                    (passthrough — see ``structural_balance`` for the
                    standalone variant), proxy-chain mining over the
                    Apache AGE knowledge graph (``legba_graph``).
``anomaly_detection`` Signal-volume rate-spikes, sentiment-shift
                    z-scores, novel-entity emergence over recent
                    ``time_bucket()`` windows on the primary Postgres pool.
``structural_balance`` Signed-edge triadic balance on the entity-
                    relationship graph (AlliedWith / HostileTo /
                    HostileTo via reified-Nexus intent).
``calibration_tracking`` Analyst confidence-vs-outcome tracking —
                    Brier score, rolling reliability bins, drift
                    detection across windows.
================== ==================================================

Each sub-handler is a separate module under ``deterministic_handlers/`` so
the optimizer (L-176) can iterate on one without disturbing the others, and
so descriptors that need only one sub-handler can import the symbol
directly without paying the others' import cost.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from ..provenance.kinds import TRACE_ONLY, OutputKind
from ..provenance.models import FindingPayload
from ...runtime.analyst_method import AnalystMethodResult
from ..type_registry import load_implementations as _load_implementations
from ..type_registry import default_registry as _default_registry

logger = logging.getLogger(__name__)

KIND_NAME: str = "deterministic"

# Default OUTPUT_KIND for deterministic — the sub-handlers that produce a
# genuine analytical FINDING write a FindingPayload-shaped row; maintenance
# sub-handlers whose REAL product is side-written are ``TRACE_ONLY`` and emit no
# analyst_outputs receipt (the run is fully audited in ``analyst_traces``). The
# dispatcher (the actor's output-dispatch chokepoint in
# :mod:`legba.runtime.dapr_actors`) resolves the effective kind per-run via
# ``OUTPUT_KIND_BY_SUB_HANDLER.get(sub_handler, OUTPUT_KIND)``.
#
# R-1: both tables are DERIVED from the type registry
# (``legba.data.type_registry.deterministic_handlers`` — one declaration module
# per handler, carrying its ``output`` and the rationale that used to live in
# comments here). Adding a handler is a new handler module plus a new
# declaration module; nothing in this file changes.
OUTPUT_KIND: OutputKind = OutputKind.FINDING


def output_kinds(registry: Any = None) -> dict[str, object]:
    """``{sub_handler: OutputKind | TRACE_ONLY}`` from the declarations."""
    reg = registry or _default_registry()
    return {
        d.id: (TRACE_ONLY if d.output == "trace_only" else OutputKind(d.output))
        for d in reg.types("deterministic_handler")
    }


def dispatch_table(registry: Any = None) -> dict[str, Any]:
    """``{sub_handler: handle}``: every declared handler module imported now.
    Any that fails to import, or lacks a callable ``handle``, stops the call
    with every failure named — never a quieter table."""
    reg = registry or _default_registry()
    return _load_implementations(
        reg.types("deterministic_handler"), registry="deterministic_handler",
    )


OUTPUT_KIND_BY_SUB_HANDLER: dict[str, object] = output_kinds()

# READ_SLICE defaults to the signals reader — graph_mining + anomaly +
# structural_balance + calibration_tracking all reason over signals.
READ_SLICE = None

# Sub-handler dispatch table. Keys match the descriptor's
# ``options.sub_handler`` exactly.
SUB_HANDLERS: dict[str, Any] = dispatch_table()


class DeterministicDispatchError(ValueError):
    """Raised when ``options.sub_handler`` is missing or unknown."""


def _resolve_sub_handler_name(options: Mapping[str, Any]) -> str:
    """Pick the sub-handler name out of ``options``.

    Accepts the top-level ``sub_handler`` key. Raises
    :class:`DeterministicDispatchError` if missing or unknown so the
    runtime can route the failure to the trace + output DLQ rather than
    silently no-op.
    """
    name = options.get("sub_handler")
    if not name:
        raise DeterministicDispatchError(
            "deterministic kind requires options['sub_handler'] "
            f"(one of {sorted(SUB_HANDLERS)!r})"
        )
    if name not in SUB_HANDLERS:
        raise DeterministicDispatchError(
            f"unknown deterministic sub_handler {name!r}; "
            f"valid: {sorted(SUB_HANDLERS)!r}"
        )
    return name


async def run_method(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None = None,
) -> AnalystMethodResult:
    """Top-level deterministic-kind dispatcher.

    Parameters
    ----------
    inputs:
        Already-materialized substrate rows the runtime fetched (per the
        descriptor's subscription). Each sub-handler treats these as a
        seed — graph mining may pull additional edges over Apache AGE,
        anomaly detection may pull historical buckets off the primary
        Postgres pool, etc. — but the row list bounds the run.
    options:
        At minimum ``{"sub_handler": "<name>", "analyst_id": ..., "run_id":
        ...}``. Sub-handlers may consume additional keys; see their docs.
    deps:
        :class:`legba.runtime.deps.StandardDeps` (or any object exposing
        ``pg_pool`` etc. for the sub-handler that needs it). ``None`` is
        accepted for unit-test paths that pre-shape inputs and don't
        require live substrate access.

    Returns
    -------
    :class:`AnalystMethodResult`
        ``finding`` is a :class:`FindingPayload` with structured ``data``
        carrying the sub-handler's results. ``usage`` is always
        ``{"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens":
        0}`` because no LLM is invoked.

    Raises
    ------
    DeterministicDispatchError
        Missing or unknown ``options['sub_handler']``.
    """
    sub_handler_name = _resolve_sub_handler_name(options)
    handler = SUB_HANDLERS[sub_handler_name]
    logger.info(
        "analysts.deterministic.dispatch sub_handler=%s analyst_id=%s run_id=%s",
        sub_handler_name,
        options.get("analyst_id"),
        options.get("run_id"),
    )
    result = await handler(inputs, options, deps)
    if not isinstance(result, AnalystMethodResult):
        raise TypeError(
            f"deterministic sub_handler {sub_handler_name!r} returned "
            f"{type(result).__name__}, expected AnalystMethodResult"
        )
    return result


__all__ = [
    "AnalystMethodResult",
    "DeterministicDispatchError",
    "FindingPayload",
    "KIND_NAME",
    "OUTPUT_KIND",
    "OUTPUT_KIND_BY_SUB_HANDLER",
    "READ_SLICE",
    "SUB_HANDLERS",
    "TRACE_ONLY",
    "dispatch_table",
    "output_kinds",
    "run_method",
]
