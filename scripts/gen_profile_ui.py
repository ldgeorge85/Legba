# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Export an instance profile's workstation labels to the UI (PROFILE-1).

Writes ``legba-ui-v3/src/lib/profile_ui.json`` from the ``ui`` (and roster)
keys of ``profiles/<id>/profile.yaml`` (:func:`legba.data.profile.export_ui`).
The workstation imports that JSON at build time (UI-D1 §2.3) until the
projection (VIEW-1) serves the same labels. The committed file is the
DEFAULT profile's export; ``tests/test_profile.py`` fails when it is not
byte-identical to what this prints, and pins the geopolitics export to a
golden of today's literals.

A second instance regenerates it from its own profile before building the UI::

    PYTHONPATH=src python3 scripts/gen_profile_ui.py                       # write (default profile)
    PYTHONPATH=src python3 scripts/gen_profile_ui.py --check               # exit 1 if stale
    PYTHONPATH=src python3 scripts/gen_profile_ui.py --profile profiles/<id>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "legba-ui-v3" / "src" / "lib" / "profile_ui.json"


def render(profile_dir: str | None = None) -> str:
    from legba.data.profile import export_ui, load_profile, resolve_profile

    return export_ui(load_profile(resolve_profile(profile_dir, root=REPO_ROOT)))


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
            print(f"{out} is stale for this profile — run scripts/gen_profile_ui.py", file=sys.stderr)
            return 1
        return 0
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
