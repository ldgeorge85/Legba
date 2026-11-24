# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult write executor's ``unit`` BRANCH (C-2b-1, W6; design C-2a §1 row 3).

A unit is a ``Family.ANALYST`` head whose kind is NOT a composition kind (and
not a consult surface). Verbs: update · transition · retire · rerun · revert
(create stays SEAMS #64 — a new unit is a design act, not a turn).

Writable (closed; ``consult_write_vocabulary.UNIT_WRITABLE``): the
``method.options`` knobs the option catalog declares for the unit's kind (or
sub-handler), the slice window ``subscription.targets.time_window``, the
rubric ``eval.rubric``, and the inline prompt ``method.system_prompt`` unless
the unit is pinned (D9 → ``protected_field``). Tier B: pausing an active unit,
retire, any prompt edit.

The machinery is shared with the composition branch:
:mod:`legba.data.registry.consult_write_analyst`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from . import consult_write_analyst as A

__all__ = ["FAMILY", "VERBS", "activation", "inspect", "plan", "plan_revert"]

CLS = "unit"

#: The verbs this branch executes — the drift guard holds them equal to the
#: vocabulary's built verbs for ``unit``.
VERBS: frozenset[str] = frozenset({"inspect", "update", "transition", "retire", "rerun", "revert"})

FAMILY = A.FAMILY


async def inspect(registry: Any, unit_id: str, *, session_id: str | None) -> dict[str, Any]:
    return await A.inspect(registry, CLS, unit_id, session_id=session_id)


async def plan(registry: Any, verb: str, args: Mapping[str, Any]) -> Any:
    return await A.plan(registry, CLS, verb, args)


async def plan_revert(registry: Any, original: Mapping[str, Any], newer: str | None) -> Any:
    return await A.plan_revert(registry, CLS, original, newer)


async def activation(
    registry: Any, unit_id: str, version: str, since: datetime, *, parse_check: Any = None,
) -> dict[str, Any] | None:
    """D10. ``parse_check`` is the executor's source parse and is not used: an
    analyst head is probed with the runtime's analyst parse (``A.runtime_parse``)."""
    del parse_check
    return await A.activation(registry, unit_id, version, since)
