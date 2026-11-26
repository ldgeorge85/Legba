# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The entity record class's APPLY LAYER — the SQL every entity write runs (W11, C-2b-2/entities).

One place writes an operator-directed change to ``entity_profiles``: the
consult write executor's ``entity`` branch (``consult_write_entity.py``) and
the journal accept of a ``merge_entities`` correction
(``journal_proposals_apply.py``) both land here, inside a transaction the
caller opened, on rows the caller LOCKED (:func:`lock`). Nothing here decides
whether a write may happen — the branch does that; this module performs it
exactly and leaves a record:

* **A version row on every write** (``entity_profile_versions``, version +1 on
  the profile): the snapshot after the write plus ``data.provenance`` — who
  (``actor``: ``consult_write:<id>`` / ``journal_proposal:<id>``), the role,
  when, why (the operator's reason), the author axis ``author = operator`` and
  ``origin_class = live`` (decision D7: the operator's hand is on the AUTHOR
  axis; ``origin_class`` answers "present or history", and an operator edit is
  the present). ``analyst_id`` names the writer, ``run_id`` the consult write.
* **One signed audit row** (:func:`record_audit`) — namespace ``entity``, the
  record id, the verb, from/to version — with the consult receipt in
  ``change_summary`` when a consult write runs it (the executor's ``in_tx``
  hook fires on the same connection, so mutation, ledger row and audit row
  commit together).

THE MERGE (:func:`do_merge`) is the tombstone-redirect merge the lifecycle
already reads: the loser gets ``merged_into = keeper`` and
``data.gc_status = 'merged'``; its name and aliases fold onto the keeper's
``data.merged_aliases`` (so entity resolution canonicalizes future mentions
onto the keeper). It does NOT move anything else: the loser's id-keyed
``entity_edges`` / event links and its name-keyed facts, nexuses and edge
candidates are re-pointed by ``entity_gc``'s compaction
(``data.merge.edges = 'deferred_to_compaction'`` tells it to fold the id-keyed
ones too). That is what makes :func:`do_unmerge` EXACT before compaction: the
loser's ``data.merge`` block records the aliases it added and the loser's own
prior ``gc_status`` / ``merge`` — the pre-image. After compaction
(``data.merge.compacted_at``) ``facts.subject`` has been rewritten in place
and an unmerge cannot be exact: SEAMS #68.

A leaf of the registry package (stdlib + the vocabulary / tags /
case-columns / origin leaves + the audit logger) — the slim registry image
imports it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from uuid import UUID

from ..case_columns import ENTITY_CLASS_FAMILY, bind_entity_type
from ..principals import handle_for_actor
from ..provenance.origin import live_gate_sql
from ..tags import VocabularyIndex, stamp_classifications
from .audit import AuditLogger
from .errors import VersionConflict

__all__ = [
    "AUTHOR",
    "FIELD_KEYS",
    "MERGE_EDGES_DEFERRED",
    "ORIGIN_CLASS",
    "SEAM_UNMERGE",
    "apply_fields_patch",
    "class_index",
    "do_edit",
    "do_merge",
    "do_remerge",
    "do_unmerge",
    "fields_of",
    "judgement",
    "lock",
    "merge_added_aliases",
    "open_facts",
    "provenance",
    "read",
    "record_audit",
    "sha",
    "state_of",
    "unmerge_removed_aliases",
    "verify_unchanged",
]

#: SEAMS #68 — entity unmerge after compaction (or of a merge whose pre-image
#: was never recorded), entity split, entity retire. (C-2a §8 named this #66;
#: that number went to the declared subject geometry before W11.)
SEAM_UNMERGE = 68

#: The author axis value every operator-directed entity write carries (D7).
AUTHOR = "operator"
#: ``origin_class`` stays ``live`` for an operator write (D7) — the closed
#: six-value vocabulary of ``provenance/origin.py`` is not widened.
ORIGIN_CLASS = "live"

#: ``data.merge.edges`` on a loser this layer merged: compaction folds the
#: id-keyed edges and event links too (``entity_gc._compact_merged_edges``).
MERGE_EDGES_DEFERRED = "deferred_to_compaction"

#: The writable view of a profile — what an edit may change, what a pre-image holds.
FIELD_KEYS: tuple[str, ...] = (
    "canonical_name", "entity_class", "aliases", "geo_country", "geo_region", "geo_lat", "geo_lon",
)

_NAME_MAX = 300
_ALIAS_MAX = 300
_GEO_TEXT_MAX = 120
#: Ids of the open facts naming a merge loser kept on the pre-image (the count is whole).
FACT_IDS_KEPT = 50

_PROFILE_COLS = (
    "id, canonical_name, entity_class, entity_type, version, geo_country, geo_region, "
    "geo_lat, geo_lon, merged_into, COALESCE(data, '{}'::jsonb) AS data"
)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, (str, bytes, bytearray)):
        value = json.loads(value)
    return dict(value or {})


def sha(obj: Any) -> str:
    """sha256 of the canonical JSON of ``obj`` — the pre/post-image hash."""
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    data = _as_dict(row["data"])
    merge = data.get("merge") if isinstance(data.get("merge"), dict) else None
    aliases = [str(a) for a in (data.get("merged_aliases") or []) if isinstance(a, str)]
    return {
        "id": str(row["id"]),
        "version": str(row["version"]),
        "canonical_name": row["canonical_name"],
        "entity_class": row["entity_class"],
        "entity_type": row["entity_type"],
        "aliases": aliases,
        "geo_country": row["geo_country"],
        "geo_region": row["geo_region"],
        "geo_lat": row["geo_lat"],
        "geo_lon": row["geo_lon"],
        "merged_into": str(row["merged_into"]) if row["merged_into"] is not None else None,
        "gc_status": data.get("gc_status"),
        "merge": merge,
        "compacted_at": (merge or {}).get("compacted_at"),
        "data": data,
    }


async def read(conn: Any, entity_id: str) -> dict[str, Any] | None:
    """One profile's snapshot, or None (an unknown or malformed id)."""
    try:
        uid = UUID(str(entity_id))
    except ValueError:
        return None
    row = await conn.fetchrow(f"SELECT {_PROFILE_COLS} FROM entity_profiles WHERE id = $1", uid)
    return _snapshot(row) if row is not None else None


async def lock(conn: Any, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Lock the profiles FOR UPDATE (in id order — two writers never deadlock)
    and return their snapshots, keyed by id."""
    uids = sorted({UUID(str(i)) for i in ids})
    rows = await conn.fetch(
        f"SELECT {_PROFILE_COLS} FROM entity_profiles WHERE id = ANY($1::uuid[]) ORDER BY id FOR UPDATE",
        uids,
    )
    return {str(r["id"]): _snapshot(r) for r in rows}


def fields_of(snap: Mapping[str, Any]) -> dict[str, Any]:
    return {k: snap[k] for k in FIELD_KEYS}


def state_of(snap: Mapping[str, Any]) -> str:
    """``live`` · ``merged`` (a redirect, unmergeable) · ``merged_compacted``."""
    if snap.get("merged_into") is None:
        return "live"
    return "merged_compacted" if snap.get("compacted_at") else "merged"


def verify_unchanged(snap: Mapping[str, Any] | None, *, version: str, fields_sha: str, what: str) -> None:
    """Compare-and-set inside the transaction: the locked row must still be the
    one the plan read. Raises ``VersionConflict`` (the executor's ``stale_read``)."""
    if snap is None:
        raise VersionConflict(f"{what} no longer exists")
    if snap["version"] != version or sha(fields_of(snap)) != fields_sha:
        raise VersionConflict(f"{what} changed since it was read (now version {snap['version']})")


async def open_facts(conn: Any, name: str, *, limit: int = FACT_IDS_KEPT) -> dict[str, Any]:
    """The OPEN live facts whose ``subject`` names ``name`` — the rows a
    compaction would re-point after a merge (count + the first ids)."""
    gate = live_gate_sql("f")
    n = await conn.fetchval(
        f"SELECT count(*) FROM facts f WHERE lower(f.subject) = lower($1) AND {gate}", name,
    )
    ids = await conn.fetch(
        f"SELECT f.id FROM facts f WHERE lower(f.subject) = lower($1) AND {gate} "
        "ORDER BY f.id LIMIT $2", name, limit,
    ) if n else []
    return {"open_naming": int(n or 0), "ids": [str(r["id"]) for r in ids], "as_of": _now_iso()}


def pair_key(a: str, b: str) -> str:
    lo, hi = sorted((str(a), str(b)))
    return f"{lo}::{hi}"


async def judgement(conn: Any, a: str, b: str) -> dict[str, Any] | None:
    """The ``entity_judgement`` row of a pair (its pre-image), or None."""
    row = await conn.fetchrow(
        "SELECT verdict, justification, decided_by, model_id, confidence, decided_at "
        "FROM entity_judgement WHERE pair_key = $1", pair_key(a, b),
    )
    if row is None:
        return None
    out = dict(row)
    out["decided_at"] = out["decided_at"].isoformat() if out.get("decided_at") else None
    return out


# ---------------------------------------------------------------------------
# The patch grammar (edit) and the merge arithmetic (pure)
# ---------------------------------------------------------------------------


class PatchProblem(ValueError):
    """``(cause, detail)`` — the branch turns it into a refusal."""

    def __init__(self, cause: str, detail: str) -> None:
        super().__init__(f"{cause}: {detail}")
        self.cause, self.detail = cause, detail


def _text(key: str, value: Any, *, cap: int, nullable: bool) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PatchProblem("preflight_failed", f"{key} must be a non-empty string{' or null' if nullable else ''}.")
    value = " ".join(value.split())
    if len(value) > cap:
        raise PatchProblem("preflight_failed", f"{key} is longer than {cap} characters.")
    return value


def _coord(key: str, value: Any, bound: float) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not -bound <= float(value) <= bound:
        raise PatchProblem("preflight_failed", f"{key} must be a number in [-{bound:g}, {bound:g}] or null.")
    return float(value)


def _alias_list(key: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise PatchProblem("preflight_failed", f"{key} must be a non-empty list of names.")
    return [str(_text(key, v, cap=_ALIAS_MAX, nullable=False)) for v in value]


def apply_fields_patch(
    snap: Mapping[str, Any], patch: Mapping[str, Any], *, writable: Iterable[str], bind_class: Any,
) -> dict[str, Any]:
    """The profile's writable view after ``patch`` — or :class:`PatchProblem`.

    ``bind_class(raw) -> str`` binds an ``entity_class`` through the live
    family (it raises :class:`PatchProblem` when the value is not a member)."""
    allowed = set(writable)
    if not isinstance(patch, Mapping) or not patch:
        raise PatchProblem("preflight_failed", "patch must be a non-empty object.")
    unknown = sorted(k for k in patch if k not in allowed)
    if unknown:
        raise PatchProblem(
            "path_not_writable",
            f"{', '.join(unknown)} — an entity patch names only: {', '.join(sorted(allowed))}.",
        )
    out = fields_of(snap)
    out["aliases"] = list(out["aliases"])
    if "canonical_name" in patch:
        out["canonical_name"] = _text("canonical_name", patch["canonical_name"], cap=_NAME_MAX, nullable=False)
    if "entity_class" in patch:
        raw = _text("entity_class", patch["entity_class"], cap=64, nullable=False)
        out["entity_class"] = bind_class(raw)
    for key in ("geo_country", "geo_region"):
        if key in patch:
            out[key] = _text(key, patch[key], cap=_GEO_TEXT_MAX, nullable=True)
    if "geo_lat" in patch:
        out["geo_lat"] = _coord("geo_lat", patch["geo_lat"], 90.0)
    if "geo_lon" in patch:
        out["geo_lon"] = _coord("geo_lon", patch["geo_lon"], 180.0)
    if "aliases_remove" in patch:
        for alias in _alias_list("aliases_remove", patch["aliases_remove"]):
            hit = next((a for a in out["aliases"] if a.lower() == alias.lower()), None)
            if hit is None:
                raise PatchProblem("preflight_failed", f"{alias!r} is not an alias of this entity.")
            out["aliases"].remove(hit)
    if "aliases_add" in patch:
        for alias in _alias_list("aliases_add", patch["aliases_add"]):
            if alias.lower() == str(out["canonical_name"]).lower():
                raise PatchProblem("preflight_failed", f"{alias!r} is the entity's own name, not an alias.")
            if all(a.lower() != alias.lower() for a in out["aliases"]):
                out["aliases"].append(alias)
    return out


def merge_added_aliases(keeper: Mapping[str, Any], loser: Mapping[str, Any]) -> list[str]:
    """The surface forms a merge folds onto the keeper: the loser's name and
    aliases the keeper does not already carry (case-insensitive), in order."""
    seen = {a.lower() for a in keeper["aliases"]} | {str(keeper["canonical_name"]).lower()}
    added: list[str] = []
    for cand in [loser["canonical_name"], *loser["aliases"]]:
        c = str(cand or "").strip()
        if c and c.lower() not in seen:
            added.append(c)
            seen.add(c.lower())
    return added


def unmerge_removed_aliases(keeper: Mapping[str, Any], added: Iterable[str]) -> list[str]:
    """Of the aliases a merge added, the ones the keeper still carries."""
    present = list(keeper["aliases"])
    removed: list[str] = []
    for alias in added:
        if alias in present:
            present.remove(alias)
            removed.append(alias)
    return removed


def _without(aliases: list[str], drop: Iterable[str]) -> list[str]:
    out = list(aliases)
    for alias in drop:
        if alias in out:
            out.remove(alias)
    return out


def class_index(values: Iterable[str]) -> VocabularyIndex:
    """A binding snapshot holding the live ``entity_class`` family only."""
    return VocabularyIndex(values={ENTITY_CLASS_FAMILY: frozenset(values)})


# ---------------------------------------------------------------------------
# Provenance, the version row, the audit row
# ---------------------------------------------------------------------------


def provenance(*, actor: str, actor_role: str, reason: str | None, write_id: str | None = None,
               journal_proposal_id: str | None = None, principal: str | None = None) -> dict[str, Any]:
    """Who / when / why / author / origin — stamped on every version row."""
    out = {
        "actor": actor, "actor_role": actor_role, "author": AUTHOR, "origin_class": ORIGIN_CLASS,
        "reason": (reason or "")[:300], "at": _now_iso(),
    }
    if write_id:
        out["write_id"] = write_id
    if journal_proposal_id:
        out["journal_proposal_id"] = journal_proposal_id
    if principal:
        out["principal"] = principal
    return out


async def _write(
    conn: Any, snap: Mapping[str, Any], *, fields: Mapping[str, Any], data: Mapping[str, Any],
    merged_into: str | None, event: str, prov: Mapping[str, Any], entity_type: str | None = None,
) -> str:
    """UPDATE the locked profile (version +1) and INSERT its version row. Returns the new version."""
    entity_type = entity_type or snap["entity_type"]
    new_version = await conn.fetchval(
        "UPDATE entity_profiles SET canonical_name = $2, entity_class = $3, entity_type = $4, "
        "geo_country = $5, geo_region = $6, geo_lat = $7, geo_lon = $8, merged_into = $9::uuid, "
        "data = $10::jsonb, version = version + 1, updated_at = now() "
        "WHERE id = $1::uuid AND version = $11 RETURNING version",
        snap["id"], fields["canonical_name"], fields["entity_class"], entity_type,
        fields["geo_country"], fields["geo_region"], fields["geo_lat"], fields["geo_lon"],
        merged_into, json.dumps(data, default=str), int(snap["version"]),
    )
    if new_version is None:
        raise VersionConflict(f"entity {snap['id']} moved inside the transaction")
    run_id = prov.get("write_id")
    await conn.execute(
        "INSERT INTO entity_profile_versions (entity_id, version, data, analyst_id, run_id) "
        "VALUES ($1::uuid, $2, $3::jsonb, $4, $5::uuid)",
        snap["id"], int(new_version),
        json.dumps({
            "event": event,
            "canonical_name": fields["canonical_name"],
            "entity_class": fields["entity_class"],
            "entity_type": entity_type,
            "merged_aliases": list(fields["aliases"]),
            "geo": {k: fields[k] for k in ("geo_country", "geo_region", "geo_lat", "geo_lon")},
            "merged_into": merged_into,
            "gc_status": data.get("gc_status"),
            "provenance": dict(prov),
        }, default=str),
        str(prov.get("actor") or "")[:200] or None,
        run_id if run_id else None,
    )
    return str(new_version)


async def record_audit(
    conn: Any, registry: Any, *, in_tx: Any | None, actor: str, actor_role: str, object_id: str,
    action: str, from_version: str, to_version: str, summary: Mapping[str, Any] | None = None,
) -> None:
    """One signed audit row on THIS connection; with a consult ``in_tx`` hook,
    its receipt rides ``change_summary`` and the hook fires after the row."""
    change = dict(summary or {})
    if in_tx is not None:
        change["receipt"] = in_tx.receipt_for(from_version=from_version, to_version=to_version)
    entry = await AuditLogger(identity=registry.signing_identity).record(
        conn, actor_id=actor, actor_role=actor_role, namespace="entity", descriptor_id=object_id,
        action=action, from_version=from_version, to_version=to_version, change_summary=change,
    )
    if in_tx is not None:
        await in_tx(conn, audit_id=entry.id, from_version=from_version, to_version=to_version)


# ---------------------------------------------------------------------------
# The writes (each on LOCKED snapshots, inside the caller's transaction)
# ---------------------------------------------------------------------------


async def do_edit(
    conn: Any, snap: Mapping[str, Any], new_fields: Mapping[str, Any], *,
    class_values: Iterable[str] | None, prov: Mapping[str, Any], event: str = "operator_edit",
) -> str:
    """Write the edited view (or a revert's restored one). A class change binds
    ``entity_type`` through the family (TAX-4) and re-stamps the TAG-1
    ``data.classifications`` for the new class."""
    data = dict(snap["data"])
    data["merged_aliases"] = list(new_fields["aliases"])
    entity_type = None
    if new_fields["entity_class"] != snap["entity_class"]:
        index = class_index(class_values or ())
        entity_type = bind_entity_type(new_fields["entity_class"], families=index)
        data = stamp_classifications(
            "entity_profiles", {"entity_class": new_fields["entity_class"]}, data, families=index,
        )
    return await _write(conn, snap, fields=new_fields, data=data, merged_into=snap["merged_into"],
                        event=event, prov=prov, entity_type=entity_type)


async def do_merge(
    conn: Any, keeper: Mapping[str, Any], loser: Mapping[str, Any], *, prov: Mapping[str, Any],
    reason: str | None,
) -> dict[str, Any]:
    """Fold ``loser`` into ``keeper``. Returns the versions and the aliases added."""
    added = merge_added_aliases(keeper, loser)
    kfields = {**fields_of(keeper), "aliases": [*keeper["aliases"], *added]}
    kdata = {**keeper["data"], "merged_aliases": kfields["aliases"]}
    keeper_version = await _write(conn, keeper, fields=kfields, data=kdata, merged_into=keeper["merged_into"],
                                  event="operator_merge_keeper", prov=prov)
    ldata = dict(loser["data"])
    ldata["gc_status"] = "merged"
    ldata["merge"] = {
        "into": keeper["id"], "reason": (reason or "")[:400], "decided_by": AUTHOR,
        "actor": prov.get("actor"), "write_id": prov.get("write_id"), "at": prov.get("at"),
        "edges": MERGE_EDGES_DEFERRED, "added_aliases": added,
        "pre": {"gc_status": loser["gc_status"], "merge": loser["merge"]},
    }
    loser_version = await _write(conn, loser, fields=fields_of(loser), data=ldata, merged_into=keeper["id"],
                                 event="operator_merge_loser", prov=prov)
    return {"keeper_version": keeper_version, "loser_version": loser_version, "added_aliases": added}


def _restore(data: dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value


async def _set_judgement(conn: Any, a: str, b: str, value: Mapping[str, Any] | None,
                         *, prov: Mapping[str, Any]) -> None:
    key = pair_key(a, b)
    if value is None:
        await conn.execute("DELETE FROM entity_judgement WHERE pair_key = $1", key)
        return
    lo, hi = sorted((str(a), str(b)))
    await conn.execute(
        "INSERT INTO entity_judgement (pair_key, entity_a, entity_b, verdict, justification, "
        "decided_by, model_id, confidence, decided_at, written_by) "
        "VALUES ($1, $2::uuid, $3::uuid, $4, $5, $6, $7, $8, COALESCE($9::timestamptz, now()), "
        "COALESCE(legba_principal($10), "
        "(SELECT cw.written_by FROM consult_writes cw WHERE cw.id = $11::uuid))) "
        "ON CONFLICT (pair_key) DO UPDATE SET verdict = EXCLUDED.verdict, "
        "justification = EXCLUDED.justification, decided_by = EXCLUDED.decided_by, "
        "model_id = EXCLUDED.model_id, confidence = EXCLUDED.confidence, decided_at = EXCLUDED.decided_at, "
        "written_by = EXCLUDED.written_by",
        key, lo, hi, value["verdict"], value.get("justification"), value["decided_by"],
        value.get("model_id"), value.get("confidence"),
        datetime.fromisoformat(value["decided_at"]) if value.get("decided_at") else None,
        # The person (or credential) behind this write — the stamped
        # actor when it is a principal's, else the Consult write row's own
        # written_by (its actor is ``consult_write:<id>``, not a principal).
        handle_for_actor(prov.get("actor")),
        _uuid_or_none(prov.get("write_id")),
    )


def _uuid_or_none(value: Any) -> str | None:
    try:
        return str(UUID(str(value))) if value else None
    except ValueError:
        return None


async def do_unmerge(
    conn: Any, keeper: Mapping[str, Any], loser: Mapping[str, Any], *, prov: Mapping[str, Any],
    added: Iterable[str], loser_pre: Mapping[str, Any],
) -> dict[str, Any]:
    """Split ``loser`` back out of ``keeper``: clear the redirect, restore the
    loser's own ``gc_status`` / ``merge`` from the pre-image, drop from the
    keeper exactly the aliases the merge added, and record the pair DISTINCT
    (``entity_judgement`` verdict ``not_same``, ``decided_by = human`` — the
    table's CHECK; a human verdict is never overwritten by an llm/rule pass —
    with the consult write named in the justification)."""
    removed = unmerge_removed_aliases(keeper, added)
    kfields = {**fields_of(keeper), "aliases": _without(keeper["aliases"], removed)}
    keeper_version = await _write(conn, keeper, fields=kfields,
                                  data={**keeper["data"], "merged_aliases": kfields["aliases"]},
                                  merged_into=keeper["merged_into"], event="operator_unmerge_keeper", prov=prov)
    ldata = dict(loser["data"])
    _restore(ldata, "gc_status", loser_pre.get("gc_status"))
    _restore(ldata, "merge", loser_pre.get("merge"))
    loser_version = await _write(conn, loser, fields=fields_of(loser), data=ldata, merged_into=None,
                                 event="operator_unmerge_loser", prov=prov)
    judged_by = prov.get("actor") or "operator"
    await _set_judgement(conn, keeper["id"], loser["id"], {
        "verdict": "not_same", "decided_by": "human", "model_id": None, "confidence": 1.0,
        "justification": f"{judged_by}: {(prov.get('reason') or '')}"[:1000], "decided_at": None,
    }, prov=prov)
    return {"keeper_version": keeper_version, "loser_version": loser_version, "removed_aliases": removed}


async def do_remerge(
    conn: Any, keeper: Mapping[str, Any], loser: Mapping[str, Any], *, prov: Mapping[str, Any],
    removed: Iterable[str], loser_merge: Mapping[str, Any] | None, loser_gc_status: Any,
    judgement_pre: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The inverse of :func:`do_unmerge` (a revert of an unmerge): the redirect,
    the loser's merge block and the keeper's removed aliases come back from the
    stored pre-image, and the pair's judgement returns to what it was."""
    kaliases = list(keeper["aliases"])
    for alias in removed:
        if alias not in kaliases:
            kaliases.append(alias)
    kfields = {**fields_of(keeper), "aliases": kaliases}
    keeper_version = await _write(conn, keeper, fields=kfields, data={**keeper["data"], "merged_aliases": kaliases},
                                  merged_into=keeper["merged_into"], event="operator_remerge_keeper", prov=prov)
    ldata = dict(loser["data"])
    _restore(ldata, "gc_status", loser_gc_status)
    _restore(ldata, "merge", dict(loser_merge) if loser_merge else None)
    loser_version = await _write(conn, loser, fields=fields_of(loser), data=ldata, merged_into=keeper["id"],
                                 event="operator_remerge_loser", prov=prov)
    await _set_judgement(conn, keeper["id"], loser["id"], judgement_pre, prov=prov)
    return {"keeper_version": keeper_version, "loser_version": loser_version}
