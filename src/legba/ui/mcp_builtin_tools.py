# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Built-in MCP tool set for :command:`legba-mcp` (A8 — the MCP substrate surface).

Why this module exists
======================

``legba.ui.mcp_server`` was, until A8, a *purely* descriptor-driven MCP
server: its catalog came only from
:data:`legba.data.outputs.mcp_tool.MCP_TOOL_REGISTRY`, which the RUNTIME
populates in-process at descriptor-activate time. A *standalone*
``legba-mcp`` process (the way an MCP client actually launches it — one
short-lived ``docker run -i`` per conversation) never shares a process with
the runtime, so that registry is **empty** and the server lists no tools.

This module closes that gap (DIRECTION §4, "Chosen approach"): a fixed,
built-in tool set that each wraps a live **registry HTTP endpoint** through
:class:`legba.runtime.registry_client.RegistryHTTPClient`. Because these
tools are constructed from code — not from runtime-populated state — they
register at server start regardless of runtime population. The
descriptor-driven catalog stays as the SECOND source (both coexist; the
server dedupes by tool name — see :func:`legba.ui.mcp_server`).

The seven built-ins
===================

============================  ======  ==================================  ========
tool                          scope   endpoint                            method
============================  ======  ==================================  ========
``substrate_findings``        read    ``GET  /api/v1/findings``           read
``substrate_situations``      read    ``GET  /api/v1/situations``         read
``substrate_signals``         read    ``GET  /api/v1/signals``            read
``lineage_walk``              read    ``GET  /api/v1/lineage/{k}/{id}``   read
``since``                     read    ``GET  /api/v1/v3/since``           read
``export``                    read    ``POST /api/v1/v3/export``          read-only compose
``consult``                   oper.   ``POST /api/v1/consult``            run
============================  ======  ==================================  ========

``scope`` is advisory metadata surfaced in each tool's description — the
actual authorization is the registry bearer token the process sends
(``LEGBA_REGISTRY_TOKEN`` / ``LEGBA_REGISTRY_API_TOKEN``). A ``read`` token
suffices for the six read tools; ``consult`` needs an ``operator``-scoped
token because it triggers an analyst run.

The no-mutation contract
========================

**No registry mutations ride MCP** — reads + the sanctioned consult-run
only. Every built-in is an HTTP ``GET`` *except* two sanctioned ``POST``s:
``export`` (composes a document from rows it only READS) and ``consult``
(triggers the ``consult_default`` analyst — a run, not an arbitrary registry
write). :func:`assert_reads_and_consult_only` encodes and enforces this;
the A8 test suite asserts it.

Honest error surface
====================

Each tool returns a structured dict. On a non-2xx from the registry the
result is a described ``{"error": ...}`` object carrying the status +
detail — never a fabricated success. A transport / DNS / connection failure
(the registry is down / unreachable) is caught and returned as a described
``registry_unreachable`` error rather than crashing the MCP frame.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import urlsplit

from ..runtime.registry_client import RegistryClientError, RegistryHTTPClient

Scope = Literal["read", "operator"]
HttpMethod = Literal["GET", "POST"]

#: The consult endpoint blocks for the whole ReAct loop; mirror the server's
#: own cap (``consult_api.DAPR_INVOKE_TIMEOUT_SECONDS`` = 300s) so the MCP
#: client waits as long as the registry will. Documented on the tool.
CONSULT_TIMEOUT_SECONDS: float = 300.0

#: The ONLY tools allowed to use a non-GET (POST) method — the sanctioned
#: consult-run and the read-only export document composer. Any other POST (or
#: any PUT/PATCH/DELETE) is a registry mutation and is rejected by
#: :func:`assert_reads_and_consult_only`.
_SANCTIONED_POST_TOOLS: frozenset[str] = frozenset({"consult", "export"})

#: Supported lineage root kinds — mirrors ``lineage_api._TABLES_BY_KIND`` keys.
#: Kept as an explicit enum in the tool schema so a client gets a validation
#: error client-side rather than a 400 round-trip.
_LINEAGE_ROW_KINDS: tuple[str, ...] = (
    "signal",
    "situation",
    "hypothesis",
    "finding",
    "meta_finding",
    "alert",
    "critique",
    "prompt_module_candidate",
)


# ---------------------------------------------------------------------------
# Env-based configuration (matches the bringup scripts + RegistryHTTPClient)
# ---------------------------------------------------------------------------


def resolve_base_url() -> str:
    """Resolve the registry ORIGIN (``scheme://host:port``) from env.

    Accepts the bringup-script name ``LEGBA_REGISTRY_URL`` first (the task's
    named config), then falls back to ``LEGBA_REGISTRY_API_URL`` (the name
    :class:`RegistryHTTPClient` reads), then ``http://localhost:8090``.

    The bringup value carries the descriptor-registry path suffix (e.g.
    ``http://legba-registry:8090/api/v1/registry``); the built-in tools
    address ABSOLUTE ``/api/v1/...`` paths that live outside that prefix, so
    we strip any path down to the bare origin. A value that is already a bare
    origin passes through unchanged.
    """
    raw = (
        os.environ.get("LEGBA_REGISTRY_URL")
        or os.environ.get("LEGBA_REGISTRY_API_URL")
        or "http://localhost:8090"
    ).strip()
    parts = urlsplit(raw)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    # No scheme (malformed) — hand back the trimmed value; the client will
    # fail loud on the first request rather than silently mis-route.
    return raw.rstrip("/")


def resolve_token() -> str | None:
    """Resolve the registry bearer token from env.

    ``LEGBA_REGISTRY_TOKEN`` (the legacy bringup name) first, then
    ``LEGBA_REGISTRY_API_TOKEN`` (the production name). ``None`` when neither
    is set — the registry accepts anonymous in dev mode.
    """
    token = (
        os.environ.get("LEGBA_REGISTRY_TOKEN")
        or os.environ.get("LEGBA_REGISTRY_API_TOKEN")
        or ""
    ).strip()
    return token or None


def build_registry_client() -> RegistryHTTPClient:
    """Construct the shared :class:`RegistryHTTPClient` for the built-in tools.

    Env-resolved origin + bearer. A per-request timeout override handles the
    long consult loop, so the client default timeout stays short for the read
    surfaces.
    """
    return RegistryHTTPClient(
        base_url=resolve_base_url(),
        token=resolve_token(),
    )


# ---------------------------------------------------------------------------
# Tool model
# ---------------------------------------------------------------------------


ToolHandler = Callable[[RegistryHTTPClient, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class BuiltinTool:
    """One built-in MCP tool wrapping a live registry endpoint.

    ``handler`` is an async callable ``(client, args) -> structured dict``.
    ``method`` records the endpoint's HTTP verb so the no-mutation contract
    is checkable without invoking anything.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    scope: Scope
    method: HttpMethod
    handler: ToolHandler = field(repr=False)

    def scoped_description(self) -> str:
        """The description with the scope requirement stated inline."""
        scope_note = (
            "requires an operator-scoped registry token"
            if self.scope == "operator"
            else "read-scoped"
        )
        return f"{self.description} ({scope_note})"


# ---------------------------------------------------------------------------
# Shared request/format helpers — the honest error surface lives here.
# ---------------------------------------------------------------------------


async def _call(
    client: RegistryHTTPClient,
    method: str,
    path: str,
    *,
    tool_name: str,
    params: dict[str, Any] | None = None,
    json_body: Any | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Issue one registry request and format the result honestly.

    2xx → the parsed body (wrapped as ``{"result": ...}`` if the body is not
    a JSON object). Non-2xx → a described ``{"error": ...}`` with the status +
    the registry's own detail. A transport failure → a described
    ``registry_unreachable`` error. Never a fabricated success.
    """
    try:
        status_code, body = await client.request_json(
            method,
            path,
            params=params,
            json_body=json_body,
            timeout_seconds=timeout_seconds,
        )
    except RegistryClientError as exc:
        return {
            "error": "registry_unreachable",
            "tool": tool_name,
            "detail": str(exc),
        }
    if 200 <= status_code < 300:
        if isinstance(body, dict):
            return body
        return {"result": body}
    detail: Any = body
    if isinstance(body, dict) and "detail" in body:
        detail = body["detail"]
    return {
        "error": f"registry returned HTTP {status_code}",
        "tool": tool_name,
        "status": status_code,
        "detail": detail,
    }


def _missing_required(args: dict[str, Any], required: tuple[str, ...]) -> str | None:
    missing = [r for r in required if args.get(r) in (None, "")]
    if missing:
        return f"missing required argument(s): {', '.join(missing)}"
    return None


def _passthrough(args: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """Project the named keys out of ``args``, dropping absent ones."""
    return {k: args[k] for k in keys if k in args and args[k] is not None}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _handle_findings(
    client: RegistryHTTPClient, args: dict[str, Any]
) -> dict[str, Any]:
    return await _call(
        client,
        "GET",
        "/api/v1/findings",
        tool_name="substrate_findings",
        params=_passthrough(
            args,
            ("target_id", "analyst_id", "severity", "q", "since", "limit", "cursor"),
        ),
    )


async def _handle_situations(
    client: RegistryHTTPClient, args: dict[str, Any]
) -> dict[str, Any]:
    return await _call(
        client,
        "GET",
        "/api/v1/situations",
        tool_name="substrate_situations",
        params=_passthrough(
            args, ("state", "target_id", "since", "limit", "cursor")
        ),
    )


async def _handle_signals(
    client: RegistryHTTPClient, args: dict[str, Any]
) -> dict[str, Any]:
    return await _call(
        client,
        "GET",
        "/api/v1/signals",
        tool_name="substrate_signals",
        params=_passthrough(
            args, ("target_id", "source_id", "language", "since", "limit", "cursor")
        ),
    )


async def _handle_lineage(
    client: RegistryHTTPClient, args: dict[str, Any]
) -> dict[str, Any]:
    err = _missing_required(args, ("row_kind", "row_id"))
    if err:
        return {"error": "invalid_arguments", "tool": "lineage_walk", "detail": err}
    row_kind = str(args["row_kind"])
    row_id = str(args["row_id"])
    return await _call(
        client,
        "GET",
        f"/api/v1/lineage/{row_kind}/{row_id}",
        tool_name="lineage_walk",
        params=_passthrough(args, ("direction", "depth")),
    )


async def _handle_since(
    client: RegistryHTTPClient, args: dict[str, Any]
) -> dict[str, Any]:
    err = _missing_required(args, ("cursor",))
    if err:
        return {"error": "invalid_arguments", "tool": "since", "detail": err}
    return await _call(
        client,
        "GET",
        "/api/v1/v3/since",
        tool_name="since",
        params={"cursor": str(args["cursor"])},
    )


async def _handle_export(
    client: RegistryHTTPClient, args: dict[str, Any]
) -> dict[str, Any]:
    err = _missing_required(args, ("items", "format"))
    if err:
        return {"error": "invalid_arguments", "tool": "export", "detail": err}
    body: dict[str, Any] = {
        "items": args["items"],
        "format": args["format"],
    }
    if args.get("title") is not None:
        body["title"] = args["title"]
    result = await _call(
        client,
        "POST",
        "/api/v1/v3/export",
        tool_name="export",
        json_body=body,
    )
    # The markdown format comes back as a raw text body (wrapped as
    # {"result": <md string>} by _call). Re-shape to a self-describing
    # envelope so the caller always knows which format it holds.
    if "error" not in result and args["format"] == "markdown" and "result" in result:
        return {"format": "markdown", "document": result["result"]}
    if "error" not in result and args["format"] == "json":
        return {"format": "json", "document": result}
    return result


async def _handle_consult(
    client: RegistryHTTPClient, args: dict[str, Any]
) -> dict[str, Any]:
    err = _missing_required(args, ("question",))
    if err:
        return {"error": "invalid_arguments", "tool": "consult", "detail": err}
    body: dict[str, Any] = {"question": str(args["question"])}
    for key in ("scope_predicate", "max_tool_rounds", "mode", "model"):
        if args.get(key) is not None:
            body[key] = args[key]
    return await _call(
        client,
        "POST",
        "/api/v1/consult",
        tool_name="consult",
        json_body=body,
        timeout_seconds=CONSULT_TIMEOUT_SECONDS,
    )


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------


def builtin_tools() -> list[BuiltinTool]:
    """Return the seven built-in tools, in stable catalog order.

    Reconstructed on each call (cheap; dataclasses hold no runtime state) so
    a fresh :command:`legba-mcp` process always has the full set regardless of
    any runtime population.
    """
    return [
        BuiltinTool(
            name="substrate_findings",
            description=(
                "List analyst findings from the Legba substrate (GET "
                "/api/v1/findings). Filter by target_id, analyst_id, severity, "
                "a keyword query, or a since timestamp; page with limit + cursor."
            ),
            scope="read",
            method="GET",
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": "Restrict to a single target/desk id.",
                    },
                    "analyst_id": {
                        "type": "string",
                        "description": "Restrict to a single emitting analyst id.",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "critical"],
                        "description": "Restrict to one severity level.",
                    },
                    "q": {
                        "type": "string",
                        "description": "Full-text keyword match over title + body.",
                    },
                    "since": {
                        "type": "string",
                        "description": "ISO-8601 timestamp; only rows produced at/after it.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Page size (default 50, max 500).",
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Opaque next_cursor from a prior page.",
                    },
                },
            },
            handler=_handle_findings,
        ),
        BuiltinTool(
            name="substrate_situations",
            description=(
                "List situations from the Legba substrate (GET "
                "/api/v1/situations). Filter by lifecycle state, target_id, or a "
                "since timestamp; page with limit + cursor."
            ),
            scope="read",
            method="GET",
            input_schema={
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "enum": ["active", "resolved", "escalating"],
                        "description": "Restrict to one situation lifecycle state.",
                    },
                    "target_id": {
                        "type": "string",
                        "description": "Restrict to a single target/desk id.",
                    },
                    "since": {
                        "type": "string",
                        "description": "ISO-8601 timestamp; only rows produced at/after it.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Page size (default 50, max 500).",
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Opaque next_cursor from a prior page.",
                    },
                },
            },
            handler=_handle_situations,
        ),
        BuiltinTool(
            name="substrate_signals",
            description=(
                "List raw source signals from the Legba substrate (GET "
                "/api/v1/signals). Filter by target_id (resolved to the target's "
                "geo scope), source_id, language, or a since timestamp; page with "
                "limit + cursor."
            ),
            scope="read",
            method="GET",
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": {
                        "type": "string",
                        "description": "Restrict to signals overlapping a target's geo scope.",
                    },
                    "source_id": {
                        "type": "string",
                        "description": "Restrict to a single source descriptor id.",
                    },
                    "language": {
                        "type": "string",
                        "description": "Restrict to one ISO language code.",
                    },
                    "since": {
                        "type": "string",
                        "description": "ISO-8601 timestamp; only rows fetched at/after it.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Page size (default 50, max 500).",
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Opaque next_cursor from a prior page.",
                    },
                },
            },
            handler=_handle_signals,
        ),
        BuiltinTool(
            name="lineage_walk",
            description=(
                "Walk the provenance DAG for one substrate row (GET "
                "/api/v1/lineage/{row_kind}/{row_id}). direction=upstream walks "
                "the rows this row was derived FROM; downstream walks rows derived "
                "FROM it; both walks both. depth is the BFS bound (default 3, max 10)."
            ),
            scope="read",
            method="GET",
            input_schema={
                "type": "object",
                "properties": {
                    "row_kind": {
                        "type": "string",
                        "enum": list(_LINEAGE_ROW_KINDS),
                        "description": "Originating substrate row kind.",
                    },
                    "row_id": {
                        "type": "string",
                        "description": "Substrate row UUID.",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["upstream", "downstream", "both"],
                        "description": "Walk direction (default upstream).",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Max BFS depth (default 3, hard cap 10).",
                    },
                },
                "required": ["row_kind", "row_id"],
            },
            handler=_handle_lineage,
        ),
        BuiltinTool(
            name="since",
            description=(
                "One composed diff of everything product-relevant that changed "
                "since a cursor (GET /api/v1/v3/since). Returns new verified "
                "findings, superseded findings, risk-band changes, situation "
                "lifecycle edges, and alerts. The server is stateless: pass the "
                "server_now from your last call back as the next cursor "
                "(at-least-once). High-value for an agent polling for changes."
            ),
            scope="read",
            method="GET",
            input_schema={
                "type": "object",
                "properties": {
                    "cursor": {
                        "type": "string",
                        "description": (
                            "ISO-8601 timestamp of your last visit (the prior "
                            "server_now). Lookback is capped at 90 days."
                        ),
                    },
                },
                "required": ["cursor"],
            },
            handler=_handle_since,
        ),
        BuiltinTool(
            name="export",
            description=(
                "Compose one full-fidelity document (markdown or JSON) from a "
                "basket of finding + journal_entry ids (POST /api/v1/v3/export). "
                "Read-only: resolves citations, verify states, and receipt links "
                "live; missing ids are reported, never dropped. Capped at 50 items."
            ),
            scope="read",
            method="POST",
            input_schema={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "Basket entries to export (max 50).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {
                                    "type": "string",
                                    "enum": ["finding", "journal_entry"],
                                },
                                "id": {"type": "string"},
                            },
                            "required": ["kind", "id"],
                        },
                    },
                    "format": {
                        "type": "string",
                        "enum": ["markdown", "json"],
                        "description": "Output document format.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional document title.",
                    },
                },
                "required": ["items", "format"],
            },
            handler=_handle_export,
        ),
        BuiltinTool(
            name="consult",
            description=(
                "Ask the Legba consult analyst a question and get a synthesized, "
                "cited answer (POST /api/v1/consult — the ReAct on-demand analyst). "
                "mode=chat is ephemeral; mode=deep persists a finding. LONG-RUNNING: "
                f"the loop blocks up to {int(CONSULT_TIMEOUT_SECONDS)}s while the "
                "analyst runs its tool rounds. Requires an operator-scoped registry "
                "token."
            ),
            scope="operator",
            method="POST",
            input_schema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to answer.",
                    },
                    "scope_predicate": {
                        "type": "string",
                        "description": "Optional substrate scope filter predicate.",
                    },
                    "max_tool_rounds": {
                        "type": "integer",
                        "description": "ReAct tool-round ceiling (default 10, max 30).",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["chat", "deep"],
                        "description": "chat = ephemeral; deep = persist a finding.",
                    },
                    "model": {
                        "type": "string",
                        "enum": ["opus", "core"],
                        "description": "Which LLM plane answers (default opus).",
                    },
                },
                "required": ["question"],
            },
            handler=_handle_consult,
        ),
    ]


# ---------------------------------------------------------------------------
# The no-mutation contract
# ---------------------------------------------------------------------------


def assert_reads_and_consult_only(tools: list[BuiltinTool] | None = None) -> None:
    """Assert no registry MUTATIONS ride MCP — reads + consult-run only.

    Enforced invariants (raises :class:`AssertionError` on violation):

      * every tool uses ``GET`` or ``POST`` (no ``PUT`` / ``PATCH`` /
        ``DELETE`` — those are the mutating verbs);
      * the only tools permitted a ``POST`` are the sanctioned set
        ``{consult, export}`` — consult is a run, export is a read-only
        document composer;
      * ``consult`` is the only tool carrying ``operator`` scope (every other
        built-in is read-scoped).

    Called at server start (defensive) and asserted directly by the A8 tests.
    """
    catalog = tools if tools is not None else builtin_tools()
    for tool in catalog:
        assert tool.method in ("GET", "POST"), (
            f"built-in tool {tool.name!r} uses non-read/consult method "
            f"{tool.method!r} — no registry mutation may ride MCP"
        )
        if tool.method == "POST":
            assert tool.name in _SANCTIONED_POST_TOOLS, (
                f"built-in tool {tool.name!r} POSTs but is not in the sanctioned "
                f"non-GET set {sorted(_SANCTIONED_POST_TOOLS)} — reads + "
                f"consult-run only"
            )
    operator_tools = {t.name for t in catalog if t.scope == "operator"}
    assert operator_tools == {"consult"}, (
        f"expected only 'consult' to need operator scope; got {operator_tools}"
    )


__all__ = [
    "CONSULT_TIMEOUT_SECONDS",
    "BuiltinTool",
    "ToolHandler",
    "Scope",
    "HttpMethod",
    "assert_reads_and_consult_only",
    "build_registry_client",
    "builtin_tools",
    "resolve_base_url",
    "resolve_token",
]
