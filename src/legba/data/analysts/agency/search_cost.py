# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The brake on a PAID search rung — the governor's day-cost cap, pre-spend.

The provider ladder can escalate from a $0 rung (SearXNG) to a METERED one
(Brave). Every existing governor cap is enforced BEFORE a pack tool is
dispatched (:meth:`~.governor.PackGovernorEnforcer.precall_check`), against an
``estimated_cost_usd`` the CALLER supplies — and every caller of ``web_search``
supplies the default, ``0.0``, because until now a search cost nothing. That is
correct for what it was and useless for what a paid rung needs: the tool cannot
know at admit time whether it will end up on the free rung or the paid one, so
an estimate stamped before dispatch would be wrong in one direction or the
other every single time.

So the cap is enforced a second time, INSIDE the ladder, at the only moment the
true cost is known: after rung 0 has failed and immediately before a metered
rung would issue its query. The mechanism is deliberately NOT a new budget —
it reads the SAME ``action_pack_invocations`` ledger over the SAME UTC-day
window as ``PackGovernorEnforcer._cost_today``, and the cap it compares against
is the pack's own ``governor.max_cost_usd_per_day``. One number, one table, two
enforcement points.

FAIL-CLOSED, NOT FAIL-QUIET
---------------------------
Three states, and only one of them spends money:

* **cap absent** — the pack declares no ``max_cost_usd_per_day``. The paid rung
  is REFUSED. This is the deliberate inversion of the governor's own "a cap
  left ``None`` is unenforced" rule, and the reason is that the two situations
  are not alike: an unenforced *rate* cap costs nothing, while an unenforced
  *spend* cap on a metered external API is an open tab. An operator who wants
  the paid rung declares what they are willing to spend on it.
* **no ledger bound** — the caller wired no :class:`SearchCostLedger` (a
  process that never assembled a pg pool for this pack). The paid rung is
  REFUSED, loudly, naming the gap. Never "spend and hope"; the base handler
  contract already states that a metered provider must hard-fail rather than
  silently bill.
* **cap present and ledger bound** — spend the day's ledger, project this
  call, and admit only if the projection stays inside the cap.

A refusal here is a SKIPPED RUNG, not a dead run: the ladder records why and
carries on, and if no rung answered the tool's failure names the cap. That
keeps the two failure modes distinguishable, which is the whole house rule —
"we could not afford to look" must never be reported as "we looked and found
nothing".

KNOWN BOUND — PRE-CALL, NOT A RESERVATION
------------------------------------------
This reads the day's spend and projects one query; it does not hold a claim on
the budget. Two concurrent ``web_search`` calls can therefore both observe the
same pre-spend total and both proceed, overshooting the cap by at most
(concurrency - 1) x ``cost_usd_per_query``. That is the SAME property
``PackGovernorEnforcer.precall_check`` has had all along, and it is named here
rather than papered over: at Brave's list price the worst-case overshoot is
cents, and the ledger still records what was actually spent. Making it exact
needs a reservation row per in-flight paid query — a heavier mechanism than this
exposure justifies, and the thing to build FIRST if a materially more expensive
rung is ever added.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


def _today_utc() -> date:
    return datetime.now(tz=timezone.utc).date()


@runtime_checkable
class SearchCostLedger(Protocol):
    """What the ladder needs in order to price a metered rung honestly.

    One method, so a test double is three lines and the production
    implementation is the one below. Deliberately narrow: the ladder must be
    able to ask "how much has this budget account spent on this pack today"
    without acquiring the right to write anything.
    """

    async def spent_today_usd(self, *, pack_id: str, budget_account: str) -> float:
        ...


@dataclass(frozen=True)
class SearchCostDecision:
    """May this metered rung issue its query, and if not, why not."""

    admitted: bool
    #: ``ok`` | ``no_cap_declared`` | ``no_ledger_bound`` | ``cap_exhausted``
    #: | ``ledger_unavailable``
    cause: str = "ok"
    cap_usd: float | None = None
    spent_usd: float | None = None
    projected_usd: float | None = None
    detail: str = ""


class PackInvocationCostLedger:
    """The production ledger — the governor's own table, read-only.

    Reads ``action_pack_invocations`` over the UTC day with the identical
    predicate ``PackGovernorEnforcer._cost_today`` uses, so the pre-spend check
    and the post-call cap can never disagree about what has been spent. Holds
    no connection: it takes a pool and acquires per call, matching the
    convention every other agency-plane component follows.
    """

    def __init__(self, pg_pool: Any) -> None:
        self._pool = pg_pool

    async def spent_today_usd(self, *, pack_id: str, budget_account: str) -> float:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(cost_usd), 0)::NUMERIC AS c
                FROM action_pack_invocations
                WHERE pack_id = $1 AND budget_account = $2
                  AND occurred_at >= $3::date
                  AND occurred_at < ($3::date + INTERVAL '1 day')
                """,
                pack_id, budget_account, _today_utc(),
            )
        return float(Decimal(row["c"])) if row and row["c"] is not None else 0.0


def pack_day_cost_cap(pack: Any) -> float | None:
    """The pack's declared ``governor.max_cost_usd_per_day``, or ``None``."""
    governor = getattr(pack, "governor", None)
    cap = getattr(governor, "max_cost_usd_per_day", None)
    if cap is None:
        return None
    try:
        return float(cap)
    except (TypeError, ValueError):
        return None


async def check_paid_rung_budget(
    *,
    pack: Any,
    ledger: Any | None,
    budget_account: str,
    cost_usd: float,
    component_id: str,
) -> SearchCostDecision:
    """Decide whether a metered rung may spend ``cost_usd`` right now.

    Never raises: a ledger that errors is reported as ``ledger_unavailable``
    and REFUSES the rung, because an unreadable spend history is not evidence
    of head-room. Callers treat every non-admitted decision the same way — skip
    the rung, record the cause.
    """
    pack_id = str(getattr(getattr(pack, "identity", None), "id", "") or "")
    cap = pack_day_cost_cap(pack)
    if cap is None:
        return SearchCostDecision(
            admitted=False, cause="no_cap_declared",
            detail=(
                f"pack {pack_id!r} declares no governor.max_cost_usd_per_day, so "
                f"the METERED rung {component_id!r} (${cost_usd:.4f}/query) has "
                "no spend ceiling to run under. Declare the cap on the pack "
                "before arming a paid provider — an unenforced spend cap on an "
                "external API is an open tab, not a permissive default."
            ),
        )
    if ledger is None:
        return SearchCostDecision(
            admitted=False, cause="no_ledger_bound", cap_usd=cap,
            detail=(
                f"no search cost ledger is bound on this run, so the day's spend "
                f"against budget_account {budget_account!r} cannot be read and "
                f"the METERED rung {component_id!r} (${cost_usd:.4f}/query) "
                "cannot be priced. REFUSED rather than billed blind."
            ),
        )
    try:
        spent = float(await ledger.spent_today_usd(
            pack_id=pack_id, budget_account=budget_account,
        ))
    except Exception as exc:
        logger.warning(
            "search_cost.ledger_unavailable pack=%s account=%s component=%s err=%s",
            pack_id, budget_account, component_id, exc,
        )
        return SearchCostDecision(
            admitted=False, cause="ledger_unavailable", cap_usd=cap,
            detail=(
                f"the invocation ledger could not be read "
                f"({type(exc).__name__}: {exc}); an unreadable spend history is "
                "not evidence of head-room, so the metered rung is REFUSED."
            ),
        )
    projected = spent + max(0.0, float(cost_usd))
    if projected > cap:
        return SearchCostDecision(
            admitted=False, cause="cap_exhausted", cap_usd=cap,
            spent_usd=spent, projected_usd=projected,
            detail=(
                f"day spend ${spent:.4f} + ${cost_usd:.4f} for one query on "
                f"{component_id!r} would reach ${projected:.4f}, over the pack's "
                f"max_cost_usd_per_day of ${cap:.4f}. NO paid query was issued."
            ),
        )
    return SearchCostDecision(
        admitted=True, cap_usd=cap, spent_usd=spent, projected_usd=projected,
    )


__all__ = [
    "PackInvocationCostLedger",
    "SearchCostDecision",
    "SearchCostLedger",
    "check_paid_rung_budget",
    "pack_day_cost_cap",
]
