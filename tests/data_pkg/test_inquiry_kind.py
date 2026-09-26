# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Program 5 lane 2 — the `inquiry` KIND, its options, its persona and the
pilot descriptor (planning/PROGRAM5_INQUIRY_DESIGN_2026-09-24.md §2/§3/§7.2).

No DB and no live pack: the LLM is a scripted double and the `inquiry_state`
pack is a fake binding keyed by tool name (the pack-double idiom
`tests/journal_w1/test_journal_arc.py` already uses for `journal_read`). Lane
p5_state builds the real pack; these tests pin the CONTRACT the two lanes meet
on — the three tool NAMES, the argument shapes, and what the kind does with
what comes back.

What is held here, in the order the design commits to it:

  * CONTINUITY IS NOT OPTIONAL. The ledger is read at PLAN and its rows reach
    the prompt the model actually sees; an EMPTY ledger renders an honest
    first-cycle line; an UNREADABLE ledger renders neither — it says so, forces
    an honesty flag, and explicitly tells the run not to narrate a first cycle.
  * THE SEALED LEDGER. A hypothesis with no resolution test is REFUSED in code
    and never reaches the pack; a question that does not say what it is about is
    refused the same way, through the platform's EXISTING deictic guard; a
    `cited_refs` uuid the ENTRY never cited is dropped rather than stored as a
    ref resolving to nothing. Each refusal is counted, stamped and flagged.
  * NO NEW SPEND PATH. A question is dispatched through the EXISTING
    `open_question` faucet when — and only when — a binding for it is wired. The
    pilot grants no write pack, so the same run raises the question into its own
    ledger and calls nothing.
  * THE ROW IS A JOURNAL ROW. entry_kind 'inquiry' (or 'crossroads' for that one
    descriptor id), derived_from ALWAYS empty, the payload Literal admits both.
  * EVERY DECLARED KNOB IS READ BY THE KIND (the X-1 reachability rule, proven
    behaviourally: the resolver must change what run_method sees).
  * THE PILOT DESCRIPTOR validates on the new kind and carries the house rules —
    $0 core plane, temperature 1.0, no max_tokens, no web pack, draft state.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

import pytest
import yaml

from legba.data.analysts.agency.agency import AgencyOutcome
from legba.data.analysts.agency.tools import ToolResult
from legba.data.analysts.handler_options import (
    known_kind_option_names,
    resolve_kind_options,
)
from legba.data.analysts.inline_target import InlineTargetDeps
from legba.data.analysts.inquiry import (
    BRIEF_MAX_CHARS,
    CROSSROADS_ANALYST_ID,
    CROSSROADS_ENTRY_KIND,
    ENTRY_KIND,
    HYPOTHESIS_WITHOUT_TEST_FLAG,
    INQUIRY_KIND,
    INQUIRY_STATE_PACK_ID,
    INQUIRY_STATE_TOOLS,
    LEDGER_CLOSE_TOOL,
    LEDGER_READ_TOOL,
    LEDGER_UNREACHABLE_FLAG,
    LEDGER_WRITE_TOOL,
    OPEN_QUESTION_TOOL,
    QUESTION_NOT_SELF_CONTAINED_FLAG,
    _LEDGER_MAX_WRITES_PER_RUN,
    entry_kind_for_analyst,
    plan_ledger_writes,
    render_state_block,
    render_user_prompt,
    resolve_brief,
    resolve_pre_pass_block,
    resolve_target_scope,
    run_method,
)

_REPO = Path(__file__).resolve().parents[2]
_PILOT = _REPO / "descriptors" / "analyst_inquiry_pilot.yaml"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _Usage:
    prompt_tokens = 7
    completion_tokens = 11
    reasoning_tokens = 0


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = _Usage()


class _ScriptedLLM:
    """Pops scripted replies in order across GATHER / field-notes / narrate /
    the ledger coda, recording every prompt it was shown."""

    subprovider = "openai_compat"

    def __init__(self, scripted: list[str]) -> None:
        self._scripted = list(scripted)
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append({"messages": messages, "system": system})
        content = self._scripted.pop(0) if self._scripted else '{"done": true}'
        return _Response(content)

    def prompts(self) -> str:
        """Every user-visible prompt this run put in front of the model."""
        out: list[str] = []
        for call in self.calls:
            for msg in call["messages"]:
                out.append(str(msg.get("content") or ""))
        return "\n".join(out)


class _FakePack:
    """A governed pack binding double: records calls, answers from `outputs`,
    and can be told to BLOCK or FAIL a named tool."""

    def __init__(
        self,
        pack_id: str,
        outputs: dict[str, dict[str, Any]] | None = None,
        *,
        blocked: frozenset[str] = frozenset(),
        failing: frozenset[str] = frozenset(),
    ) -> None:
        self.pack_id = pack_id
        self.outputs = outputs or {}
        self.blocked = blocked
        self.failing = failing
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run_tool(
        self, tool_name: str, args: dict[str, Any], **kwargs: Any
    ) -> AgencyOutcome:
        self.calls.append((tool_name, dict(args)))
        if tool_name in self.blocked:
            return AgencyOutcome(
                admitted=False, pack_id=self.pack_id, tool_name=tool_name,
                block_cause="not_allowed",
            )
        if tool_name in self.failing:
            return AgencyOutcome(
                admitted=True, pack_id=self.pack_id, tool_name=tool_name,
                tool_result=ToolResult(status="failed", error="boom"),
            )
        return AgencyOutcome(
            admitted=True, pack_id=self.pack_id, tool_name=tool_name,
            tool_result=ToolResult(
                status="completed", output=dict(self.outputs.get(tool_name, {})),
            ),
        )

    def args_for(self, tool_name: str) -> list[dict[str, Any]]:
        return [args for name, args in self.calls if name == tool_name]


def _ledger_row(
    row_id: str, kind: str, text: str, *, test: str = "", age_days: int = 3,
) -> dict[str, Any]:
    created = datetime.now(timezone.utc) - timedelta(days=age_days)
    row: dict[str, Any] = {
        "id": row_id, "kind": kind, "text": text, "status": "open",
        "created_at": created.isoformat(),
    }
    if test:
        row["resolution_test"] = test
    return row


# ---------------------------------------------------------------------------
# The tier is the descriptor
# ---------------------------------------------------------------------------


def test_entry_kind_is_the_descriptor_never_a_mode_flag() -> None:
    assert entry_kind_for_analyst("inquiry_pilot") == ENTRY_KIND == "inquiry"
    assert entry_kind_for_analyst(None) == "inquiry"
    assert (
        entry_kind_for_analyst(CROSSROADS_ANALYST_ID)
        == CROSSROADS_ENTRY_KIND
        == "crossroads"
    )


def test_journal_payload_admits_the_program5_kinds() -> None:
    """The Literal-widen regression the chronicle and the lenses both hit: an
    unwidened list rejects the new kind at validation, AFTER the entry is
    written."""
    from legba.data.provenance.models import JournalPayload

    now = datetime.now(timezone.utc)
    for kind in ("inquiry", "crossroads"):
        payload = JournalPayload(
            entry_kind=kind, title="t", body="b",
            period_start=now, period_end=now,
            data={"brief": "b", "ledger": {"carried": 0}},
        )
        assert payload.entry_kind == kind


def test_kind_is_discovered_and_writes_the_journal_output_kind() -> None:
    from legba.data.provenance.kinds import OutputKind
    from legba.data.analysts import discover_analyst_kinds

    handler = discover_analyst_kinds()[INQUIRY_KIND]
    assert handler.output_kind == OutputKind.JOURNAL
    assert handler.read_slice is None       # the default META slice primes it
    assert handler.build_prompt_module is not None


# ---------------------------------------------------------------------------
# PLAN — the state block IS the inquiry
# ---------------------------------------------------------------------------


def test_state_block_renders_every_carried_row_with_its_frozen_test() -> None:
    rows = [
        _ledger_row(
            "h1", "hypothesis", "Diesel cracks widen in northwest Europe",
            test="Weekly assessment prints a crack above 40 USD/bbl twice",
        ),
        _ledger_row("q1", "question", "Which Ryazan units are offline?"),
        _ledger_row("o1", "observation", "Druzhba flows unchanged since 09-10"),
        _ledger_row("e1", "expectation", "Urals discount widens into October"),
    ]
    out = render_state_block(rows)
    assert "YOUR STANDING STATE" in out
    for row in rows:
        assert row["text"] in out
        assert f"[{row['id']}]" in out          # closable BY ID
    assert "RESOLUTION TEST (frozen)" in out
    assert "crack above 40 USD/bbl" in out
    # each declared section is headed, in the persona's order
    assert out.index("OPEN HYPOTHESES") < out.index("QUESTIONS YOU RAISED")
    assert out.index("QUESTIONS YOU RAISED") < out.index("WHAT YOU SAID TO EXPECT")


def test_an_unrecognised_row_kind_still_renders_rather_than_being_forgotten() -> None:
    out = render_state_block([_ledger_row("x1", "conjecture", "something new")])
    assert "something new" in out
    assert "CONJECTURE" in out


def test_empty_ledger_renders_the_honest_first_cycle_line() -> None:
    out = render_state_block([])
    assert "first cycle" in out
    assert "the ledger is empty" in out


def test_an_unreadable_ledger_never_reads_as_a_first_cycle() -> None:
    """The worst failure mode for a continuity voice is silently forgetting: a
    run that could not read its state must not narrate a fresh start."""
    out = render_state_block([], failure_reason="blocked")
    assert "COULD NOT BE READ" in out
    assert "blocked" in out
    assert "Do NOT narrate this as a first cycle" in out
    assert "the ledger is empty" not in out


def test_brief_renders_verbatim_above_the_slice_and_scope_is_named() -> None:
    brief = "the Ukraine–Russia refinery campaign and Europe's fuel security"
    out = render_user_prompt(
        [{"id": str(uuid4()), "title": "Refinery struck"}],
        brief=brief,
        target_scope=("country_watch_ua", "country_g20_de"),
        state_block=render_state_block([]),
    )
    assert brief in out                                   # verbatim
    assert out.index("YOUR STANDING STATE") < out.index("YOUR BRIEF")
    assert out.index("YOUR BRIEF") < out.index("THE PRIMING SLICE")
    assert "country_watch_ua, country_g20_de" in out


def test_a_briefless_inquiry_says_so_rather_than_inventing_a_mandate() -> None:
    out = render_user_prompt([], brief="")
    assert "none is set on this descriptor" in out
    assert "do not invent a mandate" in out.lower()


def test_an_empty_priming_slice_redirects_to_the_record_not_to_silence() -> None:
    out = render_user_prompt([], brief="b")
    assert "PRIMING SLICE IS EMPTY" in out
    assert "search the corpus" in out
    assert "Never fabricate" in out


# ---------------------------------------------------------------------------
# Options — the descriptor-borne mandate
# ---------------------------------------------------------------------------


def test_brief_is_trimmed_to_the_declared_ceiling() -> None:
    assert resolve_brief({"brief": "  hello  "}) == "hello"
    assert resolve_brief({}) == ""
    assert resolve_brief({"brief": 17}) == ""
    assert len(resolve_brief({"brief": "x" * (BRIEF_MAX_CHARS + 500)})) == BRIEF_MAX_CHARS


def test_target_scope_dedups_in_order_and_tolerates_a_scalar() -> None:
    assert resolve_target_scope({"target_scope": ["b", "a", "b"]}) == ("b", "a")
    assert resolve_target_scope({"target_scope": "solo"}) == ("solo",)
    assert resolve_target_scope({}) == ()


def test_every_declared_kind_knob_is_actually_read_by_the_kind() -> None:
    """The X-1 reachability rule, proven BEHAVIOURALLY — the resolver must
    change what run_method sees. A declared-but-unread knob is dead config with
    extra steps."""
    declared = set(known_kind_option_names(INQUIRY_KIND))
    assert declared == {"brief", "target_scope", "pre_pass_module"}
    accepted = resolve_kind_options(
        INQUIRY_KIND,
        {
            "brief": "look into X",
            "target_scope": ["country_watch_ua"],
            "pre_pass_module": "legba.data.analysts.inquiry:build_prompt_module",
        },
    ).accepted
    assert resolve_brief(accepted) == "look into X"
    assert resolve_target_scope(accepted) == ("country_watch_ua",)
    assert accepted["pre_pass_module"].count(":") == 1


def test_the_catalog_refuses_an_over_long_brief_and_a_dotted_module_ref() -> None:
    resolution = resolve_kind_options(
        INQUIRY_KIND,
        {
            "brief": "x" * (BRIEF_MAX_CHARS + 1),
            "target_scope": ["Not An Id"],
            "pre_pass_module": "legba.module.attr",   # dotted, not module:attr
        },
    )
    assert resolution.accepted == {}
    assert {r.key for r in resolution.rejected} == {
        "brief", "target_scope", "pre_pass_module",
    }


# ---------------------------------------------------------------------------
# The lane-3 pre-pass hook
# ---------------------------------------------------------------------------


def _pre_pass_ok(options: Mapping[str, Any], deps: Any) -> str:
    return f"SILENCES: 3 desks unread ({options.get('brief', '')[:4]})"


async def _pre_pass_async(options: Mapping[str, Any], deps: Any) -> str:
    return "ASYNC BLOCK"


def _pre_pass_raises(options: Mapping[str, Any], deps: Any) -> str:
    raise RuntimeError("detector exploded")


def _pre_pass_empty(options: Mapping[str, Any], deps: Any) -> None:
    return None


@pytest.mark.asyncio
async def test_no_pre_pass_option_is_byte_identical_to_before_the_hook() -> None:
    steps: list[dict[str, Any]] = []
    assert await resolve_pre_pass_block({}, None, steps=steps) == ""
    assert steps == []                                   # no import, no call, no step


@pytest.mark.asyncio
@pytest.mark.parametrize("attr", ["_pre_pass_ok", "_pre_pass_async"])
async def test_a_returned_block_is_rendered_above_the_state_block(attr: str) -> None:
    steps: list[dict[str, Any]] = []
    block = await resolve_pre_pass_block(
        {"pre_pass_module": f"{__name__}:{attr}", "brief": "fuel"},
        None, steps=steps,
    )
    assert "COMPUTED BEFORE YOU READ" in block
    assert any(s["kind"] == "pre_pass_block" for s in steps)
    out = render_user_prompt(
        [], brief="fuel", state_block=render_state_block([]), pre_pass_block=block,
    )
    assert out.index("COMPUTED BEFORE YOU READ") < out.index("YOUR STANDING STATE")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ref,kind",
    [
        (f"{__name__}:_pre_pass_raises", "pre_pass_failed"),
        (f"{__name__}:_pre_pass_empty", "pre_pass_empty"),
        (f"{__name__}:does_not_exist", "pre_pass_failed"),
        ("legba.nope.missing:fn", "pre_pass_failed"),
        ("legba.data.analysts.inquiry.run_method", "pre_pass_bad_reference"),
    ],
)
async def test_every_pre_pass_failure_renders_nothing_and_never_raises(
    ref: str, kind: str,
) -> None:
    steps: list[dict[str, Any]] = []
    assert await resolve_pre_pass_block({"pre_pass_module": ref}, None, steps=steps) == ""
    assert [s["kind"] for s in steps] == [kind]


# ---------------------------------------------------------------------------
# The sealed ledger — what may be written, enforced in code
# ---------------------------------------------------------------------------


def test_a_hypothesis_without_a_resolution_test_is_refused() -> None:
    writes, _closes, counters = plan_ledger_writes(
        {
            "hypotheses": [
                {"text": "the campaign is degrading throughput"},          # no test
                {"text": "diesel cracks widen", "resolution_test": "crack > 40"},
                {"text": "blank test", "resolution_test": "   "},          # blank
            ]
        },
        [],
    )
    assert [w["text"] for w in writes] == ["diesel cracks widen"]
    assert writes[0]["resolution_test"] == "crack > 40"
    assert counters["refused_no_test"] == 2
    assert counters["hypotheses"] == 1


def test_a_question_that_does_not_say_what_it_is_about_is_refused() -> None:
    """Through the platform's EXISTING open-question discipline (CW-3's deictic
    guard), not a second copy of it."""
    writes, _closes, counters = plan_ledger_writes(
        {
            "questions": [
                {"text": "Is the framing of the incident orchestrated?"},
                {"text": "Does Ryazan refinery throughput recover by October 2026?"},
            ]
        },
        [],
    )
    assert len(writes) == 1
    assert writes[0]["kind"] == "question"
    assert "Ryazan" in writes[0]["text"]
    assert counters["refused_deictic"] == 1


def test_a_ref_the_entry_never_cited_is_dropped_never_stored() -> None:
    cited = uuid4()
    stranger = uuid4()
    writes, closes, counters = plan_ledger_writes(
        {
            "observations": [
                {"text": "banked", "cited_refs": [str(cited), str(stranger), "junk"]}
            ],
            "close": [
                {"id": "h1", "status": "confirmed", "reason": "settled",
                 "cited_refs": [str(cited)]}
            ],
        },
        [cited],
    )
    assert writes[0]["cited_refs"] == [str(cited)]
    assert closes[0]["cited_refs"] == [str(cited)]
    assert counters["dropped_refs"] == 2


def test_a_close_needs_a_real_terminal_status() -> None:
    _writes, closes, _counters = plan_ledger_writes(
        {
            "close": [
                {"id": "h1", "status": "open"},        # not a close
                {"id": "", "status": "confirmed"},     # no id
                {"id": "h2", "status": "refuted", "reason": "the record settled it"},
            ]
        },
        [],
    )
    assert [c["id"] for c in closes] == ["h2"]
    assert closes[0]["status"] == "refuted"


def test_the_per_run_cap_bites_after_the_refusals_not_before() -> None:
    parsed = {
        "hypotheses": [
            {"text": f"h{i}", "resolution_test": "t"}
            for i in range(_LEDGER_MAX_WRITES_PER_RUN + 4)
        ]
    }
    writes, _closes, _counters = plan_ledger_writes(parsed, [])
    assert len(writes) == _LEDGER_MAX_WRITES_PER_RUN


def test_a_non_mapping_reply_writes_nothing_and_never_raises() -> None:
    for parsed in (None, [], "nope", 7):
        writes, closes, counters = plan_ledger_writes(parsed, [])
        assert (writes, closes) == ([], [])
        assert counters["refused_no_test"] == 0


# ---------------------------------------------------------------------------
# The full arc, through the real run path
# ---------------------------------------------------------------------------


def _deps(scripted: list[str], read_binding: Any) -> InlineTargetDeps:
    return InlineTargetDeps(
        llm=_ScriptedLLM(scripted),
        system_prompt="INQUIRY PERSONA",
        max_rounds=1,
        agency_binding=read_binding,
    )


def _pack_bindings(
    ledger: _FakePack,
    instruments: _FakePack,
    *,
    faucet: _FakePack | None = None,
) -> dict[str, Any]:
    """The per-tool binding map the host builds for this kind: every
    inquiry_state name on the ledger pack, every journal_read name on the
    instruments pack, and (only when the descriptor grants a write pack) the
    open-question faucet."""
    from legba.data.analysts.agency.journal_read import JOURNAL_READ_TOOLS

    bindings: dict[str, Any] = {name: ledger for name in INQUIRY_STATE_TOOLS}
    bindings.update({name: instruments for name in JOURNAL_READ_TOOLS})
    if faucet is not None:
        bindings[OPEN_QUESTION_TOOL] = faucet
    return bindings


@pytest.mark.asyncio
async def test_full_arc_reads_the_ledger_at_plan_and_writes_it_at_reflect() -> None:
    ref = uuid4()
    carried = _ledger_row(
        "h1", "hypothesis", "Refinery outages bite European diesel",
        test="Two consecutive weekly reads print a widening crack",
    )
    ledger = _FakePack(INQUIRY_STATE_PACK_ID, {LEDGER_READ_TOOL: {"rows": [carried]}})
    instruments = _FakePack("journal_read", {
        "get_calibration": {
            "available": True, "forecast_unproven": True, "calibration_thin": True,
        },
    })
    read_binding = _FakePack("substrate_read")
    scripted = [
        '{"done": true}',                                        # GATHER round 1
        f"Field notes: throughput fell [[ref:{ref}]].",          # field-notes seam
        f"# Where the campaign bites\n\nThroughput fell [[ref:{ref}]].\n\n"
        "I still cannot see the storage side.",                  # the entry
        # the LEDGER coda: one good hypothesis, one testless (refused), one
        # deictic question (refused), one close of the carried row
        '{"hypotheses": [{"text": "Storage draws mask the outage",'
        ' "resolution_test": "Weekly stocks fall three weeks running"},'
        ' {"text": "Everything gets worse"}],'
        ' "questions": [{"text": "Is that attack the cause?"}],'
        ' "close": [{"id": "h1", "status": "confirmed", "reason": "the read '
        'settled it", "cited_refs": ["%s"]}]}' % ref,
    ]
    deps = _deps(scripted, read_binding)
    llm = deps.llm
    options = {
        "analyst_id": "inquiry_pilot",
        "agency_binding": read_binding,
        "gather_tool_bindings": _pack_bindings(ledger, instruments),
        "brief": "the refinery campaign and Europe's fuel security",
        "target_scope": ["country_watch_ua"],
    }

    result = await run_method([{"id": str(uuid4()), "title": "seed"}], options, deps)
    payload = result.finding

    # PLAN: the carried row reached the prompt the model actually saw.
    shown = llm.prompts()
    assert carried["text"] in shown
    assert "Two consecutive weekly reads" in shown
    assert "the refinery campaign and Europe's fuel security" in shown
    assert ledger.args_for(LEDGER_READ_TOOL) == [{"status": "open"}]

    # The entry is a JOURNAL row, off the chain.
    assert payload.entry_kind == "inquiry"
    assert result.derived_from == []
    assert ref in payload.cited_substrate_refs
    assert "Throughput fell" in payload.body

    # REFLECT coda: exactly the ONE valid hypothesis reached the pack, carrying
    # its frozen test; the testless one and the deictic question never did.
    written = ledger.args_for(LEDGER_WRITE_TOOL)
    assert len(written) == 1
    assert written[0]["kind"] == "hypothesis"
    assert written[0]["resolution_test"] == "Weekly stocks fall three weeks running"
    assert all("Everything gets worse" not in str(a) for a in written)
    assert all(a.get("kind") != "question" for a in written)

    # ... and the carried row was CLOSED by id, with the ref that settled it.
    closed = ledger.args_for(LEDGER_CLOSE_TOOL)
    assert closed == [{
        "id": "h1", "status": "confirmed", "reason": "the read settled it",
        "cited_refs": [str(ref)],
    }]

    # HONESTY: the calibration pair is FORCED from the substrate (read through
    # the journal_read binding, not the substrate_read one the kind gathers on),
    # and both refusals raise their own deterministic flag.
    assert set(payload.honesty_flags) == {
        "forecast_unproven", "calibration_thin",
        HYPOTHESIS_WITHOUT_TEST_FLAG, QUESTION_NOT_SELF_CONTAINED_FLAG,
    }
    assert instruments.args_for("get_calibration") == [{}]

    # The row's own metadata says what this cycle did to the state.
    assert payload.data["ledger"] == {
        "carried": 1, "written": 1, "closed": 1,
        "refused_no_test": 1, "refused_deictic": 1,
    }
    assert payload.data["target_scope"] == ["country_watch_ua"]


@pytest.mark.asyncio
async def test_gather_routes_each_tool_to_the_pack_that_owns_it() -> None:
    """The three-grant read surface, at the level where it can actually break.

    `substrate_read`'s 19 names ARE `_GATHER_READ_TOOLS`, so they must reach the
    DEFAULT binding; `journal_read`'s own instruments and the ledger tools belong
    to other packs, so they must reach THEIR binding instead — otherwise
    Agency.run_pack_tool refuses them on tool<->pack ownership and the inquiry
    runs with two of its three grants silently dead."""
    from legba.data.analysts.inline_target import _GATHER_READ_TOOLS
    from legba.data.analysts.inquiry import ROUTED_PACK_TOOLS

    # the contract the routing rests on, asserted rather than assumed
    assert "search_corpus" in _GATHER_READ_TOOLS          # -> the default binding
    assert "search_corpus" not in ROUTED_PACK_TOOLS
    assert LEDGER_READ_TOOL not in _GATHER_READ_TOOLS     # -> the ledger binding
    assert "get_source_health" not in _GATHER_READ_TOOLS  # -> the instruments one
    assert {LEDGER_READ_TOOL, "get_source_health"} <= set(ROUTED_PACK_TOOLS)

    ledger = _FakePack(INQUIRY_STATE_PACK_ID, {LEDGER_READ_TOOL: {"rows": []}})
    instruments = _FakePack("journal_read", {"get_source_health": {"summary": {}}})
    read_binding = _FakePack("substrate_read", {"search_corpus": {"rows": []}})
    scripted = [
        '{"tool": "search_corpus", "args": {"query": "refinery"}}',
        '{"tool": "get_source_health", "args": {}}',
        '{"tool": "ledger_read", "args": {"status": "open"}}',
        '{"done": true}',
        "Notes.",
        "An entry.",
        "{}",
    ]
    deps = InlineTargetDeps(
        llm=_ScriptedLLM(scripted),
        system_prompt="P",
        max_rounds=4,
        agency_binding=read_binding,
    )
    await run_method(
        [{"id": str(uuid4()), "title": "seed"}],
        {
            "analyst_id": "inquiry_pilot",
            "agency_binding": read_binding,
            "gather_tool_bindings": _pack_bindings(ledger, instruments),
            "brief": "b",
        },
        deps,
    )
    assert [name for name, _ in read_binding.calls] == ["search_corpus"]
    assert [name for name, _ in instruments.calls] == [
        "get_source_health", "get_calibration",
    ]
    # ledger_read once at PLAN and once from GATHER — both on the ledger pack
    assert [name for name, _ in ledger.calls] == [LEDGER_READ_TOOL, LEDGER_READ_TOOL]


@pytest.mark.asyncio
async def test_a_question_is_dispatched_through_the_existing_faucet() -> None:
    """Design §3: "Dispatch is NOT a tool of its own." The EXISTING
    `open_question` shape (question / counter / derived_from) carries it, and
    the returned row id becomes the ledger row's `dispatched_to`."""
    ref = uuid4()
    ledger = _FakePack(INQUIRY_STATE_PACK_ID, {LEDGER_READ_TOOL: {"rows": []}})
    instruments = _FakePack("journal_read")
    faucet = _FakePack("propose_facts", {OPEN_QUESTION_TOOL: {"hypothesis_id": "q-77"}})
    read_binding = _FakePack("substrate_read")
    scripted = [
        '{"done": true}',
        f"Notes [[ref:{ref}]].",
        f"An entry that cites the record [[ref:{ref}]].",
        '{"questions": [{"text": "Does Ryazan throughput recover by October 2026?",'
        ' "counter": "The units were never hit", "cited_refs": ["%s"]}]}' % ref,
    ]
    deps = _deps(scripted, read_binding)
    result = await run_method(
        [{"id": str(uuid4()), "title": "seed"}],
        {
            "analyst_id": "inquiry_pilot",
            "agency_binding": read_binding,
            "gather_tool_bindings": _pack_bindings(ledger, instruments, faucet=faucet),
            "brief": "b",
        },
        deps,
    )
    # the EXISTING tool, with the EXISTING argument names
    assert faucet.args_for(OPEN_QUESTION_TOOL) == [{
        "question": "Does Ryazan throughput recover by October 2026?",
        "derived_from": [str(ref)],
        "counter": "The units were never hit",
    }]
    written = ledger.args_for(LEDGER_WRITE_TOOL)
    assert written[0]["kind"] == "question"
    assert written[0]["dispatched_to"] == "q-77"
    # `counter` is the faucet's argument, not a ledger column
    assert "counter" not in written[0]
    assert result.finding.entry_kind == "inquiry"


@pytest.mark.asyncio
async def test_without_a_write_pack_the_question_is_banked_and_nothing_spends() -> None:
    """The PILOT's shape: substrate_read + journal_read + inquiry_state and no
    write pack at all. The question still reaches the ledger; no faucet call is
    made, because there is nothing wired to make one — so no rung of this run
    can spend."""
    ref = uuid4()
    ledger = _FakePack(INQUIRY_STATE_PACK_ID, {LEDGER_READ_TOOL: {"rows": []}})
    instruments = _FakePack("journal_read")
    read_binding = _FakePack("substrate_read")
    bindings = _pack_bindings(ledger, instruments)       # no faucet
    scripted = [
        '{"done": true}',
        f"Notes [[ref:{ref}]].",
        f"An entry [[ref:{ref}]].",
        '{"questions": [{"text": "Does Ryazan throughput recover by October 2026?",'
        ' "cited_refs": ["%s"]}]}' % ref,
    ]
    await run_method(
        [{"id": str(uuid4()), "title": "seed"}],
        {
            "analyst_id": "inquiry_pilot",
            "agency_binding": read_binding,
            "gather_tool_bindings": bindings,
            "brief": "b",
        },
        _deps(scripted, read_binding),
    )
    written = ledger.args_for(LEDGER_WRITE_TOOL)
    assert len(written) == 1
    assert written[0]["kind"] == "question"
    assert "dispatched_to" not in written[0]
    assert OPEN_QUESTION_TOOL not in bindings


@pytest.mark.asyncio
async def test_an_unreadable_ledger_flags_the_entry_and_still_writes_it() -> None:
    ledger = _FakePack(
        INQUIRY_STATE_PACK_ID, {}, blocked=frozenset({LEDGER_READ_TOOL}),
    )
    read_binding = _FakePack("substrate_read")
    scripted = ['{"done": true}', "Notes.", "An entry.", "{}"]
    result = await run_method(
        [{"id": str(uuid4()), "title": "seed"}],
        {
            "analyst_id": "inquiry_pilot",
            "agency_binding": read_binding,
            "gather_tool_bindings": _pack_bindings(ledger, _FakePack("journal_read")),
            "brief": "b",
        },
        _deps(scripted, read_binding),
    )
    assert LEDGER_UNREACHABLE_FLAG in result.finding.honesty_flags
    assert result.finding.body.strip() == "An entry."


@pytest.mark.asyncio
async def test_without_the_ledger_pack_the_coda_is_a_loud_no_op() -> None:
    """An inquiry whose inquiry_state pack is not EFFECTIVE still writes its
    entry; the ledger phase stamps `pack_not_effective` rather than silently
    doing nothing."""
    read_binding = _FakePack("substrate_read")
    scripted = ['{"done": true}', "Notes.", "An entry.", "{}"]
    result = await run_method(
        [{"id": str(uuid4()), "title": "seed"}],
        {
            "analyst_id": "inquiry_pilot",
            "agency_binding": read_binding,
            "gather_tool_bindings": {},
            "brief": "b",
        },
        _deps(scripted, read_binding),
    )
    kinds = [s.get("kind") for s in result.intermediate_steps if s["phase"] == "ledger"]
    assert kinds == ["pack_not_effective"]
    assert result.finding.entry_kind == "inquiry"
    assert result.derived_from == []


@pytest.mark.asyncio
async def test_the_crossroads_id_writes_its_own_entry_kind_on_the_same_kind() -> None:
    read_binding = _FakePack("substrate_read")
    scripted = ['{"done": true}', "Notes.", "A crossroads read.", "{}"]
    result = await run_method(
        [{"id": str(uuid4()), "title": "seed"}],
        {
            "analyst_id": CROSSROADS_ANALYST_ID,
            "agency_binding": read_binding,
            "gather_tool_bindings": {},
            "brief": "the fixed mandate",
        },
        _deps(scripted, read_binding),
    )
    assert result.finding.entry_kind == "crossroads"


# ---------------------------------------------------------------------------
# The persona
# ---------------------------------------------------------------------------


def test_persona_carries_the_hard_limits_and_the_state_block_shape() -> None:
    from legba.prompts.inquiry import (
        INQUIRY_SYSTEM,
        STATE_BLOCK_HEADER,
        STATE_SECTION_HEADERS,
    )

    # the two hard limits
    assert "you never assert a new fact" in INQUIRY_SYSTEM
    assert "NO access to the open web" in INQUIRY_SYSTEM
    # the sealed-ledger rules the code actually enforces
    assert "A HYPOTHESIS MUST CARRY A RESOLUTION TEST" in INQUIRY_SYSTEM
    assert "A QUESTION MUST BE SELF-CONTAINED" in INQUIRY_SYSTEM
    # the state block the kind renders is the one the persona describes
    assert STATE_BLOCK_HEADER in INQUIRY_SYSTEM
    for heading in STATE_SECTION_HEADERS.values():
        assert heading in INQUIRY_SYSTEM
    # scored by YIELD, not correctness (design §4)
    assert "SCORED ON YIELD" in INQUIRY_SYSTEM
    # and NONE of the diary's apparatus contract leaks in
    assert "the apparatus is your POSTSCRIPT" not in INQUIRY_SYSTEM
    assert "[[instrument]]" not in INQUIRY_SYSTEM
    assert "DISTINCT wired sources" not in INQUIRY_SYSTEM


# ---------------------------------------------------------------------------
# The pilot descriptor
# ---------------------------------------------------------------------------


def _pilot_body() -> dict[str, Any]:
    body = yaml.safe_load(_PILOT.read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return body


def test_pilot_descriptor_validates_on_the_new_kind() -> None:
    import legba.data.analysts  # noqa: F401 — registers the extension kind
    from legba.data.schemas.analyst import AnalystDescriptor

    body = _pilot_body()
    desc = AnalystDescriptor.model_validate(body, strict=False)
    assert desc.identity.id == "inquiry_pilot"
    assert desc.identity.kind == INQUIRY_KIND
    assert desc.identity.state.value == "draft"       # the orchestrator promotes
    assert desc.method.prompt_module == "legba.prompts.inquiry:INQUIRY_SYSTEM"


def test_pilot_descriptor_carries_the_house_rules() -> None:
    body = _pilot_body()
    assert int(body["method"]["budget_tokens_per_day"]) == 0
    assert float(body["method"]["llm"]["temperature"]) == 1.0
    assert "max_tokens" not in body["method"]["llm"]   # the core plane never caps
    assert "verify" in body["method"]["llm"]           # the V1 journal gate
    assert body["outputs"] == []
    assert body.get("grounding", {}).get("enabled") is False
    # daily 12:30Z, with the below-interval cooldown guard satisfied
    assert body["cadence"]["fallback_schedule"] == "30 12 * * *"
    assert int(body["cadence"]["cooldown_seconds"]) < 86400


def test_pilot_grants_the_three_read_packs_and_no_web() -> None:
    packs = {p["pack_id"] for p in _pilot_body()["action_packs"]}
    # journal_propose is the DISPATCH (a question or contention goes out as a
    # proposal through the journal gate — never a spend); added at the merge.
    assert packs == {"substrate_read", "journal_read", INQUIRY_STATE_PACK_ID, "journal_propose"}
    # NO web by construction — that property is what makes the wide read safe
    assert "web_access" not in packs and "research" not in packs
    # and no write pack: the inquiry adds nothing to the record but its entry
    assert "propose_facts" not in packs
    assert "journal_propose" in packs  # the dispatch (merge decision 2026-09-24): a question goes out as a proposal through the journal gate, never a fact write


def test_pilot_brief_and_scope_name_real_targets() -> None:
    """The design named UA / RU / DE; the REAL ids differ — RU is a G20 member,
    so there is no country_watch_ru. A scope naming a target that does not exist
    is a mandate pointed at nothing."""
    options = _pilot_body()["method"]["options"]
    assert "refinery" in options["brief"] and "fuel security" in options["brief"]
    assert len(options["brief"]) <= BRIEF_MAX_CHARS
    assert options["target_scope"] == [
        "country_watch_ua", "country_g20_ru", "country_g20_de",
    ]


def test_pilot_scope_ids_exist_in_the_bringup_rosters() -> None:
    g20 = (_REPO / "scripts" / "bringup_register_g20_country_targets.py").read_text()
    watch = (_REPO / "scripts" / "bringup_register_watch_country_targets.py").read_text()
    assert '"UA"' in watch and 'country_watch_{iso_lower}' in watch
    assert '"RU"' in g20 and '"DE"' in g20 and 'country_g20_{iso_lower}' in g20


def test_pilot_options_survive_the_catalog_untouched() -> None:
    """A knob the catalog rejects is a knob the run never sees — the descriptor
    must clear its own validator."""
    options = _pilot_body()["method"]["options"]
    resolution = resolve_kind_options(INQUIRY_KIND, options)
    assert not resolution.rejected
    assert resolution.accepted == options
