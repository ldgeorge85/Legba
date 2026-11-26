# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regressions on the consult surface.

* the per-round tool-result bound must re-render from the RAW result, so a
  result trimmed once is never trimmed again with its ``rows_total`` replaced
  by the trimmed count (list_findings 30 -> 12, query_events 50 -> ~21);
* the consult prompt tells the model ``since_hours`` is only a lower bound and
  no longer steers it to the frozen ``query_predictions`` feed, which is not in
  the consult's tool palette either;
* the provenance census resolves the ids the event drill tool hands out
  (lifecycle ledger rows, edges, actors) instead of calling them fabricated.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from legba.data.analysts.consult_on_demand import _KNOWN_TOOLS, _bounded_tool_json
from legba.data.analysts.consult_system_prompt import _SYSTEM_PROMPT
from legba.data.stack.llm.tool_round_compaction import allocate_round_bodies
from legba.runtime import substrate_cited_refs as cited


def _findings(n: int, body_chars: int = 4000) -> dict[str, Any]:
    rows = [{"id": f"f-{i}", "target_id": f"t{i}", "body": "x" * body_chars}
            for i in range(n)]
    return {"rows": rows, "refs": [r["id"] for r in rows], "count": n}


def _small_error() -> dict[str, Any]:
    return {"error": "boom", "rows": [], "refs": [], "count": 0}


def test_double_trim_keeps_the_true_rows_total():
    big = _findings(30)  # ~123 KB: trimmed at the 40 KB natural pass
    results = [big, _findings(50, 800), _small_error(), _small_error(), _small_error()]
    out = allocate_round_bodies(
        results, render=_bounded_tool_json, total_bound=40_000,
    )
    first = json.loads(out[0])
    assert first["truncated"] is True
    assert first["rows_total"] == 30            # was 12 (the pre-trimmed count)
    assert first["rows_shown"] == len(first["rows"])
    assert 0 < first["rows_shown"] < 30
    second = json.loads(out[1])
    assert second["rows_total"] == 50
    assert second["rows_shown"] < 50
    # the small errors are untouched
    assert json.loads(out[2]) == results[2]


def test_bounded_json_keeps_a_stated_total_and_reports_rows_shown():
    result = _findings(30)
    result["rows_total"] = 30
    parsed = json.loads(_bounded_tool_json(result, 20_000))
    assert parsed["rows_total"] == 30
    assert parsed["rows_shown"] == len(parsed["rows"]) < 30
    # a stale, smaller stated total never undercuts the rows actually present
    result["rows_total"] = 5
    parsed = json.loads(_bounded_tool_json(result, 20_000))
    assert parsed["rows_total"] == 30


def test_prompt_says_since_hours_is_a_lower_bound_and_drops_predictions():
    assert "LOWER BOUND" in _SYSTEM_PROMPT
    assert "list_findings(target_id=...)" in _SYSTEM_PROMPT
    assert "query_predictions" not in _SYSTEM_PROMPT


def test_consult_is_not_offered_query_predictions():
    assert "query_predictions" not in _KNOWN_TOOLS
    assert {"list_findings", "list_situations", "query_events"} <= _KNOWN_TOOLS


# ---------------------------------------------------------------------------
# F155 — the census resolves every id kind the event drill hands out
# ---------------------------------------------------------------------------


class _Conn:
    def __init__(self, tables: dict[str, list[dict[str, Any]]]):
        self.tables = tables

    async def fetch(self, sql: str, wanted: list[Any]):
        table = sql.split(" FROM ", 1)[1].split()[0]
        return [r for r in self.tables.get(table, []) if r["id"] in wanted]


class _Pool:
    def __init__(self, tables):
        self.conn = _Conn(tables)

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_census_resolves_every_drill_ref_kind_and_the_unclassified_ones():
    ids = {k: uuid4() for k in (
        "lifecycle", "edge", "entity", "situation_event",
        "finding", "prediction", "situation", "hypothesis", "ghost",
    )}
    pool = _Pool({
        "event_lifecycle_events": [{"id": ids["lifecycle"], "kind": None}],
        "event_edges": [{"id": ids["edge"], "kind": None}],
        "entity_profiles": [{"id": ids["entity"], "kind": None}],
        "situation_events": [{"id": ids["situation_event"], "kind": None}],
        "analyst_outputs": [
            {"id": ids["finding"], "kind": "finding"},
            {"id": ids["prediction"], "kind": "prediction"},
        ],
        "situations": [{"id": ids["situation"], "kind": None}],
        "hypotheses": [{"id": ids["hypothesis"], "kind": None}],
    })
    out = await cited.classify_cited_refs(
        pool, refs=[str(v) for v in ids.values()],
    )
    assert out["by_kind"] == {
        "event_lifecycle": 1, "event_edge": 1, "entity": 1,
        "situation_event": 1, "finding": 1, "prediction": 1,
        "situation": 1, "hypothesis": 1,
    }
    assert out["matched"] == 8
    assert out["unresolved"] == 8      # real rows with no origin class
    assert out["fabricated"] == 1      # only the id that is in no table
    assert out["asked"] == 9


# ---------------------------------------------------------------------------
# Survey/paging x the per-round trimmer
# ---------------------------------------------------------------------------


def test_trim_keeps_offset_and_repoints_next_offset_at_the_first_unshown_row():
    """A paged list_findings result carries ``offset`` / ``next_offset`` /
    ``rows_total``. A trim keeps all three; ``next_offset`` moves back to the
    first row the model was not shown, so paging on never skips the cut rows."""
    result = _findings(20)
    result.update({"rows_total": 95, "offset": 40, "next_offset": 60})
    parsed = json.loads(_bounded_tool_json(result, 20_000))
    assert parsed["truncated"] is True
    assert parsed["rows_total"] == 95
    assert parsed["offset"] == 40
    shown = parsed["rows_shown"]
    assert 0 < shown < 20 and shown == len(parsed["rows"])
    assert parsed["next_offset"] == 40 + shown
    assert len(_bounded_tool_json(result, 20_000)) <= 20_000


def test_trim_of_a_last_page_adds_next_offset_for_the_cut_rows():
    """The last page has no ``next_offset``; once rows are cut there ARE more
    to read, so the trimmed copy names where they start."""
    result = _findings(20)
    result.update({"rows_total": 20})
    parsed = json.loads(_bounded_tool_json(result, 20_000))
    assert "offset" not in parsed
    assert parsed["next_offset"] == parsed["rows_shown"] < 20


def test_untrimmed_paged_result_is_byte_identical():
    result = _findings(2, body_chars=10)
    result.update({"rows_total": 50, "offset": 20, "next_offset": 22})
    assert json.loads(_bounded_tool_json(result, 20_000)) == result


def test_a_tool_without_a_stated_total_gets_no_next_offset():
    parsed = json.loads(_bounded_tool_json(_findings(30), 20_000))
    assert parsed["truncated"] is True and "next_offset" not in parsed


class _RecordingFindingsPort:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def list_findings(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"rows": [], "refs": [], "count": 0}


@pytest.mark.asyncio
async def test_routing_fallback_forwards_survey_and_paging_like_the_pack():
    """``_dispatch_tool`` (the leaf's own dispatch) forwards mode / target_ids
    / offset and leaves ``limit`` unset for a survey, exactly as the pack
    handler's ``_findings_paging_kw`` does."""
    from legba.data.analysts.agency.substrate_read import _findings_paging_kw
    from legba.data.analysts.consult_tool_routing import _dispatch_tool

    cases = [
        {},
        {"mode": "Survey"},
        {"mode": "survey", "target_ids": "country_g20_ir, country_g20_us", "offset": "25"},
        {"target_ids": ["a"], "limit": 5, "offset": 40},
    ]
    for args in cases:
        port = _RecordingFindingsPort()
        await _dispatch_tool(port, name="list_findings", args=args, scope_predicate=None)
        sent = port.calls[0]
        for key, value in _findings_paging_kw(dict(args)).items():
            assert sent[key] == value, (args, key)
        if args.get("mode", "").lower() == "survey" and "limit" not in args:
            assert "limit" not in sent
        else:
            assert sent["limit"] == int(args.get("limit", 20))
