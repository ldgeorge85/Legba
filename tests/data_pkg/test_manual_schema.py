# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the manual-ingest batch format + per-kind schemas (S4-T1).

Pure validation-layer tests — NO DB (the loader adapter S4-T2 is what writes).
Covers (planning/MANUAL_INGESTION_AND_RAG_PLAN §A + PORTFOLIO plan S4-T1):

  * a valid fixture batch round-trips through ``validate_batch`` — all five
    lanes parse into their typed records with the sketched natural keys.
  * a batch with bad records is REJECTED with PER-LINE errors (file + 1-indexed
    line + reason), not one opaque failure; blank lines are skipped without
    throwing the line numbering off.
  * the confidence policy: per-record OR batch default, REFUSED absent (no
    silent 1.0) for the asserting lanes.
  * provenance tier: ``curated`` is grounding-eligible, ``manual`` is not, and
    the default is the SAFE ``manual``.
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from legba.data.seed import (
    BatchManifest,
    BatchMode,
    BatchValidationError,
    ManualDocRecord,
    ManualFactRecord,
    ManualNexusRecord,
    ProvenanceTier,
    load_manifest,
    validate_batch,
)
from legba.data.seed.manual_schema import MANUAL_BATCH_SCHEMA_VERSION

FIXTURES = Path(__file__).parent / "fixtures"
VALID_BATCH = FIXTURES / "manual_batch_valid"
BAD_BATCH = FIXTURES / "manual_batch_bad"


# ---------------------------------------------------------------------------
# Round-trip: the valid fixture batch
# ---------------------------------------------------------------------------


def test_valid_batch_round_trips():
    result = validate_batch(VALID_BATCH)
    assert result.ok, f"unexpected errors: {[str(e) for e in result.errors]}"

    # Every lane parsed into its typed records.
    assert len(result.facts) == 3
    assert len(result.entities) == 2
    assert len(result.nexuses) == 2
    assert len(result.signals) == 2
    assert len(result.docs) == 2

    # Manifest carried its identity + defaults.
    m = result.manifest
    assert m.batch_id == "fixture-valid-2026-07-02"
    assert m.default_provenance is ProvenanceTier.CURATED
    assert m.mode is BatchMode.SKIP
    assert m.default_confidence == pytest.approx(0.9)
    assert m.license == "CC0-1.0"

    # A fact that OMITTED confidence is accepted (batch default 0.9 covers it);
    # its own field stays None (the loader applies the default at write time).
    capital = next(f for f in result.facts if f.predicate == "capital")
    assert capital.confidence is None
    assert isinstance(capital.valid_from, datetime)

    # A fact that supplied confidence keeps it.
    hos = next(f for f in result.facts if f.predicate == "head of state")
    assert hos.confidence == pytest.approx(0.95)


def test_valid_batch_natural_keys_match_the_design_sketch():
    result = validate_batch(VALID_BATCH)

    fact = next(f for f in result.facts if f.predicate == "head of state")
    assert fact.natural_key() == ("Testlandia", "head of state", fact.valid_from)

    entity = result.entities[0]
    assert entity.natural_key() == entity.canonical_name

    nexus = result.nexuses[0]
    assert nexus.natural_key() == (
        nexus.subject, nexus.rel_type, nexus.object, nexus.valid_from,
    )

    # signal natural key = explicit external_id when present, else None.
    with_id = next(s for s in result.signals if s.external_id)
    assert with_id.natural_key() == with_id.external_id
    without_id = next(s for s in result.signals if not s.external_id)
    assert without_id.natural_key() is None

    doc = result.docs[0]
    assert doc.natural_key() == (doc.corpus, doc.doc_id, doc.chunk_seq)


# ---------------------------------------------------------------------------
# Rejection: per-line errors
# ---------------------------------------------------------------------------


def test_bad_batch_reports_per_line_errors():
    result = validate_batch(BAD_BATCH)
    assert not result.ok

    # facts.jsonl: line 1 valid, line 2 BLANK (skipped), lines 3/4/5 each bad.
    fact_errs = {e.line for e in result.errors if e.file == "facts.jsonl"}
    assert fact_errs == {3, 4, 5}, fact_errs
    # The blank line 2 is NOT reported — blank-line skipping keeps numbering true.
    assert 2 not in fact_errs
    # The one good record (line 1) still parsed despite its bad neighbours.
    assert len(result.facts) == 1
    assert result.facts[0].predicate == "p1"

    by_line = {
        e.line: e.reason for e in result.errors if e.file == "facts.jsonl"
    }
    # line 3: missing required valid_from -> a field-level validation message.
    assert "valid_from" in by_line[3]
    # line 4: malformed JSON is named as such (not a schema error).
    assert "JSON" in by_line[4]
    # line 5: REFUSE-absent confidence (manifest has no default_confidence).
    assert "confidence" in by_line[5]

    # nexuses.jsonl: polarity out of [-1, 1] on line 1.
    nexus_errs = [e for e in result.errors if e.file == "nexuses.jsonl"]
    assert len(nexus_errs) == 1
    assert nexus_errs[0].line == 1
    assert "polarity" in nexus_errs[0].reason


def test_record_error_is_a_greppable_one_liner():
    result = validate_batch(BAD_BATCH)
    err = next(e for e in result.errors if e.file == "facts.jsonl" and e.line == 5)
    assert str(err).startswith("facts.jsonl:5 [facts]")


def test_strict_mode_raises_with_line_numbers():
    with pytest.raises(BatchValidationError) as ei:
        validate_batch(BAD_BATCH, strict=True)
    errors = ei.value.errors
    assert errors, "the exception carries the per-line errors"
    # Every carried error names a concrete file + line.
    for e in errors:
        assert e.file and isinstance(e.line, int)


# ---------------------------------------------------------------------------
# Confidence policy — REFUSE absent (no silent 1.0)
# ---------------------------------------------------------------------------


def _write_min_batch(tmp: Path, *, manifest_yaml: str, facts_lines: str) -> Path:
    (tmp / "batch_manifest.yaml").write_text(manifest_yaml, encoding="utf-8")
    (tmp / "facts.jsonl").write_text(facts_lines, encoding="utf-8")
    return tmp


_FACT_NO_CONF = (
    '{"subject": "A", "predicate": "p", "value": "v", '
    '"valid_from": "2025-01-01"}\n'
)


def test_confidence_refused_when_absent_and_no_batch_default():
    with tempfile.TemporaryDirectory() as td:
        batch = _write_min_batch(
            Path(td),
            manifest_yaml=(
                'schema_version: "1"\n'
                "batch_id: t\n"
                "operator: legba-dev\n"
                "created_at: 2026-07-02T00:00:00Z\n"
                "files: {facts: facts.jsonl}\n"
            ),
            facts_lines=_FACT_NO_CONF,
        )
        result = validate_batch(batch)
    assert not result.ok
    assert len(result.facts) == 0
    assert result.errors[0].line == 1
    assert "silent 1.0" in result.errors[0].reason


def test_confidence_from_batch_default_is_accepted():
    with tempfile.TemporaryDirectory() as td:
        batch = _write_min_batch(
            Path(td),
            manifest_yaml=(
                'schema_version: "1"\n'
                "batch_id: t\n"
                "operator: legba-dev\n"
                "created_at: 2026-07-02T00:00:00Z\n"
                "default_confidence: 0.7\n"
                "files: {facts: facts.jsonl}\n"
            ),
            facts_lines=_FACT_NO_CONF,
        )
        result = validate_batch(batch)
    assert result.ok
    assert len(result.facts) == 1
    # The record keeps its None; the batch default is applied by the loader.
    assert result.facts[0].confidence is None


# ---------------------------------------------------------------------------
# Provenance tier + grounding-eligibility
# ---------------------------------------------------------------------------


def test_provenance_tier_grounding_eligibility():
    assert ProvenanceTier.CURATED.grounding_eligible is True
    assert ProvenanceTier.MANUAL.grounding_eligible is False


def test_default_provenance_is_safe_manual():
    """A manifest that omits the tier defaults to the NON-grounding ``manual``."""
    m = load_manifest(
        'schema_version: "1"\n'
        "batch_id: t\n"
        "operator: legba-dev\n"
        "created_at: 2026-07-02T00:00:00Z\n"
        "files: {facts: facts.jsonl}\n"
    )
    assert m.default_provenance is ProvenanceTier.MANUAL
    assert m.grounding_eligible is False


def test_curated_manifest_is_grounding_eligible():
    m = load_manifest(
        'schema_version: "1"\n'
        "batch_id: t\n"
        "operator: legba-dev\n"
        "created_at: 2026-07-02T00:00:00Z\n"
        "default_provenance: curated\n"
        "files: {facts: facts.jsonl}\n"
    )
    assert m.grounding_eligible is True


# ---------------------------------------------------------------------------
# Manifest fail-loud paths
# ---------------------------------------------------------------------------


def test_schema_version_mismatch_fails_loud():
    with pytest.raises(ValueError, match="schema_version"):
        load_manifest(
            'schema_version: "999"\n'
            "batch_id: t\n"
            "operator: legba-dev\n"
            "created_at: 2026-07-02T00:00:00Z\n"
            "files: {facts: facts.jsonl}\n"
        )


def test_empty_batch_declares_no_lane_is_rejected():
    with pytest.raises(ValueError, match="no lane"):
        load_manifest(
            'schema_version: "1"\n'
            "batch_id: t\n"
            "operator: legba-dev\n"
            "created_at: 2026-07-02T00:00:00Z\n"
            "files: {}\n"
        )


def test_missing_lane_file_reported_not_thrown():
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "batch_manifest.yaml").write_text(
            'schema_version: "1"\n'
            "batch_id: t\n"
            "operator: legba-dev\n"
            "created_at: 2026-07-02T00:00:00Z\n"
            "default_confidence: 0.5\n"
            "files: {facts: facts.jsonl}\n",
            encoding="utf-8",
        )
        # facts.jsonl deliberately not written.
        result = validate_batch(Path(td))
    assert not result.ok
    assert result.errors[0].file == "facts.jsonl"
    assert result.errors[0].line == 0
    assert "not found" in result.errors[0].reason


# ---------------------------------------------------------------------------
# extra="forbid" — an undeclared field is a per-line error (operator typo)
# ---------------------------------------------------------------------------


def test_unknown_field_rejected_per_line():
    with tempfile.TemporaryDirectory() as td:
        batch = _write_min_batch(
            Path(td),
            manifest_yaml=(
                'schema_version: "1"\n'
                "batch_id: t\n"
                "operator: legba-dev\n"
                "created_at: 2026-07-02T00:00:00Z\n"
                "default_confidence: 0.5\n"
                "files: {facts: facts.jsonl}\n"
            ),
            facts_lines=(
                '{"subject": "A", "predicate": "p", "value": "v", '
                '"valid_from": "2025-01-01", "confdence": 0.5}\n'  # typo'd key
            ),
        )
        result = validate_batch(batch)
    assert not result.ok
    assert result.errors[0].line == 1
    assert "confdence" in result.errors[0].reason


# ---------------------------------------------------------------------------
# Record models stand alone (importable, constructible)
# ---------------------------------------------------------------------------


def test_record_models_construct_directly():
    f = ManualFactRecord(
        subject="A", predicate="p", value="v",
        valid_from=datetime(2025, 1, 1),
    )
    assert f.confidence is None
    assert f.natural_key() == ("A", "p", datetime(2025, 1, 1))

    n = ManualNexusRecord(
        subject="A", object="B", rel_type="allied with", polarity=1,
        valid_from=datetime(2025, 1, 1),
    )
    assert n.channel == "direct"

    d = ManualDocRecord(corpus="world_context", doc_id="x")
    assert d.chunk_seq == 0
    assert d.natural_key() == ("world_context", "x", 0)


def test_schema_version_constant_matches_default():
    assert MANUAL_BATCH_SCHEMA_VERSION == "1"


def test_manifest_model_is_strict():
    """extra='forbid' — an unknown top-level manifest key fails loud."""
    with pytest.raises(Exception):
        BatchManifest.model_validate(
            {
                "schema_version": "1",
                "batch_id": "t",
                "operator": "legba-dev",
                "created_at": "2026-07-02T00:00:00Z",
                "files": {"facts": "facts.jsonl"},
                "bogus_key": 1,
            }
        )
