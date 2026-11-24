# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Citation target ``event``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="citation_target",
    id="event",
    table="events",
    column="event_id",
    ref_kinds=("event",),
    id_fields=("event_id", "ref_id"),
    member_fields={"open_events": "event_ids"},
    evidence="background",
    doc="An event: a member of the open-events block, or an event token whose reports did not resolve.",
)
