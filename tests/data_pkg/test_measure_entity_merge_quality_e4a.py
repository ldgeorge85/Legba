# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""E4a (#223) — unit tests for ``scripts/measure_entity_merge_quality.py``.

Pure — no DB, no LLM, no network (the sampling half of the script is
integration-only I/O against the live graph; it is not exercised here — see
the script's own module docstring for the safety model on that half:
read-only Postgres SESSION + SELECT-only queries). This file exercises the
PURE reduction/scoring path with a tiny hand-built worksheet fixture:

  * ``rows_to_predicted_clusters`` / ``rows_to_gold_clusters`` are simple,
    correct reductions (gold DROPS blank rows rather than guessing);
  * ``score_worksheet_rows`` produces the EXACT pairwise/B-cubed numbers a
    hand-computed fixture predicts (a known correct fold, a known missed
    fold, a known false-positive fold, and an unlabeled row that must be
    excluded from scoring entirely);
  * the CSV worksheet round-trips byte-for-byte through
    ``write_worksheet``/``read_worksheet`` (the header-comment skip, the
    field order, an empty ``gold_cluster`` staying empty — not becoming the
    string ``'None'`` or similar).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Load the script by file path (scripts/ is not an installed package) — same
# convention as tests/data_pkg/test_gen_entity_merge_election.py.
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import measure_entity_merge_quality as meq  # noqa: E402


def _row(row_id, entity_id, predicted, gold="", stratum="singleton",
         name="", cls="entity", context=""):
    return meq.WorksheetRow(
        row_id=row_id, stratum=stratum, entity_id=entity_id,
        canonical_name=name or entity_id, entity_class=cls,
        predicted_cluster=predicted, context=context, gold_cluster=gold,
    )


# ---------------------------------------------------------------------------
# Pure reductions
# ---------------------------------------------------------------------------


def test_rows_to_predicted_clusters_is_id_to_cluster_map():
    rows = [_row("r1", "a", "survivor:X"), _row("r2", "b", "survivor:X"),
            _row("r3", "c", "singleton:c")]
    assert meq.rows_to_predicted_clusters(rows) == {
        "a": "survivor:X", "b": "survivor:X", "c": "singleton:c",
    }


def test_rows_to_gold_clusters_drops_blank_rows():
    rows = [
        _row("r1", "a", "survivor:X", gold="cluster1"),
        _row("r2", "b", "survivor:X", gold=""),          # blank -> dropped
        _row("r3", "c", "singleton:c", gold="   "),       # whitespace-only -> dropped
        _row("r4", "d", "singleton:d", gold="cluster2"),
    ]
    gold = meq.rows_to_gold_clusters(rows)
    assert gold == {"a": "cluster1", "d": "cluster2"}
    assert "b" not in gold and "c" not in gold


# ---------------------------------------------------------------------------
# _seed_to_pg_setseed — the reproducible-sample seed transform.
# ---------------------------------------------------------------------------


def test_seed_to_pg_setseed_stays_in_valid_postgres_range():
    # SELECT setseed(x) requires x in [-1.0, 1.0) - verify the transform
    # never escapes that range, including negative/huge/zero/boundary seeds.
    for seed in (0, 1, 42, 2223, -1, -42, 1_999_999, 2_000_000, 2_000_001,
                 999_999_999_999, -999_999_999_999):
        v = meq._seed_to_pg_setseed(seed)
        assert -1.0 <= v < 1.0, (seed, v)


def test_seed_to_pg_setseed_is_deterministic_and_wraps_at_the_period():
    # same seed -> same float, always (a human re-running `sample --seed N`
    # expects a reproducible worksheet).
    assert meq._seed_to_pg_setseed(2223) == meq._seed_to_pg_setseed(2223)
    # the transform's period is 2_000_000 (the modulus) - two seeds exactly
    # one period apart collide (documented behavior, not a bug: an operator
    # picking two seeds 2 million apart to get "different" samples is not a
    # real-world scenario this needs to guard against).
    assert meq._seed_to_pg_setseed(5) == meq._seed_to_pg_setseed(5 + 2_000_000)


# ---------------------------------------------------------------------------
# score_worksheet_rows — the metric the whole harness exists to compute.
# ---------------------------------------------------------------------------


def test_score_perfect_fold_scores_unit():
    # predicted correctly folds {a,b,c} into one cluster; gold agrees exactly.
    rows = [
        _row("r1", "a", "survivor:X", gold="G1"),
        _row("r2", "b", "survivor:X", gold="G1"),
        _row("r3", "c", "survivor:X", gold="G1"),
        _row("r4", "d", "singleton:d", gold="G2"),  # correct singleton
    ]
    pw, bc, n_labeled, n_total = meq.score_worksheet_rows(rows)
    assert n_labeled == 4 and n_total == 4
    assert (pw.precision, pw.recall, pw.f1) == (1.0, 1.0, 1.0)
    assert (bc.precision, bc.recall, bc.f1) == (1.0, 1.0, 1.0)


def test_score_missed_merge_hurts_recall_not_precision():
    # gold says d~e are the same entity; predicted keeps them as two
    # singletons (a MISSED merge — recall drops, precision stays perfect
    # because every pair the system DID assert is correct).
    rows = [
        _row("r1", "a", "survivor:X", gold="G1"),
        _row("r2", "b", "survivor:X", gold="G1"),
        _row("r3", "c", "survivor:X", gold="G1"),
        _row("r4", "d", "singleton:d", gold="G2"),
        _row("r5", "e", "singleton:e", gold="G2"),  # gold: same as d; predicted: distinct
    ]
    pw, bc, n_labeled, n_total = meq.score_worksheet_rows(rows)
    assert n_labeled == 5
    # gold pairs: ab,ac,bc,de (4); predicted pairs: ab,ac,bc (3) -> tp=3,fp=0,fn=1
    assert (pw.tp, pw.fp, pw.fn) == (3, 0, 1)
    assert pw.precision == 1.0
    assert abs(pw.recall - 0.75) < 1e-9
    assert bc.recall < 1.0 and bc.precision == 1.0


def test_score_false_positive_merge_hurts_precision_not_recall():
    # predicted WRONGLY folds two distinct gold entities (f, g) into one
    # cluster (a false-positive merge — precision drops).
    rows = [
        _row("r1", "f", "survivor:Y", gold="G1"),
        _row("r2", "g", "survivor:Y", gold="G2"),  # gold says DIFFERENT entity
    ]
    pw, bc, n_labeled, n_total = meq.score_worksheet_rows(rows)
    assert n_labeled == 2
    assert (pw.tp, pw.fp, pw.fn) == (0, 1, 0)
    assert pw.precision == 0.0
    assert pw.recall == 1.0  # no gold pair existed to miss
    assert bc.precision < 1.0


def test_score_unlabeled_rows_excluded_not_guessed():
    # An unlabeled row must not silently become its own gold singleton (that
    # would inflate recall for free) — it is dropped from BOTH n_labeled and
    # the scored id universe.
    rows = [
        _row("r1", "a", "survivor:X", gold="G1"),
        _row("r2", "b", "survivor:X", gold="G1"),
        _row("r3", "unlabeled", "survivor:X", gold=""),  # same predicted cluster, no gold
    ]
    pw, bc, n_labeled, n_total = meq.score_worksheet_rows(rows)
    assert n_labeled == 2 and n_total == 3
    # only the (a,b) pair is scored; the unlabeled row contributes NOTHING.
    assert (pw.tp, pw.fp, pw.fn) == (1, 0, 0)
    assert bc.n_elements == 2


def test_score_empty_worksheet_is_the_eval_module_unit_convention():
    pw, bc, n_labeled, n_total = meq.score_worksheet_rows([])
    assert n_labeled == 0 and n_total == 0
    # matches _entity_eval's own empty-input convention (both metrics are
    # vacuously perfect on an empty comparison — see test_entity_eval_e4.py).
    assert (pw.precision, pw.recall, pw.f1) == (1.0, 1.0, 1.0)
    assert (bc.precision, bc.recall, bc.f1) == (1.0, 1.0, 1.0)


# ---------------------------------------------------------------------------
# CSV worksheet round-trip
# ---------------------------------------------------------------------------


def test_worksheet_roundtrips_through_csv(tmp_path):
    rows = [
        _row("r1", "a-id", "survivor:X", gold="G1", stratum="cluster_small",
             name="Test Entity, With Comma", cls="organization",
             context='aliases: "Test Entity" | Test Entity'),
        _row("r2", "b-id", "singleton:b-id", gold="", stratum="singleton",
             name="Unlabeled One", cls="person"),
    ]
    path = tmp_path / "worksheet.csv"
    meq.write_worksheet(path, rows)

    # the header-comment block must be present (labeling instructions) and
    # SKIPPED (not parsed as data) on read-back.
    text = path.read_text()
    assert text.startswith("#")
    assert meq.WORKSHEET_SCHEMA_VERSION in text

    back = meq.read_worksheet(path)
    assert len(back) == 2
    assert back[0].entity_id == "a-id"
    assert back[0].canonical_name == "Test Entity, With Comma"  # comma survives CSV quoting
    assert back[0].gold_cluster == "G1"
    assert back[1].entity_id == "b-id"
    assert back[1].gold_cluster == ""  # stays truly empty, not 'None'/'NULL'


# ---------------------------------------------------------------------------
# CSV/Excel formula-injection guard (CWE-1236) — canonical_name/context come
# from arbitrary NER-extracted, ingested text and this worksheet is designed
# to be opened by a human in Excel/Sheets to fill gold_cluster.
# ---------------------------------------------------------------------------


def test_formula_trigger_chars_get_guarded_on_write():
    for payload in ('=cmd|"/c calc"!A1', "=1+1", "+1", "-1", "@SUM(A1)"):
        assert meq._apply_formula_guard(payload) == "'" + payload


def test_non_trigger_values_untouched_by_guard():
    for value in ("Normal Name", "", "O'Brien", "the Atlantic"):
        assert meq._apply_formula_guard(value) == value


def test_formula_guard_roundtrips_for_realistic_values():
    # The guard/strip pair is lossless for every realistic sampled value
    # (anything that does NOT itself start with an apostrophe immediately
    # followed by a formula-trigger char — see test below for that one
    # documented, accepted edge case).
    for orig in ("=cmd", "+1", "-1", "@x", "Normal Name", "",
                 "O'Brien", "the Atlantic", "West Berlin"):
        guarded = meq._apply_formula_guard(orig)
        assert meq._strip_formula_guard(guarded) == orig


def test_formula_guard_documented_edge_case_apostrophe_then_trigger():
    # A value that ITSELF starts with an apostrophe immediately followed by a
    # formula-trigger char (e.g. a literal "'=Corp") is indistinguishable
    # from a guarded "=Corp" after the fact — the guard/strip pair resolves
    # this the SAFE way (never leaves a real trigger char unguarded on write)
    # at the cost of stripping one leading apostrophe on this one rare shape.
    edge_case = "'=Corp"
    guarded = meq._apply_formula_guard(edge_case)  # untouched (no trigger char at [0])
    assert guarded == edge_case
    assert meq._strip_formula_guard(guarded) == "=Corp"  # NOT byte-identical (documented)


def test_worksheet_write_read_neutralizes_formula_injection_end_to_end(tmp_path):
    rows = [
        _row("r1", "a-id", "survivor:X", gold="G1",
             name='=cmd|"/c calc"!A1', cls="entity",
             context="@SUM(A1:A10)"),
        _row("r2", "b-id", "singleton:b-id", gold="G2", name="Normal Name"),
    ]
    path = tmp_path / "worksheet_injection.csv"
    meq.write_worksheet(path, rows)

    # the RAW bytes on disk must NOT contain an unguarded formula trigger at
    # the start of a CSV field — read the file directly (bypassing
    # read_worksheet's own un-guarding) to prove the file itself is safe to
    # open in a spreadsheet app.
    raw_lines = [ln for ln in path.read_text().splitlines() if not ln.startswith("#")]
    data_lines = raw_lines[1:]  # skip the CSV header row
    assert any("'=cmd" in ln for ln in data_lines)  # guarded form present
    assert not any(",=cmd" in ln for ln in data_lines)  # unguarded form absent
    assert any("'@SUM" in ln for ln in data_lines)
    assert not any(",@SUM" in ln for ln in data_lines)

    # read_worksheet reverses the guard transparently.
    back = meq.read_worksheet(path)
    assert back[0].canonical_name == '=cmd|"/c calc"!A1'
    assert back[0].context == "@SUM(A1:A10)"
    assert back[1].canonical_name == "Normal Name"  # untouched value unaffected


def test_worksheet_survives_embedded_newline_hash_in_sampled_text(tmp_path):
    # Regression: a sampled canonical_name/context comes from arbitrary
    # ingested text and CAN contain an embedded newline (rare, but possible).
    # csv.DictWriter correctly quotes it into ONE CSV record; read_worksheet
    # must skip the header by a FIXED line count, not by filtering any line
    # that happens to start with '#' — a content-based filter would wrongly
    # strip a continuation line like "...\n#Name" and corrupt the row.
    rows = [
        _row("r1", "a-id", "survivor:X", gold="G1",
             name="Weird\n#Name", cls="person"),
        _row("r2", "b-id", "singleton:b-id", gold="G2", name="Normal Name"),
    ]
    path = tmp_path / "worksheet_edge_case.csv"
    meq.write_worksheet(path, rows)
    back = meq.read_worksheet(path)
    assert len(back) == 2  # NOT corrupted/dropped by the embedded '#' line
    assert back[0].canonical_name == "Weird\n#Name"
    assert back[0].gold_cluster == "G1"
    assert back[1].canonical_name == "Normal Name"
    assert back[1].gold_cluster == "G2"


def test_header_comment_line_count_matches_what_write_worksheet_emits(tmp_path):
    # The header-skip in read_worksheet trusts a STATIC line count
    # (_HEADER_COMMENT_LINE_COUNT) rather than re-deriving it per file: if
    # _HEADER_COMMENT is ever edited to add/remove a line without updating
    # the derivation, this test catches the drift directly (it re-derives
    # the count from the ACTUAL bytes write_worksheet produced).
    path = tmp_path / "worksheet_header_check.csv"
    meq.write_worksheet(path, [])
    written_header_lines = meq._HEADER_COMMENT.format(
        schema=meq.WORKSHEET_SCHEMA_VERSION).count("\n")
    assert meq._HEADER_COMMENT_LINE_COUNT == written_header_lines
    # and the first line AFTER the skip is the real CSV header, not a comment.
    with path.open() as f:
        remaining = f.readlines()[meq._HEADER_COMMENT_LINE_COUNT:]
    assert remaining[0].startswith("row_id,")


def test_worksheet_fields_are_stable_and_gold_cluster_is_last_labelable_column():
    # A reviewer edits gold_cluster (+ optionally labeler_note) in a
    # spreadsheet; the column SET must stay exactly what the header comment
    # promises (a silent field rename/reorder would desync the instructions
    # from the actual file).
    assert meq.WORKSHEET_FIELDS == [
        "row_id", "stratum", "entity_id", "canonical_name", "entity_class",
        "predicted_cluster", "context", "gold_cluster", "labeler_note",
    ]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
