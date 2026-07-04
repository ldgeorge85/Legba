#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DQ P7-F1 in-place faithfulness RE-VERIFY over the COMPOSITION heads (one-shot).

The P7-F1 fix (verify.py — bold '**Heading**' lines dropped as headings, a
citation marker trailing a sentence period pulled back inside the sentence,
'judgment'/'assumption' synthesis openers exempted from the floor, forward-looking
watch bullets exempted from BOTH floor and judge, and the shared-lineage
double-count guard keyed on same-SOURCE so a country_composition's 7 independent
units are no longer falsely collapsed) is DEPLOYED live at apply time. The OLD
verify pass, however, already persisted `critique` rows scoring several
composition heads ~0.0 — notably the UA country head and the CA country head fully
zeroed — and `effective_confidence` is READ-COMPUTED as
`min(confidence, latest_faithfulness_overall_score)`, so those heads stay
suppressed (and drag the region/world folds that cite them) until a FRESH, correct
faithfulness critique is written for each.

This harness re-runs the FIXED `verify_finding_faithfulness` — using the SAME
judge production uses (resolved from each analyst descriptor's `method.llm.verify`
via `build_llm_handler_from_stack_component`, gated by `LEGBA_VERIFY_LLM_JUDGE`) —
over the 25 country_composition heads + 5 region_composition heads + the world
head, and APPENDS one new critique per head via `write_critique`. It NEVER deletes
or overwrites a prior critique (append-only; the read takes the LATEST). Writing
the fresh, now-correct verdict auto-un-suppresses the head — no column update.

SCOPE — the live composition HEADS ONLY:
    kind='finding' AND superseded_by IS NULL AND analyst_id IN
    ('country_composition','region_composition','world_assessor'). These are the
    per-country reads, the per-region reads, and the single world read. (Run AFTER
    migration 0074 so the world layer is a single, target-less head.)

DEFAULT is a DRY-RUN (writes NOTHING) that prints the projected old->new overall
per head so the operator sees UA + CA rise from 0.00 BEFORE anything persists.
Pass --wet to persist. Pass --floor-only to force the deterministic-floor path
(judge_llm=None) — the FIXED floor alone un-zeros the bold-heading + marker-split +
judgment/assumption majority; if the judge can't be resolved/reached in this
standalone process, the harness FALLS BACK to floor-only automatically and says so.

Run INSIDE the runtime container (needs the live stack + DB + the deployed
verify.py). Example (fill password / judge flag from the live env):

    docker run --rm --network legba_default \
      -e LEGBA_DATA_PG_HOST=legba-postgres-1 -e LEGBA_DATA_PG_DB=legba \
      -e LEGBA_DATA_PG_USER=legba -e LEGBA_DATA_PG_PASSWORD=legba \
      -e LEGBA_DATA_MASTER_KEY=<from .env> \
      -e LEGBA_REGISTRY_API_URL=http://legba-registry:8090 \
      -e LEGBA_REGISTRY_API_TOKEN=<from .env> \
      -e LEGBA_VERIFY_LLM_JUDGE=1 \
      -v "$(pwd)":/work -w /work --entrypoint python \
      legba/legba-runtime-dapr:latest /work/scripts/reverify_composition_heads.py [--wet]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
from typing import Any
from uuid import uuid4

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("reverify_composition")
log.setLevel(logging.INFO)

PER_RECORD_TIMEOUT_S = 300.0    # judge-call safety; on timeout re-run floor-only
CONCURRENCY = 4                 # bounded parallel verify workers
INFLATION_FLAG_SHARE = 0.55     # warn if this share of recoveries hit EXACTLY 1.0

_COMPOSITION_ANALYSTS = ("country_composition", "region_composition", "world_assessor")

# The latest persisted faithfulness overall_score per finding (for the delta).
_LATEST_FAITH_CTE = """
WITH latest_faith AS (
  SELECT DISTINCT ON (cr.data->>'analyzed_output_id')
         (cr.data->>'analyzed_output_id') AS finding_id,
         (cr.data->>'overall_score')::real AS score
  FROM analyst_outputs cr
  WHERE cr.kind='critique'
    AND cr.body LIKE 'Faithfulness verify of finding%'
    AND cr.data->>'overall_score' IS NOT NULL
  ORDER BY cr.data->>'analyzed_output_id', cr.produced_at DESC, cr.id DESC
)
"""


def _coerce_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return None
    return value


async def _fetch_heads(conn: Any) -> list[dict[str, Any]]:
    sql = _LATEST_FAITH_CTE + """
    SELECT f.id, f.analyst_id, f.analyst_version, f.confidence, f.body,
           f.target_id, (f.data->'data') AS payload_data,
           lf.score AS old_overall, f.produced_at
    FROM analyst_outputs f
    LEFT JOIN latest_faith lf ON lf.finding_id = f.id::text
    WHERE f.kind='finding'
      AND f.superseded_by IS NULL
      AND f.analyst_id = ANY($1::text[])
    ORDER BY f.analyst_id, COALESCE(f.target_id,'~world'), f.produced_at DESC
    """
    rows = await conn.fetch(sql, list(_COMPOSITION_ANALYSTS))
    out: list[dict[str, Any]] = []
    for r in rows:
        payload = _coerce_json(r["payload_data"]) or {}
        out.append({
            "id": r["id"],
            "analyst_id": r["analyst_id"],
            "analyst_version": r["analyst_version"] or "",
            "confidence": float(r["confidence"]) if r["confidence"] is not None else 0.5,
            "body": str(r["body"] or ""),
            "citations": payload.get("citations"),
            "indicators": payload.get("indicators"),
            "target_id": r["target_id"],
            "old_overall": float(r["old_overall"]) if r["old_overall"] is not None else None,
        })
    return out


async def _descriptor_meta(conn: Any, analyst_ids: list[str]) -> dict[str, dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT descriptor_id,
               body->'method'->'llm'->'verify'->>'raw'  AS verify_ref,
               body->'method'->'llm'->'primary'->>'raw' AS primary_ref
        FROM analyst_descriptors
        WHERE is_head=TRUE AND descriptor_id = ANY($1::text[])
        """,
        analyst_ids,
    )
    return {
        r["descriptor_id"]: {
            "verify_ref": r["verify_ref"],
            "primary_ref": r["primary_ref"] or "",
        }
        for r in rows
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description="P7-F1 composition-head faithfulness re-verify")
    ap.add_argument("--wet", action="store_true", help="persist critiques (default: dry-run)")
    ap.add_argument("--floor-only", action="store_true",
                    help="force the deterministic floor (judge_llm=None) for every head")
    args = ap.parse_args()

    # Deferred imports — the deployed (baked) legba package inside the image.
    from legba.data.config import PostgresConfig
    from legba.data.postgres import PostgresStore
    from legba.data.provenance._core import AnalystContext
    from legba.data.provenance.verify import (
        _llm_judge_enabled,
        _uses_subclaim_convention,
        build_faithfulness_critique_payload,
        verify_finding_faithfulness,
    )
    from legba.data.provenance.writes import write_critique

    mode = "WET (writing critiques)" if args.wet else "DRY-RUN (writing nothing)"
    log.info("=== P7-F1 composition-head re-verify — %s ===", mode)
    log.info("judge_enabled(env)=%s  floor_only_flag=%s",
             _llm_judge_enabled(), args.floor_only)

    store = PostgresStore(PostgresConfig.from_env())
    await store.connect()
    pool = store.pool

    async with pool.acquire() as conn:
        heads = await _fetch_heads(conn)
        meta = await _descriptor_meta(conn, sorted({c["analyst_id"] for c in heads}))

    by_analyst: dict[str, int] = {}
    for h in heads:
        by_analyst[h["analyst_id"]] = by_analyst.get(h["analyst_id"], 0) + 1
    log.info("composition heads: %d total  %s", len(heads), by_analyst)

    # ---- resolve the judge (same path production uses) ------------------
    judge_cache: dict[str, Any] = {}
    judge_note = ""
    floor_only = args.floor_only

    if not floor_only:
        if not _llm_judge_enabled():
            floor_only = True
            judge_note = "LEGBA_VERIFY_LLM_JUDGE is off in this process"
        else:
            verify_refs = sorted({
                meta.get(c["analyst_id"], {}).get("verify_ref")
                for c in heads
                if meta.get(c["analyst_id"], {}).get("verify_ref")
            })
            if not verify_refs:
                floor_only = True
                judge_note = "no analyst declares method.llm.verify"
            else:
                from legba.data.registry.credentials import CredentialVault
                from legba.runtime.analyst_deps_builder import (
                    build_llm_handler_from_stack_component,
                )
                from legba.runtime.registry_client import RegistryHTTPClient

                registry_client = RegistryHTTPClient()
                vault = CredentialVault(store)
                for ref in verify_refs:
                    try:
                        judge_cache[ref] = await build_llm_handler_from_stack_component(
                            ref,
                            registry_client=registry_client,
                            secrets_resolve=vault.resolve,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning("judge build FAILED for %s: %s", ref, exc)
                probe_ref = verify_refs[0]
                probe_handler = judge_cache.get(probe_ref)
                if probe_handler is None:
                    floor_only = True
                    judge_note = f"handler build failed for {probe_ref}"
                else:
                    try:
                        resp = await asyncio.wait_for(
                            probe_handler.chat_complete(
                                [{"role": "user", "content": "Reply with the word ok."}],
                                max_tokens=2048, temperature=0.0,
                            ),
                            timeout=120.0,
                        )
                        _ = getattr(resp, "content", "")
                        judge_note = f"resolved {list(judge_cache)} via method.llm.verify; probe OK"
                    except Exception as exc:  # noqa: BLE001
                        floor_only = True
                        judge_note = f"judge probe FAILED ({type(exc).__name__}: {exc})"

    path_label = "FLOOR-ONLY" if floor_only else "JUDGE"
    log.info("verify path = %s  (%s)", path_label, judge_note or "forced by flag")

    def _judge_for(analyst_id: str) -> Any:
        if floor_only:
            return None
        ref = meta.get(analyst_id, {}).get("verify_ref")
        return judge_cache.get(ref) if ref else None

    async def _run_one(cand: dict[str, Any]) -> dict[str, Any]:
        citations = cand["citations"]
        indicators = cand["indicators"]
        is_sub = _uses_subclaim_convention(citations)
        # Every composition uses the [[ref:N]] sub-claim convention, so pass the
        # finding_confidence (the T7 hedge/ceiling leg reads it); on the unit path
        # it is inert.
        fconf = cand["confidence"] if is_sub else None
        judge = _judge_for(cand["analyst_id"])
        try:
            report = await asyncio.wait_for(
                verify_finding_faithfulness(
                    body=cand["body"], citations=citations, judge_llm=judge,
                    finding_confidence=fconf, indicators=indicators,
                ),
                timeout=PER_RECORD_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 — timeout/other → floor-only rerun
            log.warning("verify timeout/err on %s (%s) — floor-only rerun",
                        cand["id"], type(exc).__name__)
            report = await verify_finding_faithfulness(
                body=cand["body"], citations=citations, judge_llm=None,
                finding_confidence=fconf, indicators=indicators,
            )
        judge_model = str(getattr(judge, "subprovider", "") or "deterministic-floor")
        payload = build_faithfulness_critique_payload(
            report,
            analyzed_output_id=cand["id"],
            analyzed_analyst_id=cand["analyst_id"],
            analyzed_analyst_version=cand["analyst_version"],
            analyzed_model=meta.get(cand["analyst_id"], {}).get("primary_ref", ""),
            judge_model=judge_model,
        )
        return {
            **cand,
            "report": report,
            "payload": payload,
            "new_overall": float(payload["overall_score"]),
            "new_faith": float(report.faithfulness_score),
            "judge_status": report.judge_status,
            "checkable": report.checkable_claims,
            "supported": report.supported_claims,
            "is_sub": is_sub,
        }

    sem = asyncio.Semaphore(CONCURRENCY)

    async def _guarded(cand: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            return await _run_one(cand)

    results = await asyncio.gather(*(_guarded(c) for c in heads))

    # ---- projected-delta report (always) --------------------------------
    log.info("--- projected old->new overall per head (%s) ---", path_label)
    for r in sorted(results, key=lambda x: (x["analyst_id"], str(x["target_id"] or "~world"))):
        old = r["old_overall"]
        old_s = f"{old:.2f}" if old is not None else "  (none)"
        log.info(
            "  %s  %-20s %-16s old=%s -> new=%.2f (faith=%.2f, %s, %d/%d)",
            str(r["id"])[:8], r["analyst_id"], str(r["target_id"] or "world")[:16],
            old_s, r["new_overall"], r["new_faith"], r["judge_status"],
            r["supported"], r["checkable"],
        )
    _report_distribution(results)

    if not args.wet:
        log.info("DRY-RUN complete — WROTE NOTHING. Re-run with --wet to persist.")
        await store.close()
        return

    # ---- WET: append a fresh critique per head --------------------------
    written = 0
    dlq = 0
    for r in results:
        ctx = AnalystContext(
            analyst_id=r["analyst_id"],
            analyst_version=r["analyst_version"],
            run_id=uuid4(),
            target_id=r["target_id"],
            target_version=None,
        )
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    row, _entry = await write_critique(
                        conn, analyst_ctx=ctx, payload=r["payload"],
                        derived_from=[r["id"]],
                    )
            if row is None:
                dlq += 1
                log.warning("critique DLQ for head %s", r["id"])
            else:
                written += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("write failed for %s: %s", r["id"], exc)
    log.info("WROTE %d fresh critiques (%d DLQ) over %d heads", written, dlq, len(results))
    await store.close()


def _report_distribution(results: list[dict[str, Any]]) -> None:
    n = len(results)
    if n == 0:
        log.info("no heads to summarize")
        return
    old = [r["old_overall"] for r in results if r["old_overall"] is not None]
    new = [r["new_overall"] for r in results]
    zeroed_old = sum(1 for r in results if (r["old_overall"] or 0.0) < 0.30)
    zeroed_new = sum(1 for v in new if v < 0.30)
    exact_one = sum(1 for v in new if v == 1.0)
    js: dict[str, int] = {}
    for r in results:
        js[r["judge_status"]] = js.get(r["judge_status"], 0) + 1
    log.info("=== BEFORE / AFTER (n=%d heads) ===", n)
    if old:
        log.info("  mean overall (heads w/ prior critique): old=%.3f", statistics.mean(old))
    log.info("  mean overall new: %.3f", statistics.mean(new))
    log.info("  heads < 0.30: old=%d -> new=%d", zeroed_old, zeroed_new)
    log.info("  judge_status counts: %s", js)
    share_one = exact_one / n
    log.info("  OVER-INFLATION WATCH: new==1.0 = %d / %d (%.1f%%)",
             exact_one, n, 100 * share_one)
    if share_one > INFLATION_FLAG_SHARE:
        log.warning("  ^^ FLAG: >%.0f%% hit exactly 1.0 — possible lenient inflation",
                    100 * INFLATION_FLAG_SHARE)
    buckets = [0, 0, 0, 0, 0]
    for v in new:
        buckets[min(int(v * 5), 4)] += 1
    log.info("  new hist [0-.2)(.2-.4)(.4-.6)(.6-.8)(.8-1]: %s", buckets)


if __name__ == "__main__":
    asyncio.run(main())
