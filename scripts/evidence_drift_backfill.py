#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source drift backfill — DRY RUN ONLY: classify what is already there and
report the flags the drift pass would write (lane D-1). Writes nothing.

Three populations, each in a READ-ONLY transaction:

1. ``drift`` — every ``fetch_drift_observations`` row without a
   classification, through the same function the drift pass runs
   (:func:`legba.data.evidence_drift_store.classify_drift_row`, ``write=False``).
2. ``versions`` — consecutive versions of one page address
   (``fetch_versions`` role page/archiver) with no drift row between them
   (their extracted text was equal: ``identical_text`` expected).
3. ``archive`` (``--archive``) — addresses the evidence archive holds more than
   one DIFFERENT copy of (signals sharing a canonical-URL hash, archived
   with different bytes). This history predates the version table (0245), so
   these pairs never reached ``fetch_drift_observations``; they are what the
   drift pass would have seen. Needs the archive root (run inside the runtime
   container) to read the bytes; the dependants are walked from the signals
   whose archived copy is the earlier one.

Output: counts by class per population, the flags that would be written by
class and dependant kind, and the top N substantive-or-worse pairs with their
dependants (and whether each cited span still appears). ``--json`` prints the
whole report as JSON.

D-2 — ``--assess`` (with ``--archive``): the drift ASSESSMENT of every
flag-writing archive pair, code only (the same :func:`assess_code` the pass
runs, with the page's own stamps read from the stored bytes), each would-be
flag's claim effect, whether it would auto-close, and the per-source table.
``--assess-model`` adds the model step through the SAME handler the runtime
builds (``--model-ref``, default the free core plane
``llm.primary.openai_compat``); the script prints the component's endpoint
host and model before the first call and refuses to call a component whose
endpoint is not in ``--allow-host``. ``--out DIR`` writes one JSON per pair
(both texts, the diff, the code and model assessments) plus ``summary.json``.
Still a dry run: nothing is written to the database.

    python3 scripts/evidence_drift_backfill.py [--archive] [--top 20] [--json]
    python3 scripts/evidence_drift_backfill.py --archive --assess [--assess-model --allow-host H] --out DIR
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ARCHIVE_PAIRS_SQL = """
WITH multi AS (
    SELECT s.canonical_url_hash AS h
      FROM evidence_archive ea JOIN signals s ON s.id = ea.signal_id
     WHERE ea.status = 'archived' AND ea.sha256 IS NOT NULL AND s.canonical_url_hash IS NOT NULL
     GROUP BY 1 HAVING count(DISTINCT ea.sha256) > 1
)
SELECT s.canonical_url_hash AS h, s.id AS signal_id, s.source_id,
       coalesce(ea.fetched_url, s.canonical_url) AS url, ea.sha256, ea.content_type, ea.archived_at
  FROM evidence_archive ea JOIN signals s ON s.id = ea.signal_id JOIN multi ON multi.h = s.canonical_url_hash
 WHERE ea.status = 'archived' AND ea.sha256 IS NOT NULL
 ORDER BY 1, ea.archived_at, s.id
"""

VERSION_PAIRS_SQL = """
SELECT v.address, v.id, v.extracted_text, v.text_sha256, v.first_seen_at,
       lag(v.id) OVER w AS prev_id, lag(v.extracted_text) OVER w AS prev_text,
       lag(v.text_sha256) OVER w AS prev_text_sha
  FROM fetch_versions v
 WHERE v.receipt_kind = 'fetch' AND v.role IN ('page', 'archiver')
   AND v.address IN (SELECT address FROM fetch_versions WHERE receipt_kind = 'fetch'
                      AND role IN ('page', 'archiver') GROUP BY address HAVING count(*) > 1)
WINDOW w AS (PARTITION BY v.address ORDER BY v.first_seen_at, v.id)
"""


def _dep_summary(deps: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(d["kind"] for d in deps))


def _stamps(body: Any) -> dict[str, Any]:
    from legba.data.evidence_drift_assess import page_stamps

    return page_stamps(body) if body else {}


async def _published(conn: Any, signal_ids: list[str]) -> Any:
    """The earliest feed ``published_at`` of the signals built from a copy."""
    from legba.data.evidence_drift_assess import _parse_ts

    rows = await conn.fetch("SELECT payload->>'published_at' AS p FROM signals WHERE id = ANY($1::uuid[])",
                            signal_ids)
    ts = [t for t in (_parse_ts(r["p"]) for r in rows) if t is not None]
    return min(ts) if ts else None


async def _assess_pair(conn: Any, report: dict[str, Any], args: argparse.Namespace, a: dict[str, Any],
                       b: dict[str, Any], cls: Any, deps: list[dict[str, Any]]) -> None:
    """D-2: the code (and, with --assess-model, the model) assessment of one
    flag-writing archive pair and each would-be flag's claim effect. Writes
    nothing to the database."""
    from legba.data.evidence_drift_assess import assess_code, auto_close_allowed, claim_effect, full_diff
    from legba.data.evidence_drift_assess_model import (
        assess_prompt, claim_effect_prompt, combine, parse_assessment, parse_claim_effect, run_model,
    )
    from legba.data.evidence_drift_assess_store import MODEL_EFFECTS_PER_CHANGE

    diff = full_diff(a["text"], b["text"])
    published = await _published(conn, a["signals"])
    code = assess_code(
        drift_class=cls.drift_class, rule=cls.rule, diff=diff, address=a["url"],
        earlier_title=a["title"], later_title=b["title"], earlier_stamps=a["stamps"],
        later_stamps=b["stamps"], published_at=published, earlier_seen_at=a["at"],
        earlier_last_seen_at=a["at"], later_seen_at=b["at"], retention=cls.retention,
    )
    entry: dict[str, Any] = {
        "n": len(report.setdefault("assessments", [])), "address": a["url"], "source_id": a["source_id"],
        "earlier_at": str(a["at"]), "later_at": str(b["at"]), "class": cls.drift_class, "rule": cls.rule,
        "code": code.as_dict(), "model": None, "final": None, "effects": [],
    }
    final = code
    llm = report.get("_llm")
    if llm is not None:
        content, err = await run_model(llm, assess_prompt(
            diff, code_type=code.code_type, code_components=list(code.code_components),
            earlier_title=a["title"], later_title=b["title"]))
        from legba.data.evidence_drift_assess_model import ModelResult

        res = ModelResult("call_failed", reason=err) if err else parse_assessment(content or "", diff)
        entry["model"] = res.as_dict()
        final = combine(code, res)
    entry["final"] = final.as_dict()
    from legba.data.evidence_drift_assess import ACKNOWLEDGING_LEVELS, derive_history_rewrite

    comps = final.component_types or [final.change_type]
    alt, _ = derive_history_rewrite(comps, final.acknowledged, final.standing_hours,
                                    acknowledging=ACKNOWLEDGING_LEVELS | {"metadata_date_only"})
    entry["history_rewrite_if_metadata_counts"] = alt
    calls = 0
    for d in deps:
        eff, after, basis = claim_effect(d.get("span_check"), diff, drift_class=cls.drift_class)
        e = {"kind": d["kind"], "id": d["id"], "depth": d["depth"], "effect": eff, "after": after,
             "basis": basis, "cited_span": (d.get("cited_span") or "")[:400] or None, "model": None,
             "would_auto_close": auto_close_allowed(change_type=final.change_type, code_type=code.code_type,
                                                     code_effect=eff)}
        if llm is not None and eff == "not_checkable" and d.get("cited_span") and calls < MODEL_EFFECTS_PER_CHANGE:
            calls += 1
            content, err = await run_model(llm, claim_effect_prompt(d["cited_span"], diff))
            e["model"] = {"status": "call_failed", "reason": err} if err else \
                parse_claim_effect(content or "", diff).as_dict()
        entry["effects"].append(e)
    report["assessments"].append(entry)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        full = dict(entry, earlier_title=a["title"], later_title=b["title"], earlier_text=a["text"],
                    later_text=b["text"], diff={"pairs": diff.pairs, "removed": diff.removed,
                                                "added": diff.added},
                    published=None if published is None else published.isoformat(),
                    stamps={"earlier": {k: str(v) for k, v in (a["stamps"] or {}).items()},
                            "later": {k: str(v) for k, v in (b["stamps"] or {}).items()}})
        (out / f"pair_{entry['n']:02d}.json").write_text(json.dumps(full, default=str, indent=1))


async def _model_handler(conn: Any, args: argparse.Namespace) -> tuple[Any, str]:
    """The SAME handler the runtime builds for the model step. Prints the
    endpoint host and model, and refuses a host not in --allow-host."""
    from urllib.parse import urlsplit

    from legba.data.postgres import PostgresStore
    from legba.data.registry.credentials import CredentialVault
    from legba.runtime.analyst_deps_builder import build_llm_handler_from_stack_component
    from legba.runtime.registry_client import RegistryHTTPClient

    store = PostgresStore.from_env()
    await store.connect()
    vault = CredentialVault(store)

    async def _secrets(secret_id: str) -> bytes:
        return await vault.resolve(secret_id)

    handler = await build_llm_handler_from_stack_component(
        args.model_ref, registry_client=RegistryHTTPClient(), secrets_resolve=_secrets)
    cfg = getattr(handler, "_cfg", None)
    endpoint = str(getattr(getattr(cfg, "api_endpoint", None), "raw", "") or "")
    model = str(getattr(getattr(cfg, "model_name", None), "raw", "") or "")
    host = (urlsplit(endpoint).hostname or "").lower()
    print(f"== model step: component={args.model_ref} host={host} model={model}", file=sys.stderr)
    if host not in {h.lower() for h in args.allow_host}:
        raise SystemExit(f"refusing to call {host!r}: not in --allow-host {args.allow_host}")
    return handler, f"{args.model_ref}:{model}"


async def _archive_population(conn: Any, report: dict[str, Any], args: argparse.Namespace) -> None:
    from legba.data.archive import archive_root
    from legba.data.archive_store import EvidenceStore
    from legba.data.evidence_drift_classify import check_cited_span, classify_pair, extract_title
    from legba.data.evidence_drift_walk import walk_from_records
    from legba.runtime.fetch_receipts import extract_page_text

    store = EvidenceStore.from_env(archive_root())
    rows = await conn.fetch(ARCHIVE_PAIRS_SQL)
    groups: dict[str, list[Any]] = {}
    for r in rows:
        groups.setdefault(r["h"], []).append(r)
    pop = report["populations"].setdefault("archive", {"pairs": 0, "by_class": Counter(),
                                                       "unreadable": 0, "addresses": len(groups)})
    for _h, items in groups.items():
        copies: list[dict[str, Any]] = []
        for it in items:
            if copies and copies[-1]["sha256"] == it["sha256"]:
                copies[-1]["signals"].append(str(it["signal_id"]))
                continue
            if any(c["sha256"] == it["sha256"] for c in copies):
                next(c for c in copies if c["sha256"] == it["sha256"])["signals"].append(str(it["signal_id"]))
                continue
            try:
                body = store.get(it["sha256"])
            except Exception:  # noqa: BLE001
                body = None
            copies.append({"sha256": it["sha256"], "url": it["url"], "source_id": it["source_id"],
                           "at": it["archived_at"], "signals": [str(it["signal_id"])],
                           "text": extract_page_text(body, it["content_type"]) if body else None,
                           "title": extract_title(body), "readable": body is not None,
                           "stamps": _stamps(body)})
        for a, b in zip(copies, copies[1:]):
            pop["pairs"] += 1
            if not (a["readable"] and b["readable"]):
                pop["unreadable"] += 1
                continue
            cls = classify_pair(a["text"], b["text"], earlier_title=a["title"], later_title=b["title"])
            pop["by_class"][cls.drift_class] += 1
            if not cls.flags_dependants:
                continue
            walk = await walk_from_records(conn, [("signal", s, None) for s in a["signals"]],
                                           max_depth=args.depth, max_dependants=args.max_dependants)
            deps = []
            for d in walk.dependants:
                sc = check_cited_span(d.cited_span, a["text"], b["text"]) if d.cited_span else None
                deps.append({"kind": d.kind, "id": d.id, "depth": d.depth,
                             "span_in_later": None if sc is None else sc.span_in_later,
                             "missing": [] if sc is None else sc.missing_in_later[:2],
                             "span_check": None if sc is None else sc.as_dict(),
                             "cited_span": d.cited_span})
            if getattr(args, "assess", False):
                await _assess_pair(conn, report, args, a, b, cls, deps)
            report["would_flag"].append({
                "population": "archive", "address": a["url"], "source_id": a["source_id"],
                "earlier": str(a["at"]), "later": str(b["at"]), "class": cls.drift_class,
                "rule": cls.rule, "critical": {k: v[:8] for k, v in cls.critical.items()},
                "changed": cls.changed[:2], "removed": cls.removed[:2], "dependants": deps,
                "inputs_only": walk.inputs_only, "truncated": walk.truncated,
            })


async def _amain(args: argparse.Namespace) -> int:
    import asyncpg

    from legba.data.evidence_drift_classify import classify_pair, class_severity
    from legba.data.evidence_drift_store import classify_drift_row

    conn = await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )
    report: dict[str, Any] = {"populations": {}, "would_flag": []}
    try:
        async with conn.transaction(readonly=True):
            await conn.execute("SET LOCAL statement_timeout = '600s'")
            has_cls = await conn.fetchval("SELECT to_regclass('public.evidence_drift_classifications')")
            pending = await conn.fetch(
                "SELECT d.id, d.address, d.source_id, d.earlier_version_id, d.later_version_id, "
                "d.earlier_fetched_at, d.later_fetched_at, d.later_status, d.later_gone "
                "FROM fetch_drift_observations d"
                + ("" if has_cls is None else
                   " WHERE NOT EXISTS (SELECT 1 FROM evidence_drift_classifications c WHERE c.drift_id = d.id)")
                + " ORDER BY d.id")
            pop = report["populations"].setdefault("drift", {"rows": len(pending), "by_class": Counter()})
            for d in pending:
                s = await classify_drift_row(conn, d, max_depth=args.depth,
                                             max_dependants=args.max_dependants, write=False)
                pop["by_class"][s["class"]] += 1
                if s["dependants"] or s["class"] in ("substantive_edit", "title_changed",
                                                     "retracted_or_replaced", "gone"):
                    report["would_flag"].append({
                        "population": "drift", "address": s["address"], "source_id": s["source_id"],
                        "class": s["class"], "rule": s["rule"],
                        "critical": s["evidence"].get("critical", {}),
                        "changed": s["evidence"].get("changed", [])[:2],
                        "removed": s["evidence"].get("removed", [])[:2],
                        "dependants": s["dependants"], "inputs_only": s["inputs_only"],
                        "truncated": s["walk_truncated"],
                    })
            vpop = report["populations"].setdefault("versions", {"pairs": 0, "by_class": Counter()})
            for r in await conn.fetch(VERSION_PAIRS_SQL):
                if r["prev_id"] is None:
                    continue
                vpop["pairs"] += 1
                vpop["by_class"][classify_pair(r["prev_text"], r["extracted_text"]).drift_class] += 1
            if args.archive:
                if args.assess and args.assess_model:
                    report["_llm"], report["model_id"] = await _model_handler(conn, args)
                await _archive_population(conn, report, args)
    finally:
        await conn.close()

    report.pop("_llm", None)
    if "assessments" in report:
        report["assessment_summary"] = _assessment_summary(report["assessments"])
        if args.out:
            Path(args.out).mkdir(parents=True, exist_ok=True)
            (Path(args.out) / "summary.json").write_text(json.dumps(
                {"model_id": report.get("model_id"), "summary": report["assessment_summary"],
                 "assessments": report["assessments"]}, default=str, indent=1))
    flags = Counter()
    for w in report["would_flag"]:
        for d in w["dependants"]:
            flags[(w["class"], d["kind"])] += 1
    report["flags_would_write"] = {f"{c}/{k}": n for (c, k), n in sorted(flags.items())}
    report["flags_would_write_total"] = sum(flags.values())
    report["would_flag"].sort(key=lambda w: (-class_severity(w["class"]), -len(w["dependants"])))
    report["top"] = report.pop("would_flag")[: args.top]
    for pop in report["populations"].values():
        pop["by_class"] = dict(pop["by_class"])
    if args.json:
        print(json.dumps(report, default=str, indent=1))
        return 0
    print("== populations")
    for name, pop in report["populations"].items():
        print(f"  {name}: " + ", ".join(f"{k}={v}" for k, v in pop.items() if k != "by_class"))
        for k, v in sorted(pop["by_class"].items(), key=lambda x: -x[1]):
            print(f"      {k}: {v}")
    print(f"== flags the pass would write: {report['flags_would_write_total']}")
    for k, v in report["flags_would_write"].items():
        print(f"  {k}: {v}")
    print(f"== top {len(report['top'])} substantive-or-worse")
    for w in report["top"]:
        print(f"  [{w['class']} / {w['rule']}] {w['address'][:110]} ({w.get('source_id')})")
        print(f"      critical={w['critical']}")
        print(f"      dependants={_dep_summary(w['dependants'])} inputs_only={w['inputs_only']}"
              f" truncated={w['truncated']}")
        spans = [d for d in w["dependants"] if d.get("span_in_later") is not None]
        if spans:
            gone = sum(1 for d in spans if d["span_in_later"] is False)
            print(f"      cited spans checked={len(spans)} no longer in the new text={gone}")
    return 0


def _assessment_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts the report prints: by type (code / final), acknowledged,
    history rewrites, claim effects, auto-closes, and the per-source table."""
    out: dict[str, Any] = {"pairs": len(items), "code_type": Counter(), "final_type": Counter(),
                           "acknowledged": Counter(), "acknowledged_signals": Counter(), "history_rewrite": 0,
                           "history_rewrite_if_metadata_counts": 0, "late_unexplained_change": 0,
                           "model_type_doubt": Counter(), "finding_spans": Counter(), "claim_effect": Counter(),
                           "would_auto_close": 0, "model_status": Counter(), "per_source": {}}
    for it in items:
        c, f = it["code"], it["final"]
        out["code_type"][c["change_type"]] += 1
        out["final_type"][f["change_type"]] += 1
        out["acknowledged"][f["acknowledged"]] += 1
        out["history_rewrite"] += bool(f["history_rewrite"])
        out["history_rewrite_if_metadata_counts"] += bool(it.get("history_rewrite_if_metadata_counts"))
        out["late_unexplained_change"] += bool(f["late_unexplained_change"])
        for sig in f.get("acknowledged_signals") or []:
            out["acknowledged_signals"][sig] += 1
        if f.get("model_type_doubt"):
            out["model_type_doubt"][f["model_type_doubt"]["type"]] += 1
        for e in it["effects"]:
            if e["kind"] == "finding":
                out["finding_spans"][e["basis"].get("reason") if e["effect"] == "not_checkable"
                                     else "span_is_page_text"] += 1
        if it.get("model"):
            out["model_status"][it["model"]["status"]] += 1
        for e in it["effects"]:
            out["claim_effect"][e["effect"]] += 1
            out["would_auto_close"] += bool(e["would_auto_close"])
        src = out["per_source"].setdefault(it["source_id"] or "unknown", {
            "assessed": 0, "by_type": Counter(), "acknowledged": Counter(), "history_rewrite": 0})
        src["assessed"] += 1
        src["by_type"][f["change_type"]] += 1
        src["acknowledged"][f["acknowledged"]] += 1
        src["history_rewrite"] += bool(f["history_rewrite"])
    return json.loads(json.dumps(out, default=dict))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--archive", action="store_true",
                    help="also classify evidence-archive copies (needs the archive root)")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--max-dependants", type=int, default=200)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--assess", action="store_true",
                    help="Assess every flag-writing archive pair (code only; needs --archive)")
    ap.add_argument("--assess-model", action="store_true",
                    help="Also run the model step through the runtime's own handler")
    ap.add_argument("--model-ref", default="llm.primary.openai_compat")
    ap.add_argument("--allow-host", action="append", default=[],
                    help="endpoint host the model step may call (repeat); anything else is refused")
    ap.add_argument("--out", default=None, help="Write per-pair JSON and summary.json here")
    return asyncio.run(_amain(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
