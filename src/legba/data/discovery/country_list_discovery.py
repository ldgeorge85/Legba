# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-181 ``country_list_discovery`` — one target per country in a configured list.

Per L-106 §3 worked example, the canonical "discovery → relabel chain →
materialized target" walk is the *country news* template:

    descriptor identity: country_news_template
    discovery:
      kind: country_list_discovery
      config:
        list_source: iso_3166
        filter_predicate: "country.region == 'Americas'"
        schedule: "0 3 * * *"
      relabel:
        - { source_labels: [country_iso2],
            target_label: scope.geo,
            action: set_list }
        - { source_labels: [country_iso2, country_languages],
            target_label: scope.languages,
            action: lookup_languages }
        - { source_labels: [scope.languages],
            target_label: scope.languages,
            action: merge_list,
            extend_with: ['en'] }
        - { source_labels: [country_iso2],
            target_label: id,
            action: format,
            replacement: "country_news_{{ country_iso2 | lower }}" }
        - { source_labels: [region],
            action: keep,
            predicate: "region != 'antarctica'" }

…and the registry materialises ~56 country target descriptors (one per
Americas country) automatically every time the cron schedule fires.

This module owns the *handler* half of the contract — the relabel chain
itself is driven by :mod:`legba.data.discovery.relabel` against the
descriptor's ``relabel`` block. The handler's only job is to:

  1. Resolve the configured list source to a row iterator.
  2. Apply the (optional) ``filter_predicate`` over each row.
  3. Emit one :class:`CandidateTarget` per surviving row, carrying the
     row's iso2 / iso3 / name / region / subregion / languages as
     ``label_set`` keys + the source pointer in ``source_metadata``.

The disappearance-ratio enforcement is the *registry*'s job too — this
module just exposes the policy via ``CONFIG_SCHEMA.resync_policy`` so a
descriptor can override the default 0.30 threshold per L-180 §5.

List sources
------------

Three modes:

* ``iso_3166`` — substrate-cached snapshot (the ``iso_countries`` table
  from migration ``0019_iso_countries_seed.sql``). Production default.
  Requires a ``ctx.stack_resolve("postgres")`` factory; tests can pass
  inline rows via :func:`CountryListDiscovery.bind_inline_rows` instead.
* ``url:<https://...>`` — fetch + cache JSON from an external list. The
  Wave-C scope; the implementation is *stubbed* with a clear error so
  the descriptor schema can accept the value and the runtime fails fast
  with a useful message rather than silently mis-routing. Documented as
  a follow-up.
* ``inline:<json>`` — escape hatch for unit tests and bootstrap. The
  config value after the prefix is a JSON-encoded list of row dicts.

Each row is a dict with the eight columns from ``iso_countries``: ``iso2``,
``iso3``, ``numeric``, ``name``, ``official``, ``region``, ``subregion``,
``languages`` (list[str]). The handler is permissive about missing
columns — only ``iso2`` is required (the natural key); other columns
default to empty.

Filter predicate
----------------

A small Starlark / Python predicate that gates which rows materialise.
Evaluated against the row's columns *before* the discovery emits the
candidate, so the disappearance-ratio enforcement and downstream relabel
chain only see countries the operator asked for. Bound names:

    country.iso2 / iso3 / name / region / subregion / numeric / official
    languages  (list[str])

The predicate runs through the same evaluator
:mod:`legba.data.discovery.relabel` uses for its ``keep`` / ``drop``
actions, so the sandbox + AST surface stays uniform.

Schedule
--------

The ``schedule`` field is a 5-field Unix-style cron expression
(``"0 3 * * *"`` = daily at 03:00 UTC). The handler doesn't run the
cron — that's the runtime / Dapr cron-binding's job — but the config
type validates the expression at descriptor-registration time so a
typo gets surfaced before the discovery is registered.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, AsyncIterator, ClassVar, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._contract import (
    CandidateTarget,
    DiscoveryContext,
    DiscoveryEvidence,
    DiscoveryHealth,
    ResyncPolicy,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


KIND_NAME = "country_list_discovery"
SCHEMA_VERSION = "legba/discovery/country_list/1.0.0"


# ---------------------------------------------------------------------------
# Cron expression validation
# ---------------------------------------------------------------------------


_CRON_FIELD_5 = re.compile(
    r"^\s*\S+\s+\S+\s+\S+\s+\S+\s+\S+\s*$"
)
"""Surface-level shape check — five whitespace-separated tokens. Token
syntax (ranges, lists, steps, named days) is checked per-field below.
The runtime's actual cron-binding does the strict semantic validation;
this regex only catches obvious typos at registration time."""


# Accept digits + the standard cron wildcards / operators per-field. The
# upper-bounds aren't checked here (e.g. minute 0–59) — the runtime cron
# library does that during scheduling. We just reject characters that
# never belong in a cron field.
_CRON_TOKEN = re.compile(r"^[\d\*/,\-A-Za-z?LWH#]+$")


def _validate_cron(expr: str) -> str:
    if not _CRON_FIELD_5.match(expr):
        raise ValueError(
            f"cron expression must have exactly 5 whitespace-separated fields; "
            f"got {expr!r}"
        )
    for token in expr.split():
        if not _CRON_TOKEN.match(token):
            raise ValueError(
                f"cron field {token!r} contains characters not allowed in a "
                f"cron expression (allowed: digits, *, /, ,, -, A-Z, a-z, ?, "
                f"L, W, H, #); full expression: {expr!r}"
            )
    return expr


# ---------------------------------------------------------------------------
# CONFIG_SCHEMA
# ---------------------------------------------------------------------------


_ISO_3166_BUILTIN = "iso_3166"
"""Magic ``list_source`` value selecting the substrate-cached
``iso_countries`` snapshot per migration 0019."""

_URL_PREFIX = "url:"
"""``list_source`` prefix selecting a URL-fetched JSON list. Wave-C."""

_INLINE_PREFIX = "inline:"
"""``list_source`` prefix selecting a JSON-encoded inline list. The
remainder of the value is parsed as JSON; the result must be a list of
row dicts. Intended for unit tests and bootstrap fixtures."""

_SUBSTRATE_PREFIX = "substrate:"
"""``list_source`` prefix selecting an arbitrary substrate query string.
Reserved for future use (operator-defined country lists materialised by
a SQL view). The current implementation rejects it with a clear error."""


class CountryListDiscoveryConfig(BaseModel):
    """Pydantic config schema for :class:`CountryListDiscovery`.

    Surface
    ~~~~~~~

    ``list_source``
        Pointer to the country list. One of:

        * ``"iso_3166"`` — the substrate-cached snapshot.
        * ``"url:<https://...>"`` — fetch external JSON.
        * ``"inline:<json-list>"`` — JSON-encoded list of row dicts.
        * ``"substrate:<query>"`` — reserved.

        Default: ``"iso_3166"``.

    ``filter_predicate``
        Starlark / Python expression gating which rows materialise.
        Evaluated *before* the discovery yields a candidate, so the
        downstream disappearance check sees only the filtered set.
        Bound names: ``country`` (a ``Row`` with ``iso2`` / ``iso3`` /
        ``name`` / ``region`` / ``subregion`` / ``numeric`` /
        ``official``), ``languages`` (list[str]).

        Default: ``""`` (no filter, emit all rows).

    ``schedule``
        Cron expression for resync cadence. The handler doesn't run the
        cron — the runtime's cron-binding does — but the config
        validates the shape so typos surface at descriptor registration.

        Default: ``"0 3 * * *"`` (daily 03:00 UTC).

    ``resync_policy``
        Disappearance-ratio policy per L-180 §5. Default threshold 0.30
        per :data:`DEFAULT_DISAPPEARANCE_RATIO_THRESHOLD`.

    ``default_languages_fallback``
        BCP-47 list to attach as ``country_languages`` when the row's
        list is empty. The relabel chain's ``lookup_languages`` action
        also accepts a second source_label as a fallback; this just
        sets the candidate-side default so descriptors don't have to.
    """

    model_config = ConfigDict(extra="forbid")

    list_source: str = Field(
        default=_ISO_3166_BUILTIN,
        min_length=1,
        max_length=4096,
        description=(
            "Country list pointer — 'iso_3166', 'url:<https://...>', "
            "'inline:<json>', or 'substrate:<query>'."
        ),
    )
    filter_predicate: str = Field(
        default="",
        max_length=4096,
        description=(
            "Optional Starlark/Python predicate over country.{iso2/iso3/name/"
            "region/subregion/numeric/official} + languages. Empty = no filter."
        ),
    )
    schedule: str = Field(
        default="0 3 * * *",
        min_length=1,
        max_length=128,
        description="5-field Unix cron expression for resync cadence.",
    )
    resync_policy: ResyncPolicy = Field(default_factory=ResyncPolicy)
    default_languages_fallback: list[str] = Field(default_factory=lambda: ["en"])

    @field_validator("schedule")
    @classmethod
    def _check_schedule(cls, v: str) -> str:
        return _validate_cron(v)

    @field_validator("list_source")
    @classmethod
    def _check_list_source(cls, v: str) -> str:
        if v == _ISO_3166_BUILTIN:
            return v
        if v.startswith(_URL_PREFIX):
            url = v[len(_URL_PREFIX):]
            if not (url.startswith("http://") or url.startswith("https://")):
                raise ValueError(
                    f"list_source 'url:' prefix must point at an http(s) URL; "
                    f"got {v!r}"
                )
            return v
        if v.startswith(_INLINE_PREFIX):
            # Parse-validate the JSON payload so a malformed inline list
            # surfaces at descriptor-registration time, not at the first
            # discovery cycle.
            payload = v[len(_INLINE_PREFIX):]
            try:
                parsed = json.loads(payload)
            except Exception as exc:
                raise ValueError(
                    f"list_source 'inline:' payload is not valid JSON: {exc}"
                ) from exc
            if not isinstance(parsed, list):
                raise ValueError(
                    f"list_source 'inline:' payload must be a JSON list; "
                    f"got {type(parsed).__name__}"
                )
            return v
        if v.startswith(_SUBSTRATE_PREFIX):
            # Reserved — accepted at config-time but the handler rejects
            # at discover() time. Avoids a silent half-implementation.
            return v
        raise ValueError(
            f"unrecognised list_source {v!r}; "
            f"expected 'iso_3166', 'url:<...>', 'inline:<...>', or "
            f"'substrate:<...>'"
        )


CONFIG_SCHEMA = CountryListDiscoveryConfig
"""Module-level export name the discovery-kind registry walker looks for."""


# ---------------------------------------------------------------------------
# Row → CandidateTarget bridge
# ---------------------------------------------------------------------------


class _Row(BaseModel):
    """One row from the country list source — the shape the handler emits.

    Public type only because :func:`_eval_filter_predicate` binds an
    instance into the predicate context as ``country``. The handler
    constructs these internally and they are not part of the kind's
    external surface (the relabel chain consumes ``label_set`` keys, not
    the dataclass).
    """

    model_config = ConfigDict(extra="ignore")

    iso2: str
    iso3: str = ""
    numeric: str = ""
    name: str = ""
    official: str = ""
    region: str = ""
    subregion: str = ""
    languages: list[str] = Field(default_factory=list)


def _row_to_candidate(
    row: _Row,
    *,
    list_source: str,
    source_version: str,
    row_index: int,
    default_languages_fallback: list[str],
) -> CandidateTarget:
    """Build the :class:`CandidateTarget` the registry's relabel chain expects.

    Label-set keys are the names the L-106 §3 worked example uses
    (``country_iso2`` / ``country_iso3`` / ``country_name`` / etc.) so
    descriptors can reference them in relabel rules without renaming.
    The handler also exposes a bare ``region`` label because the worked
    example's final ``keep`` predicate (``region != 'antarctica'``)
    reads that name.
    """
    languages = list(row.languages) if row.languages else list(default_languages_fallback)
    label_set: dict[str, Any] = {
        "country_iso2": row.iso2,
        "country_iso3": row.iso3,
        "country_numeric": row.numeric,
        "country_name": row.name,
        "country_official": row.official,
        "country_region": row.region,
        "country_subregion": row.subregion,
        "country_languages": languages,
        # Bare names so the L-106 §3 `region != 'antarctica'` keep
        # predicate reads cleanly without a copy step.
        "region": (row.region.lower() if row.region else ""),
        "subregion": row.subregion,
        "name": row.name,
    }
    source_metadata = {
        "list_source": list_source,
        "list_source_version": source_version,
        "row_index": row_index,
    }
    evidence = DiscoveryEvidence(
        source_id=f"discovery.country_list.{list_source}",
        source_version=source_version,
        row_index=row_index,
        extra={"region": row.region, "subregion": row.subregion},
    )
    return CandidateTarget(
        natural_key=row.iso2,
        label_set=label_set,
        source_metadata=source_metadata,
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Filter-predicate evaluation
# ---------------------------------------------------------------------------


def _eval_filter_predicate(predicate: str, row: _Row) -> bool:
    """Evaluate the optional filter predicate against a row.

    Reuses :func:`legba.data.discovery.relabel._safe_python_eval` for
    parity with the relabel ``keep`` / ``drop`` evaluator. Bound names:

      * ``country`` — :class:`_Row` instance; attribute access (`country.iso2`).
      * ``languages`` — top-level list[str] for predicates that don't
        want to type `country.languages` repeatedly.

    Empty / blank predicate returns True (no filter).
    """
    if not predicate or not predicate.strip():
        return True

    bindings = {
        "country": row,
        "languages": list(row.languages),
        # Bare names too — the L-106 §3 examples write
        # `region == 'Americas'` straight without the country. prefix.
        "iso2": row.iso2,
        "iso3": row.iso3,
        "name": row.name,
        "region": row.region,
        "subregion": row.subregion,
    }
    # Delegate to the safe-Python evaluator from relabel.py. It accepts
    # attribute access (`country.iso2`) and `in` / membership operators,
    # which is everything the brief asks for.
    from .relabel import _safe_python_eval

    return bool(_safe_python_eval(predicate, bindings, None))


# ---------------------------------------------------------------------------
# List-source resolution
# ---------------------------------------------------------------------------


def _parse_inline_rows(raw: str) -> list[_Row]:
    """Parse the JSON payload from an ``inline:<json>`` ``list_source``.

    Each list element must be a dict shaped like the ``iso_countries``
    table columns. Missing columns default per :class:`_Row`. The
    pydantic validation surfaces malformed rows with a clear error.
    """
    payload = raw[len(_INLINE_PREFIX):]
    parsed = json.loads(payload)
    if not isinstance(parsed, list):
        raise ValueError(
            f"inline list_source must be a JSON list; got {type(parsed).__name__}"
        )
    out: list[_Row] = []
    for idx, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise ValueError(
                f"inline list_source row {idx} must be a dict; "
                f"got {type(item).__name__}"
            )
        out.append(_Row.model_validate(item))
    return out


async def _resolve_iso_3166_from_substrate(
    ctx: DiscoveryContext,
) -> tuple[list[_Row], str]:
    """Load all ``iso_countries`` rows via the runtime's stack resolver.

    The resolver is an async callable that returns a Postgres-shaped
    accessor object — duck-typed to expose either ``fetch(query)`` (the
    asyncpg / :class:`legba.data.postgres.PostgresStore` shape) or
    ``read_rows(query)``. Returns ``(rows, source_version)``; the
    source_version is the row count + a stable suffix so the
    discovery_state diff loop sees a stable string even when the table
    hasn't changed between cycles.
    """
    if ctx.stack_resolve is None:
        raise RuntimeError(
            "country_list_discovery with list_source='iso_3166' requires "
            "ctx.stack_resolve to bind a postgres reader; got None"
        )

    accessor = ctx.stack_resolve("postgres")
    if hasattr(accessor, "__await__"):
        accessor = await accessor  # type: ignore[misc]

    query = (
        "SELECT iso2, iso3, numeric, name, official, region, subregion, "
        "languages FROM iso_countries ORDER BY iso2"
    )

    if hasattr(accessor, "fetch"):
        rows_raw = await accessor.fetch(query)
    elif hasattr(accessor, "read_rows"):
        rows_raw = await accessor.read_rows(query)
    else:
        raise RuntimeError(
            "stack_resolve('postgres') accessor must expose fetch() or "
            f"read_rows(); got {type(accessor).__name__}"
        )

    rows: list[_Row] = []
    for r in rows_raw:
        # asyncpg.Record supports mapping access; dict tolerates the same.
        raw_langs = r["languages"] if "languages" in r.keys() else []  # type: ignore[union-attr]
        if isinstance(raw_langs, str):
            try:
                raw_langs = json.loads(raw_langs)
            except Exception:
                raw_langs = []
        rows.append(
            _Row(
                iso2=r["iso2"],
                iso3=r["iso3"] if "iso3" in r.keys() else "",  # type: ignore[union-attr]
                numeric=r["numeric"] if "numeric" in r.keys() else "",  # type: ignore[union-attr]
                name=r["name"] if "name" in r.keys() else "",  # type: ignore[union-attr]
                official=r["official"] if "official" in r.keys() else "",  # type: ignore[union-attr]
                region=r["region"] if "region" in r.keys() else "",  # type: ignore[union-attr]
                subregion=r["subregion"] if "subregion" in r.keys() else "",  # type: ignore[union-attr]
                languages=list(raw_langs) if raw_langs else [],
            )
        )

    source_version = f"iso_3166@n={len(rows)}"
    return rows, source_version


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class CountryListDiscovery:
    """L-181 ``country_list_discovery`` discovery kind.

    One target per country in the configured list. Satisfies the
    :class:`legba.data.discovery.DiscoveryKind` Protocol. The
    discovery-kind registry walker
    :func:`legba.data.discovery.discover_discovery_kinds` picks this up
    automatically via the module-level ``KIND_NAME`` + this class.
    """

    kind: ClassVar[str] = KIND_NAME
    family: ClassVar[Literal["discovery"]] = "discovery"
    schema_version: ClassVar[str] = SCHEMA_VERSION
    config_schema: ClassVar[type[BaseModel]] = CountryListDiscoveryConfig

    def __init__(self) -> None:
        # Optional inline override — set via :meth:`bind_inline_rows`.
        # Bypasses the list_source field; useful for tests that don't
        # want to construct a stack_resolve mock.
        self._inline_rows_override: list[_Row] | None = None
        # P-13 G20 fix: the actor resolves the descriptor's declared
        # `deps.postgres` ONCE and binds the resolved bundle here. When set,
        # the `iso_3166` list_source reads via `load_country_rows(resolved)`
        # instead of the retired per-cycle `ctx.stack_resolve('postgres')`.
        self._resolved_deps: Any | None = None
        self._last_source_version: str = ""
        self._last_emitted: int = 0
        self._last_error: str | None = None

    # ----- actor-resolved dep binding (P-13 G20 fix) ------------------

    def bind_resolved_deps(self, resolved: Any) -> None:
        """Bind the actor-resolved :class:`ResolvedDiscoveryDeps` bundle.

        The discovery actor calls this at activation after resolving the
        descriptor's declared ``deps`` (``postgres: true`` for the
        ``iso_3166`` list source). Once bound, the ``iso_3166`` path reads
        ``iso_countries`` via the resolved Postgres pool — NOT via the old
        per-target ``ctx.stack_resolve('postgres')`` callable. This is the
        seam that fixes the G20 ``stack_resolve`` blocker.
        """
        self._resolved_deps = resolved

    # ----- test hook --------------------------------------------------

    def bind_inline_rows(self, rows: Iterable[dict[str, Any] | _Row]) -> None:
        """Test-only: bypass list_source resolution with an explicit list."""
        bound: list[_Row] = []
        for r in rows:
            if isinstance(r, _Row):
                bound.append(r)
            else:
                bound.append(_Row.model_validate(r))
        self._inline_rows_override = bound

    # ----- main entry -------------------------------------------------

    async def discover(
        self,
        ctx: DiscoveryContext,
    ) -> AsyncIterator[CandidateTarget]:
        """Yield one :class:`CandidateTarget` per surviving country row.

        Order: iso2-ascending — keeps the diff loop stable across cycles.
        """
        cfg = ctx.config
        if not isinstance(cfg, CountryListDiscoveryConfig):
            # Defensive — the runtime parses cfg via CONFIG_SCHEMA before
            # construction, but tests sometimes pass a raw dict.
            cfg = CountryListDiscoveryConfig.model_validate(
                cfg.model_dump() if isinstance(cfg, BaseModel) else cfg
            )

        try:
            rows, source_version = await self._resolve_rows(ctx, cfg)
        except Exception as exc:
            self._last_error = f"resolve_rows: {type(exc).__name__}: {exc}"
            ctx.logger.exception(
                "country_list_discovery.resolve_rows_failed list_source=%s",
                cfg.list_source,
            )
            raise

        self._last_source_version = source_version
        emitted = 0
        for idx, row in enumerate(rows):
            try:
                if not _eval_filter_predicate(cfg.filter_predicate, row):
                    continue
            except Exception as exc:
                # Predicate compile / eval error — fail the cycle rather
                # than silently skipping the row. The L-106 §5 disappearance
                # check would otherwise misclassify the row as 'disappeared'
                # and route the operator into resync_review for a typo.
                self._last_error = (
                    f"filter_predicate: {type(exc).__name__}: {exc}"
                )
                raise

            candidate = _row_to_candidate(
                row,
                list_source=cfg.list_source,
                source_version=source_version,
                row_index=idx,
                default_languages_fallback=list(cfg.default_languages_fallback),
            )
            emitted += 1
            yield candidate

        self._last_emitted = emitted
        self._last_error = None
        ctx.logger.info(
            "country_list_discovery.cycle_complete list_source=%s "
            "source_version=%s rows_in=%d emitted=%d",
            cfg.list_source, source_version, len(rows), emitted,
        )

    async def _resolve_rows(
        self,
        ctx: DiscoveryContext,
        cfg: CountryListDiscoveryConfig,
    ) -> tuple[list[_Row], str]:
        """Resolve the configured list source to a row list + version stamp."""
        if self._inline_rows_override is not None:
            return list(self._inline_rows_override), "inline_override"

        if cfg.list_source == _ISO_3166_BUILTIN:
            # P-13 G20 fix: prefer the actor-resolved Postgres dep when the
            # actor bound one. This reads iso_countries via the descriptor's
            # declared `deps.postgres`, resolved ONCE by the actor — NOT via
            # the per-target ctx.stack_resolve('postgres') plumbing that was
            # the original G20 blocker.
            if self._resolved_deps is not None:
                from .deps_resolver import load_country_rows

                row_dicts, source_version = await load_country_rows(
                    self._resolved_deps
                )
                rows = [_Row.model_validate(r) for r in row_dicts]
                return rows, source_version
            # Legacy fallback: the old per-cycle stack_resolve path. Retained
            # so descriptors not yet migrated to declared deps still run under
            # a host that threads ctx.stack_resolve. The actor-resolved path
            # above is the supported one.
            return await _resolve_iso_3166_from_substrate(ctx)

        if cfg.list_source.startswith(_INLINE_PREFIX):
            rows = _parse_inline_rows(cfg.list_source)
            return rows, f"inline@n={len(rows)}"

        if cfg.list_source.startswith(_URL_PREFIX):
            # Wave-C scope — surface a clear error rather than silently
            # half-implementing the fetch. The descriptor schema accepts
            # the value; the discovery cycle fails fast with a useful
            # message until the URL fetcher lands.
            raise NotImplementedError(
                f"list_source 'url:...' is reserved for Wave C; "
                f"use 'iso_3166' (substrate-cached snapshot) or "
                f"'inline:<json>' (test escape hatch) until the URL "
                f"fetcher is wired. Got: {cfg.list_source!r}"
            )

        if cfg.list_source.startswith(_SUBSTRATE_PREFIX):
            raise NotImplementedError(
                f"list_source 'substrate:...' is reserved; "
                f"use 'iso_3166' for the default substrate snapshot. "
                f"Got: {cfg.list_source!r}"
            )

        raise ValueError(
            f"unrecognised list_source at discovery time: {cfg.list_source!r}"
        )

    # ----- health -----------------------------------------------------

    async def healthcheck(self, ctx: DiscoveryContext) -> DiscoveryHealth:
        """Cheap probe: report the last cycle's count + any cached error."""
        if self._last_error:
            state: Literal["healthy", "degraded", "unhealthy"] = "unhealthy"
        elif self._last_emitted == 0 and self._last_source_version == "":
            # Never run — say healthy (the runtime probes pre-first-cycle
            # to decide whether to schedule the cron at all).
            state = "healthy"
        else:
            state = "healthy"
        return DiscoveryHealth(
            state=state,
            last_error=self._last_error,
            candidates_24h=self._last_emitted,
            materialized_targets=self._last_emitted,
            detail={
                "last_source_version": self._last_source_version,
                "kind": KIND_NAME,
            },
        )


# Public class export the registry walker resolves via getattr(module,
# "HANDLER", None) — keeps the registry parity with sibling kinds.
HANDLER = CountryListDiscovery
DISCOVERY_HANDLER = CountryListDiscovery


async def discover(ctx: DiscoveryContext) -> AsyncIterator[CandidateTarget]:
    """Module-level convenience entry — instantiates the handler per call.

    The registry walker prefers a class via ``HANDLER`` when both are
    present; this thin async-generator delegate exists so the walker's
    fallback path (module-level ``discover`` callable) also resolves
    cleanly. Production code instantiates :class:`CountryListDiscovery`
    once at descriptor activation; this delegate is a convenience for
    one-off scripts.
    """
    handler = CountryListDiscovery()
    async for candidate in handler.discover(ctx):
        yield candidate


async def healthcheck(ctx: DiscoveryContext) -> DiscoveryHealth:
    """Module-level convenience health probe — mirrors :func:`discover`."""
    handler = CountryListDiscovery()
    return await handler.healthcheck(ctx)


__all__ = [
    "CONFIG_SCHEMA",
    "CountryListDiscovery",
    "CountryListDiscoveryConfig",
    "DISCOVERY_HANDLER",
    "HANDLER",
    "KIND_NAME",
    "SCHEMA_VERSION",
    "discover",
    "healthcheck",
]
