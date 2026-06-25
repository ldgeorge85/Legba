# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-190 substrate-write surface — the "I produced X, persist it" path.

Output kinds are how analysts surface results. This module is the
substrate-write surface: a typed facade over
:mod:`legba.data.provenance.writes` that exposes one writer per analyst-
output payload type. Where the operator-facing kinds (``alert``,
``pushover``, ``xmpp``, …) fan a payload to a transport surface, this kind
is the canonical *persistence* surface — every other output kind routes a
payload through here first so the row lands in substrate with full
provenance + lineage before any side-effects fire.

Surface
-------

``write_finding`` / ``write_situation`` / ``write_hypothesis`` /
``write_prediction`` / ``write_alert`` — one function per payload family in
:mod:`legba.data.provenance.models`. Each:

  1. Validates the payload against its registered pydantic model (the kind
     registry in :mod:`legba.data.provenance.kinds` is the source of truth).
  2. Builds an :class:`AnalystContext` from the bare ``(target_id,
     analyst_id, analyst_version, target_version, run_id)`` tuple.
  3. Calls :func:`legba.data.provenance.writes.write_analyst_output` with
     the correct :class:`OutputKind` enum + the validated payload.
  4. Returns the new row UUID directly.

Failure modes
-------------

Validation failures **raise** ``pydantic.ValidationError`` directly — they
do *not* route to ``output_dead_letter``. The DLQ table exists for the
generic ``write_analyst_output`` surface, which is the runtime-host entry
point and needs the "soft-fail + operator triage" contract. The substrate-
writer kind is the *typed* entry point: callers commit to a payload type
at the call site, so a validation failure is a programming error, not a
schema-drift incident, and should surface synchronously.

If a substrate write happens to land an invalid payload anyway (e.g.
dict-shaped input from a Phase 6 analyst), the underlying
``write_analyst_output`` will still DLQ-route it — we surface that case by
raising a :class:`SubstrateWriteFailed` carrying the DLQ entry, so the
caller can choose to swallow + log or re-raise.

Module-level registration
-------------------------

``KIND_NAME = "substrate_writer"`` so the host registry can discover this
module the same way it discovers analyst kinds (see
``legba.data.analysts.deterministic`` for the pattern).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence
from uuid import UUID, uuid4

import asyncpg
from pydantic import ValidationError

from ..provenance._core import AnalystContext
from ..provenance.dlq import OutputDeadLetterEntry
from ..provenance.kinds import OutputKind, spec_for_kind
from ..provenance.models import (
    AlertPayload,
    FactPayload,
    FindingPayload,
    HypothesisPayload,
    NexusPayload,
    PredictionPayload,
    SituationPayload,
)
from ..provenance.writes import (
    NatsPublishFn,
    OutputRow,
    write_analyst_output,
)


# ---------------------------------------------------------------------------
# Kind identity (host registry hook)
# ---------------------------------------------------------------------------


KIND_NAME: str = "substrate_writer"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubstrateWriteFailed(Exception):
    """Raised when the underlying ``write_analyst_output`` DLQ-routes a
    payload that we'd already validated.

    Should not normally fire — substrate.py validates payloads up-front,
    so by the time we reach the underlying writer, validation has passed.
    If the writer DLQ-routes regardless (e.g. schema-uri override drift,
    or an asymmetric coercion path through pydantic), we surface the DLQ
    entry to the caller so they can decide retry vs. log.
    """

    kind: OutputKind
    dlq_entry: OutputDeadLetterEntry

    def __str__(self) -> str:                                # pragma: no cover
        return (
            f"SubstrateWriteFailed(kind={self.kind.value!r}, "
            f"dlq_id={self.dlq_entry.id!s})"
        )


# ---------------------------------------------------------------------------
# Internal: shared write path
# ---------------------------------------------------------------------------


async def _write_for_kind(
    conn: asyncpg.Connection,
    *,
    kind: OutputKind,
    target_id: str | None,
    analyst_id: str,
    payload: Any,
    derived_from: Sequence[UUID],
    analyst_version: str = "",
    target_version: str | None = None,
    run_id: UUID | None = None,
    publish_fn: NatsPublishFn | None = None,
    schema_uri: str | None = None,
    row_id: UUID | None = None,
) -> UUID:
    """Typed-payload write helper shared by every per-kind wrapper.

    1. Validate payload against the kind's registered pydantic model. Bad
       payloads raise :class:`pydantic.ValidationError` synchronously.
    2. Build an :class:`AnalystContext` from the bare scalars.
    3. Delegate to :func:`write_analyst_output`. The returned tuple is
       collapsed to a single UUID; a DLQ tuple is re-raised as
       :class:`SubstrateWriteFailed`.
    """
    spec = spec_for_kind(kind)

    # 1) Up-front validation. We DO NOT want to route programming errors
    #    through the DLQ — callers picked the typed entry point precisely
    #    to get synchronous ValidationError surfacing.
    if isinstance(payload, dict):
        validated = spec.payload_model.model_validate(payload)
    elif isinstance(payload, spec.payload_model):
        validated = payload
    else:
        # Foreign model — re-validate against the declared shape.
        validated = spec.payload_model.model_validate(
            payload.model_dump(mode="python")
        )

    # 2) Build context. Runtime callers pass full identity; tests can
    #    supply just target_id + analyst_id.
    analyst_ctx = AnalystContext(
        analyst_id=analyst_id,
        analyst_version=analyst_version,
        run_id=run_id or uuid4(),
        target_id=target_id,
        target_version=target_version,
    )

    # 3) Delegate.
    output, dlq = await write_analyst_output(
        conn,
        analyst_ctx=analyst_ctx,
        kind=kind,
        output_payload=validated,
        derived_from=list(derived_from),
        publish_fn=publish_fn,
        schema_uri=schema_uri,
        row_id=row_id,
    )

    if output is None:
        # Defensive: we already validated, so this is the asymmetric-
        # coercion edge case. Surface the DLQ entry to the caller.
        assert dlq is not None
        raise SubstrateWriteFailed(kind=kind, dlq_entry=dlq)

    return output.id


# ---------------------------------------------------------------------------
# Per-kind wrappers
# ---------------------------------------------------------------------------


async def write_finding(
    conn: asyncpg.Connection,
    target_id: str | None,
    analyst_id: str,
    payload: FindingPayload | dict[str, Any],
    derived_from: Sequence[UUID],
    *,
    analyst_version: str = "",
    target_version: str | None = None,
    run_id: UUID | None = None,
    publish_fn: NatsPublishFn | None = None,
    schema_uri: str | None = None,
    row_id: UUID | None = None,
) -> UUID:
    """Persist a :class:`FindingPayload` row. Returns the new row UUID.

    Raises ``pydantic.ValidationError`` on invalid payload.
    """
    return await _write_for_kind(
        conn,
        kind=OutputKind.FINDING,
        target_id=target_id,
        analyst_id=analyst_id,
        payload=payload,
        derived_from=derived_from,
        analyst_version=analyst_version,
        target_version=target_version,
        run_id=run_id,
        publish_fn=publish_fn,
        schema_uri=schema_uri,
        row_id=row_id,
    )


async def write_situation(
    conn: asyncpg.Connection,
    target_id: str | None,
    analyst_id: str,
    payload: SituationPayload | dict[str, Any],
    derived_from: Sequence[UUID],
    *,
    analyst_version: str = "",
    target_version: str | None = None,
    run_id: UUID | None = None,
    publish_fn: NatsPublishFn | None = None,
    schema_uri: str | None = None,
    row_id: UUID | None = None,
) -> UUID:
    """Persist a :class:`SituationPayload` row. Returns the new row UUID.

    Raises ``pydantic.ValidationError`` on invalid payload.
    """
    return await _write_for_kind(
        conn,
        kind=OutputKind.SITUATION,
        target_id=target_id,
        analyst_id=analyst_id,
        payload=payload,
        derived_from=derived_from,
        analyst_version=analyst_version,
        target_version=target_version,
        run_id=run_id,
        publish_fn=publish_fn,
        schema_uri=schema_uri,
        row_id=row_id,
    )


async def write_hypothesis(
    conn: asyncpg.Connection,
    target_id: str | None,
    analyst_id: str,
    payload: HypothesisPayload | dict[str, Any],
    derived_from: Sequence[UUID],
    *,
    analyst_version: str = "",
    target_version: str | None = None,
    run_id: UUID | None = None,
    publish_fn: NatsPublishFn | None = None,
    schema_uri: str | None = None,
    row_id: UUID | None = None,
) -> UUID:
    """Persist a :class:`HypothesisPayload` row. Returns the new row UUID.

    Raises ``pydantic.ValidationError`` on invalid payload.
    """
    return await _write_for_kind(
        conn,
        kind=OutputKind.HYPOTHESIS,
        target_id=target_id,
        analyst_id=analyst_id,
        payload=payload,
        derived_from=derived_from,
        analyst_version=analyst_version,
        target_version=target_version,
        run_id=run_id,
        publish_fn=publish_fn,
        schema_uri=schema_uri,
        row_id=row_id,
    )


async def write_prediction(
    conn: asyncpg.Connection,
    target_id: str | None,
    analyst_id: str,
    payload: PredictionPayload | dict[str, Any],
    derived_from: Sequence[UUID],
    *,
    analyst_version: str = "",
    target_version: str | None = None,
    run_id: UUID | None = None,
    publish_fn: NatsPublishFn | None = None,
    schema_uri: str | None = None,
    row_id: UUID | None = None,
) -> UUID:
    """Persist a :class:`PredictionPayload` row. Returns the new row UUID.

    Raises ``pydantic.ValidationError`` on invalid payload.
    """
    return await _write_for_kind(
        conn,
        kind=OutputKind.PREDICTION,
        target_id=target_id,
        analyst_id=analyst_id,
        payload=payload,
        derived_from=derived_from,
        analyst_version=analyst_version,
        target_version=target_version,
        run_id=run_id,
        publish_fn=publish_fn,
        schema_uri=schema_uri,
        row_id=row_id,
    )


async def write_alert(
    conn: asyncpg.Connection,
    target_id: str | None,
    analyst_id: str,
    payload: AlertPayload | dict[str, Any],
    derived_from: Sequence[UUID],
    *,
    analyst_version: str = "",
    target_version: str | None = None,
    run_id: UUID | None = None,
    publish_fn: NatsPublishFn | None = None,
    schema_uri: str | None = None,
    row_id: UUID | None = None,
) -> UUID:
    """Persist an :class:`AlertPayload` row. Returns the new row UUID.

    Raises ``pydantic.ValidationError`` on invalid payload.

    NOTE: this is the substrate-write side. Operator-facing alert fan-out
    (NATS / Pushover / XMPP / Matrix) lives in
    :mod:`legba.data.outputs.alert` once it lands; that kind reads rows
    written here.
    """
    return await _write_for_kind(
        conn,
        kind=OutputKind.ALERT,
        target_id=target_id,
        analyst_id=analyst_id,
        payload=payload,
        derived_from=derived_from,
        analyst_version=analyst_version,
        target_version=target_version,
        run_id=run_id,
        publish_fn=publish_fn,
        schema_uri=schema_uri,
        row_id=row_id,
    )


async def write_fact(
    conn: asyncpg.Connection,
    target_id: str | None,
    analyst_id: str,
    payload: FactPayload | dict[str, Any],
    derived_from: Sequence[UUID],
    *,
    analyst_version: str = "",
    target_version: str | None = None,
    run_id: UUID | None = None,
    publish_fn: NatsPublishFn | None = None,
    schema_uri: str | None = None,
    row_id: UUID | None = None,
) -> UUID:
    """Persist a :class:`FactPayload` row to the dedicated ``facts`` table
    (anchor §5 PIECE 2). Returns the new row UUID.

    The same typed facade Piece 4's synthesize stage + analyst kinds call
    the way they call ``write_finding``. The underlying ``_insert_fact``
    upserts on the temporal-triple unique index. Raises
    ``pydantic.ValidationError`` on an invalid payload.
    """
    return await _write_for_kind(
        conn,
        kind=OutputKind.FACT,
        target_id=target_id,
        analyst_id=analyst_id,
        payload=payload,
        derived_from=derived_from,
        analyst_version=analyst_version,
        target_version=target_version,
        run_id=run_id,
        publish_fn=publish_fn,
        schema_uri=schema_uri,
        row_id=row_id,
    )


async def write_nexus(
    conn: asyncpg.Connection,
    target_id: str | None,
    analyst_id: str,
    payload: NexusPayload | dict[str, Any],
    derived_from: Sequence[UUID],
    *,
    analyst_version: str = "",
    target_version: str | None = None,
    run_id: UUID | None = None,
    publish_fn: NatsPublishFn | None = None,
    schema_uri: str | None = None,
    row_id: UUID | None = None,
) -> UUID:
    """Persist a :class:`NexusPayload` row to the dedicated ``nexuses`` table
    (PIECE A — reified typed Nexus). Returns the new row UUID.

    The typed facade the ``relationship_reifier`` calls the way analyst kinds
    call ``write_finding``. The underlying ``_insert_nexus`` upserts on the
    open-only typed-triple unique index + supersedes a prior open row on a
    polarity/label change. Raises ``pydantic.ValidationError`` on an invalid
    payload.
    """
    return await _write_for_kind(
        conn,
        kind=OutputKind.NEXUS,
        target_id=target_id,
        analyst_id=analyst_id,
        payload=payload,
        derived_from=derived_from,
        analyst_version=analyst_version,
        target_version=target_version,
        run_id=run_id,
        publish_fn=publish_fn,
        schema_uri=schema_uri,
        row_id=row_id,
    )


__all__ = [
    "KIND_NAME",
    "SubstrateWriteFailed",
    "write_alert",
    "write_fact",
    "write_finding",
    "write_hypothesis",
    "write_nexus",
    "write_prediction",
    "write_situation",
]
