#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Load ONE curated ``layer_map`` YAML into ``source_layers`` / ``desk_apertures``
(Program 6 L0, migration 0214).

  PYTHONPATH=src LEGBA_DATA_PG_HOST=127.0.0.1 LEGBA_DATA_PG_DB=legba \\
      python3 scripts/load_layer_map.py \\
          descriptors/layer_map_il.yaml --target country_watch_il

NO ANALYST, NO ROUTE, NO REGISTRY-LIFECYCLE REGISTRATION reads or writes
through this path — this script and ``legba.data.layers.loader`` are the only
writer of the two tables. ``--target`` is required and never guessed: a
country's layer TABLE (source_layers) is a fact about the country's
information environment, but which DESK's aperture it backs is a separate
fact belonging to ``runtime/target_resolution.py``'s country -> target_id
convention (``country_watch_<cc>`` / ``country_g20_<cc>``), not to this
script.

IDEMPOTENT + RE-VERSIONING. Re-running against an unchanged file is a no-op;
editing entries/apertures WITHOUT bumping ``map_version`` is refused loudly
(``LayerMapVersionConflict``) rather than silently applied; bumping
``map_version`` supersedes the old open rows (``valid_until`` closed) and
opens new ones. See ``legba.data.layers.loader`` for the full contract.

``--dry-run`` validates the file (schema + full aperture-vocab coverage) and
prints what WOULD be written, without opening a database connection.

Env: ``LEGBA_DATA_PG_HOST`` / ``_PORT`` / ``_USER`` / ``_PASSWORD`` / ``_DB``.
No key is ever printed.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg
import yaml

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
)

from legba.data.layers.loader import (  # noqa: E402
    LayerMapVersionConflict,
    load_layer_map,
)
from legba.data.registry.layer_map_schema import LayerMapDescriptor  # noqa: E402


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )


def _load_descriptor(path: str) -> LayerMapDescriptor:
    body = yaml.safe_load(open(path, encoding="utf-8").read())
    return LayerMapDescriptor.model_validate(body, strict=False)


async def run(args: argparse.Namespace) -> int:
    desc = _load_descriptor(args.path)
    print(f"layer_map   : {args.path}")
    print(f"  country   : {desc.country}   map_version={desc.map_version}")
    print(f"  identity  : {desc.identity.id}  state={desc.identity.state.value}")
    print(f"  entries   : {len(desc.entries)}")
    print(f"  apertures : {len(desc.apertures)} "
          f"({', '.join(sorted(a.layer + '=' + a.declared for a in desc.apertures))})")

    if args.dry_run:
        print("DRY RUN — nothing written")
        return 0

    conn = await _connect()
    try:
        async with conn.transaction():
            receipt = await load_layer_map(conn, desc, target_id=args.target)
    except LayerMapVersionConflict as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    finally:
        await conn.close()

    print(f"  wrote      inserted={receipt.inserted} "
          f"superseded={receipt.superseded} unchanged={receipt.unchanged}")
    for key, count in sorted(receipt.kind_counts.items()):
        print(f"    {key}: {count}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="the layer_map YAML file")
    parser.add_argument(
        "--target", required=True,
        help="the desk target_id the apertures half writes to, e.g. "
             "country_watch_il / country_g20_ru",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
