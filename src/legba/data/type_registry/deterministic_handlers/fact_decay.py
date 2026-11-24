# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``fact_decay``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="fact_decay",
    implementation="legba.data.analysts.deterministic_handlers.fact_decay:handle",
    output="trace_only",
    doc="The legacy temporal decay stamp on facts.",
)
