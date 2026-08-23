#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""rust23_absence_catchset.py — derive the TRUE-FAIL arm of the RUST-2 replay.

WHY THIS EXISTS. The suppression replay
(``scripts/rust23_absence_replay.py``) measures the seven adjudicated
ABSENCE-rubric items, and every one of them is ``gt_fail=False``. It therefore
measures FALSE-FAIL SUPPRESSION and nothing else: it cannot see the failure mode
a rubric written to stop over-failing is most likely to introduce. D5
disqualified its own best-scoring cell for exactly that (soft precision bought
with pass-side integrity), so an absence-rubric change with no catch-side
measurement is not deployable on this program's own precedent.

THE SOURCE. ``planning/PROOF_ROUND_2026-08-20/`` lane C-D graded the product's
insufficiency / absence claims against Tier-1/2 world evidence and returned
``evidence-existed`` on 29 of 51 graded atoms (SCORES.md §C-D). Those are true
fails with an adjudicated, ARCHIVED refuting span — the only population in the
record that can answer "does v4 still catch a negative the evidence refutes?".

WHAT RECONSTRUCTION MEANS HERE, STATED PLAINLY. It is NOT the same construction
as the suppression set and must not be reported as if it were. The suppression
items are real preserved judge payloads: the desk's own body and its own
citations, exactly as the judge saw them. A C-D item is different by
construction — the whole PROOF finding is that the refuting evidence was NOT in
the desk's slice (Class 3: it aged out of every 72h window before T0). Replaying
those with their original citations would show BOTH arms answering "supported",
correctly, and would measure nothing about either rubric.

So this builds the COUNTERFACTUAL the catch question actually asks: the desk's
absence claim, with an evidence map that CONTAINS the grader's archived refuting
span as a cited signal. It answers "if this evidence had been collected, does
the rubric still fail the claim?" — a question about the RUBRIC, which is what
is being changed. It does not and cannot answer "would production have caught
it", which is a collection-and-continuity question the PROOF round already
answered (no).

ADMISSION CRITERIA — an atom is replayable only if all four hold. Everything
else is SKIPPED and COUNTED with its reason; nothing is hand-written to make an
item fit.

  1. a quotable PROSE absence claim (the grader recorded the product's own
     words, not just a dimension name);
  2. ``verify._claim_kind(claim) == 'absence'`` — the production router sends
     it to the rubric under test. An atom that routes elsewhere cannot measure
     this rubric no matter how true the grader's finding is;
  3. a decisive refuting SPAN, and
  4. an ARCHIVE file that exists and in which that span RESOLVES, checked with
     the round's own ``span_checker.span_resolves`` — byte-for-byte the
     mechanics the graders ran in-loop. An unresolvable span is not a decisive
     verdict ("no span, no decisive verdict", Annex C).

The dominant expected skip is (1)/(2): most C-D atoms target a SCORECARD band
(``insufficient-evidence``, reason below-floor), which the deterministic floor
produces and the judge never grades.

Writes a fixture the replay harness consumes. Read-only over ``planning/``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

import legba  # noqa: E402

_expected = os.path.join(REPO, "src", "legba")
if not os.path.abspath(legba.__file__).startswith(_expected + os.sep):
    raise SystemExit(
        f"IMPORT PROVENANCE FAILURE: 'legba' resolved to {legba.__file__}\n"
        f"expected it under {_expected}; re-run with PYTHONPATH={_expected[:-6]}"
    )

from legba.data.provenance import verify  # noqa: E402

#: Verdicts that mark a TRUE FAIL — the product denied something the record
#: carried. One grader emitted a compound string; ``startswith`` catches it and
#: the item is then admitted or skipped on the same four criteria as the rest.
TRUE_FAIL_PREFIX = "evidence-existed"

#: Where a grader may have put the claim prose, in preference order.
_CLAIM_KEYS = ("claim", "atom", "target", "text", "key")
#: Where a grader may have put the decisive span, in preference order.
_SPAN_KEYS = ("decisive_span", "span", "second_decisive_span", "supporting_span")
#: Where a grader may have put the archive path, in preference order.
_ARCHIVE_KEYS = ("span_archive", "archive", "second_archive")

#: The product's own prose, as graders quoted it inside a longer ``target``
#: string: '...' or "..." runs long enough to be a sentence rather than a label.
_QUOTED_PROSE_RE = re.compile(r"['‘’\"“”]([^'‘’\"“”]{25,400})['‘’\"“”]")


def cd_lane(verdict: dict):
    """The C-D block under whichever spelling this grader used."""
    for key in verdict:
        if key.replace("-", "_").lower() == "c_d":
            return verdict[key]
    return None


def cd_atoms(block) -> list[dict]:
    """Flatten a grader's C-D block into atoms. Two shapes are in the record: a
    bare list, and a dict of named sub-lists (``hedges`` / ``insufficiency``)."""
    if isinstance(block, list):
        return [a for a in block if isinstance(a, dict)]
    if isinstance(block, dict):
        out: list[dict] = []
        for name, value in block.items():
            if isinstance(value, list):
                out += [dict(a, _sub=name) for a in value if isinstance(a, dict)]
        return out
    return []


def resolve_archive(grades_dir: str, lane: str, raw: str) -> str:
    """The round's OWN resolver (c5_span_sweep.resolve_archive), reused so this
    script and the round agree about where a span lives."""
    path = raw.strip()
    if path.startswith("grades/"):
        path = path[len("grades/"):]
    if not path.startswith("archive/"):
        path = f"archive/{lane}/{path}"
    return os.path.join(grades_dir, path)


#: Typographic characters the PRODUCT emits and a GRADER types as ASCII. The
#: composition renderer uses non-breaking hyphens (U+2011) and narrow no-break
#: spaces (U+202F); a grader quoting "energy-market reporting" types a plain
#: hyphen. Folding them is the difference between locating the product's own
#: sentence and silently skipping the item as unreconstructable — three atoms
#: were lost to exactly this before it was added.
_DASH_FOLD = {ord(c): "-" for c in "‐‑‒–—―−"}
_SPACE_FOLD = {ord(c): " " for c in "    ⁠"}


def norm(text: str) -> str:
    folded = (text or "").translate(_DASH_FOLD).translate(_SPACE_FOLD)
    return re.sub(r"\s+", " ", folded).strip().casefold()


def span_resolves(page_text: str, span: str) -> bool:
    """``span_checker.span_resolves``, verbatim. An empty span NEVER resolves."""
    needle = norm(span)
    return bool(needle) and needle in norm(page_text)


def locators(atom: dict) -> list[list[str]]:
    """The grader's own quoted fragments of the PRODUCT's prose, as locators.

    A grader writing ``target: "energy_security (CB-d642df7d, reason
    below-floor; composition: 'energy security ... had no read this cycle')"``
    has quoted the product's words inside a bookkeeping string, with an ellipsis
    standing in for text it elided. That fragment is NOT a claim — feeding it to
    a judge would be grading the grader's abbreviation. It is a LOCATOR: split
    it on the ellipsis and every part must appear in the product's real sentence,
    which is then what gets replayed. That is what keeps this a reconstruction of
    the product's own words rather than a paraphrase of them.
    """
    out: list[list[str]] = []
    for key in _CLAIM_KEYS:
        value = atom.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        for match in _QUOTED_PROSE_RE.finditer(value):
            parts = [
                norm(p) for p in re.split(r"\s*(?:\.\.\.|…)\s*", match.group(1))
                if len(norm(p)) >= 8
            ]
            if parts:
                out.append(parts)
    return out


def product_bodies(round_dir: str, lane: str) -> dict[str, str]:
    """``{where: body}`` — the raw published texts the spans were segmented from,
    so the replay can hand the judge the claim's OWN context rather than a
    reconstructed sentence floating alone."""
    country = lane.split("_")[0]
    path = os.path.join(round_dir, "scoring", f"stage2_{country}.json")
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        stage2 = json.load(fh)
    out: dict[str, str] = {}
    composition = stage2.get("composition") or {}
    if isinstance(composition, dict) and isinstance(composition.get("body"), str):
        out["composition"] = composition["body"]
    heads = [h.get("body") for h in (stage2.get("unit_heads_context") or [])
             if isinstance(h, dict) and isinstance(h.get("body"), str)]
    if heads:
        out["unit_head"] = "\n\n".join(heads)
    return out


def product_spans(round_dir: str, lane: str) -> list[tuple[str, str]]:
    """``[(where, span)]`` — every claim span the PRODUCT actually published for
    this country, segmented by the pipeline's own splitter.

    Sources are the two the graders were shown: the composition body (the graded
    artifact) and the seven unit-head bodies carried as context. Segmenting with
    ``verify._segment_claims`` rather than a regex means the span admitted here
    is byte-identical to the span the judge would have been handed.
    """
    country = lane.split("_")[0]
    path = os.path.join(round_dir, "scoring", f"stage2_{country}.json")
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        stage2 = json.load(fh)
    out: list[tuple[str, str]] = []
    composition = stage2.get("composition") or {}
    body = composition.get("body") if isinstance(composition, dict) else None
    if isinstance(body, str):
        out += [("composition", s) for s in verify._segment_claims(body)]
    for head in stage2.get("unit_heads_context") or []:
        head_body = head.get("body") if isinstance(head, dict) else None
        if isinstance(head_body, str):
            out += [("unit_head", s) for s in verify._segment_claims(head_body)]
    return out


def first(atom: dict, keys) -> tuple[str, str]:
    for key in keys:
        value = atom.get(key)
        if isinstance(value, str) and value.strip():
            return key, value.strip()
    return "", ""


def build(round_dir: str) -> tuple[list[dict], list[dict]]:
    grades = os.path.join(round_dir, "grades")
    admitted: list[dict] = []
    skipped: list[dict] = []
    spans_by_lane: dict[str, list[tuple[str, str]]] = {}
    bodies_by_lane: dict[str, dict[str, str]] = {}
    for name in sorted(os.listdir(grades)):
        if not (name.startswith("verdict_") and name.endswith(".json")):
            continue
        lane = name[len("verdict_"):-len(".json")]
        with open(os.path.join(grades, name)) as fh:
            verdict_doc = json.load(fh)
        for n, atom in enumerate(cd_atoms(cd_lane(verdict_doc)), start=1):
            if not str(atom.get("verdict", "")).startswith(TRUE_FAIL_PREFIX):
                continue
            item_id = f"{lane}-CD{n}"
            note = {"item_id": item_id, "lane": lane, "atom": atom}

            # (1)+(2) locate the grader's quote inside the PRODUCT's own text and
            # take the segmented span, then require the production router to send
            # that span to the rubric under test.
            fragments = locators(atom)
            if not fragments:
                skipped.append({**note, "skip": "no_quoted_product_prose",
                                "detail": "the grader recorded a scorecard "
                                          "dimension/item id only — a band the "
                                          "floor produces, never a prose claim "
                                          "any rubric grades"})
                continue
            bodies_by_lane.setdefault(lane, product_bodies(round_dir, lane))
            claim, where = "", ""
            for source, span in spans_by_lane.setdefault(
                lane, product_spans(round_dir, lane)
            ):
                low = norm(span)
                if not any(all(p in low for p in parts) for parts in fragments):
                    continue
                if verify._claim_kind(span) != verify.CLAIM_KIND_ABSENCE:
                    continue
                if not verify._is_judgeable_claim(span):
                    continue
                claim, where = span, source
                break
            if not claim:
                skipped.append({**note, "skip": "not_a_prose_absence_claim",
                                "detail": "the grader's quote located no "
                                          "published span that the production "
                                          "router sends to the absence rubric"})
                continue

            # (3) a decisive refuting span.
            span_key, span = first(atom, _SPAN_KEYS)
            if not span:
                skipped.append({**note, "skip": "no_decisive_span",
                                "detail": "grader recorded the finding in prose "
                                          "only; no span, no decisive verdict"})
                continue

            # (4) an archive the span actually resolves in.
            arch_key, archive_raw = first(atom, _ARCHIVE_KEYS)
            if not archive_raw:
                skipped.append({**note, "skip": "no_archive",
                                "detail": "span recorded without an archived page"})
                continue
            path = resolve_archive(grades, lane, archive_raw)
            if not os.path.exists(path):
                skipped.append({**note, "skip": "archive_missing",
                                "detail": f"{archive_raw} -> {path}"})
                continue
            with open(path, errors="replace") as fh:
                page = fh.read()
            if not span_resolves(page, span):
                skipped.append({**note, "skip": "span_does_not_resolve",
                                "detail": f"span not verbatim in {archive_raw}"})
                continue

            admitted.append({
                "item_id": item_id,
                "lane": lane,
                "claim_text": claim,
                "claim_kind": verify._claim_kind(claim),
                "claim_source": where,
                # The product's REAL body the claim was located in, replayed as
                # the finding body so the span keeps its own context and is
                # segmented exactly as production segmented it.
                "body": bodies_by_lane[lane][where],
                # The grader's archived page, carried whole; the harness windows
                # it around the span so production's source cap cannot cut the
                # very evidence the item exists to test.
                "page_text": page,
                # Deterministic, so a re-derivation produces the identical
                # fixture and the replay stays reproducible.
                "signal_id": str(uuid.uuid5(uuid.NAMESPACE_URL, item_id)),
                "refuting_span": span,
                "span_key": span_key,
                "archive": archive_raw,
                "archive_path": path,
                "source_url": atom.get("source_url") or atom.get("source_that_existed") or "",
                "source_tier": atom.get("tier") or atom.get("source_tier") or "",
                "outlet": atom.get("outlet") or "",
                "publish_date": atom.get("publish_date") or "",
                "grader_why": atom.get("why") or atom.get("note") or "",
                # The adjudicated truth for this arm: the record says the
                # product's negative was WRONG, so the judge SHOULD fail it.
                "gt_fail": True,
            })
    return admitted, skipped


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", default="/usr/local/deployments/active/legba/"
                                          "planning/PROOF_ROUND_2026-08-20")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    admitted, skipped = build(args.round)
    print(f"C-D true-fail atoms admitted: {len(admitted)}")
    for item in admitted:
        print(f"  {item['item_id']:10s} tier={item['source_tier'] or '?'} "
              f"{item['claim_text'][:78]!r}")
    print(f"\nSKIPPED: {len(skipped)} (every one counted, none hand-fixed)")
    by_reason: dict[str, int] = {}
    for item in skipped:
        by_reason[item["skip"]] = by_reason.get(item["skip"], 0) + 1
    for reason, count in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        print(f"  {count:3d}  {reason}")
    with open(args.out, "w") as fh:
        json.dump({"admitted": admitted, "skipped": skipped}, fh, indent=1)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
