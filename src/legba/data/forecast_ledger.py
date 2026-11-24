# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The exogenous forecast ledger — the ONE store for migration 0242 (ST0-A).

Every probability names what resolves it and when (the Story's third
promise). This module is the model and the store for that record:

  * :class:`ForecastQuestion` — the question, its resolver kind (the decided
    vocabulary, :data:`RESOLVER_KINDS`) and spec, its window, due date, grace,
    pre-registered void rules and the operator's condition;
  * :class:`ForecastNumber` — one issuer's probability on one question
    (:data:`ISSUER_KINDS`: the operator, the scheduled tower, the consult, the
    frontier and Clef comparison arms, later the think tank's seats and actor
    models), or a stated ABSENCE (``p`` None + ``absent_reason``) — an issuer
    with no number is recorded as absent, never imputed;
  * :class:`ForecastResolution` — the outcome (yes | no | void), who resolved
    it and on what evidence.

THE SEAL. A question and each number are hashed (``content_sha256``,
canonical JSON) at insert; a number's hash binds its question's hash.
:func:`seal_question` stamps ``sealed_at`` on the question and its numbers in
one transaction and refuses while the question lacks an operator number, a
valid resolver spec, or still carries an ``<OPERATOR …>`` placeholder. After
the seal :func:`state_number` refuses (and the 0242 triggers refuse any
UPDATE of a sealed number and any change to a sealed question but its
``status``). :func:`resolve` refuses before ``due_at`` unless ``early`` with
evidence named, and refuses a second resolution unless it is a correction
with a reason (the old row retires, an ``override_log`` row says who and
why). ``receipt_anchor`` folds every ledger hash (:data:`LEDGER_LEAVES_SQL`,
:func:`ledger_leaf`) into the day's Merkle root.

LEAF. stdlib only, plus the stdlib-only ``principals`` leaf (``yaml`` is
imported inside :func:`parse_forecasts_yaml`):
the registry-slim route ``registry/forecast_ledger_api.py`` imports this
module, so nothing from ``legba.data.analysts`` or ``legba.runtime`` may
appear here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .principals import handle_for_actor

__all__ = [
    "DEFAULT_YAML",
    "ENDOGENOUS_RESOLVER_KINDS",
    "ISSUER_KINDS",
    "LEDGER_LEAVES_SQL",
    "LEDGER_VERSION",
    "LedgerRefusal",
    "ForecastNumber",
    "ForecastQuestion",
    "MACHINE_RESOLVABLE",
    "OUTCOMES",
    "P_MAX",
    "P_MIN",
    "RESOLVER_KINDS",
    "SeedEntry",
    "STATUSES",
    "build_ledger_rows",
    "clamp_p",
    "fetch_ledger",
    "ledger_leaf",
    "parse_forecasts_yaml",
    "resolve",
    "seal_problems",
    "seal_question",
    "state_number",
    "upsert_question",
]

#: The ledger contract revision — hash recipe, vocabularies, the seal rule.
LEDGER_VERSION = "2026-11/st0"

#: The decided resolver vocabulary (plan 10 §3.2, D-10). Exogenous: a named
#: authority, document, series or reported event decides the question.
RESOLVER_KINDS: tuple[str, ...] = (
    "event_reported",
    "official_document",
    "official_result",
    "series_threshold",
    "count_threshold",
    "scheduled_event_status",
)
#: Endogenous kinds score Legba's own desks: stored with exogenous=false,
#: never in the headline Brier.
ENDOGENOUS_RESOLVER_KINDS: tuple[str, ...] = ("indicator_state", "desk_convergence")
#: The kinds a job resolves without the operator's accept.
MACHINE_RESOLVABLE: frozenset[str] = frozenset({"series_threshold"})

#: Who may hold a number (migration 0242's CHECK). The operator's numbers, the
#: scheduled analysts (``tower``), the consult, the frontier comparison arm
#: (``frontier_arm``), the $0 Clef arm (``clef_arm``); ``synthesis``, ``seat``
#: and ``actor_model`` are the think tank's (ST2/ST3).
ISSUER_KINDS: tuple[str, ...] = (
    "operator", "tower", "consult", "synthesis", "seat", "actor_model",
    "frontier_arm", "clef_arm",
)
OUTCOMES: tuple[str, ...] = ("yes", "no", "void")
STATUSES: tuple[str, ...] = ("open", "resolved", "void")

#: A probability is a hedge (the acute pilot's clamp rule): 0.01..0.99,
#: three decimals (``numeric(4,3)``).
P_MIN = 0.01
P_MAX = 0.99

#: The marker a seed carries where the operator still owes text or a number.
OPERATOR_PLACEHOLDER = "<OPERATOR"

_THRESHOLD_OPS = (">=", ">", "<=", "<", "==")
_SCHEDULED_EXPECT = ("held", "postponed", "cancelled")

#: The keys each resolver kind's spec must carry. ``also`` (any kind) is a
#: list of sub-conditions ``{kind, spec, negate}`` that must ALL hold — F5's
#: "a halt announced by both AND no attributed strike in the last 7 days".
_SPEC_REQUIRED: dict[str, tuple[str, ...]] = {
    "event_reported": ("class",),
    "official_document": ("issuers", "doc_type"),
    "official_result": ("authority", "outcome"),
    "series_threshold": ("source", "series", "op", "value", "on"),
    "count_threshold": ("authority", "count", "op", "value"),
    "scheduled_event_status": ("scheduled_event_id", "expect"),
    "indicator_state": (),
    "desk_convergence": (),
}

#: Default seed file — gitignored per ``seeds/*.yaml``; the tracked schema
#: doc is ``seeds/forecasts.example.yaml`` (FICTIONAL rows only).
DEFAULT_YAML = Path(__file__).resolve().parents[3] / "seeds" / "forecasts.yaml"


class LedgerRefusal(ValueError):
    """A ledger rule refused the write. ``code`` names the rule
    (``invalid``, ``sealed``, ``unknown_question``, ``before_due``,
    ``early_without_evidence``, ``already_resolved``, ``unsealed``,
    ``seal_incomplete``, ``already_sealed``)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


# ---------------------------------------------------------------------------
# Pure helpers — time, hashing, the clamp
# ---------------------------------------------------------------------------


def _instant(value: Any, *, name: str) -> datetime:
    """An aware UTC instant; naive is read as UTC, a ``date`` is midnight UTC."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name}: cannot parse {value!r} as ISO-8601") from exc
    else:
        raise ValueError(f"{name}: expected an ISO-8601 instant, got {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _opt_instant(value: Any, *, name: str) -> datetime | None:
    return None if value is None else _instant(value, name=name)


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return _instant(value, name="t").isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return None if value is None else str(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return _iso(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return str(value)


def canonical_json(obj: Any) -> str:
    """The hash recipe's byte form: sorted keys, no whitespace, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=_jsonable)


def sha256_hex(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def clamp_p(p: float) -> float:
    """Pull a probability into [0.01, 0.99] at three decimals."""
    return round(min(P_MAX, max(P_MIN, float(p))), 3)


def _json_field(raw: Any) -> Any:
    """asyncpg hands jsonb back as text unless a codec is set."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


# ---------------------------------------------------------------------------
# The resolver spec
# ---------------------------------------------------------------------------


def resolver_spec_problems(kind: str, spec: Any, *, depth: int = 0) -> list[str]:
    """What is wrong with ``spec`` for ``kind`` (empty = valid)."""
    if kind not in RESOLVER_KINDS + ENDOGENOUS_RESOLVER_KINDS:
        return [f"resolver_kind {kind!r} is not in the decided vocabulary {RESOLVER_KINDS}"]
    if not isinstance(spec, Mapping) or not spec:
        return [f"{kind}: resolver_spec must be a non-empty mapping"]
    problems = [
        f"{kind}: resolver_spec lacks {key!r}"
        for key in _SPEC_REQUIRED[kind]
        if spec.get(key) in (None, "", [], {})
    ]
    if kind in ("series_threshold", "count_threshold") and spec.get("op") not in (None, *_THRESHOLD_OPS):
        problems.append(f"{kind}: op {spec.get('op')!r} is not one of {_THRESHOLD_OPS}")
    if kind == "scheduled_event_status" and spec.get("expect") not in (None, *_SCHEDULED_EXPECT):
        problems.append(f"{kind}: expect {spec.get('expect')!r} is not one of {_SCHEDULED_EXPECT}")
    if kind in ("series_threshold", "count_threshold") and spec.get("value") is not None:
        try:
            float(spec["value"])
        except (TypeError, ValueError):
            problems.append(f"{kind}: value {spec['value']!r} is not a number")
    also = spec.get("also")
    if also is not None:
        if depth or not isinstance(also, list) or not also:
            problems.append(f"{kind}: 'also' must be a non-empty list of {{kind, spec}} (one level)")
        else:
            for i, sub in enumerate(also, start=1):
                if not isinstance(sub, Mapping):
                    problems.append(f"{kind}: also[{i}] is not a mapping")
                    continue
                problems += [
                    f"also[{i}]: {p}"
                    for p in resolver_spec_problems(str(sub.get("kind")), sub.get("spec"), depth=1)
                ]
    return problems


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


@dataclass
class ForecastQuestion:
    slug: str
    question: str
    resolver_kind: str
    resolver_spec: dict[str, Any]
    due_at: datetime
    exogenous: bool = True
    question_class: str = "general"
    window_start: datetime | None = None
    window_end: datetime | None = None
    grace_hours: int = 72
    condition: str | None = None
    void_rules: str | None = None
    subject_keys: tuple[str, ...] = ()
    created_by: str = "operator"

    def content(self) -> dict[str, Any]:
        """Every field the seal covers (``created_by`` is provenance, not content)."""
        return {
            "slug": self.slug,
            "question": self.question,
            "question_class": self.question_class,
            "resolver_kind": self.resolver_kind,
            "resolver_spec": self.resolver_spec,
            "window_start": _iso(self.window_start),
            "window_end": _iso(self.window_end),
            "due_at": _iso(self.due_at),
            "grace_hours": int(self.grace_hours),
            "condition": self.condition,
            "void_rules": self.void_rules,
            "exogenous": bool(self.exogenous),
            "subject_keys": list(self.subject_keys),
        }

    def content_sha256(self) -> str:
        return sha256_hex(self.content())

    def problems(self) -> list[str]:
        out: list[str] = []
        if not self.slug.strip():
            out.append("slug is required")
        if not self.question.strip():
            out.append("question is required")
        out += resolver_spec_problems(self.resolver_kind, self.resolver_spec)
        if self.exogenous and self.resolver_kind in ENDOGENOUS_RESOLVER_KINDS:
            out.append(f"{self.resolver_kind} is endogenous: exogenous must be false")
        if (self.window_start and self.window_end and self.window_start > self.window_end):
            out.append("window_start is after window_end")
        if self.resolver_kind == "event_reported" and not (
            (self.window_start and self.window_end) or self.resolver_spec.get("window")
        ):
            out.append("event_reported needs a window (window: {start, end} or spec.window)")
        if self.grace_hours < 0:
            out.append("grace_hours is negative")
        return out

    def owed_fields(self) -> list[str]:
        """Fields that still carry an ``<OPERATOR …>`` placeholder."""
        return [
            k for k, v in self.content().items()
            if OPERATOR_PLACEHOLDER in canonical_json(v)
        ]

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ForecastQuestion":
        return cls(
            slug=str(row["slug"]),
            question=str(row["question"]),
            question_class=str(row.get("question_class") or "general"),
            resolver_kind=str(row["resolver_kind"]),
            resolver_spec=_json_field(row["resolver_spec"]) or {},
            due_at=_instant(row["due_at"], name="due_at"),
            exogenous=bool(row["exogenous"]),
            window_start=_opt_instant(row.get("window_start"), name="window_start"),
            window_end=_opt_instant(row.get("window_end"), name="window_end"),
            grace_hours=int(row.get("grace_hours") or 0),
            condition=row.get("condition"),
            void_rules=row.get("void_rules"),
            subject_keys=tuple(str(k) for k in (row.get("subject_keys") or ())),
            created_by=str(row.get("created_by") or "operator"),
        )


@dataclass
class ForecastNumber:
    issuer_kind: str
    issuer_ref: str
    p: float | None
    as_of: datetime
    absent_reason: str | None = None
    rationale: str | None = None
    rationale_ref: str | None = None
    #: Operational metadata (model, usage, est. cost, packet hash). Stored,
    #: shown, NOT hashed: the seal covers the claim, not the meter.
    detail: dict[str, Any] = field(default_factory=dict)

    def normalised_p(self) -> float | None:
        return None if self.p is None else clamp_p(self.p)

    def content(self, question_sha256: str) -> dict[str, Any]:
        return {
            "question_sha256": question_sha256,
            "issuer_kind": self.issuer_kind,
            "issuer_ref": self.issuer_ref,
            "p": self.normalised_p(),
            "absent_reason": self.absent_reason,
            "as_of": _iso(self.as_of),
            "rationale": self.rationale,
            "rationale_ref": self.rationale_ref,
        }

    def content_sha256(self, question_sha256: str) -> str:
        return sha256_hex(self.content(question_sha256))

    def problems(self) -> list[str]:
        out: list[str] = []
        if self.issuer_kind not in ISSUER_KINDS:
            out.append(f"issuer_kind {self.issuer_kind!r} is not one of {ISSUER_KINDS}")
        if not str(self.issuer_ref).strip():
            out.append("issuer_ref is required")
        if (self.p is None) == (not (self.absent_reason or "").strip()):
            out.append("a number carries a p or an absent_reason — exactly one")
        if self.p is not None and not 0.0 <= float(self.p) <= 1.0:
            out.append(f"p {self.p!r} is not a probability")
        return out


@dataclass
class ForecastResolution:
    outcome: str
    resolved_by: str
    resolved_at: datetime
    evidence_refs: tuple[str, ...] = ()
    early: bool = False
    note: str | None = None

    def content_sha256(self, question_sha256: str) -> str:
        return sha256_hex({
            "question_sha256": question_sha256,
            "outcome": self.outcome,
            "resolved_by": self.resolved_by,
            "resolved_at": _iso(self.resolved_at),
            "evidence_refs": list(self.evidence_refs),
            "early": bool(self.early),
            "note": self.note,
        })


# ---------------------------------------------------------------------------
# The store (asyncpg connection; every write goes through one of these)
# ---------------------------------------------------------------------------

_Q_COLS = (
    "slug, question, question_class, resolver_kind, resolver_spec, window_start, "
    "window_end, due_at, grace_hours, condition, void_rules, exogenous, subject_keys, "
    "created_by, content_sha256"
)

_SELECT_QUESTION_BY_SLUG = (
    "SELECT id::text AS id, sealed_at, content_sha256 FROM forecast_questions WHERE slug = $1"
)
_SELECT_QUESTION_BY_ID = (
    f"SELECT id::text AS id, {_Q_COLS}, status, sealed_at FROM forecast_questions "
    "WHERE id = $1::uuid"
)
_INSERT_QUESTION = f"""
INSERT INTO forecast_questions ({_Q_COLS}, written_by)
VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10, $11, $12, $13::text[], $14, $15,
        legba_principal($16))
RETURNING id::text AS id
"""
_UPDATE_QUESTION = """
UPDATE forecast_questions
   SET slug = $2, question = $3, question_class = $4, resolver_kind = $5,
       resolver_spec = $6::jsonb, window_start = $7, window_end = $8, due_at = $9,
       grace_hours = $10, condition = $11, void_rules = $12, exogenous = $13,
       subject_keys = $14::text[], created_by = $15, content_sha256 = $16,
       written_by = legba_principal($17)
 WHERE id = $1::uuid AND sealed_at IS NULL
"""
_SELECT_NUMBER = """
SELECT id::text AS id, content_sha256, sealed_at FROM forecast_numbers
 WHERE question_id = $1::uuid AND issuer_kind = $2 AND issuer_ref = $3
"""
_INSERT_NUMBER = """
INSERT INTO forecast_numbers (question_id, issuer_kind, issuer_ref, p, absent_reason,
                              as_of, rationale, rationale_ref, detail, content_sha256,
                              written_by)
VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, legba_principal($11))
RETURNING id::text AS id
"""
_UPDATE_NUMBER = """
UPDATE forecast_numbers
   SET p = $2, absent_reason = $3, as_of = $4, rationale = $5, rationale_ref = $6,
       detail = $7::jsonb, content_sha256 = $8, issued_at = now(),
       written_by = legba_principal($9)
 WHERE id = $1::uuid AND sealed_at IS NULL
"""
_OPERATOR_NUMBER_COUNT = """
SELECT count(*) FROM forecast_numbers
 WHERE question_id = $1::uuid AND issuer_kind = 'operator' AND p IS NOT NULL
"""
_NUMBERS_FOR_QUESTION = """
SELECT id::text AS id, issuer_kind, issuer_ref, p::float8 AS p, absent_reason, as_of,
       rationale, rationale_ref, content_sha256
  FROM forecast_numbers WHERE question_id = $1::uuid
"""
_SEAL_QUESTION ="UPDATE forecast_questions SET sealed_at = $2 WHERE id = $1::uuid AND sealed_at IS NULL"
_SEAL_NUMBERS = "UPDATE forecast_numbers SET sealed_at = $2 WHERE question_id = $1::uuid AND sealed_at IS NULL"
_ACTIVE_RESOLUTION = """
SELECT id::text AS id, content_sha256 FROM forecast_resolutions
 WHERE question_id = $1::uuid AND active
"""
_RETIRE_RESOLUTION = "UPDATE forecast_resolutions SET active = false WHERE id = $1::uuid AND active"
_INSERT_RESOLUTION = """
INSERT INTO forecast_resolutions (question_id, outcome, resolved_at, resolved_by,
                                  evidence_refs, early, proposal_id, note, content_sha256,
                                  written_by)
VALUES ($1::uuid, $2, $3, $4, $5::text[], $6, $7::uuid, $8, $9, legba_principal($10))
RETURNING id::text AS id
"""
_SET_STATUS = "UPDATE forecast_questions SET status = $2 WHERE id = $1::uuid"
_INSERT_OVERRIDE = """
INSERT INTO override_log (actor, object_kind, object_id, action, before_sha256,
                          after_sha256, reason)
VALUES ($1, $2, $3::uuid, $4, $5, $6, $7)
"""


def _question_args(q: ForecastQuestion) -> list[Any]:
    return [
        q.slug, q.question, q.question_class, q.resolver_kind,
        canonical_json(q.resolver_spec), q.window_start, q.window_end, q.due_at,
        int(q.grace_hours), q.condition, q.void_rules, bool(q.exogenous),
        list(q.subject_keys), q.created_by, q.content_sha256(),
    ]


async def upsert_question(
    conn: Any, q: ForecastQuestion, *, written_by: str | None = None,
) -> tuple[str, str]:
    """``(outcome, id)`` — outcome ``inserted`` | ``updated`` | ``unchanged``.

    ``written_by`` is the writing principal's handle (lane 9) — not part of the
    sealed content (``created_by`` is the question's stated author).

    Idempotent on the natural key ``slug``. A changed question is restated
    only while unsealed; a sealed question with different content refuses
    (``sealed``) — the ledger never rewrites words a number was sealed on.
    """
    problems = q.problems()
    if problems:
        raise LedgerRefusal("invalid", f"{q.slug}: " + "; ".join(problems))
    row = await conn.fetchrow(_SELECT_QUESTION_BY_SLUG, q.slug)
    sha = q.content_sha256()
    if row is None:
        new = await conn.fetchrow(_INSERT_QUESTION, *_question_args(q), written_by)
        return "inserted", str(new["id"])
    if row["content_sha256"] == sha:
        return "unchanged", str(row["id"])
    if row["sealed_at"] is not None:
        raise LedgerRefusal("sealed", f"{q.slug} is sealed; its content cannot change")
    await conn.execute(_UPDATE_QUESTION, row["id"], *_question_args(q), written_by)
    return "updated", str(row["id"])


async def _question(conn: Any, question_id: str) -> Mapping[str, Any]:
    row = await conn.fetchrow(_SELECT_QUESTION_BY_ID, str(question_id))
    if row is None:
        raise LedgerRefusal("unknown_question", f"no forecast question {question_id}")
    return row


async def state_number(
    conn: Any, question_id: str, n: ForecastNumber, *, written_by: str | None = None,
) -> tuple[str, str]:
    """``(outcome, id)`` — state (or, before the seal, restate) one issuer's
    number. Refuses on a sealed question: a position is stated before the
    seal or not at all. ``written_by`` is the writing principal's handle
    (lane 9); the issuer columns say whose number it is."""
    problems = n.problems()
    if problems:
        raise LedgerRefusal("invalid", "; ".join(problems))
    q = await _question(conn, question_id)
    if q["sealed_at"] is not None:
        raise LedgerRefusal("sealed", f"{q['slug']} is sealed; no number may be stated now")
    sha = n.content_sha256(str(q["content_sha256"]))
    existing = await conn.fetchrow(_SELECT_NUMBER, str(question_id), n.issuer_kind, n.issuer_ref)
    # numeric(4,3) binds from a Decimal of the clamped value's text — a float
    # would carry its binary expansion onto the wire.
    p = None if n.normalised_p() is None else Decimal(str(n.normalised_p()))
    detail = canonical_json(n.detail or {})
    if existing is None:
        new = await conn.fetchrow(
            _INSERT_NUMBER, str(question_id), n.issuer_kind, n.issuer_ref, p,
            n.absent_reason, n.as_of, n.rationale, n.rationale_ref, detail, sha, written_by,
        )
        return "inserted", str(new["id"])
    if existing["content_sha256"] == sha:
        return "unchanged", str(existing["id"])
    await conn.execute(
        _UPDATE_NUMBER, existing["id"], p, n.absent_reason, n.as_of, n.rationale,
        n.rationale_ref, detail, sha, written_by,
    )
    return "updated", str(existing["id"])


async def seal_problems(conn: Any, question_id: str) -> list[str]:
    """Why ``question_id`` may not be sealed (empty = it may)."""
    row = await _question(conn, question_id)
    q = ForecastQuestion.from_row(row)
    out = [f"{q.slug}: {p}" for p in q.problems()]
    out += [f"{q.slug}: {k} still carries an operator placeholder" for k in q.owed_fields()]
    if row["sealed_at"] is not None:
        out.append(f"{q.slug}: already sealed at {_iso(row['sealed_at'])}")
    if not await conn.fetchval(_OPERATOR_NUMBER_COUNT, str(question_id)):
        out.append(f"{q.slug}: no operator number")
    # A number binds the words it was stated on: a question restated after a
    # number was stated leaves that number on the old wording — restate it.
    q_sha = str(row["content_sha256"])
    for n in await conn.fetch(_NUMBERS_FOR_QUESTION, str(question_id)):
        stated = ForecastNumber(
            issuer_kind=str(n["issuer_kind"]), issuer_ref=str(n["issuer_ref"]),
            p=None if n["p"] is None else float(n["p"]), as_of=n["as_of"],
            absent_reason=n["absent_reason"], rationale=n["rationale"],
            rationale_ref=n["rationale_ref"],
        )
        if stated.content_sha256(q_sha) != n["content_sha256"]:
            out.append(
                f"{q.slug}: the {n['issuer_kind']} number ({n['issuer_ref']}) was stated "
                "on an earlier wording of the question; restate it"
            )
    return out


async def seal_question(conn: Any, question_id: str, *, at: datetime | None = None) -> datetime:
    """Seal one question and every number on it, in one transaction."""
    problems = await seal_problems(conn, question_id)
    if problems:
        raise LedgerRefusal("seal_incomplete", "; ".join(problems))
    when = _instant(at, name="at") if at is not None else datetime.now(timezone.utc)
    async with conn.transaction():
        await conn.execute(_SEAL_QUESTION, str(question_id), when)
        await conn.execute(_SEAL_NUMBERS, str(question_id), when)
    return when


async def resolve(
    conn: Any,
    question_id: str,
    *,
    outcome: str,
    resolved_by: str,
    evidence_refs: Sequence[str] = (),
    note: str | None = None,
    early: bool = False,
    proposal_id: str | None = None,
    correction_reason: str | None = None,
    now: datetime | None = None,
) -> str:
    """Record the outcome; returns the resolution id.

    Refuses: an unknown outcome; a yes/no on an unsealed question (it was
    never a forecast); before ``due_at`` unless ``early`` with evidence; a
    second resolution unless ``correction_reason`` is given (the old row
    retires and an ``override_log`` row records who and why).
    """
    if outcome not in OUTCOMES:
        raise LedgerRefusal("invalid", f"outcome {outcome!r} is not one of {OUTCOMES}")
    q = await _question(conn, question_id)
    when = _instant(now, name="now") if now is not None else datetime.now(timezone.utc)
    refs = tuple(str(r) for r in evidence_refs if str(r).strip())
    if outcome != "void" and q["sealed_at"] is None:
        raise LedgerRefusal("unsealed", f"{q['slug']} was never sealed; only void may close it")
    if when < _instant(q["due_at"], name="due_at"):
        if not early:
            raise LedgerRefusal(
                "before_due", f"{q['slug']} is due {_iso(q['due_at'])}; pass early with evidence"
            )
        if not refs:
            raise LedgerRefusal("early_without_evidence", f"{q['slug']}: an early resolution names its evidence")
    active = await conn.fetchrow(_ACTIVE_RESOLUTION, str(question_id))
    if active is not None and not (correction_reason or "").strip():
        raise LedgerRefusal("already_resolved", f"{q['slug']} is resolved; a correction needs a reason")
    res = ForecastResolution(
        outcome=outcome, resolved_by=resolved_by, resolved_at=when,
        evidence_refs=refs, early=bool(early and when < _instant(q["due_at"], name="due_at")),
        note=note,
    )
    sha = res.content_sha256(str(q["content_sha256"]))
    async with conn.transaction():
        if active is not None:
            await conn.execute(_RETIRE_RESOLUTION, active["id"])
        new = await conn.fetchrow(
            _INSERT_RESOLUTION, str(question_id), outcome, when, resolved_by,
            list(refs), res.early, proposal_id, note, sha,
            # Lane 9: resolved_by is the server-stamped audit actor.
            handle_for_actor(resolved_by),
        )
        if active is not None:
            await conn.execute(
                _INSERT_OVERRIDE, resolved_by, "forecast_resolution", active["id"],
                "correct", active["content_sha256"], sha, correction_reason,
            )
        await conn.execute(_SET_STATUS, str(question_id), "void" if outcome == "void" else "resolved")
    return str(new["id"])


# ---------------------------------------------------------------------------
# The read — the Ledger route's payload, and the anchor's leaves
# ---------------------------------------------------------------------------

LEDGER_QUESTIONS_SQL = """
SELECT id::text AS id, slug, question, question_class, resolver_kind,
       resolver_spec::text AS resolver_spec, window_start, window_end, due_at,
       grace_hours, condition, void_rules, exogenous, subject_keys, status,
       sealed_at, created_at, content_sha256
  FROM forecast_questions
 ORDER BY due_at, slug
 LIMIT $1
"""
LEDGER_NUMBERS_SQL = """
SELECT question_id::text AS question_id, issuer_kind, issuer_ref, p::float8 AS p,
       absent_reason, as_of, issued_at, rationale, rationale_ref, sealed_at,
       content_sha256
  FROM forecast_numbers
 WHERE question_id = ANY($1::uuid[])
 ORDER BY issuer_kind, issuer_ref
"""
LEDGER_RESOLUTIONS_SQL = """
SELECT question_id::text AS question_id, id::text AS id, outcome, resolved_at,
       resolved_by, evidence_refs, early, note
  FROM forecast_resolutions
 WHERE active AND question_id = ANY($1::uuid[])
"""

#: Every ledger hash, in the fixed order the anchor folds them: (kind, id)
#: in BYTE order (``COLLATE "C"``) — a locale collation would order the
#: uuids' hyphens differently on another host, and the root must be
#: recomputable by anyone holding the same rows.
LEDGER_LEAVES_SQL = """
SELECT kind, id, content_sha256 FROM (
    SELECT 'forecast_number' AS kind, id::text AS id, content_sha256 FROM forecast_numbers
    UNION ALL
    SELECT 'forecast_question', id::text, content_sha256 FROM forecast_questions
    UNION ALL
    SELECT 'forecast_resolution', id::text, content_sha256 FROM forecast_resolutions
) leaves
ORDER BY kind COLLATE "C", id COLLATE "C"
"""


def ledger_leaf(kind: str, row_id: str, content_sha256: str) -> bytes:
    """One anchor leaf = ``sha256(kind ':' id || content_sha256)``."""
    return hashlib.sha256(f"{kind}:{row_id}{content_sha256}".encode("utf-8")).digest()


def _status_rank(status: str) -> int:
    return 0 if status == "open" else 1


def build_ledger_rows(
    questions: Iterable[Mapping[str, Any]],
    numbers: Iterable[Mapping[str, Any]],
    resolutions: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """The Ledger's rows: open questions first by due date (what resolves
    next), then closed ones by due date; each with every issuer's number and
    the active resolution. ``overdue`` = open past due + grace (shown, never
    silently expired)."""
    when = now or datetime.now(timezone.utc)
    by_q: dict[str, list[dict[str, Any]]] = {}
    for n in numbers:
        by_q.setdefault(str(n["question_id"]), []).append({
            "issuer_kind": n["issuer_kind"],
            "issuer_ref": n["issuer_ref"],
            "p": None if n.get("p") is None else float(n["p"]),
            "absent_reason": n.get("absent_reason"),
            "as_of": _iso(n.get("as_of")),
            "issued_at": _iso(n.get("issued_at")),
            "rationale": n.get("rationale"),
            "rationale_ref": n.get("rationale_ref"),
            "sealed": n.get("sealed_at") is not None,
            "content_sha256": n.get("content_sha256"),
        })
    res = {str(r["question_id"]): r for r in resolutions}
    rows: list[dict[str, Any]] = []
    for q in questions:
        qid = str(q["id"])
        due = _instant(q["due_at"], name="due_at")
        grace = int(q.get("grace_hours") or 0)
        r = res.get(qid)
        rows.append({
            "id": qid,
            "slug": q["slug"],
            "question": q["question"],
            "question_class": q.get("question_class"),
            "resolver_kind": q["resolver_kind"],
            "resolver_spec": _json_field(q.get("resolver_spec")),
            "machine_resolvable": q["resolver_kind"] in MACHINE_RESOLVABLE,
            "window_start": _iso(q.get("window_start")),
            "window_end": _iso(q.get("window_end")),
            "due_at": _iso(due),
            "grace_hours": grace,
            "condition": q.get("condition"),
            "void_rules": q.get("void_rules"),
            "exogenous": bool(q.get("exogenous")),
            "subject_keys": [str(k) for k in (q.get("subject_keys") or [])],
            "status": q.get("status") or "open",
            "sealed_at": _iso(q.get("sealed_at")),
            "content_sha256": q.get("content_sha256"),
            "overdue": (q.get("status") or "open") == "open"
            and (when.timestamp() > due.timestamp() + grace * 3600),
            "numbers": sorted(
                by_q.get(qid, []),
                key=lambda n: (ISSUER_KINDS.index(n["issuer_kind"])
                               if n["issuer_kind"] in ISSUER_KINDS else 99, n["issuer_ref"]),
            ),
            "resolution": None if r is None else {
                "id": r["id"],
                "outcome": r["outcome"],
                "resolved_at": _iso(r.get("resolved_at")),
                "resolved_by": r.get("resolved_by"),
                "evidence_refs": [str(x) for x in (r.get("evidence_refs") or [])],
                "early": bool(r.get("early")),
                "note": r.get("note"),
            },
        })
    rows.sort(key=lambda row: (_status_rank(row["status"]), row["due_at"] or "", row["slug"]))
    return rows


async def fetch_ledger(conn: Any, *, limit: int = 200, now: datetime | None = None) -> list[dict[str, Any]]:
    """The three reads + :func:`build_ledger_rows`."""
    questions = [dict(r) for r in await conn.fetch(LEDGER_QUESTIONS_SQL, int(limit))]
    ids = [q["id"] for q in questions]
    if not ids:
        return []
    numbers = [dict(r) for r in await conn.fetch(LEDGER_NUMBERS_SQL, ids)]
    resolutions = [dict(r) for r in await conn.fetch(LEDGER_RESOLUTIONS_SQL, ids)]
    return build_ledger_rows(questions, numbers, resolutions, now=now)


# ---------------------------------------------------------------------------
# The seed file — machinery, never data (house rule 8)
# ---------------------------------------------------------------------------


@dataclass
class SeedEntry:
    """One parsed seed row: the question and the operator's number, if given
    (``None`` = owed; the loader leaves it unstated and ``--seal`` refuses)."""

    question: ForecastQuestion
    operator_probability: float | None
    operator_ref: str = "operator"
    operator_rationale: str | None = None
    #: When the operator stated the number — part of its hash, so it comes
    #: from the FILE (never the run clock): a re-run must write nothing.
    operator_as_of: datetime | None = None
    desks: tuple[str, ...] = ()


def _parse_entry(row: Any, *, default_as_of: Any = None) -> SeedEntry:
    if not isinstance(row, Mapping):
        raise ValueError("expected a mapping")
    for key in ("slug", "question", "resolver_kind", "resolver_spec", "due_at"):
        if row.get(key) in (None, ""):
            raise ValueError(f"{key} is required")
    window = row.get("window") or {}
    if not isinstance(window, Mapping):
        raise ValueError("window must be a mapping {start, end}")
    keys = row.get("subject_keys") or []
    if not isinstance(keys, (list, tuple)):
        raise ValueError("subject_keys must be a list")
    q = ForecastQuestion(
        slug=str(row["slug"]).strip(),
        question=" ".join(str(row["question"]).split()),
        question_class=str(row.get("class") or "general").strip(),
        resolver_kind=str(row["resolver_kind"]).strip(),
        resolver_spec=json.loads(canonical_json(row["resolver_spec"])),
        due_at=_instant(row["due_at"], name="due_at"),
        exogenous=bool(row.get("exogenous", True)),
        window_start=_opt_instant(window.get("start"), name="window.start"),
        window_end=_opt_instant(window.get("end"), name="window.end"),
        grace_hours=int(row.get("grace_hours", 72)),
        condition=(str(row["condition"]).strip() if row.get("condition") else None),
        void_rules=(" ".join(str(row["void_rules"]).split()) if row.get("void_rules") else None),
        subject_keys=tuple(str(k).strip() for k in keys if str(k).strip()),
        created_by=str(row.get("created_by") or "operator"),
    )
    problems = q.problems()
    if problems:
        raise ValueError("; ".join(problems))
    raw_p = row.get("operator_probability")
    p: float | None = None
    if raw_p is not None:
        p = float(raw_p)
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"operator_probability {raw_p!r} is not a probability")
    raw_as_of = row.get("operator_as_of") or default_as_of
    if p is not None and raw_as_of is None:
        raise ValueError("operator_as_of (per row or top level) is required with an operator_probability")
    return SeedEntry(
        question=q,
        operator_probability=p,
        operator_ref=str(row.get("operator_ref") or "operator"),
        operator_rationale=(str(row["operator_rationale"]).strip()
                            if row.get("operator_rationale") else None),
        operator_as_of=_opt_instant(raw_as_of, name="operator_as_of"),
        desks=tuple(q.subject_keys),
    )


def parse_forecasts_yaml(path: Path) -> tuple[list[SeedEntry], list[str]]:
    """Parse + validate a ``forecasts.yaml`` seed file → ``(entries, errors)``.
    A bad row is reported and skipped (the bulk-upload house pattern); two rows
    with one slug are an error on the second."""
    import yaml  # deferred: the registry-slim route imports this module

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    rows = raw.get("forecasts") if isinstance(raw, Mapping) else None
    entries: list[SeedEntry] = []
    errors: list[str] = []
    seen: set[str] = set()
    for i, row in enumerate(rows or [], start=1):
        try:
            entry = _parse_entry(row, default_as_of=raw.get("operator_as_of"))
        except (ValueError, TypeError) as exc:
            errors.append(f"row {i}: {exc}")
            continue
        if entry.question.slug in seen:
            errors.append(f"row {i}: duplicate slug {entry.question.slug!r}")
            continue
        seen.add(entry.question.slug)
        entries.append(entry)
    return entries, errors
