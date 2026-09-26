# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``receipt_anchor`` sub-handler — H13's EXTERNAL timestamp on the receipt chain.

The receipt chain (``analyst_traces.receipt_hash`` / ``prev_receipt_hash``) is
tamper-evident INTERNALLY — a rewrite invalidates every successor — but nothing
inside the substrate can prove a head existed BEFORE a given moment. An external
timestamp closes that: once a day this handler

  * reads the LATEST ``receipt_hash`` per analyst (the chain heads, sorted by
    ``analyst_id`` — leaf = ``sha256(analyst_id || receipt_hash)``),
  * folds them into a single 32-byte Merkle root (duplicate-last padding on an
    odd level — the deterministic textbook fold, so the root is recomputable
    by anyone holding the same head set),
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

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "receipt_anchor"

#: The anchor contract revision — leaf shape, the duplicate-last fold, the
#: head-selection query and the calendar set. Bump when any of them changes so
#: a stored proof's semantics are legible without a deploy-date guess.
RECEIPT_ANCHOR_VERSION = "2026-09/h13"

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
    """Fold leaves into one 32-byte root: pairwise ``sha256(left + right)``,
    the odd tail DUPLICATED (the textbook convention — deterministic and
    recomputable). An empty head set anchors ``sha256('')`` — an honest empty
    day, never a skipped one."""
    if not leaves:
        return hashlib.sha256(b"").digest()
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            hashlib.sha256(level[i] + level[i + 1]).digest()
            for i in range(0, len(level), 2)
        ]
    return level[0]


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
        leaves = [
            leaf_for(str(r["analyst_id"]), str(r["receipt_hash"]))
            for r in heads
        ]
        root = merkle_root(leaves)
        root_hex = root.hex()
        for cal in CALENDARS:
            await conn.execute(_ENSURE_SQL, day_date, root_hex, len(leaves), cal)
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
    "CALENDARS",
    "STATUS_PENDING",
    "STATUS_SUBMITTED",
]
