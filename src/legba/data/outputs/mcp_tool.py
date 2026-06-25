# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""MCP tool output kind — L-194.

Surfaces each analyst as a Model Context Protocol tool so Claude Code (or
any MCP client) can call the analyst as a tool.

Wiring shape (descriptor side)::

    outputs:
      - kind: mcp_tool
        config:
          tool_name: "intelligence.india_energy_assessment"
          description: "Latest energy-security assessment for India."
          input_schema:
            type: object
            properties:
              question: {type: string}
            required: [question]
          mode: latest_output            # (default) — return analyst's most
                                          # recent output for the bound scope.
          # mode: consult_on_demand     # — trigger an on-demand analyst run
                                          # using the call's args as input.

When the descriptor activates, the runtime calls
:meth:`MCPToolRegistry.register_from_descriptor`. The :command:`legba-mcp`
stdio server then lists every registered MCP tool alongside the legacy
read-only analytical tools, and dispatches MCP `tools/call` messages
through :meth:`MCPToolRegistry.handle`.

Two dispatch modes are supported:

* ``latest_output`` (default) — the handler returns the analyst's most
  recent output for the bound scope. The runtime injects a
  ``latest_output_provider`` callable at registration time; in unit tests
  a stub provider is passed directly.

* ``consult_on_demand`` — the handler triggers an analyst run via the
  injected ``on_demand_dispatcher`` callable and returns the result. This
  is the daily-driver path for the ``consult_on_demand`` analyst kind
  (L-178) — restoring the legacy ``consult`` MCP tool's behavior as a
  descriptor-registered output rather than a hand-wired tool.

The registry deliberately does **not** import :mod:`mcp` at module-import
time — the MCP SDK types are only resolved inside :meth:`list_mcp_tools`
and :meth:`build_message_frame` so unit tests can run without the SDK
loaded. In production the SDK is a required dep (see ``pyproject.toml``
``dependencies``: ``mcp>=1.0``).
"""

from __future__ import annotations

import inspect
import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

log = logging.getLogger("legba.data.outputs.mcp_tool")


KIND_NAME = "mcp_tool"
"""Registered output-kind name (matches the ``OutputBinding.kind`` literal)."""


# ---------------------------------------------------------------------------
# Config — parsed from the descriptor's `OutputBinding.config` dict.
# ---------------------------------------------------------------------------


McpToolMode = Literal["latest_output", "consult_on_demand"]


class McpToolConfig(BaseModel):
    """Per-descriptor MCP tool config.

    Parsed from the ``OutputBinding(kind='mcp_tool').config`` dict on the
    descriptor. ``OutputBinding`` keeps a permissive ``dict[str, Any]``
    config payload by design (see :mod:`legba.data.schemas.target`); this
    model is the *output kind*'s narrow contract.

    The ``mcp_tool`` shorthand key is accepted as a sibling to
    ``tool_name`` for ergonomics when authors prefer the inline form
    (``config: {mcp_tool: "intelligence.x"}``).
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1, max_length=128)
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    mode: McpToolMode = "latest_output"

    @field_validator("tool_name")
    @classmethod
    def _tool_name_shape(cls, v: str) -> str:
        # MCP allows letters/digits/dots/underscores/hyphens; we mirror that.
        if not all(c.isalnum() or c in "._-" for c in v):
            raise ValueError(
                f"mcp_tool.tool_name {v!r} must only contain alphanumerics, "
                f"'.', '_', '-'"
            )
        return v

    @field_validator("input_schema")
    @classmethod
    def _input_schema_is_object(cls, v: dict[str, Any]) -> dict[str, Any]:
        # MCP tools require an object-shaped input schema (per the spec
        # `tools.inputSchema` field — see https://modelcontextprotocol.io).
        if not isinstance(v, dict):
            raise ValueError("input_schema must be a JSON-Schema object")
        if v.get("type", "object") != "object":
            raise ValueError(
                "input_schema.type must be 'object' (MCP tool requirement)"
            )
        return v


def parse_config(raw: dict[str, Any]) -> McpToolConfig:
    """Parse a descriptor-side ``OutputBinding.config`` into an :class:`McpToolConfig`.

    Accepts both the canonical form (``tool_name: ...``) and the
    shorthand ``mcp_tool: "tool.name"`` aliases. Falls back to the
    analyst's method input schema if no ``input_schema`` is provided in
    the binding config (this mirrors the task spec — "Tool name +
    JSON-Schema input from descriptor `analyst.method.input_schema`").
    """
    cfg = dict(raw)
    # Shorthand: `mcp_tool: "tool.name"` becomes `tool_name: "tool.name"`.
    if "mcp_tool" in cfg and "tool_name" not in cfg:
        cfg["tool_name"] = cfg.pop("mcp_tool")
    # Drop unknown analyst-side fields not in our model.
    cfg = {k: v for k, v in cfg.items() if k in {"tool_name", "description", "input_schema", "mode"}}
    return McpToolConfig.model_validate(cfg)


# ---------------------------------------------------------------------------
# Dispatcher types — injected by the runtime (or by tests).
# ---------------------------------------------------------------------------


OnDemandDispatcher = Callable[[str, dict[str, Any]], Awaitable[Any]]
"""``(analyst_id, args) -> latest analyst output payload``. Runs the analyst
on-demand and returns the synthesized result."""

LatestOutputProvider = Callable[[str, dict[str, Any]], Awaitable[Any]]
"""``(analyst_id, args) -> latest persisted output for the scope``. Reads
the most recent ``analyst_outputs`` row (or equivalent) for the bound
target/analyst scope. ``args`` lets the provider apply scope filters
declared on the binding (e.g., ``since``)."""


@dataclass
class RegisteredMcpTool:
    """One activated MCP tool entry inside the registry.

    Tests assert against the fields directly; production code calls
    :meth:`MCPToolRegistry.handle` which dispatches via the bound
    callables.
    """

    config: McpToolConfig
    analyst_id: str
    analyst_version: str
    on_demand_dispatcher: OnDemandDispatcher | None
    latest_output_provider: LatestOutputProvider | None

    @property
    def tool_name(self) -> str:
        return self.config.tool_name

    @property
    def mode(self) -> McpToolMode:
        return self.config.mode


# ---------------------------------------------------------------------------
# Registry — process-wide singleton populated by descriptor activation.
# ---------------------------------------------------------------------------


class MCPToolRegistry:
    """Process-wide registry of MCP-tool output bindings.

    The runtime calls :meth:`register_from_descriptor` when an analyst
    descriptor's lifecycle transitions to ``active`` and
    :meth:`unregister_for_analyst` when it transitions away. The
    :command:`legba-mcp` stdio server reads from this registry to publish
    the tool catalog and dispatch ``tools/call`` requests.

    Idempotency: re-registering the same ``tool_name`` for the same
    analyst is a no-op (it overwrites the entry); re-registering the
    same ``tool_name`` for a *different* analyst raises ``ValueError``
    so descriptor-author collisions surface loudly rather than
    silently shadowing each other.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tools: dict[str, RegisteredMcpTool] = {}

    # -- registration ----------------------------------------------------

    def register_from_descriptor(
        self,
        descriptor: Any,
        *,
        on_demand_dispatcher: OnDemandDispatcher | None = None,
        latest_output_provider: LatestOutputProvider | None = None,
    ) -> list[RegisteredMcpTool]:
        """Scan ``descriptor.outputs`` for ``kind='mcp_tool'`` bindings and register each.

        Returns the list of :class:`RegisteredMcpTool` entries that were
        materialized from this descriptor. Returns an empty list if the
        descriptor has no MCP-tool outputs.

        ``descriptor`` is expected to be an :class:`AnalystDescriptor`
        instance — we duck-type ``.identity.id``, ``.identity.version``,
        ``.outputs`` so the registry stays decoupled from the schema
        module's full import path.
        """
        analyst_id, analyst_version, outputs = _extract_descriptor_fields(descriptor)

        registered: list[RegisteredMcpTool] = []
        for binding in outputs:
            kind = getattr(binding, "kind", None) or (binding.get("kind") if isinstance(binding, dict) else None)
            if kind != KIND_NAME:
                continue
            cfg_raw = getattr(binding, "config", None)
            if cfg_raw is None and isinstance(binding, dict):
                cfg_raw = binding.get("config", {})
            try:
                cfg = parse_config(cfg_raw or {})
            except ValidationError as err:
                raise ValueError(
                    f"invalid mcp_tool output binding on analyst {analyst_id!r}: {err}"
                ) from err

            entry = RegisteredMcpTool(
                config=cfg,
                analyst_id=analyst_id,
                analyst_version=analyst_version,
                on_demand_dispatcher=on_demand_dispatcher,
                latest_output_provider=latest_output_provider,
            )
            with self._lock:
                existing = self._tools.get(cfg.tool_name)
                if existing is not None and existing.analyst_id != analyst_id:
                    raise ValueError(
                        f"mcp_tool name {cfg.tool_name!r} already registered "
                        f"by analyst {existing.analyst_id!r}; cannot reassign "
                        f"to {analyst_id!r}"
                    )
                self._tools[cfg.tool_name] = entry
            registered.append(entry)
            log.info(
                "mcp_tool registered: name=%s analyst=%s mode=%s",
                cfg.tool_name, analyst_id, cfg.mode,
            )
        return registered

    def unregister(self, tool_name: str) -> None:
        """Drop a single tool by name. Idempotent."""
        with self._lock:
            self._tools.pop(tool_name, None)

    def unregister_for_analyst(self, analyst_id: str) -> list[str]:
        """Drop every tool registered against ``analyst_id``. Returns the dropped names."""
        with self._lock:
            dropped = [
                name for name, t in self._tools.items() if t.analyst_id == analyst_id
            ]
            for name in dropped:
                self._tools.pop(name, None)
        return dropped

    def clear(self) -> None:
        """Drop every registration (test helper)."""
        with self._lock:
            self._tools.clear()

    # -- query -----------------------------------------------------------

    def get(self, tool_name: str) -> RegisteredMcpTool | None:
        with self._lock:
            return self._tools.get(tool_name)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._tools.keys())

    def entries(self) -> list[RegisteredMcpTool]:
        with self._lock:
            return list(self._tools.values())

    def list_mcp_tools(self) -> list[Any]:
        """Return :class:`mcp.types.Tool` instances for every registered binding.

        ``mcp`` is imported lazily so unit tests that exercise the
        registry shape don't need the SDK installed.
        """
        from mcp.types import Tool  # type: ignore[import-not-found]

        with self._lock:
            tools = list(self._tools.values())
        return [
            Tool(
                name=t.config.tool_name,
                description=t.config.description or f"Analyst {t.analyst_id} (mode={t.config.mode})",
                inputSchema=t.config.input_schema,
            )
            for t in tools
        ]

    # -- dispatch --------------------------------------------------------

    async def handle(self, tool_name: str, args: dict[str, Any]) -> str:
        """Dispatch an MCP ``tools/call`` request through the registered binding.

        Returns the analyst's response serialized as text suitable for
        wrapping in an :class:`mcp.types.TextContent`. Validation
        failures and dispatcher errors are formatted as ``"Error: …"``
        strings (matching the legacy server's failure convention) rather
        than raised — this keeps the MCP message frame well-formed.
        """
        entry = self.get(tool_name)
        if entry is None:
            return f"Error: unknown MCP tool {tool_name!r}"

        # Light-touch input validation against the declared input schema.
        validation_error = _validate_args_against_schema(args, entry.config.input_schema)
        if validation_error is not None:
            return f"Error: input validation failed: {validation_error}"

        try:
            if entry.config.mode == "consult_on_demand":
                if entry.on_demand_dispatcher is None:
                    return (
                        "Error: mcp_tool is configured for mode=consult_on_demand "
                        "but no on_demand_dispatcher is bound."
                    )
                result = await _maybe_await(
                    entry.on_demand_dispatcher(entry.analyst_id, args)
                )
            else:  # latest_output (default)
                if entry.latest_output_provider is None:
                    return (
                        "Error: mcp_tool is configured for mode=latest_output "
                        "but no latest_output_provider is bound."
                    )
                result = await _maybe_await(
                    entry.latest_output_provider(entry.analyst_id, args)
                )
        except Exception as exc:  # noqa: BLE001 — bubble as MCP text error
            log.exception("mcp_tool dispatch failed: name=%s analyst=%s", tool_name, entry.analyst_id)
            return f"Error executing {tool_name}: {exc}"

        return _format_text_result(result)

    # -- MCP message frame -----------------------------------------------

    def build_message_frame(self, tool_name: str, text_payload: str) -> list[Any]:
        """Return the ``[TextContent]`` payload an MCP server hands back to
        an MCP client for a ``tools/call`` result.

        Separated for unit testing — the actual call site lives in
        :mod:`legba.ui.mcp_server`.
        """
        from mcp.types import TextContent  # type: ignore[import-not-found]
        return [TextContent(type="text", text=text_payload)]


# Module-level singleton — the runtime + legba-mcp share this instance.
MCP_TOOL_REGISTRY = MCPToolRegistry()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_descriptor_fields(descriptor: Any) -> tuple[str, str, list[Any]]:
    """Pull ``(analyst_id, analyst_version, outputs)`` out of a descriptor.

    Duck-typed so this module doesn't have to import
    :class:`AnalystDescriptor` directly (avoids circular imports between
    ``outputs/`` and ``schemas/``).
    """
    identity = getattr(descriptor, "identity", None)
    if identity is None and isinstance(descriptor, dict):
        identity = descriptor.get("identity", {})
    if identity is None:
        raise ValueError("descriptor has no .identity")

    analyst_id = getattr(identity, "id", None)
    if analyst_id is None and isinstance(identity, dict):
        analyst_id = identity.get("id")
    if not analyst_id:
        raise ValueError("descriptor.identity has no id")

    analyst_version = getattr(identity, "version", None)
    if analyst_version is None and isinstance(identity, dict):
        analyst_version = identity.get("version", "")

    outputs = getattr(descriptor, "outputs", None)
    if outputs is None and isinstance(descriptor, dict):
        outputs = descriptor.get("outputs", [])
    return str(analyst_id), str(analyst_version or ""), list(outputs or [])


def _validate_args_against_schema(
    args: dict[str, Any], schema: dict[str, Any]
) -> str | None:
    """Lightweight required-field + type check.

    Production deployments may wire `jsonschema` for full coverage; the
    SDK ships without that dep so this stays at the level the legacy
    server enforced (presence + obvious type mismatch).
    """
    required = schema.get("required") or []
    if isinstance(required, list):
        missing = [name for name in required if name not in args]
        if missing:
            return f"missing required argument(s): {', '.join(missing)}"

    props = schema.get("properties") or {}
    if isinstance(props, dict):
        for name, prop_schema in props.items():
            if name not in args:
                continue
            expected = prop_schema.get("type") if isinstance(prop_schema, dict) else None
            if expected is None:
                continue
            if not _matches_jsonschema_type(args[name], expected):
                return f"argument {name!r} must be of type {expected}"
    return None


def _matches_jsonschema_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    return True  # unknown type — defer to MCP-side strictness


async def _maybe_await(value: Any) -> Any:
    """Allow dispatchers to be either sync or async callables (test ergonomics)."""
    if inspect.isawaitable(value):
        return await value
    return value


def _format_text_result(result: Any) -> str:
    """Format a dispatcher's return value into MCP `TextContent.text`."""
    if isinstance(result, str):
        return result
    if isinstance(result, BaseModel):
        return result.model_dump_json(indent=2)
    if isinstance(result, (dict, list)):
        try:
            return json.dumps(result, indent=2, default=_json_default)
        except (TypeError, ValueError):
            return repr(result)
    if result is None:
        return "(no output)"
    return str(result)


def _json_default(value: Any) -> Any:
    # Mirror provenance/writes.py's pattern — fall back to str() for non-JSON-native types.
    return str(value)


__all__ = [
    "KIND_NAME",
    "McpToolConfig",
    "McpToolMode",
    "MCPToolRegistry",
    "MCP_TOOL_REGISTRY",
    "RegisteredMcpTool",
    "OnDemandDispatcher",
    "LatestOutputProvider",
    "parse_config",
]
