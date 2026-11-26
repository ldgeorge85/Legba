#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The FLOOR-ONLY critiques an outage left behind: list them,
and re-judge them.

When every judge route is down, the faithfulness verify pass falls to the
deterministic floor: it checks citation structure, not whether the cited text
supports the claim, and the score is capped at the 0.85 provisional ceiling.
OpenRouter's 402 outage (2026-10-02 23:08Z → 10-05 02:03Z) wrote 641 such
critiques on 10-03 and 628 on 10-04. NOTHING RE-JUDGES THEM AUTOMATICALLY — not
the recovery of the route, not the next run of the unit. A recovered judge only
grades what is written after it recovers.

This script is the operator's inventory of that debt, and its repayment:

  * which critiques, per UTC day, carry ``judge_status='deterministic'`` because
    the judge was ASKED and did not answer (``judge_unavailable_reason`` in
    ``judge_error`` / ``judge_empty`` — an unsampled or flag-off floor never
    asked anyone and is not listed);
  * how many of their findings are STILL floor-only (their newest faithfulness
    critique is one of these — a later run did not re-judge them);
  * with ``--list N``, the newest N analyzed output ids, to re-judge by hand;
  * the next ``--limit`` (default 50) critiques ``--apply`` would re-judge,
    oldest first — still floor-only, not yet superseded.

READ-ONLY, and the default.

``--apply --limit N`` (SEAMS #69 resolved) re-judges: for each of the
oldest N still-floor-only critiques, the SAME verify pass the critic runs, over
the finding's STORED body and citations (and the retained input slice of its own
run — nothing is re-fetched), against the analyzed analyst's CURRENT judge chain.
An adjudicated verdict is written as a NEW critique (``verification.rejudge_of``
= the old id, ``verification.judge_trigger = 'rejudge_floor'``) and the old floor
is closed (``superseded_by`` / ``superseded_at``), in one transaction. A judge
that does not answer writes NOTHING — never a second floor — and is counted;
five failures in a row stop the run. It spends on a hosted judge and moves
published scores: an explicit ``--limit`` (at most 500) is required, and
``LEGBA_VERIFY_LLM_JUDGE`` must be on. The finding itself is never written.
The machinery is ``legba.runtime.rejudge_floor``.

``--missing`` is the SECOND selection mode, exclusive with the floor
mode above: cited findings whose verify leg NEVER RAN — a rolling deploy
recreated the runtime between the finding's write and its critique's — so they
stand published with no faithfulness critique at all. Selected: ``kind='finding'``
rows with citations, of an analyst the critic covers (one with faithfulness
critiques in the window), not a deterministic rollup, not superseded, older than
``--min-age-minutes`` (default 30), with no faithfulness critique; oldest first.
The dry run lists them per UTC day; ``--apply --limit N`` runs the same verify
pass and writes the critique the critic would have written
(``verification.judge_trigger = 'verify_backfill'``,
``verification.backfill_of_run = <the finding's run_id>``; nothing superseded).
A judge that does not answer writes nothing. The ``verify_backfill`` analyst
runs the same selection and write every 30 minutes, so this is the hand tool.

    PYTHONPATH=src python3 scripts/rejudge_floor_critiques.py --missing
    PYTHONPATH=src python3 scripts/rejudge_floor_critiques.py --missing --apply --limit 50

    # in the registry/runtime container, or anywhere LEGBA_DATA_PG_* reaches
    PYTHONPATH=src python3 scripts/rejudge_floor_critiques.py \\
        --since 2026-10-02T23:00Z --until 2026-10-05T03:00Z

    # the apply also needs the registry (LEGBA_REGISTRY_API_URL/_TOKEN) and the
    # vault (LEGBA_DATA_MASTER_KEY) — the runtime container has all three
    PYTHONPATH=src python3 scripts/rejudge_floor_critiques.py \\
        --since 2026-10-17T00:00Z --until 2026-10-23T03:09Z --apply --limit 50
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

#: The faithfulness critique family — the load-bearing title prefix every verify
#: lateral pins.
TITLE_PREFIX = "Faithfulness verify%"
#: The two floor reasons that mean "the judge was asked and did not answer".
UNAVAILABLE_REASONS = ("judge_error", "judge_empty")
SEAM = 69

#: Per UTC day: floor-only critiques in the window, and how many of their
#: findings' NEWEST faithfulness critique is still floor-only.
COUNT_BY_DAY_SQL = """
WITH floored AS (
    SELECT o.id, o.created_at,
           (o.data->>'analyzed_output_id') AS analyzed_output_id
      FROM public.analyst_outputs o
     WHERE o.kind = 'critique'
       AND o.title LIKE $3::text
       AND o.created_at >= $1::timestamptz
       AND o.created_at <  $2::timestamptz
       AND o.data->'data'->'verification'->>'judge_status' = 'deterministic'
       AND o.data->'data'->'verification'->>'judge_unavailable_reason' = ANY($4::text[])
       AND ($5::text IS NULL OR o.data->>'analyzed_analyst_id' = $5)
),
newest AS (
    SELECT DISTINCT ON (o.data->>'analyzed_output_id')
           o.data->>'analyzed_output_id' AS analyzed_output_id, o.id
      FROM public.analyst_outputs o
     WHERE o.kind = 'critique'
       AND o.title LIKE $3::text
       AND o.data->>'analyzed_output_id' IN (SELECT analyzed_output_id FROM floored)
     ORDER BY o.data->>'analyzed_output_id', o.created_at DESC, o.id DESC
)
SELECT date_trunc('day', f.created_at AT TIME ZONE 'UTC')::date AS day,
       count(*)::int                                            AS floor_critiques,
       count(*) FILTER (WHERE n.id = f.id)::int                 AS still_floor_only
  FROM floored f
  LEFT JOIN newest n ON n.analyzed_output_id = f.analyzed_output_id
 GROUP BY 1
 ORDER BY 1
"""

LIST_SQL = """
SELECT o.created_at, o.data->>'analyzed_output_id' AS analyzed_output_id,
       o.data->>'analyzed_analyst_id' AS analyzed_analyst_id
  FROM public.analyst_outputs o
 WHERE o.kind = 'critique'
   AND o.title LIKE $3::text
   AND o.created_at >= $1::timestamptz
   AND o.created_at <  $2::timestamptz
   AND o.data->'data'->'verification'->>'judge_status' = 'deterministic'
   AND o.data->'data'->'verification'->>'judge_unavailable_reason' = ANY($4::text[])
   AND ($5::text IS NULL OR o.data->>'analyzed_analyst_id' = $5)
 ORDER BY o.created_at DESC
 LIMIT $6
"""


#: The dry run previews this many apply candidates unless ``--limit`` says.
DEFAULT_LIMIT = 50


def _parse_ts(value: str) -> datetime:
    v = value.strip()
    if v.endswith("Z") or v.endswith("z"):
        v = v[:-1] + "+00:00"
    ts = datetime.fromisoformat(v)
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure: the per-day rows → the printed/JSON summary (totals beside the days)."""
    days = [
        {
            "day": (r["day"].isoformat() if isinstance(r["day"], date) else str(r["day"])),
            "floor_critiques": int(r["floor_critiques"] or 0),
            "still_floor_only": int(r["still_floor_only"] or 0),
        }
        for r in rows
    ]
    return {
        "days": days,
        "floor_critiques": sum(d["floor_critiques"] for d in days),
        "still_floor_only": sum(d["still_floor_only"] for d in days),
    }


async def run(
    conn: Any,
    *,
    since: datetime,
    until: datetime,
    analyst_id: str | None = None,
    list_n: int = 0,
    preview_n: int = 0,
) -> dict[str, Any]:
    """The dry run: counts by day (and optionally the newest ``list_n`` ids, and
    the next ``preview_n`` critiques ``--apply`` would re-judge)."""
    rows = await conn.fetch(
        COUNT_BY_DAY_SQL, since, until, TITLE_PREFIX, list(UNAVAILABLE_REASONS),
        analyst_id,
    )
    out = summarize([dict(r) for r in rows])
    out.update(since=since.isoformat(), until=until.isoformat(), analyst_id=analyst_id)
    if list_n > 0:
        listed = await conn.fetch(
            LIST_SQL, since, until, TITLE_PREFIX, list(UNAVAILABLE_REASONS),
            analyst_id, int(list_n),
        )
        out["listed"] = [
            {
                "created_at": r["created_at"].isoformat(),
                "analyzed_output_id": r["analyzed_output_id"],
                "analyzed_analyst_id": r["analyzed_analyst_id"],
            }
            for r in listed
        ]
    if preview_n > 0:
        from legba.runtime.rejudge_floor import select_floor_critiques

        candidates = await select_floor_critiques(
            conn, since=since, until=until, limit=preview_n, analyst_id=analyst_id,
        )
        out["would_rejudge"] = [
            {
                "created_at": (
                    c.created_at.isoformat()
                    if hasattr(c.created_at, "isoformat") else str(c.created_at)
                ),
                "critique_id": str(c.critique_id),
                "finding_id": str(c.finding_id) if c.finding_id else None,
                "analyzed_analyst_id": c.analyzed_analyst_id,
                "old_score": c.old_overall_score,
            }
            for c in candidates
        ]
    return out


async def run_missing(
    conn: Any,
    *,
    since: datetime,
    until: datetime,
    min_age_minutes: int,
    analyst_id: str | None = None,
    preview_n: int = 0,
) -> dict[str, Any]:
    """The ``--missing`` dry run: critique-less cited findings per UTC day, and
    the next ``preview_n`` ``--apply`` would verify."""
    from legba.runtime import rejudge_floor as RF

    days = await RF.count_missing_by_day(
        conn, since=since, until=until, min_age_minutes=min_age_minutes,
        analyst_id=analyst_id,
    )
    out: dict[str, Any] = {
        "mode": "missing",
        "since": since.isoformat(),
        "until": until.isoformat(),
        "analyst_id": analyst_id,
        "min_age_minutes": int(min_age_minutes),
        "days": [
            {
                "day": d["day"].isoformat() if isinstance(d["day"], date) else str(d["day"]),
                "missing": int(d["missing"]),
            }
            for d in days
        ],
    }
    out["missing"] = sum(d["missing"] for d in out["days"])
    if preview_n > 0:
        candidates = await RF.select_missing_findings(
            conn, since=since, until=until, limit=preview_n,
            min_age_minutes=min_age_minutes, analyst_id=analyst_id,
        )
        out["would_backfill"] = [
            {
                "produced_at": (
                    c.produced_at.isoformat()
                    if hasattr(c.produced_at, "isoformat") else str(c.produced_at)
                ),
                "finding_id": str(c.finding_id),
                "run_id": str(c.finding_run_id) if c.finding_run_id else None,
                "analyst_id": c.analyst_id,
            }
            for c in candidates
        ]
    return out


def render_missing(summary: dict[str, Any]) -> str:
    lines = [
        f"cited findings with NO faithfulness critique (older than "
        f"{summary['min_age_minutes']} min) {summary['since']} → {summary['until']}"
        + (f"  analyst={summary['analyst_id']}" if summary.get("analyst_id") else ""),
        f"{'day (UTC)':<12} {'missing':>10}",
    ]
    for d in summary["days"]:
        lines.append(f"{d['day']:<12} {d['missing']:>10}")
    lines.append(f"{'total':<12} {summary['missing']:>10}")
    if "would_backfill" in summary:
        lines.append(
            "--missing --apply would verify (oldest first): "
            f"{len(summary['would_backfill'])}"
        )
        for item in summary["would_backfill"]:
            lines.append(
                f"  {item['produced_at']}  finding={item['finding_id']}  "
                f"run={item['run_id']}  {item['analyst_id']}"
            )
    lines.append(
        "dry run — nothing verified, nothing written. Verifying spends on the "
        "judge: --missing --apply --limit N (the verify_backfill sweep does this "
        "every 30 min)."
    )
    return "\n".join(lines)


def render(summary: dict[str, Any]) -> str:
    lines = [
        f"floor-only critiques (judge asked, did not answer) "
        f"{summary['since']} → {summary['until']}"
        + (f"  analyst={summary['analyst_id']}" if summary.get("analyst_id") else ""),
        f"{'day (UTC)':<12} {'critiques':>10} {'still floor-only':>17}",
    ]
    for d in summary["days"]:
        lines.append(f"{d['day']:<12} {d['floor_critiques']:>10} {d['still_floor_only']:>17}")
    lines.append(
        f"{'total':<12} {summary['floor_critiques']:>10} {summary['still_floor_only']:>17}"
    )
    for item in summary.get("listed", []):
        lines.append(
            f"  {item['created_at']}  {item['analyzed_output_id']}  "
            f"{item['analyzed_analyst_id']}"
        )
    if "would_rejudge" in summary:
        lines.append(
            "--apply would re-judge (oldest first, still floor-only, unsuperseded): "
            f"{len(summary['would_rejudge'])}"
        )
        for item in summary["would_rejudge"]:
            lines.append(
                f"  {item['created_at']}  critique={item['critique_id']}  "
                f"finding={item['finding_id']}  {item['analyzed_analyst_id']}  "
                f"old={item['old_score']}"
            )
    lines.append(
        "dry run — nothing re-judged, nothing written. Re-judging spends on the "
        f"judge: --apply --limit N (SEAMS #{SEAM}, built)."
    )
    return "\n".join(lines)


def _max_apply_limit() -> int:
    from legba.runtime.rejudge_floor import MAX_APPLY_LIMIT

    return MAX_APPLY_LIMIT


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="List floor-only faithfulness critiques an outage left behind "
        "(dry run, the default) or re-judge them (--apply --limit N; SEAMS #69).",
    )
    ap.add_argument("--since", help="window start, ISO-8601 (default: 7 days ago)")
    ap.add_argument("--until", help="window end, ISO-8601 (default: now)")
    ap.add_argument("--analyst", help="restrict to one analyzed analyst id")
    ap.add_argument("--list", type=int, default=0, metavar="N",
                    help="also print the newest N analyzed output ids")
    ap.add_argument("--limit", type=int, default=None, metavar="N",
                    help=f"critiques to re-judge, oldest first (dry run: a preview, "
                    f"default {DEFAULT_LIMIT}; --apply: REQUIRED, at most "
                    f"{_max_apply_limit()})")
    ap.add_argument("--json", action="store_true", help="print JSON")
    ap.add_argument("--dry-run", action="store_true",
                    help="explicit dry run (the default)")
    ap.add_argument("--apply", action="store_true",
                    help="re-judge the selected critiques and supersede their floors "
                    "(spends on the judge; needs an explicit --limit)")
    ap.add_argument("--skip-weak", action="store_true",
                    help="with --apply: never supersede a floor with a WEAK chain "
                    "rung's verdict (the in-house 8B); counted weak_skipped")
    ap.add_argument("--max-consecutive-failures", type=int, default=None, metavar="K",
                    help="with --apply: stop after K judge failures in a row "
                    "(default 5; 0 = never stop)")
    ap.add_argument("--missing", action="store_true",
                    help="Select cited findings with NO faithfulness critique "
                    "(their verify leg never ran) instead of floor critiques; "
                    "--apply writes the critique the critic would have written")
    ap.add_argument("--min-age-minutes", type=int, default=None, metavar="M",
                    help="with --missing: only findings older than M minutes "
                    f"(default {_default_min_age()}; a younger one may still be verifying)")
    return ap


def _default_min_age() -> int:
    from legba.runtime.rejudge_floor import DEFAULT_MIN_AGE_MINUTES

    return DEFAULT_MIN_AGE_MINUTES


def check_args(args: argparse.Namespace) -> str | None:
    """Every refusal, before any read or write. ``None`` = go."""
    if args.limit is not None and args.limit < 1:
        return "--limit must be >= 1"
    if args.apply and args.dry_run:
        return "--apply and --dry-run are exclusive"
    if args.missing and args.list:
        return "--list is the floor mode's listing; --missing previews with --limit"
    if args.min_age_minutes is not None and not args.missing:
        return "--min-age-minutes needs --missing"
    if args.min_age_minutes is not None and args.min_age_minutes < 0:
        return "--min-age-minutes must be >= 0"
    if not args.apply:
        if args.skip_weak or args.max_consecutive_failures is not None:
            return "--skip-weak / --max-consecutive-failures need --apply"
        return None
    if args.limit is None:
        return ("--apply requires an explicit --limit N: re-judging spends on the "
                "judge and supersedes published verdicts")
    if args.limit > _max_apply_limit():
        return f"--limit is at most {_max_apply_limit()} with --apply"
    if args.max_consecutive_failures is not None and args.max_consecutive_failures < 0:
        return "--max-consecutive-failures must be >= 0"
    from legba.data.provenance.verify import _llm_judge_enabled

    if not _llm_judge_enabled():
        return ("--apply needs LEGBA_VERIFY_LLM_JUDGE on: with the judge off every "
                "re-run is a floor again, and a floor is never written twice")
    return None


def resolve_window(args: argparse.Namespace, *, now: datetime) -> tuple[datetime, datetime]:
    until = _parse_ts(args.until) if args.until else now
    since = _parse_ts(args.since) if args.since else until - timedelta(days=7)
    if since >= until:
        raise ValueError("--since must be before --until")
    return since, until


async def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    refusal = check_args(args)
    if refusal is not None:
        ap.error(refusal)
    try:
        since, until = resolve_window(args, now=datetime.now(timezone.utc))
    except ValueError as exc:
        ap.error(str(exc))
    if args.apply:
        return await _apply(args, since=since, until=until)
    import asyncpg

    from legba.data.config import PostgresConfig

    conn = await asyncpg.connect(PostgresConfig.from_env().dsn)
    try:
        await conn.execute("SET statement_timeout = '120s'")
        if args.missing:
            summary = await run_missing(
                conn, since=since, until=until, min_age_minutes=_min_age(args),
                analyst_id=args.analyst, preview_n=args.limit or DEFAULT_LIMIT,
            )
        else:
            summary = await run(
                conn, since=since, until=until, analyst_id=args.analyst,
                list_n=args.list, preview_n=args.limit or DEFAULT_LIMIT,
            )
    finally:
        await conn.close()
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(render_missing(summary) if args.missing else render(summary))
    return 0


def _min_age(args: argparse.Namespace) -> int:
    return _default_min_age() if args.min_age_minutes is None else args.min_age_minutes


async def _apply(args: argparse.Namespace, *, since: datetime, until: datetime) -> int:
    """``--apply``: select, re-judge one row at a time, print each outcome."""
    from legba.data.config import PostgresConfig
    from legba.data.postgres import PostgresStore
    from legba.data.registry.credentials import CredentialVault
    from legba.runtime import rejudge_floor as RF
    from legba.runtime.registry_client import RegistryHTTPClient

    store = PostgresStore(PostgresConfig.from_env())
    await store.connect()
    registry = RegistryHTTPClient()
    vault = CredentialVault(store)
    resolve, close_handlers = RF.live_judge_resolver(
        registry_client=registry, secrets_resolve=vault.resolve,
    )

    def _print_row(outcome: Any) -> None:
        if not args.json:
            print(RF.render_row(outcome), flush=True)

    try:
        async with store.pool.acquire() as conn:
            await conn.execute("SET statement_timeout = '120s'")
            if args.missing:
                rows = await RF.select_missing_findings(
                    conn, since=since, until=until, limit=args.limit,
                    min_age_minutes=_min_age(args), analyst_id=args.analyst,
                )
                what = "critique-less finding(s)"
            else:
                rows = await RF.select_floor_critiques(
                    conn, since=since, until=until, limit=args.limit,
                    analyst_id=args.analyst,
                )
                what = "floor critique(s)"
            if not args.json:
                verb = "verifying" if args.missing else "re-judging"
                print(
                    f"{verb} {len(rows)} {what} {since.isoformat()} → "
                    f"{until.isoformat()} (limit {args.limit}, oldest first)",
                    flush=True,
                )
            summary = await RF.apply(
                conn, rows, resolve,
                accept_weak=not args.skip_weak,
                max_consecutive_failures=(
                    RF.DEFAULT_MAX_CONSECUTIVE_FAILURES
                    if args.max_consecutive_failures is None
                    else args.max_consecutive_failures
                ),
                on_row=_print_row,
            )
    finally:
        await close_handlers()
        if hasattr(registry, "aclose"):
            try:
                await registry.aclose()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        await store.close()
    print(json.dumps(summary.as_dict(), indent=2) if args.json
          else RF.render_summary(summary))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
