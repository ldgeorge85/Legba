# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Citations as rows, the pure parts (no database).

* the planner: every sentence a row with its offsets; each marker resolved
  through the stored citation entries to a typed target; a grounding block
  to one row per member; a desk's own earlier read marked ``restates``; a
  marker or entry that names nothing logged with its reason; the unmarked
  basis fallback bound to no sentence; a composition's carried unit markers
  counted apart, never logged;
* the handed list from a desk's render index (signal bags and prebuilt
  grounding citations);
* the declarations held to the migration (the typed columns, the reasons,
  the roles) and to ``inline_target``'s index key;
* the report SQL names every declared kind; the switches' defaults.

Invented, neutral fixtures throughout.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from legba.data import citation_rows as CR
from legba.data import citation_rows_store as CS
from legba.data.citation_rows_audit import report_sql
from legba.data.type_registry import default_registry
from legba.data.type_registry.citation_targets import SUPPORT_COLUMNS

REPO = Path(__file__).resolve().parents[2]
MIGRATION = next((REPO / "src/legba/data/migrations").glob("*_citations_as_rows.sql")).read_text()

S1 = "11111111-1111-4111-8111-111111111111"
S2 = "22222222-2222-4222-8222-222222222222"
F1 = "33333333-3333-4333-8333-333333333333"
F2 = "44444444-4444-4444-8444-444444444444"
Q1 = "55555555-5555-4555-8555-555555555555"

BODY = (
    "## The read\n"
    "Harbour traffic at the invented port of Velmara stopped on 3 March [1][2]. "
    "A ministry said the closure was temporary [5]. "
    "The window ledger shows the same pattern [3]. Questions stand [4].\n"
    "Nothing else changed."
)
CITATIONS = [
    {"marker": "[1]", "signal_id": S1, "source_id": "source.test.dm_a", "source_text": "Traffic stopped.",
     "claim": {"claimant": "Velmara port union"}},
    {"marker": "[2]", "ordinal": 2, "ref_kind": "prior_read", "ref_id": F1, "evidence_text": "Earlier read."},
    {"marker": "[3]", "ref_kind": "window_ledger", "ledger_finding_ids": [F1, F2, "not-a-uuid"],
     "evidence_text": "Ledger."},
    {"marker": "[4]", "ref_kind": "open_questions", "question_ids": [Q1], "evidence_text": "Q."},
    {"marker": "[9]", "signal_id": S2, "resolution": "fallback_basis", "snippet": "x"},
    {"ref_kind": "desk_baseline", "baseline_keys": []},
    {"marker": "[7]", "ref_kind": "no_such_kind", "ref_id": S2},
]


def _plan(**kw):
    return CR.plan_citation_rows(BODY, CITATIONS, **kw)


def test_every_sentence_is_a_row_with_offsets_into_the_body():
    plan = _plan()
    assert [s.ordinal for s in plan.sentences] == list(range(len(plan.sentences)))
    assert plan.sentences[0].kind == "heading"
    for s in plan.sentences:
        assert BODY[s.char_start:s.char_end] == s.text
        assert re.fullmatch(r"[0-9a-f]{64}", s.text_sha256)
    assert plan.sentences[-1].text == "Nothing else changed." and plan.sentences[-1].marker_count == 0
    assert plan.cited_sentences == 4


def test_markers_resolve_to_typed_targets_with_roles():
    plan = _plan()
    by = {(s.sentence_ordinal, s.marker, s.target.kind, s.target.key): s for s in plan.supports}
    first = next(s for s in plan.supports if s.marker == "[1]")
    assert first.role == "cites" and first.target.column == "signal_id"
    assert (first.stance, first.speaker, first.source_id) == ("claim", "Velmara port union", "source.test.dm_a")
    assert first.shown_field == "source_text" and first.shown_sha256
    prior = next(s for s in plan.supports if s.marker == "[2]")
    assert prior.role == "restates" and prior.target.kind == "finding" and prior.target.column == "output_id"
    assert prior.sentence_ordinal == first.sentence_ordinal
    ledger = [s for s in plan.supports if s.marker == "[3]"]
    assert {s.target.key for s in ledger} == {F1, F2}
    assert all(s.role == "restates" and s.target.via_block == "window_ledger" for s in ledger)
    q = next(s for s in plan.supports if s.marker == "[4]")
    assert (q.target.kind, q.target.column, q.target.via_block, q.role) == (
        "hypothesis", "hypothesis_id", "open_questions", "cites")
    basis = next(s for s in plan.supports if s.marker == "[9]")
    assert basis.role == "basis" and basis.sentence_ordinal is None and basis.stance == "report"
    assert len(by) == len(plan.supports)


def test_what_names_nothing_is_logged_with_its_reason():
    reasons = sorted(((u.marker, u.reason) for u in _plan().unresolved), key=str)
    assert ("[5]", "marker_not_in_citations") in reasons
    assert ("[3]", "malformed_id") in reasons
    assert ("[7]", "unknown_ref_kind") in reasons
    assert (None, "entry_without_target") in reasons  # the empty baseline block
    assert {r for _, r in reasons} <= set(CR.UNRESOLVED_REASONS)


def test_a_compositions_carried_unit_markers_are_counted_apart():
    body = "Velmara stays closed [[ref:1]]. The unit said traffic stopped [4]. Unknown [[ref:3]]."
    cits = [{"marker": "[[ref:1]]", "ref_kind": "finding", "ref_id": F1}]
    plan = CR.plan_citation_rows(body, cits)
    assert plan.foreign_markers == 1 and plan.markers == 2
    assert [(u.marker, u.reason) for u in plan.unresolved] == [("[[ref:3]]", "marker_not_in_citations")]
    assert [(s.marker, s.target.kind) for s in plan.supports] == [("[[ref:1]]", "finding")]


def test_a_marker_after_the_full_stop_belongs_to_the_sentence_it_closes():
    plan = CR.plan_citation_rows("Traffic stopped. [1] Then it resumed.", [{"marker": "[1]", "signal_id": S1}])
    assert plan.supports[0].sentence_ordinal == 0
    assert plan.sentences[0].marker_count == 1 and plan.sentences[1].marker_count == 0


def test_the_handed_list_comes_from_the_render_index():
    index = {
        1: {"signal_id": S1, "title": "t"},
        2: {"signal_id": None, "title": "no id"},
        3: {CR.PREBUILT_INDEX_KEY: {"ref_kind": "situation_register", "situation_ids": [Q1]}},
        4: {CR.PREBUILT_INDEX_KEY: {"ref_kind": "prior_read", "ref_id": F1}},
    }
    handed = CR.handed_from_index(index)
    assert [(h.ordinal, h.kind, h.key, h.via_block) for h in handed] == [
        (1, "signal", S1, None), (3, "situation", Q1, "situation_register"), (4, "finding", F1, None)]
    specs = CR.target_specs()
    groups = {(g.kind, g.via_block): g for g in CR.group_handed(handed, specs)}
    assert groups[("signal", None)].keys == (S1,) and groups[("signal", None)].ordinals == (1,)
    plan = CR.plan_citation_rows("A [1].", [{"marker": "[1]", "signal_id": S1}], handed=handed)
    assert plan.handed_count == 3 and plan.handed is not None
    assert CR.plan_citation_rows("A [1].", [{"marker": "[1]", "signal_id": S1}]).handed is None


def test_inline_target_keys_its_index_as_the_planner_reads_it():
    from legba.data.analysts import inline_target as IT

    assert IT._PREBUILT_CITATION_KEY == CR.PREBUILT_INDEX_KEY
    assert IT._extract_citations("A [1].", {1: {"signal_id": S1}})[0][0]["signal_id"] == S1
    assert IT.AnalystMethodResult.__dataclass_fields__["handed_refs"].default is None


def _support_columns() -> list[str]:
    block = MIGRATION.split("CREATE TABLE IF NOT EXISTS public.citation_support", 1)[1].split("\n);", 1)[0]
    one = block.split("num_nonnulls(", 1)[1].split(")", 1)[0]
    return [c.strip() for c in one.replace("\n", " ").split(",")]


def test_the_declarations_are_the_migrations_columns_and_lists():
    assert _support_columns() == list(SUPPORT_COLUMNS)
    decls = default_registry().types("citation_target")
    assert {d.id for d in decls} == set(CR.target_specs())
    for d in decls:
        assert d.attr("column") in SUPPORT_COLUMNS
        assert re.search(rf"^\s+{d.attr('column')}\s", MIGRATION, re.M), d.id
    reasons = MIGRATION.split("reason            text NOT NULL CHECK (reason IN (", 1)[1].split("))", 1)[0]
    assert re.findall(r"'([a-z_]+)'", reasons) == list(CR.UNRESOLVED_REASONS)
    assert "CHECK (role IN ('cites', 'restates', 'basis'))" in MIGRATION
    for col in ("written_by", "backfill_batch", "origin"):
        for table in ("finding_sentences", "citation_support", "citation_unresolved", "run_handed",
                      "finding_citation_receipts"):
            section = MIGRATION.split(f"CREATE TABLE IF NOT EXISTS public.{table}", 1)[1].split("\n);", 1)[0]
            assert re.search(rf"^\s+{col}\s", section, re.M), (table, col)


def test_the_report_sql_names_every_declared_kind():
    for source in ("json", "rows"):
        sql = report_sql(source, day_start="2026-11-14T00:00:00+00:00", day_end="2026-11-15T00:00:00+00:00")
        for kind in CR.target_specs():
            assert f"'{kind}'" in sql
        assert "UPDATE " not in sql.upper().replace("UPDATED", "") and "DELETE" not in sql.upper()
    with pytest.raises(ValueError):
        report_sql("trace", day_start="a", day_end="b")


def test_the_switches_default_to_write_and_not_refuse(monkeypatch):
    monkeypatch.delenv(CS.CITATION_ROWS_ENV, raising=False)
    monkeypatch.delenv(CS.REFUSE_UNRESOLVED_ENV, raising=False)
    assert CS.citation_rows_enabled() and not CS.refuse_unresolved_enabled()
    monkeypatch.setenv(CS.CITATION_ROWS_ENV, "0")
    monkeypatch.setenv(CS.REFUSE_UNRESOLVED_ENV, "1")
    assert not CS.citation_rows_enabled() and CS.refuse_unresolved_enabled()
