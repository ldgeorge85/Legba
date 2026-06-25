# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-17 — register the shared global-news SourceDescriptors.

Registers the three shared, open, poll RSS sources every G20 country target
wires to by predicate (one poll/connection per source for the whole platform):

  * source.bbc.world        (https://feeds.bbci.co.uk/news/world/rss.xml)
  * source.aljazeera.world  (https://www.aljazeera.com/xml/rss/all.xml)
  * source.dw.world         (https://rss.dw.com/atom/rss-en-all)

Direct-DB registration via DescriptorRegistry against the migrated Postgres
(default ``legba_pivot_test`` — override with LEGBA_DATA_PG_DB).  Idempotent:
re-runs report ``unchanged`` for already-current heads.

Each descriptor is validated against the real pydantic SourceDescriptor schema
before it touches the DB (the registry validates again on the write path).
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _p17_registrar import (  # noqa: E402
    Family,
    RegisterResult,
    close_registry,
    open_registry,
    print_results,
    register_descriptor,
)

from legba.data.schemas.source import SourceDescriptor  # noqa: E402

DESCRIPTORS_DIR = pathlib.Path(__file__).resolve().parent.parent / "descriptors"

SOURCE_FILES = [
    "source_bbc_world.yaml",
    "source_aljazeera_world.yaml",
    "source_dw_world.yaml",
]


def _load(name: str) -> SourceDescriptor:
    body = yaml.safe_load((DESCRIPTORS_DIR / name).read_text())
    # Placeholder 16-zero version satisfies the [a-f0-9]{16,64} pattern; the
    # registry stamps the real content hash on write.
    body.setdefault("identity", {})["version"] = "0" * 16
    return SourceDescriptor.model_validate(body, strict=False)


async def main() -> int:
    pg, reg = await open_registry()
    try:
        results: list[RegisterResult] = []
        for fname in SOURCE_FILES:
            desc = _load(fname)
            results.append(
                await register_descriptor(pg, reg, family=Family.SOURCE, descriptor=desc)
            )
        failures = print_results("Shared news sources:", results)
    finally:
        await close_registry(pg, reg)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
