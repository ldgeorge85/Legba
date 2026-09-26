# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P1 event candidate collection and materialization helpers.

This leaf normalizes three input shapes into one evidence model:

* live ``signals`` rows, enriched with canonical entity links and source-class
  metadata;
* synthetic signal dictionaries used by unit tests;
* bounded tower candidates — verify-passed findings and evidence-bearing
  ``situation_events`` rows.

It deliberately does not write. ``event_clustering.py`` owns transactions and
calls ``provenance.write_event`` for every event row.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from uuid import UUID

from ... import critic_fold
from ...provenance.models import EventEntityLinkPayload, EventSignalLinkPayload
from ._event_matcher import (
    EventCluster,
    EventEvidence,
    aggregate_category,
    aggregate_entities,
    aggregate_polity,
    earliest_evidence_at,
    latest_evidence_at,
)

_SOURCE_CLASSES = {"reporting", "analysis", "official", "state_media"}
_EVENT_ENTITY_ROLES = {
    "actor", "target", "location", "observer", "victim", "mediator"
}
_SEVERITIES = {"critical", "high", "medium", "low", "routine"}
_EVENT_TYPES = {"incident", "development", "shift", "threshold"}
_TOWER_PRODUCER_ID = "tower_backfill"


def _utc(value: Any) -> datetime | None:
    """Coerce a datetime / ISO string to aware UTC."""
    if isinstance(value, datetime):
        dt = value
    elif value:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    else:
        return None
    return (
        dt.replace(tzinfo=timezone.utc)
        if dt.tzinfo is None
        else dt.astimezone(timezone.utc)
    )


def _uuid(value: Any) -> UUID | None:
    """Coerce ``value`` to UUID, returning ``None`` on malformed input."""
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _text(value: Any) -> str:
    """Normalize a scalar to a stripped string."""
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    """Return a mapping, decoding a JSON object string when necessary."""
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, Mapping) else {}
    return {}


def _strings(value: Any) -> tuple[str, ...]:
    """Normalize a scalar-or-list field to a deduped string tuple."""
    if value is None:
        return ()
    seq = (value,) if isinstance(value, (str, bytes)) else value
    try:
        return tuple(sorted({_text(v) for v in seq if _text(v)}))
    except TypeError:
        return ()


def _payload_field(row: Mapping[str, Any], *names: str) -> Any:
    """Read the first non-empty scalar from a signal payload / row."""
    payload = _mapping(row.get("payload"))
    for source in (row, payload):
        for name in names:
            value = source.get(name)
            if value not in (None, "", [], {}):
                return value
    return None


def _source_class(value: Any) -> str:
    """Normalize the denormalized source class to the link CHECK vocabulary."""
    v = _text(value).lower()
    return v if v in _SOURCE_CLASSES else "reporting"


def _entity_links(row: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Normalize entity link detail carried by a signal row."""
    raw = row.get("entity_links")
    if raw is None:
        raw = row.get("entities")
    if raw is None:
        raw = _mapping(row.get("payload")).get("entities")
    out: list[dict[str, Any]] = []
    for item in raw or ():
        if isinstance(item, str):
            item = {"canonical_name": item}
        if not isinstance(item, Mapping):
            continue
        entity_id = _uuid(
            item.get("entity_id") or item.get("id") or item.get("resolved_entity_id")
        )
        name = _text(
            item.get("canonical_name")
            or item.get("name")
            or item.get("surface")
            or item.get("entity")
        )
        if entity_id is None and not name:
            continue
        out.append({
            "entity_id": entity_id,
            "canonical_name": name,
            "role": _text(item.get("role") or "actor"),
            "confidence": float(item.get("confidence") or 0.8),
        })
    return tuple(out)


def normalize_signal(row: Mapping[str, Any]) -> EventEvidence | None:
    """Normalize one signal-shaped row into ``EventEvidence``.

    Returns ``None`` when the row has no resolvable id — a candidate without an
    id cannot become ``signal_event_links`` evidence and would only make a
    synthetic event look real.
    """
    signal_id = _uuid(row.get("id") or row.get("signal_id"))
    if signal_id is None:
        return None
    links = _entity_links(row)
    entity_ids = tuple(
        x["entity_id"] for x in links if x.get("entity_id") is not None
    )
    declared_ids = tuple(
        x for x in (_uuid(v) for v in row.get("entity_ids") or ()) if x
    )
    entity_names = tuple(
        x["canonical_name"] for x in links if x.get("canonical_name")
    ) or _strings(
        row.get("entity_names")
        or _mapping(row.get("payload")).get("entity_names")
        or _mapping(row.get("payload")).get("entities_resolved")
    )
    fetched_at = _utc(
        row.get("fetched_at")
        or row.get("event_timestamp")
        or _payload_field(row, "event_timestamp", "published_at")
    )
    confidence = row.get("confidence")
    if confidence is None:
        confidence = row.get("source_credibility")
    if confidence is None:
        confidence = _payload_field(row, "confidence")
    try:
        conf = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        conf = 0.5
    extra = dict(_mapping(row.get("extra")))
    extra["entity_links"] = links
    return EventEvidence(
        id=signal_id,
        title=_text(
            row.get("title")
            or _payload_field(row, "title", "headline", "name")
        ),
        category=_text(
            row.get("category")
            or _payload_field(row, "category", "topic", "situation_kind")
        ).lower(),
        tags=_strings(row.get("tags")),
        fetched_at=fetched_at,
        source_id=_text(row.get("source_id")),
        source_class=_source_class(row.get("source_class")),
        source_kind=_text(row.get("source_kind")),
        geo=_strings(row.get("geo") or _mapping(row.get("payload")).get("geo")),
        entity_ids=tuple(sorted(set(entity_ids + declared_ids), key=str)),
        entity_names=entity_names,
        embedding_ref=_text(row.get("embedding_ref")),
        confidence=conf,
        candidate_kind=_text(row.get("candidate_kind") or "signal"),
        source_ref_id=_uuid(row.get("source_ref_id") or row.get("source_ref")),
        fact_subjects=frozenset(_strings(row.get("fact_subjects"))),
        extra=extra,
    )


@dataclass(frozen=True)
class EventCandidate:
    """A promote-ready or declined event candidate."""

    kind: str
    members: tuple[EventEvidence, ...]
    event_signature: str
    title: str
    category: str
    event_type: str
    severity: str
    time_start: datetime | None
    time_end: datetime | None
    geo: tuple[str, ...]
    confidence: float
    oversized: bool
    promoted: bool
    decline_reason: str = ""
    source_method: str = "clustering"
    source_ref_id: UUID | None = None
    lineage: tuple[UUID, ...] = ()
    data: dict[str, Any] = field(default_factory=dict)
    #: P1b — the candidate's position in its source ordering, used as the
    #: resume bound when a budget-truncated run stops mid-write. Tower legs
    #: set it to the source row's (produced_at|occurred_at, id); cluster
    #: candidates fall back to their oldest member position.
    cursor_ts: datetime | None = None
    cursor_id: UUID | None = None

    @property
    def cursor_position(self) -> tuple[datetime, UUID] | None:
        """The exclusive/inclusive resume position for this candidate's leg."""
        if self.cursor_ts is not None and self.cursor_id is not None:
            return (self.cursor_ts, self.cursor_id)
        positions = [
            (m.fetched_at, m.id) for m in self.members
            if m.fetched_at is not None
        ]
        return min(positions) if positions else None

    @property
    def signal_ids(self) -> list[UUID]:
        """All resolvable signal ids in stable order."""
        return [m.id for m in sorted(self.members, key=lambda x: str(x.id))]

    @property
    def distinct_source_count(self) -> int:
        """Distinct non-empty ``source_id`` values across members."""
        return len({m.source_id for m in self.members if m.source_id})

    @property
    def derived_from(self) -> list[UUID]:
        """The event's lineage: signals first, then producer/source refs."""
        seen: set[UUID] = set()
        out: list[UUID] = []
        for value in list(self.signal_ids) + list(self.lineage):
            if value not in seen:
                seen.add(value)
                out.append(value)
        return out


def _event_type(members: Iterable[EventEvidence]) -> str:
    """Modal declared event type, defaulting to ``incident``."""
    counts: Counter[str] = Counter()
    for member in members:
        value = _text(member.extra.get("event_type")).lower()
        if value in _EVENT_TYPES:
            counts[value] += 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if counts else "incident"


def _severity(members: Iterable[EventEvidence]) -> str:
    """Modal declared severity, with deterministic order."""
    counts: Counter[str] = Counter()
    for member in members:
        values = [_text(member.extra.get("severity")).lower()]
        values.extend(
            t[9:] for t in member.tags
            if t.startswith("severity:") and t[9:] in _SEVERITIES
        )
        counts.update(v for v in values if v in _SEVERITIES)
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if counts else "medium"


def cluster_candidate(cluster: EventCluster) -> EventCandidate:
    """Apply the promotion bar and mint a signature for a signal cluster."""
    from ...events.signature import event_signature

    members = cluster.members
    title = ""
    latest = max(
        members,
        key=lambda m: (
            # ``tz=`` is not a ``datetime.replace`` keyword — the fallback
            # raised TypeError for any member with no ``fetched_at``, which is
            # exactly when it is reached. P1c fixes it to ``tzinfo=``.
            _utc(m.fetched_at) or datetime.min.replace(tzinfo=timezone.utc),
            str(m.id),
        ),
        default=None,
    )
    if latest is not None:
        title = latest.title
    if not title:
        title = "Unnamed event"
    category = aggregate_category(members)
    entity_names = aggregate_entities(members)
    signature = event_signature(
        category,
        entity_names,
        aggregate_polity(members),
    )
    promoted = False
    reason = ""
    if cluster.oversized:
        reason = "oversized"
    elif signature is None:
        reason = "signature_unmintable"
    elif len({m.source_id for m in members if m.source_id}) >= 2:
        promoted = True
    elif any(m.source_class == "official" for m in members):
        promoted = True
    else:
        reason = "single_source"
    conf_values = [m.confidence for m in members]
    confidence = sum(conf_values) / len(conf_values) if conf_values else 0.5
    return EventCandidate(
        kind="cluster",
        members=tuple(members),
        event_signature=signature or f"evt:unminted-{latest.id if latest else 'none'}",
        title=title[:2048],
        category=category[:256],
        event_type=_event_type(members),
        severity=_severity(members),
        time_start=earliest_evidence_at(members),
        time_end=latest_evidence_at(members),
        geo=tuple(sorted({g.lower() for m in members for g in m.geo if g})),
        confidence=max(0.0, min(1.0, confidence)),
        oversized=cluster.oversized,
        promoted=promoted,
        decline_reason=reason,
        source_method="clustering",
        data={"member_count": len(members)},
    )


def tower_candidate(
    *,
    kind: str,
    source_ref_id: UUID,
    title: str,
    topic: str,
    category: str,
    occurred_at: datetime | None,
    lineage: Iterable[Any],
    members: Iterable[EventEvidence],
    confidence: float = 0.5,
    data: Mapping[str, Any] | None = None,
    cursor_ts: datetime | None = None,
    cursor_id: UUID | None = None,
) -> EventCandidate | None:
    """Build a tower candidate, returning ``None`` when it cannot be signed."""
    from ...events.signature import event_signature

    items = tuple(members)
    names = aggregate_entities(items)
    signature = event_signature(topic or category, names, aggregate_polity(items))
    if signature is None:
        return None
    lineage_ids = tuple(
        x for x in (_uuid(v) for v in lineage) if x is not None
    )
    return EventCandidate(
        kind=kind,
        members=items,
        event_signature=signature,
        title=(_text(title) or signature)[:2048],
        category=_text(category or topic).lower()[:256],
        event_type="development" if kind == "situation_event" else "incident",
        severity="medium",
        time_start=earliest_evidence_at(items) or occurred_at,
        time_end=latest_evidence_at(items) or occurred_at,
        geo=tuple(sorted({g.lower() for m in items for g in m.geo if g})),
        confidence=max(0.0, min(1.0, confidence)),
        oversized=False,
        promoted=True,
        source_method="tower",
        source_ref_id=source_ref_id,
        lineage=lineage_ids,
        data=dict(data or {}),
        cursor_ts=cursor_ts,
        cursor_id=cursor_id,
    )


def signal_links_for(candidate: EventCandidate) -> list[EventSignalLinkPayload]:
    """Payload link rows for one candidate, preserving evidence time."""
    links: list[EventSignalLinkPayload] = []
    for member in candidate.members:
        if member.fetched_at is None:
            continue
        links.append(EventSignalLinkPayload(
            signal_id=member.id,
            linked_at=member.fetched_at,
            relevance=1.0,
            source_class=_source_class(member.source_class),
            source_kind=member.source_kind[:128],
            source_id=member.source_id[:256],
        ))
    return links


def _event_role(role: Any) -> str:
    """Map a signal link role onto the event-actor role vocabulary."""
    r = _text(role).lower()
    if r in _EVENT_ENTITY_ROLES:
        return r
    return "location" if r in {"place", "geo"} else "actor"


def entity_links_for(candidate: EventCandidate) -> list[EventEntityLinkPayload]:
    """Payload actor links from the members' resolved entity links."""
    best: dict[tuple[UUID, str], EventEntityLinkPayload] = {}
    for member in candidate.members:
        for raw in member.extra.get("entity_links") or ():
            entity_id = _uuid(raw.get("entity_id"))
            if entity_id is None:
                continue
            role = _event_role(raw.get("role"))
            key = (entity_id, role)
            conf = max(0.0, min(1.0, float(raw.get("confidence") or 0.5)))
            prior = best.get(key)
            derived = sorted({*(prior.derived_from if prior else ()), member.id}, key=str)
            best[key] = EventEntityLinkPayload(
                entity_id=entity_id,
                role=role,  # type: ignore[arg-type]
                confidence=max(conf, prior.confidence if prior else 0.0),
                derived_from=derived,
            )
    return [best[k] for k in sorted(best, key=lambda x: (str(x[0]), x[1]))]


#: P1b — the slice resumes from a (fetched_at, id) watermark when one is
#: stored ($3/$4); without one the lookback is the backstop (the
#: situation_tracker idiom: a watermark, however old, is honored as-is and
#: only an absent one falls back to the window).
_SIGNAL_SLICE_SQL = """
    SELECT s.id, s.source_id, s.fetched_at, s.payload, s.geo, s.tags,
           s.source_credibility, s.embedding_ref,
           COALESCE(sd.kind, '') AS source_kind,
           CASE WHEN sd.body->'scope'->>'source_class' IN
                     ('reporting','analysis','official','state_media')
                THEN sd.body->'scope'->>'source_class'
                ELSE 'reporting' END AS source_class
      FROM signals s
      LEFT JOIN source_descriptors sd
        ON sd.descriptor_id = s.source_id AND sd.is_head
     WHERE s.modality = 'text'
       AND (s.canonical_signal_id IS NULL OR s.canonical_signal_id = s.id)
       AND (
            ($3::timestamptz IS NULL AND s.fetched_at > $1)
         OR ($3::timestamptz IS NOT NULL
             AND (s.fetched_at, s.id) > ($3::timestamptz, $4::uuid))
       )
     ORDER BY s.fetched_at ASC, s.id ASC
     LIMIT $2
"""


_SIGNAL_ENTITIES_SQL = """
    SELECT sel.signal_id, public.resolve_entity(sel.entity_id) AS entity_id,
           ep.canonical_name, sel.role, sel.confidence
      FROM signal_entity_links sel
      JOIN entity_profiles ep
        ON ep.id = public.resolve_entity(sel.entity_id)
     WHERE sel.signal_id = ANY($1::uuid[])
     ORDER BY sel.signal_id, ep.canonical_name
"""


#: P1c: the ``f.derived_from && $1`` clause is a GIN-index PRE-FILTER, not a
#: second predicate — the row-level test stays on the unnested ``ref.signal_id``
#: and the result set is unchanged (proved row-identical on the live table,
#: 185 = 185 rows, zero difference either direction). Without it Postgres can
#: only seq-scan ``facts`` (157,516 rows, ~23,700 buffers) because the filter
#: sits on the LATERAL's output rather than on the indexed array column.
#: Measured 2026-09-23 on one live tower candidate: 468 ms -> 11.9 ms.
_SIGNAL_FACT_SUBJECTS_SQL = """
    SELECT ref.signal_id, f.subject
      FROM facts f
      JOIN LATERAL unnest(f.derived_from) AS ref(signal_id) ON TRUE
     WHERE f.derived_from && $1::uuid[]
       AND ref.signal_id = ANY($1::uuid[])
       AND btrim(f.subject) <> ''
     ORDER BY ref.signal_id, f.subject
"""


_SIGNAL_BY_ID_SQL = """
    SELECT s.id, s.source_id, s.fetched_at, s.payload, s.geo, s.tags,
           s.source_credibility, s.embedding_ref,
           COALESCE(sd.kind, '') AS source_kind,
           CASE WHEN sd.body->'scope'->>'source_class' IN
                     ('reporting','analysis','official','state_media')
                THEN sd.body->'scope'->>'source_class'
                ELSE 'reporting' END AS source_class
      FROM signals s
      LEFT JOIN source_descriptors sd
        ON sd.descriptor_id = s.source_id AND sd.is_head
     WHERE s.id = ANY($1::uuid[])
       AND s.modality = 'text'
     ORDER BY s.fetched_at ASC, s.id ASC
"""


# H17 — SET-BASED. The watermark/cursor predicate bounds the outer CTE; ONE
# `DISTINCT ON` pass reads those ids' critiques through the expression index.
_FINDING_CANDIDATES_SQL = f"""
    WITH f AS MATERIALIZED (
        SELECT f.id, f.title, f.produced_at, f.confidence, f.data
          FROM analyst_outputs f
         WHERE f.kind = 'finding'
           AND f.superseded_by IS NULL
           AND (
                ($4::timestamptz IS NULL AND f.produced_at > $1)
             OR ($4::timestamptz IS NOT NULL
                 AND (f.produced_at, f.id) > ($4::timestamptz, $5::uuid))
           )
    ), {critic_fold.faithfulness_score_cte()}
    SELECT f.id, f.title, f.produced_at, f.confidence, f.data,
           LEAST(f.confidence, v.faithfulness_score) AS effective_confidence
      FROM f
      JOIN v ON v.fid = f.id::text
     WHERE LEAST(f.confidence, v.faithfulness_score) >= $2
     ORDER BY f.produced_at ASC, f.id ASC
     LIMIT $3
"""


_SITUATION_CANDIDATES_SQL = """
    SELECT se.id, se.situation_id, se.occurred_at, se.delta, se.why,
           se.derived_from, s.name, s.category, s.intensity_score
      FROM situation_events se
      JOIN situations s ON s.id = se.situation_id
     WHERE se.delta <> 'unchanged_checkpoint'
       AND se.derived_from <> '{}'::uuid[]
       AND (
            ($3::timestamptz IS NULL AND se.occurred_at > $1)
         OR ($3::timestamptz IS NOT NULL
             AND (se.occurred_at, se.id) > ($3::timestamptz, $4::uuid))
       )
     ORDER BY se.occurred_at ASC, se.id ASC
     LIMIT $2
"""


#: P1c — the one-hop expansion, keyed BY PARENT so a whole page of candidates
#: can be expanded in one round trip and still be attributed back to the
#: candidate that asked. ``_EXPAND_REFS_SQL``'s ``DISTINCT next_ref`` shape
#: could only ever serve one candidate at a time.
_EXPAND_REFS_BY_PARENT_SQL = """
    SELECT ao.id AS parent, unnest(ao.derived_from) AS next_ref
      FROM analyst_outputs ao WHERE ao.id = ANY($1::uuid[])
    UNION ALL
    SELECT fx.id AS parent, unnest(fx.derived_from) AS next_ref
      FROM facts fx WHERE fx.id = ANY($1::uuid[])
"""


async def _enrich_signal_evidence(
    conn: Any, evidence: list[EventEvidence]
) -> list[EventEvidence]:
    """Attach resolved entities and fact subjects to normalized signals."""
    if not evidence:
        return []
    signal_ids = [e.id for e in evidence]
    links = await conn.fetch(_SIGNAL_ENTITIES_SQL, signal_ids)
    by_signal: dict[UUID, list[dict[str, Any]]] = {}
    for row in links:
        sid = _uuid(row["signal_id"])
        if sid is not None:
            by_signal.setdefault(sid, []).append(dict(row))
    subjects: dict[UUID, set[str]] = {}
    for row in await conn.fetch(_SIGNAL_FACT_SUBJECTS_SQL, signal_ids):
        sid = _uuid(row["signal_id"])
        if sid is not None:
            subjects.setdefault(sid, set()).add(_text(row["subject"]).lower())
    out: list[EventEvidence] = []
    for item in evidence:
        details = tuple(by_signal.get(item.id, ()))
        names = tuple(
            _text(x.get("canonical_name")) for x in details if x.get("canonical_name")
        )
        ids = tuple(sorted({
            x for x in (_uuid(d.get("entity_id")) for d in details)
            if x is not None
        }, key=str))
        extra = dict(item.extra)
        extra["entity_links"] = details
        out.append(replace(
            item,
            entity_ids=ids,
            entity_names=names,
            fact_subjects=frozenset(item.fact_subjects | subjects.get(item.id, set())),
            extra=extra,
        ))
    return out


async def fetch_signal_evidence(
    conn: Any,
    *,
    since: datetime,
    max_signals: int,
    cursor: tuple[datetime, UUID] | None = None,
) -> list[EventEvidence]:
    """Fetch the bounded canonical signal slice plus evidence features.

    ``cursor`` is the (fetched_at, id) watermark the last tick stopped at;
    when present it — not ``since`` — is the resume bound.
    """
    rows = await conn.fetch(
        _SIGNAL_SLICE_SQL,
        since,
        int(max_signals),
        cursor[0] if cursor else None,
        cursor[1] if cursor else None,
    )
    evidence = [x for x in (normalize_signal(r) for r in rows) if x is not None]
    return await _enrich_signal_evidence(conn, evidence)


async def resolve_candidate_signals_batch(
    conn: Any,
    lineages: Mapping[Any, Iterable[Any]],
) -> dict[Any, list[EventEvidence]]:
    """Resolve MANY candidates' lineages to live signals in one round.

    P1c. The per-candidate path cost four round trips each, and one of them —
    the fact-subject enrichment — seq-scanned ``facts`` every time (measured
    468 ms per candidate on the live table, so ~23 s of a 120 s turn for a
    50-candidate page before any write happened). This resolves the whole page
    in a fixed four queries regardless of page size: one parent-keyed
    expansion, one signal fetch, and the two enrichment queries inside a single
    :func:`_enrich_signal_evidence` call.

    Returns one list per input key, each ordered by ``(fetched_at, id)`` —
    the same order ``_SIGNAL_BY_ID_SQL`` produced per candidate, so a batched
    page and a per-candidate walk yield identical members.
    """
    refs_by_key: dict[Any, set[UUID]] = {
        key: {x for x in (_uuid(v) for v in lineage) if x is not None}
        for key, lineage in lineages.items()
    }
    all_refs = {ref for refs in refs_by_key.values() for ref in refs}
    if not all_refs:
        return {key: [] for key in refs_by_key}

    hops: dict[UUID, set[UUID]] = {}
    for row in await conn.fetch(_EXPAND_REFS_BY_PARENT_SQL, sorted(all_refs)):
        parent = _uuid(row["parent"])
        ref = _uuid(row["next_ref"])
        if parent is not None and ref is not None:
            hops.setdefault(parent, set()).add(ref)

    expanded_by_key: dict[Any, set[UUID]] = {}
    for key, refs in refs_by_key.items():
        expanded = set(refs)
        for ref in refs:
            expanded |= hops.get(ref, set())
        expanded_by_key[key] = expanded

    wanted = {rid for ids in expanded_by_key.values() for rid in ids}
    rows = await conn.fetch(_SIGNAL_BY_ID_SQL, sorted(wanted))
    members = [x for x in (normalize_signal(r) for r in rows) if x]
    # ``_SIGNAL_BY_ID_SQL`` already returns (fetched_at, id) order, and the
    # enrichment preserves input order, so slicing by membership below keeps
    # each candidate's per-candidate ordering without a re-sort.
    enriched = await _enrich_signal_evidence(conn, members)
    return {
        key: [item for item in enriched if item.id in ids]
        for key, ids in expanded_by_key.items()
    }


async def _resolve_candidate_signals(
    conn: Any,
    lineage: Iterable[Any],
) -> list[EventEvidence]:
    """Resolve a candidate's refs plus one output/fact hop to live signals."""
    resolved = await resolve_candidate_signals_batch(conn, {"one": lineage})
    return resolved["one"]


def _topic_from_data(data: Mapping[str, Any]) -> str:
    """The backfill's topic precedence, over an analyst-output payload."""
    inner = data.get("data")
    sources = (data, inner) if isinstance(inner, Mapping) else (data,)
    for source in sources:
        for key in ("category", "topic", "situation_kind", "event_type"):
            value = source.get(key)
            if _text(value):
                return _text(value).lower()
    return ""


async def fetch_tower_candidates(
    conn: Any,
    *,
    since: datetime,
    floor: float,
    max_candidates: int,
    findings_cursor: tuple[datetime, UUID] | None = None,
    situations_cursor: tuple[datetime, UUID] | None = None,
) -> tuple[list[EventCandidate], dict[str, int], dict[str, Any]]:
    """Fetch bounded verify-passed finding and situation-events candidates.

    P1b: each leg resumes from its own (ts, id) watermark — findings order by
    ``produced_at``, situation_events by ``occurred_at`` — so a truncated tick
    continues instead of restarting. The third return value carries each
    leg's consumed tail position (``None`` when the leg did not run or saw no
    rows) and whether the leg's page was full.

    P1c: both legs' rows are fetched FIRST and the whole page's lineage is
    resolved in one batched round (:func:`resolve_candidate_signals_batch`).
    The situations leg's ``remaining`` budget is unchanged — it is still what
    the findings leg left over — so the page's composition is identical; only
    the number of round trips changed.
    """
    candidates: list[EventCandidate] = []
    counters = Counter()
    tails: dict[str, Any] = {
        "findings": None,
        "situations": None,
        "findings_ran": True,
        "situations_ran": False,
        "findings_full": False,
        "situations_full": False,
    }
    findings = await conn.fetch(
        _FINDING_CANDIDATES_SQL,
        since,
        float(floor),
        int(max_candidates),
        findings_cursor[0] if findings_cursor else None,
        findings_cursor[1] if findings_cursor else None,
    )
    tails["findings_full"] = len(findings) >= int(max_candidates)

    remaining = max(0, int(max_candidates) - len(findings))
    if remaining:
        situations = await conn.fetch(
            _SITUATION_CANDIDATES_SQL,
            since,
            remaining,
            situations_cursor[0] if situations_cursor else None,
            situations_cursor[1] if situations_cursor else None,
        )
    else:
        situations = []

    # P1c — ONE resolution round for the whole page (both legs), keyed by the
    # source row id, instead of four round trips per candidate.
    lineages: dict[Any, list[Any]] = {
        ("finding", row["id"]): [row["id"]] for row in findings
    }
    lineages.update({
        ("situation_event", row["id"]):
            list(row.get("derived_from") or ()) + [row["id"]]
        for row in situations
    })
    resolved = await resolve_candidate_signals_batch(conn, lineages)

    for row in findings:
        data = _mapping(row.get("data"))
        lineage = [row["id"]]
        members = resolved.get(("finding", row["id"]), [])
        counters["tower_findings_examined"] += 1
        tails["findings"] = (_utc(row["produced_at"]), row["id"])
        if len(members) < 2:
            counters["tower_parked_insufficient_signals"] += 1
            continue
        cand = tower_candidate(
            kind="finding",
            source_ref_id=row["id"],
            title=_text(row.get("title")),
            topic=_topic_from_data(data),
            category=_topic_from_data(data),
            occurred_at=_utc(row.get("produced_at")),
            lineage=lineage,
            members=members,
            confidence=float(row.get("effective_confidence") or 0.5),
            data={"tower_path": "finding", "finding_id": str(row["id"])},
            cursor_ts=_utc(row.get("produced_at")),
            cursor_id=row["id"],
        )
        if cand is None:
            counters["tower_parked_signature_unmintable"] += 1
            continue
        candidates.append(cand)

    tails["situations_ran"] = remaining > 0
    tails["situations_full"] = bool(situations) and len(situations) >= remaining
    for row in situations:
        lineage = list(row.get("derived_from") or ()) + [row["id"]]
        members = resolved.get(("situation_event", row["id"]), [])
        counters["tower_situations_examined"] += 1
        tails["situations"] = (_utc(row["occurred_at"]), row["id"])
        if not members:
            counters["tower_parked_insufficient_signals"] += 1
            continue
        cand = tower_candidate(
            kind="situation_event",
            source_ref_id=row["id"],
            title=_text(row.get("why")) or _text(row.get("name")),
            topic=_text(row.get("category")) or _text(row.get("delta")),
            category=_text(row.get("category")),
            occurred_at=_utc(row.get("occurred_at")),
            lineage=lineage,
            members=members,
            confidence=0.7,
            data={
                "tower_path": "situation_events",
                "situation_event_id": str(row["id"]),
                "situation_id": str(row["situation_id"]),
                "delta": row.get("delta"),
            },
            cursor_ts=_utc(row.get("occurred_at")),
            cursor_id=row["id"],
        )
        if cand is None:
            counters["tower_parked_signature_unmintable"] += 1
            continue
        candidates.append(cand)
    counters["tower_candidates"] = len(candidates)
    return candidates, dict(counters), tails


def tower_producer_id() -> str:
    """Producer id for tower-derived events (the 0205 producer)."""
    return _TOWER_PRODUCER_ID


__all__ = [
    "EventCandidate",
    "cluster_candidate",
    "entity_links_for",
    "fetch_signal_evidence",
    "fetch_tower_candidates",
    "normalize_signal",
    "resolve_candidate_signals_batch",
    "signal_links_for",
    "tower_candidate",
    "tower_producer_id",
]
