# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``journal_read`` pack — the journal_assessor's GOVERNED read surface.

Wave 0 (planning/JOURNAL_ASSESSOR_PLAN.md §5 / §12) is deliberately MINIMAL:
ONE reused existing tool — ``list_findings`` — enough for the journal to narrate
truthfully over the platform's OWN finished intelligence (recent findings +
meta-findings; the cross_analyst_correlator contradiction/agreement/blind-spot
outputs are the richest fuel). The ~9 net-new self-instrument tools (assessments,
graph_mining, structural_balance, critic_scores, calibration, run_health,
source_health, budget_status, get_journal_delta) are Wave 1 — each is its own
end-to-end build (port method + tool + pack + _KNOWN_TOOLS + registry PUT on all
four surfaces).

WHY a SEPARATE pack (not just granting substrate_read): the §7.6 grant invariant.
The journal must be granted ONLY this read pack (and later the journal_proposals
pack) so it is structurally NOT effective for any pack whose tools call
write_fact / write_nexus / write_hypothesis — the grant-layer backstop for the
never-write-a-fact invariant (§3.1).

FOUR-SURFACE convergence (memory: consult-tools-must-be-pack-tools). The drift
guard is REAL — a tool not in the live pack blocks as ``unknown_tool`` even on
the governed path. The four surfaces must agree:

  1. the in-code TUPLE (``JOURNAL_READ_TOOLS``) — below;
  2. the DESCRIPTOR (``descriptors/action_pack_journal_read.yaml``);
  3. the HANDLERS (``register_journal_read_tools`` — reuses the substrate_read
     ``list_findings_tool`` handler, which the global ``default_tool_registry``
     already registers; this module re-registers the same callable so the
     per-pack drift test can assert tuple == registered handlers);
  4. every tool that the journal's run_method can dispatch ∈ this pack.

The handler is the SAME ``list_findings_tool`` the substrate_read pack uses — a
tool name maps to one global handler; the pack is the GRANT/governance boundary,
not a second copy of the handler.
"""

from __future__ import annotations

import logging

from .substrate_read import list_findings_tool
from .tools import ToolRegistry

logger = logging.getLogger(__name__)

JOURNAL_READ_PACK_ID = "journal_read"

# Wave 0: ONE reused tool. Keep this tuple == the descriptor's `tools` names ==
# the handlers registered below (the per-pack drift guard asserts it).
JOURNAL_READ_TOOLS = (
    "list_findings",
)


def register_journal_read_tools(registry: ToolRegistry) -> None:
    """Register the journal_read pack's tool handlers.

    Wave 0 reuses the substrate_read ``list_findings_tool`` handler (one tool
    name → one global handler; the pack is the grant boundary, not a handler
    copy). Called by ``default_tool_registry`` so the global registry carries
    the handler whether or not substrate_read is also registered (register is
    idempotent — a repeated name overwrites with the same callable).
    """
    registry.register("list_findings", list_findings_tool)


__all__ = [
    "JOURNAL_READ_PACK_ID",
    "JOURNAL_READ_TOOLS",
    "register_journal_read_tools",
]
