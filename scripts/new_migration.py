#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Create a new, TIME-NAMED migration file (R-1).

    python3 scripts/new_migration.py claim_host_class
    # -> src/legba/data/migrations/20261114T1232_claim_host_class.sql

The name is the current UTC minute plus the slug, so a lane never reads or
bumps a shared counter and two lanes never collide on merge. The runner
applies time-named files after every numbered one, in time then slug order,
and records each by name (``legba.data.migrations``). ``--print`` prints the
name without writing.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from legba.data.migrations import MIGRATIONS_DIR, new_migration_name  # noqa: E402

TEMPLATE = """-- SPDX-FileCopyrightText: 2026 Lewis George
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- {name}
--
-- What this migration does and why (one paragraph). Idempotent: re-applying
-- must be a no-op (IF NOT EXISTS / ON CONFLICT DO NOTHING).

"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("slug", help="lower_snake_case: what the migration does")
    parser.add_argument("--print", action="store_true", help="print the name only")
    args = parser.parse_args(argv)
    name = new_migration_name(args.slug)
    if args.print:
        print(name)
        return 0
    path = MIGRATIONS_DIR / name
    if path.exists():
        print(f"{path} exists", file=sys.stderr)
        return 1
    path.write_text(TEMPLATE.format(name=name), encoding="utf-8")
    print(path.relative_to(REPO_ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
