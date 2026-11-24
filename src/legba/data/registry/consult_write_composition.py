# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult write executor's ``composition`` BRANCH (C-2b-1, W6; design C-2a §1 row 4).

A composition is a ``Family.ANALYST`` head whose kind is a composition kind
(``consult_write_vocabulary.COMPOSITION_KINDS``): it composes by quotation
over other analysts' heads. Verbs: update · transition · retire · rerun ·
revert.

Writable (closed; ``consult_write_vocabulary.COMPOSITION_WRITABLE``): its
inputs (``subscription.other_analysts`` — the slice membership, given as a
list of analyst ids; each new one must exist and not be retired), the input
window (``subscription.other_analysts.time_window``, set on every input), the
``method.options`` knobs the catalog declares for its kind (the tier caps and
regime switches), and an inline prompt as for units. Tier B: any membership
edit (it moves the tower), pausing an active composition, retire, any prompt
edit.

The machinery is shared with the unit branch:
:mod:`legba.data.registry.consult_write_analyst`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from . import consult_write_analyst as A

__all__ = ["FAMILY", "VERBS", "activation", "inspect", "plan", "plan_revert"]

CLS = "composition"

#: The verbs this branch executes — the drift guard holds them equal to the
#: vocabulary's built verbs for ``composition``.
VERBS: frozenset[str] = frozenset({"inspect", "update", "transition", "retire", "rerun", "revert"})

FAMILY = A.FAMILY


async def inspect(registry: Any, composition_id: str, *, session_id: str | None) -> dict[str, Any]:
    return await A.inspect(registry, CLS, composition_id, session_id=session_id)


async def plan(registry: Any, verb: str, args: Mapping[str, Any]) -> Any:
    return await A.plan(registry, CLS, verb, args)


async def plan_revert(registry: Any, original: Mapping[str, Any], newer: str | None) -> Any:
    return await A.plan_revert(registry, CLS, original, newer)


async def activation(
    registry: Any, composition_id: str, version: str, since: datetime, *, parse_check: Any = None,
) -> dict[str, Any] | None:
    """D10, as for units (the runtime's analyst parse; the first trace at the new version)."""
    del parse_check
    return await A.activation(registry, composition_id, version, since)
