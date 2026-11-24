# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""TEST-ONLY: five bad declarations in one module (an entry-point-style module
may declare several). Each must be named in the one KindDiscoveryError."""

from legba.data.type_registry import declare

# 1. collides with a real handler's id
declare(family="deterministic_handler", id="graph_mining",
        implementation="x.y:handle", output="finding", doc="A second graph_mining.")
# 2. no doc line
declare(family="deterministic_handler", id="bad_no_doc",
        implementation="x.y:handle", output="finding", doc="")
# 3. a field the family does not know
declare(family="deterministic_handler", id="bad_unknown_field",
        implementation="x.y:handle", output="finding", doc="x", colour="red")
# 4. structural claims on a trace-only handler
declare(family="deterministic_handler", id="bad_trace_claims",
        implementation="x.y:handle", output="trace_only", structural_claims=True, doc="x")
# 5. an alert kind with no topic and an unknown family elsewhere
declare(family="alert_kind", id="bad_no_topic", position=1, subject_kind="host",
        lifecycle="event", quiet_close_hours=1, group="host", paging="logged_only", doc="x")
declare(family="record_kind", id="bad_family", doc="x")
