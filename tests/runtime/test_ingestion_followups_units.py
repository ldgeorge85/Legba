# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit checks (no database): the pull.done ``sinks=`` field, the
PortWatch descriptor's subject kind + attribution, and the hash neutrality of
the new ``scope.attribution`` field. DB-backed proofs (persisted rows, batch
timing) are ``tests/data_pkg/test_ingestion_followups_n_r.py``."""

from __future__ import annotations

import yaml

from legba.data.schemas.source import SourceDescriptor
from legba.data.schemas.versioning import content_hash
from legba.data.sources.portwatch import PortWatchConfig, parse_page
from legba.runtime.source_factory import _unwrap_factory_dict
from legba.runtime.source_poll_records import sink_summary

from tests.runtime._ingest_fakes import REPO

PORTWATCH = REPO / "descriptors" / "source_imf_portwatch_chokepoints.yaml"


def test_sink_summary_for_the_pull_done_line() -> None:
    assert sink_summary({}) == "-"
    assert sink_summary({"observation": {"written": 3, "unchanged": 5, "revised": 1, "rejected": 0,
                                         "rejected_reasons": []}}) == \
        "observation:written=3,revised=1,unchanged=5,rejected=0"


def test_portwatch_declares_entity_subjects_and_the_attribution_line() -> None:
    desc = SourceDescriptor.model_validate(yaml.safe_load(PORTWATCH.read_text()), strict=False)
    assert desc.scope.license_class == "api_terms"
    assert desc.scope.attribution == "Source: IMF PortWatch (portwatch.imf.org)"
    cfg = PortWatchConfig(**_unwrap_factory_dict(desc.config))
    series = desc.sink.observation_config().series
    assert cfg.subject_kind == "entity" and {s.subject_kind for s in series} == {"entity"}
    body = (b'{"features": [{"attributes": {"date": "2026-11-08", "portid": "chokepoint6", '
            b'"portname": "Strait of Hormuz", "ObjectId": 7, "n_total": 101}}]}')
    records, more, _sha = parse_page(body, cfg=cfg, page_url="https://x.invalid/q")
    assert not more and records and {r.subject_kind for r in records} == {"entity"}
    # The handler default stays the previous value (see the config comment).
    assert PortWatchConfig().subject_kind == "global"


def test_scope_attribution_is_hash_neutral_when_unset() -> None:
    body = yaml.safe_load(PORTWATCH.read_text())
    body["scope"].pop("attribution")
    without = SourceDescriptor.model_validate(body, strict=False)
    assert "attribution" not in without.model_dump(mode="json")["scope"]
    explicit_none = SourceDescriptor.model_validate(
        {**body, "scope": {**body["scope"], "attribution": None}}, strict=False)
    assert content_hash(explicit_none) == content_hash(without)
    with_line = SourceDescriptor.model_validate(
        {**body, "scope": {**body["scope"], "attribution": "x"}}, strict=False)
    assert content_hash(with_line) != content_hash(without)
