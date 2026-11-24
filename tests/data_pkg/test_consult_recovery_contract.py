# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A recovered turn ("Synthesize from evidence") answers with a turn contract
(``consult_contract/1``) and stores it on the new turn, rather than none.

Since Q-2 the recovery run settles its OWN contract over the original turn's
window / grounding / coverage (``test_consult_contract_gaps_q2``); it wins.
When the run produced none (an unarmed runtime, no synthesis), the original
turn's 0223 response bag supplies it, as roll 81 made it. Nothing is
invented: none from either recovers with none.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from legba.data.registry import consult_persistence
from legba.data.registry.consult_synthesis_api import (
    project_recovery,
    recovered_contract,
    with_recovered_contract,
)

_CONTRACT = {
    "schema": "consult_contract/1",
    "window": {"from": "2026-11-07T00:00:00Z", "to": "2026-11-14T00:00:00Z"},
    "reach": {"sources": 12},
}


class _Conn:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.sql: list[str] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.sql.append(sql)
        return self.row

    async def fetchval(self, sql: str, *args: Any) -> str:
        return "the question"


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _Conn:
        return self.conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


def _row(response: Any) -> dict[str, Any]:
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "session_id": "00000000-0000-0000-0000-0000000000aa",
        "role": "assistant", "content": "cut", "steps": "[]", "tool_calls": "[]",
        "cited_refs": "[]", "finding_id": None, "request_id": "req-0",
        "synthesis_status": "incomplete", "replay_transcript": None,
        "usage": None, "response": response, "created_at": None,
    }


@pytest.mark.asyncio
async def test_the_recovery_loader_reads_the_contract_the_turn_stored() -> None:
    bag = dict.fromkeys(consult_persistence.RESPONSE_BAG_KEYS)
    bag["contract"] = _CONTRACT
    conn = _Conn(_row(json.dumps(bag)))
    ev = await consult_persistence.load_turn_for_recovery(_Pool(conn), request_id="req-0")
    assert ev is not None and ev["contract"] == _CONTRACT
    assert "response" in conn.sql[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, json.dumps({"model": "core", "contract": None})])
async def test_a_turn_that_stored_no_contract_recovers_with_none(response: Any) -> None:
    ev = await consult_persistence.load_turn_for_recovery(
        _Pool(_Conn(_row(response))), request_id="req-0",
    )
    assert ev is not None and ev["contract"] is None


def test_the_recovered_answer_carries_the_original_contract() -> None:
    out = project_recovery(
        {"answer": "the recovered answer", "replay_fidelity": "exact"},
        evidence={"session_id": "s", "turn_id": "t0", "contract": _CONTRACT},
        request_id="req-1", chosen_model="core", turn_id="t1",
    )
    assert out.contract == _CONTRACT
    none = project_recovery(
        {"answer": "x", "replay_fidelity": "exact"},
        evidence={"session_id": "s", "turn_id": "t0", "contract": None},
        request_id="req-1", chosen_model="core", turn_id="t1",
    )
    assert none.contract is None


def test_the_recovered_turn_stores_the_same_contract_it_answers_with() -> None:
    bag = dict.fromkeys(consult_persistence.RESPONSE_BAG_KEYS)
    stored = with_recovered_contract(bag, {"contract": _CONTRACT})
    assert stored is not None and stored["contract"] == _CONTRACT
    assert set(stored) == set(consult_persistence.RESPONSE_BAG_KEYS)
    assert with_recovered_contract(None, {"contract": _CONTRACT}) is None
    # Nothing stored, nothing produced: nothing invented.
    assert recovered_contract(None, {}) is None
    # A run that did produce its own is kept when the original stored none.
    assert recovered_contract({"schema": "consult_contract/1"}, {}) == {
        "schema": "consult_contract/1"
    }
    # Q-2: a recovery run that settled its OWN contract (it carries
    # ``recovery``) wins over the original's — the original's grounding
    # accounting describes prose that is no longer the answer.
    own = {"schema": "consult_contract/1", "recovery": {"window_from": "original_turn"}}
    assert recovered_contract(own, {"contract": _CONTRACT}) == own
    stored = with_recovered_contract({**bag, "contract": own}, {"contract": _CONTRACT})
    assert stored is not None and stored["contract"] == own
