# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The run record (plan P4 §3.2): everything a round run did, kept.

Which seat ran at which phase instance under which spec version and content
hash, what it was handed (ids and seals), what it wrote and its seal, its
card's hash, plane, family and thinking choice, gate results, timings; every
phase's open/close and close reason; every tally with its dissent; absent
seats; per-seat gate rates; totals against the worst case. Nothing is
dropped: losing positions, failed attempts and raw replies included.
"""
from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .interpreter import Interpreter


def _dissent(receipts: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in receipts:
        if r.status != "sealed" or not isinstance(r.payload, dict):
            continue
        if r.row_type == "tally/1":
            for d in r.payload.get("dissent") or ():
                out.append({"kind": "vote", "phase": r.seat, **d})
            for c in r.payload.get("candidates") or ():
                if c.get("outcome") in ("falls", "tie"):
                    out.append({"kind": "candidate", "phase": r.seat,
                                "candidate": c.get("candidate"), "outcome": c.get("outcome")})
        for s in r.payload.get("dissent") or ():
            if isinstance(s, dict) and "text" in s:
                out.append({"kind": "sentence", "seat": r.seat, "key": s.get("key"),
                            "text": s.get("text")})
    return out


def _gate_rates(receipts: list[Any]) -> dict[str, dict[str, Any]]:
    rates: dict[str, dict[str, Any]] = {}
    for r in receipts:
        if r.attempt < 0 or not r.gates:
            continue
        row = rates.setdefault(r.seat, {"attempts": 0, "passed": 0, "failed_by_gate": Counter()})
        row["attempts"] += 1
        if all(g.get("passed") for g in r.gates):
            row["passed"] += 1
        for g in r.gates:
            if not g.get("passed"):
                row["failed_by_gate"][g.get("gate")] += 1
    return {k: {**v, "failed_by_gate": dict(v["failed_by_gate"]),
                "pass_rate": round(v["passed"] / v["attempts"], 4) if v["attempts"] else None}
            for k, v in sorted(rates.items())}


def build_record(it: Interpreter, header: dict[str, Any], charged: float) -> dict[str, Any]:
    receipts = it.store.receipts()
    phases = it.store.phases()
    worst = it.report.worst.as_dict() if it.report is not None and it.report.worst else None
    tallies = [{"phase": r.seat, "path": r.path, **(r.payload or {})}
               for r in receipts if r.row_type == "tally/1" and r.status == "sealed"]
    absent = [{"seat": r.seat, "path": r.path, "reason": r.failure, "candidate": r.candidate}
              for r in receipts if r.status == "absent"]
    return {
        "run": header,
        "spec": {"id": it.spec.id, "version": it.spec.version, "sha256": it.spec.sha256,
                 "source": it.spec.source, "publishes": it.spec.publishes},
        "totals": {"model_calls": it.calls, "code_calls": it.code_calls,
                   "charged_s": round(charged, 6), "peak_parallel": it.peak,
                   "steps": len(receipts), "absent": len(absent)},
        "worst_case": worst,
        "budgets": it.spec.budgets.model_dump(),
        "steps": [r.to_json() for r in receipts],
        "phases": [p.to_json() for p in phases],
        "tallies": tallies,
        "dissent": _dissent(receipts),
        "absent": absent,
        "gate_rates": _gate_rates(receipts),
        "warnings": list(it.report.warnings) if it.report is not None else [],
    }
