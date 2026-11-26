# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``reference_builder``.

Output: R2 reference_builder — THE REFERENCE UNIT. A genuine FINDING on the
correctness_grader precedent: a counting-not-repairing receipt carrying
data.per_target (each target's verified-development count beside the
candidates that failed each fence, its per-dimension counts and its thin
set) plus the cost the build spent on the core plane. The REFERENCES
themselves are side-written to unit_references (migration 0196, G1's table,
which G1 only reads); this receipt is the lane's own watchable heartbeat,
and it NEVER alerts and NEVER gates.

Dispatch: R2 reference_builder — THE REFERENCE UNIT (docs/SEAMS.md #55
closed). Per tick: resolve the live roster (targets whose dimension desks
have produced recently), take the SINGLE most overdue one, and build its
14-day reference on the $0 core plane through the web_access pack —
searching under a Tier 1-2 discovery allowlist, fetching under a 2-failure
domain blocklist, forcing a NOTE turn after every fetch, archiving every
page content-addressed, then DROPPING every committed development that cites
an unfetched URL, carries no machine-readable in-window publish date, or
whose decisive span is not an exact substring of the archived text.
Dimensions left with fewer than two survivors are written to
thin_dimensions, which the grader reads. Flag
LEGBA_REFERENCE_BUILDER_ENABLED, default OFF: off, it does nothing at all.

Verify exemption: the reference
builder is the correctness grader's other half and the same argument binds.
Its receipt reports what the instrument did — which target it built, how
many committed developments survived which fence, which dimensions came out
thin, what it cost the core plane — and asserts nothing about the world a
faithfulness judge could grade. The REFERENCE it writes is deliberately not
a finding at all: it is an INPUT to a measurement, and routing it through
the pass that scores our prose against our own citations is exactly the
circularity the reference exists to escape. Added here because
OUTPUT_KIND_BY_SUB_HANDLER marks it FINDING and this set MUST stay equal to
the FINDING emitters.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="reference_builder",
    implementation="legba.data.analysts.deterministic_handlers.reference_builder:handle",
    output="finding",
    requires_pool=True,
    doc="Builds a target's 14-day reference through the web, keeping only fenced developments.",
)
