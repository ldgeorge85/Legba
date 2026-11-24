# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``inspect_entity`` — the body, extracted from the port (lane E2).

The port sat near its size ceiling, and E2 gave this reader a ``known_at``
record-time cut (classified in :mod:`legba.data.raw_reader_known_at`), so the
body moved here the way ``search_signals`` moved to ``substrate_signal_search``
in E0. Without ``known_at`` every statement is byte-identical to the port's
(sha-pinned in ``tests/data_pkg/test_record_reader_known_at.py``).

Under ``known_at`` K: the profile must have entered the record before K (else
``found=False``); versions, signal mentions (the link AND the signal) and facts
are those recorded before K, the facts read by validity at K (a fact closed
after K held at K); ``version`` is the newest version row before K, the moving
profile fields with no ledger (``completeness_score``) are masked, and every
profile timestamp after K is masked. Name, type and geo are today's (declared
in the classification module).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..data.provenance import origin as _origin
from . import substrate_record_known_at as _record_cut
from .substrate_known_at import recorded_before_sql

__all__ = [
    "INSPECT_RECENT_FACTS",
    "INSPECT_RECENT_SIGNAL_MENTIONS",
    "INSPECT_RECENT_VERSIONS",
    "inspect_entity",
]

# Default number of recent signal mentions ``inspect_entity`` joins in.
INSPECT_RECENT_SIGNAL_MENTIONS = 10
# Default number of entity_profile_versions rows surfaced by inspect_entity.
INSPECT_RECENT_VERSIONS = 5
# Default number of current facts (keyed by subject) surfaced by inspect_entity.
INSPECT_RECENT_FACTS = 30

_PROFILE_TIME_FIELDS = ("last_event_link_at", "last_verified_at", "produced_at")


def _iso(v: Any) -> str | None:
    return v.isoformat() if isinstance(v, datetime) else None


async def inspect_entity(
    pool: Any, *, name: str, known_at: datetime | None = None,
) -> dict[str, Any]:
    """The ``inspect_entity`` body — see the port method's docstring."""
    n = (name or "").strip()
    if not n:
        return {
            "entity": name,
            "found": False,
            "facts": [],
            "versions": [],
            "recent_signal_mentions": [],
            "refs": [],
            "error": "name must be non-empty",
        }
    k = known_at is not None
    kp: tuple[Any, ...] = (known_at,) if k else ()

    async with pool.acquire() as conn:
        profile = await conn.fetchrow(
            """
                SELECT id, canonical_name, entity_type, entity_class,
                       version, completeness_score, last_event_link_at,
                       last_verified_at, geo_country, geo_region,
                       produced_at, analyst_id, target_id
                FROM entity_profiles
                WHERE LOWER(canonical_name) = LOWER($1)
                  -- E5: a merged loser is a tombstone, not a live entity (its
                  -- surface is now an alias of the keeper). Exclude it so a
                  -- merged fragment can't surface as a separate entity.
                  AND merged_into IS NULL
                """ + (f"  AND {recorded_before_sql('created_at', 2)}\n" if k else ""),
            n, *kp,
        )
        if profile is None:
            out = {
                "entity": name,
                "found": False,
                "facts": [],
                "versions": [],
                "recent_signal_mentions": [],
                "refs": [],
            }
            return _record_cut.stamp(out, known_at, how="entity_profiles.created_at") if k else out

        entity_id = profile["id"]
        version_rows = await conn.fetch(
            """
                SELECT id, version, cycle_number, analyst_id, created_at
                FROM entity_profile_versions
                WHERE entity_id = $1
                """ + (f"AND {recorded_before_sql('created_at', 3)}\n                " if k else "")
            + """ORDER BY version DESC, created_at DESC
                LIMIT $2
                """,
            entity_id,
            INSPECT_RECENT_VERSIONS,
            *kp,
        )
        mention_rows = await conn.fetch(
            """
                SELECT sel.signal_id, sel.role, sel.confidence, sel.created_at,
                       s.payload->>'title' AS title,
                       s.payload->>'title_en' AS title_en,
                       s.payload->>'category' AS category,
                       s.fetched_at AS signal_produced_at
                FROM signal_entity_links sel
                LEFT JOIN signals s ON s.id = sel.signal_id
                WHERE sel.entity_id = $1
                """ + (
                f"AND {recorded_before_sql('sel.created_at', 3)}"
                f" AND {recorded_before_sql('s.created_at', 3)}\n                " if k else "")
            + """ORDER BY sel.created_at DESC
                LIMIT $2
                """,
            entity_id,
            INSPECT_RECENT_SIGNAL_MENTIONS,
            *kp,
        )
        # Current facts about this entity, keyed by subject = canonical
        # name (the same enumerate-via-subject convention the prior
        # facts_note pointed callers at).  Gated to OPEN rows only —
        # the 0032 pair plus the P7 origin-class leg (live_gate_sql)
        # — so inspect_entity never surfaces a replaced/expired/history fact.
        # Under known_at: the facts that held at K by validity, recorded
        # before K, live classes only.
        fact_gate = (
            f"{recorded_before_sql('created_at', 3)}\n"
            f"                  AND {_origin.as_of_gate_sql('', 3)}\n"
            f"                  AND {_origin.origin_class_clause('', None)}"
            if k else _origin.live_gate_sql("")
        )
        fact_rows = await conn.fetch(
            f"""
                SELECT id, subject, predicate, value, confidence, source_type,
                       valid_from, produced_at
                FROM facts
                WHERE LOWER(subject) = LOWER($1)
                  AND {fact_gate}
                ORDER BY produced_at DESC
                LIMIT $2
                """,
            profile["canonical_name"],
            INSPECT_RECENT_FACTS,
            *kp,
        )

    refs: list[str] = [str(entity_id)]
    versions: list[dict[str, Any]] = []
    for v in version_rows:
        refs.append(str(v["id"]))
        versions.append({
            "id": str(v["id"]),
            "version": v["version"],
            "cycle_number": v["cycle_number"],
            "analyst_id": v["analyst_id"],
            "created_at": _iso(v["created_at"]),
        })

    facts: list[dict[str, Any]] = []
    for f in fact_rows:
        fid = f["id"]
        refs.append(str(fid))
        facts.append({
            "id": str(fid),
            "subject": f["subject"],
            "predicate": f["predicate"],
            "value": f["value"],
            "confidence": float(f["confidence"])
                if f["confidence"] is not None else None,
            # source_type rides every fact row (mirrors query_facts F1) so a
            # reader — consult LLM / agency read tools / UI — can discount an
            # ingestion extraction (confidence is not a usable trust signal
            # for ingestion; the uncited grounding preamble excludes it
            # wholesale, this cited surface LABELS it).
            "source_type": f["source_type"],
            "valid_from": _iso(f["valid_from"]),
            "produced_at": _iso(f["produced_at"]),
        })

    mentions: list[dict[str, Any]] = []
    for m in mention_rows:
        sid = m["signal_id"]
        refs.append(str(sid))
        mentions.append({
            "signal_id": str(sid),
            "role": m["role"],
            "confidence": float(m["confidence"])
                if m["confidence"] is not None else None,
            "linked_at": _iso(m["created_at"]),
            "title": m["title"],
            # T-1b (M13): stored English title (translate route); absent else.
            "title_en": m["title_en"],
            "category": m["category"],
            "signal_produced_at": _iso(m["signal_produced_at"]),
        })

    prof: dict[str, Any] = {
        "id": str(entity_id),
        "canonical_name": profile["canonical_name"],
        "entity_type": profile["entity_type"],
        "entity_class": profile["entity_class"],
        "version": profile["version"],
        "completeness_score": float(profile["completeness_score"])
            if profile["completeness_score"] is not None else None,
        "last_event_link_at": _iso(profile["last_event_link_at"]),
        "last_verified_at": _iso(profile["last_verified_at"]),
        "geo_country": profile["geo_country"],
        "geo_region": profile["geo_region"],
        "produced_at": _iso(profile["produced_at"]),
        "analyst_id": profile["analyst_id"],
        "target_id": profile["target_id"],
    }
    out = {
        "entity": name,
        "found": True,
        "profile": prof,
        "facts": facts,
        "facts_note": (
            "current facts keyed by subject = canonical_name "
            "(superseded/expired rows excluded); for substring or "
            "predicate-scoped enumeration call query_facts(subject="
            f"{profile['canonical_name']!r})"
        ),
        "versions": versions,
        "recent_signal_mentions": mentions,
        "refs": refs,
    }
    if not k:
        return out
    # The record at K: the newest version row before K (none: unknown), the
    # completeness score has no ledger (masked), later timestamps masked.
    prof["version"] = max((v["version"] for v in versions), default=None)
    prof["completeness_score"] = None
    masked = 1 + _record_cut.mask_after(prof, _PROFILE_TIME_FIELDS, known_at)
    out["facts_note"] = (
        "facts that held at known_at by validity and were recorded before it, "
        "keyed by subject = canonical_name"
    )
    return _record_cut.stamp(
        out, known_at, masked=masked,
        how="entity_profiles / entity_profile_versions / signal_entity_links / "
            "signals / facts .created_at < K; facts by validity at K",
    )
