# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult write executor's ``entity`` BRANCH — the first RECORD class (W11; design C-2a §1 row 5, §4).

An entity is a row of ``entity_profiles`` (its history in
``entity_profile_versions``). It has no registry head: a write is a
provenance-carrying record edit through the apply layer
(``consult_write_entity_apply.py``) in ONE transaction with the ledger row and
a signed audit row (namespace ``entity``; actor ``consult_write:<id>``, role
``operator_via_consult``). Verbs:

* :func:`inspect` — ``inspect_object(class='entity', id=<uuid or exact name>)``:
  the profile's writable view (name, class, aliases, geometry), its version,
  state (``live`` / ``merged`` / ``merged_compacted``), the merge record and
  whether an unmerge is possible, the open facts naming it, the last versions
  and this session's writes on it. It seeds the read set (id + version).
* ``edit`` (``edit_entity``) — a patch of :data:`W.ENTITY_WRITABLE` under
  ``expected_version`` (compare-and-set; ``stale_read`` when it moved). The
  class binds through the live ``entity_class`` family or is refused by name.
  Tier B. Reversal: a new version restoring the pre-image.
* ``merge`` (``merge_entities``) — keeper + loser, both live and read. Records
  the pre-image (both rows, the loser's prior ``gc_status`` / ``merge``, the
  open facts naming the loser that a compaction would fold). Compaction stays
  the lifecycle's (``entity_gc``). Tier B. Reversal: an unmerge.
* ``unmerge`` (``unmerge_entity``) — only while the loser is uncompacted and
  the merge recorded its pre-image; else ``kind_not_built`` naming SEAMS #68.
  Records the pair distinct. Tier B. Reversal: the re-merge.
* :func:`plan_revert` — the stored handle; ``superseded_since`` when the
  post-image is no longer current (any later change to the rows it wrote).

Refused, by name: an id that does not exist (``ref_not_read``), a merged
entity edited (``preflight_failed``), a merge of a compacted loser
(``kind_not_built``, SEAMS #68), a class outside the family
(``preflight_failed`` naming the family), a name + class another live
profile carries (``preflight_failed``: merge them instead).

A leaf of the registry package: no runtime or analyst module.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID

from .. import consult_write_vocabulary as W
from . import consult_write_entity_apply as E
from .consult_write_source import Plan, Refused
from .errors import VersionConflict

__all__ = ["FAMILY", "VERBS", "activation", "inspect", "journal_merge", "plan", "plan_revert"]

#: The verbs this branch executes — the drift guard holds them equal to the
#: vocabulary's built verbs for ``entity``.
VERBS: frozenset[str] = frozenset({"inspect", "edit", "merge", "unmerge", "revert"})

#: A record class has no registry family (the executor's parse-failure
#: rollback never applies: there is no head to parse).
FAMILY = None
_CLS = "entity"
_ROLE = "operator_via_consult"
_SEAM = E.SEAM_UNMERGE


def _is_uuid(value: Any) -> bool:
    try:
        UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _label(snap: Mapping[str, Any]) -> str:
    return f"{snap['canonical_name']} ({snap['entity_class']})"


def _class_values(registry: Any) -> frozenset[str]:
    vocab = getattr(registry, "vocabulary", None)
    if vocab is None:
        return frozenset()
    try:
        return frozenset(vocab.values("entity_class"))
    except Exception:  # noqa: BLE001 — an unreadable family binds nothing (fail closed below)
        return frozenset()


def _binder(registry: Any) -> Any:
    values = _class_values(registry)
    vocab = getattr(registry, "vocabulary", None)

    def bind(raw: str) -> str:
        if not values:
            raise E.PatchProblem(
                "preflight_failed",
                "entity_class cannot be bound: the entity_class family is not loaded on the registry.",
            )
        value = raw
        try:
            value = vocab.resolve_alias("entity_class", raw) if vocab is not None else raw
        except Exception:  # noqa: BLE001 — no alias: the raw value is checked as is
            value = raw
        if value not in values:
            raise E.PatchProblem(
                "preflight_failed",
                f"entity_class {raw!r} is not a value of the entity_class family "
                f"({', '.join(sorted(values))}).",
            )
        return value

    return bind


async def _get(registry: Any, entity_id: str, what: str) -> dict[str, Any]:
    async with registry.pg.acquire() as conn:
        snap = await E.read(conn, entity_id)
    if snap is None:
        raise Refused("ref_not_read", f"{what} {entity_id!r} is not an entity record.")
    return snap


def _compacted_refusal(snap: Mapping[str, Any], *, doing: str) -> Refused:
    return Refused(
        "kind_not_built",
        f"{doing} of {_label(snap)} is not built (SEAMS #{_SEAM}): it was merged into "
        f"{snap['merged_into']} and compacted at {snap['compacted_at']} — compaction rewrote "
        "facts.subject in place, so the merge can no longer be undone exactly.",
    )


def _unmergeable(snap: Mapping[str, Any]) -> str:
    """Why an unmerge of this loser is (not) possible — inspect says it."""
    if snap["merged_into"] is None:
        return "not merged"
    if snap["compacted_at"]:
        return f"not built (SEAMS #{_SEAM}): compacted at {snap['compacted_at']}"
    if not isinstance((snap["merge"] or {}).get("added_aliases"), list):
        decided = (snap["merge"] or {}).get("decided_by") or "the lifecycle"
        return (f"not built (SEAMS #{_SEAM}): this merge (decided_by {decided}) recorded no "
                "pre-image and folded its edges when it ran")
    return "possible until entity_gc compacts it"


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


async def _resolve(conn: Any, ref: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """A uuid, or an exact canonical name (one live profile, else one merged)."""
    if _is_uuid(ref):
        return await E.read(conn, ref), []
    rows = await conn.fetch(
        "SELECT id, entity_class, merged_into FROM entity_profiles "
        "WHERE lower(canonical_name) = lower($1) ORDER BY entity_class LIMIT 20", ref,
    )
    live = [r for r in rows if r["merged_into"] is None]
    pick = live if live else rows
    if len(pick) == 1:
        return await E.read(conn, str(pick[0]["id"])), []
    return None, [{"id": str(r["id"]), "entity_class": r["entity_class"],
                   "merged": r["merged_into"] is not None} for r in rows]


async def inspect(registry: Any, ref: str, *, session_id: str | None) -> dict[str, Any]:
    async with registry.pg.acquire() as conn:
        snap, candidates = await _resolve(conn, str(ref))
        if snap is None:
            out: dict[str, Any] = {"found": False, "class": _CLS, "id": str(ref)}
            if candidates:
                out["ambiguous"] = candidates
                out["hint"] = "several profiles carry that name; inspect one by its id."
            return out
        versions = await conn.fetch(
            "SELECT version, data->>'event' AS event, analyst_id, created_at FROM entity_profile_versions "
            "WHERE entity_id = $1::uuid ORDER BY version DESC, created_at DESC LIMIT $2",
            snap["id"], W.LIMITS["inspect_versions"],
        )
        facts = await E.open_facts(conn, snap["canonical_name"], limit=0)
        writes = await conn.fetch(
            "SELECT id, verb, status, executed_at, reversed_by_write_id FROM consult_writes "
            "WHERE class = 'entity' AND (object_ref = $1 OR args->>'keeper_id' = $1) "
            "AND session_id = $2::uuid AND status = 'executed' AND verb <> 'revert' "
            "ORDER BY created_at DESC LIMIT 10",
            snap["id"], session_id,
        ) if session_id else []
    merge = snap["merge"] or {}
    return {
        "found": True,
        "class": _CLS,
        "id": snap["id"],
        "name": snap["canonical_name"],
        "version": snap["version"],
        "state": E.state_of(snap),
        "fields": E.fields_of(snap),
        "entity_type": snap["entity_type"],
        "merged_into": snap["merged_into"],
        "merge": {
            "into": merge.get("into"), "decided_by": merge.get("decided_by"), "at": merge.get("at"),
            "compacted_at": snap["compacted_at"], "unmerge": _unmergeable(snap),
        } if snap["merged_into"] else None,
        "open_facts": {"naming_this_name": facts["open_naming"], "as_of": facts["as_of"]},
        "writable": list(W.ENTITY_WRITABLE),
        "entity_classes": sorted(_class_values(registry)),
        "protected": ["id", "version", "entity_type (follows entity_class)", "derived_from", "schema_uri"],
        "versions": [
            {"version": str(r["version"]), "event": r["event"], "writer": r["analyst_id"],
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
            "edit_entity, merge_entities and unmerge_entity wait for the operator's Confirm. "
            "A merge can be unmerged until entity_gc compacts the loser."
        ),
    }


# ---------------------------------------------------------------------------
# plan — a model call (or a stored row) → an executable write
# ---------------------------------------------------------------------------


def _stale_since_staged(args: Mapping[str, Any], key: str, now_sha: str, what: str) -> None:
    stored = args.get(key)
    if stored is not None and stored != now_sha:
        raise Refused("stale_read", f"{what} changed since the change was staged; inspect it again.")


async def plan(registry: Any, verb: str, args: Mapping[str, Any]) -> Plan:
    if verb == "edit":
        return await _plan_edit(registry, args)
    if verb == "merge":
        return await _plan_merge(registry, args)
    if verb == "unmerge":
        return await _plan_unmerge(registry, args)
    raise Refused("class_not_writable", f"entity has no {verb} verb.")


async def _plan_edit(registry: Any, args: Mapping[str, Any]) -> Plan:
    entity_id = str(args["entity_id"])
    snap = await _get(registry, entity_id, "entity")
    expected = str(args["expected_version"])
    if snap["version"] != expected:
        raise Refused("stale_read", f"entity {_label(snap)} is at version {snap['version']}, not {expected}; "
                                    "inspect it again.")
    if snap["merged_into"] is not None:
        raise Refused("preflight_failed", f"entity {_label(snap)} is merged into {snap['merged_into']}; "
                                          "edit the survivor.")
    pre_fields = E.fields_of(snap)
    pre_sha = E.sha(pre_fields)
    _stale_since_staged(args, "pre_sha256", pre_sha, f"entity {_label(snap)}")
    patch = dict(args.get("patch") or {})
    try:
        new_fields = E.apply_fields_patch(snap, patch, writable=W.ENTITY_WRITABLE, bind_class=_binder(registry))
    except E.PatchProblem as exc:
        raise Refused(exc.cause, exc.detail) from exc
    if new_fields == pre_fields:
        raise Refused("no_change", f"the patch re-states entity {_label(snap)}; nothing to write.")
    async with registry.pg.acquire() as conn:
        clash = await conn.fetchval(
            "SELECT id FROM entity_profiles WHERE lower(canonical_name) = lower($1) "
            "AND entity_class = $2 AND id <> $3::uuid LIMIT 1",
            new_fields["canonical_name"], new_fields["entity_class"], entity_id,
        )
    if clash is not None:
        raise Refused("preflight_failed", f"entity {clash} already carries the name "
                                          f"{new_fields['canonical_name']!r} as a {new_fields['entity_class']}; "
                                          "merge the two instead.")
    class_values = _class_values(registry)
    post_version = str(int(snap["version"]) + 1)
    canon = {"entity_id": entity_id, "expected_version": expected, "patch": patch,
             "reason": args.get("reason"), "pre_sha256": pre_sha}

    async def mutate(*, actor: str, in_tx: Any) -> None:
        wid = actor.split(":", 1)[1]
        async with registry.pg.acquire() as conn, conn.transaction():
            locked = (await E.lock(conn, [entity_id])).get(entity_id)
            E.verify_unchanged(locked, version=expected, fields_sha=pre_sha, what=f"entity {entity_id}")
            prov = E.provenance(actor=actor, actor_role=_ROLE, reason=args.get("reason"), write_id=wid)
            new_version = await E.do_edit(conn, locked, new_fields, class_values=class_values, prov=prov)
            await E.record_audit(conn, registry, in_tx=in_tx, actor=actor, actor_role=_ROLE,
                                 object_id=entity_id, action="edit", from_version=expected,
                                 to_version=new_version)

    return Plan(
        cls=_CLS, verb="edit", object_ref=entity_id, args=canon, tier=W.entity_tier("edit"),
        pre={"version": snap["version"], "state": E.state_of(snap), "fields": pre_fields, "sha256": pre_sha},
        post_state=E.state_of(snap),
        reversal={"kind": "restore_fields", "fields": pre_fields, "post_version": post_version,
                  "post_sha256": E.sha(new_fields)},
        mutate=mutate, expected_version=expected,
        card=f"Edit entity {_label(snap)} ({', '.join(sorted(patch))})",
        extra={"post_fields": new_fields},
    )


def _pair_sha(keeper: Mapping[str, Any], loser: Mapping[str, Any]) -> str:
    return E.sha({"keeper": E.fields_of(keeper), "loser": E.fields_of(loser),
                  "loser_gc": loser["gc_status"], "loser_merge": loser["merge"]})


async def _plan_merge(registry: Any, args: Mapping[str, Any]) -> Plan:
    keeper_id, loser_id = str(args["keeper_id"]), str(args["loser_id"])
    if keeper_id == loser_id:
        raise Refused("preflight_failed", "keeper_id and loser_id are the same entity.")
    loser = await _get(registry, loser_id, "loser")
    keeper = await _get(registry, keeper_id, "keeper")
    if loser["compacted_at"]:
        raise _compacted_refusal(loser, doing="A merge")
    if loser["merged_into"] is not None:
        raise Refused("preflight_failed", f"loser {_label(loser)} is already merged into {loser['merged_into']}; "
                                          "unmerge it first.")
    if keeper["merged_into"] is not None:
        raise Refused("preflight_failed", f"keeper {_label(keeper)} is merged into {keeper['merged_into']}; "
                                          "merge into that survivor instead.")
    pre_sha = _pair_sha(keeper, loser)
    _stale_since_staged(args, "pre_sha256", pre_sha, "the pair")
    for key, snap in (("keeper_version", keeper), ("loser_version", loser)):
        if args.get(key) is not None and str(args[key]) != snap["version"]:
            raise Refused("stale_read", f"{_label(snap)} moved to version {snap['version']}; inspect it again.")
    async with registry.pg.acquire() as conn:
        facts = await E.open_facts(conn, loser["canonical_name"])
    added = E.merge_added_aliases(keeper, loser)
    keeper_after = [*keeper["aliases"], *added]
    canon = {"keeper_id": keeper_id, "loser_id": loser_id, "reason": args.get("reason"),
             "keeper_version": keeper["version"], "loser_version": loser["version"], "pre_sha256": pre_sha}

    async def mutate(*, actor: str, in_tx: Any) -> None:
        wid = actor.split(":", 1)[1]
        async with registry.pg.acquire() as conn, conn.transaction():
            rows = await E.lock(conn, [keeper_id, loser_id])
            k, lo = rows.get(keeper_id), rows.get(loser_id)
            E.verify_unchanged(k, version=keeper["version"], fields_sha=E.sha(E.fields_of(keeper)),
                               what=f"keeper {keeper_id}")
            E.verify_unchanged(lo, version=loser["version"], fields_sha=E.sha(E.fields_of(loser)),
                               what=f"loser {loser_id}")
            if _pair_sha(k, lo) != pre_sha:
                raise VersionConflict("the pair changed since it was read")
            prov = E.provenance(actor=actor, actor_role=_ROLE, reason=args.get("reason"), write_id=wid)
            done = await E.do_merge(conn, k, lo, prov=prov, reason=args.get("reason"))
            await E.record_audit(conn, registry, in_tx=in_tx, actor=actor, actor_role=_ROLE,
                                 object_id=loser_id, action="merge", from_version=loser["version"],
                                 to_version=done["loser_version"],
                                 summary={"keeper_id": keeper_id, "keeper_version": done["keeper_version"]})

    return Plan(
        cls=_CLS, verb="merge", object_ref=loser_id, args=canon, tier=W.entity_tier("merge"),
        pre={
            "version": loser["version"], "state": "live", "sha256": pre_sha,
            "loser": {**E.fields_of(loser), "version": loser["version"], "gc_status": loser["gc_status"]},
            "keeper": {**E.fields_of(keeper), "version": keeper["version"]},
            "facts": facts,
        },
        post_state="merged",
        reversal={
            "kind": "unmerge", "keeper_id": keeper_id, "added_aliases": added,
            "loser_pre": {"gc_status": loser["gc_status"], "merge": loser["merge"]},
            "post": {"loser_version": str(int(loser["version"]) + 1),
                     "keeper_version": str(int(keeper["version"]) + 1),
                     "keeper_aliases_sha256": E.sha(keeper_after)},
        },
        mutate=mutate, expected_version=loser["version"],
        card=(f"Merge entity {_label(loser)} into {_label(keeper)} · folds {len(added)} alias(es) · "
              f"{facts['open_naming']} open fact(s) name the loser (compaction re-points them) · "
              "unmergeable until compaction"),
    )


async def _unmerge_parts(registry: Any, loser_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    loser = await _get(registry, loser_id, "entity")
    if loser["merged_into"] is None:
        raise Refused("preflight_failed", f"entity {_label(loser)} is not merged; there is nothing to unmerge.")
    if loser["compacted_at"]:
        raise _compacted_refusal(loser, doing="An unmerge")
    why = _unmergeable(loser)
    if not why.startswith("possible"):
        raise Refused("kind_not_built", f"An unmerge of {_label(loser)} is {why}.")
    keeper = await _get(registry, loser["merged_into"], "keeper")
    return loser, keeper


async def _plan_unmerge(registry: Any, args: Mapping[str, Any]) -> Plan:
    loser_id = str(args["loser_id"])
    loser, keeper = await _unmerge_parts(registry, loser_id)
    keeper_id = keeper["id"]
    pre_sha = _pair_sha(keeper, loser)
    _stale_since_staged(args, "pre_sha256", pre_sha, "the pair")
    added = list((loser["merge"] or {}).get("added_aliases") or [])
    loser_pre = dict((loser["merge"] or {}).get("pre") or {})
    removed = E.unmerge_removed_aliases(keeper, added)
    keeper_after = [a for a in keeper["aliases"]]
    for alias in removed:
        keeper_after.remove(alias)
    async with registry.pg.acquire() as conn:
        judged = await E.judgement(conn, keeper_id, loser_id)
    canon = {"loser_id": loser_id, "reason": args.get("reason"), "keeper_id": keeper_id,
             "keeper_version": keeper["version"], "loser_version": loser["version"], "pre_sha256": pre_sha}
    mutate = _unmerge_mutate(registry, keeper, loser, pre_sha=pre_sha, added=added, loser_pre=loser_pre,
                             reason=args.get("reason"), action="unmerge")
    return Plan(
        cls=_CLS, verb="unmerge", object_ref=loser_id, args=canon, tier=W.entity_tier("unmerge"),
        pre={
            "version": loser["version"], "state": "merged", "sha256": pre_sha,
            "loser": {**E.fields_of(loser), "version": loser["version"], "gc_status": loser["gc_status"],
                      "merge": loser["merge"]},
            "keeper": {**E.fields_of(keeper), "version": keeper["version"]},
            "judgement": judged,
        },
        post_state="live",
        reversal={
            "kind": "remerge", "keeper_id": keeper_id, "removed_aliases": removed,
            "loser_merge": loser["merge"], "loser_gc_status": loser["gc_status"], "judgement_pre": judged,
            "post": {"loser_version": str(int(loser["version"]) + 1),
                     "keeper_version": str(int(keeper["version"]) + 1),
                     "keeper_aliases_sha256": E.sha(keeper_after)},
        },
        mutate=mutate, expected_version=loser["version"],
        card=(f"Unmerge entity {_label(loser)} from {_label(keeper)} · drops {len(removed)} folded alias(es) "
              "· records the two as distinct"),
    )


def _unmerge_mutate(
    registry: Any, keeper: Mapping[str, Any], loser: Mapping[str, Any], *, pre_sha: str | None,
    added: list[str], loser_pre: Mapping[str, Any], reason: Any, action: str,
    post: Mapping[str, Any] | None = None,
) -> Any:
    keeper_id, loser_id = keeper["id"], loser["id"]

    async def mutate(*, actor: str, in_tx: Any) -> None:
        wid = actor.split(":", 1)[1]
        async with registry.pg.acquire() as conn, conn.transaction():
            rows = await E.lock(conn, [keeper_id, loser_id])
            k, lo = rows.get(keeper_id), rows.get(loser_id)
            E.verify_unchanged(k, version=keeper["version"], fields_sha=E.sha(E.fields_of(keeper)),
                               what=f"keeper {keeper_id}")
            E.verify_unchanged(lo, version=loser["version"], fields_sha=E.sha(E.fields_of(loser)),
                               what=f"loser {loser_id}")
            if lo["merged_into"] != keeper_id or lo["compacted_at"]:
                raise VersionConflict(f"loser {loser_id} is no longer an uncompacted redirect to {keeper_id}")
            if pre_sha is not None and _pair_sha(k, lo) != pre_sha:
                raise VersionConflict("the pair changed since it was read")
            prov = E.provenance(actor=actor, actor_role=_ROLE, reason=reason, write_id=wid)
            done = await E.do_unmerge(conn, k, lo, prov=prov, added=added, loser_pre=loser_pre)
            await E.record_audit(conn, registry, in_tx=in_tx, actor=actor, actor_role=_ROLE,
                                 object_id=loser_id, action=action, from_version=loser["version"],
                                 to_version=done["loser_version"],
                                 summary={"keeper_id": keeper_id, "keeper_version": done["keeper_version"]})

    return mutate


# ---------------------------------------------------------------------------
# revert — the stored handle, never a fresh model guess
# ---------------------------------------------------------------------------


def _superseded(what: str, original: Mapping[str, Any], newer: str | None, now: str) -> Refused:
    return Refused(
        "superseded_since",
        f"{what} changed since write {original['id']} ({now})"
        + (f"; the newer change is write {newer}" if newer else "")
        + ". Nothing was reverted.",
    )


async def plan_revert(registry: Any, original: Mapping[str, Any], newer: str | None) -> Plan:
    reversal = dict(original.get("reversal") or {})
    kind = reversal.get("kind")
    object_id = str(original["object_ref"])
    canon = {"write_id": str(original["id"]), "class": _CLS, "id": object_id}
    no_undo = {"kind": "none", "why": "a revert is undone by asking for the change again"}
    if kind == "restore_fields":
        snap = await _get(registry, object_id, "entity")
        if snap["version"] != reversal.get("post_version") or E.sha(E.fields_of(snap)) != reversal.get("post_sha256"):
            raise _superseded(f"entity {_label(snap)}", original, newer, f"now version {snap['version']}")
        if snap["merged_into"] is not None:
            raise _superseded(f"entity {_label(snap)}", original, newer, f"merged into {snap['merged_into']}")
        restored = dict(reversal["fields"])
        class_values = _class_values(registry)
        version, fields_sha = snap["version"], E.sha(E.fields_of(snap))

        async def mutate_restore(*, actor: str, in_tx: Any) -> None:
            wid = actor.split(":", 1)[1]
            async with registry.pg.acquire() as conn, conn.transaction():
                locked = (await E.lock(conn, [object_id])).get(object_id)
                E.verify_unchanged(locked, version=version, fields_sha=fields_sha, what=f"entity {object_id}")
                prov = E.provenance(actor=actor, actor_role=_ROLE, reason=f"revert of consult write {original['id']}",
                                    write_id=wid)
                new_version = await E.do_edit(conn, locked, restored, class_values=class_values, prov=prov,
                                              event="operator_revert")
                await E.record_audit(conn, registry, in_tx=in_tx, actor=actor, actor_role=_ROLE,
                                     object_id=object_id, action="revert", from_version=version,
                                     to_version=new_version)

        return Plan(
            cls=_CLS, verb="revert", object_ref=object_id, args=canon, tier=W.entity_tier("revert", reverses="edit"),
            pre={"version": version, "state": E.state_of(snap), "fields": E.fields_of(snap), "sha256": fields_sha},
            post_state=E.state_of(snap), reversal=no_undo, mutate=mutate_restore, expected_version=version,
            card=f"Revert entity {_label(snap)} to its fields before write {original['id']}",
        )
    if kind in ("unmerge", "remerge"):
        return await _plan_revert_pair(registry, original, reversal, newer, canon, no_undo)
    raise Refused("preflight_failed",
                  f"write {original['id']} cannot be undone: {reversal.get('why') or 'no reversal'}.")


async def _plan_revert_pair(
    registry: Any, original: Mapping[str, Any], reversal: Mapping[str, Any], newer: str | None,
    canon: dict[str, Any], no_undo: dict[str, Any],
) -> Plan:
    loser_id, keeper_id = str(original["object_ref"]), str(reversal["keeper_id"])
    loser = await _get(registry, loser_id, "entity")
    keeper = await _get(registry, keeper_id, "keeper")
    post = dict(reversal.get("post") or {})
    if reversal["kind"] == "unmerge" and loser["compacted_at"]:
        raise _compacted_refusal(loser, doing="A revert (unmerge)")
    wanted_into = keeper_id if reversal["kind"] == "unmerge" else None
    if (loser["merged_into"] != wanted_into or loser["version"] != post.get("loser_version")
            or keeper["version"] != post.get("keeper_version")
            or E.sha(keeper["aliases"]) != post.get("keeper_aliases_sha256")):
        raise _superseded(f"the pair {_label(loser)} / {_label(keeper)}", original, newer,
                          f"loser v{loser['version']}, keeper v{keeper['version']}")
    current = {"version": loser["version"], "state": E.state_of(loser), "sha256": _pair_sha(keeper, loser),
               "loser": {**E.fields_of(loser), "version": loser["version"]},
               "keeper": {**E.fields_of(keeper), "version": keeper["version"]}}
    if reversal["kind"] == "unmerge":
        mutate = _unmerge_mutate(
            registry, keeper, loser, pre_sha=None, added=list(reversal.get("added_aliases") or []),
            loser_pre=dict(reversal.get("loser_pre") or {}), reason=f"revert of consult write {original['id']}",
            action="revert",
        )
        return Plan(
            cls=_CLS, verb="revert", object_ref=loser_id, args=canon,
            tier=W.entity_tier("revert", reverses="merge"), pre=current, post_state="live", reversal=no_undo,
            mutate=mutate, expected_version=loser["version"],
            card=f"Revert the merge: unmerge {_label(loser)} from {_label(keeper)}",
        )

    async def mutate_remerge(*, actor: str, in_tx: Any) -> None:
        wid = actor.split(":", 1)[1]
        async with registry.pg.acquire() as conn, conn.transaction():
            rows = await E.lock(conn, [keeper_id, loser_id])
            k, lo = rows.get(keeper_id), rows.get(loser_id)
            E.verify_unchanged(k, version=keeper["version"], fields_sha=E.sha(E.fields_of(keeper)),
                               what=f"keeper {keeper_id}")
            E.verify_unchanged(lo, version=loser["version"], fields_sha=E.sha(E.fields_of(loser)),
                               what=f"loser {loser_id}")
            if lo["merged_into"] is not None:
                raise VersionConflict(f"entity {loser_id} was merged again since")
            prov = E.provenance(actor=actor, actor_role=_ROLE, reason=f"revert of consult write {original['id']}",
                                write_id=wid)
            done = await E.do_remerge(
                conn, k, lo, prov=prov, removed=list(reversal.get("removed_aliases") or []),
                loser_merge=reversal.get("loser_merge"), loser_gc_status=reversal.get("loser_gc_status"),
                judgement_pre=reversal.get("judgement_pre"),
            )
            await E.record_audit(conn, registry, in_tx=in_tx, actor=actor, actor_role=_ROLE,
                                 object_id=loser_id, action="revert", from_version=loser["version"],
                                 to_version=done["loser_version"],
                                 summary={"keeper_id": keeper_id, "keeper_version": done["keeper_version"]})

    return Plan(
        cls=_CLS, verb="revert", object_ref=loser_id, args=canon,
        tier=W.entity_tier("revert", reverses="unmerge"), pre=current, post_state="merged", reversal=no_undo,
        mutate=mutate_remerge, expected_version=loser["version"],
        card=f"Revert the unmerge: merge {_label(loser)} back into {_label(keeper)}",
    )


# ---------------------------------------------------------------------------
# activation — a record write has none
# ---------------------------------------------------------------------------


async def activation(
    registry: Any, entity_id: str, version: str, since: datetime, *, parse_check: Any = None,
) -> dict[str, Any] | None:
    """A record write has no runtime head to activate: always ``not_applicable``."""
    del registry, entity_id, version, since, parse_check
    return {"status": "not_applicable", "why": "a record write has no runtime activation",
            "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}


# ---------------------------------------------------------------------------
# The journal accept of a ``merge_entities`` correction (journal_proposals_apply)
# ---------------------------------------------------------------------------


async def _journal_ref(conn: Any, ref: Any, what: str) -> dict[str, Any]:
    ref = str(ref or "").strip()
    if not ref:
        raise Refused("preflight_failed", f"merge_entities names no {what}.")
    snap, candidates = await _resolve(conn, ref)
    if snap is None:
        if candidates:
            raise Refused("preflight_failed", f"the {what} {ref!r} names {len(candidates)} profiles; "
                                              "a merge needs one id.")
        raise Refused("ref_not_read", f"the {what} {ref!r} is not an entity record.")
    return snap


async def journal_merge(
    conn: Any, registry: Any, *, proposal_id: str, diff: Mapping[str, Any], actor: str,
) -> dict[str, Any]:
    """Apply an ACCEPTED ``{"op": "merge_entities", "from": <loser>, "into": <keeper>}``
    correction through the same merge as consult (``from`` / ``into`` are entity
    ids, or exact names that resolve to ONE profile). The journal proposed; the
    operator accepted: provenance names the proposal, the audit row the
    accepting principal (role ``operator_via_journal``). Raises :class:`Refused`
    when the merge may not run — never a silent success."""
    loser = await _journal_ref(conn, diff.get("from"), "loser (from)")
    keeper = await _journal_ref(conn, diff.get("into"), "keeper (into)")
    if loser["id"] == keeper["id"]:
        raise Refused("preflight_failed", "from and into are the same entity.")
    if loser["compacted_at"]:
        raise _compacted_refusal(loser, doing="A merge")
    if loser["merged_into"] is not None or keeper["merged_into"] is not None:
        raise Refused("preflight_failed", "one side is already merged; merge into the survivor.")
    async with conn.transaction():
        rows = await E.lock(conn, [keeper["id"], loser["id"]])
        k, lo = rows.get(keeper["id"]), rows.get(loser["id"])
        E.verify_unchanged(k, version=keeper["version"], fields_sha=E.sha(E.fields_of(keeper)), what="keeper")
        E.verify_unchanged(lo, version=loser["version"], fields_sha=E.sha(E.fields_of(loser)), what="loser")
        facts = await E.open_facts(conn, lo["canonical_name"])
        who = f"journal_proposal:{proposal_id}"
        reason = str(diff.get("reason") or diff.get("rationale") or "accepted journal correction")
        prov = E.provenance(actor=who, actor_role="operator_via_journal", reason=reason,
                            journal_proposal_id=str(proposal_id), principal=actor)
        done = await E.do_merge(conn, k, lo, prov=prov, reason=reason)
        await E.record_audit(
            conn, registry, in_tx=None, actor=actor, actor_role="operator_via_journal", object_id=lo["id"],
            action="merge", from_version=lo["version"], to_version=done["loser_version"],
            summary={"journal_proposal_id": str(proposal_id), "keeper_id": k["id"],
                     "keeper_version": done["keeper_version"], "added_aliases": done["added_aliases"]},
        )
    return {
        "op": "merge_entities", "applied": "merged", "keeper_id": keeper["id"], "loser_id": loser["id"],
        "added_aliases": done["added_aliases"], "open_facts_naming_loser": facts["open_naming"],
        "edges": E.MERGE_EDGES_DEFERRED,
        "unmerge": "unmerge_entity (consult) until entity_gc compacts the loser",
    }
