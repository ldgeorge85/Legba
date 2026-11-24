# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic handler ``series_change``.

Output: N-L series_change — anchored-window robust-z change detector over
LIVE observations (migration 0246). The finding cites observation ids, each
with the fetch_sha256 of the response it was parsed from. No LLM.

Dispatch: N-L series_change — see OUTPUT_KIND_BY_SUB_HANDLER above.

Verify exemption: N-L — a deterministic rule over live series (no model, no
prose claim); its finding cites observation ids + response hashes.
"""

from legba.data.type_registry import declare


TYPE = declare(
    family="deterministic_handler",
    id="series_change",
    implementation="legba.data.analysts.deterministic_handlers.series_change:handle",
    output="finding",
    requires_pool=True,
    doc="Anchored-window robust-z change detector over live series observations.",
)
