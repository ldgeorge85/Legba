# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Calibration honesty — one definition, shared by every reader.

The legacy calibration figures (``calibration_tracking``'s Brier over the
country_predictor / competing_hypotheses lines) were read by four surfaces that
each re-derived the same flags inline: the journal tool
(``SubstrateQueryPort.get_calibration``), ``GET /eval/calibration``, the
journal honesty banner, and the handler itself. This leaf module (stdlib only,
so the slim registry image can import it) holds the named thresholds, the
signed hypothesis-confidence mapping, and ONE reducer for a stored finding.

WHAT THESE FIGURES ARE NOT. They are not the sealed forecast ledger
(``forecast_questions`` / ``forecast_resolutions``): nothing here reads it. They
are legacy, under diagnosis, and every reduction carries :data:`LEGACY_LABEL`
and :data:`LEDGER_NOTE` so a reader cannot mistake one for the other.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

#: Below this many exogenously-resolved acute-pilot calls the pilot's Brier is
#: withheld ("accumulating"). The single home of the former hard-coded "n<30".
ACUTE_PILOT_MIN_SAMPLE = 30

#: Below this many exogenous rows the headline Brier is "insufficient" and the
#: calibration is "thin".
EXOGENOUS_MIN_SAMPLE = 5

#: A calibration figure whose newest member prediction is older than this many
#: days is returned ``stale: true`` with its newest date.
CALIBRATION_STALE_AFTER_DAYS = 30

#: The date from which hypothesis claimed-confidence is SIGNED. Stored findings
#: before this date used 0.5 + 0.15*|evidence_balance| (a thesis with strongly
#: negative evidence scored as high confidence IN it). Stored history is not
#: rewritten; the calibration is recomputed with the corrected mapping from the
#: first run at or after this date.
SIGNED_CONFIDENCE_FROM = "2026-11-11"

#: The label every legacy figure carries.
LEGACY_LABEL = "untrusted — legacy, under diagnosis"

#: The reminder that these are not the sealed ledger.
LEDGER_NOTE = (
    "These figures are NOT the sealed forecast ledger (forecast_questions / "
    "forecast_resolutions); this computation does not read it."
)

#: Analysts whose "outcome" is whether OUR OWN collected signal count fell in a
#: predicted range — collection volume, not world events.
COLLECTION_VOLUME_ANALYSTS: frozenset[str] = frozenset({"country_predictor"})

VOLUME_LABEL = "collection-volume predictor (outcome = our own signal count; not a forecast of world events)"

# Population keys carried in the finding's ``populations`` block.
POP_HEADLINE = "headline_exogenous"
POP_POOLED = "pooled_diagnostic"
POP_VOLUME = "collection_volume_predictor"
POP_SELF = "hypotheses_self_consistency"
POP_WEAK = "hypotheses_lexical_weak"

_POP_LABELS: dict[str, str] = {
    POP_HEADLINE: "exogenous forecast calibration",
    POP_POOLED: "pooled diagnostic (excludes the collection-volume predictor)",
    POP_VOLUME: VOLUME_LABEL,
    POP_SELF: "hypotheses graded by their own status (self-consistency, not calibration)",
    POP_WEAK: "hypotheses/predictions graded by a lexical proxy (weak tier)",
}


def hypothesis_claimed_confidence(evidence_balance: Any) -> float:
    """Signed claimed confidence for a hypothesis: ``clamp(0.5 + 0.15*balance)``.

    The sign is kept: strong evidence AGAINST the thesis is LOW confidence in it.
    (The retired mapping took ``abs(balance)``.)
    """
    try:
        bal = int(evidence_balance or 0)
    except (TypeError, ValueError):
        bal = 0
    return max(0.0, min(1.0, 0.5 + 0.15 * bal))


def _parse_ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        dt = v
    elif isinstance(v, str) and v:
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def newest_member_at(rows: list[Mapping[str, Any]]) -> str | None:
    """ISO timestamp of the newest member prediction (``produced_at``)."""
    best: datetime | None = None
    for r in rows:
        dt = _parse_ts(r.get("produced_at"))
        if dt is not None and (best is None or dt > best):
            best = dt
    return best.isoformat() if best else None


def staleness(
    newest: Any,
    *,
    now: datetime | None = None,
    after_days: int = CALIBRATION_STALE_AFTER_DAYS,
) -> dict[str, Any]:
    """``{stale, newest_member_at, age_days}``; ``stale`` is None when unknown."""
    dt = _parse_ts(newest)
    if dt is None:
        return {"stale": None, "newest_member_at": None, "age_days": None}
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age = (now - dt).total_seconds() / 86400.0
    return {
        "stale": age > after_days,
        "newest_member_at": dt.isoformat(),
        "age_days": round(age, 1),
    }


def population_label(key: str) -> str:
    return _POP_LABELS.get(key, key)


def reduce_calibration(
    data: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reduce a ``calibration_tracking`` finding's metrics dict to the honest read.

    Every Brier is returned with ITS OWN n. ``brier_exogenous`` pairs with
    ``exogenous_sample_size``; ``brier_pooled`` with ``pooled_sample_size``
    (the pooled count — never paired with another Brier). The acute-pilot keys
    are untouched. A finding written before scoping existed (no ``populations`` block)
    cannot be scoped, so its mixed headline is WITHHELD and kept under
    ``legacy_unscoped`` until the next calibration run replaces it.
    """
    bss = data.get("brier_skill_score")
    ready = bool(data.get("forecast_acute_ready"))
    degenerate = bool(data.get("forecast_acute_degenerate"))
    forecast_proven = (
        ready and not degenerate and isinstance(bss, (int, float)) and bss > 0.0
    )
    pops_raw = data.get("populations")
    legacy_format = not isinstance(pops_raw, Mapping)

    populations: list[dict[str, Any]] = []
    if not legacy_format:
        for key, p in pops_raw.items():
            if not isinstance(p, Mapping):
                continue
            st = staleness(p.get("newest_member_at"), now=now)
            populations.append({
                "key": key,
                "label": population_label(key),
                "brier": p.get("brier"),
                "n": p.get("n"),
                **st,
            })

    out: dict[str, Any] = {
        "label": LEGACY_LABEL,
        "ledger_note": LEDGER_NOTE,
        "stale_after_days": CALIBRATION_STALE_AFTER_DAYS,
        "hypothesis_confidence_signed_from": SIGNED_CONFIDENCE_FROM,
        "legacy_finding_format": legacy_format,
        "populations": populations,
        "insufficient_exogenous": data.get("insufficient_exogenous"),
        "self_consistency_only": data.get("self_consistency_only"),
        "collection_volume_sample_size": data.get("collection_volume_sample_size"),
    }
    exo_n = data.get("exogenous_sample_size")
    if legacy_format:
        out.update({
            "brier_exogenous": None,
            "exogenous_sample_size": exo_n,
            "brier_pooled": None,
            "pooled_sample_size": None,
            "stale": None,
            "newest_member_at": None,
            "withheld_reason": (
                "finding predates the collection-volume fix: its Brier mixes the collection-volume "
                "predictor with other lines and cannot be scoped; withheld until "
                "the next calibration run"
            ),
            "legacy_unscoped": {
                "brier_exogenous": data.get("brier_exogenous"),
                "exogenous_sample_size": exo_n,
                "brier_pooled": data.get("brier_pooled"),
                "pooled_sample_size": data.get("sample_size"),
            },
        })
    else:
        head = next((p for p in populations if p["key"] == POP_HEADLINE), None)
        out.update({
            "brier_exogenous": data.get("brier_exogenous"),
            "exogenous_sample_size": exo_n,
            "brier_pooled": data.get("brier_pooled"),
            "pooled_sample_size": data.get("sample_size"),
            "stale": head["stale"] if head else None,
            "newest_member_at": head["newest_member_at"] if head else None,
        })
    calibration_thin = not isinstance(exo_n, int) or exo_n < EXOGENOUS_MIN_SAMPLE
    out.update({
        "forecast_unproven": not forecast_proven,
        "calibration_thin": calibration_thin,
    })
    return out
