# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A deterministic synthetic ``event_reconciler`` scan, shaped like the live one.

The live scan on 2026-11-13 (read-only profile, no rows copied): 5,000 of
5,225 events, two producers, one signature per event, events from June to
November with ~500-600 a week since late September, actor links per event at
p10/p25/p50/p75/p90/p99 = 14/54/186/288/363/497 drawn from ~125 k entities
with a heavy head (the top entities sit on 2,000-3,700 events each), and an
evidence span (time_start to latest link) of ~60 h median, ~90 h p90 and
~1,800 h p99.

``actor_scale`` shrinks the per-event actor counts so the quadratic legacy
walk stays affordable inside a unit test; ``hazards`` mixes in the shapes the
walk must treat identically either way (exact end ties, duplicate ids, string
and naive timestamps, string and malformed actor ids, missing times).
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from uuid import UUID

_P = [(0.10, 14), (0.25, 54), (0.50, 186), (0.75, 288), (0.90, 363),
      (0.99, 497), (1.00, 805)]


def _actor_count(rng: random.Random) -> int:
    u = rng.random()
    lo_q, lo_v = 0.0, 0
    for q, v in _P:
        if u <= q:
            frac = (u - lo_q) / (q - lo_q) if q > lo_q else 0.0
            return int(lo_v + frac * (v - lo_v))
        lo_q, lo_v = q, v
    return _P[-1][1]


def _uuid_from(rng: random.Random) -> UUID:
    return UUID(int=rng.getrandbits(128), version=4)


def make_rows(
    n: int,
    *,
    seed: int = 7,
    actor_scale: float = 1.0,
    entity_pool: int = 20_000,
    hazards: bool = True,
    weeks: int = 22,
) -> list[dict]:
    """``n`` event rows in the scan's ``ORDER BY time_start, id`` order."""
    rng = random.Random(seed)
    pool = [_uuid_from(rng) for _ in range(entity_pool)]
    cum: list[float] = []
    acc = 0.0
    for rank in range(1, entity_pool + 1):
        acc += 1.0 / (rank ** 1.05)
        cum.append(acc)
    t_end = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
    t_begin = t_end - timedelta(weeks=weeks)
    span = (t_end - t_begin).total_seconds()
    rows: list[dict] = []
    for idx in range(n):
        # Density ramps toward the recent weeks, like the live table.
        start = t_begin + timedelta(seconds=span * (rng.random() ** 0.45))
        if hazards and rng.random() < 0.15:
            start = start.replace(minute=0, second=0, microsecond=0)
        dur_h = rng.lognormvariate(4.1, 0.6)
        if rng.random() < 0.02:
            dur_h = rng.uniform(300, 2000)
        evidence_end = start + timedelta(hours=dur_h)
        if hazards and rng.random() < 0.2:
            evidence_end = evidence_end.replace(minute=0, second=0, microsecond=0)
        k = max(0, int(_actor_count(rng) * actor_scale))
        actors: list = rng.choices(pool, cum_weights=cum, k=k)
        row: dict = {
            "id": _uuid_from(rng),
            "event_signature": f"evt:{idx:06d}#evt:ir",
            "analyst_id": "event_clustering" if rng.random() < 0.8 else "tower",
            "title": f"event {idx}",
            "category": "conflict",
            "time_start": start,
            "time_end": start + timedelta(hours=dur_h / 2),
            "produced_at": start + timedelta(minutes=5),
            "derived_from": [],
            "evidence_end": evidence_end,
            "actor_ids": actors,
        }
        if hazards:
            r = rng.random()
            if r < 0.01:
                row["time_start"] = None
            elif r < 0.015:
                row["time_start"] = None
                row["produced_at"] = None
                row["evidence_end"] = None
                row["time_end"] = None
            elif r < 0.03:
                row["evidence_end"] = None
            elif r < 0.04:
                row["time_start"] = start.isoformat().replace("+00:00", "Z")
            elif r < 0.05:
                row["evidence_end"] = evidence_end.replace(tzinfo=None)
            elif r < 0.055:
                row["id"] = str(row["id"])
            elif r < 0.058:
                row["id"] = "not-a-uuid"
            if rng.random() < 0.05 and actors:
                row["actor_ids"] = [str(a) for a in actors] + ["junk", None]
        rows.append(row)
    if hazards and n > 10:
        # Duplicate ids: the same event twice, once with a different actor
        # set, so an exact-key tie must resolve to the earlier row.
        for _ in range(max(1, n // 200)):
            a = rng.randrange(n)
            twin = dict(rows[a])
            twin["actor_ids"] = list(twin["actor_ids"])[: max(2, len(twin["actor_ids"]) // 2)]
            rows.insert(rng.randrange(len(rows) + 1), twin)
        # Exact end ties on shared actors between distinct events.
        for _ in range(max(1, n // 100)):
            a, b = rng.randrange(len(rows)), rng.randrange(len(rows))
            if a != b and rows[a].get("evidence_end") is not None:
                rows[b] = dict(rows[b])
                rows[b]["evidence_end"] = rows[a]["evidence_end"]
                rows[b]["actor_ids"] = list(rows[a]["actor_ids"] or ())[:5]
    return rows


__all__ = ["make_rows"]
