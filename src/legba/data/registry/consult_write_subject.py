# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult write executor's ``subject`` BRANCH (C-2b-1/subject, W6; design C-2a §1 row 2, §4, §12).

A *subject* is what the platform watches — a place, a theme, an entity, an
estate. In code it is a target descriptor (``Family.TARGET``,
``target_descriptors``; C-2a §12): the tools say ``subject``, this branch maps
it. Same shape as the source branch (``consult_write_source.py``, the
template): everything goes through the registry's own write paths in-process —
``DescriptorRegistry.register`` / ``update`` / ``retire`` / ``rollback`` — with
the consult write's transaction hook (``in_tx``) so the mutation, the ledger
row and the signed audit row commit as one.

* :func:`inspect` — the head's writable fields (name, scope geo / tags /
  entity classes), the read-only scope context, version, state, the last
  versions, the protected and writable paths, and this session's executed
  writes on the object (the only place a ``write_id`` for ``revert_write``
  comes from).
* :func:`plan` — create (lands ``draft``) / update / transition / retire as a
  :class:`Plan`. Entity classes are checked against the live vocabulary at
  plan time (the registry would otherwise refuse at execution AND write a
  dead-letter row). Tier B: pause of an ACTIVE subject, and retire (cannot be
  undone).
* :func:`plan_revert` — the stored reversal: rollback to ``pre.version``, the
  inverse transition, or retire a never-activated draft; ``superseded_since``
  when the post-image is no longer the head.
* :func:`activation` — one probe (D10). The runtime parses a target head with
  ``TargetDescriptor.model_validate(typed, strict=False)``
  (``runtime/dapr_host._target_deps_resolver``); that is the ``parse_failed``
  leg. There is NO durable signal that the runtime has READ a given subject
  head: a TargetActor is a passive subscriber that stamps its descriptor
  version only into its own Dapr state on FIRST activation, and the units that
  read a subject's scope stamp ``analyst_outputs.target_version`` as NULL
  (``runtime/actor_substrate_slice.py``). So a parsed head is
  ``not_applicable`` with that reason — never a fabricated ``verified``.

**Cadence is not writable here, on purpose.** C-2a §1 lists cadence for the
subject row, but a target carries no cadence the runtime reads: the only
cadence-shaped field, ``analyst.cadence.fallback_schedule``, is a measured
vestige (``deterministic_handlers/desk_reference.py`` — design F-5) and the
registry warns that the inline ``analyst`` block is inert. A subject is read on
the cadence of the units that subscribe to it — change the unit. Inspect says
so instead of offering a write that changes nothing.

**Sidecar trailers ride along.** A target body may carry underscore-prefixed
operational trailers (the discovery autowire's ``_auto_wired_sources`` —
provenance plus the autowire's idempotency key) that the typed model strips.
A new head minted from the typed body would drop them; this branch copies them
from the prior head onto the new one inside the same transaction.

A leaf of the registry package: it imports the descriptor registry, the
vocabulary and the patch leaves, and the source branch's ``Plan`` /
``Refused`` (one refusal type for the executor) — no runtime or analyst module.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .. import consult_write_patch as P
from .. import consult_write_vocabulary as W
from ..schemas.lifecycle import ALLOWED_TRANSITIONS, LifecycleState
from .consult_write_source import Plan, Refused
from .descriptor import Family
from .errors import DescriptorNotFound

__all__ = ["FAMILY", "VERBS", "activation", "inspect", "plan", "plan_revert"]

#: The verbs this branch executes — the drift guard holds them equal to the
#: vocabulary's built verbs for ``subject``.
VERBS: frozenset[str] = frozenset({"inspect", "create", "update", "transition", "retire", "revert"})

#: The neutral class → the code family (C-2a §12).
FAMILY = Family.TARGET
_CLS = "subject"

_CADENCE_NOTE = (
    "not writable: a subject carries no cadence the runtime reads (the inline "
    "analyst.cadence block is an inert vestige); the units that subscribe to it "
    "run on their own cadence — change the unit"
)
_NO_SIGNAL = (
    "parsed; no durable runtime signal records a subject head being read "
    "(subject-bound outputs carry no target version)"
)


def _short(version: str | None) -> str:
    return (version or "")[:8]


async def _head(registry: Any, subject_id: str) -> Any:
    try:
        return await registry.get(subject_id, family=FAMILY)
    except DescriptorNotFound as exc:
        raise Refused("ref_not_read", f"subject {subject_id!r} does not exist.") from exc


async def _typed_body(registry: Any, subject_id: str) -> dict[str, Any]:
    typed = await registry.get_typed(subject_id, family=FAMILY)
    return typed.model_dump(mode="json", by_alias=True)


def _model(body: Mapping[str, Any]) -> Any:
    from ..schemas.target import TargetDescriptor

    return TargetDescriptor.model_validate(dict(body), strict=False)


def _legal(from_state: str, to_state: str) -> bool:
    try:
        return LifecycleState(to_state) in ALLOWED_TRANSITIONS[LifecycleState(from_state)]
    except ValueError:
        return False


def _vocab_problem(registry: Any, body: Mapping[str, Any]) -> str | None:
    """Entity classes / relationship types the live vocabulary does not know."""
    vocab = getattr(registry, "vocabulary", None)
    if vocab is None:
        return None
    scope = body.get("scope") or {}
    for family, key in (("entity_class", "entity_classes"), ("relationship_type", "relationship_types")):
        try:
            vocab.make_validator(family)(list(scope.get(key) or []))
        except Exception as exc:  # noqa: BLE001 — UnknownVocabularyValue; the message is the refusal
            return f"scope.{key}: {exc}"[:600]
    return None


def _check(registry: Any, body: Mapping[str, Any]) -> Any:
    """Runtime parse + live vocabulary, then the typed model — or Refused."""
    failed = P.preflight(_CLS, body) or _vocab_problem(registry, body)
    if failed:
        raise Refused("preflight_failed", failed)
    return _model(body)


class _CarryTrailers:
    """Wrap the executor's ``in_tx`` hook: after it ran, copy the prior head's
    underscore-prefixed sidecar trailers onto the new head (same transaction)."""

    def __init__(self, inner: Any, subject_id: str) -> None:
        self.inner, self.subject_id = inner, subject_id

    def receipt_for(self, **kw: Any) -> dict[str, Any]:
        return self.inner.receipt_for(**kw)

    async def __call__(self, conn: Any, *, audit_id: Any, from_version: str | None, to_version: str | None) -> None:
        await self.inner(conn, audit_id=audit_id, from_version=from_version, to_version=to_version)
        if not from_version or not to_version or from_version == to_version:
            return
        rows = {
            r["version"]: r["body"] for r in await conn.fetch(
                f"SELECT version, body FROM {FAMILY.table} WHERE descriptor_id = $1 AND version = ANY($2::text[])",
                self.subject_id, [from_version, to_version],
            )
        }
        prior, new = (_as_dict(rows.get(from_version)), _as_dict(rows.get(to_version)))
        carry: dict[str, Any] = {}
        for key, value in prior.items():
            if not key.startswith("_"):
                continue
            mine = new.get(key)
            carry[key] = {**mine, **value} if isinstance(mine, dict) and isinstance(value, dict) else value
        if carry and any(new.get(k) != v for k, v in carry.items()):
            await conn.execute(
                f"UPDATE {FAMILY.table} SET body = body || $3::jsonb WHERE descriptor_id = $1 AND version = $2",
                self.subject_id, to_version, json.dumps(carry, default=str),
            )


def _as_dict(body: Any) -> dict[str, Any]:
    if isinstance(body, (str, bytes, bytearray)):
        body = json.loads(body)
    return dict(body or {})


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


async def inspect(registry: Any, subject_id: str, *, session_id: str | None) -> dict[str, Any]:
    try:
        head = await registry.get(subject_id, family=FAMILY)
    except DescriptorNotFound:
        return {"found": False, "class": _CLS, "id": subject_id}
    body = await _typed_body(registry, subject_id)
    scope = body.get("scope") or {}
    async with registry.pg.acquire() as conn:
        versions = await conn.fetch(
            f"SELECT version, state, is_head, created_at FROM {FAMILY.table} "
            "WHERE descriptor_id = $1 ORDER BY created_at DESC LIMIT $2",
            subject_id, W.LIMITS["inspect_versions"],
        )
        writes = await conn.fetch(
            "SELECT id, verb, status, executed_at, reversed_by_write_id "
            "FROM consult_writes WHERE class = 'subject' AND object_ref = $1 "
            "AND session_id = $2::uuid AND status = 'executed' AND verb <> 'revert' "
            "ORDER BY created_at DESC LIMIT 10",
            subject_id, session_id,
        ) if session_id else []
    return {
        "found": True,
        "class": _CLS,
        "id": subject_id,
        "name": head.name,
        "version": head.version,
        "state": head.state,
        "fields": P.editable_view(_CLS, body),
        "read_only": {
            "scope.domain": scope.get("domain"),
            "scope.languages": scope.get("languages"),
            "scope.predicate": scope.get("predicate"),
            "sources": len(body.get("sources") or []),
        },
        "cadence": _CADENCE_NOTE,
        "writable": list(W.writable_paths(_CLS)),
        "protected": ["identity.id", "identity.version", "schema_uri"],
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
            "pause of an active subject and retire need the operator's confirmation; "
            "retire cannot be undone — prefer paused."
        ),
    }


# ---------------------------------------------------------------------------
# plan — a model call → an executable write
# ---------------------------------------------------------------------------


async def plan(registry: Any, verb: str, args: Mapping[str, Any]) -> Plan:
    if verb == "create":
        return await _plan_create(registry, args)
    subject_id = str(args["id"])
    expected = str(args["expected_version"])
    head = await _head(registry, subject_id)
    if head.version != expected:
        raise Refused(
            "stale_read",
            f"subject {subject_id} is at {_short(head.version)} ({head.state}), not "
            f"{_short(expected)}; inspect it again.",
        )
    pre = {"version": head.version, "state": head.state}
    canon = {k: args[k] for k in ("class", "id", "expected_version", "reason") if k in args}
    if verb == "update":
        patch = dict(args.get("patch") or {})
        body = await _typed_body(registry, subject_id)
        domain = (body.get("scope") or {}).get("domain")
        if "scope.geo" in patch and domain == "estate":
            raise Refused("path_not_writable", "scope.geo is not a field of an estate subject's scope.")
        new_body, problem = P.apply_patch(_CLS, body, patch)
        if problem is not None:
            raise Refused(*problem)
        assert new_body is not None
        typed = _check(registry, new_body)

        async def mutate_update(*, actor: str, in_tx: Any) -> Any:
            return await registry.update(
                subject_id, typed, actor, actor_role="operator_via_consult",
                in_tx=_CarryTrailers(in_tx, subject_id),
            )

        return Plan(
            cls=_CLS, verb="update", object_ref=subject_id,
            args={**canon, "patch": patch},
            tier=W.subject_tier("update", from_state=head.state, to_state=None),
            pre=pre, post_state=head.state,
            reversal={"kind": "rollback", "to_version": head.version},
            mutate=mutate_update, expected_version=head.version,
            card=f"Update subject {subject_id} ({', '.join(sorted(patch))})",
        )
    to_state = "retired" if verb == "retire" else str(args["to_state"])
    if to_state == head.state:
        raise Refused("no_change", f"subject {subject_id} is already {to_state}.")
    if not _legal(head.state, to_state):
        raise Refused(
            "preflight_failed",
            f"{head.state} → {to_state} is not a legal lifecycle move for a subject.",
        )
    tier = W.subject_tier(verb, from_state=head.state, to_state=to_state)
    if verb == "retire":
        async def mutate_retire(*, actor: str, in_tx: Any) -> Any:
            return await registry.retire(
                subject_id, actor, family=FAMILY, actor_role="operator_via_consult",
                reason=str(args.get("reason") or "")[:300] or None, in_tx=in_tx,
            )

        return Plan(
            cls=_CLS, verb="retire", object_ref=subject_id,
            args={**canon, "to_state": "retired"}, tier=tier, pre=pre, post_state="retired",
            reversal={"kind": "none", "why": "retired is terminal"},
            mutate=mutate_retire, expected_version=head.version,
            card=f"Retire subject {subject_id} · cannot be undone",
        )
    body = await _typed_body(registry, subject_id)
    body["identity"]["state"] = to_state
    typed = _check(registry, body)

    async def mutate_transition(*, actor: str, in_tx: Any) -> Any:
        return await registry.update(
            subject_id, typed, actor, actor_role="operator_via_consult",
            in_tx=_CarryTrailers(in_tx, subject_id),
        )

    inverse = (
        {"kind": "transition", "to_state": head.state}
        if _legal(to_state, head.state)
        else {"kind": "none", "why": f"{to_state} → {head.state} is not a legal move"}
    )
    verb_word = {"active": "Activate", "paused": "Pause"}.get(to_state, f"Move to {to_state}:")
    return Plan(
        cls=_CLS, verb="transition", object_ref=subject_id,
        args={**canon, "to_state": to_state}, tier=tier, pre=pre, post_state=to_state,
        reversal=inverse, mutate=mutate_transition, expected_version=head.version,
        card=f"{verb_word} subject {subject_id}",
    )


async def _plan_create(registry: Any, args: Mapping[str, Any]) -> Plan:
    body = P.normalize_create(args.get("body") or {}, now_iso=datetime.now(timezone.utc).isoformat())
    subject_id = str(body["identity"].get("id") or "")
    if not subject_id:
        raise Refused("preflight_failed", "body.identity.id is required.")
    try:
        await registry.get(subject_id, family=FAMILY)
    except DescriptorNotFound:
        pass
    else:
        raise Refused("preflight_failed", f"subject {subject_id!r} already exists; update it instead.")
    typed = _check(registry, body)

    async def mutate_create(*, actor: str, in_tx: Any) -> Any:
        return await registry.register(typed, actor, actor_role="operator_via_consult", in_tx=in_tx)

    return Plan(
        cls=_CLS, verb="create", object_ref=subject_id,
        args={"class": _CLS, "body": body, "reason": args.get("reason")},
        tier="A", pre=None, post_state="draft",
        reversal={"kind": "retire_draft"}, mutate=mutate_create, expected_version=None,
        card=f"Create draft subject {subject_id}",
    )


# ---------------------------------------------------------------------------
# revert — the stored handle, never a fresh model guess
# ---------------------------------------------------------------------------


async def plan_revert(registry: Any, original: Mapping[str, Any], newer: str | None) -> Plan:
    """The inverse of ``original`` (an executed write row) as a Plan."""
    subject_id = str(original["object_ref"])
    reversal = dict(original.get("reversal") or {})
    post = dict(original.get("post_image") or {})
    pre = dict(original.get("pre_image") or {})
    kind = reversal.get("kind")
    if kind == "none" or kind is None:
        raise Refused(
            "preflight_failed",
            f"write {original['id']} cannot be undone: {reversal.get('why') or 'no reversal'}.",
        )
    head = await _head(registry, subject_id)
    if head.version != post.get("version") or head.state != post.get("state"):
        raise Refused(
            "superseded_since",
            f"subject {subject_id} moved since write {original['id']} "
            f"(now {_short(head.version)}, {head.state})"
            + (f"; the newer change is write {newer}" if newer else "")
            + ". Nothing was reverted.",
        )
    current = {"version": head.version, "state": head.state}
    canon = {"write_id": str(original["id"]), "class": _CLS, "id": subject_id}
    no_undo = {"kind": "none", "why": "a revert is undone by asking for the change again"}
    if kind == "rollback":
        target = str(reversal["to_version"])

        async def mutate_rollback(*, actor: str, in_tx: Any) -> Any:
            return await registry.rollback(
                subject_id, target, actor, family=FAMILY, actor_role="operator_via_consult",
                reason=f"revert of consult write {original['id']}",
                in_tx=_CarryTrailers(in_tx, subject_id),
            )

        return Plan(
            cls=_CLS, verb="revert", object_ref=subject_id, args=canon, tier="A",
            pre=current, post_state=str(pre.get("state") or head.state), reversal=no_undo,
            mutate=mutate_rollback, expected_version=head.version,
            card=f"Revert subject {subject_id} to {_short(target)}",
            extra={"to_version": target},
        )
    if kind == "transition":
        to_state = str(reversal["to_state"])
        body = await _typed_body(registry, subject_id)
        body["identity"]["state"] = to_state
        typed = _check(registry, body)

        async def mutate_back(*, actor: str, in_tx: Any) -> Any:
            return await registry.update(
                subject_id, typed, actor, actor_role="operator_via_consult",
                in_tx=_CarryTrailers(in_tx, subject_id),
            )

        return Plan(
            cls=_CLS, verb="revert", object_ref=subject_id, args=canon,
            tier=W.subject_tier("transition", from_state=head.state, to_state=to_state),
            pre=current, post_state=to_state, reversal=no_undo, mutate=mutate_back,
            expected_version=head.version, card=f"Revert subject {subject_id} to {to_state}",
        )
    if kind == "retire_draft":
        if head.state != "draft":
            raise Refused(
                "superseded_since",
                f"subject {subject_id} is {head.state}, no longer the draft this write created; "
                "pause or retire it directly.",
            )

        async def mutate_retire_draft(*, actor: str, in_tx: Any) -> Any:
            return await registry.retire(
                subject_id, actor, family=FAMILY, actor_role="operator_via_consult",
                reason=f"revert of consult write {original['id']}", in_tx=in_tx,
            )

        return Plan(
            cls=_CLS, verb="revert", object_ref=subject_id, args=canon, tier="A",
            pre=current, post_state="retired", reversal=no_undo, mutate=mutate_retire_draft,
            expected_version=head.version, card=f"Revert: retire the draft subject {subject_id}",
        )
    raise Refused("preflight_failed", f"unknown reversal kind {kind!r}.")


# ---------------------------------------------------------------------------
# activation (D10) — one probe
# ---------------------------------------------------------------------------


ParseCheck = Callable[[Mapping[str, Any]], "str | None"]


async def activation(
    registry: Any, subject_id: str, version: str, since: datetime,
    *, parse_check: ParseCheck,
) -> dict[str, Any] | None:
    """One look at the new head: always a verdict (there is nothing durable to wait for)."""
    del since
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    head = await registry.get(subject_id, family=FAMILY)
    if head.version != version:
        return {"status": "superseded", "head": head.version, "at": now}
    body = await _typed_body(registry, subject_id)
    failed = parse_check(body)
    if failed:
        return {"status": "parse_failed", "error": failed[:600], "at": now}
    return {"status": "not_applicable", "why": _NO_SIGNAL, "parsed": True, "state": head.state, "at": now}
