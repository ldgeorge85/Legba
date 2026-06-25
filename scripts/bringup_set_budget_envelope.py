# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A3 — seed the global budget envelope (the system-wide daily ceiling).

The per-analyst caps (`method.budget_tokens_per_day` in each descriptor) bound
each analyst individually. The global envelope (migration 0022,
`global_budget_envelope`) is the SYSTEM-WIDE backstop: when the sum of every
analyst's `budget_ledger.tokens_used` for the bucket crosses `tokens_cap`,
`BudgetEnforcer.precall_check` returns `global_exhausted` and the runtime flips
the process-global demote/pause flag for EVERY actor for the rest of the day.

Why this matters under A2 concurrency
-------------------------------------
A2 made the per-(analyst, target) cadence runs concurrent (the country_assessor
fans out to 19 country workers, bounded at _FANOUT_CHUNK=5). Concurrent LLM
calls all land in the same daily bucket; the per-analyst cap protects each
analyst, but only the GLOBAL envelope protects the aggregate daily ceiling when
several analysts burn concurrently. Seeding it is what keeps "demote-on-
exhaustion still protects the daily ceiling under concurrency" (A3 requirement).

Chosen cap — math
-----------------
Active source-first per-analyst caps (LLM-bearing analysts only; deterministic
kinds — cross_source_dedup / finding_supersession / entity_resolution — spend
zero tokens and carry no cap):

    country_assessor   400 000   (19-target fan-out, gpt-oss-120b)
    country_optimizer  200 000   (daily DSPy compile, off-peak 03:00)
    consult_default    100 000   (on-demand A2A consult)
    country_critic      50 000   (paid Anthropic, sampled grading)
    -----------------------------
    sum of caps        750 000   tok/day

The post-A3 cadences are each individually budget-safe (see the descriptor
headers), so the EXPECTED aggregate daily burn is well under the sum of caps.
We set the global envelope to 800 000 tok/day — the sum of caps plus ~7%
headroom for consult bursts. It only fires if multiple analysts simultaneously
approach their individual ceilings (the concurrency overrun A3 guards against),
at which point `on_exceeded='demote_all'` halts paid spend for the rest of the
UTC day. NULL `usd_cap` — the dollar ceiling is governed by the per-provider
price tables + the assessor's self-hosted (zero-priced) primary; tokens are the
binding dimension here.

The envelope is per-bucket (one row per UTC day). This script upserts the row
for `--days` buckets starting today (default 7) so a fresh instance has a cap
in place immediately; the runtime/operator can adjust later via the
`/api/v1/budget/envelope` surface. A-5/G5: the enforcer also auto-rolls the
most recent prior bucket forward when today's row is missing (materialized
with a "auto-rollover from <date>" note), so the cap no longer silently
ceases to exist after the seeded window — seeding several days here is
belt-and-braces plus operator visibility, not the only line of defense.

Override the target DB with LEGBA_DATA_PG_DB (defaults to legba_pivot_test).
"""
from __future__ import annotations

import argparse
import asyncio
import os
from datetime import date, datetime, timedelta, timezone

from legba.data.postgres import PostgresStore

# Sum of the active LLM-bearing per-analyst caps + ~7% headroom. See module
# docstring for the derivation.
DEFAULT_TOKENS_CAP = 800_000
DEFAULT_ON_EXCEEDED = "demote_all"
NOTE = (
    "A3 cadence-vs-budget: system-wide daily ceiling = sum of active "
    "per-analyst caps (750k) + ~7% headroom. demote_all on exhaustion."
)


def _today_utc() -> date:
    return datetime.now(tz=timezone.utc).date()


async def _upsert_envelope(
    store: PostgresStore,
    *,
    bucket: date,
    tokens_cap: int,
    on_exceeded: str,
    note: str,
) -> str:
    async with store.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT tokens_cap, on_exceeded FROM global_budget_envelope WHERE bucket = $1",
            bucket,
        )
        await conn.execute(
            """
            INSERT INTO global_budget_envelope (bucket, tokens_cap, usd_cap, on_exceeded, note)
            VALUES ($1, $2, NULL, $3, $4)
            ON CONFLICT (bucket) DO UPDATE
            SET tokens_cap   = EXCLUDED.tokens_cap,
                on_exceeded  = EXCLUDED.on_exceeded,
                note         = EXCLUDED.note,
                last_updated = NOW()
            """,
            bucket,
            tokens_cap,
            on_exceeded,
            note,
        )
    if existing is None:
        return "inserted"
    if int(existing["tokens_cap"] or 0) == tokens_cap and existing["on_exceeded"] == on_exceeded:
        return "unchanged"
    return "updated"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokens-cap", type=int, default=DEFAULT_TOKENS_CAP)
    parser.add_argument("--on-exceeded", default=DEFAULT_ON_EXCEEDED,
                        choices=["demote_all", "pause_all", "alert_only"])
    parser.add_argument("--days", type=int, default=7,
                        help="Number of UTC-day buckets to seed, starting today.")
    args = parser.parse_args()

    store = PostgresStore.from_env()
    await store.connect()
    try:
        start = _today_utc()
        for i in range(args.days):
            bucket = start + timedelta(days=i)
            action = await _upsert_envelope(
                store,
                bucket=bucket,
                tokens_cap=args.tokens_cap,
                on_exceeded=args.on_exceeded,
                note=NOTE,
            )
            print(
                f"global_budget_envelope[{bucket}] {action}: "
                f"tokens_cap={args.tokens_cap} on_exceeded={args.on_exceeded}"
            )
    finally:
        await store.close()

    print(
        f"DONE — global budget envelope seeded "
        f"(DB={os.environ.get('LEGBA_DATA_PG_DB', 'legba_pivot_test')})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
