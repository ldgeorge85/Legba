# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lane G-5: model and judge seats, tools bound with the cut and the run's own
rows excluded, and the substrate code seats — against fakes. No database, no
model, no network.

* the prompt is the card and the handed inputs only (nothing the store
  withheld reaches it), with the row type's output contract;
* replies are read with the house fold and cut to the declared row type;
* thinking per seat, the house temperature, no ``max_tokens``, the seat's
  plane, the scheduled class, the host's call cap;
* tools: an unclassified tool is refused, ``known_at`` is the run's, and a
  BLIND seat's tool call cannot see a sibling's output (a planted sibling row
  is filtered out by id and by text before the model sees it);
* the five substrate code functions return their typed rows from read-only
  SELECTs with the as-of cut, and write nothing.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from legba.data.rounds.interpreter import Interpreter
from legba.data.rounds.model_seats import (
    CallCapReached,
    LoopThread,
    ModelSeats,
    output_contract,
    parse_reply,
    render_prompt,
)
from legba.data.rounds.rows import ROW_TYPES, parse_row
from legba.data.rounds.seats import Dispatch, FrozenClock, SeatCall, SeatFailure, SyntheticSeats
from legba.data.rounds.spec import parse_spec
from legba.data.rounds.store import MemoryStore
from legba.data.rounds.substrate_seats import SubstrateSeats
from legba.data.rounds.tables import Handed
from legba.data.rounds.tool_binding import (
    Exclusions,
    RoundTools,
    exclusions_from_receipts,
    filter_output,
    rows_from_output,
)
from legba.data.stack.llm.base import LLMResponse, LLMToolCall, LLMUsage
from legba.shared.sampling import HOUSE_TEMPERATURE

from ._rounds_helpers import INPUTS, PRIMARY, SECOND, base_spec, mutate, write_cards

AS_OF = datetime(2026, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeLLM:
    """A handler double: answers each row type with a gate-clean payload citing
    the synthetic row-0, records every call's kwargs and messages; offers one
    tool call on the first round when tools are offered and ``tool`` is set."""

    subprovider = "vllm"

    def __init__(self, tool: tuple[str, dict[str, Any]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.tool = tool
        self.n = 0

    async def chat_complete(self, messages: list[dict[str, Any]], **kw: Any) -> LLMResponse:
        self.calls.append({"messages": [dict(m) for m in messages], **kw})
        if kw.get("tools") and self.tool and not any(m.get("role") == "tool" for m in messages):
            name, args = self.tool
            return LLMResponse("", "tool_calls", [LLMToolCall("t1", name, args)],
                               LLMUsage(prompt_tokens=10, completion_tokens=2, model="m"))
        row_type = kw["system"].split("## Output: `", 1)[1].split("`", 1)[0]
        user = messages[0]["content"]
        self.n += 1
        sentence = {"key": f"s{self.n}",
                    "text": f"Position number {self.n} holds that the subject shows a steady state",
                    "support": [{"ref_id": "row-0", "quote": "Synthetic source 0 reports"}]}
        if row_type == "verdict/1":
            cands = user.split("## The candidates you rule on\n", 1)[-1].split("\n## ")[0]
            cands = [c for c in cands.splitlines() if c.strip()] if "candidates you rule" in user \
                else [user.split("## The candidate you rule on\n", 1)[1].splitlines()[0]]
            body: dict[str, Any] = {"verdicts": [{"candidate": c, "outcome": "stands",
                                                  "reason": "supported"} for c in cands],
                                    "ranking": cands}
        else:
            body = {"sentences": [sentence]}
        # the house fold: full-width brackets in a reply are folded, and an
        # undeclared key is dropped (and listed), not fatal
        text = json.dumps({**body, "commentary": "extra"}).replace("Position", "Position 【1】")
        return LLMResponse(text, "stop", [], LLMUsage(prompt_tokens=100, completion_tokens=20,
                                                      reasoning_tokens=7, model="m"))


def _code(call: SeatCall):
    return SyntheticSeats()(call)


def _spec(raw):
    spec, refusals = parse_spec(raw)
    assert spec is not None, refusals
    return spec


@pytest.fixture()
def loop():
    lt = LoopThread()
    yield lt
    lt.close()


def _run(raw, tmp_path: Path, models: ModelSeats):
    write_cards(tmp_path)

    def provider(call):
        return _code(call) if call.seat.kind == "code" else models(call)

    store = MemoryStore()
    it = Interpreter(_spec(raw), store, Dispatch(provider), inputs=INPUTS, run_id="r1",
                     clock=FrozenClock(), card_root=tmp_path)
    return it.run(), store


# --------------------------------------------------------------------------
# Rendering and reading
# --------------------------------------------------------------------------


def _call(**kw) -> SeatCall:
    spec = _spec(base_spec())
    seat = spec.seats["a"]
    base = dict(run_id="r", seat_id="a", seat=seat, row_type="position/1", attempt=0, ordinal=0,
                path="main/open", scope={}, inputs=(
                    Handed("input.question", "input.question", None, None, "Will it happen?"),
                    Handed("freeze", "main/freeze#0", "freeze", "frozen_scope/1",
                           {"subject": "s", "as_of": "x",
                            "rows": [{"id": "row-0", "text": "Synthetic source 0 reports"}]})),
                card="cards/reader.md", card_text="# Reader\nRead the rows.")
    base.update(kw)
    return SeatCall(**base)


def test_prompt_is_the_card_the_contract_and_the_handed_inputs_only():
    system, user = render_prompt(_call())
    assert system.startswith("# Reader\nRead the rows.")
    assert "## Output: `position/1`" in system and '"sentences"' in system
    assert "Will it happen?" in user and "row-0" in user and "main/freeze#0" in user
    # nothing else: a field the store did not hand is not in the prompt
    other = _call(inputs=(Handed("input.question", "input.question", None, None, "Q?"),))
    s2, u2 = render_prompt(other)
    assert "row-0" not in s2 + u2 and "main/freeze#0" not in u2


def test_every_row_type_renders_a_contract():
    for rt in ROW_TYPES:
        assert f"`{rt}`" in output_contract(rt)


def test_reply_is_folded_unwrapped_and_cut_to_the_row_type():
    payload, dropped = parse_reply(
        "position/1",
        '```json\n{"position": {"sentences": [{"key": "k", "text": "A 【2】 b",'
        ' "confidence": 0.3}], "extra": 1}}\n```')
    assert payload == {"sentences": [{"key": "k", "text": "A [2] b"}]}
    assert sorted(dropped) == ["extra", "sentences[0].confidence"]
    parse_row("position/1", payload)
    with pytest.raises(SeatFailure) as exc:
        parse_reply("position/1", "no object here")
    assert exc.value.raw == "no object here"


# --------------------------------------------------------------------------
# One clean call per attempt: thinking, temperature, plane, no max_tokens
# --------------------------------------------------------------------------


def test_model_and_judge_seats_run_a_round_end_to_end(loop, tmp_path):
    raw = mutate(base_spec())
    raw["seats"]["editor"]["thinking"] = "on"
    core, judge = FakeLLM(), FakeLLM()
    models = ModelSeats(planes={PRIMARY: core, SECOND: judge}, run=loop.run)
    rec, _ = _run(raw, tmp_path, models)
    assert rec["run"]["state"] == "ended", rec["run"]
    for c in core.calls + judge.calls:
        assert c["temperature"] == HOUSE_TEMPERATURE
        assert "max_tokens" not in c
        assert len(c["messages"]) == 1  # one clean context per attempt
    assert [c.get("thinking") for c in core.calls] == [False, False, False, True]
    assert all("thinking" not in c for c in judge.calls)  # judges declare none
    sealed = [s for s in rec["steps"] if s["status"] == "sealed" and s["seat"] in ("a", "editor")]
    assert sealed and all(s["released"] for s in sealed)
    a = next(s for s in sealed if s["seat"] == "a")
    assert a["call"]["calls"][0]["reasoning_tokens"] == 7
    assert a["call"]["dropped_keys"] == ["commentary"]
    assert "[1]" in a["payload"]["sentences"][0]["text"]
    assert rec["tallies"] and rec["tallies"][0]["outcome"]


def test_unknown_plane_and_interactive_context_are_refused(loop):
    models = ModelSeats(planes={}, run=loop.run)
    with pytest.raises(SeatFailure, match="plane_not_built"):
        models(_call())
    from legba.data.stack.llm import call_gate

    models = ModelSeats(planes={PRIMARY: FakeLLM()}, run=loop.run)

    async def inside():
        with call_gate.interactive_calls():
            return await models._attempt(_call())

    with pytest.raises(SeatFailure, match="scheduled class"):
        asyncio.run(inside())


def test_the_call_cap_refuses_before_sending(loop):
    llm = FakeLLM()
    models = ModelSeats(planes={PRIMARY: llm}, run=loop.run, take_call=lambda: False)
    with pytest.raises(CallCapReached):
        models(_call())
    assert llm.calls == []


def test_declared_tools_without_a_binding_fail_loudly(loop):
    raw = mutate(base_spec())
    raw["seats"]["a"]["tools"] = ["substrate_read"]
    spec = _spec(raw)
    models = ModelSeats(planes={PRIMARY: FakeLLM()}, run=loop.run)
    with pytest.raises(SeatFailure, match="tools_not_bound"):
        models(_call(seat=spec.seats["a"]))


# --------------------------------------------------------------------------
# Tools: the cut, refusals, and blindness through tools
# --------------------------------------------------------------------------


def _tools(dispatch, exclusions=lambda: Exclusions()) -> RoundTools:
    return RoundTools(dispatch=dispatch, known_at=AS_OF,
                      packs={"substrate_read": ("search_signals", "query_facts", "list_targets")},
                      classified={"search_signals": "cut", "list_targets": "exempt"},
                      exclusions=exclusions)


def test_unclassified_tools_are_refused_and_the_cut_is_the_runs():
    seen: list[tuple[str, Any, Any]] = []

    async def dispatch(name, args, known_at):
        seen.append((name, dict(args), known_at))
        return {"rows": [{"id": "sig-1", "title": "A title of a row"}]}

    tools = _tools(dispatch)
    seat = _spec(mutate(base_spec()) | {}).seats["a"].model_copy(update={"tools": ("substrate_read",)})
    assert tools.tools_for(seat) == ("search_signals", "list_targets")
    call = _call(seat=seat)
    out, rows, entry = asyncio.run(tools.call(call, "query_facts", {}))
    assert entry["refused"] == "no_known_at_cut" and "error" in out and not seen
    out, rows, entry = asyncio.run(tools.call(call, "inspect_entity", {}))
    assert entry["refused"] == "no_known_at_cut"
    out, rows, entry = asyncio.run(tools.call(
        call, "search_signals", {"query": "x", "known_at": "2030-01-01", "scope_predicate": "p"}))
    assert seen == [("search_signals", {"query": "x"}, AS_OF)]
    assert rows == [{"id": "sig-1", "kind": "signal", "text": "A title of a row",
                     "title": "A title of a row"}]


def test_filter_output_drops_run_rows_by_id_and_by_text():
    excl = Exclusions(frozenset({"main/open/a#0"}),
                      ("position number 1 holds that the subject shows a steady state",))
    out, dropped = filter_output({"rows": [
        {"id": "main/open/a#0", "text": "x"},
        {"id": "sig-2", "text": "A copy: Position number 1 holds that the subject shows a "
                                "steady state, it said."},
        {"id": "sig-3", "text": "an unrelated row"}], "refs": ["main/open/a#0", "sig-3"]}, excl)
    assert dropped == 3
    assert out == {"rows": [{"id": "sig-3", "text": "an unrelated row"}], "refs": ["sig-3"]}
    single, n = filter_output({"id": "main/open/a#0", "body": "x"}, excl)
    assert n == 1 and "error" in single


def test_a_blind_seats_tools_cannot_see_a_siblings_output(loop, tmp_path):
    """Seats a, b, c are blind siblings with tools. The dispatcher plants, in
    every result, a row carrying sibling a's receipt id and a row quoting a's
    sentence (as a host that persisted round rows where a reader can reach them
    would). Seat c's model never sees either; the legitimate row comes through."""
    raw = mutate(base_spec())
    raw["seats"]["a"]["tools"] = ["substrate_read"]
    store_ref: dict[str, Any] = {}

    async def dispatch(name, args, known_at):
        assert known_at == AS_OF
        return {"rows": [
            {"id": "main/open/a#0", "text": "the planted sibling receipt"},
            {"id": "sig-planted", "text": "Echo: Position [1] number 1 holds that the subject "
                                          "shows a steady state, said the sibling."},
            {"id": "row-0", "text": "Synthetic source 0 reports that the subject subject-x"}]}

    tools = _tools(dispatch, lambda: exclusions_from_receipts(store_ref["store"].receipts()))
    llm = FakeLLM(tool=("search_signals", {"query": "subject"}))
    models = ModelSeats(planes={PRIMARY: llm, SECOND: FakeLLM()}, run=loop.run, tools=tools,
                        max_tool_rounds=1)
    write_cards(tmp_path)
    store = MemoryStore()
    store_ref["store"] = store

    def provider(call):
        return _code(call) if call.seat.kind == "code" else models(call)

    it = Interpreter(_spec(raw), store, Dispatch(provider), inputs=INPUTS, run_id="r1",
                     clock=FrozenClock(), card_root=tmp_path)
    rec = it.run()
    assert rec["run"]["state"] == "ended"
    tool_msgs = [m for c in llm.calls for m in c["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 3  # a, b and c each made one tool call
    c_sees = tool_msgs[2]["content"]
    assert "main/open/a#0" not in c_sees and "sig-planted" not in c_sees
    assert "Position number 1" not in c_sees and "row-0" in c_sees
    c_log = [e for e in tools.log if e["seat"] == "c"]
    assert c_log[0]["dropped_run_rows"] >= 2
    c_step = next(s for s in rec["steps"] if s["seat"] == "c" and s["status"] == "sealed")
    assert c_step["tools_bound"] is True
    assert any(h["read"] == "tool" and h["rows"] == ["row-0"] for h in c_step["handed"])


def test_rows_from_output_reads_a_document_body():
    rows = rows_from_output({"doc_id": "d1", "title": "T", "body": "The body text."})
    assert rows == [{"id": "d1", "kind": "signal", "text": "The body text. T", "title": "T"}]


# --------------------------------------------------------------------------
# Substrate code seats: read-only, cut, typed
# --------------------------------------------------------------------------


class FakeRecord:
    def __init__(self) -> None:
        self.sql: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        self.sql.append((sql, params))
        if "FROM target_descriptors" in sql:
            return [{"name": "Testland", "body": {"scope": {"geo": ["TL"]},
                                                  "sources": [{"source_id": "src-a"}]}}]
        if "FROM observations" in sql:
            return [{"id": f"o{i}", "series_id": "gdp", "subject": "TL", "unit": "usd",
                     "valid_from": None, "value": 1.0 + i} for i in range(3)]
        if "FROM signals" in sql and "ANY($1::uuid[])" in sql:
            return [{"id": "00000000-0000-0000-0000-000000000001", "source_id": "src-a",
                     "access_class": "public", "title": "Testland report",
                     "text": "Testland reports that the port reopened on the morning"}]
        if "FROM signals" in sql:
            t = AS_OF - timedelta(hours=1)
            return [{"id": f"00000000-0000-0000-0000-00000000000{i}", "source_id": f"src-{i % 2}",
                     "fetched_at": t - timedelta(hours=i), "created_at": t, "salience":
                     {"magnitude": i / 10}, "access_class": "public", "title": f"Title {i}",
                     "title_en": None, "origin_group_id": None, "published_at": None,
                     "geo_fallback": None, "text": f"Testland row {i} text"} for i in range(1, 6)]
        return []


async def _list_sources(name, args, known_at):
    assert name == "list_sources" and known_at == AS_OF
    return {"rows": [{"source_id": "src-a", "staleness": "dead", "last_poll_outcome": "error",
                      "last_signal_at": None},
                     {"source_id": "src-z", "staleness": "dead", "last_poll_outcome": None}]}


def _code_call(fn: str, inputs: tuple[Handed, ...]) -> SeatCall:
    raw = mutate(base_spec())
    raw["seats"]["freeze"]["fn"] = fn
    seat = _spec(raw).seats["freeze"]
    return SeatCall("r", "x", seat, seat.output, 0, 0, "main", {}, inputs)


def test_the_substrate_code_seats_read_with_the_cut_and_return_typed_rows(loop):
    rec = FakeRecord()
    subs = SubstrateSeats(fetch=rec.fetch, run=loop.run, as_of=AS_OF, subject="tl",
                          dispatch=_list_sources, required=2)
    inputs = (Handed("input.subject", "input.subject", None, None, "tl"),
              Handed("input.as_of", "input.as_of", None, None, AS_OF.isoformat()))
    scope = subs(_code_call("scope_and_freeze", inputs)).payload
    parse_row("frozen_scope/1", scope)
    assert [r.get("required", False) for r in scope["rows"]] == [True, True, False, False, False]
    freeze_sql, params = next((s, p) for s, p in rec.sql if "FROM signals" in s)
    assert "created_at < $" in freeze_sql and "fetched_at < $" in freeze_sql
    assert "source_id = ANY(" in freeze_sql and AS_OF in params
    assert AS_OF - timedelta(hours=168) in params
    handed = (Handed("freeze", "k", "freeze", "frozen_scope/1", scope),)
    packet = subs(_code_call("core_packet_and_slices", handed)).payload
    parse_row("seat_slices/1", packet)
    assert packet["core"] and len(packet["core"]) <= 24
    panel = subs(_code_call("series_panel", handed)).payload
    parse_row("series_panel/1", panel)
    assert panel["series"][0]["values"] == [1.0, 2.0, 3.0]
    gaps = subs(_code_call("coverage_audit", handed)).payload
    parse_row("blind_spots/1", gaps)
    assert [g["what"] for g in gaps["absences"]] == ["src-a"]
    ruling = {"sentences": [{"key": "k", "text": "In Testland the port reopened.",
                             "support": [{"ref_id": "00000000-0000-0000-0000-000000000001",
                                          "quote": "the port reopened"}]}]}
    shadow = subs(_code_call("statement_gate_shadow", (
        Handed("adjudicator", "k2", "adjudicator", "ruling/1", ruling),
        Handed("verify.tally", "k3", None, "tally/1", {"outcome": "stands"})))).payload
    parse_row("statement_proposals/1", shadow)
    gate = shadow["proposals"][0]["gate"]
    assert gate["shadow"] is True and gate["verifier"] == "stands"
    assert isinstance(gate["passed"], bool)
    # read-only: every statement the seats sent is a SELECT
    assert rec.sql and all(s.lstrip().upper().startswith("SELECT") for s, _ in rec.sql)
