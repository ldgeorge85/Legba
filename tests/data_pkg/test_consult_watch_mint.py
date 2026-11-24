# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""F-4 ``propose_watch`` — a consult's watch candidates become standing open questions.

What is proven here, and on what:

  * PARSING (pure) — a candidate's resolution test is its own words and its
    horizon is its own ``by YYYY-MM-DD`` clause; nothing is invented.
  * SW-10 (pure) — no horizon, an unreadable one or a past one is refused by
    SW-10's own name; the arc window is a fallback only while it is in the future.
  * THE ROW (real driver) — a candidate mints a ``hypotheses`` row with
    ``status='open_question'`` through the ``open_question`` tool's writer; read
    back, the marker carries ``resolves_by``, ``created_by='consult'`` and the
    turn's origin (session, request, turn index), and ``target_id`` is the desk
    the citations name most.
  * REFUSALS (real driver) — no test → ``unmintable_no_test``; no horizon →
    ``hypothesis_refused_no_resolves_by``; neither writes a row.
  * THE FOLD (real driver) — a second consult proposing the same indicator
    folds onto the standing row; one row, not two.
  * THE HOOK — ``run_method`` mints on an accepted final and stamps
    ``watch_minted`` on the response AND on the finding carrier; a deps bundle
    with no write surface, or the knob off, stamps nothing.
  * THE BAG (real driver) — ``watch_minted`` rides the 0223 response bag and
    the session read-back lifts it; the chat projection lifts it too.
  * CLAIM_WATCH — the minted row is in the matcher's open set (its own SQL) and
    is not a meta question.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts import consult_watch_mint as wm
from legba.data.analysts.consult_on_demand import (
    FINAL_SENTINEL,
    ConsultOnDemandDeps,
    run_method,
)
from legba.data.config import PostgresConfig
from legba.data.provenance import AnalystContext
from legba.data.provenance.models import ConsultResponsePayload, FindingPayload

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
FUTURE = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
PAST = (datetime.now(timezone.utc) - timedelta(days=3)).date().isoformat()


# ---------------------------------------------------------------------------
# Pure — parsing and the horizon rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw, test, horizon", [
    ("the Moldovan vote is postponed by 2026-10-14",
     "the Moldovan vote is postponed", "2026-10-14"),
    ("a second outlet names the successor — by 2026-11-30",
     "a second outlet names the successor", "2026-11-30"),
    ("Baltic cable cuts exceed three (by 2026-12-15T00:00Z)",
     "Baltic cable cuts exceed three", "2026-12-15T00:00Z"),
    ("Baltic cable cuts exceed three", "Baltic cable cuts exceed three", None),
    ("by 2026-10-14", "", "2026-10-14"),
    ("  — ; ", "", None),
    # The live roll-32 candidate (session 5cef6e9b): U+2011 non-breaking
    # hyphens in the date and the indicator — refused before the dash fold.
    ("major\u2011arms\u2011acquisition observed by 2026\u201110\u201117",
     "major-arms-acquisition observed", "2026-10-17"),
    ("sanctions waiver renewed \u2013 by 2026\u221210\u221220",
     "sanctions waiver renewed", "2026-10-20"),
    # W4b-F5: the live text — the ")" closes a group opened inside the
    # indicator, so it stays; the old strip left "(e.g., new missile system".
    ("detection of a major arms acquisition (e.g., new missile system) "
     "by 2026-10-17",
     "detection of a major arms acquisition (e.g., new missile system)",
     "2026-10-17"),
    ("detection of a major arms acquisition [e.g., new system]; by 2026-10-17",
     "detection of a major arms acquisition [e.g., new system]", "2026-10-17"),
    # An unmatched closer is still stripped.
    ("the vote is postponed) by 2026-10-17", "the vote is postponed",
     "2026-10-17"),
])
def test_parse_candidate_reads_its_own_words_and_its_own_date(raw, test, horizon):
    cand = wm.parse_candidate(raw)
    assert cand.test == test
    assert cand.horizon_text == horizon


def test_a_date_inside_the_indicator_is_not_a_horizon():
    """Only the trailing ``by <date>`` clause is a horizon — a date the
    indicator talks ABOUT is part of the observation."""
    cand = wm.parse_candidate("the 2026-10-14 vote is postponed")
    assert cand.horizon_text is None
    assert cand.test == "the 2026-10-14 vote is postponed"


def test_horizon_from_the_candidate():
    cand = wm.parse_candidate("x happens by 2026-10-14")
    horizon, source, refusal = wm.resolve_horizon(cand, answer="", now=NOW)
    assert horizon == datetime(2026, 10, 14, tzinfo=timezone.utc)
    assert source == "candidate"
    assert refusal is None


def test_a_past_horizon_is_refused_never_defaulted():
    cand = wm.parse_candidate("x happens by 2026-10-01")
    horizon, source, refusal = wm.resolve_horizon(cand, answer="", now=NOW)
    assert horizon is None and source is None
    assert "not in the future" in refusal


def test_an_unreadable_horizon_is_refused():
    cand = wm.WatchCandidate(raw="x by 2026-13-45", test="x", horizon_text="2026-13-45")
    horizon, _src, refusal = wm.resolve_horizon(cand, answer="", now=NOW)
    assert horizon is None
    assert "ISO-8601" in refusal


def test_the_arc_window_end_is_a_fallback_only_while_it_is_future():
    cand = wm.parse_candidate("x happens")
    future = "survey window: 90 days, 2026-09-01 to 2026-12-31"
    past = "survey window: 90 days, 2026-06-29 to 2026-09-27"
    horizon, source, _ = wm.resolve_horizon(cand, answer=future, now=NOW)
    assert horizon == datetime(2026, 12, 31, tzinfo=timezone.utc)
    assert source == "arc_window"
    horizon, _src, refusal = wm.resolve_horizon(cand, answer=past, now=NOW)
    assert horizon is None
    assert "arc window ends 2026-09-27" in refusal
    horizon, _src, refusal = wm.resolve_horizon(cand, answer="no window", now=NOW)
    assert horizon is None
    assert "names no horizon" in refusal


def test_fold_key_normalises_case_space_and_trailing_punctuation():
    assert wm.fold_key("The vote  is postponed.") == wm.fold_key("the vote is postponed")
    assert wm.fold_key("the vote is postponed") != wm.fold_key("the vote is held")


def test_reason_and_marker_names_do_not_drift():
    """SW-10's refusal and the shared marker key are MIRRORED here, not
    imported at module import time; these are the drift guards."""
    from legba.data.analysts.agency.inquiry_state import HYPOTHESIS_NO_HORIZON_REFUSAL
    from legba.data.analysts.deterministic_handlers.claim_watch import (
        HARVEST_MARKER_KEY,
        harvest_class,
        is_meta_question,
    )
    from legba.data.analysts.inquiry import HYPOTHESIS_WITHOUT_HORIZON_FLAG
    from legba.data.analysts.open_question_conversion import OPEN_QUESTION_MARKER_KEY

    assert wm.REASON_NO_HORIZON == HYPOTHESIS_WITHOUT_HORIZON_FLAG
    assert wm.REASON_NO_HORIZON == HYPOTHESIS_NO_HORIZON_REFUSAL
    assert wm.MARKER_KEY == OPEN_QUESTION_MARKER_KEY == HARVEST_MARKER_KEY
    # A consult watch is a question about the WORLD — never a meta class.
    marker = [{"marker": wm.MARKER_KEY, "origin": "consult", "source_id": "consult:x"}]
    assert harvest_class(marker) is None
    assert not is_meta_question(marker)


def test_the_knob_is_declared_and_defaults_on():
    from legba.data.option_catalog.handler_options import (
        ANALYST_KIND_OPTIONS,
        resolve_kind_options,
    )

    names = {s.name for s in ANALYST_KIND_OPTIONS["consult_on_demand"]}
    assert wm.MINT_OPTION in names
    assert wm.mint_enabled({}) is True
    assert wm.mint_enabled({wm.MINT_OPTION: False}) is False
    # A non-bool per-run value never flips it off silently — the default holds.
    assert wm.mint_enabled({wm.MINT_OPTION: "no"}) is True
    resolution = resolve_kind_options(
        "consult_on_demand", {wm.MINT_OPTION: False},
    )
    assert resolution.accepted == {wm.MINT_OPTION: False}
    assert not resolution.rejected


# ---------------------------------------------------------------------------
# Real driver
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield p
    # Sweep this file's rows: global open-question consumers (the claim_watch
    # scanner tests) must never see them.
    try:
        async with p.acquire() as c:
            await c.execute(
                "DELETE FROM hypotheses WHERE status = 'open_question' "
                "AND diagnostic_evidence @> $1::jsonb",
                json.dumps([{"marker": wm.MARKER_KEY, "origin": "consult"}]),
            )
            await c.execute(
                "DELETE FROM analyst_outputs WHERE analyst_id = 'f4_desk_unit'"
            )
    finally:
        await p.close()


def _options() -> dict[str, Any]:
    return {
        "analyst_id": "consult_default",
        "analyst_version": "v-f4-test",
        "run_id": uuid4(),
    }


def _consult(candidates: list[str], *, refs: list[UUID] | None = None,
             question: str | None = None) -> ConsultResponsePayload:
    return ConsultResponsePayload(
        question=question or f"Will Moldova's election go ahead? {uuid4().hex[:6]}",
        answer="## Bottom line\n\nbody",
        cited_substrate_refs=refs or [],
        data={"watch_candidates": candidates},
    )


async def _row(pool: Any, hid: str) -> Any:
    async with pool.acquire() as c:
        return await c.fetchrow("SELECT * FROM hypotheses WHERE id = $1", UUID(hid))


async def _finding(pool: Any, target_id: str) -> UUID:
    from legba.data.provenance.writes import write_finding

    async with pool.acquire() as c:
        row, _ = await write_finding(
            c,
            analyst_ctx=AnalystContext(
                analyst_id="f4_desk_unit", analyst_version="v1",
                run_id=uuid4(), target_id=target_id,
            ),
            payload=FindingPayload(title=f"finding on {target_id}"),
            derived_from=[],
        )
    return row.id


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_candidate_mints_an_open_question_row_read_back(pool):
    desk_a = f"country_f4a_{uuid4().hex[:6]}"
    desk_b = f"country_f4b_{uuid4().hex[:6]}"
    refs = [await _finding(pool, desk_a), await _finding(pool, desk_a),
            await _finding(pool, desk_b)]
    indicator = f"the Moldovan parliamentary vote {uuid4().hex[:6]} is postponed"
    consult = _consult([f"{indicator} by {FUTURE}"], refs=refs)
    options = _options()
    first = {
        "question": consult.question, "request_id": "req-f4-1",
        "session_id": "11111111-2222-3333-4444-555555555555",
        "messages": [
            {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ],
    }
    minted = await wm.mint_watch_candidates(
        consult, first=first, options=options, pool=pool,
    )
    assert minted is not None and len(minted) == 1
    entry = minted[0]
    assert entry["refused"] is False and entry["folded"] is False
    assert entry["reason"] is None
    assert entry["resolves_by"].startswith(FUTURE)

    row = await _row(pool, entry["hypothesis_id"])
    assert row["status"] == "open_question"
    assert row["thesis"] == indicator
    assert row["analyst_id"] == "consult_default"
    assert row["run_id"] == options["run_id"]
    # The desk the citations name most — claim_watch's per-desk fold keys on it.
    assert row["target_id"] == desk_a
    assert set(row["derived_from"]) == set(refs)
    marker = json.loads(row["diagnostic_evidence"])[0]
    assert marker["marker"] == wm.MARKER_KEY
    assert marker["origin"] == "consult"
    assert marker["created_by"] == "consult"
    assert marker["resolution_test"] == indicator
    assert marker["resolves_by"].startswith(FUTURE)
    assert marker["horizon_source"] == "candidate"
    assert marker["subjects"] == [desk_a, desk_b]
    # The origin names the turn.
    assert marker["session_id"] == first["session_id"]
    assert marker["request_id"] == "req-f4-1"
    assert marker["turn"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_deictic_indicator_is_folded_against_the_consult_question(pool):
    """The ``open_question`` tool's CW-3 fold, on the consult's own question."""
    question = f"Is the Moldovan election {uuid4().hex[:6]} still on track?"
    consult = _consult([f"the summit is postponed by {FUTURE}"], question=question)
    minted = await wm.mint_watch_candidates(
        consult, first={"question": question}, options=_options(), pool=pool,
    )
    row = await _row(pool, minted[0]["hypothesis_id"])
    assert row["thesis"].startswith("the summit is postponed (in reference to: ")
    assert "Moldovan election" in row["thesis"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_refusals_write_no_row(pool):
    no_test = f"by {FUTURE}"
    no_horizon = f"Baltic cable cuts {uuid4().hex[:6]} exceed three"
    past = f"Baltic cable cuts {uuid4().hex[:6]} exceed four by {PAST}"
    consult = _consult([no_test, no_horizon, past])
    async with pool.acquire() as c:
        before = await c.fetchval("SELECT count(*) FROM hypotheses")
    minted = await wm.mint_watch_candidates(
        consult, first={"question": consult.question}, options=_options(), pool=pool,
    )
    async with pool.acquire() as c:
        after = await c.fetchval("SELECT count(*) FROM hypotheses")
    assert after == before
    by_cand = {e["candidate"]: e for e in minted}
    assert by_cand[no_test]["reason"] == "unmintable_no_test"
    assert by_cand[no_horizon]["reason"] == "hypothesis_refused_no_resolves_by"
    assert "names no horizon" in by_cand[no_horizon]["detail"]
    assert by_cand[past]["reason"] == "hypothesis_refused_no_resolves_by"
    assert "not in the future" in by_cand[past]["detail"]
    assert all(e["refused"] and e["hypothesis_id"] is None for e in minted)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_second_consult_on_the_same_question_folds_not_duplicates(pool):
    indicator = f"Chisinau postpones the poll {uuid4().hex[:6]}"
    question = "Will Moldova's election go ahead?"
    first_minted = await wm.mint_watch_candidates(
        _consult([f"{indicator} by {FUTURE}"], question=question),
        first={"question": question, "request_id": "r1"},
        options=_options(), pool=pool,
    )
    # The same indicator, re-cased and re-punctuated, on a later consult.
    second_minted = await wm.mint_watch_candidates(
        _consult([f"{indicator.upper()}. by {FUTURE}"], question=question),
        first={"question": question, "request_id": "r2"},
        options=_options(), pool=pool,
    )
    hid = first_minted[0]["hypothesis_id"]
    assert first_minted[0]["folded"] is False
    assert second_minted[0]["folded"] is True
    assert second_minted[0]["hypothesis_id"] == hid
    async with pool.acquire() as c:
        n = await c.fetchval(
            "SELECT count(*) FROM hypotheses WHERE status = 'open_question' "
            "AND diagnostic_evidence @> $1::jsonb",
            json.dumps([{"marker": wm.MARKER_KEY, "origin": "consult",
                         "source_id": wm.fold_key(indicator)}]),
        )
    assert n == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_minted_row_is_in_claim_watchs_open_set(pool):
    """claim_watch's own open-set SQL, its own default statuses, its own cap."""
    from legba.data.analysts.deterministic_handlers.claim_watch import (
        DEFAULT_QUESTION_CAP,
        DEFAULT_QUESTION_STATUSES,
        _OPEN_QUESTIONS_SQL,
    )

    consult = _consult([f"Gagauzia {uuid4().hex[:6]} declares a boycott by {FUTURE}"])
    minted = await wm.mint_watch_candidates(
        consult, first={"question": consult.question}, options=_options(), pool=pool,
    )
    async with pool.acquire() as c:
        rows = await c.fetch(
            _OPEN_QUESTIONS_SQL, list(DEFAULT_QUESTION_STATUSES), DEFAULT_QUESTION_CAP,
        )
    assert UUID(minted[0]["hypothesis_id"]) in {r["id"] for r in rows}


@pytest.mark.asyncio
async def test_nothing_is_attempted_without_a_surface_or_with_the_knob_off():
    consult = _consult([f"x happens by {FUTURE}"])
    assert await wm.mint_watch_candidates(
        consult, first={}, options=_options(), pool=None,
    ) is None
    assert await wm.mint_watch_candidates(
        consult, first={}, options={**_options(), wm.MINT_OPTION: False},
        pool=object(),
    ) is None
    assert await wm.mint_watch_candidates(
        _consult([]), first={}, options=_options(), pool=object(),
    ) is None


@pytest.mark.asyncio
async def test_a_broken_surface_is_stamped_per_candidate_never_raised():
    class _Broken:
        def acquire(self):
            raise RuntimeError("pool down")

    consult = _consult([f"x happens by {FUTURE}", "y happens"])
    minted = await wm.mint_watch_candidates(
        consult, first={}, options=_options(), pool=_Broken(),
    )
    assert [e["reason"] for e in minted] == ["mint_failed", "mint_failed"]
    assert all(e["refused"] for e in minted)


# ---------------------------------------------------------------------------
# The hook — through run_method
# ---------------------------------------------------------------------------


@dataclass
class _Usage:
    prompt_tokens: int = 10
    completion_tokens: int = 5
    reasoning_tokens: int = 0


@dataclass
class _Response:
    content: str = ""
    usage: _Usage | None = None


class _ScriptedLLM:
    subprovider = "vllm-test"

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def chat_complete(self, messages: list[Mapping[str, Any]], **_: Any) -> Any:
        return _Response(content=self._responses.pop(0), usage=_Usage())


class _BareSubstrate:
    async def search_signals(self, **_: Any) -> dict[str, Any]:
        return {"rows": [], "refs": [], "count": 0}


def _final(watch: str) -> str:
    return (
        f"{FINAL_SENTINEL}\nuncertainty: 0.4\nwatch_candidates: {watch}\n\n"
        "## Bottom line\n\nbody"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_method_mints_and_stamps_the_response_and_the_finding(pool):
    indicator = f"Transnistria {uuid4().hex[:6]} closes the crossing"
    steps: list[dict[str, Any]] = []

    async def _publish(step: dict[str, Any]) -> None:
        steps.append(step)

    result = await run_method(
        [{"question": "Will Moldova's election go ahead?", "request_id": "r-hook",
          "messages": []}],
        _options(),
        ConsultOnDemandDeps(
            llm=_ScriptedLLM([_final(f"{indicator} by {FUTURE}; no date here")]),
            substrate=_BareSubstrate(), pg_pool=pool, step_publish=_publish,
        ),
    )
    minted = result.consult_response.data["watch_minted"]
    assert [e["refused"] for e in minted] == [False, True]
    assert minted[1]["reason"] == "hypothesis_refused_no_resolves_by"
    row = await _row(pool, minted[0]["hypothesis_id"])
    assert row["thesis"] == indicator
    assert json.loads(row["diagnostic_evidence"])[0]["turn"] == 0
    # The finding carrier (the deep path's read-back) carries the same stamp.
    carried = result.finding.data["consult_response"]["data"]["watch_minted"]
    assert carried == minted
    trace = [s for s in steps if s.get("kind") == "watch_mint"]
    assert trace and trace[0]["minted"] == 1
    assert trace[0]["refused"] == {"hypothesis_refused_no_resolves_by": 1}


@pytest.mark.asyncio
async def test_run_method_without_a_surface_stamps_nothing():
    result = await run_method(
        [{"question": "q"}],
        _options(),
        ConsultOnDemandDeps(
            llm=_ScriptedLLM([_final(f"x happens by {FUTURE}")]),
            substrate=_BareSubstrate(),
        ),
    )
    assert result.consult_response.data["watch_candidates"] == [f"x happens by {FUTURE}"]
    assert "watch_minted" not in result.consult_response.data


# ---------------------------------------------------------------------------
# The bag — persisted, read back, projected
# ---------------------------------------------------------------------------


_MINTED = [
    {"candidate": "x happens by 2026-12-01", "hypothesis_id": str(uuid4()),
     "refused": False, "reason": None, "resolves_by": "2026-12-01T00:00:00+00:00",
     "folded": False},
    {"candidate": "y happens", "hypothesis_id": None, "refused": True,
     "reason": "hypothesis_refused_no_resolves_by", "resolves_by": None,
     "folded": False},
]


def test_the_chat_projection_lifts_watch_minted():
    from legba.data.registry.consult_api import _project_consult_response

    resp = _project_consult_response(
        {"answer": "a", "data": {"watch_minted": _MINTED}},
        finding_id=None, derived_from=[],
    )
    assert resp.watch_minted == _MINTED
    bare = _project_consult_response({"answer": "a", "data": {}},
                                     finding_id=None, derived_from=[])
    assert bare.watch_minted is None


def test_the_recovery_projection_lifts_watch_minted():
    from legba.data.registry.consult_synthesis_api import project_recovery

    out = project_recovery(
        {"answer": "a", "watch_minted": _MINTED},
        evidence={}, request_id="r", chosen_model="core", turn_id=None,
    )
    assert out.watch_minted == _MINTED


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_bag_key_is_persisted_and_lifted_on_read_back(pool):
    from legba.data.registry import consult_persistence as cp
    from legba.data.registry.consult_api import ConsultResponse
    from legba.data.registry.consult_sessions_api import ConsultSessionDetail

    assert "watch_minted" in cp.RESPONSE_BAG_KEYS
    session_id = await cp.create_session(pool, mode="chat", question="f4 bag")
    try:
        await cp.append_turn(
            pool, session_id=session_id, role="assistant", content="a",
            response=cp.response_bag(ConsultResponse(answer="a", watch_minted=_MINTED)),
        )
        loaded = await cp.load_session(pool, session_id)
        detail = ConsultSessionDetail(**loaded)
        assert detail.turns[0].watch_minted == _MINTED
        async with pool.acquire() as c:
            raw = await c.fetchval(
                "SELECT response FROM consult_turns WHERE session_id = $1",
                UUID(session_id),
            )
        assert json.loads(raw)["watch_minted"] == _MINTED
    finally:
        async with pool.acquire() as c:
            await c.execute(
                "DELETE FROM consult_turns WHERE session_id = $1", UUID(session_id),
            )
            await c.execute(
                "DELETE FROM consult_sessions WHERE id = $1", UUID(session_id),
            )


def test_the_recovery_input_names_the_turn_it_recovers_and_nothing_to_drill():
    from legba.data.registry.consult_synthesis_api import build_recovery_input

    first = build_recovery_input(
        {"question": "q", "session_id": "s-1", "turn_id": "t-1"},
        llm_component_override=None, request_id="r-1",
    )
    assert first["session_id"] == "s-1"
    assert first["turn_id"] == "t-1"
    assert "messages" not in first and "max_tool_rounds" not in first
    # The mint's origin for a recovery names the turn it replaces; there is no
    # transcript, so no turn index is claimed.
    assert wm._origin(first) == {
        "session_id": "s-1", "request_id": "r-1", "turn": None,
        "recovers_turn_id": "t-1",
    }
