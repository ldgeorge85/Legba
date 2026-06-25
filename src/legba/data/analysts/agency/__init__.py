# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Action-pack agency mechanism (P-11 / PIVOT §4.8).

The Claude-Code-skill model, made declarative + governed. An analyst declares
``action_packs`` (what it MAY use); a target/domain declares
``allowed_action_packs`` (what the context PERMITS); a pack declares its own
applicability + governor (when it's RELEVANT + how much it may spend). Effective
agency is the gated intersection.

Public surface:

  * :class:`Agency` / :func:`Agency.run_pack_tool` — the ONE entry point that
    resolves → governs → dispatches a pack tool, emitting operator-visible
    governor events on every decision (the hard gate).
  * :func:`resolve_pack` + :class:`PackResolution` — the three-way allow-list.
  * :class:`PackGovernorEnforcer` — per-pack rate/budget enforcement over the
    ``action_pack_invocations`` ledger + the global token envelope.
  * :class:`ToolRegistry` / :func:`default_tool_registry` + the seed handlers
    (process_media → job plane; escalate/create_incident → channels; the
    four ``substrate_read`` read tools → SubstrateQueryPort).
  * :class:`AgencyToolBinding` (A-3) — the per-analyst production on-ramp:
    consult routes its ReAct tool calls through a ``substrate_read``
    binding; the actor run path fires the ``escalate_finding`` pack through
    one when a landed finding crosses the severity gate.
  * :class:`GovernorEvent` / :func:`record_governor_event` /
    :func:`recent_events` — the operator-visible event log.

Migration 0025 (``action_pack_invocations`` + ``governor_events``) is the
governor's substrate; it extends the 0022 budget envelope rather than
replacing it.
"""

from __future__ import annotations

from .agency import Agency, AgencyOutcome
from .binding import (
    GLOBAL_SCOPE,
    AgencyToolBinding,
    escalation_gate_decision,
    fetch_action_pack,
    grants_include,
)
from .events import (
    GOVERNOR_EVENTS_SUBJECT_PREFIX,
    GovernorEvent,
    recent_events,
    record_governor_event,
)
from .governor import GovernorDecision, PackGovernorEnforcer
from .resolution import (
    PackResolution,
    TargetScopeView,
    resolve_pack,
    scope_view_from_target,
)
from .tools import (
    ChannelEmitter,
    ToolCall,
    ToolContext,
    ToolHandler,
    ToolRegistry,
    ToolResult,
    WritebackContext,
    default_tool_registry,
)

__all__ = [
    "Agency",
    "AgencyOutcome",
    "AgencyToolBinding",
    "GLOBAL_SCOPE",
    "ChannelEmitter",
    "escalation_gate_decision",
    "fetch_action_pack",
    "grants_include",
    "GOVERNOR_EVENTS_SUBJECT_PREFIX",
    "GovernorDecision",
    "GovernorEvent",
    "PackGovernorEnforcer",
    "PackResolution",
    "TargetScopeView",
    "ToolCall",
    "ToolContext",
    "ToolHandler",
    "ToolRegistry",
    "ToolResult",
    "WritebackContext",
    "default_tool_registry",
    "recent_events",
    "record_governor_event",
    "resolve_pack",
    "scope_view_from_target",
]
