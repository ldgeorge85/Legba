#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""journal_aperture_replay.py — T2.2's measurement and its two-arm replay.

TWO COMMANDS, one harness.

``measure`` rebuilds the journal's DELIVERED priming window for a historical
24 h window exactly as the narrator saw it — the same SQL clauses
(``actor_substrate_slice._read_substrate_slice``'s META / ``target_filter=None``
leg), the same ``ORDER BY fetched_at DESC LIMIT 360`` fetch, the same
``_diversify_by_source(per_source_cap=15, limit=120)``, the same
``journal_slice._select_journal_slice`` cut to 60 — and then clusters the FULL
24 h candidate pool (every row passing the clauses, no LIMIT and no cap) with
the shipped ``journal_clusters.cluster_pool``. The contrast between "cluster mass
in the pool" and "members that reached the window" IS the aperture evidence.

``replay`` runs the live narrator over a rebuilt window under both arms:
  * arm **B** — ``LEGBA_JOURNAL_CLUSTER_FIRST`` OFF (today's window). This is the
    BASELINE, not a control: arm C deliberately changes what the voice reads, so
    no byte-identical control exists and none is claimed.
  * arm **C** — the flag ON (cluster block + '▸' headers + the desk roster).
Both arms render through the REAL ``journal_assessor._render_user_prompt`` and
call the REAL core plane the way ``scripts/d6_clause_replay.py`` does (plain HTTP
to ``LEGBA_LLM_API_ENDPOINT``, key from the runtime's own credential vault, never
printed, ``max_tokens`` deliberately not sent).

READ-ONLY BY CONSTRUCTION. The Postgres connection sets
``default_transaction_read_only``; every statement is a SELECT. Nothing is
written, registered, or deployed.

ONE DOCUMENTED DEVIATION from the live slice: the graph-structure context leg
(``_slice_graph_structure_cap``, <= 8 rows, ``id=None``, APPENDED after the
signal rows) is not rebuilt. Those rows carry no id, cannot be cited, are not
windowed by ``fetched_at`` so they cannot be reconstructed as-of a past window at
all, and they land after every signal row in the delivered order.

USAGE
    python3 scripts/journal_aperture_replay.py measure \\
        --window-end 2026-09-09T00:00:00Z --window-end 2026-09-08T00:00:00Z \\
        --out /tmp/aperture.json
    python3 scripts/journal_aperture_replay.py replay \\
        --window-end 2026-09-09T00:00:00Z --window-end 2026-09-08T00:00:00Z \\
        --rounds 2 --out /tmp/replay.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(REPO_ROOT / "src"))

#: The three clauses ``_read_substrate_slice`` puts on a META signal read, in its
#: own order. ``LEGBA_RESEARCH_EVIDENCE`` is ``substrate`` on this deployment, so
#: the research exclusion is ON — the same predicate the live reader appends.
_BASE_CLAUSES = (
    "(payload->>'event_class') IS DISTINCT FROM 'backfill'",
    "(canonical_signal_id IS NULL OR canonical_signal_id = id)",
    "(retrieval_origin IS NULL OR retrieval_origin NOT LIKE 'web_search:%')",
)

_SLICE_COLUMNS = (
    "id, source_id, source_version, canonical_url, payload, language, geo, "
    "tags, fetched_at, derived_from, entity_classes, source_credibility, "
    "modality, salience"
)

#: ``max(200, _slice_row_cap() * 3)`` at the shipped ``LEGBA_SLICE_ROW_CAP=120``.
FETCH_LIMIT = 360
#: ``_slice_row_cap()``.
ROW_CAP = 120
#: ``_global_slice_per_source_cap()``.
PER_SOURCE_CAP = 15


def _load_env() -> None:
    """The repo ``.env`` (or the deployment's), without clobbering exports.

    Values are never printed, echoed, or written to the results file — the same
    contract ``d6_clause_replay._load_env`` holds.
    """
    env = REPO_ROOT / ".env"
    if not env.is_file():
        env = Path("/usr/local/deployments/active/legba/.env")
    if not env.is_file():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


async def _connect() -> Any:
    import asyncpg

    from legba.data.config import PostgresConfig

    cfg = PostgresConfig.from_env()
    return await asyncpg.connect(
        host=cfg.host, port=cfg.port, user=cfg.user, password=cfg.password,
        database=cfg.database,
        server_settings={"default_transaction_read_only": "on"},
    )


def _shape(record: Any) -> dict[str, Any]:
    """One live signals row as the analyst's input dict.

    The back-compat mapping at the tail of ``_read_substrate_slice``, copied
    field for field: ``title`` off the payload, ``data`` = the payload,
    ``produced_at`` = ``fetched_at``.
    """
    d = dict(record)
    payload = d.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    sal = d.get("salience")
    if isinstance(sal, str):
        try:
            sal = json.loads(sal)
        except Exception:
            sal = None
    d["salience"] = sal
    d["id"] = str(d["id"]) if d.get("id") is not None else None
    d["target_id"] = None
    d["target_version"] = None
    d["source_url"] = d.get("canonical_url")
    d["title"] = payload.get("title") if isinstance(payload, dict) else None
    d["data"] = payload
    d["geo"] = list(d.get("geo") or [])
    d["produced_at"] = d.get("fetched_at")
    return d


async def _pool_for_window(conn: Any, end: datetime) -> list[dict[str, Any]]:
    """Every row in ``(end - 24h, end]`` passing the live reader's clauses."""
    where = " AND ".join(
        ("fetched_at > $1", "fetched_at <= $2") + _BASE_CLAUSES
    )
    rows = await conn.fetch(
        f"SELECT {_SLICE_COLUMNS} FROM signals WHERE {where} "
        "ORDER BY fetched_at DESC",
        end - timedelta(hours=24), end,
    )
    return [_shape(r) for r in rows]


def _delivered_inputs(pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows the analyst's ``inputs`` actually held: the recency fetch, then
    the per-source diversity cap. ``pool`` is already ``fetched_at DESC``."""
    from legba.runtime.actor_substrate_slice import _diversify_by_source

    return _diversify_by_source(
        pool[:FETCH_LIMIT], per_source_cap=PER_SOURCE_CAP, limit=ROW_CAP
    )


#: T2.3 — the LEG-2 window clause. The live reader has no upper bound (it reads
#: ``NOW()``); a historical replay needs one, so the same three base clauses get
#: ``fetched_at > $1 AND fetched_at <= $2`` in front of them, exactly as
#: ``_pool_for_window`` already does for leg 1. Everything else — the columns,
#: the strata, the caps, the interleave — comes from the SHIPPED
#: ``journal_slice_v2.journal_slice_v2_sql``, so this harness cannot measure a
#: leg that differs from the one the actor would run.
_WINDOWED_WHERE = "WHERE " + " AND ".join(
    ("fetched_at > $1", "fetched_at <= $2") + _BASE_CLAUSES
)


async def _leg2_fetch(conn: Any, end: datetime) -> list[dict[str, Any]]:
    """The leg-2 fetch for ``(end - 24h, end]`` — the shipped statement."""
    from legba.runtime.journal_slice_v2 import journal_slice_v2_sql

    sql = journal_slice_v2_sql(
        columns=_SLICE_COLUMNS, where=_WINDOWED_WHERE, total=FETCH_LIMIT,
        row_cap=ROW_CAP, per_source_cap=PER_SOURCE_CAP,
    )
    rows = await conn.fetch(sql, end - timedelta(hours=24), end)
    return [_shape(r) for r in rows]


async def _delivered_inputs_v2(conn: Any, end: datetime) -> list[dict[str, Any]]:
    """Leg 2's ``inputs``: the stratified fetch, then the SAME per-source
    diversity cap the caller applies to leg 1's rows. Nothing downstream of the
    fetch changes — that is the whole point of holding the 360-row budget."""
    from legba.runtime.actor_substrate_slice import _diversify_by_source

    return _diversify_by_source(
        await _leg2_fetch(conn, end), per_source_cap=PER_SOURCE_CAP, limit=ROW_CAP
    )


def _span(rows: list[dict[str, Any]]) -> str:
    stamps = [r["fetched_at"] for r in rows if r.get("fetched_at")]
    return str(max(stamps) - min(stamps)) if stamps else "0:00:00"


def _source_hist(rows: list[dict[str, Any]], top: int = 8) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for r in rows:
        sid = str(r.get("source_id"))
        counts[sid] = counts.get(sid, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "distinct_sources": len(counts),
        "max_rows_one_source": ranked[0][1] if ranked else 0,
        "top": [{"source_id": s, "rows": n} for s, n in ranked[:top]],
    }


def _title(row: dict[str, Any]) -> str:
    from legba.data.analysts.journal_clusters import _row_title

    return _row_title(row)


def _cluster_table(
    clusters: list[Any],
    window_ids: set[str],
    top: int,
    *,
    extra: dict[str, set[str]] | None = None,
) -> list[dict[str, Any]]:
    """The per-cluster table. ``extra`` names further id-sets to count members
    against — T2.3 passes leg 2's fetch/inputs/window so ONE cluster ranking
    (the full 24 h pool's) is scored under both legs, which is the only way the
    comparison is apples to apples."""
    from legba.data.analysts.signal_salience import magnitude_of

    out: list[dict[str, Any]] = []
    for rank, c in enumerate(clusters[:top], start=1):
        member_ids = [str(r.get("id")) for r in c.members]
        row = {
            "rank": rank,
            "key": c.key,
            "label": c.label(),
            "members": len(c.members),
            "sources": c.sources,
            "mass": c.mass,
            "max_magnitude": round(c.max_magnitude, 3),
            "in_window": sum(1 for i in member_ids if i in window_ids),
            "lead_title": _title(c.lead)[:150],
            "lead_magnitude": round(magnitude_of(c.lead.get("salience")), 3),
        }
        for name, ids in (extra or {}).items():
            row[name] = sum(1 for i in member_ids if i in ids)
        out.append(row)
    return out


def _find_cluster(clusters: list[Any], needle_ids: set[str]) -> dict[str, Any] | None:
    for rank, c in enumerate(clusters, start=1):
        if {str(r.get("id")) for r in c.members} & needle_ids:
            return {"rank": rank, "key": c.key, "label": c.label(),
                    "members": len(c.members), "sources": c.sources,
                    "mass": c.mass}
    return None


async def cmd_measure(args: argparse.Namespace) -> int:
    from legba.data.analysts.journal_clusters import (
        _JOURNAL_CLUSTER_MAX_UBIQUITY, cluster_pool, journal_window, row_anchors,
    )

    os.environ.pop("LEGBA_JOURNAL_CLUSTER_FIRST", None)   # arm B for the window
    conn = await _connect()
    results: list[dict[str, Any]] = []
    try:
        for raw_end in args.window_end:
            end = datetime.fromisoformat(raw_end.replace("Z", "+00:00"))
            pool = await _pool_for_window(conn, end)
            inputs = _delivered_inputs(pool)
            window = journal_window([dict(r) for r in inputs])
            window_ids = {str(r.get("id")) for r in window}
            input_ids = {str(r.get("id")) for r in inputs}
            # T2.3 — the same window read through the salience-stratified leg.
            fetch2 = await _leg2_fetch(conn, end)
            inputs2 = await _delivered_inputs_v2(conn, end)
            window2 = journal_window([dict(r) for r in inputs2])
            window2_ids = {str(r.get("id")) for r in window2}
            input2_ids = {str(r.get("id")) for r in inputs2}
            fetch2_ids = {str(r.get("id")) for r in fetch2}
            fetch1_ids = {str(r.get("id")) for r in pool[:FETCH_LIMIT]}
            legs = {
                "in_fetch_leg1": fetch1_ids, "in_fetch_leg2": fetch2_ids,
                "in_inputs_leg1": input_ids, "in_inputs_leg2": input2_ids,
                "in_window_leg2": window2_ids,
            }

            df: dict[str, int] = {}
            for row in pool:
                for name in row_anchors(row):
                    df[name] = df.get(name, 0) + 1
            ubiquity = sorted(
                ({"polity": k, "rows": v, "share": round(v / len(pool), 4)}
                 for k, v in df.items()),
                key=lambda d: -d["rows"],
            )[:15]

            pool_clusters = cluster_pool(pool)
            input_clusters = cluster_pool(inputs)

            needle = {
                str(r.get("id")) for r in pool
                if "settlement" in _title(r).lower()
                and ("uk " in _title(r).lower() or "britain" in _title(r).lower()
                     or "british" in _title(r).lower())
            }
            results.append({
                "window_end": raw_end,
                "pool_rows": len(pool),
                "fetch_limit_rows": min(len(pool), FETCH_LIMIT),
                "delivered_inputs": len(inputs),
                "narrator_window": len(window),
                "distinct_sources_pool": len({r.get("source_id") for r in pool}),
                "distinct_sources_inputs": len({r.get("source_id") for r in inputs}),
                "ubiquity_ceiling": _JOURNAL_CLUSTER_MAX_UBIQUITY,
                "anchor_ubiquity_top15": ubiquity,
                "pool_clusters_total": len(pool_clusters),
                "pool_clusters_top10": _cluster_table(
                    pool_clusters, window_ids, 10, extra=legs),
                "input_clusters_total": len(input_clusters),
                "input_clusters_top10": _cluster_table(input_clusters, window_ids, 10),
                "settlement_probe_rows": len(needle),
                "settlement_in_pool": _find_cluster(pool_clusters, needle),
                "settlement_in_inputs": _find_cluster(input_clusters, needle),
                "settlement_rows_in_inputs": len(needle & input_ids),
                "settlement_rows_in_window": len(needle & window_ids),
                # ---- T2.3 leg comparison -------------------------------
                "leg1": {
                    "fetch_rows": len(fetch1_ids), "fetch_span": _span(pool[:FETCH_LIMIT]),
                    "inputs": len(inputs), "window": len(window),
                    "fetch_sources": _source_hist(pool[:FETCH_LIMIT]),
                    "inputs_sources": _source_hist(inputs),
                    "top10_rows_delivered": sum(
                        len({str(r.get("id")) for r in c.members} & window_ids)
                        for c in pool_clusters[:10]),
                    "settlement_rows_in_fetch": len(needle & fetch1_ids),
                },
                "leg2": {
                    "fetch_rows": len(fetch2), "fetch_span": _span(fetch2),
                    "inputs": len(inputs2), "window": len(window2),
                    "fetch_sources": _source_hist(fetch2),
                    "inputs_sources": _source_hist(inputs2),
                    "top10_rows_delivered": sum(
                        len({str(r.get("id")) for r in c.members} & window2_ids)
                        for c in pool_clusters[:10]),
                    "settlement_rows_in_fetch": len(needle & fetch2_ids),
                    "settlement_rows_in_inputs": len(needle & input2_ids),
                    "settlement_rows_in_window": len(needle & window2_ids),
                    "input_clusters_total": len(cluster_pool(inputs2)),
                },
                "top10_member_rows": sum(len(c.members) for c in pool_clusters[:10]),
            })
            print(f"measured {raw_end}: pool={len(pool)} "
                  f"leg1 inputs={len(inputs)} window={len(window)} "
                  f"leg2 inputs={len(inputs2)} window={len(window2)} "
                  f"pool_clusters={len(pool_clusters)}")
    finally:
        await conn.close()
    Path(args.out).write_text(json.dumps(results, indent=2, default=str))
    print(f"wrote {args.out}")
    return 0


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


class _Usage:
    prompt_tokens = 0
    completion_tokens = 0
    reasoning_tokens = 0


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = _Usage()


class CorePlaneLLM:
    """``d6_clause_replay.CorePlaneLLM``'s wire call, verbatim in shape.

    ``max_tokens`` is deliberately NOT sent — the core plane never sends it, and
    an arm that truncated differently would be a confound rather than an arm.
    """

    def __init__(self, session: Any, endpoint: str, key: str, model: str) -> None:
        self._s = session
        self._ep = endpoint.rstrip("/")
        self._key = key
        self._model = model
        self.calls = 0

    async def complete(self, system: str, user: str, temperature: float = 1.0) -> str:
        self.calls += 1
        async with self._s.post(
            f"{self._ep}/v1/chat/completions",
            json={
                "model": self._model, "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            headers={"Authorization": f"Bearer {self._key}"},
        ) as resp:
            resp.raise_for_status()
            body = await resp.json()
        return body["choices"][0]["message"]["content"] or ""


async def _core_key() -> str:
    """The core plane's key, from the vault the runtime uses. Never printed."""
    from legba.data.registry.credentials import CredentialVault

    vault = CredentialVault.from_env()
    await vault.store.connect()
    try:
        return (await vault.resolve("llm.primary.api_key")).decode()
    finally:
        await vault.store.close()


async def _newest_world_body(conn: Any) -> str:
    row = await conn.fetchrow(
        "SELECT body FROM analyst_outputs WHERE analyst_id = 'world_assessor' "
        "AND kind = 'finding' AND superseded_by IS NULL "
        "ORDER BY produced_at DESC LIMIT 1"
    )
    return (row["body"] or "") if row else ""


def _render(inputs: list[dict[str, Any]], roster: str) -> str:
    from legba.data.analysts import journal_assessor as ja

    return ja._render_user_prompt(inputs, tier="entry", coverage_roster=roster)


def _entry_metrics(body: str, window: list[dict[str, Any]]) -> dict[str, Any]:
    """The per-entry numbers the report tabulates, all from shipped code."""
    from legba.data.analysts.journal_reflect import _reflect_claims
    from legba.data.analysts.journal_slice import _labeled_journal_slice

    claims, cited_refs, flags = _reflect_claims(body)
    labelled = {
        str(r.get("id")): r.get("journal_label")
        for r in _labeled_journal_slice(window)
    }
    cited = [str(c) for c in cited_refs]
    return {
        "body_chars": len(body),
        "claims": len(claims),
        "cited_refs": len(cited),
        "refs_per_claim": round(len(cited) / len(claims), 3) if claims else 0.0,
        "reflect_flags": sorted(flags),
        "cited_instrument": sum(1 for c in cited if labelled.get(c) == "instrument"),
        "cited_routine": sum(1 for c in cited if labelled.get(c) == "routine"),
        "cited_in_window": sum(1 for c in cited if c in labelled),
    }


async def cmd_replay(args: argparse.Namespace) -> int:
    import aiohttp

    from legba.data.analysts.journal_clusters import (
        CLUSTER_FIRST_ENV, cluster_pool, coverage_roster_block, journal_window,
    )
    from legba.prompts.journal_assessor import JOURNAL_SYSTEM

    endpoint = os.environ["LEGBA_LLM_API_ENDPOINT"]
    model = os.environ.get("LEGBA_LLM_MODEL_NAME", "gpt-oss-120b")
    key = await _core_key()

    conn = await _connect()
    try:
        world_body = await _newest_world_body(conn)
        pools = {}
        for raw_end in args.window_end:
            end = datetime.fromisoformat(raw_end.replace("Z", "+00:00"))
            # ``--leg 2`` reads the window through the shipped stratified leg;
            # everything after the fetch (diversity cap, selector, render,
            # narrator, grader) is identical, so the arm isolates the FETCH.
            pools[raw_end] = (
                await _delivered_inputs_v2(conn, end) if args.leg == 2
                else _delivered_inputs(await _pool_for_window(conn, end))
            )
    finally:
        await conn.close()
    roster = coverage_roster_block(world_body)

    out: list[dict[str, Any]] = []
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        llm = CorePlaneLLM(session, endpoint, key, model)
        for raw_end, inputs in pools.items():
            for arm in args.arms:
                if arm == "C":
                    os.environ[CLUSTER_FIRST_ENV] = "1"
                else:
                    os.environ.pop(CLUSTER_FIRST_ENV, None)
                rows = [dict(r) for r in inputs]
                window = journal_window(rows)
                prompt = _render(rows, roster if arm == "C" else "")
                headers = [ln for ln in prompt.splitlines() if ln.startswith("▸")]
                clusters = cluster_pool(rows) if arm == "C" else []
                for rnd in range(1, args.rounds + 1):
                    case: dict[str, Any] = {
                        "window_end": raw_end, "arm": arm, "round": rnd,
                        "leg": args.leg,
                        "prompt_chars": len(prompt),
                        "window_rows": len(window),
                        "cluster_headers": headers,
                        "roster_chars": len(roster) if arm == "C" else 0,
                        "top_clusters": [c.label() for c in clusters[:6]],
                    }
                    try:
                        body = await llm.complete(JOURNAL_SYSTEM, prompt)
                        case["body"] = body
                        case.update(_entry_metrics(body, window))
                    except Exception as exc:  # noqa: BLE001 — recorded, not hidden
                        case["error"] = f"{type(exc).__name__}: {exc}"
                    print(f"{raw_end} arm {arm} r{rnd}: "
                          f"claims={case.get('claims')} "
                          f"refs={case.get('cited_refs')} "
                          f"chars={case.get('body_chars')}")
                    out.append(case)
    os.environ.pop(CLUSTER_FIRST_ENV, None)
    Path(args.out).write_text(json.dumps(out, indent=2, default=str))
    print(f"wrote {args.out}")
    return 0


#: The judge route ``d6_clause_replay`` uses ($0, OpenRouter free tier).
JUDGE_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
JUDGE_URL = "https://openrouter.ai/api/v1/chat/completions"


class JudgeLLM:
    """The ``chat_complete`` surface ``verify._run_judge`` expects."""

    subprovider = "openrouter"

    def __init__(self, session: Any, key: str) -> None:
        self._s = session
        self._key = key
        self.calls = 0

    async def chat_complete(
        self, messages, *, max_tokens=16384, temperature=0.0, system=None, **kw
    ):
        self.calls += 1
        wire = ([{"role": "system", "content": system}] if system else []) + [
            dict(m) for m in messages
        ]
        last: Exception | None = None
        for _ in range(4):
            try:
                async with self._s.post(
                    JUDGE_URL,
                    json={"model": JUDGE_MODEL, "messages": wire,
                          "temperature": temperature, "max_tokens": max_tokens},
                    headers={"Authorization": f"Bearer {self._key}"},
                ) as resp:
                    resp.raise_for_status()
                    body = await resp.json()
                return _Resp(body["choices"][0]["message"]["content"] or "")
            except Exception as exc:  # noqa: BLE001 — retried, then surfaced
                last = exc
                await asyncio.sleep(3.0)
        raise RuntimeError(f"judge call failed after retries: {last}")


async def cmd_score(args: argparse.Namespace) -> int:
    """Grade every replayed entry through the SHIPPED journal verify path.

    ``journal_reflect._reflect_claims`` -> ``build_journal_verify_inputs`` ->
    ``actor_critic._resolve_journal_citation_bridge`` (read-only) ->
    ``verify.verify_finding_faithfulness``. Exactly the chain
    ``actor_critic._maybe_verify`` runs on a live journal row; the only thing
    this harness supplies is the payload shim, because the entries here were
    never written to ``analyst_outputs``.

    Two numbers per entry, the same pair ``d6_clause_replay`` reports: the
    DETERMINISTIC FLOOR (``judge_llm=None``) and, when ``OPENROUTER_API_KEY`` is
    present, the JUDGED ``citation_support``.
    """
    import aiohttp

    from types import SimpleNamespace

    from legba.data.analysts.journal_assessor import build_journal_verify_inputs
    from legba.data.analysts.journal_reflect import _reflect_claims
    from legba.data.provenance.verify import verify_finding_faithfulness
    from legba.runtime.actor_critic import _resolve_journal_citation_bridge

    cases = json.loads(Path(args.results).read_text())
    judge_key = os.environ.get("OPENROUTER_API_KEY") or ""
    if judge_key:
        os.environ["LEGBA_VERIFY_LLM_JUDGE"] = "1"
    conn = await _connect()
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            judge = JudgeLLM(session, judge_key) if judge_key else None
            for case in cases:
                body_text = case.get("body") or ""
                if not body_text:
                    case["faithfulness"] = None
                    continue
                claims, _refs, _flags = _reflect_claims(body_text)
                doc, ordered = build_journal_verify_inputs(
                    SimpleNamespace(claims=claims)
                )
                if not doc:
                    case["faithfulness"] = {"judgeable": False}
                    continue
                citations = await _resolve_journal_citation_bridge(conn, ordered)
                floor = await verify_finding_faithfulness(
                    body=doc, citations=citations, judge_llm=None,
                )
                out = {
                    "judgeable": True,
                    "claims_judged": len(getattr(floor, "claims", []) or []),
                    "deterministic_floor": round(
                        float(getattr(floor, "faithfulness_score", 0.0)), 4
                    ),
                }
                if judge is not None:
                    judged = await verify_finding_faithfulness(
                        body=doc, citations=citations, judge_llm=judge,
                    )
                    out["citation_support"] = round(
                        float(getattr(judged, "faithfulness_score", 0.0)), 4
                    )
                    out["judge_status"] = getattr(judged, "judge_status", None)
                    out["unsupported"] = [
                        s.text[:160]
                        for s in (getattr(judged, "unsupported_spans", None) or [])
                    ]
                case["faithfulness"] = out
                print(f"{case['window_end']} arm {case['arm']} r{case['round']}: "
                      f"floor={out['deterministic_floor']} "
                      f"cit={out.get('citation_support')}")
    finally:
        await conn.close()
    Path(args.out).write_text(json.dumps(cases, indent=2, default=str))
    print(f"wrote {args.out}")
    return 0


def main() -> int:
    _load_env()
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("measure")
    m.add_argument("--window-end", action="append", required=True)
    m.add_argument("--out", required=True)
    m.set_defaults(fn=cmd_measure)

    r = sub.add_parser("replay")
    r.add_argument("--window-end", action="append", required=True)
    r.add_argument("--rounds", type=int, default=2)
    r.add_argument("--arms", default="BC")
    r.add_argument("--leg", type=int, choices=(1, 2), default=1,
                   help="1 = the recency fetch (today); 2 = the T2.3 "
                        "salience-stratified fetch (LEGBA_JOURNAL_SLICE_V2)")
    r.add_argument("--timeout", type=float, default=900.0)
    r.add_argument("--out", required=True)
    r.set_defaults(fn=cmd_replay)

    s = sub.add_parser("score")
    s.add_argument("--results", required=True)
    s.add_argument("--timeout", type=float, default=1800.0)
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_score)

    args = ap.parse_args()
    return asyncio.run(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
