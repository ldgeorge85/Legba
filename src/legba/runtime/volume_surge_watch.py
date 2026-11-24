# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``volume_surge`` — a subject's signal count far above its own baseline,
from several independent outlets (L-1, the single alert path).

THE GAP. On 2026-11-13 the FAA's nationwide ground stop produced about 25
signals from 14 outlets within the hour, and nothing surfaced it: no event row
formed, no alert, while three engine-noise pages were delivered in the same
hour. Every existing detector looks at findings or at a desk's daily count;
nothing watched the hour.

THE DETECTOR (deterministic, no model). Every ten minutes, per subject (a
country: an ISO2 code on ``signals.geo``):

* ``count``   — live-origin signals fetched in the last :data:`WINDOW_MINUTES`
  that are FRESH (published at most :data:`FRESH_HOURS` before they were
  fetched — a backlog drain or a feed replay is not news; 11-13 15:00-18:00 was
  one, median lag 16 h, and it lit up a dozen countries);
* ``outlets`` — distinct independent outlets among them (two feeds of one
  publisher count once: :func:`legba.data.analysts.source_independence.publisher_of`);
* the subject's OWN baseline — the mean and standard deviation of its hourly
  fresh counts over the previous :data:`BASELINE_DAYS` days (zero hours
  included; the deviation floored at the Poisson ``sqrt(mean)`` and at 1, the
  ``desk_baseline`` estimator's robustness rule).

It FIRES when ``count >= MIN_SIGNALS`` AND ``outlets >= MIN_OUTLETS`` AND
``count >= mean + MIN_Z * sigma`` AND ``count >= MIN_RATIO * mean``.

THE THRESHOLDS were chosen on the last 30 days of live data (read-only, the
L-1 report lists every episode): 12 signals, 8 outlets, z 5, ratio 3 fire on
14 episodes in 30 days (about 3 a week) including the ground stop (US, 74
signals from 20 outlets at 22:50Z against an hourly mean of 19). Each is an
env knob (``LEGBA_VOLUME_SURGE_*``).

THE ALERT. One ``volume_surge`` episode per subject: raised (medium) on every
tick the surge holds — the first raise opens it, later raises update it — and
closed once the subject has not fired for :data:`CLOSE_AFTER_MINUTES`. The
initial page rule does NOT page this kind: it is logged and on the Watch
surface until the owner adds a rule entry.

LOOP SAFETY. The reads are async; the per-row work (freshness parsing, the
baseline vectors) runs in a worker thread (``asyncio.to_thread``) — the
runtime's loop-lag monitor is live. A subject's baseline is cached for the
hour. Never raises into the watchdog.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

KIND = "volume_surge"
SEVERITY = "medium"
MIN_INTERVAL_S = 600.0

ENV_ENABLED = "LEGBA_VOLUME_SURGE_CHECK"


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class SurgeThresholds:
    window_minutes: float = 60.0
    min_signals: float = 12.0
    min_outlets: float = 8.0
    min_z: float = 5.0
    min_ratio: float = 3.0
    baseline_days: float = 14.0
    fresh_hours: float = 3.0
    close_after_minutes: float = 60.0

    @classmethod
    def from_env(cls) -> "SurgeThresholds":
        d = cls()
        return cls(**{
            f: _env_float(f"LEGBA_VOLUME_SURGE_{f.upper()}", getattr(d, f))
            for f in d.__dataclass_fields__
        })


WINDOW_MINUTES = SurgeThresholds.window_minutes
FRESH_HOURS = SurgeThresholds.fresh_hours
BASELINE_DAYS = SurgeThresholds.baseline_days
CLOSE_AFTER_MINUTES = SurgeThresholds.close_after_minutes


@dataclass
class Surge:
    subject: str
    count: int
    outlets: int
    mean: float
    sigma: float
    z: float
    ratio: float
    signal_ids: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    top_outlets: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"Signal surge: {self.subject} — {self.count} signals from "
            f"{self.outlets} independent outlets in the last hour "
            f"({self.ratio:.1f}x its hourly mean of {self.mean:.1f})"
        )

    def detail(self, th: SurgeThresholds) -> str:
        lines = [
            f"{self.count} fresh live signals tagged {self.subject} in the last "
            f"{th.window_minutes:g} min from {self.outlets} independent outlets; "
            f"its hourly baseline over {th.baseline_days:g} days is mean "
            f"{self.mean:.2f}, sigma {self.sigma:.2f} (z {self.z:.1f}).",
            f"Fires at >= {th.min_signals:g} signals, >= {th.min_outlets:g} outlets, "
            f"z >= {th.min_z:g} and >= {th.min_ratio:g}x the mean.",
            "Outlets: " + ", ".join(self.top_outlets[:12]),
            "Headlines:",
            *[f"- {t}" for t in self.titles[:6]],
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pure core (also the offline evaluation's)
# ---------------------------------------------------------------------------


_TIME_OF_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def parse_instant(raw: Any) -> Optional[datetime]:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw or "").strip()
    # A date with no time of day ("20261113", "2026-11-13") says nothing about
    # freshness within the day: unknown, like a missing value (GDELT rows).
    if len(text) < 16 or not _TIME_OF_DAY.match(text):
        return None
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def is_fresh(fetched_at: datetime, published_raw: Any, fresh_hours: float) -> bool:
    """Published at most ``fresh_hours`` before it was fetched (an unparseable
    or missing publication time counts as fresh: the fetch is the only clock)."""
    published = parse_instant(published_raw)
    if published is None:
        return True
    return fetched_at - published <= timedelta(hours=fresh_hours)


def outlet_key(source_id: Any) -> str:
    from ..data.analysts.source_independence import publisher_of

    sid = str(source_id or "")
    return publisher_of(sid) or sid


def baseline_stats(hourly: Sequence[int]) -> tuple[float, float]:
    """``(mean, sigma)`` of an hourly count vector; sigma floored at
    ``sqrt(max(mean, 1))`` (a quiet subject never gets a near-zero band)."""
    n = len(hourly)
    if n == 0:
        return 0.0, 1.0
    mean = sum(hourly) / n
    var = sum((x - mean) ** 2 for x in hourly) / (n - 1) if n > 1 else 0.0
    return mean, max(math.sqrt(var), math.sqrt(max(mean, 1.0)))


def evaluate(count: int, outlets: int, mean: float, sigma: float,
             th: SurgeThresholds) -> tuple[bool, float, float]:
    z = (count - mean) / sigma if sigma > 0 else 0.0
    ratio = count / max(mean, 0.5)
    fires = (
        count >= th.min_signals and outlets >= th.min_outlets
        and z >= th.min_z and ratio >= th.min_ratio
    )
    return fires, z, ratio


def hourly_vector(instants: Iterable[datetime], *, end_hour: datetime, hours: int) -> list[int]:
    """Counts per hour for the ``hours`` hours before ``end_hour`` (zeros kept)."""
    start = end_hour - timedelta(hours=hours)
    counts = Counter(
        int((t - start).total_seconds() // 3600) for t in instants if start <= t < end_hour
    )
    return [counts.get(i, 0) for i in range(hours)]


def window_candidates(rows: Sequence[Mapping[str, Any]], th: SurgeThresholds) -> dict[str, dict[str, Any]]:
    """Group the window's fresh rows by subject: count, outlets, ids, titles."""
    out: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "outlets": Counter(), "ids": [], "titles": []}
    )
    for r in rows:
        fetched = parse_instant(r["fetched_at"])
        if fetched is None or not is_fresh(fetched, r.get("published_at"), th.fresh_hours):
            continue
        c = out[str(r["subject"])]
        c["count"] += 1
        c["outlets"][outlet_key(r.get("source_id"))] += 1
        if len(c["ids"]) < 25:
            c["ids"].append(str(r.get("id")))
        title = str(r.get("title") or "").strip()
        if title and len(c["titles"]) < 8 and title not in c["titles"]:
            c["titles"].append(title[:160])
    return {
        s: c for s, c in out.items()
        if c["count"] >= th.min_signals and len(c["outlets"]) >= th.min_outlets
    }


def detect(
    candidates: Mapping[str, Mapping[str, Any]],
    baselines: Mapping[str, tuple[float, float]],
    th: SurgeThresholds,
) -> list[Surge]:
    surges: list[Surge] = []
    for subject, c in sorted(candidates.items()):
        mean, sigma = baselines.get(subject, (0.0, 1.0))
        fires, z, ratio = evaluate(c["count"], len(c["outlets"]), mean, sigma, th)
        if fires:
            surges.append(Surge(
                subject=subject, count=c["count"], outlets=len(c["outlets"]),
                mean=mean, sigma=sigma, z=z, ratio=ratio,
                signal_ids=list(c["ids"]), titles=list(c["titles"]),
                top_outlets=[o for o, _ in c["outlets"].most_common(12)],
            ))
    return surges


# ---------------------------------------------------------------------------
# Live pass (the watchdog tick)
# ---------------------------------------------------------------------------


def _live_clause() -> str:
    from ..data.provenance.origin import origin_class_clause

    return origin_class_clause("s")


_WINDOW_SQL = """
    SELECT s.id, g AS subject, s.source_id, s.fetched_at,
           s.payload ->> 'published_at' AS published_at,
           left(s.payload ->> 'title', 160) AS title
      FROM signals s, unnest(s.geo) AS g
     WHERE s.fetched_at >= $1 AND s.fetched_at < $2
       AND {live}
"""

_BASELINE_SQL = """
    SELECT g AS subject, s.fetched_at, s.payload ->> 'published_at' AS published_at
      FROM signals s, unnest(s.geo) AS g
     WHERE s.fetched_at >= $1 AND s.fetched_at < $2
       AND s.geo && $3::text[] AND g = ANY($3::text[])
       AND {live}
"""
# Live EXPLAIN (11-13, read-only): the window read is an index scan on
# signals_fetched_at_idx, ~7 ms; the baseline read (14 days, three busy
# subjects) a BitmapAnd of the fetched_at and geo GIN indexes, ~0.5 s — run at
# most once per subject per hour, and only for a subject already past the
# count and outlet floors this tick.

_LAST_PASS: dict[int, float] = {}
#: (subject, end_hour iso) → (mean, sigma); bounded by subject count.
_BASELINE_CACHE: dict[tuple[str, str], tuple[float, float]] = {}
#: wd id → subject → the last time it fired (episode bookkeeping for closes).
_FIRING: dict[int, dict[str, datetime]] = {}


def enabled() -> bool:
    raw = (os.environ.get(ENV_ENABLED) or "").strip().lower()
    return raw not in ("0", "false", "off", "no")


def _baselines_from_rows(
    rows: Sequence[Mapping[str, Any]], subjects: Iterable[str], end_hour: datetime,
    th: SurgeThresholds,
) -> dict[str, tuple[float, float]]:
    by_subject: dict[str, list[datetime]] = defaultdict(list)
    for r in rows:
        fetched = parse_instant(r["fetched_at"])
        if fetched is not None and is_fresh(fetched, r.get("published_at"), th.fresh_hours):
            by_subject[str(r["subject"])].append(fetched)
    hours = int(th.baseline_days * 24)
    return {
        s: baseline_stats(hourly_vector(by_subject.get(s, []), end_hour=end_hour, hours=hours))
        for s in subjects
    }


async def check_volume_surge_once(
    wd: Any, *, now: Optional[datetime] = None, force: bool = False,
    thresholds: Optional[SurgeThresholds] = None,
) -> list[Surge]:
    """One pass: raise every surging subject, close the ones that cleared.

    Leader-gated and rate-limited like its siblings. Never raises.
    """
    try:
        if not enabled() or wd._pg is None:
            return []
        if wd._is_leader is not None and not wd._is_leader():
            return []
        mono = time.monotonic()
        last = _LAST_PASS.get(id(wd))
        if not force and last is not None and (mono - last) < MIN_INTERVAL_S:
            return []
        _LAST_PASS[id(wd)] = mono
        th = thresholds or SurgeThresholds.from_env()
        now = now or datetime.now(timezone.utc)
        start = now - timedelta(minutes=th.window_minutes)
        live = _live_clause()
        async with wd._pg.acquire() as conn:
            window = [dict(r) for r in await conn.fetch(
                _WINDOW_SQL.format(live=live), start, now)]
        candidates = await asyncio.to_thread(window_candidates, window, th)
        end_hour = start.replace(minute=0, second=0, microsecond=0)
        missing = [s for s in candidates if (s, end_hour.isoformat()) not in _BASELINE_CACHE]
        if missing:
            async with wd._pg.acquire() as conn:
                rows = [dict(r) for r in await conn.fetch(
                    _BASELINE_SQL.format(live=live),
                    end_hour - timedelta(days=th.baseline_days), end_hour, missing)]
            fresh = await asyncio.to_thread(_baselines_from_rows, rows, missing, end_hour, th)
            if len(_BASELINE_CACHE) > 2000:
                _BASELINE_CACHE.clear()
            for s, stats in fresh.items():
                _BASELINE_CACHE[(s, end_hour.isoformat())] = stats
        baselines = {s: _BASELINE_CACHE[(s, end_hour.isoformat())] for s in candidates}
        surges = detect(candidates, baselines, th)
        await _raise(wd, surges, now, th)
        return surges
    except Exception as exc:  # noqa: BLE001 — never kill the watchdog loop
        logger.warning("volume_surge.check_failed err=%s", exc)
        return []


async def _raise(wd: Any, surges: list[Surge], now: datetime, th: SurgeThresholds) -> None:
    from ..data.alerts.raise_alert import raise_alert

    pool = getattr(wd._pg, "pool", wd._pg)
    firing = _FIRING.setdefault(id(wd), {})
    for s in surges:
        firing[s.subject] = now
        logger.info(
            "volume_surge.firing subject=%s count=%d outlets=%d mean=%.2f z=%.1f ratio=%.1f",
            s.subject, s.count, s.outlets, s.mean, s.z, s.ratio,
        )
        await raise_alert(
            pool, kind=KIND, severity=SEVERITY, summary=s.summary(),
            detail=s.detail(th), subject_id=s.subject,
            evidence_refs=[f"signal:{i}" for i in s.signal_ids],
            raised_by="volume_surge_watch", dispatcher=wd._alert_sinks,
            hold=None if wd._alert_sinks is not None else "no alert dispatcher wired",
            now=now,
        )
    clear_after = timedelta(minutes=th.close_after_minutes)
    for subject, last_fired in list(firing.items()):
        if now - last_fired < clear_after:
            continue
        del firing[subject]
        await raise_alert(
            pool, kind=KIND, severity="info", close=True, subject_id=subject,
            summary=f"Signal surge over: {subject} back under its thresholds",
            raised_by="volume_surge_watch", dispatcher=wd._alert_sinks, now=now,
        )


__all__ = [
    "CLOSE_AFTER_MINUTES",
    "ENV_ENABLED",
    "KIND",
    "Surge",
    "SurgeThresholds",
    "baseline_stats",
    "check_volume_surge_once",
    "detect",
    "evaluate",
    "hourly_vector",
    "is_fresh",
    "window_candidates",
]
