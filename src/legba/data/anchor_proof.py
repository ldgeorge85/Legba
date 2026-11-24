# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The daily anchor's Merkle tree, its stored leaves, and inclusion proofs (F-1).

``receipt_anchor`` folds the receipt-chain heads, every forecast-ledger hash
and the audit-chain head into ONE root a day and timestamps the root outside
(OpenTimestamps). The root alone proves nothing about one leaf. This module:

  * holds THE fold (:func:`merkle_root`: pairwise ``sha256(left || right)``
    over the raw 32-byte digests, the odd tail DUPLICATED, an empty set
    anchoring ``sha256(b'')``) — the handler folds with it, so the proof and
    the anchor can never disagree about the tree;
  * the leaf recipe per kind (:func:`leaf_input`): a chain head is
    ``sha256(analyst_id || receipt_hash)``, every other leaf is
    ``sha256(kind ':' ref || content_hash)`` (``forecast_ledger.ledger_leaf``);
  * :func:`store_tree` — migration 0262: the ordered leaves of the tree a
    day ACTUALLY anchored (only when the day's ``receipt_anchors`` root is
    this root; a later tick the same day folds a different head set and
    stores nothing). Never rewrites a stored day;
  * :func:`inclusion_proof` — for one leaf, the first stored day holding it
    (or a named day), its position, the sibling path, the root, and the
    day's external-timestamp rows as the system knows them. The server does
    not verify an OpenTimestamps proof; it says so.

Verifying a path (:func:`fold_path`): start from the leaf; for each step,
``sha256(sibling || node)`` when the sibling is on the LEFT, else
``sha256(node || sibling)``; the result must equal the root.

LEAF: stdlib + ``forecast_ledger`` (stdlib) only — the registry-slim proof
route imports this.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from .forecast_ledger import ledger_leaf

__all__ = [
    "HEAD_KIND",
    "AUDIT_KIND",
    "AnchorLeaf",
    "fold_path",
    "inclusion_proof",
    "leaf_input",
    "merkle_path",
    "merkle_root",
    "store_tree",
]

#: The kind of a receipt-chain head leaf (one per analyst).
HEAD_KIND = "analyst_head"
#: The kind of the audit-log chain head leaf (``receipt_anchor.AUDIT_CHAIN_LEAF_KIND``).
AUDIT_KIND = "descriptor_audit_log"

#: What the system says about the outside timestamp: it submits the root and
#: stores the calendar's answer; it never verifies that answer itself.
EXTERNAL_TIMESTAMP_NOTE = (
    "The root was submitted to the public timestamp calendars listed; this "
    "system stores their answers and does not verify them. Check the stored "
    "proof offline with the OpenTimestamps client."
)


def _sha(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def leaf_input(kind: str, ref: str, content_hash: str) -> str:
    """The text a leaf hashes: ``ref || content`` for a chain head, else
    ``kind ':' ref || content``."""
    if kind == HEAD_KIND:
        return f"{ref}{content_hash}"
    return f"{kind}:{ref}{content_hash}"


@dataclass(frozen=True)
class AnchorLeaf:
    """One leaf of a day's tree and what it binds."""

    kind: str
    ref: str
    content_hash: str

    @property
    def digest(self) -> bytes:
        if self.kind == HEAD_KIND:
            return _sha(leaf_input(self.kind, self.ref, self.content_hash).encode("utf-8"))
        return ledger_leaf(self.kind, self.ref, self.content_hash)


def merkle_root(leaves: Sequence[bytes]) -> bytes:
    """Fold leaves into one 32-byte root: pairwise ``sha256(left + right)``,
    the odd tail DUPLICATED (deterministic, recomputable). An empty set
    anchors ``sha256(b'')`` — an honest empty day, never a skipped one."""
    if not leaves:
        return _sha(b"")
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [_sha(level[i] + level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def merkle_path(leaves: Sequence[bytes], index: int) -> list[tuple[str, bytes]]:
    """The sibling path of ``leaves[index]`` under :func:`merkle_root`'s fold:
    ``[(side, sibling), …]`` from the leaf up, ``side`` the sibling's side
    (``left`` | ``right``). A node that is the duplicated odd tail is its own
    right sibling."""
    if not 0 <= index < len(leaves):
        raise IndexError(f"leaf index {index} outside 0..{len(leaves) - 1}")
    path: list[tuple[str, bytes]] = []
    level = list(leaves)
    i = index
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        if i % 2:
            path.append(("left", level[i - 1]))
        else:
            path.append(("right", level[i + 1]))
        level = [_sha(level[j] + level[j + 1]) for j in range(0, len(level), 2)]
        i //= 2
    return path


def fold_path(leaf: bytes, path: Iterable[tuple[str, bytes]]) -> bytes:
    """The root a leaf and its sibling path fold to."""
    node = leaf
    for side, sib in path:
        node = _sha(sib + node) if side == "left" else _sha(node + sib)
    return node


# ---------------------------------------------------------------------------
# Stored trees (migration 0262)
# ---------------------------------------------------------------------------

ANCHORED_ROOT_SQL = """
SELECT root_hash FROM receipt_anchors WHERE day = $1::date ORDER BY created_at, calendar LIMIT 1
"""
INSERT_TREE_SQL = """
INSERT INTO receipt_anchor_trees (day, root_hash, leaf_count, anchor_version)
VALUES ($1::date, $2, $3, $4)
ON CONFLICT (day) DO NOTHING
RETURNING day
"""
INSERT_LEAVES_SQL = """
INSERT INTO receipt_anchor_leaves (day, position, leaf_hash, kind, ref, content_hash)
SELECT $1::date, t.p, t.l, t.k, t.r, t.c
  FROM unnest($2::int[], $3::text[], $4::text[], $5::text[], $6::text[]) AS t(p, l, k, r, c)
"""
#: The first stored day whose tree holds this leaf ($2 NULL), or that day ($2 set).
TREE_FOR_LEAF_SQL = """
SELECT l.day, l.position, t.root_hash, t.leaf_count, t.anchor_version, t.recorded_at
  FROM receipt_anchor_leaves l
  JOIN receipt_anchor_trees t ON t.day = l.day
 WHERE l.leaf_hash = $1 AND ($2::date IS NULL OR l.day = $2::date)
 ORDER BY l.day, l.position
 LIMIT 1
"""
LEAVES_FOR_DAY_SQL = """
SELECT position, leaf_hash FROM receipt_anchor_leaves WHERE day = $1::date ORDER BY position
"""
ANCHOR_ROWS_FOR_DAY_SQL = """
SELECT calendar, status, root_hash, submitted_at, created_at, (proof IS NOT NULL) AS has_proof
  FROM receipt_anchors WHERE day = $1::date ORDER BY calendar
"""
FIRST_STORED_DAY_SQL = "SELECT min(day) AS day FROM receipt_anchor_trees"


async def store_tree(conn: Any, day: date, root_hex: str, leaves: Sequence[AnchorLeaf],
                     *, anchor_version: str) -> str:
    """Store the ordered leaves of the tree ``day`` anchored. Returns
    ``stored``, ``already_stored`` (the day's tree is on record — never
    rewritten) or ``not_the_anchored_root`` (the day's anchor holds a root
    folded by an earlier tick: these leaves are not that tree)."""
    anchored = await conn.fetchval(ANCHORED_ROOT_SQL, day)
    if anchored != root_hex:
        return "not_the_anchored_root"
    async with conn.transaction():
        if await conn.fetchrow(INSERT_TREE_SQL, day, root_hex, len(leaves), anchor_version) is None:
            return "already_stored"
        if leaves:
            await conn.execute(
                INSERT_LEAVES_SQL, day, list(range(len(leaves))),
                [lf.digest.hex() for lf in leaves], [lf.kind for lf in leaves],
                [lf.ref for lf in leaves], [lf.content_hash for lf in leaves],
            )
    return "stored"


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    iso = getattr(v, "isoformat", None)
    return iso() if callable(iso) else str(v)


async def inclusion_proof(conn: Any, leaf_hex: str, *, day: date | None = None,
                          cache: dict[Any, Any] | None = None) -> dict[str, Any]:
    """The proof that ``leaf_hex`` is in a stored anchored tree.

    ``included: false`` names the first stored day (``None`` when no tree is
    stored yet) so the caller can say why. When included: ``day``,
    ``position``, ``leaf_count``, ``path`` (``[{side, hash}]`` from the leaf
    up), ``root``, the tree's ``anchor_version``, ``recomputed`` (the server
    re-folded the stored leaves to the stored root), ``anchor_root_matches``
    (the day's ``receipt_anchors`` root is this root) and ``timestamps`` — the
    day's calendar rows as the system knows them, never verified here."""
    leaf_hex = leaf_hex.lower()
    hit = await conn.fetchrow(TREE_FOR_LEAF_SQL, leaf_hex, day)
    if hit is None:
        first = await conn.fetchval(FIRST_STORED_DAY_SQL)
        return {"included": False, "leaf": leaf_hex, "first_stored_day": _iso(first)}
    if cache is not None and hit["day"] in cache:
        rows = cache[hit["day"]]
    else:
        rows = list(await conn.fetch(LEAVES_FOR_DAY_SQL, hit["day"]))
        if cache is not None:
            cache[hit["day"]] = rows
    hexes = [str(r["leaf_hash"]) for r in rows]
    digests = [bytes.fromhex(h) for h in hexes]
    pos = int(hit["position"])
    root = str(hit["root_hash"])
    recomputed = (
        len(digests) == int(hit["leaf_count"])
        and [int(r["position"]) for r in rows] == list(range(len(rows)))
        and merkle_root(digests).hex() == root
    )
    path = merkle_path(digests, pos) if 0 <= pos < len(digests) else []
    anchors = [dict(r) for r in await conn.fetch(ANCHOR_ROWS_FOR_DAY_SQL, hit["day"])]
    return {
        "included": True,
        "leaf": leaf_hex,
        "day": _iso(hit["day"]),
        "position": pos,
        "leaf_count": int(hit["leaf_count"]),
        "path": [{"side": side, "hash": sib.hex()} for side, sib in path],
        "root": root,
        "anchor_version": hit["anchor_version"],
        "tree_recorded_at": _iso(hit["recorded_at"]),
        "recomputed": bool(recomputed),
        "anchor_root_matches": bool(anchors) and all(a["root_hash"] == root for a in anchors),
        "timestamps": [
            {"calendar": a["calendar"], "status": a["status"], "submitted_at": _iso(a["submitted_at"]),
             "has_proof": bool(a["has_proof"]), "verified_here": False}
            for a in anchors
        ],
        "external_timestamp_note": EXTERNAL_TIMESTAMP_NOTE,
    }


def leaves_from_rows(rows: Iterable[Mapping[str, Any]]) -> list[AnchorLeaf]:
    """``(kind, id, content_sha256)`` ledger rows as anchor leaves."""
    return [AnchorLeaf(str(r["kind"]), str(r["id"]), str(r["content_sha256"])) for r in rows]
