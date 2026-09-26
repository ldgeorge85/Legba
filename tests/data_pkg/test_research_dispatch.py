# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R-B — the COVERAGE-FLOOR DISPATCH and the corpus researcher's web leg.

``planning/RESEARCH_PROGRAM_SPEC_2026-09-05.md`` §2.2 / §2.3. Four claims, and
each one is a claim about a SEAM rather than about a function in isolation:

  * **The dispatch writes ONE standing question per newly-breaching gap**,
    carrying the target + its geo, the named gap, the desk's bounded question
    (F-6) and the dispatching alert id — and it does so from the alert scan's
    REAL binding path (``deterministic.run_method`` routed by
    ``options.sub_handler``), never a direct call into the sub-module.
  * **It is idempotent under re-detection.** A persisting breach mints no
    second row; a resolved-then-re-breached gap on a later day mints a new one.
  * **The row drains.** The contract is asserted through
    ``SubstrateGroundingResolver.resolve_open_questions`` — grounding's real
    reader over the real backlog SQL — not against a hand-built dict, because
    the only thing that matters is whether the researcher's own drain sees it.
  * **Flag off is byte-identical.** ``tests/data_pkg/test_coverage_floor_scan
    .py`` passing UNCHANGED is the primary proof; the assertions here add the
    positive statement that a flag-off scan writes no question and leaves the
    receipt without the dispatch keys.

Plus the descriptor side: the ``research`` grant and the web-on-null prompt
rule, validated through ``Family.ANALYST.model.model_validate`` — the call the
registry itself makes on a PUT body — rather than a raw YAML read.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml

from legba.data.analysts import deterministic
from legba.data.analysts import research_regime as rr
from legba.data.analysts.deterministic_handlers import (
    _reference_gap_dispatch as rgd,
)
from legba.data.analysts.deterministic_handlers import _research_dispatch as rd
from legba.data.analysts.deterministic_handlers import alert_trigger_scan as ats
from legba.data.analysts.deterministic_handlers import claim_watch as cw
from legba.data.config import PostgresConfig
from legba.data.provenance import AnalystContext, FindingPayload, write_finding
from legba.data.registry.descriptor import Family
from legba.runtime import dispatched_question as dq
from legba.runtime.analyst_method import AnalystMethodResult
from legba.runtime.grounding import (
    GroundingOpenQuestion,
    SubstrateGroundingResolver,
    _HARVEST_CLASS_PRIORITY,
    _UNKNOWN_HARVEST_CLASS_PRIORITY,
    build_open_questions_block,
    dispatch_scope_of,
    harvest_class_of,
    open_question_priority_key,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DESCRIPTORS = REPO_ROOT / "descriptors"

_NOW = datetime(2026, 9, 5, 9, 4, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. THE FLAG — one env var, three values, unknown fails SAFE
# ---------------------------------------------------------------------------


def test_flag_defaults_to_off_and_gates_both_legs(monkeypatch):
    monkeypatch.delenv(rr.RESEARCH_EVIDENCE_ENV, raising=False)
    assert rr.research_evidence_regime() == rr.REGIME_OFF
    assert rr.research_evidence_enabled() is False
    assert rr.research_reaches_desks() is False


@pytest.mark.parametrize(
    "value,enabled,desks",
    [
        ("off", False, False),
        ("substrate", True, False),
        ("desks", True, True),
        ("  DESKS  ", True, True),  # trimmed + case-folded, not rejected
    ],
)
def test_flag_three_values(monkeypatch, value, enabled, desks):
    monkeypatch.setenv(rr.RESEARCH_EVIDENCE_ENV, value)
    assert rr.research_evidence_enabled() is enabled
    assert rr.research_reaches_desks() is desks


def test_unknown_flag_value_reads_as_off_not_as_desks(monkeypatch):
    """A typo must never put unreviewed web text into a desk's read."""
    monkeypatch.setenv(rr.RESEARCH_EVIDENCE_ENV, "desk")  # missing the 's'
    assert rr.research_evidence_regime() == rr.REGIME_OFF
    assert rr.research_reaches_desks() is False


# ---------------------------------------------------------------------------
# 2. THE HARVEST CLASS — coverage_floor ranks FIRST, ordinals stay relative
#
# 2026-09-06 TUNE: originally ordinal 1 (immediately after below_floor). The
# live 03:37Z corpus_researcher run picked an Aug-02 unit_payload backlog row
# over a same-week dispatched IL/Palestine coverage_floor gap — moved to
# ordinal 0 (see grounding._HARVEST_CLASS_PRIORITY's docstring and
# open_question_priority_key's AGE DECAY note for the full mechanism: the
# class ordinal alone could not have fixed this, since tier 1 (live_reach) is
# evaluated first and a fresh dispatch always has live_reach == 0).
# ---------------------------------------------------------------------------


def test_coverage_floor_ranks_first_ahead_of_below_floor():
    order = [
        "coverage_floor", "below_floor", "fact_contention",
        "freshness_advisory", "scorecard_disagreement", "unit_payload",
        "collection_gap",
    ]
    keys = [
        open_question_priority_key(
            live_reach=0, harvest_class=c, desk_salience=0.0,
            age_days=0.0, question_id=c,
        )
        for c in order
    ]
    assert sorted(keys) == keys
    assert _HARVEST_CLASS_PRIORITY["coverage_floor"] == 0
    # Every known class still outranks an unseeded one.
    assert max(_HARVEST_CLASS_PRIORITY.values()) < _UNKNOWN_HARVEST_CLASS_PRIORITY


def test_stale_backlog_row_with_live_reach_sinks_below_a_fresh_coverage_floor_dispatch():
    """THE FINDING, reproduced directly: a month-old ``unit_payload`` row that
    traces forward to a live product (``live_reach=1``) must NOT outrank a
    same-week, reachless (``live_reach=0``) ``coverage_floor`` dispatch — the
    exact shape of the 2026-09-06 03:37Z live run (an Aug-02 unit_payload
    ``Q1`` beating a 09-04 IL/Palestine coverage_floor gap)."""
    stale_unit_payload = open_question_priority_key(
        live_reach=1, harvest_class="unit_payload", desk_salience=0.0,
        age_days=35.0, question_id="aug_backlog_row",
    )
    fresh_coverage_floor = open_question_priority_key(
        live_reach=0, harvest_class="coverage_floor", desk_salience=8.6589,
        age_days=2.0, question_id="il_palestine_gap",
    )
    assert sorted([stale_unit_payload, fresh_coverage_floor])[0] == fresh_coverage_floor


def test_non_stale_live_reach_still_beats_a_coverage_floor_dispatch():
    """Decay only strips a STALE row's false tier-1 claim — a genuinely
    fresh, actively-consumed backlog row keeps tier-1 precedence over a
    coverage_floor dispatch, exactly as tier 1's docstring rules."""
    fresh_with_reach = open_question_priority_key(
        live_reach=1, harvest_class="below_floor", desk_salience=0.0,
        age_days=1.0, question_id="fresh_reacher",
    )
    fresh_coverage_floor = open_question_priority_key(
        live_reach=0, harvest_class="coverage_floor", desk_salience=0.0,
        age_days=1.0, question_id="il_palestine_gap",
    )
    assert sorted([fresh_with_reach, fresh_coverage_floor])[0] == fresh_with_reach


def test_dispatch_marker_reads_as_coverage_floor_through_the_shared_reader():
    """``origin`` must be ``harvest`` — the ONLY value for which
    ``harvest_class_of`` reads an explicit class off the marker. A third origin
    would silently label every dispatched question ``unknown`` and rank it
    LAST, the exact inverse of the ruling."""
    marker = rd.dispatch_marker(
        target_id="country_watch_il", geo=["IL"], entity_fold="palestine",
        gap_name="Palestine", source_id="s", alert_output_id=uuid4(),
        desk_id="internal_stability", bounded_question="Is it?", run_id=uuid4(),
    )
    assert harvest_class_of([marker]) == "coverage_floor"
    assert marker["question_source"] == "coverage_floor"


def test_reference_gap_ranks_second_behind_coverage_floor_and_ahead_of_harvest():
    """A-4's class in the SAME table, at ordinal 1.

    The ordinals are RELATIVE — the tier-3 assertion is that the sequence is
    sorted, not that any integer is pinned — so the check that matters is the
    ORDER: coverage_floor, then reference_gap, then every harvested class.
    """
    order = [
        "coverage_floor", "reference_gap", "below_floor", "fact_contention",
        "freshness_advisory", "scorecard_disagreement", "unit_payload",
        "collection_gap",
    ]
    keys = [
        open_question_priority_key(
            live_reach=0, harvest_class=c, desk_salience=0.0,
            age_days=0.0, question_id=c,
        )
        for c in order
    ]
    assert sorted(keys) == keys
    assert _HARVEST_CLASS_PRIORITY["reference_gap"] == 1
    # Inserting a class must not collide the LAST known class with the
    # unknown-class sentinel, which would make a future class tie with
    # collection_gap instead of ranking after it.
    assert max(_HARVEST_CLASS_PRIORITY.values()) < _UNKNOWN_HARVEST_CLASS_PRIORITY
    assert len(set(_HARVEST_CLASS_PRIORITY.values())) == len(
        _HARVEST_CLASS_PRIORITY
    )


def test_reference_gap_marker_reads_through_the_shared_reader():
    """A-4 stamps the SAME four-key vocabulary through the SAME builder.

    ``origin`` stays ``harvest`` — it is not a parameter, precisely because it
    is the only value for which ``harvest_class_of`` reads an explicit class
    off the marker.
    """
    marker = rd.dispatch_marker(
        target_id="country_watch_il", geo=["IL"], entity_fold="lebanon",
        gap_name="Border clash", source_id="country_watch_il|lebanon",
        alert_output_id=None, desk_id=None, bounded_question=None,
        run_id=uuid4(),
        harvest_class=rd.REFERENCE_GAP_HARVEST_CLASS,
        question_source="reference_gap",
        extra={"unit": "escalation", "evidence_kind": "unit_reference_label"},
    )
    assert marker["origin"] == rd.MARKER_ORIGIN == "harvest"
    assert harvest_class_of([marker]) == "reference_gap"
    assert marker["question_source"] == "reference_gap"
    assert marker["unit"] == "escalation"
    # The geo the researcher's ranking fix depends on.
    assert dispatch_scope_of([marker]) == ("country_watch_il", ("IL",))


def test_marker_extra_can_never_overwrite_the_shared_vocabulary():
    """A producer's own carry ADDS to the marker and may never redefine what
    the shared reader keys on — otherwise a typo in one producer silently
    unranks every row it writes."""
    marker = rd.dispatch_marker(
        target_id="t", geo=["IL"], entity_fold="f", gap_name="g",
        source_id="real", alert_output_id=None, desk_id=None,
        bounded_question=None, run_id=None,
        harvest_class=rd.REFERENCE_GAP_HARVEST_CLASS,
        extra={
            "marker": "hijacked", "origin": "not_harvest",
            "harvest_class": "collection_gap", "source_id": "spoofed",
            "unit": "escalation",
        },
    )
    assert marker["marker"] == rd.MARKER_KEY
    assert marker["origin"] == "harvest"
    assert marker["harvest_class"] == "reference_gap"
    assert marker["source_id"] == "real"
    assert marker["unit"] == "escalation"
    assert harvest_class_of([marker]) == "reference_gap"


def test_reference_gap_is_not_a_meta_question_class():
    """Same ruling as ``coverage_floor``, for the same reason and more so: the
    premise of a reference gap is literally that the world reported something
    our collection does not carry, so a new signal naming it is exactly the
    evidence that bears on it."""
    assert rd.REFERENCE_GAP_HARVEST_CLASS not in cw.META_QUESTION_CLASSES


def test_coverage_floor_is_not_a_meta_question_class():
    """§7 R-B's explicit check. A coverage-gap question is about the WORLD —
    "does Palestine bear on this target" — and a wire story naming that polity
    is exactly the evidence that bears on it. Excluding it would silence the
    matcher on the one class whose premise is that the world says something our
    register does not."""
    assert rd.HARVEST_CLASS not in cw.META_QUESTION_CLASSES


# ---------------------------------------------------------------------------
# 3. PURE builders — the source_id, the marker, the thesis
# ---------------------------------------------------------------------------


def test_source_id_is_stable_within_a_day_and_new_across_days():
    a = rd.dispatch_source_id("country_watch_il", "palestine", _NOW)
    b = rd.dispatch_source_id(
        "country_watch_il", "palestine", _NOW + timedelta(hours=6)
    )
    c = rd.dispatch_source_id(
        "country_watch_il", "palestine", _NOW + timedelta(days=9)
    )
    assert a == b, "a re-scan the same day must not mint a second key"
    assert a != c, "a resolved-then-re-breached gap earns a new key"
    assert a == "country_watch_il|palestine|2026-09-05"


def test_source_id_normalises_a_naive_timestamp_to_utc():
    naive = datetime(2026, 9, 5, 9, 4, 1)
    assert rd.dispatch_source_id("t", "f", naive).endswith("2026-09-05")


def test_marker_carries_the_whole_dispatch_contract():
    alert_id, run_id = uuid4(), uuid4()
    marker = rd.dispatch_marker(
        target_id="country_watch_il",
        geo=["IL", ""],  # blanks dropped, never carried as a phantom code
        entity_fold="palestine",
        gap_name="Palestine",
        source_id="country_watch_il|palestine|2026-09-05",
        alert_output_id=alert_id,
        desk_id="internal_stability",
        bounded_question="What is this country's near-term internal stability?",
        run_id=run_id,
    )
    assert marker["marker"] == rd.MARKER_KEY
    assert marker["origin"] == "harvest"
    assert marker["harvest_class"] == "coverage_floor"
    assert marker["question_source"] == "coverage_floor"
    assert marker["target_id"] == "country_watch_il"
    assert marker["geo"] == ["IL"]
    assert marker["entity_fold"] == "palestine"
    assert marker["gap_name"] == "Palestine"
    assert marker["dispatched_by"] == str(alert_id)
    assert marker["run_id"] == str(run_id)
    assert marker["desk_id"] == "internal_stability"
    assert "internal stability" in marker["bounded_question"]
    # JSON-safe end to end: it is written into a jsonb column.
    assert json.loads(json.dumps(marker)) == marker


def test_marker_omits_a_desk_it_could_not_resolve():
    """F-6's cheaper fallback: a target with no dimension-signed open frame, or
    whose hottest desk declares no bounded_question, carries the gap ALONE.
    Never a fabricated question."""
    marker = rd.dispatch_marker(
        target_id="t", geo=["XX"], entity_fold="f", gap_name="G",
        source_id="s", alert_output_id=None, desk_id=None,
        bounded_question=None, run_id=None,
    )
    assert "desk_id" not in marker and "bounded_question" not in marker
    assert marker["dispatched_by"] is None


def test_thesis_states_the_gap_the_measurement_and_the_desks_question():
    thesis = rd.build_thesis(
        target_id="country_watch_il",
        gap_name="Palestine",
        cluster={"n_signals": 235, "n_days": 15, "slice_share": 0.1998},
        open_frame_count=8,
        desk_id="internal_stability",
        bounded_question="What is this country's near-term internal stability?",
    )
    assert "Palestine" in thesis and "country_watch_il" in thesis
    assert "235 signals" in thesis and "over 15 days" in thesis
    assert "20.0% of its entity-bearing slice" in thesis
    assert "none of its 8 open frames name it" in thesis
    # The desk's question rides as explicitly labelled CONTEXT (F-6): the gap
    # is a COLLECTION gap, not that desk's gap.
    assert "hottest open desk (internal_stability)" in thesis
    # It says out loud why the corpus is the wrong instrument.
    assert "reaching OUTSIDE it" in thesis
    assert len(thesis) <= 4096


def test_thesis_without_a_desk_still_names_the_gap():
    thesis = rd.build_thesis(
        target_id="t", gap_name="Palestine", cluster={}, open_frame_count=0,
        desk_id=None, bounded_question=None,
    )
    assert "Palestine" in thesis and "no open frame names it" in thesis
    assert "hottest open desk" not in thesis


def test_payload_for_cluster_is_json_safe_and_bounds_its_lineage():
    payload = rd.dispatch_payload_for_cluster(
        target_id="country_watch_il",
        geo=["IL"],
        cluster={
            "name": "Palestine", "entity_fold": "palestine", "n_signals": 235,
            "n_days": 15, "mean_magnitude": 0.5194, "slice_share": 0.1998,
            "exemplar_signal_ids": [str(uuid4()) for _ in range(40)],
        },
        open_frame_count=8,
        rising_edge=_NOW,
        context={"desk_id": "internal_stability", "bounded_question": "Is it?"},
    )
    assert json.loads(json.dumps(payload)) == payload
    assert len(payload["exemplar_signal_ids"]) == rd._MAX_DERIVED_REFS
    assert payload["source_id"] == "country_watch_il|palestine|2026-09-05"


# ---------------------------------------------------------------------------
# 4. THE TARGET CARRY — dispatch_scope_of + the render
# ---------------------------------------------------------------------------


def test_dispatch_scope_of_reads_target_and_geo_off_the_marker():
    marker = rd.dispatch_marker(
        target_id="country_watch_il", geo=["IL", "PS"], entity_fold="palestine",
        gap_name="Palestine", source_id="s", alert_output_id=None,
        desk_id=None, bounded_question=None, run_id=None,
    )
    assert dispatch_scope_of([marker]) == ("country_watch_il", ("IL", "PS"))
    # asyncpg may hand back the jsonb as a string.
    assert dispatch_scope_of(json.dumps([marker])) == (
        "country_watch_il", ("IL", "PS"),
    )


@pytest.mark.parametrize(
    "raw",
    [
        None, "", "not json", [], {}, "[]", [{"marker": "something_else"}],
        [{"marker": "open_question_origin", "origin": "harvest",
          "harvest_class": "below_floor"}],
    ],
)
def test_dispatch_scope_of_is_empty_for_every_undispatched_shape(raw):
    assert dispatch_scope_of(raw) == (None, ())


def _gq(**over: Any) -> GroundingOpenQuestion:
    base: dict[str, Any] = dict(
        id=uuid4(), thesis="Does Palestine bear on country_watch_il?",
        harvest_class="coverage_floor", target_id=None,
        produced_at=_NOW, live_reach=0, desk_salience=0.0,
    )
    base.update(over)
    return GroundingOpenQuestion(**base)


def test_render_prints_the_scope_token_only_for_a_dispatched_question():
    scoped = _gq(target_id="country_watch_il", geo=("IL",)).render(
        tag="Q1", now=_NOW
    )
    assert "scope=country_watch_il geo=IL" in scoped

    # The byte-identity half: a question carrying no scope renders exactly as
    # it did before R-B existed — no empty token, no trailing separator.
    plain = _gq(harvest_class="below_floor").render(tag="Q1", now=_NOW)
    assert plain == "[Q1] (below_floor; opened today) " + _gq().thesis
    assert "scope=" not in plain
    # A target with no geo is NOT a scope: geo is the only reachability key.
    assert "scope=" not in _gq(target_id="country_watch_il").render(
        tag="Q1", now=_NOW
    )


def test_block_render_carries_the_scope_into_the_prompt():
    block = build_open_questions_block(
        [_gq(target_id="country_watch_il", geo=("IL",))], now=_NOW
    )
    assert block is not None and "scope=country_watch_il geo=IL" in block


# ---------------------------------------------------------------------------
# 5. THE DRAIN — the row contract asserted through grounding's REAL reader
# ---------------------------------------------------------------------------


class _StubConn:
    def __init__(self, rows: list[Mapping[str, Any]]) -> None:
        self._rows = rows

    async def fetch(self, *_a: Any, **_k: Any) -> list[Mapping[str, Any]]:
        return self._rows


class _StubAcquire:
    def __init__(self, conn: _StubConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _StubConn:
        return self._conn

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class _StubPool:
    def __init__(self, rows: list[Mapping[str, Any]]) -> None:
        self._conn = _StubConn(rows)

    def acquire(self) -> _StubAcquire:
        return _StubAcquire(self._conn)


def _backlog_row(marker: Mapping[str, Any], **over: Any) -> dict[str, Any]:
    """One row shaped exactly as ``_OPEN_QUESTIONS_BACKLOG_SQL`` projects it."""
    row: dict[str, Any] = {
        "id": uuid4(),
        "thesis": "Does Palestine bear on country_watch_il?",
        "target_id": marker.get("target_id"),
        "produced_at": _NOW,
        "diagnostic_evidence": json.dumps([marker]),
        "live_reach": 0,
        "desk_salience": 0.0,
    }
    row.update(over)
    return row


async def test_dispatched_row_drains_through_the_real_reader():
    """The written marker → ``resolve_open_questions`` → a question the block
    renders with its scope. Driven through the resolver, not a hand-built
    ``GroundingOpenQuestion``, because the drain is the contract."""
    marker = rd.dispatch_marker(
        target_id="country_watch_il", geo=["IL"], entity_fold="palestine",
        gap_name="Palestine", source_id="country_watch_il|palestine|2026-09-05",
        alert_output_id=uuid4(), desk_id="internal_stability",
        bounded_question="What is this country's near-term internal stability?",
        run_id=uuid4(),
    )
    other = {"marker": "open_question_origin", "origin": "unit_payload"}
    rows = [
        _backlog_row(other, thesis="a unit's own uncertainty"),
        _backlog_row(marker),
    ]
    resolver = SubstrateGroundingResolver(pg_pool=_StubPool(rows))
    out = await resolver.resolve_open_questions(limit=8)

    assert [q.harvest_class for q in out] == ["coverage_floor", "unit_payload"]
    dispatched = out[0]
    assert dispatched.target_id == "country_watch_il"
    assert dispatched.geo == ("IL",)
    assert "scope=country_watch_il geo=IL" in dispatched.render(tag="Q1", now=_NOW)


async def test_dispatched_question_outranks_the_whole_undispatched_backlog():
    """The one class ordering claim that matters operationally: against a
    backlog made only of the classes that are live today, a coverage-floor
    question is offered."""
    marker = rd.dispatch_marker(
        target_id="country_watch_il", geo=["IL"], entity_fold="palestine",
        gap_name="Palestine", source_id="s", alert_output_id=None,
        desk_id=None, bounded_question=None, run_id=None,
    )
    rows = [
        _backlog_row(
            {"marker": "open_question_origin", "origin": "unit_payload"},
            thesis=f"unit q{i}",
            produced_at=_NOW - timedelta(days=40),
            desk_salience=9.9,
        )
        for i in range(20)
    ]
    rows.append(_backlog_row(marker, thesis="the coverage gap"))
    resolver = SubstrateGroundingResolver(pg_pool=_StubPool(rows))
    out = await resolver.resolve_open_questions(limit=8)
    assert out[0].thesis == "the coverage gap"


async def test_deps_builder_sink_carries_the_scope_for_the_run():
    """The carrier's last hop: the tag → question sink the run resolves
    against gains ``target_id`` + ``geo``, so the scope is available to the run
    and not only to the model's eyes."""
    from legba.data.analysts.inline_target import GROUNDING_QUESTION_SINK_KEY
    from legba.data.schemas.analyst import AnalystDescriptor
    from legba.runtime.analyst_deps_builder import _build_grounding_hook

    marker = rd.dispatch_marker(
        target_id="country_watch_il", geo=["IL"], entity_fold="palestine",
        gap_name="Palestine", source_id="s", alert_output_id=None,
        desk_id=None, bounded_question=None, run_id=None,
    )
    descriptor = AnalystDescriptor.model_validate(
        {
            "identity": {
                "id": "corpus_researcher", "name": "R", "kind": "inline_target",
                "schema_uri": "legba/analyst/1.0.0", "version": "0" * 16,
                "type_signature": {
                    "input_type": "legba.runtime.SignalList",
                    "output_type": "legba.runtime.Finding",
                },
                "state": "active", "owner": "t",
            },
            "subscription": {
                "substrate": {"direct_queries": True, "gather_only": False}
            },
            "method": {
                "kind": "llm_planner",
                "prompt_module": "legba.runtime.analyst_method:_DEFAULT_SYSTEM",
                "llm": {
                    "primary": {
                        "factory_kind": "stack_ref", "raw": "llm.x",
                        "expected_family": "llm_provider",
                    }
                },
            },
            "cadence": {"fallback_schedule": "37 3,15 * * *"},
            "grounding": {
                "enabled": True, "sources": ["open_questions"], "max_facts": 8
            },
        },
        strict=False,
    )
    hook = _build_grounding_hook(
        descriptor, pg_pool=_StubPool([_backlog_row(marker)])
    )
    assert hook is not None
    sink: dict[str, Any] = {}
    out = await hook([], {"target_id": None, GROUNDING_QUESTION_SINK_KEY: sink})
    assert out is not None and "scope=country_watch_il geo=IL" in out
    assert sink["Q1"]["target_id"] == "country_watch_il"
    assert sink["Q1"]["geo"] == ["IL"]
    assert sink["Q1"]["harvest_class"] == "coverage_floor"


# ---------------------------------------------------------------------------
# 6. THE WRITE — DB-backed, through the real provenance writer
# ---------------------------------------------------------------------------


_TARGET = "country_watch_rbd"


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


async def _sweep(conn: Any) -> None:
    await conn.execute("DELETE FROM signals WHERE source_id = $1", _SOURCE)
    await conn.execute(
        "DELETE FROM situations WHERE target_id LIKE $1", f"{_TARGET}%"
    )
    await conn.execute(
        "DELETE FROM target_descriptors WHERE descriptor_id LIKE $1", f"{_TARGET}%"
    )
    await conn.execute(
        "DELETE FROM analyst_descriptors WHERE owner = 'test_rb_dispatch'"
    )


@pytest_asyncio.fixture
async def clean_slate(pg_pool):
    """Fresh backlog and watermarks before, and this file's own evidence swept
    AFTER as well as before.

    The teardown half is not hygiene: every desk in this file and in
    ``test_coverage_floor_scan.py`` scopes on ``geo && ['IL']``, so signals left
    behind here land inside a sibling file's window slice, its brand-new desk
    breaches on scan 1, the 0091 seed contract adopts it silently, and the test
    that meant to watch a breach fire watches nothing happen. That is a real
    property of the scan (a desk sees every signal its geo matches, whoever
    inserted it), so the fixture removes the rows rather than the property.
    """
    async with pg_pool.acquire() as conn:
        # The whole open-question backlog, not just this file's rows: the
        # resolver assertions below read the top 8 of the REAL ranking, so a
        # sibling file's leftover below_floor row would crowd this file's
        # dispatched question out and read as a dispatch failure. The same
        # sweep, for the same reason, that test_corpus_research_backlog.py and
        # test_claim_watch.py already do.
        await conn.execute(
            "DELETE FROM hypotheses WHERE status = 'open_question'"
        )
        await conn.execute(
            "DELETE FROM hypotheses WHERE analyst_id = $1", rd.DISPATCH_ANALYST_ID
        )
        await conn.execute(
            "DELETE FROM hypotheses WHERE analyst_id = $1", rgd.DISPATCH_ANALYST_ID
        )
        await conn.execute("TRUNCATE alert_trigger_watermarks")
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE analyst_id = 'alert_trigger_scan'"
        )
        await _sweep(conn)
    yield
    async with pg_pool.acquire() as conn:
        await _sweep(conn)


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv(rr.RESEARCH_EVIDENCE_ENV, rr.REGIME_SUBSTRATE)


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.delenv(rr.RESEARCH_EVIDENCE_ENV, raising=False)


def _dispatch(**over: Any) -> dict[str, Any]:
    entry = rd.dispatch_payload_for_cluster(
        target_id=_TARGET,
        geo=["IL"],
        cluster={
            "name": "Palestine", "entity_fold": "palestine", "n_signals": 235,
            "n_days": 15, "mean_magnitude": 0.52, "slice_share": 0.1998,
            "exemplar_signal_ids": [],
        },
        open_frame_count=8,
        rising_edge=_NOW,
        context={
            "desk_id": "internal_stability",
            "bounded_question": "What is this country's near-term stability?",
        },
    )
    entry.update(over)
    return entry


async def _questions(conn: Any) -> list[Any]:
    return await conn.fetch(
        "SELECT id, thesis, target_id, status, diagnostic_evidence, derived_from "
        "FROM hypotheses WHERE analyst_id = $1 ORDER BY produced_at, id",
        rd.DISPATCH_ANALYST_ID,
    )


async def test_dispatch_writes_one_open_question_carrying_the_contract(
    pg_pool, clean_slate, flag_on
):
    alert_id = uuid4()
    async with pg_pool.acquire() as conn:
        n = await rd.dispatch_open_questions(
            conn, [_dispatch()], alert_output_id=alert_id, run_id=uuid4()
        )
        rows = await _questions(conn)
    assert n == 1 and len(rows) == 1
    row = rows[0]
    assert row["status"] == "open_question"
    assert row["target_id"] == _TARGET      # the column the backlog SQL joins on
    assert "Palestine" in row["thesis"]
    marker = json.loads(row["diagnostic_evidence"])[0]
    assert marker["harvest_class"] == "coverage_floor"
    assert marker["geo"] == ["IL"]          # the reachability key, carried
    assert marker["dispatched_by"] == str(alert_id)
    assert marker["question_source"] == "coverage_floor"
    assert marker["desk_id"] == "internal_stability"


async def test_dispatched_row_is_offered_by_the_live_backlog_sql(
    pg_pool, clean_slate, flag_on
):
    """End of the carrier chain against the REAL schema: the row the dispatch
    wrote is found by the actual recursive-CTE backlog query, classed
    ``coverage_floor``, and renders its scope."""
    async with pg_pool.acquire() as conn:
        await rd.dispatch_open_questions(
            conn, [_dispatch()], alert_output_id=uuid4(), run_id=uuid4()
        )
    resolver = SubstrateGroundingResolver(pg_pool=pg_pool)
    out = await resolver.resolve_open_questions(limit=8)
    mine = [q for q in out if q.target_id == _TARGET]
    assert len(mine) == 1
    assert mine[0].harvest_class == "coverage_floor"
    assert mine[0].geo == ("IL",)
    assert "scope=" in mine[0].render(tag="Q1")


async def test_dispatched_row_ranks_q1_ahead_of_a_stale_live_reaching_backlog_row(
    pg_pool, clean_slate, flag_on,
):
    """THE FINDING, reproduced against the REAL schema: the 2026-09-06 03:37Z
    live run's ``addressed_question`` was a MONTH-OLD ``unit_payload`` row
    that traced forward to a live product — NOT the same-week dispatched
    IL/Palestine ``coverage_floor`` gap it should have investigated. Seed
    exactly that competition (a stale row with genuine ``live_reach`` plus a
    fresh dispatch) through the real recursive-CTE backlog query and confirm
    the dispatch now wins Q1 — the geo it carries is what the researcher's
    ``web_evidence`` call must quote back per §1.3/F-8."""
    async with pg_pool.acquire() as conn:
        # The stale competitor: a 35-day-old unit_payload row a LIVE
        # (non-superseded) finding still consumes — before this fix, tier 1
        # (live_reach > 0) alone put this ahead of ANY fresh dispatch.
        stale_row = await conn.fetchrow(
            "INSERT INTO hypotheses (thesis, status, target_id, produced_at, "
            "diagnostic_evidence) VALUES ($1, 'open_question', NULL, "
            "now() - interval '35 days', $2::jsonb) RETURNING id",
            "an August backlog question",
            json.dumps([{"marker": "open_question_origin", "origin": "unit_payload",
                         "finding_id": str(uuid4())}]),
        )
        ctx = AnalystContext(analyst_id="test_consumer", analyst_version="v1", run_id=uuid4())
        consumer, _dlq = await write_finding(
            conn, analyst_ctx=ctx,
            payload=FindingPayload(title="consumer", body="b", confidence=0.5),
            derived_from=[],
        )
        assert consumer is not None
        await conn.execute(
            "INSERT INTO output_consumption (consumer_id, consumed_id, consumer_kind, context) "
            "VALUES ($1, $2, 'test_consumer', 'composition_basis')",
            consumer.id, stale_row["id"],
        )

        # The fresh dispatch — same-week live gap, live_reach=0 by construction.
        await rd.dispatch_open_questions(
            conn, [_dispatch()], alert_output_id=uuid4(), run_id=uuid4()
        )

    resolver = SubstrateGroundingResolver(pg_pool=pg_pool)
    out = await resolver.resolve_open_questions(limit=8)
    assert out, "expected both the stale row and the dispatch to be offered"
    q1 = out[0]
    assert q1.harvest_class == "coverage_floor"
    assert q1.target_id == _TARGET
    assert q1.geo == ("IL",)                       # the carry, on the WINNING row
    assert "scope=country_watch_rbd geo=IL" in q1.render(tag="Q1")


async def test_dispatch_is_idempotent_across_re_detection(
    pg_pool, clean_slate, flag_on
):
    """A persisting breach re-offered to the writer mints NOTHING; the same gap
    on a later day (resolved, then re-breached) mints a new question."""
    async with pg_pool.acquire() as conn:
        first = await rd.dispatch_open_questions(
            conn, [_dispatch()], alert_output_id=uuid4()
        )
        again = await rd.dispatch_open_questions(
            conn, [_dispatch()], alert_output_id=uuid4()
        )
        # Same UTC day, a different alert row: still ONE question.
        same_day = await rd.dispatch_open_questions(
            conn,
            [_dispatch(source_id=rd.dispatch_source_id(
                _TARGET, "palestine", _NOW + timedelta(hours=7)))],
            alert_output_id=uuid4(),
        )
        later = await rd.dispatch_open_questions(
            conn,
            [_dispatch(source_id=rd.dispatch_source_id(
                _TARGET, "palestine", _NOW + timedelta(days=9)))],
            alert_output_id=uuid4(),
        )
        rows = await _questions(conn)
    assert (first, again, same_day, later) == (1, 0, 0, 1)
    assert len(rows) == 2


async def test_dispatch_writes_nothing_with_the_flag_off(
    pg_pool, clean_slate, flag_off
):
    async with pg_pool.acquire() as conn:
        n = await rd.dispatch_open_questions(
            conn, [_dispatch()], alert_output_id=uuid4()
        )
        assert await _questions(conn) == []
    assert n == 0


async def test_dispatch_carries_exemplar_lineage_but_not_the_alert_row(
    pg_pool, clean_slate, flag_on
):
    """``derived_from`` is the cluster's exemplar SIGNALS. The dispatching
    alert is named on the marker instead: mixing a product id into a question's
    signal lineage would make the forward walk read a detector's own alert as
    evidence for the question it dispatched."""
    exemplars = [uuid4() for _ in range(3)]
    alert_id = uuid4()
    async with pg_pool.acquire() as conn:
        await rd.dispatch_open_questions(
            conn,
            [_dispatch(exemplar_signal_ids=[str(e) for e in exemplars])],
            alert_output_id=alert_id,
        )
        rows = await _questions(conn)
    derived = list(rows[0]["derived_from"])
    assert sorted(str(d) for d in derived) == sorted(str(e) for e in exemplars)
    assert str(alert_id) not in [str(d) for d in derived]


async def test_dispatch_degrades_and_never_raises(pg_pool, clean_slate, flag_on):
    """DEGRADE-NOT-BREAK: the alert row is already durable and a research
    side-write may not cost the operator a detector."""

    class _Exploding:
        async def fetchval(self, *_a: Any, **_k: Any) -> Any:
            raise RuntimeError("substrate down")

    assert await rd.dispatch_open_questions(
        _Exploding(), [_dispatch()], alert_output_id=uuid4()
    ) == 0

    # A malformed entry is isolated: its siblings still land.
    async with pg_pool.acquire() as conn:
        n = await rd.dispatch_open_questions(
            conn,
            [{"target_id": "", "source_id": ""}, "not a mapping", _dispatch()],
            alert_output_id=uuid4(),
        )
    assert n == 1


def _gap(**over: Any) -> dict[str, Any]:
    """One A-4 candidate, shaped as ``_reference_diff.reference_gap_candidates``
    emits it."""
    candidate = {
        "reference_id": str(uuid4()),
        "target_id": _TARGET,
        "unit": "escalation",
        "geo": ["IL"],
        "ordinal": 1,
        "headline": "Border clash reported",
        "sentence": "Lebanon reported a border clash overnight.",
        "entity_folds": ["lebanon"],
        "urls": ["https://a.example/1"],
        "materiality": "high",
        "n_slice_rows": 120,
        "arms_failed": ["c1_url", "c2_entity", "c3_prose"],
    }
    candidate.update(over)
    return rgd.select_candidates([candidate])[0]


async def _gap_questions(conn: Any) -> list[Any]:
    return await conn.fetch(
        "SELECT id, thesis, target_id, status, diagnostic_evidence, derived_from "
        "FROM hypotheses WHERE analyst_id = $1 ORDER BY produced_at, id",
        rgd.DISPATCH_ANALYST_ID,
    )


async def test_reference_gap_dispatch_writes_through_the_shared_write_path(
    pg_pool, clean_slate, flag_on
):
    """A-4 leg 2 through the SAME writer, probe and marker builder as R-B —
    only the class, the question source and the analyst id differ."""
    async with pg_pool.acquire() as conn:
        n = await rd.dispatch_open_questions(
            conn,
            [rgd.build_dispatch_entry(_gap())],
            alert_output_id=None,
            run_id=uuid4(),
            harvest_class=rgd.HARVEST_CLASS,
            question_source=rgd.QUESTION_SOURCE,
            analyst_id=rgd.DISPATCH_ANALYST_ID,
        )
        rows = await _gap_questions(conn)
    assert n == 1 and len(rows) == 1
    row = rows[0]
    assert row["status"] == "open_question"
    assert row["target_id"] == _TARGET
    assert "REFERENCE GAP" in row["thesis"]
    assert "Border clash reported" in row["thesis"]
    marker = json.loads(row["diagnostic_evidence"])[0]
    assert marker["origin"] == "harvest"
    assert marker["harvest_class"] == "reference_gap"
    assert marker["question_source"] == "reference_gap"
    assert marker["geo"] == ["IL"]
    assert marker["unit"] == "escalation"
    assert marker["evidence_kind"] == "unit_reference_label"
    # No signal lineage to claim: the item is UNCOLLECTED by construction.
    assert list(row["derived_from"]) == []


async def test_reference_gap_row_drains_and_classes_through_the_live_backlog(
    pg_pool, clean_slate, flag_on
):
    """The end of the carrier chain against the REAL schema: the row A-4 wrote
    is found by the actual recursive-CTE backlog query, classed
    ``reference_gap``, and renders the geo the researcher's ``web_evidence``
    call has to quote back."""
    async with pg_pool.acquire() as conn:
        await rd.dispatch_open_questions(
            conn, [rgd.build_dispatch_entry(_gap())], alert_output_id=None,
            harvest_class=rgd.HARVEST_CLASS,
            question_source=rgd.QUESTION_SOURCE,
            analyst_id=rgd.DISPATCH_ANALYST_ID,
        )
    resolver = SubstrateGroundingResolver(pg_pool=pg_pool)
    out = await resolver.resolve_open_questions(limit=8)
    mine = [q for q in out if q.target_id == _TARGET]
    assert len(mine) == 1
    assert mine[0].harvest_class == "reference_gap"
    assert mine[0].geo == ("IL",)
    assert "scope=" in mine[0].render(tag="Q1")


async def test_a_coverage_floor_gap_still_outranks_a_reference_gap(
    pg_pool, clean_slate, flag_on
):
    """Both classes in the backlog at once, both fresh: the ordinal decides,
    and it says coverage_floor first. A-4 must not displace R-B."""
    async with pg_pool.acquire() as conn:
        await rd.dispatch_open_questions(
            conn, [rgd.build_dispatch_entry(_gap())], alert_output_id=None,
            harvest_class=rgd.HARVEST_CLASS,
            question_source=rgd.QUESTION_SOURCE,
            analyst_id=rgd.DISPATCH_ANALYST_ID,
        )
        await rd.dispatch_open_questions(
            conn, [_dispatch()], alert_output_id=uuid4()
        )
    resolver = SubstrateGroundingResolver(pg_pool=pg_pool)
    classes = [
        q.harvest_class
        for q in await resolver.resolve_open_questions(limit=8)
        if q.harvest_class in ("coverage_floor", "reference_gap")
    ]
    assert classes[:2] == ["coverage_floor", "reference_gap"]


async def test_reference_gap_dispatch_is_idempotent_while_the_question_is_open(
    pg_pool, clean_slate, flag_on
):
    """THE RISING EDGE. The same gap re-detected tomorrow — a different
    reference row, a different day — mints NOTHING while the question is open,
    because the probe keys on (target, item_fold) and is scoped to
    ``status='open_question'``. A DIFFERENT fold on the same desk is a
    different need and does mint."""
    async with pg_pool.acquire() as conn:
        first = await rd.dispatch_open_questions(
            conn, [rgd.build_dispatch_entry(_gap())], alert_output_id=None,
            harvest_class=rgd.HARVEST_CLASS, analyst_id=rgd.DISPATCH_ANALYST_ID,
        )
        # Tomorrow: a NEW reference row names the same development again.
        stats: dict[str, int] = {}
        again = await rd.dispatch_open_questions(
            conn,
            [rgd.build_dispatch_entry(_gap(reference_id=str(uuid4())))],
            alert_output_id=None,
            harvest_class=rgd.HARVEST_CLASS, analyst_id=rgd.DISPATCH_ANALYST_ID,
            stats=stats,
        )
        # A different polity on the same desk is a different question.
        other = await rd.dispatch_open_questions(
            conn,
            [rgd.build_dispatch_entry(
                _gap(entity_folds=["syria"], headline="Syria border move")
            )],
            alert_output_id=None,
            harvest_class=rgd.HARVEST_CLASS, analyst_id=rgd.DISPATCH_ANALYST_ID,
        )
        rows = await _gap_questions(conn)
    assert (first, again, other) == (1, 0, 1)
    assert stats["existing"] == 1
    assert len(rows) == 2


async def test_reference_gap_and_coverage_floor_keys_never_collide(
    pg_pool, clean_slate, flag_on
):
    """The two producers share the probe, so they must NOT share a key space:
    the class is part of the containment probe, so the same ``source_id``
    under two classes is two questions, not one silently suppressed."""
    shared = f"{_TARGET}|palestine"
    async with pg_pool.acquire() as conn:
        a = await rd.dispatch_open_questions(
            conn, [_dispatch(source_id=shared)], alert_output_id=uuid4()
        )
        b = await rd.dispatch_open_questions(
            conn,
            [{**rgd.build_dispatch_entry(_gap()), "source_id": shared}],
            alert_output_id=None,
            harvest_class=rgd.HARVEST_CLASS, analyst_id=rgd.DISPATCH_ANALYST_ID,
        )
    assert (a, b) == (1, 1)


async def test_dispatch_is_capped_per_scan(pg_pool, clean_slate, flag_on):
    entries = [
        _dispatch(source_id=f"{_TARGET}|fold{i}|2026-09-05", entity_fold=f"fold{i}")
        for i in range(rd.MAX_DISPATCH_PER_SCAN + 5)
    ]
    async with pg_pool.acquire() as conn:
        n = await rd.dispatch_open_questions(
            conn, entries, alert_output_id=uuid4()
        )
    assert n == rd.MAX_DISPATCH_PER_SCAN


# ---------------------------------------------------------------------------
# 7. THE ROLLUP CARRY — a capped candidate must still dispatch
# ---------------------------------------------------------------------------


def test_apply_desk_cap_carries_research_dispatch_onto_the_rollup():
    """A suppressed candidate's watermark advances when the rollup lands, so a
    dropped dispatch would advance past the gap's only rising edge having
    dispatched nothing — a silent, permanent loss."""
    def _cand(cls: str, sev: str, dispatch: list[dict[str, Any]]) -> Any:
        return ats.AlertCandidate(
            trigger_class=cls, severity=sev, title=f"{cls} t", body="b",
            target_id="d1", watermarks=[(cls, f"{cls}|k", {"breached": True})],
            research_dispatch=dispatch,
        )

    payload = [_dispatch()]
    kept, rollups = ats.apply_desk_cap(
        [
            _cand("band_crossing", "high", []),
            _cand(ats.TRIGGER_COVERAGE_FLOOR, "low", payload),
        ],
        cap=1,
    )
    assert [c.trigger_class for c in kept] == ["band_crossing"]
    assert len(rollups) == 1
    assert rollups[0].research_dispatch == payload
    # And the watermark it rides with, so the two stay together.
    assert rollups[0].watermarks


def test_every_other_candidate_carries_an_empty_dispatch_by_default():
    cand = ats.AlertCandidate(
        trigger_class="band_crossing", severity="high", title="t", body="b",
        target_id=None,
    )
    assert cand.research_dispatch == []


# ---------------------------------------------------------------------------
# 8. END TO END — the REAL binding path (deterministic.run_method)
# ---------------------------------------------------------------------------


_SOURCE = "test_rb_dispatch_source"
_DESK_ANALYST = "internal_stability"
_BOUNDED_Q = (
    "What is this country's near-term internal political stability, viewed "
    "through a coup-vulnerability lens, and where is it going?"
)


class _FakeDispatcher:
    def __init__(self) -> None:
        self.payloads: list[Any] = []

    async def fan_out(self, payload: Any) -> list[Any]:
        self.payloads.append(payload)
        return []


class _Deps:
    def __init__(self, pool: Any) -> None:
        self.pg_pool = pool
        self.extras = {"alert_sink_dispatcher": _FakeDispatcher()}


async def _run(pool: Any, **opts: Any) -> AnalystMethodResult:
    result = await deterministic.run_method(
        [],
        {
            "sub_handler": "alert_trigger_scan",
            "analyst_id": "alert_trigger_scan",
            "run_id": str(uuid4()),
            "coverage_floor_min_scan_interval_hours": 0.0,
            "daily_page_budget": 10_000,
            **opts,
        },
        _Deps(pool),
    )
    assert isinstance(result, AnalystMethodResult)
    return result


async def _insert_desk(conn: Any, desk: str) -> None:
    await conn.execute(
        "INSERT INTO target_descriptors "
        "  (descriptor_id, version, schema_uri, is_head, state, owner, name, body) "
        "VALUES ($1, 'v1', 'legba/target/2.0.0', TRUE, 'active', "
        "        'test_rb_dispatch', $1, $2::jsonb) ON CONFLICT DO NOTHING",
        desk,
        json.dumps({"scope": {"geo": ["IL"], "tags": ["watch"]}}),
    )


async def _seed_breaching_evidence(conn: Any, desk: str) -> None:
    """The IL shape: a foreign polity in every entity-bearing signal of the
    desk's own window, high-salience, on twelve days — against a register whose
    only frames are about something else."""
    payload = {
        "title": "seeded",
        "entities": [
            {"text": e, "class": "country", "confidence": 1.0}
            for e in ("Iran", "Iranian")
        ],
    }
    salience = {"magnitude": 0.8, "authority": "reporting",
                "event_class": "kinetic_strike"}
    for day in range(12):
        for _ in range(3):
            sid = uuid4()
            await conn.execute(
                "INSERT INTO signals "
                "  (id, source_id, payload, salience, geo, fetched_at, content_hash) "
                "VALUES ($1, $6, $2::jsonb, $3::jsonb, ARRAY['IL']::text[], "
                "        now() - make_interval(days => $4, hours => 1), $5)",
                sid, json.dumps(payload), json.dumps(salience), day,
                sid.hex, _SOURCE,
            )
    # The register: dimension-SIGNED frames, none of which names Iran. The
    # signature is what F-6's hottest-desk selector reads — situations
    # .analyst_id names the PRODUCER, never the desk.
    for name, dim, intensity in (
        ("Coalition splinters as right-wing bloc forms", _DESK_ANALYST, 8.65),
        ("Third Dolphin-class submarine delivered", "military_posture", 1.04),
    ):
        await conn.execute(
            "INSERT INTO situations "
            "  (id, data, name, status, category, target_id, intensity_score, "
            "   event_count, valid_from, situation_signature, analyst_id) "
            "VALUES ($1, '{}'::jsonb, $2, 'dormant', 'country', $3, $4, 5, now(), "
            "        $5, 'situation_clustering')",
            uuid4(), name, desk, intensity, f"sig:{desk}#dim:{dim}",
        )
    # The desk descriptor carrying the bounded question F-6 quotes — read from
    # the REGISTRY, because descriptors/ is in neither container image.
    await conn.execute(
        "INSERT INTO analyst_descriptors "
        "  (descriptor_id, version, schema_uri, kind, is_head, state, owner, "
        "   name, body) "
        "VALUES ($1, 'v1', 'legba/analyst/1.0.0', 'inline_target', TRUE, "
        "        'active', 'test_rb_dispatch', $1, $2::jsonb) "
        "ON CONFLICT DO NOTHING",
        _DESK_ANALYST,
        json.dumps({"method": {"bounded_question": _BOUNDED_Q}}),
    )


async def test_coverage_floor_alert_dispatches_exactly_one_open_question(
    pg_pool, clean_slate, flag_on
):
    desk = f"{_TARGET}_{uuid4().hex[:6]}"
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, desk)

    await _run(pg_pool)                       # scan 1 — the 0091 seed contract
    async with pg_pool.acquire() as conn:
        assert await _questions(conn) == []   # nothing to dispatch yet
        await _seed_breaching_evidence(conn, desk)

    r2 = await _run(pg_pool)                  # scan 2 — the rising edge
    async with pg_pool.acquire() as conn:
        rows = await _questions(conn)
        alert = await conn.fetchval(
            "SELECT id FROM analyst_outputs WHERE kind = 'alert' "
            "AND target_id = $1 ORDER BY produced_at DESC LIMIT 1",
            desk,
        )
    assert len(rows) == 1
    marker = json.loads(rows[0]["diagnostic_evidence"])[0]
    assert marker["gap_name"] == "Iran"
    assert marker["geo"] == ["IL"]
    assert marker["target_id"] == desk
    assert marker["dispatched_by"] == str(alert)
    # F-6: the HOTTEST desk's bounded question, chosen off the 0188 signature.
    assert marker["desk_id"] == _DESK_ANALYST
    assert marker["bounded_question"] == _BOUNDED_Q
    assert _BOUNDED_Q in rows[0]["thesis"]
    assert r2.finding.data["research_dispatched"] == 1
    assert r2.finding.data["research_dispatch_failures"] == 0

    # Scan 3 — the SAME standing breach. No new alert, and no second question.
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        assert len(await _questions(conn)) == 1


async def test_flag_off_scan_dispatches_nothing_and_omits_the_receipt_keys(
    pg_pool, clean_slate, flag_off
):
    """The G2 half this file owns. ``test_coverage_floor_scan.py`` passing
    unchanged is the primary byte-identity proof; this states positively that a
    flag-off scan writes no question and its receipt carries no dispatch keys —
    so a pooling reader cannot mistake regime 0 for a dispatch of size zero."""
    desk = f"{_TARGET}_{uuid4().hex[:6]}"
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, desk)
    await _run(pg_pool)
    async with pg_pool.acquire() as conn:
        await _seed_breaching_evidence(conn, desk)
    r2 = await _run(pg_pool)

    async with pg_pool.acquire() as conn:
        assert await _questions(conn) == []
        fired = await conn.fetchval(
            "SELECT count(*) FROM analyst_outputs WHERE kind = 'alert' "
            "AND target_id = $1",
            desk,
        )
    assert fired == 1, "the detector itself is untouched by the flag"
    assert "research_dispatched" not in r2.finding.data
    assert "research_dispatch_failures" not in r2.finding.data
    assert "research_dispatched" not in r2.finding.body


# ---------------------------------------------------------------------------
# 9. THE DESCRIPTOR — validated through the registry's own PUT-body call
# ---------------------------------------------------------------------------


def _corpus_researcher_body() -> dict[str, Any]:
    return yaml.safe_load(
        (DESCRIPTORS / "analyst_corpus_researcher.yaml").read_text("utf-8")
    )


def _corpus_researcher() -> Any:
    """``model_validate(..., strict=False)`` — the call the registry makes on a
    PUT body, so a descriptor that passes here is one the registry accepts."""
    return Family.ANALYST.model.model_validate(
        _corpus_researcher_body(), strict=False
    )


def test_corpus_researcher_grants_the_research_pack_beside_web_access():
    packs = [p.pack_id for p in _corpus_researcher().action_packs]
    assert packs == ["substrate_read", "web_access", "research"]


def test_the_research_grant_is_declared_in_the_tree_not_a_model_default():
    body = _corpus_researcher_body()
    assert {"pack_id": "research"} in body["action_packs"]


def test_prompt_makes_the_web_leg_mandatory_at_a_null_result():
    prompt = _corpus_researcher().method.system_prompt
    assert "web_evidence" in prompt
    assert "THE WEB LEG IS NOT OPTIONAL AT A NULL RESULT" in prompt
    assert "MUST call web_evidence(query) BEFORE you write a null result" in prompt
    # The null result stays a legitimate finding — the rule raises the bar, it
    # does not remove the honest outcome.
    assert "complete, legitimate" in prompt


def test_prompt_states_f8_as_a_rule_not_a_hope():
    """F-8: a run that does not carry the question's scope reaches NO desk. The
    prompt has to say that as a consequence the model can act on, not as an
    aspiration."""
    prompt = _corpus_researcher().method.system_prompt
    assert "CARRY THE QUESTION'S SCOPE" in prompt
    assert "THIS IS A RULE, NOT A PREFERENCE" in prompt
    assert "scope=<target_id> geo=<CODES>" in prompt
    assert "NO desk will ever see it" in prompt
    assert "reaches no desk" in prompt
    # And it forbids the failure mode that would make the carry a lie.
    assert "Never invent a scope" in prompt


def test_the_rendered_scope_token_matches_what_the_prompt_tells_the_model():
    """The prompt tells the model to read ``scope=<target_id> geo=<CODES>``;
    the renderer must actually emit that shape. A drift here would be invisible
    until a live run silently dropped every dispatch's geo."""
    rendered = GroundingOpenQuestion(
        id=uuid4(), thesis="t", harvest_class="coverage_floor",
        target_id="country_watch_il", produced_at=_NOW, live_reach=0,
        desk_salience=0.0, geo=("IL",),
    ).render(tag="Q1", now=_NOW)
    assert "scope=country_watch_il geo=IL" in rendered
    prompt = _corpus_researcher().method.system_prompt
    assert "scope=<target_id> geo=<CODES>" in prompt


def test_shipped_manifest_matches_the_edited_descriptor():
    """The prompt manifest is a build artefact checked into the tree; editing a
    prompt without regenerating makes the R4 drift gauge quietly wrong."""
    import hashlib

    from legba.data.registry.production_gauge_integrity import MANIFEST_PATH

    manifest = json.loads(Path(MANIFEST_PATH).read_text("utf-8"))
    prompt = _corpus_researcher().method.system_prompt
    lines = [ln.rstrip() for ln in prompt.replace("\r\n", "\n").split("\n")]
    normalized = "\n".join(lines).strip() + "\n"
    expected = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    assert manifest["prompts"]["corpus_researcher"]["sha256"] == expected


# ---------------------------------------------------------------------------
# 9. THE ASSIGNMENT — a DISPATCHED question COMMANDS the run (2026-09-07)
#
# THE DEFECT. The 09-06 ranking tune worked: the live 03:37Z trace shows the
# IL/Palestine coverage_floor gap rendered as [Q1], carrying
# ``scope=country_watch_il geo=IL``, ahead of seven unit_payload rows. The run
# answered [Q2] — a 35-day-old Sizewell B wildfire question — made one
# ``search_corpus`` call, never reached the web, stamped no
# ``addressed_question``, and left the IL row open. Ranking a MENU correctly is
# not dispatch. These tests hold the line that a dispatched question is the
# run's JOB: the only question offered, carrying the id the tool actually
# reads, claimed so the next tick takes the next gap.
# ---------------------------------------------------------------------------


def _dispatched_q(**over: Any) -> GroundingOpenQuestion:
    kwargs: dict[str, Any] = dict(
        id=uuid4(), thesis="COVERAGE GAP — Palestine", produced_at=_NOW,
        harvest_class="coverage_floor", target_id="country_watch_il",
        live_reach=0, desk_salience=0.0, geo=("IL",),
    )
    kwargs.update(over)
    return GroundingOpenQuestion(**kwargs)


def test_select_picks_the_first_dispatched_class_row_in_ranked_order():
    harvested = _gq(harvest_class="unit_payload", thesis="wildfire")
    dispatched = _dispatched_q()
    picked = dq.select_dispatched_assignment([harvested, dispatched, _gq()])
    assert picked is not None
    assert picked.question is dispatched
    assert picked.tag == "Q1"


def test_select_returns_none_for_a_purely_harvested_backlog():
    """The live pre-dispatch state, and the one this must not disturb: eight
    unit_payload rows and nothing dispatched ⇒ no assignment, ordinary menu."""
    backlog = [_gq(harvest_class="unit_payload", thesis=f"q{i}") for i in range(8)]
    assert dq.select_dispatched_assignment(backlog) is None
    assert dq.select_dispatched_assignment([]) is None


def test_select_skips_a_dispatched_row_that_carries_no_target():
    """Without a target the tool can resolve no geo, so such a row would send a
    run to fetch evidence no desk can ever read (F-8). It stays on the ordinary
    backlog and can still be self-selected; it does not get to command a run."""
    scopeless = _dispatched_q(target_id=None, geo=())
    real = _dispatched_q()
    picked = dq.select_dispatched_assignment([scopeless, real])
    assert picked is not None and picked.question is real
    assert dq.select_dispatched_assignment([scopeless]) is None


def test_reference_gap_commands_a_run_the_same_way_coverage_floor_does():
    """Both DISPATCHED classes — an alert fired on each — and only those two."""
    assert dq.DISPATCHED_HARVEST_CLASSES == ("coverage_floor", "reference_gap")
    picked = dq.select_dispatched_assignment(
        [_gq(harvest_class="below_floor"),
         _dispatched_q(harvest_class="reference_gap")]
    )
    assert picked is not None and picked.harvest_class == "reference_gap"
    # Every HARVESTED class stays a suggestion, including ones that rank above
    # unit_payload — the distinction is "an alert fired", not "ranks high".
    for cls in ("below_floor", "fact_contention", "freshness_advisory",
                "scorecard_disagreement", "unit_payload", "collection_gap"):
        assert dq.select_dispatched_assignment(
            [_dispatched_q(harvest_class=cls)]
        ) is None


def test_assignment_block_states_the_job_and_prints_the_id_the_tool_reads():
    """The two things the 03:37Z prompt did not do: name ONE question as the
    job, and print the ``hypothesis_id`` ``web_evidence`` actually resolves geo
    from. The old block printed ``scope=`` / ``geo=`` — neither of which is a
    parameter of that tool — so a perfectly obedient model still landed
    geo-less evidence that reached no desk."""
    q = _dispatched_q()
    assignment = dq.DispatchedAssignment(q)
    block = dq.build_assignment_block(assignment, now=_NOW)
    assert "DISPATCHED RESEARCH ASSIGNMENT" in block
    assert "STANDING OPEN QUESTIONS" not in block   # not a menu any more
    # The question, tagged, with the scope token the descriptor documents…
    assert "[Q1]" in block and "scope=country_watch_il geo=IL" in block
    # …and the id that actually carries it, in the exact call shape — which is
    # the GATHER PROTOCOL's object, not Python. The clause used to print
    # ``web_evidence(query=…, hypothesis_id=…)``, a syntax the loop cannot
    # execute; handed that against a system prompt asking for {"tool", "args"},
    # the live 09-07/09-08 runs emitted {"action": …} and then narrated the
    # call into their findings. A prompt that contradicts its own protocol is
    # the defect whether or not the parser has since been widened to cover it.
    assert f"hypothesis_id={q.id}" in block
    assert (
        '{"tool": "web_evidence", "args": {"query": "<your query>", '
        f'"hypothesis_id": "{q.id}"}}'
    ) in block
    assert "web_evidence(query=" not in block
    # And DESCRIBING the call is named as not making it — the exact failure.
    assert "DESCRIBING this call is not making it" in block
    # The web leg is stated as compelled, and the reason is the null test.
    assert "MANDATORY" in block and "MUST" in block
    assert "cannot close by construction" in block
    assert assignment.geo == ("IL",) and assignment.question_id == q.id


def test_assignment_block_tells_the_model_the_ignored_kwargs_are_ignored():
    """The second half of the same bug: the tool silently drops target_id/geo
    arguments. Saying so is what stops a model from believing it carried the
    scope when it carried nothing."""
    block = dq.build_assignment_block(dq.DispatchedAssignment(_dispatched_q()))
    assert "does NOT accept a target_id or geo argument" in block


def test_the_sink_marks_which_entry_is_the_assignment():
    q = _dispatched_q()
    sink: dict[str, Any] = {}
    dq.fill_question_sink(sink, [q], assignment=dq.DispatchedAssignment(q))
    assert set(sink) == {"Q1"}                      # one question, one tag
    assert sink["Q1"]["dispatched"] is True
    assert sink["Q1"]["target_id"] == "country_watch_il"
    assert sink["Q1"]["geo"] == ["IL"]
    # A non-dispatched run's entries say so rather than omitting the key.
    plain: dict[str, Any] = {}
    dq.fill_question_sink(plain, [_gq(harvest_class="unit_payload")])
    assert plain["Q1"]["dispatched"] is False


def test_fill_question_sink_is_a_no_op_without_a_listening_run():
    dq.fill_question_sink(None, [_dispatched_q()])   # must not raise


@pytest.mark.parametrize(
    "raw,expected",
    [("", 26.0), ("   ", 26.0), ("nonsense", 26.0), ("0", 26.0), ("-3", 26.0),
     ("1.5", 1.5)],
)
def test_claim_reclaim_hours_env_override(monkeypatch, raw, expected):
    monkeypatch.setenv(dq.CLAIM_RECLAIM_HOURS_ENV, raw)
    assert dq.claim_reclaim_hours() == expected


def test_claim_reclaim_hours_defaults_when_unset(monkeypatch):
    monkeypatch.delenv(dq.CLAIM_RECLAIM_HOURS_ENV, raising=False)
    assert dq.claim_reclaim_hours() == 26.0


# --- DB-backed: the real hook, the real backlog SQL, the real claim ---------


async def _sweep_claims(pg_pool: Any) -> None:
    async with pg_pool.acquire() as conn:
        ids = [
            r["id"] for r in await conn.fetch(
                "SELECT id FROM hypotheses WHERE status = ANY($1::text[])",
                [dq.CLAIMED_STATUS, dq.ANSWERED_STATUS],
            )
        ]
        if ids:
            await conn.execute(
                "DELETE FROM bearing_edges WHERE dst_id = ANY($1::uuid[])", ids
            )
            await conn.execute(
                "DELETE FROM hypotheses WHERE id = ANY($1::uuid[])", ids
            )


@pytest_asyncio.fixture
async def claim_slate(pg_pool, clean_slate):
    """``clean_slate`` sweeps ``open_question`` rows; a CLAIMED/ANSWERED row has
    LEFT that status, so it needs its own sweep on both sides or it survives
    into a sibling test's ranking — the same reason clean_slate sweeps the whole
    backlog rather than only this file's rows."""
    await _sweep_claims(pg_pool)
    yield
    await _sweep_claims(pg_pool)


def _researcher_descriptor() -> Any:
    from legba.data.schemas.analyst import AnalystDescriptor

    return AnalystDescriptor.model_validate(
        {
            "identity": {
                "id": "corpus_researcher", "name": "R", "kind": "inline_target",
                "schema_uri": "legba/analyst/1.0.0", "version": "0" * 16,
                "type_signature": {
                    "input_type": "legba.runtime.SignalList",
                    "output_type": "legba.runtime.Finding",
                },
                "state": "active", "owner": "t",
            },
            "subscription": {
                "substrate": {"direct_queries": True, "gather_only": False}
            },
            "method": {
                "kind": "llm_planner",
                "prompt_module": "legba.runtime.analyst_method:_DEFAULT_SYSTEM",
                "llm": {"primary": {"factory_kind": "stack_ref", "raw": "llm.x",
                                    "expected_family": "llm_provider"}},
            },
            "cadence": {"fallback_schedule": "37 3,15 * * *"},
            "grounding": {
                "enabled": True, "sources": ["open_questions"], "max_facts": 8
            },
        },
        strict=False,
    )


async def _seed_stale_backlog(conn: Any, n: int = 3) -> list[Any]:
    """``n`` OLDER ``unit_payload`` questions WITH genuine forward reach — the
    exact competition the 03:37Z run lost to (Sizewell B and friends)."""
    ids = []
    ctx = AnalystContext(
        analyst_id="test_consumer", analyst_version="v1", run_id=uuid4()
    )
    for i in range(n):
        row = await conn.fetchrow(
            "INSERT INTO hypotheses (thesis, status, target_id, produced_at, "
            "diagnostic_evidence) VALUES ($1, 'open_question', NULL, "
            "now() - make_interval(days => $2), $3::jsonb) RETURNING id",
            f"Will the wildfire near Sizewell B force an outage? ({i})",
            35 + i,
            json.dumps([{"marker": "open_question_origin",
                         "origin": "unit_payload", "finding_id": str(uuid4())}]),
        )
        consumer, _dlq = await write_finding(
            conn, analyst_ctx=ctx,
            payload=FindingPayload(title=f"c{i}", body="b", confidence=0.5),
            derived_from=[],
        )
        assert consumer is not None
        await conn.execute(
            "INSERT INTO output_consumption (consumer_id, consumed_id, "
            "consumer_kind, context) VALUES ($1, $2, 'test_consumer', "
            "'composition_basis')",
            consumer.id, row["id"],
        )
        ids.append(row["id"])
    return ids


async def _run_hook(pg_pool: Any, *, run_id: Any = None):
    """One GROUND phase through the REAL production wiring: the deps-builder's
    own grounding hook over the real resolver, the real backlog SQL and the
    real claim write."""
    from legba.data.analysts.inline_target import GROUNDING_QUESTION_SINK_KEY
    from legba.runtime.analyst_deps_builder import _build_grounding_hook

    hook = _build_grounding_hook(_researcher_descriptor(), pg_pool=pg_pool)
    assert hook is not None
    sink: dict[str, Any] = {}
    opts: dict[str, Any] = {"target_id": None, GROUNDING_QUESTION_SINK_KEY: sink}
    if run_id is not None:
        opts["run_id"] = run_id
    return await hook([], opts), sink


async def test_the_dispatched_gap_becomes_the_run_s_only_question(
    pg_pool, claim_slate, flag_on,
):
    """THE FIX, against the live shape: three older unit_payload questions with
    forward reach plus one fresh IL coverage_floor dispatch. Before, all four
    were offered and the run picked a wildfire. Now the dispatch is the ONLY
    question in the prompt — there is no [Q2] left to pick."""
    run_id = uuid4()
    async with pg_pool.acquire() as conn:
        stale = await _seed_stale_backlog(conn)
        await rd.dispatch_open_questions(
            conn, [_dispatch()], alert_output_id=uuid4(), run_id=uuid4()
        )
        qid = await conn.fetchval(
            "SELECT id FROM hypotheses WHERE analyst_id = $1",
            rd.DISPATCH_ANALYST_ID,
        )

    block, sink = await _run_hook(pg_pool, run_id=run_id)

    assert block is not None
    assert "DISPATCHED RESEARCH ASSIGNMENT" in block
    assert "STANDING OPEN QUESTIONS" not in block
    assert "Palestine" in block
    assert f"hypothesis_id={qid}" in block
    assert f"scope={_TARGET} geo=IL" in block
    # The menu is gone: not one of the three stale questions is offered, and
    # the only tag the model can answer with is Q1.
    assert "Sizewell" not in block
    assert "[Q2]" not in block
    assert set(sink) == {"Q1"}
    assert sink["Q1"]["id"] == str(qid) and sink["Q1"]["dispatched"] is True
    assert sink["Q1"]["geo"] == ["IL"]

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, run_id FROM hypotheses WHERE id = $1", qid
        )
        # …and the three stale rows are untouched: a claim is not a purge.
        still_open = await conn.fetchval(
            "SELECT count(*) FROM hypotheses WHERE id = ANY($1::uuid[]) "
            "AND status = 'open_question'", stale,
        )
    # THE STATUS MOVED — claimed by this run, stamped with its id.
    assert row["status"] == dq.CLAIMED_STATUS
    assert row["run_id"] == run_id
    assert still_open == 3


async def test_an_undispatched_backlog_still_renders_the_whole_menu(
    pg_pool, claim_slate, flag_on,
):
    """The fallback, unchanged: no dispatched question ⇒ the ordinary
    priority-ordered STANDING OPEN QUESTIONS block, every row offered, nothing
    claimed. Self-selection keeps happening exactly when it should."""
    async with pg_pool.acquire() as conn:
        stale = await _seed_stale_backlog(conn)

    block, sink = await _run_hook(pg_pool)

    assert block is not None
    assert "STANDING OPEN QUESTIONS" in block
    assert "DISPATCHED RESEARCH ASSIGNMENT" not in block
    assert "Sizewell" in block
    assert len(sink) == len(stale)
    assert all(entry["dispatched"] is False for entry in sink.values())
    async with pg_pool.acquire() as conn:
        claimed = await conn.fetchval(
            "SELECT count(*) FROM hypotheses WHERE status = $1", dq.CLAIMED_STATUS,
        )
    assert claimed == 0


async def test_the_next_tick_takes_the_next_gap(pg_pool, claim_slate, flag_on):
    """The whole point of moving the status. Two dispatched gaps: tick one gets
    the first, tick two gets the SECOND rather than re-assigning the first
    forever."""
    second = _dispatch(
        entity_fold="lebanon", gap_name="Lebanon",
        source_id=rd.dispatch_source_id(_TARGET, "lebanon", _NOW),
    )
    async with pg_pool.acquire() as conn:
        n = await rd.dispatch_open_questions(
            conn, [_dispatch(), second], alert_output_id=uuid4(), run_id=uuid4(),
        )
        ids = [
            r["id"] for r in await conn.fetch(
                "SELECT id FROM hypotheses WHERE analyst_id = $1 ORDER BY id",
                rd.DISPATCH_ANALYST_ID,
            )
        ]
    assert n == 2 and len(ids) == 2

    first_block, _ = await _run_hook(pg_pool, run_id=uuid4())
    second_block, _ = await _run_hook(pg_pool, run_id=uuid4())
    assert first_block and second_block

    def _assigned(block: str) -> Any:
        return next(i for i in ids if f"hypothesis_id={i}" in block)

    assert _assigned(first_block) != _assigned(second_block)
    async with pg_pool.acquire() as conn:
        claimed = await conn.fetchval(
            "SELECT count(*) FROM hypotheses WHERE status = $1", dq.CLAIMED_STATUS,
        )
    assert claimed == 2


async def test_a_claim_is_answered_only_by_a_finding_that_bears_on_it(
    pg_pool, claim_slate, flag_on,
):
    """The settle takes its evidence from the append-only ``bearing_edges``
    pointer the runtime writes AFTER the finding persists — never from the
    assumption that the run worked. ``src_kind`` is load-bearing: claim_watch
    writes tens of thousands of SIGNAL -> hypothesis edges under the same
    ``bears_on`` kind, and one of those must never read as a research answer."""
    from legba.data.provenance.bearing import record_bearing_edge

    async with pg_pool.acquire() as conn:
        await rd.dispatch_open_questions(
            conn, [_dispatch()], alert_output_id=uuid4(), run_id=uuid4()
        )
        qid = await conn.fetchval(
            "SELECT id FROM hypotheses WHERE analyst_id = $1", rd.DISPATCH_ANALYST_ID
        )
    await _run_hook(pg_pool, run_id=uuid4())

    # A routine signal match lands on the same question. It is not an answer.
    now = datetime.now(timezone.utc)
    async with pg_pool.acquire() as conn:
        await record_bearing_edge(
            conn, src_kind="signal", src_id=uuid4(), src_as_of=now,
            dst_kind="hypothesis", dst_id=qid, dst_as_of=now, weight=1.0,
            planes=["entity"], matcher_version="test",
        )
    await dq.settle_claimed_questions(pg_pool, resolved_by="corpus_researcher")
    async with pg_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT status FROM hypotheses WHERE id = $1", qid
        ) == dq.CLAIMED_STATUS

    # The research finding's OWN edge does settle it.
    async with pg_pool.acquire() as conn:
        await record_bearing_edge(
            conn, src_kind="finding", src_id=uuid4(),
            src_as_of=datetime.now(timezone.utc), dst_kind="hypothesis",
            dst_id=qid, dst_as_of=now, weight=1.0, planes=["research"],
            matcher_version="test",
        )
    counts = await dq.settle_claimed_questions(
        pg_pool, resolved_by="corpus_researcher"
    )
    assert counts["answered"] == 1
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, resolved_by, resolved_at FROM hypotheses WHERE id = $1",
            qid,
        )
    assert row["status"] == dq.ANSWERED_STATUS
    assert row["resolved_by"] == "corpus_researcher"
    assert row["resolved_at"] is not None


async def test_an_abandoned_claim_returns_to_the_backlog(
    pg_pool, claim_slate, flag_on, monkeypatch,
):
    """A run that died between GROUND and PERSIST must never bury a gap. The
    claim expires and the question is offered again — the gap is still real,
    only this run's hold on it lapsed."""
    async with pg_pool.acquire() as conn:
        await rd.dispatch_open_questions(
            conn, [_dispatch()], alert_output_id=uuid4(), run_id=uuid4()
        )
        qid = await conn.fetchval(
            "SELECT id FROM hypotheses WHERE analyst_id = $1", rd.DISPATCH_ANALYST_ID
        )
    await _run_hook(pg_pool, run_id=uuid4())
    async with pg_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT status FROM hypotheses WHERE id = $1", qid
        ) == dq.CLAIMED_STATUS

    monkeypatch.setenv(dq.CLAIM_RECLAIM_HOURS_ENV, "0.0000001")
    counts = await dq.settle_claimed_questions(
        pg_pool, resolved_by="corpus_researcher"
    )
    assert counts["reclaimed"] == 1
    async with pg_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT status FROM hypotheses WHERE id = $1", qid
        ) == "open_question"


async def test_a_claim_still_dedups_the_dispatch(pg_pool, claim_slate, flag_on):
    """A claimed question has left ``open_question`` but is emphatically still
    open. If the dedup probe could not see it, the next coverage-floor scan
    would mint a duplicate of the gap a run is mid-way through answering."""
    async with pg_pool.acquire() as conn:
        await rd.dispatch_open_questions(
            conn, [_dispatch()], alert_output_id=uuid4(), run_id=uuid4()
        )
    await _run_hook(pg_pool, run_id=uuid4())
    async with pg_pool.acquire() as conn:
        again = await rd.dispatch_open_questions(
            conn, [_dispatch()], alert_output_id=uuid4(), run_id=uuid4()
        )
        total = await conn.fetchval(
            "SELECT count(*) FROM hypotheses WHERE analyst_id = $1",
            rd.DISPATCH_ANALYST_ID,
        )
    assert again == 0 and total == 1


async def test_the_claim_and_settle_writes_degrade_and_never_raise():
    class _Boom:
        def acquire(self):
            raise RuntimeError("substrate down")

    assert await dq.claim_dispatched_question(_Boom(), question_id=uuid4()) is False
    assert await dq.settle_claimed_questions(_Boom(), resolved_by="x") == {
        "answered": 0, "released": 0, "reclaimed": 0,
    }
