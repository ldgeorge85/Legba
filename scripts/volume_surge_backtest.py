#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Replay the ``volume_surge`` detector offline over exported signal rows.

Runs the detector's own pure core (``legba.runtime.volume_surge_watch``:
freshness, the independent-outlet fold, the per-subject hourly baseline, the
four thresholds) on a ten-minute tick over the last ``--days`` of an export,
with the live episode rule (an episode closes once its subject has not fired
for ``close_after_minutes``), and prints one line per episode. Nothing here
touches a database: export first, READ-ONLY, e.g. with psql inside the
database container::

    BEGIN READ ONLY;
    \\copy (SELECT s.id, g, s.source_id, s.fetched_at,
                   s.payload->>'published_at',
                   left(replace(s.payload->>'title', E'\\n', ' '), 160)
              FROM signals s, unnest(s.geo) g
             WHERE s.fetched_at > now() - interval '45 days'
               AND s.origin_class IN ('live', 'web_retrieval', 'seed'))
        TO STDOUT WITH (FORMAT csv)
    ROLLBACK;

Usage::

    PYTHONPATH=src python3 scripts/volume_surge_backtest.py rows.csv --days 30
    # any threshold can be overridden the way the runtime reads it:
    LEGBA_VOLUME_SURGE_MIN_OUTLETS=10 PYTHONPATH=src python3 scripts/volume_surge_backtest.py rows.csv
"""
from __future__ import annotations

import argparse
import bisect
import csv
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from legba.runtime import volume_surge_watch as vs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--days", type=float, default=30.0)
    ap.add_argument("--step-minutes", type=float, default=10.0)
    args = ap.parse_args(argv)
    th = vs.SurgeThresholds.from_env()

    rows = []
    with open(args.csv, newline="") as fh:
        for rid, subject, source_id, fetched, published, title in csv.reader(fh):
            ts = vs.parse_instant(fetched)
            if ts is not None:
                rows.append({"id": rid, "subject": subject, "source_id": source_id,
                             "fetched_at": ts, "published_at": published or None,
                             "title": title})
    rows.sort(key=lambda r: r["fetched_at"])
    times = [r["fetched_at"] for r in rows]
    fresh_by_subject: dict[str, list[datetime]] = defaultdict(list)
    for r in rows:
        if vs.is_fresh(r["fetched_at"], r["published_at"], th.fresh_hours):
            fresh_by_subject[r["subject"]].append(r["fetched_at"])

    end = times[-1].replace(second=0, microsecond=0) + timedelta(minutes=1)
    tick = end - timedelta(days=args.days)
    step = timedelta(minutes=args.step_minutes)
    hours = int(th.baseline_days * 24)
    cache: dict[tuple[str, datetime], tuple[float, float]] = {}
    open_eps: dict[str, dict] = {}
    episodes: list[dict] = []
    while tick <= end:
        start = tick - timedelta(minutes=th.window_minutes)
        lo, hi = bisect.bisect_left(times, start), bisect.bisect_left(times, tick)
        cands = vs.window_candidates(rows[lo:hi], th)
        end_hour = start.replace(minute=0, second=0, microsecond=0)
        base = {}
        for s in cands:
            key = (s, end_hour)
            if key not in cache:
                ts = fresh_by_subject.get(s, [])
                a = bisect.bisect_left(ts, end_hour - timedelta(hours=hours))
                b = bisect.bisect_left(ts, end_hour)
                cache[key] = vs.baseline_stats(vs.hourly_vector(ts[a:b], end_hour=end_hour, hours=hours))
            base[s] = cache[key]
        for s in vs.detect(cands, base, th):
            ep = open_eps.get(s.subject)
            if ep is None:
                ep = open_eps[s.subject] = {"subject": s.subject, "opened": tick, "peak": s}
                episodes.append(ep)
            ep["last"] = tick
            if s.z > ep["peak"].z:
                ep["peak"] = s
        for subject, ep in list(open_eps.items()):
            if tick - ep["last"] >= timedelta(minutes=th.close_after_minutes):
                del open_eps[subject]
        tick += step

    print(f"thresholds: {th}")
    print(f"episodes in {args.days:g} days: {len(episodes)} "
          f"({len(episodes) / args.days * 7:.1f} a week)")
    for ep in episodes:
        p = ep["peak"]
        print(f"{ep['opened']:%m-%d %H:%M}Z -> {ep['last']:%H:%M}Z  {p.subject:3s} "
              f"peak {p.count} signals / {p.outlets} outlets, mean {p.mean:.1f}, "
              f"z {p.z:.1f}, x{p.ratio:.1f} | {p.titles[0] if p.titles else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
