# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.seed.manual_batch — the generic manual-ingest batch loader (S4-T2).

A *manual batch* (a directory validated by :mod:`legba.data.seed.manual_schema`,
S4-T1) is the operator's ground-state backfill / merge / force lane. This module
is the ADAPTER + LOADER that turns a validated batch into knowledge-layer rows,
riding the EXISTING seed plane — the same primitives ``_driver`` uses
(``_resolve_entity`` entity-canon upsert, ``write_fact`` / ``write_nexus``
temporal writes, the ``seed_batches`` content-hash-deduped ledger) — rather than
a parallel write path. Nothing here mutates a fact in place or hard-deletes; a
value change closes the prior row via the temporal ``valid_until`` /
``superseded_by`` supersession and opens the new one.

Three reconciliation MODES (declared on the manifest, overridable on the CLI):

  * ``skip`` (default) — insert-if-absent by natural key. A record whose
    ``(subject, predicate)`` already has an OPEN fact (or whose typed nexus
    triple is already open) is left UNTOUCHED. So a re-run is a genuine no-op:
    every record short-circuits BEFORE any write (the seed driver "always
    writes" its upsert, which would bump confidence + refresh markers — skip
    must not). Batch-level idempotency also rides the ``seed_batches``
    content-hash dedupe (one ledger row per distinct batch).
  * ``merge`` — a same-value record is a no-op (``unchanged``); a value CHANGE
    supersedes the prior open row(s) and opens the new one, RESPECTING the
    source-tier guard. A manual (tier-1) batch that tries to retire a
    ``seed`` / ``curated`` (tier-2) prior does NOT silently create contention —
    it is reported as a ``conflict`` and skipped (use ``force``).
  * ``force`` — operator authority. A value change supersedes EVERY matching
    open prior unconditionally, INCLUDING a higher-tier ``seed`` / ``curated``
    one. History is still preserved (the prior row is closed, not deleted).

DECISIVE SUPERSESSION — the pre-close (load-bearing). A ``supersede`` decision
(``merge`` or ``force``) pre-closes the differing-value open prior(s) itself, by
calling ``supersede_prior_facts`` with ``incoming_source_type=None`` (the
journal-correction precedent: a ``None`` incoming rank disables the guard) BEFORE
the ``write_fact`` insert, while STILL stamping the new row ``source_type='manual'``
— so the operator's reconciliation lands WITHOUT quietly widening the row's
provenance tier to a grounding-eligible ``curated``. The guard-off pre-close is
what makes the batch's own supersession DECISIVE against two write-path defaults
that would otherwise leave the new value NOT the single open truth:

  * THE TIER TRAP. ``supersede_prior_facts`` guards a downgrade: an incoming fact
    whose ``source_type`` ranks BELOW the prior's (``_SOURCE_TIER_RANK`` —
    ``seed``/``curated`` = 2, everything else incl. ``manual`` = 1) cannot close
    it. A manual (tier-1) batch could NEVER supersede a seeded fact through the
    normal write path. This only matters for ``force`` — a ``merge`` that meets a
    higher-tier prior is caught earlier: ``classify_fact`` returns ``conflict``
    (reported, no write), NOT a silent contention. So by the time a ``merge``
    reaches the pre-close, no open prior outranks the incoming and guard-off is
    safe (it closes exactly the same-or-lower-tier priors the classifier allowed).
  * THE CONTENTION COEXISTENCE CARVE-OUT. With ``LEGBA_FACT_CONTENTION`` ON
    (the live default), the write path's ``supersede_prior_facts`` SPARES a
    same-tier FUZZY-DISTINCT prior (leaving e.g. "Ada" open beside "Bo" so the
    detect-only arbiter can group them next cadence). That coexistence is for
    AMBIENT machine disagreement — an operator ``merge`` / ``force`` is an
    EXPLICIT reconciliation, so the pre-close (``incoming_source_type=None``
    disables the carve-out too) makes the new value the single open truth rather
    than minting a contention the operator did not ask for.

Nexuses have no tier guard (``supersede_prior_nexuses`` closes any differing
prior), so ``merge`` and ``force`` coincide for them.

PROVENANCE (the honesty rail). The manifest's ``default_provenance`` maps
straight onto the row ``source_type``: ``curated`` (grounding-eligible — the
Tier-1 preamble trusts it) or ``manual`` (STORED, not injected). The tier is
never widened as a side effect of a mode: a ``manual`` batch's rows stay
``manual`` in every mode. Confidence is the value the S4-T1 validator already
guaranteed present (per-record or the batch default) — never a silent ``1.0``.

DRY-RUN. ``run_manual_batch(..., dry_run=True)`` performs the WHOLE reconciliation
inside a transaction that is ROLLED BACK at the end, so it writes nothing yet its
create/supersede/skip/unchanged/conflict tallies are produced by the exact code
path a wet run takes — the counts MATCH by construction. The report (per-lane
tallies + the conflict list) is persisted onto the batch's ``seed_batches``
manifest on a wet run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict

from ..nats import BACKFILL_EVENT_CLASS
from ..provenance import (
    FactPayload,
    NexusPayload,
    supersede_prior_facts,
    write_fact,
    write_nexus,
)
from ..provenance.writes import _canonical_rel_type, _source_tier_rank
from ..sources._contract import InMemoryStateStore, Signal, SourceContext
from ..sources.baseline import run_baseline
from ..vocabulary import normalize_predicate
from ._base import SeedContext, SeedEntity, SeedFact, SeedNexus, SeedSource
from ._driver import _content_hash, _resolve_entity, _seed_ctx
from .manual_schema import (
    BatchManifest,
    BatchMode,
    ManualEntityRecord,
    ManualFactRecord,
    ManualNexusRecord,
    ManualSignalRecord,
    ValidatedBatch,
    validate_batch,
)

logger = logging.getLogger(__name__)

# The knowledge-layer lanes this loader writes. The signals lane (S4-T4) rides
# the NORMAL signal contract (see the SIGNALS section below); the vector-docs
# lane (the RAG plane) enters through its own Qdrant sink — a batch may declare
# it, but THIS loader defers it (reports the count, writes nothing).
_KNOWLEDGE_LANES = ("entities", "facts", "nexuses")
_DEFERRED_LANES = ("docs",)

# The optional enrichment hook the loader threads into ``run_baseline`` — the
# registry pipeline factory's ``(signal, ctx) -> signal|None`` stage (language /
# geocode / classify / NER). The CLI runs WITHOUT it (tier-1 structured
# enrichment only); a runtime caller or a test may wire one.
EnrichmentStage = Callable[[Signal, SourceContext], Awaitable["Signal | None"]]
# The optional fan-out hook — ``(signal, event_class) -> awaitable``. Best-effort
# and OFF in the CLI (a backfill is excluded from every reactive window anyway,
# so nothing reactive consumes the fan-out); wired where a live publisher exists.
PublishSignal = Callable[[Signal, str], Awaitable[Any]]

# The manual-ingest source-id prefix. Every backfilled signal is stamped with
# ``source.manual.<batch>`` as its origin source, and a matching (non-polling)
# ``source_descriptors`` head row is registered so the source resolves for
# provenance / lineage / the UI without ever being polled or poll-liveness-checked.
_MANUAL_SOURCE_PREFIX = "source.manual."
# A fixed namespace so a backfilled signal's id is DETERMINISTIC on
# ``(source_id, external_id|content_hash)`` — a batch re-run collides on the row
# id (``ON CONFLICT (id) DO NOTHING``) and is an idempotent no-op, not a dup.
_SIGNAL_ID_NAMESPACE = UUID("6f2a7b1e-9c3d-4e5a-8b0f-2d4c6e8a1b3f")


class _ManualSourceConfig(BaseModel):
    """A bare config for the manual source's :class:`SourceContext` — the
    baseline reads no config keys off it, so it carries none."""

    model_config = ConfigDict(extra="allow")


class RecordAction(str, Enum):
    """What the loader did (or would do) with one record.

    ``create`` — no open prior for the natural key; a fresh open row is inserted.
    ``supersede`` — a differing-value prior was closed and this row opened.
    ``skip`` — ``skip`` mode, an open prior exists → left untouched (no write).
    ``unchanged`` — the value is already the open truth → idempotent no-op.
    ``conflict`` — ``merge`` mode wanted to retire a HIGHER-tier prior → refused
        (reported for the operator; no write). Only facts produce this.
    ``dlq`` — the underlying ``write_fact`` / ``write_nexus`` routed to the
        dead-letter (a schema failure) — counted so a batch can't silently drop.
    """

    CREATE = "create"
    SUPERSEDE = "supersede"
    SKIP = "skip"
    UNCHANGED = "unchanged"
    CONFLICT = "conflict"
    DLQ = "dlq"


# The tally keys every lane report carries (stable so dry/wet diff cleanly).
_ACTION_KEYS = tuple(a.value for a in RecordAction)


def _coerce_mode(mode: BatchMode | str | None, manifest: BatchManifest) -> BatchMode:
    """Resolve the effective mode: an explicit CLI override wins, else the
    manifest's declared ``mode`` (which itself defaults to ``skip``)."""
    if mode is None:
        return manifest.mode
    if isinstance(mode, BatchMode):
        return mode
    try:
        return BatchMode(str(mode).strip().lower())
    except ValueError as exc:  # pragma: no cover — CLI argparse choices guard this
        raise ValueError(
            f"unknown mode {mode!r}; expected one of "
            f"{', '.join(m.value for m in BatchMode)}"
        ) from exc


def _source_type_for(manifest: BatchManifest) -> str:
    """Map the manifest provenance tier onto the row ``source_type``.

    ``curated`` (grounding-eligible) vs ``manual`` (stored, not injected). This
    is the ONLY place the tier becomes a row stamp — no mode ever widens it.
    """
    return "curated" if manifest.default_provenance.grounding_eligible else "manual"


# ---------------------------------------------------------------------------
# PURE classifiers — the mode decision, no DB (so they unit-test directly)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriorFact:
    """One OPEN prior fact for a ``(subject, predicate)`` — the classifier input.

    ``tier`` is the prior's ``_source_tier_rank`` (2 = seed/curated authoritative,
    1 = everything else); the loader resolves it from the row's ``source_type``.
    """

    value: str
    tier: int


@dataclass(frozen=True)
class PriorNexus:
    """One OPEN prior nexus for a typed triple — the classifier input.

    A nexus's "value" is its ``(polarity, label)`` (mirrors
    ``supersede_prior_nexuses``'s value-differs predicate). Nexuses have no tier
    guard, so no rank is carried.
    """

    polarity: int
    label: str


def classify_fact(
    *,
    incoming_value: str,
    mode: BatchMode,
    priors: Iterable[PriorFact],
    incoming_tier: int,
) -> RecordAction:
    """Decide the action for one fact record against its open priors.

    Pure + deterministic (the loader runs it identically in dry + wet so the
    tallies match). ``priors`` are the OPEN rows for ``(lower(subject),
    lower(predicate))``. ``incoming_tier`` is the batch's ``_source_tier_rank``
    (only consulted in ``merge``).
    """
    priors = list(priors)
    lv = incoming_value.strip().lower()
    same = any(p.value.strip().lower() == lv for p in priors)
    diff = [p for p in priors if p.value.strip().lower() != lv]

    if mode is BatchMode.SKIP:
        # Insert-if-absent by natural key: any open prior for this
        # subject+predicate means "present" → leave it (a re-run no-op).
        return RecordAction.SKIP if priors else RecordAction.CREATE

    if same:
        return RecordAction.UNCHANGED  # already the open truth — nothing to do.
    if not diff:
        return RecordAction.CREATE  # no open prior at all.

    if mode is BatchMode.MERGE:
        # Never SILENTLY retire a higher-authority prior — refuse + report so the
        # operator makes an explicit call (force). The seed-plane guard would
        # otherwise leave both rows open (contention), which merge must not mint.
        if any(p.tier > incoming_tier for p in diff):
            return RecordAction.CONFLICT
        return RecordAction.SUPERSEDE

    # FORCE — operator authority, supersede regardless of tier.
    return RecordAction.SUPERSEDE


def classify_nexus(
    *,
    incoming_polarity: int,
    incoming_label: str,
    mode: BatchMode,
    priors: Iterable[PriorNexus],
) -> RecordAction:
    """Decide the action for one nexus record against its open priors.

    Nexuses have no source-tier guard, so ``merge`` and ``force`` coincide (both
    supersede a differing prior); there is no ``conflict`` outcome.
    """
    priors = list(priors)
    il = incoming_label.strip().lower()
    same = any(
        p.polarity == incoming_polarity and p.label.strip().lower() == il
        for p in priors
    )

    if mode is BatchMode.SKIP:
        return RecordAction.SKIP if priors else RecordAction.CREATE
    if same:
        return RecordAction.UNCHANGED
    if not priors:
        return RecordAction.CREATE
    return RecordAction.SUPERSEDE


# ---------------------------------------------------------------------------
# Record → typed seed payload mapping (mirrors _base seed payloads 1:1)
# ---------------------------------------------------------------------------


def _record_data(record_data: dict[str, Any], manifest: BatchManifest) -> dict[str, Any]:
    """Thread the batch's provenance defaults onto a row's ``data`` bag.

    License / source_url / notes / operator / batch_id ride under a
    ``manual_batch`` sub-key so every written row is self-describing (which
    operator batch, under what license) without polluting the hot columns. A
    record's own ``data`` wins on any key collision.
    """
    prov: dict[str, Any] = {
        "batch_id": manifest.batch_id,
        "operator": manifest.operator,
    }
    if manifest.license:
        prov["license"] = manifest.license
    if manifest.source_url:
        prov["source_url"] = manifest.source_url
    if manifest.provenance_notes:
        prov["provenance_notes"] = manifest.provenance_notes
    merged = {"manual_batch": prov}
    merged.update(record_data or {})
    return merged


def _fact_confidence(record: ManualFactRecord, manifest: BatchManifest) -> float:
    """Resolve the honest confidence: per-record, else the batch default.

    The S4-T1 validator already REFUSED a record that resolves to neither, so
    this never fabricates a ``1.0``; the assertion is a belt-and-braces guard.
    """
    conf = record.confidence if record.confidence is not None else manifest.default_confidence
    assert conf is not None, (
        "confidence unresolved despite S4-T1 validation — refusing a silent 1.0"
    )
    return float(conf)


def _seed_payloads(vb: ValidatedBatch) -> list[SeedEntity | SeedFact | SeedNexus]:
    """Map the validated knowledge-lane records to typed seed payloads.

    Used for the ``seed_batches`` content-hash (a stable fingerprint of what the
    batch writes to the KNOWLEDGE layer) and by :class:`ManualBatchSeedSource.map`.
    Confidence is resolved here so the fingerprint reflects the real written value.
    Signals (S4-T4) and docs are excluded from THIS fingerprint — they carry their
    own idempotency (a signal's deterministic id + ``ON CONFLICT``; a doc's
    ``(corpus, doc_id, chunk_seq)`` key), so they are reconciled on their own sinks
    rather than through the knowledge-lane content-hash dedupe.
    """
    manifest = vb.manifest
    out: list[SeedEntity | SeedFact | SeedNexus] = []
    for e in vb.entities:
        out.append(
            SeedEntity(
                canonical_name=e.canonical_name,
                entity_class=e.entity_class,
                geo_lat=e.geo_lat,
                geo_lon=e.geo_lon,
                geo_country=e.geo_country,
                data=dict(e.data),
            )
        )
    for f in vb.facts:
        out.append(
            SeedFact(
                subject=f.subject,
                predicate=f.predicate,
                value=f.value,
                valid_from=f.valid_from,
                valid_until=f.valid_until,
                confidence=_fact_confidence(f, manifest),
                geo_lat=f.geo_lat,
                geo_lon=f.geo_lon,
                data=dict(f.data),
            )
        )
    for n in vb.nexuses:
        out.append(
            SeedNexus(
                subject=n.subject,
                object=n.object,
                rel_type=n.rel_type,
                polarity=n.polarity,
                valid_from=n.valid_from,
                valid_until=n.valid_until,
                intermediary=n.intermediary,
                label=n.label,
                intent=n.intent,
                channel=n.channel,
                confidence=(
                    n.confidence
                    if n.confidence is not None
                    else float(manifest.default_confidence or 0.0)
                ),
                data=dict(n.data),
            )
        )
    return out


# ---------------------------------------------------------------------------
# The adapter (SeedSource) — plugs the batch into the seed plane
# ---------------------------------------------------------------------------


class ManualBatchSeedSource:
    """A generic manual-ingest batch as a :class:`SeedSource`.

    ``fetch`` validates the batch directory (strict — a bad record raises with
    the offending ``file:line``); ``map`` yields the typed seed payloads. The
    reconciliation MODES live in :func:`run_manual_batch` (a mode-aware sibling
    of ``run_seed_source``), not in ``map`` — the adapter stays pure data. The
    directory is passed via ``ctx.options['batch_dir']``.
    """

    def __init__(self, source_type: str = "manual") -> None:
        # source_type is set from the manifest tier by run_manual_batch; the
        # default keeps a bare instance honest (stored-not-injected).
        self.name = "manual_batch"
        self.source_type = source_type

    async def fetch(self, ctx: SeedContext) -> ValidatedBatch:
        batch_dir = (ctx.options or {}).get("batch_dir")
        if not batch_dir:
            raise ValueError("manual_batch adapter needs options['batch_dir']")
        vb = validate_batch(batch_dir, strict=True)
        self.source_type = _source_type_for(vb.manifest)
        return vb

    def map(self, raw: ValidatedBatch) -> Iterable[SeedEntity | SeedFact | SeedNexus]:
        return _seed_payloads(raw)


# ---------------------------------------------------------------------------
# The run report
# ---------------------------------------------------------------------------


@dataclass
class ManualBatchReport:
    """The outcome of a manual-batch load (dry or wet).

    Per-lane action tallies plus the human-readable conflict list, the deferred
    (non-knowledge) lane counts, and any per-record errors. Persisted onto the
    ``seed_batches`` manifest on a wet run; printed by the CLI.
    """

    batch_id: str
    mode: str
    source_type: str
    grounding_eligible: bool
    dry_run: bool
    seed_batch_id: UUID | None = None
    entities_resolved: int = 0
    facts: dict[str, int] = field(default_factory=lambda: {k: 0 for k in _ACTION_KEYS})
    nexuses: dict[str, int] = field(default_factory=lambda: {k: 0 for k in _ACTION_KEYS})
    # The signals lane (S4-T4): backfilled observations written through the
    # normal signal contract. ``create`` = a new row written; ``skip`` = the
    # deterministic id already exists (idempotent re-run) or a baseline filter
    # dropped it; ``dlq`` = the write raised. Signals never supersede (they are
    # append-only observations deduped by canonical link, not asserted facts).
    signals: dict[str, int] = field(default_factory=lambda: {k: 0 for k in _ACTION_KEYS})
    manual_source_id: str | None = None
    deferred: dict[str, int] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def record(self, lane: str, action: RecordAction) -> None:
        getattr(self, lane)[action.value] += 1

    @property
    def has_writes_pending(self) -> bool:
        """True when the batch would change the substrate (create/supersede)."""
        touched = (
            self.facts["create"]
            + self.facts["supersede"]
            + self.nexuses["create"]
            + self.nexuses["supersede"]
            + self.signals["create"]
        )
        return touched > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "mode": self.mode,
            "source_type": self.source_type,
            "grounding_eligible": self.grounding_eligible,
            "dry_run": self.dry_run,
            "seed_batch_id": str(self.seed_batch_id) if self.seed_batch_id else None,
            "entities_resolved": self.entities_resolved,
            "facts": dict(self.facts),
            "nexuses": dict(self.nexuses),
            "signals": dict(self.signals),
            "manual_source_id": self.manual_source_id,
            "deferred_lanes": dict(self.deferred),
            "conflicts": list(self.conflicts),
            "errors": list(self.errors),
        }


# ---------------------------------------------------------------------------
# Per-record apply helpers (DB)
# ---------------------------------------------------------------------------


async def _open_prior_facts(conn: Any, subject: str, predicate: str) -> list[PriorFact]:
    rows = await conn.fetch(
        """
        SELECT value, source_type FROM facts
         WHERE lower(subject)   = lower($1)
           AND lower(predicate) = lower($2)
           AND valid_until IS NULL
           AND superseded_by IS NULL
        """,
        subject,
        predicate,
    )
    return [PriorFact(value=r["value"], tier=_source_tier_rank(r["source_type"])) for r in rows]


async def _open_prior_nexuses(
    conn: Any, subject: str, intermediary: str | None, object_: str, rel_type: str
) -> list[PriorNexus]:
    rows = await conn.fetch(
        """
        SELECT polarity, label FROM nexuses
         WHERE lower(subject)                   = lower($1)
           AND lower(COALESCE(intermediary,'')) = lower(COALESCE($2, ''))
           AND lower(object)                    = lower($3)
           AND lower(rel_type)                  = lower($4)
           AND valid_until IS NULL
           AND superseded_by IS NULL
        """,
        subject,
        intermediary,
        object_,
        rel_type,
    )
    return [PriorNexus(polarity=int(r["polarity"]), label=r["label"] or "") for r in rows]


async def _apply_fact(
    conn: Any,
    record: ManualFactRecord,
    *,
    manifest: BatchManifest,
    mode: BatchMode,
    source_type: str,
    incoming_tier: int,
    batch_id: UUID,
    actx: Any,
    report: ManualBatchReport,
) -> RecordAction:
    predicate = normalize_predicate(record.predicate)
    priors = await _open_prior_facts(conn, record.subject, predicate)
    action = classify_fact(
        incoming_value=record.value,
        mode=mode,
        priors=priors,
        incoming_tier=incoming_tier,
    )
    if action is RecordAction.CONFLICT:
        report.conflicts.append(
            f"fact ({record.subject}|{predicate}|{record.value}): a higher-tier "
            "prior blocks a merge supersession — re-run with --mode=force to override"
        )
        return action
    if action in (RecordAction.SKIP, RecordAction.UNCHANGED):
        return action

    # CREATE or SUPERSEDE → write through the temporal fact path.
    await _resolve_entity(conn, canonical_name=record.subject)
    await _resolve_entity(conn, canonical_name=record.value)
    row_id = uuid4()
    if action is RecordAction.SUPERSEDE:
        # A DECISIVE operator supersession — pre-close the differing-value open
        # prior(s) with the guard OFF (incoming_source_type=None) so it lands
        # regardless of BOTH the source-tier guard AND the contested-claims
        # COEXISTENCE carve-out. The latter is the load-bearing reason to pre-
        # close even in `merge`: when LEGBA_FACT_CONTENTION is ON, the write
        # path's own supersede would SPARE a same-tier fuzzy-distinct prior
        # (leaving "Ada" open beside "Bo" as a contention for the detect-only
        # arbiter) — but an operator merge is an explicit reconciliation, not
        # ambient machine disagreement, so the new value MUST become the single
        # open truth. Guard-off is safe here for BOTH modes: `classify_fact`
        # already refused a HIGHER-tier prior in `merge` (→ CONFLICT, returned
        # above before any write), so by here no open prior outranks the
        # incoming; `force` is operator authority over any tier. Either way the
        # new row is still stamped `source_type='manual'` below (never widened
        # to grounding-eligible `curated`).
        await supersede_prior_facts(
            conn,
            subject=record.subject,
            predicate=predicate,
            value=record.value,
            new_fact_id=row_id,
            incoming_source_type=None,
        )
    out, dlq = await write_fact(
        conn,
        analyst_ctx=actx,
        payload=FactPayload(
            subject=record.subject,
            predicate=predicate,
            value=record.value,
            confidence=_fact_confidence(record, manifest),
            source_type=source_type,
            valid_from=record.valid_from,
            valid_until=record.valid_until,
            geo_lat=record.geo_lat,
            geo_lon=record.geo_lon,
            data=_record_data(record.data, manifest),
        ),
        derived_from=[],
        source_type=source_type,
        seed_batch_id=batch_id,
        row_id=row_id,
    )
    if dlq is not None or out is None:
        report.errors.append(
            f"fact ({record.subject}|{predicate}|{record.value}) → DLQ"
        )
        return RecordAction.DLQ
    return action


async def _apply_nexus(
    conn: Any,
    record: ManualNexusRecord,
    *,
    manifest: BatchManifest,
    mode: BatchMode,
    source_type: str,
    batch_id: UUID,
    actx: Any,
    report: ManualBatchReport,
) -> RecordAction:
    rel_type = _canonical_rel_type(record.rel_type)
    priors = await _open_prior_nexuses(
        conn, record.subject, record.intermediary, record.object, rel_type
    )
    action = classify_nexus(
        incoming_polarity=record.polarity,
        incoming_label=record.label,
        mode=mode,
        priors=priors,
    )
    if action in (RecordAction.SKIP, RecordAction.UNCHANGED):
        return action

    # CREATE or SUPERSEDE → write through the temporal nexus path
    # (supersede_prior_nexuses has no tier guard, so merge/force coincide).
    for name in (record.subject, record.object, record.intermediary):
        if name:
            await _resolve_entity(conn, canonical_name=name)
    conf = record.confidence if record.confidence is not None else manifest.default_confidence
    out, dlq = await write_nexus(
        conn,
        analyst_ctx=actx,
        payload=NexusPayload(
            subject=record.subject,
            intermediary=record.intermediary,
            object=record.object,
            rel_type=rel_type,
            label=record.label,
            polarity=record.polarity,
            intent=record.intent,
            channel=record.channel,
            confidence=float(conf) if conf is not None else 0.0,
            valid_from=record.valid_from,
            valid_until=record.valid_until,
            data=_record_data(record.data, manifest),
        ),
        derived_from=[],
        source_type=source_type,
        seed_batch_id=batch_id,
    )
    if dlq is not None or out is None:
        report.errors.append(
            f"nexus ({record.subject}|{rel_type}|{record.object}) → DLQ"
        )
        return RecordAction.DLQ
    return action


# ---------------------------------------------------------------------------
# SIGNALS lane (S4-T4) — backfill through the NORMAL signal contract
# ---------------------------------------------------------------------------
#
# A backfilled record maps to a target-agnostic ``Signal`` that rides the exact
# write path a live source uses: per-source baseline enrichment (``run_baseline``
# — content_hash/language/geo-from-scope/tags always; geocode/NER when an
# enrichment stage is wired), then ``write_canonical_signal`` (the host-side
# source-credibility lookup + the canonical INSERT + dedupe), then a best-effort
# fan-out. The ONE thing that marks it historical: ``event_class=backfill`` in
# the payload (beside ``published_at``, the real event time). Every UNIT's fresh
# reactive-window read excludes that event_class; the accumulation / fact /
# grounding / entity-resolution paths keep it. See ``legba.data.nats``.


def manual_source_id(batch_id: str) -> str:
    """The ``source.manual.<batch>`` origin source id for a batch's signals.

    The batch id is flattened to a single dotless token (source ids are NATS
    subject tokens; dots are separators) so the fan-out subject + subscription
    resolution stay well-formed.
    """
    token = re.sub(r"[^a-z0-9]+", "_", batch_id.strip().lower()).strip("_") or "batch"
    return f"{_MANUAL_SOURCE_PREFIX}{token}"


def _signal_content_hash(record: ManualSignalRecord) -> str:
    """A stable content fingerprint for one backfilled signal.

    Keys the deterministic id (so a re-run dedupes) and the dedupe layer. Built
    from the natural content: external id (when the source carried one), title,
    body, and the real event time.
    """
    basis = "\x1f".join(
        [
            record.external_id or "",
            record.title or "",
            record.body or "",
            record.published_at.astimezone(timezone.utc).isoformat(),
        ]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _deterministic_signal_id(source_id: str, natural_key: str) -> UUID:
    """A stable signal id from ``(source_id, external_id|content_hash)`` so a
    batch re-run collides on ``ON CONFLICT (id) DO NOTHING`` (idempotent)."""
    return uuid5(_SIGNAL_ID_NAMESPACE, f"{source_id}\x1f{natural_key}")


def signal_from_record(
    record: ManualSignalRecord,
    *,
    manifest: BatchManifest,
    source_id: str,
    fetched_at: datetime,
) -> Signal:
    """Map one validated ``ManualSignalRecord`` to a canonical ``Signal``.

    ``published_at`` (the REAL event time, backdated) + ``event_class=backfill``
    live in the payload (their persistence home — the ``signals`` table has no
    such columns). ``fetched_at`` is the LOAD time. Inline ``entities`` go under
    ``payload.entities`` so the (no-window) entity-resolution analyst picks the
    backfill up and feeds it into the graph/facts; inline ``geo`` becomes the
    indexed ``geo`` column so the accumulation reads scope it correctly.
    """
    content_hash = _signal_content_hash(record)
    natural_key = record.external_id or content_hash
    payload: dict[str, Any] = dict(record.data or {})
    if record.title:
        payload.setdefault("title", record.title)
    if record.body:
        payload.setdefault("body", record.body)
    # event_class + published_at — the backfill markers the window reads key on.
    payload["event_class"] = BACKFILL_EVENT_CLASS
    payload["published_at"] = record.published_at.astimezone(timezone.utc).isoformat()
    if record.entities:
        payload.setdefault("entities", list(record.entities))
    payload.setdefault(
        "manual_batch",
        {"batch_id": manifest.batch_id, "operator": manifest.operator},
    )
    return Signal(
        signal_id=_deterministic_signal_id(source_id, natural_key),
        source_id=source_id,
        source_version="1",
        produced_by_kind="source",
        fetched_at=fetched_at,
        owner_tenant="default",
        modality=record.modality or "text",
        payload=payload,
        canonical_url=record.canonical_url,
        language_hint=record.language,
        language=record.language,
        geo=list(record.geo),
        tags=list(record.tags),
        source_credibility=record.source_credibility,
        content_hash=content_hash,
    )


async def _register_manual_source(
    conn: Any, *, source_id: str, manifest: BatchManifest
) -> None:
    """Register (idempotently) the ``source.manual.<batch>`` head descriptor.

    Written ``state='configured'`` (NOT ``'active'``) + ``kind='manual'`` so the
    source RESOLVES for provenance/lineage/UI yet is excluded from boot actor
    wiring, from the poll-stall liveness watchdog, and from cadence polling — a
    manual source never polls. Stable ``version='1'`` + ``ON CONFLICT (…) DO
    NOTHING`` makes a re-run a no-op (exactly one head row per batch source).
    """
    body = {
        "identity": {
            "id": source_id,
            "name": f"Manual backfill — {manifest.batch_id}",
            "kind": "manual",
            "schema_uri": "legba/source/1.0.0",
            "version": "1",
            "abstraction_level": "L1",
            "state": "configured",
            "owner": manifest.operator,
        },
        "scope": {
            "owner_tenant": "default",
            "geo": [],
            "languages": [],
            "tags": ["manual", "backfill"],
        },
        "acquisition": "manual",
        "manual_batch": {
            "batch_id": manifest.batch_id,
            "operator": manifest.operator,
            "no_poll": True,
            "event_class": BACKFILL_EVENT_CLASS,
        },
    }
    await conn.execute(
        """
        INSERT INTO source_descriptors
            (descriptor_id, version, schema_uri, is_head, abstraction_level,
             kind, state, owner, name, body, inherits, created_at)
        VALUES ($1, '1', 'legba/source/1.0.0', true, 'L1',
                'manual', 'configured', $2, $3, $4::jsonb, '{}'::text[], now())
        ON CONFLICT (descriptor_id, version) DO NOTHING
        """,
        source_id,
        manifest.operator,
        f"Manual backfill — {manifest.batch_id}",
        json.dumps(body),
    )


async def _apply_signal(
    conn: Any,
    record: ManualSignalRecord,
    *,
    manifest: BatchManifest,
    source_id: str,
    fetched_at: datetime,
    enrichment_stage: EnrichmentStage | None,
    publish_signal: PublishSignal | None,
    report: ManualBatchReport,
) -> RecordAction:
    """Enrich + write ONE backfilled signal through the normal contract.

    Returns ``create`` (written), ``skip`` (idempotent id collision on a re-run,
    or a baseline filter dropped it), or ``dlq`` (the write raised).
    """
    sig = signal_from_record(
        record, manifest=manifest, source_id=source_id, fetched_at=fetched_at
    )
    ctx = SourceContext(
        target_id=source_id,
        target_version="1",
        source_id=source_id,
        config=_ManualSourceConfig(),
        state_store=InMemoryStateStore(),
        # A record's inline geo also seeds the baseline geo-from-scope backstop;
        # when the record carried none, a wired geocode stage fills the column.
        scope_geo=list(record.geo),
    )
    enriched = await run_baseline(
        sig, ctx, media="reference", enrichment_stage=enrichment_stage
    )
    if enriched is None:  # a baseline enrichment filter dropped it.
        return RecordAction.SKIP

    # The canonical write rides the runtime source path (host-side credibility
    # lookup + INSERT + dedupe). Imported lazily so importing this module (and
    # the pure classifier tests) doesn't pull the runtime source stack in.
    from ...runtime.source_actor import write_canonical_signal

    written_id = await write_canonical_signal(
        conn, enriched, source_version="1", owner_tenant="default"
    )
    if written_id is None:  # ON CONFLICT (id) — an idempotent re-run no-op.
        return RecordAction.SKIP

    # Best-effort fan-out on the backfill event-class subject. A publish failure
    # must never lose the (already-written) row — and nothing reactive consumes
    # a backfill anyway (every window read excludes it), so this is advisory.
    if publish_signal is not None:
        try:
            await publish_signal(enriched, BACKFILL_EVENT_CLASS)
        except Exception as exc:  # pragma: no cover — advisory fan-out
            logger.warning(
                "manual_batch.signal.publish_failed signal=%s err=%s",
                enriched.signal_id, exc,
            )
    return RecordAction.CREATE


# ---------------------------------------------------------------------------
# The ledger row (reuses the driver's content-hash dedupe)
# ---------------------------------------------------------------------------


async def _upsert_ledger_row(
    conn: Any,
    *,
    source: str,
    kind: str,
    source_type: str,
    content_hash: str,
    manifest_json: dict[str, Any],
) -> UUID:
    """Create or reuse the ``seed_batches`` ledger row for this batch.

    Dedupes on the natural key ``(source, kind, manifest->>content_hash)`` — the
    SAME dedupe ``run_seed_source`` uses — so a re-run of an identical batch
    UPDATEs the prior row in place (one ledger row per distinct batch) instead of
    minting a duplicate that overstates volume.
    """
    existing_id = await conn.fetchval(
        """
        SELECT id FROM seed_batches
         WHERE source = $1 AND kind = $2
           AND manifest->>'content_hash' = $3
         ORDER BY imported_at DESC
         LIMIT 1
        """,
        source,
        kind,
        content_hash,
    )
    if existing_id is not None:
        return await conn.fetchval(
            """
            UPDATE seed_batches
               SET source_type = $2, manifest = $3::jsonb, imported_at = now()
             WHERE id = $1
            RETURNING id
            """,
            existing_id,
            source_type,
            json.dumps(manifest_json),
        )
    return await conn.fetchval(
        """
        INSERT INTO seed_batches (source, kind, source_type, manifest)
        VALUES ($1, $2, $3, $4::jsonb)
        RETURNING id
        """,
        source,
        kind,
        source_type,
        json.dumps(manifest_json),
    )


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------


class _DryRunRollback(Exception):
    """Sentinel raised to force the ``dry_run`` transaction to roll back."""


async def run_manual_batch(
    pool: Any,
    *,
    batch_dir: str | Path,
    mode: BatchMode | str | None = None,
    dry_run: bool = False,
    enrichment_stage: EnrichmentStage | None = None,
    publish_signal: PublishSignal | None = None,
) -> ManualBatchReport:
    """Validate + load one manual-ingest batch through the seed plane.

    ``pool`` is an asyncpg pool. The whole batch is reconciled inside ONE
    transaction (atomic all-or-nothing); on ``dry_run`` the transaction is
    ROLLED BACK so nothing persists while the report's tallies still reflect the
    exact create/supersede/skip/unchanged/conflict decisions a wet run would
    make (dry ≡ wet counts). A wet run persists the report onto the batch's
    ``seed_batches`` manifest.

    Raises :class:`~legba.data.seed.manual_schema.BatchValidationError` (strict
    validation) BEFORE touching the DB when any record is malformed.
    """
    vb = validate_batch(batch_dir, strict=True)  # fail-loud on a bad batch.
    manifest = vb.manifest
    eff_mode = _coerce_mode(mode, manifest)
    source_type = _source_type_for(manifest)
    incoming_tier = _source_tier_rank(source_type)

    payloads = _seed_payloads(vb)
    content_hash = _content_hash("manual_batch", manifest.batch_id, payloads)

    report = ManualBatchReport(
        batch_id=manifest.batch_id,
        mode=eff_mode.value,
        source_type=source_type,
        grounding_eligible=manifest.grounding_eligible,
        dry_run=dry_run,
        deferred={lane: len(getattr(vb, lane)) for lane in _DEFERRED_LANES},
    )

    manifest_json: dict[str, Any] = {
        "batch_id": manifest.batch_id,
        "operator": manifest.operator,
        "mode": eff_mode.value,
        "provenance": manifest.default_provenance.value,
        "grounding_eligible": manifest.grounding_eligible,
        "content_hash": content_hash,
        "license": manifest.license,
        "source_url": manifest.source_url,
        "declared_lanes": list(manifest.files.declared().keys()),
        "imported_at": datetime.now(tz=timezone.utc).isoformat(),
        "dry_run": dry_run,
    }

    actx = _seed_ctx("manual_batch")

    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                batch_id = await _upsert_ledger_row(
                    conn,
                    source="manual_batch",
                    kind=manifest.batch_id,
                    source_type=source_type,
                    content_hash=content_hash,
                    manifest_json=manifest_json,
                )
                report.seed_batch_id = batch_id

                # Each record runs inside a per-record SAVEPOINT (a nested
                # asyncpg transaction) so an unexpected DB error rolls back ONLY
                # that record and the batch keeps going (degrade-not-drop) —
                # without poisoning the outer transaction the dry-run parity +
                # atomicity rely on.

                # 1) Explicit entity enrichment (idempotent upsert; not gated by
                #    mode — an entity is not an assertion).
                for e in vb.entities:
                    try:
                        async with conn.transaction():
                            await _resolve_entity(
                                conn,
                                canonical_name=e.canonical_name,
                                entity_class=e.entity_class,
                                geo_lat=e.geo_lat,
                                geo_lon=e.geo_lon,
                                geo_country=e.geo_country,
                                data=dict(e.data),
                            )
                        report.entities_resolved += 1
                    except Exception as exc:  # degrade-not-drop
                        report.errors.append(f"entity {e.canonical_name!r}: {exc}")

                # 2) Facts.
                for f in vb.facts:
                    try:
                        async with conn.transaction():
                            action = await _apply_fact(
                                conn,
                                f,
                                manifest=manifest,
                                mode=eff_mode,
                                source_type=source_type,
                                incoming_tier=incoming_tier,
                                batch_id=batch_id,
                                actx=actx,
                                report=report,
                            )
                        report.record("facts", action)
                    except Exception as exc:  # degrade-not-drop
                        report.errors.append(
                            f"fact ({f.subject}|{f.predicate}|{f.value}): {exc}"
                        )

                # 3) Nexuses.
                for n in vb.nexuses:
                    try:
                        async with conn.transaction():
                            action = await _apply_nexus(
                                conn,
                                n,
                                manifest=manifest,
                                mode=eff_mode,
                                source_type=source_type,
                                batch_id=batch_id,
                                actx=actx,
                                report=report,
                            )
                        report.record("nexuses", action)
                    except Exception as exc:  # degrade-not-drop
                        report.errors.append(
                            f"nexus ({n.subject}|{n.rel_type}|{n.object}): {exc}"
                        )

                # 4) Signals (S4-T4) — backfill through the normal signal
                #    contract. Register the ``source.manual.<batch>`` origin
                #    source once, then write each signal (event_class=backfill,
                #    published_at=event time, fetched_at=load time). One load time
                #    for the whole batch so a re-run's fetched_at is stable-ish.
                if vb.signals:
                    src_id = manual_source_id(manifest.batch_id)
                    report.manual_source_id = src_id
                    load_time = datetime.now(tz=timezone.utc)
                    # A dry-run rolls the whole transaction back, so it must not
                    # emit a real fan-out for a row that will never persist.
                    eff_publish = None if dry_run else publish_signal
                    try:
                        async with conn.transaction():
                            await _register_manual_source(
                                conn, source_id=src_id, manifest=manifest
                            )
                    except Exception as exc:  # degrade-not-drop
                        report.errors.append(f"manual source register {src_id!r}: {exc}")
                    for s in vb.signals:
                        try:
                            async with conn.transaction():
                                action = await _apply_signal(
                                    conn,
                                    s,
                                    manifest=manifest,
                                    source_id=src_id,
                                    fetched_at=load_time,
                                    enrichment_stage=enrichment_stage,
                                    publish_signal=eff_publish,
                                    report=report,
                                )
                            report.record("signals", action)
                        except Exception as exc:  # degrade-not-drop
                            report.record("signals", RecordAction.DLQ)
                            report.errors.append(
                                f"signal ({s.external_id or s.title!r}): {exc}"
                            )

                # 5) Persist the report + counts onto the ledger row (a wet run;
                #    on dry_run the whole transaction is discarded below).
                manifest_with_report = dict(manifest_json)
                manifest_with_report["report"] = report.as_dict()
                await conn.execute(
                    """
                    UPDATE seed_batches
                       SET counts = $2::jsonb, manifest = $3::jsonb
                     WHERE id = $1
                    """,
                    batch_id,
                    json.dumps(
                        {
                            "facts": report.facts,
                            "nexuses": report.nexuses,
                            "signals": report.signals,
                            "entities": report.entities_resolved,
                            "deferred": report.deferred,
                        }
                    ),
                    json.dumps(manifest_with_report),
                )

                if dry_run:
                    raise _DryRunRollback()
        except _DryRunRollback:
            # Expected: the dry-run transaction rolled back — nothing persisted,
            # the in-memory report stands. seed_batch_id was a would-be id.
            report.seed_batch_id = None

    return report


__all__ = [
    "RecordAction",
    "PriorFact",
    "PriorNexus",
    "classify_fact",
    "classify_nexus",
    "ManualBatchSeedSource",
    "ManualBatchReport",
    "run_manual_batch",
    "manual_source_id",
    "signal_from_record",
]
