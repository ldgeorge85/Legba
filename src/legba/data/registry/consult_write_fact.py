# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult write executor's ``fact`` BRANCH (W11 C-2b-2/facts; design C-2a §1 row 6, §4).

A RECORD class: no registry head, no lifecycle. A fact write is a
provenance-carrying insert or close on ``facts`` through the provenance
writers (``provenance/writes.py:insert_operator_fact``,
``provenance/writes_supersession.py``'s exact-id closers), in ONE transaction
with its ``consult_writes`` row and one signed audit row (namespace ``fact``,
the pair as the object, actor ``consult_write:<id>``, role
``operator_via_consult``) — the executor's ``in_tx`` hook, called here because
no ``DescriptorRegistry`` method writes a record (C-2a §3: "Records get it from
the executor").

**The object is the PAIR** ``(subject, predicate)``; its ref is
``<subject> | <predicate>`` lower-cased (``consult_write_vocabulary.fact_ref``)
and its "version" is the digest of its OPEN set (``open_set_digest`` over the
open row ids): what compare-and-set checks inside the transaction, what a
staged write re-checks at confirm, what a revert checks before it undoes.

* :func:`inspect` — the pair's open rows with provenance, authority tier,
  valid window, origin class and the rows each superseded; the last closed
  rows; this session's executed writes on the pair. It seeds the read set:
  every open id it returns may be named by ``assert_fact``'s ``supersede``
  list or by ``close_fact``.
* ``assert`` — the operator's word as ONE new open row, ``source_type
  'operator'`` (D6: tier 2 with seed/curated, grounding like curated),
  confidence 1.0 unless given, ``origin_class 'live'`` (D7), the predicate
  bound through the registered vocabulary where its family exists (else
  ``src:`` legacy — an unregistered predicate never blocks an assert). The
  close set is EXPLICIT (``supersede``):

  - ``none`` — no differing value is closed (both stand open);
  - ``differing`` — the pair's open rows with a different value are closed,
    EXCEPT seed/curated rows: the instance's curated baseline is replaced only
    by naming its id. A broad sweep the operator did not look at must not
    silently retire curated ground truth, while an earlier operator row and
    every machine row (ingestion / agent / proposed / manual) are fair game;
  - ``[ids]`` — exactly those open rows of the pair, any tier (the operator
    saw them through ``inspect_object``; the gate holds them to the read set).

  A same-value MACHINE row is replaced by the operator's row in every mode (one
  open row per triple — the open-triple index forbids two); a same-value
  AUTHORITATIVE row (seed / curated / operator) makes the assert ``no_change``.
  The closed ids are the write's pre-image; closing more than one row is
  Tier B.
* ``close`` — one open row: ``valid_until = now``, ``superseded_by`` stays
  empty, ``data.closed_by = consult_write:<id>`` (the 0117 soft-close idiom).
* :func:`plan_revert` — assert: close the asserted row and reopen EXACTLY the
  rows it closed, refused ``superseded_since`` unless the pair's open set is
  still the write's post-image and each row is still closed by THIS write's
  fact; afterwards the open set must equal the stored pre-image (else the
  transaction rolls back). close: reopen the row, refused ``superseded_since``
  unless it is still closed by this write and no other open row holds the
  same triple.
* :func:`activation` — ``not_applicable``: a fact has no runtime head; it is
  read by the next query.

A leaf of the registry package: provenance writers and the vocabulary leaves —
no runtime module, no analyst module.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from .. import consult_write_vocabulary as W
from ..provenance import writes_supersession as S
from ..provenance.writes import insert_operator_fact
from ..tags import EMPTY_INDEX, VocabularyIndex, split_tag
from ..vocabulary import normalize_predicate
from . import consult_write_source as _source
from .errors import VersionConflict

__all__ = ["FAMILY", "NAMESPACE", "VERBS", "activation", "inspect", "plan", "plan_revert"]

#: The verbs this branch executes — the drift guard holds them equal to the
#: vocabulary's built verbs for ``fact``.
VERBS: frozenset[str] = frozenset({"inspect", "assert", "close", "revert"})

#: A record class has no registry family. The executor reads ``FAMILY`` only to
#: roll a head back after a proven ``parse_failed``, which :func:`activation`
#: never reports for a fact.
FAMILY = None

#: The audit namespace of a fact write (C-2a §3: namespace = the class).
NAMESPACE = "fact"

Plan = _source.Plan
Refused = _source.Refused

#: ``source_type`` values on the authoritative tier (rank 2).
AUTHORITATIVE: frozenset[str] = frozenset({"seed", "curated", S.OPERATOR_SOURCE_TYPE})
#: The curated baseline: never closed by ``supersede='differing'``.
CURATED: frozenset[str] = frozenset({"seed", "curated"})

_ROLE = "operator_via_consult"
_RECENT_CLOSED = 5


def _iso(ts: Any) -> str | None:
    if isinstance(ts, datetime):
        return ts.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return None


def _st(row: Mapping[str, Any]) -> str:
    return str(row.get("source_type") or "").strip().lower()


def _data(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("data")
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _brief(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": str(row["id"]), "value": row["value"], "source_type": row.get("source_type")}


def _view(row: Mapping[str, Any]) -> dict[str, Any]:
    st = _st(row)
    return {
        "id": str(row["id"]),
        "value": row["value"],
        "source_type": row.get("source_type"),
        "tier": S._source_tier_rank(st),
        "authority": "authoritative" if st in AUTHORITATIVE else "machine",
        "confidence": float(row["confidence"]) if row.get("confidence") is not None else None,
        "valid_from": _iso(row.get("valid_from")),
        "origin_class": row.get("origin_class"),
        "recorded_at": _iso(row.get("created_at")),
        "analyst_id": row.get("analyst_id"),
        "write_id": _data(row).get("write_id"),
    }


def _is_uuid(value: Any) -> bool:
    try:
        UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _pair(subject: str, predicate: str) -> tuple[str, str]:
    return subject.strip(), normalize_predicate(predicate.strip())


async def _families(conn: Any, predicate: str) -> VocabularyIndex:
    """The vocabulary snapshot the predicate binds against: its own family's
    registered entries when the predicate is family-shaped, else EMPTY (a bare
    predicate binds ``src:`` legacy without a read). A family with no entries
    is unloaded, so the predicate binds ``unclassified`` — recorded, never a
    refusal."""
    family, _value = split_tag(predicate)
    if family is None:
        return EMPTY_INDEX
    rows = await conn.fetch(
        "SELECT family, value, aliases, deprecated FROM vocabulary_entries WHERE family = $1",
        family,
    )
    return VocabularyIndex.from_entries([dict(r) for r in rows]) if rows else EMPTY_INDEX


async def _closed_rows(conn: Any, subjects: Sequence[str], predicate: str) -> list[Any]:
    """The pair's most recently closed rows (by the exact stored subject
    spellings — ``idx_facts_subject`` — never a scan of ``facts``)."""
    return await conn.fetch(
        """
        SELECT id, value, source_type, valid_until, superseded_by, superseded_at,
               data->>'closed_by' AS closed_by
          FROM facts
         WHERE subject = ANY($1::text[])
           AND lower(predicate) = lower($2)
           AND valid_until IS NOT NULL
         ORDER BY valid_until DESC, id
         LIMIT $3
        """,
        list(dict.fromkeys(subjects)),
        predicate,
        _RECENT_CLOSED * 2,
    )


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


async def inspect(registry: Any, ref: str, *, session_id: str | None) -> dict[str, Any]:
    async with registry.pg.acquire() as conn:
        focus: str | None = None
        if _is_uuid(ref):
            row = await conn.fetchrow("SELECT subject, predicate FROM facts WHERE id = $1", UUID(str(ref)))
            if row is None:
                return {"found": False, "class": "fact", "id": ref}
            subject, predicate = str(row["subject"]), str(row["predicate"])
            focus = str(UUID(str(ref)))
        else:
            parsed = W.parse_fact_ref(ref)
            if parsed is None:
                return {"found": False, "class": "fact", "id": ref,
                        "hint": "a fact is inspected by its id or as '<subject> | <predicate>'."}
            subject, predicate = _pair(*parsed)
        open_rows = await S.read_open_fact_set(conn, subject=subject, predicate=predicate)
        spellings = [subject, *(str(r["subject"]) for r in open_rows)]
        closed = await _closed_rows(conn, spellings, predicate)
        pair_ref = W.fact_ref(subject, predicate)
        writes = await conn.fetch(
            "SELECT id, verb, status, executed_at, reversed_by_write_id "
            "FROM consult_writes WHERE class = 'fact' AND object_ref = $1 "
            "AND session_id = $2::uuid AND status = 'executed' AND verb <> 'revert' "
            "ORDER BY created_at DESC LIMIT 10",
            pair_ref, session_id,
        ) if session_id else []
    by_superseder: dict[str, list[dict[str, Any]]] = {}
    for c in closed:
        if c["superseded_by"] is not None:
            by_superseder.setdefault(str(c["superseded_by"]), []).append({
                "id": str(c["id"]), "value": c["value"], "source_type": c["source_type"],
                "superseded_at": _iso(c["superseded_at"]),
            })
    rows = []
    for r in open_rows:
        v = _view(r)
        v["supersedes"] = by_superseder.get(v["id"], [])
        rows.append(v)
    return {
        "found": True,
        "class": "fact",
        "id": pair_ref,
        "subject": subject,
        "predicate": predicate,
        "focus": focus,
        "version": S.open_set_digest(r["id"] for r in open_rows),
        "state": f"{len(open_rows)} open",
        "open": rows,
        "recent_closed": [
            {"id": str(c["id"]), "value": c["value"], "source_type": c["source_type"],
             "valid_until": _iso(c["valid_until"]),
             "superseded_by": str(c["superseded_by"]) if c["superseded_by"] else None,
             "closed_by": c["closed_by"]}
            for c in closed[:_RECENT_CLOSED]
        ],
        "writes": [
            {"write_id": str(w["id"]), "verb": w["verb"],
             "executed_at": w["executed_at"].isoformat() if w["executed_at"] else None,
             "reverted": w["reversed_by_write_id"] is not None}
            for w in writes
        ],
        "supersede": (
            "assert_fact supersede: 'none' closes no other value; 'differing' closes the open rows "
            "with a different value except seed/curated ones; a list closes exactly those open ids "
            "(any tier). A same-value machine row is replaced by the operator's row."
        ),
        "confirmation": "closing or superseding more than one open row waits for the operator's confirmation.",
    }


# ---------------------------------------------------------------------------
# plan — a model call → an executable write
# ---------------------------------------------------------------------------


async def plan(registry: Any, verb: str, args: Mapping[str, Any]) -> Plan:
    if verb == "assert":
        return await _plan_assert(registry, args)
    if verb == "close":
        return await _plan_close(registry, args)
    raise Refused("class_not_writable", f"fact has no {verb} verb in the write plane.")


def _valid_from(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    try:
        ts = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise Refused("preflight_failed", f"valid_from {raw!r} is not an ISO date or date-time.") from exc
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _supersede(raw: Any) -> tuple[str, list[str]]:
    if isinstance(raw, str) and raw in W.SUPERSEDE_MODES:
        return raw, []
    if isinstance(raw, (list, tuple)) and raw:
        bad = [str(i) for i in raw if not _is_uuid(i)]
        if bad:
            raise Refused("ref_not_read", f"{', '.join(bad[:3])} is not a fact id inspect_object returned.")
        return "ids", sorted({str(UUID(str(i))) for i in raw})
    raise Refused(
        "preflight_failed",
        f"supersede must be 'none', 'differing' or a list of fact ids; got {raw!r}.",
    )


def _audit_logger(registry: Any) -> Any:
    return registry._audit  # noqa: SLF001 — the registry's signing logger (C-2a §3)


async def _record(
    registry: Any, conn: Any, in_tx: Any, *, actor: str, pair_ref: str, action: str,
    from_version: str, to_version: str,
) -> None:
    """The receipt on one signed audit row, then the ledger row — on ``conn``."""
    receipt = in_tx.receipt_for(from_version=from_version, to_version=to_version)
    entry = await _audit_logger(registry).record(
        conn, actor_id=actor, actor_role=_ROLE, namespace=NAMESPACE, descriptor_id=pair_ref,
        action=action, from_version=from_version, to_version=to_version,
        change_summary={"receipt": receipt},
    )
    await in_tx(conn, audit_id=entry.id, from_version=from_version, to_version=to_version)


async def _locked_digest(conn: Any, subject: str, predicate: str, expected: str) -> str:
    """Lock the pair, re-read its open set in THIS transaction, compare-and-set."""
    await S.lock_fact_pair(conn, subject=subject, predicate=predicate)
    now = S.open_set_digest(r["id"] for r in await S.read_open_fact_set(conn, subject=subject, predicate=predicate))
    if now != expected:
        raise VersionConflict(f"the open set of {W.fact_ref(subject, predicate)} moved inside the transaction")
    return now


async def _plan_assert(registry: Any, args: Mapping[str, Any]) -> Plan:
    subject, predicate = _pair(str(args.get("subject") or ""), str(args.get("predicate") or ""))
    value = str(args.get("value") or "").strip()
    if not subject or not predicate or not value:
        raise Refused("preflight_failed", "subject, predicate and value must each be non-empty.")
    raw_conf = args.get("confidence")
    try:
        confidence = 1.0 if raw_conf is None else float(raw_conf)
    except (TypeError, ValueError) as exc:
        raise Refused("preflight_failed", f"confidence {raw_conf!r} is not a number.") from exc
    if not 0.0 <= confidence <= 1.0:
        raise Refused("preflight_failed", f"confidence {confidence} is outside 0..1.")
    valid_from = _valid_from(args.get("valid_from"))
    mode, ids = _supersede(args.get("supersede"))
    cites = [str(c) for c in (args.get("cites") or ()) if str(c).strip()]
    reason = str(args.get("reason") or "")[: W.LIMITS["reason_max_chars"]]
    pair_ref = W.fact_ref(subject, predicate)

    async with registry.pg.acquire() as conn:
        open_rows = await S.read_open_fact_set(conn, subject=subject, predicate=predicate)
    pre_digest = S.open_set_digest(r["id"] for r in open_rows)
    staged = args.get("expected_open")
    if staged is not None and staged != pre_digest:
        raise Refused(
            "stale_read",
            f"the open set of {pair_ref} changed since this change was staged "
            f"({len(open_rows)} open now); inspect it again.",
        )
    same = [r for r in open_rows if str(r["value"]).strip().lower() == value.lower()]
    held = [r for r in same if _st(r) in AUTHORITATIVE]
    if held:
        raise Refused(
            "no_change",
            f"{pair_ref} = {value!r} already holds as fact {held[0]['id']} ({held[0]['source_type']}); "
            "nothing was written.",
        )
    closes: dict[str, Any] = {str(r["id"]): r for r in same}  # a same-value machine row is replaced
    by_id = {str(r["id"]): r for r in open_rows}
    if mode == "differing":
        for r in open_rows:
            if str(r["value"]).strip().lower() != value.lower() and _st(r) not in CURATED:
                closes.setdefault(str(r["id"]), r)
    elif mode == "ids":
        for fid in ids:
            if fid not in by_id:
                raise Refused(
                    "stale_read",
                    f"fact {fid} is not an open row of {pair_ref} (closed since it was read, "
                    "or another pair's); inspect it again.",
                )
            closes.setdefault(fid, by_id[fid])
    close_ids = list(closes)
    raw_new = args.get("new_fact_id")
    new_id = UUID(str(raw_new)) if _is_uuid(raw_new) else uuid4()
    post_ids = [i for i in by_id if i not in closes] + [str(new_id)]
    post_digest = S.open_set_digest(post_ids)
    lineage = [UUID(c) for c in cites if _is_uuid(c)]
    canon = {
        "subject": subject, "predicate": predicate, "value": value,
        "valid_from": valid_from.isoformat() if valid_from else None,
        "confidence": confidence, "supersede": ids if mode == "ids" else mode,
        "cites": cites, "reason": reason, "new_fact_id": str(new_id), "expected_open": pre_digest,
    }

    async def mutate_assert(*, actor: str, in_tx: Any) -> Any:
        write_id = actor.split(":", 1)[1]
        async with registry.pg.acquire() as conn:
            async with conn.transaction():
                now = await _locked_digest(conn, subject, predicate, pre_digest)
                closed = await S.close_facts_by_id(
                    conn, ids=[UUID(i) for i in close_ids], superseded_by=new_id, closed_by=actor,
                )
                if sorted(str(c) for c in closed) != sorted(close_ids):
                    raise VersionConflict(f"the rows to close on {pair_ref} moved inside the transaction")
                await insert_operator_fact(
                    conn, row_id=new_id, subject=subject, predicate=predicate, value=value,
                    confidence=confidence, valid_from=valid_from,
                    data={"write_id": write_id, "asserted_by": actor, "reason": reason, "cites": cites},
                    derived_from=lineage, families=await _families(conn, predicate),
                )
                await _record(registry, conn, in_tx, actor=actor, pair_ref=pair_ref, action="assert",
                              from_version=now, to_version=post_digest)

    shown = "; ".join(f"{i[:8]} {closes[i]['value']!r} ({closes[i]['source_type']})" for i in close_ids)
    return Plan(
        cls="fact", verb="assert", object_ref=pair_ref, args=canon,
        tier=W.fact_tier(len(close_ids)),
        pre={"version": pre_digest, "state": f"{len(open_rows)} open", "subject": subject,
             "predicate": predicate, "open": [_brief(r) for r in open_rows], "closes": close_ids},
        post_state=f"{len(post_ids)} open",
        reversal={"kind": "close_and_reopen", "fact_id": str(new_id), "reopen": close_ids},
        mutate=mutate_assert, expected_version=pre_digest,
        card=(
            f"Assert fact {subject} · {predicate} = {value!r} (operator, confidence {confidence:.2f})"
            + (f" · closes {len(close_ids)}: {shown}" if close_ids else " · closes nothing")
        ),
        extra={"fact_id": str(new_id)},
    )


async def _plan_close(registry: Any, args: Mapping[str, Any]) -> Plan:
    raw = args.get("fact_id")
    if not _is_uuid(raw):
        raise Refused("ref_not_read", f"{raw!r} is not a fact id inspect_object returned.")
    fid = UUID(str(raw))
    reason = str(args.get("reason") or "")[: W.LIMITS["reason_max_chars"]]
    async with registry.pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, subject, predicate, value, source_type, valid_until, superseded_by, "
            "origin_class, data IS NULL AS data_null FROM facts WHERE id = $1",
            fid,
        )
        if row is None:
            raise Refused("ref_not_read", f"fact {fid} does not exist.")
        subject, predicate = str(row["subject"]), str(row["predicate"])
        open_rows = await S.read_open_fact_set(conn, subject=subject, predicate=predicate)
    if row["origin_class"] not in ("live", "web_retrieval", "seed"):
        raise Refused("protected_field", f"fact {fid} is imported history ({row['origin_class']}); it cannot be closed.")
    if row["valid_until"] is not None or row["superseded_by"] is not None:
        raise Refused("stale_read", f"fact {fid} is already closed; inspect the pair again.")
    pair_ref = W.fact_ref(subject, predicate)
    pre_digest = S.open_set_digest(r["id"] for r in open_rows)
    post_digest = S.open_set_digest(r["id"] for r in open_rows if r["id"] != fid)

    async def mutate_close(*, actor: str, in_tx: Any) -> Any:
        async with registry.pg.acquire() as conn:
            async with conn.transaction():
                now = await _locked_digest(conn, subject, predicate, pre_digest)
                closed = await S.close_facts_by_id(
                    conn, ids=[fid], superseded_by=None, closed_by=actor, reason=reason or None,
                )
                if closed != [fid]:
                    raise VersionConflict(f"fact {fid} moved inside the transaction")
                await _record(registry, conn, in_tx, actor=actor, pair_ref=pair_ref, action="close",
                              from_version=now, to_version=post_digest)

    return Plan(
        cls="fact", verb="close", object_ref=pair_ref,
        args={"fact_id": str(fid), "reason": reason},
        tier=W.fact_tier(1),
        pre={"version": pre_digest, "state": f"{len(open_rows)} open", "subject": subject,
             "predicate": predicate, "fact": _brief(row), "closes": [str(fid)]},
        post_state=f"{len(open_rows) - 1} open",
        reversal={"kind": "reopen", "fact_id": str(fid), "data_null": bool(row["data_null"])},
        mutate=mutate_close, expected_version=pre_digest,
        card=f"Close fact {str(fid)[:8]}: {subject} · {predicate} = {row['value']!r} ({row['source_type']})",
    )


# ---------------------------------------------------------------------------
# revert — the stored handle, never a fresh model guess
# ---------------------------------------------------------------------------


async def plan_revert(registry: Any, original: Mapping[str, Any], newer: str | None) -> Plan:
    """The inverse of ``original`` (an executed fact write row) as a Plan."""
    reversal = dict(original.get("reversal") or {})
    pre = dict(original.get("pre_image") or {})
    post = dict(original.get("post_image") or {})
    kind = reversal.get("kind")
    subject, predicate = str(pre.get("subject") or ""), str(pre.get("predicate") or "")
    if kind not in ("close_and_reopen", "reopen") or not subject or not predicate:
        raise Refused("preflight_failed", f"write {original['id']} carries no fact reversal.")
    pair_ref = W.fact_ref(subject, predicate)
    fact_id = UUID(str(reversal["fact_id"]))
    later = f"; the newer change is write {newer}" if newer else ""
    async with registry.pg.acquire() as conn:
        open_rows = await S.read_open_fact_set(conn, subject=subject, predicate=predicate)
        reopen = [UUID(str(i)) for i in (reversal.get("reopen") or ())]
        rows = await conn.fetch(
            "SELECT id, value, valid_until, superseded_by, data->>'closed_by' AS closed_by "
            "FROM facts WHERE id = ANY($1::uuid[])",
            [fact_id, *reopen],
        )
    now = S.open_set_digest(r["id"] for r in open_rows)
    state = {str(r["id"]): r for r in rows}
    canon = {"write_id": str(original["id"]), "class": "fact", "id": pair_ref}
    no_undo = {"kind": "none", "why": "a revert is undone by asking for the change again"}
    if kind == "close_and_reopen":
        if now != post.get("version"):
            raise Refused(
                "superseded_since",
                f"the open set of {pair_ref} moved since write {original['id']} "
                f"({len(open_rows)} open now){later}. Nothing was reverted.",
            )
        moved = [str(i) for i in reopen if (state.get(str(i)) or {}).get("superseded_by") != fact_id]
        if moved:
            raise Refused(
                "superseded_since",
                f"fact {moved[0]} is no longer closed by this write's fact {fact_id}{later}. Nothing was reverted.",
            )
        target = str(pre.get("version"))

        async def mutate_unassert(*, actor: str, in_tx: Any) -> Any:
            async with registry.pg.acquire() as conn:
                async with conn.transaction():
                    start = await _locked_digest(conn, subject, predicate, now)
                    closed = await S.close_facts_by_id(
                        conn, ids=[fact_id], superseded_by=None, closed_by=actor,
                        reason=f"revert of consult write {original['id']}",
                    )
                    reopened = await S.reopen_facts_by_id(conn, ids=reopen, superseded_by=fact_id)
                    after = S.open_set_digest(
                        r["id"] for r in await S.read_open_fact_set(conn, subject=subject, predicate=predicate))
                    if closed != [fact_id] or sorted(reopened) != sorted(reopen) or after != target:
                        raise VersionConflict(f"the open set of {pair_ref} did not return to its pre-image")
                    await _record(registry, conn, in_tx, actor=actor, pair_ref=pair_ref, action="revert",
                                  from_version=start, to_version=after)

        return Plan(
            cls="fact", verb="revert", object_ref=pair_ref, args=canon, tier="A",
            pre={"version": now, "state": f"{len(open_rows)} open", "subject": subject, "predicate": predicate},
            post_state=str(pre.get("state") or ""), reversal=no_undo, mutate=mutate_unassert,
            expected_version=now,
            card=f"Revert: close fact {str(fact_id)[:8]} on {pair_ref} and reopen {len(reopen)}",
        )
    fact = state.get(str(fact_id))
    marker = f"consult_write:{original['id']}"
    if fact is None or fact["valid_until"] is None or fact["superseded_by"] is not None or fact["closed_by"] != marker:
        raise Refused(
            "superseded_since",
            f"fact {fact_id} is no longer closed by write {original['id']}{later}. Nothing was reverted.",
        )
    twin = [r for r in open_rows if str(r["value"]).strip().lower() == str(fact["value"]).strip().lower()]
    if twin:
        raise Refused(
            "superseded_since",
            f"fact {twin[0]['id']} now holds the same value open on {pair_ref}{later}. Nothing was reverted.",
        )
    post_digest = S.open_set_digest([*(r["id"] for r in open_rows), fact_id])
    null_data = [fact_id] if reversal.get("data_null") else []

    async def mutate_reopen(*, actor: str, in_tx: Any) -> Any:
        async with registry.pg.acquire() as conn:
            async with conn.transaction():
                start = await _locked_digest(conn, subject, predicate, now)
                reopened = await S.reopen_facts_by_id(
                    conn, ids=[fact_id], superseded_by=None, closed_by=marker, null_data_ids=null_data,
                )
                if reopened != [fact_id]:
                    raise VersionConflict(f"fact {fact_id} moved inside the transaction")
                await _record(registry, conn, in_tx, actor=actor, pair_ref=pair_ref, action="revert",
                              from_version=start, to_version=post_digest)

    return Plan(
        cls="fact", verb="revert", object_ref=pair_ref, args=canon, tier="A",
        pre={"version": now, "state": f"{len(open_rows)} open", "subject": subject, "predicate": predicate},
        post_state=f"{len(open_rows) + 1} open", reversal=no_undo, mutate=mutate_reopen,
        expected_version=now, card=f"Revert: reopen fact {str(fact_id)[:8]} on {pair_ref}",
    )


# ---------------------------------------------------------------------------
# activation — a record has no runtime head
# ---------------------------------------------------------------------------


async def activation(
    registry: Any, object_ref: str, version: str, since: datetime, *, parse_check: Any,
) -> dict[str, Any] | None:
    del registry, object_ref, version, since, parse_check
    return {
        "status": "not_applicable",
        "why": "a fact has no runtime head to activate; the next read sees it",
        "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
