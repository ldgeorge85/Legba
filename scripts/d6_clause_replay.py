#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""d6_clause_replay.py — the WEAK-SPECIMEN CLAUSE's before/after, N=5 world spines.

WHAT THIS MEASURES. D-6 §7.6 diagnosed the weakest of its five replay specimens
(fidelity floor 0.545) as *"prose about the record's own machinery … an analyst
narrating an instrument rather than a world"* and named ONE prompt clause as the
lever — instrument-plan item N2, prereg item E-2. The first natural post-fix live
Assessment then measured the diagnosis exactly: ``citation_support`` **0.4286**
on 2026-09-06 00:15Z, with ALL FOUR ``soft_fail`` claims in that single class and
every substantive world claim supported. This script replays five real world
spines through the REAL channel path under both prompts and grades both arms
through the REAL verify path.

THE TWO ARMS
  A (before) : ``ASSESSMENT_SYSTEM`` as it stood at ``bf4e7e8a``, read from a
               JSON file emitted by ``emit-prompt`` against a baseline tree.
               A byte-level baseline, not a reconstruction of one.
  B (after)  : whatever ``assessment_prompts.ASSESSMENT_SYSTEM`` currently is.
               Read live, never copied, so this script cannot drift from the
               thing it claims to be measuring.

ONLY THE SYSTEM PROMPT MOVES. The user prompt (``build_assessment_prompt``), the
spine, the marker resolution, the evidence map, the temperature and the model are
identical across arms — the arm is planted by rebinding
``assessment_prompts.ASSESSMENT_SYSTEM``, which is the single name the run
resolves the WORLD voice from (P3 Lane A: ``run_assessment`` asks
``assessment_prompts.system_prompt_for(payload)``, which reads that name at call
time and answers the country constant for a country record). Everything else
runs byte-identically to production, which is what makes the contrast the clause
and not a confound.

THE FIVE SPINES, and how the legacy-era three are built. Two are live
``assembly.v1`` payloads (2026-09-05 12:00Z and 2026-09-06 00:00Z — the records
the two live Assessments actually read). The other three world cycles ran BEFORE
the assembly cutover and carry ``regime: legacy`` with no blocks, so their spines
are rebuilt the way D-6 built its five: D-2's OWN builder
(``assembly_payload.build_assembly``) over that run's OWN input heads, taken from
its ``derived_from``. Nothing is hand-written and nothing is invented — the
builder is the production one and the inputs are the run's own.

THE GRADER IS THE DEPLOYED ONE. ``verify.verify_finding_faithfulness`` over the
channel's own ``citations``, with the live judge (the same
``llm.judge.openrouter_nemotron120b.openai_compat`` route the live critique used,
$0). Both numbers are reported per arm:

  * ``citation_support`` — the JUDGED branch score. This is
    ``fidelity_to_spine`` as the live row measures it, and the number the G3 bar
    (>= 0.90) is about.
  * the DETERMINISTIC FLOOR (judge off) — the comparable of D-6 §7's five
    numbers (1.0 / 0.80 / 1.0 / 0.75 / 0.545), so this replay can be read
    against that one.

READ-ONLY, BY CONSTRUCTION. The Postgres connection is opened with
``default_transaction_read_only`` and every statement is a SELECT. Nothing is
written to the database, nothing is registered, nothing is deployed. Credentials
are read from the checkout's ``.env`` and never logged or written to the output.

THE G3 LANE (2026-09-06), and why it needs a SECOND TREE rather than a second
prompt. D-6's arms differed by one module constant, so both could be planted in
one process. G3's arms differ in the GRADER (``provenance.assessment_weighting``,
which does not exist on the base tree), in the EVIDENCE MAP (the declared
aperture rides into every citation) and in the MARKER SURFACE
(``aperture_unrostered``) as well as in the prompt. Rebinding four surfaces in
one process would measure a chimera, so each arm runs the tree it belongs to:

  A (before) : ``PYTHONPATH=<base d5ff79e7 checkout>/src`` — the v2 clause and
               the old grader, as the tree that produced D-6's own numbers.
  B (after)  : ``PYTHONPATH=src`` in this checkout — v2 clause + the licence +
               the roster.

The script itself is THIS tree's in both runs (``sys.path.append`` puts its own
``src`` LAST, so a ``PYTHONPATH`` entry wins), which is what lets one harness
drive two trees over the SAME spine file, the same core plane and the same judge
route. ``g3-score`` then joins the two result files by spine label.

USAGE
    python3 scripts/d6_clause_replay.py emit-prompt --out /tmp/arm_a.json
    python3 scripts/d6_clause_replay.py spines      --out /tmp/spines.json
    python3 scripts/d6_clause_replay.py replay \\
        --spines /tmp/spines.json --arm-a /tmp/arm_a.json --out /tmp/results.json
    python3 scripts/d6_clause_replay.py score --results /tmp/results.json

    # G3 — one arm per tree, then join
    PYTHONPATH=<base>/src python3 scripts/d6_clause_replay.py g3-arm \\
        --spines /tmp/spines.json --arm A --out /tmp/g3_a.json
    PYTHONPATH=src python3 scripts/d6_clause_replay.py g3-arm \\
        --spines /tmp/spines.json --arm B --out /tmp/g3_b.json
    python3 scripts/d6_clause_replay.py g3-score --arm-a /tmp/g3_a.json \\
        --arm-b /tmp/g3_b.json

    # G3 — the non-Assessment half, on both trees, then diff
    PYTHONPATH=<base>/src python3 scripts/d6_clause_replay.py legacy-regrade \\
        --out /tmp/legacy_a.json
    PYTHONPATH=src python3 scripts/d6_clause_replay.py legacy-regrade \\
        --out /tmp/legacy_b.json
    python3 scripts/d6_clause_replay.py legacy-diff --a /tmp/legacy_a.json \\
        --b /tmp/legacy_b.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
# APPENDED, not inserted — ``emit-prompt`` is run once against a BASELINE tree on
# PYTHONPATH and once against this one, and a path insert at position 0 would
# silently make both arms this checkout.
sys.path.append(str(REPO_ROOT / "src"))

#: The two live assembly spines, newest first. Pinned by id rather than by a
#: "newest 2" query so a re-run measures the same records.
LIVE_SPINE_IDS: tuple[str, ...] = (
    "d6894219-5a24-4ea7-b13d-a19691d76dac",  # 2026-09-06 00:00Z, 8 blocks
    "cfc16d1c-03e3-489e-88dd-22fe809e8389",  # 2026-09-05 12:00Z, 1 block
)

#: The three most recent PRE-CUTOVER world cycles, rebuilt from their own inputs.
LEGACY_RUN_IDS: tuple[str, ...] = (
    "41f1dbe3-b9a6-437b-a5ba-979ad2e1bd3b",  # 2026-09-05 00:00Z
    "cb1de0dc-3c2e-42df-8b92-9b6af710d92b",  # 2026-09-04 12:00Z
    "f8d80ddd-0a27-4821-a2c9-0f3775e18c8b",  # 2026-09-04 00:00Z
)

JUDGE_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
JUDGE_URL = "https://openrouter.ai/api/v1/chat/completions"


def _load_env() -> None:
    """The repo ``.env``, without clobbering anything already exported.

    A worktree has no ``.env`` of its own, so the deployment's is the fallback —
    the same resolution ``temp_title_frame_replay`` uses. Values are never
    printed, echoed or written to the results file.
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
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    # The script runs on the HOST; the container hostname does not resolve here.
    if os.environ.get("LEGBA_DATA_PG_HOST") in (
        None, "", "postgres", "legba-postgres-1",
    ):
        os.environ["LEGBA_DATA_PG_HOST"] = "127.0.0.1"


# ---------------------------------------------------------------------------
# emit-prompt
# ---------------------------------------------------------------------------


def cmd_emit_prompt(args: argparse.Namespace) -> int:
    import hashlib

    from legba.data.analysts import assessment_prompts as apr

    out = {
        "ASSESSMENT_SYSTEM": apr.ASSESSMENT_SYSTEM,
        "PROMPT_VERSION": apr.PROMPT_VERSION,
    }
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1))
    digest = hashlib.sha256(apr.ASSESSMENT_SYSTEM.encode()).hexdigest()
    print(
        f"{apr.PROMPT_VERSION}: {len(apr.ASSESSMENT_SYSTEM)} chars "
        f"sha256={digest[:16]}"
    )
    return 0


# ---------------------------------------------------------------------------
# spines
# ---------------------------------------------------------------------------


_LIVE_SQL = """
SELECT id::text, created_at, confidence, data -> 'data' -> 'assembly' AS assembly
FROM analyst_outputs
WHERE id = ANY($1::uuid[])
"""

#: The run's OWN input heads, in the shape ``build_assembly`` consumes. Every
#: column here is one the production slice read carries; nothing is synthesised.
_INPUTS_SQL = """
SELECT p.id, p.kind, p.analyst_id, p.target_id, p.title, p.body, p.severity,
       p.confidence, p.produced_at, p.derived_from, p.data,
       v.faithfulness_score AS faithfulness_score,
       LEAST(p.confidence, v.faithfulness_score) AS effective_confidence
FROM analyst_outputs w
JOIN analyst_outputs p ON p.id = ANY(w.derived_from)
LEFT JOIN LATERAL (
    SELECT (cr.data->>'overall_score')::real AS faithfulness_score
      FROM analyst_outputs cr
     WHERE cr.kind = 'critique'
       AND cr.data->>'analyzed_output_id' = p.id::text
       AND cr.data->>'overall_score' IS NOT NULL
       AND cr.title LIKE 'Faithfulness verify%'
     ORDER BY cr.produced_at DESC, cr.id DESC
     LIMIT 1
) v ON TRUE
WHERE w.id = $1::uuid
"""

_RUN_SQL = """
SELECT id::text, created_at, confidence
FROM analyst_outputs WHERE id = ANY($1::uuid[])
"""


async def _connect() -> Any:
    import asyncpg

    from legba.data.config import PostgresConfig

    cfg = PostgresConfig.from_env()
    return await asyncpg.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        database=cfg.database,
        server_settings={"default_transaction_read_only": "on"},
    )


def _as_row(record: Any) -> dict[str, Any]:
    """One live row as the assembler's input dict, with ``data`` decoded."""
    row = dict(record)
    raw = row.get("data")
    if isinstance(raw, str):
        row["data"] = json.loads(raw)
    produced = row.get("produced_at")
    if produced is not None:
        row["produced_at"] = produced.isoformat()
    return row


async def cmd_spines(args: argparse.Namespace) -> int:
    from legba.data.analysts import assembly_payload as ap

    conn = await _connect()
    try:
        spines: list[dict[str, Any]] = []

        for rec in await conn.fetch(_LIVE_SQL, list(LIVE_SPINE_IDS)):
            assembly = rec["assembly"]
            if isinstance(assembly, str):
                assembly = json.loads(assembly)
            spines.append({
                "spine_id": rec["id"],
                "label": rec["created_at"].strftime("%m-%d %H:%MZ") + " (live)",
                "origin": "live_assembly",
                "confidence": float(rec["confidence"] or 0.5),
                "payload": assembly,
            })

        runs = {r["id"]: r for r in await conn.fetch(_RUN_SQL, list(LEGACY_RUN_IDS))}
        for run_id in LEGACY_RUN_IDS:
            rows = [_as_row(r) for r in await conn.fetch(_INPUTS_SQL, run_id)]
            if not rows:
                print(f"skip {run_id}: no input heads on the row", file=sys.stderr)
                continue
            # D-2's own builder, over the run's own inputs — the same two calls
            # the production path makes around it (salience, then the order key
            # that also mints derived_from).
            ap.attach_cited_salience(rows, {})
            ordered = sorted(rows, key=ap.order_key)
            run = runs[run_id]
            payload = ap.build_assembly(
                tier=ap.TIER_WORLD,
                as_of=run["created_at"].isoformat(),
                candidates=ordered,
                carried=ordered[: ap.BLOCK_CAP],
            )
            spines.append({
                "spine_id": run_id,
                "label": run["created_at"].strftime("%m-%d %H:%MZ") + " (rebuilt)",
                "origin": "rebuilt_from_inputs",
                "confidence": float(run["confidence"] or 0.5),
                "payload": payload,
            })
    finally:
        await conn.close()

    spines.sort(key=lambda s: s["label"])
    Path(args.out).write_text(json.dumps(spines, ensure_ascii=False, indent=1))
    for s in spines:
        print(
            f"{s['label']:<24} {s['origin']:<20} "
            f"blocks={len(s['payload'].get('blocks') or [])}"
        )
    return 0


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def apr_module():
    """The tree's own ``assessment_prompts``, resolved at CALL time.

    Imported lazily and never aliased at module scope: this harness is executed
    with two different ``legba`` packages on the path, and a top-level import
    would bind whichever ran first.
    """
    from legba.data.analysts import assessment_prompts

    return assessment_prompts


class _Resp:
    def __init__(self, content: str, usage: Any = None) -> None:
        self.content = content
        self.usage = usage


class _Usage:
    prompt_tokens = 0
    completion_tokens = 0
    reasoning_tokens = 0


class CorePlaneLLM:
    """The ``chat_complete`` surface ``_reason_via_llm`` expects, on the wire.

    Plain HTTP rather than ``build_llm_handler_from_stack_component``: that
    builder needs a live registry client and the whole deps stack, and the only
    thing this replay needs is the same wire call the handler makes.
    ``max_tokens`` is deliberately NOT sent — the core plane never sends it, and
    an arm that truncated differently would be a confound rather than an arm.
    """

    subprovider = "core_plane_replay"

    def __init__(self, session: Any, endpoint: str, key: str, model: str) -> None:
        self._s = session
        self._ep = endpoint.rstrip("/")
        self._key = key
        self._model = model
        self.calls = 0

    async def chat_complete(self, messages, *, temperature=1.0, system=None, **kw):
        self.calls += 1
        wire = ([{"role": "system", "content": system}] if system else []) + [
            dict(m) for m in messages
        ]
        async with self._s.post(
            f"{self._ep}/v1/chat/completions",
            json={"model": self._model, "temperature": temperature,
                  "messages": wire},
            headers={"Authorization": f"Bearer {self._key}"},
        ) as resp:
            resp.raise_for_status()
            body = await resp.json()
        return _Resp(body["choices"][0]["message"]["content"] or "", _Usage())


class JudgeLLM:
    """The ``chat_complete`` surface ``verify._run_judge`` expects.

    Every system prompt is forwarded UNTOUCHED — this replay swaps the
    Assessment's voice prompt, never the grader's, which is what keeps the two
    arms comparable and keeps the number this script prints the same number the
    live critique produces.
    """

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


async def _core_key() -> str:
    """The core plane's key, from the vault the runtime uses. Never printed."""
    from legba.data.registry.credentials import CredentialVault

    vault = CredentialVault.from_env()
    await vault.store.connect()
    try:
        return (await vault.resolve("llm.primary.api_key")).decode()
    finally:
        await vault.store.close()


async def _one_arm(
    spine: dict[str, Any], *, system: str | None, llm: Any, judge: Any,
) -> dict[str, Any]:
    """One arm on one spine: the real channel run, then the real verify pass.

    ``system=None`` is THE G3 LANE: run the tree's own ``ASSESSMENT_SYSTEM``
    with nothing rebound, because in that lane the arm IS the tree (see the
    module banner). ``system=<bytes>`` is D-6's lane, where the two arms differ
    by exactly one module constant and both live in one process.
    """
    from legba.data.analysts import assessment_channel as ac
    from legba.data.analysts import assessment_unsupported as au
    from legba.data.analysts import meta_findings_synthesizer as synth
    from legba.data.provenance import verify as vf

    payload = spine["payload"]
    row = {
        "id": spine["spine_id"],
        "kind": "finding",
        "analyst_id": "world_assessor",
        "target_id": None,
        "title": "World read",
        "body": "rendered by the record",
        "confidence": spine["confidence"],
        "produced_at": payload.get("as_of"),
        "data": {"data": {"meta": True, "assembly": payload}},
    }
    # THE ARM. The one name the run resolves the world voice from, rebound for
    # the duration of this call and restored after it. Nothing else moves. In
    # the G3 lane nothing is rebound at all — the tree on ``PYTHONPATH`` is the
    # arm.
    #
    # P3 LANE A MOVED THIS NAME BY ONE MODULE, and the rebind has to follow it or
    # this script silently measures nothing. ``run_assessment`` no longer passes
    # a module constant directly: it asks ``assessment_prompts.system_prompt_for
    # (payload)``, which resolves the voice from the RECORD's tier (the country
    # channel has its own). That resolver reads ``apr.ASSESSMENT_SYSTEM`` at
    # call time, so THAT is the name an arm has to plant now —
    # ``ac.ASSESSMENT_SYSTEM`` is an inert re-export and rebinding it would
    # produce a clean-looking replay in which both arms ran the same prompt.
    #
    # STEP E MOVED IT AGAIN, CONDITIONALLY, and that is worse than a move: a
    # WORLD record that carries its countries' assessments now resolves to
    # ``WORLD_ASSESSMENT_SYSTEM_V4``, so a rebind of ``ASSESSMENT_SYSTEM``
    # would plant a prompt that is never read and BOTH arms would run v4. That
    # is the clean-looking replay of nothing this comment was written to stop,
    # so it is checked rather than warned about: a planted arm over a
    # context-carrying spine FAILS the run and says why.
    from legba.data.analysts import assessment_prompts as _apr

    if system is not None and _apr.has_context_spans(payload):
        raise SystemExit(
            "this spine carries context spans (STEP E), so the voice resolves "
            "to WORLD_ASSESSMENT_SYSTEM_V4 and rebinding ASSESSMENT_SYSTEM "
            "would measure nothing — both arms would run v4. Replay a "
            "pre-STEP-E world spine, or plant WORLD_ASSESSMENT_SYSTEM_V4 "
            "instead and say so in the run's label."
        )

    before = _apr.ASSESSMENT_SYSTEM
    if system is not None:
        _apr.ASSESSMENT_SYSTEM = system
    try:
        result = await synth._run(
            [row],
            {"analyst_id": ac.ASSESSMENT_ANALYST_ID},
            llm=llm,
            max_tokens=768,
            temperature=1.0,
            system_prompt="unused on this branch",
        )
    finally:
        _apr.ASSESSMENT_SYSTEM = before

    body = result.finding.body
    citations = result.finding.data.get("citations") or []
    marks, checked = au.find_unsupported(body, payload)

    judged = await vf.verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=judge,
        finding_confidence=result.finding.confidence, title=result.finding.title,
    )
    floor = await vf.verify_finding_faithfulness(
        body=body, citations=citations, judge_llm=None,
        finding_confidence=result.finding.confidence, title=result.finding.title,
    )
    branch = (judged.branch_scores or {}).get("citation_support") or {}
    return {
        "prompt_version": getattr(apr_module(), "PROMPT_VERSION", ""),
        "unsupported_version": checked.get("version"),
        "judge_pipeline_version": getattr(vf, "JUDGE_PIPELINE_VERSION", ""),
        # G3: the licence's own receipts, so the redistribution is a number.
        "weighted": {
            k: v for k, v in (judged.counters or {}).items()
            if k.startswith("weighted_comparison")
        },
        "title": result.finding.title,
        "body": body,
        "citations": len(citations),
        "judge_status": judged.judge_status,
        "citation_support": branch.get("score"),
        "citation_checkable": branch.get("checkable"),
        "citation_supported": branch.get("supported"),
        "faithfulness_score": judged.faithfulness_score,
        "deterministic_floor": floor.faithfulness_score,
        "checkable_claims": judged.checkable_claims,
        "supported_claims": judged.supported_claims,
        "marks_by_class": checked["by_class"],
        "marks": [
            {"class": m["class"], "text": m["text"]} for m in marks
        ],
        "unsupported_spans": [
            {"reason": s.reason, "text": s.text[:200]}
            for s in (judged.unsupported_spans or [])
        ],
    }


async def cmd_replay(args: argparse.Namespace) -> int:
    import aiohttp

    from legba.data.analysts import assembly_payload as ap
    from legba.data.analysts import assessment_prompts as apr

    os.environ[ap.ASSEMBLY_ENV] = "1"
    os.environ["LEGBA_VERIFY_LLM_JUDGE"] = "1"

    arm_a = json.loads(Path(args.arm_a).read_text())["ASSESSMENT_SYSTEM"]
    arm_b = apr.ASSESSMENT_SYSTEM
    if arm_a == arm_b:
        print("arm A and arm B are the same bytes — nothing to measure",
              file=sys.stderr)
        return 2

    spines = json.loads(Path(args.spines).read_text())
    if args.limit:
        spines = spines[: args.limit]

    endpoint = os.environ["LEGBA_LLM_API_ENDPOINT"]
    model = os.environ.get("LEGBA_LLM_MODEL_NAME", "gpt-oss-120b")
    core_key = await _core_key()
    judge_key = os.environ["OPENROUTER_API_KEY"]

    out: list[dict[str, Any]] = []
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        llm = CorePlaneLLM(session, endpoint, core_key, model)
        judge = JudgeLLM(session, judge_key)
        for spine in spines:
            case: dict[str, Any] = {
                "label": spine["label"],
                "spine_id": spine["spine_id"],
                "origin": spine["origin"],
                "blocks": len(spine["payload"].get("blocks") or []),
            }
            for arm, system in (("A", arm_a), ("B", arm_b)):
                if arm not in args.arms:
                    continue
                try:
                    case[arm] = await _one_arm(
                        spine, system=system, llm=llm, judge=judge,
                    )
                except Exception as exc:  # noqa: BLE001 — recorded, never hidden
                    case[arm] = {"error": f"{type(exc).__name__}: {exc}"}
                got = case[arm]
                print(
                    f"{spine['label']:<24} arm {arm}: "
                    f"cit={got.get('citation_support')} "
                    f"floor={got.get('deterministic_floor')} "
                    f"instrument={(got.get('marks_by_class') or {}).get('instrument_prose')}"
                    + (f"  ERROR {got['error']}" if "error" in got else "")
                )
            out.append(case)
            Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1))

    print(f"\nwrote {args.out} ({len(out)} cases)")
    return 0


# ---------------------------------------------------------------------------
# G3 — one arm per tree
# ---------------------------------------------------------------------------


async def cmd_g3_arm(args: argparse.Namespace) -> int:
    """Run THIS tree over the shared spine file. The tree is the arm."""
    import aiohttp

    from legba.data.analysts import assembly_payload as ap

    os.environ[ap.ASSEMBLY_ENV] = "1"
    os.environ["LEGBA_VERIFY_LLM_JUDGE"] = "1"

    spines = json.loads(Path(args.spines).read_text())
    if args.limit:
        spines = spines[: args.limit]

    endpoint = os.environ["LEGBA_LLM_API_ENDPOINT"]
    model = os.environ.get("LEGBA_LLM_MODEL_NAME", "gpt-oss-120b")
    core_key = await _core_key()
    judge_key = os.environ["OPENROUTER_API_KEY"]

    out: list[dict[str, Any]] = []
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        llm = CorePlaneLLM(session, endpoint, core_key, model)
        judge = JudgeLLM(session, judge_key)
        for spine in spines:
            case: dict[str, Any] = {
                "label": spine["label"],
                "spine_id": spine["spine_id"],
                "origin": spine["origin"],
                "blocks": len(spine["payload"].get("blocks") or []),
                "arm": args.arm,
            }
            try:
                case["result"] = await _one_arm(
                    spine, system=None, llm=llm, judge=judge,
                )
            except Exception as exc:  # noqa: BLE001 — recorded, never hidden
                case["result"] = {"error": f"{type(exc).__name__}: {exc}"}
            got = case["result"]
            print(
                f"{spine['label']:<24} arm {args.arm}: "
                f"cit={got.get('citation_support')} "
                f"floor={got.get('deterministic_floor')} "
                f"marks={got.get('marks_by_class')} "
                f"weighted={got.get('weighted')}"
                + (f"  ERROR {got['error']}" if "error" in got else "")
            )
            out.append(case)
            Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1))

    print(f"\nwrote {args.out} ({len(out)} cases)")
    return 0


def _cls(result: dict[str, Any], name: str) -> Any:
    return (result.get("marks_by_class") or {}).get(name)


def cmd_g3_score(args: argparse.Namespace) -> int:
    a_cases = {c["label"]: c for c in json.loads(Path(args.arm_a).read_text())}
    b_cases = {c["label"]: c for c in json.loads(Path(args.arm_b).read_text())}
    labels = sorted(set(a_cases) | set(b_cases))

    print(
        f"{'spine':<24} {'blk':>4} {'A cit':>7} {'B cit':>7} "
        f"{'A flr':>7} {'B flr':>7} {'A ip':>5} {'B ip':>5} {'B ap':>5}"
    )
    print("-" * 84)
    for label in labels:
        a = (a_cases.get(label) or {}).get("result") or {}
        b = (b_cases.get(label) or {}).get("result") or {}
        blocks = (a_cases.get(label) or b_cases.get(label) or {}).get("blocks")
        print(
            f"{label:<24} {str(blocks):>4} "
            f"{str(a.get('citation_support')):>7} "
            f"{str(b.get('citation_support')):>7} "
            f"{str(a.get('deterministic_floor')):>7} "
            f"{str(b.get('deterministic_floor')):>7} "
            f"{str(_cls(a, 'instrument_prose')):>5} "
            f"{str(_cls(b, 'instrument_prose')):>5} "
            f"{str(_cls(b, 'aperture_unrostered')):>5}"
        )
    print("-" * 84)
    for name, cases in (("A", a_cases), ("B", b_cases)):
        rows = [(c.get("result") or {}) for c in cases.values()]
        cit = [r.get("citation_support") for r in rows]
        real = [v for v in cit if v is not None]
        weighted: dict[str, int] = {}
        for r in rows:
            for k, v in (r.get("weighted") or {}).items():
                weighted[k] = weighted.get(k, 0) + int(v)
        print(
            f"arm {name}: mean cit={_mean(cit)} "
            f"mean floor={_mean([r.get('deterministic_floor') for r in rows])} "
            f">=0.90 on {sum(1 for v in real if v >= 0.90)}/{len(real)} "
            f"prompt={rows[0].get('prompt_version') if rows else '?'} "
            f"stamp={rows[0].get('judge_pipeline_version') if rows else '?'}"
        )
        if weighted:
            print(f"        weighted-comparison counters: {weighted}")

    # THE PER-CLASS REDISTRIBUTION, which is what §4.4 of the D-6 report was for.
    for name, cases in (("A", a_cases), ("B", b_cases)):
        census: dict[str, int] = {}
        for c in cases.values():
            for span in ((c.get("result") or {}).get("unsupported_spans") or []):
                census[span["reason"]] = census.get(span["reason"], 0) + 1
        print(f"arm {name} reason census: {dict(sorted(census.items()))}")
    return 0


# ---------------------------------------------------------------------------
# G3 — the NON-ASSESSMENT half: the legacy population, re-graded on both trees
# ---------------------------------------------------------------------------

_LEGACY_SQL = """
SELECT p.id::text AS finding_id, p.title, p.body,
       p.data -> 'data' -> 'citations' AS citations,
       p.confidence
FROM analyst_outputs p
WHERE p.kind = 'finding'
  AND p.data -> 'data' -> 'assembly' ->> 'regime' = 'legacy'
  AND p.data -> 'data' -> 'citations' IS NOT NULL
ORDER BY p.produced_at DESC
LIMIT $1
"""


async def cmd_legacy_regrade(args: argparse.Namespace) -> int:
    """Re-grade the LEGACY-regime population through the real pass, judge OFF.

    Judge off on purpose: the arms differ deterministically or not at all, and a
    live judge at temperature would put sampling noise on top of a byte-identity
    claim. The deterministic floor plus every fold is exactly the surface this
    train could have moved.
    """
    from legba.data.provenance import verify as vf

    conn = await _connect()
    try:
        rows = [dict(r) for r in await conn.fetch(_LEGACY_SQL, args.limit)]
    finally:
        await conn.close()

    out: dict[str, Any] = {}
    for row in rows:
        citations = row["citations"]
        if isinstance(citations, str):
            citations = json.loads(citations)
        report = await vf.verify_finding_faithfulness(
            body=row["body"] or "",
            citations=citations,
            judge_llm=None,
            finding_confidence=float(row["confidence"] or 0.5),
            title=row["title"] or "",
        )
        out[row["finding_id"]] = report.as_dict()
    Path(args.out).write_text(
        json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True, default=str)
    )
    print(f"wrote {args.out} ({len(out)} legacy-regime findings re-graded)")
    return 0


#: The one field that is EXPECTED to differ across two trees and is not a graded
#: behaviour: the stamp is the LABEL of the change. Excluding it is what makes
#: the comparison a statement about grading rather than about versioning — and
#: the run prints the field-level census either way, so the exclusion cannot
#: hide a second difference behind the first.
_STAMP_FIELD = "judge_pipeline_version"


def cmd_legacy_diff(args: argparse.Namespace) -> int:
    a = json.loads(Path(args.a).read_text())
    b = json.loads(Path(args.b).read_text())
    ids = sorted(set(a) | set(b))

    census: dict[str, int] = {}
    for fid in ids:
        left, right = a.get(fid) or {}, b.get(fid) or {}
        for field in sorted(set(left) | set(right)):
            lv = json.dumps(left.get(field), sort_keys=True, default=str)
            rv = json.dumps(right.get(field), sort_keys=True, default=str)
            if lv != rv:
                census[field] = census.get(field, 0) + 1
    print(f"fields that differ, over {len(ids)} findings: "
          f"{dict(sorted(census.items())) or 'none'}")

    same = 0
    for fid in ids:
        left = {k: v for k, v in (a.get(fid) or {}).items() if k != _STAMP_FIELD}
        right = {k: v for k, v in (b.get(fid) or {}).items() if k != _STAMP_FIELD}
        ls = json.dumps(left, sort_keys=True, default=str)
        rs = json.dumps(right, sort_keys=True, default=str)
        if ls == rs:
            same += 1
        else:
            print(f"DIFF {fid}")
            print(f"  A {ls[:400]}")
            print(f"  B {rs[:400]}")
    print(f"{same}/{len(ids)} BYTE-IDENTICAL verification dicts "
          f"(excluding {_STAMP_FIELD}, which is the change's own label)")
    return 0 if same == len(ids) else 1


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------


def _mean(values: Sequence[float]) -> float | None:
    real = [v for v in values if v is not None]
    return round(sum(real) / len(real), 4) if real else None


def cmd_score(args: argparse.Namespace) -> int:
    cases = json.loads(Path(args.results).read_text())
    print(
        f"{'spine':<24} {'blocks':>6} {'A cit':>7} {'B cit':>7} "
        f"{'A floor':>8} {'B floor':>8} {'A ip':>5} {'B ip':>5}"
    )
    print("-" * 78)
    for c in cases:
        a, b = c.get("A") or {}, c.get("B") or {}
        print(
            f"{c['label']:<24} {c['blocks']:>6} "
            f"{str(a.get('citation_support')):>7} {str(b.get('citation_support')):>7} "
            f"{str(a.get('deterministic_floor')):>8} "
            f"{str(b.get('deterministic_floor')):>8} "
            f"{str((a.get('marks_by_class') or {}).get('instrument_prose')):>5} "
            f"{str((b.get('marks_by_class') or {}).get('instrument_prose')):>5}"
        )
    print("-" * 78)
    for arm in ("A", "B"):
        arms = [c.get(arm) or {} for c in cases]
        cit = [x.get("citation_support") for x in arms]
        print(
            f"arm {arm}: mean citation_support={_mean(cit)} "
            f"mean floor={_mean([x.get('deterministic_floor') for x in arms])} "
            f">=0.90 on {sum(1 for v in cit if v is not None and v >= 0.90)}"
            f"/{len([v for v in cit if v is not None])}"
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    _load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(required=True)

    p = sub.add_parser("emit-prompt")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_emit_prompt)

    p = sub.add_parser("spines")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_spines)

    p = sub.add_parser("replay")
    p.add_argument("--spines", required=True)
    p.add_argument("--arm-a", required=True, help="emit-prompt output of the BASE tree")
    p.add_argument("--out", required=True)
    p.add_argument("--arms", default="AB")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--timeout", type=float, default=900.0)
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("score")
    p.add_argument("--results", required=True)
    p.set_defaults(fn=cmd_score)

    p = sub.add_parser("g3-arm", help="run THIS tree over the shared spines")
    p.add_argument("--spines", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--arm", default="B")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--timeout", type=float, default=900.0)
    p.set_defaults(fn=cmd_g3_arm)

    p = sub.add_parser("g3-score", help="join two g3-arm result files")
    p.add_argument("--arm-a", required=True)
    p.add_argument("--arm-b", required=True)
    p.set_defaults(fn=cmd_g3_score)

    p = sub.add_parser("legacy-regrade")
    p.add_argument("--out", required=True)
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(fn=cmd_legacy_regrade)

    p = sub.add_parser("legacy-diff")
    p.add_argument("--a", required=True)
    p.add_argument("--b", required=True)
    p.set_defaults(fn=cmd_legacy_diff)

    args = parser.parse_args(argv)
    out = args.fn(args)
    return asyncio.run(out) if asyncio.iscoroutine(out) else out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
