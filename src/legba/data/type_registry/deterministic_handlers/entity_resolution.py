# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``entity_resolution``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="entity_resolution",
    implementation="legba.data.analysts.deterministic_handlers.entity_resolution:handle",
    output="trace_only",
    doc="Folds new NER mentions into canonical entities.",
)
