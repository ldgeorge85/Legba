#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Carried-state census, before and after level-shaped indicators (X4 LEVEL,
W11-F36) — READ-ONLY.

Over every bounded unit's head on every desk (the newest read per unit and
subject produced in the last ``--head-days``), three columns:

BEFORE — the carried-state rule as it stood at 765cc216: a status-bearing head
    with no ``triggered`` indicator whose newest ``triggered`` read is older
    than N of the unit's cadence windows is ``carried_unsupported``.

AFTER — the same heads under the shipped rule (``legba.data.carried_state``,
    the module's own ``LAST_EVIDENCE_SQL``): a level-shaped indicator
    ``observed_at_level`` on a citation re-evidences too. On live data this
    moves only once the converted descriptors are registered and their units
    have run — until then it prints the same count as BEFORE, and says why.

PROJECTION — for the carried heads of the units the profile marks
    ``structural``: would the head have cleared had its level indicators
    existed? Measured by a PROXY, not by the model: the head's own cited
    signals (citations carrying a ``signal_id`` — never its prior read or the
    register) are matched against each declared level indicator's vocabulary
    (:data:`PROXY_TERMS`, in this script and nowhere else). A match is an
    in-window report the unit could have cited at that level — an upper bound
    on what an honest unit can clear, never a forecast of what it will mark.

NEVER writes: every statement runs inside ``BEGIN READ ONLY`` with a 120 s
statement timeout, through ``docker exec <container> psql``::

  PYTHONPATH=src python3 scripts/census_carried_level.py \\
      [--container legba-postgres-1] [--head-days 7] [--json-out PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from legba.data import carried_state as cs  # noqa: E402
from legba.data import indicator_shape as shape  # noqa: E402
from legba.data.profile import load_profile  # noqa: E402

PROFILE = REPO / "profiles" / "geopolitics" / "profile.yaml"

#: The rule as it stood at the pin: only ``triggered`` re-evidenced.
BEFORE_JSONPATH = '$.data.indicators[*] ? (@.status == "triggered")'

#: The pin's LAST_EVIDENCE_SQL, the predicate spelled out (BEFORE).
BEFORE_LAST_EVIDENCE_SQL = cs.LAST_EVIDENCE_SQL.replace(
    shape.RE_EVIDENCE_JSONPATH, BEFORE_JSONPATH
)

HEADS_SQL = f"""
SELECT DISTINCT ON (o.analyst_id, o.target_id)
       o.analyst_id, o.target_id, o.id::text AS id, o.produced_at, o.severity,
       o.data -> 'tags' AS tags,
       jsonb_typeof(o.data -> 'data' -> 'indicators') = 'array' AS has_indicators,
       jsonb_path_exists(o.data, '{BEFORE_JSONPATH}') AS ev_before,
       jsonb_path_exists(o.data, '{shape.RE_EVIDENCE_JSONPATH}') AS ev_after,
       (SELECT jsonb_agg(jsonb_build_object(
                 'signal_id', c ->> 'signal_id', 'title', c ->> 'title',
                 'snippet', left(c ->> 'snippet', 800)))
          FROM jsonb_array_elements(
                 CASE WHEN jsonb_typeof(o.data -> 'data' -> 'citations') = 'array'
                      THEN o.data -> 'data' -> 'citations' ELSE '[]'::jsonb END) c
         WHERE c ->> 'signal_id' IS NOT NULL) AS signal_cites
  FROM public.analyst_outputs o
 WHERE o.kind = 'finding'
   AND o.analyst_id = ANY($1::text[])
   AND o.target_id IS NOT NULL
   AND o.produced_at > $2
 ORDER BY o.analyst_id, o.target_id, o.produced_at DESC, o.id DESC
"""

COOLDOWN_SQL = """
SELECT descriptor_id, (body -> 'cadence' ->> 'cooldown_seconds')::bigint AS cooldown_s
  FROM public.analyst_descriptors
 WHERE is_head AND descriptor_id = ANY($1::text[])
"""

#: The PROJECTION's vocabulary, per declared level indicator: a cited signal
#: matches when its title or snippet carries one term of EVERY group. Proxy
#: only — it lives here, never in the product.
_ENERGY = r"oil|gas|lng|diesel|crude|fuel|petrol|gasoline|coal|energy|power|electric|refin"
PROXY_TERMS: dict[str, tuple[str, ...]] = {
    "energy-trade-restriction-in-force": (
        r"sanction|embargo|price cap|export ban|import ban|blacklist", _ENERGY,
    ),
    "energy-route-disruption-in-force": (
        r"strait|hormuz|red sea|bab el|suez|pipeline|tanker|shipping|grid|chokepoint",
        r"attack|closed|closure|blockad|seiz|strike|disrupt|halt|shut",
    ),
    "energy-supply-stress-in-force": (
        r"shortage|rationing|load.?shedding|blackout|power cut|outage|reserve release"
        r"|emergency|price surge|surge|record high|multi-year high|spike|soar",
        _ENERGY,
    ),
    "forces-in-active-operations": (
        r"airstrike|air strike|offensive|clash|shelling|missile (?:strike|attack)"
        r"|drone (?:strike|attack)|raid|bombard|combat|fighting|killed|intercept",
        r"forces|troops|army|military|navy|naval|air force|soldier|irgc|militia",
    ),
    "heightened-posture-in-force": (
        r"martial law|mobiliz|mobilis|state of emergency|high alert|combat readiness"
        r"|readiness|reservist|emergency deployment|war footing|alert level",
    ),
}


def _literal(v: Any) -> str:
    if isinstance(v, datetime):
        return f"'{v.isoformat()}'::timestamptz"
    if isinstance(v, (list, tuple)):
        inner = ",".join("'" + str(x).replace("'", "''") + "'" for x in v)
        return f"ARRAY[{inner}]::text[]"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    raise TypeError(f"unbindable parameter {type(v).__name__}")


class PsqlReader:
    """``fetch`` over ``docker exec <container> psql``, READ ONLY, 120 s."""

    def __init__(self, container: str) -> None:
        self.container = container

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        for i in range(len(args), 0, -1):
            sql = sql.replace(f"${i}", _literal(args[i - 1]))
        script = (
            "BEGIN READ ONLY; SET LOCAL statement_timeout = '120s'; "
            f"SELECT row_to_json(q) FROM ({sql}) q; COMMIT;"
        )
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", self.container, "psql", "-U", "legba", "-d",
            os.environ.get("LEGBA_DATA_PG_DB", "legba"), "-qAtX", "-v", "ON_ERROR_STOP=1",
            "-c", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(err.decode("utf-8", "replace"))
        rows = []
        for line in out.decode("utf-8").splitlines():
            line = line.strip()
            if line.startswith("{"):
                row = json.loads(line)
                if isinstance(row.get("produced_at"), str):
                    row["produced_at"] = datetime.fromisoformat(row["produced_at"])
                rows.append(row)
        return rows


def level_declarations() -> dict[str, dict[str, shape.LevelSpec]]:
    """unit id -> its declared level indicators, read off the TREE descriptors."""
    out: dict[str, dict[str, shape.LevelSpec]] = {}
    for path in sorted((REPO / "descriptors").glob("analyst_*.yaml")):
        body = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        specs = shape.level_specs(((body.get("method") or {}).get("options")) or {})
        if specs:
            out[str(body["identity"]["id"])] = specs
    return out


def proxy_matches(cites: list[dict[str, Any]] | None, specs: dict[str, shape.LevelSpec]) -> dict[str, str]:
    """level id -> the title of the first cited signal its vocabulary matches."""
    hits: dict[str, str] = {}
    for ind_id in specs:
        groups = [re.compile(g, re.IGNORECASE) for g in PROXY_TERMS.get(ind_id, ())]
        if not groups:
            continue
        for c in cites or []:
            text = f"{c.get('title') or ''}\n{c.get('snippet') or ''}"
            if all(g.search(text) for g in groups):
                hits[ind_id] = str(c.get("title") or "")[:110]
                break
    return hits


async def run(reader: PsqlReader, head_days: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    profile = load_profile(PROFILE)
    units = [u.id for u in profile.rosters.units]
    structural = {u.id for u in profile.rosters.units if u.structural}
    declared = level_declarations()

    heads = await reader.fetch(HEADS_SQL, units, now - timedelta(days=head_days))
    cooldown = {
        r["descriptor_id"]: (int(r["cooldown_s"]) / 3600.0 if r.get("cooldown_s") else None)
        for r in await reader.fetch(COOLDOWN_SQL, units)
    }
    bearing = [h for h in heads if h.get("has_indicators") and cs.carries_status(h)]
    keys = [(h["analyst_id"], h["target_id"]) for h in bearing]
    lookback = now - timedelta(days=cs.EVIDENCE_LOOKBACK_DAYS)
    pairs = sorted(set(keys))

    async def last_evidence(sql: str) -> dict[tuple[str, str], dict[str, Any]]:
        rows = await reader.fetch(sql, [a for a, _ in pairs], [t for _, t in pairs], lookback)
        return {(r["analyst_id"], r["target_id"]): r for r in rows}

    ev_before = await last_evidence(BEFORE_LAST_EVIDENCE_SQL) if pairs else {}
    ev_after = await last_evidence(cs.LAST_EVIDENCE_SQL) if pairs else {}

    out_rows = []
    for h in bearing:
        key = (h["analyst_id"], h["target_id"])
        cad = cooldown.get(h["analyst_id"])
        before = cs.carry_verdict(
            {**h, "evidenced": bool(h["ev_before"])}, last_evidence=ev_before.get(key),
            cadence_h=cad, now=now,
        )
        after = cs.carry_verdict(
            {**h, "evidenced": bool(h["ev_after"])}, last_evidence=ev_after.get(key),
            cadence_h=cad, now=now,
        )
        proj = (
            proxy_matches(h.get("signal_cites"), declared.get(h["analyst_id"], {}))
            if before is not None and h["analyst_id"] in structural else {}
        )
        out_rows.append({
            "unit": h["analyst_id"], "target": h["target_id"], "status": cs.status_level(h),
            "carried_before": before is not None, "carried_after": after is not None,
            "projected_clear": bool(proj), "proxy_hits": proj,
            "last_evidence_age_h": (before or {}).get("last_evidence_age_h"),
        })
    return {
        "now": now.isoformat(), "head_days": head_days, "heads": len(heads),
        "status_bearing": len(bearing), "structural": sorted(structural),
        "declared": {u: sorted(s) for u, s in declared.items()},
        "windows": cs.carry_expiry_windows(), "rows": out_rows,
    }


def report(res: dict[str, Any]) -> None:
    rows = res["rows"]
    before = sum(r["carried_before"] for r in rows)
    after = sum(r["carried_after"] for r in rows)
    proj = sum(r["projected_clear"] for r in rows)
    print(f"# Carried-state census — {res['now'][:16]}Z; heads produced in the last "
          f"{res['head_days']} d; N = {res['windows']:g} windows\n")
    print(f"unit heads: {res['heads']} · status-bearing (indicators declared, status above "
          f"low): {res['status_bearing']}\n")
    print("| | carried_unsupported |")
    print("|---|---|")
    print(f"| BEFORE (triggered only — the rule at 765cc216) | {before} of {res['status_bearing']} |")
    print(f"| AFTER (shipped rule, live rows) | {after} of {res['status_bearing']} |")
    print(f"| PROJECTION (structural units: a cited in-window signal matches a level) | "
          f"{before - proj} of {res['status_bearing']} ({proj} would clear) |")
    print("\nAFTER moves only once a head carries an `observed_at_level` indicator, i.e. after the "
          "converted descriptors are registered and their units have run.\n")
    print("## By unit\n")
    print("| unit | structural | status-bearing | carried before | carried after | projected clear |")
    print("|---|---|---|---|---|---|")
    per: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        c = per[r["unit"]]
        c["bearing"] += 1
        c["before"] += r["carried_before"]
        c["after"] += r["carried_after"]
        c["proj"] += r["projected_clear"]
    for unit in sorted(per):
        c = per[unit]
        mark = "yes" if unit in res["structural"] else "—"
        p = c["proj"] if unit in res["structural"] else "—"
        print(f"| {unit} | {mark} | {c['bearing']} | {c['before']} | {c['after']} | {p} |")
    print("\n## Structural units' carried heads (projection detail)\n")
    print("| unit | desk | status | last evidence (h) | would clear on | first matching cited signal |")
    print("|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda r: (r["unit"], r["target"])):
        if not r["carried_before"] or r["unit"] not in res["structural"]:
            continue
        age = r["last_evidence_age_h"]
        age_txt = f"{age:.0f}" if isinstance(age, (int, float)) else "none in 90 d"
        hits = r["proxy_hits"]
        on = ", ".join(sorted(hits)) or "— (stays carried)"
        title = (next(iter(hits.values())) or "(untitled signal)").replace("|", "/") if hits else ""
        print(f"| {r['unit']} | {r['target']} | {r['status']} | {age_txt} | {on} | {title} |")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--container", default="legba-postgres-1")
    ap.add_argument("--head-days", type=int, default=7)
    ap.add_argument("--json-out")
    args = ap.parse_args()
    res = await run(PsqlReader(args.container), args.head_days)
    report(res)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
