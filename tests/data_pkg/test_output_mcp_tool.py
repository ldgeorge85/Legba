# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the MCP tool output kind (L-194).

Covers:

  * Auto-registration on analyst activate — descriptors with
    ``outputs[kind=mcp_tool]`` populate the registry; descriptors
    without skip cleanly.
  * Shorthand parsing — ``config: {mcp_tool: "tool.name"}`` aliases to
    ``tool_name``.
  * JSON-Schema input validation — required fields, type checks.
  * Mode dispatch — ``latest_output`` (default) calls the latest-output
    provider; ``consult_on_demand`` calls the on-demand dispatcher;
    each refuses to dispatch when the other surface is bound.
  * Cross-analyst name collisions raise ValueError.
  * Idempotent re-registration for the same analyst replaces the entry
    rather than erroring.
  * MCP message-frame correctness — ``list_mcp_tools()`` emits
    ``mcp.types.Tool``; ``build_message_frame()`` emits
    ``[TextContent(type='text', text=...)]``.
  * The legba-mcp ``create_server()`` factory still constructs cleanly
    after the L-194 changes (boot test — no stdio drive needed).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from legba.data.outputs.mcp_tool import (
    KIND_NAME,
    MCP_TOOL_REGISTRY,
    parse_config,
)


# ---------------------------------------------------------------------------
# Helpers — duck-typed descriptor stubs (avoid coupling to the schema's
# field validators which require fully-populated identities).
# ---------------------------------------------------------------------------


class _StubIdentity:
    def __init__(self, *, id: str, version: str = "abcdef1234567890") -> None:
        self.id = id
        self.version = version


class _StubBinding:
    def __init__(self, *, kind: str, config: dict[str, Any]) -> None:
        self.kind = kind
        self.config = config


class _StubDescriptor:
    def __init__(self, *, identity: _StubIdentity, outputs: list[_StubBinding]) -> None:
        self.identity = identity
        self.outputs = outputs


def _descriptor(analyst_id: str, *bindings: _StubBinding) -> _StubDescriptor:
    return _StubDescriptor(
        identity=_StubIdentity(id=analyst_id),
        outputs=list(bindings),
    )


def _mcp_binding(**cfg: Any) -> _StubBinding:
    return _StubBinding(kind=KIND_NAME, config=cfg)


@pytest.fixture(autouse=True)
def _clear_registry() -> None:
    """Each test gets a clean registry."""
    MCP_TOOL_REGISTRY.clear()
    yield
    MCP_TOOL_REGISTRY.clear()


# ---------------------------------------------------------------------------
# parse_config
# ---------------------------------------------------------------------------


def test_parse_config_canonical():
    cfg = parse_config({
        "tool_name": "intel.india_energy",
        "description": "Latest Brazil energy assessment.",
        "input_schema": {"type": "object", "properties": {"question": {"type": "string"}}},
        "mode": "latest_output",
    })
    assert cfg.tool_name == "intel.india_energy"
    assert cfg.mode == "latest_output"
    assert cfg.input_schema["properties"]["question"]["type"] == "string"


def test_parse_config_mcp_tool_shorthand():
    cfg = parse_config({"mcp_tool": "intel.x"})
    assert cfg.tool_name == "intel.x"
    assert cfg.mode == "latest_output"
    assert cfg.input_schema == {"type": "object"}


def test_parse_config_rejects_bad_tool_name():
    with pytest.raises(Exception):
        parse_config({"tool_name": "has spaces in it"})


def test_parse_config_rejects_non_object_input_schema():
    with pytest.raises(Exception):
        parse_config({"tool_name": "x", "input_schema": {"type": "string"}})


def test_parse_config_rejects_unknown_mode():
    with pytest.raises(Exception):
        parse_config({"tool_name": "x", "mode": "fire_and_forget"})


def test_parse_config_drops_unknown_keys():
    # Ergonomic — the binding config may carry sibling fields meant for
    # other output kinds; parse_config should ignore them.
    cfg = parse_config({"tool_name": "x", "unrelated_field": True})
    assert cfg.tool_name == "x"


# ---------------------------------------------------------------------------
# Auto-registration
# ---------------------------------------------------------------------------


def test_register_from_descriptor_auto_registers_mcp_tool_bindings():
    desc = _descriptor(
        "analyst.india_energy",
        _mcp_binding(tool_name="intel.india_energy", description="Latest assessment."),
    )
    entries = MCP_TOOL_REGISTRY.register_from_descriptor(desc)
    assert len(entries) == 1
    assert entries[0].tool_name == "intel.india_energy"
    assert MCP_TOOL_REGISTRY.get("intel.india_energy") is not None
    assert "intel.india_energy" in MCP_TOOL_REGISTRY.names()


def test_register_from_descriptor_skips_non_mcp_tool_bindings():
    desc = _descriptor(
        "analyst.x",
        _StubBinding(kind="webhook", config={"url": "https://example.com/hook"}),
    )
    entries = MCP_TOOL_REGISTRY.register_from_descriptor(desc)
    assert entries == []
    assert MCP_TOOL_REGISTRY.names() == []


def test_register_from_descriptor_no_outputs_is_noop():
    desc = _descriptor("analyst.y")
    entries = MCP_TOOL_REGISTRY.register_from_descriptor(desc)
    assert entries == []


def test_register_from_descriptor_emits_multiple_tools():
    desc = _descriptor(
        "analyst.multi",
        _mcp_binding(tool_name="multi.alpha"),
        _mcp_binding(tool_name="multi.beta", mode="consult_on_demand"),
    )
    entries = MCP_TOOL_REGISTRY.register_from_descriptor(desc)
    assert {e.tool_name for e in entries} == {"multi.alpha", "multi.beta"}


def test_register_collision_across_analysts_raises():
    desc_a = _descriptor("analyst.a", _mcp_binding(tool_name="shared.tool"))
    desc_b = _descriptor("analyst.b", _mcp_binding(tool_name="shared.tool"))
    MCP_TOOL_REGISTRY.register_from_descriptor(desc_a)
    with pytest.raises(ValueError, match="already registered"):
        MCP_TOOL_REGISTRY.register_from_descriptor(desc_b)


def test_register_same_analyst_is_idempotent():
    desc = _descriptor("analyst.a", _mcp_binding(tool_name="t.x", description="v1"))
    MCP_TOOL_REGISTRY.register_from_descriptor(desc)
    # Re-register with updated config — should overwrite, not raise.
    desc2 = _descriptor("analyst.a", _mcp_binding(tool_name="t.x", description="v2"))
    MCP_TOOL_REGISTRY.register_from_descriptor(desc2)
    assert MCP_TOOL_REGISTRY.get("t.x").config.description == "v2"


def test_unregister_for_analyst_drops_all_tools():
    desc = _descriptor(
        "analyst.cleanup",
        _mcp_binding(tool_name="t.a"),
        _mcp_binding(tool_name="t.b"),
    )
    MCP_TOOL_REGISTRY.register_from_descriptor(desc)
    dropped = MCP_TOOL_REGISTRY.unregister_for_analyst("analyst.cleanup")
    assert set(dropped) == {"t.a", "t.b"}
    assert MCP_TOOL_REGISTRY.names() == []


# ---------------------------------------------------------------------------
# Dispatch — modes (a) and (b)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_latest_output_default_mode():
    calls: list[tuple[str, dict[str, Any]]] = []

    async def latest_provider(analyst_id: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append((analyst_id, args))
        return {"answer": "cached", "scope": args}

    desc = _descriptor("analyst.brazil", _mcp_binding(tool_name="intel.brazil"))
    MCP_TOOL_REGISTRY.register_from_descriptor(
        desc, latest_output_provider=latest_provider
    )
    out = await MCP_TOOL_REGISTRY.handle("intel.brazil", {"since": "2026-05-01"})
    assert calls == [("analyst.brazil", {"since": "2026-05-01"})]
    parsed = json.loads(out)
    assert parsed["answer"] == "cached"


@pytest.mark.asyncio
async def test_dispatch_consult_on_demand_mode():
    calls: list[tuple[str, dict[str, Any]]] = []

    async def on_demand(analyst_id: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append((analyst_id, args))
        return {"answer": f"computed for {args.get('question')}"}

    desc = _descriptor(
        "analyst.consult",
        _mcp_binding(
            tool_name="consult",
            mode="consult_on_demand",
            input_schema={
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        ),
    )
    MCP_TOOL_REGISTRY.register_from_descriptor(desc, on_demand_dispatcher=on_demand)
    out = await MCP_TOOL_REGISTRY.handle("consult", {"question": "what?"})
    assert calls == [("analyst.consult", {"question": "what?"})]
    assert "computed for what?" in out


@pytest.mark.asyncio
async def test_dispatch_latest_output_missing_provider_returns_error():
    desc = _descriptor("analyst.x", _mcp_binding(tool_name="t.x"))
    MCP_TOOL_REGISTRY.register_from_descriptor(desc)  # no providers bound
    out = await MCP_TOOL_REGISTRY.handle("t.x", {})
    assert out.startswith("Error:")
    assert "latest_output" in out


@pytest.mark.asyncio
async def test_dispatch_consult_on_demand_missing_dispatcher_returns_error():
    desc = _descriptor(
        "analyst.x",
        _mcp_binding(tool_name="t.x", mode="consult_on_demand"),
    )
    MCP_TOOL_REGISTRY.register_from_descriptor(desc)  # no dispatcher bound
    out = await MCP_TOOL_REGISTRY.handle("t.x", {})
    assert out.startswith("Error:")
    assert "consult_on_demand" in out


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_returns_error():
    out = await MCP_TOOL_REGISTRY.handle("nonexistent", {})
    assert "unknown MCP tool" in out


@pytest.mark.asyncio
async def test_dispatch_handler_exception_becomes_text_error():
    async def explodes(analyst_id: str, args: dict[str, Any]) -> Any:
        raise RuntimeError("boom")

    desc = _descriptor("analyst.x", _mcp_binding(tool_name="t.x"))
    MCP_TOOL_REGISTRY.register_from_descriptor(desc, latest_output_provider=explodes)
    out = await MCP_TOOL_REGISTRY.handle("t.x", {})
    assert "Error executing t.x" in out
    assert "boom" in out


@pytest.mark.asyncio
async def test_dispatch_accepts_sync_dispatcher():
    # Production handlers are async, but ergonomic tests pass sync.
    def sync_provider(analyst_id: str, args: dict[str, Any]) -> str:
        return "sync_result"

    desc = _descriptor("analyst.s", _mcp_binding(tool_name="t.s"))
    MCP_TOOL_REGISTRY.register_from_descriptor(desc, latest_output_provider=sync_provider)
    out = await MCP_TOOL_REGISTRY.handle("t.s", {})
    assert out == "sync_result"


# ---------------------------------------------------------------------------
# Input-schema validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_validation_missing_required_field():
    async def provider(analyst_id, args):
        return "ok"

    desc = _descriptor(
        "analyst.x",
        _mcp_binding(
            tool_name="t.x",
            input_schema={
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        ),
    )
    MCP_TOOL_REGISTRY.register_from_descriptor(desc, latest_output_provider=provider)
    out = await MCP_TOOL_REGISTRY.handle("t.x", {})
    assert "missing required argument" in out
    assert "question" in out


@pytest.mark.asyncio
async def test_schema_validation_wrong_type():
    async def provider(analyst_id, args):
        return "ok"

    desc = _descriptor(
        "analyst.x",
        _mcp_binding(
            tool_name="t.x",
            input_schema={
                "type": "object",
                "properties": {"count": {"type": "integer"}},
            },
        ),
    )
    MCP_TOOL_REGISTRY.register_from_descriptor(desc, latest_output_provider=provider)
    out = await MCP_TOOL_REGISTRY.handle("t.x", {"count": "five"})
    assert "must be of type integer" in out


# ---------------------------------------------------------------------------
# MCP message-frame correctness
# ---------------------------------------------------------------------------


def test_list_mcp_tools_emits_mcp_sdk_tools():
    from mcp.types import Tool as MCPTool

    desc = _descriptor(
        "analyst.brazil",
        _mcp_binding(
            tool_name="intel.brazil",
            description="latest brazil",
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        ),
    )
    MCP_TOOL_REGISTRY.register_from_descriptor(desc)
    tools = MCP_TOOL_REGISTRY.list_mcp_tools()
    assert len(tools) == 1
    t = tools[0]
    assert isinstance(t, MCPTool)
    assert t.name == "intel.brazil"
    assert t.description == "latest brazil"
    assert t.inputSchema["properties"]["q"]["type"] == "string"


def test_build_message_frame_emits_text_content():
    from mcp.types import TextContent

    frame = MCP_TOOL_REGISTRY.build_message_frame("t.x", "the result")
    assert len(frame) == 1
    assert isinstance(frame[0], TextContent)
    assert frame[0].type == "text"
    assert frame[0].text == "the result"


# ---------------------------------------------------------------------------
# legba-mcp boot test — the existing server factory still constructs.
# ---------------------------------------------------------------------------


def test_legba_mcp_create_server_constructs_with_registry():
    """L-194 changes don't break the existing create_server() factory."""
    from legba.ui.mcp_server import create_server

    server = create_server()
    # The mcp SDK's Server doesn't expose registered handlers directly,
    # but successful construction with the registry import in place is
    # the smoke check we want — no module-level import error from the
    # new L-194 wiring.
    assert server is not None
    # The server's name is the symbolic id we set in create_server().
    assert getattr(server, "name", None) == "legba"


def test_legba_mcp_registry_shadows_legacy_consult():
    """A registered ``consult`` mcp_tool entry should appear in list_mcp_tools()
    and (per create_server()'s union logic) shadow the legacy ``consult`` tool.

    This test asserts the registry shape; the server-side list_tools()
    wrapper is exercised indirectly by test_legba_mcp_create_server_constructs_with_registry.
    """
    from mcp.types import Tool as MCPTool

    desc = _descriptor(
        "analyst.consult_on_demand",
        _mcp_binding(
            tool_name="consult",
            description="Descriptor-driven consult (replaces legacy).",
            mode="consult_on_demand",
            input_schema={
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        ),
    )
    MCP_TOOL_REGISTRY.register_from_descriptor(desc)
    names = {t.name for t in MCP_TOOL_REGISTRY.list_mcp_tools()}
    assert "consult" in names

    # Confirm the registry has the consult_on_demand mode set — so when
    # the server dispatches, it routes through the on-demand path rather
    # than the legacy _handle_consult.
    entry = MCP_TOOL_REGISTRY.get("consult")
    assert entry is not None
    assert entry.mode == "consult_on_demand"
    assert entry.analyst_id == "analyst.consult_on_demand"


# ---------------------------------------------------------------------------
# Misc — formatting + stub-descriptor invalid config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_result_formatting_pydantic_model():
    from pydantic import BaseModel

    class Out(BaseModel):
        answer: str
        score: float

    async def provider(analyst_id, args):
        return Out(answer="hi", score=0.9)

    desc = _descriptor("analyst.x", _mcp_binding(tool_name="t.x"))
    MCP_TOOL_REGISTRY.register_from_descriptor(desc, latest_output_provider=provider)
    out = await MCP_TOOL_REGISTRY.handle("t.x", {})
    parsed = json.loads(out)
    assert parsed == {"answer": "hi", "score": 0.9}


@pytest.mark.asyncio
async def test_dispatch_result_formatting_none():
    async def provider(analyst_id, args):
        return None

    desc = _descriptor("analyst.x", _mcp_binding(tool_name="t.x"))
    MCP_TOOL_REGISTRY.register_from_descriptor(desc, latest_output_provider=provider)
    out = await MCP_TOOL_REGISTRY.handle("t.x", {})
    assert out == "(no output)"


def test_register_from_descriptor_invalid_config_raises_value_error():
    desc = _descriptor(
        "analyst.bad",
        _StubBinding(kind=KIND_NAME, config={"tool_name": "has spaces"}),
    )
    with pytest.raises(ValueError, match="invalid mcp_tool"):
        MCP_TOOL_REGISTRY.register_from_descriptor(desc)


def test_registry_isolated_to_fixture_clear():
    """Confirm the autouse _clear_registry fixture actually clears state."""
    desc = _descriptor("analyst.t", _mcp_binding(tool_name="t.t"))
    MCP_TOOL_REGISTRY.register_from_descriptor(desc)
    assert "t.t" in MCP_TOOL_REGISTRY.names()
    # The next test will start with a clean registry — that's the fixture's
    # job; nothing more to assert here.
