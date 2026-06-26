# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Write-side wrappers — every substrate row goes through these.

Public surface:

  * ``write_analyst_output(conn, *, analyst_ctx, kind, output_payload,
    derived_from)`` → ``OutputRow``. Generic analyst-output wrapper. The
    kind picks the table + schema + NATS subject from KIND_REGISTRY.

The legacy target-owned source-kind signal writer (``write_target_signal``)
was retired with L-205 / B5 — SourceActor now writes canonical,
target-agnostic signals (see ``runtime/source_actor.py``).

  * ``write_finding`` / ``write_situation`` / ``write_hypothesis`` /
    ``write_prediction`` / ``write_alert`` / ``write_meta_finding`` /
    ``write_critique`` — thin per-kind specializations.

All analyst-output writes validate the payload against its registered
pydantic model first. Failures route to ``output_dead_letter`` (L-107 §6)
and the wrapper returns ``None`` for the row + the DLQ entry.

NATS event emission is best-effort and pluggable: pass ``publish_fn`` to
the call site. Phase 5 runtime injects this; tests can omit it.

AGE :DerivedFrom edge mirroring is deferred to L-204 (per L-107 §2); we
leave a hook by accepting an ``age_hook`` callable but never call it in
Phase 1.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Sequence
from uuid import UUID, uuid4

import asyncpg
from pydantic import BaseModel, ValidationError

from ._core import (
    AnalystContext,
    ProvenanceFields,
    TargetContext,
    append_derived_from,
    from_analyst,
    from_target,
)
from ..vocabulary import normalize_predicate
from .dlq import OutputDeadLetterEntry, route_to_output_dead_letter
from .kinds import (
    KIND_REGISTRY,
    OutputKind,
    OutputKindSpec,
    spec_for_kind,
)
from .models import (
    AlertPayload,
    CritiquePayload,
    FactPayload,
    FindingPayload,
    HypothesisPayload,
    JournalPayload,
    MetaFindingPayload,
    NexusPayload,
    PredictionPayload,
    SituationPayload,
)


logger = logging.getLogger(__name__)


# Pluggable NATS publish — (subject, payload_bytes) → awaitable.
NatsPublishFn = Callable[[str, bytes], Awaitable[None]]

# Pluggable AGE hook (L-204) — gets the new row id + parent row ids.
AgeEdgeHook = Callable[[UUID, list[UUID]], Awaitable[None]]


# ---------------------------------------------------------------------------
# Source-tier precedence (Holes-A A1 — confidence-tier-aware supersession)
# ---------------------------------------------------------------------------
#
# A fact's ``source_type`` is its provenance class. For auto-supersession we
# rank those classes on a TOTAL ORDER of authority: an AUTHORITATIVE fact (a
# human-curated seed) must NOT be closed by a lower-authority MACHINE-extracted
# one (an ingestion-NER hit or an analyst LLM emission). Within the SAME tier
# recency still wins — a NEW leader fact supersedes the OLD leader fact of the
# same tier exactly as before.
#
# Total order (higher int = more authoritative):
#   seed     == curated   -> 2   (AUTHORITATIVE: human/operator-owned ground truth)
#   ingestion == agent    -> 1   (MACHINE-EXTRACTED: NER hit / LLM emission)
#   <anything else / None>-> 1   (unknown class is treated as machine-extracted)
#
# ``seed`` and ``curated`` are deliberately the SAME rank (neither outranks the
# other — both are operator-blessed); likewise ``ingestion`` and ``agent``. The
# guard blocks ONLY a STRICT downgrade (incoming tier < prior row's tier), so
# same-tier and upgrades pass through untouched.
_SOURCE_TIER_RANK: dict[str, int] = {
    "seed": 2,
    "curated": 2,
    "ingestion": 1,
    "agent": 1,
}
_DEFAULT_SOURCE_TIER_RANK = 1


def _source_tier_rank(source_type: str | None) -> int:
    """Map a fact ``source_type`` onto its authority rank (see the table above).

    An unknown / ``None`` class falls back to the MACHINE-extracted rank (1) so
    an unrecognised producer can never masquerade as authoritative.
    """
    if not source_type:
        return _DEFAULT_SOURCE_TIER_RANK
    return _SOURCE_TIER_RANK.get(source_type.strip().lower(), _DEFAULT_SOURCE_TIER_RANK)


def noisy_or_confidence(existing: float, incoming: float, *, cap: float = 0.99) -> float:
    """Bounded noisy-OR combine of two AGREEING confidences (Holes-A A2).

    When N independent sources corroborate the SAME (subject, predicate, value),
    each adds evidence: the combined belief must RISE above any single source's
    confidence yet never reach certainty. We use the standard noisy-OR::

        combined = 1 - (1 - existing) * (1 - incoming)

    clamped to ``cap`` (default 0.99) so repeated agreement asymptotes just
    below 1.0 and never asserts impossible certainty. Inputs are clamped into
    ``[0, 1]`` first so a malformed/overshooting confidence can't break the
    monotonicity (the result is always >= max(existing, incoming) and < 1.0).
    """
    lo, hi = 0.0, 1.0
    e = lo if existing < lo else hi if existing > hi else existing
    i = lo if incoming < lo else hi if incoming > hi else incoming
    combined = 1.0 - (1.0 - e) * (1.0 - i)
    return combined if combined < cap else cap


# ---------------------------------------------------------------------------
# Return shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalRow:
    id: UUID
    target_id: str
    target_version: str
    produced_at: datetime
    derived_from: list[UUID]
    schema_uri: str


@dataclass(frozen=True)
class OutputRow:
    id: UUID
    kind: OutputKind
    table: str
    analyst_id: str
    analyst_version: str
    run_id: UUID
    produced_at: datetime
    derived_from: list[UUID]
    schema_uri: str
    target_id: str | None
    target_version: str | None


# (write_target_signal — the legacy target-owned source-kind ingestion wrapper
# — was removed with L-205. It INSERTed into the pre-pivot signals columns
# (data/title/target_id/...) that migration 0024 dropped. SourceActor now writes
# canonical, target-agnostic signals; see runtime/source_actor.py.)


# ---------------------------------------------------------------------------
# write_analyst_output (generic)
# ---------------------------------------------------------------------------


async def write_analyst_output(
    conn: asyncpg.Connection,
    *,
    analyst_ctx: AnalystContext,
    kind: OutputKind | str,
    output_payload: BaseModel | dict[str, Any],
    derived_from: Sequence[UUID],
    publish_fn: NatsPublishFn | None = None,
    age_hook: AgeEdgeHook | None = None,
    schema_uri: str | None = None,
    row_id: UUID | None = None,
    source_type: str | None = None,
    seed_batch_id: UUID | None = None,
    source_signal_ids: Sequence[UUID] | None = None,
) -> tuple[OutputRow | None, OutputDeadLetterEntry | None]:
    """Generic analyst-output writer.

    Pipeline:
      1. Resolve kind → spec (table + model + default schema_uri + NATS).
      2. Validate payload against the spec's pydantic model.
         On ValidationError → DLQ insert, return (None, entry).
      3. Build provenance fields via ``from_analyst``.
      4. INSERT into the spec's table (kind-aware projection).
      5. Best-effort NATS publish.
      6. AGE hook (deferred — L-204; never called in Phase 1).

    Returns (OutputRow, None) on success; (None, OutputDeadLetterEntry) on
    schema failure.

    ``source_type`` / ``seed_batch_id`` are the curated-seeding marker
    (planning/SEEDING_SKETCH.md). They are honored ONLY by the ``facts`` and
    ``nexuses`` insert routes (the knowledge-layer tables); other kinds ignore
    them. Both default to ``None`` so existing callers are unchanged: a fact
    write with ``source_type=None`` falls back to the payload's ``source_type``
    (``'agent'``) and a NULL ``seed_batch_id``. A seed write passes
    ``source_type='seed'`` + the batch id so the row is stamped + selectively
    refreshable/purgeable.

    ``source_signal_ids`` (D15) is honored ONLY by the ``nexuses`` route: the
    originating signal/fact UUIDs the nexus was reified from. The ``nexuses``
    insert UNIONS it with ``derived_from`` and the payload's own
    ``source_signal_ids`` and writes the full set to BOTH the ``derived_from``
    lineage array and the ``source_signal_ids`` slice. ``None`` (the default)
    leaves the existing behavior (payload-carried ids + ``derived_from``).
    """
    spec = spec_for_kind(kind)
    effective_schema_uri = schema_uri or spec.schema_uri

    # 1) Validate payload.
    try:
        validated = _coerce_payload(spec, output_payload)
    except ValidationError as err:
        # FK constraint: output_dead_letter.run_id REFERENCES
        # analyst_traces(run_id) ON DELETE SET NULL. If the runtime already
        # wrote a trace row (status='output_schema_fail') for this run we
        # link it; otherwise leave run_id NULL so the DLQ insert succeeds
        # independently. Phase 5 runtime is responsible for the trace ↔
        # DLQ cross-link.
        linked_run_id = await _existing_trace_run_id(conn, analyst_ctx.run_id)
        entry = await route_to_output_dead_letter(
            conn,
            analyst_id=analyst_ctx.analyst_id,
            analyst_version=analyst_ctx.analyst_version,
            run_id=linked_run_id,
            declared_schema_uri=effective_schema_uri,
            attempted_payload=_payload_as_dict(output_payload),
            error=err,
        )
        return None, entry

    # 2) Build provenance fields.
    prov = from_analyst(
        analyst_ctx,
        schema_uri=effective_schema_uri,
        derived_from=list(derived_from),
    )

    # 3) INSERT.
    new_id = row_id or uuid4()
    produced_at = prov.produced_at
    await _insert_for_spec(
        conn,
        spec=spec,
        row_id=new_id,
        kind_str=spec.kind.value,
        payload=validated,
        prov=prov,
        produced_at=produced_at,
        effective_schema_uri=effective_schema_uri,
        source_type=source_type,
        seed_batch_id=seed_batch_id,
        source_signal_ids=source_signal_ids,
    )

    # 4) Best-effort NATS publish.
    if publish_fn is not None and spec.nats_subject_pattern:
        subject = spec.nats_subject_pattern.format(
            analyst_id=prov.analyst_id or "_",
            target_id=prov.target_id or "_",
        )
        envelope = {
            "id": str(new_id),
            "kind": spec.kind.value,
            "analyst_id": prov.analyst_id,
            "analyst_version": prov.analyst_version,
            "run_id": str(prov.run_id) if prov.run_id else None,
            "target_id": prov.target_id,
            "produced_at": produced_at.isoformat(),
            "schema_uri": effective_schema_uri,
            "derived_from": [str(u) for u in prov.derived_from],
        }
        await _safe_publish(publish_fn, subject, envelope)

    # 5) AGE hook — mirror this output's lineage into the graph as
    #    (:Output)-[:DerivedFrom]->(:Output) edges. Wired by the runtime
    #    (dapr_actors) behind the opt-in LEGBA_AGE_DERIVED_FROM flag via
    #    provenance.output_graph.make_conn_age_output_hook; the relational
    #    `derived_from UUID[]` array + recursive-CTE walk remains the lineage
    #    source of truth, so an unset hook (the default) changes nothing. The
    #    hook is best-effort and must not fail the write.
    if age_hook is not None:
        await age_hook(new_id, list(prov.derived_from))

    return (
        OutputRow(
            id=new_id,
            kind=spec.kind,
            table=spec.table,
            analyst_id=prov.analyst_id or "",
            analyst_version=prov.analyst_version or "",
            run_id=prov.run_id or analyst_ctx.run_id,
            produced_at=produced_at,
            derived_from=list(prov.derived_from),
            schema_uri=effective_schema_uri,
            target_id=prov.target_id,
            target_version=prov.target_version,
        ),
        None,
    )


# ---------------------------------------------------------------------------
# Per-kind specializations
# ---------------------------------------------------------------------------


async def write_finding(
    conn: asyncpg.Connection,
    *,
    analyst_ctx: AnalystContext,
    payload: FindingPayload | dict[str, Any],
    derived_from: Sequence[UUID],
    **kwargs: Any,
) -> tuple[OutputRow | None, OutputDeadLetterEntry | None]:
    return await write_analyst_output(
        conn,
        analyst_ctx=analyst_ctx,
        kind=OutputKind.FINDING,
        output_payload=payload,
        derived_from=derived_from,
        **kwargs,
    )


async def write_situation(
    conn: asyncpg.Connection,
    *,
    analyst_ctx: AnalystContext,
    payload: SituationPayload | dict[str, Any],
    derived_from: Sequence[UUID],
    **kwargs: Any,
) -> tuple[OutputRow | None, OutputDeadLetterEntry | None]:
    return await write_analyst_output(
        conn,
        analyst_ctx=analyst_ctx,
        kind=OutputKind.SITUATION,
        output_payload=payload,
        derived_from=derived_from,
        **kwargs,
    )


async def write_hypothesis(
    conn: asyncpg.Connection,
    *,
    analyst_ctx: AnalystContext,
    payload: HypothesisPayload | dict[str, Any],
    derived_from: Sequence[UUID],
    **kwargs: Any,
) -> tuple[OutputRow | None, OutputDeadLetterEntry | None]:
    return await write_analyst_output(
        conn,
        analyst_ctx=analyst_ctx,
        kind=OutputKind.HYPOTHESIS,
        output_payload=payload,
        derived_from=derived_from,
        **kwargs,
    )


async def write_prediction(
    conn: asyncpg.Connection,
    *,
    analyst_ctx: AnalystContext,
    payload: PredictionPayload | dict[str, Any],
    derived_from: Sequence[UUID],
    **kwargs: Any,
) -> tuple[OutputRow | None, OutputDeadLetterEntry | None]:
    return await write_analyst_output(
        conn,
        analyst_ctx=analyst_ctx,
        kind=OutputKind.PREDICTION,
        output_payload=payload,
        derived_from=derived_from,
        **kwargs,
    )


async def write_alert(
    conn: asyncpg.Connection,
    *,
    analyst_ctx: AnalystContext,
    payload: AlertPayload | dict[str, Any],
    derived_from: Sequence[UUID],
    **kwargs: Any,
) -> tuple[OutputRow | None, OutputDeadLetterEntry | None]:
    return await write_analyst_output(
        conn,
        analyst_ctx=analyst_ctx,
        kind=OutputKind.ALERT,
        output_payload=payload,
        derived_from=derived_from,
        **kwargs,
    )


async def write_meta_finding(
    conn: asyncpg.Connection,
    *,
    analyst_ctx: AnalystContext,
    payload: MetaFindingPayload | dict[str, Any],
    derived_from: Sequence[UUID],
    **kwargs: Any,
) -> tuple[OutputRow | None, OutputDeadLetterEntry | None]:
    return await write_analyst_output(
        conn,
        analyst_ctx=analyst_ctx,
        kind=OutputKind.META_FINDING,
        output_payload=payload,
        derived_from=derived_from,
        **kwargs,
    )


async def write_critique(
    conn: asyncpg.Connection,
    *,
    analyst_ctx: AnalystContext,
    payload: CritiquePayload | dict[str, Any],
    derived_from: Sequence[UUID],
    **kwargs: Any,
) -> tuple[OutputRow | None, OutputDeadLetterEntry | None]:
    """Write an analyst-kind critique row to the generic `analyst_outputs`
    table.

    NOTE: This is NOT the eval-loop critique table (``analyst_critiques``).
    The eval-loop critic (L-175) writes to ``analyst_critiques`` directly
    with judge_analyst_id semantics. This wrapper is for analyst-kind
    outputs whose primary product is a narrative critique.
    """
    return await write_analyst_output(
        conn,
        analyst_ctx=analyst_ctx,
        kind=OutputKind.CRITIQUE,
        output_payload=payload,
        derived_from=derived_from,
        **kwargs,
    )


async def write_fact(
    conn: asyncpg.Connection,
    *,
    analyst_ctx: AnalystContext,
    payload: FactPayload | dict[str, Any],
    derived_from: Sequence[UUID],
    **kwargs: Any,
) -> tuple[OutputRow | None, OutputDeadLetterEntry | None]:
    """Write an analyst-/workflow-emitted ``fact`` row to the dedicated
    ``facts`` table (anchor §5 PIECE 2).

    Mirrors ``write_hypothesis``/``write_finding``. The underlying
    ``_insert_fact`` uses the ``idx_facts_temporal_triple_open`` ``ON
    CONFLICT`` upsert (the facts table's partial-on-open UNIQUE index —
    hypotheses have none) so a repeated OPEN triple lifts confidence + unions
    lineage instead of raising.

    The ingest-time ``fact_extractor`` stage writes source-owned facts via
    its own lower-level ``_insert_fact`` call (no analyst_id); this wrapper
    is the analyst-output path so both producers share one write contract.
    """
    return await write_analyst_output(
        conn,
        analyst_ctx=analyst_ctx,
        kind=OutputKind.FACT,
        output_payload=payload,
        derived_from=derived_from,
        **kwargs,
    )


async def write_nexus(
    conn: asyncpg.Connection,
    *,
    analyst_ctx: AnalystContext,
    payload: NexusPayload | dict[str, Any],
    derived_from: Sequence[UUID],
    source_signal_ids: Sequence[UUID] | None = None,
    **kwargs: Any,
) -> tuple[OutputRow | None, OutputDeadLetterEntry | None]:
    """Write a reified-relationship ``nexus`` row to the dedicated ``nexuses``
    table (PIECE A — reified typed Nexus).

    Mirrors ``write_fact``/``write_hypothesis``. The underlying
    ``_insert_nexus`` uses the ``idx_nexuses_triple_open`` open-only partial
    UNIQUE index (the nexuses table's per-open-triple constraint — mirrors
    facts) so a repeated OPEN triple lifts confidence + unions lineage instead
    of raising, and runs ``supersede_prior_nexuses`` first so a value/polarity
    CHANGE for an existing ``(subject, intermediary, object, rel_type)`` closes
    the prior open row(s) and opens this one. ``rel_type`` is normalized to ONE
    canonical lowercase-spaced vocabulary on write (``normalize_predicate``), so
    mixed-case producers (``CoOccursWith`` vs ``co-occurs-with``) converge on
    the single key the open-triple unique index + supersession dedup on (D18).

    ``source_signal_ids`` (D15) — the originating signal/fact UUIDs the nexus
    was reified FROM. Producers (the reifier, proposed_edge_governance) gather
    these and pass them here; ``write_nexus`` populates BOTH the relational
    ``derived_from`` lineage array AND the ``source_signal_ids`` convenience
    slice on the row so 100%-empty-provenance nexuses (lineage couldn't enter
    the tier) can't recur. The param is keyword-default ``None`` for
    backward-compat: when omitted, the ids fall back to ``derived_from`` (and
    the payload's own ``source_signal_ids``) so existing call sites are
    unchanged. The explicit param, ``derived_from``, and any payload-carried
    ``source_signal_ids`` are UNIONED — a producer can supply the ids via any of
    the three and the row carries the full set in both columns.
    """
    return await write_analyst_output(
        conn,
        analyst_ctx=analyst_ctx,
        kind=OutputKind.NEXUS,
        output_payload=payload,
        derived_from=derived_from,
        source_signal_ids=source_signal_ids,
        **kwargs,
    )


async def write_journal(
    conn: asyncpg.Connection,
    *,
    analyst_ctx: AnalystContext,
    payload: JournalPayload | dict[str, Any],
    derived_from: Sequence[UUID] = (),
    **kwargs: Any,
) -> tuple[OutputRow | None, OutputDeadLetterEntry | None]:
    """Write one ``journal`` row (entry | consolidation) to the dedicated
    ``journal_entries`` table (plan §3.2 / §8).

    Mirrors ``write_situation``/``write_nexus`` but with one hard difference:
    the journal is a **direction-asymmetric lineage node** — it carries its
    citations ONLY in the in-payload ``claims`` / ``cited_substrate_refs``, and
    ``journal_entries.derived_from`` is written **empty** so a downstream
    lineage walk FROM a fact/situation/nexus can NEVER surface the lyrical
    journal row inside the very provenance graph the Why room renders as "the
    chain" (§3.5). We therefore IGNORE any ``derived_from`` passed in and force
    it empty — the off-chain invariant is not left to call-site discipline.

    The journal must NEVER write a fact / nexus / finding / situation /
    hypothesis (§3.1); this is the only write helper it is granted (and the
    grant layer §7.6 enforces the rest).
    """
    if derived_from:
        logger.warning(
            "write_journal.derived_from_ignored analyst=%s n=%d — the journal "
            "is off the chain; derived_from is forced empty (§3.5)",
            analyst_ctx.analyst_id, len(tuple(derived_from)),
        )
    return await write_analyst_output(
        conn,
        analyst_ctx=analyst_ctx,
        kind=OutputKind.JOURNAL,
        output_payload=payload,
        derived_from=[],  # OFF the chain — always empty (§3.5).
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Per-table INSERT routing
# ---------------------------------------------------------------------------


async def _insert_for_spec(
    conn: asyncpg.Connection,
    *,
    spec: OutputKindSpec,
    row_id: UUID,
    kind_str: str,
    payload: BaseModel,
    prov: ProvenanceFields,
    produced_at: datetime,
    effective_schema_uri: str,
    source_type: str | None = None,
    seed_batch_id: UUID | None = None,
    source_signal_ids: Sequence[UUID] | None = None,
) -> None:
    """Dispatch INSERT based on the spec's target table.

    ``source_type`` / ``seed_batch_id`` are the curated-seeding marker; only
    the knowledge-layer tables (``facts`` / ``nexuses``) honor them.
    ``source_signal_ids`` is honored only by the ``nexuses`` route (D15).
    """
    table = spec.table
    if table == "analyst_outputs":
        await _insert_analyst_output(
            conn,
            row_id=row_id,
            kind_str=kind_str,
            payload=payload,
            prov=prov,
            produced_at=produced_at,
            effective_schema_uri=effective_schema_uri,
        )
    elif table == "situations":
        await _insert_situation(
            conn,
            row_id=row_id,
            payload=payload,
            prov=prov,
            produced_at=produced_at,
            effective_schema_uri=effective_schema_uri,
        )
    elif table == "hypotheses":
        await _insert_hypothesis(
            conn,
            row_id=row_id,
            payload=payload,
            prov=prov,
            produced_at=produced_at,
            effective_schema_uri=effective_schema_uri,
        )
    elif table == "facts":
        await _insert_fact(
            conn,
            row_id=row_id,
            payload=payload,
            prov=prov,
            produced_at=produced_at,
            effective_schema_uri=effective_schema_uri,
            source_type=source_type,
            seed_batch_id=seed_batch_id,
        )
    elif table == "nexuses":
        await _insert_nexus(
            conn,
            row_id=row_id,
            payload=payload,
            prov=prov,
            produced_at=produced_at,
            effective_schema_uri=effective_schema_uri,
            source_type=source_type,
            seed_batch_id=seed_batch_id,
            source_signal_ids=source_signal_ids,
        )
    elif table == "journal_entries":
        # Consolidation supersession runs INSIDE this route (close-prior +
        # insert-new are one logical step on the same conn). Entries are pure
        # append. derived_from is forced empty here regardless of what was
        # passed — the off-chain invariant (§3.5).
        await _insert_journal_entry(
            conn,
            row_id=row_id,
            payload=payload,
            prov=prov,
            produced_at=produced_at,
            effective_schema_uri=effective_schema_uri,
        )
    else:
        raise NotImplementedError(
            f"no INSERT routing for table {table!r} (kind={spec.kind.value!r}); "
            f"register one in writes._insert_for_spec when the table lands."
        )


async def _insert_analyst_output(
    conn: asyncpg.Connection,
    *,
    row_id: UUID,
    kind_str: str,
    payload: BaseModel,
    prov: ProvenanceFields,
    produced_at: datetime,
    effective_schema_uri: str,
) -> None:
    severity = getattr(payload, "severity", None)
    data_payload = payload.model_dump(mode="json")
    await conn.execute(
        """
        INSERT INTO analyst_outputs (
            id, kind, title, body, confidence, severity, data,
            target_id, target_version, analyst_id, analyst_version,
            produced_at, derived_from, schema_uri, run_id
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7::jsonb,
            $8, $9, $10, $11,
            $12, $13, $14, $15
        )
        """,
        row_id,
        kind_str,
        getattr(payload, "title", ""),
        getattr(payload, "body", ""),
        float(getattr(payload, "confidence", 0.5)),
        severity,
        json.dumps(data_payload, default=_json_default),
        prov.target_id,
        prov.target_version,
        prov.analyst_id,
        prov.analyst_version,
        produced_at,
        list(prov.derived_from),
        effective_schema_uri,
        prov.run_id,
    )


async def _insert_situation(
    conn: asyncpg.Connection,
    *,
    row_id: UUID,
    payload: BaseModel,
    prov: ProvenanceFields,
    produced_at: datetime,
    effective_schema_uri: str,
) -> None:
    p = payload                                          # type: ignore[assignment]
    data_payload = p.model_dump(mode="json")
    sig = getattr(p, "situation_signature", None)
    valid_from = getattr(p, "valid_from", None)
    valid_until = getattr(p, "valid_until", None)
    # The upsert key is (situation_signature, analyst_id); NULLs are distinct in a
    # unique index, so a signatured situation written with a NULL analyst_id would
    # SILENTLY DUPLICATE on re-emit instead of upserting. Situations are always
    # analyst-produced, so require analyst_id when a signature is present and fail
    # LOUD rather than corrupt the dedup (review follow-up — guards the standard
    # path before future producers use it for situations).
    if sig and not prov.analyst_id:
        raise ValueError(
            "write_situation: a signatured situation requires an analyst_id "
            "(the upsert key is (situation_signature, analyst_id); a NULL "
            "analyst_id would duplicate instead of upsert)"
        )
    # UPSERT on the (situation_signature, analyst_id) key (migration 0040) when a
    # signature is present — re-emitting a situation UPDATES the open row instead
    # of duplicating it (the prior plain INSERT had no upsert key, which is why
    # situation_clustering bypassed this path). A signature-less situation has no
    # upsert target, so it falls back to a plain INSERT.
    if sig:
        await conn.execute(
            """
            INSERT INTO situations (
                id, data, name, status, category, last_event_at, event_count,
                intensity_score,
                target_id, target_version, analyst_id, analyst_version,
                produced_at, derived_from, schema_uri, run_id,
                situation_signature, valid_from, valid_until
            ) VALUES (
                $1, $2::jsonb, $3, $4, $5, $6, $7,
                $8,
                $9, $10, $11, $12,
                $13, $14, $15, $16,
                $17, $18, $19
            )
            ON CONFLICT (situation_signature, analyst_id)
                WHERE situation_signature IS NOT NULL
            DO UPDATE SET
                data=EXCLUDED.data, name=EXCLUDED.name, status=EXCLUDED.status,
                category=EXCLUDED.category, last_event_at=EXCLUDED.last_event_at,
                event_count=EXCLUDED.event_count,
                intensity_score=EXCLUDED.intensity_score,
                derived_from=EXCLUDED.derived_from,
                valid_until=EXCLUDED.valid_until,
                valid_from=LEAST(situations.valid_from, EXCLUDED.valid_from),
                updated_at=NOW()
            """,
            row_id,
            json.dumps(data_payload.get("data", {}), default=_json_default),
            p.name,                                      # type: ignore[attr-defined]
            p.status,                                    # type: ignore[attr-defined]
            p.category,                                  # type: ignore[attr-defined]
            p.last_event_at,                             # type: ignore[attr-defined]
            p.event_count,                               # type: ignore[attr-defined]
            float(p.intensity_score),                    # type: ignore[attr-defined]
            prov.target_id,
            prov.target_version,
            prov.analyst_id,
            prov.analyst_version,
            produced_at,
            list(prov.derived_from),
            effective_schema_uri,
            prov.run_id,
            sig,
            valid_from,
            valid_until,
        )
        return
    await conn.execute(
        """
        INSERT INTO situations (
            id, data, name, status, category, last_event_at, event_count,
            intensity_score,
            target_id, target_version, analyst_id, analyst_version,
            produced_at, derived_from, schema_uri, run_id,
            valid_from, valid_until
        ) VALUES (
            $1, $2::jsonb, $3, $4, $5, $6, $7,
            $8,
            $9, $10, $11, $12,
            $13, $14, $15, $16,
            $17, $18
        )
        """,
        row_id,
        json.dumps(data_payload.get("data", {}), default=_json_default),
        p.name,                                          # type: ignore[attr-defined]
        p.status,                                        # type: ignore[attr-defined]
        p.category,                                      # type: ignore[attr-defined]
        p.last_event_at,                                 # type: ignore[attr-defined]
        p.event_count,                                   # type: ignore[attr-defined]
        float(p.intensity_score),                        # type: ignore[attr-defined]
        prov.target_id,
        prov.target_version,
        prov.analyst_id,
        prov.analyst_version,
        produced_at,
        list(prov.derived_from),
        effective_schema_uri,
        prov.run_id,
        valid_from,
        valid_until,
    )


async def _insert_hypothesis(
    conn: asyncpg.Connection,
    *,
    row_id: UUID,
    payload: BaseModel,
    prov: ProvenanceFields,
    produced_at: datetime,
    effective_schema_uri: str,
) -> None:
    p = payload                                          # type: ignore[assignment]
    await conn.execute(
        """
        INSERT INTO hypotheses (
            id, situation_id, thesis, counter_thesis,
            diagnostic_evidence, supporting_signals, refuting_signals,
            evidence_balance, status,
            target_id, target_version, analyst_id, analyst_version,
            produced_at, derived_from, schema_uri, run_id
        ) VALUES (
            $1, $2, $3, $4,
            $5::jsonb, $6, $7,
            $8, $9,
            $10, $11, $12, $13,
            $14, $15, $16, $17
        )
        """,
        row_id,
        getattr(p, "situation_id", None),
        p.thesis,                                        # type: ignore[attr-defined]
        getattr(p, "counter_thesis", ""),
        json.dumps(getattr(p, "diagnostic_evidence", []), default=_json_default),
        list(getattr(p, "supporting_signals", []) or []),
        list(getattr(p, "refuting_signals", []) or []),
        int(getattr(p, "evidence_balance", 0)),
        getattr(p, "status", "active"),
        prov.target_id,
        prov.target_version,
        prov.analyst_id,
        prov.analyst_version,
        produced_at,
        list(prov.derived_from),
        effective_schema_uri,
        prov.run_id,
    )


async def supersede_prior_facts(
    conn: asyncpg.Connection,
    *,
    subject: str,
    predicate: str,
    value: str,
    new_fact_id: UUID,
    incoming_source_type: str | None = None,
) -> int:
    """Close any open fact(s) for ``(lower(subject), lower(predicate))`` whose
    VALUE differs from the incoming ``value``, pointing them at ``new_fact_id``
    — UNLESS the prior row outranks the incoming fact on source authority
    (Holes-A A1 — confidence-tier-aware supersession).

    This is the altitude-0 auto-supersession the old system had (PIECE B —
    temporal-fact hardening): the canonical "what is true now" for a
    subject+predicate is the single open row (``valid_until IS NULL AND
    superseded_by IS NULL``). When a new fact asserts a DIFFERENT value for the
    same subject+predicate, the prior open row(s) are closed:
    ``valid_until = now()`` + ``superseded_by = <new id>``. The new row is then
    inserted open by the caller (``_insert_fact`` / ``_insert_ingestion_fact``).

    Contract / safety:
      * **source-tier guard (A1)** — an incoming MACHINE-extracted fact
        (``ingestion``/``agent``) does NOT close an open AUTHORITATIVE fact
        (``seed``/``curated``) for the same subject+predicate. Authority ranks
        on the total order in :data:`_SOURCE_TIER_RANK`
        (``seed == curated > ingestion == agent``); the UPDATE skips any prior
        row whose tier is STRICTLY higher than the incoming one. WITHIN the same
        tier recency still wins, so a NEW leader fact supersedes the OLD leader
        fact of the same tier exactly as before — the guard blocks only the
        downgrade direction. When ``incoming_source_type`` is ``None`` (e.g. the
        operator journal-correction caller, which is maximally authoritative) NO
        tier filtering is applied and the historical behavior is preserved.
      * **value-differs only** — a re-assert of the SAME value is NOT a
        supersession; that path stays the ``idx_facts_temporal_triple_open``
        ``ON CONFLICT`` upsert (confidence lift + lineage union). The
        ``lower(value) <> lower($3)`` predicate guarantees the identical-triple
        row is never closed by its own re-ingest.
      * **idempotent** — only rows still open
        (``valid_until IS NULL AND superseded_by IS NULL``) are touched; a
        replay closes nothing new once the prior is already superseded.
      * **same connection** — the caller runs this immediately before the
        insert on the same ``conn`` so the close + open are one logical step
        (the dapr write path acquires one connection per output).

    Returns the number of prior rows closed (0 when this is the first
    assertion of the subject+predicate, a same-value re-assert, or every
    differing-value prior row outranks the incoming fact on authority).
    """
    # A1 — source-tier precedence. When the caller declares the incoming fact's
    # source_type we forbid closing any prior row whose authority rank is
    # STRICTLY higher (an ingestion/agent fact must not retire a seed/curated
    # one). A NULL incoming rank disables the filter (historical / operator
    # correction path stays unconditional). The guard is `source_type IS NULL`
    # tolerant — a legacy untyped row is treated as the machine-extracted rank,
    # so it can never silently outrank and block a legitimate supersession.
    incoming_rank = (
        None if incoming_source_type is None
        else _source_tier_rank(incoming_source_type)
    )
    result = await conn.execute(
        """
        UPDATE facts
           SET valid_until   = now(),
               superseded_by = $4,
               updated_at    = now()
         WHERE lower(subject)   = lower($1)
           AND lower(predicate) = lower($2)
           AND lower(value)    <> lower($3)
           AND valid_until IS NULL
           AND superseded_by IS NULL
           AND id <> $4
           AND (
                 $5::int IS NULL
                 OR CASE lower(coalesce(source_type, ''))
                        WHEN 'seed'      THEN 2
                        WHEN 'curated'   THEN 2
                        WHEN 'ingestion' THEN 1
                        WHEN 'agent'     THEN 1
                        ELSE 1
                    END <= $5::int
               )
        """,
        subject,
        predicate,
        value,
        new_fact_id,
        incoming_rank,
    )
    try:
        return int(result.split()[-1]) if result else 0
    except (ValueError, IndexError):                     # pragma: no cover
        return 0


async def collapse_open_triple(
    conn: asyncpg.Connection,
    *,
    subject: str,
    predicate: str,
    value: str,
    new_fact_id: UUID,
    confidence: float,
    derived_from: Sequence[UUID],
    valid_from: datetime | None = None,
) -> UUID | None:
    """Collapse a standing fact triple onto ONE open row regardless of
    ``valid_from`` drift (D17 — full-triple supersession leaked open duplicates
    via per-cycle valid_from drift).

    The ``idx_facts_temporal_triple_open`` partial-unique index keys on the FULL
    quad INCLUDING ``COALESCE(valid_from, '1970-01-01')``, so the SAME
    ``(subject, predicate, value)`` re-asserted from N cycles with N distinct
    event-times accumulates N OPEN rows — the live "Russia located in UK" ×8
    noise the ON CONFLICT upsert never catches (its conflict target carries the
    valid_from dimension, so a drifted valid_from is a NEW conflict key, not a
    hit).

    This helper closes that dimension for SAME-value OPEN rows BEFORE the
    insert: if an open row for ``(lower(subject), lower(predicate),
    lower(value))`` already exists (ANY valid_from), it is refreshed in place
    (confidence → noisy-OR combine capped at 0.99 per A2, lineage unioned,
    EARLIEST valid_from kept) and its id is
    returned so the caller SKIPS the insert. Returns ``None`` when no open row
    exists (the caller proceeds to insert the fresh open row).

    Contract / safety:
      * **same-value only** — the match is on ``lower(value) = lower($3)``; a
        DIFFERENT value is NOT collapsed here (that path is
        :func:`supersede_prior_facts`, which the caller runs first).
      * **open-only** — only rows still open (``valid_until IS NULL AND
        superseded_by IS NULL``) are touched; a closed/superseded row is never
        resurrected (matches the partial index's WHERE).
      * **confidence aggregation on agreement (A2)** — a replay of the same
        triple is CORROBORATION from another source, so confidence is combined
        with a bounded noisy-OR (``1 - (1-existing)*(1-incoming)``, clamped to
        ``<= 0.99``) rather than lifted to the plain max. N agreeing sources
        therefore raise confidence ABOVE any single one yet never reach
        certainty. Before this was ``GREATEST`` (max), which never rose above
        the single most-confident source. Lineage is still unioned; the row
        count is unchanged (idempotent in row terms, monotone-increasing in
        confidence toward the 0.99 cap).
      * **deterministic pick** — when (legacy data) more than one open row for
        the triple exists, the EARLIEST (``valid_from ASC, created_at ASC``) is
        refreshed; the caller's :func:`supersede_prior_facts` already collapsed
        differing-value rows, and future writes converge on this one open row.

    Runs on the caller's connection so the collapse + insert are one logical
    step. Shared by BOTH fact producers (the analyst ``_insert_fact`` path and
    the ingest ``fact_extractor._insert_ingestion_fact`` path) so a standing
    triple keeps ONE open row across producers.
    """
    existing_id = await conn.fetchval(
        """
        UPDATE facts
           -- A2: bounded noisy-OR combine of agreeing confidences, capped at
           -- 0.99 so corroboration raises belief above any single source but
           -- never reaches certainty (was GREATEST/max).
           SET confidence   = LEAST(
                                 0.99,
                                 1.0 - (1.0 - facts.confidence) * (1.0 - $4)
                               ),
               derived_from = COALESCE((SELECT array_agg(DISTINCT e)
                               FROM unnest(facts.derived_from || $5::uuid[]) e),
                              '{}'::uuid[]),
               -- LEAST/GREATEST skip NULL args in Postgres (NULL only if ALL
               -- are NULL), so this keeps the EARLIEST known valid_from and is
               -- a no-op when either side is NULL — matches the ingest path.
               valid_from   = LEAST(facts.valid_from, $6),
               updated_at   = now()
         WHERE id = (
                 SELECT id FROM facts
                  WHERE lower(subject)   = lower($1)
                    AND lower(predicate) = lower($2)
                    AND lower(value)     = lower($3)
                    AND valid_until IS NULL
                    AND superseded_by IS NULL
                    AND id <> $7
                  ORDER BY valid_from ASC, created_at ASC
                  LIMIT 1
               )
        RETURNING id
        """,
        subject,
        predicate,
        value,
        float(confidence),
        list(derived_from),
        valid_from,
        new_fact_id,
    )
    return existing_id


async def _insert_fact(
    conn: asyncpg.Connection,
    *,
    row_id: UUID,
    payload: BaseModel,
    prov: ProvenanceFields,
    produced_at: datetime,
    effective_schema_uri: str,
    source_type: str | None = None,
    seed_batch_id: UUID | None = None,
) -> None:
    """Insert (or upsert) one ``facts`` row.

    ``source_type`` overrides the payload's ``source_type`` when given (the
    curated-seeding path passes ``'seed'``); ``None`` preserves the existing
    behavior (payload's ``source_type``, default ``'agent'``).
    ``seed_batch_id`` stamps the row's owning seed batch (NULL for non-seed
    writes). On a same-triple upsert the marker is left untouched — a re-import
    is a no-op, an original live row is never re-stamped as seed.

    The ``facts`` table carries the ``idx_facts_temporal_triple_open``
    PARTIAL UNIQUE index on ``(lower(subject), lower(predicate),
    lower(value), COALESCE(valid_from, '1970-01-01'))`` scoped to OPEN rows
    only (``valid_until IS NULL AND superseded_by IS NULL``; migration 0032)
    — UNLIKE hypotheses, which have no unique constraint. So a second
    identical OPEN triple+valid_from would raise without ``ON CONFLICT``. We
    upsert: combine confidence with a bounded noisy-OR (A2 — corroboration
    raises belief above any single source, capped at 0.99) and union the
    lineage arrays. This is the same idempotency contract the ``fact_extractor``
    stage uses, so the analyst-emitted path and the ingest path agree.

    The conflict-inference ``WHERE valid_until IS NULL AND superseded_by IS
    NULL`` predicate matches the partial index so the upsert can ONLY land on
    the single open row. Closed (superseded) rows that retain the same triple
    are invisible to conflict inference, so a re-assert of a previously-closed
    value inserts a fresh open row instead of re-opening the closed one — no
    dangling ``superseded_by`` pointer, no stale closed row resurrected (PIECE
    B temporal-fact hardening).

    Before the insert we run ``supersede_prior_facts`` so a value-CHANGE for an
    existing ``(subject, predicate)`` closes the prior open row(s) and opens
    this one (PIECE B). The incoming fact's resolved ``source_type`` is passed
    through so A1's source-tier guard refuses to close a higher-authority prior
    row (an analyst/ingestion fact does not retire a seed/curated one), while
    same-tier recency-wins updates (e.g. a new leader fact) still supersede. A
    same-value re-assert closes nothing (the upsert path owns it).
    """
    p = payload                                          # type: ignore[assignment]
    data_payload = getattr(p, "data", {}) or {}
    evidence_set = getattr(p, "evidence_set", {}) or {}
    # Converge the predicate vocabulary at the write path (PIECE B data
    # quality): the seed driver emits CamelCase ("LeaderOf"), the ingest
    # extractor lowercase-spaced ("leader of") — same relation, two surface
    # forms. Normalize to the canonical lowercase-spaced form so the
    # lower(predicate) supersession/dedup key lines up across both producers.
    predicate = normalize_predicate(getattr(p, "predicate"))
    # Resolve the incoming fact's authority class BEFORE supersession so A1's
    # source-tier guard can refuse to close a higher-tier prior row (an
    # analyst/ingestion fact must not retire a seed/curated one). The override
    # arg wins (curated-seeding passes 'seed'); else the payload's source_type
    # (default 'agent').
    effective_source_type = source_type or getattr(p, "source_type", "agent")
    await supersede_prior_facts(
        conn,
        subject=getattr(p, "subject"),
        predicate=predicate,
        value=getattr(p, "value"),
        new_fact_id=row_id,
        incoming_source_type=effective_source_type,
    )
    # D17 — collapse a standing triple onto ONE open row regardless of
    # valid_from drift. The ON CONFLICT upsert below keys on the FULL quad
    # (including COALESCE(valid_from, '1970-01-01')), so the SAME triple
    # re-asserted across cycles with drifting valid_from accumulates duplicate
    # OPEN rows. collapse_open_triple refreshes the existing open row in place
    # (confidence→noisy-OR per A2, lineage union, earliest valid_from) and, when
    # it hits, we SKIP the insert — the analyst path now matches the ingest
    # path's same-triple dedup so both producers keep one open row.
    collapsed_into = await collapse_open_triple(
        conn,
        subject=getattr(p, "subject"),
        predicate=predicate,
        value=getattr(p, "value"),
        new_fact_id=row_id,
        confidence=float(getattr(p, "confidence", 1.0)),
        derived_from=list(prov.derived_from),
        valid_from=getattr(p, "valid_from", None),
    )
    if collapsed_into is not None:
        # An open row already carries this exact triple — refreshed in place,
        # no duplicate inserted (mirrors _insert_ingestion_fact).
        return
    await conn.execute(
        """
        INSERT INTO facts (
            id, subject, predicate, value, confidence, source_type,
            source_cycle, valid_from, valid_until, geo_lat, geo_lon, data,
            evidence_set, target_id, target_version, analyst_id,
            analyst_version, produced_at, derived_from, schema_uri, run_id,
            seed_batch_id
        ) VALUES (
            $1, $2, $3, $4, $5, $6,
            $7, $8, $9, $10, $11, $12::jsonb,
            $13::jsonb, $14, $15, $16,
            $17, $18, $19, $20, $21,
            $22
        )
        ON CONFLICT (lower(subject), lower(predicate), lower(value),
                     COALESCE(valid_from, '1970-01-01 00:00:00+00'::timestamptz))
                 WHERE valid_until IS NULL AND superseded_by IS NULL
        DO UPDATE SET
            -- A2: same-triple re-assert is CORROBORATION — combine confidences
            -- with a bounded noisy-OR (capped at 0.99) so N agreeing sources
            -- raise belief above any single one without ever reaching
            -- certainty (was GREATEST/max).
            confidence   = LEAST(
                             0.99,
                             1.0 - (1.0 - facts.confidence) * (1.0 - EXCLUDED.confidence)
                           ),
            -- COALESCE to '{}' — array_agg over zero rows is NULL, which would
            -- violate the derived_from NOT NULL when BOTH sides are empty (e.g.
            -- a seed-fact re-import, where lineage is empty on both rows). The
            -- nexus upsert already guards this the same way.
            derived_from = COALESCE((SELECT array_agg(DISTINCT e)
                            FROM unnest(facts.derived_from || EXCLUDED.derived_from) e),
                           '{}'::uuid[]),
            -- Carry a NEWLY-supplied creation-time TTL on a same-triple
            -- re-assert, but NEVER clobber an existing one to NULL (the
            -- conflict target is OPEN rows only, so this can never overwrite a
            -- supersession close — those rows are not visible to ON CONFLICT).
            valid_until  = COALESCE(EXCLUDED.valid_until, facts.valid_until),
            updated_at   = now()
        """,
        row_id,
        getattr(p, "subject"),
        predicate,
        getattr(p, "value"),
        float(getattr(p, "confidence", 1.0)),
        effective_source_type,
        getattr(p, "source_cycle", None),
        getattr(p, "valid_from", None),
        getattr(p, "valid_until", None),
        getattr(p, "geo_lat", None),
        getattr(p, "geo_lon", None),
        json.dumps(data_payload, default=_json_default),
        json.dumps(evidence_set, default=_json_default),
        prov.target_id,
        prov.target_version,
        prov.analyst_id,
        prov.analyst_version,
        produced_at,
        list(prov.derived_from),
        effective_schema_uri,
        prov.run_id,
        seed_batch_id,
    )


async def supersede_prior_nexuses(
    conn: asyncpg.Connection,
    *,
    subject: str,
    intermediary: str | None,
    object_: str,
    rel_type: str,
    polarity: int,
    label: str,
    new_nexus_id: UUID,
) -> int:
    """Close any open nexus(es) for the typed triple
    ``(lower(subject), lower(COALESCE(intermediary,'')), lower(object),
    lower(rel_type))`` whose VALUE (polarity OR label) differs from the
    incoming one, pointing them at ``new_nexus_id`` (PIECE A — mirrors
    :func:`supersede_prior_facts`).

    The canonical "what holds now" for a reified relationship is the single
    open row (``valid_until IS NULL AND superseded_by IS NULL``). When the
    reifier re-types the SAME triple with a DIFFERENT polarity sign or label,
    the prior open row(s) are closed (``valid_until = now()`` +
    ``superseded_by = <new id>``) and the new row is inserted open by the
    caller.

    Contract / safety (identical to facts):
      * **value-differs only** — a re-assert of the SAME polarity AND label is
        NOT a supersession; that path stays the ``idx_nexuses_triple_open``
        ``ON CONFLICT`` upsert (confidence lift + lineage union). The
        ``(polarity <> $5 OR lower(label) <> lower($6))`` predicate guarantees
        the identical row is never closed by its own re-ingest.
      * **idempotent** — only OPEN rows are touched; a replay closes nothing
        new once the prior is already superseded.
      * **same connection** — the caller runs this immediately before the
        insert on the same ``conn`` so close + open are one logical step.

    Returns the number of prior rows closed.
    """
    result = await conn.execute(
        """
        UPDATE nexuses
           SET valid_until   = now(),
               superseded_by = $7,
               updated_at    = now()
         WHERE lower(subject)                  = lower($1)
           AND lower(COALESCE(intermediary,'')) = lower(COALESCE($2, ''))
           AND lower(object)                   = lower($3)
           AND lower(rel_type)                 = lower($4)
           AND (polarity <> $5 OR lower(label) <> lower($6))
           AND valid_until IS NULL
           AND superseded_by IS NULL
           AND id <> $7
        """,
        subject,
        intermediary,
        object_,
        rel_type,
        int(polarity),
        label,
        new_nexus_id,
    )
    try:
        return int(result.split()[-1]) if result else 0
    except (ValueError, IndexError):                     # pragma: no cover
        return 0


async def _insert_nexus(
    conn: asyncpg.Connection,
    *,
    row_id: UUID,
    payload: BaseModel,
    prov: ProvenanceFields,
    produced_at: datetime,
    effective_schema_uri: str,
    source_type: str | None = None,
    seed_batch_id: UUID | None = None,
    source_signal_ids: Sequence[UUID] | None = None,
) -> None:
    """Insert (or upsert) one ``nexuses`` row (PIECE A — faithful copy of
    :func:`_insert_fact`).

    ``source_type`` overrides the payload's ``source_type`` when given (the
    curated-seeding path passes ``'seed'``); ``None`` preserves existing
    behavior (payload's ``source_type`` if present, else ``'agent'``).
    ``seed_batch_id`` stamps the row's owning seed batch (NULL for non-seed
    writes); the same-triple upsert leaves the marker untouched.

    The ``nexuses`` table carries the ``idx_nexuses_triple_open`` PARTIAL
    UNIQUE index on ``(lower(subject), lower(COALESCE(intermediary,'')),
    lower(object), lower(rel_type))`` scoped to OPEN rows only. So a second
    identical OPEN triple would raise without ``ON CONFLICT``. We upsert: lift
    confidence to the max, union lineage + source_signal_ids, and union the
    raw evidence into ``data``-free columns. The conflict-inference ``WHERE``
    predicate matches the partial index so the upsert can ONLY land on the
    single open row; closed (superseded) rows that retain the same triple are
    invisible to conflict inference, so a re-assert of a previously-closed
    polarity/label inserts a fresh open row instead of re-opening the closed
    one (no dangling ``superseded_by``).

    Before the insert we run ``supersede_prior_nexuses`` so a polarity/label
    CHANGE for an existing typed triple closes the prior open row(s) and opens
    this one. A same-value re-assert closes nothing (the upsert path owns it).
    """
    p = payload                                          # type: ignore[assignment]
    data_payload = getattr(p, "data", {}) or {}
    # D15 — populate BOTH the derived_from lineage array AND the
    # source_signal_ids slice from the union of every id source: the explicit
    # write_nexus(source_signal_ids=...) param, the row's derived_from lineage,
    # and any payload-carried source_signal_ids. 100% of agent nexuses were
    # carrying empty provenance because the call site set neither; now any one
    # of the three populates both columns (dedupe-preserving order union).
    payload_ssids = list(getattr(p, "source_signal_ids", []) or [])
    explicit_ssids = list(source_signal_ids or [])
    lineage_union = append_derived_from(
        list(prov.derived_from), [*explicit_ssids, *payload_ssids]
    )
    # source_signal_ids = the originating signal/fact ids (the explicit param +
    # payload-carried + lineage), so the convenience slice is never emptier than
    # the lineage it slices.
    ssid_union = append_derived_from(
        explicit_ssids, [*payload_ssids, *list(prov.derived_from)]
    )
    nexus_source_signal_ids = ssid_union
    intermediary = getattr(p, "intermediary", None)
    polarity = int(getattr(p, "polarity", 0) or 0)
    label = getattr(p, "label", "") or ""
    # Converge the rel_type vocabulary at the write path to ONE canonical form
    # (D18): the seed driver writes CamelCase ("MemberOf"), proposed_edge_gov
    # writes "CoOccursWith", and a hyphen/underscore variant ("co-occurs-with")
    # would otherwise slip past normalize_predicate's exact-key map and split
    # the vocabulary — defeating the lower(rel_type) open-triple unique index so
    # the supersession path stays inert (0 superseded). _canonical_rel_type
    # collapses every separator surface first, so all variants fold to one key.
    rel_type = _canonical_rel_type(getattr(p, "rel_type"))
    await supersede_prior_nexuses(
        conn,
        subject=getattr(p, "subject"),
        intermediary=intermediary,
        object_=getattr(p, "object"),
        rel_type=rel_type,
        polarity=polarity,
        label=label,
        new_nexus_id=row_id,
    )
    effective_source_type = source_type or getattr(p, "source_type", "agent")
    await conn.execute(
        """
        INSERT INTO nexuses (
            id, subject, intermediary, object, rel_type, label, polarity,
            intent, channel, confidence, valid_from, valid_until,
            derived_from, source_signal_ids, data,
            target_id, target_version, analyst_id, analyst_version,
            produced_at, schema_uri, run_id, source_type, seed_batch_id
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7,
            $8, $9, $10, $11, $12,
            $13, $14, $15::jsonb,
            $16, $17, $18, $19,
            $20, $21, $22, $23, $24
        )
        ON CONFLICT (lower(subject), lower(COALESCE(intermediary, '')),
                     lower(object), lower(rel_type))
                 WHERE valid_until IS NULL AND superseded_by IS NULL
        DO UPDATE SET
            confidence        = GREATEST(nexuses.confidence, EXCLUDED.confidence),
            -- COALESCE the agg to '{}' — array_agg over zero rows is NULL, which
            -- would violate the NOT NULL on these columns when both sides are
            -- empty (the facts upsert dodges this by not aggregating arrays).
            derived_from      = COALESCE((SELECT array_agg(DISTINCT e)
                                 FROM unnest(nexuses.derived_from
                                             || EXCLUDED.derived_from) e),
                                '{}'::uuid[]),
            source_signal_ids = COALESCE((SELECT array_agg(DISTINCT e)
                                 FROM unnest(nexuses.source_signal_ids
                                             || EXCLUDED.source_signal_ids) e),
                                '{}'::uuid[]),
            -- Carry a NEWLY-supplied creation-time TTL but never clobber an
            -- existing one to NULL (open-row-only conflict target — a
            -- supersession close is invisible here; mirrors the facts upsert).
            valid_until       = COALESCE(EXCLUDED.valid_until, nexuses.valid_until),
            updated_at        = now()
        """,
        row_id,
        getattr(p, "subject"),
        intermediary,
        getattr(p, "object"),
        rel_type,
        label,
        polarity,
        getattr(p, "intent", "") or "",
        getattr(p, "channel", "direct") or "direct",
        float(getattr(p, "confidence", 1.0)),
        getattr(p, "valid_from", None),
        getattr(p, "valid_until", None),
        lineage_union,
        nexus_source_signal_ids,
        json.dumps(data_payload, default=_json_default),
        prov.target_id,
        prov.target_version,
        prov.analyst_id,
        prov.analyst_version,
        produced_at,
        effective_schema_uri,
        prov.run_id,
        effective_source_type,
        seed_batch_id,
    )


# ---------------------------------------------------------------------------
# Journal — off-chain dedicated table (plan §3.2 / §8)
# ---------------------------------------------------------------------------


async def supersede_prior_consolidation(
    conn: asyncpg.Connection,
    *,
    new_entry_id: UUID,
    analyst_id: str | None,
) -> UUID | None:
    """Close the single open journal CONSOLIDATION, pointing it at
    ``new_entry_id`` (plan §8 — mirrors :func:`supersede_prior_facts`).

    The "current inner landscape" is the single open consolidation row
    (``entry_kind='consolidation' AND valid_until IS NULL AND superseded_by IS
    NULL``). A newer consolidation CLOSES the prior one (``valid_until = now()``
    + ``superseded_by = <new id>``) rather than overwriting it, so history
    accrues and you can ask "what did the journal believe, when."

    Contract / safety:
      * **bootstrap** — the FIRST consolidation supersedes nothing (no open row
        → returns None, ``supersedes`` stays NULL). NULL ``supersedes`` is
        allowed for ``entry_kind='consolidation'``.
      * **single-open invariant** — ``SELECT … FOR UPDATE`` locks the open row
        so a concurrent run blocks until this transaction commits; combined with
        the ``uq_journal_single_open_consolidation`` partial-unique index, at
        most one consolidation is ever open. A racing/replayed run that tries to
        open a second while one is still open raises on the index rather than
        double-opening.
      * **idempotent under replay** — only a row still open is closed; a replay
        after the prior is already closed closes nothing new (returns None) and
        leaves the existing ``superseded_by`` pointer untouched.
      * **same connection** — the caller runs this immediately before the insert
        on the same ``conn`` so close + open are one logical step.

    Returns the id of the consolidation that was closed, or ``None`` when there
    was no open consolidation (the bootstrap case, or a replay).
    """
    # Lock the single open consolidation (if any) so a concurrent consolidation
    # blocks here rather than both opening a second row.
    row = await conn.fetchrow(
        """
        SELECT id
          FROM journal_entries
         WHERE entry_kind = 'consolidation'
           AND valid_until IS NULL
           AND superseded_by IS NULL
           AND id <> $1
         ORDER BY produced_at DESC
         FOR UPDATE
        """,
        new_entry_id,
    )
    if row is None:
        return None
    prior_id: UUID = row["id"]
    await conn.execute(
        """
        UPDATE journal_entries
           SET valid_until   = now(),
               superseded_by = $1,
               updated_at    = now()
         WHERE id = $2
           AND valid_until IS NULL
           AND superseded_by IS NULL
        """,
        new_entry_id,
        prior_id,
    )
    return prior_id


async def _insert_journal_entry(
    conn: asyncpg.Connection,
    *,
    row_id: UUID,
    payload: BaseModel,
    prov: ProvenanceFields,
    produced_at: datetime,
    effective_schema_uri: str,
) -> None:
    """Insert one ``journal_entries`` row (entry | consolidation).

    For a CONSOLIDATION, ``supersede_prior_consolidation`` runs FIRST (close the
    prior open consolidation on the same conn) so the close + open are one
    logical step; the ``supersedes`` column on the new row records which prior it
    closed (NULL for the bootstrap first consolidation). ENTRIES are pure append
    — no supersession.

    ``derived_from`` is forced EMPTY here regardless of ``prov.derived_from`` —
    the journal is the direction-asymmetric lineage node (§3.5). The citations
    live only in ``claims`` / ``cited_substrate_refs``.
    """
    p = payload                                          # type: ignore[assignment]
    entry_kind = getattr(p, "entry_kind", "entry")
    claims = [c.model_dump(mode="json") if hasattr(c, "model_dump") else c
              for c in (getattr(p, "claims", []) or [])]
    cited_refs = list(getattr(p, "cited_substrate_refs", []) or [])
    honesty_flags = list(getattr(p, "honesty_flags", []) or [])

    supersedes: UUID | None = None
    if entry_kind == "consolidation":
        supersedes = await supersede_prior_consolidation(
            conn,
            new_entry_id=row_id,
            analyst_id=prov.analyst_id,
        )

    await conn.execute(
        """
        INSERT INTO journal_entries (
            id, entry_kind, title, body, claims, cited_substrate_refs,
            period_start, period_end, honesty_flags,
            superseded_by,
            target_id, target_version, analyst_id, analyst_version,
            produced_at, derived_from, schema_uri, run_id
        ) VALUES (
            $1, $2, $3, $4, $5::jsonb, $6,
            $7, $8, $9,
            $10,
            $11, $12, $13, $14,
            $15, $16, $17, $18
        )
        """,
        row_id,
        entry_kind,
        getattr(p, "title", ""),
        getattr(p, "body", ""),
        json.dumps(claims, default=_json_default),
        cited_refs,
        getattr(p, "period_start", None),
        getattr(p, "period_end", None),
        honesty_flags,
        # `superseded_by` on the NEW row is always NULL at insert (it is the
        # newest open row); `supersedes` is recorded in the data/relation via the
        # prior row's superseded_by pointer set above. We keep the new row open.
        None,
        prov.target_id,
        prov.target_version,
        prov.analyst_id,
        prov.analyst_version,
        produced_at,
        [],  # OFF the chain — always empty (§3.5).
        effective_schema_uri,
        prov.run_id,
    )
    _ = supersedes  # recorded via the prior row's superseded_by pointer above


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Split a CamelCase / PascalCase token boundary ("CoOccursWith" → "Co Occurs
# With") so a hyphen/underscore/space/CamelCase variant of the SAME rel_type all
# fold to one separator-collapsed surface form before the canonical-map lookup.
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _canonical_rel_type(rel_type: str) -> str:
    """Canonicalize a fact predicate / nexus rel_type onto ONE form on write
    (D18 — mixed-case ``CoOccursWith`` vs ``co-occurs-with`` defeated the
    open-triple unique index, so the supersession path stayed inert at 0).

    :func:`legba.data.vocabulary.normalize_predicate` only maps EXACT
    case-folded keys (``cooccurswith`` → ``co occurs with``), so a separator
    variant (``co-occurs-with``, ``co_occurs_with``, ``Co Occurs With``) slipped
    through verbatim and split the vocabulary. We first collapse every separator
    surface — hyphen / underscore / CamelCase boundary / runs of whitespace —
    into single spaces, THEN run the seam's canonical map on BOTH the collapsed
    spaced form and the space-stripped form. The first hit wins; an unmapped
    predicate still flows through ``normalize_predicate`` (verbatim) so novel
    operator predicates are never rewritten.

    Pure / deterministic / no DB. Empty or whitespace-only → returned unchanged
    (callers own required-field validation).
    """
    if not rel_type or not rel_type.strip():
        return rel_type
    # Split CamelCase, then turn -/_ into spaces and collapse whitespace runs.
    spaced = _CAMEL_BOUNDARY_RE.sub(" ", rel_type)
    spaced = re.sub(r"[-_\s]+", " ", spaced).strip()
    # Try the canonical map on the collapsed spaced form (the dominant live
    # form) and on the space-stripped form (the CamelCase map keys), preferring
    # whichever the seam actually maps. normalize_predicate case-folds + returns
    # verbatim on a miss, so a double-miss returns the spaced form unchanged.
    mapped_spaced = normalize_predicate(spaced)
    if mapped_spaced != spaced:
        return mapped_spaced
    mapped_tight = normalize_predicate(spaced.replace(" ", ""))
    if mapped_tight != spaced.replace(" ", ""):
        return mapped_tight
    # No canonical-map hit: return the separator-collapsed, lowercased spaced
    # form so two surface variants of an UNMAPPED predicate still converge on
    # one key (the open-triple index lower()s the column, so lowercase here
    # keeps the row's stored form aligned with the dedup key).
    return spaced.casefold()


def _coerce_payload(
    spec: OutputKindSpec, payload: BaseModel | dict[str, Any]
) -> BaseModel:
    """Validate / re-validate payload against the kind's declared model."""
    if isinstance(payload, spec.payload_model):
        return payload
    if isinstance(payload, dict):
        return spec.payload_model.model_validate(payload)
    # Different pydantic model — re-validate against the declared kind shape.
    return spec.payload_model.model_validate(payload.model_dump(mode="python"))


def _payload_as_dict(payload: BaseModel | dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    return payload.model_dump(mode="json")


def _json_default(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)


async def _existing_trace_run_id(
    conn: asyncpg.Connection, run_id: UUID | None
) -> UUID | None:
    """Return run_id only if it already exists in analyst_traces."""
    if run_id is None:
        return None
    found = await conn.fetchval(
        "SELECT run_id FROM analyst_traces WHERE run_id = $1", run_id
    )
    return found


async def _safe_publish(
    publish_fn: NatsPublishFn, subject: str, envelope: dict[str, Any]
) -> None:
    """Best-effort NATS publish — swallow errors so substrate writes don't
    abort on transient broker hiccups. The DLQ + retry policy is L-110's."""
    try:
        await publish_fn(subject, json.dumps(envelope, default=_json_default).encode("utf-8"))
    except Exception:                                    # pragma: no cover
        import logging
        logging.getLogger(__name__).exception(
            "NATS publish failed subject=%s", subject
        )
