# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult write executor's ``source`` BRANCH (C-2b-1; design C-2a §1 row 1, §4).

The one class the frame builds end to end. Everything here goes through the
registry's own write paths, in-process — ``DescriptorRegistry.register`` /
``update`` / ``retire`` / ``rollback`` — with the consult write's transaction
hook (``in_tx``) so the mutation, the ledger row and the signed audit row
commit as one.

* :func:`inspect` — the head's allowlisted fields, version, state, the last
  versions, the protected and writable paths, and this session's executed
  writes on the object (the read that seeds the gate's read set and the only
  place a ``write_id`` for ``revert_write`` comes from).
* :func:`plan` — a model call (create / update / transition / retire) turned
  into a :class:`Plan`: the canonical args, the pre-image, the tier, the
  reversal handle and a ``mutate(actor, in_tx)`` that executes it. Refusals
  raise :class:`Refused` with a ``WRITE_BLOCK_CAUSES`` word.
* :func:`plan_revert` — the stored reversal of an executed write as a Plan,
  refused ``superseded_since`` when the post-image is no longer the head.
* :func:`activation` — one probe of a new head (D10): the runtime parser over
  the served shape (``parse_failed``), the first poll under that version
  (``verified``), or ``not_applicable`` for a state no actor polls.

A leaf of the registry package: it imports the descriptor registry, the
vocabulary and the patch leaf — no runtime module, no analyst module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

from .. import consult_write_patch as P
from .. import consult_write_vocabulary as W
from ..schemas.lifecycle import ALLOWED_TRANSITIONS, LifecycleState
from .descriptor import Family
from .errors import DescriptorNotFound

__all__ = ["FAMILY", "Plan", "Refused", "VERBS", "activation", "inspect", "plan", "plan_revert"]

#: The verbs this branch executes — the drift guard holds them equal to the
#: vocabulary's built verbs for ``source``.
VERBS: frozenset[str] = frozenset({"inspect", "create", "update", "transition", "retire", "revert"})

FAMILY = Family.SOURCE


class Refused(Exception):
    """A refusal with a ``WRITE_BLOCK_CAUSES`` cause."""

    def __init__(self, cause: str, detail: str) -> None:
        assert cause in W.WRITE_BLOCK_CAUSES, cause
        super().__init__(f"{cause}: {detail}")
        self.cause, self.detail = cause, detail


Mutate = Callable[..., Awaitable[Any]]


@dataclass
class Plan:
    """One executable source write."""

    cls: str
    verb: str
    object_ref: str
    args: dict[str, Any]
    tier: str
    pre: dict[str, Any] | None
    post_state: str
    reversal: dict[str, Any]
    mutate: Mutate
    #: The head version the mutation must start from (compare-and-set inside
    #: the transaction); None for a create.
    expected_version: str | None = None
    card: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _short(version: str | None) -> str:
    return (version or "")[:8]


async def _head(registry: Any, source_id: str) -> Any:
    try:
        return await registry.get(source_id, family=FAMILY)
    except DescriptorNotFound as exc:
        raise Refused("ref_not_read", f"source {source_id!r} does not exist.") from exc


async def _typed_body(registry: Any, source_id: str) -> dict[str, Any]:
    typed = await registry.get_typed(source_id, family=FAMILY)
    return typed.model_dump(mode="json", by_alias=True)


def _model(body: Mapping[str, Any]) -> Any:
    from ..schemas.source import SourceDescriptor

    return SourceDescriptor.model_validate(dict(body), strict=False)


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


async def inspect(registry: Any, source_id: str, *, session_id: str | None) -> dict[str, Any]:
    try:
        head = await registry.get(source_id, family=FAMILY)
    except DescriptorNotFound:
        return {"found": False, "class": "source", "id": source_id}
    body = await _typed_body(registry, source_id)
    async with registry.pg.acquire() as conn:
        versions = await conn.fetch(
            f"SELECT version, state, is_head, created_at FROM {FAMILY.table} "
            "WHERE descriptor_id = $1 ORDER BY created_at DESC LIMIT $2",
            source_id, W.LIMITS["inspect_versions"],
        )
        writes = await conn.fetch(
            "SELECT id, verb, status, executed_at, reversed_by_write_id, reversal "
            "FROM consult_writes WHERE class = 'source' AND object_ref = $1 "
            "AND session_id = $2::uuid AND status = 'executed' AND verb <> 'revert' "
            "ORDER BY created_at DESC LIMIT 10",
            source_id, session_id,
        ) if session_id else []
    return {
        "found": True,
        "class": "source",
        "id": source_id,
        "name": head.name,
        "version": head.version,
        "state": head.state,
        "fields": P.editable_view("source", body),
        "writable": list(W.writable_paths("source")),
        "protected": ["identity.id", "identity.version", "schema_uri", "config.*_secret"],
        "versions": [
            {"version": r["version"], "state": r["state"], "head": r["is_head"],
             "created_at": r["created_at"].isoformat()}
            for r in versions
        ],
        "writes": [
            {"write_id": str(r["id"]), "verb": r["verb"],
             "executed_at": r["executed_at"].isoformat() if r["executed_at"] else None,
             "reverted": r["reversed_by_write_id"] is not None}
            for r in writes
        ],
        "confirmation": (
            "activate, pause of an active source and retire need the operator's "
            "confirmation; retire cannot be undone — prefer paused."
        ),
    }


# ---------------------------------------------------------------------------
# plan — a model call → an executable write
# ---------------------------------------------------------------------------


def _legal(from_state: str, to_state: str) -> bool:
    try:
        return LifecycleState(to_state) in ALLOWED_TRANSITIONS[LifecycleState(from_state)]
    except ValueError:
        return False


async def plan(registry: Any, verb: str, args: Mapping[str, Any]) -> Plan:
    if verb == "create":
        return await _plan_create(registry, args)
    source_id = str(args["id"])
    expected = str(args["expected_version"])
    head = await _head(registry, source_id)
    if head.version != expected:
        raise Refused(
            "stale_read",
            f"source {source_id} is at {_short(head.version)} ({head.state}), not "
            f"{_short(expected)}; inspect it again.",
        )
    pre = {"version": head.version, "state": head.state}
    canon = {k: args[k] for k in ("class", "id", "expected_version", "reason") if k in args}
    if verb == "update":
        body = await _typed_body(registry, source_id)
        new_body, problem = P.apply_patch("source", body, args.get("patch") or {})
        if problem is not None:
            raise Refused(*problem)
        assert new_body is not None
        failed = P.preflight("source", new_body)
        if failed:
            raise Refused("preflight_failed", failed)
        typed = _model(new_body)

        async def mutate_update(*, actor: str, in_tx: Any) -> Any:
            return await registry.update(
                source_id, typed, actor, actor_role="operator_via_consult", in_tx=in_tx,
            )

        return Plan(
            cls="source", verb="update", object_ref=source_id,
            args={**canon, "patch": dict(args.get("patch") or {})},
            tier=W.source_tier("update", from_state=head.state, to_state=None),
            pre=pre, post_state=head.state,
            reversal={"kind": "rollback", "to_version": head.version},
            mutate=mutate_update, expected_version=head.version,
            card=f"Update source {source_id} ({', '.join(sorted(args.get('patch') or {}))})",
        )
    to_state = "retired" if verb == "retire" else str(args["to_state"])
    if to_state == head.state:
        raise Refused("no_change", f"source {source_id} is already {to_state}.")
    if not _legal(head.state, to_state):
        raise Refused(
            "preflight_failed",
            f"{head.state} → {to_state} is not a legal lifecycle move for a source.",
        )
    tier = W.source_tier(verb, from_state=head.state, to_state=to_state)
    if verb == "retire":
        async def mutate_retire(*, actor: str, in_tx: Any) -> Any:
            return await registry.retire(
                source_id, actor, family=FAMILY, actor_role="operator_via_consult",
                reason=str(args.get("reason") or "")[:300] or None, in_tx=in_tx,
            )

        return Plan(
            cls="source", verb="retire", object_ref=source_id,
            args={**canon, "to_state": "retired"}, tier=tier, pre=pre, post_state="retired",
            reversal={"kind": "none", "why": "retired is terminal"},
            mutate=mutate_retire, expected_version=head.version,
            card=f"Retire source {source_id} · cannot be undone",
        )
    body = await _typed_body(registry, source_id)
    body["identity"]["state"] = to_state
    failed = P.preflight("source", body)
    if failed:
        raise Refused("preflight_failed", failed)
    typed = _model(body)

    async def mutate_transition(*, actor: str, in_tx: Any) -> Any:
        return await registry.update(
            source_id, typed, actor, actor_role="operator_via_consult", in_tx=in_tx,
        )

    inverse = (
        {"kind": "transition", "to_state": head.state}
        if _legal(to_state, head.state)
        else {"kind": "none", "why": f"{to_state} → {head.state} is not a legal move"}
    )
    verb_word = {"active": "Activate", "paused": "Pause"}.get(to_state, f"Move to {to_state}:")
    return Plan(
        cls="source", verb="transition", object_ref=source_id,
        args={**canon, "to_state": to_state}, tier=tier, pre=pre, post_state=to_state,
        reversal=inverse, mutate=mutate_transition, expected_version=head.version,
        card=f"{verb_word} source {source_id}",
    )


async def _plan_create(registry: Any, args: Mapping[str, Any]) -> Plan:
    body = P.normalize_create(args.get("body") or {}, now_iso=datetime.now(timezone.utc).isoformat())
    source_id = str(body["identity"].get("id") or "")
    if not source_id:
        raise Refused("preflight_failed", "body.identity.id is required.")
    try:
        await registry.get(source_id, family=FAMILY)
    except DescriptorNotFound:
        pass
    else:
        raise Refused("preflight_failed", f"source {source_id!r} already exists; update it instead.")
    failed = P.preflight("source", body)
    if failed:
        raise Refused("preflight_failed", failed)
    typed = _model(body)

    async def mutate_create(*, actor: str, in_tx: Any) -> Any:
        return await registry.register(typed, actor, actor_role="operator_via_consult", in_tx=in_tx)

    return Plan(
        cls="source", verb="create", object_ref=source_id,
        args={"class": "source", "body": body, "reason": args.get("reason")},
        tier="A", pre=None, post_state="draft",
        reversal={"kind": "retire_draft"}, mutate=mutate_create, expected_version=None,
        card=f"Create draft source {source_id}",
    )


# ---------------------------------------------------------------------------
# revert — the stored handle, never a fresh model guess
# ---------------------------------------------------------------------------


async def plan_revert(registry: Any, original: Mapping[str, Any], newer: str | None) -> Plan:
    """The inverse of ``original`` (an executed write row) as a Plan."""
    source_id = str(original["object_ref"])
    reversal = dict(original.get("reversal") or {})
    post = dict(original.get("post_image") or {})
    pre = dict(original.get("pre_image") or {})
    kind = reversal.get("kind")
    if kind == "none" or kind is None:
        raise Refused(
            "preflight_failed",
            f"write {original['id']} cannot be undone: {reversal.get('why') or 'no reversal'}.",
        )
    head = await _head(registry, source_id)
    if head.version != post.get("version") or head.state != post.get("state"):
        raise Refused(
            "superseded_since",
            f"source {source_id} moved since write {original['id']} "
            f"(now {_short(head.version)}, {head.state})"
            + (f"; the newer change is write {newer}" if newer else "")
            + ". Nothing was reverted.",
        )
    current = {"version": head.version, "state": head.state}
    canon = {"write_id": str(original["id"]), "class": "source", "id": source_id}
    no_undo = {"kind": "none", "why": "a revert is undone by asking for the change again"}
    if kind == "rollback":
        target = str(reversal["to_version"])

        async def mutate_rollback(*, actor: str, in_tx: Any) -> Any:
            return await registry.rollback(
                source_id, target, actor, family=FAMILY, actor_role="operator_via_consult",
                reason=f"revert of consult write {original['id']}", in_tx=in_tx,
            )

        return Plan(
            cls="source", verb="revert", object_ref=source_id, args=canon, tier="A",
            pre=current, post_state=str(pre.get("state") or head.state), reversal=no_undo,
            mutate=mutate_rollback, expected_version=head.version,
            card=f"Revert source {source_id} to {_short(target)}",
            extra={"to_version": target},
        )
    if kind == "transition":
        to_state = str(reversal["to_state"])
        body = await _typed_body(registry, source_id)
        body["identity"]["state"] = to_state
        failed = P.preflight("source", body)
        if failed:
            raise Refused("preflight_failed", failed)
        typed = _model(body)

        async def mutate_back(*, actor: str, in_tx: Any) -> Any:
            return await registry.update(
                source_id, typed, actor, actor_role="operator_via_consult", in_tx=in_tx,
            )

        return Plan(
            cls="source", verb="revert", object_ref=source_id, args=canon,
            tier=W.source_tier("transition", from_state=head.state, to_state=to_state),
            pre=current, post_state=to_state, reversal=no_undo, mutate=mutate_back,
            expected_version=head.version, card=f"Revert source {source_id} to {to_state}",
        )
    if kind == "retire_draft":
        if head.state != "draft":
            raise Refused(
                "superseded_since",
                f"source {source_id} is {head.state}, no longer the draft this write created; "
                "pause or retire it directly.",
            )

        async def mutate_retire_draft(*, actor: str, in_tx: Any) -> Any:
            return await registry.retire(
                source_id, actor, family=FAMILY, actor_role="operator_via_consult",
                reason=f"revert of consult write {original['id']}", in_tx=in_tx,
            )

        return Plan(
            cls="source", verb="revert", object_ref=source_id, args=canon, tier="A",
            pre=current, post_state="retired", reversal=no_undo, mutate=mutate_retire_draft,
            expected_version=head.version, card=f"Revert: retire the draft source {source_id}",
        )
    raise Refused("preflight_failed", f"unknown reversal kind {kind!r}.")


# ---------------------------------------------------------------------------
# activation (D10) — one probe
# ---------------------------------------------------------------------------


ParseCheck = Callable[[Mapping[str, Any]], "str | None"]


async def activation(
    registry: Any, source_id: str, version: str, since: datetime,
    *, parse_check: ParseCheck,
) -> dict[str, Any] | None:
    """One look at the new head: a verdict dict, or None (keep watching)."""
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    head = await registry.get(source_id, family=FAMILY)
    if head.version != version:
        return {"status": "superseded", "head": head.version, "at": now}
    body = await _typed_body(registry, source_id)
    failed = parse_check(body)
    if failed:
        return {"status": "parse_failed", "error": failed[:600], "at": now}
    if head.state != "active":
        return {
            "status": "not_applicable",
            "why": f"the source is {head.state}; no actor polls it",
            "parsed": True, "at": now,
        }
    async with registry.pg.acquire() as conn:
        seen = await conn.fetchval(
            "SELECT min(occurred_at) FROM source_poll_outcomes "
            "WHERE source_id = $1 AND source_version = $2 AND occurred_at >= $3",
            source_id, version, since,
        )
    if seen is None:
        return None
    return {"status": "verified", "first_poll_at": seen.isoformat(), "at": now}
