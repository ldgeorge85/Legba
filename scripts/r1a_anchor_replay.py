#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""r1a_anchor_replay.py — R1-a's bar over the live substrate, READ-ONLY.

WHAT THIS IS. The amended anchor bar (``legba.data._frame_anchor``) replayed
over the five desks ``planning/R1_FRAME_REPAIR_AMENDMENT_2026-09-06.md`` §2.2
swept — IL, SA, TR, UA, JP — against the eight predictions §5 pre-registered.
It mints nothing, writes nothing and changes nothing; it exists so that P-1…P-8
have a producer BEFORE R1-b binds the bar to ``situation_clustering``.

WHY IT SHELLS OUT TO ``psql`` RATHER THAN OPENING A POOL. Every read here is a
``COPY (SELECT …) TO STDOUT``, run through ``docker compose exec -T postgres``.
That is not a convenience: it makes "this script cannot write to the live
database" a property a reader can check by looking, rather than a claim the
script makes about itself. ``--dry-run`` (the default, and the only mode) is
therefore a statement about the whole program, not a branch inside it.

THE FOLD IS THE SHIPPED ONE. Candidates are gated by the shipped
``_entity_canon.canonicalize_entity`` and the shipped ``_polity_match``
home exclusion; the bar is ``_frame_anchor.anchors_for`` with no local copy of
any clause. If this script and the module ever disagree it is because the
module changed, which is the only way a replay is worth reading.

TWO SWEEPS, because the amendment is ambiguous in exactly one place. §2.3 keeps
``min_anchor_days`` at 5 and re-bases it onto authoring days; §2.1's predicate
line and the Q-A′ fold both omit it. The module applies it (a declared knob
that gates nothing is dead config). So the replay reports the anchor sets at
``min_anchor_days=5`` AND at ``0`` — the latter being the configuration §2.2's
published numbers were actually computed in — and the report states the cost.

Usage::

    PYTHONPATH=src python3 scripts/r1a_anchor_replay.py --dry-run
    PYTHONPATH=src python3 scripts/r1a_anchor_replay.py --dry-run --json out.json
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from typing import Any

from legba.data._frame_anchor import (
    FrameAnchorConfig,
    PolitySurfaceIndex,
    anchors_for,
    candidate_polities,
    naming_findings,
)
from legba.data._polity_match import home_prose

#: The amendment §2.2 sweep set. IL and JP are the two cells that decide the
#: design (IL/Palestine must anchor, JP/North Korea must not); SA, TR and UA
#: are the desks whose named gaps §4.6 promised to close.
REPLAY_DESKS: tuple[str, ...] = (
    "country_watch_il",
    "country_g20_sa",
    "country_g20_tr",
    "country_watch_ua",
    "country_g20_jp",
)

COMPOSE_DIR = "/usr/local/deployments/active/legba"

#: The Q-A′ projection, verbatim in shape: one hop, no signal walk, no ``data``
#: column. ``situation_signature IS NOT NULL`` is what makes a finding a
#: CANDIDATE MEMBER of a frame, which is the population the mint scopes over.
_FINDINGS_SQL = """
COPY (
  SELECT target_id, analyst_id, id::text, title,
         left(coalesce(body, ''), {max_body}) AS body,
         produced_at
    FROM analyst_outputs
   WHERE kind = 'finding'
     AND situation_signature IS NOT NULL
     AND target_id = ANY(ARRAY[{desks}])
     AND produced_at > now() - interval '{days} days'
) TO STDOUT WITH (FORMAT csv, HEADER true)
"""

_DESK_GEO_SQL = """
COPY (
  SELECT td.descriptor_id,
         coalesce(string_agg(DISTINCT g.code, ',' ORDER BY g.code), '') AS iso2,
         coalesce(string_agg(DISTINCT ic.name, ',' ORDER BY ic.name), '') AS names
    FROM target_descriptors td
    LEFT JOIN LATERAL jsonb_array_elements_text(td.body->'scope'->'geo')
              AS g(code) ON TRUE
    LEFT JOIN iso_countries ic ON ic.iso2 = g.code
   WHERE td.is_head = TRUE
     AND coalesce(td.state, 'active') <> 'retired'
     AND (td.body->'scope'->'tags') ?| array['g20','watch']
   GROUP BY td.descriptor_id
) TO STDOUT WITH (FORMAT csv, HEADER true)
"""

_OPEN_FRAMES_SQL = """
COPY (
  SELECT target_id, count(*) AS open_frames,
         count(DISTINCT analyst_id) AS dimensions
    FROM situations
   WHERE superseded_by IS NULL
     AND (valid_until IS NULL OR valid_until > now())
     AND status <> 'closed'
     AND target_id = ANY(ARRAY[{desks}])
   GROUP BY target_id
) TO STDOUT WITH (FORMAT csv, HEADER true)
"""


def _psql(sql: str) -> list[dict[str, str]]:
    """One read-only COPY, parsed. Raises loudly on anything but success."""
    proc = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres",
         "psql", "-U", "legba", "-d", "legba", "-v", "ON_ERROR_STOP=1",
         "-Aqt", "-c", sql],
        cwd=COMPOSE_DIR, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"psql failed ({proc.returncode}):\n{proc.stderr}")
    return list(csv.DictReader(io.StringIO(proc.stdout)))


def _quoted(desks: tuple[str, ...]) -> str:
    return ", ".join("'" + d.replace("'", "''") + "'" for d in desks)


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def load(cfg: FrameAnchorConfig) -> dict[str, Any]:
    desks = _quoted(REPLAY_DESKS)
    geo = {r["descriptor_id"]: r for r in _psql(_DESK_GEO_SQL)}
    frames = {r["target_id"]: r for r in _psql(
        _OPEN_FRAMES_SQL.format(desks=desks))}
    rows = _psql(_FINDINGS_SQL.format(
        max_body=int(cfg.max_body_chars), desks=desks,
        days=int(cfg.window_days)))
    findings: list[dict[str, Any]] = []
    for r in rows:
        findings.append({
            "target_id": r["target_id"],
            "analyst_id": r["analyst_id"],
            "id": r["id"],
            "title": r["title"],
            "body": r["body"],
            "produced_at": _parse_ts(r["produced_at"]),
        })
    return {"geo": geo, "frames": frames, "findings": findings}


def replay(data: dict[str, Any], cfg: FrameAnchorConfig) -> dict[str, Any]:
    """Every (desk, dimension) scope through the shipped bar."""
    by_desk: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in data["findings"]:
        by_desk[row["target_id"]].append(row)

    out: dict[str, Any] = {"desks": {}, "seconds": 0.0}
    started = time.perf_counter()
    for desk in REPLAY_DESKS:
        desk_rows = by_desk.get(desk, [])
        meta = data["geo"].get(desk, {})
        iso2 = [c for c in (meta.get("iso2") or "").split(",") if c]
        names = [n for n in (meta.get("names") or "").split(",") if n]
        blob = home_prose(iso2, names)
        candidates = candidate_polities(
            home_blob=blob, class_gate=cfg.class_gate)
        by_dim: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in desk_rows:
            by_dim[row["analyst_id"]].append(row)
        dims: dict[str, list[dict[str, Any]]] = {}
        residue: dict[str, int] = {}
        for dimension, scope in sorted(by_dim.items()):
            anchors = anchors_for(
                scope, desk_rows=desk_rows, home_blob=blob, config=cfg,
                candidates=candidates,
            )
            dims[dimension] = [
                {"polity": a.polity, "token": a.token,
                 "evidence_mass": a.evidence_mass, "anchor_days": a.anchor_days,
                 "ubiquity": a.desk_ubiquity}
                for a in anchors
            ]
            # D-l's residue, counted rather than assumed: a member that names
            # NONE of its dimension's anchors routes to ``_domestic``, and a
            # dimension mints that key only if it has at least one. The
            # amendment's §2.5 claim is that the residue is live (IL keeps
            # 2-29 unanchored members on 8 of its 9 dimensions), so counting
            # it is the honest projection and "one per dimension" is only an
            # upper bound.
            if anchors:
                index = PolitySurfaceIndex([a.polity for a in anchors])
                named = naming_findings(
                    scope, index, max_body_chars=cfg.max_body_chars)
                routed = {f for fs in named.values() for f in fs}
                residue[dimension] = len(scope) - len(routed)
            else:
                residue[dimension] = len(scope)
        anchored = sum(len(v) for v in dims.values())
        domestic = sum(1 for n in residue.values() if n > 0)
        out["desks"][desk] = {
            "findings": len(desk_rows),
            "dimensions": len(dims),
            "candidates": len(candidates),
            "anchored_keys": anchored,
            "domestic_keys": domestic,
            "domestic_members": residue,
            "projected_open": anchored + domestic,
            "open_today": int(data["frames"].get(desk, {}).get(
                "open_frames", 0) or 0),
            "by_dimension": dims,
        }
    out["seconds"] = round(time.perf_counter() - started, 3)
    return out


def _polity_dims(desk_row: dict[str, Any], polity: str) -> int:
    return sum(
        1 for anchors in desk_row["by_dimension"].values()
        if any(a["polity"] == polity for a in anchors)
    )


def predictions(result: dict[str, Any]) -> list[dict[str, Any]]:
    """P-1…P-5, P-8 scored. P-6 is a unit test; P-7 is an ops observation."""
    d = result["desks"]
    il = d["country_watch_il"]
    jp = d["country_g20_jp"]
    projected = [row["projected_open"] for row in d.values()]
    mean = sum(projected) / len(projected) if projected else 0.0
    us_keys = sorted(
        f"{desk}:{dim}"
        for desk, row in d.items()
        for dim, anchors in row["by_dimension"].items()
        if any(a["polity"] == "United States" for a in anchors)
    )
    residue = sorted(
        desk for desk, row in d.items() if row["domestic_keys"] < 1)
    checks = [
        {"id": "P-1", "claim": "IL anchors Palestine on >=3 of its dimensions",
         "measured": f"{_polity_dims(il, 'Palestine')} / {il['dimensions']}",
         "pass": _polity_dims(il, "Palestine") >= 3},
        {"id": "P-2", "claim": "JP mints zero North Korea keys",
         "measured": f"{_polity_dims(jp, 'North Korea')} / {jp['dimensions']}",
         "pass": _polity_dims(jp, "North Korea") == 0},
        {"id": "P-3", "claim": "no United States key on any replay desk",
         "measured": ", ".join(us_keys) or "none",
         "pass": not us_keys},
        {"id": "P-4", "claim": "open frames <=32 per desk, 5-desk mean <=20",
         "measured": f"max {max(projected, default=0)}, mean {mean:.1f}",
         "pass": max(projected, default=0) <= 32 and mean <= 20},
        {"id": "P-5", "claim": ">=1 non-empty _domestic residue per desk",
         "measured": "every desk" if not residue else ", ".join(residue),
         "pass": not residue},
        {"id": "P-8", "claim": "IL/Iran anchors 0 dimensions",
         "measured": f"{_polity_dims(il, 'Iran')} / {il['dimensions']}",
         "pass": _polity_dims(il, "Iran") == 0},
    ]
    return checks


def batched_vs_naive(data: dict[str, Any], cfg: FrameAnchorConfig) -> dict[str, Any]:
    """P-6 on the LIVE rows: the batched matcher against a nested loop.

    The unit test pins this on a >=200-row fixture; here it is measured where
    it matters, over the whole replay corpus, and the wall-clock of both paths
    is what amendment §2.6's 15x claim is about.
    """
    from legba.data._polity_match import represented_by
    from legba.data._frame_anchor import finding_prose

    rows = data["findings"]
    meta = data["geo"].get("country_watch_il", {})
    iso2 = [c for c in (meta.get("iso2") or "").split(",") if c]
    names = [n for n in (meta.get("names") or "").split(",") if n]
    candidates = candidate_polities(
        home_blob=home_prose(iso2, names), class_gate=cfg.class_gate)
    index = PolitySurfaceIndex(candidates)

    t0 = time.perf_counter()
    fast = naming_findings(rows, index, max_body_chars=cfg.max_body_chars)
    fast_s = time.perf_counter() - t0
    fast_pairs = {(p, f) for p, fs in fast.items() for f in fs}

    t0 = time.perf_counter()
    slow_pairs: set[tuple[str, str]] = set()
    for row in rows:
        prose = finding_prose(row, max_body_chars=cfg.max_body_chars)
        for polity in candidates:
            if represented_by(polity, prose) is not None:
                slow_pairs.add((polity, row["id"]))
    slow_s = time.perf_counter() - t0

    return {
        "rows": len(rows), "candidates": len(candidates),
        "batched_hits": len(fast_pairs), "naive_hits": len(slow_pairs),
        "identical": fast_pairs == slow_pairs,
        "batched_seconds": round(fast_s, 3),
        "naive_seconds": round(slow_s, 3),
        "speedup": round(slow_s / fast_s, 1) if fast_s else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="the only mode; every read is a SELECT")
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--min-anchor-days", type=int, default=None,
                    help="override the days clause (the replay runs 5 and 0)")
    ap.add_argument("--skip-naive", action="store_true",
                    help="skip the P-6 nested-loop control (it is slow)")
    args = ap.parse_args()

    base = FrameAnchorConfig()
    data = load(base)
    report: dict[str, Any] = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "desks": list(REPLAY_DESKS),
        "findings_read": len(data["findings"]),
        "config": {f: getattr(base, f) for f in base.__dataclass_fields__},
        "sweeps": {},
    }

    settings = ([args.min_anchor_days] if args.min_anchor_days is not None
                else [base.min_anchor_days, 0])
    for days in settings:
        cfg = FrameAnchorConfig(**{
            **{f: getattr(base, f) for f in base.__dataclass_fields__},
            "min_anchor_days": days,
        })
        result = replay(data, cfg)
        result["predictions"] = predictions(result)
        report["sweeps"][f"min_anchor_days={days}"] = result

    if not args.skip_naive:
        report["p6"] = batched_vs_naive(data, base)

    text = json.dumps(report, indent=2, sort_keys=False, default=str)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
