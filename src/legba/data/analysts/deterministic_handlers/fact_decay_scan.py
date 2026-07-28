# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``fact_decay_scan`` sub-handler — C4 fact confidence-decay readout stamper
(MISP decaying-indicators / OpenCTI decay-rules mechanics; LLM-free).

A deterministic META analyst on a DAILY cadence that walks every OPEN fact,
computes its derived decay readout via the pure model in
:mod:`legba.data.facts.decay` (per-class MISP retention curve at
days-since-last-sighting × the stored confidence), and stamps the result into
the ``fact_decay_states`` SIDECAR (migration 0098).

HARD RULE — readout, never mutation
-----------------------------------
This handler NEVER touches a ``facts`` row. ``confidence`` /
``confidence_components`` / ``updated_at`` stay exactly as the write path left
them (unlike the legacy ``fact_decay`` sweep, which subtracts from the stored
scalar). The only writes are the sidecar upsert + the prune of sidecar rows
whose fact has since closed. Drop the sidecar, re-run the scan, identical
content — the readout is derived, recomputable state.

Sightings (derived — no new write path, no new column on ``facts``)
-------------------------------------------------------------------
A corroborating re-assert of the same open triple UNIONs the backing signal
ids into ``facts.derived_from`` (both fact producers). So:

    last_sighting_at = max(COALESCE(signals.fetched_at, signals.created_at))
                       over facts.derived_from,
                       falling back to facts.created_at (birth = first
                       sighting) when no backing signal survives (seed facts;
                       signals purged by retention — the fallback leans
                       CONSERVATIVE: a fact whose sightings were purged decays
                       from birth, it is never pinned artificially fresh).

``facts.updated_at`` is deliberately NOT used — it is polluted by
non-sighting touches (the legacy decay mutation, contention-arbiter marker
stamps, entity_gc subject renames).

Summary finding (zero-state honest)
-----------------------------------
Every run emits ONE summary FINDING — counts per decay_state + the top revoke
candidates by decayed confidence — even when the distribution is all-fresh or
the substrate is empty (an honest zero is a measurement, not noise). The
handler therefore sits in the FINDING-emitters set and the
``STRUCTURAL_VERIFY_EXEMPT_ANALYSTS`` registry (the drift guard asserts
equality), rendering everywhere with the honest ``unverified — structural``
badge.

Consumption (ships OFF)
-----------------------
Nothing reads the sidecar by default. The grounding fact read joins it ONLY
under ``LEGBA_FACT_DECAY_WEIGHTING`` (default OFF): revoke candidates are then
excluded from the grounding preamble and decayed_confidence annotates the
rendered lines. Flipping the flag is a measured operator step.

Registered via ``scripts/bringup_register_fact_decay_scan.py`` (descriptor
``descriptors/analyst_fact_decay_scan.yaml``, ships ``state: draft``).
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID, uuid4

from ....runtime.analyst_method import AnalystMethodResult
from ...facts.decay import (
    DECAY_STATES,
    DecayConfig,
    decayed_confidence,
    load_decay_config,
)
from ...provenance.models import FindingPayload

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "fact_decay_scan"

#: Per-scan cap on facts examined (options.max_facts). The live open set is
#: ~7.5k; the cap is generous headroom, not a working limit.
DEFAULT_MAX_FACTS = 50_000

#: Revoke candidates listed in the summary finding (options.top_candidates).
DEFAULT_TOP_CANDIDATES = 10

#: The open-fact scan + the derived last-sighting in ONE read. The per-row
#: subselect resolves each backing signal id against the signals pkey (the
#: whole open set derives in <1s live). Excludes rows the legacy expire pass
#: has marked ``data.expired`` (they are past valid_until anyway).
_OPEN_FACTS_SQL = """
    SELECT f.id, f.subject, f.predicate, f.value, f.confidence,
           f.source_type, f.created_at,
           (SELECT max(COALESCE(s.fetched_at, s.created_at))
              FROM signals s
             WHERE s.id = ANY(f.derived_from)) AS last_signal_at
      FROM facts f
     WHERE f.superseded_by IS NULL
       AND (f.valid_until IS NULL OR f.valid_until > now())
       AND COALESCE(f.data->>'expired', 'false') != 'true'
     ORDER BY f.created_at
     LIMIT $1
"""

#: Idempotent per-fact stamp — one sidecar row per open fact, latest wins.
_UPSERT_SQL = """
    INSERT INTO fact_decay_states (
        fact_id, decayed_confidence, decay_state, decay_class, retention,
        lifetime_days, last_sighting_at, sighting_source, stored_confidence,
        computed_at, run_id
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
    ON CONFLICT (fact_id) DO UPDATE SET
        decayed_confidence = EXCLUDED.decayed_confidence,
        decay_state        = EXCLUDED.decay_state,
        decay_class        = EXCLUDED.decay_class,
        retention          = EXCLUDED.retention,
        lifetime_days      = EXCLUDED.lifetime_days,
        last_sighting_at   = EXCLUDED.last_sighting_at,
        sighting_source    = EXCLUDED.sighting_source,
        stored_confidence  = EXCLUDED.stored_confidence,
        computed_at        = EXCLUDED.computed_at,
        run_id             = EXCLUDED.run_id,
        updated_at         = now()
"""

#: Prune readouts whose fact closed (superseded/expired) or vanished since the
#: last scan — the sidecar mirrors the OPEN set only.
_PRUNE_SQL = """
    DELETE FROM fact_decay_states d
     WHERE NOT EXISTS (
             SELECT 1 FROM facts f
              WHERE f.id = d.fact_id
                AND f.superseded_by IS NULL
                AND (f.valid_until IS NULL OR f.valid_until > now())
           )
"""


def compute_readouts(
    rows: list[Mapping[str, Any]],
    *,
    now: datetime,
    config: DecayConfig,
) -> list[dict[str, Any]]:
    """Pure core: fact rows → readout dicts (the stamp + summary substrate).

    ``last_sighting_at`` = the newer of the derived backing-signal max and
    ``created_at`` (a signal observed BEFORE the row was born can only be the
    first sighting, not the last). ``sighting_source`` records which won.
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        created_at = r.get("created_at")
        last_signal_at = r.get("last_signal_at")
        candidates = [
            ("created_at", created_at),
            ("signal", last_signal_at),
        ]
        best_source, best_at = "created_at", created_at
        for source, ts in candidates:
            if ts is not None and (best_at is None or ts >= best_at):
                best_source, best_at = source, ts
        readout = decayed_confidence(
            confidence=r.get("confidence"),
            predicate=r.get("predicate"),
            source_type=r.get("source_type"),
            now=now,
            last_sighting_at=best_at,
            config=config,
        )
        out.append(
            {
                "fact_id": r["id"],
                "subject": r.get("subject"),
                "predicate": r.get("predicate"),
                "value": r.get("value"),
                "stored_confidence": (
                    float(r["confidence"]) if r.get("confidence") is not None else 0.0
                ),
                "decayed_confidence": readout.decayed_confidence,
                "decay_state": readout.decay_state,
                "decay_class": readout.decay_class,
                "retention": readout.retention,
                "lifetime_days": readout.lifetime_days,
                "elapsed_days": readout.elapsed_days,
                "last_sighting_at": best_at,
                "sighting_source": best_source,
            }
        )
    return out


def _build_finding(
    *,
    readouts: list[dict[str, Any]],
    pruned: int,
    top_candidates: int,
    config: DecayConfig,
) -> FindingPayload:
    counts = Counter(r["decay_state"] for r in readouts)
    by_state = {state: int(counts.get(state, 0)) for state in DECAY_STATES}
    total = len(readouts)

    revoke = sorted(
        (r for r in readouts if r["decay_state"] == "revoke_candidate"),
        key=lambda r: (r["decayed_confidence"], -r["elapsed_days"]),
    )[: max(0, top_candidates)]

    if total == 0:
        title = "Fact decay scan: 0 open facts examined"
    else:
        title = (
            "Fact decay scan: "
            + ", ".join(f"{by_state[s]} {s}" for s in DECAY_STATES)
            + f" of {total} open facts"
        )

    lines: list[str] = [
        f"open_facts_examined={total}",
        "counts_per_state: "
        + ", ".join(f"{s}={by_state[s]}" for s in DECAY_STATES),
        f"sidecar_rows_pruned={pruned}",
        f"revoke_threshold={config.revoke_threshold}",
    ]
    if total == 0:
        lines.append(
            "No open facts on the substrate — nothing stamped (honest zero)."
        )
    elif not revoke:
        lines.append("No revoke candidates this scan (honest zero).")
    else:
        lines.append(f"Top revoke candidates (lowest decayed confidence first, cap {top_candidates}):")
        for r in revoke:
            lines.append(
                f"  - {r['subject']} | {r['predicate']} | {r['value']}"
                f" — decayed {r['decayed_confidence']:.3f}"
                f" (stored {r['stored_confidence']:.2f},"
                f" class {r['decay_class']},"
                f" {r['elapsed_days']:.0f}d since sighting)"
            )
    lines.append(
        "Readout only: no facts.confidence was mutated (sidecar "
        "fact_decay_states; consumption gated OFF behind "
        "LEGBA_FACT_DECAY_WEIGHTING)."
    )

    return FindingPayload(
        title=title[:2048],
        body="\n".join(lines)[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", "fact_decay_scan", "decay"],
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "open_facts_examined": total,
            "counts_per_state": by_state,
            "sidecar_rows_pruned": pruned,
            "revoke_threshold": config.revoke_threshold,
            "reaction_fresh": config.reaction_fresh,
            "reaction_aging": config.reaction_aging,
            "top_revoke_candidates": [
                {
                    "fact_id": str(r["fact_id"]),
                    "subject": r["subject"],
                    "predicate": r["predicate"],
                    "value": r["value"],
                    "decayed_confidence": round(r["decayed_confidence"], 4),
                    "stored_confidence": round(r["stored_confidence"], 4),
                    "decay_class": r["decay_class"],
                    "elapsed_days": round(r["elapsed_days"], 1),
                }
                for r in revoke
            ],
        },
    )


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — one global decay-readout scan.

    REFUSES LOUD on a missing pool (the geo_convergence_scan contract): a scan
    that cannot read the substrate must error visibly, never report an honest-
    looking zero distribution it did not measure.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        raise RuntimeError(
            "fact_decay_scan requires a live deps.pg_pool — refusing to "
            "report a decay distribution without reading the substrate"
        )

    raw_run_id = options.get("run_id")
    try:
        run_uuid = UUID(str(raw_run_id)) if raw_run_id else uuid4()
    except (ValueError, TypeError):
        run_uuid = uuid4()

    max_facts = max(1, int(options.get("max_facts", DEFAULT_MAX_FACTS)))
    top_candidates = max(
        0, int(options.get("top_candidates", DEFAULT_TOP_CANDIDATES))
    )
    config = load_decay_config()
    now = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        rows = await conn.fetch(_OPEN_FACTS_SQL, max_facts)
        readouts = compute_readouts(
            [dict(r) for r in rows], now=now, config=config
        )
        if readouts:
            await conn.executemany(
                _UPSERT_SQL,
                [
                    (
                        r["fact_id"],
                        float(r["decayed_confidence"]),
                        r["decay_state"],
                        r["decay_class"],
                        float(r["retention"]),
                        float(r["lifetime_days"]),
                        r["last_sighting_at"],
                        r["sighting_source"],
                        float(r["stored_confidence"]),
                        now,
                        run_uuid,
                    )
                    for r in readouts
                ],
            )
        prune_result = await conn.execute(_PRUNE_SQL)
    pruned = 0
    try:
        pruned = int(str(prune_result).split()[-1])
    except (ValueError, IndexError, AttributeError):
        pruned = 0

    finding = _build_finding(
        readouts=readouts,
        pruned=pruned,
        top_candidates=top_candidates,
        config=config,
    )
    logger.info(
        "fact_decay_scan.done examined=%d counts=%s pruned=%d",
        len(readouts),
        dict(Counter(r["decay_state"] for r in readouts)),
        pruned,
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["SUB_HANDLER_NAME", "compute_readouts", "handle"]
