# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Citation target ``desk_baseline``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="citation_target",
    id="desk_baseline",
    table="desk_baselines",
    column="legacy_key",
    key="text",
    member_fields={"desk_baseline": "baseline_keys"},
    evidence="background",
    doc="A desk baseline metric keyed 'desk:metric' (no uuid): held in legacy_key, counted, never joined.",
)
