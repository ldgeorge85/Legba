# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.tools — analyst tool registry.

Tools are units of work the runtime resolves for analyst kinds that
whitelist them on their descriptor (per topology v2 §4.6, n8n
cluster-node pattern; per `legba_kind_contracts.md` §5 — the `Tool`
protocol). Unlike output kinds (which materialize results into a
surface), tools are *invoked from inside* the analyst's `run` phase to
fetch auxiliary signal — trust scores, lookups, etc.

This package collects each tool as a standalone async callable plus a
typed deps shape. The runtime owns wiring: when an analyst declares
`tools.whitelist = ["mnemosyne_trust_query", ...]` the runtime
constructs the deps bundle for each tool from the descriptor's
`Property.Secret` / `Property.StackRef` references and registers the
callable under `deps.tools[TOOL_NAME]`.

First tool: L-211 `mnemosyne_trust_query` (moved Phase 10 → Phase 6
2026-05-20 so the resolution mechanism exists when analyst kinds are
built, not bolted on later).

Per-tool LLM-facing schema (L-175 critic tool-threading)
--------------------------------------------------------

The L-175 critic kind passes ``tools=[{name, description, input_schema},
...]`` to the Anthropic Messages API so the judge LLM can invoke
whitelisted tools mid-grade.  Each tool's LLM-facing schema is declared
HERE (in the registry shim) rather than in the tool module itself —
keeping the tool module a pure async callable with no LLM-shape
coupling.  :func:`get_tool_definition` returns the Anthropic-shaped
dict for one tool name; the critic builds the ``tools=[...]`` list by
mapping over its descriptor's ``method.tools_whitelist``.
"""

from __future__ import annotations

from typing import Any

from .mnemosyne_trust_query import (
    TOOL_NAME as MNEMOSYNE_TRUST_QUERY_TOOL_NAME,
    MnemosyneTrustQueryDeps,
    MnemosyneTrustQueryError,
    call as mnemosyne_trust_query,
)


# ---------------------------------------------------------------------------
# LLM-facing tool definitions (Anthropic Messages API shape)
# ---------------------------------------------------------------------------
#
# Per-tool ``{name, description, input_schema}`` triples the critic kind
# (and any future ReAct-loop kinds with a tools_whitelist) hand to
# ``LLMProviderHandler.chat_complete(tools=...)``.  Each entry's
# ``input_schema`` matches the tool module's ``call()`` argument shape;
# any drift between the schema and the runtime ``call()`` surfaces as a
# tool-side validation error (e.g. :class:`MnemosyneTrustQueryError`
# from ``_validate_args``), which the critic loop folds into a
# ``tool_result`` block so the LLM can recover.
#
# Keys are :data:`TOOL_NAME` strings (the same strings analysts whitelist
# in ``descriptor.method.tools_whitelist``).

_MNEMOSYNE_TRUST_QUERY_DEFINITION: dict[str, Any] = {
    "name": MNEMOSYNE_TRUST_QUERY_TOOL_NAME,
    "description": (
        "Query the Mnemosyne federation for an aggregated trust score on "
        "a peer DID. Returns {weight: float (0.0 = no trust, 1.0 = full "
        "trust), hops: int (-1 = unreachable)}. Returns {error: "
        "\"chain_unavailable\"} when the trust chain primitive is offline "
        "or {error: \"transport_error\"} on network failure. Use to "
        "inform the confidence/calibration dimension of a critique when "
        "the analyzed output cites a federated source."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "peer_did": {
                "type": "string",
                "description": (
                    "did:key:z... identifier of the peer being scored."
                ),
            },
            "scope": {
                "type": "string",
                "enum": [
                    "general",
                    "data_access",
                    "delegation",
                    "group_membership",
                ],
                "description": (
                    "Trust scope to query. Defaults to 'general' when "
                    "omitted."
                ),
                "default": "general",
            },
        },
        "required": ["peer_did"],
    },
}

#: Registry of LLM-facing tool definitions keyed by ``TOOL_NAME``.
#:
#: Operators registering a new tool add the ``{name, description,
#: input_schema}`` entry here and the analyst kinds' ReAct loops pick
#: it up automatically (no per-kind code change).
TOOL_DEFINITIONS: dict[str, dict[str, Any]] = {
    MNEMOSYNE_TRUST_QUERY_TOOL_NAME: _MNEMOSYNE_TRUST_QUERY_DEFINITION,
}


def get_tool_definition(tool_name: str) -> dict[str, Any] | None:
    """Return the LLM-facing ``{name, description, input_schema}`` for one tool.

    Returns ``None`` when the tool isn't in the registry — the caller
    (typically the critic's ReAct loop) skips unknown names with a
    warning so an operator-side typo in the descriptor's
    ``tools_whitelist`` doesn't crash the analyst run.
    """

    definition = TOOL_DEFINITIONS.get(tool_name)
    if definition is None:
        return None
    # Return a shallow copy so callers can't mutate the registry entry.
    return {
        "name": definition["name"],
        "description": definition["description"],
        "input_schema": dict(definition["input_schema"]),
    }


__all__ = [
    "MNEMOSYNE_TRUST_QUERY_TOOL_NAME",
    "MnemosyneTrustQueryDeps",
    "MnemosyneTrustQueryError",
    "TOOL_DEFINITIONS",
    "get_tool_definition",
    "mnemosyne_trust_query",
]
