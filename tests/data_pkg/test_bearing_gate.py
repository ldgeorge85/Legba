# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W-B1/W-B2 — the bearing pipeline, driven directly (no DB, no network).

Covers the module's whole contract in isolation: the two parsers (including
the refusal to guess positionally), the OFF default, all four gate outcomes
(yes / no / unavailable / deferred), the stamp-and-write posture under an
outage, the confirm leg's yes/no/unavailable/deferred, and the receipt
counters. The DB-backed end-to-end path — that a gate NO really writes no
row, that a stamp really lands in ``bearing_edges.data``, that a gated-out
question raises no review flag — lives in ``test_claim_watch.py``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from legba.data.analysts.deterministic_handlers import bearing_gate as bg


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLM:
    """Scripted chat_complete. ``replies`` may hold strings (returned in
    order) or Exception instances (raised)."""

    def __init__(self, *replies: Any, default: Any = "YES") -> None:
        self.replies = list(replies)
        self.default = default
        self.prompts: list[str] = []

    async def chat_complete(self, messages, **kwargs):
        # The row under test rides the LAST user turn (fewshot/1 adopted the
        # prompt-lab's system + exemplar-turns prefix); record that, so the
        # thesis/signal assertions keep testing the message that varies.
        self.prompts.append(messages[-1]["content"])
        reply = self.replies.pop(0) if self.replies else self.default
        if isinstance(reply, Exception):
            raise reply
        return _Resp(reply)


def _cand(thesis: str = "a thesis", signal: str = "a signal") -> bg.EdgeCandidate:
    now = datetime.now(timezone.utc)
    return bg.EdgeCandidate(
        signal_id=uuid4(),
        signal_as_of=now,
        signal_text=signal,
        question_id=uuid4(),
        question_as_of=now,
        question_thesis=thesis,
        weight=0.5,
        planes=["entity"],
    )


class _Deps:
    def __init__(self, **extras: Any) -> None:
        self.extras = dict(extras)


def _gate_deps(llm: Any) -> _Deps:
    return _Deps(**{bg.SLM_DEPS_EXTRA_KEY: llm})


async def _run(cands, *, deps, mode="on", gate_cap=100, confirm_cap=100):
    counters: dict[str, Any] = {}
    kept = await bg.run_bearing_pipeline(
        cands,
        deps=deps,
        mode=mode,
        gate_ref=bg.DEFAULT_BEARING_GATE_REF,
        gate_cap=gate_cap,
        confirm_cap=confirm_cap,
        counters=counters,
    )
    return kept, counters


@pytest.fixture(autouse=True)
def _clear_client_cache():
    bg._GATE_CLIENT_CACHE.clear()
    yield
    bg._GATE_CLIENT_CACHE.clear()


# ---------------------------------------------------------------------------
# Defaults — the X-1 contract lives or dies here
# ---------------------------------------------------------------------------


def test_ships_off_so_an_optionless_descriptor_changes_nothing():
    """The whole byte-identical claim rests on this one constant."""
    assert bg.DEFAULT_BEARING_GATE == "off"
    assert bg.gate_enabled(bg.DEFAULT_BEARING_GATE) is False


@pytest.mark.parametrize(
    "value", ["off", "", None, "yes", "true", "1", "enabled", "no"]
)
def test_anything_but_on_reads_off(value):
    """Anything unrecognised must read OFF. A gate that turned ITSELF on from
    a typo would start silently refusing edges — the failure direction that
    loses data. (The catalog choice-locks the value, so a typo is refused
    loudly there; this is the handler-side belt.)"""
    assert bg.gate_enabled(value) is False


@pytest.mark.parametrize("value", ["on", "ON", " on ", "On"])
def test_case_and_whitespace_are_tolerated_on_the_way_in(value):
    assert bg.gate_enabled(value) is True


def test_the_gate_ref_default_is_the_measured_component():
    assert bg.DEFAULT_BEARING_GATE_REF == "llm.verify.slm_8b"


def test_prompt_constants_are_the_tuning_surface():
    """The prompt-lab lane edits these; the version rides every stamped edge,
    so a prompt change without a version bump would make a precision shift
    unattributable."""
    assert "{thesis}" in bg.GATE_PROMPT and "{signal}" in bg.GATE_PROMPT
    assert "{pairs}" in bg.CONFIRM_PROMPT
    assert bg.GATE_PROMPT_VERSION and bg.CONFIRM_PROMPT_VERSION


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("YES", "yes"),
        ("NO", "no"),
        ("yes", "yes"),
        (" No.\n", "no"),
        ("**YES**", "yes"),
        ("Answer: NO — the signal concerns a different dispute.", "no"),
        ("YES, this bears on the thesis.", "yes"),
    ],
)
def test_parse_gate_verdict_reads_a_real_reply(reply, expected):
    assert bg.parse_gate_verdict(reply) == expected


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "   ",
        None,
        123,
        "I cannot determine this.",
        "NOTHING in the signal is relevant",   # NOT a NO — word-bounded
        "YESTERDAY the strike occurred",        # NOT a YES — word-bounded
    ],
)
def test_parse_gate_verdict_returns_none_when_it_cannot_read_one(reply):
    """An unreadable reply is the MODEL failing, not the edge failing — the
    caller turns None into stamp-and-write, never into a refusal. A substring
    match on NOTHING/YESTERDAY would silently drop or admit real edges."""
    assert bg.parse_gate_verdict(reply) is None


def test_parse_confirm_batch_binds_by_echoed_id():
    content = (
        '[{"id": "e1", "bears": "yes", "reason": "same dispute"},'
        ' {"id": "e0", "bears": "no", "reason": "different country"}]'
    )
    got = bg.parse_confirm_batch(content, ["e0", "e1"])
    assert got == {
        "e0": ("no", "different country"),
        "e1": ("yes", "same dispute"),
    }


def test_parse_confirm_batch_never_guesses_positionally():
    """The signal_salience rule. A verdict bound to the wrong edge would
    record "a 120B model says this bears" about a pair it never saw — worse
    than no verdict, because the stamp is what the measurement loop trusts."""
    content = '[{"id": "not-in-batch", "bears": "yes", "reason": "x"}]'
    assert bg.parse_confirm_batch(content, ["e0", "e1"]) == {}


def test_parse_confirm_batch_tolerates_fences_and_prose_and_coercions():
    content = (
        "Here you go:\n```json\n"
        '[{"id": "e0", "bears": true, "reason": "yes it does"},'
        ' {"id": "e1", "bears": "N", "reason": ""}]\n```'
    )
    got = bg.parse_confirm_batch(content, ["e0", "e1"])
    assert got["e0"] == ("yes", "yes it does")
    assert got["e1"] == ("no", "")


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json at all",
        '[{"id": "e0", "bears": "maybe"}]',      # verdict outside the vocabulary
        '[{"id": "e0"}]',                          # no verdict
        '{"id": "e0", "bears": "yes"}',           # object, not array
    ],
)
def test_parse_confirm_batch_drops_what_it_cannot_read(content):
    assert bg.parse_confirm_batch(content, ["e0"]) == {}


def test_parse_confirm_batch_first_binding_wins_on_a_duplicate_id():
    content = (
        '[{"id": "e0", "bears": "yes", "reason": "first"},'
        ' {"id": "e0", "bears": "no", "reason": "second"}]'
    )
    assert bg.parse_confirm_batch(content, ["e0"]) == {"e0": ("yes", "first")}


def test_signal_digest_reads_the_payload_shapes_and_bounds_length():
    assert bg.signal_digest({"title": "T", "summary": "B"}) == "T — B"
    assert bg.signal_digest({"headline": "H"}) == "H"
    assert bg.signal_digest('{"title": "J"}') == "J"
    assert bg.signal_digest("not json") == ""
    assert bg.signal_digest(None) == ""
    long = bg.signal_digest({"title": "x", "body": "y " * 5000})
    assert len(long) == bg.MAX_SIGNAL_CHARS


# ---------------------------------------------------------------------------
# OFF — the shipped default
# ---------------------------------------------------------------------------


async def test_off_returns_the_candidates_untouched_and_calls_nothing():
    llm = _FakeLLM("NO")
    cands = [_cand(), _cand()]
    kept, counters = await _run(cands, deps=_gate_deps(llm), mode="off")
    assert kept == cands
    assert llm.prompts == []                        # no call was made
    assert all(c.gate is None for c in kept)
    assert all(c.data_payload() == {} for c in kept)  # == the 0116 default
    assert counters["bearing_gate_mode"] == "off"
    assert counters["bearing_gate_calls"] == 0
    assert counters["bearing_gated_out"] == 0


async def test_receipt_counters_are_seeded_even_when_the_gate_never_ran():
    _, counters = await _run([], deps=_Deps(), mode="off")
    for key in bg.bearing_counter_defaults():
        assert key in counters, f"receipt is missing {key}"


# ---------------------------------------------------------------------------
# W-B1 — the four gate outcomes
# ---------------------------------------------------------------------------


async def test_gate_yes_keeps_the_edge_and_stamps_it():
    cand = _cand(thesis="Iran sanctions relief stalls", signal="Sanctions talks collapse")
    kept, counters = await _run([cand], deps=_gate_deps(_FakeLLM("YES")))
    assert kept == [cand]
    assert cand.gate == "yes"
    data = cand.data_payload()
    assert data["bearing_gate"] == "yes"
    assert data["bearing_gate_ref"] == bg.DEFAULT_BEARING_GATE_REF
    assert data["bearing_gate_prompt"] == bg.GATE_PROMPT_VERSION
    assert counters["bearing_gate_yes"] == 1
    assert counters["bearing_gate_calls"] == 1


async def test_gate_no_refuses_the_edge_entirely():
    """The point of the leg: a NO writes NO ROW, and the refusal is counted
    rather than vanishing."""
    cand = _cand()
    kept, counters = await _run([cand], deps=_gate_deps(_FakeLLM("NO")))
    assert kept == []
    assert counters["bearing_gated_out"] == 1
    assert counters["bearing_gate_yes"] == 0


async def test_gate_asks_about_the_thesis_and_the_signal():
    llm = _FakeLLM("YES")
    await _run([_cand(thesis="THESIS-TEXT", signal="SIGNAL-TEXT")], deps=_gate_deps(llm))
    prompt = llm.prompts[0]
    assert "THESIS-TEXT" in prompt and "SIGNAL-TEXT" in prompt


@pytest.mark.parametrize(
    "reply",
    [
        RuntimeError("connection refused"),   # endpoint down
        TimeoutError("timed out"),            # hung
        "I am not sure about this one.",      # unparseable
    ],
)
async def test_an_unanswerable_gate_stamps_and_writes(reply):
    """STAMP-AND-WRITE. A gate that failed CLOSED would turn one 8B outage
    into a silent hole in the bearing plane; consumers filter on the stamp
    instead."""
    cand = _cand()
    kept, counters = await _run([cand], deps=_gate_deps(_FakeLLM(reply)))
    assert kept == [cand]                       # the edge SURVIVES
    assert cand.gate == "unavailable"
    assert cand.data_payload()["bearing_gate"] == "unavailable"
    assert counters["bearing_gate_errors"] == 1
    assert counters["bearing_gated_out"] == 0   # an outage is not a refusal


async def test_over_the_call_cap_stamps_deferred_and_writes():
    cands = [_cand() for _ in range(4)]
    kept, counters = await _run(
        cands, deps=_gate_deps(_FakeLLM(default="YES")), gate_cap=2
    )
    assert len(kept) == 4                       # nothing is dropped by a BUDGET
    assert [c.gate for c in kept] == ["yes", "yes", "deferred", "deferred"]
    assert counters["bearing_gate_calls"] == 2
    assert counters["bearing_gate_deferred"] == 2


async def test_a_zero_cap_leaves_the_leg_on_with_no_calls():
    llm = _FakeLLM(default="NO")
    cands = [_cand() for _ in range(3)]
    kept, counters = await _run(cands, deps=_gate_deps(llm), gate_cap=0)
    assert len(kept) == 3 and llm.prompts == []
    assert counters["bearing_gate_deferred"] == 3


async def test_one_edges_outage_does_not_infect_its_neighbours():
    a, b, c = _cand(), _cand(), _cand()
    llm = _FakeLLM("YES", RuntimeError("boom"), "NO")
    kept, counters = await _run([a, b, c], deps=_gate_deps(llm))
    assert kept == [a, b]
    assert (a.gate, b.gate) == ("yes", "unavailable")
    assert counters["bearing_gate_yes"] == 1
    assert counters["bearing_gate_errors"] == 1
    assert counters["bearing_gated_out"] == 1
    assert counters["bearing_gate_calls"] == 3


# ---------------------------------------------------------------------------
# Client resolution — the production path, not a hardcoded endpoint
# ---------------------------------------------------------------------------


async def test_the_gate_client_is_built_from_the_registry_component_and_vault(
    monkeypatch,
):
    """The endpoint and the basic-auth pair must come from the stack component
    the operator registered + the CredentialVault — never from code."""
    import legba.runtime.analyst_deps_builder as adb

    seen: dict[str, Any] = {}

    async def _fake_build(component_id, *, registry_client, secrets_resolve):
        seen["component_id"] = component_id
        seen["secrets_resolve"] = secrets_resolve
        return _FakeLLM("YES")

    monkeypatch.setattr(
        adb, "build_llm_handler_from_stack_component", _fake_build
    )
    cand = _cand()
    kept, counters = await _run([cand], deps=_Deps())  # nothing injected
    assert kept == [cand] and cand.gate == "yes"
    assert seen["component_id"] == bg.DEFAULT_BEARING_GATE_REF
    # The resolver handed over is the vault's own bound resolve().
    from legba.data.registry.credentials import CredentialVault

    assert seen["secrets_resolve"].__func__ is CredentialVault.resolve


async def test_a_built_client_is_cached_across_runs(monkeypatch):
    import legba.runtime.analyst_deps_builder as adb

    builds = {"n": 0}

    async def _fake_build(component_id, *, registry_client, secrets_resolve):
        builds["n"] += 1
        return _FakeLLM(default="YES")

    monkeypatch.setattr(
        adb, "build_llm_handler_from_stack_component", _fake_build
    )
    await _run([_cand()], deps=_Deps())
    await _run([_cand()], deps=_Deps())
    assert builds["n"] == 1


async def test_a_failed_client_build_stamps_every_candidate_unavailable(
    monkeypatch,
):
    """A registry that is not up yet, or a component whose vault entries are
    missing, must NOT silence the matcher — and must not be cached, so the
    next tick retries."""
    import legba.runtime.analyst_deps_builder as adb

    async def _boom(component_id, *, registry_client, secrets_resolve):
        raise RuntimeError("stack-component not found in registry")

    monkeypatch.setattr(adb, "build_llm_handler_from_stack_component", _boom)
    cands = [_cand() for _ in range(3)]
    kept, counters = await _run(cands, deps=_Deps())
    assert kept == cands
    assert all(c.gate == "unavailable" for c in kept)
    assert counters["bearing_gate_errors"] == 3
    assert counters["bearing_gate_calls"] == 0
    assert bg._GATE_CLIENT_CACHE == {}  # a failure is never cached


# ---------------------------------------------------------------------------
# W-B2 — the confirm leg
# ---------------------------------------------------------------------------


def _confirm_deps(gate_llm: Any, confirm_llm: Any) -> _Deps:
    return _Deps(
        **{
            bg.SLM_DEPS_EXTRA_KEY: gate_llm,
            bg.CONFIRM_LLM_DEPS_EXTRA_KEY: confirm_llm,
        }
    )


async def test_confirm_stamps_gate_passed_edges_with_a_verdict_and_reason():
    a, b = _cand(), _cand()
    confirm = _FakeLLM(
        '[{"id": "e0", "bears": "yes", "reason": "same escalation"},'
        ' {"id": "e1", "bears": "no", "reason": "unrelated commodity"}]'
    )
    kept, counters = await _run(
        [a, b], deps=_confirm_deps(_FakeLLM(default="YES"), confirm)
    )
    assert (a.confirm, a.confirm_reason) == ("yes", "same escalation")
    assert (b.confirm, b.confirm_reason) == ("no", "unrelated commodity")
    assert a.data_payload()["bearing_confirm"] == "yes"
    assert b.data_payload()["bearing_confirm_reason"] == "unrelated commodity"
    assert counters["bearing_confirm_yes"] == 1
    assert counters["bearing_confirm_no"] == 1
    assert counters["bearing_confirm_calls"] == 1


async def test_confirm_never_blocks_an_edge_even_when_it_says_no():
    """By the time the confirm runs the gate has already decided. A 'no' is
    recorded ON the edge for the consumers, it does not retract it."""
    cand = _cand()
    confirm = _FakeLLM('[{"id": "e0", "bears": "no", "reason": "no bearing"}]')
    kept, _ = await _run(
        [cand], deps=_confirm_deps(_FakeLLM("YES"), confirm)
    )
    assert kept == [cand]
    assert cand.gate == "yes" and cand.confirm == "no"


async def test_confirm_only_looks_at_gate_passed_edges():
    passed, outage = _cand(), _cand()
    confirm = _FakeLLM('[{"id": "e0", "bears": "yes", "reason": "r"}]')
    gate = _FakeLLM("YES", RuntimeError("down"))
    kept, counters = await _run([passed, outage], deps=_confirm_deps(gate, confirm))
    assert kept == [passed, outage]
    assert passed.confirm == "yes"
    assert outage.confirm is None            # never asked about
    assert counters["bearing_confirm_yes"] == 1


async def test_a_core_plane_outage_stamps_unavailable_and_moves_on():
    a, b = _cand(), _cand()
    kept, counters = await _run(
        [a, b],
        deps=_confirm_deps(_FakeLLM(default="YES"), _FakeLLM(RuntimeError("502"))),
    )
    assert kept == [a, b]
    assert a.confirm == "unavailable" and b.confirm == "unavailable"
    assert counters["bearing_confirm_unavailable"] == 2


async def test_a_pair_the_model_omitted_is_unavailable_not_guessed():
    a, b = _cand(), _cand()
    confirm = _FakeLLM('[{"id": "e0", "bears": "yes", "reason": "r"}]')
    kept, counters = await _run(
        [a, b], deps=_confirm_deps(_FakeLLM(default="YES"), confirm)
    )
    assert a.confirm == "yes"
    assert b.confirm == "unavailable"
    assert counters["bearing_confirm_unavailable"] == 1


async def test_confirm_batches_and_defers_past_its_cap():
    cands = [_cand() for _ in range(bg.CONFIRM_BATCH_SIZE + 3)]
    confirm = _FakeLLM(default="[]")
    kept, counters = await _run(
        cands,
        deps=_confirm_deps(_FakeLLM(default="YES"), confirm),
        confirm_cap=bg.CONFIRM_BATCH_SIZE + 1,
    )
    assert len(kept) == len(cands)
    assert counters["bearing_confirm_deferred"] == 2
    assert counters["bearing_confirm_calls"] == 2  # a full batch + one


async def test_an_unwired_confirm_leg_is_silent_not_an_outage():
    """No ``method.llm.primary`` on the descriptor is the SHIPPED state. The
    leg did not run, which is not the same as it having failed — stamping
    'unavailable' would fabricate an outage on every edge, forever."""
    cand = _cand()
    kept, counters = await _run([cand], deps=_gate_deps(_FakeLLM("YES")))
    assert cand.gate == "yes"
    assert cand.confirm is None
    assert "bearing_confirm" not in cand.data_payload()
    assert counters["bearing_confirm_unavailable"] == 0
    assert counters["bearing_confirm_calls"] == 0


async def test_a_zero_confirm_cap_disables_the_leg():
    cand = _cand()
    confirm = _FakeLLM(default="[]")
    await _run(
        [cand],
        deps=_confirm_deps(_FakeLLM("YES"), confirm),
        confirm_cap=0,
    )
    assert cand.confirm is None
    assert confirm.prompts == []


async def test_the_confirm_prompt_carries_every_pair_with_its_echo_id():
    a, b = _cand(thesis="T-A", signal="S-A"), _cand(thesis="T-B", signal="S-B")
    confirm = _FakeLLM(default="[]")
    await _run([a, b], deps=_confirm_deps(_FakeLLM(default="YES"), confirm))
    prompt = confirm.prompts[0]
    for token in ("id=e0", "id=e1", "T-A", "S-A", "T-B", "S-B"):
        assert token in prompt
