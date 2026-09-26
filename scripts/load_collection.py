#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Program 7g-1 — load a collection's series into ``observations``.

A COLLECTION IS NOT A SOURCE and this is not an ingest. There is no NATS
publish here, no signal write, no enrichment enqueue and no analyst run: this
program fetches numbers from a provider and writes rows DIRECTLY into
``observations``. That directness IS the firewall's first line — ten years of
history entering through the live ingest pipeline would read as the largest
surge in the platform's life, as thirty publishers coming back from the dead,
and as a calibration set made of numbers nobody predicted.

WHAT MAKES IT SAFE TO RUN TWICE. The load is idempotent through the table's
own unique key ``(collection_id, series_id, subject, valid_from, valid_to,
record_time)``: a second run of the same collection VERSION writes the same
rows, conflicts, and does nothing. It is resumable through
``collection_loads.resume_key`` — the last ``series_id:subject`` pair that
completed — so ``--resume`` after an interruption starts at the next pair
rather than at the beginning. Neither mechanism trusts the other: resume
skips work, the unique key guarantees correctness.

WHAT IT REFUSES, LOUDLY.

* A ``documents_*`` loader kind. Documents land in ``signals``, and the
  loader kinds that would write them are a declared seam (SEAMS #62) — the
  ``signals_origin_class_history_writer_not_built`` CHECK refuses the row
  at the table, and this program refuses at the top of the load rather than
  writing a partial holding.
* A cadence this program cannot read a PERIOD from. The shared provider
  parsers key their maps by YEAR, so ``cadence: annual`` is what a series
  row can be built from today; a sub-annual cadence raises before any fetch
  rather than silently stamping a whole-year validity on a monthly number.
* A pair whose provider published no revision stamp. ``record_time`` is when
  the PROVIDER published or revised a figure; stamping ``now()`` in its place
  would turn a 2016 number into something recorded today, which is precisely
  the lie the bitemporal table exists to prevent. Such a pair is SKIPPED with
  its reason recorded in the load ledger, never written with a guessed time.
* A year the provider does not hold. No row is written. Absence is absence,
  never a zero a reader could average.

ONE PARSER, TWO READERS. The provider response shapes are read by
``scripts/verify_collection_manifest.py`` — ``year_map_from_world_bank`` and
``year_map_from_eia_record``, plus its bulk download/scan and its digest —
and this program imports them rather than re-deriving them. A loader that
parsed a World Bank envelope its own way could disagree with the verifier
about what a provider holds, and the disagreement would surface only as a
wrong number inside a citation.

USAGE::

    python3 scripts/load_collection.py descriptors/collection_<id>.yaml
    python3 scripts/load_collection.py collection.series_pilot_2016_2026
    python3 scripts/load_collection.py <manifest> --dry-run
    python3 scripts/load_collection.py <manifest> --resume
    python3 scripts/load_collection.py <manifest> --dsn postgresql://...

``--dry-run`` fetches and reports what WOULD be written and touches the
database not at all — not even the ledger row.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol, Sequence

import asyncpg
import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from legba.data.config import PostgresConfig                    # noqa: E402
from legba.data.schemas.collection import (                     # noqa: E402
    DOCUMENT_LOADER_KINDS,
    CollectionDescriptor,
    SeriesEntry,
)

#: Deploy marker — the rolling deploy greps for this exact string.
COLLECTION_LOADER_VERSION = "2026-09/7g-1"

#: The one cadence a series row can be built from today (see the module
#: docstring's refusal list and SEAMS #62's narrative).
SUPPORTED_CADENCES: frozenset[str] = frozenset({"annual", "yearly"})

logger = logging.getLogger("load_collection")


# ===========================================================================
# 0. The shared parser — imported, never re-implemented
# ===========================================================================


def _load_verifier() -> Any:
    """Import ``scripts/verify_collection_manifest.py`` as a module.

    The verifier is a CLI rather than a package member, so it is loaded by
    file location. The module is placed in ``sys.modules`` BEFORE it is
    executed because ``@dataclass`` resolves a class's own module out of
    ``sys.modules`` while processing its fields.
    """
    name = "verify_collection_manifest"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parent / "verify_collection_manifest.py"
    )
    if spec is None or spec.loader is None:      # pragma: no cover - packaging
        raise RuntimeError("cannot import scripts/verify_collection_manifest.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


vcm = _load_verifier()


# ===========================================================================
# 1. Errors
# ===========================================================================


class CollectionLoadError(RuntimeError):
    """The load cannot proceed, and no partial holding will be written."""


class DocumentLoaderNotBuilt(CollectionLoadError):
    """SEAMS #62 — the ``documents_*`` collection loader kinds.

    Series numbers go to ``observations``; DOCUMENTS would go to ``signals``
    with a history ``origin_class``, through the same deterministic
    enrichment the live lane runs, and the ~70 un-swept ``signals`` readers
    have not been swept for the history classes. Raised at the top of the
    load: nothing is fetched, nothing is written, and the
    ``signals_origin_class_history_writer_not_built`` CHECK is the second
    layer behind it.
    """


# ===========================================================================
# 2. Rows
# ===========================================================================


@dataclass(frozen=True)
class ObservationRow:
    """One row bound for ``observations`` — every column the table requires."""

    collection_id: str
    series_id: str
    subject_kind: str
    subject: str
    valid_from: date
    valid_to: date
    record_time: datetime
    # ``Decimal``, not ``float``: the column is ``numeric`` and a provider's
    # figure is the number it published. Round-tripping through binary
    # floating point to reach the driver would quietly re-round a value the
    # platform then cites as fetched.
    value: Decimal | None
    unit: str
    value_text: str | None
    source_url: str
    sha256: str
    origin_class: str
    provenance: dict[str, Any]

    def provenance_json(self) -> str:
        return json.dumps(self.provenance, sort_keys=True, default=str)


@dataclass(frozen=True)
class FetchedSeries:
    """What one (series, subject) fetch produced, before it becomes rows."""

    by_year: dict[int, Any]
    provider_last_updated: str | None
    source_url: str
    sha256: str
    provider_revision: dict[str, Any] = field(default_factory=dict)


@dataclass
class LoadResult:
    """What a load did — counted in DURABLE terms.

    ``pairs_done`` and ``resume_key`` describe what has been written and
    committed, not what has been read: rows are batched, so a pair can be
    fetched and still be in memory when a run dies. See ``run_load``.
    """

    collection_id: str
    collection_version: str
    pairs_total: int = 0
    pairs_done: int = 0
    rows_written: int = 0
    rows_skipped: int = 0
    skipped: list[str] = field(default_factory=list)
    resume_key: str | None = None
    status: str = "running"
    error: str | None = None


# One statement per batch, and the conflict target is the table's own
# identity index — which is what makes a second load of the same collection
# version write nothing rather than duplicate. RETURNING is how the written
# count is MEASURED: a re-run that inserts nothing must report zero, never
# the batch length.
_INSERT_SQL = """
    INSERT INTO observations
        (collection_id, series_id, subject_kind, subject,
         valid_from, valid_to, record_time,
         value, unit, value_text, source_url, sha256, origin_class, provenance)
    SELECT t.collection_id, t.series_id, t.subject_kind, t.subject,
           t.valid_from, t.valid_to, t.record_time,
           t.value, t.unit, t.value_text, t.source_url, t.sha256,
           t.origin_class, t.provenance::jsonb
      FROM unnest($1::text[], $2::text[], $3::text[], $4::text[],
                  $5::date[], $6::date[], $7::timestamptz[],
                  $8::numeric[], $9::text[], $10::text[], $11::text[],
                  $12::text[], $13::text[], $14::text[])
           AS t(collection_id, series_id, subject_kind, subject,
                valid_from, valid_to, record_time,
                value, unit, value_text, source_url, sha256,
                origin_class, provenance)
    ON CONFLICT (collection_id, series_id, subject, valid_from, valid_to,
                 record_time)
    DO NOTHING
    RETURNING id
"""


# ===========================================================================
# 3. Period + record-time arithmetic
# ===========================================================================


def annual_period(year: int) -> tuple[date, date]:
    """The VALID-time period an annual observation is about."""
    return date(year, 1, 1), date(year, 12, 31)


def parse_record_time(raw: str | None) -> datetime | None:
    """The provider's publication/revision stamp as an aware UTC datetime.

    Two shapes in the wild: the World Bank's whole-release date
    (``2026-07-13``) and the EIA's per-series ISO instant with an offset
    (``2026-06-02T15:11:49-04:00``). A bare date is read as UTC midnight —
    the honest reading of "this release landed that day", and it is a
    PROVIDER-supplied instant either way.

    ``None`` (and an unparseable stamp) come back as ``None`` so the caller
    can SKIP the pair. Never falls back to ``now()``: a 2016 figure recorded
    "today" is the exact lie the bitemporal table exists to prevent.
    """
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def rows_for_pair(
    descriptor: CollectionDescriptor,
    series: SeriesEntry,
    subject: str,
    fetched: FetchedSeries,
    *,
    collection_version: str,
) -> tuple[list[ObservationRow], str | None]:
    """Turn one fetched (series, subject) into rows, or say why it was skipped.

    Returns ``(rows, skip_reason)``. A pair with a provider stamp but no held
    year yields ``([], None)`` — that is not a skip, it is a provider hole
    the manifest already records.
    """
    cadence = series.cadence.strip().lower()
    if cadence not in SUPPORTED_CADENCES:
        raise CollectionLoadError(
            f"series {series.series_id!r} declares cadence {series.cadence!r}; "
            f"this loader builds a VALID period from a YEAR only "
            f"({sorted(SUPPORTED_CADENCES)}). A sub-annual cadence would have "
            "to stamp a whole-year validity on a monthly number, which it "
            "refuses to do (SEAMS #62)."
        )
    record_time = parse_record_time(fetched.provider_last_updated)
    if record_time is None:
        return [], (
            f"{series.series_id}:{subject} — the provider published no "
            "revision stamp, so record_time is unknowable; the row is NOT "
            "written with a guessed time"
        )

    first_year = int(series.valid_from[:4])
    last_year = int(series.valid_to[:4])
    subject_kind = series.subject_kind or descriptor.subject_kind
    rows: list[ObservationRow] = []
    for year in range(first_year, last_year + 1):
        value = fetched.by_year.get(year)
        if value is None:
            # The provider holds nothing for this year. No row: absence is
            # absence, and a zero here would be a number nobody published.
            continue
        valid_from, valid_to = annual_period(year)
        provenance: dict[str, Any] = {
            "provider": series.provider,
            "dataset": series.dataset,
            "indicator": series.indicator,
            "indicator_name": series.indicator_name,
            "provider_last_updated": fetched.provider_last_updated,
            "collection_version": collection_version,
            "licence_class": descriptor.licence_class,
            "fetch_mode": series.fetch.mode,
            "row_offset": sorted(fetched.by_year).index(year),
        }
        if fetched.provider_revision:
            provenance["provider_revision"] = fetched.provider_revision
        numeric: Decimal | None
        text: str | None
        try:
            numeric, text = Decimal(str(value)), None
        except (TypeError, ValueError, InvalidOperation):
            numeric, text = None, str(value)
        rows.append(
            ObservationRow(
                collection_id=descriptor.identity.id,
                series_id=series.series_id,
                subject_kind=subject_kind,
                subject=subject,
                valid_from=valid_from,
                valid_to=valid_to,
                record_time=record_time,
                value=numeric,
                unit=series.unit,
                value_text=text,
                source_url=fetched.source_url,
                sha256=fetched.sha256,
                origin_class=descriptor.origin_class,
                provenance=provenance,
            )
        )
    return rows, None


# ===========================================================================
# 4. Fetching
# ===========================================================================


class PairFetcher(Protocol):
    """Whatever can answer "what does the provider hold for this pair".

    A protocol rather than a concrete client so the firewall proof can drive
    the whole write path from the verifier's RECORDED fixtures with no socket
    open anywhere — the test exercises the real row builder, the real SQL and
    the real ledger, and only the transport is substituted.
    """

    async def prepare(self, plan: "LoadPlan") -> None: ...

    async def fetch(
        self, series: SeriesEntry, subject: str, iso3: str | None
    ) -> FetchedSeries: ...

    async def close(self) -> None: ...


@dataclass
class LoadPlan:
    """The ordered (series, subject) pairs a load walks, and its bulk groups."""

    descriptor: CollectionDescriptor
    manifest: Any                     # vcm.Manifest — the verifier's fetch plan
    pairs: list[tuple[SeriesEntry, str]]

    def bulk_groups(self) -> dict[tuple[str, str], list[Any]]:
        """The verifier's own grouping of ``bulk_jsonl`` series by archive."""
        return vcm._bulk_groups(self.manifest)


def build_plan(descriptor: CollectionDescriptor, manifest: Any) -> LoadPlan:
    """The canonical pair ORDER — what ``resume_key`` is an index into.

    Declaration order, series then subject: a resume must walk the same
    sequence the interrupted run did, and "the order the manifest is written
    in" is the only ordering both a human and a rerun can agree on.
    """
    pairs = [
        (series, subject)
        for series in descriptor.manifest.series
        for subject in series.subjects
    ]
    return LoadPlan(descriptor=descriptor, manifest=manifest, pairs=pairs)


def pair_key(series: SeriesEntry, subject: str) -> str:
    """The ``series_id:subject`` shape the manifest declares as its resume key."""
    return f"{series.series_id}:{subject}"


class _RateLimiter:
    """One minimum-interval gate per provider (``rate_limit_per_second``)."""

    def __init__(self, rates: dict[str, float]) -> None:
        self._min_interval = {
            provider: 1.0 / float(rate) for provider, rate in rates.items()
        }
        self._last: dict[str, float] = {}

    async def wait(self, provider: str) -> None:
        interval = self._min_interval.get(provider)
        if not interval:
            return
        last = self._last.get(provider)
        now = time.monotonic()
        if last is not None:
            delay = interval - (now - last)
            if delay > 0:
                await asyncio.sleep(delay)
        self._last[provider] = time.monotonic()


class HttpPairFetcher:
    """The production fetcher — the verifier's own transport, reused.

    ``json_api`` is one GET per (series, subject); ``bulk_jsonl`` downloads
    each provider archive ONCE for the whole run and scans it for every pair
    that names it, because twenty EIA series would otherwise be twenty
    downloads of the same 24 MB file.
    """

    def __init__(self, cache_dir: Path, rates: dict[str, float]) -> None:
        self._cache_dir = cache_dir
        self._limiter = _RateLimiter(rates)
        self._client = httpx.Client(
            headers={"User-Agent": f"legba-collection-loader/{COLLECTION_LOADER_VERSION}"}
        )
        self._bulk_records: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self._bulk_digest: dict[tuple[str, str], str] = {}

    async def prepare(self, plan: LoadPlan) -> None:
        for key, specs in plan.bulk_groups().items():
            url, member = key
            wanted = {
                vcm.resolve_indicator(spec, subject, plan.manifest.iso3(subject))
                for spec in specs
                for subject in spec.subjects
            }
            archive = self._cache_dir / Path(httpx.URL(url).path).name
            logger.info("fetching %s (%d series) ...", url, len(wanted))
            await self._limiter.wait(specs[0].provider)
            vcm.download_bulk(self._client, url, archive)
            digest = vcm.sha256_of(archive)
            logger.info(
                "%s: %d bytes, sha256 %s",
                archive.name, archive.stat().st_size, digest,
            )
            self._bulk_digest[key] = digest
            self._bulk_records[key] = vcm.scan_bulk_jsonl(
                archive, member, wanted,
                str(specs[0].fetch.get("record_key") or "series_id"),
            )

    async def fetch(
        self, series: SeriesEntry, subject: str, iso3: str | None
    ) -> FetchedSeries:
        url = series.source_url_template.format(subject=subject, iso3=iso3 or "")
        await self._limiter.wait(series.provider)
        if series.fetch.mode == "json_api":
            response = self._client.get(url, timeout=vcm._JSON_TIMEOUT_S)
            response.raise_for_status()
            body = response.content
            payload = json.loads(body.decode("utf-8"))
            by_year, last_updated = vcm.year_map_from_world_bank(payload)
            return FetchedSeries(
                by_year=by_year,
                provider_last_updated=last_updated,
                source_url=url,
                sha256=hashlib.sha256(body).hexdigest(),
            )
        key = (str(series.fetch.bulk_url), str(series.fetch.bulk_member))
        indicator = series.indicator.format(subject=subject, iso3=iso3 or "")
        record = self._bulk_records.get(key, {}).get(indicator)
        by_year, last_updated = vcm.year_map_from_eia_record(record)
        return FetchedSeries(
            by_year=by_year,
            provider_last_updated=last_updated,
            source_url=url,
            sha256=self._bulk_digest.get(key, ""),
            provider_revision={"bulk_member": series.fetch.bulk_member},
        )

    async def close(self) -> None:
        self._client.close()


# ===========================================================================
# 5. The ledger
# ===========================================================================


_LEDGER_SELECT = """
    SELECT id, status, resume_key, pairs_done, rows_written, rows_skipped
      FROM collection_loads
     WHERE collection_id = $1 AND collection_version = $2
"""

_LEDGER_UPSERT = """
    INSERT INTO collection_loads
        (collection_id, collection_version, descriptor_version, loader_kind,
         resume_key, status, pairs_total, pairs_done, rows_written,
         rows_skipped, started_at, provenance)
    VALUES ($1, $2, $3, $4, $5, 'running', $6, $7, $8, $9, now(), $10::jsonb)
    ON CONFLICT (collection_id, collection_version) DO UPDATE
       SET status = 'running',
           descriptor_version = EXCLUDED.descriptor_version,
           loader_kind = EXCLUDED.loader_kind,
           resume_key = EXCLUDED.resume_key,
           pairs_total = EXCLUDED.pairs_total,
           pairs_done = EXCLUDED.pairs_done,
           rows_written = EXCLUDED.rows_written,
           rows_skipped = EXCLUDED.rows_skipped,
           started_at = now(),
           finished_at = NULL,
           error = NULL,
           provenance = EXCLUDED.provenance
    RETURNING id
"""

_LEDGER_PROGRESS = """
    UPDATE collection_loads
       SET resume_key = $2, pairs_done = $3, rows_written = $4,
           rows_skipped = $5
     WHERE id = $1
"""

_LEDGER_FINISH = """
    UPDATE collection_loads
       SET status = $2, finished_at = now(), error = $3,
           resume_key = $4, pairs_done = $5, rows_written = $6,
           rows_skipped = $7, provenance = $8::jsonb
     WHERE id = $1
"""


# ===========================================================================
# 6. The load
# ===========================================================================


async def run_load(
    plan: LoadPlan,
    fetcher: PairFetcher,
    conn: asyncpg.Connection | None,
    *,
    resume: bool = False,
    dry_run: bool = False,
) -> LoadResult:
    """Walk the plan, write the rows, keep the ledger honest.

    ``conn=None`` is only legal with ``dry_run=True``: the dry run must be
    incapable of writing, not merely unwilling.
    """
    descriptor = plan.descriptor
    if descriptor.loader.kind in DOCUMENT_LOADER_KINDS:
        raise DocumentLoaderNotBuilt(
            f"collection {descriptor.identity.id!r} declares loader.kind "
            f"{descriptor.loader.kind!r}. The collection DOCUMENT loader "
            "kinds are not built (SEAMS #62): documents land in `signals` "
            "with a history origin_class, and the signals readers are not "
            "swept for the history classes — the "
            "`signals_origin_class_history_writer_not_built` CHECK refuses "
            "the row at the table. Nothing was fetched and nothing written."
        )
    if conn is None and not dry_run:
        raise CollectionLoadError("a real load needs a database connection")

    collection_version = descriptor.manifest_hash()
    result = LoadResult(
        collection_id=descriptor.identity.id,
        collection_version=collection_version,
        pairs_total=len(plan.pairs),
    )

    start_at = 0
    ledger_id = None
    if not dry_run:
        assert conn is not None
        prior = await conn.fetchrow(
            _LEDGER_SELECT, descriptor.identity.id, collection_version
        )
        prior_resume = prior["resume_key"] if prior is not None else None
        if resume and prior_resume:
            keys = [pair_key(s, subj) for s, subj in plan.pairs]
            if prior_resume in keys:
                start_at = keys.index(prior_resume) + 1
                result.pairs_done = start_at
                result.rows_written = int(prior["rows_written"] or 0)
                result.rows_skipped = int(prior["rows_skipped"] or 0)
                result.resume_key = prior_resume
                logger.info(
                    "resuming after %s — %d of %d pairs already done",
                    prior_resume, start_at, len(plan.pairs),
                )
            else:
                logger.warning(
                    "resume_key %r is not a pair in this manifest version; "
                    "starting from the beginning (the unique key makes that "
                    "safe)", prior_resume,
                )
        ledger_id = await conn.fetchval(
            _LEDGER_UPSERT,
            descriptor.identity.id,
            collection_version,
            descriptor.identity.version,
            descriptor.loader.kind,
            result.resume_key,
            result.pairs_total,
            result.pairs_done,
            result.rows_written,
            result.rows_skipped,
            json.dumps(
                {
                    "loader_version": COLLECTION_LOADER_VERSION,
                    "origin_class": descriptor.origin_class,
                    "licence_class": descriptor.licence_class,
                    "resumed": bool(start_at),
                },
                sort_keys=True,
            ),
        )

    await fetcher.prepare(plan)
    batch: list[ObservationRow] = []
    # THE RESUME KEY ONLY ADVANCES PAST A FLUSH. Rows are batched, so a pair
    # can be fetched and turned into rows that are still in memory when the
    # run dies; recording it as done would silently lose exactly those rows.
    # `pending_*` is what has been READ, `result.*` what is DURABLE — and
    # re-fetching a pair whose rows were already written is free, because the
    # identity key makes the second write a no-op. Resume skips work; the
    # unique key is what guarantees correctness.
    pending_key: str | None = result.resume_key
    pending_done = result.pairs_done
    try:
        for index in range(start_at, len(plan.pairs)):
            series, subject = plan.pairs[index]
            iso3 = descriptor.iso3_for(subject)
            fetched = await fetcher.fetch(series, subject, iso3)
            rows, skip_reason = rows_for_pair(
                descriptor, series, subject, fetched,
                collection_version=collection_version,
            )
            if skip_reason is not None:
                result.rows_skipped += 1
                result.skipped.append(skip_reason)
                logger.warning("SKIPPED %s", skip_reason)
            batch.extend(rows)
            pending_key = pair_key(series, subject)
            pending_done += 1
            if dry_run:
                result.rows_written += len(rows)
                result.pairs_done = pending_done
                result.resume_key = pending_key
                batch.clear()
            elif len(batch) >= descriptor.loader.batch:
                assert conn is not None
                result.rows_written += await _flush(conn, batch)
                batch.clear()
                result.resume_key = pending_key
                result.pairs_done = pending_done
                await conn.execute(
                    _LEDGER_PROGRESS, ledger_id, result.resume_key,
                    result.pairs_done, result.rows_written, result.rows_skipped,
                )
        if not dry_run:
            assert conn is not None
            if batch:
                result.rows_written += await _flush(conn, batch)
                batch.clear()
            result.resume_key = pending_key
            result.pairs_done = pending_done
        result.status = "completed"
    except BaseException as exc:                      # noqa: BLE001
        result.status = (
            "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        )
        result.error = f"{type(exc).__name__}: {exc}"
        if not dry_run and conn is not None:
            await _finish(conn, ledger_id, result)
        raise
    finally:
        await fetcher.close()

    if not dry_run and conn is not None:
        await _finish(conn, ledger_id, result)
    return result


async def _flush(conn: asyncpg.Connection, rows: Sequence[ObservationRow]) -> int:
    """Insert one batch; return how many rows the table actually took."""
    if not rows:
        return 0
    inserted = await conn.fetch(
        _INSERT_SQL,
        [r.collection_id for r in rows],
        [r.series_id for r in rows],
        [r.subject_kind for r in rows],
        [r.subject for r in rows],
        [r.valid_from for r in rows],
        [r.valid_to for r in rows],
        [r.record_time for r in rows],
        [r.value for r in rows],
        [r.unit for r in rows],
        [r.value_text for r in rows],
        [r.source_url for r in rows],
        [r.sha256 for r in rows],
        [r.origin_class for r in rows],
        [r.provenance_json() for r in rows],
    )
    return len(inserted)


async def _finish(
    conn: asyncpg.Connection, ledger_id: Any, result: LoadResult
) -> None:
    await conn.execute(
        _LEDGER_FINISH,
        ledger_id,
        result.status,
        result.error,
        result.resume_key,
        result.pairs_done,
        result.rows_written,
        result.rows_skipped,
        json.dumps(
            {
                "loader_version": COLLECTION_LOADER_VERSION,
                "skipped": result.skipped,
            },
            sort_keys=True,
        ),
    )


# ===========================================================================
# 7. Resolving the descriptor
# ===========================================================================


def resolve_descriptor_path(spec: str, descriptors_dir: Path) -> Path:
    """A path, or a descriptor id matched against ``collection_*.yaml``."""
    candidate = Path(spec)
    if candidate.exists():
        return candidate
    for path in sorted(descriptors_dir.glob("collection_*.yaml")):
        body = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if str((body.get("identity") or {}).get("id") or "") == spec:
            return path
    raise CollectionLoadError(
        f"{spec!r} is neither a readable path nor the `identity.id` of any "
        f"{descriptors_dir}/collection_*.yaml"
    )


def load_descriptor(path: Path) -> tuple[CollectionDescriptor, Any]:
    """Parse one manifest file BOTH ways, and refuse if they disagree.

    The pydantic schema is the gate — the fail-closed licence class, the
    firewall's eight surfaces, the history-only origin class. The verifier's
    ``parse_manifest`` is the fetch PLAN, and reusing it is what keeps one
    parser for the provider shapes. Both read the same file; a series present
    in one and not the other means the file is not the thing either of them
    thinks it is.
    """
    body = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        raise CollectionLoadError(f"{path}: not a YAML mapping")
    descriptor = CollectionDescriptor.model_validate(body, strict=False)
    manifest = vcm.parse_manifest(path)
    schema_ids = {s.series_id for s in descriptor.manifest.series}
    plan_ids = {s.series_id for s in manifest.series}
    if schema_ids != plan_ids:
        raise CollectionLoadError(
            f"{path}: the schema and the verifier disagree about which series "
            f"this manifest holds (schema-only {sorted(schema_ids - plan_ids)}, "
            f"plan-only {sorted(plan_ids - schema_ids)})"
        )
    return descriptor, manifest


def render_summary(result: LoadResult, *, dry_run: bool) -> str:
    verb = "WOULD write" if dry_run else "wrote"
    lines = [
        f"collection {result.collection_id} version {result.collection_version}",
        f"  {result.pairs_done}/{result.pairs_total} (series, subject) pairs; "
        f"{verb} {result.rows_written} observation rows; "
        f"{result.rows_skipped} pairs skipped",
    ]
    for reason in result.skipped:
        lines.append(f"  SKIPPED {reason}")
    if result.resume_key:
        lines.append(f"  resume_key {result.resume_key}")
    lines.append(f"  status {result.status}")
    return "\n".join(lines)


# ===========================================================================
# 8. Entry point
# ===========================================================================


async def _amain(args: argparse.Namespace) -> int:
    path = resolve_descriptor_path(args.collection, args.descriptors_dir)
    descriptor, manifest = load_descriptor(path)
    plan = build_plan(descriptor, manifest)
    print(
        f"collection {descriptor.identity.id} "
        f"(state={descriptor.identity.state.value}, "
        f"origin_class={descriptor.origin_class}, "
        f"licence_class={descriptor.licence_class}, "
        f"loader={descriptor.loader.kind})",
        file=sys.stderr,
    )
    print(
        f"  {len(descriptor.manifest.series)} series x "
        f"{len(descriptor.subjects)} subjects = {len(plan.pairs)} pairs, "
        f"window {descriptor.window.valid_from}..{descriptor.window.valid_to}",
        file=sys.stderr,
    )
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    fetcher = HttpPairFetcher(
        args.cache_dir, dict(descriptor.loader.rate_limit_per_second)
    )
    conn = None
    if not args.dry_run:
        dsn = args.dsn or PostgresConfig.from_env().dsn
        conn = await asyncpg.connect(dsn)
    try:
        result = await run_load(
            plan, fetcher, conn, resume=args.resume, dry_run=args.dry_run
        )
    finally:
        if conn is not None:
            await conn.close()
    print(render_summary(result, dry_run=args.dry_run))
    return 0 if result.status == "completed" else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Load a collection's series into `observations`.",
    )
    parser.add_argument(
        "collection",
        help="path to a descriptors/collection_*.yaml, or its identity.id",
    )
    parser.add_argument("--resume", action="store_true",
                        help="continue after the ledger's resume_key")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and report; touch the database not at all")
    parser.add_argument("--dsn", default=None,
                        help="Postgres DSN (default: PostgresConfig.from_env())")
    parser.add_argument("--descriptors-dir", type=Path,
                        default=REPO_ROOT / "descriptors")
    parser.add_argument(
        "--cache-dir", type=Path,
        default=Path("/var/tmp") / "legba-collection-cache",
        help="where provider bulk archives are cached between runs",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    return asyncio.run(_amain(args))


if __name__ == "__main__":       # pragma: no cover - CLI entry point
    raise SystemExit(main())
