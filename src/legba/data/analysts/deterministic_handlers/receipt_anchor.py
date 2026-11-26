# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``receipt_anchor`` sub-handler — H13's EXTERNAL timestamp on the receipt chain.

The receipt chain (``analyst_traces.receipt_hash`` / ``prev_receipt_hash``) is
tamper-evident INTERNALLY — a rewrite invalidates every successor — but nothing
inside the substrate can prove a head existed BEFORE a given moment. An external
timestamp closes that: once a day this handler

  * reads the LATEST ``receipt_hash`` per analyst (the chain heads, sorted by
    ``analyst_id`` — leaf = ``sha256(analyst_id || receipt_hash)``), then
    every forecast-ledger hash (migration 0242: questions, numbers and
    resolutions, sorted by kind then id — leaf =
    ``sha256(kind ':' id || content_sha256)``, ``forecast_ledger.ledger_leaf``),
    so a forecast number is externally timestamped from the night after it
    was stated, then ONE leaf for the administrative audit
    log's chain head (migration 0248: leaf =
    ``ledger_leaf("descriptor_audit_log", str(chain_seq), row_hash)``), so an
    outside witness covers administrative history too,
  * folds them into a single 32-byte Merkle root (duplicate-last padding on an
    odd level — the deterministic textbook fold, so the root is recomputable
    by anyone holding the same head set; the fold lives in
    :mod:`legba.data.anchor_proof`, shared with the inclusion proof),
  * F-1 (migration 0262): stores the ORDERED LEAVES of the tree the day
    anchors (``receipt_anchor_trees`` / ``receipt_anchor_leaves``) — only on
    the tick whose root IS the day's anchored root — so an inclusion proof
    for any one leaf (a forecast seal among them) can be served
    (``GET /v3/ledger/seal-proof``). Days anchored before 0262 have no stored
    leaves and are never back-filled or rewritten,
  * POSTs the digest to two OpenTimestamps public calendars (raw body; the
    response bytes ARE the proof), and
  * lands one ``receipt_anchors`` row per (day, calendar): ``status='submitted'``
    with the proof, or ``'pending'`` when the calendar was unreachable — a
    pending row is retried on the NEXT tick (a day-old root is still the true
    attestation for that day), never an error that stops the run.

``GET /api/v1/v3/system/receipt-anchors`` lists the ledger (registry-slim
``receipt_anchors_api``); verification is deliberately NOT built in-tree —
``docs/ANALYSIS.md`` §measurement carries the offline ``ots`` recipe. The
returned :class:`FindingPayload` is a per-run RECEIPT (counts only), marked
``TRACE_ONLY`` in :mod:`legba.data.analysts.deterministic`.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Mapping

import httpx

from ...anchor_proof import HEAD_KIND, AnchorLeaf, leaves_from_rows, store_tree
from ...anchor_proof import merkle_root as _fold
from ...forecast_ledger import LEDGER_LEAVES_SQL, ledger_leaf
from ...forecast_resolution import RECORD_LEAVES_SQL
from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "receipt_anchor"

#: The anchor contract revision — leaf shape, the duplicate-last fold, the
#: head-selection query and the calendar set. Bump when any of them changes so
#: a stored proof's semantics are legible without a deploy-date guess.
#: 2026-11/st0: the forecast-ledger leaves follow the chain heads.
#: 2026-11/audit-chain: the audit-log chain head is the LAST leaf.
#: 2026-11/f1-leaves: the same tree (leaf shape, order and fold unchanged);
#: its ordered leaves are stored beside the root (migration 0262).
RECEIPT_ANCHOR_VERSION = "2026-11/f1-leaves"

#: The audit-log leaf's kind (``ledger_leaf(kind, id, hash)``).
AUDIT_CHAIN_LEAF_KIND = "descriptor_audit_log"

#: The two public OpenTimestamps calendars the digest is POSTed to (raw body;
#: the response bytes are the proof). Two calendars so one pool's downtime
#: cannot silently strand a day's anchor.
CALENDARS: tuple[str, ...] = (
    "https://a.pool.opentimestamps.org/digest",
    "https://b.pool.opentimestamps.org/digest",
)

#: Status vocabulary on receipt_anchors.
STATUS_PENDING = "pending"
STATUS_SUBMITTED = "submitted"

#: Per-calendar POST budget — the anchor must never hang a cadence tick.
_OTS_TIMEOUT_S = 15.0

#: The chain heads: the LATEST receipt_hash per analyst. ``DISTINCT ON`` +
#: ``ORDER BY analyst_id`` is the leaf ordering the Merkle fold depends on —
#: the same set in the same order always yields the same root.
_HEADS_SQL = """
SELECT DISTINCT ON (analyst_id) analyst_id, receipt_hash
  FROM analyst_traces
 ORDER BY analyst_id, run_started_at DESC, run_id DESC
"""

#: Every (day, calendar) slot exists exactly once — ON CONFLICT makes a re-run
#: the same day idempotent (the day's root is pinned by the first write).
_ENSURE_SQL = """
INSERT INTO receipt_anchors (day, root_hash, leaf_count, calendar, status)
VALUES ($1::date, $2, $3, $4, 'pending')
ON CONFLICT (day, calendar) DO NOTHING
"""

#: Pending backlog — today's fresh rows plus every older day still awaiting a
#: calendar that was down. Retried every tick until a calendar answers.
_PENDING_SQL = """
SELECT id::text AS id, day::text AS day, root_hash, calendar
  FROM receipt_anchors
 WHERE status = 'pending'
 ORDER BY day, calendar
"""

_SUBMIT_SQL = """
UPDATE receipt_anchors
   SET status = 'submitted', proof = $2::bytea, submitted_at = now()
 WHERE id = $1::uuid
"""


# ---------------------------------------------------------------------------
# The pure fold — deterministic, testable without a DB or a calendar
# ---------------------------------------------------------------------------


def leaf_for(analyst_id: str, receipt_hash: str) -> bytes:
    """One Merkle leaf = ``sha256(analyst_id || receipt_hash)`` — the lane's
    exact byte recipe (text concatenation, UTF-8)."""
    return hashlib.sha256(f"{analyst_id}{receipt_hash}".encode("utf-8")).digest()


def merkle_root(leaves: list[bytes]) -> bytes:
    """Fold leaves into one 32-byte root — :func:`legba.data.anchor_proof.
    merkle_root` (pairwise ``sha256(left + right)``, the odd tail DUPLICATED;
    an empty head set anchors ``sha256('')``). One fold for the anchor and
    the inclusion proof."""
    return _fold(leaves)


async def _ledger_leaf_rows(conn: Any, warnings: list[str]) -> list[AnchorLeaf]:
    """The forecast ledger's leaves, in the fixed (kind, id) order. A ledger
    that cannot be read (migration 0242 not applied yet) anchors the chain
    heads alone and says so in the receipt — never a failed run."""
    try:
        rows = await conn.fetch(LEDGER_LEAVES_SQL)
    except Exception as exc:  # noqa: BLE001 — heads-only root, named
        warnings.append(f"receipt_anchor.ledger_unavailable:{type(exc).__name__}")
        return []
    leaves = leaves_from_rows(rows)
    # The resolution records (0244) fold after the 0242 leaves, read on
    # their own so a missing 0244 never drops the ledger's leaves.
    try:
        recs = await conn.fetch(RECORD_LEAVES_SQL)
    except Exception as exc:  # noqa: BLE001 — named, never a failed run
        warnings.append(f"receipt_anchor.resolution_records_unavailable:{type(exc).__name__}")
        recs = []
    return leaves + leaves_from_rows(recs)


async def _ledger_leaves(conn: Any, warnings: list[str]) -> list[bytes]:
    """The forecast ledger's leaf digests (see :func:`_ledger_leaf_rows`)."""
    return [lf.digest for lf in await _ledger_leaf_rows(conn, warnings)]


async def _audit_chain_head(conn: Any, warnings: list[str]) -> tuple[int, str] | None:
    """The audit-log chain head ``(chain_seq, row_hash)`` — ``None`` before
    migration 0248 or on an empty chain, said in the receipt; never a failed
    run."""
    from ...registry.audit_chain import chain_head

    try:
        head = await chain_head(conn)
    except Exception as exc:  # noqa: BLE001 — named, never a failed run
        warnings.append(f"receipt_anchor.audit_chain_unavailable:{type(exc).__name__}")
        return None
    if head is None:
        warnings.append("receipt_anchor.audit_chain_empty")
    return head


def audit_chain_leaf(chain_seq: int, row_hash: str) -> bytes:
    """The audit-log head's leaf: ``ledger_leaf(AUDIT_CHAIN_LEAF_KIND,
    str(chain_seq), row_hash)``."""
    return ledger_leaf(AUDIT_CHAIN_LEAF_KIND, str(chain_seq), row_hash)


async def _submit_digest(calendar: str, digest: bytes) -> bytes:
    """POST the raw 32-byte digest to a calendar; the response bytes are the
    proof. Raises on transport failure / non-2xx — the caller marks 'pending'."""
    async with httpx.AsyncClient(timeout=_OTS_TIMEOUT_S) as client:
        resp = await client.post(calendar, content=digest)
        resp.raise_for_status()
        return resp.content


# ---------------------------------------------------------------------------
# Receipt assembly (pure)
# ---------------------------------------------------------------------------


def build_receipt(
    *,
    day: str,
    leaf_count: int,
    root_hash: str,
    submitted: int,
    pending: int,
    warnings: list[str],
    ledger_leaf_count: int = 0,
    audit_chain_head: tuple[int, str] | None = None,
    leaves_stored: str | None = None,
) -> FindingPayload:
    """The per-run TRACE_ONLY receipt — the day, the root, and per-calendar
    outcomes. Counts only: the proof bytes live in ``receipt_anchors``, never
    in a finding body."""
    head = (
        f"Receipt anchor {day}: root={root_hash[:16]}… leaves={leaf_count} "
        f"(submitted={submitted}, pending={pending})"
    )
    return FindingPayload(
        title=head[:2048],
        body=(
            f"day={day}\nroot_hash={root_hash}\nleaf_count={leaf_count}\n"
            f"submitted={submitted}\npending={pending}\n"
            f"calendars={list(CALENDARS)}\nwarnings={warnings}\n"
        )[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", SUB_HANDLER_NAME],
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "receipt_anchor_version": RECEIPT_ANCHOR_VERSION,
            "day": day,
            "root_hash": root_hash,
            "leaf_count": int(leaf_count),
            "ledger_leaf_count": int(ledger_leaf_count),
            # The anchored audit head, so a holder of this receipt and the
            # day's proof can check the chain still has this hash at this seq.
            "audit_chain_head": (
                {"chain_seq": audit_chain_head[0], "row_hash": audit_chain_head[1]}
                if audit_chain_head else None
            ),
            # F-1: whether this tick stored the day's ordered leaves
            # (stored | already_stored | not_the_anchored_root | unavailable).
            "leaves_stored": leaves_stored,
            "anchors_submitted": int(submitted),
            "anchors_pending": int(pending),
            "calendars": list(CALENDARS),
            "warnings": warnings,
        },
    )


# ---------------------------------------------------------------------------
# Public handler entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    A META single global run (no ``targets`` selector). ``deps``/pool of None
    degrades to an honest empty receipt; a calendar that is unreachable leaves
    its row ``pending`` for the next tick — neither is a run failure.
    """
    warnings: list[str] = []
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    # The day is a ``date`` on the wire and a string in the receipt. asyncpg
    # binds ``$1::date`` from a ``datetime.date`` and refuses a str with
    # "'str' object has no attribute 'toordinal'" — which is exactly how the
    # first live tick (2026-09-25 00:10Z) hard-failed: the fake connection in
    # the tests accepted the string the real driver never would.
    day_date = datetime.now(timezone.utc).date()
    day = day_date.isoformat()
    if pool is None:
        warnings.append("receipt_anchor.no_pool")
        return AnalystMethodResult(
            finding=build_receipt(
                day=day, leaf_count=0, root_hash="", submitted=0,
                pending=0, warnings=warnings,
            ),
            usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        )

    # Compute today's root + ensure the (day, calendar) rows exist, then lift
    # the pending set — all INSIDE one short connection checkout; the network
    # calls below happen with NO connection held.
    async with pool.acquire() as conn:
        heads = await conn.fetch(_HEADS_SQL)
        tree = [
            AnchorLeaf(HEAD_KIND, str(r["analyst_id"]), str(r["receipt_hash"]))
            for r in heads
        ]
        ledger_rows = await _ledger_leaf_rows(conn, warnings)
        tree += ledger_rows
        audit_head = await _audit_chain_head(conn, warnings)
        if audit_head is not None:
            tree.append(AnchorLeaf(AUDIT_CHAIN_LEAF_KIND, str(audit_head[0]), audit_head[1]))
        leaves = [lf.digest for lf in tree]
        root = merkle_root(leaves)
        root_hex = root.hex()
        for cal in CALENDARS:
            await conn.execute(_ENSURE_SQL, day_date, root_hex, len(leaves), cal)
        # F-1: the ordered leaves of the tree this day anchors, when this
        # tick's root IS the anchored one. A missing 0262 is named, never a
        # failed run (the anchor itself stands without its leaves).
        try:
            leaves_stored = await store_tree(
                conn, day_date, root_hex, tree, anchor_version=RECEIPT_ANCHOR_VERSION,
            )
        except Exception as exc:  # noqa: BLE001 — named in the receipt
            leaves_stored = "unavailable"
            warnings.append(f"receipt_anchor.leaves_unavailable:{type(exc).__name__}")
        pending_rows = await conn.fetch(_PENDING_SQL)

    submitted = 0
    pending = 0
    for row in pending_rows:
        try:
            proof = await _submit_digest(
                str(row["calendar"]), bytes.fromhex(str(row["root_hash"]))
            )
        except Exception as exc:  # noqa: BLE001 — pending, retried next tick
            pending += 1
            logger.info(
                "receipt_anchor.calendar_pending cal=%s day=%s err_class=%s",
                row["calendar"], row["day"], type(exc).__name__,
            )
            continue
        async with pool.acquire() as conn:
            await conn.execute(_SUBMIT_SQL, row["id"], proof)
        submitted += 1

    logger.info(
        "receipt_anchor.tick day=%s leaves=%d submitted=%d pending=%d",
        day, len(leaves), submitted, pending,
    )
    return AnalystMethodResult(
        finding=build_receipt(
            day=day,
            leaf_count=len(leaves),
            root_hash=root_hex,
            submitted=submitted,
            pending=pending,
            warnings=warnings,
            ledger_leaf_count=len(ledger_rows),
            audit_chain_head=audit_head,
            leaves_stored=leaves_stored,
        ),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = [
    "handle",
    "build_receipt",
    "leaf_for",
    "merkle_root",
    "SUB_HANDLER_NAME",
    "RECEIPT_ANCHOR_VERSION",
    "AUDIT_CHAIN_LEAF_KIND",
    "audit_chain_leaf",
    "CALENDARS",
    "STATUS_PENDING",
    "STATUS_SUBMITTED",
]
