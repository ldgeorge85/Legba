#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The BLIND grading sheet and the tell-test input (design §4, §5.1, §5.4).

``sheet`` — every graded answer, normalised (``normalise.py``), under a random
6-character id, in a SEEDED shuffle across all answers (so arms are shuffled
within and across questions), each beside its question's gold. The id → (arm,
question) KEY is written to a separate file the grader never sees; its sha256
and the seed go into ``run.json`` before grading starts. The key file must not
sit inside the sheet's directory (refused).

The design's void rules (§3.5) are ENFORCED here, where gold exists:

  * a non-INSUFFICIENT question whose gold row is missing, unsigned, or marked
    ``met: false`` is VOID (the gold builder could not meet its gold source);
  * an INSUFFICIENT question is VOID when ``authoritative_answer_exists`` is
    true, or when its absence proof is missing or unsigned;
  * a question the harness refused before the run (a G-CORPUS anchor missing
    from the corpus) is VOID with that reason;
  * a desk whose brief was empty at T is void FOR A ONLY, and A's refusal is
    published as A's result for that row;
  * a class with more than 2 void questions is marked descriptive-only.

Void questions are listed with their reason and never replaced. A label ending
in ``*`` (a stock-consult run, not the pre-registered arm) is excluded. A run
holding any ``dry_run`` record is not a run and is refused (``--practice``
lifts that for fixtures only).

``tell-test`` — 30 normalised answers, 6 per arm A–E, stratified by class,
shuffled under fresh ids, with the five arm titles from the design, and a
sealed key. :func:`tell_test_passes` is the pre-registered criterion: accuracy
≤ 35 %.

``pin-grader`` — THE GRADERS (design v1.1 §4.4, re-pinned 2026-11-05 after
the 2026-10-23 router retirement took v1.0's Gemini / Llama 3.3 70B / Mistral
Medium routes away):

  * primary model grader — :data:`GRADER_COMPONENT` (the Nemotron
    think judge, gateway alias :func:`grader_alias`); no arm uses it;
  * second rater — the OPERATOR, on a manual sheet (the stratified 20 % plus
    every answer the primary marked contradicted); no component;
  * gold drafter — :data:`GOLD_DRAFTER_COMPONENT` (Gemma 4, thinking on,
    alias :func:`gold_drafter_alias`); it never grades, the grader never builds gold, and the
    operator signs every gold row.

"The exact served model id is probed and pinned in the run record before G1.
A route change mid-run voids the run." ``pin-grader`` reads both components
off the registry and the gateway's ``/v1/models`` entry for each alias, and
writes ``grader_pin`` + ``grader_model_served`` into ``run.json``. Run again
(before the tell test, before grading, after grading) it RE-PROBES and
compares: a different served id, alias or component version is written as
``grader_route_changed`` and exits 2 — the run is void. ``sheet`` and
``tell-test`` refuse a run with no pin or a recorded route change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import string
import sys
from pathlib import Path
from typing import Any

import yaml as _yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import design as design_mod  # noqa: E402
import normalise  # noqa: E402

SHEET_SCHEMA = "legba.bakeoff.sheet/1"
KEY_SCHEMA = "legba.bakeoff.blind_key/1"
TELL_ARMS = ("A", "B", "C", "D", "E")
TELL_PER_ARM = 6
TELL_PASS_MAX = 0.35
CLASS_ORDER = ("HEADLINE", "LONG-MEMORY", "SLOW-CHANGE", "INSUFFICIENT")
DESCRIPTIVE_IF_VOID_OVER = 2
GRADABLE = ("answered", "partial", "error")

#: Design v1.1 §4.4. Component ids, not model names: the gateway serves aliases.
GRADER_COMPONENT = "llm.judge.gateway_rung2_think.openai_compat"
SECOND_RATER = "operator"  # a manual sheet — no component, no model
GOLD_DRAFTER_COMPONENT = "llm.verify.gateway_gold_drafter.openai_compat"
_DESCRIPTORS = Path(__file__).resolve().parents[2] / "descriptors"
_ALIAS_FILES = {
    GRADER_COMPONENT: "stack_component_llm_judge_gateway_rung2_think.yaml",
    GOLD_DRAFTER_COMPONENT: "stack_component_llm_verify_gateway_gold_drafter.yaml",
}


def _descriptor_alias(component_id: str) -> str:
    """A component's alias, read from its reviewable descriptor and resolved
    from the environment when the descriptor names a variable (the served
    alias is deployment data; the pin check compares the live row against
    it). An unset variable fails loudly, naming it."""
    from legba.data.schemas.env_refs import resolve_env_value

    body = _yaml.safe_load((_DESCRIPTORS / _ALIAS_FILES[component_id]).read_text(encoding="utf-8"))
    raw = body["config"]["model_name"]["raw"]
    return str(resolve_env_value(raw, component_id=component_id, field="model_name"))


def grader_alias() -> str:
    return _descriptor_alias(GRADER_COMPONENT)


def gold_drafter_alias() -> str:
    return _descriptor_alias(GOLD_DRAFTER_COMPONENT)


def __getattr__(name: str) -> str:
    # ``GRADER_ALIAS`` / ``GOLD_DRAFTER_ALIAS`` resolve on each read, so
    # importing this module never needs the environment.
    if name == "GRADER_ALIAS":
        return grader_alias()
    if name == "GOLD_DRAFTER_ALIAS":
        return gold_drafter_alias()
    raise AttributeError(name)


#: ``/v1/models`` entry keys that may name the model BEHIND an alias, in order.
_SERVED_KEYS = ("root", "served_model", "model", "upstream_model", "parent")


class SheetRefused(RuntimeError):
    pass


def load_records(run_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted((Path(run_dir) / "records").glob("*.json"))]


def void_rules(
    questions: list[Any], gold: dict[str, Any], records: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[tuple[str, str], str], list[str]]:
    """``(void question → reason, void (question, arm) → reason, descriptive classes)``."""
    void: dict[str, str] = {}
    for q in questions:
        row = (gold.get("questions") or {}).get(q.qid)
        signed = bool(row and row.get("signed_by"))
        if q.klass == "INSUFFICIENT":
            if row and row.get("authoritative_answer_exists"):
                void[q.qid] = "an authoritative answer exists (INSUFFICIENT void)"
            elif not signed or not row.get("absence_proof"):
                void[q.qid] = "gold_missing: no operator-signed absence proof"
        elif not signed:
            void[q.qid] = "gold_missing: no operator-signed gold row"
        elif row.get("met") is False:
            void[q.qid] = "the gold builder could not meet the gold source by T+7d"
    arm_void: dict[tuple[str, str], str] = {}
    for r in records:
        reason = (r.get("refusal") or {}).get("reason") or ""
        if r.get("status") == "refused" and reason.startswith("gold_missing"):
            void.setdefault(r["question_id"], reason)
        if r.get("arm") == "A" and r.get("status") == "refused" and reason.startswith("void_for_A"):
            arm_void[(r["question_id"], "A")] = reason
    by_class: dict[str, int] = {}
    klass = {q.qid: q.klass for q in questions}
    for qid in void:
        by_class[klass.get(qid, "?")] = by_class.get(klass.get(qid, "?"), 0) + 1
    descriptive = sorted(c for c, n in by_class.items() if n > DESCRIPTIVE_IF_VOID_OVER)
    return void, arm_void, descriptive


def _ids(rng: random.Random, n: int) -> list[str]:
    alphabet = string.ascii_lowercase + string.digits
    out: list[str] = []
    seen: set[str] = set()
    while len(out) < n:
        x = "".join(rng.choice(alphabet) for _ in range(6))
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _seal(key: dict[str, Any], key_out: Path, sheet_dir: Path) -> str:
    key_out = Path(key_out).resolve()
    if sheet_dir.resolve() in key_out.parents:
        raise SheetRefused(f"the key {key_out} must not sit inside the sheet directory {sheet_dir}")
    key_out.parent.mkdir(parents=True, exist_ok=True)
    blob = normalise.dumps(key)
    key_out.write_text(blob, encoding="utf-8")
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_sheet(
    dz: Any, records: list[dict[str, Any]], gold: dict[str, Any], *, seed: int,
    practice: bool = False, questions: list[Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(sheet, key)`` — pure; :func:`main` writes and seals them."""
    if not practice and any(r.get("status") == "dry_run" for r in records):
        raise SheetRefused("the run holds dry_run records: a dry run is not a run")
    qs = questions or list(dz.questions)
    void, arm_void, descriptive = void_rules(qs, gold, records)
    by_q = {q.qid: q for q in qs}
    items: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for r in records:
        qid, arm = r["question_id"], r["arm"]
        if str(r.get("label") or "").endswith("*"):
            excluded.append({"question_id": qid, "arm": arm, "reason": "not the pre-registered arm"})
        elif qid in void or (qid, arm) in arm_void or qid not in by_q:
            continue
        elif r.get("status") not in GRADABLE:
            excluded.append({"question_id": qid, "arm": arm, "reason": f"status {r.get('status')}"})
        else:
            items.append(r)
    rng = random.Random(seed)
    ids = _ids(rng, len(items))
    order = list(range(len(items)))
    rng.shuffle(order)
    sheet_items, key_items = [], {}
    for pos, i in enumerate(order):
        r = items[i]
        bid = ids[i]
        n = normalise.normalise(r)
        g = (gold.get("questions") or {}).get(r["question_id"]) or {}
        sheet_items.append({
            "position": pos + 1,
            "blind_id": bid,
            "question_id": r["question_id"],
            "question": by_q[r["question_id"]].text,
            "class": by_q[r["question_id"]].klass,
            "as_of": r.get("as_of"),
            "gold": {k: g.get(k) for k in ("gold_atoms", "error_atoms", "prior_state_note", "absence_proof")},
            "answer": n["text"],
            "receipts_axis_4": n["receipts"],
        })
        key_items[bid] = {"arm": r["arm"], "label": r.get("label"), "question_id": r["question_id"],
                          "cut": n["cut"]}
    sheet = {
        "schema": SHEET_SCHEMA,
        "design_sha256": dz.sha256,
        "seed": seed,
        "items": sheet_items,
        "void_questions": [{"question_id": q, "reason": why} for q, why in sorted(void.items())],
        "void_for_arm": [{"question_id": q, "arm": a, "reason": why}
                         for (q, a), why in sorted(arm_void.items())],
        "descriptive_only_classes": descriptive,
        "excluded": excluded,
    }
    key = {"schema": KEY_SCHEMA, "design_sha256": dz.sha256, "seed": seed, "items": key_items}
    return sheet, key


def build_tell_test(dz: Any, records: list[dict[str, Any]], *, seed: int,
                    exclude: set[tuple[str, str]] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """30 answers, 6 per arm A–E, stratified by class (round-robin over the four
    classes in the design's order: 2/2/1/1), seeded. ``exclude`` holds
    ``(question_id, arm)`` pairs never drawn (a re-test after a failed tell
    test leaves out the answers whose key was opened)."""
    rng = random.Random(seed)
    exclude = exclude or set()
    chosen: list[dict[str, Any]] = []
    for arm in TELL_ARMS:
        pool = {c: sorted((r for r in records if r.get("arm") == arm and r.get("class") == c
                           and r.get("status") in ("answered", "partial")
                           and (r["question_id"], r["arm"]) not in exclude
                           and not str(r.get("label") or "").endswith("*")),
                          key=lambda r: r["question_id"]) for c in CLASS_ORDER}
        for c in pool:
            rng.shuffle(pool[c])
        picked: list[dict[str, Any]] = []
        while len(picked) < TELL_PER_ARM and any(pool.values()):
            for c in CLASS_ORDER:
                if pool[c] and len(picked) < TELL_PER_ARM:
                    picked.append(pool[c].pop())
        if len(picked) < TELL_PER_ARM:
            raise SheetRefused(f"arm {arm}: only {len(picked)} gradable answers for the tell test (need 6)")
        chosen += picked
    ids = _ids(rng, len(chosen))
    order = list(range(len(chosen)))
    rng.shuffle(order)
    items = [{"blind_id": ids[i], "answer": normalise.normalise(chosen[i])["text"]} for i in order]
    key = {"schema": KEY_SCHEMA, "seed": seed, "purpose": "tell_test",
           "items": {ids[i]: {"arm": chosen[i]["arm"], "question_id": chosen[i]["question_id"],
                              "class": chosen[i]["class"]} for i in order}}
    test = {
        "schema": "legba.bakeoff.tell_test/1",
        "design_sha256": dz.sha256,
        "instruction": "For each answer, name the arm (A, B, C, D or E) that produced it.",
        "arms": [{"arm": a, "description": dz.arm_titles[a]} for a in TELL_ARMS],
        "items": items,
        "pass_if_accuracy_at_most": TELL_PASS_MAX,
    }
    return test, key


def tell_test_passes(predictions: dict[str, str], key: dict[str, Any]) -> tuple[bool, float]:
    """The pre-registered criterion: the grader names the arm in ≤ 35 % of answers."""
    items = key["items"]
    hits = sum(1 for bid, arm in predictions.items() if items.get(bid, {}).get("arm") == arm)
    acc = hits / len(items) if items else 1.0
    return acc <= TELL_PASS_MAX, round(acc, 4)


def served_model_id(entry: dict[str, Any]) -> str:
    """The model behind a gateway alias, as its ``/v1/models`` entry names it
    (the alias itself when the entry names nothing further)."""
    for k in _SERVED_KEYS:
        v = entry.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return str(entry["id"])


def _models_entry(listing: dict[str, Any], alias: str) -> dict[str, Any]:
    for item in listing.get("data") or []:
        if isinstance(item, dict) and item.get("id") == alias:
            return item
    raise SheetRefused(f"the gateway's /v1/models lists no {alias!r} — the grader route is not up")


def grader_pin(
    components: dict[str, dict[str, Any]], listings: dict[str, dict[str, Any]], *, probed_at: str,
) -> dict[str, Any]:
    """The pin written to ``run.json`` — pure. ``components`` maps component id
    → ``common.Registry.stack_component`` row; ``listings`` maps the same id →
    that component's ``/v1/models`` body."""
    out: dict[str, Any] = {"probed_at": probed_at, "second_rater": SECOND_RATER}
    for role, cid, alias in (("primary_grader", GRADER_COMPONENT, grader_alias()),
                             ("gold_drafter", GOLD_DRAFTER_COMPONENT, gold_drafter_alias())):
        comp = components[cid]
        model = str(comp.get("model_name") or "")
        if model != alias:
            raise SheetRefused(f"{cid} serves {model!r}, the design pins the alias {alias!r}")
        entry = _models_entry(listings[cid], alias)
        out[role] = {
            "component": cid,
            "component_version": comp.get("version"),
            "component_state": comp.get("state"),
            "api_endpoint": comp.get("api_endpoint"),
            "alias": alias,
            "served_model": served_model_id(entry),
            "models_entry_sha256": hashlib.sha256(normalise.dumps(entry).encode("utf-8")).hexdigest(),
        }
    return out


_PIN_FIELDS = ("component", "component_version", "alias", "served_model", "models_entry_sha256")


def pin_drift(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """What changed between two pins (empty = the route is the one pinned)."""
    out = []
    for role in ("primary_grader", "gold_drafter"):
        for f in _PIN_FIELDS:
            a, b = (old.get(role) or {}).get(f), (new.get(role) or {}).get(f)
            if a != b:
                out.append(f"{role}.{f}: {a!r} -> {b!r}")
    return out


def apply_pin(run: dict[str, Any], pin: dict[str, Any]) -> list[str]:
    """Write the first pin, or compare a re-probe against it. Returns the drift
    (and records it as ``grader_route_changed`` — the run is void, §4.4)."""
    old = run.get("grader_pin")
    if old is None:
        run["grader_pin"] = pin
        run["grader_model_served"] = pin["primary_grader"]["served_model"]
        return []
    drift = pin_drift(old, pin)
    run.setdefault("grader_rechecks", []).append({"probed_at": pin["probed_at"], "drift": drift})
    if drift:
        run["grader_route_changed"] = {"probed_at": pin["probed_at"], "drift": drift}
    return drift


def grading_refusal(run: dict[str, Any] | None) -> str | None:
    """Why a sheet / tell test must not be built for this run, or None."""
    if run is None:
        return None  # no run ledger (fixtures): nothing to check against
    if not run.get("grader_pin"):
        return "no grader pin in run.json — run `grade_sheet.py pin-grader` first"
    if run.get("grader_route_changed"):
        return f"the grader route changed mid-run — the run is void: {run['grader_route_changed']}"
    return None


async def recheck_pin(run_dir: Path, *, probe: Any = None) -> dict[str, Any]:
    """Before ANY grader or gold-drafter call: refuse an unpinned or void run,
    RE-PROBE the route, record the re-check in ``run.json``, and refuse on drift
    (a different served model, alias or component version voids the run, §4.4).
    Returns the run as written. ``probe`` is ``async (run_dir) -> pin`` (tests
    pass a fake; the default reads the registry and the gateway)."""
    run_path = Path(run_dir) / "run.json"
    if not run_path.is_file():
        raise SheetRefused(f"{run_path} does not exist — open the run (run_arm.py) first")
    run = json.loads(run_path.read_text(encoding="utf-8"))
    refusal = grading_refusal(run)
    if refusal:
        raise SheetRefused(refusal)
    pin = await (probe or _probe)(Path(run_dir))
    drift = apply_pin(run, pin)
    run_path.write_text(json.dumps(run, indent=1, sort_keys=True), encoding="utf-8")
    if drift:
        raise SheetRefused("GRADER ROUTE CHANGED — the run is void: " + "; ".join(drift))
    return run


async def _probe(run_dir: Path) -> dict[str, Any]:  # pragma: no cover — G1 only (registry + gateway)
    import httpx

    import common

    common.load_env()
    reg = common.Registry.from_env()
    components = {cid: reg.stack_component(cid) for cid in (GRADER_COMPONENT, GOLD_DRAFTER_COMPONENT)}
    listings: dict[str, dict[str, Any]] = {}
    async with common.readonly_conn() as conn:
        for cid, comp in components.items():
            key = await common.resolve_secret(conn, comp["api_key_ref"])
            base = str(comp["api_endpoint"]).rstrip("/")
            url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.get(url, headers={"Authorization": f"Bearer {key}"})
                r.raise_for_status()
                listings[cid] = r.json()
    return grader_pin(components, listings, probed_at=common.utcnow().isoformat())


def _write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normalise.dumps(obj), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="blind grading sheet / tell test / grader pin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pg = sub.add_parser("pin-grader", help="probe + pin (or re-check) the grader route in run.json")
    pg.add_argument("--run-dir", required=True)
    ts = sub.add_parser("tell-score", help="score the grader's tell-test answers against the sealed key")
    ts.add_argument("--key", required=True, help="the sealed tell-test key (tell-test --key-out)")
    ts.add_argument("--predictions", required=True, help='JSON {"<blind_id>": "<arm A-E>", ...}')
    ts.add_argument("--run-dir", help="records the result in <run-dir>/run.json")
    for name in ("sheet", "tell-test"):
        s = sub.add_parser(name)
        s.add_argument("--design", required=True)
        s.add_argument("--run-dir", required=True)
        s.add_argument("--seed", type=int, required=True)
        s.add_argument("--out", required=True, help="directory for the grader-facing file")
        s.add_argument("--key-out", required=True, help="sealed key path, OUTSIDE --out")
        if name == "sheet":
            s.add_argument("--gold", required=True)
            s.add_argument("--practice", action="store_true")
        else:
            s.add_argument("--exclude-key", action="append", default=[],
                           help="an earlier tell key: its (question, arm) answers are not drawn again")
    a = ap.parse_args(argv)
    if a.cmd == "tell-score":
        key = json.loads(Path(a.key).read_text(encoding="utf-8"))
        preds = json.loads(Path(a.predictions).read_text(encoding="utf-8"))
        ok, acc = tell_test_passes(preds, key)
        if a.run_dir:
            run_path = Path(a.run_dir) / "run.json"
            run = json.loads(run_path.read_text(encoding="utf-8"))
            run.setdefault("tell_tests", []).append({"accuracy": acc, "passed": ok, "seed": key.get("seed")})
            run_path.write_text(json.dumps(run, indent=1, sort_keys=True), encoding="utf-8")
        print(f"tell test: accuracy {acc:.2%} — {'PASS' if ok else 'FAIL: revise the normaliser; no grading'}")
        return 0 if ok else 3
    if a.cmd == "pin-grader":
        import asyncio

        run_path = Path(a.run_dir) / "run.json"
        if not run_path.is_file():
            raise SheetRefused(f"{run_path} does not exist — open the run (run_arm.py) first")
        run = json.loads(run_path.read_text(encoding="utf-8"))
        pin = asyncio.run(_probe(Path(a.run_dir)))
        drift = apply_pin(run, pin)
        run_path.write_text(json.dumps(run, indent=1, sort_keys=True), encoding="utf-8")
        if drift:
            print("GRADER ROUTE CHANGED — the run is void:\n  " + "\n  ".join(drift))
            return 2
        print(f"grader pinned: {GRADER_COMPONENT} alias {grader_alias()} serves "
              f"{run['grader_model_served']}; gold drafter {pin['gold_drafter']['served_model']}; "
              f"second rater {SECOND_RATER}")
        return 0
    dz = design_mod.load_design(a.design)
    records = load_records(Path(a.run_dir))
    run_file = Path(a.run_dir) / "run.json"
    refusal = grading_refusal(json.loads(run_file.read_text(encoding="utf-8")) if run_file.is_file() else None)
    if refusal and not getattr(a, "practice", False):
        raise SheetRefused(refusal)
    out = Path(a.out)
    if a.cmd == "sheet":
        gold = json.loads(Path(a.gold).read_text(encoding="utf-8"))
        obj, key = build_sheet(dz, records, gold, seed=a.seed, practice=a.practice)
        target = out / "sheet.json"
    else:
        exclude = {(v["question_id"], v["arm"]) for k in a.exclude_key
                   for v in json.loads(Path(k).read_text(encoding="utf-8"))["items"].values()}
        obj, key = build_tell_test(dz, records, seed=a.seed, exclude=exclude)
        if exclude:
            key["excluded_from"] = sorted(a.exclude_key)
        target = out / "tell_test.json"
    key_sha = _seal(key, Path(a.key_out), out)
    _write(target, obj)
    run_path = Path(a.run_dir) / "run.json"
    if run_path.is_file():
        run = json.loads(run_path.read_text(encoding="utf-8"))
        run.setdefault("blind_keys", []).append({"purpose": a.cmd, "sha256": key_sha, "seed": a.seed})
        run_path.write_text(json.dumps(run, indent=1, sort_keys=True), encoding="utf-8")
    print(f"wrote {target} ({len(obj['items'])} items); key sealed sha256={key_sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
