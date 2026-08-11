# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``retention_sweep`` — C2 "one janitor" shared TTL-purge engine.

The 2026-07-28 coherence audit named the janitor cluster (``signals_retention``,
``analyst_traces_retention`` — whose own header says it "mirrors
signals_retention exactly" — plus ``nexus_decay``'s prune step and the unbuilt
archive retention) as the clearest case of the system growing N deterministic
organs where one archetype would do. This module is that one archetype for
the TTL-*purge* shape (age-ordered ``DELETE ... LIMIT batch``, keep-class
exemption, disabled-by-default): a single engine that executes a
``retention_policies`` config row (migration 0109) instead of each target
hand-rolling its own TTL constant / env-var name / batch default in Python.

MIGRATED (byte-identical behavior; see the now-delegating shim modules):
    * ``signals_retention``        (migration 0036)
    * ``analyst_traces_retention`` (migration 0101)

NOT migrated here (deliberately — see migration 0109's header for the full
rationale): ``nexus_decay``'s prune step is a confidence-DECAY stamp, never a
DELETE, so it doesn't fit this engine's shape as-is; archive retention is a
declared, unbuilt seam (docs/SEAMS.md) with no TTL concept yet at all. Neither
schema nor this engine precludes folding either in later.

CONTRACT PRESERVED FROM THE STANDALONE HANDLERS (do not regress):
    * TTL resolution order: ``options["ttl_days"]`` ALWAYS wins > the policy's
      ``env_fallback_var`` env var > the policy row's configured ``ttl_days``
      default (0 = disabled). X-1 note: ``options`` now carries the
      descriptor's ``method.options`` block on a plain CADENCE fire (the
      runtime merges it), so a descriptor-declared ``ttl_days`` reaches this
      engine through the FIRST rung — a forced run is no longer required, and
      the env var is no longer the only knob reachable in production.
    * ``ttl_days <= 0`` (the shipped default for every seeded policy) is a
      total no-op: the pool is never touched. Deleting substrate data is an
      operator decision.
    * ``deps is None`` (the unit-test path used throughout this codebase) is
      a zeroed, honest run that resolves TTL from options/env WITHOUT any
      database access — it falls back to each policy's built-in defaults
      (:data:`_POLICIES`), which mirror the migration 0109 seed rows exactly.
    * When a live pool IS available, the policy row is read fresh every run
      (an operator's edit to ``retention_policies`` — ttl_days, keep_classes,
      batch_size, enabled — takes effect on the next tick with no redeploy).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)


def _row_count(status: str | None) -> int:
    """Parse the trailing integer from an asyncpg command tag (``DELETE 7``)."""
    if not status:
        return 0
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):  # pragma: no cover - defensive
        return 0


# ---------------------------------------------------------------------------
# Per-policy shape: the DELETE adapter + the FindingPayload builder. Each
# target's mechanics differ (child-table cleanup, cascade counting) so these
# stay per-policy, but everything AROUND them — TTL resolution, the
# disabled/enabled gate, policy-row loading, usage zeroing — is the ONE shared
# flow in :func:`handle_policy`.
# ---------------------------------------------------------------------------

PurgeFn = Callable[..., Awaitable[dict[str, int]]]
FindingFn = Callable[..., FindingPayload]


@dataclass(frozen=True)
class _PolicyDefaults:
    """The migration 0109 seed shape, mirrored here so ``deps=None`` (no pool)
    never needs a live database to answer "is this policy disabled"."""

    table_name: str
    ttl_days: int
    keep_classes: tuple[str, ...]
    batch_size: int
    enabled: bool
    env_fallback_var: str | None


@dataclass(frozen=True)
class _PolicySpec:
    defaults: _PolicyDefaults
    purge: PurgeFn
    build_finding: FindingFn
    zero_counters: Mapping[str, int]


# ---------------------------------------------------------------------------
# signals_retention adapter — migration 0036 semantics, unchanged.
# ---------------------------------------------------------------------------


def _corpus_index_name() -> str:
    """The OpenSearch index a purged signal's doc lives in.

    Read from the SAME config the indexer and the drain read, so a deployment
    that overrides ``LEGBA_DATA_OPENSEARCH_INDEX`` tombstones against the index
    it actually writes. Pure dataclass + env read — importing it costs nothing
    and does NOT require opensearch-py to be installed."""
    from ...config import OpenSearchConfig

    return OpenSearchConfig.from_env().index


async def _purge_signals(
    pool: Any, *, ttl_days: int, batch_limit: int, keep_classes: Sequence[str],
) -> dict[str, int]:
    """Purge one batch of aged signals + their value-referenced children.

    All deletes for a batch run in ONE transaction so a signal and its child
    rows are removed atomically (no window where a child is orphaned).
    """
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
                    list(keep_classes),
                    batch_limit,
                )
            ]
            if not ids:
                return {
                    "signals_purged": 0,
                    "entity_links_purged": 0,
                    "aliases_purged": 0,
                    "corpus_tombstoned": 0,
                }

            # Children FIRST (no FK to signals — explicit cleanup so the
            # purge never orphans a link or alias).
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

            # The OpenSearch corpus doc for each purged signal (`_id` = the
            # signal id) is now an ORPHAN: a searchable hit pointing at a row
            # that no longer exists, which `read_document` will serve verbatim
            # because that path does no existence check. Record the intent HERE,
            # inside the same transaction as the DELETE, so the tombstone and the
            # purge can never disagree; the `corpus_retention` sweep drains the
            # queue against OpenSearch out of band. Doing the delete inline
            # instead would put a fallible network call in this transaction — a
            # timeout would either abort a good purge or commit it and lose the
            # delete, which is the orphan we are preventing. See migration 0175.
            tombstoned = _row_count(
                await conn.execute(
                    """
                    INSERT INTO corpus_tombstones (doc_id, index_name, reason)
                    SELECT id, $2, 'signals_retention'
                      FROM unnest($1::uuid[]) AS t(id)
                    ON CONFLICT (doc_id) DO NOTHING
                    """,
                    ids,
                    _corpus_index_name(),
                )
            )

    return {
        "signals_purged": signals_purged,
        "entity_links_purged": entity_links_purged,
        "aliases_purged": aliases_purged,
        "corpus_tombstoned": tombstoned,
    }


def _finding_signals(counters: Mapping[str, int], *, ttl_days: int) -> FindingPayload:
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
        data={
            "sub_handler": "signals_retention",
            "ttl_days": ttl_days,
            **dict(counters),
        },
    )


# ---------------------------------------------------------------------------
# analyst_traces_retention adapter — migration 0101 semantics, unchanged.
# ---------------------------------------------------------------------------


async def _purge_traces(
    pool: Any, *, ttl_days: int, batch_limit: int, keep_classes: Sequence[str],
) -> dict[str, int]:
    """Purge one batch of aged traces (+ their CASCADE-linked critiques).

    ``keep_classes`` is accepted for signature parity with the shared engine
    but unused: ``analyst_traces`` carries no retention_class column (the
    seeded policy's ``keep_classes`` is the empty array for exactly this
    reason).

    All work for a batch runs in ONE transaction: the count of cascading
    critiques and the trace delete are read/applied atomically so the honest
    summary matches exactly what the DB removed.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            ids = [
                r["run_id"]
                for r in await conn.fetch(
                    """
                    SELECT run_id
                      FROM analyst_traces
                     WHERE run_started_at < NOW() - ($1::int * INTERVAL '1 day')
                     ORDER BY run_started_at ASC
                     LIMIT $2
                    """,
                    ttl_days,
                    batch_limit,
                )
            ]
            if not ids:
                return {"traces_purged": 0, "critiques_cascaded": 0}

            # Honest disclosure of the ON DELETE CASCADE side-effect: count
            # the linked critiques the trace delete is about to drop (the DB
            # does the delete itself via the FK — we only count).
            critiques_cascaded = int(
                await conn.fetchval(
                    "SELECT count(*) FROM analyst_critiques "
                    "WHERE trace_id = ANY($1::uuid[])",
                    ids,
                )
                or 0
            )
            # output_dead_letter.run_id → ON DELETE SET NULL (rows
            # preserved); no explicit cleanup needed.
            traces_purged = _row_count(
                await conn.execute(
                    "DELETE FROM analyst_traces WHERE run_id = ANY($1::uuid[])",
                    ids,
                )
            )

    return {"traces_purged": traces_purged, "critiques_cascaded": critiques_cascaded}


def _finding_traces(counters: Mapping[str, int], *, ttl_days: int) -> FindingPayload:
    tp = counters.get("traces_purged", 0)
    if ttl_days <= 0:
        title = "Analyst-traces retention: disabled (ttl_days<=0) — no purge"
    else:
        title = (
            f"Analyst-traces retention: purged {tp} trace(s) older than "
            f"{ttl_days}d ({counters.get('critiques_cascaded', 0)} linked "
            f"critiques cascaded)"
        )
    body = "\n".join(f"{k}={v}" for k, v in counters.items())
    tags = ["deterministic", "analyst_traces_retention"]
    if tp:
        tags.append("traces_purged")
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={
            "sub_handler": "analyst_traces_retention",
            "ttl_days": ttl_days,
            **dict(counters),
        },
    )


# ---------------------------------------------------------------------------
# The registry — the ONE place a new policy_name is wired to its adapter.
# ---------------------------------------------------------------------------

_POLICIES: dict[str, _PolicySpec] = {
    "signals_retention": _PolicySpec(
        defaults=_PolicyDefaults(
            table_name="signals",
            ttl_days=0,
            keep_classes=("retain_always", "evidence_hold"),
            batch_size=5_000,
            enabled=True,
            env_fallback_var="LEGBA_SIGNALS_RETENTION_TTL_DAYS",
        ),
        purge=_purge_signals,
        build_finding=_finding_signals,
        zero_counters={
            "signals_purged": 0,
            "entity_links_purged": 0,
            "aliases_purged": 0,
            "corpus_tombstoned": 0,
        },
    ),
    "analyst_traces_retention": _PolicySpec(
        defaults=_PolicyDefaults(
            table_name="analyst_traces",
            ttl_days=0,
            keep_classes=(),
            batch_size=5_000,
            enabled=True,
            env_fallback_var="LEGBA_ANALYST_TRACES_TTL_DAYS",
        ),
        purge=_purge_traces,
        build_finding=_finding_traces,
        zero_counters={"traces_purged": 0, "critiques_cascaded": 0},
    ),
}

#: Public read-only view of the wired policy names (drift-guard / test use).
KNOWN_POLICIES: frozenset[str] = frozenset(_POLICIES)


async def _load_policy_row(pool: Any, policy_name: str) -> Mapping[str, Any] | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT table_name, ttl_days, keep_classes, batch_size, enabled, "
            "env_fallback_var "
            "FROM retention_policies WHERE policy_name = $1",
            policy_name,
        )


async def handle_policy(
    policy_name: str,
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Execute ONE named ``retention_policies`` row — the shared engine every
    migrated janitor delegates to.

    Sweeps the substrate directly via ``deps.pg_pool`` (the ``inputs`` slice
    is ignored, same as the pre-consolidation standalone handlers — the unit
    of work is "all aged rows for this policy"). ``deps is None`` (unit path)
    or an effective ``ttl_days <= 0`` (disabled — the default) yields a
    zeroed, honest run; the pool is never touched in either case.
    """
    spec = _POLICIES[policy_name]
    d = spec.defaults
    pool = getattr(deps, "pg_pool", None) if deps is not None else None

    row: Mapping[str, Any] | None = None
    if pool is not None:
        try:
            row = await _load_policy_row(pool, policy_name)
        except Exception as exc:
            logger.warning(
                "retention_sweep.policy_load_failed policy=%s err=%s",
                policy_name, exc,
            )

    default_ttl = row["ttl_days"] if row is not None else d.ttl_days
    keep_classes: tuple[str, ...] = (
        tuple(row["keep_classes"])
        if row is not None and row["keep_classes"] is not None
        else d.keep_classes
    )
    batch_size_default = row["batch_size"] if row is not None else d.batch_size
    enabled = bool(row["enabled"]) if row is not None else d.enabled
    env_var = (
        row["env_fallback_var"]
        if row is not None and row["env_fallback_var"]
        else d.env_fallback_var
    )

    # TTL resolution: run options first (tests / explicit invocations), then
    # the env opt-in, then the policy's configured default. Cadence fires
    # carry ONLY {"sub_handler": ...} in options — see module docstring.
    raw_ttl = options.get("ttl_days")
    if raw_ttl is None:
        env_raw = os.getenv(env_var, "").strip() if env_var else ""
        raw_ttl = env_raw or default_ttl
    ttl_days = int(raw_ttl)

    counters: dict[str, int] = dict(spec.zero_counters)
    if pool is not None and enabled and ttl_days > 0:
        batch_limit = int(options.get("batch_limit", batch_size_default))
        try:
            counters = await spec.purge(
                pool,
                ttl_days=ttl_days,
                batch_limit=batch_limit,
                keep_classes=keep_classes,
            )
        except Exception as exc:
            logger.warning(
                "retention_sweep.failed policy=%s err=%s", policy_name, exc,
            )

    finding = spec.build_finding(counters, ttl_days=ttl_days)
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle_policy", "KNOWN_POLICIES"]
