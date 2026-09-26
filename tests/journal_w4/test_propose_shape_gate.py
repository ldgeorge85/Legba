# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""H10 — journal proposals learn the three apply shapes (plan §7.4).

The apply worker (``journal_proposals_apply.py``) has always recognised
EXACTLY three diff shapes — ``correction``/``supersede_fact``,
``change``/``update_descriptor`` or ``update_stack``, and
``self_revision``/the champion-promotion shape. The journal's proposal
instruction never named them, so a free-form diff sailed through
``propose_*`` straight to a 'pending' row and only failed — with a
``ProposalApplyError`` — the moment an operator clicked ACCEPT. All 30
pending proposals of 2026-08/09 were exactly this: free-form, unapplyable,
and undiscovered until accept time.

This file proves the fix at the WRITE boundary (``journal_propose.py``'s
``_propose``), which now imports the apply worker's OWN
``validate_proposal_shape`` / ``check_supersede_fact_grounding`` — never a
duplicated copy of the shape rules — so:

  * a diff that fits none of the three shapes is stored
    ``status='archived'``, ``decision_reason='unapplyable_shape'`` — never
    'pending';
  * each of the three shapes, well-formed, still passes and lands 'pending';
  * a ``supersede_fact`` correction that names a subject+predicate with no
    matching OPEN fact in the substrate, or cites no ref that resolves to a
    real substrate row (the "the president is X" / "the king is alive"
    failure mode — a correction recalled from the model's own memory, never
    read off the substrate), is refused at write with
    ``decision_reason='uncited_world_fact'``.

Runs against the DISPOSABLE container (conftest), NEVER the live db.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from legba.data.analysts.agency.journal_propose import (
    propose_change_tool,
    propose_correction_tool,
    propose_self_revision_tool,
)
from legba.data.analysts.agency.tools import ToolCall, ToolContext, WritebackContext
from legba.data.provenance import AnalystContext
from legba.data.registry.journal_proposals_apply import (
    ProposalApplyError,
    UncitedWorldFactError,
    check_supersede_fact_grounding,
    validate_proposal_shape,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Pure unit tests — validate_proposal_shape (no I/O, no DB).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "proposal_kind, diff",
    [
        ("correction", {"op": "supersede_fact", "subject": "Postgres",
                         "predicate": "status", "value": "up"}),
        ("correction", {"op": "merge_entities", "from": "a", "into": "b"}),
        ("correction", {"op": "correct_situation", "situation_id": str(uuid4()),
                         "patch": {}}),
        ("change", {"op": "update_descriptor", "family": "analyst",
                     "descriptor_id": "country_critic",
                     "patch": {"cadence": {"cooldown_seconds": 21000}}}),
        ("change", {"op": "update_stack", "stack_id": "some-stack",
                     "patch": {"x": 1}}),
        ("self_revision", {"op": "revise_prompt",
                            "target_analyst_id": "journal_assessor",
                            "new_prompt_text": "revised instructions",
                            "summary": "why"}),
    ],
)
async def test_each_of_the_three_shapes_validates(proposal_kind, diff):
    """Each of the three recognised shapes, well-formed, passes silently."""
    validate_proposal_shape(proposal_kind, diff)  # must not raise


@pytest.mark.parametrize(
    "proposal_kind, diff",
    [
        # Free-form / not one of the three shapes at all.
        ("correction", {"note": "the country_critic seems slow lately"}),
        ("correction", {"op": "vibes_based_hunch"}),
        ("change", {"observation": "cadence looks halved"}),
        ("self_revision", {"op": "revise_prompt"}),  # missing both required fields
        # Wrong op for the kind.
        ("correction", {"op": "update_descriptor", "family": "analyst",
                         "descriptor_id": "x", "patch": {}}),
        ("change", {"op": "supersede_fact", "subject": "x", "predicate": "y",
                     "value": "z"}),
        # Present op, missing required fields.
        ("correction", {"op": "supersede_fact", "subject": "Postgres"}),
        ("change", {"op": "update_descriptor", "family": "analyst"}),
        ("change", {"op": "update_stack", "patch": {"x": 1}}),
        # Unknown proposal_kind entirely.
        ("vibes", {"op": "anything"}),
    ],
)
async def test_a_diff_that_fits_no_shape_raises(proposal_kind, diff):
    with pytest.raises(ProposalApplyError):
        validate_proposal_shape(proposal_kind, diff)


# ---------------------------------------------------------------------------
# check_supersede_fact_grounding — DB-backed (the no-new-fact rule).
# ---------------------------------------------------------------------------


async def test_grounding_passes_when_fact_exists_and_ref_resolves(pg_pool):
    async with pg_pool.acquire() as conn:
        fact = await conn.fetchrow(
            "INSERT INTO facts (subject, predicate, value, confidence, "
            "source_type, schema_uri) VALUES ('Aliyev', 'role', 'Chairman', "
            "0.9, 'seed', 'iglu:legba/fact/jsonschema/1-0-0') RETURNING id"
        )
        await check_supersede_fact_grounding(
            conn,
            diff={"op": "supersede_fact", "subject": "Aliyev", "predicate": "role",
                  "value": "President"},
            cited_substrate_refs=[fact["id"]],
        )  # must not raise


async def test_grounding_is_a_noop_for_non_supersede_fact_ops(pg_pool):
    """merge_entities / correct_situation are out of this rule's scope."""
    async with pg_pool.acquire() as conn:
        await check_supersede_fact_grounding(
            conn,
            diff={"op": "merge_entities", "from": "a", "into": "b"},
            cited_substrate_refs=[],
        )  # must not raise even with zero refs and no matching fact


async def test_grounding_refuses_no_cited_refs(pg_pool):
    """The four 'from memory' cases (e.g. 'the president is X') carry no
    citation at all — refused before the substrate is even queried for the
    fact."""
    async with pg_pool.acquire() as conn:
        with pytest.raises(UncitedWorldFactError):
            await check_supersede_fact_grounding(
                conn,
                diff={"op": "supersede_fact", "subject": "Ruritania",
                      "predicate": "president", "value": "someone I recall"},
                cited_substrate_refs=[],
            )


async def test_grounding_refuses_an_unknown_cited_ref(pg_pool):
    """A cited ref that resolves to NOTHING in the substrate reads the same as
    no citation — it was invented, not returned by a read tool."""
    async with pg_pool.acquire() as conn:
        with pytest.raises(UncitedWorldFactError):
            await check_supersede_fact_grounding(
                conn,
                diff={"op": "supersede_fact", "subject": "Ruritania",
                      "predicate": "president", "value": "someone I recall"},
                cited_substrate_refs=[uuid4()],  # never written anywhere
            )


async def test_grounding_refuses_when_no_matching_fact_exists(pg_pool):
    """A cited ref IS real (e.g. an unrelated signal/finding the journal did
    read), but the substrate carries no open fact for the named
    subject+predicate at all — "may only name a fact that exists in the
    substrate"."""
    async with pg_pool.acquire() as conn:
        real_row = await conn.fetchrow(
            "INSERT INTO analyst_outputs (kind, title, body, schema_uri, data) "
            "VALUES ('finding', 'unrelated', 'body', "
            "'iglu:legba/finding/jsonschema/1-0-0', '{}'::jsonb) RETURNING id"
        )
        with pytest.raises(UncitedWorldFactError):
            await check_supersede_fact_grounding(
                conn,
                diff={"op": "supersede_fact", "subject": "Ruritania",
                      "predicate": "king_is_alive", "value": "true"},
                cited_substrate_refs=[real_row["id"]],
            )


@pytest.mark.parametrize(
    "subject, predicate, value",
    [
        ("Freedonia", "president", "Rufus T. Firefly (from memory)"),
        ("Ruritania", "king_is_alive", "true (from memory)"),
        ("Elbonia", "head_of_state", "General Alcazar (from memory)"),
        ("Genovia", "monarch_status", "reigning (from memory)"),
    ],
)
async def test_the_four_world_fact_from_memory_cases_are_refused(
    pg_pool, subject, predicate, value,
):
    """The exact failure mode this rule exists to stop: a correction that
    asserts a world fact recalled from the model's own parametric memory,
    naming a subject+predicate the substrate has never actually carried a
    fact for, with no real citation."""
    async with pg_pool.acquire() as conn:
        with pytest.raises(UncitedWorldFactError):
            await check_supersede_fact_grounding(
                conn,
                diff={"op": "supersede_fact", "subject": subject,
                      "predicate": predicate, "value": value},
                cited_substrate_refs=[],
            )


# ---------------------------------------------------------------------------
# The full write path — propose_*_tool now archives-on-arrival instead of
# leaving an unapplyable diff 'pending'.
# ---------------------------------------------------------------------------


def _ctx_with_writeback(pg_pool) -> ToolContext:
    analyst_ctx = AnalystContext(
        analyst_id="journal_assessor",
        analyst_version="0" * 64,
        run_id=uuid4(),
        target_id=None,
        target_version=None,
    )
    return ToolContext(
        writeback=WritebackContext(
            pg_pool=pg_pool, analyst_ctx=analyst_ctx, publish_fn=None
        )
    )


async def _row(pg_pool, proposal_id):
    async with pg_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT status, decision_reason FROM journal_proposals WHERE id = $1",
            proposal_id,
        )


@pytest.mark.parametrize(
    "tool, tool_name, diff",
    [
        (propose_correction_tool, "propose_correction",
         {"note": "the country_critic cadence seems off, not sure how to phrase it"}),
        (propose_change_tool, "propose_change",
         {"observation": "cadence looks halved"}),
        (propose_self_revision_tool, "propose_self_revision",
         {"thought": "I wonder if I should write differently"}),
    ],
)
async def test_a_free_form_diff_is_archived_with_unapplyable_shape(
    pg_pool, tool, tool_name, diff,
):
    ctx = _ctx_with_writeback(pg_pool)
    call = ToolCall(
        pack_id="journal_propose", tool_name=tool_name,
        args={"rationale": "an observation, not sure it fits a shape",
              "diff": diff, "cited_substrate_refs": [str(uuid4())]},
        requested_by="analyst::journal_assessor",
    )

    result = await tool(call, _PACK, ctx)

    assert result.status == "completed", result.error
    assert result.output["status"] == "archived"
    assert result.output["decision_reason"] == "unapplyable_shape"

    row = await _row(pg_pool, result.output["proposal_id"])
    assert row["status"] == "archived"
    assert row["decision_reason"] == "unapplyable_shape"


async def test_supersede_fact_with_unknown_ref_is_archived_uncited_world_fact(
    pg_pool,
):
    ctx = _ctx_with_writeback(pg_pool)
    call = ToolCall(
        pack_id="journal_propose", tool_name="propose_correction",
        args={
            "rationale": "I believe the president has changed",
            "diff": {"op": "supersede_fact", "subject": "Freedonia",
                     "predicate": "president", "value": "Rufus T. Firefly"},
            "cited_substrate_refs": [str(uuid4())],  # resolves to nothing
        },
        requested_by="analyst::journal_assessor",
    )

    result = await propose_correction_tool(call, _PACK, ctx)

    assert result.status == "completed", result.error
    assert result.output["status"] == "archived"
    assert result.output["decision_reason"] == "uncited_world_fact"

    row = await _row(pg_pool, result.output["proposal_id"])
    assert row["status"] == "archived"
    assert row["decision_reason"] == "uncited_world_fact"


async def test_supersede_fact_with_no_cited_ref_is_archived_uncited_world_fact(
    pg_pool,
):
    ctx = _ctx_with_writeback(pg_pool)
    call = ToolCall(
        pack_id="journal_propose", tool_name="propose_correction",
        args={
            "rationale": "the king is alive, I recall",
            "diff": {"op": "supersede_fact", "subject": "Ruritania",
                     "predicate": "king_is_alive", "value": "true"},
            "cited_substrate_refs": [],
        },
        requested_by="analyst::journal_assessor",
    )

    result = await propose_correction_tool(call, _PACK, ctx)

    assert result.status == "completed", result.error
    assert result.output["status"] == "archived"
    assert result.output["decision_reason"] == "uncited_world_fact"


async def test_a_grounded_supersede_fact_still_lands_pending(pg_pool):
    """The counter-proof: a well-formed, grounded correction is unaffected —
    it still lands 'pending' with no decision_reason."""
    async with pg_pool.acquire() as conn:
        fact = await conn.fetchrow(
            "INSERT INTO facts (subject, predicate, value, confidence, "
            "source_type, schema_uri) VALUES ('Postgres', 'status', 'down', "
            "0.9, 'seed', 'iglu:legba/fact/jsonschema/1-0-0') RETURNING id"
        )
    ctx = _ctx_with_writeback(pg_pool)
    call = ToolCall(
        pack_id="journal_propose", tool_name="propose_correction",
        args={
            "rationale": "I read this from my own run health instrument",
            "diff": {"op": "supersede_fact", "subject": "Postgres",
                     "predicate": "status", "value": "up"},
            "cited_substrate_refs": [str(fact["id"])],
        },
        requested_by="analyst::journal_assessor",
    )

    result = await propose_correction_tool(call, _PACK, ctx)

    assert result.status == "completed", result.error
    assert result.output["status"] == "pending"
    assert "decision_reason" not in result.output

    row = await _row(pg_pool, result.output["proposal_id"])
    assert row["status"] == "pending"
    assert row["decision_reason"] is None


# A minimal ActionPack stand-in (the handlers only read pack.identity.id).
from legba.data.schemas.action_pack import ActionPack  # noqa: E402

_PACK = ActionPack.model_validate(
    {
        "identity": {
            "id": "journal_propose",
            "name": "journal_propose",
            "schema_uri": "legba/action_pack/1.0.0",
            "version": "a" * 16,
            "state": "active",
            "owner": "journal_revival",
            "created": "2026-06-25T00:00:00Z",
        },
        "tools": [
            {"name": "propose_correction"},
            {"name": "propose_change"},
            {"name": "propose_self_revision"},
        ],
    },
    strict=False,
)
