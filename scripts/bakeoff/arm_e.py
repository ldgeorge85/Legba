#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Arm E — deterministic change detection + one frontier read (design §2, arm E).

CHANGE DETECTION (no model). The indicator flips come from the DEBOUNCE lane's
one reader, ``debounced_flips(conn, target_id, since, *, until, limit)``
(``legba.data.analysts.deterministic_handlers.indicator_tracker``, table
``indicator_flips``, migration 0237): debounced rows only, oldest first, cut at
T (``until``) so nothing confirmed after the as-of leaks in. This harness does
NOT build a debounce. It applies the design's two pre-registered rules ON TOP
of the reader, because the reader's hysteresis confirms a change that wobbled
first (``N N T N T T`` confirms N→T) and the design does not:

  * a CLEAN change is a debounced row with ``raw_flips_between == 1`` —
    ``activated`` (→ triggered) or ``cleared`` (triggered →); a row that took
    more toggles to settle is reported as ``wobbled``, not as a change;
  * a (desk, unit, indicator) pair with MORE THAN 3 raw toggles in the window
    is ``unstable`` and none of its rows is a change. The toggles are counted
    from the raw-grade rows of the same table (``indicator_flips.grade='raw'``,
    the audit trail the debounce keeps), read-only, in the same window.

``backfill`` rows (found on a stream's first sweep, from history) are kept for
a past window and marked ``history`` — never presented as new.

THE PACKET: the change list (statement, desk, unit, first and last run dates,
direction); for each change the text of the signals its source findings cite
(onset + confirming run → their ``citations[].signal_id``); and the desk's
indicator roster with "no change" for every indicator that did not move.

INTERPRETATION: one Messages API call, no tools, the design's fixed prompt,
the as-of + scope lines. HARD STOP $1 (design §7) by construction: the call's
``max_tokens`` is sized so that the input (estimated pessimistically at 1 token
per 3 characters) plus the full output cap cannot cross the stop; if fewer than
4,000 output tokens fit, the question is refused rather than squeezed.

The G0 lane runs this as a DRY RUN only (fixtures); a paid call needs the gate
(``--execute --run-ceiling-usd --prices``) exactly as ``run_arm.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import design as design_mod  # noqa: E402
import messages_api  # noqa: E402

FRONTIER_COMPONENT = common.FRONTIER_COMPONENT  # the id is historical; it serves common.FRONTIER_MODEL
UNSTABLE_TOGGLES = 3          # design: "more than 3 times in the window"
CLEAN_RAW_FLIPS = 1           # coordinator/design: a clean change settled in one toggle
RECEIPT_CHARS = 1500          # per cited signal in the packet
MIN_OUTPUT_TOKENS = 4000
MAX_OUTPUT_TOKENS = 32768
CHARS_PER_TOKEN_PESSIMISTIC = 3.0

FlipReader = Callable[..., Awaitable[list[dict[str, Any]]]]

_RAW_TOGGLES_SQL = """
SELECT target_id, source_analyst_id, indicator_id, count(*) AS toggles
  FROM indicator_flips
 WHERE grade = 'raw'
   AND ($1::text IS NULL OR target_id = $1)
   AND evidence_at > $2 AND evidence_at <= $3
 GROUP BY 1, 2, 3
"""


def default_reader() -> FlipReader:
    """The debounce lane's reader. Fails LOUD when this tree does not carry it
    (it lands with branch x3-debounce / migration 0237)."""
    sys.path.insert(0, str(common.REPO_ROOT / "src"))
    try:
        from legba.data.analysts.deterministic_handlers.indicator_tracker import debounced_flips
    except ImportError as exc:  # pragma: no cover — depends on the merged tree
        raise RuntimeError(
            "debounced_flips is not in this tree: arm E reads the DEBOUNCE lane's "
            "reader (x3-debounce, migration 0237) and builds no debounce of its own"
        ) from exc
    return debounced_flips


def _key(r: dict[str, Any]) -> tuple[str, str, str]:
    return (str(r.get("target_id")), str(r.get("source_analyst_id")), str(r.get("indicator_id")))


def classify(
    rows: list[dict[str, Any]], raw_toggles: dict[tuple[str, str, str], int],
) -> list[dict[str, Any]]:
    """The design's rules over the reader's debounced rows (pure; tested)."""
    out = []
    for r in rows:
        k = _key(r)
        toggles = raw_toggles.get(k, int(r.get("raw_flips_between") or 0))
        if toggles > UNSTABLE_TOGGLES:
            kind = "unstable"
        elif int(r.get("raw_flips_between") or 0) != CLEAN_RAW_FLIPS:
            kind = "wobbled"
        elif r.get("to_status") == "triggered":
            kind = "activated"
        elif r.get("from_status") == "triggered":
            kind = "cleared"
        else:
            kind = "changed"
        out.append({
            "desk": r.get("target_id"),
            "unit": r.get("source_analyst_id"),
            "indicator_id": r.get("indicator_id"),
            "statement": r.get("statement"),
            "from_status": r.get("from_status"),
            "to_status": r.get("to_status"),
            "first_run": _iso(r.get("onset_at")),
            "last_run": _iso(r.get("confirmed_at")),
            "classification": kind,
            "history": bool(r.get("backfill")),
            "raw_toggles_in_window": toggles,
            "finding_ids": [x for x in (r.get("onset_finding_id"), r.get("confirmed_finding_id")) if x],
        })
    return out


def _iso(v: Any) -> str | None:
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)


async def roster(conn: Any, desk: str, as_of: datetime) -> list[dict[str, Any]]:
    """Every indicator the desk's units carried in their latest run at T."""
    sys.path.insert(0, str(common.REPO_ROOT / "src"))
    from legba.data.analysts.deterministic_handlers.indicator_tracker import _extract_indicators

    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (analyst_id) id::text AS id, analyst_id, data
          FROM analyst_outputs
         WHERE kind = 'finding' AND target_id = $1 AND produced_at <= $2
         ORDER BY analyst_id, produced_at DESC, id DESC
        """,
        desk, as_of,
    )
    out = []
    for r in rows:
        for ind in _extract_indicators({"data": r["data"]}):
            out.append({"desk": desk, "unit": r["analyst_id"], "indicator_id": ind.get("id"),
                        "statement": ind.get("statement"), "status": ind.get("status")})
    return out


def render_packet(changes: list[dict[str, Any]], roster_rows: list[dict[str, Any]],
                  receipts: dict[str, list[dict[str, Any]]]) -> str:
    lines = ["CHANGE LIST (deterministic; debounced indicator flips in the window)"]
    real = [c for c in changes if c["classification"] in ("activated", "cleared", "changed")]
    if not real:
        lines.append("- no clean change in the window")
    for i, c in enumerate(real, 1):
        hist = " [history: found on first sweep]" if c["history"] else ""
        lines.append(
            f"{i}. {c['statement']} — desk {c['desk']}, unit {c['unit']}: {c['classification']} "
            f"({c['from_status']} → {c['to_status']}); first run {c['first_run']}, last run {c['last_run']}{hist}"
        )
        for fid in c["finding_ids"]:
            for d in receipts.get(fid, []):
                lines.append(
                    f"   receipt (signal:{d['id']}) {d.get('masthead')}, {d.get('date_word')} "
                    f"{d.get('date')}: {d.get('title')} — {str(d.get('body') or '')[:RECEIPT_CHARS]}"
                )
    other = [c for c in changes if c["classification"] in ("unstable", "wobbled")]
    if other:
        lines.append("\nNOT CHANGES (toggled; reported, not read as change)")
        for c in other:
            lines.append(f"- {c['statement']} — {c['desk']}/{c['unit']}: {c['classification']} "
                         f"({c['raw_toggles_in_window']} raw toggles in the window)")
    moved = {(c["desk"], c["unit"], c["indicator_id"]) for c in changes}
    lines.append("\nINDICATOR ROSTER (no change unless listed above)")
    for r in roster_rows:
        if (r["desk"], r["unit"], r["indicator_id"]) not in moved:
            lines.append(f"- {r['statement']} — {r['desk']}/{r['unit']}: no change (status {r['status']})")
    return "\n".join(lines)


def output_cap(prompt: str, price: dict[str, float], hard_stop: float) -> int | None:
    """``max_tokens`` that keeps one call under ``hard_stop``, or None when <4k fit."""
    input_usd = (len(prompt) / CHARS_PER_TOKEN_PESSIMISTIC) * price["input"] / 1_000_000
    room = hard_stop - input_usd
    cap = int(room * 1_000_000 / price["output"]) if room > 0 else 0
    cap = min(cap, MAX_OUTPUT_TOKENS)
    return cap if cap >= MIN_OUTPUT_TOKENS else None


async def build(conn: Any, q: Any, as_of: datetime, reader: FlipReader) -> dict[str, Any]:
    # An "as of T" question has an instant for a window: no flip can be confirmed
    # inside it, so its packet is the roster alone (stated, not widened).
    start, end = q.window_bounds(as_of)
    rows = await reader(conn, q.desk, start, until=end, limit=1000)
    raw = {(str(r["target_id"]), str(r["source_analyst_id"]), str(r["indicator_id"])): int(r["toggles"])
           for r in await conn.fetch(_RAW_TOGGLES_SQL, q.desk, start, end)}
    changes = classify(rows, raw)
    fids = sorted({f for c in changes for f in c["finding_ids"]})
    expanded = await common.expand_findings(conn, fids, depth=1) if fids else {}
    docs = await common.resolve_signals(conn, [s for v in expanded.values() for s in v])
    receipts = {f: [docs[s] for s in sigs if s in docs and docs[s].get("resolved") is not False]
                for f, sigs in expanded.items()}
    ros = await roster(conn, q.desk, as_of)
    return {"changes": changes, "roster": ros, "receipts": receipts,
            "counts": dict(Counter(c["classification"] for c in changes)),
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "text": render_packet(changes, ros, receipts)}


def e_prompt(dz: design_mod.Design, q: Any, as_of: datetime, packet_text: str) -> str:
    return (f"{dz.prompt_e}\n\nAs of: {as_of.isoformat()}. Desk scope: {q.desk}.\n\n"
            f"Question: {q.text}\n\n{packet_text}")


async def run_e(conn: Any, reg: common.Registry, dz: design_mod.Design, q: Any, rec: dict[str, Any],
                run: dict[str, Any], gate: common.PaidGate, out: Path, as_of: datetime,
                reader: FlipReader) -> dict[str, Any]:
    hard_stop = dz.hard_stops["E"]
    rec["cost"]["hard_stop_usd"] = hard_stop
    pkt = await build(conn, q, as_of, reader)
    rec["packet"] = {k: v for k, v in pkt.items() if k != "text"}
    rec["receipts"] = [{"ref": s["id"], "kind": "signal", "documents": [s]}
                       for docs in pkt["receipts"].values() for s in docs]
    frontier = reg.stack_component(FRONTIER_COMPONENT)
    rec["model"] = {"component": FRONTIER_COMPONENT, "model_name": frontier["model_name"],
                    "version": frontier["version"], "pinned_model": common.FRONTIER_MODEL}
    prompt = e_prompt(dz, q, as_of, pkt["text"])
    rec["provenance"]["prompt_sha256"] = common.hashlib.sha256(dz.prompt_e.encode()).hexdigest()
    prices = common.Prices.load(gate.prices_path, frontier["model_name"]) if gate.prices_path else None
    cap = output_cap(prompt, prices.per_m(), hard_stop) if prices else MAX_OUTPUT_TOKENS
    body = {"model": frontier["model_name"], "max_tokens": cap or 0,
            "messages": [{"role": "user", "content": prompt}]}
    rec["request"] = {"endpoint": f"{frontier['api_endpoint']}/v1/messages", "body": body,
                      "prompt_chars": len(prompt)}
    refusal = gate.refusal()
    if refusal is None:
        refusal = common.frontier_refusal(frontier)
    if refusal is None and cap is None:
        refusal = f"hard_stop: the packet leaves fewer than {MIN_OUTPUT_TOKENS} output tokens under ${hard_stop}"
    if refusal is None:
        refusal = gate.admits(run, hard_stop)
    if refusal is not None:
        rec.update(status="dry_run" if refusal.startswith("dry_run") else "refused", refusal={"reason": refusal})
        return rec
    key = await common.resolve_secret(conn, frontier["api_key_ref"])
    t0 = time.monotonic()
    charged = hard_stop
    try:
        resp = await messages_api.post(frontier["api_endpoint"], key, body)
        u = messages_api.usage_of(resp)
        charged = prices.cost(input_tokens=u["input_tokens"], output_tokens=u["output_tokens"],
                              cache_read=u["cache_read_tokens"], cache_write=u["cache_write_tokens"])
        text = messages_api.text_of(resp)
        rec.update(raw_answer=text, status="answered" if resp.get("stop_reason") == "end_turn" else "partial")
        rec["usage"].update(input_tokens=u["input_tokens"], output_tokens=u["output_tokens"],
                            cache_read_tokens=u["cache_read_tokens"], cache_write_tokens=u["cache_write_tokens"],
                            calls=1, tool_rounds=0)
        from run_arm import stated_confidence_line

        rec["stated_confidence"] = stated_confidence_line(text)
        rec["confidence_source"] = "confidence_line"
    except Exception as exc:  # noqa: BLE001 — recorded; the stop is charged
        rec.update(status="error", refusal={"reason": f"messages call failed: {type(exc).__name__}: {exc}"})
    rec["elapsed_s"] = round(time.monotonic() - t0, 2)
    rec["cost"].update(usd=round(charged, 6), price_per_m=prices.per_m(),
                       hard_stop_breached=charged > hard_stop)
    run["spent_usd"] = round(float(run.get("spent_usd") or 0.0) + charged, 6)
    common.save_run(out, run)
    return rec


async def main_async(a: argparse.Namespace) -> int:
    common.load_env()
    dz = design_mod.load_design(a.design)
    as_of = common.parse_as_of(a.as_of)
    out = Path(a.out)
    run = common.open_run(out, as_of=as_of, design=dz, ceiling=a.run_ceiling_usd)
    gate = common.PaidGate(a.execute, run.get("run_ceiling_usd"), Path(a.prices) if a.prices else None)
    reg = common.Registry.from_env()
    if a.practice:
        from run_arm import load_practice

        qs = [q for q in load_practice(Path(a.practice)) if not a.question or q.qid in a.question]
    else:
        qs = list(dz.questions) if a.all else [dz.question(x) for x in a.question or []]
    reader = default_reader()
    async with common.readonly_conn() as conn:
        for q in qs:
            if common.existing_spent(out, q.qid, "E"):
                continue
            rec = common.new_record(run=run, question=q, arm="E", as_of=as_of)
            rec = await run_e(conn, reg, dz, q, rec, run, gate, out, as_of, reader)
            common.write_record(out, rec)
            print(f"{q.qid} E {rec['status']} {(rec.get('refusal') or {}).get('reason', '')[:100]}")
    common.save_run(out, run)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="bake-off arm E")
    ap.add_argument("--design", required=True)
    ap.add_argument("--as-of", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--question", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--practice", help="a practice-question file (P01..), never one of the 36")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--run-ceiling-usd", type=float)
    ap.add_argument("--prices")
    return ap.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async(parse_args())))


__all__ = ["build", "classify", "default_reader", "output_cap", "render_packet", "run_e"]
