# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W11-F152 -- the consult can call ``get_source_health``.

The 2026-11-05 consult dig: the consult read ``list_sources`` (a thin row per
source, ``last_poll_outcome='empty'`` on 59 of 116 healthy feeds) and reported
the fleet as dead, while the honest verdict -- ``get_source_health``'s
``staleness`` -- was registered for the stack plane and the journal but absent
from the consult's ``_KNOWN_TOOLS``. Pinned here: the name is known, dispatches
to the port with its arguments, is on the native tool wire, is a pack tool the
governed consult path will not block, and the prompt tells the model about it
and about what 'empty' means.
"""

from __future__ import annotations

import pytest

from legba.data.analysts.agency.substrate_read import SUBSTRATE_READ_TOOLS
from legba.data.analysts.consult_system_prompt import _SYSTEM_PROMPT
from legba.data.analysts.consult_tool_routing import _KNOWN_TOOLS, _dispatch_tool
from legba.data.stack.llm.tool_rounds import TOOL_SCHEMAS


class _Port:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def get_source_health(self, **kw):
        self.calls.append(("get_source_health", kw))
        return {"summary": {}, "rows": [], "refs": [], "count": 0}


def test_get_source_health_is_a_known_consult_tool_and_a_pack_tool():
    assert "get_source_health" in _KNOWN_TOOLS
    assert "get_source_health" in SUBSTRATE_READ_TOOLS     # governed path
    assert "get_source_health" in TOOL_SCHEMAS             # native wire


@pytest.mark.asyncio
async def test_dispatch_routes_get_source_health_to_the_port_with_its_args():
    port = _Port()
    out = await _dispatch_tool(
        port, name="get_source_health",
        args={"silent_only": True, "silent_hours": 72, "limit": 10},
        scope_predicate=None,
    )
    assert "error" not in out
    assert port.calls == [(
        "get_source_health",
        {"silent_only": True, "silent_hours": 72, "limit": 10},
    )]
    # Defaults: whole fleet, 48 h.
    port2 = _Port()
    await _dispatch_tool(
        port2, name="get_source_health", args={}, scope_predicate=None,
    )
    assert port2.calls[0][1] == {
        "silent_only": False, "silent_hours": 48, "limit": 200,
    }


def test_prompt_names_the_tool_and_reads_empty_as_quiet_not_dead():
    assert "get_source_health(" in _SYSTEM_PROMPT
    assert "NOT a dead one" in _SYSTEM_PROMPT
    assert "staleness" in _SYSTEM_PROMPT
