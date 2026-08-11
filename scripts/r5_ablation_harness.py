#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""r5_ablation_harness.py — the hostile A/B: tower composition vs a one-shot baseline.

WHY THIS EXISTS
---------------
Every standing review converges on the same missing experiment.

  * ``planning/review3.md`` §5, §9 — "No baseline comparison anywhere. […] the
    central comparative question — *is any of this better than a much dumber
    thing?* — has no experiment." The named fix: "pick three desks, run Legba's
    verified composition against a $0.02 GPT-mini summary of the same 72h slice,
    blind-grade both, publish the delta."
  * ``planning/review1.md`` §3.5, §9 — "227k lines of pipeline and no A/B against
    the one-shot baseline it was built to beat. […] If no reported number moves,
    that is the most important finding the project will ever produce."
  * ``planning/review2.md`` — same gap, same shape.

This harness BUILDS that experiment. It does not grade it, does not deploy
anything, and never writes to the substrate.

WHAT THE TWO ARMS ARE
---------------------
TOWER arm
    The live, already-produced ``country_composition`` finding for the desk — the
    head (non-superseded) row, pulled read-only. **The tower is never re-run.**
    Behind that one row sit the seven-to-nine unit findings it composes, each of
    which ran its own inline_target REASON pass with GATHER, citation extraction,
    the faithfulness judge, and the composition verify floor.

BASELINE arm
    ONE one-shot core-plane completion over the SAME evidence window. No units,
    no verify, no judge, no composition, no continuity memory, no situation
    register, no grounding. A single prompt and a single answer — deliberately
    the dumbest thing that could possibly work.

The arms are therefore separated by the ENTIRE tower, holding evidence and model
family fixed. That is the ablation.

EVIDENCE-WINDOW FIDELITY (the load-bearing part)
-----------------------------------------------
The baseline must see what the units saw — no more, no less — or the comparison
measures evidence access instead of architecture. So the slice is reconstructed
through the LIVE readers rather than re-derived:

  1. **Window.** Each unit is an ``inline_target`` whose descriptor declares
     ``subscription.targets.time_window = "72h"`` (verified live for all eight
     unit analysts). ``_read_substrate_slice`` reads ``fetched_at > NOW() - 72h``
     at RUN time, so the window is anchored to when the units actually ran:

         win_end   = max(produced_at) over the composition's ``derived_from`` units
         win_start = min(produced_at) over those units  -  72h

     This is the UNION of the per-unit windows. It is a superset of any single
     unit's window (the units ran minutes-to-hours apart), which if anything hands
     the baseline slightly MORE evidence than any one unit had. That asymmetry is
     deliberate and points AGAINST the tower — a hostile A/B should never
     handicap the baseline.

  2. **Predicates.** Byte-for-byte the reader's:
     ``geo && ARRAY[cc]`` (the target's ``scope.geo``; every country target
     carries ``source_id: null``, so geo is the only discriminator),
     ``SIGNALS_EXCLUDE_BACKFILL_SQL``, and the canonical-only clause.

  3. **Selection.** The reader's own functions, imported not reimplemented:
     ``actor_substrate_slice._diversify_by_source`` (per-source cap 15, row cap
     120) then ``inline_target._orient`` (recency sort, dead-row drop, INPUT-token
     budget pack under ``LEGBA_LLM_INPUT_TOKEN_BUDGET``).

  4. **Rendering.** ``inline_target._render_signal`` — the exact block format the
     units read, including the ``ingested=`` / ``published=`` provenance line that
     exists so a model cannot mistake fetch time for event time.

  What the baseline does NOT get, because they are not signals: the
  ``[ASSESSED STRUCTURE]`` graph-metric pseudo-rows and the QW1-B desk-grounding
  rows (prior read / situation register / desk baseline / open questions). Those
  are tower machinery, and handing them to the baseline would leak the tower into
  the control arm. Recorded in the bundle as ``tower_only_context``.

DESK SELECTION (deterministic — no cherry-picking)
--------------------------------------------------
Three desks spanning the volume regimes, chosen by rule over the LIVE per-desk
in-window signal count, ties broken by ``descriptor_id`` ascending:

  BUSY    max volume among ``country_watch_*`` (the active-theatre desks)
  MEDIUM  the ``country_g20_*`` desk at the LOW MEDIAN of all G20 desk volumes
  QUIET   min volume across all 32 country desks

The rule is evaluated and its full ranking table is recorded in the bundle, so
the picks are auditable and re-derivable rather than asserted.

COST / SAFETY
-------------
  * LLM: exactly ONE completion per desk (3 total) on the $0 core plane
    ``llm.primary.openai_compat``. No billed plane is reachable from this script —
    the component id is a module constant and is asserted before the call.
  * DB: a single asyncpg pool opened with server-enforced
    ``default_transaction_read_only=on``. A stray write raises rather than lands.
  * Nothing is persisted to the substrate, no descriptor is touched, no container
    is rebuilt, no analyst is forced.

USAGE
-----
    python3 scripts/r5_ablation_harness.py run \
        --env-file /usr/local/deployments/active/legba/.env \
        --out /path/to/scratch/R5_BUNDLE.json

    # Dry-run the selection + slices with NO LLM call (0 completions):
    python3 scripts/r5_ablation_harness.py run --no-llm --out ...

    # Override the picks (e.g. operator wants a different quiet desk):
    python3 scripts/r5_ablation_harness.py run --desks country_watch_ir,country_g20_sa,country_watch_bf ...

Render the operator-facing pack from the bundle with
``scripts/r5_ablation_render.py``; score the filled sheet with
``scripts/r5_ablation_score.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

# ---------------------------------------------------------------------------
# Constants — every one of these is a live-verified fact, not a guess.
# ---------------------------------------------------------------------------

#: The $0 self-hosted plane. HARD constraint: this harness must never reach a
#: billed plane (``llm.anthropic.*``, ``llm.judge.cerebras.*``). Asserted below.
CORE_COMPONENT = "llm.primary.openai_compat"

#: The composing analyst whose head output is the TOWER arm.
COMPOSITION_ANALYST = "country_composition"

#: Live descriptor value for every unit analyst that feeds a country composition
#: (``subscription.targets.time_window``). Verified against the live
#: ``analyst_descriptors`` heads at build time; the harness re-verifies and
#: refuses to proceed on a mismatch rather than silently replaying a wrong window.
UNIT_WINDOW_HOURS = 72

#: ``actor_substrate_slice._slice_row_cap`` default. The per-source cap is read
#: through the live ``_global_slice_per_source_cap()`` rather than duplicated, so
#: a tuned deployment's cap flows through without editing this file.
DEFAULT_ROW_CAP = 120

#: Live plane sampling pin (``vllm._DEFAULT_TEMPERATURE``; the whole fleet was
#: moved to 1.0 per the gpt-oss model card). The tower arm was produced at this
#: temperature, so the baseline runs there too — the ablation is the pipeline,
#: not the sampler.
BASELINE_TEMPERATURE = 1.0

#: Deliberately minimal. Anything more — a house style, an evidence discipline, a
#: structure — would be smuggling the tower's prompt engineering into the control.
BASELINE_SYSTEM = "You are an intelligence analyst. Write in plain prose."

#: The ONE concession to blinding (see ``r5_ablation_render.py`` and the grading
#: protocol): the tower's output carries citation markers, so a baseline forbidden
#: from citing would be identifiable at a glance and the blind grade would be
#: worthless. A single clause granting the same permission is not a pipeline — it
#: adds no unit, no retrieval, no verification pass. Flagged in the pack so the
#: operator can reject the concession and re-run without it via --no-cite.
BASELINE_CITE_CLAUSE = (
    "When you use a signal, cite it by its bracketed number, e.g. [3]."
)

#: BLINDING CONCESSION 2 (``--no-envelope`` disables). The first live run produced
#: a baseline 2-4x longer than the tower body, in markdown with headings and
#: tables, against a tower that writes continuous prose — arm identity was legible
#: at a glance and the "blind" grade would have been worthless.
#:
#: The envelope is MEASURED, per desk, from that desk's own tower body (rounded to
#: 25 words, +/- 50) rather than chosen. It constrains PRESENTATION only: it adds
#: no evidence, no retrieval, no verification, no memory, and no analytic
#: structure — the baseline is still one prompt and one answer. It also merely
#: makes the prompt's OWN pre-existing words operative: the instruction already
#: said "concise" and the system prompt already said "plain prose"; the model
#: ignored both. Disclosed in the grading protocol with the full prompt so the
#: operator can reject the concession and re-run with --no-envelope.
BASELINE_FORMAT_CLAUSE = (
    "Write {lo}-{hi} words of continuous prose. No headings, no tables, "
    "no bullet lists."
)


def _length_envelope(tower_body: str) -> tuple[int, int]:
    """Per-desk word envelope measured from the desk's own tower body."""
    words = len((tower_body or "").split())
    target = max(150, int(round(words / 25.0) * 25))
    return (max(100, target - 50), target + 50)

#: ``iso_countries.name`` carries ISO long forms ("Iran, Islamic Republic of").
#: Echoed back by the baseline they would be a BLINDING TELL — the tower writes
#: "Iran". So the prompt uses the common short name. Explicit for the cases a
#: comma-split gets wrong ("Korea, Republic of" -> "Korea"), comma-split otherwise.
COUNTRY_ALIASES = {
    "IR": "Iran", "KP": "North Korea", "KR": "South Korea", "RU": "Russia",
    "GB": "United Kingdom", "US": "United States", "TW": "Taiwan",
    "CD": "Democratic Republic of the Congo", "SY": "Syria", "VE": "Venezuela",
    "BO": "Bolivia", "TZ": "Tanzania", "MD": "Moldova", "LA": "Laos",
    "VN": "Vietnam", "MM": "Myanmar", "CI": "Ivory Coast",
}


def _country_name(cc: str, iso_name: str | None) -> str:
    if cc in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[cc]
    return (iso_name or cc).split(",")[0].strip()


def _load_env(env_file: str | None) -> None:
    """Load ``LEGBA_*`` connection env so the harness matches the live plane.

    Notably ``LEGBA_LLM_INPUT_TOKEN_BUDGET`` — the ORIENT pack bound. Replaying
    with a different budget would change WHICH signals the baseline sees.
    """
    try:
        from dotenv import load_dotenv
    except Exception:  # pragma: no cover - dotenv optional
        return
    path = Path(env_file) if env_file else (_REPO_ROOT / ".env")
    if path.exists():
        load_dotenv(path, override=False)


def _iso(dt: Any) -> str | None:
    if isinstance(dt, datetime):
        return dt.astimezone(timezone.utc).isoformat()
    return None if dt is None else str(dt)


def _jsonable(value: Any) -> Any:
    """JSON-safe coercion for asyncpg row values (UUID / datetime / Record)."""
    if isinstance(value, (uuid.UUID, datetime)):
        return _iso(value) if isinstance(value, datetime) else str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Substrate reads — ALL read-only.
# ---------------------------------------------------------------------------


async def _country_targets(conn: Any) -> dict[str, dict[str, Any]]:
    """Every head ``country_*`` target with its geo scope + declared sources.

    Returns ``{descriptor_id: {"cc": "IR", "source_ids": [...]}}``. ``source_ids``
    is normally EMPTY: every live country target carries a selector-shaped source
    ref with ``source_id: null``, so ``_read_substrate_slice`` falls through to the
    geo predicate alone. We resolve it anyway rather than assume, because the
    reader's ``source_id = ANY(...)`` clause would silently narrow the slice if a
    target ever gained an explicit ref — and the replay would drift from live
    without telling anyone.
    """
    rows = await conn.fetch(
        "SELECT descriptor_id, body FROM target_descriptors "
        "WHERE is_head = TRUE AND descriptor_id LIKE 'country\\_%' "
        "ORDER BY descriptor_id"
    )
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        body = r["body"]
        if isinstance(body, str):
            body = json.loads(body)
        scope = (body or {}).get("scope") or {}
        geo = [g for g in (scope.get("geo") or []) if g]
        if not geo:
            continue
        source_ids = [
            s.get("source_id")
            for s in ((body or {}).get("sources") or [])
            if isinstance(s, dict) and s.get("source_id")
        ]
        out[r["descriptor_id"]] = {
            "cc": geo[0],
            "geo": geo,
            "source_ids": source_ids,
            "predicate": scope.get("predicate"),
        }
    return out


async def _verify_unit_windows(
    conn: Any, analyst_ids: set[str]
) -> dict[str, Any]:
    """Assert the units that ACTUALLY compose still declare a 72h window.

    The replay window is derived from ``UNIT_WINDOW_HOURS``. If a composing
    descriptor were retuned after this harness was written, the reconstruction
    would silently describe a window the units never read — the exact class of
    silent drift that made the predictor emit ``no_inputs`` for weeks. Fail loud.

    Scoped to the analysts observed in the live compositions' ``derived_from``,
    NOT to every ``inline_target`` head: the fleet still carries retired
    (``country_assessor``, last output 2026-07-01) and probe (``p2_probe_unit``)
    descriptors on a 24h window that compose nothing. Checking those would fail
    the guardrail on analysts the experiment never touches.
    """
    if not analyst_ids:
        return {"declared": {}, "mismatched": {}, "checked": []}
    rows = await conn.fetch(
        "SELECT descriptor_id, "
        "       body->'subscription'->'targets'->>'time_window' AS tw "
        "FROM analyst_descriptors WHERE is_head = TRUE "
        "  AND descriptor_id = ANY($1::text[])",
        sorted(analyst_ids),
    )
    declared = {r["descriptor_id"]: r["tw"] for r in rows}
    bad = {k: v for k, v in declared.items() if v != f"{UNIT_WINDOW_HOURS}h"}
    return {"declared": declared, "mismatched": bad, "checked": sorted(analyst_ids)}


async def _head_composition(conn: Any, target_id: str) -> dict[str, Any] | None:
    """The live head (non-superseded) country_composition finding for a desk."""
    row = await conn.fetchrow(
        "SELECT id, target_id, title, body, confidence, severity, data, "
        "       produced_at, derived_from, analyst_version "
        "FROM analyst_outputs "
        "WHERE analyst_id = $1 AND kind = 'finding' AND superseded_by IS NULL "
        "  AND target_id = $2 "
        "ORDER BY produced_at DESC LIMIT 1",
        COMPOSITION_ANALYST,
        target_id,
    )
    return dict(row) if row else None


async def _composed_units(conn: Any, derived_from: list[Any]) -> list[dict[str, Any]]:
    """The unit findings the composition composed — its ``derived_from`` rows."""
    if not derived_from:
        return []
    rows = await conn.fetch(
        "SELECT id, analyst_id, kind, title, confidence, produced_at "
        "FROM analyst_outputs WHERE id = ANY($1::uuid[]) "
        "ORDER BY produced_at",
        list(derived_from),
    )
    return [dict(r) for r in rows]


def _window_for(units: list[dict[str, Any]]) -> tuple[datetime, datetime] | None:
    """(win_start, win_end) — the UNION of the composed units' own 72h windows."""
    stamps = [u["produced_at"] for u in units if u.get("produced_at")]
    if not stamps:
        return None
    return (min(stamps) - timedelta(hours=UNIT_WINDOW_HOURS), max(stamps))


async def _window_signal_count(
    conn: Any, *, cc: str, win_start: datetime, win_end: datetime
) -> int:
    """Reader-predicate signal count in the desk's own composition window.

    This is the volume the deterministic desk-selection rule ranks on. It uses the
    desk's REAL window rather than a rolling NOW()-72h so the ranking describes the
    experiment's actual evidence, not the state of the feed at selection time.
    """
    from legba.data.nats import SIGNALS_EXCLUDE_BACKFILL_SQL

    return await conn.fetchval(
        f"""
        SELECT count(*) FROM signals
        WHERE fetched_at > $1 AND fetched_at <= $2
          AND {SIGNALS_EXCLUDE_BACKFILL_SQL}
          AND (canonical_signal_id IS NULL OR canonical_signal_id = id)
          AND geo && $3::text[]
        """,
        win_start,
        win_end,
        [cc],
    )


async def _slice_rows(
    conn: Any,
    *,
    cc: str,
    source_ids: list[str],
    win_start: datetime,
    win_end: datetime,
) -> list[dict[str, Any]]:
    """The unit's evidence slice, reconstructed through the live reader's own SQL.

    Mirrors ``actor_substrate_slice._read_substrate_slice`` clause-for-clause, with
    ONE addition: an upper bound at ``win_end``. The live reader has no upper bound
    because it runs at NOW(); a replay needs one or it would hand the baseline
    signals that landed AFTER the units ran — which would be leakage, in the
    baseline's favour, and would make a tower loss unreadable.
    """
    from legba.data.nats import SIGNALS_EXCLUDE_BACKFILL_SQL

    clauses = [
        "fetched_at > $1",
        "fetched_at <= $2",
        SIGNALS_EXCLUDE_BACKFILL_SQL,
        "(canonical_signal_id IS NULL OR canonical_signal_id = id)",
    ]
    params: list[Any] = [win_start, win_end]
    if source_ids:
        params.append(source_ids)
        clauses.append(f"source_id = ANY(${len(params)})")
    params.append([cc])
    clauses.append(f"geo && ${len(params)}::text[]")

    row_cap = int(os.getenv("LEGBA_SLICE_ROW_CAP") or DEFAULT_ROW_CAP)
    fetch_limit = max(200, row_cap * 3)
    rows = await conn.fetch(
        f"""
        SELECT id, source_id, source_version, canonical_url,
               payload, language, geo, tags, fetched_at, derived_from,
               entity_classes, source_credibility, modality, salience
        FROM signals
        WHERE {" AND ".join(clauses)}
        ORDER BY fetched_at DESC
        LIMIT {fetch_limit}
        """,
        *params,
    )
    return [dict(r) for r in rows]


def _shape_like_reader(rows: list[dict[str, Any]], target_id: str) -> list[dict[str, Any]]:
    """Back-compat shaping the reader applies before handing rows to the analyst.

    Lifted verbatim from ``_read_substrate_slice``'s tail: ``title`` / ``data`` /
    ``produced_at`` / ``source_url`` are what ``_render_signal`` and ``_orient``
    actually read, and getting this wrong would silently render empty blocks.
    """
    out: list[dict[str, Any]] = []
    for d in rows:
        payload = d.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        d = dict(d)
        d["target_id"] = target_id
        d["target_version"] = None
        d["source_url"] = d.get("canonical_url")
        d["title"] = payload.get("title") if isinstance(payload, dict) else None
        d["data"] = payload
        d["produced_at"] = d.get("fetched_at")
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Desk selection — deterministic, auditable, re-derivable.
# ---------------------------------------------------------------------------


def _select_desks(ranking: list[dict[str, Any]]) -> dict[str, str]:
    """Apply the published selection rule to the live volume ranking.

    Ties break on ``descriptor_id`` ascending so a re-run on identical data picks
    identically. Every branch is recorded in the bundle's ``selection`` block.
    """
    usable = [r for r in ranking if r["window"] is not None]
    watch = sorted(
        (r for r in usable if r["target_id"].startswith("country_watch_")),
        key=lambda r: (-r["volume"], r["target_id"]),
    )
    g20 = sorted(
        (r for r in usable if r["target_id"].startswith("country_g20_")),
        key=lambda r: (r["volume"], r["target_id"]),
    )
    allrows = sorted(usable, key=lambda r: (r["volume"], r["target_id"]))
    if not watch or not g20 or not allrows:
        raise SystemExit("R5: not enough desks with live compositions to select from")

    busy = watch[0]["target_id"]
    # LOW median: with an even count take the lower of the two middles, so the
    # pick is a real desk rather than an interpolation, and is stable.
    med_volume = statistics.median_low([r["volume"] for r in g20])
    medium = next(r["target_id"] for r in g20 if r["volume"] == med_volume)
    quiet = next(r["target_id"] for r in allrows if r["target_id"] not in (busy, medium))
    return {"busy": busy, "medium_g20": medium, "quiet": quiet}


# ---------------------------------------------------------------------------
# Baseline arm — one prompt, one call, no pipeline.
# ---------------------------------------------------------------------------


def _baseline_prompt(
    *,
    country: str,
    blocks: list[str],
    cite: bool,
    window_hours: int,
    envelope: tuple[int, int] | None,
) -> str:
    lines = [
        f"Here are {len(blocks)} news signals from the last {window_hours} hours "
        f"for {country}.",
        "",
        "Write a concise intelligence read: what matters, what changed, "
        "what to watch.",
    ]
    tail = []
    if envelope is not None:
        tail.append(BASELINE_FORMAT_CLAUSE.format(lo=envelope[0], hi=envelope[1]))
    if cite:
        tail.append(BASELINE_CITE_CLAUSE)
    if tail:
        lines += ["", " ".join(tail)]
    lines += ["", ""]
    return "\n".join(lines) + "\n\n".join(blocks)


class _CorePlane:
    """Minimal core-plane client — the heartbeat/temp_ab_replay pattern.

    Resolves the endpoint, model and credential from the LIVE registry row rather
    than from env, so the probe follows component edits (this is why
    ``scripts/host_llm_heartbeat.sh`` does it this way and why the models-host
    ``/v1/models`` check stayed green through 19h of dead completions).
    """

    def __init__(self) -> None:
        self.handler: Any = None
        self.registry: Any = None
        self.store: Any = None

    async def open(self, component_id: str) -> None:
        if component_id != CORE_COMPONENT:
            raise SystemExit(
                f"R5: refusing non-core plane {component_id!r}; this harness is "
                f"pinned to the $0 plane {CORE_COMPONENT!r}"
            )
        from legba.data.config import PostgresConfig
        from legba.data.postgres import PostgresStore
        from legba.data.registry.credentials import CredentialVault
        from legba.runtime.analyst_deps_builder import (
            build_llm_handler_from_stack_component,
        )
        from legba.runtime.registry_client import RegistryHTTPClient

        self.store = PostgresStore(PostgresConfig.from_env())
        await self.store.connect()
        vault = CredentialVault(self.store)

        async def _resolve(secret_id: str) -> bytes:
            return await vault.resolve(secret_id)

        self.registry = RegistryHTTPClient()
        self.handler = await build_llm_handler_from_stack_component(
            component_id, registry_client=self.registry, secrets_resolve=_resolve,
        )

    @property
    def model(self) -> str:
        cfg = getattr(self.handler, "_cfg", None)
        return str(getattr(getattr(cfg, "model_name", None), "raw", "?"))

    async def complete(self, *, system: str, user: str) -> dict[str, Any]:
        # NO max_tokens: the core (vLLM) plane serves its own output budget and
        # the house rule is that the core plane never receives an output cap.
        resp = await self.handler.chat_complete(
            [{"role": "user", "content": user}],
            system=system,
            temperature=BASELINE_TEMPERATURE,
        )
        usage = getattr(resp, "usage", None)
        return {
            "content": (getattr(resp, "content", "") or "").strip(),
            "finish_reason": getattr(resp, "finish_reason", None),
            "usage": {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
            }
            if usage is not None
            else None,
        }

    async def close(self) -> None:
        for closer in (
            lambda: self.handler.on_deactivate(None) if self.handler else None,
            lambda: self.registry.aclose() if self.registry else None,
            lambda: self.store.close() if self.store else None,
        ):
            try:
                coro = closer()
                if coro is not None:
                    await coro
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def _run(args: argparse.Namespace) -> int:
    import asyncpg

    from legba.data.analysts.inline_target import (
        _MAX_INPUT_SIGNALS,
        _orient,
        _render_signal,
    )
    from legba.data.analysts._llm_budget import input_token_budget
    from legba.runtime.actor_substrate_slice import (
        _diversify_by_source,
        _global_slice_per_source_cap,
        _slice_row_cap,
    )
    from legba.data.config import PostgresConfig

    cfg = PostgresConfig.from_env()
    pool = await asyncpg.create_pool(
        host=cfg.host, port=cfg.port, user=cfg.user, password=cfg.password,
        database=cfg.database, min_size=1, max_size=3,
        # Server-enforced: a stray write in this harness raises, never lands.
        server_settings={"default_transaction_read_only": "on"},
    )
    bundle: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "experiment": "R5 hostile ablation — tower composition vs one-shot baseline",
        "core_component": CORE_COMPONENT,
        "baseline_temperature": BASELINE_TEMPERATURE,
        "baseline_system": BASELINE_SYSTEM,
        "baseline_cite_clause": BASELINE_CITE_CLAUSE if not args.no_cite else None,
        "baseline_format_clause": None if args.no_envelope else BASELINE_FORMAT_CLAUSE,
        "blinding_concessions": [
            c
            for c in (
                None if args.no_cite else "citation permission (symmetric with tower)",
                None
                if args.no_envelope
                else "length + continuous-prose envelope, measured per desk from "
                "that desk's own tower body",
            )
            if c
        ],
        "unit_window_hours": UNIT_WINDOW_HOURS,
        "caps": {
            "per_source": _global_slice_per_source_cap(),
            "row_cap": _slice_row_cap(),
            "max_input_signals": _MAX_INPUT_SIGNALS,
            "input_token_budget": input_token_budget(),
        },
        "desks": [],
    }

    try:
        async with pool.acquire() as conn:
            targets = await _country_targets(conn)
            names = {
                r["iso2"]: r["name"]
                for r in await conn.fetch("SELECT iso2, name FROM iso_countries")
            }

            # --- rank every desk on its own composition window ----------------
            ranking: list[dict[str, Any]] = []
            composing_analysts: set[str] = set()
            for tid, meta in targets.items():
                comp = await _head_composition(conn, tid)
                units = await _composed_units(conn, comp["derived_from"]) if comp else []
                composing_analysts.update(
                    str(u["analyst_id"]) for u in units if u.get("analyst_id")
                )
                window = _window_for(units) if comp else None
                volume = (
                    await _window_signal_count(
                        conn, cc=meta["cc"], win_start=window[0], win_end=window[1]
                    )
                    if window
                    else 0
                )
                ranking.append(
                    {
                        "target_id": tid,
                        "cc": meta["cc"],
                        "country": _country_name(meta["cc"], names.get(meta["cc"])),
                        "volume": volume,
                        "n_units": len(units),
                        "window": [_iso(window[0]), _iso(window[1])] if window else None,
                        "composition_at": _iso(comp["produced_at"]) if comp else None,
                    }
                )
            ranking.sort(key=lambda r: (-r["volume"], r["target_id"]))
            bundle["ranking"] = ranking

            # --- guardrail: every COMPOSING unit must still declare 72h -------
            wcheck = await _verify_unit_windows(conn, composing_analysts)
            bundle["unit_window_check"] = wcheck
            if wcheck["mismatched"]:
                raise SystemExit(
                    "R5: composing unit descriptors no longer declare a "
                    f"{UNIT_WINDOW_HOURS}h window: {wcheck['mismatched']}. "
                    "Re-derive UNIT_WINDOW_HOURS before replaying."
                )

            if args.desks:
                picks_list = [d.strip() for d in args.desks.split(",") if d.strip()]
                if len(picks_list) != 3:
                    raise SystemExit("R5: --desks needs exactly 3 comma-separated ids")
                picks = dict(zip(("busy", "medium_g20", "quiet"), picks_list))
                rule = "operator override via --desks"
            else:
                picks = _select_desks(ranking)
                rule = (
                    "busy = max volume among country_watch_*; "
                    "medium_g20 = low-median volume among country_g20_*; "
                    "quiet = min volume across all country desks; "
                    "ties break on descriptor_id ascending"
                )
            bundle["selection"] = {"rule": rule, "picks": picks}

            # --- per-desk reconstruction --------------------------------------
            for regime, tid in picks.items():
                meta = targets[tid]
                comp = await _head_composition(conn, tid)
                if comp is None:
                    raise SystemExit(f"R5: no head composition for {tid}")
                units = await _composed_units(conn, comp["derived_from"])
                window = _window_for(units)
                if window is None:
                    raise SystemExit(f"R5: cannot derive a window for {tid}")
                win_start, win_end = window

                raw = await _slice_rows(
                    conn,
                    cc=meta["cc"],
                    source_ids=meta["source_ids"],
                    win_start=win_start,
                    win_end=win_end,
                )
                shaped = _shape_like_reader(raw, tid)
                # The reader applies the diversity cap to every geo-scoped desk
                # slice (a firehose source must not monopolise a desk's window).
                capped = _diversify_by_source(
                    shaped,
                    per_source_cap=_global_slice_per_source_cap(),
                    limit=_slice_row_cap(),
                )
                # ORIENT: recency sort, dead-row drop, INPUT-token budget pack.
                packed, derived = _orient(capped, tid)
                blocks = [_render_signal(i, r) for i, r in enumerate(packed, start=1)]

                source_mix: dict[str, int] = {}
                for r in packed:
                    source_mix[r.get("source_id") or "?"] = (
                        source_mix.get(r.get("source_id") or "?", 0) + 1
                    )

                desk: dict[str, Any] = {
                    "regime": regime,
                    "target_id": tid,
                    "cc": meta["cc"],
                    "country": _country_name(meta["cc"], names.get(meta["cc"])),
                    "window": {
                        "start": _iso(win_start),
                        "end": _iso(win_end),
                        "hours": UNIT_WINDOW_HOURS,
                        "derivation": (
                            "min(unit.produced_at) - 72h .. max(unit.produced_at) "
                            "over the composition's derived_from units"
                        ),
                    },
                    "evidence": {
                        # window_total = every reader-predicate row in the window;
                        # fetched_pre_cap is that count after the reader's own
                        # over-fetch LIMIT (max(200, row_cap*3)), which is what the
                        # diversity cap then walks. They differ on a busy desk and
                        # conflating them would misreport the evidence base.
                        "window_total": next(
                            (r["volume"] for r in ranking if r["target_id"] == tid), None
                        ),
                        "fetched_pre_cap": len(raw),
                        "after_diversity_cap": len(capped),
                        "after_orient_pack": len(packed),
                        "distinct_sources": len(source_mix),
                        "source_mix": dict(
                            sorted(source_mix.items(), key=lambda kv: -kv[1])
                        ),
                        "signal_ids": [str(u) for u in derived],
                        "headlines": [
                            {
                                "n": i,
                                "title": (r.get("data") or {}).get("title")
                                or r.get("title")
                                or "(untitled)",
                                "source_id": r.get("source_id"),
                                "published_at": (r.get("data") or {}).get("published_at"),
                                "ingested_at": _iso(r.get("produced_at")),
                            }
                            for i, r in enumerate(packed, start=1)
                        ],
                        "rendered_blocks": blocks,
                    },
                    "tower": {
                        "output_id": str(comp["id"]),
                        "title": comp["title"],
                        "body": comp["body"],
                        "confidence": comp["confidence"],
                        "severity": comp["severity"],
                        "produced_at": _iso(comp["produced_at"]),
                        "analyst_version": comp["analyst_version"],
                        "composed_units": [
                            {
                                "analyst_id": u["analyst_id"],
                                "title": u["title"],
                                "confidence": u["confidence"],
                                "produced_at": _iso(u["produced_at"]),
                            }
                            for u in units
                        ],
                    },
                    "tower_only_context": (
                        "The tower additionally received graph-metric "
                        "[ASSESSED STRUCTURE] pseudo-rows and the QW1-B desk "
                        "grounding block (prior read / open situation register / "
                        "desk baseline / standing questions). These are not "
                        "signals and are withheld from the baseline by design — "
                        "handing them over would leak the tower into the control."
                    ),
                }
                bundle["desks"].append(desk)

        # --- baseline generations (outside the DB pool) -----------------------
        if args.no_llm:
            for desk in bundle["desks"]:
                desk["baseline"] = {"skipped": "--no-llm"}
        else:
            plane = _CorePlane()
            await plane.open(CORE_COMPONENT)
            bundle["baseline_model"] = plane.model
            try:
                for desk in bundle["desks"]:
                    envelope = (
                        None
                        if args.no_envelope
                        else _length_envelope(desk["tower"]["body"])
                    )
                    desk["baseline_envelope"] = list(envelope) if envelope else None
                    user = _baseline_prompt(
                        country=desk["country"],
                        blocks=desk["evidence"]["rendered_blocks"],
                        cite=not args.no_cite,
                        window_hours=UNIT_WINDOW_HOURS,
                        envelope=envelope,
                    )
                    started = datetime.now(timezone.utc)
                    result = await asyncio.wait_for(
                        plane.complete(system=BASELINE_SYSTEM, user=user),
                        timeout=args.timeout,
                    )
                    desk["baseline"] = {
                        **result,
                        "prompt_chars": len(user),
                        "prompt": user,
                        "model": plane.model,
                        "temperature": BASELINE_TEMPERATURE,
                        "started_at": started.isoformat(),
                        "elapsed_s": round(
                            (datetime.now(timezone.utc) - started).total_seconds(), 1
                        ),
                    }
                    print(
                        f"[R5] baseline {desk['target_id']:<20} "
                        f"signals={desk['evidence']['after_orient_pack']:>3} "
                        f"chars={len(result['content']):>5} "
                        f"{desk['baseline']['elapsed_s']}s",
                        file=sys.stderr,
                    )
            finally:
                await plane.close()
    finally:
        await pool.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(_jsonable(bundle), indent=2, ensure_ascii=False))
    print(f"[R5] bundle -> {out} ({out.stat().st_size} bytes)", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="build the ablation bundle")
    run.add_argument("--out", required=True, help="bundle JSON path")
    run.add_argument("--env-file", default=None)
    run.add_argument("--desks", default=None, help="override picks: busy,medium,quiet")
    run.add_argument("--no-llm", action="store_true", help="skip baseline generation")
    run.add_argument(
        "--no-cite",
        action="store_true",
        help="drop the baseline citation clause (harder blinding trade-off)",
    )
    run.add_argument(
        "--no-envelope",
        action="store_true",
        help="drop the measured length/prose envelope (harder blinding trade-off)",
    )
    run.add_argument("--timeout", type=float, default=600.0)
    args = ap.parse_args()
    _load_env(getattr(args, "env_file", None))
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
