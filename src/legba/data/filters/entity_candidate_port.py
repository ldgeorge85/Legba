# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Postgres trigram-backed :class:`EntityCandidatePort` (entity-resolution W1).

The :class:`~legba.data.filters.slm_entity_resolve.SLMEntityResolveHandler`
takes an :class:`EntityCandidatePort` to surface candidate ``entity_profiles``
matches for an ambiguous mention. This module supplies the production binding:
a ``pg_trgm`` similarity query against ``entity_profiles`` (the extension is
created in migration 0001 baseline).

Class-aware
-----------
The query is class-aware so the candidate set respects the composite identity
the substrate now keys on (``(lower(canonical_name), entity_class)``, migration
0035). When the caller passes a concrete ``entity_type`` the lookup prefers
same-class candidates; the generic ``"other"`` type (the handler's default when
the mention carries no type) does not constrain by class.

No stub
-------
The port is only constructed when a live ``pg_pool`` is wired (see
``pipeline._build_slm_entity_resolve``). It never fabricates candidates — a
query failure raises so the handler records the entry as unresolved rather than
inventing a match.
"""

from __future__ import annotations

from typing import Any, Mapping

#: Default minimum trigram similarity for a row to be a candidate. Mirrors the
#: legacy subconscious gate; the SLM still adjudicates, this only bounds the
#: candidate set so we don't ship the whole table to the model.
_DEFAULT_MIN_SIMILARITY: float = 0.2


class PostgresEntityCandidatePort:
    """``EntityCandidatePort`` backed by a ``pg_trgm`` similarity query.

    Conforms structurally to
    :class:`legba.data.filters.slm_entity_resolve.EntityCandidatePort`.
    """

    def __init__(
        self,
        pg_pool: Any,
        *,
        min_similarity: float = _DEFAULT_MIN_SIMILARITY,
    ) -> None:
        if pg_pool is None:
            raise ValueError(
                "PostgresEntityCandidatePort requires a pg_pool; the trigram "
                "candidate lookup cannot run without it (no stub)"
            )
        self._pool = pg_pool
        self._min_similarity = float(min_similarity)

    async def fetch_candidates(
        self,
        *,
        entity_name: str,
        entity_type: str,
        limit: int = 10,
    ) -> list[Mapping[str, Any]]:
        """Return up to ``limit`` candidate ``entity_profiles`` rows.

        Each row carries ``entity_id``/``canonical_name``/``entity_type`` plus a
        ``trgm_similarity`` float the handler uses for its cross-validation
        downgrade. Ordered by descending similarity.
        """
        name = (entity_name or "").strip()
        if not name:
            return []

        # A concrete entity_type narrows by class; the generic "other" does
        # not constrain (the handler's no-type default).
        constrain_class = bool(entity_type) and entity_type != "other"

        sql = """
            SELECT id AS entity_id,
                   canonical_name,
                   entity_type,
                   entity_class,
                   similarity(lower(canonical_name), lower($1)) AS trgm_similarity
              FROM entity_profiles
             WHERE similarity(lower(canonical_name), lower($1)) >= $2
        """
        args: list[Any] = [name, self._min_similarity]
        if constrain_class:
            sql += " AND (entity_class = $4 OR entity_type = $4)"
        sql += " ORDER BY trgm_similarity DESC LIMIT $3"
        args.append(int(limit))
        if constrain_class:
            args.append(entity_type)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [dict(r) for r in rows]


__all__ = ["PostgresEntityCandidatePort"]
