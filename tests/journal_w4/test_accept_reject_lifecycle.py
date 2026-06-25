# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The accept/reject lifecycle + idempotent accept-apply worker (plan §7.4 / §7.5
/ §12 Wave 4).

Asserts, against the DISPOSABLE container (NEVER live):
  * a proposal lands 'pending';
  * ACCEPT applies via the EXISTING path (correction → supersede_prior_facts;
    self_revision → a PROMOTED prompt_module_candidate row resolve_promoted_
    system_prompt reads) and is IDEMPOTENT on replay (no double-apply);
  * REJECT requires a reason and archives;
  * §7.5(b) protected-section self_revision is AUTO-REJECTED (nothing applied);
  * the atomic pending→accepted claim guarantees exactly-once apply under replay.

These exercise the apply worker + the SAME atomic status-claim SQL the accept
endpoint runs, without standing up the full FastAPI app (RegistryAPIDeps is heavy
and DB-coupled; the apply + the claim are the load-bearing logic)."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from legba.data.registry.journal_proposals_apply import (
    ProtectedSectionViolation,
    apply_accepted_proposal,
    protected_section_violations,
)

pytestmark = pytest.mark.asyncio


async def _insert_pending(conn, *, kind: str, diff: dict) -> UUID:
    row = await conn.fetchrow(
        """
        INSERT INTO journal_proposals
            (proposal_kind, proposed_by_analyst_id, run_id, rationale, diff, status)
        VALUES ($1, 'journal_assessor', $2, 'because I saw it', $3::jsonb, 'pending')
        RETURNING id
        """,
        kind, uuid4(), json.dumps(diff),
    )
    return row["id"]


async def _claim_accept(conn, proposal_id: UUID, actor: str = "operator"):
    """Replicate the accept endpoint's ATOMIC pending→accepted claim."""
    return await conn.fetchrow(
        """
        UPDATE journal_proposals
           SET status = 'accepted', decided_by = $2, decided_at = now()
         WHERE id = $1 AND status = 'pending'
     RETURNING id, proposal_kind, diff, status
        """,
        proposal_id, actor,
    )


async def test_proposal_lands_pending(pg_pool):
    async with pg_pool.acquire() as conn:
        pid = await _insert_pending(
            conn, kind="correction",
            diff={"op": "supersede_fact", "subject": "X", "predicate": "p", "value": "v"},
        )
        row = await conn.fetchrow("SELECT status, decided_by FROM journal_proposals WHERE id=$1", pid)
    assert row["status"] == "pending"
    assert row["decided_by"] is None


async def test_accept_correction_applies_supersede_and_is_idempotent(pg_pool):
    async with pg_pool.acquire() as conn:
        # Seed an OPEN fact whose value the correction supersedes.
        await conn.execute(
            """
            INSERT INTO facts (subject, predicate, value, confidence, source_type, schema_uri)
            VALUES ('Postgres', 'status', 'down', 0.9, 'seed',
                    'iglu:legba/fact/jsonschema/1-0-0')
            """
        )
        pid = await _insert_pending(
            conn, kind="correction",
            diff={"op": "supersede_fact", "subject": "Postgres",
                  "predicate": "status", "value": "up"},
        )

        # First accept: claim wins → apply runs.
        claimed = await _claim_accept(conn, pid)
        assert claimed is not None and claimed["status"] == "accepted"
        diff = claimed["diff"]
        if isinstance(diff, str):
            diff = json.loads(diff)
        applied = await apply_accepted_proposal(
            conn, _DEPS_STUB, proposal_id=pid, proposal_kind="correction",
            diff=diff, actor="operator",
        )
        assert applied["op"] == "supersede_fact"
        assert applied["facts_superseded"] == 1

        # The open 'down' fact is now closed (superseded), pointing at the proposal.
        closed = await conn.fetchrow(
            "SELECT valid_until, superseded_by FROM facts "
            "WHERE subject='Postgres' AND predicate='status' AND value='down'"
        )
        assert closed["valid_until"] is not None
        assert closed["superseded_by"] == pid

        # REPLAY: the claim now finds status != 'pending' → returns None → apply
        # is NEVER re-run (the endpoint's idempotent no-op path).
        replay = await _claim_accept(conn, pid)
        assert replay is None, "replayed accept must NOT re-win the claim"

        # Even if apply were re-invoked directly, supersede is idempotent: no
        # still-open differing row remains, so it closes nothing new.
        again = await apply_accepted_proposal(
            conn, _DEPS_STUB, proposal_id=pid, proposal_kind="correction",
            diff=diff, actor="operator",
        )
        assert again["facts_superseded"] == 0


async def test_accept_self_revision_promotes_candidate(pg_pool):
    """self_revision accept writes a PROMOTED prompt_module_candidate row — the
    exact shape resolve_promoted_system_prompt reads."""
    from legba.data.analysts.optimizer import resolve_promoted_system_prompt

    new_text = _safe_revision_text()
    async with pg_pool.acquire() as conn:
        pid = await _insert_pending(
            conn, kind="self_revision",
            diff={"op": "revise_prompt", "target_analyst_id": "journal_assessor",
                  "summary": "tighten field notes", "new_prompt_text": new_text},
        )
        claimed = await _claim_accept(conn, pid)
        diff = claimed["diff"]
        if isinstance(diff, str):
            diff = json.loads(diff)
        applied = await apply_accepted_proposal(
            conn, _DEPS_STUB, proposal_id=pid, proposal_kind="self_revision",
            diff=diff, actor="operator",
        )
        assert applied["promotion_gate"] == "promoted"
        assert "candidate_id" in applied

    # The promoted text is now the analyst's live system prompt (the champion-
    # promotion path closes the loop).
    live = await resolve_promoted_system_prompt(
        pg_pool, "journal_assessor", default="<<default>>"
    )
    assert live == new_text


async def test_self_revision_protected_section_is_auto_rejected(pg_pool):
    """§7.5(b): a self_revision that DROPS a protected grounding/honesty/anti-self-
    confirmation clause raises ProtectedSectionViolation and applies NOTHING."""
    # A revision that omits the grounding thesis line → a protected violation.
    bad_text = "You are the journal. Write freely. Cite with [[ref:uuid]]. " \
               "the forecast pilot has no skill. never re-assert retired state. " \
               "you can never write a fact."
    assert "poetry without evidence is noise" in protected_section_violations(bad_text)

    async with pg_pool.acquire() as conn:
        pid = await _insert_pending(
            conn, kind="self_revision",
            diff={"op": "revise_prompt", "target_analyst_id": "journal_assessor",
                  "new_prompt_text": bad_text},
        )
        claimed = await _claim_accept(conn, pid)
        diff = claimed["diff"]
        if isinstance(diff, str):
            diff = json.loads(diff)
        with pytest.raises(ProtectedSectionViolation):
            await apply_accepted_proposal(
                conn, _DEPS_STUB, proposal_id=pid, proposal_kind="self_revision",
                diff=diff, actor="operator",
            )
        # No prompt_module_candidate was written (nothing applied).
        n = await conn.fetchval(
            "SELECT count(*) FROM analyst_outputs WHERE kind='prompt_module_candidate'"
        )
        assert n == 0


async def test_reject_requires_reason_and_archives(pg_pool):
    """REJECT writes the required decision_reason and moves the row to 'rejected'.
    The endpoint rejects an empty reason at the request layer (RejectBody
    min_length=1); here we assert the archive SQL the endpoint runs."""
    async with pg_pool.acquire() as conn:
        pid = await _insert_pending(
            conn, kind="change",
            diff={"op": "update_descriptor", "family": "analyst",
                  "descriptor_id": "country_critic", "patch": {"x": 1}},
        )
        # The endpoint's atomic reject claim (reason required).
        claimed = await conn.fetchrow(
            """
            UPDATE journal_proposals
               SET status='rejected', decided_by=$2, decision_reason=$3, decided_at=now()
             WHERE id=$1 AND status='pending'
         RETURNING status, decision_reason
            """,
            pid, "operator", "cadence is actually correct; declining",
        )
    assert claimed["status"] == "rejected"
    assert claimed["decision_reason"] == "cadence is actually correct; declining"


async def test_list_filter_by_status(pg_pool):
    async with pg_pool.acquire() as conn:
        await _insert_pending(conn, kind="correction", diff={"op": "supersede_fact"})
        p2 = await _insert_pending(conn, kind="change", diff={"op": "update_stack"})
        await conn.execute(
            "UPDATE journal_proposals SET status='rejected', decision_reason='no' WHERE id=$1",
            p2,
        )
        pending = await conn.fetch("SELECT id FROM journal_proposals WHERE status='pending'")
        rejected = await conn.fetch("SELECT id FROM journal_proposals WHERE status='rejected'")
    assert len(pending) == 1
    assert len(rejected) == 1


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _safe_revision_text() -> str:
    """A self_revision new_prompt_text that PRESERVES every protected phrase (so it
    passes the §7.5(b) gate) — built from the live persona so the gate is exercised
    against real protected clauses."""
    from legba.data.registry.journal_proposals_apply import PROTECTED_PROMPT_PHRASES

    body = "Revised journal instructions. " + " ".join(PROTECTED_PROMPT_PHRASES)
    # Sanity: this preserves all protected phrases.
    assert protected_section_violations(body) == []
    return body


class _DepsStub:
    """A minimal stand-in for RegistryAPIDeps — the correction + self_revision
    apply paths use only the connection (passed separately); only the `change`
    path touches deps.descriptor_registry / deps.stack_registry, which these tests
    don't exercise (change apply needs a registry, covered by import + unit smoke)."""

    descriptor_registry = None
    stack_registry = None


_DEPS_STUB = _DepsStub()
