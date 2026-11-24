# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``collection_gap``.

Output: S3-T3 collection_gap — monthly aggregation of the scorecard
insufficient-evidence signal into a "collection requirements" FINDING (which
desk×dimension cells are starved + the source classes that would feed them).
A user-facing collection-management product; a no-gap sweep is suppressed
via the run's force_trace_only (NOT this map).

Dispatch: S3-T3 collection_gap — monthly deterministic aggregation of the
scorecard insufficient-evidence signal per desk×dimension into a "collection
requirements" finding (starved cells + the plausible feed source classes);
trace-only when nothing is starved.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="collection_gap",
    implementation="legba.data.analysts.deterministic_handlers.collection_gap:handle",
    output="finding",
    doc="Monthly collection requirements: the starved desk and dimension cells.",
)
