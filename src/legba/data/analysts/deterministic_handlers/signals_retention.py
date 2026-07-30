# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``signals_retention`` sub-handler — TTL purge of aged signals.

Graph-and-data Wave-1b, item 3. The ``signals`` table was an unpartitioned,
13-indexed, retention-free table that grew without bound. Per locked decision
D4 (REVIEW_CONSOLIDATED_2026-06-16) the release answer is a scheduled TTL
PURGE (not a range-partition — partitioning is heavy and the volume is small).

C2 "one janitor" (2026-07-28 coherence pass): this module is now a thin
DELEGATE onto the shared :mod:`._retention_sweep` engine, which executes the
``signals_retention`` row of the ``retention_policies`` config table
(migration 0109) instead of hand-rolling its own TTL constant / env-var name
/ batch default. Behavior is BYTE-IDENTICAL to the pre-consolidation
standalone implementation (git history has the retired body; the shared
engine's ``_purge_signals`` / ``_finding_signals`` are that same code,
parameterized) — this is a REGISTRATION SHIM, not a rewrite:

  * ``ttl_days <= 0`` (the DEFAULT, seeded into the policy row) DISABLES the
    purge — deleting signals is an operator decision; the job ships inert.
  * Opt-in levers, highest first: an explicit run ``options["ttl_days"]``
    (forced runs / tests) > the descriptor's own ``method.options.ttl_days``
    — X-1 made that channel real, so a descriptor-carried ``ttl_days`` DOES
    now reach this handler on a plain cadence fire > the
    ``LEGBA_SIGNALS_RETENTION_TTL_DAYS`` env var (the pre-X-1 path; still
    honored — ff65f78). The shipped posture is still all three unset, i.e.
    disabled: deleting signals is a deliberate operator decision.
  * Keep-class exemptions (``retain_always`` / ``evidence_hold``), batch
    size, and the value-referenced children cleanup (``signal_entity_links``,
    ``signal_aliases``) are all preserved — they now live in the policy row +
    the shared engine's ``_purge_signals`` adapter, not here.

Output ``data`` keys (unchanged):
    signals_purged      int — signal rows deleted this run
    entity_links_purged int — signal_entity_links rows deleted
    aliases_purged      int — signal_aliases rows deleted
    ttl_days            int — the effective TTL (0 = disabled)

RETIREMENT NOTE: this module stays importable/registered in
:data:`legba.data.analysts.deterministic.SUB_HANDLERS` under the SAME
sub_handler name (``signals_retention``) so no descriptor/dispatch change is
required — it just no longer carries its own purge SQL.
"""

from __future__ import annotations

from typing import Any, Mapping

from ....runtime.analyst_method import AnalystMethodResult
from . import _retention_sweep

SUB_HANDLER_NAME = "signals_retention"


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    Delegates to :func:`legba.data.analysts.deterministic_handlers.
    _retention_sweep.handle_policy` with ``policy_name="signals_retention"``.
    ``deps is None`` (unit path) or ``ttl_days <= 0`` (default) yields a
    zeroed, no-purge run — identical to the pre-consolidation behavior.
    """
    return await _retention_sweep.handle_policy(
        SUB_HANDLER_NAME, inputs, options, deps
    )


__all__ = ["handle", "SUB_HANDLER_NAME"]
