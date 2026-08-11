#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""r5_ablation_render.py — turn an R5 bundle into the operator's blind-grading pack.

Reads the JSON bundle from ``scripts/r5_ablation_harness.py`` and writes five
files: the blind pack the operator grades, the answer key they must not open
first, the grading protocol, an empty scoresheet, and the full evidence slices
for optional deep-dive.

THE BLINDING PROBLEM, AND WHAT THIS ACTUALLY DOES
-------------------------------------------------
Perfect blinding is not achievable between these two arms, and pretending
otherwise would be the worse error. The honest split is:

  INCIDENTAL differences — normalized away here, symmetrically, mechanically:
    * citation marker SYNTAX (``[[ref:2]]`` and full-width ``【2】`` both become
      ``[2]``; the core-plane models emit full-width brackets, which is why
      ``inline_target._normalize_citation_markers`` exists at all)
    * markdown heading levels, bullet glyphs, stray blank runs
    * output LENGTH — handled upstream in the harness via a per-desk envelope
      measured from that desk's own tower body

  INTRINSIC differences — deliberately LEFT IN, and disclosed:
    * the tower narrates in terms of its own analytic units ("the
      energy-security unit reports…") and names its situation register. That is
      a real property of the product — and one the reviews independently flag as
      a defect ("machine-internals leaking into analytic text"). Rewriting it
      would falsify the artifact AND conceal a known problem from its own
      evaluation.
    * citation INDEX MAGNITUDE. The tower's markers index 5-9 unit findings; the
      baseline's index 6-120 signals. So a ``[74]`` can only be the baseline.
      Stripping the markers to hide this would strip evidence, which is worse.

So the pack does not claim to be blind. It claims to be ORDER-RANDOMIZED and
PRESENTATION-NORMALIZED, and the protocol closes the gap a different way: the
operator records, per pair, which arm they BELIEVE is the tower, BEFORE scoring.
That converts an unmeasurable confound into a measured covariate — if the
guesses are accurate the scoring script says so and discounts accordingly,
rather than the whole exercise quietly resting on a blind that never held.

USAGE
-----
    python3 scripts/r5_ablation_render.py \
        --bundle /path/R5_BUNDLE.json --out-dir /path/scratch
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

#: Stable across re-renders of the same bundle, so a re-run does not silently
#: reshuffle a pack the operator already started grading.
_SHUFFLE_SALT = "R5-ablation-v1"

#: ``[[ref:12]]`` -> ``[12]``. The tower's composition layer emits this form; it
#: is marker SYNTAX, not evidence, so normalizing it removes a pure typography
#: tell without touching a single claim or dropping a single reference.
_REF_MARKER_RE = re.compile(r"\[\[ref:\s*(\d+)\s*\]\]")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BULLET_RE = re.compile(r"^(\s*)[•*•‣●]\s+", re.MULTILINE)
_BLANKS_RE = re.compile(r"\n{3,}")


def _normalize_arm_text(text: str) -> str:
    """Symmetric, mechanical presentation normalization. Claims are untouched."""
    from legba.data.analysts.inline_target import _normalize_citation_markers

    out = _normalize_citation_markers(text or "")
    out = _REF_MARKER_RE.sub(r"[\1]", out)
    out = _HEADING_RE.sub("", out)
    out = _BULLET_RE.sub(r"\1- ", out)
    out = _BLANKS_RE.sub("\n\n", out)
    return out.strip()


def _order_for(target_id: str) -> list[str]:
    """Deterministic per-desk arm order. Seeded by desk id + a fixed salt."""
    digest = hashlib.sha256(f"{_SHUFFLE_SALT}:{target_id}".encode()).digest()
    return ["tower", "baseline"] if digest[0] % 2 == 0 else ["baseline", "tower"]


def _pair_ids(desks: list[dict[str, Any]]) -> dict[str, str]:
    """Opaque pair labels (P1, P2, …) so the pack never leaks the regime order."""
    return {d["target_id"]: f"P{i}" for i, d in enumerate(desks, start=1)}


def _fact_sheet(desk: dict[str, Any]) -> list[str]:
    """What the operator needs to judge temporal claims — without the firehose."""
    ev = desk["evidence"]
    top = list(ev["source_mix"].items())[:6]
    lines = [
        f"- **Country:** {desk['country']}",
        f"- **Evidence window:** {desk['window']['start']} → {desk['window']['end']} "
        f"(the {desk['window']['hours']}h window the pipeline actually read)",
        f"- **Signals in window:** {ev['window_total']}; "
        f"**shown to both arms after the pipeline's own caps:** "
        f"{ev['after_orient_pack']} from {ev['distinct_sources']} sources",
        "- **Heaviest sources:** " + ", ".join(f"{k} ({v})" for k, v in top),
        "",
        "Ten most recent headlines in the window (both arms saw these and "
        f"{max(0, ev['after_orient_pack'] - 10)} more):",
        "",
    ]
    for h in ev["headlines"][:10]:
        pub = h.get("published_at") or h.get("ingested_at") or "?"
        lines.append(f"  {h['n']}. {h['title']}  — *{h['source_id']}, {pub}*")
    return lines


def _write_blind_pack(bundle: dict[str, Any], out: Path) -> None:
    desks = bundle["desks"]
    pairs = _pair_ids(desks)
    L: list[str] = [
        "# R5 — blind grading pack",
        "",
        "Three country desks. Two intelligence reads each, in randomized order.",
        "One read came out of the full Legba pipeline; the other came from a "
        "single prompt to the same model over the same evidence. **Which is "
        "which is not stated here** — the mapping is in `R5_ANSWER_KEY.md`, "
        "which you should not open until the scoresheet is filled in.",
        "",
        "Read `R5_GRADING_PROTOCOL.md` first. Budget ~15 minutes.",
        "",
        "These reads are a frozen snapshot taken at bundle-capture time. The "
        "fleet has kept running since; grade what is here, not the live desk.",
        "",
        "---",
        "",
    ]
    for desk in desks:
        pid = pairs[desk["target_id"]]
        L += [f"## {pid} — {desk['country']}", "", "### Evidence available to both reads", ""]
        L += _fact_sheet(desk)
        L += ["", "---", ""]
        for slot, arm in enumerate(_order_for(desk["target_id"]), start=1):
            text = (
                desk["tower"]["body"]
                if arm == "tower"
                else desk["baseline"]["content"]
            )
            L += [
                f"### {pid} — Read {slot}",
                "",
                _normalize_arm_text(text),
                "",
            ]
        L += ["---", ""]
    L += [
        "## When you are done",
        "",
        "1. Fill `R5_SCORESHEET.csv` (3 rows).",
        "2. Only then open `R5_ANSWER_KEY.md`.",
        "3. Run `python3 scripts/r5_ablation_score.py "
        "--sheet R5_SCORESHEET.csv --bundle R5_BUNDLE.json`.",
        "",
    ]
    out.write_text("\n".join(L))


def _write_answer_key(bundle: dict[str, Any], out: Path) -> None:
    desks = bundle["desks"]
    pairs = _pair_ids(desks)
    L: list[str] = [
        "# R5 — answer key",
        "",
        "**Do not open until `R5_SCORESHEET.csv` is filled in.**",
        "",
        f"Generated {bundle['generated_at']} · baseline model "
        f"`{bundle.get('baseline_model')}` on `{bundle['core_component']}` "
        f"(self-hosted, $0) at temperature {bundle['baseline_temperature']}.",
        "",
        "## Mapping",
        "",
        "| Pair | Desk | Regime | Read 1 | Read 2 |",
        "|---|---|---|---|---|",
    ]
    for desk in desks:
        order = _order_for(desk["target_id"])
        L.append(
            f"| {pairs[desk['target_id']]} | {desk['country']} "
            f"(`{desk['target_id']}`) | {desk['regime']} "
            f"| {'TOWER' if order[0] == 'tower' else 'BASELINE'} "
            f"| {'TOWER' if order[1] == 'tower' else 'BASELINE'} |"
        )
    L += [
        "",
        "## Snapshot semantics",
        "",
        "The tower arm is the composition that was HEAD when the bundle was "
        "captured. The fleet runs on cadence, so by the time you grade this the "
        "live head for one or more of these desks has probably been superseded. "
        "That does not weaken the experiment and nothing needs re-running: the "
        "evidence window, the signal slice, and the baseline generation are all "
        "pinned to the captured row's own window, so the pair remains internally "
        "consistent forever. It does mean this is a frozen artifact — re-running "
        "the harness produces a NEW experiment against a newer head, not a "
        "reproduction of this one.",
        "",
        "## Desk selection (deterministic)",
        "",
        f"Rule: {bundle['selection']['rule']}",
        "",
        "| Desk | Country | Signals in its window | Units composed |",
        "|---|---|---:|---:|",
    ]
    for r in bundle["ranking"][:12]:
        L.append(
            f"| `{r['target_id']}` | {r['country']} | {r['volume']} | {r['n_units']} |"
        )
    L += [
        "",
        f"(full ranking of all {len(bundle['ranking'])} desks is in the bundle "
        "under `ranking`)",
        "",
        "## Provenance",
        "",
    ]
    for desk in desks:
        t = desk["tower"]
        b = desk["baseline"]
        L += [
            f"### {pairs[desk['target_id']]} — {desk['country']} ({desk['regime']})",
            "",
            f"- **Tower arm:** live `country_composition` finding `{t['output_id']}`, "
            f"produced {t['produced_at']}, confidence {t['confidence']:.2f}, "
            f"composed from {len(t['composed_units'])} unit findings. "
            "Pulled read-only; the tower was NOT re-run.",
            f"  - title: *{t['title']}*",
            "  - composed units: "
            + ", ".join(f"`{u['analyst_id']}`" for u in t["composed_units"]),
            f"- **Baseline arm:** one completion, {b.get('elapsed_s')}s, "
            f"prompt {b.get('prompt_chars')} chars over "
            f"{desk['evidence']['after_orient_pack']} signals, "
            f"length envelope {desk.get('baseline_envelope')} words.",
            "",
        ]
    L += [
        "## The exact baseline prompt",
        "",
        f"System: `{bundle['baseline_system']}`",
        "",
        "User (evidence blocks elided):",
        "",
        "```",
        (desks[0]["baseline"].get("prompt") or "").split("\n\n[1]")[0].strip(),
        "```",
        "",
        "## Blinding concessions made to the baseline",
        "",
        "Both are PRESENTATION-level. Neither adds evidence, retrieval, "
        "verification, memory, or analytic structure — the baseline remains one "
        "prompt and one answer.",
        "",
    ]
    for c in bundle.get("blinding_concessions") or ["(none — raw baseline)"]:
        L.append(f"- {c}")
    L += [
        "",
        "Re-run without them via `--no-cite` / `--no-envelope` on the harness if "
        "you judge the concessions unacceptable.",
        "",
        "## What the tower got that the baseline did not",
        "",
        desks[0]["tower_only_context"],
        "",
        "## Raw (un-normalized) arm texts",
        "",
        "The pack applies symmetric presentation normalization (marker syntax, "
        "heading levels, bullet glyphs, blank runs). Originals below for audit.",
        "",
    ]
    for desk in desks:
        L += [
            f"### {pairs[desk['target_id']]} — {desk['country']} — TOWER (raw)",
            "",
            "```",
            desk["tower"]["body"],
            "```",
            "",
            f"### {pairs[desk['target_id']]} — {desk['country']} — BASELINE (raw)",
            "",
            "```",
            desk["baseline"]["content"],
            "```",
            "",
        ]
    out.write_text("\n".join(L))


def _write_protocol(bundle: dict[str, Any], out: Path) -> None:
    desks = bundle["desks"]
    pairs = _pair_ids(desks)
    ids = ", ".join(pairs[d["target_id"]] for d in desks)
    L = f"""# R5 — grading protocol

**Time budget: 15 minutes.** Three pairs ({ids}), two reads each, four scores
per read plus one forced preference and one guess. Twenty-seven cells total.

## What is being decided

Every standing review lands on the same sentence. review3 §9: *"a hostile A/B:
pick three desks, run Legba's verified composition against a $0.02 GPT-mini
summary of the same 72h slice, blind-grade both, publish the delta. If Legba
wins, the cathedral has a congregation. If it doesn't, everything else — the
receipts, the seams, the chorus diff — is ornamentation on a very well-documented
RSS reader with a diary."* review1 §9: *"If no reported number moves, that is the
most important finding the project will ever produce."*

One read per pair is the full pipeline: {len(desks[0]['tower']['composed_units'])}-odd
specialist units, each with its own GATHER pass, citation extraction, and
faithfulness judge, then a composition layer with a verify floor, continuity
memory, and a situation register. The other is one prompt to the same self-hosted
model over the same signals. **You are grading whether all of that machinery
produces a better read.**

## Order of operations (do not deviate)

1. Read `R5_BLIND_PACK.md`. **Do not open `R5_ANSWER_KEY.md`.**
2. For each pair: skim the evidence fact sheet, then read both reads.
3. Fill one row per pair in `R5_SCORESHEET.csv`.
4. Only then open the answer key.

## The four axes (1-5, integers)

Score each read independently. Do not score relative to the other read — the
preference column captures the comparison.

| Axis | Column | 1 | 3 | 5 |
|---|---|---|---|---|
| **Useful to an analyst** | `useful` | I learned nothing I could use | Tells me the situation but nothing I would repeat to anyone | I would forward this |
| **Correct per your knowledge** | `correct` | Contains claims I know to be wrong | Nothing wrong, nothing checkable | Every checkable claim matches what I know |
| **Actionability** | `actionable` | No implication for anything I would do | Vague "watch this space" | Names a specific thing to watch, decide, or collect |
| **Temporal clarity** | `temporal` | Cannot tell what is new vs standing | New/standing mostly separable | Explicitly separates the standing picture from the last {bundle['unit_window_hours']}h delta |

**`correct` may be `NA`** when you have no independent knowledge of the desk —
this is expected on the quiet desk. `NA` rows are dropped from that axis only,
never imputed. Do not guess; an invented correctness score is worse than a gap.

## The forced preference

`preferred` — `1` or `2`, no ties. The question is not "which is better written"
but: **which one would you rather have found on your desk this morning?**

This is the decision-relevant number. The four axes explain it; the preference
is it.

## The guess (this is the honest part)

`tower_guess` — `1`, `2`, or `unsure`. **Fill it in before you score, and do not
change it after.**

The blind is imperfect and saying so is better than faking it. Two tells survive
by design:

- **The pipeline narrates its own internals.** One arm sometimes says things like
  "the energy-security unit reports…" or refers to a situation register. That is a
  real property of the product — and a defect the reviews already flag ("machine
  internals leaking into analytic text"). Editing it out would have falsified the
  artifact and hidden a known problem from its own evaluation.
- **Citation index magnitude.** One arm's markers point at a handful of
  intermediate findings; the other's point at the {max(d['evidence']['after_orient_pack'] for d in desks)}-signal
  slice. A `[74]` can therefore only be one of them. Removing the markers would
  have meant stripping evidence, which is worse than the tell.

Presentation WAS normalized symmetrically: citation marker syntax, heading levels,
bullet glyphs, blank runs, and output length (the baseline was given a length
envelope measured from that desk's own pipeline output).

So: score the content axes as if you did not know. Then the scoring script uses
your guesses to state plainly how much the result rests on a blind that held.
If you guessed right every time and the pipeline swept, that is weaker evidence
than a coin-flip guess rate with the same sweep — and the output will say so.

## Scoring honestly given the tells

- **Do not reward citation density.** More markers is not more correct. Both arms
  cite; they cite different things.
- **Do not reward or punish the internals-narration on `useful`.** If "the
  energy-security unit reports X" annoys you, that belongs in a note, not in a
  score — it is a prose defect already on a fix train, and the experiment is about
  whether the pipeline's ANALYSIS is better, not its voice.
- **Do check a claim or two.** The `correct` axis is the only axis where your own
  knowledge is the instrument. The fact sheet gives you the window and the top
  headlines; that is usually enough to catch a temporal error.
- **A read that correctly says "nothing changed" is not a low score.** On a quiet
  desk, a confident null is the right answer and should score well on `temporal`
  and `correct`. Manufacturing significance from six signals is the failure mode
  to punish.

## Notes column

Free text, optional, one line. The most valuable thing you can write is the
specific claim that made you prefer one read, or the specific error that sank one.

## After scoring

    python3 scripts/r5_ablation_score.py \\
        --sheet R5_SCORESHEET.csv --bundle R5_BUNDLE.json

## What n=3 can and cannot say

Three pairs is a **screening experiment**. A clean 3-0 sweep is p=0.25 two-sided
on an exact sign test — it cannot reach significance, and the scoring script will
refuse to pretend otherwise. What n=3 CAN do is kill the question fast: if the
pipeline loses or ties at n=3, that is already the most important finding the
project has produced, and no larger n is needed to justify acting on it. A
pipeline win at n=3 is a licence to run the decision-grade version (n>=10 desks,
same harness, `--desks`), not a result to publish.
"""
    out.write_text(L)


def _write_scoresheet(bundle: dict[str, Any], out: Path) -> bool:
    """Write the empty sheet — but NEVER over grading already done.

    Re-rendering the pack (to fix a typo, to regenerate after a bundle refresh) is
    a normal thing to do mid-grading, and silently truncating the operator's
    half-filled sheet would destroy the only irreplaceable artifact in this whole
    experiment. Returns False when an existing sheet was preserved.
    """
    if out.exists():
        existing = list(csv.DictReader(out.open()))
        if any(
            (r.get(c) or "").strip()
            for r in existing
            for c in r
            if c not in ("pair", "country")
        ):
            print(
                f"[R5] {out.name} already has grades — PRESERVED, not overwritten",
                file=sys.stderr,
            )
            return False
    pairs = _pair_ids(bundle["desks"])
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "pair", "country",
                "r1_useful", "r1_correct", "r1_actionable", "r1_temporal",
                "r2_useful", "r2_correct", "r2_actionable", "r2_temporal",
                "preferred", "tower_guess", "notes",
            ]
        )
        for desk in bundle["desks"]:
            w.writerow(
                [pairs[desk["target_id"]], desk["country"]] + [""] * 11
            )
    return True


def _write_evidence(bundle: dict[str, Any], out: Path) -> None:
    pairs = _pair_ids(bundle["desks"])
    L = [
        "# R5 — full evidence slices (optional deep-dive)",
        "",
        "The exact numbered blocks both arms were given, rendered by the "
        "pipeline's own `inline_target._render_signal`. You do not need this to "
        "grade — the fact sheet in the blind pack is enough. Use it to check a "
        "specific citation.",
        "",
        "Note: the numbering here matches the BASELINE arm's citation markers. "
        "The pipeline arm's markers index its intermediate unit findings, not "
        "this list.",
        "",
    ]
    for desk in bundle["desks"]:
        L += [
            f"## {pairs[desk['target_id']]} — {desk['country']} "
            f"({desk['evidence']['after_orient_pack']} signals)",
            "",
            "```",
            "\n\n".join(desk["evidence"]["rendered_blocks"]),
            "```",
            "",
        ]
    out.write_text("\n".join(L))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    bundle = json.loads(Path(args.bundle).read_text())
    if any("content" not in (d.get("baseline") or {}) for d in bundle["desks"]):
        raise SystemExit("R5: bundle has no baseline generations (was --no-llm used?)")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_blind_pack(bundle, out_dir / "R5_BLIND_PACK.md")
    _write_answer_key(bundle, out_dir / "R5_ANSWER_KEY.md")
    _write_protocol(bundle, out_dir / "R5_GRADING_PROTOCOL.md")
    _write_scoresheet(bundle, out_dir / "R5_SCORESHEET.csv")
    _write_evidence(bundle, out_dir / "R5_EVIDENCE_SLICES.md")
    for name in (
        "R5_BLIND_PACK.md", "R5_ANSWER_KEY.md", "R5_GRADING_PROTOCOL.md",
        "R5_SCORESHEET.csv", "R5_EVIDENCE_SLICES.md",
    ):
        p = out_dir / name
        print(f"[R5] {p} ({p.stat().st_size} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
