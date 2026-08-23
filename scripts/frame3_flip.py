#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FRAME-3 — flip the NINE units' live ``method.system_prompt`` to severity-as-state.

DRY BY DEFAULT. With no flags this GETs, diffs and prints; it writes nothing.
``--apply`` is the only thing that PUTs.

    python3 scripts/frame3_flip.py                     # dry-run
    python3 scripts/frame3_flip.py --apply             # PUT
    python3 scripts/frame3_flip.py --only escalation   # canary

WHY A SCRIPT AND NOT A DEPLOY, unchanged from ``scripts/voice4_flip/``: a unit's
system prompt does not live in the code image. ``inline_target`` reads it from
the descriptor the registry serves, so the new contract reaches production only
when these descriptors are PUT. The FRAME-3 code half — the ``severity_delta``
reader, the composition render, the scorecard field — ships in the image and is
tolerant of an absent delta, so the two halves may land in either order; until
this script runs, every delta is simply ``None`` and every band means exactly
what it meant before.

THREE WAYS THIS DIFFERS FROM THE VOICE-4 KIT, each deliberate:

1. **ALL NINE UNITS, INCLUDING THE VOICE-HELD DESK.** ``narrative_coordination``
   is held from the D6 VOICE rewrite because its replay could not catch the
   coordination signal on two positive windows — a hold on that desk's PROSE.
   FRAME-3 is orthogonal to it and cannot honor it: ``narrative_coordination``
   is one of the seven ``scorecard_banding.DIMENSIONS``, and a card whose seven
   dimensions mix two meanings of ``severity`` is worse than one uniformly on
   the old meaning. So the rule was added to the BASE ``UNIT_READ_CONTRACT``
   (which the held desk carries) rather than to the D6 amendment, and every desk
   flips together. The VOICE hold is untouched: this script swaps the prompt the
   tree holds, and the tree still gives the held desk its pre-D6 body shape.

2. **THE PIN IS A CONTENT PROPERTY, NOT A FROZEN DIGEST.** VOICE-4 pinned nine
   sha256s because its prompts came from drafts in gitignored ``planning/`` and
   "byte-faithful to the draft" had to stay re-checkable in a clean checkout.
   FRAME-3's prompts come from the TREE, where the whole house contract is
   pinned against ``_tradecraft`` by ``tests/data_pkg/test_voice_contract.py``
   on every run. A second frozen digest here would go stale on the next
   unrelated descriptor edit and start refusing correct flips — so the check is
   the one that actually states this train's claim: the tree prompt must carry
   :data:`~legba.data.analysts._tradecraft.SEVERITY_AS_STATE_RULE`.

3. **IT IS RE-RUNNABLE AS A GAUGE.** A dry run after the flip prints, per desk,
   whether the live prompt carries the rule — which is the fleet check
   ``voice4_flip/verify_flip.py`` is for, and does not need its own script here.

See ``voice4_flip/_flip_common`` for the registry lifecycle (a PUT on an active
head is direct), the envelope rule (GET returns a row, PUT wants the bare body),
and why the PUT base is the live head rather than the tree file.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "voice4_flip"))
sys.path.insert(0, str(REPO_ROOT / "src"))

from _flip_common import (  # noqa: E402
    HELD_UNIT,
    PROMPT_PATH,
    dig,
    get_head,
    norm,
    plant,
    registry_base,
    registry_client,
    sha,
    structural_diff,
    token,
    tree_body,
    tree_prompt,
)
from legba.data.analysts._tradecraft import SEVERITY_AS_STATE_RULE  # noqa: E402

#: Every bounded unit. The VOICE-held desk is a member here — see the module
#: docstring — so the tuple is built from the VOICE kit's eight plus that desk
#: rather than re-listed, and a tenth unit joining the fleet joins this train by
#: joining that one.
from _flip_common import UNITS as _VOICE_UNITS  # noqa: E402

UNITS: tuple[str, ...] = _VOICE_UNITS + (HELD_UNIT,)

#: The sentence this train exists to land, whitespace-normalized. Normalized
#: because the tree wraps the constant inside a YAML block scalar at a width
#: that is a property of the file, not of the contract.
RULE_MARK: str = norm(SEVERITY_AS_STATE_RULE)


def carries_rule(prompt: str) -> bool:
    return RULE_MARK in norm(prompt)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Flip the nine unit prompts to the severity-as-state contract.",
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="actually PUT (default is a dry run that writes nothing)",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="GET and diff only — the default",
    )
    ap.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="UNIT",
        help=f"restrict to this unit (repeatable); default is all {len(UNITS)}",
    )
    ap.add_argument(
        "--strict-structure",
        action="store_true",
        help=(
            "refuse to PUT a unit whose live head disagrees with its tree file "
            "outside method.system_prompt. OFF by default: this train PRESERVES "
            "such fields (the PUT base is the live head), so drift is reported "
            "rather than blocking."
        ),
    )
    args = ap.parse_args()

    units = tuple(args.only) if args.only else UNITS
    if unknown := [u for u in units if u not in UNITS]:
        raise SystemExit(
            f"unknown unit(s): {', '.join(unknown)}\n  known: {', '.join(UNITS)}"
        )

    label = "APPLY (PUT)" if args.apply else "DRY-RUN (no writes)"
    print(f"FRAME-3 severity-as-state prompt flip — {label}")
    print(f"registry: {registry_base()}")
    print(f"units:    {len(units)} of {len(UNITS)}\n")

    flipped: list[str] = []
    already: list[str] = []
    failures: list[str] = []
    drift_notes: list[str] = []

    with registry_client(registry_base(), token()) as client:
        for unit in units:
            intended = tree_prompt(unit)
            if not carries_rule(intended):
                failures.append(
                    f"{unit}: the TREE descriptor does not carry "
                    "SEVERITY_AS_STATE_RULE — this train's own change is missing "
                    "from the file it would ship; do not flip"
                )
                print(f"  ! {unit}: TREE MISSING THE RULE — refusing\n")
                continue

            try:
                live, version, state = get_head(client, unit)
            except Exception as exc:
                failures.append(f"{unit}: GET head — {exc}")
                print(f"  ! {unit}: GET head failed — {exc}\n")
                continue

            old = dig(live, PROMPT_PATH) or ""
            print(f"  * {unit} @ head {version[:12]} state={state}")
            print(
                f"      prompt  {sha(old)[:12]} -> {sha(intended)[:12]}"
                f"   {len(old)} -> {len(intended)} chars"
            )
            print(
                "      severity contract: "
                f"live={'standing' if carries_rule(old) else 'delta'} "
                "-> standing"
            )

            tree = tree_body(unit)
            tree_state = dig(tree, ("identity", "state"))
            if tree_state != state:
                print(
                    f"      lifecycle: live={state}, tree file says {tree_state!r} "
                    "— live wins (the PUT base is the live head)"
                )

            drift = structural_diff(live, tree)
            if drift:
                print(f"      STRUCTURAL DRIFT ({len(drift)}) beyond system_prompt:")
                for line in drift:
                    print(f"        - {line}")
                    drift_notes.append(f"{unit}: {line}")
                if args.strict_structure:
                    failures.append(
                        f"{unit}: {len(drift)} structural difference(s) live vs "
                        "tree (--strict-structure)"
                    )
            else:
                print("      structural: clean (nothing but system_prompt differs)")

            if old == intended:
                already.append(unit)
                print("      already flipped — no PUT needed\n")
                continue

            if not args.apply:
                print("      [dry-run] would PUT\n")
                continue

            if drift and args.strict_structure:
                print("      SKIPPED: resolve the structural drift first\n")
                continue

            plant(live, PROMPT_PATH, intended)
            live.setdefault("identity", {})["version"] = version
            r = client.put(f"/descriptors/analyst/{unit}", json=live)
            if r.status_code != 200:
                failures.append(f"{unit}: HTTP {r.status_code} {r.text[:300]}")
                print(f"      FAILED: HTTP {r.status_code} {r.text[:300]}\n")
                continue
            new_version = str(r.json().get("version"))

            # Read back and compare BYTES. A 200 is the registry's opinion; the
            # only thing that settles a byte-faithfulness claim is the bytes.
            try:
                back, back_version, back_state = get_head(client, unit)
            except Exception as exc:
                failures.append(f"{unit}: PUT ok but read-back failed — {exc}")
                print(f"      PUT ok -> {new_version[:12]}, read-back FAILED: {exc}\n")
                continue
            landed = dig(back, PROMPT_PATH) or ""
            if landed != intended:
                failures.append(
                    f"{unit}: read-back mismatch — live sha {sha(landed)[:12]} "
                    f"!= intended {sha(intended)[:12]}"
                )
                print(
                    f"      PUT ok -> {new_version[:12]} but READ-BACK MISMATCH "
                    f"({sha(landed)[:12]} != {sha(intended)[:12]})\n"
                )
                continue
            flipped.append(unit)
            print(
                f"      PUT ok -> {back_version[:12]} state={back_state}; "
                f"read-back byte-identical\n"
            )

    print("=" * 68)
    print(f"flipped:         {len(flipped)}  {', '.join(flipped) or '-'}")
    print(f"already current: {len(already)}  {', '.join(already) or '-'}")
    print(f"failed:          {len(failures)}")
    for f in failures:
        print(f"  ! {f}")
    if drift_notes:
        print(
            f"\ntree/live drift OUTSIDE the prompt ({len(drift_notes)}) — "
            "PRE-EXISTING, neither shipped nor removed by this train, because "
            "every PUT body is the live head with one field swapped. Each is a "
            "tree-vs-registry question of its own; decide separately."
        )
    if args.apply and flipped:
        print(
            "\nThe fleet now tags severity as STANDING STATE. The bands do not "
            "move until each desk's next run writes a head under the new "
            "contract — take the §7 before/after band diff (one conflict desk, "
            "one quiet desk) against scorecards stamped "
            "banding_semantics='standing' before reading the fleet."
        )
    if not args.apply:
        print("\nDry run — nothing was written. Re-run with --apply to PUT.")
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
