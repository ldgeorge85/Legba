# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S-1 — tests for the embedded no-auth source catalog bring-up script.

Two layers, per the S-1 brief:

1. Schema validation of EVERY embedded catalog definition against the real
   ``SourceDescriptor`` pydantic schema AND the real per-kind handler config
   schemas (``RSSConfig`` / ``GeoJSONConfig``) — **no live HTTP**.
2. Registration of the full catalog into the per-session migrated test DB via
   the script's own logic (``register_catalog`` / ``seed_credibility``),
   asserting head rows + ``source_credibility`` rows (tier /
   state_affiliation from migration 0031) and idempotent re-runs.
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

from legba.data.postgres import PostgresStore
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.source import SourceDescriptor
from legba.data.sources.geojson import GeoJSONConfig
from legba.data.sources.rss import RSSConfig

# ---------------------------------------------------------------------------
# Import the script module. ``_p17_registrar`` (a transitive import) sets a
# process-global LEGBA_DATA_PG_DB default at import time — snapshot/restore so
# importing the script here can't leak DB selection into sibling tests.
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[2] / "scripts"
_HAD_PG_DB = "LEGBA_DATA_PG_DB" in os.environ
sys.path.insert(0, str(_SCRIPTS_DIR))
try:
    from bringup_register_source_catalog import (  # noqa: E402
        CATALOG,
        SCORED_BY,
        VALID_TIERS,
        build_descriptor,
        credibility_rows,
        cron_for,
        register_catalog,
        seed_credibility,
    )
finally:
    if not _HAD_PG_DB:
        os.environ.pop("LEGBA_DATA_PG_DB", None)


# 0014-baseline hosts that the catalog deliberately re-lists (its seed is a
# no-op for them: ON CONFLICT DO NOTHING keeps the baseline row).
_BASELINE_OVERLAP_HOSTS = {"npr.org", "economist.com"}

# Hosts the S-1 brief explicitly requires the state-affiliation flag on.
_REQUIRED_STATE_AFFILIATED = {
    "tass.com", "xinhuanet.com", "globaltimes.cn",
    "aa.com.tr", "tehrantimes.com", "voanews.com",
}


# ---------------------------------------------------------------------------
# 1. Catalog definitions validate against the real schemas (no HTTP, no DB)
# ---------------------------------------------------------------------------


def test_catalog_size_in_brief_band():
    assert 35 <= len(CATALOG) <= 50


def test_catalog_ids_and_urls_unique():
    ids = [e.id for e in CATALOG]
    urls = [e.url for e in CATALOG]
    assert len(set(ids)) == len(ids)
    assert len(set(urls)) == len(urls)


@pytest.mark.parametrize("entry", CATALOG, ids=lambda e: e.id)
def test_every_definition_validates_against_source_descriptor_schema(entry):
    desc = build_descriptor(entry)
    assert isinstance(desc, SourceDescriptor)
    # Shared open poll source, mirroring the registered YAML descriptors.
    assert desc.identity.state == LifecycleState.ACTIVE
    assert desc.acquisition == "poll"
    assert desc.scope.owner_tenant == "shared"
    assert desc.subscription_policy == "open"
    assert desc.cadence is not None and desc.cadence.schedule is not None
    # Selector-matchable metadata is the point of the catalog.
    assert desc.scope.tags, "catalog source must advertise tags"
    assert desc.scope.languages, "catalog source must advertise a language"
    # Round-trip through dict — the YAML-descriptor parse shape (see
    # scripts/bringup_register_sources.py::_load, which parses strict=False).
    reparsed = SourceDescriptor.model_validate(desc.model_dump(mode="python"), strict=False)
    assert reparsed.identity.id == entry.id


@pytest.mark.parametrize("entry", CATALOG, ids=lambda e: e.id)
def test_every_definition_validates_against_handler_config_schema(entry):
    desc = build_descriptor(entry)
    raw_config = {key: value["raw"] for key, value in desc.config.items()}
    if entry.kind == "rss":
        cfg = RSSConfig.model_validate(raw_config)
    elif entry.kind == "geojson":
        cfg = GeoJSONConfig.model_validate(raw_config)
    else:
        pytest.fail(f"unknown catalog kind {entry.kind!r}")
    assert cfg.url == entry.url


def _enrichment_kinds(entry):
    return [s.kind for s in build_descriptor(entry).pipeline.enrichment]


def test_nws_geojson_feed_gets_text_enrichment():
    """The NWS Severe/Extreme alert feed carries rich English headlines; it
    opts into language + entity enrichment (enrich_text=True) so it stops
    dragging language/entity coverage (the live audit's 0%-enriched 400
    signals). Other geojson feeds stay geocode-only."""
    nws = next(e for e in CATALOG if e.id == "source.nws.active_alerts")
    assert nws.enrich_text is True
    kinds = _enrichment_kinds(nws)
    # language_detect must precede ner_multilingual (ner uses detected lang).
    assert kinds.index("language_detect") < kinds.index("ner_multilingual")
    assert "geocode" in kinds


def test_default_geojson_feed_is_geocode_only():
    """A structured feed without enrich_text keeps the lean geocode-only chain
    (no text enrichers on high-volume/low-text GIS feeds like USGS/NASA)."""
    plain = next(
        e for e in CATALOG if e.kind == "geojson" and not e.enrich_text
    )
    assert _enrichment_kinds(plain) == ["geocode"]


def test_fact_extract_news_feeds_get_the_stage():
    """The text-rich news feeds opted into fact extraction (graph-and-data
    Wave-1b item 4) carry a fact_extractor stage AFTER ner_multilingual and
    BEFORE geocode; the geocode stage stays last."""
    fact_ids = {"source.france24.english", "source.cna.all",
                "source.npr.world", "source.economist.international"}
    seen = set()
    for entry in CATALOG:
        if entry.id not in fact_ids:
            continue
        seen.add(entry.id)
        assert entry.fact_extract is True
        kinds = _enrichment_kinds(entry)
        assert "fact_extractor" in kinds, entry.id
        assert kinds.index("ner_multilingual") < kinds.index("fact_extractor")
        assert kinds.index("fact_extractor") < kinds.index("geocode")
    assert seen == fact_ids, f"missing fact-extract feeds: {fact_ids - seen}"


def test_non_fact_extract_feed_has_no_fact_stage():
    """A news feed NOT opted in (incremental rollout) keeps the plain chain —
    fact extraction is per-descriptor opt-in, not blanket-on."""
    plain = next(
        e for e in CATALOG
        if e.kind == "rss" and not e.fact_extract
    )
    assert "fact_extractor" not in _enrichment_kinds(plain)


@pytest.mark.parametrize("entry", CATALOG, ids=lambda e: e.id)
def test_cadences_are_valid_and_conservative(entry):
    from croniter import croniter

    expr = cron_for(entry)
    assert croniter.is_valid(expr)
    # The runtime converts poll crons to constant-period reminders.
    from legba.runtime.dapr_cron import cron_to_reminder_timing

    due, period = cron_to_reminder_timing(expr)
    assert period.total_seconds() == entry.cadence_minutes * 60
    # Conservative-cadence rule: >= 1h for RSS publishers; the structured
    # hazard feeds (geojson) may go down to 30 min.
    floor_minutes = 60 if entry.kind == "rss" else 30
    assert entry.cadence_minutes >= floor_minutes


def test_credibility_seed_rows_consistent_and_flagged():
    rows = credibility_rows()           # raises on per-host conflicts
    hosts = [r[0] for r in rows]
    assert len(set(hosts)) == len(hosts)
    by_host = {r[0]: r for r in rows}
    for host, score, rationale, scored_by, tier, state_aff in rows:
        assert host and "/" not in host and not host.startswith("www.")
        assert 0.0 <= score <= 1.0
        assert rationale
        assert scored_by == SCORED_BY
        assert tier in VALID_TIERS
        assert isinstance(state_aff, bool)
    for host in _REQUIRED_STATE_AFFILIATED:
        assert by_host[host][5] is True, f"{host} must be state_affiliation=True"
    # every catalog entry contributes a credibility seed host
    assert {e.cred_host for e in CATALOG} == set(hosts)


# ---------------------------------------------------------------------------
# 2. Registration into the migrated per-session test DB via the script logic
# ---------------------------------------------------------------------------


async def _catalog_rows(pg: PostgresStore, ids: list[str]) -> set[tuple[str, str]]:
    """Every ``(descriptor_id, version)`` currently stored for ``ids``.

    The unit the scoped teardown below works in: a set difference against a
    pre-run snapshot is exactly "the rows this test wrote".
    """
    async with pg.acquire() as conn:
        rows = await conn.fetch(
            "SELECT descriptor_id, version FROM source_descriptors "
            "WHERE descriptor_id = ANY($1::text[])",
            ids,
        )
    return {(r["descriptor_id"], r["version"]) for r in rows}


@pytest.fixture()
async def retired_catalog_descriptors(migrated_pg):
    """Retire EXACTLY the ``source_descriptors`` rows the wrapped test writes.

    THE ROOTED CAUSE OF FIVE NIGHTLY ALLOWLIST ENTRIES (task #23).
    ``source_descriptors`` is ONE table shared by the whole single-process
    suite — ``migrated_pg`` is session-scoped. The registration test below
    writes the entire embedded catalog into it: 35-50 ACTIVE ``is_head`` rows
    spanning all four ``scope.source_class`` values, each with the catalog
    entry's ``geo`` tags. Nothing removed them again.

    ``collection_gap._match_candidate_sources`` — the "reuse before create"
    cross-reference behind every ``collection_requirements`` proposal — reads
    that table GLOBALLY: *any* ``is_head`` source whose ``scope.source_class``
    is one of the dimension's wanted classes and whose ``scope.geo`` is empty
    or overlaps the desk. So from the moment this module had run, five sibling
    tests in ``tests/data_pkg/test_collection_requirements.py`` were reading
    35-50 candidate sources they never seeded:

        test_match_candidate_sources_prefers_non_active_and_respects_geo
        test_match_candidate_sources_no_geo_filter_when_desk_has_none
        test_attach_candidates_fillable_with_suggested_fetch_url
        test_attach_candidates_unfillable_when_nothing_matches
        test_handle_drains_source_request_backlog

    …and exactly those five, no others: the file's other candidate-reading
    tests survive because the catalog rows are ``active`` (the ORDER BY puts
    them last, and an active candidate never contributes a
    ``suggested_fetch_url``). Reproduce the pre-fix behaviour by deleting this
    fixture and running

        bash scripts/run_tests_in_container.sh \\
          tests/data_pkg/test_source_catalog_bringup.py \\
          tests/data_pkg/test_collection_requirements.py -p no:randomly

    — those five fail, and the victim file alone passes (21 passed). It hides
    in FILE order only because ``test_collection_requirements`` sorts before
    ``test_source_catalog_bringup``; pytest-randomly sorts MODULES by a
    seed-keyed crc32 with no regard for directory, so roughly every other seed
    inverts them. That is why the 2026-08-09 sweep saw three quiet nights and
    the entries refired on 08-14/15.

    SCOPED, NOT A WIPE: only ``(descriptor_id, version)`` pairs absent from
    the pre-run snapshot are removed, so an operator-owned head that was
    already present (``register_catalog``'s ``_STICKY_OFF_STATES`` preserve
    path) survives untouched. ``source_credibility`` rows are deliberately
    left alone: they are ON CONFLICT DO NOTHING seeds over a migration-0014
    baseline this test asserts is preserved, and nothing reads them as a
    global.

    SELF-VERIFYING: the teardown asserts the table is back to its snapshot.
    An ERROR on this fixture is the regression pin — it fires the moment the
    retirement stops covering what the registration writes (a new catalog id
    shape, a second write path), instead of leaving it to a shuffled nightly
    weeks later.
    """
    ids = [e.id for e in CATALOG]
    pg = PostgresStore(migrated_pg)
    await pg.connect()
    try:
        before = await _catalog_rows(pg, ids)
        try:
            yield
        finally:
            # The FAILURE path leaks just as hard as the success one — a test
            # that dies half way through registration still leaves whatever it
            # got as far as writing.
            mine = await _catalog_rows(pg, ids) - before
            if mine:
                async with pg.acquire() as conn:
                    await conn.executemany(
                        "DELETE FROM source_descriptors "
                        "WHERE descriptor_id = $1 AND version = $2",
                        sorted(mine),
                    )
            left = await _catalog_rows(pg, ids)
            assert left == before, (
                "catalog descriptor retirement is no longer scoped to what "
                f"the test wrote — {sorted(left ^ before)} differ from the "
                "pre-run snapshot; the collection_requirements "
                "order-dependency is back"
            )
    finally:
        await pg.close()


async def test_catalog_registers_head_rows_and_credibility_rows(
    migrated_pg, retired_catalog_descriptors
):
    pg = PostgresStore(migrated_pg)
    await pg.connect()
    reg = DescriptorRegistry(pg)
    await reg.start()
    try:
        results = await register_catalog(pg, reg)
        failed = [r for r in results if r.action == "failed"]
        assert not failed, f"failed registrations: {[(r.descriptor_id, r.detail) for r in failed]}"
        assert all(r.action == "registered" for r in results)
        assert len(results) == len(CATALOG)

        async with pg.acquire() as conn:
            heads = await conn.fetch(
                "SELECT descriptor_id, state FROM source_descriptors WHERE is_head"
            )
            head_ids = {r["descriptor_id"] for r in heads}
            assert {e.id for e in CATALOG} <= head_ids
            assert all(
                r["state"] == "active"
                for r in heads
                if r["descriptor_id"] in {e.id for e in CATALOG}
            )

        inserted, skipped = await seed_credibility(pg)
        assert inserted + skipped == len(credibility_rows())
        # npr.org / economist.com are already seeded by migration 0014 —
        # the catalog seed must NOT overwrite the baseline.
        assert skipped == len(_BASELINE_OVERLAP_HOSTS)

        async with pg.acquire() as conn:
            tass = await conn.fetchrow(
                "SELECT score, tier, state_affiliation, scored_by "
                "FROM source_credibility WHERE source_host = 'tass.com'"
            )
            assert tass is not None
            assert tass["tier"] == "wire"
            assert tass["state_affiliation"] is True
            assert tass["scored_by"] == SCORED_BY

            gdacs = await conn.fetchrow(
                "SELECT tier, state_affiliation FROM source_credibility "
                "WHERE source_host = 'gdacs.org'"
            )
            assert gdacs["tier"] == "gov" and gdacs["state_affiliation"] is True

            # Migration-0031 backfill on the 0014 baseline rows.
            npr = await conn.fetchrow(
                "SELECT score, tier, state_affiliation, scored_by "
                "FROM source_credibility WHERE source_host = 'npr.org'"
            )
            assert npr["scored_by"] == "system.seed"      # baseline preserved
            assert float(npr["score"]) == pytest.approx(0.90)
            assert npr["tier"] == "wire"
            assert npr["state_affiliation"] is False
            dw = await conn.fetchrow(
                "SELECT tier, state_affiliation FROM source_credibility "
                "WHERE source_host = 'dw.com'"
            )
            assert dw["tier"] == "wire" and dw["state_affiliation"] is True

        # Idempotency: a re-run is all "unchanged" + zero new credibility rows.
        rerun = await register_catalog(pg, reg)
        assert all(r.action == "unchanged" for r in rerun), [
            (r.descriptor_id, r.action, r.detail) for r in rerun if r.action != "unchanged"
        ]
        inserted2, skipped2 = await seed_credibility(pg)
        assert inserted2 == 0
        assert skipped2 == len(credibility_rows())
    finally:
        await reg.stop()
        await pg.close()
