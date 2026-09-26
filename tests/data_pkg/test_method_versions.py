# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""H12 — a method / scale version on every instrument read.

Every numeric instrument the platform publishes declares ``METHOD_VERSION``
beside the constants it versions and writes it onto the row the instrument
produces: ``data.method_version`` on ``analyst_outputs`` payloads,
``situations.data.method_version`` on every situation write, and a
``method_version`` column on the dedicated ledgers (``acute_forecasts``,
``band_calibration_claims``, ``grader_calibrations``). ``docs/ANALYSIS.md``
§10.9 lists every version with what changed and when — the parse-the-doc test
below keeps the table honest (the ``test_doc_counts_consistent`` precedent: a
doc that can drift silently is a doc nobody trusts).

Pure — no DB, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"
ANALYSIS = REPO_ROOT / "docs" / "ANALYSIS.md"

#: A module-level ``METHOD_VERSION = "instrument/tag"`` assignment.
_DECL_RE = re.compile(r'^METHOD_VERSION(?::\s*str)?\s*=\s*"([^"]+)"', re.MULTILINE)

#: The §10.9 table's version column.
_ROW_RE = re.compile(r"^\| `([^`]+)` \| `([^`]+)` \|", re.MULTILINE)


def _declared_versions() -> dict[str, str]:
    """Every ``METHOD_VERSION`` constant in ``src/`` → {module_path: value}."""
    found: dict[str, str] = {}
    for path in sorted(SRC.rglob("*.py")):
        for m in _DECL_RE.finditer(path.read_text(encoding="utf-8")):
            found[str(path.relative_to(REPO_ROOT))] = m.group(1)
    return found


def _doc_table_versions() -> dict[str, str]:
    """The §10.9 "Method versions" table → {instrument: version}."""
    text = ANALYSIS.read_text(encoding="utf-8")
    section = text.split("### 10.9 Method versions", 1)[1].split("### ", 1)[0]
    return {m.group(1): m.group(2) for m in _ROW_RE.finditer(section)}


def test_every_method_version_constant_has_a_docs_row() -> None:
    """THE EXHAUSTIVE PIN — a METHOD_VERSION declared in code with no table row
    is an instrument whose revision is undocumented; a table row with no
    constant is a version nothing emits. Both directions are checked."""
    declared = _declared_versions()
    table = _doc_table_versions()
    assert declared, "no METHOD_VERSION constants found in src/ — the grep broke"
    missing = {
        f"{mod} ({ver})"
        for mod, ver in declared.items()
        if ver not in table.values()
    }
    assert not missing, (
        "METHOD_VERSION constants with no §10.9 table row: " + ", ".join(missing)
    )


def test_the_table_names_every_instrument_by_module() -> None:
    """The instrument column names the module, so the row is greppable from the
    constant's home and vice versa."""
    declared = _declared_versions()
    table = _doc_table_versions()
    for mod in declared:
        # Private-leaf modules drop the leading underscore in their public
        # instrument name (`_correctness_calibration` → `correctness_calibration`).
        stem = Path(mod).stem.lstrip("_")
        assert stem in table, f"{mod}: no §10.9 row names this instrument"


def test_retroactive_boundaries_are_documented() -> None:
    """The lane's retroactive row: the 0188 mega-frame split re-based
    ``intensity_score`` — a pre-stamp revision boundary the table must name so
    a pre/post diff reads as an instrument change, not a world change."""
    text = ANALYSIS.read_text(encoding="utf-8")
    section = text.split("### 10.9 Method versions", 1)[1]
    assert "0188" in section and "re-base" in section


# ---------------------------------------------------------------------------
# The stamp actually lands on every row each instrument writes
# ---------------------------------------------------------------------------


def test_scorecard_payload_carries_the_banding_version() -> None:
    from legba.data.analysts.deterministic_handlers import (
        scorecard_banding as sb,
        scorecard_producer as sp,
    )

    verdict = {
        "target_id": "country_g20_us",
        "generated_at": "2026-09-24T00:00:00+00:00",
        "floors": {},
        "dimensions": {},
        "composition": {"present": False, "basis": []},
    }
    payload = sp.build_scorecard_payload("country_g20_us", verdict)
    assert payload.data["method_version"] == sb.METHOD_VERSION


def test_situation_fields_carry_the_clustering_version() -> None:
    from datetime import datetime, timezone

    from legba.data.analysts.deterministic_handlers import (
        situation_clustering as sc,
    )

    rows = [
        {
            "id": "a",
            "title": "a frame",
            "produced_at": datetime(2026, 9, 20, tzinfo=timezone.utc),
        }
    ]
    fields = sc._situation_fields(
        "sig:x", rows, now=datetime(2026, 9, 24, tzinfo=timezone.utc)
    )
    assert fields["method_version"] == sc.METHOD_VERSION


def test_situation_receipt_carries_the_version() -> None:
    from legba.data.analysts.deterministic_handlers import (
        situation_clustering as sc,
    )

    receipt = sc._build_finding(created=1, updated=2, clusters=[], target_id=None)
    assert receipt.data["method_version"] == sc.METHOD_VERSION


def test_calibration_receipt_carries_the_version() -> None:
    from legba.data.analysts.deterministic_handlers import (
        calibration_tracking as ct,
    )

    receipt = ct._build_finding(
        brier=None,
        sample_size=0,
        reliability_bins=[],
        per_analyst={},
        rolling=[],
        drift_z=None,
        drift_threshold=2.0,
        resolution_sources={},
        self_consistency_only=False,
        brier_exogenous=None,
        brier_self_consistency=None,
        brier_pooled=None,
        exogenous_sample_size=0,
        self_consistency_fraction=0.0,
        insufficient_exogenous=False,
        forecast_acute={},
        warnings=[],
        target_id=None,
    )
    assert receipt.data["method_version"] == ct.METHOD_VERSION


def test_band_calibration_receipt_and_claim_sql_carry_the_version() -> None:
    from legba.data.analysts.deterministic_handlers import (
        band_calibration_tracker as bct,
    )

    receipt = bct.build_finding(
        summary={},
        logged=0,
        resolved_by_horizon={},
        skipped_non_directional=0,
        scanned_rows=0,
        warnings=[],
    )
    assert receipt.data["method_version"] == bct.METHOD_VERSION
    # …and the claims ledger's INSERT writes the column (migration 0211).
    assert "method_version" in bct._INSERT_CLAIM_SQL


def test_forecast_mint_stamps_the_version_column() -> None:
    """``acute_forecasts`` has no JSONB — the version is a column, minted beside
    ``method`` (which always existed; a revision never did)."""
    import inspect

    from legba.data.analysts.deterministic_handlers import forecast_acute

    src = inspect.getsource(forecast_acute.issue_weekly_forecasts)
    assert "method_version" in src and "METHOD_VERSION" in src
    assert "METHOD_VERSION" in forecast_acute.__all__


def test_grader_calibration_writers_stamp_the_version() -> None:
    """Both ``grader_calibrations`` writers (the regate script and the seed
    script) carry the column — asserted on their source, the same pattern the
    doc tests use for files tests cannot import."""
    for name in ("correctness_regate.py", "load_unit_reference.py"):
        text = (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "method_version" in text and "METHOD_VERSION" in text, name


def test_country_scorecard_route_projects_the_stamp() -> None:
    """The eval route surfaces ``method_version`` so the panel's chip reads it
    (``None`` on a pre-H12 card — never a guessed value)."""
    from legba.data.registry.v3_api import CountryScorecard

    assert "method_version" in CountryScorecard.model_fields


def test_version_format_is_pinned() -> None:
    """``<instrument>/2026-09.N`` — the human-readable form the lane fixes."""
    for mod, ver in _declared_versions().items():
        assert re.fullmatch(r"[a-z_]+/\d{4}-\d{2}\.\d+", ver), f"{mod}: {ver!r}"
