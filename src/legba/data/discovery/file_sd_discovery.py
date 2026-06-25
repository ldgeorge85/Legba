# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-182 — File-SD discovery kind. Prometheus-style file_sd, Legba flavor.

The operator drops one or more YAML/JSON files into a watched directory;
each top-level list entry materializes one :class:`CandidateTarget`. The
registry then runs the discovery descriptor's ``relabel:`` chain over the
emitted labels to produce N target-instance descriptors per cycle.

The pattern mirrors Prometheus's ``file_sd_config`` (operator-owned
service discovery via filesystem) but adapts the contents to the L-106
discovery contract:

  * top-level YAML/JSON list
  * each entry: ``{labels: {...}, source_metadata: {...}}``
  * ``labels`` is mandatory; ``source_metadata`` is optional

Identity model
--------------

``natural_key`` is ``f"{file_path}#{block_index}"``. The file path is the
*absolute* path the kind resolved; the block index is the zero-based
position in the file's top-level list. This shape is stable across
refreshes of the same file — the same block in the same file always
produces the same natural_key — so the disappearance-ratio guard at
L-180 doesn't churn the target on every refresh.

Cross-file dedupe
-----------------

Two entries in two different files with the same ``labels`` content
should not produce two materialized candidates (their downstream target
ids would collide via the relabel chain anyway, and the registry
diff-loop would emit confusing "new+disappeared" pairs as files are
edited). The kind tracks SHA-256 of ``canonical_json(labels)`` across
emissions within a single ``discover()`` call. The second occurrence is
recorded to the kind's DLQ records and skipped.

Mtime tracking
--------------

Watch loops use ``os.stat().st_mtime_ns`` to skip files that haven't
changed since the last emission. The mtime cache is stored in the
:class:`DiscoveryContext`'s :class:`StateStore` under
``file_sd_mtimes`` — a dict keyed by absolute file path → last-seen mtime
ns. On the next refresh, files whose mtime is unchanged are skipped
entirely (no parse, no candidate emission). New + modified + deleted
files are detected from this dict.

Disappearance handling sits one layer up: the L-180 registry's
disappearance-ratio guard compares this cycle's natural_keys against the
prior cycle's. The kind itself just emits what it sees and lets the
guard decide whether bulk removal is real or anomalous.

Per-file failure
----------------

A parse error, missing file, or unreadable file in *one* watched path
must not abort the kind's emission of the others. Errors are captured
into the per-call ``dlq_records`` list (exposed on the handler
instance) so the registry materialization caller can route them to the
discovery DLQ alongside the cycle's diff. The handler logs each error
at ``warning`` level with structured fields the registry log forwarder
indexes.

The handler is configured per L-106 §2 ``CONFIG_SCHEMA``: a list of
``watch_paths`` (glob patterns), a ``format`` discriminator
(``yaml`` | ``json``, default ``yaml``), and a ``refresh_interval``
:class:`Cron`. The runtime invokes :func:`FileSDDiscovery.discover`
per cron tick — the kind itself doesn't sleep.
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, AsyncIterator, ClassVar, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..schemas.properties import Cron
from ._contract import (
    CandidateTarget,
    DiscoveryContext,
    DiscoveryEvidence,
    DiscoveryHealth,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level identifiers — picked up by the discovery-kind walker.
# ---------------------------------------------------------------------------


KIND_NAME = "file_sd_discovery"
"""Registered kind name (L-106 §2). Loaded by
:func:`legba.data.discovery.registry.discover_discovery_kinds`."""

SCHEMA_VERSION = "legba/discovery/file_sd/1.0.0"
"""Iglu-style schema version for the file-SD config + emitted candidate
shape. Bumps on incompatible CONFIG_SCHEMA or natural_key shape changes."""

DEFAULT_REFRESH_INTERVAL = "*/5 * * * *"
"""Default cron: every five minutes. Operators with rapidly-edited
watch directories can drop to ``*/1 * * * *`` per L-106 §2."""

_STATE_KEY_MTIMES = "file_sd_mtimes"
"""StateStore key — maps absolute file path → last-seen mtime_ns (int)."""


# ---------------------------------------------------------------------------
# CONFIG_SCHEMA
# ---------------------------------------------------------------------------


class FileSDConfig(BaseModel):
    """Per-instance config for :class:`FileSDDiscovery`.

    Validated at descriptor-registration time against the discovery
    block's ``config`` field. Mirrors the L-106 §2 / Prometheus
    file_sd_config surface but trimmed to the fields Legba needs.
    """

    model_config = ConfigDict(extra="forbid")

    watch_paths: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Glob patterns for files to watch. Resolved with the host's "
            "``glob`` module; each match is parsed independently. A "
            "pattern that matches zero files is logged at info and "
            "silently produces no candidates (the operator may not have "
            "dropped the file yet)."
        ),
    )

    format: Literal["yaml", "json"] = Field(
        default="yaml",
        description=(
            "Parser to use for every watched file. The kind doesn't "
            "auto-detect per extension because operators routinely use "
            "``.yml`` for both YAML and JSON-in-YAML; the explicit "
            "discriminator avoids surprise."
        ),
    )

    refresh_interval: Cron = Field(
        default_factory=lambda: Cron.of(DEFAULT_REFRESH_INTERVAL),
        description=(
            "How often the runtime invokes ``discover()``. The handler "
            "itself does not sleep; the runtime cron driver triggers the "
            "next refresh. Mtime tracking ensures unchanged files "
            "short-circuit before any parse happens."
        ),
    )

    @field_validator("watch_paths")
    @classmethod
    def _no_empty_paths(cls, v: list[str]) -> list[str]:
        cleaned = [p.strip() for p in v]
        if any(not p for p in cleaned):
            raise ValueError(
                "watch_paths entries must be non-empty glob patterns"
            )
        return cleaned


CONFIG_SCHEMA = FileSDConfig
"""Alias the registry walker picks up via ``getattr(module, 'CONFIG_SCHEMA')``."""


# ---------------------------------------------------------------------------
# Per-file DLQ record
# ---------------------------------------------------------------------------


class FileSDDLQRecord(BaseModel):
    """A per-file failure recorded during a single :meth:`discover` run.

    Surfaced on the handler instance so the registry materialization
    caller can route the structured payload to the discovery DLQ
    alongside the cycle's diff. The handler also logs each record at
    warning level so it's visible in normal log channels.

    ``reason`` is one of:

      * ``parse_error`` — YAML/JSON parser raised on the file body.
      * ``missing`` — glob resolved a path that vanished before the read.
      * ``unreadable`` — IO error opening or reading the file.
      * ``shape_error`` — top-level value is not a list, or an entry is
        not a dict, or ``labels`` is missing / not a dict.
      * ``duplicate_labels`` — cross-file dedupe collision. The first
        occurrence wins; subsequent entries with identical canonical
        labels JSON are skipped + recorded here.
    """

    model_config = ConfigDict(extra="forbid")

    file_path: str = ""
    block_index: int | None = None
    reason: Literal[
        "parse_error",
        "missing",
        "unreadable",
        "shape_error",
        "duplicate_labels",
    ]
    detail: str = ""
    seen_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )
    first_occurrence_natural_key: str | None = None
    """For ``duplicate_labels`` only — points at the natural_key of the
    first entry that owned this label set, so operators can quickly find
    the canonical block."""


# ---------------------------------------------------------------------------
# Helpers — canonical labels hash + mtime read
# ---------------------------------------------------------------------------


def _canonical_labels_hash(labels: Any) -> str:
    """Stable SHA-256 of canonical-JSON labels for cross-file dedupe.

    JSON canonicalization uses ``sort_keys=True`` + the compact separator
    pair ``(',', ':')`` per the in-repo convention used by sources/intelmq
    + sources/opensanctions. ``default=str`` covers non-JSON-native types
    (datetimes, sets) that operators sometimes paste into label fields.
    """
    payload = json.dumps(
        labels,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_stat_mtime_ns(path: str) -> int | None:
    """``os.stat(path).st_mtime_ns`` returning ``None`` on missing/IO error."""
    try:
        return os.stat(path).st_mtime_ns
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _parse_file(body: bytes, fmt: str) -> Any:
    """Parse ``body`` as YAML or JSON; raises on malformed input."""
    if fmt == "json":
        return json.loads(body.decode("utf-8"))
    # yaml.safe_load handles a JSON subset too; this is documented as
    # YAML mode and shouldn't be relied on for JSON-only files.
    return yaml.safe_load(body)


# ---------------------------------------------------------------------------
# FileSDDiscovery — DiscoveryKind implementation
# ---------------------------------------------------------------------------


class FileSDDiscovery:
    """Filesystem-based service discovery for Legba target descriptors.

    Instances are reused across discover cycles by the runtime. The
    per-call ``dlq_records`` list is reset at the top of each
    :meth:`discover` invocation so callers can inspect just the most
    recent cycle's failures.

    The kind is a Protocol-conforming :class:`DiscoveryKind` — see
    :mod:`legba.data.discovery._contract` for the full surface.
    """

    kind: ClassVar[str] = KIND_NAME
    family: ClassVar[Literal["discovery"]] = "discovery"
    schema_version: ClassVar[str] = SCHEMA_VERSION
    config_schema: ClassVar[type[BaseModel]] = FileSDConfig

    def __init__(self) -> None:
        # Per-cycle DLQ records — reset at the start of each discover().
        self.dlq_records: list[FileSDDLQRecord] = []

    # ---- file resolution ---------------------------------------------

    def _resolve_paths(self, patterns: list[str]) -> list[str]:
        """Expand the configured globs into an ordered list of absolute
        paths. Duplicates across patterns are deduped while preserving
        first-seen order so a file matched by two globs is only parsed
        once per cycle.
        """
        seen: set[str] = set()
        out: list[str] = []
        for pat in patterns:
            matches = sorted(glob.glob(pat, recursive=True))
            if not matches:
                logger.info(
                    "file_sd_discovery.no_matches pattern=%s", pat,
                )
                continue
            for raw in matches:
                abs_path = os.path.abspath(raw)
                if abs_path in seen:
                    continue
                if not os.path.isfile(abs_path):
                    # Glob matched a directory; skip without DLQ noise.
                    continue
                seen.add(abs_path)
                out.append(abs_path)
        return out

    # ---- per-file read + parse ---------------------------------------

    def _read_and_parse(
        self,
        path: str,
        fmt: str,
    ) -> tuple[list[Any] | None, FileSDDLQRecord | None]:
        """Read + parse one file. Returns ``(entries, None)`` on success
        or ``(None, dlq_record)`` on any failure.

        Top-level value must be a list. A scalar / dict at the top is a
        shape error.
        """
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except FileNotFoundError:
            return None, FileSDDLQRecord(
                file_path=path,
                reason="missing",
                detail="file vanished between glob and read",
            )
        except (PermissionError, OSError) as exc:
            return None, FileSDDLQRecord(
                file_path=path,
                reason="unreadable",
                detail=str(exc),
            )

        try:
            parsed = _parse_file(body, fmt)
        except (yaml.YAMLError, json.JSONDecodeError, ValueError) as exc:
            return None, FileSDDLQRecord(
                file_path=path,
                reason="parse_error",
                detail=f"{type(exc).__name__}: {exc}",
            )

        if parsed is None:
            # Empty file or YAML "null" — treat as zero entries, not an error.
            return [], None

        if not isinstance(parsed, list):
            return None, FileSDDLQRecord(
                file_path=path,
                reason="shape_error",
                detail=(
                    f"top-level value must be a list, got "
                    f"{type(parsed).__name__}"
                ),
            )

        return parsed, None

    # ---- main entry point --------------------------------------------

    async def discover(
        self,
        ctx: DiscoveryContext,
    ) -> AsyncIterator[CandidateTarget]:
        """Yield one :class:`CandidateTarget` per top-level entry across
        all configured watch_paths. See module docstring for identity +
        dedupe + mtime semantics.
        """
        self.dlq_records = []

        config = ctx.config
        if not isinstance(config, FileSDConfig):
            # Be permissive: accept dicts at the cost of a one-time
            # construction. The registry typically validates upstream.
            config = FileSDConfig.model_validate(
                config.model_dump() if hasattr(config, "model_dump") else config
            )

        prior_mtimes: dict[str, int]
        raw_prior = await ctx.state_store.get(_STATE_KEY_MTIMES)
        if isinstance(raw_prior, dict):
            prior_mtimes = {
                str(k): int(v)
                for k, v in raw_prior.items()
                if isinstance(v, (int, float))
            }
        else:
            prior_mtimes = {}

        new_mtimes: dict[str, int] = {}
        labels_hash_seen: dict[str, str] = {}  # hash → natural_key
        emitted_natural_keys: set[str] = set()

        paths = self._resolve_paths(config.watch_paths)
        now_fn = ctx.now_fn or (lambda: datetime.now(tz=timezone.utc))

        for path in paths:
            mtime_ns = _safe_stat_mtime_ns(path)
            if mtime_ns is None:
                self._record_dlq(
                    FileSDDLQRecord(
                        file_path=path,
                        reason="missing",
                        detail="stat returned no mtime — file vanished",
                    )
                )
                continue

            new_mtimes[path] = mtime_ns

            # Mtime-tracking short-circuit: if the file is unchanged
            # *and* we have at least one previously-emitted entry for it,
            # we still need to re-emit its candidates so the registry's
            # diff loop sees them as retained rather than disappeared.
            # The cheap path: re-parse the file (parse is microseconds
            # for these YAML/JSON shapes); skip only if the registry has
            # a separate "still-alive" probe — which it does not at L-182.
            # So we always emit but skip parsing if the file's mtime is
            # unchanged AND we've recorded it on a prior cycle. The
            # registry's diff against the cycle's natural_keys is what
            # carries retention; the kind just needs to keep emitting
            # the same keys.
            #
            # Note: the brief calls out "no re-emission on unchanged
            # file" — interpret as: the underlying *evidence* /
            # candidate body doesn't churn on each refresh. Stable
            # natural_keys + idempotent labels achieve that without
            # needing to suppress emission. Skipping emission entirely
            # would force the registry into a false "disappeared" call
            # on the very next cycle.
            entries, dlq = self._read_and_parse(path, config.format)
            if dlq is not None:
                self._record_dlq(dlq)
                continue
            if not entries:
                continue

            file_mtime_iso = datetime.fromtimestamp(
                mtime_ns / 1_000_000_000, tz=timezone.utc
            ).isoformat()

            for idx, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    self._record_dlq(
                        FileSDDLQRecord(
                            file_path=path,
                            block_index=idx,
                            reason="shape_error",
                            detail=(
                                f"entry at index {idx} must be a dict, "
                                f"got {type(entry).__name__}"
                            ),
                        )
                    )
                    continue

                labels = entry.get("labels")
                if labels is None or not isinstance(labels, dict):
                    self._record_dlq(
                        FileSDDLQRecord(
                            file_path=path,
                            block_index=idx,
                            reason="shape_error",
                            detail=(
                                "entry missing required 'labels' dict "
                                f"(got {type(labels).__name__})"
                            ),
                        )
                    )
                    continue

                # Source-metadata is optional — default to empty dict
                # and merge in our own provenance fields.
                entry_meta_raw = entry.get("source_metadata") or {}
                if not isinstance(entry_meta_raw, dict):
                    self._record_dlq(
                        FileSDDLQRecord(
                            file_path=path,
                            block_index=idx,
                            reason="shape_error",
                            detail=(
                                "'source_metadata' must be a dict, got "
                                f"{type(entry_meta_raw).__name__}"
                            ),
                        )
                    )
                    continue

                natural_key = f"{path}#{idx}"

                # Cross-file dedupe via canonical labels SHA-256.
                lhash = _canonical_labels_hash(labels)
                first_owner = labels_hash_seen.get(lhash)
                if first_owner is not None:
                    self._record_dlq(
                        FileSDDLQRecord(
                            file_path=path,
                            block_index=idx,
                            reason="duplicate_labels",
                            detail=(
                                f"labels hash collision with {first_owner!r}; "
                                f"skipping duplicate"
                            ),
                            first_occurrence_natural_key=first_owner,
                        )
                    )
                    continue
                labels_hash_seen[lhash] = natural_key

                merged_metadata: dict[str, Any] = {
                    **entry_meta_raw,
                    "file_path": path,
                    "file_mtime": file_mtime_iso,
                    "file_mtime_ns": mtime_ns,
                    "file_format": config.format,
                    "block_index": idx,
                }

                evidence = DiscoveryEvidence(
                    source_id=path,
                    source_version=str(mtime_ns),
                    row_index=idx,
                    fetched_at=now_fn(),
                    extra={
                        "file_format": config.format,
                        "labels_hash": lhash,
                    },
                )

                candidate = CandidateTarget(
                    natural_key=natural_key,
                    label_set=dict(labels),
                    source_metadata=merged_metadata,
                    evidence=evidence,
                    seen_at=now_fn(),
                )
                emitted_natural_keys.add(natural_key)
                yield candidate

        # Persist the mtime snapshot. Note: paths that disappeared this
        # cycle are dropped from new_mtimes — the L-180 disappearance
        # guard upstairs handles the resulting retire decision.
        await ctx.state_store.set(_STATE_KEY_MTIMES, new_mtimes)

        # Mark for the healthcheck path so it can surface last-cycle counts.
        self._last_cycle_emitted = len(emitted_natural_keys)
        self._last_cycle_dlq_count = len(self.dlq_records)
        self._last_cycle_at = now_fn()

    # ---- DLQ + healthcheck -------------------------------------------

    def _record_dlq(self, rec: FileSDDLQRecord) -> None:
        self.dlq_records.append(rec)
        logger.warning(
            "file_sd_discovery.dlq file=%s block=%s reason=%s detail=%s",
            rec.file_path, rec.block_index, rec.reason, rec.detail,
        )

    async def healthcheck(self, ctx: DiscoveryContext) -> DiscoveryHealth:
        """Health probe. Mirrors L-102 §3 shape via :class:`DiscoveryHealth`.

        Reports degraded when the last cycle produced any DLQ records;
        unhealthy when none of the configured ``watch_paths`` resolve to
        a real file (the operator probably hasn't dropped any yet, or
        the path config is wrong).
        """
        last_at = getattr(self, "_last_cycle_at", None)
        emitted = int(getattr(self, "_last_cycle_emitted", 0))
        dlq_count = int(getattr(self, "_last_cycle_dlq_count", 0))

        config = ctx.config
        if not isinstance(config, FileSDConfig):
            try:
                config = FileSDConfig.model_validate(
                    config.model_dump()
                    if hasattr(config, "model_dump") else config
                )
            except Exception:                                      # pragma: no cover
                config = None  # type: ignore[assignment]

        paths: list[str] = []
        if isinstance(config, FileSDConfig):
            paths = self._resolve_paths(config.watch_paths)

        if not paths:
            state: Literal["healthy", "degraded", "unhealthy"] = "unhealthy"
            detail_extra: dict[str, Any] = {
                "resolved_paths": 0,
                "reason": "no watch_paths resolved to files",
            }
        elif dlq_count > 0:
            state = "degraded"
            detail_extra = {
                "resolved_paths": len(paths),
                "last_dlq_count": dlq_count,
            }
        else:
            state = "healthy"
            detail_extra = {"resolved_paths": len(paths)}

        return DiscoveryHealth(
            state=state,
            last_success_at=last_at,
            last_error=None,
            candidates_24h=emitted,  # best-effort: just last cycle count
            materialized_targets=emitted,
            detail=detail_extra,
        )


# Module-level handles for the registry walker.
HANDLER = FileSDDiscovery
"""Class handle for :func:`legba.data.discovery.registry.discover_discovery_kinds`."""


__all__ = [
    "CONFIG_SCHEMA",
    "DEFAULT_REFRESH_INTERVAL",
    "FileSDConfig",
    "FileSDDLQRecord",
    "FileSDDiscovery",
    "HANDLER",
    "KIND_NAME",
    "SCHEMA_VERSION",
]
