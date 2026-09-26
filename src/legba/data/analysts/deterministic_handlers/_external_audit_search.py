# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE SEARCH LEG of the external audit — one query, and the rung it ran on.

Split out of ``_external_audit_width`` when the PAID RUNG arrived: the ladder
is a concern of its own (which provider answers a claim, and what it is allowed
to cost), and the width module was at the size gate's threshold. It is a LEAF —
it imports from the grader and the queue and from nothing else in this package,
and ``_external_audit_width`` imports it ONE WAY and re-exports both names, so
every existing call site resolves unchanged.

THE LADDER, IN ONE PLACE:

  * rung 0 is ``serp_provider_order[0]`` — SearXNG, free, and the rung EVERY
    claim's primary query runs on. Nothing here changes that.
  * every rung after it is METERED, and is reached only by an explicit pin
    (:func:`escalate_to_paid_rung`). The ``web_search`` tool's own ladder
    escalates when a rung FAILS; the case the audit needs is the opposite one —
    rung 0 ANSWERED, with an empty nobody can believe — so the escalation is
    driven from here and never from a fallback.
  * a refusal at any rung (no spend cap declared, an undeclared rung, an
    unresolved key, a timeout) comes back as a REASON STRING. None of them is
    an empty result set: "we could not afford to look" and "we looked and found
    nothing" must never share a shape.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping, Sequence

from ._external_audit_grader import EvidenceEnvelope

logger = logging.getLogger(__name__)

#: One query's ceiling. The tool has its own timeout; this is the backstop for
#: a binding that hangs somewhere the tool's own does not cover. Defined HERE
#: and re-exported by ``_external_audit_width``, so there is one number.
SEARCH_TIMEOUT_SECONDS = 45.0


# ---------------------------------------------------------------------------
# The search leg — one query, through the real pack binding
# ---------------------------------------------------------------------------


async def run_search(
    binding: Any, query: str, *, limit: int, provider_order: Sequence[str],
    provider: str = "",
) -> tuple[EvidenceEnvelope | None, str]:
    """One ``web_search`` through the real agency binding.

    Returns ``(envelope, unchecked_reason)``. A non-empty reason means the search
    plane never answered — a BLOCK at the gate, a tool failure, a
    degraded/unverified empty — and the claim is recorded UNCHECKED rather than
    graded. That is the pack's own empty-is-suspect doctrine reaching the verdict
    vocabulary: "the search found nothing" and "the search did not happen" must
    never share a shape.

    ``provider_order`` is the SERP ladder; ``provider_order[0]`` is the rung a
    call with no explicit ``provider`` lands on, and it is only a LABEL here —
    the tool's own ToolSpec decides rung 0.

    ``provider`` PINS this call to one named rung (``"serper"``, or a full
    component id). It is how the paid rung is reached at all: the tool's
    generic ladder escalates when a rung FAILS, and the case this audit needs
    is the opposite one — rung 0 ANSWERED, with an empty nobody can believe.
    An unpinned call behaves exactly as it always did.
    """
    args: dict[str, Any] = {"query": query, "limit": limit}
    if provider:
        args["provider"] = provider
    try:
        outcome = await asyncio.wait_for(
            binding.run_tool("web_search", args),
            timeout=SEARCH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return None, "web_search timed out"
    except Exception as exc:
        logger.warning("external_audit.width_search_failed err=%s", exc)
        return None, f"web_search raised: {exc}"

    if not getattr(outcome, "admitted", False):
        cause = getattr(outcome, "block_cause", None) or "blocked"
        return None, f"agency gate blocked web_search: {cause}"
    result = getattr(outcome, "tool_result", None)
    if result is None:
        return None, "web_search returned no tool result"
    output = dict(getattr(result, "output", None) or {})
    status = {
        k: output.get(k)
        for k in (
            "status", "degraded", "degraded_detail", "unresponsive_engines",
            "liveness", "liveness_detail", "supports_absence_claim",
            "absence_statement", "absence_warning", "provider", "count",
        )
        if k in output
    }
    if getattr(result, "status", "") != "completed":
        return None, str(getattr(result, "error", "") or "web_search failed")
    results = tuple(
        dict(r) for r in (output.get("results") or []) if isinstance(r, Mapping)
    )
    # The rung that ACTUALLY answered, off the tool's own output — this is
    # what reaches ``external_grades.search_provider`` and is the audit
    # ledger's record of which rung decided the claim.
    answered_by = str(
        output.get("provider")
        or (provider or (provider_order[0] if provider_order else ""))
    )
    return EvidenceEnvelope(
        query=query, results=results, search_status=status,
        provider=answered_by,
    ), ""


#: The rungs below rung 0, i.e. everything the audit may ESCALATE to. Rung 0
#: is free and carries the majority of claims; every rung after it is metered.
def paid_rungs(provider_order: Sequence[str]) -> tuple[str, ...]:
    """``provider_order`` minus rung 0. Empty ⇒ escalation is INERT."""
    return tuple(str(r).strip() for r in list(provider_order)[1:] if str(r).strip())


async def escalate_to_paid_rung(
    binding: Any, query: str, *, limit: int, provider_order: Sequence[str],
) -> tuple[EvidenceEnvelope | None, str]:
    """ONE query on the first paid rung, for a claim rung 0 could not decide.

    WHEN THIS RUNS — and it is narrow on purpose:

      * the claim is ABSENCE-SHAPED and rung 0's envelope does not support an
        absence claim (SearXNG returned empty and could not prove its engine
        set was alive), or
      * rung 0 graded NOT_FOUND and offered NO reformulation to try, or
      * (2026-09-21/3) rung 0 graded NOT_FOUND and DID offer a reformulation —
        that query also runs here when a paid rung is declared, because the
        second search is worth spending only on an index that can change the
        answer.

    All three are cases where the claim currently ends unusable having spent
    ONE search. So the escalation spends the SECOND search per claim — the
    slot a NOT_FOUND reformulation would otherwise have spent (and the one it
    still spends, on this rung, when a paid rung is declared) — and never
    twice: worst-case egress per claim stays ``EGRESS_CALLS_PER_CLAIM``
    (primary + one second search + one span fetch), which is the number
    ``GOVERNOR_MAX_CLAIMS_PER_TICK`` and the day's ``max_serp_per_day``
    budget are both derived from. Raising it would silently overrun a
    reservation the queue made before the tick started.

    NEVER a general fallback. A rung 0 that FAILED is already handled by the
    tool's own ladder; a rung 0 that ANSWERED a decidable claim is never
    re-asked, because re-asking it would spend money to confirm an answer we
    already have.

    Returns ``(envelope, unchecked_reason)`` exactly like :func:`run_search`.
    Every refusal reaches here as a reason string — a missing spend cap
    (``search_cost_no_cap_declared``), an undeclared rung
    (``search_provider_not_declared``), an unresolved key — and NONE of them
    is an empty result set. The caller keeps rung 0's verdict in that case
    rather than inventing a stronger one.
    """
    rungs = paid_rungs(provider_order)
    if not rungs:
        return None, "no paid rung declared"
    return await run_search(
        binding, query, limit=limit, provider_order=provider_order,
        provider=rungs[0],
    )


__all__ = [
    "SEARCH_TIMEOUT_SECONDS",
    "escalate_to_paid_rung",
    "paid_rungs",
    "run_search",
]
