# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the legba-mcp built-in tool set (A8 — MCP substrate surface).

Covers:

  * Request build — each tool issues the right method + path + query/body to
    the registry (mocked via ``httpx.MockTransport`` injected into
    ``RegistryHTTPClient``, the existing registry-client mock pattern).
  * Response shape — a 2xx body is passed through structured; export's md /
    json envelopes are self-describing.
  * Standalone-non-empty — the merged catalog lists the seven built-ins with
    an EMPTY descriptor registry (no runtime), which is the whole point of A8.
  * No-mutation contract — ``assert_reads_and_consult_only`` passes for the
    real catalog and rejects a mutating verb / an un-sanctioned POST.
  * Error passthrough — a registry 4xx/5xx returns a described error (never a
    fabricated success); a transport failure returns ``registry_unreachable``.
  * Dedupe precedence — a descriptor tool colliding with a built-in name is
    dropped; the built-in wins.
  * Consult long-timeout — the consult tool threads the 300s override.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable

import httpx
import pytest

from legba.data.outputs.mcp_tool import MCP_TOOL_REGISTRY
from legba.runtime.registry_client import RegistryHTTPClient
from legba.ui.mcp_builtin_tools import (
    CONSULT_TIMEOUT_SECONDS,
    BuiltinTool,
    _handle_consult,
    assert_reads_and_consult_only,
    builtin_tools,
    resolve_base_url,
    resolve_token,
)
from legba.ui.mcp_server import create_server, merge_tool_catalogs

try:
    from mcp.types import Tool
except Exception:  # pragma: no cover - mcp is a required prod dep
    Tool = None  # type: ignore[assignment]


EXPECTED_BUILTIN_NAMES = sorted(
    [
        "substrate_findings",
        "substrate_situations",
        "substrate_signals",
        "lineage_walk",
        "since",
        "export",
        "consult",
    ]
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry():
    """Keep the process-wide descriptor registry empty around each test so the
    standalone-property assertions see ONLY the built-ins."""
    MCP_TOOL_REGISTRY.clear()
    yield
    MCP_TOOL_REGISTRY.clear()


def _tool(name: str) -> BuiltinTool:
    for t in builtin_tools():
        if t.name == name:
            return t
    raise AssertionError(f"no built-in named {name!r}")


def _capturing_client(
    responder: Callable[[httpx.Request], httpx.Response],
) -> tuple[RegistryHTTPClient, list[httpx.Request]]:
    """A RegistryHTTPClient backed by an httpx MockTransport that records every
    request. Mirrors the pattern in tests/runtime/test_qdrant_factory.py."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return responder(request)

    transport = httpx.MockTransport(handler)
    inner = httpx.AsyncClient(transport=transport, base_url="http://registry.test")
    client = RegistryHTTPClient(
        base_url="http://registry.test", token="test-token", client=inner,
    )
    return client, captured


class _RecordingClient:
    """A minimal stand-in exposing ``request_json`` — lets a test observe the
    per-request timeout override, which a MockTransport request cannot carry."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json_body: Any = None,
        timeout_seconds: Any = None,
    ) -> tuple[int, Any]:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "params": params,
                "json_body": json_body,
                "timeout_seconds": timeout_seconds,
            }
        )
        return 200, {"answer": "ok"}


# ---------------------------------------------------------------------------
# Catalog shape
# ---------------------------------------------------------------------------


def test_seven_builtins_with_expected_names_and_scopes():
    tools = builtin_tools()
    assert sorted(t.name for t in tools) == EXPECTED_BUILTIN_NAMES
    scope_by_name = {t.name: t.scope for t in tools}
    # Only consult needs operator scope; every read tool is read-scoped.
    assert scope_by_name["consult"] == "operator"
    for name in EXPECTED_BUILTIN_NAMES:
        if name != "consult":
            assert scope_by_name[name] == "read", name


def test_every_builtin_input_schema_is_object():
    for t in builtin_tools():
        assert t.input_schema.get("type") == "object", t.name


# ---------------------------------------------------------------------------
# Standalone-non-empty (the A8 fix)
# ---------------------------------------------------------------------------


def test_standalone_lists_seven_builtins_without_runtime():
    """With an EMPTY descriptor registry (no runtime population), the merged
    catalog the server lists still carries all seven built-ins."""
    assert MCP_TOOL_REGISTRY.names() == []  # nothing populated
    merged = merge_tool_catalogs(builtin_tools(), MCP_TOOL_REGISTRY.list_mcp_tools())
    assert sorted(t.name for t in merged) == EXPECTED_BUILTIN_NAMES


def test_create_server_constructs_standalone():
    """The stdio server factory builds cleanly with no runtime + an injected
    (unused-at-construction) client."""
    client = RegistryHTTPClient(base_url="http://registry.test", token=None)
    server = create_server(registry_client=client, builtins=builtin_tools())
    assert server is not None
    assert server.name == "legba"


def test_dedupe_builtin_wins_over_descriptor_collision():
    """A descriptor tool that reuses a built-in name is dropped; the built-in
    wins. A non-colliding descriptor tool is kept."""
    assert Tool is not None
    fake_registry = [
        Tool(
            name="substrate_findings",  # collides
            description="SHADOWED descriptor version",
            inputSchema={"type": "object"},
        ),
        Tool(
            name="intelligence.custom_desk",  # unique
            description="a descriptor-only analyst tool",
            inputSchema={"type": "object"},
        ),
    ]
    merged = merge_tool_catalogs(builtin_tools(), fake_registry)
    names = [t.name for t in merged]
    assert names.count("substrate_findings") == 1
    assert "intelligence.custom_desk" in names
    assert len(merged) == 8  # 7 built-ins + 1 non-colliding descriptor tool
    sf = next(t for t in merged if t.name == "substrate_findings")
    assert "SHADOWED" not in sf.description


# ---------------------------------------------------------------------------
# No-mutation contract
# ---------------------------------------------------------------------------


def test_no_mutation_contract_holds_for_real_catalog():
    # Does not raise.
    assert_reads_and_consult_only(builtin_tools())
    # Every tool is GET, except the two sanctioned POSTs.
    posts = {t.name for t in builtin_tools() if t.method == "POST"}
    assert posts == {"consult", "export"}


def test_no_mutation_contract_rejects_mutating_verb():
    async def _noop(client, args):  # pragma: no cover - never called
        return {}

    bad = BuiltinTool(
        name="delete_thing",
        description="x",
        input_schema={"type": "object"},
        scope="read",
        method="PUT",  # a mutating verb
        handler=_noop,
    )
    with pytest.raises(AssertionError):
        assert_reads_and_consult_only([bad])


def test_no_mutation_contract_rejects_unsanctioned_post():
    async def _noop(client, args):  # pragma: no cover - never called
        return {}

    bad = BuiltinTool(
        name="mutate_registry",
        description="x",
        input_schema={"type": "object"},
        scope="read",
        method="POST",  # POST but not in the sanctioned {consult, export} set
        handler=_noop,
    )
    with pytest.raises(AssertionError):
        assert_reads_and_consult_only([bad])


# ---------------------------------------------------------------------------
# Request build + response shape (read GETs)
# ---------------------------------------------------------------------------


async def test_findings_request_build_and_passthrough():
    body = {"data": [{"id": "f1", "title": "t"}], "next_cursor": None}
    client, captured = _capturing_client(
        lambda _r: httpx.Response(200, json=body)
    )
    try:
        result = await _tool("substrate_findings").handler(
            client,
            {"target_id": "brazil", "severity": "high", "limit": 10, "cursor": None},
        )
    finally:
        await client.close()
    req = captured[0]
    assert req.method == "GET"
    assert req.url.path == "/api/v1/findings"
    assert req.url.params.get("target_id") == "brazil"
    assert req.url.params.get("severity") == "high"
    assert req.url.params.get("limit") == "10"
    # A None-valued optional filter is dropped, never sent as "None".
    assert "cursor" not in req.url.params
    # Bearer is threaded.
    assert req.headers.get("authorization") == "Bearer test-token"
    # 2xx body is passed through structured.
    assert result == body


async def test_situations_request_build():
    client, captured = _capturing_client(
        lambda _r: httpx.Response(200, json={"data": [], "next_cursor": None})
    )
    try:
        await _tool("substrate_situations").handler(
            client, {"state": "active", "target_id": "india"}
        )
    finally:
        await client.close()
    req = captured[0]
    assert req.url.path == "/api/v1/situations"
    assert req.url.params.get("state") == "active"
    assert req.url.params.get("target_id") == "india"


async def test_signals_request_build():
    client, captured = _capturing_client(
        lambda _r: httpx.Response(200, json={"data": [], "next_cursor": None})
    )
    try:
        await _tool("substrate_signals").handler(
            client, {"source_id": "rss.example", "language": "en", "limit": 5}
        )
    finally:
        await client.close()
    req = captured[0]
    assert req.url.path == "/api/v1/signals"
    assert req.url.params.get("source_id") == "rss.example"
    assert req.url.params.get("language") == "en"
    assert req.url.params.get("limit") == "5"


async def test_lineage_request_build_and_path():
    row_id = str(uuid.uuid4())
    client, captured = _capturing_client(
        lambda _r: httpx.Response(200, json={"root": {"id": row_id}, "nodes": []})
    )
    try:
        result = await _tool("lineage_walk").handler(
            client,
            {"row_kind": "finding", "row_id": row_id, "direction": "both", "depth": 5},
        )
    finally:
        await client.close()
    req = captured[0]
    assert req.url.path == f"/api/v1/lineage/finding/{row_id}"
    assert req.url.params.get("direction") == "both"
    assert req.url.params.get("depth") == "5"
    assert result["root"]["id"] == row_id


async def test_lineage_missing_required_no_http_call():
    client, captured = _capturing_client(
        lambda _r: httpx.Response(500)  # should never be hit
    )
    try:
        result = await _tool("lineage_walk").handler(client, {"row_kind": "finding"})
    finally:
        await client.close()
    assert captured == []  # no request issued
    assert result["error"] == "invalid_arguments"
    assert "row_id" in result["detail"]


async def test_since_request_build():
    client, captured = _capturing_client(
        lambda _r: httpx.Response(200, json={"cursor": "x", "server_now": "y", "counts": {}})
    )
    try:
        result = await _tool("since").handler(
            client, {"cursor": "2026-07-24T00:00:00+00:00"}
        )
    finally:
        await client.close()
    req = captured[0]
    assert req.url.path == "/api/v1/v3/since"
    assert req.url.params.get("cursor") == "2026-07-24T00:00:00+00:00"
    assert "counts" in result


async def test_since_missing_cursor_no_http_call():
    client, captured = _capturing_client(lambda _r: httpx.Response(500))
    try:
        result = await _tool("since").handler(client, {})
    finally:
        await client.close()
    assert captured == []
    assert result["error"] == "invalid_arguments"


# ---------------------------------------------------------------------------
# Request build + response shape (the sanctioned POSTs)
# ---------------------------------------------------------------------------


async def test_export_markdown_envelope():
    client, captured = _capturing_client(
        lambda _r: httpx.Response(
            200, text="# Legba export\n", headers={"content-type": "text/markdown"}
        )
    )
    fid = str(uuid.uuid4())
    try:
        result = await _tool("export").handler(
            client,
            {"items": [{"kind": "finding", "id": fid}], "format": "markdown"},
        )
    finally:
        await client.close()
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/v3/export"
    sent = json.loads(req.content)
    assert sent == {"items": [{"kind": "finding", "id": fid}], "format": "markdown"}
    assert result == {"format": "markdown", "document": "# Legba export\n"}


async def test_export_json_envelope_and_title():
    doc = {"title": "My report", "items": []}
    client, captured = _capturing_client(lambda _r: httpx.Response(200, json=doc))
    fid = str(uuid.uuid4())
    try:
        result = await _tool("export").handler(
            client,
            {
                "items": [{"kind": "finding", "id": fid}],
                "format": "json",
                "title": "My report",
            },
        )
    finally:
        await client.close()
    sent = json.loads(captured[0].content)
    assert sent["title"] == "My report"
    assert result == {"format": "json", "document": doc}


async def test_consult_request_build_passthrough():
    envelope = {"answer": "the answer", "finding_id": None, "model": "opus"}
    client, captured = _capturing_client(lambda _r: httpx.Response(200, json=envelope))
    try:
        result = await _tool("consult").handler(
            client, {"question": "what changed?", "mode": "chat"}
        )
    finally:
        await client.close()
    req = captured[0]
    assert req.method == "POST"
    assert req.url.path == "/api/v1/consult"
    sent = json.loads(req.content)
    assert sent["question"] == "what changed?"
    assert sent["mode"] == "chat"
    assert result == envelope


async def test_consult_missing_question():
    client, captured = _capturing_client(lambda _r: httpx.Response(500))
    try:
        result = await _tool("consult").handler(client, {})
    finally:
        await client.close()
    assert captured == []
    assert result["error"] == "invalid_arguments"


async def test_consult_threads_long_timeout():
    """The consult tool must give the registry the full ReAct-loop budget —
    a MockTransport request can't observe the timeout, so a recording client
    captures the override directly."""
    rec = _RecordingClient()
    result = await _handle_consult(rec, {"question": "hi"})  # type: ignore[arg-type]
    assert rec.calls[0]["timeout_seconds"] == CONSULT_TIMEOUT_SECONDS
    assert rec.calls[0]["method"] == "POST"
    assert rec.calls[0]["path"] == "/api/v1/consult"
    assert result == {"answer": "ok"}


# ---------------------------------------------------------------------------
# Honest error surface
# ---------------------------------------------------------------------------


async def test_error_passthrough_4xx_described_not_fabricated():
    client, _ = _capturing_client(
        lambda _r: httpx.Response(400, json={"detail": "limit must be in [1, 500]"})
    )
    try:
        result = await _tool("substrate_findings").handler(client, {"limit": 9999})
    finally:
        await client.close()
    assert result["error"] == "registry returned HTTP 400"
    assert result["status"] == 400
    assert result["tool"] == "substrate_findings"
    assert result["detail"] == "limit must be in [1, 500]"
    # Crucially: no fabricated data payload.
    assert "data" not in result


async def test_error_passthrough_404_described():
    client, _ = _capturing_client(
        lambda _r: httpx.Response(404, json={"detail": "no finding row"})
    )
    try:
        result = await _tool("lineage_walk").handler(
            client, {"row_kind": "finding", "row_id": str(uuid.uuid4())}
        )
    finally:
        await client.close()
    assert result["status"] == 404
    assert result["detail"] == "no finding row"


async def test_transport_failure_returns_registry_unreachable():
    def _boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client, _ = _capturing_client(_boom)
    try:
        result = await _tool("substrate_findings").handler(client, {})
    finally:
        await client.close()
    assert result["error"] == "registry_unreachable"
    assert result["tool"] == "substrate_findings"
    assert "connection refused" in result["detail"]


# ---------------------------------------------------------------------------
# Env config resolution
# ---------------------------------------------------------------------------


def test_resolve_base_url_strips_registry_path(monkeypatch):
    monkeypatch.delenv("LEGBA_REGISTRY_API_URL", raising=False)
    monkeypatch.setenv(
        "LEGBA_REGISTRY_URL", "http://legba-registry:8090/api/v1/registry"
    )
    assert resolve_base_url() == "http://legba-registry:8090"


def test_resolve_base_url_falls_back_to_api_url(monkeypatch):
    monkeypatch.delenv("LEGBA_REGISTRY_URL", raising=False)
    monkeypatch.setenv("LEGBA_REGISTRY_API_URL", "http://localhost:8090")
    assert resolve_base_url() == "http://localhost:8090"


def test_resolve_token_prefers_legacy_then_api(monkeypatch):
    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)
    monkeypatch.setenv("LEGBA_REGISTRY_TOKEN", "legacy-tok")
    assert resolve_token() == "legacy-tok"
    monkeypatch.delenv("LEGBA_REGISTRY_TOKEN", raising=False)
    monkeypatch.setenv("LEGBA_REGISTRY_API_TOKEN", "prod-tok")
    assert resolve_token() == "prod-tok"
