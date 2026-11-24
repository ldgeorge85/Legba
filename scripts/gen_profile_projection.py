# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Export every profile's projection view into the package (W9 VIEW-1).

Writes ``src/legba/data/profile_projection.json``: one PLANE entry per
``profiles/<id>/profile.yaml`` — the profile's ``projection/view.yaml``
resolved against its manifest (units, tiers, labels, bands, scope), or
``view: null`` for a profile that declares no projection (its plane refuses
``projection_disabled``) — plus the default plane (the selected profile's).
The runtime and registry images carry ``src/`` only, so this packaged copy is
what ``legba.data.profile_projection`` reads when ``LEGBA_PROFILE`` is unset —
the same build-time export ``profile_rosters.json`` is
(``scripts/gen_profile_rosters.py``). ``tests/data_pkg/test_profile_projection.py``
fails when the committed file is stale.

    PYTHONPATH=src python3 scripts/gen_profile_projection.py            # write
    PYTHONPATH=src python3 scripts/gen_profile_projection.py --check    # exit 1 if stale
    PYTHONPATH=src python3 scripts/gen_profile_projection.py --profile profiles/<id>   # default plane
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILES_DIR = REPO_ROOT / "profiles"
OUT = REPO_ROOT / "src" / "legba" / "data" / "profile_projection.json"


def render(profile_dir: str | None = None, *, profiles_dir: Path = PROFILES_DIR) -> str:
    from legba.data.profile import MANIFEST_NAME, load_profile, resolve_profile
    from legba.data.profile_projection import export_planes

    profiles = [load_profile(d) for d in sorted(profiles_dir.iterdir()) if (d / MANIFEST_NAME).is_file()]
    default = load_profile(resolve_profile(profile_dir, root=REPO_ROOT))
    return export_planes(profiles, default_plane=default.plane)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", default=None,
                        help="the profile whose plane is the default, relative to the repo root "
                             "(default: $LEGBA_PROFILE, else profiles/geopolitics)")
    parser.add_argument("--out", default=str(OUT), help="output path")
    parser.add_argument("--check", action="store_true", help="exit 1 if the output is stale")
    args = parser.parse_args(argv)
    text = render(args.profile)
    out = Path(args.out)
    if args.check:
        current = out.read_text(encoding="utf-8") if out.exists() else ""
        if current != text:
            print(f"{out} is stale — run scripts/gen_profile_projection.py", file=sys.stderr)
            return 1
        return 0
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
