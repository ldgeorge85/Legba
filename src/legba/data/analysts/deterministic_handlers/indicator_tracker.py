# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``indicator_tracker`` sub-handler — S3-T2 structured-I&W flips, debounced.

The S3-T1 slice gave each bounded unit a machine-checkable MIRROR of its
forward-looking "Indicators to watch" prose: ``FindingPayload.data.indicators[]``
(stored at ``analyst_outputs.data->'data'->'indicators'``), each entry a stable
``id`` slug + a ``status`` (``triggered`` / ``not_observed`` / ``expired``). This
handler closes the loop: a DETERMINISTIC (no-LLM) META analyst that follows
those indicators run over run per ``(target_id, source unit analyst_id)`` stream
and reports when an indicator's status CHANGES — most importantly a
``not_observed`` → ``triggered`` activation (a pre-registered warning signpost
firing).

Once, and only when it held (W11-F55, X3)
-----------------------------------------

Until 2026-10 the handler diffed the two newest runs of every stream on every
heartbeat and re-published the whole flip set whenever anything moved: 11,517
flip mentions for 684 distinct flips in a week, nearly symmetric up/down
because an indicator toggling every run read as a stream of changes. Now:

  * each stream has a WATERMARK (``alert_trigger_watermarks``, class
    ``indicator_tracker``): a sweep walks only the source runs newer than it,
    in order, once (:mod:`._indicator_flip_store`);
  * every consecutive-run change is recorded as a ``raw`` flip in
    ``indicator_flips`` (migration 0237) — the audit, never fed;
  * a change counts — a ``debounced`` flip — only when the new status has
    HELD for ``hysteresis_runs`` consecutive runs (default 2) or across runs
    spanning ``hysteresis_hours`` (default: one cadence of the source unit,
    its ``cadence.cooldown_seconds``), and differs from the status the
    indicator last held (:mod:`._indicator_debounce`; docs/TUNABLES.md);
  * the feed receipt carries only the debounced flips THIS sweep inserted
    (the unique index decides "new"), so a re-run over the same evidence
    inserts nothing and is ``force_trace_only`` — no feed row, ever twice.

The feed and the Wall (which see only this receipt) and the bake-off's arm E
(through :func:`debounced_flips`) read the debounced stream; raw flips are
never handed to a reader.

A newly-INTRODUCED indicator (no prior run in its lineage) is NOT a flip, and
the first status an indicator ever holds is its baseline, not a change. Flips
found on a stream's FIRST sweep come from history: they are recorded with
``backfill = true`` for the reader and never fed.

``deps=None`` runs the synthetic (no-DB) path: ``inputs`` are pre-shaped finding
rows (``id`` / ``target_id`` / ``analyst_id`` / ``produced_at`` (a datetime) +
the indicator list, as ``row['indicators']`` or the persisted
``row['data']['data']['indicators']`` shape), walked from an empty state with
no watermark and nothing persisted; every debounced flip it derives is fed.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import UUID

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult
from ._indicator_debounce import (
    DEFAULT_HYSTERESIS_RUNS,
    StreamState,
    advance_stream,
)
from ._indicator_flip_store import (
    debounced_flips,
    fetch_new_runs,
    persist_sweep,
    read_cadence_hours,
    read_watermarks,
    stream_key,
)

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "indicator_tracker"

# The three I&W indicator statuses (schemas.analyst.IndicatorEntry.status).
# ``observed_at_level`` (X4 LEVEL, a level-shaped indicator standing at its
# level) is deliberately OFF this scale: a standing condition is not a firing,
# so ``_clean_indicators`` skips it and it never becomes a flip.
_STATUSES = frozenset({"triggered", "not_observed", "expired"})

# Only findings produced no earlier than this many days ago are walked — an
# indicator stream older than this is settled history, not a live watch. Generous
# default; override via options['lookback_days'].
_DEFAULT_LOOKBACK_DAYS = 30
# Per-run safety cap on how many flips ride inside the summary finding's data /
# body (the flip rows are the product; keep the JSONB + feed row bounded).
_MAX_FLIPS_IN_FINDING = 200

#: H12 — the instrument revision the flip series below was computed under
#: (docs/ANALYSIS.md §10.9), stamped ``data.method_version`` on every receipt
#: and every ``indicator_flips`` row. Covers ``_DEFAULT_LOOKBACK_DAYS``, the
#: watermarked in-order walk per ``(target_id, source analyst_id)``, the
#: id-then-statement join, the newly-introduced-id rule, the hysteresis rule
#: (held for N runs or M hours, against the last confirmed status) and the
#: activation predicate. Bump it when any of them moves, so a flip-count diff
#: across the change reads as an instrument revision, not a world change.
METHOD_VERSION = "indicator_tracker/2026-10.1"

#: K3 — the SCALE the flip series is expressed ON (see docs/ANALYSIS.md §10.9).
#: Indicators are not a continuous measure: the scale IS the three-value status
#: vocabulary ``triggered`` / ``not_observed`` / ``expired`` (``_STATUSES``,
#: mirroring ``schemas.analyst.IndicatorEntry.status``) and the reading of a
#: transition between them. ``2026-07`` is the era S3-T1/T2 opened (2026-07-02),
#: when that vocabulary was fixed; it has not moved since. Add a value, retire
#: one, or change what a transition MEANS and the era moves with it — every
#: stored flip before the change is then on a different scale. (The debounce
#: changes which transitions are COUNTED — the method — not what one means.)
SCALE_VERSION = "indicator_status/2026-07"


# ---------------------------------------------------------------------------
# Indicator extraction (shared by the live + synthetic paths)
# ---------------------------------------------------------------------------


def _parse_data(raw: Any) -> dict[str, Any]:
    """Normalize a finding row's ``data`` to a dict (tolerate a JSONB str)."""
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _extract_indicators(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The indicator entries carried by one finding row.

    Reads, in priority order:
      1. a direct ``row['indicators']`` list (the convenience shape unit tests
         author);
      2. the persisted payload shape — ``analyst_outputs.data`` is the full
         ``FindingPayload`` model_dump, so a unit's structured indicators live at
         ``data->'data'->'indicators'`` (nested payload sub-dict); a top-level
         ``data->'indicators'`` is tolerated as a fallback.

    Returns only well-shaped entries (a dict carrying a non-empty ``id`` and a
    known ``status``); everything else is dropped (degrade-not-drop).
    """
    direct = row.get("indicators")
    if isinstance(direct, list):
        return _clean_indicators(direct)
    data = _parse_data(row.get("data"))
    inner = data.get("data")
    candidates = ((inner,) if isinstance(inner, Mapping) else ()) + (data,)
    for src in candidates:
        ind = src.get("indicators")
        if isinstance(ind, list):
            return _clean_indicators(ind)
    return []


def _clean_indicators(entries: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in entries:
        if not isinstance(e, Mapping):
            continue
        eid = e.get("id")
        status = e.get("status")
        if not eid or status not in _STATUSES:
            continue
        out.append(dict(e))
    return out


# ---------------------------------------------------------------------------
# The sweep core — pure; the live path wraps it in the watermark store
# ---------------------------------------------------------------------------


@dataclass
class FlipSweep:
    """What one sweep found. ``raw`` / ``debounced`` are every flip the walk
    produced (before the store decides which are new); ``states`` are the
    advanced watermarks of the streams that moved."""

    raw: list[dict[str, Any]] = field(default_factory=list)
    debounced: list[dict[str, Any]] = field(default_factory=list)
    states: dict[str, StreamState] = field(default_factory=dict)
    streams_walked: int = 0


def _flip_order(f: Mapping[str, Any]) -> tuple[Any, ...]:
    # Activations (→triggered) first, then by stream and indicator, so the
    # receipt body is stable for one flip set.
    return (
        0 if f["activation"] else 1,
        str(f["target_id"]),
        str(f["source_analyst_id"]),
        str(f["indicator_id"]),
        str(f.get("confirmed_at") or f.get("evidence_at")),
    )


def collect_flips(
    rows: list[dict[str, Any]],
    *,
    states: Mapping[str, StreamState] | None = None,
    hysteresis_runs: int = DEFAULT_HYSTERESIS_RUNS,
    hysteresis_hours: float | None = None,
    cadence_hours: Mapping[str, float] | None = None,
) -> FlipSweep:
    """Walk every ``(target_id, analyst_id)`` stream in ``rows`` on from its
    watermark in ``states`` (none = a first sight: the rows are its history).

    ``hysteresis_hours`` overrides the hours leg for every stream (``0`` turns
    it off); left ``None``, each stream uses its unit's ``cadence_hours``
    (one cadence), and no hours leg when that is unknown. Flips found on a
    first-sight stream are stamped ``backfill``.
    """
    states = states or {}
    cadence_hours = cadence_hours or {}
    groups: dict[tuple[Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[(r.get("target_id"), r.get("analyst_id"))].append(r)

    sweep = FlipSweep()
    for (target_id, analyst_id), members in groups.items():
        key = stream_key(analyst_id, target_id)
        prior = states.get(key)
        first_sight = prior is None or prior.last_produced_at is None
        runs = [
            {"id": m.get("id"), "produced_at": m.get("produced_at"),
             "indicators": _extract_indicators(m)}
            for m in sorted(members, key=lambda m: (m.get("produced_at"), str(m.get("id"))))
        ]
        hours = (
            float(hysteresis_hours) if hysteresis_hours is not None
            else cadence_hours.get(str(analyst_id))
        )
        new_state, raw, deb = advance_stream(
            prior or StreamState(), runs,
            target_id=target_id, analyst_id=analyst_id,
            hysteresis_runs=hysteresis_runs, hysteresis_hours=hours,
        )
        if new_state.last_finding_id == (prior.last_finding_id if prior else None):
            continue
        sweep.streams_walked += 1
        for f in (*raw, *deb):
            f["backfill"] = first_sight
        sweep.raw.extend(raw)
        sweep.debounced.extend(deb)
        sweep.states[key] = new_state

    sweep.raw.sort(key=_flip_order)
    sweep.debounced.sort(key=_flip_order)
    return sweep


# ---------------------------------------------------------------------------
# Finding assembly
# ---------------------------------------------------------------------------


def _flip_finding_ids(flips: list[dict[str, Any]]) -> list[UUID]:
    """The real, de-duplicated source-finding UUIDs backing ``flips`` — for a
    debounced flip, the run before the change (old status) and the run that
    confirmed the new one (A2 verify-path fix, 2026-07-31): this handler read
    exactly these rows, so they are a genuine, cheap, resolvable citation —
    never fabricated. A flip whose ids are absent/unparseable contributes
    nothing."""
    seen: set[UUID] = set()
    out: list[UUID] = []
    for f in flips:
        ids = f.get("source_finding_ids") or {}
        for raw in (ids.get("prev"), ids.get("curr")):
            if not raw:
                continue
            try:
                uid = UUID(str(raw))
            except (ValueError, AttributeError, TypeError):
                continue
            if uid not in seen:
                seen.add(uid)
                out.append(uid)
    return out


def _receipt_flip(f: Mapping[str, Any]) -> dict[str, Any]:
    """One debounced flip as it rides the receipt (JSON-safe)."""
    def _iso(v: Any) -> Any:
        return v.isoformat() if hasattr(v, "isoformat") else v

    return {
        "target_id": f.get("target_id"),
        "source_analyst_id": f.get("source_analyst_id"),
        "indicator_id": str(f["indicator_id"]),
        "statement": str(f.get("statement") or "")[:2048],
        "from_status": f["from_status"],
        "to_status": f["to_status"],
        "activation": bool(f["activation"]),
        "match": f.get("match") or "id",
        # The source unit's OWN raw ordinal citations on the confirming entry
        # (decorative, kept for continuity; NOT the finding-level citations).
        "citations": list(f.get("citations") or []),
        "onset_at": _iso(f.get("evidence_at")),
        "confirmed_at": _iso(f.get("confirmed_at")),
        "confirmed_by": f.get("confirmed_by"),
        "held_runs": f.get("held_runs"),
        "raw_flips_between": f.get("raw_flips_between"),
        # A2 — the real source runs: the last one in the old status and the
        # one that confirmed the new status.
        "source_finding_ids": {
            "prev": str(f["prev_finding_id"]) if f.get("prev_finding_id") else None,
            "curr": str(f["confirmed_finding_id"]) if f.get("confirmed_finding_id") else None,
        },
    }


def _build_finding(
    flips: list[dict[str, Any]],
    groups_compared: int,
    *,
    raw_recorded: int = 0,
    backfilled: int = 0,
) -> FindingPayload:
    """The receipt over the debounced ``flips`` (receipt-shaped dicts).
    ``raw_recorded`` / ``backfilled`` are audit counts of what this sweep
    recorded and did NOT feed."""
    activations = [f for f in flips if f["activation"]]
    n = len(flips)
    a = len(activations)
    if n:
        title = (
            f"Indicator tracker: {n} indicator flip(s), {a} newly triggered, "
            f"across {groups_compared} unit-stream(s)"
        )
        lines: list[str] = []
        for f in flips[:_MAX_FLIPS_IN_FINDING]:
            tgt = f["target_id"] or "global"
            mark = "!! " if f["activation"] else "- "
            held = f.get("held_runs")
            between = f.get("raw_flips_between")
            tail = ""
            if held is not None:
                tail = f" (held {held} run(s)"
                if between and int(between) > 1:
                    tail += f"; settled after {between} raw toggles"
                tail += ")"
            lines.append(
                f"{mark}[{tgt}/{f['source_analyst_id']}] "
                f"{f['statement']}: {f['from_status']} -> {f['to_status']}{tail}"
            )
        body = "\n".join(lines)
    else:
        title = "Indicator tracker: no indicator status changes"
        body = "No held indicator status changes across the walked unit-streams this sweep."
    tags = ["deterministic", SUB_HANDLER_NAME]
    if a:
        tags.append("indicator_triggered")
    # A2 (verify-path fix, 2026-07-31): cite the indicator-bearing findings this
    # sweep actually read — cheap and real.
    citations = [{"ref_kind": "finding", "ref_id": str(uid)} for uid in _flip_finding_ids(flips)]
    # C2b — the structural_claims verify opt-in (provenance.kinds.
    # STRUCTURAL_CLAIMS_VERIFY_ANALYSTS). Both identities are re-derivable from
    # ``flips``/``groups_compared`` alone and hold by construction.
    structural_claims = [
        {
            "id": "flip_partition",
            "statement": (
                f"flip_count ({n}) = activation_count ({a}) + "
                f"non_activation_count ({n - a})"
            ),
            "op": "sum",
            "asserted": n,
            "basis": [a, n - a],
        },
        {
            "id": "groups_compared_distinct",
            "statement": (
                f"groups_compared ({groups_compared}) = distinct (target_id, "
                "source_analyst_id) unit-streams among the reported flips"
            ),
            "op": "distinct_count",
            "asserted": groups_compared,
            "basis": [f"{f['target_id']}::{f['source_analyst_id']}" for f in flips],
        },
    ]
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "method_version": METHOD_VERSION,
            "scale_version": SCALE_VERSION,
            # The receipt carries DEBOUNCED flips only (W11-F55).
            "flip_grade": "debounced",
            "flip_count": n,
            "activation_count": a,
            "groups_compared": groups_compared,
            "flips": flips[:_MAX_FLIPS_IN_FINDING],
            # Audit counts — recorded in indicator_flips, never fed.
            "raw_flips_recorded": raw_recorded,
            "backfilled_flips_recorded": backfilled,
            "citations": citations,
            "structural_claims": structural_claims,
        },
    )


def _fed(flips: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Receipt flips (non-backfill debounced) and their distinct stream count."""
    fed = [_receipt_flip(f) for f in flips if f["grade"] == "debounced" and not f.get("backfill")]
    streams = {(f["target_id"], f["source_analyst_id"]) for f in fed}
    return fed, len(streams)


# ---------------------------------------------------------------------------
# Public handler entry point
# ---------------------------------------------------------------------------


def _options(options: Mapping[str, Any]) -> tuple[int, int, float | None]:
    lookback_days = int(options.get("lookback_days", _DEFAULT_LOOKBACK_DAYS))
    runs = max(1, int(options.get("hysteresis_runs", DEFAULT_HYSTERESIS_RUNS)))
    raw_hours = options.get("hysteresis_hours")
    hours = None if raw_hours is None else max(0.0, float(raw_hours))
    return lookback_days, runs, hours


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see the module docstring.

    ``deps`` is the analyst pool bundle (``deps.pg_pool``); ``deps=None`` runs the
    synthetic path (walks pre-shaped ``inputs`` rows, no DB) for unit tests.

    Options
    -------
    lookback_days:
        Only source findings this recent are walked (default 30).
    hysteresis_runs:
        Consecutive runs a status must be observed in to hold (default 2).
    hysteresis_hours:
        Alternatively, the span (first to newest run in the status) at which it
        holds; unset = one cadence of the source unit; 0 = runs only.
    """
    lookback_days, n_runs, hours = _options(options)
    pool = getattr(deps, "pg_pool", None) if deps is not None else None

    new_flips: list[dict[str, Any]]
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                watermarks = await read_watermarks(conn)
                rows = await fetch_new_runs(conn, watermarks, lookback_days=lookback_days)
                cadence = (
                    await read_cadence_hours(conn, {str(r["analyst_id"]) for r in rows})
                    if hours is None else {}
                )
            sweep = collect_flips(
                rows, states=watermarks, hysteresis_runs=n_runs,
                hysteresis_hours=hours, cadence_hours=cadence,
            )
            new_flips = (
                await persist_sweep(
                    pool, [*sweep.raw, *sweep.debounced], sweep.states,
                    method_version=METHOD_VERSION,
                )
                if sweep.states else []
            )
        except Exception as exc:  # noqa: BLE001 — degrade-not-drop
            # Nothing was persisted (one transaction), so the next sweep redoes
            # this one from the same watermarks.
            logger.warning("indicator_tracker.sweep_failed err=%s", exc)
            new_flips = []
    else:
        sweep = collect_flips(
            [dict(r) for r in inputs], hysteresis_runs=n_runs, hysteresis_hours=hours,
        )
        new_flips = [*sweep.raw, *sweep.debounced]
        for f in new_flips:
            f["backfill"] = False

    fed, groups_compared = _fed(new_flips)
    finding = _build_finding(
        fed, groups_compared,
        raw_recorded=sum(1 for f in new_flips if f["grade"] == "raw"),
        backfilled=sum(1 for f in new_flips if f["grade"] == "debounced" and f.get("backfill")),
    )

    # A feed row only when this sweep INSERTED a debounced, non-backfill flip.
    # A re-run over the same evidence inserts nothing → trace-only; so does a
    # sweep that recorded only raw toggles or history.
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        force_trace_only=not fed,
        # A2: real lineage back to the source findings the fed flips rest on.
        derived_from=_flip_finding_ids(fed),
    )


__all__ = [
    "handle",
    "collect_flips",
    "debounced_flips",
    "FlipSweep",
    "SUB_HANDLER_NAME",
    "METHOD_VERSION",
    "SCALE_VERSION",
]
