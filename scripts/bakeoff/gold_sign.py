#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The operator's signature on ONE gold row (design v1.1 §4.4: "the operator
verifies and signs every gold row") — scripted so every signature has one shape.

Signs ``<D>/gold.json`` (the operator's verified copy of ``gold_draft.json``:
``cp $D/gold_draft.json $D/gold.json``, then verify and edit each row — every
atom's real source — before signing it). The row is checked first:

  * non-INSUFFICIENT, gold met: 2–6 gold atoms, each with text and a source
    that is not the drafter's ``unverified — operator to supply`` marker;
    0–3 error atoms; a prior-state note. ``--not-met`` signs the row as
    ``met: false`` instead (the gold source could not be met by T+7 d — the
    question is VOID, §3.5) and needs none of that.
  * INSUFFICIENT: the same atoms plus a non-empty ``absence_proof``;
    ``--authoritative-answer-exists`` signs it ``true`` (the question is VOID).

Signing writes ``signed: true``, ``signed_by``, ``signed_at`` (UTC) and
``signed_row_sha256`` — the sha256 of the row's content fields
(``gold_draft.ROW_CONTENT``), so a later edit to a signed row is detectable.
A signed row is never re-signed without ``--force``.

USAGE
    python3 scripts/bakeoff/gold_sign.py --run-dir <D> --qid HE01 --by "<name>" [--not-met]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import gold_draft  # noqa: E402


class SignRefused(RuntimeError):
    pass


def row_sha256(row: dict[str, Any]) -> str:
    content = {k: row.get(k) for k in gold_draft.ROW_CONTENT}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def sign_problems(row: dict[str, Any], *, met: bool, authoritative: bool) -> list[str]:
    """Why this row may not be signed as asked (empty = it may)."""
    insufficient = row.get("class") == "INSUFFICIENT"
    if (not insufficient and not met) or (insufficient and authoritative):
        return []  # a void row needs no gold, only the operator's word
    p: list[str] = []
    ga, ea = row.get("gold_atoms") or [], row.get("error_atoms") or []
    lo, hi = gold_draft.GOLD_ATOMS
    if not lo <= len(ga) <= hi:
        p.append(f"{len(ga)} gold atoms (need {lo}-{hi})")
    elo, ehi = gold_draft.ERROR_ATOMS
    if not elo <= len(ea) <= ehi:
        p.append(f"{len(ea)} error atoms (need {elo}-{ehi})")
    for a in ga:
        if not isinstance(a, dict) or not str(a.get("text") or "").strip():
            p.append(f"gold atom {a!r} has no text")
            continue
        src = str(a.get("source") or "").strip()
        if not src or src == gold_draft.UNVERIFIED:
            p.append(f"gold atom {a.get('id')}: no verified source")
    if not str(row.get("prior_state_note") or "").strip():
        p.append("no prior_state_note")
    if insufficient and not str(row.get("absence_proof") or "").strip():
        p.append("no absence_proof")
    return p


def sign(row: dict[str, Any], *, by: str, met: bool = True, authoritative: bool = False,
         force: bool = False, at: str | None = None) -> dict[str, Any]:
    if not by.strip():
        raise SignRefused("--by must name the operator")
    if row.get("signed_by") and not force:
        raise SignRefused(f"{row.get('question_id')} is already signed by {row['signed_by']} "
                          f"at {row.get('signed_at')} (--force to re-sign)")
    problems = sign_problems(row, met=met, authoritative=authoritative)
    if problems:
        raise SignRefused(f"{row.get('question_id')}: " + "; ".join(problems))
    out = dict(row)
    if out.get("class") == "INSUFFICIENT":
        out["authoritative_answer_exists"] = authoritative
    else:
        out["met"] = met
    out.update(signed=True, signed_by=by.strip(), signed_at=at or common.utcnow().isoformat())
    out["signed_row_sha256"] = row_sha256(out)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="the operator's signature on one gold row")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--qid", required=True)
    ap.add_argument("--by", required=True, help="the operator's name")
    ap.add_argument("--gold", help="default <run-dir>/gold.json")
    ap.add_argument("--not-met", action="store_true", help="the gold source could not be met (void)")
    ap.add_argument("--authoritative-answer-exists", action="store_true",
                    help="INSUFFICIENT: an authoritative answer exists (void)")
    ap.add_argument("--force", action="store_true", help="re-sign a signed row")
    a = ap.parse_args(argv)
    path = Path(a.gold) if a.gold else Path(a.run_dir) / "gold.json"
    if not path.is_file():
        raise SignRefused(f"{path} does not exist — copy gold_draft.json to gold.json, verify and edit "
                          "each row, then sign it")
    doc = json.loads(path.read_text(encoding="utf-8"))
    row = (doc.get("questions") or {}).get(a.qid)
    if row is None:
        raise SignRefused(f"{path} has no row {a.qid}")
    doc["questions"][a.qid] = sign(row, by=a.by, met=not a.not_met,
                                   authoritative=a.authoritative_answer_exists, force=a.force)
    common.write_json(path, doc)
    r = doc["questions"][a.qid]
    print(f"{a.qid} signed by {r['signed_by']} at {r['signed_at']} (row sha256 {r['signed_row_sha256'][:12]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
