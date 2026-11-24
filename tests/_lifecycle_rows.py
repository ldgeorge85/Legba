# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The host lifecycle overlay as text, for tests that look for a row (R-1).

The rows moved from ``descriptors/LIFECYCLE.md`` into one file per row under
``descriptors/LIFECYCLE.d/``. A test that asserts a row is recorded should read
it through here (or through the walker's ``load_lifecycle``), never by opening
``LIFECYCLE.md`` alone.
"""
from __future__ import annotations

from pathlib import Path


def lifecycle_text(descriptors_dir: Path) -> str:
    """``LIFECYCLE.md`` followed by every row file, one row per line."""
    md = descriptors_dir / "LIFECYCLE.md"
    parts = [md.read_text(encoding="utf-8")] if md.exists() else []
    rows_dir = descriptors_dir / "LIFECYCLE.d"
    if rows_dir.is_dir():
        parts += [p.read_text(encoding="utf-8") for p in sorted(rows_dir.glob("*.md"))]
    return "\n".join(parts)
