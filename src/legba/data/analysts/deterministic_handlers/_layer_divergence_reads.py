# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Program 6 L2 — the READ half of ``layer_divergence``: the four queries that
turn a loaded map into one desk's :class:`TargetBundle`, plus the synthetic
(no-DB) path the unit tests build bundles through.

Extracted from :mod:`legba.data.analysts.deterministic_handlers
.layer_divergence` for the module-size gate, along the same seam
``_layer_fold`` came off: this module knows about TABLES and nothing about the
statistic; the divergence module knows about the statistic and reaches a
database only through these four functions. Neither imports the other's
concerns, so the ``_Cfg`` object stays on the statistic's side and
:func:`fetch_bundle` takes the two scalars it actually needs.

NO OPTION READS LIVE HERE, deliberately. ``tests/data_pkg
/test_handler_options_x1.py``'s reachability sweep reads a sub-handler's OWN
leaf module for ``options.get("…")`` literals and follows delegation only
through its declared ``_DELEGATES`` table. Keeping every option read in
``layer_divergence.py`` means the sweep sees all of them without a delegation
entry, and a knob can never go quietly unreachable — which is the entire defect
that catalog exists to close.

WHY THE DESK -> COUNTRY JOIN IS A JOIN AND NOT A STRING RULE
-------------------------------------------------------------

``source_layers`` is keyed by ISO-3166-1 country and ``desk_apertures`` by
``target_id``; migration 0214 says out loud that the mapping between those two
vocabularies is ``runtime/target_resolution.py``'s and not the table's, and
``layers/loader.py`` refuses to guess it ("a country's layer TABLE is a fact
about the country's information environment; which DESK reads it is a separate
fact this module refuses to guess"). So this module does not parse
``country_watch_ru`` into ``RU`` either. It joins on ``map_version``, which the
LOADER stamps onto both halves of one load — a recorded fact about which map
those rows came from, not an inference from a name. An operator who wants a
pairing the data does not record supplies it explicitly, as
``"<target_id>:<CC>"``.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from ._layer_fold import SignalItem, TargetBundle

#: The shape a ``target_id`` may take. Mirrors
#: ``handler_options_programs._DIVERGENCE_TARGET_RE``'s target half — the
#: catalog refuses a malformed DESCRIPTOR value ahead of the run, and this
#: refuses one that reached the handler by any other route (a forced run, a
#: test, a future caller). The same illegal shape refused twice, by two
#: mechanisms, on purpose — the ``layer_map_schema`` precedent.
_TARGET_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

#: Time columns a day may be bucketed by — CHOICE-LOCKED because the value is
#: interpolated into :data:`_SIGNAL_SCAN_SQL` rather than bound as a parameter
#: (a column name cannot be a bind parameter). ``fetched_at`` leads and is the
#: shipped default because it is the INDEXED column
#: (``signals_fetched_at_idx``, migration 0001); ``created_at`` carries no
#: index at all, so bucketing by it turns a bounded scan into a sequential one
#: over the whole table. Both are offered because they answer slightly
#: different questions — when the source published into our reach, and when the
#: row landed — and an operator comparing a replay against a live run may need
#: the second.
DAY_BASIS_CHOICES: tuple[str, ...] = ("fetched_at", "created_at")
DEFAULT_DAY_BASIS: str = "fetched_at"

#: Desk -> country, joined on the fact the L0 LOADER establishes: one
#: ``load_layer_map`` run stamps the SAME ``map_version`` onto both halves of
#: the map (``source_layers`` for the country, ``desk_apertures`` for the
#: desk). See the module banner for why this is a join and not a string rule.
#: Both tables hold hundreds of open rows, so the hash join over two filtered
#: scans is cheap; neither carries an index on ``map_version`` and neither
#: needs one at this size.
_RESOLVE_DESKS_SQL = """
    SELECT DISTINCT a.target_id, l.country, a.map_version
      FROM public.desk_apertures a
      JOIN public.source_layers l ON l.map_version = a.map_version
     WHERE a.valid_until IS NULL
       AND l.valid_until IS NULL
"""

#: This desk's whole aperture — ``idx_desk_apertures_target_open`` covers it.
_APERTURE_SQL = """
    SELECT layer, declared, reason, map_version
      FROM public.desk_apertures
     WHERE target_id = $1 AND valid_until IS NULL
"""

#: This country's layer table — ``idx_source_layers_country_open`` covers it.
_LAYER_MAP_SQL = """
    SELECT source_id, layer
      FROM public.source_layers
     WHERE country = $1 AND valid_until IS NULL
"""

#: The ONE scan over ``signals``, once per desk per run. Three deliberate
#: choices, each of which a live ``EXPLAIN`` should confirm:
#:
#:   * ``AS MATERIALIZED`` — without it the outer ``ORDER BY ts DESC LIMIT``
#:     can push down into a plain index scan on the time column, which then
#:     FILTERS a whole window of the corpus to find the handful of rows that
#:     are about this country. Materializing forces the three ANDed predicates
#:     to choose their own access path first (a bitmap AND of
#:     ``signals_geo_gin`` and ``signals_source_idx`` is the intended plan) and
#:     only then sorts what survived.
#:   * the ``geo &&`` predicate leads because it is the SELECTIVE one: an
#:     outlet mapped into a country's layer table still publishes mostly about
#:     somewhere else, so the country filter is what makes this bounded.
#:   * the time column is INTERPOLATED, never bound — a column name cannot be a
#:     bind parameter — and it is choice-locked to :data:`DAY_BASIS_CHOICES` by
#:     the option catalog AND re-checked in :func:`fetch_bundle`, so no
#:     caller-supplied string can reach this template.
#:
#: The ``LIMIT`` is passed as ``max_rows + 1`` so a truncated read is DETECTED
#: (the +1 probe) rather than silently reported as a complete one.
_SIGNAL_SCAN_SQL = """
    WITH scoped AS MATERIALIZED (
        SELECT s.id,
               s.source_id,
               s.{ts} AS ts,
               s.content_hash,
               s.payload->>'title' AS title
          FROM public.signals s
         WHERE s.geo && $1::text[]
           AND s.source_id = ANY($2::text[])
           AND s.{ts} >= $3
           AND s.{ts} < $4
    )
    SELECT id, source_id, ts, content_hash, title
      FROM scoped
     ORDER BY ts DESC, id
     LIMIT $5
"""

_LAST_BODY_SQL = """
    SELECT body FROM analyst_outputs
     WHERE analyst_id = $1 AND kind = 'finding'
     ORDER BY produced_at DESC
     LIMIT 1
"""


def parse_target_option(raw: Any) -> tuple[str, str] | None:
    """``"country_watch_ru:RU"`` -> ``("country_watch_ru", "RU")``.

    A bare ``"country_watch_ru"`` yields an EMPTY country half, which
    :func:`resolve_desks` fills from the recorded ``map_version`` join. A
    malformed entry yields ``None`` and is named on the receipt rather than
    silently dropped.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    target_id, sep, country = text.partition(":")
    target_id = target_id.strip()
    if not _TARGET_ID_RE.match(target_id):
        return None
    if not sep:
        return (target_id, "")
    country = country.strip().upper()
    if len(country) != 2 or not country.isalpha():
        return None
    return (target_id, country)


async def resolve_desks(
    conn: Any, wanted: Sequence[tuple[str, str]]
) -> tuple[list[tuple[str, str, str]], list[dict[str, Any]]]:
    """``([(target_id, country, map_version)], unresolved)``.

    With NO named targets the population is every desk whose aperture map
    resolves to a country — the sweep widens on its own as maps are curated and
    loaded, and measures nothing at all while the tables are empty, which is
    the honest behaviour for an instrument whose input is hand-curated.
    Naming targets bounds it to those.
    """
    rows = await conn.fetch(_RESOLVE_DESKS_SQL)
    joined = {
        str(r["target_id"]): (str(r["country"]), str(r["map_version"]))
        for r in rows
    }
    if not wanted:
        return (
            sorted(
                (tid, country, version)
                for tid, (country, version) in joined.items()
            ),
            [],
        )

    resolved: list[tuple[str, str, str]] = []
    unresolved: list[dict[str, Any]] = []
    for target_id, country in wanted:
        hit = joined.get(target_id)
        if hit is not None:
            resolved.append((target_id, country or hit[0], hit[1]))
        elif country:
            # The operator named the country explicitly, so a desk whose
            # APERTURE half has not been loaded (or was loaded at a different
            # map_version) is still measurable — the layer table is what the
            # counts need. The missing aperture then shows up as six UNDECLARED
            # layers, which excludes every pair and says so on the receipt.
            resolved.append((target_id, country, ""))
        else:
            unresolved.append({
                "target_id": target_id,
                "reason": (
                    "no open desk_apertures row shares a map_version with any "
                    "open source_layers row — load the map, or name the "
                    "country explicitly as '<target_id>:<CC>'"
                ),
            })
    return sorted(resolved), unresolved


async def fetch_bundle(
    conn: Any,
    *,
    target_id: str,
    country: str,
    map_version: str,
    as_of: date,
    window_days: int,
    day_basis: str,
    max_rows: int,
) -> TargetBundle:
    """The three reads that make one desk's bundle.

    The signal scan is SKIPPED when the country's layer map is empty: with no
    source in any layer there is nothing a count could be about, and running
    the scan anyway would spend the run's one expensive query to produce zeros.
    """
    bundle = TargetBundle(
        target_id=target_id, country=country, map_version=map_version
    )
    for row in await conn.fetch(_APERTURE_SQL, target_id):
        bundle.apertures[str(row["layer"])] = (
            str(row["declared"]), str(row["reason"] or "")
        )
    for row in await conn.fetch(_LAYER_MAP_SQL, country):
        bundle.layer_map[str(row["source_id"])] = str(row["layer"])
    if not bundle.layer_map:
        return bundle

    start = datetime.combine(
        as_of - timedelta(days=max(1, int(window_days)) - 1),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    end = datetime.combine(
        as_of + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
    )
    basis = day_basis if day_basis in DAY_BASIS_CHOICES else DEFAULT_DAY_BASIS
    rows = await conn.fetch(
        _SIGNAL_SCAN_SQL.format(ts=basis),
        [country],
        sorted(bundle.layer_map),
        start,
        end,
        int(max_rows) + 1,
    )
    bundle.rows_truncated = len(rows) > max_rows
    for row in rows[:max_rows]:
        bundle.signals.append(SignalItem(
            signal_id=str(row["id"]),
            source_id=str(row["source_id"]),
            ts=row["ts"],
            content_hash=str(row["content_hash"] or ""),
            title=str(row["title"] or ""),
        ))
    return bundle


async def last_emitted_body(pool: Any, analyst_id: str) -> str | None:
    """Body of the most recent FEED finding this analyst emitted, or ``None``.

    A trace-only suppressed run writes no ``analyst_outputs`` row, so this is
    the last NON-suppressed summary — exactly what a re-swept identical
    divergence set should be deduped against (the ``indicator_tracker``
    contract, verbatim).
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_LAST_BODY_SQL, analyst_id)
    return row["body"] if row else None


def synthetic_bundles(inputs: Sequence[Mapping[str, Any]]) -> list[TargetBundle]:
    """Pre-shaped ``inputs`` -> bundles: the ``deps=None`` unit-test path.

    One input row per desk::

        {"target_id": ..., "country": "RU", "map_version": ...,
         "layer_map": {source_id: layer},
         "apertures": [{"layer":…, "declared":…, "reason":…}],
         "signals": [SignalItem | {"id", "source_id", "ts", …}]}

    A row with no ``target_id`` is skipped rather than defaulted — a bundle
    with no desk has nothing to be a measurement OF.
    """
    bundles: list[TargetBundle] = []
    for raw in inputs:
        if not isinstance(raw, Mapping) or not raw.get("target_id"):
            continue
        bundle = TargetBundle(
            target_id=str(raw["target_id"]),
            country=str(raw.get("country") or ""),
            map_version=str(raw.get("map_version") or ""),
            layer_map={
                str(k): str(v) for k, v in (raw.get("layer_map") or {}).items()
            },
        )
        for entry in raw.get("apertures") or ():
            if isinstance(entry, Mapping) and entry.get("layer"):
                bundle.apertures[str(entry["layer"])] = (
                    str(entry.get("declared") or "unmeasured"),
                    str(entry.get("reason") or ""),
                )
        for entry in raw.get("signals") or ():
            if isinstance(entry, SignalItem):
                bundle.signals.append(entry)
            elif isinstance(entry, Mapping):
                bundle.signals.append(SignalItem(
                    signal_id=str(entry.get("id") or entry.get("signal_id") or ""),
                    source_id=str(entry.get("source_id") or ""),
                    ts=entry["ts"],
                    content_hash=str(entry.get("content_hash") or ""),
                    title=str(entry.get("title") or ""),
                ))
        bundles.append(bundle)
    return bundles


__all__ = [
    "DAY_BASIS_CHOICES",
    "DEFAULT_DAY_BASIS",
    "fetch_bundle",
    "last_emitted_body",
    "parse_target_option",
    "resolve_desks",
    "synthetic_bundles",
]
