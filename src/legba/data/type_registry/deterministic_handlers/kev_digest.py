# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``kev_digest``.

Output: K-2 kev_digest — the new catalogue entries since the last digest,
copied field by field from the register feed's signals (no LLM). A quiet run
is force_trace_only (the indicator_tracker pattern), not this map.

Dispatch: K-2 kev_digest — see OUTPUT_KIND_BY_SUB_HANDLER above.

Verify exemption: K-2 — the KEV digest copies catalogue fields verbatim (no
model, no prose claim); its finding cites the register feed's signal ids.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="kev_digest",
    implementation="legba.data.analysts.deterministic_handlers.kev_digest:handle",
    output="finding",
    requires_pool=True,
    doc="The KEV catalogue entries new since the last digest, copied field by field.",
)
