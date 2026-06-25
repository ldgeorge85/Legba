# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``SubscriptionEngine`` — the P-08 fan-out + subscription seam.

Ties the pieces together (PIVOT §4.4 / §4.4.1 / §6.1):

  1. **Resolve** a target's ``list[SourceRef]`` (explicit + selector over
     source_descriptors SCOPE) → concrete ``ResolvedBinding`` set
     (:mod:`.sourceref`).
  2. **Enforce** each binding against the source's ``subscription_policy``
     (open / allowlist / grant via wiring_descriptor) at REGISTRATION
     (:mod:`.policy`). A locked source refuses an unauthorized target here.
  3. **Plan** the coarse NATS subject filters (tenant / source / modality)
     and bind ONE per-target aggregated JetStream consumer (:mod:`.subjects`
     + ``NatsStore.ensure_durable_consumer``).
  4. **Match** delivered/persistent signals exactly via the structured SQL
     ``WHERE`` (GIN/btree indexes) + the Starlark residual on the narrowed set
     (:mod:`.filter`).
  5. **Observe** per-target consumer lag (``num_pending``) +  stream growth.

The engine is the control-plane object the runtime + API hold; it does not own
the actor loop (that's P-06/P-10) — it owns the *wiring* and the *matching*.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ...data.nats import (
    SIGNAL_STREAM_NAME,
    SIGNAL_STREAM_SUBJECTS,
    signal_subject,
    subject_token,
)
from ...data.schemas.source import SourceRef
from .filter import build_sql_filter, matches
from .policy import (
    SubscriptionPolicyError,
    enforce_subscription,
    load_source_policy,
)
from .sourceref import resolve_source_refs
from .subjects import ResolvedBinding, subject_filters_for

logger = logging.getLogger(__name__)


def target_consumer_name(target_id: str) -> str:
    """The durable name of a target's single aggregated consumer."""
    return f"target_{subject_token(target_id)}"


@dataclass
class TargetSubscription:
    """The materialised wiring for one target after registration."""

    target_id: str
    target_tenant: str
    bindings: list[ResolvedBinding]
    subject_filters: list[str]
    consumer_name: str
    refused: list[SubscriptionPolicyError] = field(default_factory=list)

    @property
    def source_ids(self) -> list[str]:
        return [b.source_id for b in self.bindings]


class SubscriptionEngine:
    """Control-plane wiring + matching for source→target fan-out.

    Construct with a connected ``PostgresStore`` and (optionally) a connected
    ``NatsStore``. With NATS the engine ensures the shared raw-pool stream +
    per-target durable consumers; without it (unit context) the engine still
    resolves refs, enforces policy, plans subjects and matches signals — the
    NATS calls are simply skipped.
    """

    def __init__(self, pg: Any, *, nats: Any = None) -> None:
        self._pg = pg
        self._nats = nats

    # ------------------------------------------------------------------
    # Stream bootstrap
    # ------------------------------------------------------------------

    async def ensure_signal_stream(
        self,
        *,
        retention: str = "interest",
        max_age_seconds: int = 86_400,
        max_msgs: int = 1_000_000,
    ) -> bool:
        """Idempotently ensure the shared raw-pool signal stream exists.

        One stream (``legba_signals``) covers ``legba.signals.>``; per-target
        consumers attach subject-filtered (PIVOT §6.1). Default retention is
        ``interest`` (cheap real-time); lossless per-source streams are a
        future per-source override.
        """
        if self._nats is None:
            return False
        return await self._nats.ensure_stream(
            SIGNAL_STREAM_NAME,
            SIGNAL_STREAM_SUBJECTS,
            retention=retention,
            max_age_seconds=max_age_seconds,
            max_msgs=max_msgs,
        )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def register_target(
        self,
        *,
        target_id: str,
        target_tenant: str,
        source_refs: list[SourceRef],
        strict_policy: bool = True,
    ) -> TargetSubscription:
        """Wire a target's subscriptions: resolve → enforce → consumer.

        ``strict_policy=True`` (default): a refused binding raises
        :class:`SubscriptionPolicyError` (the acceptance behavior — a
        ``grant`` source refuses an ungranted target). ``strict_policy=False``
        drops refused bindings and records them on
        :attr:`TargetSubscription.refused` (used by the selector auto-wire
        path, where one bad source shouldn't sink the whole target).

        The per-target consumer is (re)bound to the union of coarse subject
        filters across all authorized bindings — ONE consumer per target
        (PIVOT §6.1).
        """
        resolved = await resolve_source_refs(
            self._pg,
            target_id=target_id,
            target_tenant=target_tenant,
            source_refs=source_refs,
        )

        authorized: list[ResolvedBinding] = []
        refused: list[SubscriptionPolicyError] = []
        for b in resolved:
            policy = await load_source_policy(self._pg, b.source_id)
            if policy is None:
                logger.warning("source %s vanished during registration", b.source_id)
                continue
            try:
                await enforce_subscription(
                    self._pg,
                    source=policy,
                    target_id=target_id,
                    target_tenant=target_tenant,
                )
            except SubscriptionPolicyError as exc:
                if strict_policy:
                    raise
                logger.info("subscription refused (non-strict): %s", exc)
                refused.append(exc)
                continue
            authorized.append(b)

        subject_filters = subject_filters_for(authorized)
        consumer_name = target_consumer_name(target_id)

        if self._nats is not None and authorized:
            await self.ensure_signal_stream()
            await self._nats.ensure_durable_consumer(
                SIGNAL_STREAM_NAME,
                consumer_name,
                filter_subjects=subject_filters,
            )

        return TargetSubscription(
            target_id=target_id,
            target_tenant=target_tenant,
            bindings=authorized,
            subject_filters=subject_filters,
            consumer_name=consumer_name,
            refused=refused,
        )

    # ------------------------------------------------------------------
    # Late-join registration — register + catch-up + seamless forward (P-12)
    # ------------------------------------------------------------------

    async def register_target_with_catch_up(
        self,
        *,
        target_id: str,
        target_tenant: str,
        source_refs: list[SourceRef],
        sink: Any,
        strict_policy: bool = True,
        limit_per_binding: int | None = None,
    ) -> tuple[TargetSubscription, Any]:
        """Register a (possibly late-joining) target with catch-up + forward.

        The minimal P-12 wiring. Captures the stream boundary BEFORE resolving
        the subscription so anything published during registration is forward
        (never lost), resolves + enforces policy via :meth:`register_target`,
        then runs the :class:`~.backfill.Backfiller`: replays the matching
        historical slice through ``sink`` once and (re)binds the per-target
        consumer at ``boundary_seq + 1`` so live delivery resumes with no gap
        or duplicate. Returns ``(subscription, BackfillResult)``.
        """
        from .backfill import Backfiller, capture_cursor

        cursor = await capture_cursor(self)
        subscription = await self.register_target(
            target_id=target_id,
            target_tenant=target_tenant,
            source_refs=source_refs,
            strict_policy=strict_policy,
        )
        backfiller = Backfiller(self)
        result = await backfiller.catch_up_and_forward(
            subscription,
            sink,
            limit_per_binding=limit_per_binding,
            cursor=cursor,
        )
        return subscription, result

    # ------------------------------------------------------------------
    # Publish (used by acquisition / tests to push a signal onto the bus)
    # ------------------------------------------------------------------

    async def publish_signal(
        self,
        *,
        signal: Any,
        event_class: str = "raw",
    ) -> str:
        """Publish a Signal to its coarse subject. Returns the subject.

        Reads tenant/source/modality off the Signal (the source-first
        contract). Acquisition (P-06) owns the real publish; this exists so
        the engine + tests can drive the bus end-to-end. ``event_class``
        defaults to ``raw`` for a source row, ``derived`` for produced rows —
        callers pass ``derived`` for job/analyst output.
        """
        subject = signal_subject(
            tenant=getattr(signal, "owner_tenant", "default"),
            source_id=signal.source_id,
            modality=getattr(signal, "modality", "text"),
            event_class=event_class,
        )
        if self._nats is not None:
            payload = signal.model_dump_json().encode("utf-8")
            await self._nats.publish_json(subject, payload)
        return subject

    # ------------------------------------------------------------------
    # Matching — batch read-slice over the persistent pool
    # ------------------------------------------------------------------

    async def read_slice(
        self,
        binding: ResolvedBinding,
        *,
        limit: int | None = None,
        apply_residual: bool = True,
    ) -> list[dict[str, Any]]:
        """The batch read-slice for one binding: SQL ``WHERE`` + residual.

        Pushes the structured filter to SQL (GIN/btree), then evaluates the
        Starlark residual in Python on the narrowed set (PIVOT §4.4). Returns
        signal rows as plain dicts.
        """
        sqlf = build_sql_filter(
            source_id=binding.source_id,
            owner_tenant=binding.owner_tenant,
            subscription=binding.subscription,
        )
        sql = sqlf.select_signals(limit=limit)
        async with self._pg.acquire() as conn:
            rows = await conn.fetch(sql, *sqlf.params)
        out = [dict(r) for r in rows]
        if apply_residual and binding.subscription.predicate:
            from .filter import residual_matches_async

            kept: list[dict[str, Any]] = []
            for r in out:
                if await residual_matches_async(binding.subscription, r):
                    kept.append(r)
            out = kept
        return out

    async def read_target_slice(
        self,
        subscription: TargetSubscription,
        *,
        limit_per_binding: int | None = None,
    ) -> list[dict[str, Any]]:
        """Union the read-slices across all of a target's bindings (deduped)."""
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for b in subscription.bindings:
            for row in await self.read_slice(b, limit=limit_per_binding):
                rid = str(row.get("id"))
                if rid in seen:
                    continue
                seen.add(rid)
                merged.append(row)
        return merged

    def delivers(
        self,
        binding: ResolvedBinding,
        row: dict[str, Any],
    ) -> bool:
        """Real-time per-signal match for one delivered row + binding.

        A delivered NATS message (coarse-subject filtered) is re-checked here
        against the full structured filter + residual before the target acts.
        """
        return matches(
            binding.subscription,
            row,
            source_id=binding.source_id,
            owner_tenant=binding.owner_tenant,
        )

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    async def consumer_lag(self, target_id: str) -> dict[str, Any]:
        """Per-target consumer lag snapshot (``num_pending`` = lag)."""
        if self._nats is None:
            raise RuntimeError("consumer_lag requires a connected NatsStore")
        return await self._nats.consumer_lag(
            SIGNAL_STREAM_NAME, target_consumer_name(target_id)
        )

    async def stream_growth(self) -> dict[str, Any]:
        """Raw-pool stream growth snapshot (slow-consumer monitoring)."""
        if self._nats is None:
            raise RuntimeError("stream_growth requires a connected NatsStore")
        return await self._nats.stream_growth(SIGNAL_STREAM_NAME)


__all__ = [
    "SubscriptionEngine",
    "TargetSubscription",
    "target_consumer_name",
]
