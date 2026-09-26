# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""TITLE-FRAME-FIX — the composition-tier A/B prompt replay.

The measurement this train ships on, and the FIRST composition-tier replay the
house has run: ``VOICE_REPLAY_2026-08-20/REPLAY_REPORT.md`` deferred all four
composition variants (*"The prompts are code constants … the draft is not a
string swap"*), so the tier that produces the Morning Read had been measured
zero times when ``VOICE_ORGANIC_REVIEW_2026-09-01`` was written.

WHAT IT DOES. For each of N real recent world / region composition runs it
reconstructs that run's USER prompt from the run's own inputs, then calls the
core plane TWICE — once under the shipped system prompt (arm A) and once under
this train's (arm B) — and scores both arms with the same deterministic
classifier the frame gauge uses.

THE THREE SUBCOMMANDS, and why they are separate processes:

    emit-prompts   dump the four composition SYSTEM prompts from whichever tree
                   is on PYTHONPATH. Run it once against the baseline checkout
                   and once against this one; that is what makes arm A a real
                   BYTE-LEVEL baseline rather than a reconstruction of one.
    replay         reconstruct user prompts + call both arms. Runs under the
                   CURRENT tree, because ``_orient`` / ``_render_user_prompt``
                   are untouched by this train and the user prompt is therefore
                   arm-invariant — which the script ASSERTS rather than assumes.
    score          the gauge over both arms' titles, side by side.

BOTH ERROR DIRECTIONS, per the VOICE-3 precedent. Arm B has to move the frame
rate DOWN and hold the roll-call rate at ZERO: the frame this train removes was
itself installed on 2026-08-03 as the cure for the pre-07-01 headline that named
nobody (``World situational assessment — 2026-06-16``, 60 of 60). A replay that
only measured the frame would score a walk back into that as a total success.

READ-ONLY against the database, by construction: the connection is opened with
``default_transaction_read_only`` and every statement is a SELECT. It writes
nothing to Postgres and registers nothing.
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
# APPENDED, not inserted. ``emit-prompts`` is run once against a BASELINE tree
# on PYTHONPATH and once against this one, and the whole point of arm A is that
# it is the baseline's bytes — a path insert at position 0 would silently make
# both arms this checkout and the replay would compare the change to itself.
sys.path.append(str(REPO_ROOT / "src"))

#: The four composition system prompts, by the module attribute that holds them.
#: ``run_method`` picks between them on target shape
#: (``meta_findings_synthesizer.py:3290-3302``); the two this replay exercises
#: are the world and region ones.
PROMPT_ATTRS: tuple[str, ...] = (
    "_COMPOSITION_SYSTEM",
    "_REGION_COMPOSITION_SYSTEM",
    "_WORLD_OVER_REGIONS_SYSTEM",
    "_THEMATIC_COMPOSITION_SYSTEM",
)

#: analyst_id -> the system prompt that analyst's runs actually used.
ANALYST_PROMPT: dict[str, str] = {
    "world_assessor": "_WORLD_OVER_REGIONS_SYSTEM",
    "region_composition": "_REGION_COMPOSITION_SYSTEM",
}

CORE_COMPONENT = "llm.primary.openai_compat"


def _load_env() -> None:
    """The repo ``.env``, without clobbering anything already exported."""
    env = REPO_ROOT / ".env"
    if not env.is_file():  # a worktree has no .env of its own
        env = Path("/usr/local/deployments/active/legba/.env")
    if not env.is_file():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    # The scripts run on the HOST; the container hostname does not resolve here.
    if os.environ.get("LEGBA_DATA_PG_HOST") in (None, "", "postgres", "legba-postgres-1"):
        os.environ["LEGBA_DATA_PG_HOST"] = "127.0.0.1"


# ---------------------------------------------------------------------------
# emit-prompts
# ---------------------------------------------------------------------------


def cmd_emit_prompts(args: argparse.Namespace) -> int:
    from legba.data.analysts import composition_prompts as cp

    out = {name: getattr(cp, name) for name in PROMPT_ATTRS if hasattr(cp, name)}
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1))
    for name, prompt in out.items():
        print(f"{name}: {len(prompt)} chars")
    return 0


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


# STRATIFIED PER ANALYST, not simply "the N most recent runs". The region tier
# runs ~6x per world cycle, so a flat ORDER BY would hand a 12-case replay one
# world read and eleven region ones — and the world tier is the locked one
# (93.1% vs the region tier's 30.7% under the conservative reading). Taking the
# top K of EACH keeps the arm that matters most from being a sample of one.
_CASES_SQL = """
SELECT run_id, analyst_id, target_id, run_started_at, input_row_refs, live_title
FROM (
    SELECT t.run_id, t.analyst_id, t.target_id, t.run_started_at,
           t.input_row_refs, o.title AS live_title,
           ROW_NUMBER() OVER (
               PARTITION BY t.analyst_id ORDER BY t.run_started_at DESC
           ) AS rn
    FROM analyst_traces t
    JOIN LATERAL (
        SELECT ao.title
        FROM analyst_outputs ao
        WHERE ao.run_id = t.run_id AND ao.kind = 'finding'
          AND (ao.data -> 'data' ->> 'meta') = 'true'
        ORDER BY ao.produced_at DESC
        LIMIT 1
    ) o ON TRUE
    WHERE t.analyst_id = ANY($1::text[])
      AND t.status = 'success'
      AND array_length(t.input_row_refs, 1) > 1
) ranked
WHERE rn <= $2
ORDER BY analyst_id, run_started_at DESC
"""

_INPUTS_SQL = """
SELECT id, analyst_id, title, body, confidence, data, produced_at, target_id
FROM analyst_outputs
WHERE id = ANY($1::uuid[])
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


async def _api_key() -> str:
    from legba.data.registry.credentials import CredentialVault

    vault = CredentialVault.from_env()
    await vault.store.connect()
    try:
        return (await vault.resolve("llm.primary.api_key")).decode()
    finally:
        await vault.store.close()


def _render_case_user_prompt(rows: Sequence[dict[str, Any]]) -> str:
    """The run's core user prompt, through the REAL renderers.

    ``render_prompt_pack.py``'s documented BEST-EFFORT reconstruction: real
    ``_orient`` + ``_render_user_prompt`` over the run's own input rows. The
    runtime-conditional blocks (salience lead, freshness advisory, continuity,
    contested facts, coverage) are NOT replayed — they read live state at run
    time. That is a known and stated limit, and it is arm-INVARIANT: both arms
    see the identical user prompt, so a missing block cannot favour either one.
    """
    from legba.data.analysts import meta_findings_synthesizer as meta

    sliced, _derived, contributing = meta._orient(list(rows))
    return meta._render_user_prompt(sliced, contributing, include_source_ids=True)


async def _call(session: Any, endpoint: str, key: str, model: str,
                system: str, user: str, temperature: float) -> str:
    """One core-plane chat completion.

    Plain HTTP rather than ``build_llm_handler_from_stack_component``: that
    builder needs a live registry client and the whole deps stack, and the only
    thing this replay needs from the provider is the same wire call the handler
    makes. ``max_tokens`` is deliberately NOT sent — the core plane never sends
    it (``vllm.py``), and an arm that truncated differently would not be an
    arm, it would be a confound.
    """
    payload = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    async with session.post(
        f"{endpoint.rstrip('/')}/v1/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {key}"},
    ) as resp:
        resp.raise_for_status()
        body = await resp.json()
    return body["choices"][0]["message"]["content"]


def _extract_title(raw: str) -> str:
    """The ``title`` field of a strict-JSON composition response.

    Tolerant on purpose: the composition envelope is strict JSON but the tier
    has shipped a malformed one before (``WORLD_READ_JSON_LEAK.md``), and a
    parse failure must be REPORTED as one rather than silently dropping a case
    and shrinking whichever arm happened to fail.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        return str(json.loads(text).get("title") or "")
    except Exception:
        import re

        m = re.search(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        return json.loads(f'"{m.group(1)}"') if m else ""


async def _replay(args: argparse.Namespace) -> int:
    import aiohttp

    arm_a = json.loads(Path(args.arm_a).read_text())
    arm_b = json.loads(Path(args.arm_b).read_text())
    key = await _api_key()
    conn = await _connect()
    try:
        cases = await conn.fetch(_CASES_SQL, list(ANALYST_PROMPT), args.limit)
        prepared: list[dict[str, Any]] = []
        for case in cases:
            rows = [dict(r) for r in await conn.fetch(
                _INPUTS_SQL, list(case["input_row_refs"])
            )]
            if len(rows) < 2:
                continue
            try:
                user = _render_case_user_prompt(rows)
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                print(f"  ! render failed {case['run_id']}: {exc!r}")
                continue
            prepared.append({
                "run_id": str(case["run_id"]),
                "analyst_id": case["analyst_id"],
                "target_id": case["target_id"],
                "run_started_at": str(case["run_started_at"]),
                "live_title": case["live_title"],
                "n_inputs": len(rows),
                "user": user,
            })
    finally:
        await conn.close()

    print(f"prepared {len(prepared)} cases")
    out_path = Path(args.out)
    results: list[dict[str, Any]] = []
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for i, case in enumerate(prepared, 1):
            attr = ANALYST_PROMPT[case["analyst_id"]]
            row: dict[str, Any] = {k: v for k, v in case.items() if k != "user"}
            row["prompt_attr"] = attr
            row["user_chars"] = len(case["user"])
            for arm, prompts in (("A", arm_a), ("B", arm_b)):
                if arm not in args.arms:
                    continue
                system = prompts[attr]
                titles: list[str] = []
                for rep in range(args.reps):
                    try:
                        raw = await _call(
                            session, args.endpoint, key, args.model,
                            system, case["user"], args.temperature,
                        )
                        titles.append(_extract_title(raw))
                    except Exception as exc:  # noqa: BLE001
                        titles.append("")
                        row.setdefault("errors", []).append(f"{arm}{rep}:{exc!r}")
                row[f"arm_{arm}_titles"] = titles
                row[f"arm_{arm}_system_chars"] = len(system)
            results.append(row)
            print(f"[{i}/{len(prepared)}] {case['analyst_id']} {case['target_id']}")
            print(f"    live: {case['live_title']}")
            for arm in args.arms:
                for t in row[f"arm_{arm}_titles"]:
                    print(f"    {arm}: {t}")
            out_path.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"wrote {out_path}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    return asyncio.run(_replay(args))


# ---------------------------------------------------------------------------
# score
# ---------------------------------------------------------------------------


def _arm_stats(titles: Sequence[str]) -> dict[str, Any]:
    from legba.data.analysts.deterministic_handlers import _title_frame_gauge as g

    real = [t for t in titles if t.strip()]
    n = len(real)
    if not n:
        return {"n": 0}
    verdicts = [g.classify_frame(t) for t in real]
    dims: set[str] = set()
    for t in real:
        dims |= g.title_dimensions(t)
    longest, churn = g.streak_and_churn(real)
    return {
        "n": n,
        "frame": sum(v.strict for v in verdicts),
        "frame_rate": round(sum(v.strict for v in verdicts) / n, 4),
        "frame_core": sum(v.core for v in verdicts),
        "frame_rate_core": round(sum(v.core for v in verdicts) / n, 4),
        "roll_call": sum(g.is_roll_call(t) for t in real),
        "roll_call_rate": round(sum(g.is_roll_call(t) for t in real) / n, 4),
        "masthead": sum(g.is_masthead(t) for t in real),
        "multi_subject": sum(len(g.entity_tokens(t)) >= 2 for t in real),
        "mean_entities": round(sum(len(g.entity_tokens(t)) for t in real) / n, 2),
        "mean_chars": round(sum(len(t) for t in real) / n, 1),
        "over_90_chars": sum(len(t) > 90 for t in real),
        "dimension_coverage": len(dims),
        "dimensions": sorted(dims),
        "crown_streak_max": longest,
        "crown_churn": None if churn is None else round(churn, 4),
    }


def cmd_score(args: argparse.Namespace) -> int:
    results = json.loads(Path(args.results).read_text())
    arms = {
        "LIVE": [r["live_title"] for r in results],
        "A": [t for r in results for t in r.get("arm_A_titles", [])],
        "B": [t for r in results for t in r.get("arm_B_titles", [])],
    }
    if args.also:
        # A revised arm, replayed over the SAME pinned run_ids. Joined on run_id
        # rather than on position so a dropped case cannot silently misalign the
        # comparison.
        extra = json.loads(Path(args.also).read_text())
        by_run = {r["run_id"]: r for r in extra}
        arms[args.also_label] = [
            t
            for r in results
            for t in by_run.get(r["run_id"], {}).get("arm_B_titles", [])
        ]
    stats = {arm: _arm_stats(t) for arm, t in arms.items()}
    print(json.dumps(stats, indent=1))
    if args.out:
        Path(args.out).write_text(json.dumps(
            {"stats": stats, "titles": arms}, ensure_ascii=False, indent=1
        ))
    return 0


# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    _load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("emit-prompts")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_emit_prompts)

    p = sub.add_parser("replay")
    p.add_argument("--arm-a", required=True)
    p.add_argument("--arm-b", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--limit", type=int, default=12)
    p.add_argument("--reps", type=int, default=1)
    # ARM SELECTION. A second replay of a REVISED arm B does not need to
    # re-run arm A: arm A's prompt has not moved, the case set is pinned by
    # run_id, and re-calling it would only add sampling noise to a baseline
    # that is already measured on exactly these cases.
    p.add_argument("--arms", default="AB")
    p.add_argument("--temperature", type=float, default=1.0)
    p.add_argument("--endpoint", default=os.environ.get("LEGBA_REPLAY_ENDPOINT", ""),
                   help="core-plane OpenAI-compatible base URL (env LEGBA_REPLAY_ENDPOINT; required)")
    p.add_argument("--model", default=os.environ.get("LEGBA_REPLAY_MODEL", "gpt-oss-120b"),
                   help="served model name (env LEGBA_REPLAY_MODEL)")
    p.add_argument("--timeout", type=float, default=600.0)
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("score")
    p.add_argument("--results", required=True)
    p.add_argument("--also", help="a second results file whose arm B is a REVISED arm")
    p.add_argument("--also-label", default="B2")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_score)

    args = parser.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
