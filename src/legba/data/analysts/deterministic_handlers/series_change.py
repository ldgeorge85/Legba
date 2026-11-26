# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``series_change`` sub-handler — a deterministic change detector over LIVE series.

No model call. For every (series, subject) a live source writes into
``observations`` (N-L, migration 0246), it compares the latest window against
a baseline window under ONE declared rule and writes a finding that cites the
observation ids it rests on — each citation carrying the ``fetch_sha256`` of
the response the value was parsed from, so the receipt chain ends in stored
bytes (``fetch_versions`` → the evidence store).

THE RULE (``METHOD_VERSION`` ``series_change/2026-11.1``, stamped in every finding):

* values are read AS OF the run (``record_time <= as_of``, the latest vintage
  per day) — a later revision never leaks into an earlier run's read;
* windows are anchored on the NEWEST period HELD for that (series, subject),
  not on today — PortWatch lands ~4-5 days late, and a today-anchored window
  would read the lag as a collapse;
* ``current`` = the mean of the newest ``current_days`` daily values; the
  baseline = the ``baseline_days`` daily values immediately before them; a
  missing day is ABSENT (never zero-filled — absence is not a count);
* the estimator is desk_baseline's :func:`estimate_baseline` VERBATIM:
  trailing mean, ``max(stddev, sqrt(mean))`` Poisson-floored robust sigma,
  band ``mean ± n_sigma·sigma``, ``above`` only past an absolute floor
  ``min_current`` and ``below`` only when the baseline itself clears it;
* a pair with fewer than ``min_baseline_points`` baseline values or fewer
  than ``current_days`` current values is ``insufficient`` and never cited.

THE RULE-JUDGED SERIES (``series_change/2026-11.2``). A count's
baseline band is the wrong lens for a price or a policy rate: a Poisson floor
of ``sqrt(mean)`` on a $90 price is a +/-$19 band, and a rate that moves once
a quarter is "within" forever. So a series can be named by one of two
declared rules instead, and is then judged ONLY by them (never by the band):

* ``level_rules`` -- ``"<series glob><op><level>"`` (``op`` one of
  ``>= > <= <``, e.g. ``eia.pet_spot.rbrte>=100``): an event when a held value
  ENTERS the declared condition (the value before it did not satisfy it --
  ``level_entered``) or LEAVES it (``level_left``). Holding above a level is
  not an event.
* ``step_series`` -- series globs (e.g. ``bis.cbpol.*``): every change between
  two consecutive held values larger than ``step_min`` (default 0: any
  change) is an event (``step_up`` / ``step_down``) -- a rate decision is an
  event, never noise.

Both read the same as-of vintages and report the events whose later value
falls in the current window (the newest ``current_days`` held days); each
event cites the two observations it rests on. A pair not named by a rule
keeps the band rule exactly as before (an empty rule set changes nothing).

NAMED RULES BY SERIES FAMILY (``series_change/2026-11.3``). A rate
or exchange-rate FAMILY is given a rule by NAME through ``series_rules``
(``"<series glob>=<rule name>"``, e.g. ``ecb.fm.*=policy_rate``); the names
are a closed table here (:data:`NAMED_RULES`), so a family's judgement is
reviewed code, never a free number in an option:

* ``policy_rate`` -- every change between two consecutive held values is an
  event (``step_up`` / ``step_down``), whatever ``step_min`` says: a policy
  rate decision is never noise;
* ``fx_move`` -- an exchange rate moves every day, so a step on every change
  would be all noise: an event (``move_up`` / ``move_down``) when the NEWEST
  held value differs from the last held value BEFORE the current window by at
  least :data:`FX_MOVE_PCT` percent (relative). It cites the two values.

A series named by ``series_rules`` is judged ONLY by its named rule(s) (plus
any ``level_rules`` / ``step_series`` that also name it); with no
``series_rules`` option the analyst reads exactly as 2026-11.2 did.

The finding is always written (the deterministic kind's FINDING dispatch is
static per sub-handler); with no crossing it says so and cites nothing.
``derived_from`` = the cited observation ids.
"""

from __future__ import annotations

import fnmatch
import logging
import re
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult
from .desk_baseline import DEFAULT_N_SIGMA, estimate_baseline

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "series_change"
METHOD_VERSION = "series_change/2026-11.3"

DEFAULT_CURRENT_DAYS = 7
DEFAULT_BASELINE_DAYS = 28
DEFAULT_MIN_CURRENT = 5.0
DEFAULT_MIN_BASELINE_POINTS = 14
MAX_CITED_CROSSINGS = 50

__all__ = [
    "DEFAULT_BASELINE_DAYS",
    "DEFAULT_CURRENT_DAYS",
    "FX_MOVE_PCT",
    "METHOD_VERSION",
    "NAMED_RULES",
    "PROPOSED_SERIES_RULES",
    "SUB_HANDLER_NAME",
    "evaluate_pair",
    "handle",
    "move_event",
    "parse_level_rule",
    "parse_series_rule",
    "rule_events",
]

#: I-5 -- the relative move (percent) an ``fx_move`` family needs between the
#: last held value before the current window and the newest held value.
FX_MOVE_PCT = 2.0
#: I-5 -- the closed table of named rules ``series_rules`` may give a family.
NAMED_RULES: dict[str, dict[str, float]] = {
    "policy_rate": {"step_min": 0.0},
    "fx_move": {"move_pct": FX_MOVE_PCT},
}
#: ``<series glob>=<rule name>`` -- the option catalog pins the same shape.
SERIES_RULE_RE = re.compile(r"^([a-z0-9_.*?]+)=([a-z_]+)$")
#: I-5 -- the rules PROPOSED for the rate/FX families in the tree (the ECB
#: series from X-1 and the I-5 series). NOT registered by code: the roll adds
#: this list as the analyst's ``series_rules`` option.
PROPOSED_SERIES_RULES: tuple[str, ...] = (
    "ecb.fm.*=policy_rate",
    "ecb.exr.*=fx_move",
    "boe.iadb.*=policy_rate",
    "bcb.sgs.*=policy_rate",
    "nbu.exchange.*=fx_move",
    "nbu.key.*=policy_rate",     # the NBU key rates (source.nbu.key_rates)
)

#: ``<series glob><op><level>`` -- the option catalog pins the same shape.
LEVEL_RULE_RE = re.compile(r"^([a-z0-9_.*?]+)(>=|<=|>|<)(-?[0-9]+(?:\.[0-9]+)?)$")
_OPS = {">=": lambda v, x: v >= x, ">": lambda v, x: v > x,
        "<=": lambda v, x: v <= x, "<": lambda v, x: v < x}

#: Latest vintage per (series, subject, day) as of $2, for the live sources in
#: $1, over the lookback from $3. Rides observations_live_vintage_idx (0246).
_WINDOW_SQL = """
SELECT DISTINCT ON (source_id, series_id, subject, valid_from)
       id, source_id, series_id, subject, valid_from, value, unit,
       record_time, fetch_sha256
  FROM observations
 WHERE origin_class = 'live'
   AND source_id = ANY($1::text[])
   AND valid_from >= $3
   AND record_time <= $2
   AND value IS NOT NULL
 ORDER BY source_id, series_id, subject, valid_from, record_time DESC
"""

_LIVE_SOURCES_SQL = """
SELECT DISTINCT source_id FROM observations
 WHERE origin_class = 'live' AND source_id IS NOT NULL
   AND valid_from >= $1
"""


def evaluate_pair(
    points: Sequence[Mapping[str, Any]], *, current_days: int, baseline_days: int,
    n_sigma: float, min_current: float, min_baseline_points: int,
) -> dict[str, Any]:
    """Apply the rule to one (series, subject)'s daily points (pure, tested).

    ``points`` are rows with ``valid_from`` and ``value`` (any order)."""
    pts = sorted(points, key=lambda p: p["valid_from"])
    if not pts:
        return {"status": "insufficient", "reason": "no_points"}
    anchor: date = pts[-1]["valid_from"]
    cur_lo = anchor - timedelta(days=current_days - 1)
    base_lo = cur_lo - timedelta(days=baseline_days)
    current = [p for p in pts if cur_lo <= p["valid_from"] <= anchor]
    baseline = [p for p in pts if base_lo <= p["valid_from"] < cur_lo]
    if len(current) < current_days or len(baseline) < min_baseline_points:
        return {"status": "insufficient", "anchor": anchor.isoformat(),
                "current_n": len(current), "baseline_n": len(baseline)}
    cur_mean = statistics.fmean(float(p["value"]) for p in current)
    est = estimate_baseline(
        [float(p["value"]) for p in baseline], cur_mean,
        n_sigma=n_sigma, min_current=min_current,
    )
    return {
        "status": est.deviation,             # above | below | within
        "anchor": anchor.isoformat(),
        "current_window": [cur_lo.isoformat(), anchor.isoformat()],
        "baseline_window": [base_lo.isoformat(), (cur_lo - timedelta(days=1)).isoformat()],
        "current_mean": round(cur_mean, 3),
        "baseline_mean": round(est.expected, 3),
        "robust_sigma": round(est.robust_sigma, 3),
        "band": [round(est.band_low, 3), round(est.band_high, 3)],
        "z": None if est.deviation_sigma is None else round(est.deviation_sigma, 2),
        "current_n": len(current), "baseline_n": len(baseline),
        "current_points": current,
    }


def parse_level_rule(rule: str) -> tuple[str, str, float]:
    """``(glob, op, level)``; ``ValueError`` on a malformed rule."""
    m = LEVEL_RULE_RE.match(rule.replace(" ", ""))
    if m is None:
        raise ValueError(f"level rule {rule!r} is not '<series glob><op><level>'")
    return m.group(1), m.group(2), float(m.group(3))


def parse_series_rule(rule: str) -> tuple[str, str]:
    """``(glob, rule name)``; ``ValueError`` on a malformed rule or a name
    outside :data:`NAMED_RULES`."""
    m = SERIES_RULE_RE.match(rule.replace(" ", ""))
    if m is None:
        raise ValueError(f"series rule {rule!r} is not '<series glob>=<rule name>'")
    if m.group(2) not in NAMED_RULES:
        raise ValueError(f"series rule {rule!r}: {m.group(2)!r} is not one of {sorted(NAMED_RULES)}")
    return m.group(1), m.group(2)


def move_event(points: Sequence[Mapping[str, Any]], *, pct: float, current_days: int) -> dict[str, Any] | None:
    """The ``fx_move`` event over ``points`` (pure, tested): the newest held
    value against the last held value BEFORE the current window (the newest
    ``current_days`` held days); an event when the relative move is at least
    ``pct`` percent. ``None`` without a value before the window or on a zero
    base (a relative move from zero is undefined, never infinite)."""
    pts = sorted(points, key=lambda p: p["valid_from"])
    if len(pts) < 2:
        return None
    cur = pts[-1]
    lo = cur["valid_from"] - timedelta(days=current_days - 1)
    before = [p for p in pts if p["valid_from"] < lo]
    if not before:
        return None
    prev = before[-1]
    a, b = float(prev["value"]), float(cur["value"])
    if a == 0:
        return None
    move = (b - a) / abs(a) * 100.0
    if abs(move) < pct:
        return None
    return {"status": "move_up" if move > 0 else "move_down", "rule": f"move>={pct:g}%",
            "move_pct": round(move, 3), "prev": prev, "cur": cur}


def rule_events(
    points: Sequence[Mapping[str, Any]], *, levels: Sequence[tuple[str, float]] = (),
    step: bool = False, step_min: float = 0.0,
) -> list[dict[str, Any]]:
    """Every event over ``points`` (pure, tested): for each pair of
    consecutive held values, a level ENTERED or LEFT (``levels`` =
    ``[(op, level)]``) and, with ``step``, a change larger than ``step_min``.
    ``prev`` / ``cur`` are the two points the event rests on."""
    pts = sorted(points, key=lambda p: p["valid_from"])
    out: list[dict[str, Any]] = []
    for prev, cur in zip(pts, pts[1:]):
        a, b = float(prev["value"]), float(cur["value"])
        for op, level in levels:
            was, now = _OPS[op](a, level), _OPS[op](b, level)
            if was != now:
                out.append({"status": "level_entered" if now else "level_left",
                            "rule": f"{op}{level:g}", "prev": prev, "cur": cur})
        if step and b != a and abs(b - a) > step_min:
            out.append({"status": "step_up" if b > a else "step_down",
                        "rule": f"step>{step_min:g}", "prev": prev, "cur": cur})
    return out


def _rules_for(series_id: str, levels: Sequence[tuple[str, str, float]],
               steps: Sequence[str],
               named: Sequence[tuple[str, str]] = ()) -> tuple[list[tuple[str, float]], bool, set[str]]:
    mine = [(op, lv) for glob, op, lv in levels if fnmatch.fnmatchcase(series_id, glob)]
    names = {name for glob, name in named if fnmatch.fnmatchcase(series_id, glob)}
    return mine, any(fnmatch.fnmatchcase(series_id, g) for g in steps), names


def _judge_by_rules(pts: Sequence[Mapping[str, Any]], levels: list[tuple[str, float]], step: bool,
                    step_min: float, current_days: int, names: set[str] | frozenset[str] = frozenset(),
                    ) -> dict[str, Any]:
    """The rule-judged pair's report: the events whose later value is in the
    current window (anchored on the newest held day, like the band rule)."""
    if len(pts) < 2:
        return {"status": "insufficient", "reason": "fewer than two held values"}
    anchor: date = max(p["valid_from"] for p in pts)
    lo = anchor - timedelta(days=current_days - 1)
    events = [e for e in rule_events(pts, levels=levels, step=step, step_min=step_min)
              if e["cur"]["valid_from"] >= lo]
    if "policy_rate" in names:
        policy_min = NAMED_RULES["policy_rate"]["step_min"]
        events += [{**e, "rule": f"policy_rate:{e['rule']}"}
                   for e in rule_events(pts, step=True, step_min=policy_min)
                   if e["cur"]["valid_from"] >= lo
                   and not any(x["cur"] is e["cur"] and x["status"] == e["status"] for x in events)]
    if "fx_move" in names:
        mv = move_event(pts, pct=NAMED_RULES["fx_move"]["move_pct"], current_days=current_days)
        if mv is not None:
            events.append({**mv, "rule": f"fx_move:{mv['rule']}"})
    return {"status": "event" if events else "quiet", "anchor": anchor.isoformat(),
            "current_window": [lo.isoformat(), anchor.isoformat()], "events": events}


def _citation(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ref_kind": "observation", "ref_id": str(row["id"]),
        "series_id": row["series_id"], "subject": row["subject"],
        "valid_from": row["valid_from"].isoformat(),
        "record_time": row["record_time"].isoformat(),
        "fetch_sha256": row["fetch_sha256"],
        "source_id": row["source_id"],
    }


async def handle(
    inputs: list[dict[str, Any]], options: Mapping[str, Any], deps: Any | None,
) -> AnalystMethodResult:
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        raise RuntimeError(
            "series_change requires a live deps.pg_pool — refusing to report "
            "'no change' without reading the series"
        )
    current_days = max(1, int(options.get("current_days", DEFAULT_CURRENT_DAYS)))
    baseline_days = max(2, int(options.get("baseline_days", DEFAULT_BASELINE_DAYS)))
    n_sigma = float(options.get("n_sigma", DEFAULT_N_SIGMA))
    min_current = float(options.get("min_current", DEFAULT_MIN_CURRENT))
    min_baseline = max(2, int(options.get("min_baseline_points", DEFAULT_MIN_BASELINE_POINTS)))
    sources = [str(s) for s in (options.get("source_ids") or [])]
    level_rules = [str(r) for r in (options.get("level_rules") or [])]
    levels = [parse_level_rule(r) for r in level_rules]
    steps = [str(g) for g in (options.get("step_series") or [])]
    step_min = float(options.get("step_min", 0.0))
    series_rules = [str(r) for r in (options.get("series_rules") or [])]
    named = [parse_series_rule(r) for r in series_rules]
    as_of = datetime.now(tz=timezone.utc)
    # Lookback covers both windows plus the provider lag and slack.
    since = (as_of - timedelta(days=current_days + baseline_days + 30)).date()

    async with pool.acquire() as conn:
        if not sources:
            sources = sorted(r["source_id"] for r in await conn.fetch(_LIVE_SOURCES_SQL, since))
        rows = await conn.fetch(_WINDOW_SQL, sources, as_of, since) if sources else []

    pairs: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        pairs[(r["source_id"], r["series_id"], r["subject"])].append(r)

    crossings: list[dict[str, Any]] = []
    counts = {"above": 0, "below": 0, "within": 0, "insufficient": 0, "event": 0, "quiet": 0}
    for (source_id, series_id, subject), pts in sorted(pairs.items()):
        mine, step, names = _rules_for(series_id, levels, steps, named)
        if mine or step or names:
            res = _judge_by_rules(pts, mine, step, step_min, current_days, names)
            counts[res["status"]] += 1
            for e in res.get("events", []):
                crossings.append({
                    "source_id": source_id, "series_id": series_id, "subject": subject,
                    "status": e["status"], "rule": e["rule"], "anchor": res["anchor"],
                    "from": {"valid_from": e["prev"]["valid_from"].isoformat(),
                             "value": float(e["prev"]["value"])},
                    "to": {"valid_from": e["cur"]["valid_from"].isoformat(),
                           "value": float(e["cur"]["value"])},
                    "z": None, "citations": [_citation(e["prev"]), _citation(e["cur"])],
                })
            continue
        res = evaluate_pair(
            pts, current_days=current_days, baseline_days=baseline_days,
            n_sigma=n_sigma, min_current=min_current, min_baseline_points=min_baseline,
        )
        counts[res["status"] if res["status"] in counts else "insufficient"] += 1
        if res["status"] in ("above", "below"):
            cited = [_citation(p) for p in res.pop("current_points")]
            crossings.append({"source_id": source_id, "series_id": series_id,
                              "subject": subject, **res, "citations": cited})
    crossings.sort(key=lambda c: -abs(c.get("z") or 0.0))
    shown = crossings[:MAX_CITED_CROSSINGS]
    citations = [c for x in shown for c in x["citations"]]
    lines = [_line(x) for x in shown]
    title = (f"Series change: {len(crossings)} crossing(s) across {len(pairs)} series"
             if crossings else f"Series change: no crossings across {len(pairs)} series")
    body = (
        f"Rule {METHOD_VERSION} (anchored-window robust z, desk_baseline's estimator): current = newest {current_days} held days, baseline = "
        f"the {baseline_days} days before, n_sigma={n_sigma}, floor={min_current}; level rules "
        f"{level_rules}, step series {steps} (step_min={step_min}), series rules {series_rules}. "
        f"above={counts['above']} below={counts['below']} within={counts['within']} "
        f"insufficient={counts['insufficient']} rule_events={counts['event']} "
        f"rule_quiet={counts['quiet']}.\n" + "\n".join(lines)
    )
    finding = FindingPayload(
        title=title[:2048], body=body[:65536], confidence=1.0 if pairs else 0.0,
        tags=["series_change", "deterministic", "no_llm"],
        data={
            "sub_handler": SUB_HANDLER_NAME, "method_version": METHOD_VERSION,
            "as_of": as_of.isoformat(), "sources": sources, "counts": counts,
            "crossings": [{k: v for k, v in x.items() if k != "citations"} for x in shown],
            "crossings_total": len(crossings),
            "citations": citations,
        },
    )
    from uuid import UUID  # noqa: PLC0415

    derived = [UUID(c["ref_id"]) for c in citations]
    logger.info("series_change.done pairs=%d crossings=%d", len(pairs), len(crossings))
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        derived_from=derived,
    )


def _line(x: Mapping[str, Any]) -> str:
    name = f"{x['subject']} {x['series_id'].rsplit('.', 1)[-1]}"
    if "rule" in x:
        return (f"{name}: {x['status']} ({x['rule']}: {x['from']['value']:g} on {x['from']['valid_from']} "
                f"-> {x['to']['value']:g} on {x['to']['valid_from']})")
    return (f"{name}: {x['status']} (mean {x['current_mean']} over {x['current_window'][0]}.."
            f"{x['current_window'][1]} vs baseline {x['baseline_mean']}, band {x['band'][0]}..{x['band'][1]}, "
            f"z={x['z']})")
