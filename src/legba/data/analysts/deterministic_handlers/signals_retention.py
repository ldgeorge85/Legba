# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``signals_retention`` sub-handler — TTL purge of aged signals.

Graph-and-data Wave-1b, item 3. The ``signals`` table was an unpartitioned,
13-indexed, retention-free table that grew without bound. Per locked decision
D4 (REVIEW_CONSOLIDATED_2026-06-16) the release answer is a scheduled TTL
PURGE (not a range-partition — partitioning is heavy and the volume is small).
This sub-handler is that purge, run on the deterministic-analyst maintenance
cadence (mirrors ``entity_gc`` / ``fact_decay``).

WHAT IT DOES (one transaction per batch, idempotent, degrade-not-drop):
  * Selects signals whose ``fetched_at`` is older than ``ttl_days`` AND whose
    ``retention_class`` is purgeable (NOT in the keep-set ``retain_always`` /
    ``evidence_hold`` — those are held regardless of age), oldest-first,
    ``LIMIT batch_limit`` per run (bounds lock time at scale; supported by the
    composite index from migration 0036).
  * DELETEs the dependent, *value-referenced* child rows FIRST so nothing is
    orphaned — the substrate has no DB-level FK from these children to
    ``signals`` (baseline 0001), so the cleanup is explicit:
      - ``signal_entity_links`` where ``signal_id`` is purged,
      - ``signal_aliases`` where ``alias_signal_id`` OR ``canonical_signal_id``
        is purged.
  * DELETEs the signal rows.

NOT cleaned (by design): the ``derived_from uuid[]`` provenance arrays on
facts/nexuses/outputs may still carry a purged signal's UUID — those are
historical lineage pointers, and the recursive-CTE lineage walk simply
terminates at a missing source. A retention policy that refused to purge any
signal ever referenced by a fact would never reclaim space; D4 accepts the
dangling-pointer trade-off for this release.

A ``ttl_days <= 0`` (the DEFAULT) DISABLES the purge — the job is a no-op until
an operator sets a positive TTL on the descriptor's ``options``, so it can ship
inert and be turned on deliberately.

Output ``data`` keys:
    signals_purged      int — signal rows deleted this run
    entity_links_purged int — signal_entity_links rows deleted
    aliases_purged      int — signal_aliases rows deleted
    ttl_days            int — the effective TTL (0 = disabled)
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "signals_retention"

#: Disabled by default — ship inert, operator opts in with a positive TTL.
_DEFAULT_TTL_DAYS = 0
_DEFAULT_BATCH = 5_000

#: Retention classes that are NEVER purged regardless of age.
_KEEP_CLASSES = ("retain_always", "evidence_hold")


def _row_count(status: str | None) -> int:
    """Parse the trailing integer from an asyncpg command tag (``DELETE 7``)."""
    if not status:
        return 0
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):  # pragma: no cover - defensive
        return 0


async def _purge(pool: Any, *, ttl_days: int, batch_limit: int) -> dict[str, int]:
    """Purge one batch of aged signals + their value-referenced children.

    All deletes for a batch run in ONE transaction so a signal and its child
    rows are removed atomically (no window where a child is orphaned).
    """
    signals_purged = 0
    entity_links_purged = 0
    aliases_purged = 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            ids = [
                r["id"]
                for r in await conn.fetch(
                    """
                    SELECT id
                      FROM signals
                     WHERE fetched_at < NOW() - ($1::int * INTERVAL '1 day')
                       AND retention_class <> ALL($2::text[])
                     ORDER BY fetched_at ASC
                     LIMIT $3
                    """,
                    ttl_days,
                    list(_KEEP_CLASSES),
                    batch_limit,
                )
            ]
            if not ids:
                return {
                    "signals_purged": 0,
                    "entity_links_purged": 0,
                    "aliases_purged": 0,
                }

            # Children FIRST (no FK to signals — explicit cleanup so the purge
            # never orphans a link or alias).
            entity_links_purged = _row_count(
                await conn.execute(
                    "DELETE FROM signal_entity_links WHERE signal_id = ANY($1::uuid[])",
                    ids,
                )
            )
            aliases_purged = _row_count(
                await conn.execute(
                    "DELETE FROM signal_aliases "
                    "WHERE alias_signal_id = ANY($1::uuid[]) "
                    "   OR canonical_signal_id = ANY($1::uuid[])",
                    ids,
                )
            )
            signals_purged = _row_count(
                await conn.execute(
                    "DELETE FROM signals WHERE id = ANY($1::uuid[])", ids
                )
            )

    return {
        "signals_purged": signals_purged,
        "entity_links_purged": entity_links_purged,
        "aliases_purged": aliases_purged,
    }


def _build_finding(counters: Mapping[str, int], *, ttl_days: int) -> FindingPayload:
    sp = counters.get("signals_purged", 0)
    if ttl_days <= 0:
        title = "Signals retention: disabled (ttl_days<=0) — no purge"
    else:
        title = (
            f"Signals retention: purged {sp} signal(s) older than {ttl_days}d "
            f"({counters.get('entity_links_purged', 0)} links, "
            f"{counters.get('aliases_purged', 0)} aliases)"
        )
    body = "\n".join(f"{k}={v}" for k, v in counters.items())
    tags = ["deterministic", "signals_retention"]
    if sp:
        tags.append("signals_purged")
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={"sub_handler": SUB_HANDLER_NAME, "ttl_days": ttl_days, **dict(counters)},
    )


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    Sweeps the substrate directly via ``deps.pg_pool`` (the ``inputs`` slice is
    ignored — the unit of work is "all aged signals"). ``deps is None`` (unit
    path) or ``ttl_days <= 0`` (default) yields a zeroed, no-purge run.
    """
    counters: dict[str, int] = {
        "signals_purged": 0,
        "entity_links_purged": 0,
        "aliases_purged": 0,
    }
    ttl_days = int(options.get("ttl_days", _DEFAULT_TTL_DAYS))
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None and ttl_days > 0:
        batch_limit = int(options.get("batch_limit", _DEFAULT_BATCH))
        try:
            counters = await _purge(pool, ttl_days=ttl_days, batch_limit=batch_limit)
        except Exception as exc:
            logger.warning("signals_retention.failed err=%s", exc)

    return AnalystMethodResult(
        finding=_build_finding(counters, ttl_days=ttl_days),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle", "SUB_HANDLER_NAME"]
