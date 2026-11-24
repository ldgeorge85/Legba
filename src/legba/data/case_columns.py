# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The case-bearing columns, each bound to a registered family (W9 TAX-4).

Four substrate columns carry a CLASSIFICATION, and before this lane none of
them was bound to one (Review B §1.1b, finding 4; the 10-04 census):

    situations.category        held the desk id (``country_g20_us``) — a SUBJECT,
    events.category            not a category — plus publisher RSS words
                               (``news``, ``sport``) and junk (``severity:high``)
    events.event_type          two live values (incident / development)
    entity_profiles.entity_type 7 of the 17 ``entity_class`` values

THE CONTRACT, per column (what the column may hold after a write):

``situations.category`` / ``events.category``
    a REGISTERED classification, as its namespaced key ``<family>:<value>``
    (a namespaced value that binds ``bound`` — ``tags.bind_tag`` — or a bare
    value that binds as a ``salience_class``, written ``salience_class:<value>``;
    the same vocabulary snapshot TAG-1 binds with), else the
    literal ``unclassified``. A value that is the id of a registered SUBJECT
    (a ``target_descriptors`` row, any version) is not a category: it moves to
    ``data.subject_id`` and the column says ``unclassified`` (or the class the
    row's ``data.event_class`` names, when it binds). A value the column no
    longer holds is never dropped: the value as the producer wrote it rides in
    ``data.category_raw`` whenever the column differs from it.

``events.event_type``
    the ``event_type`` platform family (seeded from
    ``descriptors/vocabulary_event_type.yaml``; the literal :data:`EVENT_TYPES`
    is the fallback). The column is also a DB CHECK (``events_event_type_check``,
    migration 0230 adds ``unclassified``), so the family can NARROW it and never
    widen it — the read-through is intersected with the literal, the TAXON
    idiom for a CHECK-bound family. A value outside it → ``unclassified``.

``entity_profiles.entity_type``
    the ``entity_class`` family (the runtime's vocabulary snapshot; the seed
    literal ``vocabulary.ENTITY_CLASSES`` when unloaded). A value outside it →
    ``unclassified`` — never the catch-all ``entity``, which is a value of the
    family and so binds as itself only when a producer actually wrote it.

THE TOPIC KEY. The raw category was load-bearing before it was a
classification: the tower's situation leg mints its event SIGNATURE from it,
and the event matcher's prefilter and category feature compare it. Those
readers read :func:`topic_key_sql` — ``COALESCE(data->>'category_raw',
category)`` — which is the producer's value byte for byte before and after the
retrofit, so no identity moves.

The retrofit of the existing rows is ``scripts/retrofit_case_columns.py``
(``--dry-run`` default; ``--apply`` is the operator's act).

A LEAF: stdlib + ``legba.data.tags`` + ``legba.data.taxon_families`` +
``legba.data.vocabulary`` (all leaves) — importable from the slim registry
image. The one I/O helper (:func:`registered_subjects`) takes the caller's
connection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Collection, Iterable, Mapping

from . import tags as _tags
from .taxon_families import SALIENCE_CLASS
from .vocabulary import ENTITY_CLASSES

#: The literal a case column holds when nothing registered applies.
UNCLASSIFIED = "unclassified"

#: ``data`` keys the category binding writes.
SUBJECT_KEY = "subject_id"
RAW_KEY = "category_raw"
#: ``data`` key a producer may carry a salience class under (the hint).
EVENT_CLASS_KEY = "event_class"

#: The ``event_type`` platform family and its literal — the set the producer
#: can emit (``_event_candidates._EVENT_TYPES``) and the CHECK admits.
EVENT_TYPE_FAMILY = "event_type"
EVENT_TYPES: tuple[str, ...] = ("incident", "development", "shift", "threshold")

ENTITY_CLASS_FAMILY = "entity_class"

#: Binding statuses of a category value.
STATUS_REGISTERED = "registered"
STATUS_SUBJECT = "subject"
STATUS_UNCLASSIFIED = "unclassified"

#: The tables whose ``category`` is case-bearing.
CATEGORY_TABLES: tuple[str, ...] = ("situations", "events")

#: A subject is a registered target (any version — a retired subject is still
#: the subject an old row named). Hits ``target_descriptors_pkey``'s prefix.
SUBJECTS_SQL = """
    SELECT DISTINCT descriptor_id
      FROM target_descriptors
     WHERE descriptor_id = ANY($1::text[])
"""


# ---------------------------------------------------------------------------
# The families, read through the vocabulary snapshot
# ---------------------------------------------------------------------------


def _loaded(families: Any, family: str) -> frozenset[str] | None:
    if families is None or not families.family_loaded(family):
        return None
    vals = families.values.get(family) or frozenset()
    return frozenset(vals) or None


def event_type_values(families: Any = None) -> frozenset[str]:
    """The ``event_type`` family as loaded, intersected with the CHECK literal
    (narrow, never widen); the literal when unloaded or the intersection is
    empty."""
    lit = frozenset(EVENT_TYPES)
    vals = _loaded(families, EVENT_TYPE_FAMILY)
    if vals is None:
        return lit
    return (vals & lit) or lit


def entity_type_values(families: Any = None) -> frozenset[str]:
    """The ``entity_class`` family as loaded; the seed literal when not."""
    return _loaded(families, ENTITY_CLASS_FAMILY) or frozenset(ENTITY_CLASSES)


def bind_event_type(raw: Any, *, families: Any = None) -> str:
    """``raw`` when it is a current ``event_type`` value, else ``unclassified``."""
    v = raw.strip() if isinstance(raw, str) else ""
    return v if v in event_type_values(families) else UNCLASSIFIED


def bind_entity_type(raw: Any, *, families: Any = None) -> str:
    """``raw`` when it is a current ``entity_class`` value, else
    ``unclassified`` (never the catch-all ``entity``)."""
    v = raw.strip() if isinstance(raw, str) else ""
    return v if v in entity_type_values(families) else UNCLASSIFIED


# ---------------------------------------------------------------------------
# The category binding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CategoryBinding:
    """One category value, bound. ``category`` is what the column holds;
    ``raw`` what the producer wrote; ``subject_id`` the subject it named."""

    category: str
    raw: str
    status: str
    subject_id: str | None = None

    def data_fields(self) -> dict[str, str]:
        """The ``data`` keys this binding writes (none when the column holds
        the raw value unchanged)."""
        out: dict[str, str] = {}
        if self.subject_id:
            out[SUBJECT_KEY] = self.subject_id
        if self.category != self.raw:
            out[RAW_KEY] = self.raw
        return out

    @property
    def classification_value(self) -> str:
        """The value TAG-1's ``data.classifications`` binds: the registered key
        when there is one, else the raw value (kept as provenance; the literal
        ``unclassified`` is not a value and binds nothing)."""
        if self.category != UNCLASSIFIED:
            return self.category
        return "" if self.raw.strip() == UNCLASSIFIED else self.raw


def _registered_key(value: str, families: Any) -> str | None:
    """``<family>:<canonical>`` when ``value`` is a registered classification
    (a namespaced value that binds, or a bare salience class), else None."""
    if not value or value == UNCLASSIFIED:
        return None
    fam, val = _tags.split_tag(value)
    if fam is not None:
        if fam == _tags.SRC_FAMILY:
            return None
        b = _tags.bind_tag(value, families=families or _tags.EMPTY_INDEX)
        return b.key if b.status == _tags.STATUS_BOUND else None
    # a bare value binds only as a salience class — TAG-1's one rule, so the
    # column and data.classifications never disagree (an unloaded family is
    # unclassified, never guessed from a literal)
    # (resolved directly: probing a publisher word is not an unknown-tag event
    # worth bind_tag's log line)
    canon = (families or _tags.EMPTY_INDEX).resolve(SALIENCE_CLASS, value)
    return f"{SALIENCE_CLASS}:{canon}" if canon else None


def needs_subject_lookup(raw: Any) -> bool:
    """Only a bare, non-empty value can be a subject id."""
    if not isinstance(raw, str):
        return False
    s = raw.strip()
    return bool(s) and s != UNCLASSIFIED and _tags.split_tag(s)[0] is None


def bind_category(
    raw: Any,
    *,
    families: Any = None,
    subjects: Collection[str] = (),
    hint: Any = None,
) -> CategoryBinding:
    """Bind one category value (see the module doc). Never raises.

    ``subjects`` — the registered subject ids among the values being bound
    (:func:`registered_subjects`); ``hint`` — a salience class the row's data
    carries (``data.event_class``), used when the value itself is not a class.
    """
    text = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
    value = text.strip()
    key = _registered_key(value, families)
    if key is not None:
        return CategoryBinding(key, text, STATUS_REGISTERED)
    hinted = _registered_key(hint.strip(), families) if isinstance(hint, str) else None
    if value and value in subjects:
        return CategoryBinding(hinted or UNCLASSIFIED, text, STATUS_SUBJECT, value)
    if hinted is not None:
        return CategoryBinding(hinted, text, STATUS_REGISTERED)
    return CategoryBinding(UNCLASSIFIED, text, STATUS_UNCLASSIFIED)


async def registered_subjects(conn: Any, values: Iterable[Any]) -> frozenset[str]:
    """The values among ``values`` that are registered subject ids."""
    cands = sorted({v.strip() for v in values if needs_subject_lookup(v)})
    if not cands:
        return frozenset()
    rows = await conn.fetch(SUBJECTS_SQL, cands)
    return frozenset(r["descriptor_id"] for r in rows)


async def bind_category_for_write(
    conn: Any, raw: Any, *, families: Any = None, hint: Any = None,
) -> CategoryBinding:
    """The producer's write-time binding: the subject lookup on ``conn``, then
    :func:`bind_category`."""
    subjects = await registered_subjects(conn, [raw]) if needs_subject_lookup(raw) else ()
    return bind_category(raw, families=families, subjects=subjects, hint=hint)


def stamp_category_data(
    table: str,
    binding: CategoryBinding,
    data: Mapping[str, Any] | None,
    *,
    families: Any = None,
) -> dict[str, Any]:
    """A COPY of ``data`` carrying the binding's ``subject_id`` /
    ``category_raw`` (stale ones dropped) and TAG-1's ``classifications`` of
    the bound value. The caller's dict is never mutated."""
    out = dict(data or {})
    out.pop(SUBJECT_KEY, None)
    out.pop(RAW_KEY, None)
    out.update(binding.data_fields())
    return _tags.stamp_classifications(
        table, {"category": binding.classification_value}, out,
        families=families or _tags.EMPTY_INDEX,
    )


def event_class_hint(data: Mapping[str, Any] | None) -> Any:
    """The salience class a row's data carries, if any."""
    return data.get(EVENT_CLASS_KEY) if isinstance(data, Mapping) else None


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def topic_key_sql(alias: str) -> str:
    """The producer's category value, byte for byte, before and after the
    retrofit: what the signature / matcher readers compare."""
    return f"COALESCE({alias}.data->>'{RAW_KEY}', {alias}.category)"


def category_filter_sql(alias: str, param: int) -> str:
    """A ``category`` filter that matches the registered column OR the
    producer's value (a caller filtering by the old desk/topic word keeps
    working)."""
    return f"({alias}.category = ${param} OR {alias}.data->>'{RAW_KEY}' = ${param})"


__all__ = [
    "CATEGORY_TABLES",
    "CategoryBinding",
    "ENTITY_CLASS_FAMILY",
    "EVENT_CLASS_KEY",
    "EVENT_TYPES",
    "EVENT_TYPE_FAMILY",
    "RAW_KEY",
    "STATUS_REGISTERED",
    "STATUS_SUBJECT",
    "STATUS_UNCLASSIFIED",
    "SUBJECTS_SQL",
    "SUBJECT_KEY",
    "UNCLASSIFIED",
    "bind_category",
    "bind_category_for_write",
    "bind_entity_type",
    "bind_event_type",
    "category_filter_sql",
    "entity_type_values",
    "event_class_hint",
    "event_type_values",
    "needs_subject_lookup",
    "registered_subjects",
    "stamp_category_data",
    "topic_key_sql",
]
