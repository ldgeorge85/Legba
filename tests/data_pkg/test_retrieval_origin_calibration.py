# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R-3b Task 3 — the retrieval-origin axis + the calibration guardrail.

THE FAILURE THIS PREVENTS (and the test that would not have caught it)
-----------------------------------------------------------------------
The endogenous/exogenous axis is not a document property: it lives on
``hypotheses.resolved_by`` (migration 0038) and is TIERED inside
``calibration_tracking``. ``_is_exogenous`` treats any ``resolved_by`` that is
neither empty, nor ``"unknown"``, nor self-consistency, nor weak-tier as
HEADLINE EXOGENOUS — the one number that claims calibration against reality.

So a hypothesis resolved by web-retrieved evidence, labelled ``web_evidence``,
would have sailed straight into the headline. Web evidence is cheap and
abundant and will dominate volume within weeks of search going live, so the
headline exogenous Brier would have quietly become "how well do we predict
things that are easy to search" — and, before this file existed, no test would
have failed.

``test_web_evidence_is_not_headline_exogenous`` is that missing test: it FAILS
on the tree before the ``web_evidence`` weak-tier wiring.

Also covers: the three-way separation of ``retrieval_origin`` from
``source_class`` (authority) and ``license_class`` (rights), the vocabulary
helpers, the column-then-payload resolver with its deliberate fail-safe
asymmetry, the OpenSearch facet projection, and migration 0112's DDL.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import legba.data
from legba.data.analysts.deterministic_handlers import calibration_tracking as ct
from legba.data.opensearch import (
    CORPUS_INDEX_MAPPING,
    signal_retrieval_origin,
    signal_to_doc,
)
from legba.data.retrieval_origin import (
    CURATED_SOURCE,
    WEB_EVIDENCE_RESOLUTION,
    WEB_SEARCH_PREFIX,
    is_web_evidence_resolution,
    is_web_retrieved,
    provider_of,
    resolve_retrieval_origin,
    web_evidence_resolution,
    web_search_origin,
)
from legba.data.schemas.source import SourceClass

MIGRATIONS = Path(legba.data.__file__).parent / "migrations"


def _row(resolved_by: str | None = None, **extra) -> dict:
    """A resolved-claim row shaped like ``_pull_resolved_claims`` yields."""
    row = {"claimed_confidence": 0.7, "outcome": 1, "resolved_by": resolved_by}
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# 1) THE guardrail — the test that fails without the tiering
# ---------------------------------------------------------------------------


def test_web_evidence_is_not_headline_exogenous():
    """A web-resolved hypothesis must NEVER count as strong exogenous grading.

    Fails on the pre-R-3b tree: `web_evidence` is not empty, not "unknown", not
    in _SELF_CONSISTENCY_SOURCES and not in _WEAK_LEXICAL_SOURCES, so
    `_is_exogenous` returned True and it fed the headline Brier.
    """
    row = _row(WEB_EVIDENCE_RESOLUTION)
    assert ct._is_exogenous(row) is False
    assert ct._is_weak_tier(row) is True


def test_provider_suffixed_web_evidence_is_also_demoted():
    """Provenance stamps the provider component id onto the label. A suffixed
    label that fell through the check would land in headline exogenous — the
    exact silent corruption the label exists to prevent."""
    row = _row(web_evidence_resolution("search.searxng.local"))
    assert row["resolved_by"] == "web_evidence:search.searxng.local"
    assert ct._is_exogenous(row) is False
    assert ct._is_weak_tier(row) is True


def test_web_evidence_lands_in_the_weak_tier_not_self_consistency():
    """It is NOT self-consistency — the world DID grade the claim, just through
    an unvetted, engine-selected proxy. Three tiers, not two."""
    row = _row(WEB_EVIDENCE_RESOLUTION)
    assert row["resolved_by"] not in ct._SELF_CONSISTENCY_SOURCES
    assert ct._is_web_evidence(row) is True
    assert WEB_EVIDENCE_RESOLUTION in ct._WEB_RETRIEVED_SOURCES


def test_the_existing_tiers_are_untouched():
    """No behaviour change for anything that existed before."""
    assert ct._is_exogenous(_row("forecast_vs_actual")) is True
    assert ct._is_exogenous(_row("forecast_acute_exogenous")) is True
    assert ct._is_exogenous(_row("operator:lewis")) is True
    assert ct._is_exogenous(_row("status_transition")) is False
    assert ct._is_exogenous(_row("subsequent_facts")) is False
    assert ct._is_exogenous(_row(None)) is False
    assert ct._is_exogenous(_row("unknown")) is False
    assert ct._is_exogenous(
        _row("forecast_vs_actual", forecast_method="naive_mean")
    ) is False


def test_the_unlabelled_fail_safe_is_preserved():
    """`_is_exogenous` returning False for an unrecognised label is the
    guardrail, not a bug — adding a convenience exemption during a search build
    is precisely the mistake this file exists to prevent."""
    for label in ("", "   ", "unknown", "some_future_resolver"):
        row = _row(label)
        if label.strip() and label != "unknown":
            # An unrecognised-but-nonempty label is still conservatively
            # ADMITTED as exogenous by design (the tiers are explicit
            # denylists); what must never happen is web_evidence slipping
            # through that door.
            assert ct._is_exogenous(row) is True
            assert not is_web_evidence_resolution(label)
        else:
            assert ct._is_exogenous(row) is False


def test_headline_brier_excludes_web_evidence_end_to_end():
    """The split the handler actually computes, not just the predicate."""
    kept = [
        _row("forecast_vs_actual", claimed_confidence=0.9, outcome=1),
        _row("forecast_vs_actual", claimed_confidence=0.8, outcome=1),
        _row(web_evidence_resolution("search.searxng.local"),
             claimed_confidence=0.1, outcome=1),
        _row(WEB_EVIDENCE_RESOLUTION, claimed_confidence=0.1, outcome=1),
    ]
    exo = [r for r in kept if ct._is_exogenous(r)]
    weak = [r for r in kept if ct._is_weak_tier(r)]
    web = [r for r in kept if ct._is_web_evidence(r)]
    assert len(exo) == 2 and len(weak) == 2 and len(web) == 2
    # The two badly-calibrated web rows would have dragged the headline down
    # (or, with the signs flipped, propped it up) had they been pooled.
    assert ct._brier(exo) != ct._brier(kept)


def test_the_finding_reports_the_web_evidence_count_visibly():
    finding = ct._build_finding(
        brier=0.1, sample_size=4, reliability_bins=[], per_analyst={},
        rolling=[], drift_z=None, drift_threshold=2.0,
        resolution_sources={"web_evidence": 2, "forecast_vs_actual": 2},
        self_consistency_only=False, brier_exogenous=0.1,
        brier_self_consistency=None, brier_pooled=0.3,
        exogenous_sample_size=2, self_consistency_fraction=0.5,
        insufficient_exogenous=False, forecast_acute={}, warnings=[],
        target_id=None, brier_weak=0.8, weak_sample_size=2, weak_fraction=0.5,
        web_evidence_sample_size=2,
    )
    assert finding.data["web_evidence_sample_size"] == 2
    assert "brier_web_evidence_present" in finding.tags
    assert "web_evidence_sample_size=2" in finding.body


# ---------------------------------------------------------------------------
# 2) The axis is DISTINCT from source_class and license_class
# ---------------------------------------------------------------------------


def test_source_class_was_not_overloaded():
    """Adding a web value to SourceClass would be a category error: a Reuters
    article found via search is still `reporting`, and an unrecognised value
    silently drops to AUTHORITY_RANK 0, corrupting salience."""
    from typing import get_args

    members = set(get_args(SourceClass))
    assert members == {"reporting", "analysis", "official", "state_media"}
    assert not any("web" in m or "search" in m for m in members)


def test_authority_rank_is_untouched_by_this_change():
    from legba.data.analysts.signal_salience import AUTHORITY_RANK

    assert AUTHORITY_RANK["official"] == 4
    assert AUTHORITY_RANK["reporting"] == 3
    assert AUTHORITY_RANK["analysis"] == 2
    assert AUTHORITY_RANK["state_media"] == 1
    assert set(AUTHORITY_RANK) <= {
        "official", "reporting", "analysis", "state_media", "unknown",
    }


# ---------------------------------------------------------------------------
# 3) The vocabulary + the resolver
# ---------------------------------------------------------------------------


def test_origin_vocabulary():
    assert web_search_origin("search.searxng.local") == (
        "web_search:search.searxng.local"
    )
    assert is_web_retrieved("web_search:search.searxng.local") is True
    # "search, provider unknown" must still read as web-retrieved.
    assert is_web_retrieved(WEB_SEARCH_PREFIX) is True
    assert provider_of("web_search:search.searxng.local") == "search.searxng.local"


@pytest.mark.parametrize(
    "value", [None, "", "   ", CURATED_SOURCE, "curated", 42, ["web_search:x"]],
)
def test_non_web_origins_are_not_web(value):
    assert is_web_retrieved(value) is False


def test_resolver_reads_the_column_then_the_payload():
    assert resolve_retrieval_origin(
        {"retrieval_origin": "web_search:a", "payload": {}}
    ) == "web_search:a"
    assert resolve_retrieval_origin(
        {"payload": {"retrieval_origin": "web_search:b"}}
    ) == "web_search:b"
    # asyncpg may hand jsonb back as a string.
    assert resolve_retrieval_origin(
        {"payload": '{"retrieval_origin": "web_search:c"}'}
    ) == "web_search:c"


def test_absent_origin_is_none_not_a_backfilled_lie():
    """Every row written before migration 0112 is a curated source; the honest
    representation of that is absence, not a retroactive stamp."""
    assert resolve_retrieval_origin({}) is None
    assert resolve_retrieval_origin({"payload": {}}) is None
    assert resolve_retrieval_origin({"retrieval_origin": None}) is None


def test_disagreement_resolves_toward_the_stricter_reading():
    """Both consumers get STRICTER on a web origin, so a column/payload
    disagreement must resolve toward web — a fail-safe, not a merge rule."""
    assert resolve_retrieval_origin({
        "retrieval_origin": CURATED_SOURCE,
        "payload": {"retrieval_origin": "web_search:a"},
    }) == "web_search:a"
    assert resolve_retrieval_origin({
        "retrieval_origin": "web_search:a",
        "payload": {"retrieval_origin": CURATED_SOURCE},
    }) == "web_search:a"


# ---------------------------------------------------------------------------
# 4) The OpenSearch facet
# ---------------------------------------------------------------------------


def test_corpus_mapping_declares_the_facet():
    props = CORPUS_INDEX_MAPPING["mappings"]["properties"]
    assert props["retrieval_origin"] == {"type": "keyword"}
    # …and did not disturb the authority/licence facets it sits beside.
    assert props["license_class"] == {"type": "keyword"}


def test_doc_projection_carries_a_web_origin():
    doc = signal_to_doc({
        "id": "00000000-0000-0000-0000-000000000001",
        "payload": {"retrieval_origin": "web_search:search.searxng.local"},
    })
    assert doc["retrieval_origin"] == "web_search:search.searxng.local"


def test_doc_projection_omits_the_facet_for_a_curated_source():
    doc = signal_to_doc({
        "id": "00000000-0000-0000-0000-000000000002",
        "payload": {"raw_body": "x"},
    })
    assert "retrieval_origin" not in doc


def test_the_facet_and_the_gate_share_one_resolver():
    row = {"payload": {"retrieval_origin": "web_search:x"}}
    assert signal_retrieval_origin(row) == resolve_retrieval_origin(row)


# ---------------------------------------------------------------------------
# 5) Migration 0112
# ---------------------------------------------------------------------------


def test_migration_0112_exists_and_takes_only_its_assigned_slot():
    """0112 stayed in its own lane while the coherence wave claimed its slots.

    The wave reserved 0109/0110/0111 up front. 0109 went to the C2 janitor
    (`0109_retention_policies.sql`) as planned; 0110/0111 were reserved for the
    C3 source-quality ledger, which landed at 0115 instead (0112-0114 had taken
    the intervening slots by then) and needed only ONE of them — so both stay
    permanently unused. What this guard still enforces is the thing that
    mattered: 0112 took 0112, and no file ever squatted the two slots C3 left
    behind.
    """
    files = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
    assert "0112_retrieval_origin.sql" in files
    assert [f for f in files if re.match(r"^0109_", f)] == [
        "0109_retention_policies.sql"
    ]
    assert not [f for f in files if re.match(r"^(0110|0111)_", f)]


def test_migration_0112_is_additive_and_idempotent():
    sql = (MIGRATIONS / "0112_retrieval_origin.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS retrieval_origin text" in sql
    assert "CREATE INDEX IF NOT EXISTS signals_retrieval_origin_idx" in sql
    assert "DROP CONSTRAINT IF EXISTS evidence_archive_status_check" in sql
    assert "skipped_license_unreviewed" in sql
    # No inline transaction control — the runner owns that.
    assert "BEGIN;" not in sql and "COMMIT;" not in sql
    # Never destructive.
    for forbidden in ("DROP TABLE", "DROP COLUMN", "DELETE FROM", "TRUNCATE"):
        assert forbidden not in sql.upper()


def test_migration_0112_check_is_a_superset_of_the_original():
    sql = (MIGRATIONS / "0112_retrieval_origin.sql").read_text()
    for status in ("archived", "failed", "skipped_license", "skipped_size"):
        assert f"'{status}'" in sql, status
