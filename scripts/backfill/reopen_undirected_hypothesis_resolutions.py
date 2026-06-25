# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DQ-H2b backfill — re-open UNDIRECTED hypotheses auto-resolved as TRUE.

The forward fix (DQ-H2b) makes the exogenous resolver ABSTAIN on undirected
theses instead of auto-grading them TRUE whenever subsequent facts are quiet.
But the EXISTING rows were resolved by the old logic — a live audit found that
**74% (113/152) of `resolved_by='subsequent_facts'` resolutions are undirected**
(no escalation / de-escalation / status-quo claim) and 111 of those were graded
TRUE, inflating the exogenous true-rate from the genuine directional 0.103 to a
spurious 0.757. Those rows poison the calibration_tracking exogenous Brier until
they age out.

This re-opens (``resolved_outcome = resolved_by = resolved_at = NULL``) exactly
those undirected ``subsequent_facts`` resolutions, using the SAME classifier as
the forward resolver (``_thesis_direction`` + ``_thesis_is_status_quo``) so the
two can never disagree. It NEVER touches:
  * directional resolutions (escalation / de-escalation / status-quo) — those are
    genuinely graded;
  * operator-stamped rows (``resolved_by LIKE 'operator:%'``);
  * self-consistency rows (``resolved_by = 'status_transition'``).

A re-opened hypothesis simply returns to the unresolved pool; the (now-fixed)
resolver will abstain on it next sweep, so it stays out of the exogenous sample.

Idempotent (re-running re-opens nothing new — the rows are already NULL) +
dry-run by default. Operator-gated (mutates live shared-DB rows): pass --apply.

Usage (inside the runtime container, which has the classifier + live DB env):
    python scripts/backfill/reopen_undirected_hypothesis_resolutions.py          # dry run
    python scripts/backfill/reopen_undirected_hypothesis_resolutions.py --apply  # write
"""
from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

from legba.data.analysts.competing_hypotheses import (
    _thesis_direction,
    _thesis_is_status_quo,
)


def _is_undirected(thesis: str) -> bool:
    """A thesis the resolver cannot grade: no escalation / de-escalation AND no
    status-quo claim. Mirrors the forward resolver's abstain condition exactly."""
    return _thesis_direction(thesis) == 0 and not _thesis_is_status_quo(thesis)


async def main(apply: bool) -> int:
    dsn = (
        f"postgresql://{os.environ.get('LEGBA_DATA_PG_USER', 'legba')}:"
        f"{os.environ.get('LEGBA_DATA_PG_PASSWORD', 'legba')}@"
        f"{os.environ.get('LEGBA_DATA_PG_HOST', 'postgres')}:"
        f"{os.environ.get('LEGBA_DATA_PG_PORT', '5432')}/"
        f"{os.environ.get('LEGBA_DATA_PG_DB', 'legba')}"
    )
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT id, thesis, resolved_outcome
              FROM hypotheses
             WHERE resolved_by = 'subsequent_facts'
            """
        )
        undirected_ids = [
            r["id"] for r in rows if _is_undirected(str(r["thesis"] or ""))
        ]
        scanned = len(rows)
        to_reopen = len(undirected_ids)
        reopened_true = sum(
            1 for r in rows
            if _is_undirected(str(r["thesis"] or "")) and int(r["resolved_outcome"] or 0) == 1
        )
        if apply and undirected_ids:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE hypotheses
                       SET resolved_outcome = NULL,
                           resolved_by = NULL,
                           resolved_at = NULL,
                           updated_at = NOW()
                     WHERE id = ANY($1::uuid[])
                       AND resolved_by = 'subsequent_facts'
                    """,
                    undirected_ids,
                )
    finally:
        await conn.close()
    print(
        f"subsequent_facts_scanned={scanned} undirected_to_reopen={to_reopen} "
        f"(of which graded_true={reopened_true})"
    )
    print("APPLIED" if apply else "DRY RUN — pass --apply to write")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(apply="--apply" in sys.argv[1:])))
