#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Re-enqueue the width claims whose span check never ran (2026-09-05 seam).

THE INCIDENT. Between the width deploy (2026-09-05 ~18:00Z) and the seam repair
stamped ``2026-09-06/1``, the drain called W-3's ``check_span`` with keyword
arguments it does not accept and — underneath that — nothing fetched the
decisive page at all. Every call raised into the wrapper's except-branch, which
degraded the verdict to ``UNCHECKED / span_check_unavailable``. The measured
residue is **211 ledger rows that carry a non-null ``decisive_url`` AND
``decisive_span``**: 211 grader proposals of SUPPORTED or CONTRADICTED that were
never checked against the page they cite. The width ledger's "0 decisive"
reading is at least partly this, and not only the degraded search backend.

WHAT THIS SCRIPT DOES. It finds those rows, re-derives their claims from the
READS they came from — through ``claims_from_read``, the same enumeration the
drain itself uses, so a requeued claim is byte-identical to a freshly refilled
one — and puts them back on the durable queue. The next hourly ticks drain them
normally: one search, one grader call, one robots-gated ``web_fetch``, and the
gates actually run.

WHAT IT DOES NOT DO, AND WHY THAT MATTERS
-----------------------------------------
* **It never updates a row.** ``external_grades`` is append-only and enforces it
  with triggers on both UPDATE and DELETE (migration 0190). A re-grade is a NEW
  ROW under the new ``grader_pipeline_version``, landing beside the old one; the
  ledger's unique key is ``(claim_key, grader_pipeline_version, grader_family)``,
  so the two coexist by construction. The 09-05 rows keep their stamp and stay
  readable as what they are: verdicts from an instrument that never checked a
  span. That is the whole point of stamping them.
* **It never grades.** It only enqueues. No search, no fetch, no LLM call
  happens here — the drain owns all of that, under its own budgets.
* **It does not re-enqueue every UNCHECKED row.** Only the ones with a decisive
  URL AND a decisive span, i.e. the ones where a grader actually proposed a
  decisive verdict that the broken seam threw away. An UNCHECKED row with no
  decisive URL is a search-plane statement, and re-grading it would just spend
  the budget to reproduce it.

``--dry-run`` IS THE DEFAULT. It prints counts and changes nothing. ``--apply``
writes the queue row.

ONE MORE THING THE APPLY LEG HAS TO DO. ``refill`` skips any claim whose key is
in the queue's ``graded_keys`` — correctly, since it WAS graded today. These
claims were graded by the broken instrument, so ``--apply`` removes exactly the
affected keys from that set before refilling, and prints how many it cleared.
Nothing else in the queue is touched: the watermark, the spend, the dead-letter
list and every unaffected key are left exactly as they were.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from legba.data.analysts.deterministic_handlers._external_audit_claims import (  # noqa: E402
    WidthClaim,
    claims_from_read,
    iter_unique,
)
from legba.data.analysts.deterministic_handlers._external_audit_queue import (  # noqa: E402
    DEFAULT_MAX_QUEUE_DEPTH,
    load_queue,
    refill,
    save_queue,
    utc_day,
)
from legba.data.analysts.deterministic_handlers._external_audit_sampling import (  # noqa: E402
    UNCHECKED_SPAN_CHECK_UNAVAILABLE,
    VERDICT_UNCHECKED,
    width_pipeline_version,
)
from legba.data.analysts.deterministic_handlers._external_audit_width import (  # noqa: E402
    resolve_window_config,
)
from legba.data.analysts.deterministic_handlers.standing_auditor import (  # noqa: E402
    ALERT_TRIGGER_CLASS,
)

#: The affected population, stated as SQL. ``decisive_url IS NOT NULL AND
#: decisive_span IS NOT NULL`` is what separates "a grader proposed a decisive
#: verdict and we threw it away" from "the search plane never answered".
_AFFECTED_SQL = """
SELECT claim_key, graded_output_id, grader_pipeline_version, graded_at
FROM external_grades
WHERE verdict = $1
  AND unchecked_reason = $2
  AND decisive_url IS NOT NULL
  AND decisive_span IS NOT NULL
  AND ($3::text IS NULL OR grader_pipeline_version = $3::text)
ORDER BY graded_at
"""

#: The reads those claims came from. Same projection ``_external_audit_width.
#: _REFILL_SQL`` uses, because ``claims_from_read`` reads exactly these fields
#: and a different projection would enumerate a different claim.
_READS_SQL = """
SELECT ao.id, ao.analyst_id, ao.target_id, ao.title, ao.body, ao.data,
       ao.produced_at
FROM analyst_outputs ao
WHERE ao.id = ANY($1::uuid[])
"""


async def _connect_pg() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )


async def collect(
    conn: Any, *, stamp: str | None
) -> tuple[list[Mapping[str, Any]], set[str], list[Any]]:
    """The affected ledger rows, their claim_keys, and their read ids."""
    rows = [
        dict(r)
        for r in await conn.fetch(
            _AFFECTED_SQL,
            VERDICT_UNCHECKED,
            UNCHECKED_SPAN_CHECK_UNAVAILABLE,
            stamp,
        )
    ]
    keys = {str(r["claim_key"]) for r in rows}
    read_ids = sorted({r["graded_output_id"] for r in rows}, key=str)
    return rows, keys, read_ids


async def rederive(
    conn: Any, read_ids: list[Any], keys: set[str]
) -> tuple[list[WidthClaim], set[str]]:
    """Re-enumerate the affected claims off their reads.

    Returns ``(claims, unresolved)``. ``unresolved`` is the affected keys no
    read still produces — a read that was superseded, edited or deleted since it
    was graded. They are REPORTED, never silently dropped: a claim we cannot
    re-derive is one the re-grade will not cover, and that has to show up in the
    count an operator reads.
    """
    if not read_ids:
        return [], set(keys)
    rows = [dict(r) for r in await conn.fetch(_READS_SQL, read_ids)]
    enumerated: list[WidthClaim] = []
    for row in rows:
        enumerated.extend(claims_from_read(row))
    claims = [c for c in iter_unique(enumerated) if c.key in keys]
    return claims, keys - {c.key for c in claims}


async def run(
    conn: Any,
    *,
    apply: bool,
    stamp: str | None,
    max_depth: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = now or datetime.now(timezone.utc)
    rows, keys, read_ids = await collect(conn, stamp=stamp)
    claims, unresolved = await rederive(conn, read_ids, keys)

    state = await load_queue(
        conn, trigger_class=ALERT_TRIGGER_CLASS, day=utc_day(moment)
    )
    cleared = [k for k in (state.get("graded_keys") or []) if k in keys]
    report: dict[str, Any] = {
        "affected_rows": len(rows),
        "affected_claim_keys": len(keys),
        "reads": len(read_ids),
        "rederived_claims": len(claims),
        "unresolved_claim_keys": len(unresolved),
        "graded_keys_to_clear": len(cleared),
        "queue_depth_before": len(state.get("entries") or []),
        "applied": bool(apply),
    }

    if apply and claims:
        state = dict(state)
        state["graded_keys"] = [
            k for k in (state.get("graded_keys") or []) if k not in keys
        ]
        state, counts = refill(state, claims, max_depth=max_depth)
        await save_queue(conn, state, trigger_class=ALERT_TRIGGER_CLASS)
        report["refill"] = counts
    else:
        # The same arithmetic refill would do, computed without writing.
        queued = {str(e.get("claim_key") or "") for e in (state.get("entries") or [])}
        report["refill"] = {
            "offered": len(claims),
            "added": sum(1 for c in claims if c.key not in queued),
            "already_queued": sum(1 for c in claims if c.key in queued),
            "already_graded": 0,
            "dropped_over_depth": 0,
        }
    report["queue_depth_after"] = (
        report["queue_depth_before"] + report["refill"]["added"]
    )
    return report


def _print(report: Mapping[str, Any], *, stamp: str | None) -> None:
    print("width re-grade — UNCHECKED/span_check_unavailable with a decisive URL")
    print(f"  stamp filter          : {stamp or '(any)'}")
    # The stamp a re-graded row will actually carry is a function of THIS
    # host's window configuration (2026-09-07), so it is resolved from the
    # environment here rather than printed as a constant that would be wrong
    # on any deployment that has flipped `window_basis` or `window_grace_hours`.
    # Only the env leg is visible from a script: a descriptor-level option
    # would win at tick time, and the printed line says so.
    _window = resolve_window_config({})
    _target = width_pipeline_version(
        window_basis=_window.basis, grace_before_hours=_window.grace_hours,
    )
    print(
        f"  target stamp for rows : {_target} "
        f"(env basis={_window.basis} grace={_window.grace_hours}h; a "
        "descriptor option would win at tick time)"
    )
    print(f"  affected ledger rows  : {report['affected_rows']}")
    print(f"  distinct claim keys   : {report['affected_claim_keys']}")
    print(f"  distinct reads        : {report['reads']}")
    print(f"  claims re-derived     : {report['rederived_claims']}")
    print(f"  NOT re-derivable      : {report['unresolved_claim_keys']}")
    print(f"  graded_keys to clear  : {report['graded_keys_to_clear']}")
    print(f"  queue depth before    : {report['queue_depth_before']}")
    counts = report["refill"]
    print(
        "  refill                : "
        f"offered={counts['offered']} added={counts['added']} "
        f"already_queued={counts['already_queued']} "
        f"already_graded={counts['already_graded']} "
        f"dropped_over_depth={counts['dropped_over_depth']}"
    )
    print(f"  queue depth after     : {report['queue_depth_after']}")
    print(
        "  MODE                  : "
        + ("APPLIED — the queue row was written" if report["applied"]
           else "DRY RUN — nothing was written")
    )
    if report["unresolved_claim_keys"]:
        print(
            f"  NOTE: {report['unresolved_claim_keys']} claim key(s) no longer "
            "enumerate off their read (superseded/edited/deleted). They will "
            "NOT be re-graded; their 09-05 rows stand as the only record."
        )


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Re-enqueue width claims whose span check never ran "
        "(UNCHECKED/span_check_unavailable with a decisive URL). "
        "Dry-run by default.",
    )
    ap.add_argument("--apply", action="store_true",
                    help="write the queue row (default: dry-run, read-only)")
    ap.add_argument("--stamp", type=str, default="2026-09-05/1",
                    help="only rows with this grader_pipeline_version "
                         "(default: the broken instrument's stamp; pass "
                         "'' for any)")
    ap.add_argument("--max-queue-depth", type=int,
                    default=DEFAULT_MAX_QUEUE_DEPTH)
    args = ap.parse_args()

    conn = await _connect_pg()
    try:
        report = await run(
            conn,
            apply=args.apply,
            stamp=args.stamp or None,
            max_depth=args.max_queue_depth,
        )
    finally:
        await conn.close()
    _print(report, stamp=args.stamp or None)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
