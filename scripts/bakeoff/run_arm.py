#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run arms A, A-r, B and C of the bake-off — one JSON record per (question, arm).

planning/reviews/BAKEOFF_DESIGN.md v1.1 (re-stamped 2026-11-05; ``design.load_design``
checks its hash) is the specification. This script BUILDS each arm's answer and
records it; it never grades, and it spends money only through the paid gate
(``common.PaidGate``): arms B and C build their actor request in full and then
refuse with ``status: dry_run`` unless the operator passes ``--execute``,
``--run-ceiling-usd`` and a verified ``--prices`` file. Arms D and E have their
own harnesses (``arm_d.py``, ``arm_e.py``) and the same gate.

THE ARMS HERE
  A    the desk brief as served (``packet_a``) — no model, $0.
  A-r  the same packet handed to the core plane (``llm.primary.openai_compat``,
       endpoint + model read off the live stack component, key from the vault
       row it names) with the design's fixed instruction — $0 marginal.
  B    the consult ACTOR ``analyst::consult_bakeoff_raw::<ver[:16]>`` invoked
       directly on the Dapr sidecar (never the registry front door), mode chat,
       10 rounds, no pins, no case, no writes, plane override
       ``llm.anthropic.opus_4_7``, and the ``bakeoff_meter`` that arms the
       per-question HARD STOP (B $5, C $3 — read from the design's §7 table).
  C    the same over ``analyst::consult_bakeoff_verified::…``.

THE COST STOP, END TO END
  1. Run level: ``run.json`` holds the operator's ceiling and the money spent;
     a question whose hard stop would cross the ceiling is refused
     ``run_ceiling`` and named (never started).
  2. Question level: the request row's ``bakeoff_meter`` makes the consult
     loop meter provider-reported usage (cache reads/writes included) at the
     VERIFIED prices and stop drilling while one more round plus the forced
     synthesis still fits under the stop (``consult_bakeoff.BakeoffSpendGuard``).
     The partial is delivered (status ``partial``), never discarded.
  3. Ledger: the record's cost is the meter's ``verified_usd``; when the actor
     fails and no meter comes back, the question is charged its FULL hard stop
     (the money may have been spent and the harness cannot see it).
  4. A CUT call (W11-F132/F133: a round or the synthesis cut by its deadline,
     a dropped stream, a provider error) delivers the text it had written as
     the answer (``synthesis_status: partial``) — recorded ``partial``, never an
     error — but its usage never reached the meter. Each cut the trace names is
     charged the meter's own worst-case next-call projection
     (``next_call_worst_usd``), capped at the hard stop
     (:func:`consult_record_from_result`).

THE CLOCKS (rolls 55–57). The consult loop now runs up to
``LEGBA_CONSULT_BUDGET_SECONDS`` (3600) with a 900 s synthesis floor, and the
registry's own invoke timeout is derived ≥ 4800 s; the harness waits as long
(:data:`INVOKE_TIMEOUT_S`) so the loop's partial delivery, never the harness's
clock, ends a long run. The bake-off descriptors carry
``method.retry.transient.max_attempts: 1``: the actor never re-runs (re-bills)
a question.

G1 — WHAT THE OPERATOR DOES (this lane never does any of it)
  1. Verify the prices against the invoice; write the prices file
     (done 2026-11-05: ``planning/reviews/bakeoff_prices_2026-11-05.json``)
     (``{"verified_against_invoice": true, "verified_by": "...",
     "models": {"<model_name the opus_4_7 component serves>": {"input": ..,
     "output": .., "cache_read": .., "cache_write": ..}},
     "web_search_per_1k_usd": ..}``).
  2. Register + activate, through the audited lifecycle, the DRAFT descriptors
     ``action_pack_substrate_read_raw.yaml``, ``action_pack_substrate_read_verified.yaml``,
     ``analyst_consult_bakeoff_raw.yaml``, ``analyst_consult_bakeoff_verified.yaml``
     (POST /api/v1/registry/descriptors/<family>, then transition to active),
     on a runtime rolled with this lane's code.
  3. Freeze T and run A and A-r for the 36 (free), then B, C (this script),
     D (``arm_d.py``) and E (``arm_e.py``) with ``--execute --run-ceiling-usd
     <N> --prices <file>`` — all inside the 12-hour slot from T (§2, arm D).
  4. Build gold at T+72h (operator-signed ``gold.json``), then
     ``grade_sheet.py tell-test`` → the tell test (§5.4) → ``grade_sheet.py
     sheet`` → grading → adjudication → open the sealed key.
  5. Pause the four descriptors (run 2 resumes them).

  The exact command lines, in order: ``scripts/bakeoff/README.md``.

USAGE
    python3 scripts/bakeoff/run_arm.py --design <planning/reviews/BAKEOFF_DESIGN.md> \\
        --as-of 2026-10-09T12:00:00Z --out <dir> --arm A --question HE04
    python3 scripts/bakeoff/run_arm.py ... --arm B --all      # dry run: refuses at the call
    python3 scripts/bakeoff/run_arm.py ... --arm B --all --execute \\
        --run-ceiling-usd 110 --prices planning/reviews/bakeoff_prices_2026-11-05.json   # G1 only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import design as design_mod  # noqa: E402
import packet_a  # noqa: E402

ARMS_HERE = ("A", "A-r", "B", "C")
CONSULT_ARMS = {
    "B": {"analyst_id": "consult_bakeoff_raw", "pack_id": "substrate_read_raw"},
    "C": {"analyst_id": "consult_bakeoff_verified", "pack_id": "substrate_read_verified"},
}
FRONTIER_COMPONENT = common.FRONTIER_COMPONENT  # the id is historical; it serves common.FRONTIER_MODEL
CORE_COMPONENT = "llm.primary.openai_compat"
CONSULT_ROUNDS = 10
ACTOR_TYPE = "AnalystActor"
#: The harness's read timeout on the actor invoke. ≥ the registry's derived
#: floor (``consult_runs.invoke_timeout_seconds``: total budget 3600 + final
#: floor 900 + 300 margin = 4800 s on the live clocks): a 10-round Opus consult
#: plus its synthesis can run well past the 900 s this was at roll 46.
INVOKE_TIMEOUT_S = 4800.0
#: Trace step kinds that mark a model call whose usage never reached the meter
#: (the call was cancelled or died mid-stream). ``degraded_final`` counts only
#: for a synthesis cut by its deadline — ``no_budget_for_synthesis`` made no call.
UNMETERED_CUT_KINDS = ("partial_final", "round_deadline_exceeded", "round_stream_interrupted")
_PARTIAL_BANNER = "> **Synthesis interrupted"
_PARTIAL_BANNER_END = "\n\n---\n\n"
#: ``Confidence: 0.xx`` as models actually write it — bare, bold (``**Confidence:** 0.97``),
#: bulleted or quoted. The last such line wins.
_CONF = re.compile(r"(?im)^[ \t*_>#-]*confidence[ \t*_]*:[ \t*_]*([01](?:\.\d+)?)[ \t*_]*$")


# ---------------------------------------------------------------------------
# Questions (the 36, or a practice file outside them)
# ---------------------------------------------------------------------------


def load_practice(path: Path) -> list[design_mod.Question]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for r in rows:
        if not re.fullmatch(r"P\d{2}", r["qid"]):
            raise SystemExit(f"practice question ids are P01.. (got {r['qid']!r}): never one of the 36")
        out.append(design_mod.Question(r["qid"], r["class"], r["desk"], r["window"], r["gold"], r["text"],
                                       tuple(r.get("anchors") or ())))
    return out


def stated_confidence_line(text: str) -> float | None:
    found = _CONF.findall(text or "")
    return float(found[-1]) if found else None


# ---------------------------------------------------------------------------
# Pre-run void checks (design §3.5 — the parts checkable before gold exists)
# ---------------------------------------------------------------------------


async def precheck(conn: Any, reg: common.Registry, q: design_mod.Question) -> str | None:
    """A reason the question cannot run, or None. A missing GOLD SOURCE the
    harness can see before T+72h is a G-CORPUS anchor that is not in the corpus."""
    if q.anchors:
        found = await conn.fetch("SELECT id::text AS id FROM signals WHERE id = ANY($1::uuid[])", list(q.anchors))
        missing = sorted(set(q.anchors) - {r["id"] for r in found})
        if missing:
            return f"gold_missing: G-CORPUS anchor signal(s) {missing} not in the corpus"
    head = reg.descriptor_head("target", q.desk)
    if head is None:
        return f"desk_missing: {q.desk} is not a registered target"
    return None


def desk_display_name(reg: common.Registry, desk: str) -> str | None:
    head = reg.descriptor_head("target", desk) or {}
    return head.get("name") or (head.get("body") or {}).get("identity", {}).get("name")


async def resolve_receipts(conn: Any, refs: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Every ref → its SOURCE documents (findings expanded to their signals)."""
    finding_ids = [r["ref"] for r in refs if r["kind"] == "finding"]
    expanded = await common.expand_findings(conn, finding_ids) if finding_ids else {}
    all_signals = [r["ref"] for r in refs if r["kind"] == "signal"] + [
        s for sigs in expanded.values() for s in sigs
    ]
    docs = await common.resolve_signals(conn, all_signals)
    out = []
    for r in refs:
        sigs = [r["ref"]] if r["kind"] == "signal" else expanded.get(r["ref"], [])
        out.append({"ref": r["ref"], "kind": r["kind"], "documents": [docs[s] for s in sigs if s in docs]})
    return out


async def classify_refs(conn: Any, ids: list[str]) -> list[dict[str, str]]:
    ids = [i for i in dict.fromkeys(ids) if re.fullmatch(r"[0-9a-f-]{36}", i)]
    if not ids:
        return []
    sig = {r["id"] for r in await conn.fetch("SELECT id::text AS id FROM signals WHERE id = ANY($1::uuid[])", ids)}
    fnd = {r["id"] for r in await conn.fetch(
        "SELECT id::text AS id FROM analyst_outputs WHERE id = ANY($1::uuid[])", ids)}
    return [{"ref": i, "kind": "signal" if i in sig else "finding" if i in fnd else "unknown"} for i in ids]


# ---------------------------------------------------------------------------
# Arm A / A-r
# ---------------------------------------------------------------------------


async def build_packet(conn: Any, reg: common.Registry, q: design_mod.Question) -> dict[str, Any]:
    members: list[str] = []
    if q.desk.startswith("situation_"):
        sys.path.insert(0, str(common.REPO_ROOT / "src"))
        from legba.runtime.target_resolution import resolve_member_targets

        res = await resolve_member_targets(conn, q.desk)
        members = [str(m) for m in (res.get("members") or [])]
    briefs = [packet_a.desk_brief(reg, d) for d in packet_a.scope_desks(q.desk, members)]
    text, segments, refs = packet_a.render(briefs)
    comp = next((b for b in briefs if b.get("composition_id")), None)
    return {
        "desks": [b["desk"] for b in briefs],
        "members": members,
        "text": text,
        "segments": segments,
        "refs": refs,
        "composition_id": comp["composition_id"] if comp else None,
        "composition_confidence": comp["composition_confidence"] if comp else None,
        "export_refused_detail": [
            {"desk": b["desk"], "detail": (b.get("export") or {})["export_refused"]}
            for b in briefs
            if (b.get("export") or {}).get("export_refused")
            and not (b.get("export") or {})["export_refused"].startswith("no basket items")
        ],
        "missing_units": {b["desk"]: b["missing_units"] for b in briefs if b["missing_units"]},
    }


async def arm_a(conn: Any, reg: common.Registry, q: Any, rec: dict[str, Any]) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        pkt = await build_packet(conn, reg, q)
    except packet_a.PacketNotBuilt as exc:
        rec.update(status="refused", refusal={"reason": f"not_built: {exc}"})
        return rec
    rec["packet"] = {k: v for k, v in pkt.items() if k != "text"}
    rec["raw_answer"] = pkt["text"]
    refused = [b for b in pkt["export_refused_detail"] if b["desk"] == q.desk]
    if refused:
        rec.update(status="refused", refusal={
            "reason": f"void_for_A: the export refused {q.desk}'s brief at T — {refused[0]['detail']}"})
    elif not pkt["text"].strip():
        # §3.5: a desk whose brief is refused at T is void for A only, and the
        # refusal is published as A's result for that row.
        rec.update(status="refused", refusal={"reason": "void_for_A: the brief is empty at T"})
    else:
        rec["status"] = "answered"
    if pkt["composition_confidence"] is not None:
        rec["stated_confidence"] = float(pkt["composition_confidence"])
        rec["confidence_source"] = "composition"
    else:
        rec["confidence_source"] = "top_sentence_unit"  # resolved by normalise.py (N6's top)
    rec["receipts"] = await resolve_receipts(conn, pkt["refs"])
    rec["elapsed_s"] = round(time.monotonic() - t0, 2)
    rec["cost"].update(usd=0.0, priced=False)
    return rec


def ar_prompt(dz: design_mod.Design, q: Any, as_of: datetime, desks: list[str], packet: str) -> str:
    return (
        f"{dz.prompt_ar}\n\nAs of: {as_of.isoformat()}. Desk scope: {', '.join(desks)}.\n\n"
        f"Question: {q.text}\n\nPacket:\n{packet}"
    )


async def arm_ar(
    conn: Any, reg: common.Registry, dz: design_mod.Design, q: Any, rec: dict[str, Any],
    a_record: dict[str, Any] | None, as_of: datetime, *, call: bool,
) -> dict[str, Any]:
    import httpx

    if a_record is None or a_record.get("status") != "answered":
        rec.update(status="refused", refusal={"reason": "no answered arm-A packet for this question in --out"})
        return rec
    comp = reg.stack_component(CORE_COMPONENT)
    prompt = ar_prompt(dz, q, as_of, (a_record.get("packet") or {}).get("desks") or [q.desk], a_record["raw_answer"])
    body = {"model": comp["model_name"], "temperature": 1.0, "messages": [{"role": "user", "content": prompt}]}
    rec["model"] = {"component": CORE_COMPONENT, "model_name": comp["model_name"], "version": comp["version"]}
    rec["request"] = {"endpoint": common.chat_url(comp["api_endpoint"]),
                      "body_sha256": common.hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
                      "prompt_chars": len(prompt)}
    rec["provenance"]["prompt_sha256"] = common.hashlib.sha256(dz.prompt_ar.encode()).hexdigest()
    rec["receipts"] = a_record.get("receipts") or []
    rec["packet"] = {"from_arm_a": a_record["question_id"], "segments": []}
    rec["cost"].update(usd=0.0, priced=False)
    if not call:
        rec.update(status="dry_run", refusal={"reason": "dry_run: --no-core-call"})
        return rec
    key = await common.resolve_secret(conn, comp["api_key_ref"])
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=600.0) as client:
        r = await client.post(rec["request"]["endpoint"], json=body, headers={"Authorization": f"Bearer {key}"})
        r.raise_for_status()
        out = r.json()
    rec["elapsed_s"] = round(time.monotonic() - t0, 2)
    text = (out.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    usage = out.get("usage") or {}
    rec["usage"].update(input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"), calls=1)
    rec["raw_answer"] = text
    rec["stated_confidence"] = stated_confidence_line(text)
    rec["confidence_source"] = "confidence_line"
    rec["status"] = "answered" if text.strip() else "error"
    return rec


# ---------------------------------------------------------------------------
# Arms B / C — the consult actor
# ---------------------------------------------------------------------------


def consult_question(q: Any, as_of: datetime, desks: list[str]) -> str:
    """Design §2.1: the question text verbatim, then the as-of and scope line."""
    return f"{q.text}\n\nAs of: {as_of.isoformat()}. Desk scope: {', '.join(desks)}."


def consult_invoke(
    arm: str, q: Any, as_of: datetime, *, hard_stop: float, price_per_m: dict[str, float] | None,
) -> dict[str, Any]:
    first = {
        "question": consult_question(q, as_of, [q.desk]),
        # The design names the desk scope here. NOTE (report A-F): at the pin
        # search_signals accepts the predicate and does not apply it — the scope
        # reaches the model through the question's scope line.
        "scope_predicate": f'target_id == "{q.desk}"',
        "max_tool_rounds": CONSULT_ROUNDS,
        "mode": "chat",
        "request_id": str(uuid4()),
        "messages": [],
        "llm_component_override": FRONTIER_COMPONENT,
        "bakeoff_meter": {"hard_stop_usd": hard_stop, "price_per_m": price_per_m},
    }
    return {"trigger_kind": "method", "inputs": [first]}


def unmetered_cuts(steps: list[dict[str, Any]]) -> int:
    """How many model calls in the trace were cut before their usage was metered."""
    n = 0
    for st in steps or []:
        kind = st.get("kind") if isinstance(st, dict) else None
        if kind in UNMETERED_CUT_KINDS:
            n += 1
        elif kind == "degraded_final" and st.get("reason") == "synthesis_deadline_exceeded":
            n += 1
    return n


def split_partial_banner(answer: str) -> tuple[str, str | None]:
    """``(answer text, banner)`` — a cut run's answer leads with the consult
    plane's "Synthesis interrupted" banner (``partial_final_payload``). It is
    platform narration, not the model's answer, and a machine tell: the record
    keeps it apart (``partial_banner``) and the model's own text is the answer."""
    if answer.startswith(_PARTIAL_BANNER) and _PARTIAL_BANNER_END in answer:
        banner, _, text = answer.partition(_PARTIAL_BANNER_END)
        return text, banner
    return answer, None


def consult_record_from_result(
    rec: dict[str, Any], *, status_code: int, result: dict[str, Any], text: str, hard_stop: float,
) -> tuple[float, list[str]]:
    """Fill a B/C record from the actor's run envelope. Returns ``(usd charged,
    cited refs to resolve)``. Pure — the paid call and the receipt resolution
    stay in :func:`arm_consult`, so the cases below are testable with fakes.

    * a failed invoke / a non-success outcome → ``error``, the FULL hard stop;
    * a success with ``synthesis_status`` complete and no forced final →
      ``answered``; anything else (a forced final, a cut delivered as the
      partial, a degraded final) → ``partial`` with its text;
    * the charge is the meter's ``verified_usd`` plus, per cut call the trace
      names, the meter's ``next_call_worst_usd``, capped at the hard stop (but
      never below what the meter saw). No meter → the hard stop.
    """
    if status_code >= 400 or result.get("outcome") != "success":
        rec.update(status="error", refusal={"reason": f"actor: HTTP {status_code} {str(result or text)[:500]}"})
        return hard_stop, []
    cr = result.get("consult_response") or {}
    data = cr.get("data") or {}
    meter = (data.get("usage") or {}).get("bakeoff_meter") or {}
    answer, banner = split_partial_banner(cr.get("answer") or "")
    rec["raw_answer"] = answer
    if banner is not None:
        rec["partial_banner"] = banner
    unc = cr.get("uncertainty")
    rec["stated_confidence"] = round(1.0 - float(unc), 4) if unc is not None else None
    rec["confidence_source"] = "one_minus_uncertainty"
    rec["unanswered_aspects"] = list(cr.get("unanswered_aspects") or [])
    rec["synthesis_status"] = data.get("synthesis_status")
    rec["usage"].update(
        input_tokens=meter.get("input_tokens"), output_tokens=meter.get("output_tokens"),
        cache_read_tokens=meter.get("cache_read_tokens"), cache_write_tokens=meter.get("cache_write_tokens"),
        calls=meter.get("calls"), tool_rounds=data.get("rounds_used"),
    )
    charged = hard_stop
    if "verified_usd" in meter:
        verified = float(meter["verified_usd"])
        cuts = unmetered_cuts(data.get("steps") or [])
        estimate = cuts * float(meter.get("next_call_worst_usd") or 0.0)
        charged = verified + estimate
        if charged > hard_stop:
            charged = max(verified, hard_stop)
        rec["cost"]["verified_usd"] = round(verified, 6)
        rec["cost"]["unmetered_cuts"] = cuts
        rec["cost"]["unmetered_cut_estimate_usd"] = round(charged - verified, 6)
        rec["cost"]["hard_stop_breached"] = bool(meter.get("hard_stop_breached")) or verified > hard_stop
    else:
        rec["cost"]["meter_missing"] = True
    complete = data.get("synthesis_status") == "complete" and not data.get("forced_final")
    rec["status"] = "answered" if complete else "partial"
    refs = [str(x) for x in cr.get("cited_substrate_refs") or []]
    return charged, refs


async def arm_consult(
    conn: Any, reg: common.Registry, dz: design_mod.Design, q: Any, rec: dict[str, Any],
    run: dict[str, Any], gate: common.PaidGate, out: Path, as_of: datetime,
) -> dict[str, Any]:
    import httpx

    sys.path.insert(0, str(common.REPO_ROOT / "src"))
    from legba.data.analysts.consult_bakeoff import BAKEOFF_READ_PACKS

    arm = rec["arm"]
    spec = CONSULT_ARMS[arm]
    hard_stop = dz.hard_stops[arm]
    variant = BAKEOFF_READ_PACKS[spec["pack_id"]]
    rec["provenance"]["prompt_sha256"] = common.hashlib.sha256(variant.system_prompt.encode()).hexdigest()
    rec["cost"]["hard_stop_usd"] = hard_stop
    frontier = reg.stack_component(FRONTIER_COMPONENT)
    rec["model"] = {"component": FRONTIER_COMPONENT, "model_name": frontier["model_name"],
                    "version": frontier["version"], "pinned_model": common.FRONTIER_MODEL}
    prices = None
    if gate.prices_path is not None:
        prices = common.Prices.load(gate.prices_path, frontier["model_name"])
        rec["cost"]["price_per_m"] = prices.per_m()
    head = reg.descriptor_head("analyst", spec["analyst_id"])
    version = (head or {}).get("version") or ""
    actor_id = f"analyst::{spec['analyst_id']}::{version[:16] or '<unregistered>'}"
    grants = [p.get("pack_id") for p in ((head or {}).get("body") or {}).get("action_packs") or []]
    if head is not None and grants != [spec["pack_id"]]:
        rec["label"] = f"{arm}*"  # not the pre-registered arm: excluded from the primary analysis
    sidecar = os.environ.get("LEGBA_BAKEOFF_DAPR", "http://127.0.0.1:3500")
    invoke_url = f"{sidecar}/v1.0/actors/{ACTOR_TYPE}/{actor_id}/method/run"
    body = consult_invoke(arm, q, as_of, hard_stop=hard_stop, price_per_m=prices.per_m() if prices else None)
    rec["request"] = {"actor_id": actor_id, "invoke_url": invoke_url, "body": body,
                      "descriptor_state": (head or {}).get("state"), "descriptor_grants": grants}

    refusal = gate.refusal()
    if refusal is None:
        refusal = common.frontier_refusal(frontier)
    if refusal is None and head is None:
        refusal = f"unregistered: {spec['analyst_id']} is not registered (G1 step 2)"
    if refusal is None and (head or {}).get("state") != "active":
        refusal = f"inactive: {spec['analyst_id']} is {(head or {}).get('state')!r}, not active (G1 step 2)"
    if refusal is None:
        refusal = gate.admits(run, hard_stop)
        if refusal is not None:
            run["questions_refused_at_ceiling"].append(f"{q.qid}/{arm}")
    if refusal is not None:
        rec.update(status="dry_run" if refusal.startswith("dry_run") else "refused",
                   refusal={"reason": refusal})
        return rec

    # ---- the paid call (G1 only) -------------------------------------
    t0 = time.monotonic()
    charged = hard_stop  # charged in full unless a meter comes back
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(INVOKE_TIMEOUT_S, connect=10.0)) as client:
            r = await client.put(invoke_url, json=body)
        rec["elapsed_s"] = round(time.monotonic() - t0, 2)
        result = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        charged, refs = consult_record_from_result(
            rec, status_code=r.status_code, result=result, text=r.text, hard_stop=hard_stop,
        )
        if refs:
            rec["receipts"] = await resolve_receipts(conn, await classify_refs(conn, refs))
    except Exception as exc:  # noqa: BLE001 — recorded, and the stop is charged
        rec.update(status="error", refusal={"reason": f"invoke failed: {type(exc).__name__}: {exc}"})
    rec["cost"]["usd"] = round(charged, 6)
    run["spent_usd"] = round(float(run.get("spent_usd") or 0.0) + charged, 6)
    common.save_run(out, run)
    return rec


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main_async(a: argparse.Namespace) -> int:
    common.load_env()
    dz = design_mod.load_design(a.design)
    as_of = common.parse_as_of(a.as_of)
    out = Path(a.out)
    run = common.open_run(out, as_of=as_of, design=dz, ceiling=a.run_ceiling_usd)
    gate = common.PaidGate(execute=a.execute, run_ceiling_usd=run.get("run_ceiling_usd"),
                           prices_path=Path(a.prices) if a.prices else None)
    reg = common.Registry.from_env()
    if a.practice:
        questions = load_practice(Path(a.practice))
        if a.question:
            questions = [q for q in questions if q.qid in a.question]
    elif a.all:
        questions = list(dz.questions)
    else:
        questions = [dz.question(x) for x in a.question or []]
    if not questions:
        raise SystemExit("no questions selected (--question QID ..., --all, or --practice FILE)")
    async with common.readonly_conn() as conn:
        for q in questions:
            for arm in a.arm:
                if common.existing_spent(out, q.qid, arm):
                    print(f"{q.qid} {arm}: already spent — never re-run (delete the record to override)")
                    continue
                rec = common.new_record(run=run, question=q, arm=arm, as_of=as_of)
                rec["desk_display_name"] = desk_display_name(reg, q.desk)
                reason = await precheck(conn, reg, q)
                if reason:
                    rec.update(status="refused", refusal={"reason": reason})
                elif arm == "A":
                    rec = await arm_a(conn, reg, q, rec)
                elif arm == "A-r":
                    a_path = common.record_path(out, q.qid, "A")
                    a_rec = json.loads(a_path.read_text()) if a_path.is_file() else None
                    rec = await arm_ar(conn, reg, dz, q, rec, a_rec, as_of, call=not a.no_core_call)
                else:
                    rec = await arm_consult(conn, reg, dz, q, rec, run, gate, out, as_of)
                path = common.write_record(out, rec)
                print(f"{q.qid} {arm:<3} {rec['status']:<8} {(rec.get('refusal') or {}).get('reason', '')[:110]}  -> {path}")
    common.save_run(out, run)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="bake-off arms A, A-r, B, C")
    ap.add_argument("--design", required=True)
    ap.add_argument("--as-of", required=True, help="the run's T, ISO-8601 with timezone")
    ap.add_argument("--out", required=True)
    ap.add_argument("--arm", nargs="+", choices=ARMS_HERE, required=True)
    ap.add_argument("--question", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--practice", help="a practice-question file (P01..), never one of the 36")
    ap.add_argument("--execute", action="store_true", help="G1 ONLY: make the paid calls")
    ap.add_argument("--run-ceiling-usd", type=float)
    ap.add_argument("--prices")
    ap.add_argument("--no-core-call", action="store_true", help="A-r: build the request, skip the core call")
    return ap.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async(parse_args())))
