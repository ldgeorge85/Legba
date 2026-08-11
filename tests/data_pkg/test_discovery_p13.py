# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-13 — polymorphic discovery: G20 fix + source-discovery + validate-before-register.

Runs against the dev rig (legba_pivot_test + dev NATS). Exercises:

  * The G20 fix: country_list_discovery materialises the G20 country targets
    against the live ``iso_countries`` seed via the *actor-resolved Postgres
    dep* (NOT the retired per-target ``ctx.stack_resolve``). The old
    ``RuntimeError: ... requires ctx.stack_resolve`` is gone.
  * Source-discovery: a query_source_discovery candidate validates (liveness +
    trial pull/parse) then registers into ``source_descriptors``.
  * An invalid candidate (dead feed / unparseable) is rejected — never
    registered — and routed to the DLQ.
  * Selector auto-wire: a target whose ``source_selector`` matches a
    freshly-registered source auto-wires (gated by subscription_policy),
    integrating the W2 SourceRef engine.

Test isolation: each test stamps unique descriptor ids so concurrent runs in
the same DB don't collide on the head-row unique index.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from uuid import uuid4

import asyncpg
import pytest

from legba.data.config import PostgresConfig
from legba.data.discovery import (
    CandidateSource,
    ResolvedDiscoveryDeps,
    auto_wire_discovered_source,
    run_source_discovery_cycle,
    run_target_discovery_cycle,
    validate_candidate_source,
)
from legba.data.discovery.country_list_discovery import (
    CountryListDiscovery,
    CountryListDiscoveryConfig,
)
from legba.data.discovery import DiscoveryContext, InMemoryStateStore
from legba.data.discovery.deps_resolver import (
    load_country_rows,
    resolve_discovery_deps,
)
from legba.data.postgres import PostgresStore
from legba.data.provenance import canonical_json
from legba.data.schemas import TargetDescriptor, content_hash
from legba.data.schemas.source import SourceDescriptor
from legba.data.sources._contract import Signal, SourceHealth
from legba.runtime.deps import StandardDeps


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# G20 ISO-2 set (the production rollout set — G20 minus the EU, which has no
# ISO-3166-1 alpha-2). Matches descriptors/discovery_geopolitical_g20.yaml.
# ---------------------------------------------------------------------------

_G20_ISO2 = [
    "AR", "AU", "BR", "CA", "CN", "DE", "FR", "GB", "ID", "IN",
    "IT", "JP", "KR", "MX", "RU", "SA", "TR", "US", "ZA",
]


# ---------------------------------------------------------------------------
# Substrate helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
async def store() -> AsyncIterator[PostgresStore]:
    # Isolate onto the pivot test DB (the documented dev rig) — NEVER the live
    # `legba` DB. `PostgresConfig.from_env()` here resolved to production and
    # this test materialises discovery descriptors, which the LIVE runtime's
    # reactive workers then churned into the `country_g20_*` workingset
    # (silently stalling the per-/cross-target country analysts). Mirror the
    # conftest pivot config so the writes land in `legba_pivot_test`.
    cfg = PostgresConfig(
        host="127.0.0.1", port=5432, user="legba", password="legba",
        database=os.environ.get("LEGBA_PIVOT_PG_DB", "legba_pivot_test"),
    )
    s = PostgresStore(cfg)
    await s.connect()
    try:
        yield s
    finally:
        await s.close()


async def _insert_target_row(conn: asyncpg.Connection, descriptor: TargetDescriptor) -> None:
    body = descriptor.model_dump(mode="json", by_alias=True)
    version = content_hash(descriptor)
    body["identity"]["version"] = version
    await conn.execute(
        """
        INSERT INTO target_descriptors
          (descriptor_id, version, schema_uri, is_head, abstraction_level,
           state, owner, name, body, inherits, created_at)
        VALUES ($1, $2, $3, TRUE, $4, $5, $6, $7, $8::jsonb, $9, NOW())
        ON CONFLICT (descriptor_id, version) DO NOTHING
        """,
        descriptor.identity.id,
        version,
        descriptor.identity.schema_uri,
        descriptor.identity.abstraction_level.value,
        descriptor.identity.state.value,
        descriptor.identity.owner,
        descriptor.identity.name,
        canonical_json(body).decode("utf-8"),
        list(descriptor.identity.inherits),
    )


async def _insert_source_row(conn: asyncpg.Connection, descriptor: SourceDescriptor) -> None:
    body = descriptor.model_dump(mode="json", by_alias=True)
    version = content_hash(descriptor)
    body["identity"]["version"] = version
    await conn.execute(
        """
        INSERT INTO source_descriptors
          (descriptor_id, version, schema_uri, is_head, abstraction_level,
           kind, state, owner, name, body, inherits, created_at)
        VALUES ($1, $2, $3, TRUE, $4, $5, $6, $7, $8, $9::jsonb, $10, NOW())
        ON CONFLICT (descriptor_id, version) DO NOTHING
        """,
        descriptor.identity.id,
        version,
        descriptor.identity.schema_uri,
        descriptor.identity.abstraction_level.value,
        descriptor.identity.kind,
        descriptor.identity.state.value,
        descriptor.identity.owner,
        descriptor.identity.name,
        canonical_json(body).decode("utf-8"),
        list(descriptor.identity.inherits),
    )


# A minimal PIVOT-shaped L2 country template (sources = [SourceRef], not the
# legacy SourceBinding shape). This is what a country target inherits.
def _pivot_country_template(template_id: str) -> TargetDescriptor:
    body = {
        "identity": {
            "id": template_id,
            "name": "Country Geopolitical Template (pivot)",
            "schema_uri": "legba/target/2.0.0",
            "version": "0000000000000000",
            "abstraction_level": "L2",
            "inherits": [],
            "state": "configured",
            "owner": "legba_geopolitical",
            "created": "2026-06-01T00:00:00Z",
        },
        "scope": {
            "domain": "geo",
            "geo": ["XX"],
            "languages": ["en"],
            "entity_classes": ["country", "organization", "person"],
            "tags": [],
            "time_horizon_days": 90,
        },
        # PIVOT SourceRef: a selector that auto-wires any open rss/news source
        # in this geo. The discovery relabel chain rewrites scope.geo per
        # country; the selector matches sources advertising that geo.
        "sources": [
            {
                "source_selector": {
                    "kinds": ["rss"],
                    "tags": ["news"],
                },
                "subscription": {"tags": ["news"]},
            }
        ],
    }
    return TargetDescriptor.model_validate_json(json.dumps(body))


# The relabel chain mirroring discovery_geopolitical_g20.yaml (G20 keep + scope
# rewrite + id format + inherits + tags), targeting a unique template id.
def _g20_relabel_rules(template_id: str) -> list[dict[str, Any]]:
    return [
        {
            "source_labels": ["country_iso2"],
            "action": "keep",
            "predicate": "country_iso2 in " + json.dumps(_G20_ISO2),
        },
        {"source_labels": ["country_iso2"], "target_label": "scope.geo", "action": "set_list"},
        {
            "source_labels": ["country_iso2", "country_languages"],
            "target_label": "scope.languages",
            "action": "lookup_languages",
        },
        {
            "source_labels": ["scope.languages"],
            "target_label": "scope.languages",
            "action": "merge_list",
            "extend_with": ["en"],
        },
        {
            "source_labels": ["country_iso2"],
            "target_label": "identity.id",
            "action": "format",
            "replacement": "country_g20_{{ country_iso2 | lower }}",
        },
        {
            "source_labels": ["country_iso2"],
            "target_label": "identity.inherits",
            "action": "merge_list",
            "extend_with": [template_id],
        },
        {
            "source_labels": ["country_iso2"],
            "target_label": "tags",
            "action": "merge_list",
            "extend_with": ["geopolitical", "news", "g20"],
        },
    ]


def _g20_discovery_descriptor(discovery_id: str, template_id: str) -> TargetDescriptor:
    body = {
        "identity": {
            "id": discovery_id,
            "name": "G20 Per-Country Discovery (P-13 test)",
            "schema_uri": "legba/target/2.0.0",
            "version": "0000000000000000",
            "abstraction_level": "L2",
            "inherits": [template_id],
            "state": "active",
            "owner": "legba_geopolitical",
            "created": "2026-06-01T00:00:00Z",
        },
        "scope": {
            "domain": "geo",
            "geo": ["XX"],
            "languages": ["en"],
            "entity_classes": ["country"],
            "tags": [],
            "time_horizon_days": 90,
        },
        "discovery": {
            "kind": "country_list_discovery",
            "list_source": "iso_3166",
            "emit_per_match": True,
            "relabel": _g20_relabel_rules(template_id),
            "config": {"list_source": "iso_3166"},
        },
        "sources": [],
    }
    return TargetDescriptor.model_validate_json(json.dumps(body))


# ===========================================================================
# 1. G20 FIX — country_list materialises G20 targets via actor-resolved deps
# ===========================================================================


class TestG20Fix:
    async def test_actor_resolved_deps_loads_iso_countries(self, store: PostgresStore):
        """The G20 fix at the dep-resolution seam: load_country_rows reads
        iso_countries via the ACTOR-RESOLVED postgres dep — no ctx.stack_resolve."""
        deps = StandardDeps(pg_pool=store.pool)
        from legba.data.schemas.source import SourceDeps

        resolved = await resolve_discovery_deps(SourceDeps(postgres=True), deps)
        assert resolved.postgres is not None

        rows, version = await load_country_rows(resolved)
        assert len(rows) == 249
        assert version == "iso_3166@n=249"
        iso2s = {r["iso2"] for r in rows}
        assert set(_G20_ISO2).issubset(iso2s)

    async def test_country_list_discover_via_bound_deps_no_stack_resolve(
        self, store: PostgresStore
    ):
        """country_list_discovery emits candidates via bound resolved deps,
        with ctx.stack_resolve=None — the old blocker is gone."""
        deps = StandardDeps(pg_pool=store.pool)
        from legba.data.schemas.source import SourceDeps

        resolved = await resolve_discovery_deps(SourceDeps(postgres=True), deps)

        handler = CountryListDiscovery()
        handler.bind_resolved_deps(resolved)
        ctx = DiscoveryContext(
            discovery_id="g20_test",
            config=CountryListDiscoveryConfig(list_source="iso_3166"),
            state_store=InMemoryStateStore(),
            stack_resolve=None,  # the retired blocker — proves it isn't used
        )
        emitted = [c async for c in handler.discover(ctx)]
        assert len(emitted) == 249
        keys = {c.natural_key for c in emitted}
        assert set(_G20_ISO2).issubset(keys)

    async def test_missing_declared_dep_raises_at_resolution_not_mid_cycle(
        self, store: PostgresStore
    ):
        """Declaring postgres with no pool fails loudly at resolution — the
        error now points at the declaration, not a per-cycle plumbing gap."""
        from legba.data.schemas.source import SourceDeps

        deps = StandardDeps(pg_pool=None)  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="deps.postgres"):
            await resolve_discovery_deps(SourceDeps(postgres=True), deps)

    async def test_g20_targets_materialise_end_to_end(self, store: PostgresStore):
        """ACCEPTANCE: country_list materialises the 19 G20 country targets
        against the iso_countries seed; the old stack_resolve error is gone."""
        suffix = uuid4().hex[:8]
        template_id = f"template_country_p13_{suffix}"
        discovery_id = f"discovery_g20_p13_{suffix}"

        template = _pivot_country_template(template_id)
        discovery = _g20_discovery_descriptor(discovery_id, template_id)
        deps = StandardDeps(pg_pool=store.pool)

        async with store.transaction() as conn:
            # Clean slate for the fixed country_g20_* ids this acceptance test
            # materialises: the shared test DB may carry operator-owned ACTIVE
            # country_g20 heads (a prior bring-up), which the materialiser now
            # correctly REFUSES to overwrite (the skip_active_operator_target
            # guard). Clear them so fresh materialisation is exercised here.
            await conn.execute(
                "DELETE FROM target_descriptors WHERE descriptor_id LIKE 'country_g20_%'"
            )
            await _insert_target_row(conn, template)
            result = await run_target_discovery_cycle(
                conn, discovery, deps
            )

        # 249 candidates emitted; the G20 keep predicate keeps 19, drops 230.
        assert result.candidates_in == 249
        assert result.inserted_count == 19, (
            f"expected 19 G20 inserts, got {result.inserted_count}"
        )
        assert result.dropped_count == 230

        # Inspect the materialised rows.
        async with store.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT descriptor_id, body FROM target_descriptors
                WHERE is_head AND $1 = ANY(inherits)
                ORDER BY descriptor_id
                """,
                discovery_id,
            )
        assert len(rows) == 19

        # Spot-check Brazil: scope.geo == ["BR"], pt-BR + en in languages.
        br = next(r for r in rows if r["descriptor_id"] == f"country_g20_br")
        br_body = br["body"]
        if isinstance(br_body, str):
            br_body = json.loads(br_body)
        assert br_body["scope"]["geo"] == ["BR"]
        assert "pt-BR" in br_body["scope"]["languages"]
        assert "en" in br_body["scope"]["languages"]
        assert "g20" in br_body["scope"]["tags"]
        # Inherits both template + discovery descriptor.
        assert template_id in br_body["identity"]["inherits"]
        assert discovery_id in br_body["identity"]["inherits"]


# ===========================================================================
# 2 & 3. Source-discovery: validate-then-register + invalid rejected
# ===========================================================================


class _FakeLiveRSS:
    """A trial-pull handler that parses cleanly + returns one signal."""

    async def health_check(self, ctx: Any) -> SourceHealth:
        return SourceHealth(state="healthy")

    async def pull(self, ctx: Any, since: Any) -> AsyncIterator[Signal]:
        yield Signal(
            source_id=ctx.source_id,
            payload={"title": "Live feed item", "link": "https://live.example/1"},
            canonical_url="https://live.example/1",
        )


class _FakeDeadFeed:
    """A trial-pull handler that raises while pulling (404 / parse error)."""

    async def health_check(self, ctx: Any) -> SourceHealth:
        return SourceHealth(state="healthy")

    async def pull(self, ctx: Any, since: Any) -> AsyncIterator[Signal]:
        raise RuntimeError("HTTP 404 Not Found")
        yield  # pragma: no cover - unreachable; makes this an async generator


class _FakeUnhealthy:
    """A handler whose liveness probe reports unhealthy."""

    async def health_check(self, ctx: Any) -> SourceHealth:
        return SourceHealth(state="unhealthy", last_error="connection refused")

    async def pull(self, ctx: Any, since: Any) -> AsyncIterator[Signal]:
        yield  # pragma: no cover


class TestValidateBeforeRegister:
    async def test_live_candidate_validates(self):
        cand = CandidateSource(
            natural_key="https://live.example/feed.xml",
            source_kind="rss",
            probe_config={"url": "https://live.example/feed.xml"},
        )
        verdict = await validate_candidate_source(cand, handler=_FakeLiveRSS())
        assert verdict.valid is True
        assert verdict.live is True
        assert verdict.trial_signals == 1

    async def test_dead_feed_rejected(self):
        cand = CandidateSource(
            natural_key="https://dead.example/feed.xml",
            source_kind="rss",
            probe_config={"url": "https://dead.example/feed.xml"},
        )
        verdict = await validate_candidate_source(cand, handler=_FakeDeadFeed())
        assert verdict.valid is False
        assert "trial_pull_raised" in verdict.reason
        assert "404" in verdict.reason

    async def test_unhealthy_rejected(self):
        cand = CandidateSource(
            natural_key="https://down.example/feed.xml",
            source_kind="rss",
            probe_config={"url": "https://down.example/feed.xml"},
        )
        verdict = await validate_candidate_source(cand, handler=_FakeUnhealthy())
        assert verdict.valid is False
        assert "liveness_unhealthy" in verdict.reason


# A minimal PIVOT-shaped L2 source template (rss) for source-discovery.
def _source_template(template_id: str) -> SourceDescriptor:
    body = {
        "identity": {
            "id": template_id,
            "name": "Discovered RSS Source Template",
            "kind": "rss",
            "schema_uri": "legba/source/1.0.0",
            "version": "0000000000000000",
            "abstraction_level": "L2",
            "inherits": [],
            "state": "configured",
            "owner": "legba_geopolitical",
            "created": "2026-06-01T00:00:00Z",
        },
        "scope": {"owner_tenant": "default", "tags": ["news"]},
        "acquisition": "poll",
        "config": {"url": "https://placeholder.invalid/feed.xml"},
        "cadence": {"schedule": {"factory_kind": "cron", "raw": "*/15 * * * *"}},
        "subscription_policy": "open",
    }
    return SourceDescriptor.model_validate_json(json.dumps(body))


def _source_discovery_descriptor(discovery_id: str, template_id: str, feeds: list[dict]) -> SourceDescriptor:
    body = {
        "identity": {
            "id": discovery_id,
            "name": "RSS feed-list source discovery (P-13 test)",
            "kind": "query_source_discovery",
            "schema_uri": "legba/source/1.0.0",
            "version": "0000000000000000",
            "abstraction_level": "L2",
            "inherits": [template_id],
            "state": "active",
            "owner": "legba_geopolitical",
            "created": "2026-06-01T00:00:00Z",
        },
        "scope": {"owner_tenant": "default", "tags": ["news"]},
        "acquisition": "poll",
        "subscription_policy": "open",
        "discovery": {
            "kind": "query_source_discovery",
            "validate_before_register": True,
            "relabel": [
                {
                    "source_labels": ["url"],
                    "target_label": "config.url",
                    "action": "set",
                },
                {
                    "source_labels": ["host"],
                    "target_label": "identity.id",
                    "action": "format",
                    "replacement": "src_disc_{{ host | slug }}",
                },
                {
                    "source_labels": ["host"],
                    "target_label": "identity.name",
                    "action": "format",
                    "replacement": "Discovered: {{ host }}",
                },
                {
                    "source_labels": ["tags"],
                    "target_label": "scope.tags",
                    "action": "merge_list",
                    "extend_with": ["news", "discovered"],
                },
            ],
            "config": {
                "list_source": "inline:" + json.dumps(feeds),
                "default_source_kind": "rss",
            },
        },
    }
    return SourceDescriptor.model_validate_json(json.dumps(body))


class TestSourceDiscoveryMaterialization:
    async def test_valid_source_registers_invalid_rejected(self, store: PostgresStore):
        """ACCEPTANCE: a discovered source validates-then-registers; an invalid
        candidate is rejected (never written)."""
        suffix = uuid4().hex[:8]
        template_id = f"src_template_p13_{suffix}"
        discovery_id = f"src_discovery_p13_{suffix}"

        feeds = [
            {"url": "https://live.example/feed.xml", "kind": "rss", "feed_title": "Live"},
            {"url": "https://dead.example/feed.xml", "kind": "rss", "feed_title": "Dead"},
        ]
        template = _source_template(template_id)
        discovery = _source_discovery_descriptor(discovery_id, template_id, feeds)
        deps = StandardDeps(pg_pool=store.pool)

        # Probe handler resolver: live host -> _FakeLiveRSS, dead host -> _FakeDeadFeed.
        class _ProbeRouter:
            async def health_check(self, ctx: Any) -> SourceHealth:
                return SourceHealth(state="healthy")

            async def pull(self, ctx: Any, since: Any) -> AsyncIterator[Signal]:
                if "dead" in ctx.source_id:
                    raise RuntimeError("HTTP 404 Not Found")
                yield Signal(
                    source_id=ctx.source_id,
                    payload={"title": "ok"},
                    canonical_url="https://live.example/1",
                )

        async with store.transaction() as conn:
            await _insert_source_row(conn, template)
            result = await run_source_discovery_cycle(
                conn, discovery, deps,
                probe_handler=_ProbeRouter(),
                auto_wire=False,
            )

        assert result.candidates_in == 2
        assert result.registered_count == 1, (
            f"expected 1 registered, got {result.registered_count}: "
            f"{[(o.natural_key, o.rejected, o.rejected_reason, o.source_id) for o in result.materialized]}"
        )
        assert result.rejected_count == 1

        live = next(o for o in result.materialized if "live" in o.natural_key)
        dead = next(o for o in result.materialized if "dead" in o.natural_key)
        assert live.source_id is not None and not live.rejected
        assert dead.rejected and dead.source_id is None

        # The live source landed; the dead one did NOT.
        async with store.acquire() as conn:
            srcs = await conn.fetch(
                "SELECT descriptor_id, kind, body FROM source_descriptors "
                "WHERE is_head AND $1 = ANY(inherits)",
                discovery_id,
            )
        assert len(srcs) == 1
        body = srcs[0]["body"]
        if isinstance(body, str):
            body = json.loads(body)
        assert body["config"]["url"] == "https://live.example/feed.xml"
        assert "discovered" in body["scope"]["tags"]


# ===========================================================================
# 4. Selector auto-wire — discovered source attaches to matching target
# ===========================================================================


class TestSelectorAutoWire:
    async def test_open_source_auto_wires_matching_target(self, store: PostgresStore):
        """ACCEPTANCE-adjacent: a discovered open source whose scope matches a
        target's source_selector auto-wires (gated by subscription_policy),
        via the W2 SourceRef engine."""
        suffix = uuid4().hex[:8]
        target_id = f"tgt_autowire_p13_{suffix}"
        source_id = f"src_autowire_p13_{suffix}"

        # Target with a selector SourceRef: any open rss/news source.
        target = TargetDescriptor.model_validate_json(json.dumps({
            "identity": {
                "id": target_id, "name": "autowire target",
                "schema_uri": "legba/target/2.0.0", "version": "0000000000000000",
                "abstraction_level": "L1", "inherits": [], "state": "active",
                "owner": "tester", "created": "2026-06-01T00:00:00Z",
            },
            "scope": {
                "domain": "geo", "geo": ["BR"], "languages": ["pt-BR"],
                "entity_classes": ["country"], "tags": ["news"],
                "time_horizon_days": 90,
            },
            "sources": [
                {"source_selector": {"kinds": ["rss"], "tags": ["news"]},
                 "subscription": {"tags": ["news"]}},
            ],
        }))

        # An open rss source advertising tags ⊇ {news}.
        source = SourceDescriptor.model_validate_json(json.dumps({
            "identity": {
                "id": source_id, "name": "open news rss", "kind": "rss",
                "schema_uri": "legba/source/1.0.0", "version": "0000000000000000",
                "abstraction_level": "L1", "inherits": [], "state": "active",
                "owner": "tester", "created": "2026-06-01T00:00:00Z",
            },
            "scope": {"owner_tenant": "default", "tags": ["news"]},
            "acquisition": "poll",
            "config": {"url": "https://news.example/feed.xml"},
            "cadence": {"schedule": {"factory_kind": "cron", "raw": "*/15 * * * *"}},
            "subscription_policy": "open",
        }))

        async with store.transaction() as conn:
            await _insert_target_row(conn, target)
            await _insert_source_row(conn, source)
            wired = await auto_wire_discovered_source(conn, source_id=source_id)

        assert target_id in wired, f"expected {target_id} wired, got {wired}"

        # Idempotent: re-running records no new wire FOR THIS TEST'S TARGET.
        # The old `wired2 == []` was a statement about every selector-bearing
        # target in the persistent pivot DB: the sweep also wires the
        # long-lived country_g20_* heads, and any sibling (or concurrent
        # session — this DB outlives the pytest session) that re-registers
        # those targets rewrites their body and drops the trailer, so the
        # re-run "re-wires" 19 foreign targets and fails this test about
        # state it never owned (recurred 2026-08-10 in a seed-174413029
        # replay alongside another session's run). The idempotency contract
        # this test can honestly assert is its OWN wire.
        async with store.transaction() as conn:
            wired2 = await auto_wire_discovered_source(conn, source_id=source_id)
        assert target_id not in wired2, (
            f"re-run re-wired this test's own target: {wired2}"
        )

        # The provenance trailer landed on the target body.
        async with store.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT body FROM target_descriptors WHERE descriptor_id=$1 AND is_head",
                target_id,
            )
        body = row["body"]
        if isinstance(body, str):
            body = json.loads(body)
        assert source_id in (body.get("_auto_wired_sources") or {})

    async def test_locked_source_does_not_auto_wire(self, store: PostgresStore):
        """subscription_policy gating: an allowlist source never auto-wires."""
        suffix = uuid4().hex[:8]
        target_id = f"tgt_locked_p13_{suffix}"
        source_id = f"src_locked_p13_{suffix}"

        target = TargetDescriptor.model_validate_json(json.dumps({
            "identity": {
                "id": target_id, "name": "locked target",
                "schema_uri": "legba/target/2.0.0", "version": "0000000000000000",
                "abstraction_level": "L1", "inherits": [], "state": "active",
                "owner": "tester", "created": "2026-06-01T00:00:00Z",
            },
            "scope": {
                "domain": "geo", "geo": ["BR"], "languages": ["pt-BR"],
                "entity_classes": ["country"], "tags": ["news"],
                "time_horizon_days": 90,
            },
            "sources": [
                {"source_selector": {"kinds": ["rss"], "tags": ["news"]},
                 "subscription": {"tags": ["news"]}},
            ],
        }))
        source = SourceDescriptor.model_validate_json(json.dumps({
            "identity": {
                "id": source_id, "name": "locked news rss", "kind": "rss",
                "schema_uri": "legba/source/1.0.0", "version": "0000000000000000",
                "abstraction_level": "L1", "inherits": [], "state": "active",
                "owner": "tester", "created": "2026-06-01T00:00:00Z",
            },
            "scope": {"owner_tenant": "default", "tags": ["news"]},
            "acquisition": "poll",
            "config": {"url": "https://locked.example/feed.xml"},
            "cadence": {"schedule": {"factory_kind": "cron", "raw": "*/15 * * * *"}},
            "subscription_policy": "allowlist",
            "allowed_targets": ["someone_else"],
        }))

        async with store.transaction() as conn:
            await _insert_target_row(conn, target)
            await _insert_source_row(conn, source)
            wired = await auto_wire_discovered_source(conn, source_id=source_id)

        assert wired == [], "allowlist source must not auto-wire by selector"


# ===========================================================================
# 5. Integrated source-discovery + auto-wire (full source-flavor seam)
# ===========================================================================


class TestSourceDiscoveryWithAutoWire:
    async def test_discovered_source_auto_wires_target_in_one_cycle(
        self, store: PostgresStore
    ):
        """The complete source flavor: discover -> validate -> register ->
        the freshly-registered source auto-wires a pre-existing selector
        target, all in one reconcile cycle."""
        suffix = uuid4().hex[:8]
        template_id = f"src_tmpl_aw_{suffix}"
        discovery_id = f"src_disc_aw_{suffix}"
        target_id = f"tgt_aw_{suffix}"

        # A target with a selector for open rss/news sources.
        target = TargetDescriptor.model_validate_json(json.dumps({
            "identity": {
                "id": target_id, "name": "selector target",
                "schema_uri": "legba/target/2.0.0", "version": "0000000000000000",
                "abstraction_level": "L1", "inherits": [], "state": "active",
                "owner": "tester", "created": "2026-06-01T00:00:00Z",
            },
            "scope": {
                "domain": "geo", "geo": ["BR"], "languages": ["pt-BR"],
                "entity_classes": ["country"], "tags": ["news"],
                "time_horizon_days": 90,
            },
            "sources": [
                {"source_selector": {"kinds": ["rss"], "tags": ["news"]},
                 "subscription": {"tags": ["news"]}},
            ],
        }))

        feeds = [{"url": "https://news.example/feed.xml", "kind": "rss", "feed_title": "News"}]
        sd_template = _source_template(template_id)
        discovery = _source_discovery_descriptor(discovery_id, template_id, feeds)
        deps = StandardDeps(pg_pool=store.pool)

        async with store.transaction() as conn:
            await _insert_target_row(conn, target)
            await _insert_source_row(conn, sd_template)
            result = await run_source_discovery_cycle(
                conn, discovery, deps,
                probe_handler=_FakeLiveRSS(),
                auto_wire=True,
            )

        assert result.registered_count == 1
        outcome = result.materialized[0]
        assert outcome.source_id is not None
        # The materialised source carries scope.tags ⊇ {news} (template + relabel)
        # so the target's selector matches and auto-wires in the same cycle.
        assert target_id in outcome.auto_wired_targets, (
            f"expected {target_id} auto-wired; got {outcome.auto_wired_targets}"
        )


# ===========================================================================
# 6. validate-before-register with the REAL source handler (no probe inject)
# ===========================================================================


class TestRealHandlerValidation:
    async def test_real_rss_handler_rejects_unresolvable_host(self):
        """validate-before-register builds the REAL rss handler (via
        build_source_handler) and trial-pulls it against an unresolvable host —
        the candidate is rejected without any probe injection."""
        cand = CandidateSource(
            natural_key="https://nonexistent.invalid/feed.xml",
            source_kind="rss",
            probe_config={"url": "https://nonexistent.invalid/feed.xml"},
        )
        verdict = await validate_candidate_source(cand)  # no handler= -> real
        assert verdict.valid is False
        # Either liveness or trial pull catches the dead host.
        assert (
            "liveness" in verdict.reason
            or "trial_pull_raised" in verdict.reason
            or "handler_build_failed" in verdict.reason
        ), verdict.reason


# ===========================================================================
# 7. discovery_state recording (migration 0026)
# ===========================================================================


class TestDiscoveryStateRecording:
    async def test_source_register_records_discovery_state(self, store: PostgresStore):
        """A registered source records its (discovery_id, natural_key) ->
        source_id mapping in discovery_state (migration 0026), so a later cycle
        can classify retained/disappeared without re-deriving from the body."""
        suffix = uuid4().hex[:8]
        template_id = f"src_tmpl_ds_{suffix}"
        discovery_id = f"src_disc_ds_{suffix}"
        feed_url = "https://news.example/feed.xml"

        feeds = [{"url": feed_url, "kind": "rss", "feed_title": "News"}]
        sd_template = _source_template(template_id)
        discovery = _source_discovery_descriptor(discovery_id, template_id, feeds)
        deps = StandardDeps(pg_pool=store.pool)

        async with store.transaction() as conn:
            await _insert_source_row(conn, sd_template)
            result = await run_source_discovery_cycle(
                conn, discovery, deps, probe_handler=_FakeLiveRSS(), auto_wire=False
            )
        assert result.registered_count == 1
        src_id = result.materialized[0].source_id

        async with store.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT natural_key, family, descriptor_id, state, cycle_count "
                "FROM discovery_state WHERE discovery_id = $1",
                discovery_id,
            )
        assert row is not None
        assert row["natural_key"] == feed_url
        assert row["family"] == "source"
        assert row["descriptor_id"] == src_id
        assert row["state"] == "active"
