# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The harness host: run a round spec from the command line with synthetic
or scripted seats, writing receipts, phase records and the run record to a
folder (plan P4 §3.1, lane G-2).

::

    python -m legba.data.rounds.harness SPEC --out DIR \\
        --input subject=s1 --input question="Will it reopen?" --input as_of=2026-01-01 \\
        [--input cards=drafts/a,drafts/b]  [--script seats.yaml]  [--run-id ID]

* No model calls: seats are :class:`~.seats.SyntheticSeats` (deterministic,
  gate-clean) unless a ``--script`` gives replies for some seats (the rest
  stay synthetic). Pure code functions run for real.
* Resume: run the same command on the same ``--out`` folder after an
  interruption; finished steps are not re-run (their receipts are reused).
* Writes ``DIR/receipts/*.json``, ``DIR/phases/*.json``, ``DIR/run.json`` and
  ``DIR/record.json``; prints the totals beside the validator's worst case.

No registry, no database, no runtime: this host touches only its folder.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from .calibration import CalibrationBook
from .interpreter import Interpreter
from .seats import Dispatch, ScriptedSeats, SeatFn, SyntheticSeats
from .spec import SpecRefused, load_spec
from .store import FileStore


def parse_inputs(pairs: list[str], spec_inputs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"--input expects name=value, got {pair!r}")
        decl = spec_inputs.get(name)
        if decl is not None and decl.type == "id_list":
            out[name] = [v for v in value.split(",") if v]
        else:
            out[name] = value
    return out


def run_file(spec_path: str | Path, out: str | Path, inputs: dict[str, Any], *,
             script: str | Path | None = None, run_id: str | None = None,
             seats: SeatFn | None = None,
             calibration: CalibrationBook | None = None) -> dict[str, Any]:
    spec, refusals = load_spec(spec_path)
    if spec is None:
        raise SpecRefused(refusals)
    store = FileStore(out)
    header = store.get_run()
    rid = run_id or (header or {}).get("run_id") or uuid.uuid4().hex[:12]
    provider: SeatFn = seats or SyntheticSeats()
    if script is not None:
        provider = ScriptedSeats.from_file(script, fallback=provider)
    record = Interpreter(spec, store, Dispatch(provider), inputs=inputs, run_id=rid,
                         calibration=calibration).run()
    store.write_record(record)
    return record


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m legba.data.rounds.harness",
                                 description="Run a round spec with synthetic or scripted seats.")
    ap.add_argument("spec")
    ap.add_argument("--out", required=True, help="run folder (created; reused to resume)")
    ap.add_argument("--input", action="append", default=[], help="name=value (id lists: a,b,c)")
    ap.add_argument("--script", help="YAML of scripted seat replies")
    ap.add_argument("--run-id")
    args = ap.parse_args(argv)
    spec, refusals = load_spec(args.spec)
    if spec is None:
        for r in refusals:
            print(f"refused: {r}", file=sys.stderr)
        return 2
    try:
        record = run_file(args.spec, args.out, parse_inputs(args.input, dict(spec.inputs)),
                          script=args.script, run_id=args.run_id)
    except SpecRefused as exc:
        for r in exc.refusals:
            print(f"refused: {r}", file=sys.stderr)
        return 2
    t, w = record["totals"], record["worst_case"] or {}
    print(json.dumps({"run_id": record["run"]["run_id"], "state": record["run"]["state"],
                      "stop_reason": record["run"]["stop_reason"], "totals": t,
                      "worst_case": w, "record": str(Path(args.out) / "record.json")},
                     sort_keys=True))
    return 0 if record["run"]["state"] == "ended" else 1


if __name__ == "__main__":
    sys.exit(main())
