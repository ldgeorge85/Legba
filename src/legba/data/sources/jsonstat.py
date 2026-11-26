# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A closed JSON-stat 2.0 reader for ``series_api``.

JSON-stat (https://json-stat.org/format/) publishes a CUBE: an ordered list of
dimension ids with their sizes, each dimension's categories (code → position),
and the values in row-major order over those dimensions — a dense array, or a
SPARSE object keyed by the flat cell index (missing cells are absent). An
optional ``status`` (one string for every cell, an array, or a sparse object)
carries the publisher's flags (Eurostat: ``p`` provisional, ``e`` estimated).

:func:`cube_rows` turns one dataset into ROWS: one row per present cell, whose
fields are every dimension id (the category CODE at that position), ``value``
and — when the cell has one — ``status``. The rest of ``series_api`` then
reads those rows exactly like JSON or CSV rows (``date_field`` names the time
dimension, ``subject_field`` the geography, ``where`` filters other
dimensions, ``status`` can be a provenance field).

CLOSED. Only ``class: dataset`` (version 2.0, the shape publishers serve as a
single response) is read; a bundle, a collection, a missing ``id``/``size``,
sizes that disagree with the categories, a dense value array of the wrong
length, or a sparse key outside the cube is a :class:`ValueError` — never a
best-effort read. A cube with more cells than the source declared
(``jsonstat_max_cells``) is refused before any row is built.

Pure: stdlib only, no network, no clock.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

__all__ = ["cube_rows", "VALUE_FIELD", "STATUS_FIELD"]

VALUE_FIELD = "value"
STATUS_FIELD = "status"


def _codes(dim_id: str, dim: Any, size: int) -> list[str]:
    """The category codes of one dimension in position order."""
    if not isinstance(dim, Mapping) or not isinstance(dim.get("category"), Mapping):
        raise ValueError(f"jsonstat: dimension {dim_id!r} has no category block")
    cat = dim["category"]
    index = cat.get("index")
    if index is None:
        # A single-category dimension may omit index; its one code is the label key.
        labels = cat.get("label")
        if size == 1 and isinstance(labels, Mapping) and len(labels) == 1:
            return [str(next(iter(labels)))]
        raise ValueError(f"jsonstat: dimension {dim_id!r} has no category index")
    if isinstance(index, list):
        codes = [str(c) for c in index]
    elif isinstance(index, Mapping):
        codes = [""] * len(index)
        for code, pos in index.items():
            if not isinstance(pos, int) or isinstance(pos, bool) or not 0 <= pos < len(index) or codes[pos]:
                raise ValueError(f"jsonstat: dimension {dim_id!r} index position {pos!r} is invalid")
            codes[pos] = str(code)
    else:
        raise ValueError(f"jsonstat: dimension {dim_id!r} index is neither an array nor an object")
    if len(codes) != size:
        raise ValueError(f"jsonstat: dimension {dim_id!r} has {len(codes)} categories, size says {size}")
    return codes


def _cells(raw: Any, total: int, name: str) -> dict[int, Any]:
    """``{flat index: cell}`` from a dense array or a sparse object."""
    if isinstance(raw, list):
        if len(raw) != total:
            raise ValueError(f"jsonstat: dense {name} has {len(raw)} cells, the cube has {total}")
        return {i: v for i, v in enumerate(raw) if v is not None}
    if isinstance(raw, Mapping):
        out: dict[int, Any] = {}
        for key, v in raw.items():
            try:
                i = int(key)
            except (TypeError, ValueError):
                raise ValueError(f"jsonstat: sparse {name} key {key!r} is not an index") from None
            if not 0 <= i < total:
                raise ValueError(f"jsonstat: sparse {name} key {i} is outside the cube ({total} cells)")
            if v is not None:
                out[i] = v
        return out
    raise ValueError(f"jsonstat: {name} is neither an array nor an object")


def cube_rows(doc: Any, *, max_cells: int) -> list[dict[str, Any]]:
    """One row per present cell of a JSON-stat 2.0 dataset (see the module
    docstring). Raises ``ValueError`` on anything else."""
    if not isinstance(doc, Mapping):
        raise ValueError("jsonstat: the document is not an object")
    if doc.get("class") != "dataset":
        hint = doc.get("error") or doc.get("class")
        raise ValueError(f"jsonstat: not a dataset (class {str(hint)[:200]!r})")
    if str(doc.get("version", "")) != "2.0":
        raise ValueError(f"jsonstat: version {doc.get('version')!r} is not 2.0")
    ids, sizes, dims = doc.get("id"), doc.get("size"), doc.get("dimension")
    if not (isinstance(ids, list) and isinstance(sizes, list) and isinstance(dims, Mapping)):
        raise ValueError("jsonstat: a dataset needs id, size and dimension")
    if len(ids) != len(sizes) or not ids:
        raise ValueError(f"jsonstat: {len(ids)} dimension ids vs {len(sizes)} sizes")
    if len(set(ids)) != len(ids) or any(i in (VALUE_FIELD, STATUS_FIELD) for i in ids):
        raise ValueError("jsonstat: dimension ids must be distinct and not 'value'/'status'")
    if not all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in sizes):
        raise ValueError("jsonstat: sizes must be non-negative integers")
    total = math.prod(sizes)
    if total > max_cells:
        raise ValueError(f"jsonstat: the cube has {total} cells, over this source's bound of {max_cells}")
    codes = [_codes(str(i), dims.get(i), n) for i, n in zip(ids, sizes)]
    values = _cells(doc.get("value"), total, "value")
    raw_status = doc.get("status")
    if raw_status is None:
        status: dict[int, Any] = {}
    elif isinstance(raw_status, str):
        status = {i: raw_status for i in range(total)}
    elif isinstance(raw_status, list) and len(raw_status) == 1 and total != 1:
        status = {i: raw_status[0] for i in range(total)}    # one flag for every cell
    else:
        status = _cells(raw_status, total, "status")
    rows: list[dict[str, Any]] = []
    for flat in sorted(values):
        row: dict[str, Any] = {}
        rem = flat
        for dim_id, dim_codes, n in zip(reversed(ids), reversed(codes), reversed(sizes)):
            row[str(dim_id)] = dim_codes[rem % n]
            rem //= n
        row = {str(i): row[str(i)] for i in ids}            # dimension order, then value/status
        row[VALUE_FIELD] = values[flat]
        if flat in status:
            row[STATUS_FIELD] = status[flat]
        rows.append(row)
    return rows
