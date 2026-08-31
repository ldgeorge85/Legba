# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``band_crossing`` trigger-class internals (P1-3 trigger 1) — the FIRST of
:mod:`.alert_trigger_scan`'s eight trigger classes, extracted (module-size
gate) the same way triggers 5-8 already are (:mod:`._watchlist_scan`,
:mod:`.geo_convergence_scan`, :mod:`._production_deficit_scan`,
:mod:`._situation_escalation_scan`).

Compares each active desk's live-head ``kind='scorecard'`` row against the
``band_crossing`` watermark's last-seen band, per dimension — a crossing is a
verified-state transition by construction, since T1 bands rest ONLY on
already-verified claims (:mod:`.scorecard_banding`). See
:mod:`.alert_trigger_scan`'s module docstring, trigger class 1, for the
direction/severity mapping.

H3-GUARD (2026-08-27) — semantics-mismatch classification
-----------------------------------------------------------
The H3 banding train (damper retired + basis alignment) legitimately moves
~30 bands fleet-wide on its first post-deploy sweep, and every one of those
moves straddles a ``banding_semantics``/``damping_semantics`` stamp change
(H3 adds ``damping_semantics``, so every pre-H3 scorecard lacks it while every
post-H3 one carries ``"off"``). Reading that as ordinary
deterioration/evidence-gained would page an operator ~30 times for a
measurement artifact, not a world event.

Each desk's card-level semantics stamps (:func:`scorecard_banding.
bands_semantics`) ride EVERY watermark this scan writes, so the NEXT scan's
comparison can tell a semantics migration from a real transition
(:func:`scorecard_banding.semantics_changed` — a stamp absent on the PRIOR
side reads as differing from a CURRENT value it could not have had; both
cards missing the same stamp read as unchanged, so a card that never carried
either stamp behaves exactly as it always has). When the stamps differ,
:func:`.alert_trigger_scan.classify_band_transition` returns
``semantics-migration``/``low`` regardless of which way the band moved, and
every dimension on that desk carrying the mismatch folds into ONE
informational alert (:func:`_semantics_migration_candidate`) — never one per
dimension, since every dimension on a card shares the same stamps.
"""
from __future__ import annotations

from typing import Any, Mapping
from uuid import UUID

from . import scorecard_banding

_HEAD_SCORECARDS_SQL = """
    SELECT id::text AS row_id, target_id, data, produced_at
      FROM analyst_outputs
     WHERE kind = 'scorecard'
       AND superseded_by IS NULL
     ORDER BY target_id
"""


def _semantics_migration_candidate(
    *,
    desk: str,
    row_id: str,
    severity: str,
    transitions: list[dict[str, str]],
    refs: list[UUID],
    watermarks: list[tuple[str, str, dict[str, Any]]],
    event_at: Any,
) -> Any:
    """H3-GUARD — ONE informational alert per desk summarizing every
    dimension whose band moved under a semantics-stamp mismatch this scan.

    Never one per dimension: every dimension on a card shares the SAME
    ``banding_semantics`` / ``damping_semantics`` stamps, so a semantics
    deploy legitimately moves several dimensions in the same scan, and that
    is one migration event, not several (see ``classify_band_transition``'s
    ``semantics_changed``).
    """
    from .alert_trigger_scan import TRIGGER_BAND, AlertCandidate

    title = (
        f"Bands re-derived under new semantics: {desk} "
        f"({len(transitions)} dimension(s))"
    )
    body_lines = [
        f"desk={desk} scorecard_row={row_id} direction="
        f"{scorecard_banding.SEMANTICS_MIGRATION}",
        "reason=banding/damping semantics stamp differs from the prior "
        "scorecard for this desk — not a world-state change",
    ]
    body_lines.extend(
        f"  {t['dimension']}: {t['from_band']} → {t['to_band']}"
        for t in transitions
    )
    return AlertCandidate(
        trigger_class=TRIGGER_BAND,
        severity=severity,
        title=title[:512],
        body="\n".join(body_lines),
        target_id=desk,
        derived_from=refs,
        data={
            "trigger_class": TRIGGER_BAND,
            "desk": desk,
            "direction": scorecard_banding.SEMANTICS_MIGRATION,
            "scorecard_row_id": row_id,
            "transitions": transitions,
        },
        watermarks=watermarks,
        event_at=event_at,
    )


async def scan_band_crossings(
    conn: Any,
) -> tuple[list[Any], list[tuple[str, str, dict[str, Any]]], bool]:
    """Returns (candidates, silent_watermark_upserts, was_seeded)."""
    # Local imports — alert_trigger_scan imports this module; deferring the
    # reverse import to call time keeps the cycle harmless (the
    # _watchlist_scan / geo_convergence_scan / _production_deficit_scan /
    # _situation_escalation_scan precedent).
    from .alert_trigger_scan import (
        TRIGGER_BAND,
        AlertCandidate,
        _load_class_watermarks,
        _MAX_DERIVED_REFS,
        _parse_jsonish,
        _uuid_or_none,
        classify_band_transition,
    )

    seeded, watermarks = await _load_class_watermarks(conn, TRIGGER_BAND)
    rows = await conn.fetch(_HEAD_SCORECARDS_SQL)

    candidates: list[Any] = []
    silent: list[tuple[str, str, dict[str, Any]]] = []
    for row in rows:
        desk = str(row["target_id"] or "")
        if not desk:
            continue
        data = _parse_jsonish(row["data"]) or {}
        if not isinstance(data, Mapping):
            continue
        # The analyst_outputs.data column stores the FULL payload dump, so the
        # producer's payload `data` dict is NESTED under 'data' (mirrors the
        # parse_unit_eval contract in scorecard_producer). Accept a bare
        # {'bands': ...} too (defensive).
        inner = data.get("data") if isinstance(data.get("data"), Mapping) else data
        bands = inner.get("bands") if isinstance(inner, Mapping) else None
        dims = bands.get("dimensions") if isinstance(bands, Mapping) else None
        if not isinstance(dims, Mapping):
            continue
        row_id = str(row["row_id"])
        # H3-GUARD — this card's semantics stamps, read ONCE (a card-level
        # constant, not per-dimension) and carried on every watermark this
        # card writes so the NEXT scan's comparison can tell a semantics
        # migration from a real transition.
        card_semantics = scorecard_banding.bands_semantics(bands)
        migration_transitions: list[dict[str, str]] = []
        migration_watermarks: list[tuple[str, str, dict[str, Any]]] = []
        migration_refs: list[UUID] = []
        migration_severity = scorecard_banding.SEMANTICS_MIGRATION_SEVERITY
        for dim, verdict in dims.items():
            if not isinstance(verdict, Mapping):
                continue
            band = str(verdict.get("band") or "")
            if not band:
                continue
            key = f"{desk}|{dim}"
            state = {
                "band": band,
                "scorecard_row_id": row_id,
                "banding_semantics": card_semantics[0],
                "damping_semantics": card_semantics[1],
            }
            prev = watermarks.get(key)
            if not seeded or prev is None:
                # First-ever scan of the class, or a desk×dimension appearing
                # for the first time: seed silently (no previous row to
                # transition FROM).
                silent.append((TRIGGER_BAND, key, state))
                continue
            prev_band = str(prev.get("band") or "")
            if prev_band == band:
                if prev.get("scorecard_row_id") != row_id:
                    # New scorecard row, same band — bookkeeping only.
                    silent.append((TRIGGER_BAND, key, state))
                continue
            prev_semantics = (
                prev.get("banding_semantics"),
                prev.get("damping_semantics"),
            )
            changed = scorecard_banding.semantics_changed(
                prev_semantics, card_semantics
            )
            direction, severity = classify_band_transition(
                prev_band, band, semantics_changed=changed
            )
            refs: list[UUID] = []
            rid = _uuid_or_none(row_id)
            if rid is not None:
                refs.append(rid)
            for bid in verdict.get("basis") or []:
                b = _uuid_or_none(bid)
                if b is not None and b not in refs and len(refs) < _MAX_DERIVED_REFS:
                    refs.append(b)

            if direction == scorecard_banding.SEMANTICS_MIGRATION:
                migration_transitions.append(
                    {
                        "dimension": str(dim),
                        "from_band": prev_band,
                        "to_band": band,
                    }
                )
                migration_watermarks.append((TRIGGER_BAND, key, state))
                migration_severity = severity
                for ref in refs:
                    if (
                        ref not in migration_refs
                        and len(migration_refs) < _MAX_DERIVED_REFS
                    ):
                        migration_refs.append(ref)
                continue

            transition_key = f"{desk}|{dim}|{prev_band}->{band}|{row_id}"
            candidates.append(
                AlertCandidate(
                    trigger_class=TRIGGER_BAND,
                    severity=severity,
                    title=(
                        f"Band {direction}: {desk} {dim} "
                        f"{prev_band} → {band}"
                    ),
                    body=(
                        f"desk={desk} dimension={dim}\n"
                        f"from={prev_band} to={band} direction={direction}\n"
                        f"scorecard_row={row_id} "
                        f"prev_scorecard_row={prev.get('scorecard_row_id')}\n"
                        f"band_reason={verdict.get('reason')} "
                        f"effective_confidence={verdict.get('effective_confidence')}"
                    ),
                    target_id=desk,
                    derived_from=refs,
                    data={
                        "trigger_class": TRIGGER_BAND,
                        "desk": desk,
                        "dimension": str(dim),
                        "from_band": prev_band,
                        "to_band": band,
                        "direction": direction,
                        "scorecard_row_id": row_id,
                        "prev_scorecard_row_id": prev.get("scorecard_row_id"),
                        "transition_key": transition_key,
                        "band_basis": [str(b) for b in (verdict.get("basis") or [])],
                    },
                    watermarks=[(TRIGGER_BAND, key, state)],
                    event_at=row["produced_at"],
                )
            )

        if migration_transitions:
            # H3-GUARD — at most ONE informational alert for this desk, no
            # matter how many dimensions moved under the mismatch.
            candidates.append(
                _semantics_migration_candidate(
                    desk=desk,
                    row_id=row_id,
                    severity=migration_severity,
                    transitions=migration_transitions,
                    refs=migration_refs,
                    watermarks=migration_watermarks,
                    event_at=row["produced_at"],
                )
            )
    return candidates, silent, seeded


__all__ = ["scan_band_crossings"]
