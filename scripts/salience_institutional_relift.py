#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Re-apply the ``institutional_change`` salience lift to already-scored signals.

``signal_salience`` 0.2.0 runs the deterministic lift on every NEW verdict.
Signals scored before it carry the model's 0.30-0.45 for a cabinet change, an
impeachment push or a ratification, and the desks keep citing them for days.
This script reads those rows (scored, not degraded, no ``lift_rule`` stamp,
inside ``--window-hours``), runs :func:`salience_institutional.lift_salience`
on the scorer's own text and source class, and reports what would change: the
lifted count by sub-class and the share of scored signals over ``MASS_FLOOR``
before and after. No LLM call.

DRY-RUN BY DEFAULT: nothing is written unless ``--apply`` is passed, and then
only the lifted rows are rewritten (``signals.salience`` gains ``lift`` and
``lift_rule``; the magnitude rises to the act's anchor).

    python3 scripts/salience_institutional_relift.py --window-hours 96
    python3 scripts/salience_institutional_relift.py --window-hours 96 --apply
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from legba.data.analysts import salience_institutional as si  # noqa: E402
from legba.data.analysts import signal_salience as ss  # noqa: E402
from legba.data.taxon_families import SALIENCE_CLASS, bind_taxon  # noqa: E402

_SELECT = f"""
    SELECT s.id, s.payload, s.salience,
           d.body::jsonb->'scope'->>'source_class' AS source_class,
           d.body::jsonb->'config'->'classes' AS channel_classes
      FROM signals s
      LEFT JOIN source_descriptors d
        ON d.is_head = TRUE AND d.descriptor_id = s.source_id
     WHERE s.salience IS NOT NULL
       AND (s.salience->>'degraded') IS NULL
       AND (s.salience->>'lift_rule') IS NULL
       AND {ss._LIVE_SIGNALS}
       AND s.modality = 'text'
       AND s.fetched_at > now() - make_interval(hours => $1)
"""

_WRITE = "UPDATE signals SET salience = $2::jsonb, updated_at = now() WHERE id = $1::uuid"


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--window-hours", type=int, default=96)
    ap.add_argument("--apply", action="store_true", help="write the lifted rows")
    ap.add_argument("--show", type=int, default=20, help="lifted titles to print")
    args = ap.parse_args()

    bound = bind_taxon(SALIENCE_CLASS, si.INSTITUTIONAL_CHANGE)
    if not bound.bound:
        print("note: salience_class does not hold institutional_change here; "
              "the model's class is kept")
    conn = await _connect()
    try:
        rows = await conn.fetch(_SELECT, int(args.window_hours))
        before = after = 0
        lifted: list[tuple[str, dict, str]] = []
        for r in rows:
            sal = ss._jsonish(r["salience"])
            if not isinstance(sal, dict):
                continue
            mag = ss.magnitude_of(sal)
            if mag < 0.0:
                continue
            handle = ss._payload_channel_handle(r["payload"])
            sc = ss._channel_class_override(r["channel_classes"], handle) or r["source_class"]
            text = ss._signal_text(ss._jsonish(r["payload"]))
            new = si.lift_salience(sal, text, source_class=sc,
                                   bound_class=bound.value if bound.bound else None)
            before += mag > ss.MASS_FLOOR
            after += (new["magnitude"] if new else mag) > ss.MASS_FLOOR
            if new:
                new["lift_rule"] = si.INSTITUTIONAL_LIFT_VERSION
                lifted.append((str(r["id"]), new, text))
        n = max(1, len(rows))
        print(f"window {args.window_hours}h: {len(rows)} scored rows without lift_rule")
        print(f"over MASS_FLOOR: {before} ({before / n:.1%}) -> {after} ({after / n:.1%})")
        print(f"lifted: {len(lifted)} {dict(Counter(x[1]['lift']['subclass'] for x in lifted))}")
        for sid, new, text in lifted[: args.show]:
            print(f"  {new['lift']['from_magnitude']:.2f}->{new['magnitude']:.2f} "
                  f"{new['lift']['subclass']:20s} {sid[:8]} {text[:110]}")
        if not args.apply:
            print("dry run: nothing written (pass --apply to write the lifted rows)")
            return 0
        for sid, new, _ in lifted:
            await conn.execute(_WRITE, sid, json.dumps(new))
        print(f"wrote {len(lifted)} rows")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
