# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R-1 — seam ids that parallel lanes cannot collide on.

``docs/SEAMS.md`` used to carry "the next free number is N": every lane that
declared a seam edited that line and raced the others for N. Seams 1-94 keep
their numbers; a new seam takes a lower-kebab slug, and its row sits after the
numbered rows in slug order, so two lanes touch different lines.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEAMS = (REPO / "docs" / "SEAMS.md").read_text(encoding="utf-8")
LAST_NUMBERED_SEAM = 94
SLUG = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)+$")


def _section(title: str) -> str:
    return SEAMS.split(f"## {title}", 1)[1].split("\n## ", 1)[0]


def _ids(section: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"^\|\s*([0-9]+|[a-z][a-z0-9-]*)\s*\|", section, re.M)]


def test_no_shared_counter_line():
    assert "next free number" not in SEAMS.lower()


def test_numbers_are_closed_and_slugs_are_well_formed_unique_and_sorted():
    declared = _ids(_section("Declared seams"))
    resolved = _ids(_section("Resolved seams"))
    numbers = [int(i) for i in declared if i.isdigit()]
    slugs = [i for i in declared if not i.isdigit()]
    assert numbers and max(numbers) <= LAST_NUMBERED_SEAM, "new seams take a slug, not a number"
    assert len(set(numbers)) == len(numbers)
    assert all(SLUG.match(s) for s in slugs), slugs
    assert slugs == sorted(slugs), "named seams sit in slug order"
    assert len(set(slugs)) == len(slugs)
    # numbered rows first, then named ones
    first_slug = next((k for k, i in enumerate(declared) if not i.isdigit()), len(declared))
    assert all(i.isdigit() for i in declared[:first_slug])
    for i in resolved:
        assert i.isdigit() and int(i) <= LAST_NUMBERED_SEAM or SLUG.match(i), i


def test_the_generator_reads_named_seams(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "gen_not_published", REPO / "scripts" / "gen_not_published.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    md = SEAMS.replace(
        "\n\n---\n\n## Resolved seams",
        "\n| example-named-seam | A named seam | Something not built. | A guard. |"
        "\n\n---\n\n## Resolved seams", 1)
    item = gen.seam_items(md, ("example-named-seam",))[0]
    assert item["id"] == "seam-example-named-seam"
    assert item["source"] == "docs/SEAMS.md #example-named-seam"
