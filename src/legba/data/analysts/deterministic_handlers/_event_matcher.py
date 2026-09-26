# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The V3/P1 event matcher — March's measured four-feature score.

This leaf owns only pairwise similarity and connected-component clustering. The
four weighted features and their weights are the v1 contract the V3 plan pins
(``DATA_MODEL_V3`` §2.2): entity overlap .30, normalized title distance .30,
24-hour temporal proximity .20, category/tag overlap .20, threshold .50. The
embedding cosine is deliberately a separately measured additive gate, not a
sixth weighted term: a structurally valid high-cosine pair can link below the
base threshold, while the receipt can still say which leg carried it.

No database or Qdrant calls live here. ``event_clustering.py`` supplies rows in
the small ``EventEvidence`` shape and, when available, a pair-indexed cosine
map that has already survived ``cross_source_dedup``'s uuid-shaped
``embedding_ref`` gate.
"""

from __future__ import annotations

import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional
from uuid import UUID

from ..._entity_canon import (
    DIRECTIONAL_TOKENS,
    _direction_probe_tokens,
    differs_by_direction_tokens,
    identity_fold,
    is_junk_entity,
)
from ...filters.dedupe import Dedupe4TierHandler, _normalized_levenshtein

#: Deploy marker required by V3 plan §2 P1. P1b (2026-09-23) added the pass
#: budget, the watermarked cursors and the per-phase receipt timings. P1c
#: (2026-09-23) added pair BLOCKING and the per-row feature precompute; the
#: matcher weights, thresholds and promotion bar are unchanged.
EVENT_CLUSTERING_VERSION: str = "2026-09/p1c"

WEIGHT_ENTITY = 0.30
WEIGHT_TITLE = 0.30
WEIGHT_TEMPORAL = 0.20
WEIGHT_CATEGORY = 0.20
DEFAULT_MATCH_THRESHOLD = 0.50
DEFAULT_EMBEDDING_THRESHOLD = 0.97
TEMPORAL_WINDOW_SECONDS = 24.0 * 60.0 * 60.0
MAX_CLUSTER_MEMBERS = 30

#: P1c BLOCKING WINDOW. A pair is scored only when the two rows share a
#: resolved entity fold, share a fact subject, carry a caller-supplied
#: embedding cosine, or fall within this many hours of each other. Everything
#: else is DEFINED unrelated rather than scored: with no shared entity and no
#: shared fact subject, the four features can only reach the 0.50 threshold
#: through title + category, and past a few hours the temporal term that would
#: have to carry such a pair is already spent.
#:
#: Measured 2026-09-23 on the live slice (the newest 300 canonical text
#: signals span 2 h 46 m, so the pair count is dense in the first hour):
#:
#:   window   pairs scored   of 44,850
#:    6 min          3,331       7.4 %
#:   15 min          7,175      16.0 %
#:   30 min         13,579      30.3 %
#:    1 h           25,030      55.8 %
#:    2 h           40,014      89.2 %
#:    6 h           44,850     100.0 %
#:
#: At that density the 6 h default blocks NOTHING — it is the conservative
#: ceiling, not the operating point. ``pair_block_window_hours`` is the
#: descriptor knob the operator lowers (1 h leaves the 300-signal pass well
#: inside the turn budget); shared entities and fact subjects still admit a
#: pair at any age, so lowering the window never costs a corroborated link.
DEFAULT_PAIR_BLOCK_WINDOW_HOURS = 6.0

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

#: ``prepare_open_events``'s fallback id when a row carries none of its own —
#: never read by any scored feature (see that function's docstring), so any
#: fixed value is exact.
_NIL_EVENT_ID = UUID(int=0)


def _utc(value: Any) -> datetime | None:
    """Coerce an ISO string / datetime to an aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    elif value:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _uuid(value: Any) -> UUID | None:
    """Coerce ``value`` to UUID, returning ``None`` on malformed input."""
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _text(value: Any) -> str:
    """Normalize an arbitrary scalar to a stripped string."""
    return str(value or "").strip()


def _lower_list(values: Iterable[Any] | None) -> tuple[str, ...]:
    """Lowercase, strip and dedupe a scalar-or-sequence value."""
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        seq: Iterable[Any] = (values,)
    else:
        seq = values
    return tuple(sorted({_text(v).lower() for v in seq if _text(v)}))


@dataclass(frozen=True)
class EventEvidence:
    """One clustering observation (normally one canonical signal).

    ``entity_names`` are canonical surfaces, matching ``events.signature``'s
    declared input domain. ``entity_ids`` are resolved profile UUIDs and are
    kept beside the names so the write path does not have to re-resolve names.
    ``fact_subjects`` is the separately measured second pivot feature; P1 wires
    it only as a gate when a caller supplies subject labels.
    """

    id: UUID
    title: str = ""
    category: str = ""
    tags: tuple[str, ...] = ()
    fetched_at: datetime | None = None
    source_id: str = ""
    source_class: str = "reporting"
    source_kind: str = ""
    geo: tuple[str, ...] = ()
    entity_ids: tuple[UUID, ...] = ()
    entity_names: tuple[str, ...] = ()
    embedding_ref: str = ""
    confidence: float = 0.5
    candidate_kind: str = "signal"
    source_ref_id: UUID | None = None
    fact_subjects: frozenset[str] = frozenset()
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def entity_folds(self) -> frozenset[str]:
        """Identity-folded, junk-free canonical entity tokens."""
        folds = {
            identity_fold(name)
            for name in self.entity_names
            if _text(name) and not is_junk_entity(name)
        }
        return frozenset(f for f in folds if f)

    @property
    def category_terms(self) -> frozenset[str]:
        """The category/topic vocabulary used by feature four."""
        terms = set(_lower_list(self.tags))
        if self.category:
            terms.add(self.category.lower())
        return frozenset(terms)


@dataclass(frozen=True)
class EventMatch:
    """One pairwise match decision with its component scores."""

    score: float
    linked: bool
    entity_overlap: float
    title_similarity: float
    temporal_proximity: float
    category_match: float
    embedding_cosine: float | None = None
    shared_fact_subjects: int = 0
    via_embedding: bool = False
    via_shared_facts: bool = False
    refused_by_direction: bool = False


@dataclass(frozen=True)
class EventCluster:
    """One connected component of ``EventEvidence`` observations."""

    members: tuple[EventEvidence, ...]
    oversized: bool

    @property
    def member_ids(self) -> tuple[UUID, ...]:
        """Member ids in stable UUID order."""
        return tuple(sorted(m.id for m in self.members))

    @property
    def signal_ids(self) -> list[UUID]:
        """Resolvable signal ids, stable order."""
        return [m.id for m in sorted(self.members, key=lambda x: str(x.id))]


def _non_direction_stem(name: str) -> tuple[str, ...]:
    """Token stem with compass/positional words removed."""
    tokens = [
        token.strip(".,;:!?()[]{}\"'").lower()
        for token in str(name or "").split()
    ]
    return tuple(t for t in tokens if t and t not in DIRECTIONAL_TOKENS)


@dataclass(frozen=True)
class _NameProbe:
    """One entity surface's direction-probe tokens and non-direction stem,
    computed ONCE — the value `_opposed_compass_stem`'s restricted pair walk
    reads instead of re-tokenizing the same surface on every candidate x
    event comparison it takes part in.

    ``tokens`` mirrors ``differs_by_direction``'s own tokenizer
    (``_entity_canon._direction_probe_tokens``); ``stem`` is
    `_non_direction_stem`'s output — a SEPARATE tokenization, kept separate
    on purpose so probing a name here changes what neither existing function
    returns.
    """

    name: str
    tokens: tuple[str, ...]
    stem: tuple[str, ...]
    has_direction: bool


def _probe_name(name: str) -> _NameProbe:
    """`_NameProbe`'s constructor: probe tokens, stem and direction flag for
    one entity surface."""
    tokens = tuple(_direction_probe_tokens(name))
    return _NameProbe(
        name=name,
        tokens=tokens,
        stem=_non_direction_stem(name),
        has_direction=any(t in DIRECTIONAL_TOKENS for t in tokens),
    )


def _probe_names(names: Iterable[str]) -> tuple[_NameProbe, ...]:
    """Probe every non-blank surface in ``names``, ONCE, order preserved.

    A blank surface can never satisfy `_opposed_pair` (its stem is the empty
    tuple, and the stem-equality check requires a truthy stem on the left
    side), so dropping it here changes no verdict.
    """
    return tuple(_probe_name(name) for name in names if _text(name))


@dataclass(frozen=True)
class _Prepared:
    """One row's pair-invariant features, computed ONCE per row.

    ``entity_folds`` / ``category_terms`` are properties that re-run the whole
    identity-fold canon on every access, and ``normalized_title`` re-normalizes
    on every call — so the all-pairs loop used to recompute each row's features
    ``n - 1`` times. Profiled 2026-09-23 over a 300-row slice (44,850 pairs):
    the fold recompute alone was 13.7 s of a 91.5 s run. Hoisting them here is
    pure arithmetic-identical saving: the same values, computed n times instead
    of n².

    ``name_probes`` / ``direction_names`` are P1f's row-level precomputation
    of the SAME question: rather than re-tokenizing every entity surface on
    every pairwise `_opposed_compass_stem` call, each surface's probe tokens
    and non-direction stem are computed here, once, and ``direction_names``
    keeps just the subset that carries a direction token — the only surfaces
    that can ever flip the compass refusal. ``has_direction`` is exactly
    ``bool(direction_names)``.
    """

    row: EventEvidence
    folds: frozenset[str]
    terms: frozenset[str]
    title: str
    when: datetime | None
    subjects: frozenset[str]
    embed_ok: bool
    has_direction: bool
    name_probes: tuple[_NameProbe, ...] = ()
    direction_names: tuple[_NameProbe, ...] = ()


def _prepare(row: EventEvidence) -> _Prepared:
    """Compute one row's pair-invariant features."""
    name_probes = _probe_names(row.entity_names)
    direction_names = tuple(p for p in name_probes if p.has_direction)
    return _Prepared(
        row=row,
        folds=row.entity_folds,
        terms=row.category_terms,
        title=Dedupe4TierHandler.normalized_title(row.title),
        when=_utc(row.fetched_at),
        subjects=row.fact_subjects,
        embed_ok=valid_embedding_ref(row.embedding_ref),
        has_direction=bool(direction_names),
        name_probes=name_probes,
        direction_names=direction_names,
    )


def _opposed_pair(a: _NameProbe, b: _NameProbe) -> bool:
    """True when two ALREADY-PROBED surfaces differ only by opposed
    directions — the per-pair half of `_opposed_compass_stem`."""
    if not differs_by_direction_tokens(a.tokens, b.tokens):
        return False
    return bool(a.stem) and a.stem == b.stem


def _opposed_compass_stem(left: _Prepared, right: _Prepared) -> bool:
    """True when two entity surfaces differ only by opposed directions.

    ``differs_by_direction_tokens`` (hence `_opposed_pair`) can only be True
    when at least one of the two surfaces carries a direction token: with no
    directional token on either side, its positional branch's ``x in
    DIRECTIONAL_TOKENS or y in DIRECTIONAL_TOKENS`` and its multiset branch's
    empty-vs-empty comparison both answer False (see that function's
    docstring). So the only pairs that can EVER flip this True are (a
    DIRECTION-BEARING name) paired with (any name) — the two loops below walk
    exactly that restricted set, using each row's precomputed probes, instead
    of the full name x name cross product. Same boolean on every pair; the
    live density (North Korea, South Sudan, West Bank, ... on almost every
    row) is what made the full cross product expensive.
    """
    for a in left.direction_names:
        for b in right.name_probes:
            if _opposed_pair(a, b):
                return True
    for a in left.name_probes:
        for b in right.direction_names:
            if _opposed_pair(a, b):
                return True
    return False


def _entity_overlap(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard over identity-folded canonical entity sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def _title_similarity(a: str, b: str) -> float:
    """``1 - normalized Levenshtein`` over already-normalized titles."""
    if not a or not b:
        return 0.0
    return max(0.0, 1.0 - _normalized_levenshtein(a, b))


def _temporal_proximity(a: datetime | None, b: datetime | None) -> float:
    """Linear 24-hour proximity over evidence time."""
    if a is None or b is None:
        return 0.0
    gap = abs((a - b).total_seconds())
    if gap >= TEMPORAL_WINDOW_SECONDS:
        return 0.0
    return 1.0 - (gap / TEMPORAL_WINDOW_SECONDS)


def _category_match(a: frozenset[str], b: frozenset[str]) -> float:
    """One when normalized category/tag vocabularies overlap."""
    return 1.0 if a and b and a & b else 0.0


def valid_embedding_ref(value: Any) -> bool:
    """The ``cross_source_dedup`` structural gate: uuid-shaped refs only."""
    return bool(_UUID_RE.match(str(value or "").lower()))


def _score_prepared(
    left: _Prepared,
    right: _Prepared,
    *,
    threshold: float,
    embedding_cosine: float | None,
    embedding_threshold: float,
    shared_fact_threshold: int,
) -> EventMatch:
    """The one scoring implementation; ``score_pair`` is its public door."""
    refused = (
        (left.has_direction or right.has_direction)
        and _opposed_compass_stem(left, right)
    )
    entity = 0.0 if refused else _entity_overlap(left.folds, right.folds)
    title = _title_similarity(left.title, right.title)
    temporal = _temporal_proximity(left.when, right.when)
    category = _category_match(left.terms, right.terms)
    base = (
        WEIGHT_ENTITY * entity
        + WEIGHT_TITLE * title
        + WEIGHT_TEMPORAL * temporal
        + WEIGHT_CATEGORY * category
    )
    via_embedding = (
        embedding_cosine is not None
        and embedding_cosine >= embedding_threshold
        and left.embed_ok
        and right.embed_ok
        and not refused
    )
    shared_facts = len(left.subjects & right.subjects)
    via_facts = shared_facts >= max(1, int(shared_fact_threshold)) and not refused
    linked = bool((base >= threshold or via_embedding or via_facts) and not refused)
    return EventMatch(
        score=round(base, 6),
        linked=linked,
        entity_overlap=round(entity, 6),
        title_similarity=round(title, 6),
        temporal_proximity=round(temporal, 6),
        category_match=round(category, 6),
        embedding_cosine=embedding_cosine,
        shared_fact_subjects=shared_facts,
        via_embedding=via_embedding,
        via_shared_facts=via_facts,
        refused_by_direction=refused,
    )


def score_pair(
    left: EventEvidence,
    right: EventEvidence,
    *,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
    embedding_cosine: float | None = None,
    embedding_threshold: float = DEFAULT_EMBEDDING_THRESHOLD,
    shared_fact_threshold: int = 1,
) -> EventMatch:
    """Score two observations, applying the additive gates separately."""
    return _score_prepared(
        _prepare(left),
        _prepare(right),
        threshold=threshold,
        embedding_cosine=embedding_cosine,
        embedding_threshold=embedding_threshold,
        shared_fact_threshold=shared_fact_threshold,
    )


class _UnionFind:
    """Small deterministic union-find over member indexes."""

    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        """Return the component root for ``i``."""
        parent = self.parent
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        """Merge two roots with a stable smaller-root parent."""
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            hi, lo = max(ra, rb), min(ra, rb)
            self.parent[hi] = lo


def _pair_key(a: UUID, b: UUID) -> tuple[UUID, UUID]:
    """Canonical unordered pair key."""
    return (a, b) if str(a) <= str(b) else (b, a)


def _blocked_in_pairs(
    prepared: list[_Prepared],
    *,
    window_hours: float,
    embedding_cosines: Mapping[tuple[UUID, UUID], float] | None,
) -> set[tuple[int, int]]:
    """Index pairs worth scoring — the P1c blocking key.

    A pair is admitted when the two rows share a resolved entity fold, share a
    fact subject, carry a caller-supplied embedding cosine, or sit within
    ``window_hours`` of each other. Built from inverted indexes and one sweep
    over the (already time-sorted) rows, so the cost is O(n·k) in the size of
    the admitted set rather than O(n²) in the slice.

    The embedding leg is admitted explicitly because the cosine gate is
    ADDITIVE — a high-cosine pair links below the base threshold — so dropping
    it here would silently retire a real link path rather than merely skip
    arithmetic. The caller has already threshold-gated that map.
    """
    pairs: set[tuple[int, int]] = set()

    def _add(i: int, j: int) -> None:
        if i != j:
            pairs.add((i, j) if i < j else (j, i))

    by_fold: dict[str, list[int]] = {}
    by_subject: dict[str, list[int]] = {}
    for i, item in enumerate(prepared):
        for fold in item.folds:
            by_fold.setdefault(fold, []).append(i)
        for subject in item.subjects:
            by_subject.setdefault(subject, []).append(i)
    for buckets in (by_fold, by_subject):
        for members in buckets.values():
            for position, i in enumerate(members):
                for j in members[position + 1:]:
                    _add(i, j)

    # The time sweep. ``prepared`` follows the caller's (fetched_at, id) sort,
    # so the window is a forward walk that stops at the first row out of range;
    # rows with no resolvable evidence time are never admitted on time alone
    # (their temporal feature is zero by definition).
    window = max(0.0, float(window_hours)) * 3600.0
    if window > 0.0:
        for i, left in enumerate(prepared):
            if left.when is None:
                continue
            for j in range(i + 1, len(prepared)):
                right = prepared[j]
                if right.when is None:
                    continue
                if (right.when - left.when).total_seconds() >= window:
                    break
                _add(i, j)

    if embedding_cosines:
        index = {item.row.id: i for i, item in enumerate(prepared)}
        for left_id, right_id in embedding_cosines:
            i = index.get(left_id)
            j = index.get(right_id)
            if i is not None and j is not None:
                _add(i, j)
    return pairs


def cluster_evidence(
    evidence: Iterable[EventEvidence],
    *,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
    embedding_cosines: Mapping[tuple[UUID, UUID], float] | None = None,
    embedding_threshold: float = DEFAULT_EMBEDDING_THRESHOLD,
    max_members: int = MAX_CLUSTER_MEMBERS,
    shared_fact_threshold: int = 1,
    block_window_hours: float = DEFAULT_PAIR_BLOCK_WINDOW_HOURS,
) -> tuple[list[EventCluster], dict[str, int]]:
    """Single-linkage cluster ``evidence`` into bounded candidate clusters.

    ``embedding_cosines`` keys are canonical unordered signal-id pairs. The map
    is pre-gated by the caller, and this function applies the same uuid-shaped
    ``embedding_ref`` check to both endpoints before using a score.

    P1c: pairs are BLOCKED before scoring (see ``_blocked_in_pairs``). The two
    receipt counters partition the slice exactly —
    ``pairs_examined + pairs_blocked == n(n-1)/2`` — so the receipt still says
    how big the slice was, and ``pairs_examined`` now means "scored", which is
    the number that costs wall clock.
    """
    rows = sorted(
        evidence,
        key=lambda e: (
            _utc(e.fetched_at) or datetime.min.replace(tzinfo=timezone.utc),
            str(e.id),
        ),
    )
    prepared = [_prepare(row) for row in rows]
    uf = _UnionFind(len(rows))
    counters = Counter()
    matches: dict[tuple[UUID, UUID], EventMatch] = {}
    admitted = _blocked_in_pairs(
        prepared,
        window_hours=block_window_hours,
        embedding_cosines=embedding_cosines,
    )
    for i, j in sorted(admitted):
        left, right = prepared[i], prepared[j]
        pair = _pair_key(left.row.id, right.row.id)
        cosine = (embedding_cosines or {}).get(pair)
        match = _score_prepared(
            left,
            right,
            threshold=threshold,
            embedding_cosine=cosine,
            embedding_threshold=embedding_threshold,
            shared_fact_threshold=shared_fact_threshold,
        )
        matches[pair] = match
        if match.refused_by_direction:
            counters["direction_refused"] += 1
        if match.via_embedding:
            counters["embedding_linked"] += 1
        if match.via_shared_facts:
            counters["shared_fact_linked"] += 1
        if match.linked:
            uf.union(i, j)
    groups: dict[int, list[EventEvidence]] = {}
    for i, row in enumerate(rows):
        groups.setdefault(uf.find(i), []).append(row)
    clusters = [
        EventCluster(
            members=tuple(sorted(members, key=lambda e: str(e.id))),
            oversized=len(members) >= max(1, int(max_members)),
        )
        for _, members in sorted(
            groups.items(), key=lambda item: str(min(x.id for x in item[1]))
        )
    ]
    total_pairs = len(rows) * (len(rows) - 1) // 2
    counters["pairs_examined"] = len(matches)
    counters["pairs_blocked"] = total_pairs - len(matches)
    counters["pairs_linked"] = sum(1 for m in matches.values() if m.linked)
    counters["clusters"] = len(clusters)
    counters["oversized"] = sum(1 for c in clusters if c.oversized)
    return clusters, dict(counters)


def aggregate_category(members: Iterable[EventEvidence]) -> str:
    """Modal normalized category/tag, with deterministic tie-break."""
    counts: Counter[str] = Counter()
    for member in members:
        for term in member.category_terms:
            counts[term] += 1
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def aggregate_polity(members: Iterable[EventEvidence]) -> str | None:
    """Modal ISO2-like geo value across member signals."""
    counts: Counter[str] = Counter()
    for member in members:
        for geo in member.geo:
            value = _text(geo).lower()
            if value:
                counts[value] += 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def aggregate_entities(members: Iterable[EventEvidence]) -> tuple[str, ...]:
    """Canonical entity names for signature minting, folded + ordered."""
    names: list[str] = []
    seen: set[str] = set()
    for member in members:
        for name in member.entity_names:
            fold = identity_fold(name) if not is_junk_entity(name) else ""
            if fold and fold not in seen:
                seen.add(fold)
                names.append(_text(name))
    return tuple(sorted(names, key=lambda n: identity_fold(n)))


def latest_evidence_at(members: Iterable[EventEvidence]) -> datetime | None:
    """Newest evidence timestamp in a cluster."""
    stamps = [_utc(m.fetched_at) for m in members]
    stamps = [s for s in stamps if s is not None]
    return max(stamps) if stamps else None


def earliest_evidence_at(members: Iterable[EventEvidence]) -> datetime | None:
    """Oldest evidence timestamp in a cluster."""
    stamps = [_utc(m.fetched_at) for m in members]
    stamps = [s for s in stamps if s is not None]
    return min(stamps) if stamps else None


def cluster_aggregate(members: Iterable[EventEvidence]) -> EventEvidence | None:
    """One candidate cluster's aggregate features as a single evidence row.

    Hoisted out of ``match_cluster_to_event`` in P1c: the match phase scores
    one candidate against every open event (up to ``max_open_events``, live
    2,000), and re-deriving the candidate's modal category, folded entity
    roster and newest stamp inside that loop made the cluster side O(members ×
    events) for a value that does not vary with the event. Callers that match
    one candidate against many rows build this ONCE and pass it through.
    """
    items = list(members)
    if not items:
        return None
    return EventEvidence(
        id=items[0].id,
        title=max(
            items,
            key=lambda e: (
                _utc(e.fetched_at)
                or datetime.min.replace(tzinfo=timezone.utc),
                str(e.id),
            ),
        ).title,
        category=aggregate_category(items),
        tags=tuple(sorted({t for m in items for t in m.tags})),
        fetched_at=latest_evidence_at(items),
        entity_names=aggregate_entities(items),
    )


def _event_like(event_row: Mapping[str, Any], *, fallback_id: UUID) -> EventEvidence:
    """Project one open-event row into the ``EventEvidence`` shape scoring
    reads. Shared by :func:`match_cluster_to_event` and
    :func:`match_prepared_to_event` so the two stay byte-identical on the
    event side."""
    event_entities = tuple(_text(x) for x in event_row.get("entity_names") or ())
    event_geo = tuple(_lower_list(event_row.get("geo") or ()))
    event_time = (
        _utc(event_row.get("latest_linked_at"))
        or _utc(event_row.get("time_end"))
        or _utc(event_row.get("time_start"))
        or _utc(event_row.get("updated_at"))
    )
    return EventEvidence(
        id=_uuid(event_row.get("id")) or fallback_id,
        title=_text(event_row.get("title")),
        category=_text(event_row.get("category")),
        tags=event_geo,
        fetched_at=event_time,
        entity_names=event_entities,
    )


def match_cluster_to_event(
    members: Iterable[EventEvidence],
    event_row: Mapping[str, Any],
    *,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
    aggregate: EventEvidence | None = None,
) -> EventMatch:
    """Score a candidate cluster against an existing event's aggregate features.

    The event row may carry ``entity_names``, ``geo``, ``time_end`` /
    ``latest_linked_at``, ``title`` and ``category``. Entity sets and embedding
    centroids are deliberately the durable identity; a months-later signal need
    not fall inside the old event's 24-hour span to reattach.

    ``aggregate`` is this cluster's :func:`cluster_aggregate`, supplied by a
    caller that is walking many event rows; when absent it is derived here, so
    a one-shot caller is unchanged.
    """
    if aggregate is None:
        aggregate = cluster_aggregate(members)
    if aggregate is None:
        return EventMatch(0.0, False, 0.0, 0.0, 0.0, 0.0)
    event_like = _event_like(event_row, fallback_id=aggregate.id)
    return score_pair(aggregate, event_like, threshold=threshold)


def prepare_aggregate(aggregate: EventEvidence) -> _Prepared:
    """Precompute a cluster aggregate's pair-invariant features ONCE.

    P1d. A caller matching one candidate against many open events (the tower
    leg, up to 2,000 rows) previously called :func:`match_cluster_to_event`
    per row, and — even with ``aggregate`` supplied — ``score_pair`` still ran
    :func:`_prepare` on that SAME unchanging aggregate on every row: folding
    its entities, normalizing its title and scanning it for direction tokens
    from scratch each time. Profiled 2026-09-24 on a 120-member candidate
    against 2,000 open events: that redundant re-``_prepare`` of the fixed
    side was the dominant cost of the walk. Pass the result to
    :func:`match_prepared_to_event`.
    """
    return _prepare(aggregate)


def match_prepared_to_event(
    prepared_aggregate: _Prepared,
    event_row: Mapping[str, Any],
    *,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> EventMatch:
    """Score a PRE-PREPARED cluster aggregate against one open-event row.

    Identical scoring to ``match_cluster_to_event(members, event_row,
    threshold=threshold, aggregate=aggregate)`` — same weights, same additive
    gates, same verdict — but the aggregate side's features are supplied
    already-prepared (:func:`prepare_aggregate`) rather than recomputed on
    every call. Only the event side, which genuinely changes every row, is
    freshly ``_prepare``d here.
    """
    event_like = _event_like(event_row, fallback_id=prepared_aggregate.row.id)
    return _score_prepared(
        prepared_aggregate,
        _prepare(event_like),
        threshold=threshold,
        embedding_cosine=None,
        embedding_threshold=DEFAULT_EMBEDDING_THRESHOLD,
        shared_fact_threshold=1,
    )


@dataclass(frozen=True)
class PreparedEvent:
    """One open event's pair-invariant features, folded ONCE PER TICK.

    ``row`` is the original event mapping, unchanged, so a caller reading
    ``category`` / ``geo`` / ``id`` off a matched result — the write path,
    the category/geo pre-filter — keeps reading exactly what it always has.
    P1g: on a cache hit ``row`` is still THIS call's mapping, never the one
    the bundle was folded from, so that stays true across ticks.
    ``prepared`` is the same :class:`_Prepared` bundle ``match_prepared_pair``
    compares against a prepared aggregate, with no ``_prepare`` call left on
    the event side at match time.
    """

    row: Mapping[str, Any]
    prepared: _Prepared


def _entity_folds_memoized(
    entity_names: Iterable[str],
    *,
    fold_cache: dict[str, str],
    junk_cache: dict[str, bool],
) -> frozenset[str]:
    """:attr:`EventEvidence.entity_folds`'s exact output, memoizing
    ``identity_fold``/``is_junk_entity`` by exact surface ACROSS the whole
    :func:`prepare_open_events` call.

    Those two calls are the DQ P4 canon (alias/demonym/region/plural
    collapse) — measured 2026-09-24 as the dominant cost of folding a
    1,000-event / 50-300-mention-per-event open-event set (~9-10 s
    uncached). A real vocabulary (Iran, Russia, the UN, ...) recurs across
    hundreds of open events in one tick, so caching by surface here is the
    difference between folding the SAME name once per tick and once per
    event that happens to carry it — this function changes nothing about
    WHAT a fold or a junk verdict is, only how many times it is computed.
    """
    result: set[str] = set()
    for name in entity_names:
        if not _text(name):
            continue
        junk = junk_cache.get(name)
        if junk is None:
            junk = is_junk_entity(name)
            junk_cache[name] = junk
        if junk:
            continue
        fold = fold_cache.get(name)
        if fold is None:
            fold = identity_fold(name)
            fold_cache[name] = fold
        if fold:
            result.add(fold)
    return frozenset(result)


def _probe_names_memoized(
    entity_names: Iterable[str],
    *,
    probe_cache: dict[str, _NameProbe],
) -> tuple[_NameProbe, ...]:
    """`_probe_names`'s exact output, memoizing `_probe_name` by exact
    surface ACROSS the whole :func:`prepare_open_events` call.

    P1f: the direction-probe tokens and non-direction stem cached here were
    previously re-tokenized on EVERY pairwise `_opposed_compass_stem` call —
    the dominant cost once the ROW-level ``has_direction`` gate stopped
    saving anything at live density (almost every row carries a directional
    place name: North Korea, South Sudan, West Bank, ...). The same real
    vocabulary that makes `_entity_folds_memoized` worth caching recurs
    across hundreds of open events in one tick, so caching the probe by
    surface here is the difference between probing the SAME name once per
    tick and once per event that happens to carry it — this changes nothing
    about what a probe IS, only how many times it is computed.
    """
    result: list[_NameProbe] = []
    for name in entity_names:
        if not _text(name):
            continue
        probe = probe_cache.get(name)
        if probe is None:
            probe = _probe_name(name)
            probe_cache[name] = probe
        result.append(probe)
    return tuple(result)


#: Default bound on the cross-tick prepared-event cache — the shipped
#: ``DEFAULT_MAX_EVENT_CANDIDATES``, i.e. the largest open set one tick can
#: fetch. The live open set is ~1,200 rows across both producers.
PREPARED_CACHE_LIMIT = 5000

#: The cross-tick cache itself: change-stamped row identity -> folded bundle,
#: in least-recently-used order (the oldest entry is the left end). The
#: clustering actor is single-activation, so ONE process owns this dict; a
#: second process simply builds its own, and no entry is ever shared, written
#: through or invalidated from outside — a row whose fold inputs changed
#: produces a different key and therefore a different entry.
_PREPARED_CACHE: "OrderedDict[tuple[Any, ...], _Prepared]" = OrderedDict()

#: Cumulative fold accounting since process start: ``hits + misses`` is the
#: number of rows :func:`prepare_open_events` has been handed, and ``misses``
#: is the number of folds it actually performed.
_PREPARED_CACHE_STATS: dict[str, int] = {"hits": 0, "misses": 0}

#: Every event-row column :func:`_fold_open_event` reads, and therefore every
#: column whose change must produce a different cache entry. ``id`` is here
#: because the folded bundle carries it (``_Prepared.row.id``); the rest are
#: the fold's actual inputs — ``title`` (normalized title), ``category`` and
#: ``geo`` (``category_terms``), ``entity_names`` (entity folds, name probes,
#: direction names), and the four timestamps ``latest_linked_at`` /
#: ``time_end`` / ``time_start`` / ``updated_at`` that `_event_like` falls
#: through for the evidence time. ``updated_at`` alone is NOT a sufficient
#: stamp: the handler's in-tick reattach refresh rewrites ``entity_names``,
#: ``geo`` and sometimes ``latest_linked_at`` on a local row copy without
#: touching ``updated_at``, and a new member link moves ``latest_linked_at``
#: without rewriting the event row at all. The projected columns the fold
#: never reads — ``event_signature``, ``signal_ids``, ``source_ids``,
#: ``entity_ids`` — are deliberately absent: they change without changing any
#: folded value, and a caller reads them off ``PreparedEvent.row``, which is
#: always this tick's row.
_PREPARED_CACHE_KEY_COLUMNS: tuple[str, ...] = (
    "id",
    "updated_at",
    "latest_linked_at",
    "time_start",
    "time_end",
    "title",
    "category",
    "geo",
    "entity_names",
)

#: The two sequence-valued key columns. Postgres hands both back as lists,
#: which do not hash; they are copied to tuples for the key. A bare string is
#: left ALONE rather than tupled — ``_lower_list`` reads a string as one value
#: while ``tuple("ir")`` would spell it ``("i", "r")`` and collide with the
#: genuinely two-element ``["i", "r"]``, which folds differently.
_PREPARED_CACHE_SEQUENCE_COLUMNS = frozenset({"geo", "entity_names"})


def _prepared_cache_key(row: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """This row's cache identity, or ``None`` when it cannot be a key.

    A row carrying an unhashable value in a fold-input column (a test row with
    a list of lists, say) simply misses forever rather than raising — the
    cache is an optimization, never a correctness dependency.
    """
    parts: list[Any] = []
    for column in _PREPARED_CACHE_KEY_COLUMNS:
        value = row.get(column)
        if column in _PREPARED_CACHE_SEQUENCE_COLUMNS and not isinstance(
            value, (str, bytes)
        ):
            try:
                value = tuple(value or ())
            except TypeError:
                return None
        parts.append(value)
    key = tuple(parts)
    try:
        hash(key)
    except TypeError:
        return None
    return key


def prepared_cache_stats() -> dict[str, int]:
    """Cumulative ``hits`` / ``misses`` for the cross-tick prepared cache."""
    return dict(_PREPARED_CACHE_STATS)


def reset_prepared_cache() -> None:
    """Empty the cache and zero its counters. Tests only — the live handler
    never needs this: a stale entry is unreachable, not wrong."""
    _PREPARED_CACHE.clear()
    _PREPARED_CACHE_STATS["hits"] = 0
    _PREPARED_CACHE_STATS["misses"] = 0


def _fold_open_event(
    row: Mapping[str, Any],
    *,
    fold_cache: dict[str, str],
    junk_cache: dict[str, bool],
    probe_cache: dict[str, _NameProbe],
) -> _Prepared:
    """One open-event row's pair-invariant bundle — the fold itself, with the
    per-call surface memoization :func:`prepare_open_events` owns."""
    event_like = _event_like(row, fallback_id=_NIL_EVENT_ID)
    name_probes = _probe_names_memoized(
        event_like.entity_names, probe_cache=probe_cache
    )
    direction_names = tuple(p for p in name_probes if p.has_direction)
    return _Prepared(
        row=event_like,
        folds=_entity_folds_memoized(
            event_like.entity_names,
            fold_cache=fold_cache,
            junk_cache=junk_cache,
        ),
        terms=event_like.category_terms,
        title=Dedupe4TierHandler.normalized_title(event_like.title),
        when=_utc(event_like.fetched_at),
        subjects=event_like.fact_subjects,
        embed_ok=valid_embedding_ref(event_like.embedding_ref),
        has_direction=bool(direction_names),
        name_probes=name_probes,
        direction_names=direction_names,
    )


def prepare_open_events(
    events: Iterable[Mapping[str, Any]],
    *,
    cache_limit: int = PREPARED_CACHE_LIMIT,
) -> list[PreparedEvent]:
    """Fold every open event's pair-invariant features ONCE PER TICK.

    P1e. ``match_prepared_to_event`` (P1d) eliminated the redundant
    re-``_prepare`` of the FIXED aggregate side on every (candidate, event)
    pair, but left the event side exactly where it was: ``_event_like`` +
    ``_prepare`` on every row, on every call — for the tower leg, up to 2,000
    preparations PER CANDIDATE. Live-profiled 2026-09-24 on the three
    candidates past the cursor (125/119/76 members): ``_match_event`` cost
    90.5 s / 106.4 s / 13.8 s with the member cap and category/geo
    pre-filter already engaged, because the open-event set — unchanged for
    the whole tick — was re-``_prepare``d fresh for every candidate that
    walked it. Fold it once here; every candidate then scores against the
    SAME prepared list via :func:`match_prepared_pair`, and the compare is
    pure arithmetic over two already-folded sides.

    The entity fold/junk lookups are memoized ACROSS this call (see
    :func:`_entity_folds_memoized`), and — P1f — so is each surface's
    direction-probe tokens / non-direction stem (see
    :func:`_probe_names_memoized`): the same surface recurring across many
    open events in one tick is folded and probed once, not once per event.
    Everything else ``_prepare`` computes (title normalization, evidence
    time, fact subjects, the embedding gate) is cheap enough that a plain
    per-row call is exact and simple.

    The event ``id`` never feeds a scored feature (folds/terms/title/when/
    subjects/embed_ok/has_direction/name_probes/direction_names all come
    from the row's other fields), so a caller-agnostic nil fallback is
    exact — this list scores byte-identical to preparing each event inline
    with any other fallback id.

    P1g (2026-09-24) carries the fold ACROSS ticks. The memoization above is
    per-call, so a tick that re-fetched an open set that had barely moved
    re-folded all of it: 4.1-4.7 s of a 60 s tower reserve per tick
    (``tower.prepare_seconds``, 993-1022 events), almost all of it spent
    re-deriving values identical to the previous tick's. Every column the
    fold reads is now a component of :data:`_PREPARED_CACHE_KEY_COLUMNS`, so
    a row whose fold inputs are unchanged reuses its bundle and a row that
    changed in ANY of them folds again — there is no staleness window to
    reason about and nothing to invalidate. ``row`` on the returned
    :class:`PreparedEvent` is always THIS call's mapping, hit or miss, so a
    caller reading ``category`` / ``geo`` / ``id`` off it still sees this
    tick's values. ``cache_limit`` bounds the cache to the
    caller's own ``max_open_events``, evicting least-recently-used;
    :func:`prepared_cache_stats` is the receipt's hit/miss source.
    """
    fold_cache: dict[str, str] = {}
    junk_cache: dict[str, bool] = {}
    probe_cache: dict[str, _NameProbe] = {}
    limit = max(0, int(cache_limit))
    prepared: list[PreparedEvent] = []
    for row in events:
        key = _prepared_cache_key(row)
        cached = _PREPARED_CACHE.get(key) if key is not None else None
        if cached is not None:
            _PREPARED_CACHE.move_to_end(key)
            _PREPARED_CACHE_STATS["hits"] += 1
            prepared.append(PreparedEvent(row=row, prepared=cached))
            continue
        _PREPARED_CACHE_STATS["misses"] += 1
        bundle = _fold_open_event(
            row,
            fold_cache=fold_cache,
            junk_cache=junk_cache,
            probe_cache=probe_cache,
        )
        if key is not None and limit > 0:
            _PREPARED_CACHE[key] = bundle
            _PREPARED_CACHE.move_to_end(key)
            while len(_PREPARED_CACHE) > limit:
                _PREPARED_CACHE.popitem(last=False)
        prepared.append(PreparedEvent(row=row, prepared=bundle))
    return prepared


def match_prepared_pair(
    prepared_aggregate: _Prepared,
    prepared_event: _Prepared,
    *,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> EventMatch:
    """Score two PRE-PREPARED sides — the aggregate AND the event.

    P1e's compare: arithmetic over two already-folded feature bundles, with
    no ``_prepare`` call on either side. :func:`match_prepared_to_event`
    stays for a caller that has only prepared the aggregate;
    :func:`prepare_open_events` + this function is the door for a caller
    that has also prepared the event side once, ahead of the walk.
    """
    return _score_prepared(
        prepared_aggregate,
        prepared_event,
        threshold=threshold,
        embedding_cosine=None,
        embedding_threshold=DEFAULT_EMBEDDING_THRESHOLD,
        shared_fact_threshold=1,
    )


__all__ = [
    "DEFAULT_EMBEDDING_THRESHOLD",
    "DEFAULT_MATCH_THRESHOLD",
    "DEFAULT_PAIR_BLOCK_WINDOW_HOURS",
    "EVENT_CLUSTERING_VERSION",
    "EventCluster",
    "EventEvidence",
    "EventMatch",
    "MAX_CLUSTER_MEMBERS",
    "PreparedEvent",
    "aggregate_category",
    "aggregate_entities",
    "aggregate_polity",
    "cluster_aggregate",
    "cluster_evidence",
    "earliest_evidence_at",
    "latest_evidence_at",
    "match_cluster_to_event",
    "match_prepared_pair",
    "match_prepared_to_event",
    "prepare_aggregate",
    "prepare_open_events",
    "prepared_cache_stats",
    "reset_prepared_cache",
    "score_pair",
    "valid_embedding_ref",
]
