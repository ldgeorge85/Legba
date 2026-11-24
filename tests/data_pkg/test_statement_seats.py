# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S3 — the typing and verifier seats (``data/statement_typing.py``,
``data/statement_verifier.py``, ``data/statement_seats.py``) and their two
descriptors. Fakes only: no model, no database, no network."""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from legba.data import statement_seats as seats
from legba.data.statement_seat_canary import CANARY
from legba.data.statement_sentences import CitedRef, CitedSentence
from legba.data.statement_shapes import HandedSlice, SupportRow
from legba.data.statement_typing import (
    EvidenceRef,
    StandingCandidate,
    SubjectCandidate,
    TypingInput,
    assemble,
    build_typing_prompt,
    evidence_window,
    parse_valid_from,
    same_subject_candidates,
    tally_refusals,
    type_sentence,
)
from legba.data.statement_verifier import (
    DOES_NOT_SUPPORT,
    PARTLY,
    SUPPORTS,
    UNCLEAR,
    build_verifier_prompt,
    read_verdict,
    verify_support,
)
from legba.shared.sampling import HOUSE_TEMPERATURE

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "legba" / "data"


def run(coro):
    import asyncio

    return asyncio.run(coro)


@dataclass
class _Usage:
    prompt_tokens: int = 10
    completion_tokens: int = 5
    model: str = "fast"


@dataclass
class _Resp:
    content: str
    usage: _Usage = field(default_factory=_Usage)


class FakeLLM:
    """Returns queued replies; records exactly what each call was sent."""

    def __init__(self, *replies: str | Exception) -> None:
        self.replies = list(replies)
        self.sent: list[dict[str, Any]] = []

    async def chat_complete(self, messages, **kwargs):
        self.sent.append({"messages": messages, **kwargs})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return _Resp(reply)


# ---------------------------------------------------------------------------
# A typing input built from canary item c05 (an event with a number + date)
# ---------------------------------------------------------------------------

C05 = next(c for c in CANARY if c.item_id == "c05")
SIGNAL_ID = C05.support[0].ref_id
TEXT = C05.texts.by_signal[SIGNAL_ID]
PUBLISHED = C05.support[0].published_at


def _input(standing: tuple[StandingCandidate, ...] = ()) -> TypingInput:
    sentence = CitedSentence(
        0, C05.statement.text.rstrip(".") + " [2].", C05.statement.text,
        (CitedRef(2, "signal", SIGNAL_ID, "src-b", "Brennick: briefing", True),),
    )
    ev = EvidenceRef(2, "signal", SIGNAL_ID, TEXT, published_at=PUBLISHED,
                     title="Brennick: briefing", source_id="src-b", access_class="public")
    subjects = (SubjectCandidate("target", "unit_brennick", ("Brennick",)),
                SubjectCandidate("target", "unit_other", ("Elsewhere",)))
    return TypingInput(sentence, (ev,), subjects, standing)


def _reply(**over: Any) -> dict[str, Any]:
    base = {
        "kind": "event", "subject": "S1", "valid_from": "2026-05-09",
        "valid_from_precision": "day", "slots": {},
        "support": [{"row": 2, "quote": C05.support[0].quote}],
        "relation": {"action": "new", "candidate": None},
    }
    base.update(over)
    return base


HANDED = HandedSlice.of([SIGNAL_ID], "run-1")


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


def _facts(**over: Any) -> seats.RouteFacts:
    base = dict(component_id="llm.primary.openai_compat", subprovider="vllm", model_name="fast")
    base.update(over)
    return seats.RouteFacts(**base)


def test_core_route_is_accepted_and_reported():
    out = seats.assert_core_route(_facts())
    assert out["component_unpriced"] and out["model_not_forbidden_alias"]


@pytest.mark.parametrize("over", [
    {"component_id": "llm.anthropic.opus"},
    {"component_id": "llm.audit.openrouter_llama33_70b.openai_compat"},
    {"subprovider": "anthropic"},
    {"model_name": "deep"},
    {"model_name": " DEEP "},
    {"component_price": object()},
    {"price_table_entries": 2},
])
def test_anything_but_the_unpriced_core_route_is_refused(over):
    with pytest.raises(seats.CoreRouteError):
        seats.assert_core_route(_facts(**over))


# ---------------------------------------------------------------------------
# Calling and reading
# ---------------------------------------------------------------------------


def test_a_seat_call_is_one_clean_message_at_house_temperature_without_max_tokens():
    llm = FakeLLM('{"ok": 1}')
    content, call = run(seats.call_seat(llm, seat="s", system_prompt="SYS", user_content="U"))
    assert content == '{"ok": 1}' and call.ok and call.prompt_tokens == 10
    sent = llm.sent[0]
    assert sent["messages"] == [{"role": "user", "content": "U"}]
    assert sent["system"] == "SYS"
    assert sent["temperature"] == HOUSE_TEMPERATURE == 1.0
    assert "max_tokens" not in sent


def test_the_call_budget_refuses_before_sending():
    budget = seats.CallBudget(limit=1)
    llm = FakeLLM("{}", "{}")
    run(seats.call_seat(llm, seat="s", system_prompt="S", user_content="U", budget=budget))
    content, call = run(seats.call_seat(llm, seat="s", system_prompt="S", user_content="U", budget=budget))
    assert content is None and call.error == "call_budget_exhausted"
    assert len(llm.sent) == 1 and budget.remaining == 0


def test_a_transport_error_degrades_to_no_reply():
    content, call = run(seats.call_seat(FakeLLM(RuntimeError("down")), seat="s",
                                        system_prompt="S", user_content="U"))
    assert content is None and call.error == "call_failed:RuntimeError"


@pytest.mark.parametrize("raw,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('Here it is: {"a": "x [3]"} done', {"a": "x [3]"}),
    ('{"a": "x 【3】"}', {"a": "x [3]"}),
    ("not json", None),
    ("[1, 2]", None),
    ("", None),
])
def test_reply_parsing_folds_fullwidth_markers_and_never_guesses(raw, expected):
    assert seats.parse_reply_object(raw) == expected


# ---------------------------------------------------------------------------
# Typing — code decides
# ---------------------------------------------------------------------------


def test_a_good_reply_becomes_a_draft_that_passes_every_code_gate():
    res = assemble(_input(), _reply(), handed=HANDED)
    assert res.passed, [f.reason for f in res.gate.failures]
    assert res.draft.text == C05.statement.text          # the desk's sentence, unrewritten
    assert (res.draft.kind, res.draft.subject_key) == ("event", "unit_brennick")
    assert res.draft.valid_from == datetime(2026, 5, 9, tzinfo=timezone.utc)
    row = res.support[0]
    assert (row.ref_id, row.role, row.support_kind, row.published_at) == (SIGNAL_ID, "evidence", "signal", PUBLISHED)


def test_the_model_cannot_rewrite_the_statement_text():
    res = assemble(_input(), _reply(text="Floods displaced 9,999 residents."), handed=HANDED)
    assert res.draft.text == C05.statement.text


def test_a_subject_outside_the_offered_list_is_refused():
    for subject in ("S9", "unit_brennick", None, "Brennick"):
        res = assemble(_input(), _reply(subject=subject), handed=HANDED)
        assert res.draft is None and "subject_not_offered" in res.refusals and not res.passed


def test_a_quote_naming_a_row_the_sentence_did_not_cite_is_dropped_and_counted():
    res = assemble(_input(), _reply(support=[{"row": 7, "quote": "anything at all here"}]),
                   handed=HANDED)
    assert {"row_not_on_sentence", "no_support"} <= set(res.refusals)
    assert not res.passed


def test_a_fabricated_quote_fails_the_quote_gate():
    res = assemble(_input(), _reply(support=[{"row": 2, "quote": "floods displaced residents across the valley"}]),
                   handed=HANDED)
    assert not res.refusals and not res.passed
    assert ("G1", "quote_absent") in {(f.gate, f.reason) for f in res.gate.failures}


def test_a_valid_from_after_publication_fails_g2():
    res = assemble(_input(), _reply(valid_from="2026-06-01"), handed=HANDED)
    assert ("G2", "valid_from_after_publication") in {(f.gate, f.reason) for f in res.gate.failures}


def test_closed_vocabularies_refuse_by_name():
    res = assemble(_input(), _reply(kind="fact", valid_from="May 9th",
                                    relation={"action": "merges"}, slots={"value": [1]}),
                   handed=HANDED)
    assert {"kind_unknown", "valid_from_unparseable", "relation_unknown", "slot_not_scalar"} <= set(res.refusals)
    assert assemble(_input(), None, handed=HANDED).refusals == ("reply_unparseable",)


def test_a_relation_must_name_an_offered_candidate():
    cand = StandingCandidate("st-1", "event", "Floods displaced residents.", "target", "unit_brennick")
    ok = assemble(_input((cand,)), _reply(relation={"action": "restates", "candidate": "C1"}), handed=HANDED)
    assert ok.relation == "restates" and ok.relation_candidate == "st-1"
    bad = assemble(_input((cand,)), _reply(relation={"action": "revises", "candidate": "C4"}), handed=HANDED)
    assert "candidate_not_offered" in bad.refusals


def test_a_numeric_string_value_slot_is_checked_as_a_number():
    res = assemble(_input(), _reply(kind="level", slots={"value": "1,300"}), handed=HANDED)
    assert ("G2", "number_not_in_evidence") in {(f.gate, f.reason) for f in res.gate.failures}


@pytest.mark.parametrize("raw,prec,out", [
    ("2026-05-09", "day", (datetime(2026, 5, 9, tzinfo=timezone.utc), "day", True)),
    ("2026-05", None, (datetime(2026, 5, 1, tzinfo=timezone.utc), "month", True)),
    (None, None, (None, "unknown", True)),
    ("2026-05-09", "unknown", (None, "unknown", True)),
    ("2026-13-01", "day", (None, "unknown", False)),
    ("last week", "day", (None, "unknown", False)),
])
def test_valid_from_parsing(raw, prec, out):
    assert parse_valid_from(raw, prec) == out


def test_type_sentence_end_to_end_with_a_fake_model():
    llm = FakeLLM(json.dumps(_reply()))
    res = run(type_sentence(llm, _input(), system_prompt="SYS", handed=HANDED))
    assert res.passed and res.call.ok
    user = llm.sent[0]["messages"][0]["content"]
    assert "S1 · target · Brennick" in user and "ROW 2" in user and C05.support[0].quote in user


def test_type_sentence_with_no_reply_is_a_refusal_not_a_draft():
    res = run(type_sentence(FakeLLM(RuntimeError("x")), _input(), system_prompt="S", handed=HANDED))
    assert res.draft is None and res.refusals == ("no_call",)
    assert tally_refusals([res, res]) == {"no_call": 2}


def test_candidates_are_found_by_code_on_the_same_subject_only():
    mk = lambda i, key, text: StandingCandidate(f"st-{i}", "event", text, "target", key)  # noqa: E731
    standing = [mk(1, "unit_brennick", "Floods in Brennick displaced residents"),
                mk(2, "unit_other", "Floods in Brennick displaced residents"),
                mk(3, "unit_brennick", "A bridge reopened"),
                *[mk(10 + i, "unit_brennick", f"Unrelated item {i}") for i in range(8)]]
    out = same_subject_candidates("target", "unit_brennick", C05.statement.text, standing)
    assert out[0].statement_id == "st-1" and len(out) == 5
    assert all(c.subject_key == "unit_brennick" for c in out)


def test_the_evidence_window_is_the_stretch_sharing_the_sentence_words():
    text = ("filler words only here. " * 300) + "Floods in Brennick displaced 1,200 residents. " + ("tail. " * 300)
    window = evidence_window(text, "Floods in Brennick displaced residents", chars=600)
    assert "Brennick" in window and len(window) == 600
    assert evidence_window("short", "x") == "short"


def test_the_typing_prompt_carries_only_what_code_offers():
    prompt = build_typing_prompt(_input())
    assert "[2]" not in prompt.split("SUBJECTS")[0]     # markers stripped from the sentence
    assert "(none)" in prompt                              # no standing candidates offered


# ---------------------------------------------------------------------------
# Verifier — code first, then a clean context
# ---------------------------------------------------------------------------


def test_code_refuses_before_the_model_is_asked():
    llm = FakeLLM()
    bad = SupportRow(**{**C05.support[0].__dict__, "quote": "a quote that is not in the text"})
    rep = run(verify_support(llm, C05.statement, (bad,), texts=C05.texts, system_prompt="S"))
    assert (rep.verdict, rep.decided_by) == (DOES_NOT_SUPPORT, "code")
    assert any(f.startswith("G1.quote:quote_absent") for f in rep.code_failures)
    assert llm.sent == [] and not rep.establishes


def test_the_model_sees_only_the_statement_and_its_quotes():
    llm = FakeLLM('{"verdict": "supports", "reason_code": "fully_stated"}')
    rep = run(verify_support(llm, C05.statement, C05.support, texts=C05.texts, system_prompt="S"))
    assert rep.establishes and rep.decided_by == "model"
    user = llm.sent[0]["messages"][0]["content"]
    assert C05.support[0].quote in user and "KIND: event" in user and "VALID FROM: 2026-05-09" in user
    for hidden in ("briefing", "canary-source", SIGNAL_ID, "Officials confirmed"):
        assert hidden not in user
    assert user == build_verifier_prompt(C05.statement, C05.support)


@pytest.mark.parametrize("reply,verdict,reason", [
    ('{"verdict": "partly", "reason_code": "beyond_quote"}', PARTLY, "beyond_quote"),
    ('{"verdict": "does not support", "reason_code": "zzz"}', DOES_NOT_SUPPORT, "other"),
    ('{"verdict": "Supports"}', SUPPORTS, None),
    ('{"verdict": "maybe"}', UNCLEAR, None),
])
def test_verdict_reading(reply, verdict, reason):
    assert read_verdict(seats.parse_reply_object(reply)) == (verdict, reason)


def test_an_unreadable_reply_establishes_nothing():
    rep = run(verify_support(FakeLLM("I think so"), C05.statement, C05.support,
                             texts=C05.texts, system_prompt="S"))
    assert (rep.verdict, rep.decided_by, rep.establishes) == (UNCLEAR, "unparsed", False)


def test_measurement_mode_asks_the_model_even_when_code_refused_but_never_establishes():
    llm = FakeLLM('{"verdict": "supports"}')
    bad = SupportRow(**{**C05.support[0].__dict__, "quote": "a quote that is not in the text"})
    rep = run(verify_support(llm, C05.statement, (bad,), texts=C05.texts, system_prompt="S",
                             code_first=False))
    assert rep.verdict == SUPPORTS and rep.code_failures and not rep.establishes
    assert len(llm.sent) == 1


# ---------------------------------------------------------------------------
# Descriptors, prompts, route, isolation
# ---------------------------------------------------------------------------

SEAT_FILES = {
    seats.TYPING_SEAT_ID: REPO / "descriptors" / "analyst_statement_typing_seat.yaml",
    seats.VERIFIER_SEAT_ID: REPO / "descriptors" / "analyst_statement_verifier_seat.yaml",
}


@pytest.mark.parametrize("seat_id", sorted(SEAT_FILES))
def test_seat_descriptors_parse_and_hold_the_house_rules(seat_id):
    from legba.data.schemas.analyst import AnalystDescriptor

    body = yaml.safe_load(SEAT_FILES[seat_id].read_text())
    desc = AnalystDescriptor.model_validate_json(json.dumps(body))
    assert desc.identity.id == seat_id
    assert body["identity"]["state"] == "draft"                       # never scheduled by a register
    llm = body["method"]["llm"]
    assert llm["primary"]["raw"] == seats.SEAT_COMPONENT
    assert llm["temperature"] == 1.0 and "max_tokens" not in llm
    assert body["method"]["budget_tokens_per_day"] == 0
    assert body["cadence"] == {}                                       # called, never fired
    assert body["method"]["system_prompt"].strip()


@pytest.mark.parametrize("seat_id", sorted(SEAT_FILES))
def test_seat_prompts_are_in_the_manifest(seat_id):
    import importlib.util

    spec = importlib.util.spec_from_file_location("gen_manifest", REPO / "scripts" / "gen_descriptor_prompt_manifest.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    from legba.data.registry.descriptor_prompt_manifest import load_manifest

    manifest = load_manifest()
    prompt = yaml.safe_load(SEAT_FILES[seat_id].read_text())["method"]["system_prompt"]
    assert manifest["prompts"][seat_id]["sha256"] == gen.prompt_hash(prompt)


@pytest.mark.parametrize("module", ["statement_seats.py", "statement_typing.py",
                                    "statement_verifier.py", "statement_planted.py",
                                    "statement_seat_canary.py", "statement_piece_typing.py",
                                    "statement_actor_subjects.py"])
def test_seat_modules_reach_no_store_registry_or_network(module):
    """G11's permission split at the seats: nothing here can write a row, read
    a descriptor, or open a socket — the model's reply only becomes a value."""
    tree = ast.parse((SRC / module).read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(("." * node.level) + (node.module or ""))
    banned = ("asyncpg", "httpx", "psycopg", "registry", "postgres", "stack", "analysts", "runtime", "requests")
    assert not [n for n in names if any(b in n for b in banned)], names
