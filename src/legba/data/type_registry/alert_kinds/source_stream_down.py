# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Alert kind ``source_stream_down``.

Topic: I-4: a subscribed source's held connection is down
(runtime/stream_liveness.py).
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="alert_kind",
    id="source_stream_down",
    position=160,
    subject_kind="source",
    lifecycle="condition",
    quiet_close_hours=None,
    group="source_health",
    topic="engine",
    paging="logged_only",
    doc="A subscribed source's held connection is refused, stalled, reconnecting for several windows, or not held at all.",
)
