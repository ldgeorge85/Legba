# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""TEST-ONLY: the declaration of the ``fixture_echo_alert`` alert kind."""

from legba.data.type_registry import declare

TYPE = declare(
    family="alert_kind",
    id="fixture_echo_alert",
    position=325,  # after due_and_absent (320), before host_disk (330)
    subject_kind="analyst",
    lifecycle="event",
    quiet_close_hours=24,
    group="expectations",
    topic="engine",
    paging="logged_only",
    doc="The fixture handler counted zero rows (test fixture).",
)
