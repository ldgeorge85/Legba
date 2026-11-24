# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``consult_write`` pack's handlers — THIN CLIENTS (C-2b-1; design C-2a §1, §6).

A consult write executes in ONE place: the registry's executor
(``registry/consult_write_exec.py``), which owns the descriptor registry, the
signing identity and the ledger. These handlers run in the runtime, inside the
agency hard gate (grant, known tool, closed grammar, governor, ledger row +
ALLOW event), and do one thing: hand the gate-validated arguments to the
:class:`~legba.runtime.consult_writes_client.ConsultWritesClient` wired on the
binding's ``ToolContext``, which preflights and posts. What comes back — an
inspect payload, ``executed`` with its receipt, ``pending_confirmation``, or a
refusal with its cause — is returned as the tool's output; the consult write
gate (``analysts/consult_write_gate.py``) turns it into what the model reads.

The request a call belongs to is read off ``call.requested_by`` (the binding
re-pointed per request names it), never off the arguments: the model cannot
choose which run its write counts against. The turn's running cost rides
:data:`WRITE_TURN`, set by the gate around the call.

One named module-level handler per tool, registered by LITERAL name: the F-7
guard (``tests/data_pkg/test_tool_schema_matches_handlers.py``) indexes
``registry.register("<tool>", fn)`` by AST and proves each handler reads
exactly the properties its closed schema declares.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from .tools import ToolCall, ToolContext, ToolRegistry, ToolResult

__all__ = ["CONSULT_WRITE_TOOLS", "WRITE_TURN", "register_consult_write_tools"]

CONSULT_WRITE_TOOLS: tuple[str, ...] = (
    "inspect_object", "create_object", "update_object", "transition_object", "revert_write",
    "rerun_object", "edit_entity", "merge_entities", "unmerge_entity", "assert_fact", "close_fact",
)

#: ``{"turn_cost_usd": float | None}`` for the call in flight (set by the gate).
WRITE_TURN: ContextVar[dict[str, Any] | None] = ContextVar("consult_write_turn", default=None)


async def _submit(tool: str, call: ToolCall, ctx: ToolContext, payload: dict[str, Any]) -> ToolResult:
    client = getattr(ctx, "consult_writes", None)
    if client is None:
        return ToolResult(
            status="failed",
            error="consult_writes client not wired on this binding — the consult_write "
                  "pack is bound only on the workstation consult (runtime/consult_bindings)",
        )
    turn = WRITE_TURN.get() or {}
    out = await client.submit(
        tool=tool, args=payload, requested_by=call.requested_by,
        turn_cost_usd=turn.get("turn_cost_usd"),
    )
    return ToolResult(status="completed", output=dict(out), units=1)


async def inspect_object_tool(call: ToolCall, pack: Any, ctx: ToolContext) -> ToolResult:
    del pack
    args = call.args
    return await _submit("inspect_object", call, ctx, {"class": args.get("class"), "id": args.get("id")})


async def create_object_tool(call: ToolCall, pack: Any, ctx: ToolContext) -> ToolResult:
    del pack
    args = call.args
    return await _submit("create_object", call, ctx, {
        "class": args.get("class"), "body": args.get("body"), "reason": args.get("reason"),
    })


async def update_object_tool(call: ToolCall, pack: Any, ctx: ToolContext) -> ToolResult:
    del pack
    args = call.args
    return await _submit("update_object", call, ctx, {
        "class": args.get("class"), "id": args.get("id"),
        "expected_version": args.get("expected_version"), "patch": args.get("patch"),
        "reason": args.get("reason"),
    })


async def transition_object_tool(call: ToolCall, pack: Any, ctx: ToolContext) -> ToolResult:
    del pack
    args = call.args
    return await _submit("transition_object", call, ctx, {
        "class": args.get("class"), "id": args.get("id"),
        "expected_version": args.get("expected_version"), "to_state": args.get("to_state"),
        "reason": args.get("reason"),
    })


async def revert_write_tool(call: ToolCall, pack: Any, ctx: ToolContext) -> ToolResult:
    del pack
    args = call.args
    return await _submit("revert_write", call, ctx, {
        "write_id": args.get("write_id"), "reason": args.get("reason"),
    })


async def rerun_object_tool(call: ToolCall, pack: Any, ctx: ToolContext) -> ToolResult:
    del pack
    args = call.args
    return await _submit("rerun_object", call, ctx, {
        "class": args.get("class"), "id": args.get("id"), "subject": args.get("subject"),
        "reason": args.get("reason"),
    })


async def edit_entity_tool(call: ToolCall, pack: Any, ctx: ToolContext) -> ToolResult:
    del pack
    args = call.args
    return await _submit("edit_entity", call, ctx, {
        "entity_id": args.get("entity_id"), "expected_version": args.get("expected_version"),
        "patch": args.get("patch"), "reason": args.get("reason"),
    })


async def merge_entities_tool(call: ToolCall, pack: Any, ctx: ToolContext) -> ToolResult:
    del pack
    args = call.args
    return await _submit("merge_entities", call, ctx, {
        "keeper_id": args.get("keeper_id"), "loser_id": args.get("loser_id"), "reason": args.get("reason"),
    })


async def unmerge_entity_tool(call: ToolCall, pack: Any, ctx: ToolContext) -> ToolResult:
    del pack
    args = call.args
    return await _submit("unmerge_entity", call, ctx, {
        "loser_id": args.get("loser_id"), "reason": args.get("reason"),
    })


async def assert_fact_tool(call: ToolCall, pack: Any, ctx: ToolContext) -> ToolResult:
    """W11 C-2b-2/facts — the operator's fact, ``source_type='operator'`` (the executor's fact branch)."""
    del pack
    args = call.args
    return await _submit("assert_fact", call, ctx, {
        "subject": args.get("subject"), "predicate": args.get("predicate"), "value": args.get("value"),
        "valid_from": args.get("valid_from"), "confidence": args.get("confidence"),
        "supersede": args.get("supersede"), "cites": args.get("cites"), "reason": args.get("reason"),
    })


async def close_fact_tool(call: ToolCall, pack: Any, ctx: ToolContext) -> ToolResult:
    del pack
    args = call.args
    return await _submit("close_fact", call, ctx, {
        "fact_id": args.get("fact_id"), "reason": args.get("reason"),
    })


_NAMED_HANDLERS: dict[str, Any] = {
    "inspect_object": inspect_object_tool,
    "create_object": create_object_tool,
    "update_object": update_object_tool,
    "transition_object": transition_object_tool,
    "revert_write": revert_write_tool,
    "rerun_object": rerun_object_tool,
    "edit_entity": edit_entity_tool,
    "merge_entities": merge_entities_tool,
    "unmerge_entity": unmerge_entity_tool,
    "assert_fact": assert_fact_tool,
    "close_fact": close_fact_tool,
}
assert set(_NAMED_HANDLERS) == set(CONSULT_WRITE_TOOLS)


def register_consult_write_tools(registry: ToolRegistry) -> None:
    """Register the thin clients on the agency tool registry."""
    registry.register("inspect_object", inspect_object_tool)
    registry.register("create_object", create_object_tool)
    registry.register("update_object", update_object_tool)
    registry.register("transition_object", transition_object_tool)
    registry.register("revert_write", revert_write_tool)
    registry.register("rerun_object", rerun_object_tool)
    registry.register("edit_entity", edit_entity_tool)
    registry.register("merge_entities", merge_entities_tool)
    registry.register("unmerge_entity", unmerge_entity_tool)
    registry.register("assert_fact", assert_fact_tool)
    registry.register("close_fact", close_fact_tool)
