# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-182 — file_sd_discovery kind tests.

Covers the per-file emission shape, cross-file dedupe, mtime tracking,
parse-error / shape-error DLQ routing, and relabel-chain integration
(materialized target descriptor body for a 2-file fixture).

No substrate dependencies — the kind talks to the filesystem + the
in-memory state store. Each test gets a clean tempdir and a fresh
:class:`InMemoryStateStore`.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from legba.data.discovery import (
    CandidateTarget,
    DiscoveryContext,
    DiscoveryHealth,
    InMemoryStateStore,
    RelabelRule,
    discover_discovery_kinds,
    evaluate_relabel_chain,
)
from legba.data.discovery.file_sd_discovery import (
    CONFIG_SCHEMA,
    DEFAULT_REFRESH_INTERVAL,
    FileSDConfig,
    FileSDDLQRecord,
    FileSDDiscovery,
    KIND_NAME,
    SCHEMA_VERSION,
    _canonical_labels_hash,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, entries: list[dict[str, Any]]) -> None:
    path.write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")


def _write_json(path: Path, entries: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def _bump_mtime(path: Path, *, offset_seconds: float) -> None:
    """Force a different mtime on the file by setting it via os.utime."""
    stat = path.stat()
    new_t = stat.st_mtime + offset_seconds
    os.utime(path, (stat.st_atime, new_t))


def _make_ctx(
    *,
    tmpdir: Path,
    config: FileSDConfig,
    state_store: InMemoryStateStore | None = None,
) -> DiscoveryContext:
    return DiscoveryContext(
        discovery_id="d.file_sd.test",
        discovery_version="v1",
        config=config,
        state_store=state_store or InMemoryStateStore(),
    )


async def _drain(handler: FileSDDiscovery, ctx: DiscoveryContext) -> list[CandidateTarget]:
    out: list[CandidateTarget] = []
    async for c in handler.discover(ctx):
        out.append(c)
    return out


# ---------------------------------------------------------------------------
# Module identity surface
# ---------------------------------------------------------------------------


class TestModuleSurface:
    def test_kind_name_is_file_sd_discovery(self):
        assert KIND_NAME == "file_sd_discovery"

    def test_schema_version_pinned(self):
        assert SCHEMA_VERSION == "legba/discovery/file_sd/1.0.0"

    def test_default_refresh_interval_is_5min(self):
        assert DEFAULT_REFRESH_INTERVAL == "*/5 * * * *"

    def test_config_schema_aliased(self):
        assert CONFIG_SCHEMA is FileSDConfig

    def test_handler_classvars(self):
        h = FileSDDiscovery()
        assert h.kind == "file_sd_discovery"
        assert h.family == "discovery"
        assert h.schema_version == SCHEMA_VERSION
        assert h.config_schema is FileSDConfig

    def test_registry_walker_picks_up_kind(self):
        registry = discover_discovery_kinds()
        assert KIND_NAME in registry
        bundle = registry[KIND_NAME]
        assert bundle.kind_name == KIND_NAME
        assert bundle.schema_version == SCHEMA_VERSION
        assert bundle.is_static is False
        assert callable(bundle.discover)
        assert callable(bundle.healthcheck)
        assert bundle.config_schema is FileSDConfig


# ---------------------------------------------------------------------------
# CONFIG_SCHEMA
# ---------------------------------------------------------------------------


class TestConfigSchema:
    def test_minimal_config_validates(self):
        cfg = FileSDConfig(watch_paths=["/tmp/legba/*.yaml"])
        assert cfg.watch_paths == ["/tmp/legba/*.yaml"]
        assert cfg.format == "yaml"
        assert cfg.refresh_interval.raw == DEFAULT_REFRESH_INTERVAL

    def test_format_json_accepted(self):
        cfg = FileSDConfig(watch_paths=["x.json"], format="json")
        assert cfg.format == "json"

    def test_empty_watch_paths_rejected(self):
        with pytest.raises(ValueError):
            FileSDConfig(watch_paths=[])

    def test_empty_string_path_rejected(self):
        with pytest.raises(ValueError):
            FileSDConfig(watch_paths=["  "])

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValueError):
            FileSDConfig(watch_paths=["x"], unexpected="boom")  # type: ignore[call-arg]

    def test_format_must_be_yaml_or_json(self):
        with pytest.raises(ValueError):
            FileSDConfig(watch_paths=["x"], format="toml")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Canonical labels hash
# ---------------------------------------------------------------------------


class TestCanonicalLabelsHash:
    def test_equal_dicts_hash_equal_regardless_of_key_order(self):
        a = {"country_iso2": "BR", "topic": "energy"}
        b = {"topic": "energy", "country_iso2": "BR"}
        assert _canonical_labels_hash(a) == _canonical_labels_hash(b)

    def test_distinct_dicts_hash_distinctly(self):
        assert _canonical_labels_hash({"x": 1}) != _canonical_labels_hash({"x": 2})

    def test_nested_lists_normalized(self):
        # Lists are NOT order-normalized (a list is ordered); ensure that's true.
        a = {"langs": ["pt-BR", "en-BR"]}
        b = {"langs": ["en-BR", "pt-BR"]}
        assert _canonical_labels_hash(a) != _canonical_labels_hash(b)


# ---------------------------------------------------------------------------
# Single-file emission
# ---------------------------------------------------------------------------


class TestSingleFileEmission:
    @pytest.mark.asyncio
    async def test_one_file_yields_one_candidate_per_top_level_entry(self, tmp_path: Path):
        f = tmp_path / "south_america.yaml"
        _write_yaml(
            f,
            [
                {
                    "labels": {"country_iso2": "BR", "topic": "energy"},
                    "source_metadata": {"region": "south-america"},
                },
                {
                    "labels": {"country_iso2": "AR", "topic": "energy"},
                },
                {
                    "labels": {"country_iso2": "CL", "topic": "energy"},
                    "source_metadata": {"region": "south-america"},
                },
            ],
        )
        cfg = FileSDConfig(watch_paths=[str(f)])
        handler = FileSDDiscovery()
        ctx = _make_ctx(tmpdir=tmp_path, config=cfg)

        results = await _drain(handler, ctx)
        assert len(results) == 3

        nk_base = str(f.resolve())
        nks = [c.natural_key for c in results]
        assert nks == [f"{nk_base}#0", f"{nk_base}#1", f"{nk_base}#2"]
        # Labels round-tripped.
        assert dict(results[0].label_set) == {"country_iso2": "BR", "topic": "energy"}
        assert dict(results[1].label_set) == {"country_iso2": "AR", "topic": "energy"}

        # source_metadata enriched with file_path / file_mtime / file_format / block_index.
        meta0 = dict(results[0].source_metadata)
        assert meta0["file_path"] == nk_base
        assert meta0["file_format"] == "yaml"
        assert meta0["block_index"] == 0
        assert meta0["region"] == "south-america"
        assert isinstance(meta0["file_mtime_ns"], int)

        # Evidence carries provenance.
        assert results[0].evidence.source_id == nk_base
        assert results[0].evidence.row_index == 0
        assert handler.dlq_records == []

    @pytest.mark.asyncio
    async def test_json_format_parses(self, tmp_path: Path):
        f = tmp_path / "targets.json"
        _write_json(
            f,
            [
                {"labels": {"country_iso2": "US", "topic": "infra"}},
                {"labels": {"country_iso2": "CA", "topic": "infra"}},
            ],
        )
        cfg = FileSDConfig(watch_paths=[str(f)], format="json")
        handler = FileSDDiscovery()
        ctx = _make_ctx(tmpdir=tmp_path, config=cfg)
        results = await _drain(handler, ctx)
        assert [c.natural_key for c in results] == [
            f"{f.resolve()}#0",
            f"{f.resolve()}#1",
        ]
        assert dict(results[0].source_metadata)["file_format"] == "json"

    @pytest.mark.asyncio
    async def test_empty_file_yields_nothing_and_no_dlq(self, tmp_path: Path):
        f = tmp_path / "empty.yaml"
        f.write_text("", encoding="utf-8")
        cfg = FileSDConfig(watch_paths=[str(f)])
        handler = FileSDDiscovery()
        ctx = _make_ctx(tmpdir=tmp_path, config=cfg)
        results = await _drain(handler, ctx)
        assert results == []
        assert handler.dlq_records == []

    @pytest.mark.asyncio
    async def test_glob_pattern_resolves_multiple_files(self, tmp_path: Path):
        (tmp_path / "a.yaml").write_text(
            yaml.safe_dump([{"labels": {"id": "a1"}}]),
            encoding="utf-8",
        )
        (tmp_path / "b.yaml").write_text(
            yaml.safe_dump([{"labels": {"id": "b1"}}]),
            encoding="utf-8",
        )
        cfg = FileSDConfig(watch_paths=[str(tmp_path / "*.yaml")])
        handler = FileSDDiscovery()
        ctx = _make_ctx(tmpdir=tmp_path, config=cfg)
        results = await _drain(handler, ctx)
        labels = [dict(c.label_set) for c in results]
        assert {"id": "a1"} in labels and {"id": "b1"} in labels
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_glob_with_no_matches_yields_nothing(self, tmp_path: Path):
        cfg = FileSDConfig(watch_paths=[str(tmp_path / "no_such_*.yaml")])
        handler = FileSDDiscovery()
        ctx = _make_ctx(tmpdir=tmp_path, config=cfg)
        results = await _drain(handler, ctx)
        assert results == []
        # No DLQ — operator just hasn't dropped any files yet.
        assert handler.dlq_records == []


# ---------------------------------------------------------------------------
# Cross-file dedupe (canonical labels SHA-256)
# ---------------------------------------------------------------------------


class TestCrossFileDedupe:
    @pytest.mark.asyncio
    async def test_two_files_same_labels_emits_once(self, tmp_path: Path):
        a = tmp_path / "a.yaml"
        b = tmp_path / "b.yaml"
        _write_yaml(a, [{"labels": {"country_iso2": "BR", "topic": "energy"}}])
        _write_yaml(b, [{"labels": {"topic": "energy", "country_iso2": "BR"}}])
        cfg = FileSDConfig(watch_paths=[str(tmp_path / "*.yaml")])
        handler = FileSDDiscovery()
        ctx = _make_ctx(tmpdir=tmp_path, config=cfg)

        results = await _drain(handler, ctx)
        assert len(results) == 1

        # The DLQ record should call out the duplicate, pointing at the
        # first-occurrence natural_key.
        dups = [r for r in handler.dlq_records if r.reason == "duplicate_labels"]
        assert len(dups) == 1
        assert dups[0].first_occurrence_natural_key == results[0].natural_key

    @pytest.mark.asyncio
    async def test_distinct_labels_in_two_files_both_emit(self, tmp_path: Path):
        a = tmp_path / "a.yaml"
        b = tmp_path / "b.yaml"
        _write_yaml(a, [{"labels": {"id": "x", "n": 1}}])
        _write_yaml(b, [{"labels": {"id": "x", "n": 2}}])  # different value
        cfg = FileSDConfig(watch_paths=[str(tmp_path / "*.yaml")])
        handler = FileSDDiscovery()
        ctx = _make_ctx(tmpdir=tmp_path, config=cfg)
        results = await _drain(handler, ctx)
        assert len(results) == 2
        assert not any(r.reason == "duplicate_labels" for r in handler.dlq_records)

    @pytest.mark.asyncio
    async def test_dedupe_within_single_file(self, tmp_path: Path):
        f = tmp_path / "dup.yaml"
        _write_yaml(
            f,
            [
                {"labels": {"k": "v"}},
                {"labels": {"k": "v"}},  # exact duplicate
                {"labels": {"k": "v2"}},
            ],
        )
        cfg = FileSDConfig(watch_paths=[str(f)])
        handler = FileSDDiscovery()
        ctx = _make_ctx(tmpdir=tmp_path, config=cfg)
        results = await _drain(handler, ctx)
        # Two distinct emissions (indices 0 + 2); index 1 is a dedupe.
        assert len(results) == 2
        nks = [c.natural_key for c in results]
        assert nks[0].endswith("#0")
        assert nks[1].endswith("#2")
        dups = [r for r in handler.dlq_records if r.reason == "duplicate_labels"]
        assert len(dups) == 1
        assert dups[0].block_index == 1


# ---------------------------------------------------------------------------
# Per-file failure → DLQ + other files continue
# ---------------------------------------------------------------------------


class TestPerFileFailures:
    @pytest.mark.asyncio
    async def test_parse_error_in_one_file_other_files_continue(self, tmp_path: Path):
        good = tmp_path / "good.yaml"
        bad = tmp_path / "bad.yaml"
        _write_yaml(good, [{"labels": {"id": "good"}}])
        bad.write_text("not: valid: yaml: :::", encoding="utf-8")
        cfg = FileSDConfig(watch_paths=[str(tmp_path / "*.yaml")])
        handler = FileSDDiscovery()
        ctx = _make_ctx(tmpdir=tmp_path, config=cfg)
        results = await _drain(handler, ctx)
        # Good file's candidate emitted; bad file dropped to DLQ.
        assert len(results) == 1
        assert dict(results[0].label_set) == {"id": "good"}
        dlqs = [r for r in handler.dlq_records if r.reason == "parse_error"]
        assert len(dlqs) == 1
        assert dlqs[0].file_path == str(bad.resolve())

    @pytest.mark.asyncio
    async def test_top_level_dict_is_shape_error(self, tmp_path: Path):
        f = tmp_path / "wrong.yaml"
        f.write_text(yaml.safe_dump({"labels": {"id": "x"}}), encoding="utf-8")
        cfg = FileSDConfig(watch_paths=[str(f)])
        handler = FileSDDiscovery()
        ctx = _make_ctx(tmpdir=tmp_path, config=cfg)
        results = await _drain(handler, ctx)
        assert results == []
        assert any(r.reason == "shape_error" for r in handler.dlq_records)

    @pytest.mark.asyncio
    async def test_entry_missing_labels_is_shape_error(self, tmp_path: Path):
        f = tmp_path / "missing.yaml"
        _write_yaml(f, [{"source_metadata": {"region": "x"}}])
        cfg = FileSDConfig(watch_paths=[str(f)])
        handler = FileSDDiscovery()
        ctx = _make_ctx(tmpdir=tmp_path, config=cfg)
        results = await _drain(handler, ctx)
        assert results == []
        dlqs = [r for r in handler.dlq_records if r.reason == "shape_error"]
        assert len(dlqs) == 1
        assert dlqs[0].block_index == 0

    @pytest.mark.asyncio
    async def test_entry_with_non_dict_labels_is_shape_error(self, tmp_path: Path):
        f = tmp_path / "bad.yaml"
        _write_yaml(f, [{"labels": "not_a_dict"}])  # type: ignore[list-item]
        cfg = FileSDConfig(watch_paths=[str(f)])
        handler = FileSDDiscovery()
        ctx = _make_ctx(tmpdir=tmp_path, config=cfg)
        results = await _drain(handler, ctx)
        assert results == []
        assert any(r.reason == "shape_error" for r in handler.dlq_records)

    @pytest.mark.asyncio
    async def test_entry_scalar_is_shape_error(self, tmp_path: Path):
        f = tmp_path / "scalars.yaml"
        _write_yaml(f, ["scalar_entry"])  # type: ignore[list-item]
        cfg = FileSDConfig(watch_paths=[str(f)])
        handler = FileSDDiscovery()
        ctx = _make_ctx(tmpdir=tmp_path, config=cfg)
        results = await _drain(handler, ctx)
        assert results == []
        assert any(r.reason == "shape_error" for r in handler.dlq_records)

    @pytest.mark.asyncio
    async def test_non_dict_source_metadata_is_shape_error(self, tmp_path: Path):
        f = tmp_path / "bad_meta.yaml"
        _write_yaml(
            f,
            [{"labels": {"k": "v"}, "source_metadata": "scalar"}],  # type: ignore[list-item]
        )
        cfg = FileSDConfig(watch_paths=[str(f)])
        handler = FileSDDiscovery()
        ctx = _make_ctx(tmpdir=tmp_path, config=cfg)
        results = await _drain(handler, ctx)
        assert results == []
        assert any(r.reason == "shape_error" for r in handler.dlq_records)


# ---------------------------------------------------------------------------
# Mtime tracking
# ---------------------------------------------------------------------------


class TestMtimeTracking:
    @pytest.mark.asyncio
    async def test_mtime_snapshot_persists_to_state_store(self, tmp_path: Path):
        f = tmp_path / "a.yaml"
        _write_yaml(f, [{"labels": {"id": "x"}}])
        cfg = FileSDConfig(watch_paths=[str(f)])
        handler = FileSDDiscovery()
        store = InMemoryStateStore()
        ctx = _make_ctx(tmpdir=tmp_path, config=cfg, state_store=store)

        await _drain(handler, ctx)

        snapshot = await store.get("file_sd_mtimes")
        assert isinstance(snapshot, dict)
        assert str(f.resolve()) in snapshot
        assert isinstance(snapshot[str(f.resolve())], int)

    @pytest.mark.asyncio
    async def test_unchanged_file_re_emits_same_natural_keys(self, tmp_path: Path):
        """The kind re-emits stable natural_keys for unchanged files so
        the L-180 disappearance guard sees them as retained, not vanished."""
        f = tmp_path / "a.yaml"
        _write_yaml(f, [{"labels": {"id": "x"}}, {"labels": {"id": "y"}}])
        cfg = FileSDConfig(watch_paths=[str(f)])
        handler = FileSDDiscovery()
        store = InMemoryStateStore()
        ctx = _make_ctx(tmpdir=tmp_path, config=cfg, state_store=store)

        first = await _drain(handler, ctx)
        first_nks = [c.natural_key for c in first]
        assert len(first_nks) == 2

        # Same file, no modification — refresh.
        second = await _drain(handler, ctx)
        second_nks = [c.natural_key for c in second]
        assert first_nks == second_nks

        # Evidence source_version (== mtime_ns) is identical across the cycle.
        assert first[0].evidence.source_version == second[0].evidence.source_version

    @pytest.mark.asyncio
    async def test_modified_file_bumps_evidence_version(self, tmp_path: Path):
        f = tmp_path / "a.yaml"
        _write_yaml(f, [{"labels": {"id": "x"}}])
        cfg = FileSDConfig(watch_paths=[str(f)])
        handler = FileSDDiscovery()
        store = InMemoryStateStore()
        ctx = _make_ctx(tmpdir=tmp_path, config=cfg, state_store=store)

        first = await _drain(handler, ctx)
        # Modify the file + force a fresh mtime.
        _write_yaml(f, [{"labels": {"id": "x"}}, {"labels": {"id": "y"}}])
        _bump_mtime(f, offset_seconds=10.0)

        second = await _drain(handler, ctx)
        assert len(second) == 2
        # New mtime → different source_version.
        assert first[0].evidence.source_version != second[0].evidence.source_version

    @pytest.mark.asyncio
    async def test_removed_file_drops_from_mtime_snapshot(self, tmp_path: Path):
        f1 = tmp_path / "a.yaml"
        f2 = tmp_path / "b.yaml"
        _write_yaml(f1, [{"labels": {"id": "x"}}])
        _write_yaml(f2, [{"labels": {"id": "y"}}])
        cfg = FileSDConfig(watch_paths=[str(tmp_path / "*.yaml")])
        handler = FileSDDiscovery()
        store = InMemoryStateStore()
        ctx = _make_ctx(tmpdir=tmp_path, config=cfg, state_store=store)

        first = await _drain(handler, ctx)
        assert len(first) == 2

        f2.unlink()
        second = await _drain(handler, ctx)
        assert len(second) == 1
        assert dict(second[0].label_set) == {"id": "x"}

        snapshot = await store.get("file_sd_mtimes")
        assert str(f1.resolve()) in snapshot
        assert str(f2.resolve()) not in snapshot


# ---------------------------------------------------------------------------
# Healthcheck
# ---------------------------------------------------------------------------


class TestHealthcheck:
    @pytest.mark.asyncio
    async def test_unhealthy_when_no_paths_resolve(self, tmp_path: Path):
        cfg = FileSDConfig(watch_paths=[str(tmp_path / "no_such_*.yaml")])
        handler = FileSDDiscovery()
        ctx = _make_ctx(tmpdir=tmp_path, config=cfg)
        await _drain(handler, ctx)
        health = await handler.healthcheck(ctx)
        assert isinstance(health, DiscoveryHealth)
        assert health.state == "unhealthy"

    @pytest.mark.asyncio
    async def test_healthy_when_clean_cycle(self, tmp_path: Path):
        f = tmp_path / "a.yaml"
        _write_yaml(f, [{"labels": {"id": "x"}}])
        cfg = FileSDConfig(watch_paths=[str(f)])
        handler = FileSDDiscovery()
        ctx = _make_ctx(tmpdir=tmp_path, config=cfg)
        await _drain(handler, ctx)
        health = await handler.healthcheck(ctx)
        assert health.state == "healthy"
        assert health.materialized_targets == 1

    @pytest.mark.asyncio
    async def test_degraded_when_dlq_records_present(self, tmp_path: Path):
        good = tmp_path / "good.yaml"
        bad = tmp_path / "bad.yaml"
        _write_yaml(good, [{"labels": {"id": "x"}}])
        bad.write_text("[[[[ not yaml", encoding="utf-8")
        cfg = FileSDConfig(watch_paths=[str(tmp_path / "*.yaml")])
        handler = FileSDDiscovery()
        ctx = _make_ctx(tmpdir=tmp_path, config=cfg)
        await _drain(handler, ctx)
        health = await handler.healthcheck(ctx)
        assert health.state == "degraded"


# ---------------------------------------------------------------------------
# Relabel-chain integration — 2-file fixture exercising set/format/keep/merge_list
# ---------------------------------------------------------------------------


class TestRelabelChainIntegration:
    @pytest.mark.asyncio
    async def test_two_file_fixture_with_full_relabel_chain(self, tmp_path: Path):
        """Materialize candidates through the four relabel actions the
        brief calls out:

          * ``set``        — labels.topic → scope.topic
          * ``format``     — id template: ``manual_{labels.country_iso2|lower}_{labels.topic}``
          * ``keep``       — filter on labels (``country_iso2 != 'XX'``)
          * ``merge_list`` — tag accumulator (extend ``tags`` with static tags)
        """
        f1 = tmp_path / "south_america.yaml"
        f2 = tmp_path / "europe.yaml"
        _write_yaml(
            f1,
            [
                {
                    "labels": {
                        "country_iso2": "BR",
                        "topic": "energy",
                        "tags": ["operator"],
                    },
                    "source_metadata": {"region": "south-america"},
                },
                {
                    "labels": {
                        "country_iso2": "AR",
                        "topic": "energy",
                        "tags": ["operator"],
                    },
                    "source_metadata": {"region": "south-america"},
                },
                {
                    # This entry will be dropped by the keep rule.
                    "labels": {
                        "country_iso2": "XX",
                        "topic": "energy",
                        "tags": ["operator"],
                    },
                },
            ],
        )
        _write_yaml(
            f2,
            [
                {
                    "labels": {
                        "country_iso2": "DE",
                        "topic": "infra",
                        "tags": ["operator"],
                    },
                    "source_metadata": {"region": "europe"},
                },
            ],
        )

        cfg = FileSDConfig(watch_paths=[str(tmp_path / "*.yaml")])
        handler = FileSDDiscovery()
        ctx = _make_ctx(tmpdir=tmp_path, config=cfg)
        candidates = await _drain(handler, ctx)
        assert len(candidates) == 4

        rules = [
            # 1. Hoist labels.topic → scope.topic (set).
            RelabelRule(
                source_labels=["topic"],
                target_label="scope_topic",
                action="set",
            ),
            # 2. Build the target id (format).
            RelabelRule(
                source_labels=[],
                target_label="target_id",
                action="format",
                replacement="manual_{{ country_iso2 | lower }}_{{ topic }}",
            ),
            # 3. Filter the synthetic 'XX' (keep).
            RelabelRule(
                source_labels=["country_iso2"],
                action="keep",
                predicate="country_iso2 != 'XX'",
            ),
            # 4. Merge static tags into the existing list (merge_list).
            RelabelRule(
                source_labels=[],
                target_label="tags",
                action="merge_list",
                extend_with=["file_sd", "manual"],
            ),
        ]

        kept = []
        dropped = []
        for cand in candidates:
            result = evaluate_relabel_chain(cand, rules)
            if result.dropped:
                dropped.append((cand, result))
            else:
                kept.append((cand, result))

        # XX is dropped; BR/AR/DE survive.
        assert len(kept) == 3
        assert len(dropped) == 1
        assert dropped[0][0].label_set["country_iso2"] == "XX"
        assert dropped[0][1].dropped_by_action == "keep"

        # The materialized body for one kept candidate (BR) — pin all four
        # action outputs.
        br = next(c for c, _ in kept if c.label_set["country_iso2"] == "BR")
        br_result = next(r for c, r in kept if c is br)
        body = dict(br_result.labels)

        assert body["scope_topic"] == "energy"
        assert body["target_id"] == "manual_br_energy"
        # merge_list: original ['operator'] + ['file_sd', 'manual']
        assert body["tags"] == ["operator", "file_sd", "manual"]
        # Original labels preserved.
        assert body["country_iso2"] == "BR"
        assert body["topic"] == "energy"

        # And DE goes through the same chain.
        de_cand, de_result = next(
            (c, r) for c, r in kept if c.label_set["country_iso2"] == "DE"
        )
        assert de_result.labels["target_id"] == "manual_de_infra"
        assert de_result.labels["tags"] == ["operator", "file_sd", "manual"]
