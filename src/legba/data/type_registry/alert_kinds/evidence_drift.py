# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``evidence_drift``.

Topic: D-1: a source edited pages after they were read
(runtime/evidence_drift_loop.py).
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="evidence_drift",
    position=170,
    subject_kind="source",
    lifecycle="event",
    quiet_close_hours=24,
    group="source_health",
    topic="engine",
    paging="logged_only",
    doc='Pages a source published were edited, retracted or removed after the platform read them, and something relied on them (one episode per source per day; never paged by default).',
)
