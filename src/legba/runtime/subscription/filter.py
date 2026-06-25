# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Subscription matching: structured SQL ``WHERE`` + Starlark residual (P-08).

Two-stage exact matching (PIVOT §4.4):

  1. **Structured filter → SQL WHERE.** ``geo``/``tags``/``entity_classes``
     push to the GIN indexes on the signals table (array ``&&`` overlap);
     ``languages``/``modalities`` push to btree (``= ANY``); ``source_id`` and
     ``owner_tenant`` pin the per-binding coarse facts. ``canonical_only``
     adds the dedup-aware delivery clause (PIVOT §4.4 / P-02). This is the
     BATCH read slice and the narrow set the residual runs over.

  2. **Starlark residual.** ``Subscription.predicate`` (the long tail —
     ``mentions()``, ``severity_at_least()``, …) compiled once via the EXISTING
     engine (:mod:`legba.data.predicates`) and evaluated in Python on the
     SQL-narrowed rows only. We never express the residual as SQL or as a NATS
     subject (PIVOT §6.1).

The same builder serves both the real-time path (a delivered NATS message is
re-checked against the SQL predicate / residual before the target acts) and the
batch path (read-slice over the persistent pool). For real-time, one row's
worth of the predicate is the per-signal residual check.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from ...data.predicates import (
    SURFACE_CTX_CONTRACTS,
    PredicateBudgetExceeded,
    PredicateCompilationError,
    PredicateRuntimeError,
    PredicateSurface,
    compile_predicate,
)
from ...data.schemas.source import Subscription

logger = logging.getLogger(__name__)


@dataclass
class SqlFilter:
    """A composed SQL ``WHERE`` fragment + its positional params."""

    where: str
    params: list[Any] = field(default_factory=list)

    def select_signals(self, *, columns: str = "*", limit: int | None = None) -> str:
        sql = f"SELECT {columns} FROM signals WHERE {self.where} ORDER BY fetched_at DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return sql


def build_sql_filter(
    *,
    source_id: str,
    owner_tenant: str,
    subscription: Subscription,
    start_param: int = 1,
) -> SqlFilter:
    """Build the structured SQL ``WHERE`` for one resolved binding.

    Pins ``source_id`` + ``owner_tenant`` (the coarse binding facts) then ANDs
    the structured filter. Array fields use ``&&`` (GIN-backed overlap):
    a signal matches if it carries ANY of the requested geo/tags/entity_classes.
    Scalar list fields (``languages``/``modalities``) use ``= ANY($n)``.
    """
    clauses: list[str] = []
    params: list[Any] = []

    def _add(clause_tpl: str, value: Any) -> None:
        params.append(value)
        clauses.append(clause_tpl.format(n=start_param + len(params) - 1))

    _add("source_id = ${n}", source_id)
    _add("owner_tenant = ${n}", owner_tenant)

    sub = subscription
    # GIN array-overlap (any-of) on the indexed columns.
    if sub.geo:
        _add("geo && ${n}::text[]", list(sub.geo))
    if sub.tags:
        _add("tags && ${n}::text[]", list(sub.tags))
    if sub.entity_classes:
        _add("entity_classes && ${n}::text[]", list(sub.entity_classes))
    # btree any-of.
    if sub.languages:
        _add("language = ANY(${n}::text[])", list(sub.languages))
    if sub.modalities:
        _add("modality = ANY(${n}::text[])", list(sub.modalities))

    # Dedup-aware delivery (PIVOT §4.4 / P-02): canonical_only delivers a row
    # ONLY if it is itself canonical (not an alias of another). A row is an
    # alias iff canonical_signal_id is set and points elsewhere.
    if sub.canonical_only:
        clauses.append(
            "(canonical_signal_id IS NULL OR canonical_signal_id = id)"
        )

    return SqlFilter(where=" AND ".join(clauses), params=params)


# ---------------------------------------------------------------------------
# Residual evaluation
# ---------------------------------------------------------------------------


def _payload_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Normalise a signal row's ``payload`` to a dict.

    With the pool-level JSONB codec (``data/postgres.py``) every DB fetch
    already yields a dict, and the published NATS envelope is parsed JSON —
    so this is a NO-OP SAFETY net for codec-less connections (raw asyncpg in
    scripts/tests) and malformed envelopes. It is what keeps the row-form
    and envelope-form verdicts identical (G4) even off the codec pool.
    """
    payload = row.get("payload")
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str) and payload:
        try:
            parsed = json.loads(payload)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _signal_residual_ctx(row: dict[str, Any]) -> dict[str, Any]:
    """Build the ``target.scope``-surface ctx for one signal row.

    Maps the substrate signal row onto the helper-catalog ctx shape the
    EXISTING predicate engine expects (see ``data/predicates/helpers.py``):
    the signal-scoped helpers read ``signal.{entity_classes,geo_provenance,
    tags,credibility,...}``. We feed the typed columns through so
    ``mentions()`` / ``has_tag()`` / ``geo_in()`` / ``credibility()`` /
    ``recent()`` work on a real signal.

    The keys provided here are DECLARED in
    ``data/predicates/helpers.SURFACE_CTX_CONTRACTS`` (the ``signal.*`` half
    of the target.scope contract) — the compiler refuses predicates whose
    helpers this builder cannot feed. The import-time check below keeps the
    declaration and this builder mechanically in sync.
    """
    payload = _payload_dict(row)
    return {
        "signal": {
            "entity_classes": list(row.get("entity_classes") or []),
            "tags": list(row.get("tags") or []),
            "geo_provenance": list(row.get("geo") or []),
            "credibility": row.get("source_credibility"),
            "language": row.get("language"),
            "modality": row.get("modality"),
            "event_timestamp": row.get("fetched_at"),
            "ingested_at": row.get("fetched_at"),
            "severity": payload.get("severity"),
            "classification_scores": payload.get("classification_scores"),
            # Free-text surface for contains_any() — the lever a thematic target
            # uses to frame a situation (5c). In practice this is the title
            # (+ summary/body/description WHEN PRESENT — most live signals carry
            # only a title). Derived from the payload fetched on every path, so
            # it is feedable on BOTH the live residual path and the cadence slice.
            "text": _signal_text(payload),
        }
    }


def _signal_text(payload: dict[str, Any]) -> str:
    """Concatenate the human-readable signal text (title + summary/body) for
    free-text predicate matching. Best-effort: missing keys contribute nothing."""
    parts = [
        payload.get("title"),
        payload.get("summary"),
        payload.get("body"),
        payload.get("description"),
    ]
    return " ".join(str(p) for p in parts if isinstance(p, str) and p).strip()


def _check_ctx_contract_sync() -> None:
    """Import-time guard: this builder ↔ the declared surface contract.

    Adding a key to ``_signal_residual_ctx`` without declaring it (or vice
    versa) silently re-opens the envelope-vs-row divergence class, so a
    mismatch fails loud at import instead of drifting.
    """
    declared = {
        k for k in SURFACE_CTX_CONTRACTS[PredicateSurface.TARGET_SCOPE.value]
        if k.startswith("signal.")
    }
    provided = {f"signal.{k}" for k in _signal_residual_ctx({})["signal"]}
    if provided != declared:
        raise RuntimeError(
            "subscription.filter._signal_residual_ctx is out of sync with "
            "predicates.SURFACE_CTX_CONTRACTS[target.scope]: "
            f"builder provides {sorted(provided)} but contract declares "
            f"{sorted(declared)} — update both together."
        )


_check_ctx_contract_sync()


def _eval_residual(pred: str, row: dict[str, Any]) -> bool:
    """Compile + evaluate one residual against one row. Fails CLOSED.

    Catches both runtime errors and budget breaches — a target should never
    receive a signal whose residual could not be confirmed within budget.
    """
    try:
        compiled = compile_predicate(pred, PredicateSurface.TARGET_SCOPE)
        return compiled.evaluate(_signal_residual_ctx(row))
    except PredicateBudgetExceeded as exc:
        logger.warning("subscription residual exceeded budget (dropped): %s", exc)
        return False
    except PredicateCompilationError as exc:
        # A predicate registered before the ctx-contract gate (or hand-written
        # into the DB) can now refuse to compile — e.g. org_match() with no
        # ctx-builder feeding it. Fail CLOSED, loudly: the signal is dropped
        # for this binding rather than fabricating a match either way.
        logger.error(
            "subscription residual REFUSED at compile (dropped; fix the "
            "registered predicate): %s", exc,
        )
        return False
    except PredicateRuntimeError as exc:
        logger.warning("subscription residual eval failed: %s", exc)
        return False


def filter_rows_by_residual(pred: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the rows whose target.scope residual ``pred`` matches.

    Compiles the predicate ONCE then evaluates each row's residual ctx, so the
    cadence analyst-slice reader (dapr_actors._read_substrate_slice) can apply a
    target's ``scope.predicate`` to focus a thematic target's slice (5c) without
    recompiling per row. Fails CLOSED per row (a row whose residual cannot be
    confirmed is dropped). A compile failure drops the WHOLE batch (returns [])
    — loud-but-safe: a thematic target with a broken predicate reads nothing
    rather than everything. Synchronous (SIGALRM budget); call via
    ``asyncio.to_thread`` from an event-loop context.
    """
    if not pred:
        return rows
    try:
        compiled = compile_predicate(pred, PredicateSurface.TARGET_SCOPE)
    except (PredicateCompilationError, PredicateBudgetExceeded) as exc:
        logger.error(
            "slice residual REFUSED at compile (whole slice dropped; fix the "
            "target scope.predicate): %s", exc,
        )
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            if compiled.evaluate(_signal_residual_ctx(row)):
                out.append(row)
        except (PredicateBudgetExceeded, PredicateRuntimeError) as exc:
            logger.warning("slice residual eval failed (row dropped): %s", exc)
    return out


def residual_matches(subscription: Subscription, row: dict[str, Any]) -> bool:
    """Synchronous residual match for one signal row.

    No predicate → trivially True (the structured filter was the whole match).
    Used on the real-time per-signal path. The evaluator's SIGALRM budget runs
    inline here (correct on the main thread). The batch path uses
    :func:`residual_matches_async` to evaluate off-thread (no SIGALRM under the
    event loop).
    """
    pred = subscription.predicate
    if not pred:
        return True
    return _eval_residual(pred, row)


async def residual_matches_async(subscription: Subscription, row: dict[str, Any]) -> bool:
    """Async residual match — runs the predicate OFF the event-loop thread.

    The predicate engine's wall-clock budget is SIGALRM-based on the main
    thread; under an asyncio loop that interacts badly (the loop's own timers
    perturb ``setitimer`` and trip spurious budget breaches). Off the main
    thread the engine uses its best-effort post-hoc timing path (see
    ``data/predicates/evaluator.py``) — no mid-eval SIGALRM, so legitimate
    matches aren't spuriously dropped, while a genuine runaway still fails
    closed via the post-hoc check.
    """
    pred = subscription.predicate
    if not pred:
        return True
    return await asyncio.to_thread(_eval_residual, pred, row)


def matches(
    subscription: Subscription,
    row: dict[str, Any],
    *,
    source_id: str,
    owner_tenant: str,
) -> bool:
    """In-Python full match (structured + residual) for ONE signal row.

    Used on the real-time path (re-check a delivered NATS message) and in tests
    where running the SQL is unnecessary. Mirrors the SQL semantics exactly so
    SQL-narrowed and Python-narrowed sets agree.
    """
    if row.get("source_id") != source_id:
        return False
    if row.get("owner_tenant") != owner_tenant:
        return False

    sub = subscription
    sig_geo = set(row.get("geo") or [])
    sig_tags = set(row.get("tags") or [])
    sig_ec = set(row.get("entity_classes") or [])

    if sub.geo and not (set(sub.geo) & sig_geo):
        return False
    if sub.tags and not (set(sub.tags) & sig_tags):
        return False
    if sub.entity_classes and not (set(sub.entity_classes) & sig_ec):
        return False
    if sub.languages and row.get("language") not in sub.languages:
        return False
    if sub.modalities and row.get("modality") not in sub.modalities:
        return False
    if sub.canonical_only:
        csid = row.get("canonical_signal_id")
        rid = row.get("id")
        if csid is not None and not _same_uuid(csid, rid):
            return False

    return residual_matches(sub, row)


def _same_uuid(a: Any, b: Any) -> bool:
    try:
        return UUID(str(a)) == UUID(str(b))
    except (ValueError, TypeError, AttributeError):
        return a == b


__all__ = [
    "SqlFilter",
    "build_sql_filter",
    "residual_matches",
    "residual_matches_async",
    "matches",
]
