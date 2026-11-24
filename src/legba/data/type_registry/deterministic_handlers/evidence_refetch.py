# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``evidence_refetch``.

Output: evidence_refetch (W11-F138) — the textless-backlog drain for hosts
whose fetch policy changed (timesofisrael.com / jpost.com): re-runs the
archiver's own fetch leg on the newest N payload.text-less signals, oldest
first inside the window. Pure side-effect sweep; the per-run counts
(refetched / archived / blocked_by_challenge / per_source) live in the
trace.

Dispatch: evidence_refetch — W11-F138: the backlog drain that re-runs the
archiver's fetch leg on textless signals from hosts whose fetch policy
changed (the _fetch_policy registry's impersonate=on set). Ships DRAFT; the
VM applies it, nothing runs it live from the tree.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="evidence_refetch",
    implementation="legba.data.analysts.deterministic_handlers.evidence_refetch:handle",
    output="trace_only",
    doc="Re-fetches the textless backlog for hosts whose fetch policy changed.",
)
