# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""exemplar_shelf — the curated pattern-thread shelf, and its id resolver.

A pattern-thread is a **warning problem** (UK JDP 2-00): a named standing
monitor with a watch condition that emits on critical-indicator change rather
than on cadence. The SHELF is the curated index of which warning problems
exist — the input to thread-authoring, and later to the corpus batch load and
the Phase-3 warning-problem monitors (`legba.data.situations`: "a situation
whose attachment rule is a pattern-thread match").

House seed pattern: the MACHINERY ships in-repo (this module); the curated
DATA does not. The default input path is ``seeds/exemplar_shelf.yaml``, which
is gitignored per the existing ``seeds/*.yaml`` rule; the tracked schema doc is
``seeds/exemplar_shelf.example.yaml``. A missing data file degrades gracefully
— a warning and an empty shelf, never a crash (the ``world_baseline`` adapter
precedent). A file that is PRESENT but malformed fails loud
(``ExemplarShelfError``): a half-read shelf would silently stop offering
patterns, which is the failure this loader exists to make impossible.

**Why the resolver exists.** Curation removes patterns from the shelf in two
ways, and neither may leave a dangling id:

  * **merged** — the id moves into the survivor's ``aliases`` and is recorded
    in its ``merged_from``, normally with a ``variants`` entry carrying the
    distinctive framing and indicators it contributed. The old id RESOLVES to
    the survivor.
  * **retired** — the id moves to the top-level ``retired:`` list with the
    deciding document's reason quoted. The old id RESOLVES to a retired
    record.

In both cases the id is **resolvable but never offered**. ``offered_ids()`` is
the active shelf and nothing else; ``resolve()`` answers for every id the shelf
has ever carried. Slots are curation numbers and are never renumbered — a cull
leaves holes, deliberately, so an old reference to "#22" cannot silently become
a different pattern.

Nothing here authors, scores, or loads a thread: the shelf is an index, and
``stage: culled`` says the bodies are not written yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Repo-root-relative default (…/legba/seeds/exemplar_shelf.yaml). This file is
# …/src/legba/data/seed/exemplar_shelf.py → 4 parents up = repo root.
DEFAULT_YAML = Path(__file__).resolve().parents[4] / "seeds" / "exemplar_shelf.yaml"

SUPPORTED_VERSIONS = frozenset({1})

#: A pattern that can be authored now.
STATUS_READY = "ready"
#: A pattern that stays on the shelf but is gated for authoring — it MUST say
#: what unblocks it (``authoring_gate``), so a hold cannot become a quiet drop.
STATUS_HOLD = "hold"
VALID_STATUSES = frozenset({STATUS_READY, STATUS_HOLD})

#: ``Resolution.status`` values.
RESOLVED_ACTIVE = "active"
RESOLVED_MERGED = "merged"
RESOLVED_RETIRED = "retired"


class ExemplarShelfError(ValueError):
    """A curated shelf file that is present but cannot be trusted."""


@dataclass(frozen=True)
class ExemplarPattern:
    """One ACTIVE pattern-thread on the shelf (an index row, not a thread)."""

    id: str
    slot: str
    title: str
    doctrine_source: str
    tier: str | None = None
    fuse: str | None = None
    osint_legibility: str | None = None
    status: str = STATUS_READY
    authoring_gate: str | None = None
    note: str | None = None
    role: str | None = None
    research_ref: str | None = None
    aliases: tuple[str, ...] = ()
    merged_from: tuple[dict[str, Any], ...] = ()
    variants: tuple[dict[str, Any], ...] = ()
    must_stay_split_from: tuple[str, ...] = ()

    @property
    def is_ready(self) -> bool:
        return self.status == STATUS_READY

    @property
    def is_held(self) -> bool:
        return self.status == STATUS_HOLD

    def variant(self, variant_id: str) -> dict[str, Any] | None:
        """The named variant folded in by a merge, or None."""
        for variant in self.variants:
            if variant.get("id") == variant_id:
                return variant
        return None

    def variant_for(self, merged_id: str) -> dict[str, Any] | None:
        """The variant carrying what ``merged_id`` contributed, or None."""
        for row in self.merged_from:
            if row.get("id") == merged_id:
                named = row.get("variant")
                return self.variant(named) if named else None
        return None


@dataclass(frozen=True)
class RetiredPattern:
    """One pattern cut from the active shelf, kept resolvable."""

    id: str
    slot: str
    title: str
    disposition: str
    reason: str
    tier: str | None = None
    retired_on: str | None = None
    successor_note: str | None = None
    reversal: str | None = None
    research_ref: str | None = None


@dataclass(frozen=True)
class Resolution:
    """What an id — current, merged-away or retired — points at today."""

    query_id: str
    status: str
    offered: bool
    pattern: ExemplarPattern | None = None
    retired: RetiredPattern | None = None
    variant: dict[str, Any] | None = None
    reason: str | None = None

    @property
    def pattern_id(self) -> str | None:
        """The surviving pattern this id resolves to, if any."""
        return self.pattern.id if self.pattern is not None else None


@dataclass
class ExemplarShelf:
    """A loaded shelf: the active patterns, the retired ones, the resolver."""

    version: int = 1
    shelf_id: str | None = None
    stage: str | None = None
    culled_on: str | None = None
    decision: str | None = None
    sources: dict[str, Any] = field(default_factory=dict)
    cull: dict[str, Any] = field(default_factory=dict)
    patterns: tuple[ExemplarPattern, ...] = ()
    retired: tuple[RetiredPattern, ...] = ()
    pending_acquisitions: tuple[dict[str, Any], ...] = ()
    source_path: Path | None = None

    def __post_init__(self) -> None:
        self._by_id: dict[str, ExemplarPattern] = {p.id: p for p in self.patterns}
        self._retired_by_id: dict[str, RetiredPattern] = {r.id: r for r in self.retired}
        self._alias_to_pattern: dict[str, str] = {}
        for pattern in self.patterns:
            for alias in pattern.aliases:
                self._alias_to_pattern[alias] = pattern.id

    # -- the active shelf ------------------------------------------------

    def offered_ids(self) -> tuple[str, ...]:
        """Every id the shelf OFFERS — the active patterns and nothing else.

        Merged-away and retired ids are resolvable but never offered.
        """
        return tuple(p.id for p in self.patterns)

    def ready_ids(self) -> tuple[str, ...]:
        """Offered patterns that can be authored now (``status: ready``)."""
        return tuple(p.id for p in self.patterns if p.is_ready)

    def held_ids(self) -> tuple[str, ...]:
        """Offered patterns gated for authoring (``status: hold``)."""
        return tuple(p.id for p in self.patterns if p.is_held)

    def retired_ids(self) -> tuple[str, ...]:
        return tuple(r.id for r in self.retired)

    def kept_slots(self) -> tuple[str, ...]:
        """The distinct NUMBERED slots still on the shelf.

        A slot may carry more than one pattern (``19a``/``19b`` are two
        sub-patterns of slot 19 that curation forbids merging with each other),
        so this is the "how many slots survived the cull" count, which is
        smaller than ``offered_ids()`` whenever a slot is split.
        """
        return tuple(dict.fromkeys(_slot_number(p.slot) for p in self.patterns))

    def pattern(self, pattern_id: str) -> ExemplarPattern | None:
        """An active pattern by its CANONICAL id (aliases: use resolve)."""
        return self._by_id.get(pattern_id)

    def is_offered(self, any_id: str) -> bool:
        return any_id in self._by_id

    # -- the resolver ----------------------------------------------------

    def resolve(self, any_id: str) -> Resolution | None:
        """Resolve any id the shelf has ever carried, or None if unknown.

        ``active`` → the pattern itself (offered).
        ``merged`` → the survivor it was folded into, plus the variant that
        carries what it contributed (not offered).
        ``retired`` → the retired record with the cull's own reason (not
        offered).
        """
        active = self._by_id.get(any_id)
        if active is not None:
            return Resolution(
                query_id=any_id,
                status=RESOLVED_ACTIVE,
                offered=True,
                pattern=active,
            )

        survivor_id = self._alias_to_pattern.get(any_id)
        if survivor_id is not None:
            survivor = self._by_id[survivor_id]
            merged_row = next(
                (r for r in survivor.merged_from if r.get("id") == any_id), None
            )
            return Resolution(
                query_id=any_id,
                status=RESOLVED_MERGED,
                offered=False,
                pattern=survivor,
                variant=survivor.variant_for(any_id),
                reason=(merged_row or {}).get("merge_reason"),
            )

        gone = self._retired_by_id.get(any_id)
        if gone is not None:
            return Resolution(
                query_id=any_id,
                status=RESOLVED_RETIRED,
                offered=False,
                retired=gone,
                reason=gone.reason,
            )

        return None


def _slot_number(slot: str) -> str:
    """``"19a"`` -> ``"19"``. A split slot is still ONE numbered slot."""
    return slot.rstrip("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ") or slot


def _as_tuple(raw: Any, field_name: str, pattern_id: str) -> tuple[Any, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ExemplarShelfError(
            f"pattern {pattern_id!r}: {field_name} must be a list, got {type(raw).__name__}"
        )
    return tuple(raw)


def _require(row: dict[str, Any], key: str, where: str) -> str:
    value = row.get(key)
    if value is None or not str(value).strip():
        raise ExemplarShelfError(f"{where}: missing required field {key!r}")
    return str(value).strip()


def _parse_pattern(row: Any, index: int) -> ExemplarPattern:
    if not isinstance(row, dict):
        raise ExemplarShelfError(f"patterns[{index}] must be a mapping")
    where = f"patterns[{index}]"
    pattern_id = _require(row, "id", where)
    where = f"pattern {pattern_id!r}"

    status = str(row.get("status") or STATUS_READY).strip()
    if status not in VALID_STATUSES:
        raise ExemplarShelfError(
            f"{where}: status {status!r} not one of {sorted(VALID_STATUSES)}"
        )
    gate = row.get("authoring_gate")
    if status == STATUS_HOLD and not (gate and str(gate).strip()):
        raise ExemplarShelfError(
            f"{where}: status 'hold' requires authoring_gate (what unblocks it)"
        )

    aliases = tuple(str(a) for a in _as_tuple(row.get("aliases"), "aliases", pattern_id))
    merged_from = tuple(
        r for r in _as_tuple(row.get("merged_from"), "merged_from", pattern_id)
    )
    for merged in merged_from:
        if not isinstance(merged, dict):
            raise ExemplarShelfError(f"{where}: merged_from rows must be mappings")
        merged_id = _require(merged, "id", f"{where} merged_from")
        if merged_id not in aliases:
            raise ExemplarShelfError(
                f"{where}: merged_from id {merged_id!r} is not in aliases — "
                "a merged id must stay resolvable"
            )

    return ExemplarPattern(
        id=pattern_id,
        slot=_require(row, "slot", where),
        title=_require(row, "title", where),
        doctrine_source=_require(row, "doctrine_source", where),
        tier=row.get("tier"),
        fuse=row.get("fuse"),
        osint_legibility=row.get("osint_legibility"),
        status=status,
        authoring_gate=str(gate).strip() if gate else None,
        note=row.get("note"),
        role=row.get("role"),
        research_ref=row.get("research_ref"),
        aliases=aliases,
        merged_from=merged_from,
        variants=tuple(_as_tuple(row.get("variants"), "variants", pattern_id)),
        must_stay_split_from=tuple(
            str(s)
            for s in _as_tuple(
                row.get("must_stay_split_from"), "must_stay_split_from", pattern_id
            )
        ),
    )


def _parse_retired(row: Any, index: int) -> RetiredPattern:
    if not isinstance(row, dict):
        raise ExemplarShelfError(f"retired[{index}] must be a mapping")
    where = f"retired[{index}]"
    retired_id = _require(row, "id", where)
    where = f"retired {retired_id!r}"
    return RetiredPattern(
        id=retired_id,
        slot=_require(row, "slot", where),
        title=_require(row, "title", where),
        disposition=_require(row, "disposition", where),
        reason=_require(row, "reason", where),
        tier=row.get("tier"),
        retired_on=row.get("retired_on"),
        successor_note=row.get("successor_note"),
        reversal=row.get("reversal"),
        research_ref=row.get("research_ref"),
    )


def _check_shelf_integrity(
    patterns: tuple[ExemplarPattern, ...],
    retired: tuple[RetiredPattern, ...],
) -> None:
    """Every cross-row rule that keeps ids stable and non-dangling."""
    seen_ids: set[str] = set()
    seen_slots: set[str] = set()
    for pattern in patterns:
        if pattern.id in seen_ids:
            raise ExemplarShelfError(f"duplicate pattern id {pattern.id!r}")
        seen_ids.add(pattern.id)
        if pattern.slot in seen_slots:
            raise ExemplarShelfError(
                f"duplicate slot {pattern.slot!r} (slots are stable, never reused)"
            )
        seen_slots.add(pattern.slot)

    alias_owner: dict[str, str] = {}
    for pattern in patterns:
        for alias in pattern.aliases:
            if alias in seen_ids:
                raise ExemplarShelfError(
                    f"alias {alias!r} on {pattern.id!r} collides with an active pattern id"
                )
            if alias in alias_owner:
                raise ExemplarShelfError(
                    f"alias {alias!r} claimed by both {alias_owner[alias]!r} "
                    f"and {pattern.id!r}"
                )
            alias_owner[alias] = pattern.id

    retired_slots = set(seen_slots)
    retired_ids: set[str] = set()
    for gone in retired:
        if gone.id in seen_ids:
            raise ExemplarShelfError(f"retired id {gone.id!r} is also an active pattern")
        if gone.id in alias_owner:
            raise ExemplarShelfError(
                f"retired id {gone.id!r} is also an alias of {alias_owner[gone.id]!r} — "
                "an id is merged or retired, never both"
            )
        if gone.id in retired_ids:
            raise ExemplarShelfError(f"duplicate retired id {gone.id!r}")
        retired_ids.add(gone.id)
        if gone.slot in retired_slots:
            raise ExemplarShelfError(
                f"retired slot {gone.slot!r} is reused (slots are never reused)"
            )
        retired_slots.add(gone.slot)

    for pattern in patterns:
        for other in pattern.must_stay_split_from:
            if other not in seen_ids:
                raise ExemplarShelfError(
                    f"pattern {pattern.id!r}: must_stay_split_from names {other!r}, "
                    "which is not an active pattern"
                )
            if other == pattern.id:
                raise ExemplarShelfError(
                    f"pattern {pattern.id!r}: must_stay_split_from names itself"
                )


def _check_declared_cull_counts(shelf: ExemplarShelf) -> None:
    """A ``cull:`` block must match the shelf it describes.

    The counts are easy to misread (20 kept SLOTS vs 21 offered PATTERNS,
    because one slot is split), so where the curated file states them they are
    checked against the rows rather than trusted.
    """
    if not shelf.cull:
        return
    ready_slots = {_slot_number(p.slot) for p in shelf.patterns if p.is_ready}
    computed = {
        "kept_slots": len(shelf.kept_slots()),
        "ready_slots": len(ready_slots),
        "offered_patterns": len(shelf.offered_ids()),
        "ready_patterns": len(shelf.ready_ids()),
        "held_patterns": len(shelf.held_ids()),
        "killed_slots": len(shelf.retired_ids()),
        "merged_slots": sum(len(p.merged_from) for p in shelf.patterns),
    }
    for key, actual in computed.items():
        declared = shelf.cull.get(key)
        if declared is not None and int(declared) != actual:
            raise ExemplarShelfError(
                f"cull.{key} says {declared} but the shelf has {actual}"
            )


def parse_shelf(raw: Any, source_path: Path | None = None) -> ExemplarShelf:
    """Validate an already-decoded shelf mapping into an ``ExemplarShelf``."""
    if not isinstance(raw, dict):
        raise ExemplarShelfError(
            f"shelf must be a mapping at the top level, got {type(raw).__name__}"
        )

    version = raw.get("version", 1)
    if version not in SUPPORTED_VERSIONS:
        raise ExemplarShelfError(
            f"unsupported shelf version {version!r} (supported: {sorted(SUPPORTED_VERSIONS)})"
        )

    raw_patterns = raw.get("patterns")
    if raw_patterns is None:
        raise ExemplarShelfError("shelf has no 'patterns' key")
    if not isinstance(raw_patterns, list):
        raise ExemplarShelfError("'patterns' must be a list")

    patterns = tuple(_parse_pattern(row, i) for i, row in enumerate(raw_patterns))

    raw_retired = raw.get("retired") or []
    if not isinstance(raw_retired, list):
        raise ExemplarShelfError("'retired' must be a list")
    retired = tuple(_parse_retired(row, i) for i, row in enumerate(raw_retired))

    _check_shelf_integrity(patterns, retired)

    raw_pending = raw.get("pending_acquisitions") or []
    if not isinstance(raw_pending, list):
        raise ExemplarShelfError("'pending_acquisitions' must be a list")

    shelf = ExemplarShelf(
        version=int(version),
        shelf_id=raw.get("shelf_id"),
        stage=raw.get("stage"),
        culled_on=str(raw["culled_on"]) if raw.get("culled_on") else None,
        decision=raw.get("decision"),
        sources=dict(raw.get("sources") or {}),
        cull=dict(raw.get("cull") or {}),
        patterns=patterns,
        retired=retired,
        pending_acquisitions=tuple(raw_pending),
        source_path=source_path,
    )
    _check_declared_cull_counts(shelf)
    return shelf


def load_shelf(path: Path | str | None = None) -> ExemplarShelf:
    """Load the curated shelf. A missing file degrades to an empty shelf.

    House seed rule: the machinery ships, the curated DATA does not. An absent
    ``seeds/exemplar_shelf.yaml`` warns and yields a shelf that offers nothing
    — it never invents patterns. A present-but-malformed file raises
    ``ExemplarShelfError`` rather than half-loading.
    """
    yaml_path = Path(path) if path is not None else DEFAULT_YAML
    if not yaml_path.exists():
        logger.warning(
            "exemplar shelf %s not found — no bundled seed data, provide your own "
            "(see seeds/README.md and seeds/exemplar_shelf.example.yaml)",
            yaml_path,
        )
        return ExemplarShelf(source_path=yaml_path)

    try:
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ExemplarShelfError(f"exemplar shelf {yaml_path} is not valid YAML: {exc}")

    shelf = parse_shelf(raw if raw is not None else {}, source_path=yaml_path)
    logger.info(
        "exemplar shelf loaded: %d offered (%d ready, %d held), %d retired [%s]",
        len(shelf.offered_ids()),
        len(shelf.ready_ids()),
        len(shelf.held_ids()),
        len(shelf.retired_ids()),
        yaml_path,
    )
    return shelf
