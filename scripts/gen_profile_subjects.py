# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Export an instance profile's declared subjects into the package (W10 SUBJECT-1).

Writes ``src/legba/data/profile_subjects.json`` from the profile's target
descriptor files, its ``frame`` vocabulary seed and its ``subjects`` block
(:func:`legba.data.subject.export_subjects`). The runtime and registry images
carry ``src/`` only, so this packaged copy is what the subject ladder
(``legba.data.subject``) reads for a bare target id when ``LEGBA_PROFILE`` is
unset — the same build-time export ``profile_rosters.json`` is
(``scripts/gen_profile_rosters.py``). The committed file is the DEFAULT
profile's export; ``tests/data_pkg/test_subject_ladder.py`` fails when it is
stale.

    PYTHONPATH=src python3 scripts/gen_profile_subjects.py                      # write (default profile)
    PYTHONPATH=src python3 scripts/gen_profile_subjects.py --check              # exit 1 if stale
    PYTHONPATH=src python3 scripts/gen_profile_subjects.py --profile profiles/<id>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "src" / "legba" / "data" / "profile_subjects.json"


def render(profile_dir: str | None = None) -> str:
    from legba.data.profile import load_profile, resolve_profile
    from legba.data.subject import export_subjects

    return export_subjects(load_profile(resolve_profile(profile_dir, root=REPO_ROOT)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", default=None,
                        help="profile directory, relative to the repo root "
                             "(default: $LEGBA_PROFILE, else profiles/geopolitics)")
    parser.add_argument("--out", default=str(OUT), help="output path")
    parser.add_argument("--check", action="store_true", help="exit 1 if the output is stale")
    args = parser.parse_args(argv)
    text = render(args.profile)
    out = Path(args.out)
    if args.check:
        current = out.read_text(encoding="utf-8") if out.exists() else ""
        if current != text:
            print(f"{out} is stale for this profile — run scripts/gen_profile_subjects.py", file=sys.stderr)
            return 1
        return 0
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
