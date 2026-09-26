# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pass planning for ``fact_contention_arbiter`` — what to recompute, and for how long.

WHAT WENT WRONG, MEASURED LIVE 2026-09-20
-----------------------------------------
The arbiter is a META analyst: every hourly pass recomputes EVERY contention
group from the open facts. That is the correctness story (the sidecar is exactly
recomputable, a stale verdict cannot stick) and it was affordable when the
corpus was small. At 131 255 open facts / 22 208 groups / 103 107 value clusters
it was not. One live pass measured:

    open-fact scan                                     2.2 s
    cluster + junk-gate + score, all 22 199 groups   107   s   (pure Python)
    _attach_earned_weights x 3 885 past-soak groups  247   s   (~64 ms each)
    ~339 000 per-group/per-value write round trips   ~550  s   (~1.6 ms each)

Over the 24 measured passes the turn was held a median of 651 s — min 443 s,
max 994 s — against a 180 s actor invoke timeout and a 20 s reconcile heal
deadline. Dapr actors are turn-based with reentrancy disabled, so for those
minutes the actor's turn queue was held: reconcile ``ENSURE_ACTIVE`` blew its
deadline 124 times in 24 h (``actor_turn.budget_exceeded op=reconcile.activate``),
the heal breaker opened 144 times (``action_executor.heal_suppressed``), and all
24 cadence fires — every single one — landed on *actor is closed, cannot handle
remind/run_cadence*. The activation path itself measures ~17 ms; it was never
slow. It was QUEUED behind the run.

THE OBSERVATION THAT FIXES IT
-----------------------------
Almost nothing changes between passes. The arbiter writes ~3.7 k
``fact_contention`` rows per 10 days — about 15 groups an hour — yet it was
re-clustering, re-scoring, re-weighing and re-writing all 22 208 every hour, to
land byte-identical rows plus a fresh ``now()``. The recompute-from-open-rows
contract does not require redoing work whose INPUTS are identical; it requires
that the stored answer still follow from the current open rows.

So this module makes the pass answer one question per group first, from the raw
scanned rows and before any clustering: *could this group's stored answer have
changed?* :func:`group_fingerprint` hashes exactly the inputs the decision reads
— the member rows, the tunables that gate it, whether the soak window has now
elapsed, and a coarse age bucket (see below). A group whose fingerprint matches
what is stored is skipped whole: no clustering, no earned-weight query, no
writes. It still counts toward the pass receipt and still registers as live, so
the stale-collapse sweep cannot mistake a skipped group for a vanished one.

THE AGE BUCKET, AND WHY IT IS NOT A FUDGE
-----------------------------------------
Two of the four Q·C·R·F factors are computed relative to the group (quorum share,
credibility share) and one is static (confidence mean). Only recency R decays
with wall-clock. But R's decay is common-mode: for two clusters with fixed
assertion times the RATIO R_a/R_b is ``0.5 ** ((t_b - t_a) / HALFLIFE_DAYS)`` —
independent of ``now``. So the dominance gate, the near-tie classification and
the total-order tie-break cannot flip through the passage of time alone. The one
time-sensitive comparison left is the ABSOLUTE floor (``best_score`` vs
``MIN_SURFACE_SCORE``), which a group crosses at most once, slowly (2.3 % a day
at a 30-day half-life). Bucketing the group's age at
``LEGBA_CONTENTION_REFRESH_HOURS`` (default 24 h) forces a full recompute once
per bucket, which catches that crossing a day early at worst. Because each
group's bucket boundary is anchored to its OWN newest assertion, the forced
recomputes spread themselves evenly across the 24 hourly passes rather than
arriving as one herd.

The honest cost: for a skipped group ``fact_contention_values.arbiter_score`` and
``fact_contention.updated_at`` are as of the last real recompute rather than the
last pass. That is a better reading of both fields than "always now()", which is
what they said before.

WHAT THE FINGERPRINT DOES NOT COVER
-----------------------------------
It hashes the arbiter's own version and its decision tunables, but not the
modules the arbiter DELEGATES to: change the clusterer's Levenshtein ceiling in
``value_clustering`` or a gate in ``fact_extractor`` without bumping
``ARBITER_VERSION`` and standing answers keep standing. The blast radius is
bounded rather than open-ended — every group's age bucket rolls within
``REFRESH_HOURS`` (24 h by default), which recomputes the whole corpus under the
new behavior — but the clean move when touching those modules is to bump
``ARBITER_VERSION``, which invalidates every stored fingerprint at once.

THE BUDGET
----------
:class:`PassBudget` is the belt to the fingerprint's braces, and it is what
actually guarantees the turn terminates. A cold corpus (no fingerprints stored
yet), a tunable change, or an ``ARBITER_VERSION`` bump invalidates every group at
once; the budget stops the pass at ``LEGBA_CONTENTION_PASS_BUDGET_SECONDS``
(default 120 s, comfortably inside the 180 s invoke timeout) and defers the rest.
No cursor is needed to make progress: the groups already done carry fresh
fingerprints, so the next pass skips them cheaply and the frontier advances on
its own. A truncated pass MUST NOT run the stale-collapse sweep — its live-key
set is incomplete, and collapsing on a partial set would tear down live groups.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

#: Wall-clock ceiling on ONE arbiter pass. Sits under the 180 s actor invoke
#: timeout with room for the scan and the finding write. ``<= 0`` disables the
#: budget (the pass runs to completion — pre-fix behavior).
PASS_BUDGET_ENV = "LEGBA_CONTENTION_PASS_BUDGET_SECONDS"
DEFAULT_PASS_BUDGET_SECONDS = 120.0

#: How coarsely a group's age is bucketed into its fingerprint — i.e. how often
#: an otherwise-unchanged group is force-recomputed so an absolute-floor
#: crossing cannot go unnoticed. ``<= 0`` disables the unchanged-skip entirely
#: (every group recomputed every pass — pre-fix behavior, kept as the escape
#: hatch).
REFRESH_HOURS_ENV = "LEGBA_CONTENTION_REFRESH_HOURS"
DEFAULT_REFRESH_HOURS = 24.0

#: Bumped whenever the fingerprint's own recipe changes, so a deploy that
#: changes WHAT is hashed invalidates every stored fingerprint rather than
#: silently comparing old hashes against new ones.
FINGERPRINT_VERSION = "fcp/1"

#: One round trip for every group's stored state — replaces the per-group
#: ``_upsert_group`` + ``_group_surface_state`` pair on the unchanged path and
#: the ``_group_surface_state`` read on the changed path.
PRIOR_GROUPS_SQL = """
SELECT id, subject_key, predicate_key, opened_at, status, value_count,
       junk_count, arbiter_version, input_fingerprint, surfaced_value,
       surfaced_fact_id, surfaced_by, surfaced_at, surface_rationale
  FROM fact_contention
"""


def _env_float(name: str, default: float) -> float:
    """A float env var, falling back on unset / malformed (never on a typo)."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "fact_contention_pass.bad_env %s=%r; using %s", name, raw, default
        )
        return default


def pass_budget_seconds() -> float:
    """The per-pass wall-clock ceiling in seconds (``<= 0`` = unbounded)."""
    return _env_float(PASS_BUDGET_ENV, DEFAULT_PASS_BUDGET_SECONDS)


def refresh_hours() -> float:
    """The forced-recompute bucket width in hours (``<= 0`` = never skip)."""
    return _env_float(REFRESH_HOURS_ENV, DEFAULT_REFRESH_HOURS)


def skip_enabled() -> bool:
    """Is the unchanged-group skip active? (``REFRESH_HOURS <= 0`` turns it off.)"""
    return refresh_hours() > 0.0


class PassBudget:
    """Monotonic wall-clock budget for one arbiter pass.

    Monotonic on purpose: a pass must not be lengthened or truncated by an NTP
    step, and ``time.monotonic`` is the only clock in the process that cannot
    move backwards.
    """

    __slots__ = ("_deadline", "_started")

    def __init__(self, seconds: float | None = None) -> None:
        budget = pass_budget_seconds() if seconds is None else seconds
        self._started = time.monotonic()
        self._deadline = (self._started + budget) if budget > 0 else None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    @property
    def remaining(self) -> float:
        """Seconds left, or ``float('inf')`` when the budget is disabled."""
        if self._deadline is None:
            return float("inf")
        return self._deadline - time.monotonic()

    def exhausted(self) -> bool:
        return self.remaining <= 0.0

    def allows(self, cost_seconds: float) -> bool:
        """Is there room to START an op that may take ``cost_seconds``?

        Used to gate the LLM tie-break: one call can burn its full 30 s timeout,
        and starting one with 2 s left would push the turn past the budget the
        budget exists to hold.
        """
        return self.remaining > cost_seconds


async def load_prior_groups(conn: Any) -> dict[tuple[str, str], Mapping[str, Any]]:
    """Every standing contention group's stored state, keyed by its triple.

    One round trip for the whole table (~22 k rows). Degrades to an EMPTY map on
    any read failure — which means "nothing is known to be unchanged", so the
    pass recomputes everything exactly as it did before this module existed.
    Fail-open is the right direction here: a degraded read must never let a
    group's stored answer drift unchecked.
    """
    try:
        rows = await conn.fetch(PRIOR_GROUPS_SQL)
    except Exception as exc:
        logger.warning("fact_contention_pass.prior_load_failed err=%s", exc)
        return {}
    prior: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in rows or ():
        try:
            prior[(row["subject_key"], row["predicate_key"])] = row
        except (KeyError, TypeError):  # legacy/fake row without the keys
            continue
    return prior


def tunables_fingerprint(pairs: Sequence[tuple[str, Any]]) -> str:
    """Hash the decision-gating tunables once per pass.

    Every knob that can change WHICH value a group surfaces belongs here: change
    one and every stored fingerprint must stop matching, so the whole corpus is
    re-decided under the new setting instead of coasting on answers reached
    under the old one.
    """
    blob = "|".join(f"{k}={v!r}" for k, v in pairs)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _row_token(row: Mapping[str, Any]) -> str:
    """One raw fact row, flattened to every field the decision reads."""
    derived = row.get("derived_from") or ()
    lineage = ",".join(sorted(str(d) for d in derived))
    produced = row.get("produced_at")
    return "\x1f".join(
        (
            str(row.get("id") or ""),
            str(row.get("value") or ""),
            f"{float(row.get('confidence') or 0.0):.6f}",
            str(row.get("source_type") or ""),
            "" if row.get("source_credibility") is None
            else f"{float(row['source_credibility']):.6f}",
            produced.isoformat() if isinstance(produced, datetime) else str(produced or ""),
            lineage,
            str(row.get("role_key") or ""),
        )
    )


def age_bucket(rows: Sequence[Mapping[str, Any]], now: datetime) -> int:
    """The group's newest-assertion age, in ``REFRESH_HOURS`` buckets.

    Anchored to the group's own newest ``produced_at``, so each group's forced
    recompute falls on its own boundary and the 22 k groups spread themselves
    across the day's passes instead of all rolling over at midnight.
    """
    width = refresh_hours()
    if width <= 0:
        return 0
    newest: datetime | None = None
    for row in rows:
        produced = row.get("produced_at")
        if isinstance(produced, datetime) and (newest is None or produced > newest):
            newest = produced
    if newest is None:
        return 0
    try:
        age = max((now - newest).total_seconds(), 0.0)
    except TypeError:  # naive/aware mismatch — treat as unknown age
        return 0
    return int(age // (width * 3600.0))


def group_fingerprint(
    rows: Sequence[Mapping[str, Any]],
    *,
    tunables: str,
    arbiter_version: str,
    now: datetime,
    past_soak: bool,
) -> str:
    """A stable hash of everything that determines this group's stored answer.

    Order-insensitive over the member rows (the scan's ORDER BY is stable today,
    but the fingerprint must not depend on that). Computed from the RAW scanned
    rows, before clustering — that is the point: a match lets the pass skip the
    clustering too, which is the single most expensive thing it does.
    """
    body = "\x1e".join(sorted(_row_token(r) for r in rows))
    head = "|".join(
        (
            FINGERPRINT_VERSION,
            arbiter_version,
            tunables,
            str(age_bucket(rows, now)),
            "soaked" if past_soak else "soaking",
        )
    )
    return hashlib.sha256(f"{head}\x1d{body}".encode("utf-8")).hexdigest()


def unchanged(
    prior: Mapping[str, Any] | None,
    fingerprint: str,
    *,
    arbiter_version: str,
) -> bool:
    """Can this group's stored answer be left exactly as it stands?

    Every clause is a reason a stored answer might NOT still follow from the
    current rows: no stored row at all (nothing to trust), a different arbiter
    build (the scoring itself may have changed), a collapsed group (it must be
    re-examined to re-open), or a fingerprint that does not match. A degraded row
    missing either column simply fails the comparison and is recomputed.
    """
    if not skip_enabled() or prior is None or not fingerprint:
        return False
    try:
        stored = prior["input_fingerprint"]
        version = prior["arbiter_version"]
        status = prior["status"]
    except (KeyError, TypeError):
        return False
    if not stored or stored != fingerprint:
        return False
    if version != arbiter_version:
        return False
    return status in ("contested", "surfaced")


__all__ = [
    "DEFAULT_PASS_BUDGET_SECONDS",
    "DEFAULT_REFRESH_HOURS",
    "FINGERPRINT_VERSION",
    "PASS_BUDGET_ENV",
    "PRIOR_GROUPS_SQL",
    "REFRESH_HOURS_ENV",
    "PassBudget",
    "age_bucket",
    "group_fingerprint",
    "load_prior_groups",
    "pass_budget_seconds",
    "refresh_hours",
    "skip_enabled",
    "tunables_fingerprint",
    "unchanged",
]
