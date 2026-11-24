# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Namespaced tag binding — `<family>:<value>` (W7 TAG-1).

A tag is a CLASSIFICATION only when it names a registered vocabulary family
and a value of it. This module is the one rule that decides what a raw tag
string is, at write time, against a snapshot of the registered vocabulary:

  * ``family:value`` whose family is loaded and whose value is known (an
    alias resolves to its canonical) → ``bound``;
  * a family-shaped prefix whose family is NOT loaded (never registered, or
    the registry left it unloaded) or whose value is unknown → ``unclassified``
    — the raw string is kept verbatim in ``raw``; never dropped, never guessed;
  * a bare value (no family prefix) → ``src:<value>``, status ``legacy_src``:
    the reserved namespace for provenance strings that are not a
    classification (today's 17k publisher RSS categories on ``signals.tags``,
    a free-string column value on a substrate row). ``src`` is not a
    vocabulary family; it is reserved here.

A prefix counts as a family only when it is family-SHAPED: the reserved
``src``, one of the platform families, or a namespaced ``<profile>.<axis>``
name (``schemas.vocabulary.NAMESPACED_FAMILY_RE``). ``News: World`` or
``severity:high`` are therefore bare values (``src:News: World``), not a
claim on an unregistered family.

`bind_tag` never raises. Each NEW unknown ``(family, value)`` logs one line per
process, bounded (`UNKNOWN_LOG_CAP`); after the cap one suppression line.

The substrate rows carry the result as ``data.classifications`` —
``[{family, value, status}]`` (+ ``raw`` when unclassified) — written by the
four producers (facts, situations, events, entity profiles; see
`PRODUCER_AXES`) and, for signals, by ``scripts/reclassify_tags.py --apply``.

`signal_tag_key` is the namespaced comparison key ``has_tag()`` uses for a
SIGNAL tag (bare → ``src:<tag>``), so a publisher category can never satisfy a
target-tag predicate (Review A §2.2c). `signal_tag_match_forms` is the same
key's preimage — the stored strings a SLICE filter on that tag matches (a bare
``x`` and ``src:x`` are one tag; ``demo.axis:x`` is another).

The runtime's vocabulary snapshot reaches the producers through
`install_vocabulary_provider` / `tag_vocabulary_index` (the runtime installs
``runtime.vocabulary_runtime``'s TTL cache at bring-up). With no provider
installed — the registry process, a script, a test — the index is EMPTY:
every family is unloaded, so a namespaced tag is ``unclassified`` and a bare
one ``legacy_src``. No I/O happens here.

A leaf: stdlib only.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping

#: The family-name shapes, mirrored from ``schemas.vocabulary`` (PLATFORM_FAMILIES,
#: NAMESPACED_FAMILY_RE) rather than imported: importing ``schemas`` runs its
#: package ``__init__``, which reaches ``provenance.writes`` — a producer of this
#: module — so the import would be circular. ``test_tags_binding`` pins both
#: copies equal, so a new platform family cannot drift past this rule.
PLATFORM_FAMILIES: frozenset[str] = frozenset({
    "analyst_kind",
    "band",
    "collection_gap_kind",
    "crosswalk",
    "discovery_kind",
    "edge_family",
    "enrichment_kind",
    "entity_class",
    "event_type",
    "filter_kind",
    "frame",
    "output_kind",
    "relationship_type",
    "salience_class",
    "source_kind",
    "source_layer",
})
NAMESPACED_FAMILY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,2}$")

logger = logging.getLogger(__name__)

#: The reserved namespace for a bare (un-namespaced) value.
SRC_FAMILY = "src"

STATUS_BOUND = "bound"
STATUS_UNCLASSIFIED = "unclassified"
STATUS_LEGACY_SRC = "legacy_src"
#: The closed status vocabulary of a classification.
STATUSES: tuple[str, ...] = (STATUS_BOUND, STATUS_UNCLASSIFIED, STATUS_LEGACY_SRC)

#: The `data` key the classifications ride under on every substrate row.
CLASSIFICATIONS_KEY = "classifications"

#: At most this many distinct unknown (family, value) pairs are logged per
#: process; the next one logs a single suppression line.
UNKNOWN_LOG_CAP = 1024


# ---------------------------------------------------------------------------
# The vocabulary snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VocabularyIndex:
    """An immutable snapshot of the registered vocabulary, as binding reads it.

    ``values`` maps each LOADED family to its current (non-deprecated)
    canonical values; ``aliases`` maps family → alias → canonical; a family in
    ``unloaded`` (the registry's ``families_unloaded``, or a family whose read
    failed) degrades to unclassified even if it also appears in ``values``.
    ``loaded_at`` is the wall-clock epoch of the read (``None`` = never read).
    """

    values: Mapping[str, frozenset[str]] = field(default_factory=dict)
    aliases: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    unloaded: frozenset[str] = frozenset()
    loaded_at: float | None = None

    def family_loaded(self, family: str) -> bool:
        return family in self.values and family not in self.unloaded

    def resolve(self, family: str, value: str) -> str | None:
        """The canonical value for ``value`` in a loaded ``family`` (alias
        resolved), or ``None`` when the family is unloaded or the value
        unknown."""
        if not self.family_loaded(family):
            return None
        canon = self.aliases.get(family, {}).get(value, value)
        return canon if canon in self.values[family] else None

    @classmethod
    def from_entries(
        cls,
        entries: Iterable[Any],
        *,
        unloaded: Iterable[str] = (),
        loaded_at: float | None = None,
    ) -> "VocabularyIndex":
        """Build from vocabulary entries — objects or mappings carrying
        ``family`` / ``value`` / ``aliases`` / ``deprecated``. Deprecated
        entries are left out (they no longer bind)."""
        values: dict[str, set[str]] = {}
        aliases: dict[str, dict[str, str]] = {}
        for e in entries:
            get = e.get if isinstance(e, Mapping) else (lambda k, _e=e: getattr(_e, k, None))
            fam, val = get("family"), get("value")
            if not isinstance(fam, str) or not isinstance(val, str) or get("deprecated"):
                continue
            values.setdefault(fam, set()).add(val)
            for a in get("aliases") or ():
                if isinstance(a, str):
                    aliases.setdefault(fam, {})[a] = val
        return cls(
            values={f: frozenset(v) for f, v in values.items()},
            aliases=aliases,
            unloaded=frozenset(unloaded),
            loaded_at=loaded_at,
        )

    @classmethod
    def from_cache(cls, cache: Any, *, loaded_at: float | None = None) -> "VocabularyIndex":
        """From a refreshed ``registry.vocabulary_cache.VocabularyCache``:
        its loaded entries plus its ``families_unloaded``."""
        return cls.from_entries(
            cache.registry.entries,
            unloaded=(u["family"] for u in cache.families_unloaded),
            loaded_at=loaded_at,
        )


#: No vocabulary read: every family unloaded.
EMPTY_INDEX = VocabularyIndex()


# ---------------------------------------------------------------------------
# Binding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoundTag:
    """One raw tag, bound. ``raw`` is the input verbatim."""

    family: str
    value: str
    status: str
    raw: str

    @property
    def key(self) -> str:
        return f"{self.family}:{self.value}"

    def as_classification(self) -> dict[str, str]:
        out = {"family": self.family, "value": self.value, "status": self.status}
        if self.status == STATUS_UNCLASSIFIED:
            out["raw"] = self.raw
        return out


def is_family_shaped(prefix: str) -> bool:
    """True when ``prefix`` can name a family: the reserved ``src``, a
    platform family, or a namespaced ``<profile>.<axis>`` name."""
    return (
        prefix == SRC_FAMILY
        or prefix in PLATFORM_FAMILIES
        or NAMESPACED_FAMILY_RE.match(prefix) is not None
    )


def split_tag(raw: str) -> tuple[str | None, str]:
    """``(family, value)`` when ``raw`` is ``<family-shaped>:<value>`` with a
    non-empty value; else ``(None, raw)`` — a bare value."""
    fam, sep, val = raw.partition(":")
    if sep and val and is_family_shaped(fam):
        return fam, val
    return None, raw


_unknown_lock = threading.Lock()
_unknown_seen: set[tuple[str, str]] = set()
_unknown_suppressed = False


def _note_unknown(family: str, value: str, reason: str) -> None:
    global _unknown_suppressed
    key = (family, value)
    with _unknown_lock:
        if key in _unknown_seen:
            return
        if len(_unknown_seen) >= UNKNOWN_LOG_CAP:
            if not _unknown_suppressed:
                _unknown_suppressed = True
                logger.warning(
                    "tags.unclassified log cap reached (%d distinct) — further "
                    "new unknown tags are not logged in this process",
                    UNKNOWN_LOG_CAP,
                )
            return
        _unknown_seen.add(key)
    logger.info(
        "tags.unclassified family=%s value=%r reason=%s", family, value[:200], reason,
    )


def reset_unknown_log() -> None:
    """Forget the per-process unknown-tag log state (tests)."""
    global _unknown_suppressed
    with _unknown_lock:
        _unknown_seen.clear()
        _unknown_suppressed = False


def bind_tag(raw: Any, *, families: VocabularyIndex) -> BoundTag:
    """Bind one raw tag against ``families``. Never raises (see module doc)."""
    try:
        text = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
        s = text.strip()
        if not s:
            return BoundTag("", "", STATUS_UNCLASSIFIED, text)
        fam, val = split_tag(s)
        if fam is None:
            return BoundTag(SRC_FAMILY, s, STATUS_LEGACY_SRC, text)
        if fam == SRC_FAMILY:
            return BoundTag(SRC_FAMILY, val, STATUS_LEGACY_SRC, text)
        if not families.family_loaded(fam):
            _note_unknown(fam, val, "family_unloaded")
            return BoundTag(fam, val, STATUS_UNCLASSIFIED, text)
        canon = families.resolve(fam, val)
        if canon is None:
            _note_unknown(fam, val, "value_unknown")
            return BoundTag(fam, val, STATUS_UNCLASSIFIED, text)
        return BoundTag(fam, canon, STATUS_BOUND, text)
    except Exception:  # never raises — a pathological input is unclassified
        return BoundTag("", "", STATUS_UNCLASSIFIED, repr(raw)[:200])


def bind_tags(raws: Iterable[Any], *, families: VocabularyIndex) -> list[BoundTag]:
    """Bind each raw tag; empty inputs skipped; first occurrence of each
    (family, value, status) kept, in input order."""
    out: list[BoundTag] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in raws or ():
        b = bind_tag(raw, families=families)
        if not b.family and not b.value:
            continue
        k = (b.family, b.value, b.status)
        if k in seen:
            continue
        seen.add(k)
        out.append(b)
    return out


def classifications(raws: Iterable[Any], *, families: VocabularyIndex) -> list[dict[str, str]]:
    """The ``data.classifications`` list for ``raws``."""
    return [b.as_classification() for b in bind_tags(raws, families=families)]


def signal_tag_key(raw: Any) -> str | None:
    """The namespaced comparison key of a SIGNAL tag (no vocabulary needed):
    ``<family>:<value>`` when family-shaped, else ``src:<tag>``. ``None`` for
    an empty or non-string tag."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    fam, val = split_tag(s)
    return f"{SRC_FAMILY}:{s}" if fam is None else f"{fam}:{val}"


def signal_tag_match_forms(raw: Any) -> tuple[str, ...]:
    """Every stored ``signals.tags`` string that is THIS tag — THE legacy
    equivalence rule a slice filter matches under (W8 TAX-1).

    Two stored strings are the same tag exactly when `signal_tag_key` gives
    them the same key; this returns that key's whole preimage, so a slice's
    ``tags && $n`` can compare raw column values without rewriting the column:

      * ``demo.axis:x`` → ``("demo.axis:x",)`` — a namespaced tag is only
        itself; a bare ``x`` is NOT it;
      * ``src:x`` or a bare ``x`` → ``("src:x", "x")`` — the reserved ``src``
        namespace matches its namespaced form AND the legacy bare form every
        signal carries today (the 10-04 census: all 17,808 raw values are
        ``legacy_src``). TAX-4's retrofit does not rewrite ``signals.tags``
        (nor ``facts.predicate``), so the slice matches both forms until a
        signals retrofit exists;
      * ``src:demo.axis:x`` → ``("src:demo.axis:x",)`` — the bare
        ``demo.axis:x`` keys to its own family, so it is not this tag.

    Exact-string forms: a stored tag with surrounding whitespace is not
    matched (the ingest coercion strips it). ``()`` for an empty or
    non-string tag.
    """
    key = signal_tag_key(raw)
    if key is None:
        return ()
    fam, _, val = key.partition(":")
    if fam == SRC_FAMILY and signal_tag_key(val) == key:
        return (key, val)
    return (key,)


def is_reserved_src(tag: Any) -> bool:
    """True for a tag in the reserved ``src`` namespace — it belongs to
    signal provenance and never counts as a target or event tag."""
    return isinstance(tag, str) and split_tag(tag.strip())[0] == SRC_FAMILY


# ---------------------------------------------------------------------------
# The four producers
# ---------------------------------------------------------------------------

#: Per substrate table: the case-bearing column(s) its producer binds, and the
#: family the column's values belong to when that vocabulary IS a registered
#: family (``None`` = the value is bound as written — namespaced when it
#: carries a family, ``src:`` legacy otherwise). Plus, for every table, any
#: ``data.tags`` list the producer already carries.
PRODUCER_AXES: dict[str, tuple[tuple[str, str | None], ...]] = {
    "facts": (("predicate", None),),
    "situations": (("category", None),),
    "events": (("category", None),),
    "entity_profiles": (("entity_class", "entity_class"),),
}


def producer_raw_tags(
    table: str, columns: Mapping[str, Any], data: Mapping[str, Any] | None = None,
) -> list[str]:
    """The raw tag strings a producer's row binds (see `PRODUCER_AXES`)."""
    raws: list[str] = []
    for col, family in PRODUCER_AXES[table]:
        v = columns.get(col)
        if isinstance(v, str) and v.strip():
            raws.append(f"{family}:{v.strip()}" if family else v)
    extra = (data or {}).get("tags") if isinstance(data, Mapping) else None
    if isinstance(extra, (list, tuple)):
        raws.extend(t for t in extra if isinstance(t, str))
    return raws


def stamp_classifications(
    table: str,
    columns: Mapping[str, Any],
    data: Mapping[str, Any] | None,
    *,
    families: VocabularyIndex,
) -> dict[str, Any]:
    """A COPY of ``data`` with ``classifications`` set for this row. The
    caller's dict is never mutated."""
    out = dict(data or {})
    out[CLASSIFICATIONS_KEY] = classifications(
        producer_raw_tags(table, columns, data), families=families,
    )
    return out


# ---------------------------------------------------------------------------
# The process-wide provider (installed by the runtime)
# ---------------------------------------------------------------------------

_provider: Callable[[], Awaitable[VocabularyIndex]] | None = None


def install_vocabulary_provider(
    provider: Callable[[], Awaitable[VocabularyIndex]] | None,
) -> None:
    """Install (or, with ``None``, remove) the process's vocabulary source."""
    global _provider
    _provider = provider


async def tag_vocabulary_index() -> VocabularyIndex:
    """The current snapshot for write-time binding. EMPTY when no provider is
    installed or the provider fails — binding then degrades, it never stops a
    write."""
    if _provider is None:
        return EMPTY_INDEX
    try:
        idx = await _provider()
    except Exception as exc:
        logger.warning("tags.vocabulary_provider_failed err=%s", exc)
        return EMPTY_INDEX
    return idx if isinstance(idx, VocabularyIndex) else EMPTY_INDEX


__all__ = [
    "BoundTag",
    "CLASSIFICATIONS_KEY",
    "EMPTY_INDEX",
    "PRODUCER_AXES",
    "SRC_FAMILY",
    "STATUSES",
    "STATUS_BOUND",
    "STATUS_LEGACY_SRC",
    "STATUS_UNCLASSIFIED",
    "UNKNOWN_LOG_CAP",
    "VocabularyIndex",
    "bind_tag",
    "bind_tags",
    "classifications",
    "install_vocabulary_provider",
    "is_family_shaped",
    "is_reserved_src",
    "producer_raw_tags",
    "signal_tag_key",
    "signal_tag_match_forms",
    "split_tag",
    "stamp_classifications",
    "tag_vocabulary_index",
]
