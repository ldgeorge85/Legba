# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``corpus_retention`` sub-handler — the OpenSearch corpus DELETE path.

The mirror of :mod:`corpus_indexer`. That sweep projects signals INTO
``legba_signals_corpus``; this one takes them OUT when their row is gone. Until
2026-08-03 the second half did not exist in any form: ``OpenSearchStore`` had no
delete surface at all, so every signals purge in the platform's history left its
documents behind. Measured exhaustively that day, 75,871 of 182,648 corpus docs
(41.5%) referenced rows that no longer existed — the whole population being the
07-28 intra-source duplicate collapse, whose own script header had already
conceded the debt by dumping ``--ids-out`` "for an optional later sidecar
cleanup" that nobody built.

WHY A QUEUE AND NOT A DELETE AT THE DELETION SITE
-------------------------------------------------
Three call sites delete signals. Calling OpenSearch from inside each one puts a
fallible network round-trip inside a transaction that is currently atomic and
local — a timeout would either abort a good purge or commit it and lose the
delete, which is the orphan we are trying to prevent. So the deletion sites
record INTENT transactionally into ``corpus_tombstones`` (migration 0175) — the
INSERT and the DELETE land together or not at all — and this sweep drains that
queue with a bounded budget and a retry that costs nothing.

THE DRAIN NEVER TRUSTS THE TOMBSTONE
-------------------------------------
A tombstone is a CLAIM that a row is gone. This sweep re-verifies it at drain
time (``_SELECT_DRAINABLE_SQL`` anti-joins ``signals``) and refuses any doc whose
row is alive, counting it as ``skipped_row_alive``. That inverts the failure
mode: a mistaken, stale or hand-inserted tombstone cannot destroy a live
document, and the worst remaining outcome is an orphan that outlives its
tombstone — harmless, and re-queued by the next backfill. It is also what makes
the sweep safe to point at a queue somebody else wrote.

ORDERING AGAINST THE INDEXER
-----------------------------
``corpus_indexer`` only ever reads ``signals`` rows that exist, so it can never
re-create a doc this sweep deleted: the row is gone, the SELECT cannot return it.
The two sweeps therefore need no coordination and no shared lock.

Output ``data`` keys (the cadence receipt the operator reads):
    pending          int — drainable tombstones OUTSTANDING after this run
    examined         int — tombstones pulled this run
    deleted          int — docs confirmed removed from the corpus (404 counts:
                           already-absent IS the desired end state)
    failures         int — examined docs the bulk request could not delete
    skipped_row_alive int — tombstones whose signals row still EXISTS; left
                           queued and NOT deleted (see above). Should be 0;
                           anything else means somebody tombstoned a live row.
    max_attempts     int — highest retry count among the rows examined; a doc
                           OpenSearch keeps refusing shows up here rather than
                           cycling forever in silence
    skipped_no_store int — 1 when the OpenSearchStore was not wired (else 0)
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "corpus_retention"

#: The key under which the runtime stashes the connected OpenSearchStore on
#: ``StandardDeps.extras`` for this sweep (wired in analyst_deps_builder when the
#: bound sub-handler is ``corpus_retention``).
OS_DEPS_EXTRA_KEY = "corpus_retention_os"

#: Docs to delete per tick. Deleting is cheap (no LLM, one bulk request), but the
#: 75,871-doc backfill should drain over a few hours rather than in one request
#: that ties up the cluster — at the 15-minute cadence this clears it in ~5h.
_DEFAULT_BATCH = 2000

#: Give up re-trying a single doc after this many failed drains. The row stays in
#: the table (queryable forever, `last_error` intact) but stops being selected,
#: so one permanently-poisoned id cannot consume the budget every tick.
_MAX_ATTEMPTS = 5

#: The pending queue, oldest first, ANTI-JOINED against live signals so a doc
#: whose row still exists is never handed to the deleter. Uses the partial index
#: idx_corpus_tombstones_pending. $1 = batch limit, $2 = max attempts.
_SELECT_DRAINABLE_SQL = """
    SELECT t.doc_id, t.index_name, t.attempts,
           EXISTS (SELECT 1 FROM signals s WHERE s.id = t.doc_id) AS row_alive
      FROM corpus_tombstones t
     WHERE t.purged_at IS NULL
       AND t.attempts < $2
     ORDER BY t.created_at ASC
     LIMIT $1
"""

#: Stamp the drained ids. Only ids the bulk delete CONFIRMED are passed here.
_STAMP_PURGED_SQL = """
    UPDATE corpus_tombstones
       SET purged_at = now(), attempts = attempts + 1, last_error = NULL
     WHERE doc_id = ANY($1::uuid[])
"""

#: Bump the retry counter on ids the delete could not confirm; they stay queued.
_STAMP_FAILED_SQL = """
    UPDATE corpus_tombstones
       SET attempts = attempts + 1, last_error = $2
     WHERE doc_id = ANY($1::uuid[])
       AND purged_at IS NULL
"""

#: What is still outstanding after this run — the number the S-1 backlog gauge
#: reads independently, reported here so the receipt and the gauge agree.
_PENDING_SQL = """
    SELECT count(*)::int AS pending
      FROM corpus_tombstones t
     WHERE t.purged_at IS NULL
       AND t.attempts < $1
       AND NOT EXISTS (SELECT 1 FROM signals s WHERE s.id = t.doc_id)
"""


def _resolve_store(deps: Any | None) -> Any | None:
    """Pull the connected OpenSearchStore off ``deps.extras`` (or ``None``).

    Injected by :func:`legba.runtime.analyst_deps_builder._wire_corpus_indexer_os`
    when the bound sub-handler is ``corpus_retention``. Absent → the sweep
    no-ops that tick, leaving the queue intact for a tick where the store IS
    wired."""
    if deps is None:
        return None
    extras = getattr(deps, "extras", None)
    if not isinstance(extras, Mapping):
        return None
    return extras.get(OS_DEPS_EXTRA_KEY)


async def _drain_batch(
    pool: Any, *, store: Any, default_index: str, batch_limit: int
) -> dict[str, int]:
    """Delete one batch of tombstoned docs from the corpus.

    The connection is NOT held across the bulk request. A transport failure in
    ``bulk_delete`` RAISES to the caller BEFORE any stamp, so the batch stays
    queued and is retried next tick (deleting an already-absent doc is a success,
    which is what makes the retry idempotent)."""
    counters = {
        "pending": 0,
        "examined": 0,
        "deleted": 0,
        "failures": 0,
        "skipped_row_alive": 0,
        "max_attempts": 0,
        "skipped_no_store": 0,
    }

    async with pool.acquire() as conn:
        rows = await conn.fetch(_SELECT_DRAINABLE_SQL, batch_limit, _MAX_ATTEMPTS)

    # The safety gate: never delete a doc whose substrate row is still there.
    alive = [r for r in rows if r["row_alive"]]
    drainable = [r for r in rows if not r["row_alive"]]
    counters["skipped_row_alive"] = len(alive)
    if alive:
        logger.warning(
            "corpus_retention.row_alive n=%d — tombstones whose signals row still "
            "EXISTS were skipped, not deleted. Somebody tombstoned a live row; "
            "the queue is wrong, the corpus is not.",
            len(alive),
        )
    counters["examined"] = len(rows)
    counters["max_attempts"] = max((int(r["attempts"]) for r in rows), default=0)

    if drainable:
        by_index: dict[str, list[str]] = {}
        for r in drainable:
            by_index.setdefault(r["index_name"] or default_index, []).append(
                str(r["doc_id"])
            )

        confirmed: list[Any] = []
        failed: list[Any] = []
        for index, ids in by_index.items():
            deleted = int(await store.bulk_delete(index, ids))
            # bulk_delete returns a COUNT, not a per-id result. When every id in
            # the request landed we can stamp them all; a short count means some
            # subset failed and we cannot tell which, so the whole request is
            # re-queued. The retry is free (an absent doc deletes successfully),
            # and the attempts counter bounds it.
            group = [r for r in drainable if (r["index_name"] or default_index) == index]
            if deleted >= len(ids):
                confirmed.extend(group)
            else:
                failed.extend(group)
            counters["deleted"] += deleted

        # Clamped: a store that over-reports must not drive `failures` negative
        # and turn a counter the operator reads into nonsense.
        counters["failures"] = max(0, len(drainable) - counters["deleted"])

        async with pool.acquire() as conn:
            if confirmed:
                await conn.execute(
                    _STAMP_PURGED_SQL, [r["doc_id"] for r in confirmed]
                )
            if failed:
                await conn.execute(
                    _STAMP_FAILED_SQL,
                    [r["doc_id"] for r in failed],
                    "bulk_delete returned a short count for this batch",
                )

    async with pool.acquire() as conn:
        counters["pending"] = int(
            await conn.fetchval(_PENDING_SQL, _MAX_ATTEMPTS) or 0
        )
    return counters


def _build_finding(counters: Mapping[str, int]) -> FindingPayload:
    title = (
        f"Corpus retention: deleted {counters.get('deleted', 0)} orphaned doc(s), "
        f"{counters.get('pending', 0)} still queued"
    )
    body = "\n".join(f"{k}={v}" for k, v in counters.items())
    tags = ["deterministic", "corpus_retention"]
    if counters.get("deleted", 0):
        tags.append("purged")
    if counters.get("skipped_row_alive", 0):
        tags.append("row_alive")
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={"sub_handler": SUB_HANDLER_NAME, **dict(counters)},
    )


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    Sweeps the substrate directly via ``deps.pg_pool`` (the ``inputs`` slice is
    ignored — the unit of work is "the next batch of tombstoned docs").
    ``deps is None`` (unit-test path) yields a zeroed run. Usage is always zeroed
    (deterministic kind, no LLM)."""
    counters: dict[str, int] = {
        "pending": 0,
        "examined": 0,
        "deleted": 0,
        "failures": 0,
        "skipped_row_alive": 0,
        "max_attempts": 0,
        "skipped_no_store": 0,
    }
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None:
        store = _resolve_store(deps)
        if store is None:
            counters["skipped_no_store"] = 1
            logger.warning(
                "corpus_retention.no_store — OpenSearchStore absent from "
                "deps.extras[%r]; the delete plane did not wire (dep missing / OS "
                "unreachable). Tombstones left queued this tick.",
                OS_DEPS_EXTRA_KEY,
            )
        else:
            batch_limit = int(options.get("batch_limit", _DEFAULT_BATCH))
            try:
                counters = await _drain_batch(
                    pool,
                    store=store,
                    default_index=store.cfg.index,
                    batch_limit=batch_limit,
                )
            except Exception as exc:
                logger.warning("corpus_retention.failed err=%s", exc)

    return AnalystMethodResult(
        finding=_build_finding(counters),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle", "SUB_HANDLER_NAME", "OS_DEPS_EXTRA_KEY"]
