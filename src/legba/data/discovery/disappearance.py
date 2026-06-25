# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Disappearance-ratio threshold enforcement — per L-106 §5.

When a discovery cycle produces a candidate set, the registry classifies
the diff against the prior cycle's emitted ``natural_key`` set:

  * **retained** — natural_key in both prior and current set.
  * **new**      — natural_key in current set, not in prior.
  * **disappeared** — natural_key in prior set, not in current.

Per L-106 §5, transitioning disappeared candidates to ``retired`` is the
default — but a flaky list source (a 5-second outage on the upstream
HTTP fetch, a half-written file the file_sd kind picks up mid-write) can
emit a wildly truncated candidate set and trigger cascading mass-
retirements across hundreds of materialized targets.

The disappearance-ratio threshold (default 0.30) caps the per-cycle
retirement rate. If a cycle would retire more than ``threshold`` of the
previously-active candidates, the discovery routes to ``resync_review``,
alerts on ``legba.discovery.resync_anomaly``, and requires operator
clearance before retirements proceed. Pre-existing materialized
instances stay ``active`` — only the *retirement* path pauses; ingest /
analyst loops on running instances continue (per OQ-4 default lean).

Excess-disappearance candidates are routed to a structured DLQ payload
keyed by ``(discovery_id, natural_key)`` so the operator can inspect
the failed cycle without trawling logs.

Public entry points:

  * :func:`evaluate_disappearance` — given prior + current natural_key
    sets + a :class:`ResyncPolicy`, return a structured decision
    (:class:`DisappearanceDecision`) carrying the per-candidate
    classification and the recommended action.
  * :class:`DisappearanceDecision` — the structured return.

The actual retirement / DLQ write is done by the registry materialization
loop (L-181 / L-182). This module just decides; it does not mutate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal

from ._contract import (
    DEFAULT_DISAPPEARANCE_RATIO_THRESHOLD,
    ResyncPolicy,
)


# ---------------------------------------------------------------------------
# DisappearanceDecision
# ---------------------------------------------------------------------------


DisappearanceVerdict = Literal[
    "proceed",         # ratio under threshold; retire disappeared as normal
    "anomaly",         # ratio over threshold; route to resync_review per on_anomaly
    "skipped",         # under min_prior_active; ratio check intentionally bypassed
]


@dataclass(frozen=True)
class DisappearanceDecision:
    """Structured decision returned by :func:`evaluate_disappearance`.

    Attributes
    ----------
    verdict:
        ``proceed`` if the disappearance ratio is under threshold (or
        ``min_prior_active`` skipped the check); ``anomaly`` if breached.
        Callers branch on this to decide retire-vs-pause.
    prior_count:
        Size of the prior cycle's active candidate set.
    current_count:
        Size of the current cycle's emitted candidate set.
    disappeared:
        Sorted list of natural_keys present in prior but not in current.
    new:
        Sorted list of natural_keys present in current but not in prior.
    retained:
        Sorted list of natural_keys in both sets.
    ratio:
        ``len(disappeared) / max(prior_count, 1)``. 0.0 when prior was
        empty (cold start). Never exceeds 1.0.
    threshold:
        The threshold this decision was evaluated against (echoes
        :attr:`ResyncPolicy.disappearance_ratio_threshold`).
    routes_to_dlq:
        Natural_keys whose retirement is blocked by the anomaly — the
        registry routes these to ``discovery_resync_dlq`` keyed by
        ``(discovery_id, natural_key)`` for operator inspection. Empty
        on ``proceed`` (those natural_keys retire normally) and on
        ``retire_anyway`` policy (anomaly logged but retirements still
        flow through the normal path).
    on_anomaly:
        Echoes :attr:`ResyncPolicy.on_anomaly` so the caller doesn't
        have to look it up twice.
    """

    verdict: DisappearanceVerdict
    prior_count: int
    current_count: int
    disappeared: list[str]
    new: list[str]
    retained: list[str]
    ratio: float
    threshold: float
    routes_to_dlq: list[str] = field(default_factory=list)
    on_anomaly: str = "alert_and_pause"

    @property
    def anomaly(self) -> bool:
        return self.verdict == "anomaly"

    @property
    def should_alert(self) -> bool:
        """True iff the policy says to fire ``legba.discovery.resync_anomaly``."""
        return self.anomaly and self.on_anomaly in {
            "alert_and_pause",
            "alert_only",
        }

    @property
    def should_pause(self) -> bool:
        """True iff the discovery should transition to ``paused`` state."""
        return self.anomaly and self.on_anomaly == "alert_and_pause"

    @property
    def should_retire_disappeared(self) -> bool:
        """True iff the registry should proceed with retiring
        ``disappeared`` candidates this cycle."""
        if self.verdict != "anomaly":
            return True
        return self.on_anomaly == "retire_anyway"


# ---------------------------------------------------------------------------
# evaluate_disappearance
# ---------------------------------------------------------------------------


def evaluate_disappearance(
    prior_keys: Iterable[str],
    current_keys: Iterable[str],
    *,
    policy: ResyncPolicy | None = None,
) -> DisappearanceDecision:
    """Classify a cycle's diff and emit a structured retire-or-pause
    decision per L-106 §5.

    Parameters
    ----------
    prior_keys:
        Natural-keys that were active at the end of the *previous*
        discovery cycle. The first cycle (cold start) passes an empty
        iterable; the function returns ``verdict='skipped'`` with
        ``ratio=0.0`` so all current_keys are classified as ``new``.
    current_keys:
        Natural-keys emitted by the current cycle.
    policy:
        Per-descriptor :class:`ResyncPolicy`. Defaults to the L-106 §5
        defaults (threshold=0.30, alert_and_pause, min_prior_active=10).

    Notes
    -----
    The function does **not** mutate any table or publish any NATS event
    — it only classifies + recommends. The registry materialization loop
    (L-181 / L-182) consumes the decision and acts.

    Threshold semantics: a disappearance ratio *strictly greater than*
    ``threshold`` triggers the anomaly. ``threshold=0.30`` therefore
    allows up to and including 30% disappearance per cycle without
    alerting; 30.0001% trips it. This matches the L-106 §5 "more than
    30% of previously-active candidates disappear" phrasing.

    Special cases:
      * ``threshold=0.0`` — disables the check entirely; verdict is
        always ``proceed``. Useful for hand-curated lists where the
        operator explicitly never wants the pause behavior.
      * ``prior_count < min_prior_active`` — skip the check; small
        prior sets generate noisy ratios (3 → 2 is 33%).
    """
    policy = policy or ResyncPolicy()

    prior_set = set(prior_keys)
    current_set = set(current_keys)

    disappeared = sorted(prior_set - current_set)
    new = sorted(current_set - prior_set)
    retained = sorted(prior_set & current_set)

    prior_count = len(prior_set)
    current_count = len(current_set)
    ratio = (len(disappeared) / prior_count) if prior_count > 0 else 0.0

    threshold = policy.disappearance_ratio_threshold

    # Threshold=0 disables the check.
    if threshold <= 0.0:
        return DisappearanceDecision(
            verdict="proceed",
            prior_count=prior_count,
            current_count=current_count,
            disappeared=disappeared,
            new=new,
            retained=retained,
            ratio=ratio,
            threshold=threshold,
            routes_to_dlq=[],
            on_anomaly=policy.on_anomaly,
        )

    # Cold start / too-small prior — skip the check, classify as
    # ``skipped`` so callers can still log the bypass.
    if prior_count < policy.min_prior_active:
        return DisappearanceDecision(
            verdict="skipped",
            prior_count=prior_count,
            current_count=current_count,
            disappeared=disappeared,
            new=new,
            retained=retained,
            ratio=ratio,
            threshold=threshold,
            routes_to_dlq=[],
            on_anomaly=policy.on_anomaly,
        )

    if ratio > threshold:
        # Anomaly. routes_to_dlq holds the disappeared candidates unless
        # the policy says ``retire_anyway``.
        routes_to_dlq = (
            [] if policy.on_anomaly == "retire_anyway" else list(disappeared)
        )
        return DisappearanceDecision(
            verdict="anomaly",
            prior_count=prior_count,
            current_count=current_count,
            disappeared=disappeared,
            new=new,
            retained=retained,
            ratio=ratio,
            threshold=threshold,
            routes_to_dlq=routes_to_dlq,
            on_anomaly=policy.on_anomaly,
        )

    return DisappearanceDecision(
        verdict="proceed",
        prior_count=prior_count,
        current_count=current_count,
        disappeared=disappeared,
        new=new,
        retained=retained,
        ratio=ratio,
        threshold=threshold,
        routes_to_dlq=[],
        on_anomaly=policy.on_anomaly,
    )


__all__ = [
    "DEFAULT_DISAPPEARANCE_RATIO_THRESHOLD",
    "DisappearanceDecision",
    "DisappearanceVerdict",
    "evaluate_disappearance",
]
