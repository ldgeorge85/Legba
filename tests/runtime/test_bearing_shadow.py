# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""the bearing gate's decision-model SHADOW.

Driven directly (no DB, no network): the real :func:`run_bearing_pipeline`
with a scripted chat gate, then the real :func:`run_bearing_shadow` with a
scripted decider (or the REAL decision client over a mock transport) and a
recording pool. Pins:

  * the gate's verdicts, kept edges, stamps and ``bearing_*`` counters are
    byte-identical with the shadow ON and OFF;
  * one ledger row per edge the gate ASKED (deferred / unbuilt-gate edges are
    not asked, so not shadowed), its state the gate's own row text;
  * every shadow failure — a raising decider, a timeout, a client that will
    not build, a pool that will not write — is counted, never raised;
  * the cap, the breaker, the receipt leg, the migration and the report.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from legba.data import run_accounting
from legba.data.analysts.deterministic_handlers import bearing_gate as bg
from legba.data.analysts.deterministic_handlers import bearing_shadow as bs

_REPO = Path(__file__).resolve().parents[2]
_MIGRATIONS = _REPO / "src" / "legba" / "data" / "migrations"
_SHADOW_REF = "llm.decide.gateway_typed_choice"
_GATE_REF = "llm.verify.gateway_judge.openai_compat"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


class _Gate:
    """Scripted chat gate keyed on the signal text, so the verdict per edge is
    independent of call order."""

    def __init__(self, verdicts: dict[str, Any]) -> None:
        self.verdicts = verdicts
        self.rows: list[str] = []

    async def chat_complete(self, messages, **kwargs):
        row = messages[-1]["content"]
        self.rows.append(row)
        for signal, reply in self.verdicts.items():
            if f'News item: "{signal}"' in row:
                if isinstance(reply, Exception):
                    raise reply
                return _Resp(reply)
        return _Resp("YES")


class _Decider:
    def __init__(self, choice_by_signal: dict[str, Any] | None = None, *, default: Any = "yes"):
        self.choice_by_signal = choice_by_signal or {}
        self.default = default
        self.calls: list[dict[str, Any]] = []

    async def decide(self, state, questions, *, leg=None, max_retries=None, model=None):
        self.calls.append({"state": state, "questions": questions, "leg": leg,
                           "max_retries": max_retries})
        choice = self.default
        for signal, c in self.choice_by_signal.items():
            if f'News item: "{signal}"' in state:
                choice = c
        if isinstance(choice, BaseException):
            raise choice
        p_yes = 0.83 if choice == "yes" else 0.21
        return {"bears": {"type": "choice", "choice": choice,
                          "confidence": max(p_yes, 1 - p_yes),
                          "probabilities": {"yes": p_yes, "no": 1 - p_yes}}}


class _Conn:
    def __init__(self, sink: list, fail: bool) -> None:
        self.sink = sink
        self.fail = fail

    async def executemany(self, sql, args):
        if self.fail:
            raise RuntimeError("relation decision_shadow_ledger does not exist")
        self.sink.append((sql, list(args)))


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, *, fail: bool = False) -> None:
        self.writes: list[tuple[str, list]] = []
        self.fail = fail

    def acquire(self):
        return _Acquire(_Conn(self.writes, self.fail))

    @property
    def rows(self) -> list[tuple]:
        return [r for _, args in self.writes for r in args]


class _Deps:
    def __init__(self, **extras: Any) -> None:
        self.extras = dict(extras)


def _cand(signal: str, *, thesis: str = "Will X happen?", desk: str = "", target: str = "") -> bg.EdgeCandidate:
    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    return bg.EdgeCandidate(
        signal_id=uuid4(),
        signal_as_of=now,
        signal_text=signal,
        question_id=uuid4(),
        question_as_of=now,
        question_thesis=thesis,
        weight=0.5,
        planes=["entity"],
        desk_label=desk,
        target_id=target,
    )


def _batch() -> list[bg.EdgeCandidate]:
    return [
        _cand("s-yes", desk="G20 — Türkiye [country_g20_tr]", target="country_g20_tr"),
        _cand("s-no"),
        _cand("s-garbled"),
        _cand("s-boom"),
        _cand("s-yes-2", target="country_g20_au"),
    ]


_GATE_SCRIPT = {
    "s-yes": "YES",
    "s-no": "NO",
    "s-garbled": "the model rambled",
    "s-boom": RuntimeError("gate down"),
    "s-yes-2": "YES",
}


@pytest.fixture(autouse=True)
def _clear_caches():
    bg._GATE_CLIENT_CACHE.clear()
    bs._SHADOW_CLIENT_CACHE.clear()
    yield
    bg._GATE_CLIENT_CACHE.clear()
    bs._SHADOW_CLIENT_CACHE.clear()


async def _pipeline(cands, *, shadow_on: bool, gate_cap: int = 100, confirm=None):
    counters: dict[str, Any] = {}
    asked: list | None = [] if shadow_on else None
    extras = {bg.SLM_DEPS_EXTRA_KEY: _Gate(_GATE_SCRIPT)}
    if confirm is not None:
        extras[bg.CONFIRM_LLM_DEPS_EXTRA_KEY] = confirm
    kept = await bg.run_bearing_pipeline(
        cands,
        deps=_Deps(**extras),
        mode="on",
        gate_ref=_GATE_REF,
        gate_cap=gate_cap,
        confirm_cap=100,
        counters=counters,
        asked=asked,
    )
    return kept, counters, asked


def _snapshot(kept, counters) -> str:
    return json.dumps(
        {
            "kept": [(str(c.signal_text), c.gate, c.confirm, c.data_payload()) for c in kept],
            "counters": counters,
        },
        sort_keys=True,
        default=str,
    )


async def _shadow(asked, *, decider=None, pool=None, cap=100, ref=_SHADOW_REF, counters=None, deps=None):
    counters = {} if counters is None else counters
    pool = pool or _Pool()
    if deps is None:
        deps = _Deps(**({bs.SHADOW_DEPS_EXTRA_KEY: decider} if decider is not None else {}))
    rows = await bs.run_bearing_shadow(
        asked or [],
        deps=deps,
        pool=pool,
        shadow_ref=ref,
        shadow_cap=cap,
        gate_ref=_GATE_REF,
        run_id=str(uuid4()),
        analyst_id="claim_watch",
        target_id=None,
        counters=counters,
    )
    return rows, counters, pool


# ---------------------------------------------------------------------------
# The gate is byte-identical, on or off
# ---------------------------------------------------------------------------


async def test_the_gate_is_byte_identical_with_the_shadow_on_or_off():
    off_kept, off_counters, off_asked = await _pipeline(_batch(), shadow_on=False)
    on_kept, on_counters, on_asked = await _pipeline(_batch(), shadow_on=True)
    assert off_asked is None
    assert _snapshot(on_kept, on_counters) == _snapshot(off_kept, off_counters)

    # …and the shadow, run afterwards over the ON run, moves nothing either.
    before = _snapshot(on_kept, on_counters)
    payloads = [copy.deepcopy(c.data_payload()) for c in on_kept]
    shadow_counters = dict(on_counters)
    await _shadow(on_asked, decider=_Decider({"s-yes": "no"}), counters=shadow_counters)
    assert _snapshot(on_kept, on_counters) == before
    assert [c.data_payload() for c in on_kept] == payloads
    # The shadow only ADDS its own keys; every pre-existing one is untouched.
    assert {k: shadow_counters[k] for k in on_counters} == on_counters
    assert set(shadow_counters) - set(on_counters) == set(bs.shadow_counter_defaults(_SHADOW_REF))


async def test_off_asks_nothing_writes_nothing_and_leaves_the_receipt_alone():
    kept, counters, _ = await _pipeline(_batch(), shadow_on=True)
    decider = _Decider()
    snapshot = dict(counters)
    rows, out, pool = await _shadow([(c, "yes") for c in kept], decider=decider, ref=None,
                                    counters=counters)
    assert rows == [] and decider.calls == [] and pool.writes == []
    assert out == snapshot, "an OFF shadow adds nothing to the receipt"
    for ref in ("", "   "):
        rows, _, _ = await _shadow([(kept[0], "yes")], decider=decider, ref=ref)
        assert rows == [] and decider.calls == []


async def test_one_row_per_edge_the_gate_asked_with_the_gates_own_text():
    kept, counters, asked = await _pipeline(_batch(), shadow_on=True)
    # The gate asked all five (cap 100); the NO was dropped from `kept` but
    # is still in the asked population, and the two failures are 'unavailable'.
    assert [(c.signal_text, v) for c, v in asked] == [
        ("s-yes", "yes"), ("s-no", "no"), ("s-garbled", "unavailable"),
        ("s-boom", "unavailable"), ("s-yes-2", "yes"),
    ]
    decider = _Decider({"s-yes": "yes", "s-no": "yes", "s-yes-2": "no"})
    rows, out, pool = await _shadow(asked, decider=decider)

    assert len(rows) == 5 and len(pool.rows) == 5 and len(decider.calls) == 5
    calls_by_state = {c["state"]: c for c in decider.calls}
    for (cand, verdict), row in zip(asked, rows):
        state = bg.gate_row_text(cand)
        call = calls_by_state[state]
        assert call["leg"] == bs.SHADOW_LEG and call["max_retries"] == bs.SHADOW_MAX_RETRIES
        assert call["questions"] == bs.shadow_questions()
        assert row.gate_verdict == verdict
        assert row.state_sha256 == hashlib.sha256(state.encode()).hexdigest()
        assert row.edge_ref == f"signal:{cand.signal_id}->question:{cand.question_id}"
        assert row.shadow_component_id == _SHADOW_REF and row.gate_component_id == _GATE_REF

    # The state IS the row the gate's last user turn carried — desk line,
    # thesis, item — byte for byte (the gate fake recorded what it was sent).
    expected = 'Desk: G20 — Türkiye [country_g20_tr]\nQuestion thesis: "Will X happen?"\nNews item: "s-yes"'
    assert bg.gate_row_text(asked[0][0]) == expected
    assert expected in calls_by_state
    assert out["bearing_shadow_calls"] == 5
    assert out["bearing_shadow_answered"] == 5
    assert out["bearing_shadow_agree"] == 1  # s-yes agrees; s-no and s-yes-2 disagree
    assert out["bearing_shadow_rows"] == 5 and out["bearing_shadow_errors"] == 0

    # What reaches the ledger: the run ids, the leg, the desk (question first).
    by_ref = {r[4]: r for r in pool.rows}
    first = by_ref[rows[0].edge_ref]
    assert first[1] == "claim_watch" and first[2] == "country_g20_tr"
    assert first[5] == "bearing_shadow" and first[7] == "yes" and first[9] == "yes"
    assert first[10] == pytest.approx(0.83) and first[13] is None
    no_desk = by_ref[rows[1].edge_ref]
    assert no_desk[2] is None  # no question desk and no run target


async def test_the_shadow_question_is_one_choice_with_yes_no_criteria():
    q = bs.shadow_questions()
    assert list(q) == ["bears"]
    assert q["bears"]["type"] == "choice"
    assert q["bears"]["instructions"] == (
        "Does the new evidence bear on the question — would a careful analyst "
        "update the answer because of it?"
    )
    assert set(q["bears"]["criteria"]) == {"yes", "no"}
    assert all(v.strip() for v in q["bears"]["criteria"].values())


async def test_deferred_and_unbuilt_gate_edges_are_not_shadowed():
    kept, counters, asked = await _pipeline(_batch(), shadow_on=True, gate_cap=2)
    assert counters["bearing_gate_deferred"] == 3
    assert [c.signal_text for c, _ in asked] == ["s-yes", "s-no"]
    rows, _, _ = await _shadow(asked, decider=_Decider())
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Failures are counted, never raised
# ---------------------------------------------------------------------------


async def test_a_raising_decider_is_counted_and_ledgered_never_raised():
    from legba.data.stack.llm.base import TransientLLMFailure

    _, _, asked = await _pipeline(_batch(), shadow_on=True)
    decider = _Decider({"s-no": TransientLLMFailure("busy", status=429, retry_after=5.0),
                        "s-garbled": ValueError("nope")})
    rows, out, pool = await _shadow(asked, decider=decider)
    assert out["bearing_shadow_errors"] == 2
    assert out["bearing_shadow_answered"] == 3
    errs = sorted(r.shadow_error for r in rows if r.shadow_error)
    assert errs == ["TransientLLMFailure:429", "ValueError"]
    for r in rows:
        assert (r.shadow_choice is None) == (r.shadow_error is not None)  # the CHECK
    assert len(pool.rows) == 5


async def test_a_timeout_is_counted(monkeypatch):
    monkeypatch.setattr(bs, "SHADOW_TIMEOUT_SECONDS", 0.01)

    class _Slow(_Decider):
        async def decide(self, *a, **k):
            await asyncio.sleep(1)

    _, _, asked = await _pipeline(_batch()[:1], shadow_on=True)
    rows, out, _ = await _shadow(asked, decider=_Slow())
    assert [r.shadow_error for r in rows] == ["timeout"]
    assert out["bearing_shadow_errors"] == 1


async def test_the_breaker_stops_a_dead_shadow_after_five_failures(monkeypatch):
    monkeypatch.setattr(bs, "SHADOW_CONCURRENCY", 1)  # serial: the count is exact
    cands = [_cand(f"s{i}") for i in range(12)]
    asked = [(c, "yes") for c in cands]
    decider = _Decider(default=RuntimeError("gateway gone"))
    rows, out, pool = await _shadow(asked, decider=decider)
    assert len(decider.calls) == bs.SHADOW_BREAKER_FAILURES
    assert out["bearing_shadow_errors"] == 5
    assert out["bearing_shadow_skipped"] == 7
    assert [r.shadow_error for r in rows].count("breaker_open") == 7
    assert len(pool.rows) == 12


async def test_the_cap_defers_the_tail_without_rows():
    asked = [(_cand(f"s{i}"), "no") for i in range(5)]
    rows, out, pool = await _shadow(asked, decider=_Decider(default="no"), cap=3)
    assert len(rows) == 3 and len(pool.rows) == 3
    assert out["bearing_shadow_deferred"] == 2
    assert out["bearing_shadow_agree"] == 3
    rows, out, pool = await _shadow(asked, decider=_Decider(), cap=0)
    assert rows == [] and pool.writes == [] and out["bearing_shadow_deferred"] == 5


async def test_a_client_that_will_not_build_ledgers_every_edge_with_the_error(monkeypatch):
    async def _boom(ref):
        raise RuntimeError("stack-component not found in registry")

    monkeypatch.setattr(bs, "build_component_handler", _boom)
    asked = [(_cand(f"s{i}"), "yes") for i in range(3)]
    rows, out, pool = await _shadow(asked, deps=_Deps())
    assert [r.shadow_error for r in rows] == ["client_unavailable:RuntimeError"] * 3
    assert out["bearing_shadow_errors"] == 3 and out["bearing_shadow_calls"] == 0
    assert len(pool.rows) == 3
    assert bs._SHADOW_CLIENT_CACHE == {}, "a failure is never cached"


class _RecordingChat(_Gate):
    """A chat shadow that records the WHOLE call (messages + kwargs)."""

    def __init__(self, verdicts: dict[str, Any]) -> None:
        super().__init__(verdicts)
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return await super().chat_complete(messages, **kwargs)


async def test_a_chat_component_shadows_with_the_gates_own_prompt(monkeypatch):
    """the roles swapped: a chat component as the SHADOW is asked
    the chat gate's exact conversation and call, and its row carries the
    parsed yes/no with NULL p_yes and NULL confidence."""
    chat = _RecordingChat({"s-a": "YES", "s-b": "no — different desk"})

    async def _build(ref):
        return chat

    monkeypatch.setattr(bs, "build_component_handler", _build)
    a, b = _cand("s-a", desk="G20 — Türkiye [country_g20_tr]"), _cand("s-b")
    asked = [(a, "no"), (b, "no")]
    rows, out, pool = await _shadow(asked, deps=_Deps(), ref=_GATE_REF)

    # The exact call the chat GATE makes (one definition: gate_llm_call).
    for cand in (a, b):
        call = next(c for c in chat.calls if c["messages"][-1]["content"] == bg.gate_row_text(cand))
        assert call["messages"] == bg.gate_llm_messages(cand)
        assert call["messages"][0] == {"role": "system", "content": bg.GATE_SYSTEM_PROMPT}
        assert len(call["messages"]) == 2 + 2 * len(bg.GATE_FEWSHOT_TURNS)
        assert call["kwargs"] == {"max_tokens": bg.GATE_MAX_TOKENS, "temperature": 1.0}

    by_signal = {r.edge_ref: r for r in rows}
    ra, rb = by_signal[f"signal:{a.signal_id}->question:{a.question_id}"], \
        by_signal[f"signal:{b.signal_id}->question:{b.question_id}"]
    assert (ra.shadow_choice, ra.shadow_p_yes, ra.shadow_confidence, ra.shadow_error) == (
        "yes", None, None, None)
    assert (rb.shadow_choice, rb.shadow_p_yes, rb.shadow_confidence, rb.shadow_error) == (
        "no", None, None, None)
    assert ra.shadow_component_id == _GATE_REF  # the row names the chat model as the shadow
    assert out["bearing_shadow_answered"] == 2 and out["bearing_shadow_agree"] == 1
    assert out["bearing_shadow_rows"] == 2 and out["bearing_shadow_errors"] == 0
    for r in pool.rows:  # what reaches the ledger: choice set, p_yes/confidence NULL
        assert r[9] in ("yes", "no") and r[10] is None and r[11] is None and r[13] is None


async def test_an_unparseable_chat_shadow_reply_is_a_labelled_error_row():
    rows, out, pool = await _shadow(
        [(_cand("s-x"), "yes")],
        deps=_Deps(**{bs.SHADOW_DEPS_EXTRA_KEY: _Gate({"s-x": "maybe?"})}),
        ref=_GATE_REF,
    )
    assert rows[0].shadow_choice is None and rows[0].shadow_error == bs.SHADOW_UNPARSEABLE
    assert out["bearing_shadow_errors"] == 1 and len(pool.rows) == 1


async def test_a_chat_shadow_keeps_the_timeout_and_the_breaker(monkeypatch):
    monkeypatch.setattr(bs, "SHADOW_CONCURRENCY", 1)
    cands = [_cand(f"s{i}") for i in range(8)]
    gate = _Gate({f"s{i}": RuntimeError("down") for i in range(8)})
    rows, out, _ = await _shadow([(c, "yes") for c in cands],
                                 deps=_Deps(**{bs.SHADOW_DEPS_EXTRA_KEY: gate}), ref=_GATE_REF)
    assert len(gate.rows) == bs.SHADOW_BREAKER_FAILURES
    assert out["bearing_shadow_skipped"] == 3
    assert [r.shadow_error for r in rows].count("breaker_open") == 3


async def test_a_component_with_neither_surface_is_refused_as_a_shadow(monkeypatch):
    async def _neither(ref):
        return object()

    monkeypatch.setattr(bs, "build_component_handler", _neither)
    rows, out, _ = await _shadow([(_cand("s"), "yes")], deps=_Deps())
    assert rows[0].shadow_error == "client_unavailable:TypeError"


async def test_the_shadow_question_is_the_gates_one_definition():
    assert bs.shadow_questions is bg.bearing_questions
    assert bs.SHADOW_QUESTION_ID == bg.BEARING_QUESTION_ID
    assert bs.SHADOW_CRITERIA is bg.BEARING_CRITERIA


async def test_a_built_client_is_cached_across_runs(monkeypatch):
    builds: list[str] = []

    async def _build(ref):
        builds.append(ref)
        return _Decider()

    monkeypatch.setattr(bs, "build_component_handler", _build)
    for _ in range(2):
        await _shadow([(_cand("s"), "yes")], deps=_Deps())
    assert builds == [_SHADOW_REF]


async def test_a_failed_ledger_write_is_counted_never_raised():
    asked = [(_cand(f"s{i}"), "yes") for i in range(4)]
    rows, out, pool = await _shadow(asked, decider=_Decider(), pool=_Pool(fail=True))
    assert len(rows) == 4
    assert out["bearing_shadow_write_errors"] == 4 and out["bearing_shadow_rows"] == 0


async def test_even_an_unexpected_internal_error_is_contained(monkeypatch):
    async def _explode(*a, **k):
        raise KeyError("bug")

    monkeypatch.setattr(bs, "_ask_all", _explode)
    rows, out, _ = await _shadow([(_cand("s"), "yes")], decider=_Decider())
    assert rows == [] and out["bearing_shadow_errors"] == 1


def test_probabilities_are_clamped_into_the_ledger_range():
    assert bs._unit(1.0000001) == 1.0
    assert bs._unit(-0.0001) == 0.0
    assert bs._unit("0.5") is None and bs._unit(True) is None and bs._unit(None) is None


# ---------------------------------------------------------------------------
# End to end over the REAL decision client — the receipt leg
# ---------------------------------------------------------------------------


async def test_the_real_client_stamps_the_bearing_shadow_leg_on_the_receipt():
    from legba.data.schemas import LLMProviderConfig, Property
    from legba.data.stack.llm import DecisionProviderHandler

    class _Res:
        async def resolve(self, secret_id):
            return b"tok-test"

        async def verify_exists(self, secret_id):
            return True

    class _Ctx:
        instance_id = _SHADOW_REF
        instance_version = "0" * 16
        secrets = _Res()
        config = LLMProviderConfig(
            api_endpoint=Property.Text.of("https://gateway.example.invalid/v1"),
            api_key=Property.Secret.of("llm.gateway.api_key"),
            model_name=Property.Text.of("decision-model"),
            max_tokens=Property.Number.of(1, minimum=1, maximum=10),
        )

        def telemetry(self):
            class _T:
                def log(self, *a, **k): ...

                def event(self, *a, **k): ...
            return _T()

    states: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        states.append(body["state"])
        return httpx.Response(200, json={
            "model": "decision-model",
            "answers": {"bears": {"type": "choice", "choice": "no", "confidence": 0.7,
                                  "probabilities": {"yes": 0.3, "no": 0.7}}},
            "usage": {"input_tokens": 120, "output_tokens": 0},
        })

    handler = DecisionProviderHandler()
    await handler.on_configure(_Ctx())
    handler._client = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://gateway.example.invalid", headers=handler._auth_headers(),  # noqa: SLF001
        transport=httpx.MockTransport(responder),
    )
    cand = _cand("s-real", desk="G20 — Australia [country_g20_au]")
    token = run_accounting.bind_run_accounting()
    try:
        rows, out, pool = await _shadow([(cand, "yes")], decider=handler)
        calls = run_accounting.current_llm_calls()
    finally:
        run_accounting.reset_run_accounting(token)
    assert states == [bg.gate_row_text(cand)]
    (row,) = rows
    assert row.shadow_choice == "no" and row.shadow_p_yes == pytest.approx(0.3)
    assert row.shadow_confidence == pytest.approx(0.7) and isinstance(row.shadow_latency_ms, int)
    (rec,) = calls
    assert rec["leg"] == "bearing_shadow" and rec["component_id"] == _SHADOW_REF
    assert rec["status"] == "success" and rec["prompt_tokens"] == 120
    assert out["bearing_shadow_agree"] == 0


# ---------------------------------------------------------------------------
# The option catalog, claim_watch's wiring, the migration, the report
# ---------------------------------------------------------------------------


def test_both_knobs_are_declared_in_the_claim_watch_catalog():
    from legba.data.option_catalog.handler_options import HANDLER_OPTIONS

    specs = {s.name: s for s in HANDLER_OPTIONS["claim_watch"]}
    assert specs["bearing_shadow_ref"].kind == "str"
    assert specs["bearing_shadow_cap"].kind == "int"
    src = (_REPO / "src/legba/data/analysts/deterministic_handlers/claim_watch.py").read_text()
    for knob in ("bearing_shadow_ref", "bearing_shadow_cap"):
        assert f'options.get("{knob}"' in src, f"claim_watch must read {knob} itself"


def test_claim_watch_threads_the_shadow_after_the_writes():
    src = (_REPO / "src/legba/data/analysts/deterministic_handlers/claim_watch.py").read_text()
    assert "asked=bearing_asked" in src
    assert "target_id=str(q[\"target_id\"] or \"\")" in src
    # The shadow runs after the cursor advance — i.e. after every write.
    assert src.index("await run_bearing_shadow(") > src.index("counters[\"staleness_debt\"] = await _staleness_debt(conn)")


def test_migration_0238_is_unique_and_carries_the_ledger():
    numbered = sorted(p.name for p in _MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    assert "0238_decision_shadow_ledger.sql" in numbered
    assert sum(1 for n in numbered if n.startswith("0238_")) == 1
    sql = (_MIGRATIONS / "0238_decision_shadow_ledger.sql").read_text()
    upper = sql.upper()
    assert "BEGIN;" not in upper and "COMMIT;" not in upper  # the runner owns the txn
    assert "CREATE TABLE IF NOT EXISTS public.decision_shadow_ledger" in sql
    assert "CREATE INDEX IF NOT EXISTS" in sql and "DROP " not in upper
    body = sql[sql.index("CREATE TABLE"):sql.index("CONSTRAINT")]
    cols = re.findall(r"^\s{4}([a-z_0-9]+)\s+\S", body, flags=re.MULTILINE)
    assert cols == [
        "id", "occurred_at", "run_id", "analyst_id", "target_id", "question_id",
        "edge_ref", "leg", "gate_component_id", "gate_verdict", "shadow_component_id",
        "shadow_choice", "shadow_p_yes", "shadow_confidence", "shadow_latency_ms",
        "shadow_error", "state_sha256",
    ]
    # The writer's INSERT names exactly the non-defaulted columns.
    insert_cols = re.search(r"\((.*?)\)\s*VALUES", bs._INSERT_SQL, re.S).group(1)
    assert [c.strip() for c in insert_cols.split(",")] == cols[2:]
    assert "gate_verdict IN ('yes', 'no', 'unavailable')" in sql


def _report():
    spec = importlib.util.spec_from_file_location(
        "decision_shadow_report", _REPO / "scripts" / "decision_shadow_report.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_report_summarizes_agreement_confusion_and_calibration():
    rep = _report()
    pair = {"gate_component_id": _GATE_REF, "shadow_component_id": _SHADOW_REF}
    per = [
        {"analyst_id": "claim_watch", **pair, "rows": 10, "errors": 1, "gate_unavailable": 1,
         "yy": 4, "yn": 1, "ny": 2, "nn": 1, "latency_sum_ms": 2400, "latency_n": 8},
        {"analyst_id": "other", **pair, "rows": 2, "errors": 0, "gate_unavailable": 0,
         "yy": 1, "yn": 0, "ny": 0, "nn": 1, "latency_sum_ms": 200, "latency_n": 2},
    ]
    dec = [
        {"analyst_id": "claim_watch", **pair, "decile": 9, "rows": 4, "gate_yes": 3,
         "gate_answered": 4},
        {"analyst_id": "claim_watch", **pair, "decile": 1, "rows": 4, "gate_yes": 2,
         "gate_answered": 3},
        {"analyst_id": "other", **pair, "decile": 9, "rows": 1, "gate_yes": 1, "gate_answered": 1},
    ]
    s = rep.summarize(per, dec)
    cw = s[f"claim_watch | gate={_GATE_REF} | shadow={_SHADOW_REF}"]
    assert cw["gate_component_id"] == _GATE_REF and cw["shadow_component_id"] == _SHADOW_REF
    assert cw["both_answered"] == 8 and cw["agreement"] == pytest.approx(5 / 8)
    assert cw["mean_latency_ms"] == pytest.approx(300.0)
    assert cw["deciles"][9]["gate_yes_rate"] == pytest.approx(0.75)
    assert cw["deciles"][9]["share_of_gate_yes"] == pytest.approx(3 / 5)
    assert cw["deciles"][0]["gate_yes_rate"] is None
    tot = s[rep.ALL]
    assert tot["rows"] == 12 and tot["yy"] == 5 and tot["agreement"] == pytest.approx(7 / 10)
    assert tot["mean_latency_ms"] == pytest.approx(2600 / 10)  # weighted, not mean of means
    assert tot["deciles"][9]["gate_yes"] == 4
    text = rep.render(s, leg="bearing_shadow",
                      since=datetime(2026, 10, 15, tzinfo=timezone.utc),
                      until=datetime(2026, 10, 22, tzinfo=timezone.utc))
    assert (f"| claim_watch | {_GATE_REF} | {_SHADOW_REF} | 10 | 1 | 1 | 8 | 62.5% "
            "| 4 | 1 | 2 | 1 | 300 ms |") in text
    assert f"| (all) | {_GATE_REF} | {_SHADOW_REF} | 12 |" in text
    assert "| 0.9–1.0 | 4 | 3 | 75.0% | 60.0% |" in text


def test_the_report_reads_mixed_pairings_and_null_p_yes_rows():
    """a window spanning the 11-05 role swap: Gemma gate / decision
    shadow rows, then decision gate / Gemma shadow rows (NULL p_yes, so they
    reach no decile). Each group names its gate and its shadow FROM THE ROW,
    the total says the pairing is mixed, and agreement by day still computes."""
    from datetime import date

    rep = _report()
    before = {"analyst_id": "claim_watch", "gate_component_id": _GATE_REF,
              "shadow_component_id": _SHADOW_REF}
    after = {"analyst_id": "claim_watch", "gate_component_id": _SHADOW_REF,
             "shadow_component_id": _GATE_REF}
    per = [
        {**before, "rows": 10, "errors": 0, "gate_unavailable": 0,
         "yy": 5, "yn": 1, "ny": 1, "nn": 3, "latency_sum_ms": 3000, "latency_n": 10},
        {**after, "rows": 4, "errors": 1, "gate_unavailable": 0,
         "yy": 1, "yn": 1, "ny": 0, "nn": 1, "latency_sum_ms": 0, "latency_n": 3},
    ]
    dec = [{**before, "decile": 8, "rows": 6, "gate_yes": 5, "gate_answered": 6}]
    s = rep.summarize(per, dec)
    b = s[f"claim_watch | gate={_GATE_REF} | shadow={_SHADOW_REF}"]
    a = s[f"claim_watch | gate={_SHADOW_REF} | shadow={_GATE_REF}"]
    assert b["agreement"] == pytest.approx(8 / 10) and a["agreement"] == pytest.approx(2 / 3)
    assert (a["gate_component_id"], a["shadow_component_id"]) == (_SHADOW_REF, _GATE_REF)
    assert sum(c["rows"] for c in a["deciles"].values()) == 0  # no p_yes from a chat shadow
    assert s[rep.ALL]["gate_component_id"] == rep.MIXED
    assert s[rep.ALL]["agreement"] == pytest.approx(10 / 13)

    days = rep.summarize_by_day([
        {"day": date(2026, 11, 5), **{k: after[k] for k in ("gate_component_id",
                                                           "shadow_component_id")},
         "rows": 4, "errors": 1, "gate_unavailable": 0, "yy": 1, "yn": 1, "ny": 0, "nn": 1},
        {"day": date(2026, 11, 4), "gate_component_id": _GATE_REF,
         "shadow_component_id": _SHADOW_REF, "rows": 10, "errors": 0, "gate_unavailable": 0,
         "yy": 5, "yn": 1, "ny": 1, "nn": 3},
        {"day": date(2026, 11, 6), "gate_component_id": _SHADOW_REF,
         "shadow_component_id": _GATE_REF, "rows": 1, "errors": 1, "gate_unavailable": 0,
         "yy": 0, "yn": 0, "ny": 0, "nn": 0},
    ])
    assert [d["day"] for d in days] == ["2026-11-04", "2026-11-05", "2026-11-06"]
    assert days[0]["agreement"] == pytest.approx(0.8)
    assert days[1]["agreement"] == pytest.approx(2 / 3)
    assert days[1]["gate_component_id"] == _SHADOW_REF
    assert days[2]["agreement"] is None
    text = rep.render(s, leg="bearing_shadow", by_day=days,
                      since=datetime(2026, 11, 1, tzinfo=timezone.utc),
                      until=datetime(2026, 11, 8, tzinfo=timezone.utc))
    assert f"| claim_watch | {_SHADOW_REF} | {_GATE_REF} | 4 | 1 | 0 | 3 | 66.7% |" in text
    assert f"| (all) | {rep.MIXED} | {rep.MIXED} | 14 |" in text
    assert f"| 2026-11-05 | {_SHADOW_REF} | {_GATE_REF} | 4 | 1 | 3 | 66.7% |" in text
    assert f"| 2026-11-04 | {_GATE_REF} | {_SHADOW_REF} | 10 | 0 | 10 | 80.0% |" in text
    assert "_No p_yes on these rows" in text
    assert "| 0.8–0.9 | 6 | 5 | 83.3% |" in text


def test_the_report_by_day_query_groups_by_the_rows_pairing():
    rep = _report()
    for sql in (rep._PER_ANALYST_SQL, rep._DECILE_SQL, rep._BY_DAY_SQL):
        assert "gate_component_id" in sql and "shadow_component_id" in sql
    assert "::date" in rep._BY_DAY_SQL and "GROUP BY 1, 2, 3" in rep._BY_DAY_SQL


def test_the_report_on_an_empty_window_reads_not_measured():
    rep = _report()
    s = rep.summarize([], [])
    assert s[rep.ALL]["agreement"] is None and s[rep.ALL]["mean_latency_ms"] is None
    text = rep.render(s, leg="bearing_shadow",
                      since=datetime(2026, 10, 15, tzinfo=timezone.utc),
                      until=datetime(2026, 10, 22, tzinfo=timezone.utc))
    assert "| (all) | — | — | 0 | 0 | 0 | 0 | — |" in text
    assert "| — | — | — | 0 | 0 | 0 | — |" in text  # no days, said so


_PG_ENV = (
    "LEGBA_DATA_PG_HOST", "LEGBA_DATA_PG_PORT", "LEGBA_DATA_PG_USER",
    "LEGBA_DATA_PG_PASSWORD", "LEGBA_DATA_PG_DB",
    "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB",
)


def _clear_pg_env(monkeypatch):
    # setenv-then-delenv records the original state, so anything a .env load
    # writes during the test is undone at teardown too.
    for k in _PG_ENV:
        monkeypatch.setenv(k, "x")
        monkeypatch.delenv(k)


def test_the_report_dsn_is_the_shared_resolver(monkeypatch):
    """the report connects with ``PostgresConfig.from_env().dsn``,
    the resolver the working host scripts use, not a private one."""
    import legba.data.config as data_config

    rep = _report()

    class _Sentinel:
        dsn = "postgresql://sentinel"

    monkeypatch.setattr(data_config.PostgresConfig, "from_env", classmethod(lambda cls: _Sentinel()))
    assert rep.resolve_dsn(None) == "postgresql://sentinel"
    src = (_REPO / "scripts" / "decision_shadow_report.py").read_text()
    assert "asyncpg.connect(resolve_dsn(" in src
    assert "_password_from_env_file" not in src


def test_the_report_dsn_round_trips_the_env_and_its_defaults(monkeypatch):
    from legba.data.config import PostgresConfig

    rep = _report()
    _clear_pg_env(monkeypatch)
    # nothing set: the compose defaults, the same answer as the shared resolver
    assert rep.resolve_dsn(None) == PostgresConfig.from_env().dsn
    assert rep.resolve_dsn(None) == "postgresql://legba:legba@localhost:5432/legba"
    monkeypatch.setenv("LEGBA_DATA_PG_HOST", "127.0.0.1")
    monkeypatch.setenv("POSTGRES_PASSWORD", "pw-from-legacy-name")
    assert rep.resolve_dsn(None) == "postgresql://legba:pw-from-legacy-name@127.0.0.1:5432/legba"


def test_the_report_env_file_loads_without_overriding_the_shell(monkeypatch, tmp_path):
    rep = _report()
    _clear_pg_env(monkeypatch)
    env = tmp_path / "alt.env"
    env.write_text("LEGBA_DATA_PG_HOST=postgres\nLEGBA_DATA_PG_PASSWORD=from-file\n")
    monkeypatch.setenv("LEGBA_DATA_PG_HOST", "127.0.0.1")
    assert rep.resolve_dsn(str(env)) == "postgresql://legba:from-file@127.0.0.1:5432/legba"
    with pytest.raises(SystemExit):
        rep.resolve_dsn(str(tmp_path / "missing.env"))


def test_the_report_is_read_only():
    src = (_REPO / "scripts" / "decision_shadow_report.py").read_text()
    assert "readonly=True" in src
    for verb in ("INSERT", "UPDATE ", "DELETE", "TRUNCATE", "ALTER"):
        assert verb not in src.upper().replace("READONLY", "")


# ---------------------------------------------------------------------------
# The extracted builder — the gate's resolution is unchanged (no DB here: the
# store is a fake; tests/data_pkg/test_bearing_gate.py drives the same path
# against the real store class)
# ---------------------------------------------------------------------------


class _FakeStore:
    events: list[str] = []

    def __init__(self, cfg) -> None:
        self.cfg = cfg

    async def connect(self) -> None:
        _FakeStore.events.append("connect")

    async def close(self) -> None:
        _FakeStore.events.append("close")


async def test_build_component_handler_connects_builds_and_closes(monkeypatch):
    import legba.data.postgres as pg
    import legba.runtime.analyst_deps_builder as adb
    from legba.data.registry.credentials import CredentialVault

    _FakeStore.events = []
    seen: dict[str, Any] = {}

    async def _fake_build(component_id, *, registry_client, secrets_resolve):
        seen["component_id"] = component_id
        seen["resolve"] = secrets_resolve
        _FakeStore.events.append("build")
        return _Decider()

    monkeypatch.setattr(pg, "PostgresStore", _FakeStore)
    monkeypatch.setattr(adb, "build_llm_handler_from_stack_component", _fake_build)
    handler = await bg.build_component_handler(_SHADOW_REF)
    assert isinstance(handler, _Decider)
    assert seen["component_id"] == _SHADOW_REF
    assert seen["resolve"].__func__ is CredentialVault.resolve
    assert _FakeStore.events == ["connect", "build", "close"]

    # The gate resolves through it exactly as before: built once, cached, and a
    # failure degrades to None and is never cached.
    client = await bg._resolve_gate_client(_Deps(), "llm.gate.x")
    assert isinstance(client, _Decider) and bg._GATE_CLIENT_CACHE["llm.gate.x"] is client

    async def _boom(component_id, *, registry_client, secrets_resolve):
        raise RuntimeError("not found")

    monkeypatch.setattr(adb, "build_llm_handler_from_stack_component", _boom)
    _FakeStore.events = []
    assert await bg._resolve_gate_client(_Deps(), "llm.gate.y") is None
    assert "llm.gate.y" not in bg._GATE_CLIENT_CACHE
    assert _FakeStore.events == ["connect", "close"], "the store is closed on failure"


def test_the_shadow_registers_its_handler_cache():
    from legba.runtime.llm_handler_cache import registered_cache_labels

    assert "claim_watch.bearing_shadow" in registered_cache_labels()
