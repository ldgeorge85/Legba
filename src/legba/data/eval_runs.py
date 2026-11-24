# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The published evaluation record — the ONE store for migration 0260 (E-3).

The owner publishes a standing evaluation run (the blind test and the ones
after it) with ``scripts/publish_eval_run.py``; signed-in readers see it on
the console's Ledger page through ``GET /v3/eval/runs``
(``registry/eval_runs_api.py``). This module is both halves' SQL.

WHAT IS STORED, AND WHAT NEVER IS. A version of a run: its title, a one-line
purpose, the pre-registered design's hash and version, the as-of instant,
the question count and the classes with their n, the grader, the answering
temperature, the gold version(s), the primary measure, the caveats and the
departures from the design, who published it and when — and per arm the
label, a plain-words description, the score with its interval and n, the
per-class scores with their n, and the sample index of an arm run twice.
AGGREGATES ONLY: :class:`EvalRunRecord` and :class:`EvalArmRecord` have no
field that could carry a question, a gold answer, a model answer or any
per-question text, and the tables have no such column.

APPEND-ONLY. A published version is never edited: a correction is a new
version (``version`` + 1, ``supersedes_id`` naming the version it corrects,
a ``correction_reason``); both are kept and the older one reads as
superseded. A version can be WITHDRAWN once (``withdraw``), which records
who, when and why and keeps the row; the route lists it as withdrawn. The
0260 guard trigger refuses every other UPDATE and every DELETE.

Stdlib only: the registry-slim route imports this leaf.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

STORE_VERSION = "0260/e3"

#: The audit-actor shapes a publication or withdrawal may record: a signed-in
#: user (``legba.data.users.user_actor``: ``user:<uuid>``) or a bearer
#: principal (``registry._deps.principal_actor``: ``operator:bearer:<sha12>``).
ACTOR_RE = re.compile(
    r"^(user:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|(operator|service):bearer:[0-9a-f]{12})$"
)
RUN_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ARM_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: Bounds that keep every text field a LABEL, never a carrier for a
#: question or an answer (the 0260 CHECKs repeat the column ones).
MAX_TITLE = 200
MAX_PURPOSE = 300
MAX_ARM_LABEL = 120
MAX_ARM_DESCRIPTION = 400
MAX_NOTE = 600  # one caveat or one deviation
MAX_NOTES = 40
MAX_CLASSES = 20
MAX_ARMS = 30


class EvalRunRefusal(ValueError):
    """A publication or withdrawal the store refuses, with the reasons."""

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class ClassScore:
    klass: str
    score: Optional[float]
    n: int


@dataclass(frozen=True)
class EvalArmRecord:
    arm_key: str
    label: str
    description: str
    n: int
    score: Optional[float] = None
    ci_low: Optional[float] = None
    ci_high: Optional[float] = None
    sample_of: Optional[str] = None
    sample_index: Optional[int] = None
    by_class: tuple[ClassScore, ...] = ()


@dataclass(frozen=True)
class EvalRunRecord:
    run_key: str
    title: str
    design_sha256: str
    design_version: str
    as_of: datetime
    question_count: int
    grader: str
    temperature: float
    gold_versions: tuple[str, ...]
    metric: str
    metric_label: str
    source_schema: str
    source_sha256: str
    purpose: Optional[str] = None
    design_registered_at: Optional[datetime] = None
    question_classes: tuple[tuple[str, int], ...] = ()
    higher_is_better: bool = True
    ci_level: Optional[float] = None
    caveats: tuple[str, ...] = ()
    deviations: tuple[str, ...] = ()
    arms: tuple[EvalArmRecord, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Validation and the seal
# ---------------------------------------------------------------------------


def _finite(v: Optional[float]) -> bool:
    return v is None or (isinstance(v, (int, float)) and math.isfinite(v))


def _text_ok(v: Optional[str], cap: int) -> bool:
    return v is None or (isinstance(v, str) and 0 < len(v.strip()) and len(v) <= cap)


def run_problems(run: EvalRunRecord) -> list[str]:
    """Every reason the store would refuse ``run`` (empty = publishable)."""
    p: list[str] = []
    if not RUN_KEY_RE.match(run.run_key or ""):
        p.append("run_key: letters, digits, '.', '_' or '-', at most 64")
    if not _text_ok(run.title, MAX_TITLE) or not run.title:
        p.append(f"title: required, at most {MAX_TITLE} characters")
    if not _text_ok(run.purpose, MAX_PURPOSE):
        p.append(f"purpose: at most {MAX_PURPOSE} characters (one sentence about the run)")
    for name in ("design_sha256", "source_sha256"):
        if not SHA256_RE.match(getattr(run, name) or ""):
            p.append(f"{name}: 64 lowercase hex characters")
    if not (run.design_version or "").strip():
        p.append("design_version: required")
    if run.as_of is None or run.as_of.tzinfo is None:
        p.append("as_of: a timezone-aware instant")
    if not isinstance(run.question_count, int) or run.question_count < 1:
        p.append("question_count: at least 1")
    if not (run.grader or "").strip():
        p.append("grader: required")
    if not isinstance(run.temperature, (int, float)) or not (0 <= run.temperature <= 2):
        p.append("temperature: a number from 0 to 2")
    if not run.gold_versions or not all((g or "").strip() for g in run.gold_versions):
        p.append("gold_versions: at least one, none blank")
    if not (run.metric or "").strip() or not (run.metric_label or "").strip():
        p.append("metric and metric_label: required")
    if run.ci_level is not None and not (0 < run.ci_level < 1):
        p.append("ci_level: between 0 and 1")
    if len(run.question_classes) > MAX_CLASSES:
        p.append(f"question_classes: at most {MAX_CLASSES}")
    for name in ("caveats", "deviations"):
        notes = getattr(run, name)
        if len(notes) > MAX_NOTES:
            p.append(f"{name}: at most {MAX_NOTES}")
        if any(not _text_ok(n, MAX_NOTE) for n in notes):
            p.append(f"{name}: each non-blank, at most {MAX_NOTE} characters")
    if not run.arms:
        p.append("arms: at least one")
    if len(run.arms) > MAX_ARMS:
        p.append(f"arms: at most {MAX_ARMS}")
    keys = [a.arm_key for a in run.arms]
    if len(set(keys)) != len(keys):
        p.append("arms: each arm_key once")
    for a in run.arms:
        where = f"arm {a.arm_key!r}"
        if not ARM_KEY_RE.match(a.arm_key or ""):
            p.append(f"{where}: arm_key shape")
        if not _text_ok(a.label, MAX_ARM_LABEL) or not a.label:
            p.append(f"{where}: label required, at most {MAX_ARM_LABEL}")
        if not _text_ok(a.description, MAX_ARM_DESCRIPTION) or not a.description:
            p.append(f"{where}: a plain-words description is required, at most {MAX_ARM_DESCRIPTION}")
        if not isinstance(a.n, int) or a.n < 0:
            p.append(f"{where}: n is a count")
        if not all(_finite(v) for v in (a.score, a.ci_low, a.ci_high)):
            p.append(f"{where}: score and interval are finite numbers")
        if (a.ci_low is None) != (a.ci_high is None) or (
            a.ci_low is not None and a.ci_high is not None and a.ci_low > a.ci_high
        ):
            p.append(f"{where}: an interval has both ends, low <= high")
        if (a.sample_of is None) != (a.sample_index is None) or (a.sample_index is not None and a.sample_index < 1):
            p.append(f"{where}: sample_of and sample_index (>= 1) come together")
        if a.sample_of is not None and a.sample_of not in keys:
            p.append(f"{where}: sample_of names no arm of this run")
        for c in a.by_class:
            if not (c.klass or "").strip() or not isinstance(c.n, int) or c.n < 0 or not _finite(c.score):
                p.append(f"{where}: class scores need a class name, a count and a finite score")
                break
    return p


def _iso(v: Optional[datetime]) -> Optional[str]:
    return v.isoformat() if v is not None else None


def canonical_content(run: EvalRunRecord) -> dict[str, Any]:
    """The sealed content of a version: everything published, nothing about who or when."""
    d = asdict(run)
    d["as_of"] = _iso(run.as_of)
    d["design_registered_at"] = _iso(run.design_registered_at)
    return d


def content_sha256(run: EvalRunRecord) -> str:
    blob = json.dumps(canonical_content(run), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# SQL (the fakes and the route dispatch on these constants)
# ---------------------------------------------------------------------------

_SELECT_LATEST = (
    "SELECT id::text AS id, version, content_sha256, withdrawn_at FROM public.eval_runs "
    "WHERE run_key = $1 ORDER BY version DESC LIMIT 1 FOR UPDATE"
)
_SELECT_VERSION = (
    "SELECT id::text AS id, version, withdrawn_at FROM public.eval_runs "
    "WHERE run_key = $1 AND version = $2"
)
_INSERT_RUN = (
    "INSERT INTO public.eval_runs (run_key, version, supersedes_id, correction_reason, title, purpose, "
    "design_sha256, design_version, design_registered_at, as_of, question_count, question_classes, "
    "grader, temperature, gold_versions, metric, metric_label, higher_is_better, ci_level, caveats, "
    "deviations, source_schema, source_sha256, published_by, content_sha256) "
    "VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13, $14, $15::jsonb, $16, "
    "$17, $18, $19, $20::jsonb, $21::jsonb, $22, $23, $24, $25) "
    "RETURNING id::text AS id, published_at"
)
_INSERT_ARM = (
    "INSERT INTO public.eval_run_arms (run_id, position, arm_key, label, description, sample_of, "
    "sample_index, score, ci_low, ci_high, n, by_class) "
    "VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb)"
)
_WITHDRAW = (
    "UPDATE public.eval_runs SET withdrawn_at = now(), withdrawn_by = $2, withdrawn_reason = $3 "
    "WHERE id = $1::uuid AND withdrawn_at IS NULL RETURNING withdrawn_at"
)
#: The route's reads: the as-of index, then the arms by their (run_id, position) key.
SELECT_RUNS = (
    "SELECT id::text AS id, run_key, version, supersedes_id::text AS supersedes_id, correction_reason, "
    "title, purpose, design_sha256, design_version, design_registered_at, as_of, question_count, "
    "question_classes, grader, temperature, gold_versions, metric, metric_label, higher_is_better, "
    "ci_level, caveats, deviations, published_at, withdrawn_at, withdrawn_reason, content_sha256 "
    "FROM public.eval_runs ORDER BY as_of DESC, run_key, version DESC LIMIT $1"
)
SELECT_ARMS = (
    "SELECT run_id::text AS run_id, position, arm_key, label, description, sample_of, sample_index, "
    "score, ci_low, ci_high, n, by_class FROM public.eval_run_arms "
    "WHERE run_id = ANY($1::uuid[]) ORDER BY run_id, position"
)


def _j(v: Any) -> str:
    return json.dumps(v, separators=(",", ":"))


async def publish(conn: Any, run: EvalRunRecord, *, actor: str,
                  correction_reason: Optional[str] = None) -> dict[str, Any]:
    """Write ``run`` as the next version of ``run.run_key``, in one transaction.

    The first publication is version 1. A later one is a CORRECTION: it needs
    ``correction_reason`` and supersedes the newest version. Publishing the
    same content as the newest version writes nothing (``written: False``).
    """
    problems = run_problems(run)
    if not ACTOR_RE.match(actor or ""):
        problems.append("actor: user:<uuid> or <principal>:bearer:<sha12> (the audit actor)")
    if problems:
        raise EvalRunRefusal(problems)
    sha = content_sha256(run)
    async with conn.transaction():
        latest = await conn.fetchrow(_SELECT_LATEST, run.run_key)
        if latest is not None and latest["content_sha256"] == sha and latest["withdrawn_at"] is None:
            return {"written": False, "id": latest["id"], "version": latest["version"], "content_sha256": sha}
        if latest is not None and not (correction_reason or "").strip():
            raise EvalRunRefusal([
                f"run {run.run_key!r} is already published (version {latest['version']}): a new version "
                "is a correction and needs --correction-reason"])
        if latest is None and correction_reason:
            raise EvalRunRefusal([f"run {run.run_key!r} has no published version to correct"])
        version = 1 if latest is None else int(latest["version"]) + 1
        row = await conn.fetchrow(
            _INSERT_RUN, run.run_key, version, None if latest is None else latest["id"],
            correction_reason if latest is not None else None, run.title, run.purpose,
            run.design_sha256, run.design_version, run.design_registered_at, run.as_of,
            run.question_count, _j([{"class": k, "n": n} for k, n in run.question_classes]),
            run.grader, float(run.temperature), _j(list(run.gold_versions)), run.metric,
            run.metric_label, run.higher_is_better, run.ci_level, _j(list(run.caveats)),
            _j(list(run.deviations)), run.source_schema, run.source_sha256, actor, sha,
        )
        for i, a in enumerate(run.arms):
            await conn.execute(
                _INSERT_ARM, row["id"], i, a.arm_key, a.label, a.description, a.sample_of,
                a.sample_index, a.score, a.ci_low, a.ci_high, a.n,
                _j([{"class": c.klass, "score": c.score, "n": c.n} for c in a.by_class]),
            )
    return {"written": True, "id": row["id"], "version": version, "content_sha256": sha,
            "supersedes_id": None if latest is None else latest["id"]}


async def withdraw(conn: Any, run_key: str, *, actor: str, reason: str,
                   version: Optional[int] = None) -> dict[str, Any]:
    """Withdraw one version (default: the newest). The row is kept; once only."""
    problems = []
    if not ACTOR_RE.match(actor or ""):
        problems.append("actor: user:<uuid> or <principal>:bearer:<sha12> (the audit actor)")
    if not (reason or "").strip() or len(reason) > MAX_NOTE:
        problems.append(f"reason: required, at most {MAX_NOTE} characters")
    if problems:
        raise EvalRunRefusal(problems)
    async with conn.transaction():
        if version is None:
            row = await conn.fetchrow(_SELECT_LATEST, run_key)
        else:
            row = await conn.fetchrow(_SELECT_VERSION, run_key, version)
        if row is None:
            raise EvalRunRefusal([f"no published run {run_key!r}" + (f" version {version}" if version else "")])
        if row["withdrawn_at"] is not None:
            raise EvalRunRefusal([f"run {run_key!r} version {row['version']} is already withdrawn"])
        done = await conn.fetchrow(_WITHDRAW, row["id"], actor, reason)
    return {"id": row["id"], "version": row["version"], "withdrawn_at": done["withdrawn_at"] if done else None}


# ---------------------------------------------------------------------------
# The route's read model
# ---------------------------------------------------------------------------


def _loads(v: Any, default: Any) -> Any:
    if v is None:
        return default
    if isinstance(v, (str, bytes)):
        try:
            return json.loads(v)
        except ValueError:
            return default
    return v


def _ts(v: Any) -> Optional[str]:
    return v.isoformat() if isinstance(v, datetime) else (str(v) if v is not None else None)


def build_route_runs(runs: list[dict[str, Any]], arms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows → the route's runs: status in words, superseded_by derived, arms and results."""
    by_run: dict[str, list[dict[str, Any]]] = {}
    for a in arms:
        by_run.setdefault(str(a["run_id"]), []).append(a)
    superseded_by = {str(r["supersedes_id"]): str(r["id"]) for r in runs if r.get("supersedes_id")}
    out: list[dict[str, Any]] = []
    for r in runs:
        rid = str(r["id"])
        status = ("withdrawn" if r.get("withdrawn_at") is not None
                  else "superseded" if rid in superseded_by else "current")
        rows = sorted(by_run.get(rid, []), key=lambda a: a["position"])
        out.append({
            "id": rid,
            "run_key": r["run_key"],
            "version": int(r["version"]),
            "status": status,
            "supersedes": r.get("supersedes_id"),
            "superseded_by": superseded_by.get(rid),
            "correction_reason": r.get("correction_reason"),
            "withdrawn_at": _ts(r.get("withdrawn_at")),
            "withdrawn_reason": r.get("withdrawn_reason"),
            "title": r["title"],
            "question": r.get("purpose"),
            "as_of": _ts(r["as_of"]),
            "published_at": _ts(r.get("published_at")),
            "design": {
                "preregistered_at": _ts(r.get("design_registered_at")),
                "prereg_sha256": r["design_sha256"],
                "version": r["design_version"],
            },
            "question_count": int(r["question_count"]),
            "question_classes": _loads(r.get("question_classes"), []),
            "grader": r["grader"],
            "temperature": r["temperature"],
            "gold_versions": _loads(r.get("gold_versions"), []),
            "arms": [{
                "id": a["arm_key"], "label": a["label"], "description": a["description"],
                "sample_of": a.get("sample_of"), "sample_index": a.get("sample_index"),
            } for a in rows],
            "results": [{
                "arm": a["arm_key"], "metric": r["metric"], "metric_label": r["metric_label"],
                "score": a.get("score"), "ci_low": a.get("ci_low"), "ci_high": a.get("ci_high"),
                "ci_level": r.get("ci_level"), "n": int(a["n"]),
                "higher_is_better": r.get("higher_is_better"),
                "by_class": _loads(a.get("by_class"), []),
            } for a in rows],
            "caveats": _loads(r.get("caveats"), []),
            "deviations": _loads(r.get("deviations"), []),
            "content_sha256": r["content_sha256"],
        })
    return out


async def fetch_runs(conn: Any, *, limit: int = 200) -> list[dict[str, Any]]:
    """Every published version, newest as-of first (``SELECT_RUNS`` + ``SELECT_ARMS``)."""
    runs = [dict(r) for r in await conn.fetch(SELECT_RUNS, limit)]
    if not runs:
        return []
    arms = [dict(a) for a in await conn.fetch(SELECT_ARMS, [r["id"] for r in runs])]
    return build_route_runs(runs, arms)
