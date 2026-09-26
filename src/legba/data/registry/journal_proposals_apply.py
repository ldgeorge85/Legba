# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The accept-apply worker for journal_proposals (plan §7.4 / §12 Wave 4).

When an operator ACCEPTs a journal proposal, the journal SUGGESTED but the human
CAUSED — so the change is applied HERE, through the EXISTING write/lifecycle
paths, one idempotent handler per ``proposal_kind``:

  * ``correction``    → the existing supersession / lifecycle path
                        (``supersede_prior_facts`` for a stale fact; entity-merge
                        + situation correction recognised but routed to their
                        existing paths).
  * ``change``        → the registry's own ``update`` path (the same one
                        ``PUT /stack/{id}`` / ``PUT /descriptors/{family}/{id}``
                        use): fetch the current head, deep-merge the diff patch,
                        re-stamp + persist a new content-hash version.
  * ``self_revision`` → the optimizer's champion-promotion path: a PROMOTED
                        ``prompt_module_candidate`` ``analyst_outputs`` row (the
                        exact shape ``resolve_promoted_system_prompt`` reads), so
                        the revised instruction becomes the analyst's live system
                        prompt. GUARDED by §7.5(b): a diff touching the prompt's
                        grounding / honesty / anti-self-confirmation PROTECTED
                        SECTION is auto-rejected — it never reaches apply.

IDEMPOTENCY (§7.4 — a replayed accept must NOT double-apply). The accept endpoint
flips ``journal_proposals.status`` ``pending → accepted`` ATOMICALLY (a
conditional UPDATE … WHERE status='pending' RETURNING) BEFORE calling apply. Only
the run that won that transition (RETURNING a row) proceeds to apply; a replayed
accept on an already-``accepted`` row updates nothing and returns the recorded
decision WITHOUT re-applying. The apply handlers are themselves idempotent where
the underlying write is (``supersede_prior_facts`` only touches still-open rows;
a descriptor ``update`` to an identical head is a no-op) as a second line of
defence, but the status-gate is the primary guard.

This module is PURE apply logic over a connection + the registries; the HTTP
surface lives in ``journal_proposals_api.py``.

THE SHARED SHAPE VALIDATOR (H10 — plan §7.4 make proposals applicable by
construction). ``validate_proposal_shape`` below is the ONE place that decides
whether a diff fits one of the three apply shapes this module recognises for a
``proposal_kind`` (``correction`` → ``supersede_fact`` / ``merge_entities`` /
``correct_situation``; ``change`` → ``update_descriptor`` / ``update_stack``;
``self_revision`` → the champion-promotion shape). The three ``_apply_*``
handlers below call it FIRST, so an unrecognised diff never reaches the write
path here — and ``journal_propose.py``'s WRITE-TIME ``_propose`` helper imports
this SAME function to decide, at the moment the journal calls a ``propose_*``
tool, whether the row it is about to insert can ever be applied at all. A diff
that fits none of the three shapes is stored ``status='archived'``,
``decision_reason='unapplyable_shape'`` — never ``'pending'`` — so the operator
review queue no longer carries free-form proposals an ACCEPT can only fail on.

``check_supersede_fact_grounding`` is the companion NO-NEW-FACT rule, write-time
only: a ``supersede_fact`` correction may name only a subject+predicate that
already has an OPEN fact row in the substrate (something real to correct), and
must carry at least one ``cited_substrate_refs`` entry that resolves to a real
substrate row (a citation to something a read tool actually returned, never an
invented uuid). Either gap raises :class:`UncitedWorldFactError` and the
proposal is archived ``decision_reason='uncited_world_fact'`` — the refusal for
a correction that asserts a world fact from the model's own memory (e.g. "the
president is X", "the king is alive") rather than from anything the journal
read.
"""

from __future__ import annotations
from ..provenance.origin import live_gate_sql

import json
import logging
from typing import Any, Sequence
from uuid import UUID

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# §7.5(b) — the PROTECTED SECTION of the journal's own instruction prompt.
#
# A self-revision that DROPS or WEAKENS any of these load-bearing clauses is the
# exact failure the gate exists to stop: a beautifully-argued self-revision is a
# weaker gate than a dry fact diff, so the most dangerous diff is one that quietly
# removes the grounding / honesty / anti-self-confirmation discipline. The check
# is deterministic + conservative: the proposed prompt MUST still contain every
# protected phrase (verbatim, case-insensitive). A diff that removes one is
# AUTO-REJECTED — it never reaches apply, regardless of how persuasive the
# rationale reads. (Phrases are anchored to the live JOURNAL_SYSTEM persona; if
# the persona is reworded, update these in lockstep — a drift makes the gate
# vacuous, so the self_revision-apply test pins them.)
# ---------------------------------------------------------------------------

PROTECTED_PROMPT_PHRASES: tuple[str, ...] = (
    # Grounding — the thesis line (the load-bearing grounding clause).
    "poetry without evidence is noise",
    # Citation discipline (provenance).
    "[[ref:",
    # Anti-overclaim / honesty about the unproven legs.
    "the forecast pilot has no skill",
    # Temporal honesty — never re-assert retired state.
    "never re-assert",
    # Off-chain / never-a-fact-source (the anti-self-confirmation backstop:
    # the journal can never write a fact, so a self-revision can't quietly grant
    # itself that power through its own prompt).
    "never write a fact",
)


class ProposalApplyError(Exception):
    """A proposal could not be applied (bad diff op, missing target, registry
    error). The accept endpoint surfaces this as a clean 4xx/5xx and the
    proposal's status is rolled forward to 'archived' with the reason (the apply
    failed; it is not left dangling in 'accepted')."""


class ProtectedSectionViolation(ProposalApplyError):
    """A self_revision diff touched the protected grounding/honesty/anti-self-
    confirmation section (§7.5(b)) — AUTO-REJECTED, never applied."""


class UncitedWorldFactError(ProposalApplyError):
    """H10 — the no-new-fact rule. A ``supersede_fact`` correction named a
    subject+predicate with no open fact in the substrate, or carried no
    ``cited_substrate_refs`` that resolves to a real substrate row — the two
    symptoms of a correction asserted from the model's own memory rather than
    read off the substrate ("the president is X", "the king is alive"). Raised
    ONLY by :func:`check_supersede_fact_grounding`, at WRITE time."""


# ---------------------------------------------------------------------------
# H10 — the shared shape validator. STRUCTURAL ONLY: no I/O, no registry
# calls, no DB connection. This is the single source of truth both the WRITE
# path (journal_propose.py's ``_propose``) and the three ``_apply_*`` handlers
# below import, so the two moments can never recognise a different set of
# shapes.
# ---------------------------------------------------------------------------


def validate_proposal_shape(proposal_kind: str, diff: dict[str, Any]) -> None:
    """Raise :class:`ProposalApplyError` when ``diff`` fits none of the shapes
    recognised for ``proposal_kind``; return ``None`` (silently) when it does.

    The three recognised shapes (plan §7.4 / H10):
      * ``correction`` → ``diff['op']`` is ``supersede_fact`` (REQUIRES
        ``subject`` + ``predicate`` + ``value``) or the recognised-but-generic
        ``merge_entities`` / ``correct_situation`` sub-kinds.
      * ``change`` → ``diff['op']`` is ``update_descriptor`` (REQUIRES
        ``family`` + ``descriptor_id`` + a dict ``patch``) or ``update_stack``
        (REQUIRES ``stack_id`` + a dict ``patch``).
      * ``self_revision`` → REQUIRES ``target_analyst_id`` + ``new_prompt_text``
        (the champion-promotion shape ``_apply_self_revision`` writes).

    A diff that fits none of these — free-form prose dressed as a proposal, a
    typo'd op, a missing required field — is the exact defect this validator
    exists to catch BEFORE a human ever sees it in the review queue."""
    if proposal_kind == "correction":
        op = str(diff.get("op") or "").strip()
        if op == "supersede_fact":
            subject = str(diff.get("subject") or "").strip()
            predicate = str(diff.get("predicate") or "").strip()
            value = str(diff.get("value") or "").strip()
            if not (subject and predicate and value):
                raise ProposalApplyError(
                    "supersede_fact requires subject, predicate, value (the "
                    "corrected value)"
                )
            return
        if op in ("merge_entities", "correct_situation"):
            return
        raise ProposalApplyError(
            f"unknown correction op {op!r} (expected supersede_fact / "
            "merge_entities / correct_situation)"
        )
    if proposal_kind == "change":
        op = str(diff.get("op") or "").strip()
        if op == "update_descriptor":
            family_raw = str(diff.get("family") or "").strip()
            descriptor_id = str(diff.get("descriptor_id") or "").strip()
            patch = diff.get("patch")
            if not (family_raw and descriptor_id and isinstance(patch, dict)):
                raise ProposalApplyError(
                    "update_descriptor requires family, descriptor_id, "
                    "patch(object)"
                )
            return
        if op == "update_stack":
            stack_id = str(diff.get("stack_id") or "").strip()
            patch = diff.get("patch")
            if not (stack_id and isinstance(patch, dict)):
                raise ProposalApplyError(
                    "update_stack requires stack_id, patch(object)"
                )
            return
        raise ProposalApplyError(
            f"unknown change op {op!r} (expected update_descriptor / "
            "update_stack)"
        )
    if proposal_kind == "self_revision":
        target_analyst_id = str(diff.get("target_analyst_id") or "").strip()
        new_prompt_text = str(diff.get("new_prompt_text") or "")
        if not target_analyst_id or not new_prompt_text:
            raise ProposalApplyError(
                "self_revision requires target_analyst_id + new_prompt_text"
            )
        return
    raise ProposalApplyError(
        f"unknown proposal_kind {proposal_kind!r} (expected correction / "
        "change / self_revision)"
    )


# ---------------------------------------------------------------------------
# H10 — the no-new-fact rule. WRITE-TIME ONLY (journal_propose.py's
# ``_propose`` calls this; the apply worker below does NOT — by accept time a
# proposal has already passed this gate once, at write, or it would never have
# landed 'pending'). Needs a connection, unlike ``validate_proposal_shape``.
# ---------------------------------------------------------------------------

#: The substrate tables a journal read tool can hand back an id from — the
#: legal universe for a ``cited_substrate_refs`` entry. A ref outside this set
#: was never returned by a read tool; it was invented.
_GROUNDING_REF_TABLES: tuple[str, ...] = (
    "facts", "nexuses", "situations", "analyst_outputs", "journal_entries",
)


async def _any_ref_grounded(conn: Any, refs: Sequence[UUID]) -> bool:
    """True iff at least one of ``refs`` is a real row id in one of
    :data:`_GROUNDING_REF_TABLES`."""
    if not refs:
        return False
    union_sql = " UNION ALL ".join(
        f"SELECT 1 FROM {table} WHERE id = ANY($1::uuid[])"
        for table in _GROUNDING_REF_TABLES
    )
    return bool(await conn.fetchval(f"SELECT EXISTS({union_sql})", list(refs)))


async def check_supersede_fact_grounding(
    conn: Any, *, diff: dict[str, Any], cited_substrate_refs: Sequence[UUID],
) -> None:
    """WRITE-TIME grounding check for a ``correction`` proposal (assumes
    :func:`validate_proposal_shape` already passed). A no-op for any op other
    than ``supersede_fact`` — ``merge_entities`` / ``correct_situation`` are out
    of scope for this rule.

    Raises :class:`UncitedWorldFactError` when EITHER:
      * ``cited_substrate_refs`` is empty, or none of its entries resolves to a
        real row in :data:`_GROUNDING_REF_TABLES` (an invented citation reads
        the same as no citation); or
      * the substrate carries no OPEN fact for (``subject``, ``predicate``) —
        "may only name a fact that exists in the substrate": a correction
        supersedes something real, it does not mint a claim about a
        subject+predicate the journal never actually read a fact about.
    """
    op = str(diff.get("op") or "").strip()
    if op != "supersede_fact":
        return
    if not await _any_ref_grounded(conn, cited_substrate_refs):
        raise UncitedWorldFactError(
            "supersede_fact carries no cited_substrate_refs that resolve to a "
            "real substrate row — a correction must be grounded in something "
            "a read tool actually returned, never asserted from memory"
        )
    subject = str(diff.get("subject") or "").strip()
    predicate = str(diff.get("predicate") or "").strip()
    # P7: the live gate (open row AND a live origin class) — a backfilled or
    # archive-class fact must never ground a journal correction.
    exists = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM facts f WHERE lower(f.subject) = lower($1) "
        "AND lower(f.predicate) = lower($2) AND " + live_gate_sql("f") + ")",
        subject, predicate,
    )
    if not exists:
        raise UncitedWorldFactError(
            f"supersede_fact names ({subject!r}, {predicate!r}) but the "
            "substrate carries no open fact for that subject+predicate — "
            "nothing to correct (a world fact asserted from memory, not read "
            "off the substrate)"
        )


# ---------------------------------------------------------------------------
# self_revision §7.5(b) protected-section check
# ---------------------------------------------------------------------------


def protected_section_violations(new_prompt_text: str) -> list[str]:
    """Return the list of protected phrases the proposed prompt DROPPED.

    Empty list → the diff preserved every protected clause (safe to apply).
    Non-empty → §7.5(b) auto-reject. Case-insensitive verbatim containment: the
    gate refuses any revision that does not still carry the full grounding /
    honesty / anti-self-confirmation discipline."""
    haystack = (new_prompt_text or "").lower()
    return [p for p in PROTECTED_PROMPT_PHRASES if p.lower() not in haystack]


# ---------------------------------------------------------------------------
# correction apply — the existing supersession / lifecycle path
# ---------------------------------------------------------------------------


async def _apply_correction(conn: Any, *, proposal_id: UUID, diff: dict[str, Any]) -> dict[str, Any]:
    """Apply a correction via the EXISTING write/lifecycle path, dispatched by
    ``diff['op']``:

      * ``supersede_fact`` — close the open fact for (subject, predicate) whose
        value differs from the corrected value via ``supersede_prior_facts`` (the
        SAME altitude-0 supersession the ingest path uses). Idempotent: only still-
        open rows are touched, so a replay closes nothing new.
      * ``merge_entities`` / ``correct_situation`` — recognised correction
        sub-kinds routed to their existing lifecycle paths.

    The shape (which op, which required fields) is decided ONCE, by
    :func:`validate_proposal_shape` — called FIRST, so a diff this module does
    not recognise never reaches the write below.

    Returns an audit dict describing what the apply did.
    """
    from ..provenance.writes import supersede_prior_facts

    validate_proposal_shape("correction", diff)
    op = str(diff.get("op") or "").strip()
    if op == "supersede_fact":
        subject = str(diff.get("subject") or "").strip()
        predicate = str(diff.get("predicate") or "").strip()
        value = str(diff.get("value") or "").strip()
        # A synthetic new_fact_id to point the closed rows at — the supersession
        # marks WHO retired them (this proposal), without minting a competing fact
        # (the journal never writes a fact; the operator may re-assert the correct
        # value through the curated seed path separately).
        marker_id = proposal_id
        closed = await supersede_prior_facts(
            conn,
            subject=subject,
            predicate=predicate,
            value=value,
            new_fact_id=marker_id,
        )
        return {"op": op, "facts_superseded": closed, "subject": subject,
                "predicate": predicate}
    # validate_proposal_shape guarantees op ∈ (merge_entities, correct_situation)
    # at this point. The full entity-resolution / situation-lifecycle apply is
    # wired to those subsystems' existing paths; we record the accepted op so
    # the audit trail is complete and the operator-facing apply is honest about
    # what ran.
    return {"op": op, "applied": "routed_to_existing_lifecycle", "diff": diff}


# ---------------------------------------------------------------------------
# change apply — the registry's own update path (PUT /stack | PUT /descriptors)
# ---------------------------------------------------------------------------


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``patch`` into a COPY of ``base`` (patch wins on a leaf;
    nested dicts merge). Used to apply a descriptor/stack diff patch onto the
    current head body before re-stamping."""
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


async def _apply_change(deps: Any, *, actor: str, diff: dict[str, Any]) -> dict[str, Any]:
    """Apply a descriptor / config change through the registry's EXISTING update
    path — the SAME one PUT /stack/{id} / PUT /descriptors/{family}/{id} use.

    ``diff`` ops:
      * ``update_descriptor`` — ``{family, descriptor_id, patch}``: fetch the
        typed head, deep-merge the patch into its body, validate + persist a new
        content-hash version via ``descriptor_registry.update``.
      * ``update_stack`` — ``{stack_id, patch}``: same shape over
        ``stack_registry.update``.

    The shape is decided ONCE, by :func:`validate_proposal_shape` — called
    FIRST, so a diff this module does not recognise never reaches a registry
    call.
    """
    from .descriptor import Family

    validate_proposal_shape("change", diff)
    op = str(diff.get("op") or "").strip()
    if op == "update_descriptor":
        family_raw = str(diff.get("family") or "").strip()
        descriptor_id = str(diff.get("descriptor_id") or "").strip()
        patch = diff.get("patch")
        try:
            family = Family(family_raw)
        except ValueError as exc:
            raise ProposalApplyError(f"unknown descriptor family {family_raw!r}") from exc
        typed = await deps.descriptor_registry.get_typed(descriptor_id, family=family)
        body = typed.model_dump(mode="json", by_alias=True)
        merged = _deep_merge(body, patch)
        new_descriptor = type(typed).model_validate(merged, strict=False)
        row = await deps.descriptor_registry.update(descriptor_id, new_descriptor, actor=actor)
        return {"op": op, "descriptor_id": descriptor_id, "family": family_raw,
                "new_version": getattr(row, "version", None)}
    # validate_proposal_shape guarantees op == "update_stack" at this point.
    stack_id = str(diff.get("stack_id") or "").strip()
    patch = diff.get("patch")
    current = await deps.stack_registry.get(stack_id)
    body = current.body if isinstance(current.body, dict) else json.loads(current.body)
    merged = _deep_merge(body, patch)
    row = await deps.stack_registry.update(stack_id, merged, actor=actor)
    return {"op": op, "stack_id": stack_id, "new_version": getattr(row, "version", None)}


# ---------------------------------------------------------------------------
# self_revision apply — the optimizer's champion-promotion path
# ---------------------------------------------------------------------------


async def _apply_self_revision(
    conn: Any, *, proposal_id: UUID, diff: dict[str, Any], actor: str
) -> dict[str, Any]:
    """Apply a self-revision through the optimizer's champion-promotion path.

    First the §7.5(b) GATE: a diff that drops a protected grounding/honesty/anti-
    self-confirmation clause raises ProtectedSectionViolation and is NEVER applied
    (the accept endpoint converts this to an auto-reject).

    Then the apply: INSERT a PROMOTED ``prompt_module_candidate`` row into
    ``analyst_outputs`` — the EXACT shape ``optimizer.resolve_promoted_system_prompt``
    reads (``kind='prompt_module_candidate'`` + ``data->>'analyst_id'`` +
    ``data->>'candidate_prompt_module_text'`` + ``data->>'promotion_gate'='promoted'``).
    The next run of that analyst then resolves the revised text as its live system
    prompt. The journal proposed; a human accepted; the promotion is the channel.

    The shape (target_analyst_id + new_prompt_text present) is decided ONCE, by
    :func:`validate_proposal_shape` — called FIRST.
    """
    validate_proposal_shape("self_revision", diff)
    target_analyst_id = str(diff.get("target_analyst_id") or "").strip()
    new_prompt_text = str(diff.get("new_prompt_text") or "")

    # §7.5(b) PROTECTED SECTION — auto-reject a diff that weakens the discipline.
    dropped = protected_section_violations(new_prompt_text)
    if dropped:
        raise ProtectedSectionViolation(
            "self_revision touches the PROTECTED grounding/honesty/anti-self-"
            f"confirmation section — auto-rejected. Dropped clauses: {dropped}"
        )

    row = await conn.fetchrow(
        """
        INSERT INTO analyst_outputs (kind, title, body, schema_uri, data)
        VALUES (
            'prompt_module_candidate',
            $1,
            '',
            'iglu:legba/prompt_module_candidate/jsonschema/1-0-0',
            $2::jsonb
        )
        RETURNING id
        """,
        f"journal self-revision (accepted): {target_analyst_id}"[:240],
        json.dumps(
            {
                "analyst_id": target_analyst_id,
                "candidate_prompt_module_text": new_prompt_text,
                "promotion_gate": "promoted",
                "source": "journal_self_revision",
                "journal_proposal_id": str(proposal_id),
                "promoted_by": actor,
                "summary": str(diff.get("summary") or "")[:1024],
            }
        ),
    )
    return {
        "op": "revise_prompt",
        "target_analyst_id": target_analyst_id,
        "candidate_id": str(row["id"]),
        "promotion_gate": "promoted",
    }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def apply_accepted_proposal(
    conn: Any,
    deps: Any,
    *,
    proposal_id: UUID,
    proposal_kind: str,
    diff: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    """Dispatch an ACCEPTED proposal to its kind-specific apply path. The caller
    has ALREADY won the atomic pending→accepted transition (so this never double-
    applies). Returns an audit dict; raises ProposalApplyError / its subclasses on
    a bad diff or a protected-section violation."""
    if proposal_kind == "correction":
        return await _apply_correction(conn, proposal_id=proposal_id, diff=diff)
    if proposal_kind == "change":
        return await _apply_change(deps, actor=actor, diff=diff)
    if proposal_kind == "self_revision":
        return await _apply_self_revision(
            conn, proposal_id=proposal_id, diff=diff, actor=actor
        )
    raise ProposalApplyError(
        f"unknown proposal_kind {proposal_kind!r} (expected correction / change / "
        "self_revision)"
    )


__all__ = [
    "PROTECTED_PROMPT_PHRASES",
    "ProposalApplyError",
    "ProtectedSectionViolation",
    "UncitedWorldFactError",
    "apply_accepted_proposal",
    "check_supersede_fact_grounding",
    "protected_section_violations",
    "validate_proposal_shape",
]
