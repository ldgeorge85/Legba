#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""VOICE-4 — prove the flip landed, and that the HELD desk did not move.

READ-ONLY. This script only ever GETs; there is no flag that makes it write.

    python3 scripts/voice4_flip/verify_flip.py

FOUR CHECKS, and each exists because a different thing could go wrong:

  1. TREE/LIVE AGREEMENT — for all eight units, sha256(live prompt) ==
     sha256(tree prompt) == the digest pinned in ``_flip_common``. Catches a
     partial apply, a PUT that landed on the wrong desk, and a tree that was
     edited after the digests were pinned.

  2. THE MA2 FLEET SENTENCE on all eight. The replay found units writing
     ``horizon_date``/``first_seen`` as prose, which makes ``IndicatorEntry``
     drop the whole entry silently. The repair is one sentence and it is only
     a repair if every desk has it.

  3. THE MA4 TITLE AMENDMENT on all eight — the ALL-AT-ONCE contract. The
     HOUSE READ CONTRACT opens by claiming it is "identical on every desk", so
     an amendment that lands on some desks and not others makes the contract
     lie about itself. Eight of nine is the intended state ONLY because the
     ninth is formally held; that is asserted in check 4 rather than assumed.

  4. THE HELD DESK IS UNCHANGED — ``narrative_coordination``'s live prompt
     still hashes to its pre-flip value, and does NOT carry the two fleet
     sentences. If it silently acquired them, something PUT the held desk.

Exit code is 0 only when all four pass for every desk.
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
    MA2_DATE_FORMAT_SENTENCE,
    PROMPT_PATH,
    TITLE_AMENDMENT_SENTENCE,
    UNITS,
    d6_base,
    dig,
    get_head,
    norm,
    registry_base,
    registry_client,
    sha,
    token,
    tree_prompt,
)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Verify the VOICE-4 flip landed on all eight desks (read-only).",
    )
    ap.add_argument(
        "--held-sha",
        default=HELD_SHA256,
        metavar="SHA256",
        help=(
            "expected sha256 of the HELD desk's prompt; defaults to the pinned "
            "pre-flip value. Pass the sha the dry run printed to check against "
            "what was actually live at flip time."
        ),
    )
    args = ap.parse_args()

    print("VOICE-4 flip verification — READ-ONLY")
    print(f"registry: {registry_base()}\n")

    problems: list[str] = []
    ma2 = norm(MA2_DATE_FORMAT_SENTENCE)
    title = norm(TITLE_AMENDMENT_SENTENCE)

    with registry_client(registry_base(), token()) as client:
        print("  1/3  tree-live agreement + fleet sentences")
        print(
            f"      {'unit':24s} {'live sha':14s} {'tree':6s} {'pin':5s} "
            f"{'MA2':5s} {'TITLE':5s}"
        )
        for unit in UNITS:
            try:
                live, version, state = get_head(client, unit)
            except Exception as exc:
                problems.append(f"{unit}: GET head — {exc}")
                print(f"      {unit:24s} GET FAILED — {exc}")
                continue
            prompt = dig(live, PROMPT_PATH) or ""
            live_sha = sha(prompt)
            tree_ok = live_sha == sha(tree_prompt(unit))
            # The D6 pin covers the DRAFT bytes; a later train's contract
            # paragraph is peeled off before comparing (see ``d6_base``). The
            # tree check above stays whole-prompt — that one IS about the layers.
            pin_ok = sha(d6_base(prompt)) == INTENDED_SHA256[unit]
            normed = norm(prompt)
            ma2_ok = ma2 in normed
            title_ok = title in normed

            print(
                f"      {unit:24s} {live_sha[:12]:14s} "
                f"{'ok' if tree_ok else 'FAIL':6s} {'ok' if pin_ok else 'FAIL':5s} "
                f"{'ok' if ma2_ok else 'FAIL':5s} {'ok' if title_ok else 'FAIL':5s}"
            )
            if not tree_ok:
                problems.append(
                    f"{unit}: live prompt != tree prompt "
                    f"({live_sha[:12]} vs {sha(tree_prompt(unit))[:12]})"
                )
            if not pin_ok:
                problems.append(
                    f"{unit}: live prompt != pinned digest "
                    f"({live_sha[:12]} vs {INTENDED_SHA256[unit][:12]})"
                )
            if not ma2_ok:
                problems.append(f"{unit}: MA2 date-format sentence missing")
            if not title_ok:
                problems.append(f"{unit}: MA4 TITLE amendment missing")

        print("\n  2/3  the all-at-once contract")
        print(
            f"      MA2 on all {len(UNITS)}:   "
            f"{'YES' if not [p for p in problems if 'MA2' in p] else 'NO'}"
        )
        print(
            f"      TITLE on all {len(UNITS)}: "
            f"{'YES' if not [p for p in problems if 'TITLE' in p] else 'NO'}"
        )

        print(f"\n  3/3  the HELD desk — {HELD_UNIT}")
        try:
            held_body, held_version, held_state = get_head(client, HELD_UNIT)
            held_prompt = dig(held_body, PROMPT_PATH) or ""
            # Peeled, like every other digest here: FRAME-3's contract paragraph
            # is EXPECTED on this desk, and the hold this checks is the D6 prose.
            held_sha = sha(d6_base(held_prompt))
            unchanged = held_sha == args.held_sha
            held_normed = norm(held_prompt)
            print(f"      head {held_version[:12]} state={held_state}")
            print(
                f"      prompt {held_sha[:12]} vs expected {args.held_sha[:12]} — "
                f"{'UNCHANGED' if unchanged else 'CHANGED'}"
            )
            if not unchanged:
                problems.append(
                    f"{HELD_UNIT}: prompt CHANGED ({held_sha[:12]} != "
                    f"{args.held_sha[:12]}) — the held desk was PUT"
                )
            for name, sentence in (("MA2", ma2), ("TITLE", title)):
                if sentence in held_normed:
                    problems.append(
                        f"{HELD_UNIT}: carries the {name} sentence but is HELD"
                    )
                    print(f"      {name}: PRESENT — unexpected on a held desk")
                else:
                    print(f"      {name}: absent, as expected while held")
        except Exception as exc:
            problems.append(f"{HELD_UNIT}: GET head — {exc}")
            print(f"      GET FAILED — {exc}")

    print("\n" + "=" * 68)
    if problems:
        print(f"FAIL — {len(problems)} problem(s):")
        for p in problems:
            print(f"  ! {p}")
        return 1
    print(f"PASS — {len(UNITS)} desks flipped and byte-identical to the tree;")
    print(f"       MA2 and the TITLE amendment on all {len(UNITS)};")
    print(f"       {HELD_UNIT} unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
