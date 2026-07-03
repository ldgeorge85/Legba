#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C1 in-place faithfulness RE-VERIFY over the wrongly-zeroed window (one-shot).

The C1 fix (verify.py — the LLM judge is AUTHORITATIVE over the deterministic
floor, citation-aware segmentation that re-attaches a marker trailing a period,
and a bold-heading watch-section skip) is already DEPLOYED live. The OLD buggy
floor, however, has ALREADY scored a large batch of historical findings ~0.0 and
persisted those verdicts as `critique` rows. Because ``effective_confidence`` is
READ-COMPUTED as ``min(confidence, latest_faithfulness_overall_score)``
(substrate_reads_api.py / substrate_query_port.py), those findings stay
suppressed until a FRESH, correct faithfulness critique is written for each.

This harness re-runs the FIXED ``verify_finding_faithfulness`` — using the SAME
judge production uses (resolved from each analyst descriptor's
``method.llm.verify`` via ``build_llm_handler_from_stack_component``, gated by
``LEGBA_VERIFY_LLM_JUDGE``) — over the candidate findings and APPENDS one new
critique per finding via ``write_critique``. It NEVER deletes or overwrites a
prior critique (append-only; the read takes the LATEST). Writing the fresh, now
correct verdict auto-un-suppresses the finding — no column update needed.

SCOPE — the un-suppression candidates ONLY:
    kind='finding' AND produced_at > now() - <window>h AND the finding's LATEST
    'Faithfulness verify of finding%' critique scored < 0.5. Capped at --limit
    (default 500); the lowest-scored are processed first if the cap bites.

DEFAULT is a DRY-RUN (writes NOTHING). Pass --wet to persist. Pass --floor-only
to force the deterministic-floor path (judge_llm=None) — the FIXED floor alone
still un-zeros the citation-severing + bold-heading majority. If the judge can't
be resolved / reached in this standalone process, the harness FALLS BACK to
floor-only automatically and says so.

Run INSIDE the runtime container (needs the live stack + DB + the deployed
verify.py). Example (fill the password / judge flag from the live env):

    docker run --rm --network legba_default \
      -e LEGBA_DATA_PG_HOST=legba-postgres-1 -e LEGBA_DATA_PG_DB=legba \
      -e LEGBA_DATA_PG_USER=legba -e LEGBA_DATA_PG_PASSWORD=legba \
      -e LEGBA_DATA_MASTER_KEY=<from .env> \
      -e LEGBA_REGISTRY_API_URL=http://legba-registry:8090 \
      -e LEGBA_REGISTRY_API_TOKEN=<from .env> \
      -e LEGBA_VERIFY_LLM_JUDGE=1 \
      -v "$(pwd)":/work -w /work --entrypoint python \
      legba/legba-runtime-dapr:latest /work/scripts/reverify_c1_window.py [--wet]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
from typing import Any
from uuid import UUID, uuid4

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("reverify_c1")
log.setLevel(logging.INFO)

SCORE_THRESHOLD = 0.5           # a finding is a candidate when its latest faith < this
HARD_CAP = 500                  # never process more than this many
INFLATION_FLAG_SHARE = 0.40     # warn if this share of recoveries hit EXACTLY 1.0
PER_RECORD_TIMEOUT_S = 300.0    # judge-call safety; on timeout re-run floor-only
CONCURRENCY = 6                 # bounded parallel verify workers

# The candidate selection + its total count (for cap detection). The finding's
# citations/indicators live at analyst_outputs.data->'data' (the payload's own
# nested data dict), NOT at data->'citations'.
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
    """asyncpg returns jsonb as dict (with codec) or str (raw) — normalize."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return None
    return value


async def _count_candidates(conn: Any, window_hours: int) -> int:
    sql = _LATEST_FAITH_CTE + f"""
    SELECT count(*)
    FROM latest_faith lf
    JOIN analyst_outputs f ON f.id = (lf.finding_id)::uuid AND f.kind='finding'
    WHERE lf.score < {SCORE_THRESHOLD}
      AND f.produced_at > now() - interval '{window_hours} hours'
    """
    return int(await conn.fetchval(sql))


async def _fetch_candidates(conn: Any, window_hours: int, limit: int) -> list[dict[str, Any]]:
    sql = _LATEST_FAITH_CTE + f"""
    SELECT f.id, f.analyst_id, f.analyst_version, f.confidence, f.body,
           (f.data->'data') AS payload_data, lf.score AS old_faith,
           f.produced_at
    FROM latest_faith lf
    JOIN analyst_outputs f ON f.id = (lf.finding_id)::uuid AND f.kind='finding'
    WHERE lf.score < {SCORE_THRESHOLD}
      AND f.produced_at > now() - interval '{window_hours} hours'
    ORDER BY lf.score ASC, f.produced_at ASC
    LIMIT {limit}
    """
    rows = await conn.fetch(sql)
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
            "old_faith": float(r["old_faith"]),
        })
    return out


async def _descriptor_meta(conn: Any, analyst_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Per-analyst head-descriptor: verify component id + primary model ref."""
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
    ap = argparse.ArgumentParser(description="C1 window faithfulness re-verify")
    ap.add_argument("--wet", action="store_true", help="persist critiques (default: dry-run)")
    ap.add_argument("--floor-only", action="store_true",
                    help="force the deterministic floor (judge_llm=None) for every record")
    ap.add_argument("--window-hours", type=int, default=60)
    ap.add_argument("--limit", type=int, default=HARD_CAP)
    ap.add_argument("--sample", type=int, default=10, help="dry-run preview / verify sample size")
    args = ap.parse_args()
    limit = min(args.limit, HARD_CAP)

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
    log.info("=== C1 re-verify harness — %s ===", mode)
    log.info("window=%dh  score_threshold<%.2f  limit=%d  judge_enabled(env)=%s  floor_only_flag=%s",
             args.window_hours, SCORE_THRESHOLD, limit, _llm_judge_enabled(), args.floor_only)

    store = PostgresStore(PostgresConfig.from_env())
    await store.connect()
    pool = store.pool

    # ---- candidate scope ------------------------------------------------
    async with pool.acquire() as conn:
        total = await _count_candidates(conn, args.window_hours)
        candidates = await _fetch_candidates(conn, args.window_hours, limit)
        meta = await _descriptor_meta(
            conn, sorted({c["analyst_id"] for c in candidates})
        )
    capped = total > len(candidates)
    log.info("candidates: %d total in window (<%.2f); processing %d%s",
             total, SCORE_THRESHOLD, len(candidates),
             f"  [CAPPED — lowest-scored first]" if capped else "")

    # ---- resolve the judge (same path production uses) ------------------
    judge_cache: dict[str, Any] = {}
    judge_available = False
    judge_note = ""
    floor_only = args.floor_only

    if not floor_only:
        if not _llm_judge_enabled():
            floor_only = True
            judge_note = "LEGBA_VERIFY_LLM_JUDGE is off in this process"
        else:
            # All candidate analysts declare the same verify ref in practice, but
            # resolve generally + cache per component id.
            verify_refs = sorted({
                meta.get(c["analyst_id"], {}).get("verify_ref")
                for c in candidates
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
                        handler = await build_llm_handler_from_stack_component(
                            ref,
                            registry_client=registry_client,
                            secrets_resolve=vault.resolve,
                        )
                        judge_cache[ref] = handler
                    except Exception as exc:  # noqa: BLE001
                        log.warning("judge build FAILED for %s: %s", ref, exc)
                # Reachability probe against the primary verify ref.
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
                        judge_available = True
                        judge_note = (
                            f"resolved {list(judge_cache)} via method.llm.verify; "
                            f"probe OK"
                        )
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
        """Re-verify ONE finding; return the enriched record (no write here)."""
        citations = cand["citations"]
        indicators = cand["indicators"]
        is_sub = _uses_subclaim_convention(citations)
        # Mirror production's is_composition gate: only compositions pass
        # finding_confidence (unit floor ignores it anyway).
        fconf = cand["confidence"] if is_sub else None
        judge = _judge_for(cand["analyst_id"])
        try:
            report = await asyncio.wait_for(
                verify_finding_faithfulness(
                    body=cand["body"],
                    citations=citations,
                    judge_llm=judge,
                    finding_confidence=fconf,
                    indicators=indicators,
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

    # ---- DRY-RUN: sample preview, no writes -----------------------------
    if not args.wet:
        log.info("--- DRY-RUN sample (%d rows; verify computed, NOT written) ---",
                 min(args.sample, len(candidates)))
        preview = candidates[: args.sample]
        results_preview = []
        for cand in preview:
            rec = await _run_one(cand)
            results_preview.append(rec)
            log.info(
                "  %s  %-22s old=%.2f -> new=%.2f (faith=%.2f, %s, %d/%d)  | %s",
                str(cand["id"])[:8], cand["analyst_id"][:22], cand["old_faith"],
                rec["new_overall"], rec["new_faith"], rec["judge_status"],
                rec["supported"], rec["checkable"],
                cand["body"][:80].replace("\n", " "),
            )
        log.info("DRY-RUN complete — WROTE NOTHING. Re-run with --wet to persist.")
        await store.close()
        return

    # ---- WET: re-verify all + write a fresh critique per finding --------
    log.info("--- WET run over %d findings (path=%s) ---", len(candidates), path_label)
    sem = asyncio.Semaphore(CONCURRENCY)
    results: list[dict[str, Any]] = []
    written = 0
    dlq = 0
    done = 0

    async def _process(cand: dict[str, Any]) -> None:
        nonlocal written, dlq, done
        async with sem:
            rec = await _run_one(cand)
        # Write on its own connection + transaction (append-only critique).
        ctx = AnalystContext(
            analyst_id=cand["analyst_id"],
            analyst_version=cand["analyst_version"],
            run_id=uuid4(),               # a fresh re-verify run id (no FK)
            target_id=None,
            target_version=None,
        )
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    row, entry = await write_critique(
                        conn,
                        analyst_ctx=ctx,
                        payload=rec["payload"],
                        derived_from=[cand["id"]],
                    )
            if row is None:
                dlq += 1
                log.warning("critique DLQ for finding %s", cand["id"])
            else:
                written += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("write failed for %s: %s", cand["id"], exc)
        results.append(rec)
        done += 1
        if done % 25 == 0:
            log.info("  ... %d/%d re-verified (%d written)", done, len(candidates), written)

    await asyncio.gather(*(_process(c) for c in candidates))
    log.info("WROTE %d fresh critiques (%d DLQ) over %d findings",
             written, dlq, len(candidates))

    _report_distribution(results)
    await _verify_readback(pool, results, args.sample)
    await store.close()


def _report_distribution(results: list[dict[str, Any]]) -> None:
    n = len(results)
    if n == 0:
        log.info("no results to summarize")
        return
    old = [r["old_faith"] for r in results]
    new = [r["new_overall"] for r in results]
    old_lt030 = sum(1 for v in old if v < 0.30)
    new_lt030 = sum(1 for v in new if v < 0.30)
    exact_one = sum(1 for v in new if v == 1.0)
    js = {}
    for r in results:
        js[r["judge_status"]] = js.get(r["judge_status"], 0) + 1
    log.info("=== BEFORE / AFTER distribution (n=%d) ===", n)
    log.info("  mean faithfulness:   old=%.3f  ->  new=%.3f", statistics.mean(old),
             statistics.mean(new))
    log.info("  %% < 0.30:            old=%.1f%% (%d)  ->  new=%.1f%% (%d)",
             100 * old_lt030 / n, old_lt030, 100 * new_lt030 / n, new_lt030)
    log.info("  judge_status counts: %s", js)
    share_one = exact_one / n
    log.info("  OVER-INFLATION WATCH: new==EXACTLY 1.0 = %d / %d (%.1f%%)",
             exact_one, n, 100 * share_one)
    if share_one > INFLATION_FLAG_SHARE:
        log.warning("  ^^ FLAG: >%.0f%% hit exactly 1.0 — possible lenient-judge "
                    "inflation (expected a spread, not a pile at 1.0)",
                    100 * INFLATION_FLAG_SHARE)
    else:
        log.info("  (below the %.0f%% inflation flag — healthy spread)",
                 100 * INFLATION_FLAG_SHARE)
    # A coarse histogram of the new distribution.
    buckets = [0, 0, 0, 0, 0]  # [0,.2)[.2,.4)[.4,.6)[.6,.8)[.8,1]
    for v in new:
        idx = min(int(v * 5), 4)
        buckets[idx] += 1
    log.info("  new hist [0-.2)(.2-.4)(.4-.6)(.6-.8)(.8-1]: %s", buckets)


async def _verify_readback(pool: Any, results: list[dict[str, Any]], sample: int) -> None:
    """Confirm the read-computed effective_confidence now reflects the new critique."""
    log.info("=== READ-BACK: effective_confidence after re-verify (sample) ===")
    # Prefer the biggest recoveries for the sample (most informative).
    picks = sorted(results, key=lambda r: r["new_overall"] - r["old_faith"],
                   reverse=True)[:sample]
    sql = _LATEST_FAITH_CTE + """
    SELECT f.confidence,
           lf.score AS latest_faith,
           LEAST(f.confidence, lf.score) AS eff_conf
    FROM latest_faith lf
    JOIN analyst_outputs f ON f.id = (lf.finding_id)::uuid AND f.kind='finding'
    WHERE f.id = $1
    """
    async with pool.acquire() as conn:
        for r in picks:
            row = await conn.fetchrow(sql, r["id"])
            if row is None:
                continue
            log.info(
                "  %s  %-22s old_faith=%.2f -> new_faith=%.2f  conf=%.2f  "
                "eff_conf(read)=%.2f  [%s]",
                str(r["id"])[:8], r["analyst_id"][:22], r["old_faith"],
                float(row["latest_faith"]), float(row["confidence"]),
                float(row["eff_conf"]), r["judge_status"],
            )


if __name__ == "__main__":
    asyncio.run(main())
