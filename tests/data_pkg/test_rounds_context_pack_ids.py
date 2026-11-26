# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The context builder names the rows it keeps by id; code fills them in.

A seat that re-typed every row could change a row's text on the way, and a
later quote check would then be checked against the seat's copy. Now the seat
writes ``row_ids``, the rows are copied from what it was handed or its tools
returned, and an id it was not handed is refused by the ``handed_rows`` gate.
"""
from __future__ import annotations

from pathlib import Path

from legba.data.rounds.gates import GateContext, run_gates
from legba.data.rounds.interpreter import Interpreter
from legba.data.rounds.model_seats import output_contract
from legba.data.rounds.rows import fill_rows_from_ids, parse_row
from legba.data.rounds.seats import Dispatch, FrozenClock, SeatCall, SeatReply, SyntheticSeats
from legba.data.rounds.spec import load_spec, load_yaml
from legba.data.rounds.store import MemoryStore
from legba.data.rounds.tables import Handed, evidence_rows

SPECS = Path(__file__).resolve().parents[2] / "src" / "legba" / "data" / "rounds" / "specs"

ROWS = [
    {"id": "sig-a", "kind": "signal", "text": "The port reopened on Monday.", "required": True},
    {"id": "sig-b", "kind": "signal", "text": "Traffic stayed light.", "source_id": "outlet-b"},
]


def _handed() -> list[Handed]:
    return [Handed("freeze", "r-freeze", "freeze", "frozen_scope/1",
                   {"subject": "s", "as_of": "2026-01-01", "rows": ROWS})]


def test_the_shape_asks_for_ids_and_never_for_row_text() -> None:
    contract = output_contract("context_pack/1")
    assert '"row_ids"' in contract
    assert '"rows"' not in contract and '"text"' not in contract
    assert "never write a row's text" in contract


def test_cards_ask_for_ids() -> None:
    for name in ("context_builder.md", "context_builder_v2.md"):
        card = (SPECS / "cards" / name).read_text(encoding="utf-8")
        assert "the ids of the rows" in card
        assert "same text" not in card


def test_code_fills_the_rows_from_what_was_handed() -> None:
    filled = fill_rows_from_ids("context_pack/1", {"row_ids": ["sig-b", "sig-a", "sig-b"]},
                                evidence_rows(_handed()))
    payload = parse_row("context_pack/1", filled)
    assert payload["row_ids"] == ["sig-b", "sig-a"]
    assert [r["text"] for r in payload["rows"]] == [ROWS[1]["text"], ROWS[0]["text"]]
    assert payload["rows"][0]["source_id"] == "outlet-b"


def test_a_reply_that_retyped_rows_gets_the_record_text_back() -> None:
    reply = {"rows": [{"id": "sig-a", "text": "The port reopened on Monday after a week."}]}
    payload = parse_row("context_pack/1", fill_rows_from_ids(
        "context_pack/1", reply, evidence_rows(_handed())))
    assert payload["rows"][0]["text"] == ROWS[0]["text"]


def test_tool_rows_drop_keys_the_row_type_does_not_carry() -> None:
    handed = _handed() + [Handed("tool", "tool:k", None, None,
                                 {"rows": [{"id": "web-1", "text": "Fetched.", "extra": 1}]})]
    payload = parse_row("context_pack/1", fill_rows_from_ids(
        "context_pack/1", {"row_ids": ["web-1"]}, evidence_rows(handed)))
    assert payload["rows"] == [{"id": "web-1", "kind": "signal", "text": "Fetched.",
                                "values": [], "required": False}]


def test_other_row_types_pass_through() -> None:
    reply = {"rows": [{"id": "x"}]}
    assert fill_rows_from_ids("evidence_rows/1", reply, {}) is reply


def _gates(payload: dict) -> dict[str, object]:
    filled = parse_row("context_pack/1", fill_rows_from_ids(
        "context_pack/1", payload, evidence_rows(_handed())))
    ctx = GateContext("context", "context_pack/1", filled, _handed(), "r1")
    return {g.gate: g for g in run_gates(["handed_rows", "must_include_set"], ctx)}


def test_an_id_that_was_not_handed_is_refused_by_name() -> None:
    got = _gates({"row_ids": ["sig-a", "invented-9"]})
    assert not got["handed_rows"].passed
    assert got["handed_rows"].reason == "not_in_run_slice"
    assert any((c.get("detail") or {}).get("row_id") == "invented-9"
               for c in got["handed_rows"].checks)
    assert got["must_include_set"].passed


def test_dropping_a_required_row_still_fails() -> None:
    got = _gates({"row_ids": ["sig-b"]})
    assert got["handed_rows"].passed
    assert got["must_include_set"].reason == "required_row_dropped"


def test_the_round_hands_filled_rows_downstream() -> None:
    """stance_linear/2 end to end: the context seat replies with ids only and
    the lead is handed the full rows."""
    spec, refusals = load_spec(SPECS / "stance_linear_v2.yaml")
    assert spec is not None, refusals
    seen: dict[str, list] = {}
    synthetic = SyntheticSeats()

    def provider(call: SeatCall) -> SeatReply:
        seen.setdefault(call.seat_id, [h.payload for h in call.inputs])
        reply = synthetic(call)
        if call.seat_id == "context":
            assert set(reply.payload) == {"row_ids"}
        return reply

    f = SPECS / "inputs" / f"{spec.id}.yaml"
    inputs = load_yaml(f.read_text(encoding="utf-8")) if f.is_file() else {
        "subject": "example-subject", "question": "Will it change?",
        "as_of": "2026-01-01T00:00:00Z"}
    rec = Interpreter(spec, MemoryStore(), Dispatch(provider), inputs=inputs,
                      run_id="ctx-ids", clock=FrozenClock()).run()
    assert rec["run"]["state"] == "ended"
    packs = [p for p in seen["lead"] if isinstance(p, dict) and "row_ids" in p]
    assert packs and packs[0]["rows"]
    assert [r["id"] for r in packs[0]["rows"]] == packs[0]["row_ids"]
    assert all(r.get("text") for r in packs[0]["rows"])
