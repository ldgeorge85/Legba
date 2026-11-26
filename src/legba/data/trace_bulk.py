# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compressed trace bulk: the bundle format and the one read path.

Every analyst run keeps a row in ``analyst_traces``. Three of its columns hold
most of the table's bytes and are only ever read whole, one run at a time:
``prompt_rendered``, ``input_payload`` and ``output_payload``. Once a run is
old enough, the compaction job (:mod:`legba.data.trace_bulk_compact`) packs
those three columns of up to N runs of the SAME analyst into one xz-compressed
bundle row in ``analyst_trace_bundles``, then sets the three columns to NULL on
each trace row and records two pointer columns:

* ``bulk_bundle_id`` - the bundle that holds the run's bulk;
* ``bulk_columns``   - which of the three columns were non-NULL when moved
  (a column that was NULL stays NULL and is not in the bundle).

Runs of one analyst repeat most of their prompt text from run to run, so a
bundle compresses several times better than Postgres's own per-value TOAST
compression, which sees each value alone.

Reading
-------
:func:`hydrate_traces` is the ONLY way a reader turns a fetched row back into
what it was before compaction. A reader selects the bulk columns it wants plus
:data:`POINTER_COLUMNS`; the helper fills each selected bulk column of a
compacted row from its bundle and drops the pointer keys, so the caller gets
the same dict for a compacted row as for a row that was never touched:

* text comes back as ``str``;
* a jsonb column comes back the way the connection decodes jsonb - a Python
  value on a pool with a json codec, the jsonb text on a plain connection;
* a selected ``<column>_bytes`` key (``octet_length(<column>::text)`` in SQL)
  is filled with the byte length of the stored jsonb text.

A missing bundle, a missing member or a corrupt body raises
:class:`TraceBulkError`: a reader gets the true body or an error, never a
silently empty one.

A query that filters on "column IS NOT NULL" uses :func:`present_sql` instead,
which is also true for a compacted row whose column was non-NULL.

Bundle body
-----------
``xz( b"LTB1" | u32 header length | header JSON | column texts )``. The header
lists each member's run id and, per moved column, the offset and length of its
UTF-8 text in the payload. jsonb columns are stored as the exact text Postgres
prints for them (``col::text``), so a read gives back byte-identical text. xz
carries its own CRC64, so a damaged body fails to decode instead of returning
wrong bytes.

Only the standard library is used (``lzma``, ``json``, ``struct``): this module
is imported by registry routes, and the registry image carries no third-party
compression package.
"""

from __future__ import annotations

import json
import lzma
import struct
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID

__all__ = [
    "BULK_COLUMNS",
    "CODEC",
    "JSON_COLUMNS",
    "POINTER_COLUMNS",
    "TraceBulkError",
    "decode_bundle",
    "encode_bundle",
    "hydrate_traces",
    "pointer_columns_sql",
    "present_sql",
]

#: The columns a bundle carries, in a fixed order.
BULK_COLUMNS: tuple[str, ...] = ("prompt_rendered", "input_payload", "output_payload")

#: Bulk columns whose type is jsonb (the rest are text).
JSON_COLUMNS: frozenset[str] = frozenset({"input_payload", "output_payload"})

#: The pointer columns a reader must select beside the bulk columns.
POINTER_COLUMNS: tuple[str, ...] = ("bulk_bundle_id", "bulk_columns")

#: ``analyst_trace_bundles.codec`` value for the format below.
CODEC = "xz-ltb1"

_MAGIC = b"LTB1"
_HEADER_LEN = struct.Struct(">I")
_XZ_PRESET = 6


class TraceBulkError(RuntimeError):
    """A compacted trace's bulk could not be read back exactly."""


def pointer_columns_sql(alias: str = "") -> str:
    """``"t.bulk_bundle_id, t.bulk_columns"`` for a SELECT list."""
    prefix = f"{alias}." if alias else ""
    return ", ".join(prefix + c for c in POINTER_COLUMNS)


def present_sql(column: str, alias: str = "") -> str:
    """SQL that is true when ``column`` held a value, compacted or not.

    Use it where a query used ``<column> IS NOT NULL``.
    """
    if column not in BULK_COLUMNS:
        raise ValueError(f"{column!r} is not a bulk column")
    prefix = f"{alias}." if alias else ""
    return (
        f"({prefix}{column} IS NOT NULL"
        f" OR '{column}' = ANY({prefix}bulk_columns))"
    )


# ---------------------------------------------------------------------------
# Format
# ---------------------------------------------------------------------------


def encode_bundle(
    members: Sequence[tuple[str, Mapping[str, str]]],
) -> tuple[bytes, int]:
    """Pack ``[(run_id, {column: text})]`` into one compressed body.

    Each mapping holds only the columns that were non-NULL; every value must
    be ``str`` (jsonb columns as their ``::text``). Returns ``(body,
    uncompressed_length)``.
    """
    header: list[dict[str, Any]] = []
    chunks: list[bytes] = []
    offset = 0
    seen: set[str] = set()
    for run_id, cols in members:
        rid = str(run_id)
        if rid in seen:
            raise ValueError(f"run {rid} appears twice in one bundle")
        seen.add(rid)
        spans: dict[str, list[int]] = {}
        for column in BULK_COLUMNS:
            if column not in cols:
                continue
            value = cols[column]
            if not isinstance(value, str):
                raise TypeError(f"{column} of run {rid} must be str, got {type(value).__name__}")
            data = value.encode("utf-8")
            spans[column] = [offset, len(data)]
            chunks.append(data)
            offset += len(data)
        extra = set(cols) - set(BULK_COLUMNS)
        if extra:
            raise ValueError(f"run {rid}: not bulk columns: {sorted(extra)}")
        header.append({"run_id": rid, "cols": spans})
    head = json.dumps({"members": header}, separators=(",", ":")).encode("utf-8")
    raw = _MAGIC + _HEADER_LEN.pack(len(head)) + head + b"".join(chunks)
    return lzma.compress(raw, format=lzma.FORMAT_XZ, preset=_XZ_PRESET), len(raw)


def decode_bundle(body: bytes) -> dict[str, dict[str, str]]:
    """``{run_id: {column: text}}`` from a body made by :func:`encode_bundle`."""
    try:
        raw = lzma.decompress(bytes(body), format=lzma.FORMAT_XZ)
    except lzma.LZMAError as exc:
        raise TraceBulkError(f"bundle body does not decompress: {exc}") from exc
    if raw[:4] != _MAGIC or len(raw) < 8:
        raise TraceBulkError("bundle body has no LTB1 header")
    (head_len,) = _HEADER_LEN.unpack(raw[4:8])
    base = 8 + head_len
    try:
        header = json.loads(raw[8:base].decode("utf-8"))
    except ValueError as exc:
        raise TraceBulkError(f"bundle header is not JSON: {exc}") from exc
    payload = raw[base:]
    out: dict[str, dict[str, str]] = {}
    for member in header.get("members", []):
        cols: dict[str, str] = {}
        for column, (start, length) in member["cols"].items():
            if start < 0 or start + length > len(payload):
                raise TraceBulkError(f"bundle span of {column} is out of range")
            cols[column] = payload[start:start + length].decode("utf-8")
        out[member["run_id"]] = cols
    return out


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------

_BUNDLES_SQL = """
SELECT bundle_id, codec, body
  FROM analyst_trace_bundles
 WHERE bundle_id = ANY($1::uuid[])
"""


async def _json_decoded(conn: Any) -> bool:
    """Whether this connection hands jsonb back as Python values."""
    probe = await conn.fetchval("SELECT '[]'::jsonb")
    return not isinstance(probe, str)


async def hydrate_traces(
    conn: Any,
    rows: Iterable[Mapping[str, Any]],
    *,
    aliases: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return ``rows`` as dicts with every compacted row's bulk put back.

    Each row must carry :data:`POINTER_COLUMNS` and ``run_id`` (a query that
    selects a bulk column without the pointers would hand back NULL for a
    compacted row, so a row without them is refused). ``aliases`` maps a bulk
    column to the key the query selected it under (``{"output_payload":
    "gold"}``). Only keys the query selected are filled. The pointer keys are
    removed from every returned row.
    """
    names = {c: (aliases or {}).get(c, c) for c in BULK_COLUMNS}
    out: list[dict[str, Any]] = []
    wanted: set[UUID] = set()
    for row in rows:
        d = dict(row)
        missing = [c for c in POINTER_COLUMNS if c not in d]
        if missing:
            raise TraceBulkError(
                f"trace row selected without {missing}: a compacted row would read as NULL"
            )
        if d["bulk_bundle_id"] is not None:
            if "run_id" not in d:
                raise TraceBulkError("a compacted trace row needs run_id to find its member")
            wanted.add(d["bulk_bundle_id"])
        out.append(d)

    if wanted:
        records = await conn.fetch(_BUNDLES_SQL, list(wanted))
        bundles: dict[Any, dict[str, dict[str, str]]] = {}
        for rec in records:
            if rec["codec"] != CODEC:
                raise TraceBulkError(f"bundle {rec['bundle_id']} has unknown codec {rec['codec']!r}")
            bundles[rec["bundle_id"]] = decode_bundle(rec["body"])
        as_python = await _json_decoded(conn)
        for d in out:
            bid = d["bulk_bundle_id"]
            if bid is None:
                continue
            bundle = bundles.get(bid)
            if bundle is None:
                raise TraceBulkError(f"bundle {bid} of run {d['run_id']} is missing")
            member = bundle.get(str(d["run_id"]))
            if member is None:
                raise TraceBulkError(f"run {d['run_id']} is not in bundle {bid}")
            moved = set(d["bulk_columns"] or ())
            if moved != set(member):
                raise TraceBulkError(
                    f"run {d['run_id']}: row says {sorted(moved)} moved, bundle holds {sorted(member)}"
                )
            for column in BULK_COLUMNS:
                key = names[column]
                text = member.get(column)
                if key in d:
                    if text is not None and column in JSON_COLUMNS and as_python:
                        d[key] = json.loads(text)
                    else:
                        d[key] = text
                size_key = f"{column}_bytes"
                if size_key in d:
                    d[size_key] = len(text.encode("utf-8")) if text is not None else None

    for d in out:
        for c in POINTER_COLUMNS:
            d.pop(c, None)
    return out
