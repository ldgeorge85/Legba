#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Decision-shadow report (W11-F108) — READ-ONLY.

Reads ``decision_shadow_ledger`` (migration 0238) for one leg over a window and
prints, overall and per analyst:

  * rows, shadow errors, gate-unavailable rows, mean shadow latency;
  * the 2x2 confusion, gate (rows) vs shadow (columns), over the rows where
    BOTH answered yes/no — the gate's 'unavailable' and the shadow's errors
    are counted, never folded into a cell;
  * the agreement rate over those rows;
  * the calibration by ``p_yes`` decile: per decile the rows, how many the gate
    said yes to, the gate-yes RATE in the decile (a calibrated shadow tracks
    the decile's midpoint, with the gate as the reference) and the decile's
    SHARE of all the gate's yes;
  * agreement BY DAY (UTC).

W11-F148: every figure is grouped by the (``gate_component_id``,
``shadow_component_id``) pair the ROWS carry, and printed with it. On
2026-11-05 the roles swapped (clef-flash the gate, the chat judge its shadow),
so which model held which role is read off each row, never assumed. A chat
shadow ledgers NULL ``shadow_p_yes``: its rows count toward agreement and the
confusion like any other, and its calibration section says it has no p_yes.

No model calls, no writes: one READ ONLY transaction with a 120 s statement
timeout. Run on the host against the loopback port::

  PYTHONPATH=src LEGBA_DATA_PG_HOST=127.0.0.1 \\
      python3 scripts/decision_shadow_report.py \\
      [--since 2026-10-23] [--until 2026-10-30] [--leg bearing_shadow] \\
      [--json-out PATH] [--env-file PATH]

The DSN is the one every other host script that reads the live database uses
(``scripts/rejudge_floor_critiques.py`` and friends):
``legba.data.config.PostgresConfig.from_env().dsn`` — ``LEGBA_DATA_PG_*`` over
``POSTGRES_*`` over the compose defaults, with the repo-root ``.env`` loaded at
import and never overriding the shell (W11-F115: a private resolver here never
loaded the ``.env`` and had no password default, so without ``--env-file`` it
connected with no password and the host run failed authentication).
``--env-file`` names a different ``.env`` to load first, the same way. The
repo ``.env`` names the in-network host ``postgres``, so on the host set
``LEGBA_DATA_PG_HOST=127.0.0.1`` in the shell, as above.

``--since`` defaults to seven days before ``--until`` (default: now); both take
an ISO date or timestamp, read as UTC when no zone is given.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

ALL = "(all)"

_PER_ANALYST_SQL = """
SELECT coalesce(analyst_id, '(none)')                                       AS analyst_id,
       gate_component_id,
       shadow_component_id,
       count(*)                                                             AS rows,
       count(*) FILTER (WHERE shadow_error IS NOT NULL)                     AS errors,
       count(*) FILTER (WHERE gate_verdict = 'unavailable')                 AS gate_unavailable,
       count(*) FILTER (WHERE gate_verdict = 'yes' AND shadow_choice = 'yes') AS yy,
       count(*) FILTER (WHERE gate_verdict = 'yes' AND shadow_choice = 'no')  AS yn,
       count(*) FILTER (WHERE gate_verdict = 'no'  AND shadow_choice = 'yes') AS ny,
       count(*) FILTER (WHERE gate_verdict = 'no'  AND shadow_choice = 'no')  AS nn,
       coalesce(sum(shadow_latency_ms) FILTER (WHERE shadow_error IS NULL), 0) AS latency_sum_ms,
       count(shadow_latency_ms) FILTER (WHERE shadow_error IS NULL)          AS latency_n
  FROM decision_shadow_ledger
 WHERE leg = $1 AND occurred_at >= $2 AND occurred_at < $3
 GROUP BY 1, 2, 3
 ORDER BY 1, 2, 3
"""

_DECILE_SQL = """
SELECT coalesce(analyst_id, '(none)')                          AS analyst_id,
       gate_component_id,
       shadow_component_id,
       least(floor(shadow_p_yes * 10)::int, 9)                  AS decile,
       count(*)                                                AS rows,
       count(*) FILTER (WHERE gate_verdict = 'yes')            AS gate_yes,
       count(*) FILTER (WHERE gate_verdict IN ('yes', 'no'))   AS gate_answered
  FROM decision_shadow_ledger
 WHERE leg = $1 AND occurred_at >= $2 AND occurred_at < $3
   AND shadow_p_yes IS NOT NULL
 GROUP BY 1, 2, 3, 4
 ORDER BY 1, 2, 3, 4
"""

# W11-F148 — agreement BY DAY, per (gate, shadow) pairing: the roles swapped on
# 2026-11-05 (clef-flash gate, the chat judge its shadow), so a day is only
# comparable with a day of the same pairing, and the pairing is read off the
# row, never assumed.
_BY_DAY_SQL = """
SELECT (occurred_at AT TIME ZONE 'UTC')::date                                AS day,
       gate_component_id,
       shadow_component_id,
       count(*)                                                             AS rows,
       count(*) FILTER (WHERE shadow_error IS NOT NULL)                     AS errors,
       count(*) FILTER (WHERE gate_verdict = 'unavailable')                 AS gate_unavailable,
       count(*) FILTER (WHERE gate_verdict = 'yes' AND shadow_choice = 'yes') AS yy,
       count(*) FILTER (WHERE gate_verdict = 'yes' AND shadow_choice = 'no')  AS yn,
       count(*) FILTER (WHERE gate_verdict = 'no'  AND shadow_choice = 'yes') AS ny,
       count(*) FILTER (WHERE gate_verdict = 'no'  AND shadow_choice = 'no')  AS nn
  FROM decision_shadow_ledger
 WHERE leg = $1 AND occurred_at >= $2 AND occurred_at < $3
 GROUP BY 1, 2, 3
 ORDER BY 1, 2, 3
"""

_SUM_KEYS = ("rows", "errors", "gate_unavailable", "yy", "yn", "ny", "nn",
             "latency_sum_ms", "latency_n")
_DAY_KEYS = ("rows", "errors", "gate_unavailable", "yy", "yn", "ny", "nn")

#: The pairing label for the (all) row when the window spans more than one.
MIXED = "(mixed)"
#: The pairing label when a row set carries no component columns.
UNKNOWN = "?"


def _pairing(row: Mapping[str, Any]) -> tuple[str, str]:
    """``(gate component, shadow component)`` as the ROW names them."""
    return (str(row.get("gate_component_id") or UNKNOWN),
            str(row.get("shadow_component_id") or UNKNOWN))


def _key(row: Mapping[str, Any]) -> str:
    """One summary per (analyst, gate, shadow): a window spanning the role
    swap reads as two rows, each saying which component held which role."""
    gate, shadow = _pairing(row)
    return f"{row['analyst_id']} | gate={gate} | shadow={shadow}"


def _empty_deciles() -> dict[int, dict[str, int]]:
    return {i: {"rows": 0, "gate_yes": 0, "gate_answered": 0} for i in range(10)}


def _parse_when(raw: str) -> datetime:
    when = datetime.fromisoformat(raw)
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def summarize(
    per_analyst: Iterable[Mapping[str, Any]],
    deciles: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """``{analyst | gate | shadow, or ALL: summary}`` from the two aggregate
    result sets.

    Pure — the report's arithmetic, testable without a database. Each key is
    one (analyst, gate component, shadow component) group, and its record
    carries ``analyst_id`` / ``gate_component_id`` / ``shadow_component_id``
    as the ROWS named them (W11-F148: the roles swap, so the report never
    assumes which model was the gate). ``ALL`` sums the groups (latency is
    re-weighted by its own count, never averaged of averages); its pairing
    reads :data:`MIXED` when the window holds more than one."""
    out: dict[str, dict[str, Any]] = {}
    total: dict[str, Any] = {k: 0 for k in _SUM_KEYS}
    pairings: set[tuple[str, str]] = set()

    def _label(rec: dict[str, Any], row: Mapping[str, Any]) -> None:
        gate, shadow = _pairing(row)
        pairings.add((gate, shadow))
        rec.setdefault("analyst_id", str(row["analyst_id"]))
        rec.setdefault("gate_component_id", gate)
        rec.setdefault("shadow_component_id", shadow)

    for row in per_analyst:
        rec: dict[str, Any] = {k: int(row.get(k) or 0) for k in _SUM_KEYS}
        _label(rec, row)
        out[_key(row)] = rec
        for k in _SUM_KEYS:
            total[k] += rec[k]
    out[ALL] = total
    for rec in out.values():
        rec["deciles"] = _empty_deciles()
    for row in deciles:
        d = min(max(int(row["decile"]), 0), 9)
        for key in (_key(row), ALL):
            rec = out.setdefault(key, {**{k: 0 for k in _SUM_KEYS},
                                       "deciles": _empty_deciles()})
            if key != ALL:
                _label(rec, row)
            cell = rec["deciles"][d]
            cell["rows"] += int(row.get("rows") or 0)
            cell["gate_yes"] += int(row.get("gate_yes") or 0)
            cell["gate_answered"] += int(row.get("gate_answered") or 0)
    only = next(iter(pairings)) if len(pairings) == 1 else None
    total["analyst_id"] = ALL
    total["gate_component_id"] = only[0] if only else (MIXED if pairings else "—")
    total["shadow_component_id"] = only[1] if only else (MIXED if pairings else "—")
    for rec in out.values():
        both = rec["yy"] + rec["yn"] + rec["ny"] + rec["nn"]
        rec["both_answered"] = both
        rec["agreement"] = (rec["yy"] + rec["nn"]) / both if both else None
        rec["mean_latency_ms"] = (
            rec["latency_sum_ms"] / rec["latency_n"] if rec["latency_n"] else None
        )
        gate_yes_total = sum(c["gate_yes"] for c in rec["deciles"].values())
        for cell in rec["deciles"].values():
            cell["gate_yes_rate"] = (
                cell["gate_yes"] / cell["gate_answered"] if cell["gate_answered"] else None
            )
            cell["share_of_gate_yes"] = (
                cell["gate_yes"] / gate_yes_total if gate_yes_total else None
            )
    return out


def summarize_by_day(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Agreement per (UTC day, gate component, shadow component), in day
    order — pure, like :func:`summarize`. A row with a NULL ``shadow_p_yes``
    (a chat shadow) counts exactly like any other: agreement needs only the
    two yes/no answers."""
    out: list[dict[str, Any]] = []
    for row in rows:
        gate, shadow = _pairing(row)
        rec: dict[str, Any] = {k: int(row.get(k) or 0) for k in _DAY_KEYS}
        day = row.get("day")
        rec["day"] = day.isoformat() if hasattr(day, "isoformat") else str(day)
        rec["gate_component_id"] = gate
        rec["shadow_component_id"] = shadow
        both = rec["yy"] + rec["yn"] + rec["ny"] + rec["nn"]
        rec["both_answered"] = both
        rec["agreement"] = (rec["yy"] + rec["nn"]) / both if both else None
        out.append(rec)
    out.sort(key=lambda r: (r["day"], r["gate_component_id"], r["shadow_component_id"]))
    return out


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.1%}"


def render(summary: Mapping[str, Mapping[str, Any]], *, leg: str,
           since: datetime, until: datetime,
           by_day: Iterable[Mapping[str, Any]] = ()) -> str:
    lines = [
        f"# Decision shadow — leg `{leg}`, {since:%Y-%m-%d %H:%MZ} → {until:%Y-%m-%d %H:%MZ}",
        "",
        "| analyst | gate component | shadow component | rows | shadow errors "
        "| gate unavailable | both answered | agreement "
        "| gate yes→shadow yes | yes→no | no→yes | no→no | mean latency |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    order = [k for k in summary if k != ALL] + [ALL]
    for key in order:
        r = summary[key]
        lat = "—" if r["mean_latency_ms"] is None else f"{r['mean_latency_ms']:.0f} ms"
        lines.append(
            f"| {r.get('analyst_id', key)} | {r.get('gate_component_id', UNKNOWN)} | "
            f"{r.get('shadow_component_id', UNKNOWN)} | {r['rows']} | {r['errors']} | "
            f"{r['gate_unavailable']} | "
            f"{r['both_answered']} | {_pct(r['agreement'])} | {r['yy']} | {r['yn']} | "
            f"{r['ny']} | {r['nn']} | {lat} |"
        )
    days = list(by_day)
    lines += [
        "",
        "## Agreement by day (UTC)",
        "",
        "| day | gate component | shadow component | rows | shadow errors "
        "| both answered | agreement |",
        "|---|---|---|---|---|---|---|",
    ]
    for d in days:
        lines.append(
            f"| {d['day']} | {d['gate_component_id']} | {d['shadow_component_id']} | "
            f"{d['rows']} | {d['errors']} | {d['both_answered']} | {_pct(d['agreement'])} |"
        )
    if not days:
        lines.append("| — | — | — | 0 | 0 | 0 | — |")
    for key in order:
        r = summary[key]
        lines += [
            "",
            f"## Calibration by p_yes decile — {key}",
            "",
        ]
        if not any(c["rows"] for c in r["deciles"].values()):
            # W11-F148 — a chat shadow gives no p_yes; say so rather than
            # print ten rows of zeros that read as a dead decision model.
            lines.append("_No p_yes on these rows — the shadow component gave none "
                         "(a chat shadow), or nothing answered._")
            continue
        lines += [
            "| p_yes | rows | gate yes | gate-yes rate | share of all gate yes |",
            "|---|---|---|---|---|",
        ]
        for d in range(10):
            c = r["deciles"][d]
            lines.append(
                f"| {d / 10:.1f}–{(d + 1) / 10:.1f} | {c['rows']} | {c['gate_yes']} | "
                f"{_pct(c['gate_yes_rate'])} | {_pct(c['share_of_gate_yes'])} |"
            )
    return "\n".join(lines)


def resolve_dsn(env_file: str | None = None) -> str:
    """The live-database DSN, from the shared resolver (W11-F115).

    ``PostgresConfig.from_env()`` is what the host scripts that work use; this
    report used to assemble its own connect kwargs and drifted from it. An
    explicit ``env_file`` is loaded first (without overriding the shell), the
    same loader ``legba.data.config`` applies to the repo-root ``.env``.
    """
    from legba.data import config as data_config

    if env_file:
        if not os.path.isfile(env_file):
            raise SystemExit(f"--env-file {env_file!r}: no such file")
        data_config.load_dotenv(env_file, override=False)
    return data_config.PostgresConfig.from_env().dsn


async def _read(leg: str, since: datetime, until: datetime, env_file: str | None):
    import asyncpg

    conn = await asyncpg.connect(resolve_dsn(env_file))
    try:
        async with conn.transaction(readonly=True):
            await conn.execute("SET LOCAL statement_timeout = '120s'")
            per = await conn.fetch(_PER_ANALYST_SQL, leg, since, until)
            dec = await conn.fetch(_DECILE_SQL, leg, since, until)
            day = await conn.fetch(_BY_DAY_SQL, leg, since, until)
    finally:
        await conn.close()
    return [dict(r) for r in per], [dict(r) for r in dec], [dict(r) for r in day]


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--since", help="window start (default: --until minus 7 days)")
    ap.add_argument("--until", help="window end, exclusive (default: now)")
    ap.add_argument("--leg", default="bearing_shadow")
    ap.add_argument("--json-out")
    ap.add_argument(
        "--env-file",
        help="load this .env before resolving the DSN (default: the repo-root "
             ".env legba.data.config loads; the shell always wins)",
    )
    args = ap.parse_args(argv)

    until = _parse_when(args.until) if args.until else datetime.now(timezone.utc)
    since = _parse_when(args.since) if args.since else until - timedelta(days=7)
    if since >= until:
        print("--since must be before --until", file=sys.stderr)
        return 2
    per, dec, day = await _read(args.leg, since, until, args.env_file)
    summary = summarize(per, dec)
    by_day = summarize_by_day(day)
    print(render(summary, leg=args.leg, since=since, until=until, by_day=by_day))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"leg": args.leg, "window": [since.isoformat(), until.isoformat()],
                       "summary": summary, "by_day": by_day}, fh, indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
