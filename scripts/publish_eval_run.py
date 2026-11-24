#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""publish_eval_run.py — publish ONE evaluation run to the console (E-3; migration 0260).

The owner (or an admin) runs this on the host to put a standing evaluation
run — the blind test (``scripts/bakeoff/``) and the ones after it — on the
console's Ledger page, where signed-in readers of this instance see it
through ``GET /v3/eval/runs``. Nothing is public: the route is bearer-gated
and the run's files never enter the repository.

  publish   read a run's report by path, map it to the table's shape, and
            show what would be published (DRY RUN, the default). ``--apply``
            writes it, recording ``--actor``. Publishing a run that already
            has a version is a CORRECTION: a new version that supersedes the
            old one (both kept) and needs ``--correction-reason``.
  withdraw  withdraw a published version (default: the newest). The record
            stays and is listed as withdrawn with the reason. Dry run unless
            ``--apply``.

WHAT IS NEVER STORED. Arms and aggregates only. This script reads a report's
per-arm and per-class AGGREGATES and its run metadata; it never reads the
report's per-answer rows (``answers`` / ``rows``), and it stores no question,
no gold answer, no model answer and no per-question text of any kind — the
0260 tables have no column that could hold one (``legba.data.eval_runs``),
and the dry run prints counts, field names and arm-level numbers only.

THE REPORT SHAPES IT READS (all from ``scripts/bakeoff/report.py`` and its
1b extension):

  * ``legba.bakeoff.report/1`` (run 1's ``report.json``): ``analysis.per_arm``,
    ``analysis.per_class``, ``analysis.primary.metric``, and ``run`` (run id,
    as-of, design hash and version, the grader served).
  * ``legba.bakeoff.report_1b/1`` (run 1b's ``report_1b.json``): top-level
    ``per_arm`` / ``per_class``, ``run1b`` (run id, as-of, design hash and
    version, gold hash), ``grader_pin``.
  * the variants report (run 1b's ``report_1b_goldv2.json``: ``variants``
    keyed by name, no schema): ``--variant`` picks one; its metadata comes
    from the ``run.json`` beside it; ``--run-key`` is required (a variant is
    a separate published run, never a version of its parent).

WHAT THE FILES DO NOT SAY, AND THIS SCRIPT THEREFORE TAKES AS ARGUMENTS
(never invented): ``--title``; ``--temperature`` (the answering temperature);
``--gold-version`` (one or more, the owner's names for the gold sets);
``--metric-label`` (the primary measure in plain words); ``--ci-level`` (the
interval's level); ``--arms`` (a JSON file giving EVERY arm of the report a
plain-words label and description, and the sample index of an arm run
twice); the caveats (``--caveat`` / ``--caveats-file``) and the deviations
(``--deviation`` and/or ``--deviations-md`` + ``--deviation-ids``, which take
the ``## Dn: …`` headings of the deviations record); optionally
``--purpose`` and ``--design-registered-at``.

Usage (dry run: plain host python, no database):

    python3 scripts/publish_eval_run.py publish --report <dir>/report.json \\
        --title "..." --temperature 1.0 --gold-version "gold v1" \\
        --metric-label "..." --ci-level 0.95 --arms arms.json [--caveat ...]

Apply (the registry image's env — the compose-run idiom of load_forecasts.py):

    reg() { docker compose -p legba --env-file .env -f docker-compose.yml run --rm \\
        --no-deps -v "$PWD:$PWD" -w "$PWD" -e LEGBA_DATA_PG_DB=legba \\
        --entrypoint python legba-registry "$@"; }
    reg scripts/publish_eval_run.py publish ... --actor <email | user:<uuid>> --apply
    reg scripts/publish_eval_run.py withdraw --run-key K --reason "..." --actor ... --apply

``--actor`` is the audit actor (``user:<uuid>``, or an email resolved to it);
with ``--apply`` the user must exist, be active and hold the operator class
(admin/owner). Connection: PostgresConfig.from_env(). No model is called.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from legba.data.eval_runs import (  # noqa: E402
    ACTOR_RE,
    ClassScore,
    EvalArmRecord,
    EvalRunRecord,
    EvalRunRefusal,
    content_sha256,
    publish,
    run_problems,
    withdraw,
)

SCHEMA_RUN1 = "legba.bakeoff.report/1"
SCHEMA_RUN1B = "legba.bakeoff.report_1b/1"
VARIANTS_SCHEMA = "legba.bakeoff.report_1b.variants"

#: A heading of the deviations record: ``## D7: normaliser v1.2, written …``.
_DEVIATION_HEADING = re.compile(r"^##\s+(D\d+):\s*(.+?)\s*$")


class LoadRefusal(ValueError):
    pass


# ---------------------------------------------------------------------------
# Reading the files
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise LoadRefusal(f"{path}: no such file") from None
    except ValueError as exc:
        raise LoadRefusal(f"{path}: not JSON ({exc})") from None


def _ts(v: Any, what: str) -> datetime:
    if not isinstance(v, str):
        raise LoadRefusal(f"{what}: missing from the files")
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise LoadRefusal(f"{what}: not an ISO instant") from None
    if t.tzinfo is None:
        raise LoadRefusal(f"{what}: has no timezone")
    return t


def read_report(report_path: Path, *, variant: Optional[str] = None) -> dict[str, Any]:
    """The aggregates and metadata of one report, normalised across the three shapes.

    Returns ``{schema, source_sha256, meta: {run_id, as_of, design_sha256,
    design_version, grader, gold_sha256}, primary_metric, per_arm, per_class,
    void_questions: <count>}`` — never a per-answer row.
    """
    raw = report_path.read_bytes() if report_path.exists() else None
    if raw is None:
        raise LoadRefusal(f"{report_path}: no such file")
    doc = _load_json(report_path)
    if not isinstance(doc, dict):
        raise LoadRefusal(f"{report_path}: not a report object")
    run_json_path = report_path.parent / "run.json"
    run_json = _load_json(run_json_path) if run_json_path.exists() else {}
    schema = doc.get("schema")
    primary_metric = None
    void = 0
    if schema == SCHEMA_RUN1:
        if variant:
            raise LoadRefusal("--variant applies only to a variants report")
        analysis = doc.get("analysis") or {}
        per_arm, per_class = analysis.get("per_arm"), analysis.get("per_class")
        primary_metric = (analysis.get("primary") or {}).get("metric")
        r = doc.get("run") or {}
        meta = {
            "run_id": r.get("run_id"), "as_of": r.get("as_of"),
            "design_sha256": r.get("design_sha256"), "design_version": r.get("design_version"),
            "grader": r.get("grader_model_served") or run_json.get("grader_model_served"),
            "gold_sha256": run_json.get("gold_sha256"),
        }
        void = len(doc.get("void_questions") or [])
    elif schema == SCHEMA_RUN1B:
        if variant:
            raise LoadRefusal("--variant applies only to a variants report")
        per_arm, per_class = doc.get("per_arm"), doc.get("per_class")
        r = doc.get("run1b") or {}
        pin = ((doc.get("grader_pin") or {}).get("primary_grader") or {})
        meta = {
            "run_id": r.get("run_id"), "as_of": r.get("as_of"),
            "design_sha256": r.get("design_sha256"), "design_version": r.get("design_version"),
            "grader": run_json.get("grader_model_served") or pin.get("served_model"),
            "gold_sha256": r.get("gold_sha256"),
        }
    elif schema is None and isinstance(doc.get("variants"), dict):
        names = sorted(doc["variants"])
        if not variant:
            raise LoadRefusal(f"a variants report: name one with --variant ({', '.join(names)})")
        if variant not in doc["variants"]:
            raise LoadRefusal(f"--variant {variant!r} is not in the report ({', '.join(names)})")
        if not run_json:
            raise LoadRefusal(f"a variants report carries no run metadata and {run_json_path} is missing")
        v = doc["variants"][variant]
        per_arm, per_class = v.get("per_arm"), v.get("per_class")
        schema = f"{VARIANTS_SCHEMA}#{variant}"
        meta = {
            "run_id": run_json.get("run_id"), "as_of": run_json.get("as_of"),
            "design_sha256": run_json.get("design_sha256"), "design_version": run_json.get("design_version"),
            "grader": run_json.get("grader_model_served"), "gold_sha256": run_json.get("gold_sha256"),
        }
    else:
        raise LoadRefusal(f"{report_path}: an unknown report shape (schema {schema!r})")
    if not isinstance(per_arm, dict) or not per_arm:
        raise LoadRefusal(f"{report_path}: no per-arm aggregates")
    if not isinstance(per_class, dict):
        per_class = {}
    return {
        "schema": schema, "source_sha256": hashlib.sha256(raw).hexdigest(), "meta": meta,
        "primary_metric": primary_metric, "per_arm": per_arm, "per_class": per_class,
        "void_questions": void,
    }


def read_arms_file(path: Path) -> dict[str, dict[str, Any]]:
    doc = _load_json(path)
    if not isinstance(doc, dict) or not all(isinstance(v, dict) for v in doc.values()):
        raise LoadRefusal(f"{path}: an object of arm key → {{label, description[, sample_of, sample_index]}}")
    return doc


def read_deviation_headings(path: Path, ids: list[str]) -> list[str]:
    """``## Dn: heading`` lines of a deviations record, for the ids asked, in that order."""
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _DEVIATION_HEADING.match(line)
        if m and m.group(1) not in found:
            found[m.group(1)] = f"{m.group(1)}: {m.group(2)}"
    missing = [i for i in ids if i not in found]
    if missing:
        raise LoadRefusal(f"{path}: no heading for {', '.join(missing)}")
    return [found[i] for i in ids]


def expand_ids(spec: str) -> list[str]:
    """``D1-D9,D23`` → ``[D1, …, D9, D23]``."""
    out: list[str] = []
    for part in (p.strip() for p in spec.split(",") if p.strip()):
        m = re.fullmatch(r"D(\d+)-D?(\d+)", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                raise LoadRefusal(f"--deviation-ids: {part} runs backwards")
            out += [f"D{i}" for i in range(lo, hi + 1)]
        elif re.fullmatch(r"D\d+", part):
            out.append(part)
        else:
            raise LoadRefusal(f"--deviation-ids: {part!r} is not Dn or Dn-Dm")
    return out


def read_notes_file(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


# ---------------------------------------------------------------------------
# Mapping to the table's shape
# ---------------------------------------------------------------------------


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _count(d: dict[str, Any]) -> int:
    """The n behind a score: the graded answers, else every answer."""
    v = d.get("graded", d.get("n"))
    return int(v) if isinstance(v, int) else 0


def build_record(report: dict[str, Any], a: argparse.Namespace) -> EvalRunRecord:
    meta = report["meta"]
    metric = a.metric or report["primary_metric"] or "correct"
    arms_doc = read_arms_file(Path(a.arms))
    per_arm, per_class = report["per_arm"], report["per_class"]
    missing = [k for k in per_arm if k not in arms_doc]
    extra = [k for k in arms_doc if k not in per_arm]
    if missing:
        raise LoadRefusal(f"--arms gives no plain-words label/description for: {', '.join(missing)} "
                          "(every arm is published, losses beside wins)")
    if extra:
        raise LoadRefusal(f"--arms names arms the report does not have: {', '.join(extra)}")
    arms: list[EvalArmRecord] = []
    for key, agg in per_arm.items():
        if metric not in agg:
            raise LoadRefusal(f"arm {key!r} has no {metric!r} in the report")
        ci = agg.get(f"{metric}_ci")
        lo, hi = (ci if isinstance(ci, list) and len(ci) == 2 else (None, None))
        spec = arms_doc[key]
        by_class = tuple(
            ClassScore(klass=c, score=_num((per_class[c].get(key) or {}).get(metric)),
                       n=_count(per_class[c].get(key) or {}))
            for c in per_class if isinstance(per_class[c], dict) and key in per_class[c]
        )
        arms.append(EvalArmRecord(
            arm_key=key, label=str(spec.get("label") or ""), description=str(spec.get("description") or ""),
            n=_count(agg), score=_num(agg.get(metric)), ci_low=_num(lo), ci_high=_num(hi),
            sample_of=spec.get("sample_of"), sample_index=spec.get("sample_index"), by_class=by_class,
        ))
    question_count = max((int(v["n"]) for v in per_arm.values() if isinstance(v.get("n"), int)), default=0)
    classes = tuple(
        (c, max((int(x["n"]) for x in per_class[c].values() if isinstance(x, dict) and isinstance(x.get("n"), int)),
                default=0))
        for c in per_class if isinstance(per_class[c], dict)
    )
    deviations = list(a.deviation or [])
    if a.deviations_md:
        deviations = read_deviation_headings(Path(a.deviations_md), expand_ids(a.deviation_ids or "")) + deviations
    elif a.deviation_ids:
        raise LoadRefusal("--deviation-ids needs --deviations-md")
    caveats = list(a.caveat or [])
    if a.caveats_file:
        caveats = read_notes_file(Path(a.caveats_file)) + caveats
    run_key = a.run_key or meta.get("run_id")
    if report["schema"].startswith(VARIANTS_SCHEMA) and not a.run_key:
        raise LoadRefusal("a variant is its own published run: give it --run-key")
    if not meta.get("design_sha256") or not meta.get("design_version"):
        raise LoadRefusal("the files carry no design hash/version")
    grader = a.grader or meta.get("grader")
    if not grader:
        raise LoadRefusal("the files name no grader: give --grader")
    return EvalRunRecord(
        run_key=str(run_key or ""), title=a.title, purpose=a.purpose,
        design_sha256=str(meta["design_sha256"]), design_version=str(meta["design_version"]),
        design_registered_at=_ts(a.design_registered_at, "--design-registered-at") if a.design_registered_at else None,
        as_of=_ts(meta.get("as_of"), "as_of"), question_count=question_count, question_classes=classes,
        grader=str(grader), temperature=a.temperature, gold_versions=tuple(a.gold_version or ()),
        metric=metric, metric_label=a.metric_label, higher_is_better=not a.lower_is_better,
        ci_level=a.ci_level, caveats=tuple(caveats), deviations=tuple(deviations),
        source_schema=report["schema"], source_sha256=report["source_sha256"], arms=tuple(arms),
    )


def _fmt(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.3f}"


def dry_run_summary(rec: EvalRunRecord, report: dict[str, Any]) -> dict[str, Any]:
    """What would be published: counts, field names and arm-level numbers. No question or answer text."""
    return {
        "would_publish": {
            "run_key": rec.run_key,
            "version": "1 if unpublished, else the next (a correction; needs --correction-reason)",
            "title": rec.title,
            "purpose_chars": len(rec.purpose or ""),
            "design": {"sha256": rec.design_sha256, "version": rec.design_version,
                       "registered_at": rec.design_registered_at.isoformat() if rec.design_registered_at else None},
            "as_of": rec.as_of.isoformat(),
            "question_count": rec.question_count,
            "question_classes": {c: n for c, n in rec.question_classes},
            "grader": rec.grader,
            "temperature": rec.temperature,
            "gold_versions": list(rec.gold_versions),
            "metric": rec.metric, "metric_label": rec.metric_label,
            "higher_is_better": rec.higher_is_better, "ci_level": rec.ci_level,
            "arms": [
                {"arm": x.arm_key, "label": x.label, "description_chars": len(x.description),
                 "score": _fmt(x.score), "interval": f"{_fmt(x.ci_low)}..{_fmt(x.ci_high)}", "n": x.n,
                 "sample": f"{x.sample_index} of {x.sample_of}" if x.sample_of else None,
                 "classes": len(x.by_class)}
                for x in rec.arms
            ],
            "caveats": len(rec.caveats),
            "deviations": len(rec.deviations),
            "source": {"schema": rec.source_schema, "sha256": rec.source_sha256},
            "content_sha256": content_sha256(rec),
        },
        "fields_stored": sorted(f for f in EvalRunRecord.__dataclass_fields__ if f != "arms")
        + sorted(f"arms[].{f}" for f in EvalArmRecord.__dataclass_fields__),
        "not_stored": "no question, gold answer, model answer or per-question text (the tables have no column for one)",
        "noted_in_files_not_published": {
            "void_questions": report["void_questions"],
            "gold_sha256": report["meta"].get("gold_sha256"),
        },
        "problems": run_problems(rec),
        "apply": "re-run with --actor <email|user:<uuid>> --apply to write",
    }


# ---------------------------------------------------------------------------
# The database half (--apply only)
# ---------------------------------------------------------------------------

_USER_BY_EMAIL = "SELECT id::text AS id, role, status FROM users WHERE email = $1"
_USER_BY_ID = "SELECT id::text AS id, role, status FROM users WHERE id = $1::uuid"


async def resolve_actor(conn: Any, actor: str) -> str:
    """``user:<uuid>`` or an email → the audit actor of an ACTIVE operator-class user.

    A bearer-principal actor (``operator:bearer:<sha12>``) is recorded as given.
    """
    if not actor:
        raise LoadRefusal("--actor is required with --apply")
    if re.fullmatch(r"(operator|service):bearer:[0-9a-f]{12}", actor):
        return actor
    if actor.startswith("user:"):
        row = await conn.fetchrow(_USER_BY_ID, actor[len("user:"):]) if ACTOR_RE.match(actor) else None
    elif "@" in actor:
        row = await conn.fetchrow(_USER_BY_EMAIL, actor.strip().lower())
    else:
        raise LoadRefusal("--actor: an email, user:<uuid>, or operator:bearer:<sha12>")
    if row is None:
        raise LoadRefusal(f"--actor: no user {actor!r}")
    if row["status"] != "active" or row["role"] != "operator":
        raise LoadRefusal(f"--actor: {actor!r} is not an active admin/owner (role {row['role']}, {row['status']})")
    return f"user:{row['id']}"


async def run(conn_factory: Any, a: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    """The CLI body; ``conn_factory()`` is awaited only with ``--apply``."""
    try:
        if a.cmd == "publish":
            report = read_report(Path(a.report), variant=a.variant)
            rec = build_record(report, a)
            summary = dry_run_summary(rec, report)
            if not a.apply:
                return (1 if summary["problems"] else 0), summary
            if summary["problems"]:
                return 1, summary
            conn = await conn_factory()
            try:
                actor = await resolve_actor(conn, a.actor)
                out = await publish(conn, rec, actor=actor, correction_reason=a.correction_reason)
            finally:
                await conn.close()
            return 0, {"published": out, "actor": actor}
        # withdraw
        plan = {"would_withdraw": {"run_key": a.run_key, "version": a.version or "the newest",
                                   "reason_chars": len(a.reason or "")}}
        if not a.apply:
            return 0, {**plan, "apply": "re-run with --actor ... --apply to write"}
        conn = await conn_factory()
        try:
            actor = await resolve_actor(conn, a.actor)
            out = await withdraw(conn, a.run_key, actor=actor, reason=a.reason, version=a.version)
        finally:
            await conn.close()
        return 0, {"withdrawn": out, "actor": actor}
    except (LoadRefusal, EvalRunRefusal) as exc:
        return 1, {"refused": str(exc)}


async def _connect() -> Any:  # pragma: no cover — the live path
    import asyncpg

    from legba.data.config import PostgresConfig

    return await asyncpg.connect(PostgresConfig.from_env().dsn)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("publish", help="publish one run (dry run unless --apply)")
    s.add_argument("--report", required=True, help="the run's report JSON (read-only)")
    s.add_argument("--variant", help="for a variants report: which variant")
    s.add_argument("--run-key", help="the stable id (default: the run id in the files)")
    s.add_argument("--title", required=True)
    s.add_argument("--purpose", help="one sentence: what the run answers")
    s.add_argument("--temperature", type=float, required=True, help="the answering temperature")
    s.add_argument("--gold-version", action="append", required=True, help="repeatable")
    s.add_argument("--grader", help="override the grader the files name")
    s.add_argument("--metric", help="the primary measure's key (default: the report's primary, else 'correct')")
    s.add_argument("--metric-label", required=True, help="the primary measure in plain words")
    s.add_argument("--lower-is-better", action="store_true")
    s.add_argument("--ci-level", type=float, help="the interval's level, e.g. 0.95")
    s.add_argument("--arms", required=True, help="JSON: arm key → {label, description[, sample_of, sample_index]}")
    s.add_argument("--caveat", action="append", help="repeatable")
    s.add_argument("--caveats-file", help="one caveat per line ('#' lines skipped)")
    s.add_argument("--deviation", action="append", help="repeatable")
    s.add_argument("--deviations-md", help="a deviations record with '## Dn: …' headings")
    s.add_argument("--deviation-ids", help="e.g. D1-D9 or D10-D23")
    s.add_argument("--design-registered-at", help="ISO instant the design was registered")
    s.add_argument("--correction-reason", help="required to publish a new version of a published run")
    s.add_argument("--actor", help="email, user:<uuid> or operator:bearer:<sha12> (required with --apply)")
    s.add_argument("--apply", action="store_true", help="write (default: dry run)")
    w = sub.add_parser("withdraw", help="withdraw a published version (dry run unless --apply)")
    w.add_argument("--run-key", required=True)
    w.add_argument("--version", type=int)
    w.add_argument("--reason", required=True)
    w.add_argument("--actor")
    w.add_argument("--apply", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:  # pragma: no cover — the live path
    a = parser().parse_args(argv)
    code, out = asyncio.run(run(_connect, a))
    print(json.dumps(out, indent=2, default=str, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
