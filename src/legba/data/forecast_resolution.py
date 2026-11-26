# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The resolution record and the resolution job (migration 0244).

Every act on a question's outcome is one APPEND-ONLY row of
``forecast_resolution_records``:

  * ``proposal``     — the resolver job's reading at an as-of
    (:mod:`legba.data.forecast_resolvers`): yes | no | void | undetermined |
    needs_judgement, with its evidence, clauses, judgements, searched scope and
    void rule. Idempotent on ``fingerprint``: the same reading on the same
    evidence is one row however often the job runs; a changed reading is a new
    row that ``supersedes_id`` the last.
  * ``confirmation`` — a proposal's yes | no | void accepted: by the rule
    itself for the machine-resolvable kinds (``series_threshold``), by the
    operator for every other kind. Writes the ``forecast_resolutions`` row
    (through :func:`legba.data.forecast_ledger.resolve`, which sets the
    question's ``status`` — the one column the seal lets change).
  * ``override``     — the operator decides what the rule cannot (a
    ``needs_judgement`` proposal) or decides against it: an ``override_log``
    row with the reason, then the resolution.
  * ``correction``   — an earlier confirmation/override replaced: a new
    resolution (the old retires; the 0242 store writes its ``override_log``
    row) and a record that ``supersedes_id`` the act it corrects.
  * ``rejection``    — (F-1, migration 0262) a person rejects a proposal with
    a reason. Nothing closes: the question stays open, the resolver keeps
    reading it, and a NEW reading is a new proposal; the rejected reading can
    no longer be confirmed.

The same functions serve the host script (``scripts/resolve_forecasts.py``)
and the console's write routes (``registry/forecast_resolution_api.py``).
A route's act also records the acting product role (``actor_role``: owner /
admin / contributor, migration 0262); the host script records none.

No model decides an outcome: a proposal is a rule's reading, and every
non-machine kind closes only on a person's confirmation or override.

LEAF: stdlib + the leaf ledger/resolver/evidence modules (the slim scorecard
route imports this).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from . import forecast_ledger as fl
from .forecast_evidence import collect_evidence
from .forecast_resolvers import (
    RESOLVER_VERSION,
    Proposal,
    ResolverContext,
    ResolverKindNotBuilt,
    evaluate,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ACTOR_ROLES",
    "ACT_KINDS",
    "RECORD_LEAVES_SQL",
    "RESOLUTION_JOB_VERSION",
    "RESOLVER_ACTOR",
    "confirm_proposal",
    "correct_resolution",
    "list_due_resolvers",
    "override_resolution",
    "acts_on_basis",
    "corrections_of",
    "entered_number_questions",
    "is_person_actor",
    "proposal_fingerprint",
    "record_leaf_rows",
    "reject_proposal",
    "resolution_queue",
    "run_resolution_job",
    "write_proposal",
]

RESOLUTION_JOB_VERSION = "2026-11/st1"
RESOLVER_ACTOR = f"resolver:forecast_resolver@{RESOLVER_VERSION}"
ACT_KINDS: tuple[str, ...] = ("confirmation", "override", "correction")
CLOSING_OUTCOMES: tuple[str, ...] = ("yes", "no", "void")
#: The product roles an act records (migration 0262 ``actor_role``).
ACTOR_ROLES: tuple[str, ...] = ("owner", "admin", "contributor")
#: The actor shapes of a PERSON: the host script's ``operator:<id>`` and the
#: switch-off bearer ``operator:bearer:<sha12>``, a signed-in ``user:<uuid>``,
#: and the switch-on break-glass ``admin-token:bearer:<sha12>``. Never a
#: service (``service:…``) and never the resolver.
PERSON_ACTOR_PREFIXES: tuple[str, ...] = ("operator:", "user:", "admin-token:")


def is_person_actor(actor: str) -> bool:
    a = (actor or "").strip()
    return any(a.startswith(p) and len(a) > len(p) for p in PERSON_ACTOR_PREFIXES)

AlertFn = Callable[[str, str, str], Awaitable[Any]]

_REC_COLS = (
    "question_id, record_kind, outcome, resolver_kind, resolver_version, as_of, actor, "
    "evidence, clauses, judgements, searched, void_rule, basis_record_id, supersedes_id, "
    "resolution_id, override_log_id, reason, fingerprint, content_sha256"
)
INSERT_RECORD_SQL = f"""
INSERT INTO forecast_resolution_records ({_REC_COLS})
VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10::jsonb, $11::jsonb,
        $12::jsonb, $13::uuid, $14::uuid, $15::uuid, $16::uuid, $17, $18, $19)
ON CONFLICT (question_id, fingerprint) DO NOTHING
RETURNING id::text AS id
"""
#: The same insert with the acting role (migration 0262) — used only when an
#: act names one, so the job and the host script write on a pre-0262 table.
INSERT_RECORD_WITH_ROLE_SQL = f"""
INSERT INTO forecast_resolution_records ({_REC_COLS}, actor_role)
VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10::jsonb, $11::jsonb,
        $12::jsonb, $13::uuid, $14::uuid, $15::uuid, $16::uuid, $17, $18, $19, $20)
ON CONFLICT (question_id, fingerprint) DO NOTHING
RETURNING id::text AS id
"""
#: Every act that names a proposal as its basis (confirmation, override,
#: rejection), oldest first.
ACTS_ON_BASIS_SQL = """
SELECT id::text AS id, record_kind, outcome, actor, recorded_at
  FROM forecast_resolution_records
 WHERE basis_record_id = $1::uuid AND record_kind <> 'proposal'
 ORDER BY recorded_at, id
"""
#: The corrections that supersede one act.
CORRECTIONS_OF_SQL = """
SELECT id::text AS id, outcome, recorded_at
  FROM forecast_resolution_records
 WHERE supersedes_id = $1::uuid AND record_kind = 'correction'
 ORDER BY recorded_at, id
"""
#: The questions (of $1) on which one of the identities $2 entered a number:
#: the number's issuer_ref, or the person its detail says entered it.
ENTERED_NUMBER_SQL = """
SELECT DISTINCT question_id::text AS question_id
  FROM forecast_numbers
 WHERE question_id = ANY($1::uuid[])
   AND (issuer_ref = ANY($2::text[]) OR (detail ->> 'entered_by') = ANY($2::text[]))
"""
#: The queue: sealed open questions whose newest proposal no act answers yet.
QUEUE_SQL = """
SELECT q.id::text AS id, q.slug, q.question, q.resolver_kind,
       q.resolver_spec::text AS resolver_spec, q.window_start, q.window_end, q.due_at,
       q.grace_hours, q.condition, q.void_rules, q.content_sha256,
       p.id::text AS proposal_id, p.outcome, p.as_of, p.recorded_at,
       p.evidence::text AS evidence, p.clauses::text AS clauses,
       p.judgements::text AS judgements, p.void_rule::text AS void_rule
  FROM forecast_questions q
  JOIN LATERAL (
        SELECT r.id, r.outcome, r.as_of, r.recorded_at, r.evidence, r.clauses, r.judgements,
               r.void_rule
          FROM forecast_resolution_records r
         WHERE r.question_id = q.id AND r.record_kind = 'proposal'
         ORDER BY r.recorded_at DESC, r.id DESC
         LIMIT 1) p ON TRUE
 WHERE q.sealed_at IS NOT NULL AND q.status = 'open'
   AND NOT EXISTS (SELECT 1 FROM forecast_resolution_records a
                    WHERE a.question_id = q.id AND a.record_kind <> 'proposal'
                      AND a.recorded_at >= p.recorded_at)
 ORDER BY q.due_at, q.slug
 LIMIT $1
"""
LATEST_RECORD_SQL = """
SELECT id::text AS id, question_id::text AS question_id, record_kind, outcome, resolver_kind,
       resolver_version, as_of, recorded_at, actor, evidence::text AS evidence,
       judgements::text AS judgements, fingerprint, content_sha256
  FROM forecast_resolution_records
 WHERE question_id = $1::uuid AND record_kind = ANY($2::text[])
 ORDER BY recorded_at DESC, id DESC
 LIMIT 1
"""
RECORD_BY_ID_SQL = """
SELECT id::text AS id, question_id::text AS question_id, record_kind, outcome, resolver_kind,
       resolver_version, as_of, recorded_at, actor, evidence::text AS evidence,
       judgements::text AS judgements, fingerprint, content_sha256
  FROM forecast_resolution_records WHERE id = $1::uuid
"""
LATEST_RECORDS_FOR_SQL = """
SELECT DISTINCT ON (r.question_id, r.record_kind = 'proposal')
       r.id::text AS id, r.question_id::text AS question_id, r.record_kind, r.outcome,
       r.recorded_at, r.actor, r.judgements::text AS judgements, r.evidence::text AS evidence,
       (to_jsonb(r) ->> 'actor_role') AS actor_role
  FROM forecast_resolution_records r
 WHERE r.question_id = ANY($1::uuid[])
 ORDER BY r.question_id, r.record_kind = 'proposal', r.recorded_at DESC, r.id DESC
"""
INSERT_OVERRIDE_RETURNING_SQL = """
INSERT INTO override_log (actor, object_kind, object_id, action, before_sha256,
                          after_sha256, reason)
VALUES ($1, $2, $3::uuid, $4, $5, $6, $7)
RETURNING id::text AS id
"""
RESOLUTION_SHA_SQL = "SELECT content_sha256 FROM forecast_resolutions WHERE id = $1::uuid"
JOB_QUESTIONS_SQL = """
SELECT id::text AS id, slug, question, question_class, resolver_kind,
       resolver_spec::text AS resolver_spec, window_start, window_end, due_at, grace_hours,
       condition, void_rules, exogenous, subject_keys, status, sealed_at, created_at,
       content_sha256
  FROM forecast_questions
 WHERE sealed_at IS NOT NULL AND status = 'open'
 ORDER BY due_at, slug
"""
#: The resolution records' anchor leaves (``receipt_anchor`` folds them after
#: the 0242 ledger leaves), in BYTE order like :data:`fl.LEDGER_LEAVES_SQL`.
RECORD_LEAVES_SQL = """
SELECT 'forecast_resolution_record' AS kind, id::text AS id, content_sha256
  FROM forecast_resolution_records
 ORDER BY id::text COLLATE "C"
"""


def _get(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _now(now: datetime | None) -> datetime:
    return now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)


def _loads(raw: Any, default: Any) -> Any:
    val = fl._json_field(raw)
    return default if val is None or isinstance(val, str) else val


def proposal_fingerprint(question_sha256: str, p: Proposal) -> str:
    """The identity of a READING: same question words, rule set, outcome,
    evidence, clause states, judgements and void reading ⇒ same fingerprint.
    The as-of and the scan counts are not part of it."""
    return fl.sha256_hex({
        "question_sha256": question_sha256,
        "resolver_version": p.resolver_version,
        "outcome": p.outcome,
        "evidence": sorted({str(e.get("ref")) for e in p.evidence}),
        "clauses": sorted(f"{c.get('id')}={c.get('status')}" for c in p.clauses),
        "judgements": sorted(str(j.get("key")) for j in p.judgements),
        "void_rule": None if not p.void_rule else {
            "index": p.void_rule.get("index"), "applies": p.void_rule.get("applies")},
    })


def _record_args(question_id: str, question_sha: str, *, record_kind: str, outcome: str,
                 resolver_kind: str, resolver_version: str, as_of: datetime, actor: str,
                 evidence: Any = (), clauses: Any = (), judgements: Any = (), searched: Any = None,
                 void_rule: Any = None, basis_record_id: str | None = None,
                 supersedes_id: str | None = None, resolution_id: str | None = None,
                 override_log_id: str | None = None, reason: str | None = None,
                 fingerprint: str, actor_role: str | None = None) -> list[Any]:
    """The insert's arguments (19, or 20 with ``actor_role`` — then
    :func:`_insert_record` uses :data:`INSERT_RECORD_WITH_ROLE_SQL`). The role
    enters the content hash only when it is recorded, so the hash recipe of
    every record without one is unchanged."""
    content = {
        "question_sha256": question_sha, "record_kind": record_kind, "outcome": outcome,
        "resolver_kind": resolver_kind, "resolver_version": resolver_version,
        "as_of": fl._iso(as_of), "actor": actor, "evidence": list(evidence),
        "clauses": list(clauses), "judgements": list(judgements), "searched": searched or {},
        "void_rule": void_rule, "basis_record_id": basis_record_id,
        "supersedes_id": supersedes_id, "resolution_id": resolution_id,
        "override_log_id": override_log_id, "reason": reason, "fingerprint": fingerprint,
    }
    if actor_role is not None:
        if actor_role not in ACTOR_ROLES:
            raise fl.LedgerRefusal("invalid", f"actor_role {actor_role!r} is not one of {ACTOR_ROLES}")
        content["actor_role"] = actor_role
    args = [
        question_id, record_kind, outcome, resolver_kind, resolver_version, as_of, actor,
        fl.canonical_json(list(evidence)), fl.canonical_json(list(clauses)),
        fl.canonical_json(list(judgements)), fl.canonical_json(searched or {}),
        None if void_rule is None else fl.canonical_json(void_rule),
        basis_record_id, supersedes_id, resolution_id, override_log_id, reason,
        fingerprint, fl.sha256_hex(content),
    ]
    return args + [actor_role] if actor_role is not None else args


async def _insert_record(conn: Any, args: list[Any]) -> Any:
    sql = INSERT_RECORD_WITH_ROLE_SQL if len(args) == 20 else INSERT_RECORD_SQL
    return await conn.fetchrow(sql, *args)


async def acts_on_basis(conn: Any, record_id: str) -> list[Mapping[str, Any]]:
    """Every act (confirmation / override / rejection) naming ``record_id``."""
    return list(await conn.fetch(ACTS_ON_BASIS_SQL, str(record_id)))


async def corrections_of(conn: Any, record_id: str) -> list[Mapping[str, Any]]:
    """Every correction superseding the act ``record_id``."""
    return list(await conn.fetch(CORRECTIONS_OF_SQL, str(record_id)))


async def entered_number_questions(conn: Any, question_ids: Sequence[str],
                                   identities: Sequence[str]) -> set[str]:
    """The questions of ``question_ids`` on which one of ``identities``
    entered a number (the contributor rule: a person does not confirm the
    outcome of a question they hold a number on)."""
    ids = [str(i) for i in identities if str(i or "").strip()]
    if not question_ids or not ids:
        return set()
    rows = await conn.fetch(ENTERED_NUMBER_SQL, [str(q) for q in question_ids], ids)
    return {str(r["question_id"]) for r in rows}


async def write_proposal(conn: Any, q: Mapping[str, Any], p: Proposal) -> tuple[str, str | None]:
    """``(status, record_id)`` — ``written`` (a new reading), or ``unchanged``
    (this reading is already on record: nothing written)."""
    qid, qsha = str(q["id"]), str(q["content_sha256"])
    fp = proposal_fingerprint(qsha, p)
    last = await conn.fetchrow(LATEST_RECORD_SQL, qid, ["proposal"])
    if last is not None and last["fingerprint"] == fp:
        return "unchanged", str(last["id"])
    args = _record_args(
        qid, qsha, record_kind="proposal", outcome=p.outcome, resolver_kind=p.resolver_kind,
        resolver_version=p.resolver_version, as_of=p.as_of, actor=RESOLVER_ACTOR,
        evidence=p.evidence, clauses=p.clauses, judgements=p.judgements,
        searched={**p.searched, "notes": p.notes, "lean": p.lean}, void_rule=p.void_rule,
        supersedes_id=None if last is None else str(last["id"]), fingerprint=fp,
    )
    row = await conn.fetchrow(INSERT_RECORD_SQL, *args)
    if row is None:  # an older identical reading (ON CONFLICT): still unchanged
        return "unchanged", None
    return "written", str(row["id"])


async def _question_row(conn: Any, question_id: str) -> Mapping[str, Any]:
    return await fl._question(conn, question_id)


def _refs(evidence: Iterable[Mapping[str, Any]]) -> list[str]:
    out: list[str] = []
    for e in evidence:
        ref = str(e.get("ref") or "")
        if ref and ref not in out:
            out.append(ref)
        if e.get("url") and f"url:{e['url']}" not in out:
            out.append(f"url:{e['url']}")
        # S-1: a series value's fetch receipt (the stored response it was parsed from).
        if e.get("fetch_version_id") is not None and f"fetch_version:{e['fetch_version_id']}" not in out:
            out.append(f"fetch_version:{e['fetch_version_id']}")
    return out


async def confirm_proposal(conn: Any, record_id: str, *, actor: str, note: str | None = None,
                           now: datetime | None = None, actor_role: str | None = None) -> str:
    """Accept a proposal's yes | no | void; returns the confirmation record id.

    Refuses a proposal that is not a closing outcome (``undetermined`` /
    ``needs_judgement`` go through :func:`override_resolution`), a proposal
    that is not the question's newest, a proposal a person rejected
    (``proposal_rejected``), and a rule confirming a kind that is not
    machine-resolvable (the operator confirms those)."""
    when = _now(now)
    rec = await conn.fetchrow(RECORD_BY_ID_SQL, str(record_id))
    if rec is None or rec["record_kind"] != "proposal":
        raise fl.LedgerRefusal("invalid", f"{record_id} is not a proposal")
    if rec["outcome"] not in CLOSING_OUTCOMES:
        raise fl.LedgerRefusal(
            "needs_judgement",
            f"proposal {record_id} is {rec['outcome']}: the operator decides through an override")
    newest = await conn.fetchrow(LATEST_RECORD_SQL, rec["question_id"], ["proposal"])
    if newest is not None and str(newest["id"]) != str(record_id):
        raise fl.LedgerRefusal("stale_proposal", f"{record_id} is superseded by {newest['id']}")
    if actor.startswith("resolver:") and rec["resolver_kind"] not in fl.MACHINE_RESOLVABLE:
        raise fl.LedgerRefusal("operator_required",
                               f"{rec['resolver_kind']} is proposed by the job and confirmed by the operator")
    if not actor.strip():
        raise fl.LedgerRefusal("invalid", "a confirmation names who confirmed it")
    if not actor.startswith("resolver:"):
        for act in await acts_on_basis(conn, str(record_id)):
            if act["record_kind"] == "rejection":
                raise fl.LedgerRefusal(
                    "proposal_rejected", f"proposal {record_id} was rejected ({act['id']}); it cannot be confirmed")
    q = await _question_row(conn, rec["question_id"])
    evidence = _loads(rec["evidence"], [])
    refs = _refs(evidence)
    early = when < fl._instant(q["due_at"], name="due_at")
    async with conn.transaction():
        res_id = await fl.resolve(
            conn, rec["question_id"], outcome=rec["outcome"], resolved_by=actor,
            evidence_refs=refs, note=note or f"confirmation of proposal record {record_id}",
            early=early, now=when,
        )
        fp = fl.sha256_hex({"act": "confirmation", "basis": str(record_id), "resolution": res_id})
        row = await _insert_record(conn, _record_args(
            rec["question_id"], str(q["content_sha256"]), record_kind="confirmation",
            outcome=rec["outcome"], resolver_kind=rec["resolver_kind"],
            resolver_version=rec["resolver_version"], as_of=when, actor=actor, evidence=evidence,
            basis_record_id=str(record_id), resolution_id=res_id, reason=note, fingerprint=fp,
            actor_role=actor_role,
        ))
    return str(row["id"])


async def reject_proposal(conn: Any, record_id: str, *, actor: str, reason: str,
                          now: datetime | None = None,
                          actor_role: str | None = None) -> tuple[str, bool]:
    """A person rejects a proposal: ``(rejection record id, newly written)``.

    Nothing closes and no resolution is written; the question stays open for
    the resolver. Refused without a reason, from anyone but a person, on a
    record that is not the question's newest proposal (``stale_proposal``), on
    a question no longer open (``already_resolved``) and on a proposal an act
    already answered (``already_decided``). Idempotent: rejecting the same
    proposal again returns the first rejection."""
    when = _now(now)
    if not (reason or "").strip():
        raise fl.LedgerRefusal("invalid", "a rejection carries a reason")
    if not is_person_actor(actor):
        raise fl.LedgerRefusal("operator_required", "a rejection is a person's (user:<id> or operator:<id>)")
    rec = await conn.fetchrow(RECORD_BY_ID_SQL, str(record_id))
    if rec is None or rec["record_kind"] != "proposal":
        raise fl.LedgerRefusal("invalid", f"{record_id} is not a proposal")
    acts = await acts_on_basis(conn, str(record_id))
    for act in acts:
        if act["record_kind"] == "rejection":
            return str(act["id"]), False
    if acts:
        raise fl.LedgerRefusal("already_decided", f"proposal {record_id} was answered by {acts[0]['id']}")
    newest = await conn.fetchrow(LATEST_RECORD_SQL, rec["question_id"], ["proposal"])
    if newest is not None and str(newest["id"]) != str(record_id):
        raise fl.LedgerRefusal("stale_proposal", f"{record_id} is superseded by {newest['id']}")
    q = await _question_row(conn, rec["question_id"])
    if q["status"] != "open":
        raise fl.LedgerRefusal("already_resolved", f"{q['slug']} is {q['status']}; nothing to reject")
    fp = fl.sha256_hex({"act": "rejection", "basis": str(record_id)})
    row = await _insert_record(conn, _record_args(
        rec["question_id"], str(q["content_sha256"]), record_kind="rejection",
        outcome=rec["outcome"], resolver_kind=rec["resolver_kind"],
        resolver_version=rec["resolver_version"], as_of=when, actor=actor,
        basis_record_id=str(record_id), reason=reason.strip(), fingerprint=fp,
        actor_role=actor_role,
    ))
    if row is None:  # a concurrent rejection of the same proposal won
        for act in await acts_on_basis(conn, str(record_id)):
            if act["record_kind"] == "rejection":
                return str(act["id"]), False
        raise fl.LedgerRefusal("already_decided", f"proposal {record_id} was answered meanwhile")
    return str(row["id"]), True


async def override_resolution(
    conn: Any, question_id: str, *, outcome: str, actor: str, reason: str,
    judgements: Sequence[str] = (), evidence_refs: Sequence[str] = (),
    basis_record_id: str | None = None, now: datetime | None = None,
    actor_role: str | None = None,
) -> str:
    """The operator decides: an ``override_log`` row (who, why), then the
    resolution, then the record. Returns the override record id.

    ``judgements`` names the proposal's judgement keys this decides; an
    override is refused without a reason, from a non-operator actor, or on a
    question already resolved (that is :func:`correct_resolution`)."""
    when = _now(now)
    if outcome not in CLOSING_OUTCOMES:
        raise fl.LedgerRefusal("invalid", f"outcome {outcome!r} is not one of {CLOSING_OUTCOMES}")
    if not (reason or "").strip():
        raise fl.LedgerRefusal("invalid", "an override carries a reason")
    if not is_person_actor(actor):
        raise fl.LedgerRefusal("operator_required", "an override is a person's (user:<id> or operator:<id>)")
    q = await _question_row(conn, question_id)
    if await conn.fetchrow(fl._ACTIVE_RESOLUTION, str(question_id)) is not None:
        raise fl.LedgerRefusal("already_resolved", f"{q['slug']} is resolved; correct it instead")
    basis = None
    if basis_record_id:
        basis = await conn.fetchrow(RECORD_BY_ID_SQL, str(basis_record_id))
        if basis is None or str(basis["question_id"]) != str(question_id):
            raise fl.LedgerRefusal("invalid", f"{basis_record_id} is not a record of this question")
    refs = [str(r) for r in evidence_refs if str(r).strip()]
    if basis is not None and not refs:
        refs = _refs(_loads(basis["evidence"], []))
    early = when < fl._instant(q["due_at"], name="due_at")
    decided = ", ".join(judgements) or "the outcome"
    async with conn.transaction():
        res_id = await fl.resolve(
            conn, str(question_id), outcome=outcome, resolved_by=actor, evidence_refs=refs,
            note=f"override ({decided}): {reason}", early=early, now=when,
        )
        res_sha = await conn.fetchval(RESOLUTION_SHA_SQL, res_id)
        log = await conn.fetchrow(
            INSERT_OVERRIDE_RETURNING_SQL, actor, "forecast_question", str(question_id),
            "resolve_override", None if basis is None else basis["content_sha256"], res_sha,
            f"[{decided}] {reason}",
        )
        fp = fl.sha256_hex({"act": "override", "question": str(question_id), "resolution": res_id})
        row = await _insert_record(conn, _record_args(
            str(question_id), str(q["content_sha256"]), record_kind="override", outcome=outcome,
            resolver_kind=str(q["resolver_kind"]), resolver_version=RESOLVER_VERSION, as_of=when,
            actor=actor, evidence=[{"ref": r} for r in refs],
            judgements=[{"key": k, "decided": outcome} for k in judgements],
            basis_record_id=None if basis is None else str(basis["id"]),
            resolution_id=res_id, override_log_id=str(log["id"]), reason=reason, fingerprint=fp,
            actor_role=actor_role,
        ))
    return str(row["id"])


async def correct_resolution(
    conn: Any, question_id: str, *, outcome: str, actor: str, reason: str,
    evidence_refs: Sequence[str] = (), now: datetime | None = None,
    actor_role: str | None = None, expected_prior_id: str | None = None,
) -> str:
    """Replace the active resolution: never an edit — a new resolution (the
    old retires, the 0242 store logs the override) and a ``correction`` record
    superseding the act it corrects. ``expected_prior_id`` (the routes) is
    the act the caller read: another act on record since is ``stale_act``."""
    when = _now(now)
    if not (reason or "").strip():
        raise fl.LedgerRefusal("invalid", "a correction carries a reason")
    if not is_person_actor(actor):
        raise fl.LedgerRefusal("operator_required", "a correction is a person's (user:<id> or operator:<id>)")
    q = await _question_row(conn, question_id)
    prior = await conn.fetchrow(LATEST_RECORD_SQL, str(question_id), list(ACT_KINDS))
    if prior is None:
        raise fl.LedgerRefusal("unknown_question", f"{q['slug']} has no confirmation or override to correct")
    if expected_prior_id is not None and str(prior["id"]) != str(expected_prior_id):
        raise fl.LedgerRefusal("stale_act", f"the act on record is {prior['id']}, not {expected_prior_id}")
    refs = [str(r) for r in evidence_refs if str(r).strip()]
    early = when < fl._instant(q["due_at"], name="due_at")
    async with conn.transaction():
        res_id = await fl.resolve(
            conn, str(question_id), outcome=outcome, resolved_by=actor, evidence_refs=refs,
            note=f"correction: {reason}", early=early and bool(refs), correction_reason=reason, now=when,
        )
        fp = fl.sha256_hex({"act": "correction", "supersedes": str(prior["id"]), "resolution": res_id})
        row = await _insert_record(conn, _record_args(
            str(question_id), str(q["content_sha256"]), record_kind="correction", outcome=outcome,
            resolver_kind=str(q["resolver_kind"]), resolver_version=RESOLVER_VERSION, as_of=when,
            actor=actor, evidence=[{"ref": r} for r in refs], supersedes_id=str(prior["id"]),
            resolution_id=res_id, reason=reason, fingerprint=fp, actor_role=actor_role,
        ))
    return str(row["id"])


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


def _question_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    d = dict(row)
    d["resolver_spec"] = fl._json_field(d.get("resolver_spec")) or {}
    return d


def _summary(p: Proposal) -> dict[str, Any]:
    return {
        "outcome": p.outcome, "lean": p.lean,
        "evidence": [
            {k: e.get(k) for k in ("ref", "publisher", "source_class", "published_at", "fetched_at",
                                   "published_vs_window", "clause", "part", "span", "url", "value",
                                   "valid_from", "record_time", "source_id", "fetch_sha256",
                                   "fetch_version_id", "basis")}
            for e in p.evidence
        ],
        "clauses": [{k: c.get(k) for k in ("id", "status", "text", "independent_publishers", "negated")}
                    for c in p.clauses],
        "judgements": p.judgements, "void_rule": p.void_rule, "notes": p.notes,
        "searched": p.searched,
    }


async def run_resolution_job(
    conn: Any,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    as_if_due: bool = False,
    slugs: Sequence[str] | None = None,
    auto_settle: bool = True,
    alert: AlertFn | None = None,
) -> dict[str, Any]:
    """Find the due questions, run their resolvers, write the proposals,
    settle the machine-resolvable ones, alert. Idempotent: a re-run on the
    same record writes nothing and raises no alert.

    ``dry_run`` writes nothing (and alerts nothing); ``as_if_due`` reads every
    sealed open question as though it were due and its grace spent (the
    rehearsal)."""
    when = _now(now)
    ctx = ResolverContext(as_of=when, as_if_due=as_if_due)
    report: dict[str, Any] = {
        "job_version": RESOLUTION_JOB_VERSION, "resolver_version": RESOLVER_VERSION,
        "as_of": when.isoformat(), "dry_run": dry_run, "as_if_due": as_if_due,
        "questions": [], "written": 0, "unchanged": 0, "settled": 0, "alerts": 0, "errors": 0,
    }
    rows = [_question_dict(r) for r in await conn.fetch(JOB_QUESTIONS_SQL)]
    for q in rows:
        if slugs and q["slug"] not in slugs:
            continue
        due = fl._instant(q["due_at"], name="due_at")
        if not as_if_due and when < due:
            continue
        entry: dict[str, Any] = {"slug": q["slug"], "resolver_kind": q["resolver_kind"],
                                 "due_at": due.isoformat()}
        report["questions"].append(entry)
        try:
            found = await collect_evidence(conn, q, as_of=when, ctx=ctx)
            p = evaluate(q, found["rows_by_part"], ctx, observations=found["observations"],
                         searched_by_part=found["searched_by_part"])
        except ResolverKindNotBuilt as exc:
            report["errors"] += 1
            entry.update(outcome=None, error=str(exc))
            logger.error("forecast_resolver.kind_not_built slug=%s kind=%s", q["slug"], exc.kind)
            if alert is not None and not dry_run:
                await alert(f"Forecast {q['slug']}: resolver not built", str(exc), "high")
                report["alerts"] += 1
            continue
        entry.update(_summary(p))
        if dry_run:
            entry["record"] = "dry_run"
            continue
        status, rec_id = await write_proposal(conn, q, p)
        entry.update(record=status, record_id=rec_id)
        report[status] += 1
        if status != "written":
            continue
        if (auto_settle and p.outcome in ("yes", "no") and q["resolver_kind"] in fl.MACHINE_RESOLVABLE
                and not as_if_due):
            entry["confirmation_id"] = await confirm_proposal(conn, rec_id, actor=RESOLVER_ACTOR, now=when)
            report["settled"] += 1
            entry["settled"] = True
        if alert is not None:
            await alert(*_alert_text(q, p, rec_id, settled=bool(entry.get("settled"))))
            report["alerts"] += 1
    return report


def _alert_text(q: Mapping[str, Any], p: Proposal, rec_id: str | None, *, settled: bool) -> tuple[str, str, str]:
    if p.outcome == "needs_judgement":
        head = f"Forecast {q['slug']}: needs your judgement"
        sev = "high"
    elif settled:
        head = f"Forecast {q['slug']}: resolved {p.outcome} by rule"
        sev = "medium"
    else:
        head = f"Forecast {q['slug']}: proposal {p.outcome}"
        sev = "medium" if p.outcome in CLOSING_OUTCOMES else "low"
    lines = [f"record {rec_id}; evidence rows {len(p.evidence)}"]
    lines += [f"- judgement {j['key']}: {j['question']}" for j in p.judgements]
    if p.void_rule:
        lines.append(f"void rule: {p.void_rule.get('text')} (applies={p.void_rule.get('applies')})")
    if not settled and p.outcome in CLOSING_OUTCOMES:
        lines.append(f"confirm: scripts/resolve_forecasts.py confirm --record {rec_id} --actor operator:<you>")
    if p.outcome == "needs_judgement":
        lines.append(f"decide: scripts/resolve_forecasts.py override --slug {q['slug']} --outcome yes|no|void "
                     f"--actor operator:<you> --reason '...' --basis {rec_id}")
    return head, "\n".join(lines), sev


# ---------------------------------------------------------------------------
# Reads — the consult tool and the scorecard
# ---------------------------------------------------------------------------


async def latest_records(conn: Any, question_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Per question: the newest proposal and the newest act (``{}`` when the
    0244 table cannot be read — the scorecard then shows no proposals)."""
    out: dict[str, dict[str, Any]] = {}
    if not question_ids:
        return out
    for r in await conn.fetch(LATEST_RECORDS_FOR_SQL, list(question_ids)):
        slot = "proposal" if r["record_kind"] == "proposal" else "act"
        out.setdefault(str(r["question_id"]), {})[slot] = {
            "record_id": r["id"], "record_kind": r["record_kind"], "outcome": r["outcome"],
            "recorded_at": fl._iso(r["recorded_at"]), "actor": r["actor"],
            "actor_role": _get(r, "actor_role"),
            "judgements": [{"key": j.get("key"), "question": j.get("question")}
                           for j in _loads(r["judgements"], []) if isinstance(j, Mapping)],
            "evidence_count": len(_loads(r["evidence"], [])),
        }
    return out


async def list_due_resolvers(
    conn: Any, *, now: datetime | None = None, horizon_days: int = 14,
    include_closed: bool = False, limit: int = 50,
) -> dict[str, Any]:
    """The ledger questions due (or overdue) within ``horizon_days``: what
    resolves them, when, every issuer's number, and the newest proposal and
    the judgement it waits on (tracker W11-F162)."""
    when = _now(now)
    rows = await fl.fetch_ledger(conn, limit=500, now=when)
    horizon = when + timedelta(days=max(0, int(horizon_days)))
    sel = [r for r in rows if (include_closed or r["status"] == "open")
           and fl._instant(r["due_at"], name="due_at") <= horizon]
    sel = sel[: max(1, int(limit))]
    try:
        recs = await latest_records(conn, [r["id"] for r in sel])
        recs_note = None
    except Exception as exc:  # noqa: BLE001 — 0244 not applied: proposals unknown, said so
        recs, recs_note = {}, f"resolution records unreadable ({type(exc).__name__})"
    items = []
    for r in sel:
        due = fl._instant(r["due_at"], name="due_at")
        items.append({
            "slug": r["slug"], "question": r["question"], "status": r["status"],
            "resolver_kind": r["resolver_kind"], "resolver_spec": r["resolver_spec"],
            "machine_resolvable": r["machine_resolvable"], "due_at": r["due_at"],
            "grace_until": (due + timedelta(hours=int(r["grace_hours"]))).isoformat(),
            "days_to_due": round((due - when).total_seconds() / 86400.0, 1),
            "overdue": r["overdue"], "sealed": r["sealed_at"] is not None,
            "void_rules": r["void_rules"],
            "numbers": [{"issuer_kind": n["issuer_kind"], "p": n["p"], "absent_reason": n["absent_reason"]}
                        for n in r["numbers"]],
            "resolution": r["resolution"],
            "latest_proposal": recs.get(r["id"], {}).get("proposal"),
        })
    return {
        "as_of": when.isoformat(), "horizon_days": int(horizon_days), "count": len(items),
        "note": recs_note or ("ledger forecasts by due date; a proposal is a rule's reading, the "
                              "operator confirms every kind but series_threshold"),
        "items": items,
    }


#: How much of a proposal's evidence the queue carries (the rest is counted;
#: the record route serves one question's in full up to its own cap).
QUEUE_EVIDENCE_CAP = 40


async def resolution_queue(conn: Any, *, limit: int = 100) -> list[dict[str, Any]]:
    """Sealed open questions whose newest proposal no act has answered —
    what waits on a person. Each item: the question and its sealed rule
    (spec, window, due date, grace, condition, void rules) and the proposal
    (outcome, evidence capped at :data:`QUEUE_EVIDENCE_CAP` with the total,
    clauses, judgements, the void rule weighed)."""
    out: list[dict[str, Any]] = []
    for r in await conn.fetch(QUEUE_SQL, max(1, int(limit))):
        evidence = _loads(r["evidence"], [])
        evidence = evidence if isinstance(evidence, list) else []
        out.append({
            "question_id": r["id"], "slug": r["slug"], "question": r["question"],
            "resolver_kind": r["resolver_kind"], "resolver_spec": fl._json_field(r["resolver_spec"]) or {},
            "machine_resolvable": r["resolver_kind"] in fl.MACHINE_RESOLVABLE,
            "window_start": fl._iso(r["window_start"]), "window_end": fl._iso(r["window_end"]),
            "due_at": fl._iso(r["due_at"]), "grace_hours": int(r["grace_hours"] or 0),
            "condition": r["condition"], "void_rules": r["void_rules"],
            "content_sha256": r["content_sha256"],
            "proposal": {
                "record_id": r["proposal_id"], "outcome": r["outcome"],
                "as_of": fl._iso(r["as_of"]), "recorded_at": fl._iso(r["recorded_at"]),
                "evidence": evidence[:QUEUE_EVIDENCE_CAP], "evidence_total": len(evidence),
                "clauses": _loads(r["clauses"], []), "judgements": _loads(r["judgements"], []),
                "void_rule": fl._json_field(r["void_rule"]),
            },
        })
    return out


async def record_leaf_rows(conn: Any) -> list[Mapping[str, Any]]:
    return list(await conn.fetch(RECORD_LEAVES_SQL))
