# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for S1-T1 desk retro-tagging (scripts/bringup_tag_targets.py).

Covers the PURE logic — the merge, the 25-desk mapping table, the per-desk plan,
and the read→merge→PUT loop with a FAKE in-memory registry (no httpx, no DB).
The load-bearing acceptance is idempotency + no-coverage-loss: a re-run PUTs
nothing, and the existing g20/watch coverage tags are always preserved.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import bringup_tag_targets as tt  # noqa: E402

from legba.data.schemas.target import TargetDescriptor  # noqa: E402


# --------------------------------------------------------------------------- #
# merge_tags — order-preserving dedupe, never drops.
# --------------------------------------------------------------------------- #
def test_merge_appends_new_preserving_existing_order() -> None:
    assert tt.merge_tags(["geopolitical", "news", "g20"], ["region_americas"]) == \
        ["geopolitical", "news", "g20", "region_americas"]


def test_merge_is_idempotent_when_all_present() -> None:
    existing = ["geopolitical", "news", "g20", "region_americas", "nuclear_watch"]
    assert tt.merge_tags(existing, ["region_americas", "nuclear_watch"]) == existing


def test_merge_dedupes_within_add_and_against_existing() -> None:
    assert tt.merge_tags(["a"], ["b", "b", "a", "c"]) == ["a", "b", "c"]


def test_merge_never_drops_an_existing_tag() -> None:
    existing = ["watch", "geopolitical", "news"]
    out = tt.merge_tags(existing, ["region_mena", "conflict_active"])
    for t in existing:
        assert t in out


# --------------------------------------------------------------------------- #
# The mapping table — 24 desks, one region each, watch tags from the vocab.
# --------------------------------------------------------------------------- #
def test_exactly_24_desks() -> None:
    assert len(tt.desks()) == 25
    assert len({d for d, _ in tt.desks()}) == 25  # ids unique


def test_desk_ids_follow_the_registrar_convention() -> None:
    ids = dict((iso, did) for did, iso in tt.desks())
    assert ids["US"] == "country_g20_us"
    assert ids["IR"] == "country_watch_ir"


def test_every_desk_has_exactly_one_region_tag() -> None:
    for _, iso2 in tt.desks():
        tags = tt.tags_for_iso2(iso2)
        regions = [t for t in tags if t in tt.REGION_TAGS]
        assert len(regions) == 1, f"{iso2}: {regions}"


def test_region_tags_partition_all_25_desks() -> None:
    counts: dict[str, int] = {}
    for _, iso2 in tt.desks():
        region = tt.REGION_BY_ISO2[iso2]
        counts[region] = counts.get(region, 0) + 1
    assert sum(counts.values()) == 25
    # Every region tag used is in the declared vocabulary.
    assert set(counts) <= set(tt.REGION_TAGS)


def test_watch_tags_are_a_subset_of_the_declared_vocab() -> None:
    for iso2, watch in tt.WATCH_TAGS_BY_ISO2.items():
        assert set(watch) <= set(tt.WATCH_TAGS), iso2
        assert iso2 in tt.REGION_BY_ISO2  # only real desks carry watch tags


def test_known_watch_assignments() -> None:
    # Regression-pins the defensible defaults so a later edit is deliberate.
    assert tt.tags_for_iso2("IR") == \
        ["region_mena", "nuclear_watch", "conflict_active", "sanctions_regime"]
    assert tt.tags_for_iso2("KP") == \
        ["region_indo_pacific", "nuclear_watch", "sanctions_regime"]
    assert tt.tags_for_iso2("AR") == ["region_americas"]  # no watch tags


def test_every_assigned_tag_is_a_legal_scope_tag() -> None:
    # The schema pattern the registry enforces server-side.
    from legba.data.schemas.target import GeoScope
    for _, iso2 in tt.desks():
        scope = GeoScope(geo=[iso2], tags=tt.tags_for_iso2(iso2))
        assert set(tt.tags_for_iso2(iso2)) <= set(scope.tags)


# --------------------------------------------------------------------------- #
# build_plan — touches only scope.tags, preserves everything else.
# --------------------------------------------------------------------------- #
def _fake_body(iso2: str, tags: list[str], desc_id: str) -> dict:
    return {
        "identity": {
            "id": desc_id,
            "name": f"desk {iso2}",
            "schema_uri": "legba/target/2.0.0",
            "version": "0" * 16,
            "state": "active",
            "owner": "test",
            "created": "2026-07-01T00:00:00Z",
        },
        "scope": {
            "domain": "geo",
            "geo": [iso2],
            "languages": ["en"],
            "time_horizon_days": 90,
            "tags": list(tags),
        },
        # An active target needs >=1 source (schema _state_constraints); a bare
        # explicit source_id is the minimal valid SourceRef.
        "sources": [{"source_id": "source_news_shared"}],
    }


def test_build_plan_merges_and_preserves_other_scope_fields() -> None:
    body = _fake_body("US", ["geopolitical", "news", "g20"], "country_g20_us")
    plan = tt.build_plan("country_g20_us", "US", body)
    assert plan.changed is True
    assert plan.merged_tags == [
        "geopolitical", "news", "g20", "region_americas", "nuclear_watch",
        "conflict_active",
    ]
    assert plan.newly_added == [
        "region_americas", "nuclear_watch", "conflict_active",
    ]
    # Coverage tag preserved.
    assert "g20" in plan.merged_tags
    # Non-tag scope fields carried through verbatim.
    assert plan.new_body["scope"]["geo"] == ["US"]
    assert plan.new_body["scope"]["languages"] == ["en"]
    assert plan.new_body["scope"]["time_horizon_days"] == 90
    # Input body untouched (deep copy).
    assert body["scope"]["tags"] == ["geopolitical", "news", "g20"]


def test_build_plan_idempotent_when_tags_present() -> None:
    body = _fake_body(
        "US",
        ["geopolitical", "news", "g20", "region_americas", "nuclear_watch",
         "conflict_active"],
        "country_g20_us",
    )
    plan = tt.build_plan("country_g20_us", "US", body)
    assert plan.changed is False
    assert plan.newly_added == []


def test_build_plan_handles_missing_scope_tags_key() -> None:
    body = _fake_body("ZA", [], "country_g20_za")
    del body["scope"]["tags"]
    plan = tt.build_plan("country_g20_za", "ZA", body)
    assert plan.changed is True
    assert plan.merged_tags == ["region_africa"]


# --------------------------------------------------------------------------- #
# run() with a fake registry — the read→merge→PUT loop + idempotency.
# --------------------------------------------------------------------------- #
class _FakeRegistry:
    """In-memory stand-in for the registry client: stores desk bodies and
    records PUTs, so the loop's IO contract is exercised without httpx/DB."""

    def __init__(self, bodies: dict[str, dict]):
        self.bodies = bodies
        self.puts: list[str] = []

    def get_body(self, descriptor_id: str) -> dict | None:
        return self.bodies.get(descriptor_id)

    def put_body(self, descriptor_id: str, body: dict) -> str:
        # Mimic the registry: store the new body, mint a fake version.
        self.bodies[descriptor_id] = body
        self.puts.append(descriptor_id)
        return f"v{len(self.puts)}"


def _seed_untagged() -> dict[str, dict]:
    """All 25 desks with only their original coverage tags."""
    bodies: dict[str, dict] = {}
    for desc_id, iso2 in tt.desks():
        cov = "g20" if desc_id.startswith("country_g20_") else "watch"
        bodies[desc_id] = _fake_body(iso2, ["geopolitical", "news", cov], desc_id)
    return bodies


def test_run_tags_all_25_then_reruns_as_noop() -> None:
    fake = _FakeRegistry(_seed_untagged())

    first = tt.run(get_body=fake.get_body, put_body=fake.put_body)
    assert len(first) == 25
    assert all(r.action == "updated" for r in first)
    assert len(fake.puts) == 25  # every desk written once

    # Idempotent re-run: bodies now carry the tags -> zero PUTs.
    fake.puts.clear()
    second = tt.run(get_body=fake.get_body, put_body=fake.put_body)
    assert all(r.action == "unchanged" for r in second)
    assert fake.puts == []


def test_run_preserves_coverage_tag_on_every_desk() -> None:
    """No desk loses coverage: g20/watch survive the merge (the accept bar)."""
    fake = _FakeRegistry(_seed_untagged())
    tt.run(get_body=fake.get_body, put_body=fake.put_body)
    for desc_id, _ in tt.desks():
        tags = fake.bodies[desc_id]["scope"]["tags"]
        cov = "g20" if desc_id.startswith("country_g20_") else "watch"
        assert cov in tags, desc_id
        assert "geopolitical" in tags and "news" in tags


def test_dry_run_writes_nothing() -> None:
    fake = _FakeRegistry(_seed_untagged())
    results = tt.run(get_body=fake.get_body, put_body=fake.put_body, dry_run=True)
    assert all(r.action == "would_update" for r in results)
    assert fake.puts == []  # nothing written


def test_run_reports_missing_desk() -> None:
    bodies = _seed_untagged()
    del bodies["country_watch_kp"]
    fake = _FakeRegistry(bodies)
    results = tt.run(get_body=fake.get_body, put_body=fake.put_body)
    missing = [r for r in results if r.action == "missing"]
    assert [r.descriptor_id for r in missing] == ["country_watch_kp"]


def test_run_output_merges_validate_against_target_schema() -> None:
    """The merged bodies the loop PUTs must still parse as TargetDescriptors."""
    fake = _FakeRegistry(_seed_untagged())
    tt.run(get_body=fake.get_body, put_body=fake.put_body)
    for desc_id, _ in tt.desks():
        TargetDescriptor.model_validate(fake.bodies[desc_id], strict=False)
