# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""TEST-ONLY: the declaration of the ``fixture_echo`` deterministic handler."""

from legba.data.option_catalog.handler_options_base import OptionSpec
from legba.data.type_registry import declare

TYPE = declare(
    family="deterministic_handler",
    id="fixture_echo",
    implementation="tests.data_pkg.type_registry_fixture.echo_handler:handle",
    output="finding",
    alerts=("fixture_echo_alert",),
    input_schema=(OptionSpec("label", "str", "A label copied into the finding."),),
    doc="Counts the rows handed to the run (test fixture).",
)
