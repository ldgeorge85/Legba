# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.seed.manual_schema — the manual-ingest batch FORMAT (S4-T1).

A *manual batch* is one directory an operator hands the system to backfill /
ground-state-update / merge the knowledge layer WITHOUT re-inventing the seed
plane. The batch rides the EXISTING seed machinery (``_driver`` → temporal
``write_fact`` / ``write_nexus`` → ``seed_batches`` ledger); this module only
defines + VALIDATES the on-disk shape. The loader adapter (S4-T2) consumes the
validated records next — nothing here writes to a DB.

Directory shape (the design's Lane-1..4 format)::

    batch_manifest.yaml   # BatchManifest below
    facts.jsonl           # one ManualFactRecord per line
    entities.jsonl        # one ManualEntityRecord per line
    nexuses.jsonl         # one ManualNexusRecord per line
    signals.jsonl         # one ManualSignalRecord per line
    docs.jsonl            # one ManualDocRecord per line (vector-corpus metadata)

Two honesty rails are load-bearing here (they mirror the platform's existing
gates, they are NOT new policy):

  * **Provenance tier** (``curated`` vs ``manual``). Only ``curated`` is
    grounding-eligible — it matches the Tier-1 provenance gate, which only
    injects ``seed``/``curated`` context into the authoritative preamble.
    Loading data as ``manual`` STORES it without making it a trusted prior. The
    tier defaults to the SAFE ``manual`` (a batch must ask, explicitly, to be
    grounding-eligible).
  * **Confidence** must be supplied honestly — per record OR as a batch default
    — and is REFUSED when absent for the fact/nexus lanes. There is no silent
    ``1.0`` (the data-quality audit's conf-1.0 lesson). :func:`validate_batch`
    reports an unresolvable confidence as a per-line error, not a fabricated
    default.

Field names deliberately MIRROR the typed seed payloads in ``_base`` (SeedFact
/ SeedEntity / SeedNexus) and the ``Signal`` contract so the S4-T2 adapter maps
a record → payload 1:1 without a translation layer. ``confidence`` is the one
deviation: the seed dataclasses default it to ``0.95``; a manual record leaves
it ``None`` so the REFUSE-absent policy can fire instead of a silent default.

Validation reports PER-LINE errors (file + 1-indexed line + reason), never a
single opaque failure: one bad record in a 10k-line batch names its own line.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# The manifest schema this module understands. Bump on a shape change (the
# manifest carries its own ``schema_version`` so a batch authored against an
# older format fails loud instead of silently mis-parsing).
MANUAL_BATCH_SCHEMA_VERSION = "1"


# ---------------------------------------------------------------------------
# Enums (behaviour-carrying — hence Enum, not a bare Literal)
# ---------------------------------------------------------------------------


class ProvenanceTier(str, Enum):
    """The provenance class stamped on a batch's rows.

    ``curated`` = authoritative + grounding-eligible (the Tier-1 preamble only
    trusts ``seed``/``curated`` context). ``manual`` = stored but NOT injected
    as a trusted prior. Defaulting to ``manual`` keeps an un-vetted batch out
    of the grounding path unless the operator explicitly elevates it.
    """

    CURATED = "curated"
    MANUAL = "manual"

    @property
    def grounding_eligible(self) -> bool:
        """Only ``curated`` may feed the Tier-1 grounding preamble."""
        return self is ProvenanceTier.CURATED


class BatchMode(str, Enum):
    """How the loader (S4-T2) reconciles a record against existing rows.

    ``skip`` insert-if-absent by natural key (re-run = no-op); ``merge`` fills
    empties + supersedes on a value change; ``force`` supersedes every match.
    None of the three EVER hard-deletes — history is preserved via the temporal
    ``valid_until``/``superseded_by`` close. Defined here so the manifest can
    carry the default; the write semantics live in the S4-T2 adapter.
    """

    SKIP = "skip"
    MERGE = "merge"
    FORCE = "force"


# ---------------------------------------------------------------------------
# Per-kind JSONL record schemas
# ---------------------------------------------------------------------------


class ManualFactRecord(BaseModel):
    """One ``(subject, predicate, value)`` attribute triple → a ``facts`` row.

    Mirrors :class:`~legba.data.seed._base.SeedFact`. ``valid_from`` is REQUIRED
    (manual facts are temporally honest, exactly like curated seeds — a leader
    fact carries its start date). ``confidence`` is ``None`` by default so the
    REFUSE-absent policy (:func:`validate_batch`) can require it per-record or
    fall back to the batch default — never a silent ``1.0``.

    Natural key (idempotency + merge target): ``(subject, predicate,
    valid_from)`` — the temporal-model key.
    """

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=2048)
    predicate: str = Field(min_length=1, max_length=512)
    value: str = Field(min_length=1, max_length=4096)
    valid_from: datetime
    valid_until: datetime | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    geo_lat: float | None = None
    geo_lon: float | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    def natural_key(self) -> tuple[str, str, datetime]:
        return (self.subject, self.predicate, self.valid_from)


class ManualEntityRecord(BaseModel):
    """A canonical entity to fold into ``entity_profiles``.

    Mirrors :class:`~legba.data.seed._base.SeedEntity`. Emit one only to enrich
    an entity with a class / geo the facts alone wouldn't carry; the loader
    auto-resolves every fact/nexus endpoint anyway.

    Natural key: ``canonical_name`` — resolved THROUGH the shared entity-canon
    normalizer (``legba.data._entity_canon.canonicalize_entity``) at load time,
    so dedupe here matches the live entity-resolution path. Entities carry no
    ``confidence`` (an entity is not an assertion).
    """

    model_config = ConfigDict(extra="forbid")

    canonical_name: str = Field(min_length=1, max_length=2048)
    entity_class: str = Field(default="entity", max_length=64)
    geo_lat: float | None = None
    geo_lon: float | None = None
    geo_country: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    def natural_key(self) -> str:
        return self.canonical_name


class ManualNexusRecord(BaseModel):
    """One reified, typed, SIGNED relationship → a ``nexuses`` row.

    Mirrors :class:`~legba.data.seed._base.SeedNexus`. ``polarity`` is the
    structural-balance sign (+1 supportive / -1 antagonistic / 0 neutral);
    ``valid_from`` is REQUIRED; ``confidence`` follows the same REFUSE-absent
    policy as facts.

    Natural key: ``(subject, rel_type, object, valid_from)`` — the design's
    ``(src, relation, dst, valid_from)``.
    """

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=2048)
    object: str = Field(min_length=1, max_length=2048)
    rel_type: str = Field(min_length=1, max_length=512)
    polarity: int = Field(ge=-1, le=1)
    valid_from: datetime
    valid_until: datetime | None = None
    intermediary: str | None = Field(default=None, max_length=2048)
    label: str = Field(default="", max_length=4096)
    intent: str = Field(default="", max_length=512)
    channel: str = Field(default="direct", max_length=64)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    data: dict[str, Any] = Field(default_factory=dict)

    def natural_key(self) -> tuple[str, str, str, datetime]:
        return (self.subject, self.rel_type, self.object, self.valid_from)


class ManualSignalRecord(BaseModel):
    """A backfilled event/article/report → a ``Signal`` (the signals lane).

    Enters through the NORMAL ``Signal`` contract so enrichment / fan-out /
    dedupe all apply (the loader, S4-T4, maps this record → ``Signal`` and
    stamps ``event_class=backfill``). ``published_at`` is the REAL event time
    (backdated); ``fetched_at`` is stamped at load time by the loader. Inline
    ``entities``/``geo`` let a pre-processed payload skip NER; absent, baseline
    enrichment runs.

    Natural key: ``external_id`` when supplied (a stable source id); otherwise
    the pipeline's existing content-hash dedupe keys the row (computed
    downstream from ``title``/``body`` — this schema does not fabricate one).
    A signal carries ``source_credibility`` (a source property), NOT
    ``confidence`` (facts assert; signals observe).
    """

    model_config = ConfigDict(extra="forbid")

    external_id: str | None = Field(default=None, max_length=512)
    title: str = Field(default="", max_length=4096)
    body: str = Field(default="", max_length=1_048_576)
    canonical_url: str | None = Field(default=None, max_length=4096)
    published_at: datetime
    modality: str = Field(default="text", max_length=32)
    language: str | None = Field(default=None, max_length=16)
    geo: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    source_credibility: float | None = Field(default=None, ge=0.0, le=1.0)
    data: dict[str, Any] = Field(default_factory=dict)

    def natural_key(self) -> str | None:
        return self.external_id


class ManualDocRecord(BaseModel):
    """One vector-corpus chunk's metadata → the Qdrant Lane-4 sink.

    The chunk TEXT may be inline (``text``) or referenced (``text_ref``, a path
    under the batch's ``docs/`` dir the loader reads + chunks). Payload metadata
    mirrors the RAG plan's chunk metadata so retrieval can filter by
    corpus/country/topic/effective-date.

    Natural key: ``(corpus, doc_id, chunk_seq)``.
    """

    model_config = ConfigDict(extra="forbid")

    corpus: str = Field(min_length=1, max_length=128)
    doc_id: str = Field(min_length=1, max_length=512)
    chunk_seq: int = Field(default=0, ge=0)
    title: str = Field(default="", max_length=4096)
    section: str = Field(default="", max_length=1024)
    text: str | None = Field(default=None, max_length=1_048_576)
    text_ref: str | None = Field(default=None, max_length=1024)
    countries: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    lang: str | None = Field(default=None, max_length=16)
    license: str | None = Field(default=None, max_length=256)
    source_url: str | None = Field(default=None, max_length=4096)
    effective_date: datetime | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    def natural_key(self) -> tuple[str, str, int]:
        return (self.corpus, self.doc_id, self.chunk_seq)


# The kind → (record model, manifest filename attribute) table drives the
# whole validator. Kept in one place so adding a lane is one row.
_KIND_MODELS: dict[str, type[BaseModel]] = {
    "facts": ManualFactRecord,
    "entities": ManualEntityRecord,
    "nexuses": ManualNexusRecord,
    "signals": ManualSignalRecord,
    "docs": ManualDocRecord,
}

# Lanes whose records assert something → subject to the REFUSE-absent
# confidence policy. Entities/signals/docs are NOT assertions (see their
# docstrings), so they are exempt.
_CONFIDENCE_KINDS = frozenset({"facts", "nexuses"})


# ---------------------------------------------------------------------------
# Manifest schema
# ---------------------------------------------------------------------------


class BatchFiles(BaseModel):
    """Per-kind file references relative to the batch directory.

    At least one lane must be declared (an empty batch is meaningless). A kind
    whose file is not listed here is simply absent from the batch — the loader
    processes only declared lanes.
    """

    model_config = ConfigDict(extra="forbid")

    facts: str | None = None
    entities: str | None = None
    nexuses: str | None = None
    signals: str | None = None
    docs: str | None = None

    def declared(self) -> dict[str, str]:
        """``{kind: filename}`` for every lane this batch declares."""
        return {
            kind: fn
            for kind, fn in (
                ("facts", self.facts),
                ("entities", self.entities),
                ("nexuses", self.nexuses),
                ("signals", self.signals),
                ("docs", self.docs),
            )
            if fn
        }


class BatchManifest(BaseModel):
    """``batch_manifest.yaml`` — the batch's identity + defaults.

    ``default_provenance`` (tier) and ``default_confidence`` are the honesty
    rails: the tier gates grounding-eligibility, the confidence default is the
    per-batch fallback records may omit. ``license``/``source_url``/
    ``provenance_notes`` are provenance defaults every record inherits unless it
    overrides them (records carry their own ``data`` bag; the loader threads
    these onto rows that don't specify their own).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    batch_id: str = Field(min_length=1, max_length=256)
    operator: str = Field(min_length=1, max_length=256)
    created_at: datetime
    description: str = Field(default="", max_length=8192)

    default_provenance: ProvenanceTier = ProvenanceTier.MANUAL
    mode: BatchMode = BatchMode.SKIP
    # Batch-level confidence fallback for the fact/nexus lanes; a record may
    # override per-record. May be omitted ONLY if every asserting record carries
    # its own confidence (validate_batch enforces this — REFUSE absent).
    default_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    files: BatchFiles

    # Provenance / licensing defaults inherited by records that omit them.
    license: str | None = Field(default=None, max_length=256)
    source_url: str | None = Field(default=None, max_length=4096)
    provenance_notes: str = Field(default="", max_length=8192)

    @property
    def grounding_eligible(self) -> bool:
        return self.default_provenance.grounding_eligible


# ---------------------------------------------------------------------------
# Validation result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordError:
    """One per-line validation failure.

    ``line`` is 1-indexed within ``file`` (matching what an editor shows), so
    ``manifest.yaml`` aside, an operator can jump straight to the offending
    record. ``kind`` is the lane; ``reason`` is a compact human message.
    """

    kind: str
    file: str
    line: int
    reason: str

    def __str__(self) -> str:  # stable, greppable one-liner
        return f"{self.file}:{self.line} [{self.kind}] {self.reason}"


@dataclass
class ValidatedBatch:
    """The outcome of :func:`validate_batch`.

    Carries the parsed manifest, the typed records grouped by lane, and EVERY
    per-line error (validation does not stop at the first bad record). ``ok`` is
    true only when no lane produced an error.
    """

    manifest: BatchManifest
    facts: list[ManualFactRecord] = field(default_factory=list)
    entities: list[ManualEntityRecord] = field(default_factory=list)
    nexuses: list[ManualNexusRecord] = field(default_factory=list)
    signals: list[ManualSignalRecord] = field(default_factory=list)
    docs: list[ManualDocRecord] = field(default_factory=list)
    errors: list[RecordError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def records_for(self, kind: str) -> list[Any]:
        return getattr(self, kind)


class BatchValidationError(Exception):
    """Raised by :func:`validate_batch` in strict mode when any lane failed.

    Aggregates the per-line errors; ``.errors`` is the full list so a caller
    can render each ``file:line`` reason rather than a single opaque failure.
    """

    def __init__(self, errors: list[RecordError]) -> None:
        self.errors = list(errors)
        head = "; ".join(str(e) for e in self.errors[:5])
        more = "" if len(self.errors) <= 5 else f" (+{len(self.errors) - 5} more)"
        super().__init__(f"{len(self.errors)} invalid record(s): {head}{more}")


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------


def _format_validation_error(exc: ValidationError) -> str:
    """Compact a pydantic ValidationError into a single-line reason."""
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "<record>"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts)


def load_manifest(source: str | Path) -> BatchManifest:
    """Parse + validate a ``batch_manifest.yaml`` (path or raw YAML text).

    Raises ``pydantic.ValidationError`` on a malformed manifest and ``ValueError``
    on a schema-version mismatch or a batch that declares no lane — the manifest
    is fail-loud (a bad manifest can't produce per-line record errors because
    there are no records to walk yet).
    """
    text = (
        source.read_text(encoding="utf-8")
        if isinstance(source, Path)
        else str(source)
    )
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise ValueError("batch_manifest.yaml must be a YAML mapping")
    manifest = BatchManifest.model_validate(raw)
    if manifest.schema_version != MANUAL_BATCH_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported manifest schema_version {manifest.schema_version!r}; "
            f"this build understands {MANUAL_BATCH_SCHEMA_VERSION!r}"
        )
    if not manifest.files.declared():
        raise ValueError("manifest declares no lane files (empty batch)")
    return manifest


def _parse_jsonl(
    text: str,
    *,
    kind: str,
    model: type[BaseModel],
    filename: str,
    manifest: BatchManifest,
) -> tuple[list[Any], list[RecordError]]:
    """Parse one JSONL lane, collecting per-line errors (never aborting).

    Blank / whitespace-only lines are skipped (not an error). A line that is
    not valid JSON, not a JSON object, fails model validation, or (for the
    asserting lanes) resolves to no confidence, becomes a :class:`RecordError`
    naming its 1-indexed line number.
    """
    records: list[Any] = []
    errors: list[RecordError] = []
    needs_conf = kind in _CONFIDENCE_KINDS
    have_batch_default = manifest.default_confidence is not None

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            errors.append(RecordError(kind, filename, lineno, f"invalid JSON: {exc.msg}"))
            continue
        if not isinstance(obj, dict):
            errors.append(
                RecordError(kind, filename, lineno, "record must be a JSON object")
            )
            continue
        try:
            rec = model.model_validate(obj)
        except ValidationError as exc:
            errors.append(
                RecordError(kind, filename, lineno, _format_validation_error(exc))
            )
            continue
        # REFUSE-absent confidence policy for the asserting lanes: neither a
        # per-record confidence NOR a batch default → a per-line error, never a
        # silent 1.0.
        if needs_conf and rec.confidence is None and not have_batch_default:
            errors.append(
                RecordError(
                    kind,
                    filename,
                    lineno,
                    "confidence absent and no batch default_confidence "
                    "(refusing a silent 1.0)",
                )
            )
            continue
        records.append(rec)

    return records, errors


def validate_batch(batch_dir: str | Path, *, strict: bool = False) -> ValidatedBatch:
    """Validate a manual-ingest batch directory.

    Reads ``batch_manifest.yaml``, then every declared lane file line-by-line,
    validating each record against its per-kind schema. Returns a
    :class:`ValidatedBatch` carrying the typed records AND every per-line error
    (validation walks the whole batch — one bad record does not mask the rest).

    A missing lane file listed in the manifest is reported as a single
    ``file:0`` error (the file, not a line, is the fault). With ``strict=True``
    a non-empty error list raises :class:`BatchValidationError` instead of
    returning. The manifest itself is fail-loud (see :func:`load_manifest`): a
    malformed manifest raises before any lane is walked.
    """
    batch_dir = Path(batch_dir)
    manifest = load_manifest(batch_dir / "batch_manifest.yaml")

    result = ValidatedBatch(manifest=manifest)
    for kind, filename in manifest.files.declared().items():
        model = _KIND_MODELS[kind]
        path = batch_dir / filename
        if not path.is_file():
            result.errors.append(
                RecordError(kind, filename, 0, "declared lane file not found")
            )
            continue
        recs, errs = _parse_jsonl(
            path.read_text(encoding="utf-8"),
            kind=kind,
            model=model,
            filename=filename,
            manifest=manifest,
        )
        result.records_for(kind).extend(recs)
        result.errors.extend(errs)

    if strict and result.errors:
        raise BatchValidationError(result.errors)
    return result


__all__ = [
    "MANUAL_BATCH_SCHEMA_VERSION",
    "ProvenanceTier",
    "BatchMode",
    "ManualFactRecord",
    "ManualEntityRecord",
    "ManualNexusRecord",
    "ManualSignalRecord",
    "ManualDocRecord",
    "BatchFiles",
    "BatchManifest",
    "RecordError",
    "ValidatedBatch",
    "BatchValidationError",
    "load_manifest",
    "validate_batch",
]
