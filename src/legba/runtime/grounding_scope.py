# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The EXPLICIT retrieval scope of the ``world_context`` RAG (OPEN-1, W2).

Before this module, :meth:`SubstrateGroundingResolver.resolve_world_context`
applied NO Qdrant filter — the whole curated collection — to every target whose
id did not parse as ``country_<tier>_<iso2>``. That was the right answer for the
targets that are global by design (a region composer, a shipping lane, a flow,
a situation frame, a no-target meta run) and the WRONG answer for everything
else: on an instance whose targets are not named after countries, every desk
would read every other desk's corpus. The scope is now a decision with three
outcomes, never a default:

  * ``country`` — a per-subject desk: its ISO-2 ``MatchAny`` filter. The code
    comes from the id convention the profile's gazetteer declares
    (``desk_ids``, the fast path — unchanged on the exemplar) or, when the id
    carries none, from the target's DECLARED scope: its live head's
    ``body.scope.geo`` holding exactly one ISO-2 code (W10 CANON-1,
    :func:`corpus_filter_values`).
  * ``global`` — a target whose KIND is declared global, or no target at all:
    no filter, exactly as before, but LOGGED as
    ``grounding.scope=global reason=<kind>``.
  * ``none`` — any other target: NO retrieval at all (an empty
    :class:`WorldContextChunks` carrying ``reason=unresolvable_scope``), never
    the whole collection.

WHICH KINDS ARE GLOBAL. A target declares its kind in its live head's
``body.scope.tags`` (``region``, ``lane``, ``flow``, ``situation``, ``world`` —
:data:`GLOBAL_SCOPE_KINDS`). When the head is readable its tags are
authoritative. When it is not (no pool, a stub connection, a read failure, no
head row) the documented FALLBACK is the id family: ``<kind>_*`` or the bare
kind name. On the exemplar every global target carries both, so the two paths
agree (tests/runtime/test_grounding_scope.py proves it over every non-retired
target the repository registers).

A FILTER THAT CANNOT BE BUILT IS A REFUSAL (W10 CANON-1). A ``country`` scope
whose Qdrant filter fails to build used to degrade to NO filter — the whole
collection, the fail-open OPEN-1 closed for unresolvable ids, reopened one
step later. It now refuses by name: ``reason=filter_unbuildable``, no
retrieval (``grounding.SubstrateGroundingResolver.resolve_world_context``).

WHAT STILL FALLS OPEN (SEAMS #67). ``grounding.is_per_country_target`` — the
graph-structure narrowing and the situation scope — is still the id
convention alone (a sync, DB-free test on the deps-builder path): a target
outside the profile's ``desk_ids`` convention gets the GLOBAL graph-structure
block and the global situation frames, exactly as before.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

#: Target KINDS whose world-context retrieval is legitimately global — a scope
#: tag a target head declares, and (fallback) the id family ``<kind>_*``.
GLOBAL_SCOPE_KINDS: tuple[str, ...] = ("region", "lane", "flow", "situation", "world")

SCOPE_COUNTRY = "country"
SCOPE_GLOBAL = "global"
SCOPE_NONE = "none"

REASON_NO_TARGET = "no_target"
REASON_UNRESOLVABLE = "unresolvable_scope"

SOURCE_ISO2 = "iso2"
SOURCE_SCOPE_TAG = "scope_tag"
SOURCE_ID_PREFIX = "id_prefix"
#: A ``country`` scope read off the target's declared ``scope.geo``.
SOURCE_DESCRIPTOR_GEO = "descriptor_geo"
#: A ``country`` scope whose Qdrant filter could not be built — refused.
REASON_FILTER_UNBUILDABLE = "filter_unbuildable"

#: Seconds a target's scope tags are reused before the head is re-read.
SCOPE_TAG_TTL_SECONDS = 300.0

#: The target head's DECLARED scope (its domain, geo codes and tags) — one
#: indexed head read, cached per resolver.
_SCOPE_TAGS_SQL = (
    "SELECT body->'scope' FROM target_descriptors "
    "WHERE descriptor_id = $1 AND is_head LIMIT 1"
)


@dataclass(frozen=True)
class DeclaredScope:
    """What a target head declares about its scope: its ``scope.tags`` (the
    global KIND, when it has one), its ``scope.geo`` codes and its
    ``scope.domain``. A head that answers only a tag list (an older reader, a
    test double) declares no geo; a scope with no ``tags`` key declares no
    kind (``tags=None`` — the id-family fallback decides, as before)."""

    tags: tuple[str, ...] | None
    geo: tuple[str, ...] = ()
    domain: str | None = None


@dataclass(frozen=True)
class WorldContextScope:
    """One target's world-context retrieval decision."""

    scope: str
    reason: str
    country_values: tuple[str, ...] | None = None
    source: str = ""

    @property
    def retrieves(self) -> bool:
        """False only for ``none`` — the caller must not touch the collection."""
        return self.scope != SCOPE_NONE

    @property
    def filtered(self) -> bool:
        return self.scope == SCOPE_COUNTRY


class WorldContextChunks(list):
    """The chunks :meth:`resolve_world_context` returns, typed with the scope
    decision that produced them. Still a plain ``list`` for every caller; an
    unresolvable target yields an EMPTY one whose ``reason`` says why."""

    def __init__(self, items: Iterable[Any] = (), *, scope: WorldContextScope | None = None):
        super().__init__(items)
        self.scope = scope

    @property
    def reason(self) -> str | None:
        return self.scope.reason if self.scope is not None else None


def _clean(raw: Any) -> list[str]:
    return [t.strip().lower() for t in raw if isinstance(t, str) and t.strip()]


def _normalize_scope(raw: Any) -> DeclaredScope | None:
    """A head's ``scope`` (or its bare tag list) as a :class:`DeclaredScope`,
    or None when unreadable."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if isinstance(raw, (list, tuple)):
        return DeclaredScope(tags=tuple(_clean(raw)))
    if not isinstance(raw, dict):
        return None
    tags = raw.get("tags")
    geo = raw.get("geo")
    domain = raw.get("domain")
    return DeclaredScope(
        tags=tuple(_clean(tags)) if isinstance(tags, (list, tuple)) else None,
        geo=tuple(_clean(geo)) if isinstance(geo, (list, tuple)) else (),
        domain=domain.strip().lower() if isinstance(domain, str) and domain.strip() else None,
    )


def _normalize_tags(raw: Any) -> list[str] | None:
    """A head's ``scope.tags`` as a list of strings, or None when unreadable."""
    declared = _normalize_scope(raw)
    return None if declared is None or declared.tags is None else list(declared.tags)


def corpus_filter_values(declared: DeclaredScope | None) -> list[str] | None:
    """The ``countries`` ``MatchAny`` values a target's DECLARED scope yields:
    exactly one alphabetic ISO-2 code in ``scope.geo`` → ``[lower, UPPER]`` (the
    Lane-4 payload tags a chunk with both cases). Anything else — no head, no
    geo, several codes (a region spans many; a frame is not one subject) — is
    ``None``: no single-subject filter, and the caller decides global or
    refuse; never an empty filter, never the whole collection by default."""
    if declared is None:
        return None
    codes = [g for g in declared.geo if g]
    if len(codes) != 1:
        return None
    code = codes[0]
    if len(code) != 2 or not code.isalpha():
        return None
    return [code.lower(), code.upper()]


def is_scoped_target(
    target_id: str | None,
    id_values: Sequence[str] | None,
    declared: DeclaredScope | None,
) -> bool:
    """True when the target resolves to a single-subject corpus filter — by
    the id convention (``id_values``, the gazetteer's ``desk_ids``) or by its
    declared scope (:func:`corpus_filter_values`)."""
    if not isinstance(target_id, str) or not target_id.strip():
        return False
    return bool(id_values) or corpus_filter_values(declared) is not None


def declared_global_kind(
    target_id: str, scope_tags: Sequence[str] | None,
) -> tuple[str, str] | None:
    """``(kind, source)`` when the target is declared global, else None.

    ``scope_tags`` not None → the live head answered and is authoritative (an
    exact tag match; ``region_europe`` on a country desk is NOT ``region``).
    ``scope_tags`` None → the id-family fallback."""
    if scope_tags is not None:
        tags = {t.strip().lower() for t in scope_tags if isinstance(t, str)}
        for kind in GLOBAL_SCOPE_KINDS:
            if kind in tags:
                return kind, SOURCE_SCOPE_TAG
        return None
    tid = target_id.strip().lower()
    for kind in GLOBAL_SCOPE_KINDS:
        if tid == kind or tid.startswith(kind + "_"):
            return kind, SOURCE_ID_PREFIX
    return None


def classify_world_context_scope(
    target_id: str | None,
    country_values: Sequence[str] | None,
    scope_tags: Sequence[str] | None = None,
    declared: DeclaredScope | None = None,
) -> WorldContextScope:
    """The pure decision. ``country_values`` is
    :func:`legba.runtime.grounding.world_context_country_filter_values` for the
    same target (the id convention; passed in so this module stays import-free
    of grounding). ``declared`` is the head's declared scope when it answered
    (its tags then stand for ``scope_tags``); its single geo code scopes a
    target the id convention does not cover. Order: the id code, a declared
    global KIND, a declared single geo code, else REFUSE."""
    if not isinstance(target_id, str) or not target_id.strip():
        return WorldContextScope(SCOPE_GLOBAL, REASON_NO_TARGET)
    if country_values:
        return WorldContextScope(
            SCOPE_COUNTRY, SOURCE_ISO2, tuple(country_values), SOURCE_ISO2,
        )
    if declared is not None:
        tags = None if declared.tags is None else list(declared.tags)
    else:
        tags = scope_tags
    kind_decl = declared_global_kind(target_id, tags)
    if kind_decl is not None:
        kind, source = kind_decl
        return WorldContextScope(SCOPE_GLOBAL, kind, None, source)
    geo_values = corpus_filter_values(declared)
    if geo_values:
        return WorldContextScope(
            SCOPE_COUNTRY, SOURCE_DESCRIPTOR_GEO, tuple(geo_values), SOURCE_DESCRIPTOR_GEO,
        )
    return WorldContextScope(SCOPE_NONE, REASON_UNRESOLVABLE)


class TargetScopeTagReader:
    """Reads a target head's DECLARED scope (``body.scope``: tags, geo,
    domain) through the substrate pool, cached per instance for
    :data:`SCOPE_TAG_TTL_SECONDS`. Never raises: anything unreadable comes back
    None (→ the id-family fallback for the kind; no declared geo)."""

    def __init__(self, ttl_seconds: float = SCOPE_TAG_TTL_SECONDS) -> None:
        self._ttl = float(ttl_seconds)
        self._cache: dict[str, tuple[float, DeclaredScope | None]] = {}

    async def read_scope(self, pool: Any, target_id: str) -> DeclaredScope | None:
        now = time.monotonic()
        hit = self._cache.get(target_id)
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        declared: DeclaredScope | None = None
        if pool is not None:
            try:
                async with pool.acquire() as conn:
                    raw = await conn.fetchval(_SCOPE_TAGS_SQL, target_id)
                declared = _normalize_scope(raw)
            except Exception as exc:  # noqa: BLE001 — degrade to the id family
                logger.debug(
                    "grounding.scope.tags_read_failed target_id=%s err=%s",
                    target_id, exc,
                )
                declared = None
        self._cache[target_id] = (now, declared)
        return declared

    async def read(self, pool: Any, target_id: str) -> list[str] | None:
        """The head's ``scope.tags`` alone (None when unreadable)."""
        declared = await self.read_scope(pool, target_id)
        return None if declared is None or declared.tags is None else list(declared.tags)


async def resolve_world_context_scope(
    pool: Any,
    target_id: str | None,
    country_values: Sequence[str] | None,
    *,
    reader: TargetScopeTagReader,
) -> WorldContextScope:
    """Read the head's declared scope only when it can matter (a named target
    the id convention does not resolve), classify, and LOG every decision the
    id convention did not make."""
    declared = None
    if not country_values and isinstance(target_id, str) and target_id.strip():
        declared = await reader.read_scope(pool, target_id)
    scope = classify_world_context_scope(target_id, country_values, None, declared)
    scope_tags = None if declared is None or declared.tags is None else list(declared.tags)
    if scope.source == SOURCE_DESCRIPTOR_GEO:
        logger.info(
            "grounding.scope=country source=descriptor_geo target_id=%s values=%s",
            target_id, list(scope.country_values or ()),
        )
    elif scope.scope == SCOPE_GLOBAL:
        logger.info(
            "grounding.scope=global reason=%s source=%s target_id=%s",
            scope.reason, scope.source or "-", target_id,
        )
    elif scope.scope == SCOPE_NONE:
        logger.warning(
            "grounding.scope=none reason=%s target_id=%s scope_tags=%s "
            "— no world_context retrieval",
            scope.reason, target_id, scope_tags,
        )
    return scope
