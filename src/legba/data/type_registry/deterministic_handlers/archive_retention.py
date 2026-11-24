# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``archive_retention``.

Output: archive_retention — the archive's deterministic expiry sweep
(DEFAULT OFF, LEGBA_ARCHIVE_RETENTION): removes the stored bytes of UNCITED
archive rows past retention_days and keeps the row as
status='expired_uncited'. Pure side-effect sweep; the per-run counts live in
the trace.

Dispatch: archive_retention — expire the bytes of uncited archive rows (flag
OFF by default; a flag-off tick is a zeroed receipt that touches nothing).
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="archive_retention",
    implementation="legba.data.analysts.deterministic_handlers.archive_retention:handle",
    output="trace_only",
    doc="Expires the stored bytes of uncited archive rows (off by default).",
)
