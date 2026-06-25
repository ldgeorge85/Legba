# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discovery dependency resolution — the G20 ``ctx.stack_resolve`` fix (P-13).

The blocker
-----------

The original ``country_list_discovery`` (and, by extension, the whole
discovery family) read the ``iso_countries`` seed via a per-cycle
``ctx.stack_resolve('postgres')`` callable that every *consuming target* had
to plumb through. That is the G20 blocker: a discovery descriptor that
materialises country targets could not run unless the runtime threaded a
live Postgres accessor into the :class:`DiscoveryContext` of every cycle, and
nothing in the source-first runtime did that — so the cycle raised
``RuntimeError: ... requires ctx.stack_resolve to bind a postgres reader``.

The fix (PIVOT §4 / ``source.py`` :class:`SourceDeps`)
------------------------------------------------------

The pivot introduces a *declared dependency* contract: a source (and, here, a
discovery template) declares which substrate components it needs ONCE, in a
:class:`~legba.data.schemas.source.SourceDeps` block (``postgres: true`` for
``iso_countries``-style lookups). The **actor / host** resolves those declared
components a single time at activation and hands the discovery handler a
ready-to-use :class:`ResolvedDiscoveryDeps` bundle. The handler never resolves
the stack itself, and consuming targets never plumb anything — exactly the
fan-out invariant the source-first pivot is built on (one resolution per
source/discovery, not one per consumer).

This module owns that seam:

  * :class:`ResolvedDiscoveryDeps` — the bundle the actor builds and the
    discovery materialiser consumes. Carries the resolved Postgres pool (the
    asyncpg-shaped accessor) + optional qdrant / embedding / object-store
    handles + resolved vault secrets, mirroring the
    :class:`~legba.data.schemas.source.SourceDeps` declaration field-for-field.
  * :func:`resolve_discovery_deps` — the actor-side resolver: given a declared
    :class:`SourceDeps` + a :class:`~legba.runtime.deps.StandardDeps` (which
    already owns the pool / secret resolver), produce the resolved bundle.
    Validates that every declared dependency is actually satisfiable BEFORE the
    cycle runs, so an unsatisfiable declaration fails loudly at activation
    rather than mid-cycle.
  * :func:`load_country_rows` — the concrete ``iso_countries`` reader that
    consumes :attr:`ResolvedDiscoveryDeps.postgres`. This is the function that
    replaces ``_resolve_iso_3166_from_substrate``'s ``ctx.stack_resolve`` call.

The bundle is a plain object (not pydantic) so it can carry the asyncpg pool /
callables without serialisation fuss, the same shape
:class:`~legba.runtime.source_actor.SourceDeps` uses.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ResolvedDiscoveryDeps — the actor-built bundle
# ---------------------------------------------------------------------------


@dataclass
class ResolvedDiscoveryDeps:
    """Live substrate handles the discovery actor resolved at activation.

    Mirrors :class:`legba.data.schemas.source.SourceDeps` field-for-field, but
    where ``SourceDeps`` is the *declaration* (``postgres: bool``), this is the
    *resolution* (``postgres: asyncpg.Pool | None``). The actor builds one of
    these once per discovery descriptor; the materialiser reads from it.

    A ``None`` field means the dependency was either not declared or could not
    be resolved — :func:`resolve_discovery_deps` raises if a *declared* dep is
    unsatisfiable, so a ``None`` field on a bundle returned by the resolver
    always means "not declared".
    """

    postgres: Any = None
    """The resolved asyncpg-shaped pool (``acquire()`` context manager). The
    ``iso_countries`` reader acquires a connection from it. ``None`` when the
    discovery's declared deps did not set ``postgres: true``."""

    qdrant: Any = None
    """Resolved semantic-search client (dedupe-aware discoveries). ``None``
    when not declared."""

    embedding: Any = None
    """Resolved embedding factory/client. ``None`` when not declared."""

    object_store: Any = None
    """Resolved object-store client (media-retaining discoveries). ``None``
    when not declared."""

    secrets: dict[str, Any] = field(default_factory=dict)
    """Resolved vault secrets, keyed by the secret id declared in
    ``SourceDeps.vault_secrets``. The actor resolves each via
    ``StandardDeps.secrets_resolve`` at activation so the handler never touches
    the vault directly."""

    def require_postgres(self) -> Any:
        """Return the resolved Postgres pool or raise a precise error.

        The replacement for the old ``RuntimeError('... requires
        ctx.stack_resolve ...')`` — but the error now points the operator at
        the *declaration* (add ``deps.postgres: true`` to the descriptor),
        not at a missing per-cycle plumbing callable.
        """
        if self.postgres is None:
            raise RuntimeError(
                "discovery declared no resolvable postgres dependency; add "
                "`deps: {postgres: true}` to the source/discovery descriptor "
                "so the actor resolves the substrate pool at activation "
                "(the old per-target ctx.stack_resolve('postgres') path is "
                "retired — PIVOT §4 SourceDeps)"
            )
        return self.postgres


# ---------------------------------------------------------------------------
# resolve_discovery_deps — the actor-side resolver
# ---------------------------------------------------------------------------


async def resolve_discovery_deps(
    declared: Any,
    deps: Any,
    *,
    qdrant: Any = None,
    embedding: Any = None,
    object_store: Any = None,
) -> ResolvedDiscoveryDeps:
    """Resolve a declared :class:`SourceDeps` block into live handles.

    Called by the discovery actor / host at activation — ONCE per discovery
    descriptor, regardless of how many targets consume the materialised
    instances.

    Parameters
    ----------
    declared:
        A :class:`legba.data.schemas.source.SourceDeps` (or any object exposing
        the same boolean ``postgres`` / ``qdrant`` / ``embedding`` /
        ``object_store`` attributes + a ``vault_secrets`` list). ``None`` is
        tolerated and yields an all-empty bundle (a discovery with no declared
        deps — e.g. an ``inline:`` list source).
    deps:
        A :class:`legba.runtime.deps.StandardDeps` carrying ``pg_pool`` +
        ``secrets_resolve``. The pool here is the substrate pool the actor was
        constructed with; declaring ``postgres: true`` simply *promotes* it
        into the resolved bundle (and asserts it exists).
    qdrant / embedding / object_store:
        Pre-resolved optional clients the host may inject. When a dep is
        declared but the corresponding client is ``None``, the resolver raises
        — better a loud activation failure than a half-resolved cycle.

    Raises
    ------
    RuntimeError
        If a dependency is *declared* but cannot be satisfied (e.g.
        ``postgres: true`` but ``deps.pg_pool is None``).
    """
    bundle = ResolvedDiscoveryDeps()
    if declared is None:
        return bundle

    pg_pool = getattr(deps, "pg_pool", None)
    secrets_resolve = getattr(deps, "secrets_resolve", None)

    if getattr(declared, "postgres", False):
        if pg_pool is None:
            raise RuntimeError(
                "discovery declared `deps.postgres: true` but the actor's "
                "StandardDeps.pg_pool is None — the host must construct the "
                "discovery actor with a live substrate pool"
            )
        bundle.postgres = pg_pool

    if getattr(declared, "qdrant", False):
        if qdrant is None:
            raise RuntimeError(
                "discovery declared `deps.qdrant: true` but no qdrant client "
                "was injected into resolve_discovery_deps"
            )
        bundle.qdrant = qdrant

    if getattr(declared, "embedding", False):
        if embedding is None:
            raise RuntimeError(
                "discovery declared `deps.embedding: true` but no embedding "
                "client was injected"
            )
        bundle.embedding = embedding

    if getattr(declared, "object_store", False):
        if object_store is None:
            raise RuntimeError(
                "discovery declared `deps.object_store: true` but no "
                "object-store client was injected"
            )
        bundle.object_store = object_store

    vault_secrets = list(getattr(declared, "vault_secrets", []) or [])
    if vault_secrets:
        if secrets_resolve is None:
            raise RuntimeError(
                f"discovery declared vault_secrets={vault_secrets!r} but the "
                "actor's StandardDeps.secrets_resolve is None"
            )
        for secret_id in vault_secrets:
            resolved = secrets_resolve(secret_id)
            if hasattr(resolved, "__await__"):
                resolved = await resolved
            bundle.secrets[secret_id] = resolved

    return bundle


# ---------------------------------------------------------------------------
# load_country_rows — the iso_countries reader (replaces ctx.stack_resolve)
# ---------------------------------------------------------------------------


# The eight columns the ``iso_countries`` seed (migration 0019) exposes, in the
# shape the country_list discovery's CandidateTarget.label_set expects.
_ISO_COUNTRIES_QUERY = (
    "SELECT iso2, iso3, numeric, name, official, region, subregion, "
    "languages FROM iso_countries ORDER BY iso2"
)


async def load_country_rows(
    resolved: ResolvedDiscoveryDeps,
    *,
    region: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Load ``iso_countries`` rows via the actor-resolved Postgres dep.

    This is the function that *fixes the G20 blocker*: it reads the seed via
    :attr:`ResolvedDiscoveryDeps.postgres` (resolved once by the actor from the
    descriptor's declared ``deps.postgres``) instead of the per-cycle
    ``ctx.stack_resolve('postgres')`` callable every consuming target used to
    have to plumb.

    Returns ``(rows, source_version)`` where each row is a plain dict shaped
    like the ``iso_countries`` columns (the country_list handler's ``_Row``
    accepts it directly), and ``source_version`` is a stable per-cycle stamp
    (``iso_3166@n=<count>``) so the disappearance diff loop sees an unchanged
    string when the table hasn't changed.

    Parameters
    ----------
    region:
        Optional UN-M49 region pre-filter pushed into SQL (``Americas`` /
        ``Asia`` / ...). When ``None`` (the default) every row is returned and
        any region filtering happens in the handler's ``filter_predicate``.
    """
    pool = resolved.require_postgres()

    query = _ISO_COUNTRIES_QUERY
    args: list[Any] = []
    if region:
        # Push the region narrow into SQL — the iso_countries_region_idx
        # index (migration 0019) covers it.
        query = (
            "SELECT iso2, iso3, numeric, name, official, region, subregion, "
            "languages FROM iso_countries WHERE region = $1 ORDER BY iso2"
        )
        args = [region]

    async with pool.acquire() as conn:
        records = await conn.fetch(query, *args)

    rows: list[dict[str, Any]] = []
    for r in records:
        raw_langs = r["languages"]
        if isinstance(raw_langs, (str, bytes, bytearray)):
            try:
                raw_langs = json.loads(raw_langs)
            except Exception:
                raw_langs = []
        rows.append(
            {
                "iso2": r["iso2"],
                "iso3": r["iso3"],
                "numeric": r["numeric"],
                "name": r["name"],
                "official": r["official"],
                "region": r["region"],
                "subregion": r["subregion"],
                "languages": list(raw_langs) if raw_langs else [],
            }
        )

    source_version = f"iso_3166@n={len(rows)}"
    if region:
        source_version = f"iso_3166@region={region}&n={len(rows)}"
    return rows, source_version


__all__ = [
    "ResolvedDiscoveryDeps",
    "resolve_discovery_deps",
    "load_country_rows",
]
