# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Principal kind ``service``."""

from legba.data.type_registry import declare


TYPE = declare(
    family="principal_kind",
    id="service",
    name_shape="the service name (registry, runtime, worker, console), or fleet for the shared service token",
    example="service:runtime",
    doc="A process of the stack presenting a service credential; fleet is the shared service token any process may fall back to.",
)
