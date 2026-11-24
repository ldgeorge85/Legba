# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``integrity_sweep``.

Output: Re-homed referential-integrity sweep — repairs/flags dangling refs;
the sweep counts live in the trace.

Dispatch: Re-homed referential-integrity sweep (DIRECTION §9 — events-free
successor to the 2.4-deleted integrity_verification)
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="integrity_sweep",
    implementation="legba.data.analysts.deterministic_handlers.integrity_sweep:handle",
    output="trace_only",
    requires_pool=True,
    doc="Referential-integrity sweep: repairs or flags dangling references.",
)
