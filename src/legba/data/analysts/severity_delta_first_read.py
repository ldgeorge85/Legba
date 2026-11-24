# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The FIRST read's movement call, decided in code (roll 83).

Every desk prompt that asks for a ``severity_delta:<rose|fell|steady|new>`` tag
defines ``new`` as "you have no prior read of this dimension to compare
against — never 'steady', which claims a comparison you did not make". Whether
a prior read EXISTS is not a judgement: it is a row in ``analyst_outputs`` or
it is not. The cyber desk's first run (roll 82) tagged ``steady`` with no prior
finding anywhere — a comparison it could not have made — and the steady-state
paging guard reads ``steady`` as "the desk checked and it held".

So when this analyst has never written a finding for this target, code sets the
call to ``new``, replacing whatever movement the model claimed. Nothing else
changes: a finding WITHOUT a ``severity_delta`` tag stays without one (a desk
whose prompt does not ask for it, or a model that skipped it — "nobody said"
is not "new"), and a finding with a prior read keeps the model's call.

A leaf module: ``inline_target`` calls :func:`settle_first_read_delta` once,
before NARRATE, and appends the step it returns (if any) to the trace.
"""
from __future__ import annotations

import logging
from typing import Any

from ..provenance.models import _SEVERITY_DELTA_TAG_PREFIX, severity_delta_from_tags

logger = logging.getLogger(__name__)

#: One indexed probe (``idx_analyst_outputs_analyst_id``), short-circuited by
#: EXISTS: ~0.2 ms on live when a prior finding exists, ~110 ms worst case (a
#: large analyst's first read of a new target), once per run.
_PRIOR_FINDING_SQL = (
    "SELECT EXISTS (SELECT 1 FROM analyst_outputs"
    " WHERE analyst_id = $1 AND kind = 'finding'"
    " AND target_id IS NOT DISTINCT FROM $2)"
)


async def has_prior_finding(pg: Any, *, analyst_id: str, target_id: str | None) -> bool | None:
    """Whether this analyst has any finding for this target; ``None`` = unknown."""
    try:
        got = await pg.fetchval(_PRIOR_FINDING_SQL, analyst_id, target_id)
        return got if isinstance(got, bool) else None  # only a real answer decides
    except Exception as exc:  # noqa: BLE001 — unknown leaves the model's call alone
        logger.warning("severity_delta_first_read.probe_failed analyst_id=%s err=%s", analyst_id, exc)
        return None


def first_read_tags(tags: list[str]) -> list[str]:
    """``tags`` with every ``severity_delta:`` entry replaced by one ``severity_delta:new``."""
    out = [t for t in tags if not (isinstance(t, str) and t.strip().lower().startswith(_SEVERITY_DELTA_TAG_PREFIX))]
    return out + [f"{_SEVERITY_DELTA_TAG_PREFIX}new"]


async def settle_first_read_delta(
    pg: Any, finding: Any, *, analyst_id: str | None, target_id: str | None,
) -> tuple[Any, dict[str, Any] | None]:
    """``(finding, trace_step | None)``. Rewrites the call to ``new`` only when
    the model emitted one that is not ``new`` AND this analyst has no finding
    for this target. No pg, no analyst id, or a failed probe: unchanged."""
    claimed = severity_delta_from_tags(list(finding.tags or []))
    if claimed is None or claimed == "new" or pg is None or not analyst_id:
        return finding, None
    prior = await has_prior_finding(pg, analyst_id=str(analyst_id), target_id=target_id)
    if prior is not False:
        return finding, None
    step = {"phase": "narrate", "kind": "severity_delta_first_read", "claimed": claimed, "set": "new"}
    return finding.model_copy(update={"tags": first_read_tags(list(finding.tags))}), step
