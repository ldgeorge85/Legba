# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``analyst_traces_retention`` sub-handler — TTL purge of aged analyst traces.

S-6 (disk-creep remediation). ``analyst_traces`` is the per-run debug/telemetry
receipt table (one row per analyst run: prompt, llm_calls, tool_calls,
output_payload, receipt chain). It had NO retention and grew forever
(~470MB / 164k rows, +5.4k/day), feeding the disk pressure that breached
OpenSearch's watermark. The answer MIRRORED the ``signals_retention``
precedent (migration 0036) so closely that this module's own header used to
say so verbatim — which is exactly why the C2 "one janitor" pass (2026-07-28
coherence audit) folded both onto one engine instead of two copies of the
same TTL-purge shape.

This module is now a thin DELEGATE onto the shared :mod:`._retention_sweep`
engine, which executes the ``analyst_traces_retention`` row of the
``retention_policies`` config table (migration 0109). Behavior is
BYTE-IDENTICAL to the pre-consolidation standalone implementation (git
history has the retired body; the shared engine's ``_purge_traces`` /
``_finding_traces`` are that same code, parameterized) — this is a
REGISTRATION SHIM, not a rewrite:

  * ``ttl_days <= 0`` (the DEFAULT, seeded into the policy row) DISABLES the
    purge — the job ships inert until an operator sets a deliberately
    generous positive TTL.
  * Opt-in levers, highest first: an explicit run ``options["ttl_days"]``
    (forced runs / tests) > the descriptor's own ``method.options.ttl_days``,
    which X-1 made real — a CADENCE fire now carries the descriptor's declared
    options, so this no longer needs a forced run > the
    ``LEGBA_ANALYST_TRACES_TTL_DAYS`` env var (the pre-X-1 path; still honored,
    still the only one costing a container recreate — ff65f78).
  * FK children stay DB-handled (``analyst_critiques.trace_id`` CASCADE,
    ``output_dead_letter.run_id`` SET NULL) — see the shared engine's
    ``_purge_traces`` adapter, which counts the cascade honestly.

CADENCE-HEALTH SAFETY (unchanged): the System Status / telemetry read
(``runtime_telemetry_api``) aggregates a 7-DAY window over ``analyst_traces``,
and the liveness watchdog reads ``max(run_started_at)`` per analyst. Any
operator-set TTL must stay WELL ABOVE 7 days (30+ recommended) — the policy
row's ``description`` column carries this reminder too.

Output ``data`` keys (unchanged):
    traces_purged       int — analyst_traces rows deleted this run
    critiques_cascaded  int — analyst_critiques rows CASCADE-deleted with them
    ttl_days            int — the effective TTL (0 = disabled)

RETIREMENT NOTE: this module stays importable/registered in
:data:`legba.data.analysts.deterministic.SUB_HANDLERS` under the SAME
sub_handler name (``analyst_traces_retention``) so no descriptor/dispatch
change is required — it just no longer carries its own purge SQL.
"""

from __future__ import annotations

from typing import Any, Mapping

from ....runtime.analyst_method import AnalystMethodResult
from . import _retention_sweep

SUB_HANDLER_NAME = "analyst_traces_retention"


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    Delegates to :func:`legba.data.analysts.deterministic_handlers.
    _retention_sweep.handle_policy` with
    ``policy_name="analyst_traces_retention"``. ``deps is None`` (unit path)
    or ``ttl_days <= 0`` (default) yields a zeroed, no-purge run — identical
    to the pre-consolidation behavior.
    """
    return await _retention_sweep.handle_policy(
        SUB_HANDLER_NAME, inputs, options, deps
    )


__all__ = ["handle", "SUB_HANDLER_NAME"]
