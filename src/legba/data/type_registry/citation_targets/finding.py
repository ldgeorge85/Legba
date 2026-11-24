# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Citation target ``finding``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="citation_target",
    id="finding",
    table="analyst_outputs",
    column="output_id",
    ref_kinds=("finding", "prior_read"),
    member_fields={"window_ledger": "ledger_finding_ids"},
    restates=("prior_read", "window_ledger"),
    evidence="background",
    doc="An earlier read (an analyst_outputs row): a composition's sub-read, the desk's prior read, a window-ledger member; the desk's own earlier read restates and is never new evidence.",
)
