# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``signals_retention``.

Output: Signals TTL purge — NOT in the operator-confirmed trace-only list;
disabled by default (ttl_days<=0). Left FINDING (unchanged). C2 "one
janitor" (2026-07-28 coherence pass, migration 0109): the handler now
DELEGATES to the shared deterministic_handlers._retention_sweep engine
(executes the "signals_retention" retention_policies row) instead of
carrying its own purge SQL — the sub_handler NAME + this OUTPUT_KIND entry
are unchanged, so no dispatch/descriptor change was needed.

Dispatch: Signals TTL purge (graph-and-data Wave-1b item 3 / D4). Disabled
by default (ttl_days<=0); operator opts in with a positive TTL on options.
C2 "one janitor" (migration 0109): the entry point stays this same function
object (identity-checked by several tests) — it now delegates to
deterministic_handlers._retention_sweep, the shared engine also backing
analyst_traces_retention below.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="signals_retention",
    implementation="legba.data.analysts.deterministic_handlers.signals_retention:handle",
    output="finding",
    doc="TTL purge of aged signals (off by default).",
)
