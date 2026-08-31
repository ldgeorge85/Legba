# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FRAME-3 steady-state suppression guard (2026-08-29) — extracted from
:mod:`.alert_trigger_scan` (module-size gate) the same way triggers 1 and 5-8
already are. Fully self-contained: no reverse import needed (unlike its
siblings), since :func:`classify_finding_suppression` is pure Python with no
database and no dependency on :class:`~.alert_trigger_scan.AlertCandidate`.
Re-exported into :mod:`.alert_trigger_scan`'s own namespace at import time so
``ats.classify_finding_suppression`` / ``ats.TRIGGER_FINDING_STATE`` /
``ats.DEFAULT_STEADY_COOLDOWN_HOURS`` keep working exactly as before.

See :mod:`.alert_trigger_scan`'s module docstring, trigger class 2, for the
full design rationale and the 2026-08-29 soak measurement that decided it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional

#: A SECOND, desk-scoped watermark namespace (distinct trigger_class value, own
#: PK space in the SAME `alert_trigger_watermarks` table — no migration) that
#: tracks, per desk, the severity band and wall-clock time of the last time
#: THIS DESK actually PAGED under `verified_finding` — never advanced on a
#: suppressed occurrence, so cooldown always counts from what the operator
#: last actually saw. Never emits its own alert candidates and is absent from
#: :data:`~.alert_trigger_scan.TRIGGER_CLASSES` / ``_CLASS_PRIORITY`` /
#: ``_UNVERIFIED_REASONS`` by design — it is bookkeeping for TRIGGER_FINDING,
#: not a page-emitting class of its own.
TRIGGER_FINDING_STATE = "verified_finding_state"

#: A desk's unchanged-band verified_finding candidate is suppressed only while
#: WITHIN this many hours of the desk's last real page; once elapsed, the next
#: candidate pages regardless (a heartbeat, not indefinite silence for a
#: standing-high desk). 24h chosen so an operator gets at most one
#: steady-state reminder per desk per day even under FRAME-3's every-cycle
#: "high" re-tagging — retunable per descriptor option
#: (``steady_cooldown_hours``) with no deploy, S-1's precedent.
DEFAULT_STEADY_COOLDOWN_HOURS = 24.0


def _parse_iso_datetime(raw: Any) -> Optional[datetime]:
    """A tz-aware datetime from an ISO string, or ``None`` — never raises.

    A missing tzinfo (defensive; every writer in this module stamps UTC) is
    treated as UTC rather than rejected, so a comparison against another
    tz-aware ``now`` never raises ``TypeError``.
    """
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def classify_finding_suppression(
    *,
    prev_state: Optional[Mapping[str, Any]],
    severity: str,
    delta_tag: Optional[str],
    now: datetime,
    cooldown_hours: float,
) -> tuple[bool, str]:
    """(suppress?, reason) for one ``verified_finding`` candidate.

    FRAME-3 steady-state guard (2026-08-29, soak-measured — see the module
    docstring). Three STRUCTURED signals gate a suppression; ANY one of them
    reading "changed" or "unknown" pages — only unanimous "unchanged, recent"
    suppresses:

      1. **the desk's own last-recorded severity band** (``prev_state`` —
         THIS handler's :data:`TRIGGER_FINDING_STATE` watermark, a fact this
         engine itself observed, not a model claim) must equal the current
         candidate's ``severity``;
      2. **the finding's own ``severity_delta`` tag** — a MODEL claim, and
         exactly what the original 2026-08-21 deferral refused to key on
         ALONE — is read only as a VETO here: ``rose`` / ``fell`` / ``new``
         always pages even when (1) reads unchanged (a within-band rise the
         coarse band can't see, or a first read for a dimension on an
         already-paged desk). Only ``steady`` or an absent tag lets (1) and
         (3) decide;
      3. **recency** — the cooldown must not yet have elapsed since the last
         time this desk ACTUALLY PAGED under this trigger
         (``prev_state['paged_at']``, advanced ONLY on a real page, never on
         a suppressed occurrence — see ``_scan_verified_findings``), so a
         standing-high desk (a war still running) still gets a periodic
         heartbeat page rather than indefinite silence.

    No prior desk record at all (``prev_state is None``) is the FIRST page
    ever recorded for this desk under this trigger and always pages — there
    is nothing yet to compare against.
    """
    if prev_state is None:
        return False, "first_page_for_desk"
    if delta_tag in ("rose", "fell", "new"):
        return False, f"delta_tag_{delta_tag}"
    prev_severity = str(prev_state.get("severity") or "")
    if prev_severity != severity:
        return False, "band_changed"
    prev_paged_at = _parse_iso_datetime(prev_state.get("paged_at"))
    if prev_paged_at is None:
        return False, "no_prior_page_timestamp"
    elapsed_hours = (now - prev_paged_at).total_seconds() / 3600.0
    if elapsed_hours >= cooldown_hours:
        return False, "cooldown_elapsed"
    return True, "steady_state_within_cooldown"


async def write_suppressed(
    conn: Any,
    candidates: "list[Any]",
    *,
    analyst_id: str,
    analyst_version: Optional[str],
    run_uuid: Any,
) -> tuple[int, int]:
    """Writes every guard-suppressed candidate DIRECTLY — never through the
    desk-cap/budget pageable pipeline, never fanned out. SUPPRESS != DROP:
    the row is durable, tagged ``suppressed:true`` (see ``_write_alert_row``),
    and its per-finding watermark still advances so the same finding never
    re-enters the scan. Returns ``(written, write_failures)``.
    """
    from .alert_trigger_scan import _upsert_watermark, _write_alert_row

    written = 0
    write_failures = 0
    for cand in candidates:
        cand.data.setdefault("budget_deferred", False)
        row_id = await _write_alert_row(
            conn,
            cand,
            analyst_id=analyst_id,
            analyst_version=analyst_version,
            run_uuid=run_uuid,
        )
        if row_id is None:
            write_failures += 1
            continue
        written += 1
        for wm_class, wm_key, wm_state in cand.watermarks:
            await _upsert_watermark(conn, wm_class, wm_key, wm_state, fired=False)
        # Deliberately no dispatcher.fan_out call here.
    return written, write_failures


__all__ = [
    "DEFAULT_STEADY_COOLDOWN_HOURS",
    "TRIGGER_FINDING_STATE",
    "classify_finding_suppression",
    "write_suppressed",
]
