# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE REAL-BINDING-PATH propose test (W1-C) — the one the old e2e was not.

WHY THIS FILE EXISTS. ``journal_propose`` was granted, registered, catalogued,
bound end-to-end and covered by a green e2e test — and had **0 invocations
EVER** in production (engine-review p5; confirmed live 2026-08-03 via
``action_pack_invocations`` = no journal_propose row, ``governor_events`` = not
one decision of ANY kind for the pack, ``journal_proposals`` = 0). The existing
``test_gather_propose_e2e`` is the standing exhibit for the operator's rule
"tests must traverse the REAL binding path": it calls :func:`_gather` DIRECTLY
with ``binding=None``, a hand-built ``tool_bindings`` dict, and a hand-built
META self-allow (``target_allows=grants``) — three of the four legs production
actually runs, faked. It proved the tool works when called. It could not, and
did not, prove anything ever CALLS it.

WHAT THIS TEST TRAVERSES INSTEAD, in production order:

  1. the host-shaped base binding (``target_allows=None``, ToolContext with NO
     writeback — exactly what ``dapr_host`` wires, which is why a hand-built
     self-allow hides a real failure: a META assessor with ``target_allows=None``
     denies every call at the ALLOW leg);
  2. :func:`legba.runtime.actor_output_emit._gather_write_bindings_for_target` —
     the PRODUCTION re-point, run with ``target_id=None`` so the META self-allow
     and the per-run ``WritebackContext`` injection are the shipped code, not
     the test's idea of it;
  3. :func:`legba.data.analysts.journal_assessor.run_method` — the real
     entry-point the actor dispatches, with options shaped the way
     ``dapr_actors`` shapes them (``gather_tool_bindings`` +
     ``gather_write_prompt_fragments``);
  4. ``Agency.run_pack_tool`` → resolve ∩ allow ∩ applicability → governor →
     the ``action_pack_invocations`` ledger — the counter the operator's own
     audit rule says is the proof of life;
  5. the ``journal_proposals`` row.

Assert 4 explicitly: a test that produces a proposal row but no invocation row
would be describing a path production cannot ledger.

Runs against the DISPOSABLE container (conftest), NEVER the live db.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.analysts.agency import Agency, AgencyToolBinding, ToolContext
from legba.data.analysts.agency.journal_propose import (
    JOURNAL_PROPOSE_PACK_ID,
    JOURNAL_PROPOSE_TOOLS,
)
from legba.data.analysts.inline_target import InlineTargetDeps
from legba.data.analysts.journal_assessor import run_method
from legba.data.schemas.action_pack import ActionPack
from legba.runtime.actor_output_emit import _gather_write_bindings_for_target

pytestmark = pytest.mark.asyncio


# --- the pack, exactly as descriptors/action_pack_journal_propose.yaml ships --

_PACK_FRAGMENTS = [
    "You may PROPOSE a change to the world — never make one directly.",
    "Every proposal needs a 'rationale' and a structured 'diff'.",
]


def _propose_pack() -> ActionPack:
    return ActionPack.model_validate(
        {
            "identity": {
                "id": JOURNAL_PROPOSE_PACK_ID,
                "name": "Journal Propose (Human-Gated Agency)",
                "schema_uri": "legba/action_pack/1.0.0",
                "version": "a" * 16,
                "state": "active",
                "owner": "journal_revival",
                "created": "2026-06-25T00:00:00Z",
            },
            "tools": [{"name": t} for t in JOURNAL_PROPOSE_TOOLS],
            "prompt_fragments": _PACK_FRAGMENTS,
        },
        strict=False,
    )


class _ScriptedLLM:
    """Replays a fixed turn list. ``subprovider`` keeps inline_target's
    temperature-drop logic on its normal path."""

    subprovider = "stub-test"

    def __init__(self, turns: list[str]) -> None:
        self.turns = turns
        self.prompts: list[Any] = []
        self._i = 0

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None,
                            system=None, **kw):
        self.prompts.append(messages)
        out = self.turns[min(self._i, len(self.turns) - 1)]
        self._i += 1

        class _R:
            content = out
            usage = {"prompt_tokens": 1, "completion_tokens": 1, "reasoning_tokens": 0}

        return _R()


async def _production_bindings(pg_pool) -> dict[str, Any]:
    """Build the write-binding options payload the way PRODUCTION does.

    The ``base`` dict below mirrors ``dapr_host``'s assembly for a granted write
    pack: one ``AgencyToolBinding`` per tool, ``target_allows=None`` (re-pointed
    per run), and a ToolContext with NO writeback (injected per run). Everything
    after that is the shipped ``_gather_write_bindings_for_target``.
    """
    pack = _propose_pack()
    base_binding = AgencyToolBinding(
        agency=Agency(),
        pack=pack,
        pg_pool=pg_pool,
        tool_context=ToolContext(),
        analyst_grants=[{"pack_id": JOURNAL_PROPOSE_PACK_ID}],
        target_allows=None,            # host leaves this open; the actor re-points
        requested_by="analyst::journal_assessor",
        budget_account="journal_assessor",
    )
    base = {
        "bindings": {t: base_binding for t in JOURNAL_PROPOSE_TOOLS},
        "web_fragments": None,
        "write_fragments": _PACK_FRAGMENTS,
    }
    async with pg_pool.acquire() as conn:
        return await _gather_write_bindings_for_target(
            conn,
            base=base,
            target_id=None,            # META — the self-allow leg under test
            target_version=None,
            run_id=uuid4(),
            analyst_id="journal_assessor",
            analyst_version="0" * 64,
            nats_publish=None,
        )


def _options(gw: dict[str, Any]) -> dict[str, Any]:
    """Options shaped exactly as ``dapr_actors`` shapes them for a journal run."""
    return {
        "analyst_id": "journal_assessor",
        "agency_binding": None,        # no read binding needed for this path
        "gather_tool_bindings": gw["bindings"],
        "gather_web_prompt_fragments": gw["web_fragments"],
        "gather_write_prompt_fragments": gw["write_fragments"],
    }


async def _invocations_since(pg_pool, marker: set[str]) -> list[tuple[str, str]]:
    """The ``action_pack_invocations`` rows written after ``marker`` was taken.

    The conftest truncates the journal/live tables but NOT the agency ledgers,
    so every ledger assertion here is a DELTA — otherwise a passing test would
    depend on file ordering, which is its own species of fake proof.

    The delta is an ID-SET difference, not a count-and-OFFSET. The ledger's
    primary key is ``gen_random_uuid()``, so ``ORDER BY id`` reads back in
    RANDOM order, not insertion order — ``OFFSET <prior count>`` therefore
    returns an arbitrary subset of ALL rows, old and new alike. Under shuffle
    that is exactly what the 2026-08-10 nightly caught: with a sibling test's
    two ``propose_correction`` rows already in the ledger, the queue test's
    "delta" came back as one of THOSE rows (a 2-in-3 coin flip on which UUID
    sorts last) and the test failed against its own sibling's leftovers.
    """
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, pack_id, tool_name FROM action_pack_invocations "
            "ORDER BY occurred_at, id",
        )
    return [
        (r["pack_id"], r["tool_name"]) for r in rows if str(r["id"]) not in marker
    ]


async def _invocation_marker(pg_pool) -> set[str]:
    """Every ledger row id that already exists — the only order-proof marker
    a random-UUID-keyed, never-truncated table allows."""
    async with pg_pool.acquire() as conn:
        return {
            str(r["id"])
            for r in await conn.fetch("SELECT id FROM action_pack_invocations")
        }


def _turns_ending_in_propose(ref: UUID, propose_args: dict[str, Any]) -> list[str]:
    """PLAN/GATHER is skipped (no read binding), so the arc is:
    field-notes → narrate → PROPOSE turn 1 → PROPOSE turn 2 (decline)."""
    return [
        f"Field notes: the country_critic cadence reads halved [[ref:{ref}]].",
        f"# A halved beat\n\nThe country_critic cadence reads halved "
        f"[[ref:{ref}]].\n\nI keep turning it over.",
        json.dumps({"tool": "propose_change", "args": propose_args}),
        json.dumps({"propose": False}),
    ]


async def test_run_method_queues_a_proposal_through_the_production_binding(pg_pool):
    """The whole point: ``run_method`` — not a hand-called ``_gather`` — reaches
    the propose pack through the production-built binding and lands ONE pending
    row, WITH an ``action_pack_invocations`` ledger row to prove it."""
    ref = uuid4()
    gw = await _production_bindings(pg_pool)
    marker = await _invocation_marker(pg_pool)
    llm = _ScriptedLLM(_turns_ending_in_propose(ref, {
        "rationale": "country_critic's beat reads halved against its declared cadence",
        "diff": {"op": "update_descriptor", "family": "analyst",
                 "descriptor_id": "country_critic",
                 "patch": {"cadence": {"cooldown_seconds": 21000}}},
        "cited_substrate_refs": [str(ref)],
    }))
    deps = InlineTargetDeps(llm=llm, system_prompt="PERSONA", max_rounds=1)

    result = await run_method([{"title": "seed signal"}], _options(gw), deps)

    # (5) exactly one PENDING proposal, and the entry itself still landed.
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT proposal_kind, status, rationale FROM journal_proposals"
        )
    invocations = await _invocations_since(pg_pool, marker)
    assert len(rows) == 1, f"expected exactly one queued proposal, got {rows}"
    assert rows[0]["proposal_kind"] == "change"
    assert rows[0]["status"] == "pending"
    assert result.finding.body, "the journal entry must still be written"

    # (4) THE PROOF OF LIFE. A green propose test with an empty invocation
    # ledger is precisely the shape that shipped a dead capability for months.
    assert invocations == [
        (JOURNAL_PROPOSE_PACK_ID, "propose_change")
    ], f"journal_propose left no invocation ledger row: {invocations}"

    # The trace names the phase, so an operator can see it fired.
    propose_steps = [s for s in result.intermediate_steps if s.get("phase") == "propose"]
    assert any(
        s.get("kind") == "tool_call" and s.get("tool") == "propose_change"
        and s.get("admitted") is True
        for s in propose_steps
    ), f"no admitted propose step in the trace: {propose_steps}"


async def test_the_allow_leg_is_real_not_hand_built(pg_pool):
    """THE GUARD AGAINST THIS FILE BECOMING THE OLD FILE.

    Skip the production re-point and hand ``run_method`` the HOST-shaped base
    binding directly (``target_allows=None``, as the host leaves it). If the
    META self-allow inside ``_gather_write_bindings_for_target`` were decorative
    — or if a future edit replaced it with a hand-built ``target_allows=grants``
    in the test — this would still pass. It must not: production denies here.
    """
    base = AgencyToolBinding(
        agency=Agency(),
        pack=_propose_pack(),
        pg_pool=pg_pool,
        tool_context=ToolContext(),
        analyst_grants=[{"pack_id": JOURNAL_PROPOSE_PACK_ID}],
        target_allows=None,
        requested_by="analyst::journal_assessor",
        budget_account="journal_assessor",
    )
    ref = uuid4()
    llm = _ScriptedLLM(_turns_ending_in_propose(
        ref, {"rationale": "x", "diff": {"op": "y"}}
    ))
    deps = InlineTargetDeps(llm=llm, system_prompt="PERSONA", max_rounds=1)
    result = await run_method([{"title": "seed"}], {
        "analyst_id": "journal_assessor",
        "agency_binding": None,
        "gather_tool_bindings": {t: base for t in JOURNAL_PROPOSE_TOOLS},
        "gather_write_prompt_fragments": _PACK_FRAGMENTS,
    }, deps)

    async with pg_pool.acquire() as conn:
        assert int(await conn.fetchval("SELECT count(*) FROM journal_proposals")) == 0
    assert any(
        s.get("phase") == "propose"
        and s.get("kind") == "tool_call"
        and s.get("admitted") is False
        and "not_allowed" in str(s.get("detail", ""))
        for s in result.intermediate_steps
    ), (
        "an un-re-pointed base binding ADMITTED a propose call — the three-way "
        "gate's allow leg is not doing anything, so the sibling tests prove "
        f"nothing: {result.intermediate_steps}"
    )


async def test_propose_turn_sees_the_entry_and_the_packs_own_fragments(pg_pool):
    """The phase must ask its question AFTER the entry exists — that placement
    IS the fix. Assert the propose turn's prompt actually carries the written
    entry, the pack's operator-authored fragments, and the real tool names."""
    ref = uuid4()
    gw = await _production_bindings(pg_pool)
    llm = _ScriptedLLM(_turns_ending_in_propose(ref, {
        "rationale": "cadence looks halved",
        "diff": {"op": "update_descriptor"},
    }))
    deps = InlineTargetDeps(llm=llm, system_prompt="PERSONA", max_rounds=1)
    await run_method([{"title": "seed"}], _options(gw), deps)

    # Turn 3 is the PROPOSE turn (field-notes, narrate, propose).
    propose_prompt = str(llm.prompts[2])
    assert "A halved beat" in propose_prompt, (
        "the propose turn did not see the entry it is supposed to reason over"
    )
    assert _PACK_FRAGMENTS[0] in propose_prompt, (
        "the pack's own operator-authored guidance never reached the model"
    )
    for name in JOURNAL_PROPOSE_TOOLS:
        assert name in propose_prompt
    # The anti-fabrication anchor: the entry's OWN resolved ref is offered.
    assert str(ref) in propose_prompt


async def test_declining_is_free_and_writes_nothing(pg_pool):
    """Most entries warrant nothing. A decline must cost one turn and leave the
    queue untouched — the phase is a coda, never a nag."""
    ref = uuid4()
    gw = await _production_bindings(pg_pool)
    marker = await _invocation_marker(pg_pool)
    llm = _ScriptedLLM([
        f"Field notes: quiet window [[ref:{ref}]].",
        f"# Quiet\n\nA quiet window [[ref:{ref}]].",
        json.dumps({"propose": False}),
    ])
    deps = InlineTargetDeps(llm=llm, system_prompt="PERSONA", max_rounds=1)
    result = await run_method([{"title": "seed"}], _options(gw), deps)

    async with pg_pool.acquire() as conn:
        assert int(await conn.fetchval("SELECT count(*) FROM journal_proposals")) == 0
    assert await _invocations_since(pg_pool, marker) == []
    assert any(
        s.get("phase") == "propose" and s.get("kind") == "declined"
        for s in result.intermediate_steps
    )


async def test_pack_not_effective_skips_the_phase_entirely(pg_pool):
    """A journal class that does NOT grant journal_propose (chronicle, the four
    lenses, lens_diff) must never be offered the surface. The gate is the SAME
    ``gather_write_prompt_fragments`` signal the GATHER catalog uses — one
    signal, so the prompt and the dispatch can never disagree."""
    ref = uuid4()
    llm = _ScriptedLLM([
        f"Field notes: quiet [[ref:{ref}]].",
        f"# Quiet\n\nA quiet window [[ref:{ref}]].",
    ])
    deps = InlineTargetDeps(llm=llm, system_prompt="PERSONA", max_rounds=1)
    result = await run_method(
        [{"title": "seed"}],
        {"analyst_id": "journal_assessor", "agency_binding": None},
        deps,
    )
    async with pg_pool.acquire() as conn:
        assert int(await conn.fetchval("SELECT count(*) FROM journal_proposals")) == 0
    assert any(
        s.get("phase") == "propose" and s.get("kind") == "pack_not_effective"
        for s in result.intermediate_steps
    )
    # And no extra LLM turn was spent on a pack the class does not hold.
    assert len(llm.prompts) == 2


async def test_per_run_cap_bounds_a_runaway_proposer(pg_pool):
    """'Do NOT propose lightly' is a prompt sentence; the cap is the enforcer.
    A model that proposes on every turn stops at the per-run ceiling."""
    ref = uuid4()
    gw = await _production_bindings(pg_pool)
    always_propose = json.dumps({
        "tool": "propose_correction",
        "args": {"rationale": "stale", "diff": {"op": "supersede_fact"}},
    })
    llm = _ScriptedLLM([
        f"Field notes: [[ref:{ref}]].",
        f"# E\n\nA claim [[ref:{ref}]].",
        always_propose, always_propose, always_propose, always_propose,
    ])
    deps = InlineTargetDeps(llm=llm, system_prompt="PERSONA", max_rounds=1)
    result = await run_method([{"title": "seed"}], _options(gw), deps)

    async with pg_pool.acquire() as conn:
        queued = int(await conn.fetchval("SELECT count(*) FROM journal_proposals"))
    assert queued == 2, f"per-run cap not enforced: {queued} proposals queued"
    assert any(
        s.get("phase") == "propose" and s.get("kind") == "per_run_cap"
        for s in result.intermediate_steps
    )
