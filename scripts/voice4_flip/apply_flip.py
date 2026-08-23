#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""VOICE-4 — flip the eight cleared units' live ``method.system_prompt``.

DRY BY DEFAULT. With no flags this GETs, diffs and prints; it writes nothing.
``--apply`` is the only thing that PUTs.

    python3 scripts/voice4_flip/apply_flip.py             # dry-run
    python3 scripts/voice4_flip/apply_flip.py --apply     # PUT
    python3 scripts/voice4_flip/apply_flip.py --only escalation   # canary

WHAT A DRY RUN TELLS YOU, per unit: the live head's version and lifecycle
state, the old and new prompt sha256[:12] and char counts, whether the live
prompt already matches the tree, and any structural disagreement between the
live head and the tree file OUTSIDE ``method.system_prompt`` — which must be
none. It also prints the HELD desk's current sha so the flip has a recorded
before-value to check itself against afterwards.

WHAT ``--apply`` DOES, per unit: takes the LIVE head body as the base, swaps
ONLY ``method.system_prompt``, PUTs, then GETs again and compares the live
prompt to the intended text BYTE FOR BYTE. A PUT that returns 200 but did not
land the exact bytes is reported as a failure, because the whole train is a
byte-faithfulness claim.

THE HELD DESK IS NEVER PUT. ``narrative_coordination`` is not in :data:`UNITS`
and ``--only`` will not accept it. Lifting the hold is a decision that belongs
to a replay result, not to a flag on this script.

See ``_flip_common`` for the registry lifecycle (PUT on an active head is
direct), the envelope rule (GET returns a row, PUT wants the bare body), and
why the live head rather than the tree file is the PUT base.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _flip_common import (  # noqa: E402
    HELD_SHA256,
    HELD_UNIT,
    INTENDED_SHA256,
    PROMPT_PATH,
    UNITS,
    d6_base,
    dig,
    get_head,
    plant,
    registry_base,
    registry_client,
    sha,
    structural_diff,
    token,
    tree_body,
    tree_prompt,
)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Flip the eight cleared VOICE-4 unit prompts into the registry.",
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
            "rather than blocking — see the summary."
        ),
    )
    args = ap.parse_args()

    units = tuple(args.only) if args.only else UNITS
    unknown = [u for u in units if u not in UNITS]
    if unknown:
        held = [u for u in unknown if u == HELD_UNIT]
        msg = f"unknown unit(s): {', '.join(unknown)}\n  flip-eligible: {', '.join(UNITS)}"
        if held:
            msg += (
                f"\n  {HELD_UNIT} is HELD by the VOICE-3 replay and is not "
                "flippable from this script."
            )
        raise SystemExit(msg)

    label = "APPLY (PUT)" if args.apply else "DRY-RUN (no writes)"
    print(f"VOICE-4 unit-prompt flip — {label}")
    print(f"registry: {registry_base()}")
    print(f"units:    {len(units)} of {len(UNITS)} flip-eligible\n")

    flipped: list[str] = []
    already: list[str] = []
    failures: list[str] = []
    drift_notes: list[str] = []

    with registry_client(registry_base(), token()) as client:
        for unit in units:
            intended = tree_prompt(unit)
            # The digest covers the D6 DRAFT bytes, so a later train's contract
            # paragraph is peeled before comparing (``d6_base``) — the PUT still
            # ships the FULL tree prompt, layers and all, because the tree is
            # what production is supposed to hold.
            if sha(d6_base(intended)) != INTENDED_SHA256[unit]:
                failures.append(
                    f"{unit}: tree prompt sha {sha(d6_base(intended))[:12]} != "
                    f"pinned {INTENDED_SHA256[unit][:12]} — the descriptor was "
                    "edited away from the approved D6 draft; do not flip"
                )
                print(f"  ! {unit}: TREE DIGEST MISMATCH — refusing\n")
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
                    print(
                        "        (preserved, not shipped: the PUT base is the "
                        "live head)"
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

        # The HOLD, recorded on every run so the post-apply check has a
        # before-value that came from the live registry rather than from a note.
        # Peeled like every other digest here: FRAME-3's contract paragraph IS on
        # the held desk (a tag contract cannot be per-desk when the desk is a
        # scorecard dimension), and what the HOLD holds is this desk's D6 PROSE.
        try:
            held_body, held_version, held_state = get_head(client, HELD_UNIT)
            held_prompt = dig(held_body, PROMPT_PATH) or ""
            held_now = sha(d6_base(held_prompt))
            mark = "as pinned" if held_now == HELD_SHA256 else "!! DIFFERS FROM PIN"
            print(
                f"  = {HELD_UNIT} (HELD, never PUT) @ head {held_version[:12]} "
                f"state={held_state}"
            )
            print(
                f"      prompt  {held_now[:12]}  {len(held_prompt)} chars  ({mark})\n"
            )
            if held_now != HELD_SHA256:
                failures.append(
                    f"{HELD_UNIT}: live sha {held_now[:12]} != pinned "
                    f"{HELD_SHA256[:12]} — the held desk moved; investigate "
                    "before flipping anything"
                )
        except Exception as exc:
            failures.append(f"{HELD_UNIT}: GET head — {exc}")
            print(f"  ! {HELD_UNIT}: GET head failed — {exc}\n")

    print("=" * 68)
    print(f"flipped:         {len(flipped)}  {', '.join(flipped) or '-'}")
    print(f"already current: {len(already)}  {', '.join(already) or '-'}")
    print(f"failed:          {len(failures)}")
    for f in failures:
        print(f"  ! {f}")
    if drift_notes:
        print(
            f"\ntree/live drift OUTSIDE the prompt ({len(drift_notes)}) — "
            "PRE-EXISTING, neither shipped nor removed by this train,"
        )
        print("because every PUT body is the live head with one field swapped:")
        for note in drift_notes:
            print(f"  ~ {note}")
        print("Each is a tree-vs-registry question of its own. Decide separately.")
    if not args.apply:
        print("\nDry run — nothing was written. Re-run with --apply to PUT,")
        print("then scripts/voice4_flip/verify_flip.py to check the fleet.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
