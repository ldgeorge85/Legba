# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Export an instance profile's rosters into the package (ROSTER-1).

Writes ``src/legba/data/profile_rosters.json`` from the ``rosters`` block of
``profiles/<id>/profile.yaml`` (:func:`legba.data.rosters.export_rosters`).
The runtime and registry images carry ``src/`` only, so this packaged copy is
what ``legba.data.rosters`` reads when ``LEGBA_PROFILE`` is unset — the same
build-time export the workstation's ``profile_ui.json`` is
(``scripts/gen_profile_ui.py``). The committed file is the DEFAULT profile's
export; ``tests/data_pkg/test_rosters.py`` fails when it is stale.

A second instance regenerates it from its own profile before building images::

    PYTHONPATH=src python3 scripts/gen_profile_rosters.py                      # write (default profile)
    PYTHONPATH=src python3 scripts/gen_profile_rosters.py --check              # exit 1 if stale
    PYTHONPATH=src python3 scripts/gen_profile_rosters.py --profile profiles/<id>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "src" / "legba" / "data" / "profile_rosters.json"


def render(profile_dir: str | None = None) -> str:
    from legba.data.profile import load_profile, resolve_profile
    from legba.data.rosters import export_rosters

    return export_rosters(load_profile(resolve_profile(profile_dir, root=REPO_ROOT)))


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
            print(f"{out} is stale for this profile — run scripts/gen_profile_rosters.py", file=sys.stderr)
            return 1
        return 0
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
