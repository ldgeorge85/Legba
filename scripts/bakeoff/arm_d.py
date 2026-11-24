#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Arm D — a browsing frontier model with NO Legba access (design §2, arm D).

A direct Messages API call (``messages_api``) with Anthropic's server-side
``web_search`` (max 15 uses per question, design §2) and ``web_fetch`` tools and
the design's fixed prompt (§2.1, read from the hashed design file, T filled in).
Legba is not involved: the consult ``web_access`` grant is inert at the pin.

THE TURN LOOP. A server-tool turn may end ``pause_turn`` (the server's own
iteration cap); the harness resumes it by re-sending the conversation with the
paused assistant content, and nothing else (no "continue" message). Uses are
counted ACROSS continuations, so a resumed turn gets only the searches and
fetches the question has left.

THE COST STOP ($3, design §7) is checked BETWEEN requests: a continuation is
sent only while ``spent + worst_request < hard_stop``. Inside one request the
server runs its own loop, so a single request's exposure is bounded by
construction rather than metered: ``max_tokens`` (output), the remaining
search uses, and ``max_content_tokens`` per fetch. ``worst_request_usd`` writes
that bound into the record; the operator sees it before G1. When the stop
ends the loop, the LAST assistant turn is the delivered partial (§7).

THE ARCHIVER. Every URL the final answer cites is fetched ONCE by the harness at
run time and stored under ``<out>/archive/<sha256>.bin`` with its sha256, so
the grader opens the same bytes (§2: "the URLs the answer cites, each fetched
once ... and archived"). A receipt whose page states a publication date after
T is flagged ``after_as_of`` (§2 leakage control). The fetch is injectable; the
G0 lane exercises it on fixtures only — this lane makes NO call of any kind.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import design as design_mod  # noqa: E402
import messages_api  # noqa: E402

FRONTIER_COMPONENT = common.FRONTIER_COMPONENT  # the id is historical; it serves common.FRONTIER_MODEL
SEARCH_USES = 15            # design §2, arm D: "capped at 15 per question"
FETCH_USES = 8              # harness bound (the design names none) — stated in the record
FETCH_MAX_CONTENT_TOKENS = 12_000
MAX_OUTPUT_TOKENS = 16_000
MAX_CONTINUATIONS = 5
#: Server iterations per request (the API's own cap that produces pause_turn).
SERVER_ITERATIONS = 10
PROMPT_TOKENS_ESTIMATE = 400
SEARCH_RESULT_TOKENS_ESTIMATE = 4_000  # per search, for the worst-case bound only

Fetcher = Callable[[str], Awaitable[tuple[int, bytes, str]]]


def d_prompt(dz: design_mod.Design, q: Any, as_of: datetime) -> str:
    return dz.prompt_d.replace("<T ISO>", as_of.isoformat()).replace("<text>", q.text)


def request_body(model: str, prompt: str, *, searches_left: int, fetches_left: int,
                 messages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    tools: list[dict[str, Any]] = []
    if searches_left > 0:
        tools.append({"type": messages_api.WEB_SEARCH_TOOL, "name": "web_search", "max_uses": searches_left})
    if fetches_left > 0:
        tools.append({"type": messages_api.WEB_FETCH_TOOL, "name": "web_fetch", "max_uses": fetches_left,
                      "max_content_tokens": FETCH_MAX_CONTENT_TOKENS, "citations": {"enabled": True}})
    return {
        "model": model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "messages": messages or [{"role": "user", "content": prompt}],
        "tools": tools,
    }


def worst_request_usd(price: dict[str, float], search_fee_per_1k: float | None,
                      searches_left: int, fetches_left: int, context_tokens: int) -> float:
    """An upper bound on ONE request: every server iteration re-reads a context
    that has already absorbed every remaining search and fetch, at the
    uncached price, plus the full output cap and every search fee."""
    ceiling_ctx = (context_tokens + PROMPT_TOKENS_ESTIMATE
                   + searches_left * SEARCH_RESULT_TOKENS_ESTIMATE
                   + fetches_left * FETCH_MAX_CONTENT_TOKENS)
    usd = (SERVER_ITERATIONS * ceiling_ctx * price["input"] + MAX_OUTPUT_TOKENS * price["output"]) / 1_000_000
    if searches_left and search_fee_per_1k is not None:
        usd += searches_left * search_fee_per_1k / 1000
    return round(usd, 4)


def _published_after(body: bytes, as_of: datetime) -> str | None:
    """A page-stated publication date later than T, or None (best effort)."""
    text = body[:200_000].decode("utf-8", errors="ignore")
    m = re.search(r'(?:article:published_time|datePublished)["\']?\s*(?:content=|:)\s*["\']([0-9T:\-+.Z]+)', text)
    if not m:
        return None
    try:
        dt = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return m.group(1) if dt > as_of else None


async def archive(urls: list[str], out: Path, as_of: datetime, fetch: Fetcher) -> list[dict[str, Any]]:
    """Fetch each cited URL ONCE, store the bytes by sha256, flag post-T pages."""
    adir = Path(out) / "archive"
    adir.mkdir(parents=True, exist_ok=True)
    receipts = []
    for url in urls:
        try:
            status, body, ctype = await fetch(url)
        except Exception as exc:  # noqa: BLE001 — an unfetchable receipt is recorded as such
            receipts.append({"ref": url, "kind": "url", "documents": [],
                             "archive": {"error": f"{type(exc).__name__}: {exc}"}})
            continue
        sha = hashlib.sha256(body).hexdigest()
        (adir / f"{sha}.bin").write_bytes(body)
        after = _published_after(body, as_of)
        title = re.search(rb"<title[^>]*>(.*?)</title>", body[:100_000], re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", body[:400_000].decode("utf-8", errors="ignore"))
        receipts.append({
            "ref": url, "kind": "url",
            "documents": [{
                "kind": "web", "id": url, "masthead": re.sub(r"^https?://(www\.)?([^/]+).*$", r"\2", url),
                "date": after if after else common.utcnow().isoformat(),
                "date_word": "published" if after else "fetched",
                "title": title.group(1).decode("utf-8", errors="ignore").strip() if title else None,
                "url": url, "body": " ".join(text.split())[:common.RECEIPT_BODY_CHARS],
            }],
            "archive": {"sha256": sha, "status": status, "content_type": ctype,
                        "path": f"archive/{sha}.bin", "after_as_of": after},
        })
    return receipts


async def http_fetch(url: str) -> tuple[int, bytes, str]:  # pragma: no cover — G1 only
    import httpx

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as c:
        r = await c.get(url, headers={"User-Agent": "legba-bakeoff-archiver/1"})
        return r.status_code, r.content, r.headers.get("content-type", "")


async def run_d(conn: Any, reg: common.Registry, dz: design_mod.Design, q: Any, rec: dict[str, Any],
                run: dict[str, Any], gate: common.PaidGate, out: Path, as_of: datetime,
                fetch: Fetcher = http_fetch) -> dict[str, Any]:
    hard_stop = dz.hard_stops["D"]
    rec["cost"]["hard_stop_usd"] = hard_stop
    frontier = reg.stack_component(FRONTIER_COMPONENT)
    rec["model"] = {"component": FRONTIER_COMPONENT, "model_name": frontier["model_name"],
                    "version": frontier["version"], "pinned_model": common.FRONTIER_MODEL}
    prompt = d_prompt(dz, q, as_of)
    rec["provenance"]["prompt_sha256"] = hashlib.sha256(dz.prompt_d.encode()).hexdigest()
    body = request_body(frontier["model_name"], prompt, searches_left=SEARCH_USES, fetches_left=FETCH_USES)
    prices = common.Prices.load(gate.prices_path, frontier["model_name"]) if gate.prices_path else None
    worst = worst_request_usd(prices.per_m(), prices.web_search_per_1k, SEARCH_USES, FETCH_USES, 0) if prices else None
    rec["request"] = {"endpoint": f"{frontier['api_endpoint']}/v1/messages", "body": body,
                      "bounds": {"search_uses": SEARCH_USES, "fetch_uses": FETCH_USES,
                                 "fetch_max_content_tokens": FETCH_MAX_CONTENT_TOKENS,
                                 "max_output_tokens": MAX_OUTPUT_TOKENS,
                                 "max_continuations": MAX_CONTINUATIONS,
                                 "worst_first_request_usd": worst}}
    refusal = gate.refusal()
    if refusal is None:
        refusal = common.frontier_refusal(frontier)
    if refusal is None and prices is not None and prices.web_search_per_1k is None:
        refusal = "prices: no verified web_search_per_1k_usd — D's search fee cannot be metered"
    if refusal is None:
        refusal = gate.admits(run, hard_stop)
    if refusal is not None:
        rec.update(status="dry_run" if refusal.startswith("dry_run") else "refused", refusal={"reason": refusal})
        return rec

    key = await common.resolve_secret(conn, frontier["api_key_ref"])  # pragma: no cover — G1 only
    spent, searches, fetches = 0.0, 0, 0
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
              "web_search_requests": 0}
    messages = [{"role": "user", "content": prompt}]
    last: dict[str, Any] = {}
    t0 = time.monotonic()
    status = "answered"
    for turn in range(MAX_CONTINUATIONS + 1):
        req = request_body(frontier["model_name"], prompt, searches_left=SEARCH_USES - searches,
                           fetches_left=FETCH_USES - fetches, messages=messages)
        ctx = totals["input_tokens"] + totals["cache_read_tokens"]
        if turn and spent + worst_request_usd(prices.per_m(), prices.web_search_per_1k,
                                              SEARCH_USES - searches, FETCH_USES - fetches, ctx) >= hard_stop:
            status = "partial"
            break
        try:
            last = await messages_api.post(frontier["api_endpoint"], key, req)
        except Exception as exc:  # noqa: BLE001
            rec.update(status="error", refusal={"reason": f"messages call failed: {exc}"})
            spent = max(spent, hard_stop)  # unseen spend: charge the stop
            break
        u = messages_api.usage_of(last)
        for k in totals:
            totals[k] += u[k]
        searches += u["web_search_requests"]
        fetches += u["web_fetch_requests"]
        spent += prices.cost(input_tokens=u["input_tokens"], output_tokens=u["output_tokens"],
                             cache_read=u["cache_read_tokens"], cache_write=u["cache_write_tokens"],
                             searches=u["web_search_requests"])
        if last.get("stop_reason") != "pause_turn":
            break
        messages = [{"role": "user", "content": prompt}, {"role": "assistant", "content": last.get("content")}]
    else:
        status = "partial"
    if rec.get("status") != "error":
        text = messages_api.text_of(last)
        from run_arm import stated_confidence_line

        rec.update(raw_answer=text, status=status, stated_confidence=stated_confidence_line(text),
                   confidence_source="confidence_line")
        rec["receipts"] = await archive(messages_api.cited_urls(last), out, as_of, fetch)
    rec["usage"].update(**totals, calls=turn + 1, tool_rounds=None)
    rec["elapsed_s"] = round(time.monotonic() - t0, 2)
    rec["cost"].update(usd=round(spent, 6), price_per_m=prices.per_m(), hard_stop_breached=spent > hard_stop)
    run["spent_usd"] = round(float(run.get("spent_usd") or 0.0) + spent, 6)
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
    async with common.readonly_conn() as conn:
        for q in qs:
            if common.existing_spent(out, q.qid, "D"):
                continue
            rec = common.new_record(run=run, question=q, arm="D", as_of=as_of)
            rec = await run_d(conn, reg, dz, q, rec, run, gate, out, as_of)
            common.write_record(out, rec)
            print(f"{q.qid} D {rec['status']} {(rec.get('refusal') or {}).get('reason', '')[:100]}")
    common.save_run(out, run)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="bake-off arm D")
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
