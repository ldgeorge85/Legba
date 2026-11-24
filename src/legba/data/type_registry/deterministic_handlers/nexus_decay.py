# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``nexus_decay``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="nexus_decay",
    implementation="legba.data.analysts.deterministic_handlers.nexus_decay:handle",
    output="trace_only",
    doc="Temporal decay stamps on reified relationships.",
)
