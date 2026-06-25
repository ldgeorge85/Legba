# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Budget enforcement (per legba_runtime_spec.md §5).

Pre-call envelope check + post-call ledger update. Reads
``budget_ledger`` (tokens + cost_estimate_usd) and a per-analyst envelope
from the analyst descriptor's ``method.budget_tokens_per_day``.

For the Phase 5a spike the envelope source was the descriptor itself.
The global budget envelope landed in migration 0022 and is now read
side-by-side with the per-analyst cap — when either dimension hits, the
runtime auto-demotes (per Phase 5 hardening item 4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal

import asyncpg

from ..data.provenance.budget import (
    BudgetLedgerRow,
    compute_cost_usd,
    record_budget,
)

logger = logging.getLogger(__name__)


# Outcomes:
#   * ok            — under both per-analyst and global caps.
#   * throttle      — projected (used + estimate) would cross the per-
#                     analyst cap (forward-looking warning).
#   * exhausted     — per-analyst cap fully consumed.
#   * global_exhausted — global envelope fully consumed (system-wide).
#                     The actor's per-analyst cap may still have room
#                     but the global cap demands a system-wide demote.
BudgetOutcome = Literal["ok", "throttle", "exhausted", "global_exhausted"]


@dataclass(frozen=True)
class BudgetDecision:
    """Result of a pre-call envelope check.

    ``cause`` distinguishes per-analyst vs global exhaustion so the actor
    can stamp the right value into ``budget_demotion_events.cause``.
    """

    outcome: BudgetOutcome
    tokens_used_today: int
    tokens_budget_per_day: int | None
    detail: str = ""
    # 'per_analyst' or 'global' — matches budget_demotion_events.cause.
    cause: str | None = None
    # When non-None, the global envelope's cap that was hit (informational).
    global_tokens_cap: int | None = None
    global_tokens_used: int | None = None


def _today_utc() -> date:
    return datetime.now(tz=timezone.utc).date()


class BudgetEnforcer:
    """Per-analyst envelope check + post-call ledger writer.

    Wraps the helpers in :mod:`legba.data.provenance.budget`. Construct one
    per analyst per run; the per-call check + post-call record share state.

    The check is conservative: it sums tokens already used today against
    the analyst's per-day budget. If the budget is unset (``None``) all
    calls pass. If estimated tokens for the next call would push past
    the budget, the call is throttled.
    """

    def __init__(
        self,
        *,
        analyst_id: str,
        analyst_version: str,
        budget_tokens_per_day: int | None,
        provider: str,
        model: str,
        estimated_tokens_per_run: int = 0,
    ) -> None:
        self.analyst_id = analyst_id
        self.analyst_version = analyst_version
        self.budget_tokens_per_day = budget_tokens_per_day
        self.provider = provider
        self.model = model
        # A-5/G5: the forward-looking allowance the actor passes into
        # precall_check. Resolved at deps-build time from the descriptor
        # (budget_tokens_per_run / method.llm.max_tokens) or the LLM stack
        # component's max_tokens. 0 = unknown → throttle stays unreachable
        # for that analyst (the pre-fix behavior, but now explicit).
        self.estimated_tokens_per_run = int(estimated_tokens_per_run or 0)

    async def precall_check(
        self,
        conn: asyncpg.Connection,
        *,
        estimated_tokens: int = 0,
        bucket: date | None = None,
    ) -> BudgetDecision:
        """Read the current bucket and decide ok / throttle / exhausted /
        global_exhausted.

        Two caps are consulted side-by-side:

          1. Per-analyst cap (``budget_tokens_per_day``) — descriptor-set.
          2. Global envelope (``global_budget_envelope.tokens_cap``) —
             operator-set system-wide cap. Hits trigger an auto-demote
             across ALL analysts.

        Resolution order:

          * Global exhausted → return ``global_exhausted`` first (the
            system-wide signal beats per-analyst signals).
          * Per-analyst exhausted → return ``exhausted``.
          * Either projected-over → return ``throttle``.
          * Otherwise → ``ok``.

        ``estimated_tokens`` is a forward-looking allowance the caller
        believes the call may consume.
        """
        bucket = bucket or _today_utc()

        # ---- Global envelope check (runs first) ---------------------------
        global_row = await conn.fetchrow(
            """
            SELECT tokens_cap, usd_cap, on_exceeded
            FROM global_budget_envelope
            WHERE bucket = $1
            """,
            bucket,
        )

        # A-5/G5 rollover: the envelope is a per-UTC-day row seeded by a
        # bringup script — on any later day the row is simply absent, and
        # pre-fix the system-wide cap silently ceased to exist. Inherit the
        # most recent prior bucket's caps and MATERIALIZE today's row (ON
        # CONFLICT DO NOTHING — concurrency-safe, auditable in the table
        # itself). A prior row with a NULL tokens_cap is an explicit
        # operator "no cap" and is honored — no inheritance past it.
        if global_row is None:
            prev = await conn.fetchrow(
                """
                SELECT bucket, tokens_cap, usd_cap, on_exceeded
                FROM global_budget_envelope
                WHERE bucket < $1
                ORDER BY bucket DESC
                LIMIT 1
                """,
                bucket,
            )
            if prev is not None and prev["tokens_cap"] is not None:
                await conn.execute(
                    """
                    INSERT INTO global_budget_envelope (
                        bucket, tokens_cap, usd_cap, on_exceeded, note
                    )
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (bucket) DO NOTHING
                    """,
                    bucket,
                    prev["tokens_cap"],
                    prev["usd_cap"],
                    prev["on_exceeded"],
                    f"auto-rollover from {prev['bucket'].isoformat()}",
                )
                logger.info(
                    "budget.global_envelope.rollover bucket=%s "
                    "inherited_from=%s tokens_cap=%s",
                    bucket.isoformat(),
                    prev["bucket"].isoformat(),
                    prev["tokens_cap"],
                )
                global_row = prev
        global_tokens_cap: int | None = None
        if global_row is not None and global_row["tokens_cap"] is not None:
            global_tokens_cap = int(global_row["tokens_cap"])

        global_used = 0
        if global_tokens_cap is not None:
            grow = await conn.fetchrow(
                """
                SELECT COALESCE(SUM(tokens_used), 0)::BIGINT AS used
                FROM budget_ledger
                WHERE bucket = $1
                """,
                bucket,
            )
            global_used = int(grow["used"]) if grow and grow["used"] is not None else 0
            if global_used >= global_tokens_cap:
                return BudgetDecision(
                    outcome="global_exhausted",
                    tokens_used_today=0,  # populated below when also tracked per-analyst
                    tokens_budget_per_day=self.budget_tokens_per_day,
                    detail=(
                        f"global envelope used {global_used} "
                        f">= cap {global_tokens_cap}"
                    ),
                    cause="global",
                    global_tokens_cap=global_tokens_cap,
                    global_tokens_used=global_used,
                )

        # ---- Per-analyst cap check ---------------------------------------
        # Both None and 0 mean "no per-analyst budget configured" — K-3
        # (predictor e2e, 2026-05-29) hit the case where a stat-only
        # analyst declared `budget_tokens_per_day: 0` (intending "no
        # LLM, no budget") and tripped the `used >= budget` gate
        # because 0 >= 0 is True. The descriptor schema doesn't
        # distinguish "absent" from "explicit zero"; treat them
        # equivalently here. Operators who actually want a strict
        # zero-tokens-per-day cap can set 1 + a budget_tokens_per_run
        # of 0 to get the same effect.
        if self.budget_tokens_per_day is None or self.budget_tokens_per_day == 0:
            return BudgetDecision(
                outcome="ok",
                tokens_used_today=0,
                tokens_budget_per_day=self.budget_tokens_per_day,
                detail="no per-analyst budget configured",
                global_tokens_cap=global_tokens_cap,
                global_tokens_used=global_used if global_tokens_cap is not None else None,
            )

        row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(tokens_used), 0)::BIGINT AS used
            FROM budget_ledger
            WHERE analyst_id = $1
              AND analyst_version = $2
              AND bucket = $3
            """,
            self.analyst_id,
            self.analyst_version,
            bucket,
        )
        used = int(row["used"]) if row and row["used"] is not None else 0

        if used >= self.budget_tokens_per_day:
            return BudgetDecision(
                outcome="exhausted",
                tokens_used_today=used,
                tokens_budget_per_day=self.budget_tokens_per_day,
                detail=f"used {used} >= budget {self.budget_tokens_per_day}",
                cause="per_analyst",
                global_tokens_cap=global_tokens_cap,
                global_tokens_used=global_used if global_tokens_cap is not None else None,
            )
        # Throttle: per-analyst projected over OR global projected over.
        if (
            global_tokens_cap is not None
            and global_used + estimated_tokens > global_tokens_cap
        ):
            return BudgetDecision(
                outcome="throttle",
                tokens_used_today=used,
                tokens_budget_per_day=self.budget_tokens_per_day,
                detail=(
                    f"projected global used+estimate ({global_used}+{estimated_tokens}) "
                    f"exceeds cap {global_tokens_cap}"
                ),
                cause="global",
                global_tokens_cap=global_tokens_cap,
                global_tokens_used=global_used,
            )
        if used + estimated_tokens > self.budget_tokens_per_day:
            return BudgetDecision(
                outcome="throttle",
                tokens_used_today=used,
                tokens_budget_per_day=self.budget_tokens_per_day,
                detail=(
                    f"projected used+estimate ({used}+{estimated_tokens}) "
                    f"exceeds budget {self.budget_tokens_per_day}"
                ),
                cause="per_analyst",
                global_tokens_cap=global_tokens_cap,
                global_tokens_used=global_used if global_tokens_cap is not None else None,
            )
        return BudgetDecision(
            outcome="ok",
            tokens_used_today=used,
            tokens_budget_per_day=self.budget_tokens_per_day,
            global_tokens_cap=global_tokens_cap,
            global_tokens_used=global_used if global_tokens_cap is not None else None,
        )

    async def record_demotion(
        self,
        conn: asyncpg.Connection,
        *,
        cause: str,
        primary_llm: str,
        fallback_llm: str,
        tokens_used_at_demote: int | None,
        tokens_cap_at_demote: int | None,
        bucket: date | None = None,
    ) -> None:
        """Insert a row into ``budget_demotion_events``.

        Idempotent at the call site — the actor decides whether to emit;
        this just writes the row. ``cause`` is 'per_analyst' or 'global'
        and matches the table's CHECK constraint.
        """
        bucket = bucket or _today_utc()
        await conn.execute(
            """
            INSERT INTO budget_demotion_events (
                analyst_id, analyst_version, bucket, cause,
                tokens_used_at_demote, tokens_cap_at_demote,
                primary_llm, fallback_llm
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            self.analyst_id,
            self.analyst_version,
            bucket,
            cause,
            tokens_used_at_demote,
            tokens_cap_at_demote,
            primary_llm,
            fallback_llm,
        )

    async def record(
        self,
        conn: asyncpg.Connection,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        reasoning_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        bucket: date | None = None,
        runs_increment: int = 1,
    ) -> BudgetLedgerRow:
        """Upsert the budget_ledger row. Wraps provenance.budget.record_budget."""
        return await record_budget(
            conn,
            analyst_id=self.analyst_id,
            analyst_version=self.analyst_version,
            provider=self.provider,
            model=self.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            bucket=bucket,
            runs_increment=runs_increment,
        )

    @staticmethod
    def compute_cost(
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> Decimal:
        """Convenience pass-through to provenance.budget.compute_cost_usd."""
        return compute_cost_usd(
            provider,
            model,
            prompt_tokens,
            completion_tokens,
        )


__all__ = ["BudgetDecision", "BudgetEnforcer", "BudgetOutcome"]
