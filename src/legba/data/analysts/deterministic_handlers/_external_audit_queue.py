# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W-2 (the durable queue + the hourly drain) and W-9 (the governor + the
degradation ladder), for the standing external auditor at width.

WHY A QUEUE EXISTS AT ALL. Width is ~470 searchable claims a day and ~990
outbound egress calls, produced by reads that land across the whole day. The
queue is what lets a claim survive between the tick that enumerated it and the
tick that grades it — durably, because an actor restart in the middle of a day
must not silently drop the rest of it. (It was ALSO a rate-limit spreader while
the ``web_access`` pack governed at ``max_invocations_per_hour: 120``: a single
daily sweep needed 8.2 hours at that cap. The operator lifted the pack to
1,000,000/h on 2026-09-20; the durability argument is the one that survives, and
the ceiling is now RESOLVED from the pack rather than copied — see
:func:`governor_invocations_per_hour`.)

WHERE IT LIVES, AND WHY NOT A NEW TABLE (design F-10, ruled). The auditor
already rides ``alert_trigger_watermarks`` (migration 0091) for its heartbeat
under ``trigger_class='external_audit'``. The queue takes a second key in the
same partition — ``watermark_key='_queue'`` — so width costs zero migrations on
the queue side and the two rows are read together. The overrule, if you want the
queue independently joinable, is one more migration; nothing else changes.

THE THREE PROPERTIES A DURABLE QUEUE HAS TO HAVE, each with the test that pins
it:

  * **Refill is idempotent.** A read enumerated twice adds nothing the second
    time, because membership is keyed on :attr:`WidthClaim.key` — a stable
    sha256 over folded text plus byte-exact origin, not on a row id or a
    position. Re-running the same tick is a no-op.
  * **The cursor survives a restart.** ``refill_watermark`` is the newest
    ``produced_at`` the queue has already enumerated. It advances only on a
    successful enumeration and it is written in the same row as the pending
    entries, so a crash between the two is not representable.
  * **The drain is deterministic.** Priority is (severity ↓, lead-block ↓,
    tier ↓, claim_key ↑) — a total order over a pure function of the claim, so
    two runs over the same queue drain the same claims in the same order. The
    final ``claim_key`` term is not decoration: without it, two equally-ranked
    claims tie and the order becomes whatever the JSON round-trip happened to
    preserve.

THE GOVERNOR IS ARITHMETIC, NOT A HOPE — AND NOT A COPY (W-9, fixed
2026-09-20). ``max_claims_per_tick`` is clamped in CODE to
:func:`governor_max_claims_per_tick`, which is the ``web_access`` pack's LIVE
``max_invocations_per_hour`` (read off the resolved binding, the same object
the enforcer reads) divided by :data:`EGRESS_CALLS_PER_CLAIM` — the worst-case
egress a single claim can spend (primary query + one reformulation + one
span-verification fetch). A descriptor PUT can lower the knob; it cannot raise
it past the governor, because the failure mode of exceeding it is not a slow
tick — it is a BLOCKED tick whose claims come back UNCHECKED and deflate the
decided rate for reasons that have nothing to do with the world.

That clamp used to be a hand-copied ``120``. It agreed with the pack until
2026-09-20, when the operator lifted the pack to 1,000,000/h and the copy did
not move — leaving the auditor clamped to 40 claims a tick against a search
plane that was by then wide open, hitting its day cap by ~06Z and returning
~85% of its claims "searched: 0". The ceiling is resolved now; the arithmetic
around it is unchanged.

THE DEGRADATION LADDER (design §0.13, F-11). Width assumes a paid SERP rung that
does not exist yet, so exhaustion must be honest rather than quiet. When the
day's claim or SERP budget cannot cover the pending population, the tick enters
**sampled mode**: a hash gate over ``sha256(claim_key)`` admits a
content-independent, replayable fraction of the claims, and that fraction is
stamped on the heartbeat, on every ledger row and on every published number. It
never fabricates, it never silently shrinks ``n``, and a partial day is never
published as a whole one — the aggregation reads ``sample_fraction`` off the
rows and reports the sampled days separately.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

from ._external_audit_claims import WidthClaim

logger = logging.getLogger(__name__)


#: The ``alert_trigger_watermarks`` key this queue owns, inside the auditor's
#: existing ``trigger_class='external_audit'`` partition.
QUEUE_KEY = "_queue"

#: Worst-case outbound egress one claim can spend: the primary query, ONE
#: reformulation on NOT_FOUND, and ONE span-verification ``web_fetch``.
EGRESS_CALLS_PER_CLAIM = 3

#: Env mirror of the ``web_access`` pack's live
#: ``max_invocations_per_hour``, for the callers that reach
#: :func:`plan_drain` with no resolved binding in hand (tests, replays, the
#: pure-function path). It should MIRROR the pack; it is not a second knob.
#: The pack is the authority — see :func:`governor_invocations_per_hour`.
GOVERNOR_EGRESS_PER_HOUR_ENV = "LEGBA_EXTERNAL_AUDIT_EGRESS_PER_HOUR"

#: What "the governor is not capping us" means as a number. The operator
#: LIFTED the web_access pack on 2026-09-20 (``max_invocations_per_hour``
#: 120 -> 1_000_000), so an absent/None governor and a lifted governor are
#: the same fact and resolve to the same value here.
DEFAULT_GOVERNOR_INVOCATIONS_PER_HOUR = 1_000_000

# --- shipped defaults for the W-2/W-9 knobs (all descriptor-overridable) ----
DEFAULT_MAX_CLAIMS_PER_TICK = 200
DEFAULT_MAX_CLAIMS_PER_DAY = 5000
DEFAULT_MAX_SERP_PER_DAY = 10000
DEFAULT_MAX_QUEUE_DEPTH = 20000
#: Rung order for the SERP ladder. SearXNG first (it is free and it carries the
#: majority of positive claims); a paid rung is appended by descriptor PUT the
#: day R-C's key exists, and until then the ladder has one rung and says so.
DEFAULT_SERP_PROVIDER_ORDER: tuple[str, ...] = ("searxng",)

#: Degradation reasons, closed vocabulary — each names WHICH budget ran out, so
#: an operator reading a sampled day knows which knob to move.
SAMPLED_REASON_CLAIM_BUDGET = "claim_budget_exhausted"
SAMPLED_REASON_SERP_BUDGET = "serp_budget_exhausted"

#: A claim whose LEDGER WRITE fails this many times is moved to the queue's
#: ``dead_letter`` list and never re-drained. The bound exists because a claim
#: failing for a non-transient reason (a value that trips a constraint no
#: writer fix anticipated) would otherwise loop forever, burning a fresh
#: SERP + grader call every tick to reproduce a write that will fail again.
DEFAULT_MAX_WRITE_ATTEMPTS = 3


def governor_invocations_per_hour(binding: Any | None = None) -> int:
    """The web_access pack's LIVE hourly ceiling, resolved — never a copy.

    THE STARVATION THIS EXISTS TO END (2026-09-20). This module used to hold
    ``GOVERNOR_MAX_INVOCATIONS_PER_HOUR = 120`` — a hand-copied snapshot of
    ``descriptors/action_pack_web_access.yaml``. The operator lifted the pack
    to 1,000,000/h; the copy did not move, so the clamp stayed at
    ``120 // 3 = 40`` claims a tick — ~13 claims/h, a day cap hit by ~06Z, and
    ~85% of the day's claims returning "searched: 0 / unchecked" against a
    search plane that was by then wide open. A constant cannot track a
    descriptor; a resolution can.

    Resolution order, tightest-binding first:

      1. **The live pack**, read off the resolved agency binding exactly the
         way :mod:`legba.data.analysts.agency.governor` reads it —
         ``binding.pack.governor.max_invocations_per_hour``. This is the same
         object the enforcer will consult on the very next call, so the clamp
         and the enforcement can never disagree.
      2. **The env mirror** :data:`GOVERNOR_EGRESS_PER_HOUR_ENV`, for the
         paths that have no binding (the pure-function callers, replays,
         tests). It should MIRROR the pack; it is documented as a mirror and
         not as a second authority, because two authorities is how the stale
         copy happened in the first place.
      3. :data:`DEFAULT_GOVERNOR_INVOCATIONS_PER_HOUR` — "not capped".

    ``None`` on the pack governor means UNCAPPED to the enforcer (it skips the
    dimension entirely), so it resolves here to the same default rather than
    to zero. Every read is defensive: a malformed binding degrades to the env
    or the default, because a clamp that raises is a tick that dies.
    """
    try:
        pack = getattr(binding, "pack", None)
        gov = getattr(pack, "governor", None)
        live = getattr(gov, "max_invocations_per_hour", None)
        if live is not None and int(live) > 0:
            return int(live)
    except Exception:  # pragma: no cover — a clamp must never raise
        logger.debug("external_audit.governor_read_failed", exc_info=True)
    raw = (os.getenv(GOVERNOR_EGRESS_PER_HOUR_ENV) or "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            logger.warning(
                "external_audit.governor_env_unparsable %s=%r — falling back "
                "to the uncapped default; this env should MIRROR "
                "action_pack_web_access.yaml:max_invocations_per_hour",
                GOVERNOR_EGRESS_PER_HOUR_ENV, raw,
            )
    return DEFAULT_GOVERNOR_INVOCATIONS_PER_HOUR


def governor_max_claims_per_tick(binding: Any | None = None) -> int:
    """The hard clamp on ``max_claims_per_tick``, from the LIVE governor.

    The arithmetic is unchanged and is still the whole point: one claim can
    spend :data:`EGRESS_CALLS_PER_CLAIM` outbound calls (primary query + one
    reformulation on NOT_FOUND + one span-verification fetch), so a tick may
    not be larger than the hour's ceiling divided by that. Only the ceiling
    moved — from a copy to the descriptor it was copied from.
    """
    return max(
        0, governor_invocations_per_hour(binding) // EGRESS_CALLS_PER_CLAIM
    )


#: How the drain ranks the day. Higher wins. World first because it is the read
#: with the widest audience and the fewest claims; thematic next; the voice
#: above the desks because it is the only tier still authoring; region last (it
#: is a deterministic rollup and carries no claims of its own).
_TIER_RANK: dict[str, int] = {
    "world_assessor": 4,
    "escalation_composition": 3,
    "world_assessment": 2,
    "country_composition": 1,
}

_SEVERITY_RANK: dict[str, int] = {
    "low": 0, "moderate": 1, "elevated": 2, "high": 3, "critical": 4,
}

_2_POW_64 = float(1 << 64)


# ---------------------------------------------------------------------------
# The hash gate — content-independent, replayable
# ---------------------------------------------------------------------------


def hash_gate(claim_key: str) -> float:
    """A stable ``[0, 1)`` draw for one claim key.

    The ``judge_sample_unit()`` discipline: no RNG, no clock, no ordering
    dependence. The same claim draws the same number forever, which is what lets
    a sampled day be re-derived — and what lets the double-grade sample be
    argued about after the fact instead of merely asserted.
    """
    digest = hashlib.sha256(str(claim_key or "").encode("utf-8")).hexdigest()
    return int(digest[:16], 16) / _2_POW_64


def admitted_by_sample(claim_key: str, fraction: float) -> bool:
    """Is this claim inside a ``fraction`` sample? ``fraction >= 1`` admits all."""
    if fraction >= 1.0:
        return True
    if fraction <= 0.0:
        return False
    return hash_gate(claim_key) < fraction


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------


def priority_key(claim: WidthClaim) -> tuple[int, int, int, str]:
    """The drain's total order — severity ↓, lead-block ↓, tier ↓, key ↑.

    Sorted ASCENDING on this tuple, so the three ranks are negated and the key
    breaks ties upward. A high-severity lead-block claim on the world read is
    therefore graded within one or two ticks of the read landing, which is the
    only latency property the alert plane needs.
    """
    severity = _SEVERITY_RANK.get(
        str(claim.claim_severity or "").strip().lower(), -1
    )
    tier = _TIER_RANK.get(str(claim.analyst_id or ""), 0)
    return (-severity, 0 if claim.lead_block else 1, -tier, claim.key)


# ---------------------------------------------------------------------------
# The durable state
# ---------------------------------------------------------------------------

_QUEUE_READ_SQL = """
SELECT state FROM alert_trigger_watermarks
WHERE trigger_class = $1 AND watermark_key = $2
"""

_QUEUE_WRITE_SQL = """
INSERT INTO alert_trigger_watermarks (trigger_class, watermark_key, state,
                                      updated_at)
VALUES ($1, $2, $3::jsonb, now())
ON CONFLICT (trigger_class, watermark_key) DO UPDATE
   SET state = EXCLUDED.state,
       updated_at = now()
"""


def empty_state(*, day: str = "") -> dict[str, Any]:
    """A queue that has never run. Every key present, so no reader guesses."""
    return {
        "entries": [],
        "refill_watermark": "",
        "day": day,
        "spent": {"claims": 0, "serp": 0},
        "sample_fraction": 1.0,
        "sample_reason": None,
        "graded_keys": [],
        # A per-claim LEDGER WRITE failure counter and its terminal quarantine
        # (see :func:`requeue_failed_writes`). Neither is a daily-budget
        # concept — they track one claim's write reliability, the same as
        # ``entries`` — so neither is reset by the day rollover in
        # :func:`load_queue`.
        "write_attempts": {},
        "dead_letter": [],
    }


def coerce_state(raw: Any, *, day: str = "") -> dict[str, Any]:
    """Read a stored queue row into the current shape, tolerantly.

    A row we cannot parse is replaced by an empty queue and LOGGED — never
    silently merged into, because a half-read queue would drop claims while
    reporting a healthy drain.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            logger.warning(
                "external_audit.queue_unparsable — starting an empty queue; "
                "the pending population for this day is rebuilt by refill"
            )
            raw = None
    if not isinstance(raw, Mapping):
        return empty_state(day=day)
    state = empty_state(day=day)
    entries = raw.get("entries")
    if isinstance(entries, list):
        state["entries"] = [dict(e) for e in entries if isinstance(e, Mapping)]
    state["refill_watermark"] = str(raw.get("refill_watermark") or "")
    state["day"] = str(raw.get("day") or day)
    spent = raw.get("spent")
    if isinstance(spent, Mapping):
        state["spent"] = {
            "claims": int(spent.get("claims") or 0),
            "serp": int(spent.get("serp") or 0),
        }
    try:
        state["sample_fraction"] = float(raw.get("sample_fraction", 1.0))
    except (TypeError, ValueError):
        state["sample_fraction"] = 1.0
    state["sample_reason"] = raw.get("sample_reason") or None
    graded = raw.get("graded_keys")
    if isinstance(graded, list):
        state["graded_keys"] = [str(k) for k in graded]
    attempts = raw.get("write_attempts")
    if isinstance(attempts, Mapping):
        coerced_attempts: dict[str, int] = {}
        for key, count in attempts.items():
            try:
                n = int(count)
            except (TypeError, ValueError):
                continue
            if n > 0:
                coerced_attempts[str(key)] = n
        state["write_attempts"] = coerced_attempts
    dead_letter = raw.get("dead_letter")
    if isinstance(dead_letter, list):
        state["dead_letter"] = [dict(d) for d in dead_letter if isinstance(d, Mapping)]
    return state


async def load_queue(
    conn: Any, *, trigger_class: str, day: str
) -> dict[str, Any]:
    """The durable queue, rolled over onto ``day`` if the stored day is older.

    THE ROLLOVER IS THE BUDGET RESET, and it happens on READ rather than on a
    scheduled tick: the auditor has no midnight run, so a reset that waited for
    one would carry yesterday's exhaustion into today and sample a day that had
    a full budget available.
    """
    try:
        row = await conn.fetchrow(_QUEUE_READ_SQL, trigger_class, QUEUE_KEY)
    except Exception as exc:
        logger.warning(
            "external_audit.queue_read_failed err=%s — this tick drains "
            "nothing and refills from scratch", exc,
        )
        return empty_state(day=day)
    state = coerce_state(row["state"] if row is not None else None, day=day)
    if state["day"] != day:
        state["day"] = day
        state["spent"] = {"claims": 0, "serp": 0}
        state["sample_fraction"] = 1.0
        state["sample_reason"] = None
        state["graded_keys"] = []
    return state


async def save_queue(
    conn: Any, state: Mapping[str, Any], *, trigger_class: str
) -> bool:
    """Persist the queue. Returns False on failure; never raises.

    A lost write costs the tick's progress, not the run: the next tick re-reads
    the older row, re-refills from its watermark, and the idempotent membership
    check means the only cost is re-grading claims already graded — which the
    ledger's UNIQUE then absorbs.
    """
    try:
        await conn.execute(
            _QUEUE_WRITE_SQL, trigger_class, QUEUE_KEY, json.dumps(dict(state))
        )
        return True
    except Exception as exc:
        logger.error(
            "external_audit.queue_write_failed err=%s — the drain's progress "
            "for this tick was not recorded; the next tick will re-enumerate",
            exc,
        )
        return False


# ---------------------------------------------------------------------------
# Refill
# ---------------------------------------------------------------------------


def refill(
    state: Mapping[str, Any],
    claims: Sequence[WidthClaim],
    *,
    watermark: str = "",
    max_depth: int = DEFAULT_MAX_QUEUE_DEPTH,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Add newly-enumerated claims to the queue. Idempotent on ``claim_key``.

    Returns ``(state, counts)``. ``counts`` names what happened to every claim
    offered — added, already queued, already graded today, or dropped because
    the queue is at depth — because a refill that silently discards is a
    population shift nobody can see.

    THE DEPTH CAP IS A SAFETY VALVE, NOT A WINDOW, and the distinction has bitten
    this codebase before (``finding_supersession``: an ``ORDER BY produced_at
    ASC`` plus a cap pulled the OLDEST rows and froze the leg). So the drop is
    COUNTED and reported, and the claims dropped are the LOWEST priority ones —
    the queue is sorted before truncation, so an overflowing day loses its
    quietest claims rather than its newest.
    """
    out = dict(state)
    entries = [dict(e) for e in (out.get("entries") or [])]
    queued = {str(e.get("claim_key") or "") for e in entries}
    graded = set(out.get("graded_keys") or [])
    counts = {"offered": len(claims), "added": 0, "already_queued": 0,
              "already_graded": 0, "dropped_over_depth": 0}
    for claim in claims:
        key = claim.key
        if key in graded:
            counts["already_graded"] += 1
            continue
        if key in queued:
            counts["already_queued"] += 1
            continue
        entries.append(claim.as_dict())
        queued.add(key)
        counts["added"] += 1
    if len(entries) > max_depth:
        entries.sort(key=lambda e: priority_key(WidthClaim.from_dict(e)))
        counts["dropped_over_depth"] = len(entries) - max_depth
        entries = entries[:max_depth]
        logger.warning(
            "external_audit.queue_over_depth dropped=%d depth=%d — the day's "
            "LOWEST-priority claims were dropped, not its newest; raise "
            "max_queue_depth or max_claims_per_tick",
            counts["dropped_over_depth"], max_depth,
        )
    out["entries"] = entries
    if watermark:
        out["refill_watermark"] = max(
            str(out.get("refill_watermark") or ""), watermark
        )
    return out, counts


# ---------------------------------------------------------------------------
# The drain + the degradation ladder
# ---------------------------------------------------------------------------


def plan_drain(
    state: Mapping[str, Any],
    *,
    max_claims_per_tick: int = DEFAULT_MAX_CLAIMS_PER_TICK,
    max_claims_per_day: int = DEFAULT_MAX_CLAIMS_PER_DAY,
    max_serp_per_day: int = DEFAULT_MAX_SERP_PER_DAY,
    binding: Any | None = None,
    egress_per_hour: int | None = None,
) -> tuple[list[WidthClaim], dict[str, Any], dict[str, Any]]:
    """Choose this tick's claims. Returns ``(claims, state, plan)``.

    THE ORDER OF OPERATIONS IS THE HONESTY. The tick's size is decided by three
    ceilings in this order, and the tightest one wins with its own name attached:

      1. the pack governor — ``max_claims_per_tick`` clamped to
         :func:`governor_max_claims_per_tick`, resolved from ``binding`` (the
         live pack) or from ``egress_per_hour`` when a caller already has the
         number;
      2. the day's remaining claim budget (``max_claims_per_day``);
      3. the day's remaining SERP budget (``max_serp_per_day``), converted to
         claims at :data:`EGRESS_CALLS_PER_CLAIM`.

    When (2) or (3) cannot cover what is pending, the tick degrades to SAMPLED
    mode: ``sample_fraction`` is the honest ratio, the gate is
    :func:`admitted_by_sample`, and the fraction reaches the heartbeat, every
    ledger row and every published number. This is the F-11 posture in code —
    width is built behind the ladder and is honest without the paid rung.
    """
    out = dict(state)
    entries = [dict(e) for e in (out.get("entries") or [])]
    entries.sort(key=lambda e: priority_key(WidthClaim.from_dict(e)))

    if egress_per_hour is not None:
        per_hour = max(0, int(egress_per_hour))
        governor_cap = per_hour // EGRESS_CALLS_PER_CLAIM
    else:
        per_hour = governor_invocations_per_hour(binding)
        governor_cap = governor_max_claims_per_tick(binding)
    tick_cap = max(0, min(int(max_claims_per_tick), governor_cap))
    governor_clamped = int(max_claims_per_tick) > governor_cap
    spent = dict(out.get("spent") or {})
    claims_left = max(0, int(max_claims_per_day) - int(spent.get("claims") or 0))
    serp_left = max(0, int(max_serp_per_day) - int(spent.get("serp") or 0))
    serp_as_claims = serp_left // EGRESS_CALLS_PER_CLAIM

    budget = min(tick_cap, claims_left, serp_as_claims)
    pending = len(entries)

    fraction = 1.0
    reason: str | None = None
    if pending > 0 and budget < min(tick_cap, pending):
        # The DAY ran out, not the tick. A tick that is merely full is not a
        # degradation — the rest of the queue is graded on the next tick.
        if claims_left <= serp_as_claims:
            reason = SAMPLED_REASON_CLAIM_BUDGET
        else:
            reason = SAMPLED_REASON_SERP_BUDGET
        fraction = round(min(1.0, budget / float(pending)), 4) if pending else 1.0

    selected: list[WidthClaim] = []
    remaining: list[dict[str, Any]] = []
    for entry in entries:
        claim = WidthClaim.from_dict(entry)
        if len(selected) < budget and (
            fraction >= 1.0 or admitted_by_sample(claim.key, fraction)
        ):
            selected.append(claim)
        else:
            remaining.append(entry)

    out["entries"] = remaining
    if reason is not None:
        out["sample_fraction"] = fraction
        out["sample_reason"] = reason
    plan = {
        "pending_before": pending,
        "pending_after": len(remaining),
        "selected": len(selected),
        "tick_cap": tick_cap,
        "governor_clamped": governor_clamped,
        # The resolved governor, ON THE PLAN. A tick that came back short is
        # now answerable from its own receipt — "which ceiling?" is a field,
        # not an archaeology exercise across a descriptor and a constant.
        "governor_invocations_per_hour": per_hour,
        "governor_max_claims_per_tick": governor_cap,
        "claims_left": claims_left,
        "serp_left": serp_left,
        "budget": budget,
        "sample_fraction": fraction if reason is not None else 1.0,
        "sample_reason": reason,
        "day_complete": reason is None and not remaining,
    }
    if governor_clamped:
        logger.warning(
            "external_audit.governor_clamped requested=%d clamped=%d — the "
            "web_access pack governs at %d invocations/hour (LIVE) and one "
            "claim can spend %d; a descriptor cannot raise this",
            int(max_claims_per_tick), tick_cap,
            per_hour, EGRESS_CALLS_PER_CLAIM,
        )
    if reason is not None:
        logger.warning(
            "external_audit.sampled_mode reason=%s fraction=%.4f pending=%d "
            "budget=%d — the day is graded at a SAMPLED fraction and every row "
            "and every published number carries it",
            reason, fraction, pending, budget,
        )
    return selected, out, plan


def record_spend(
    state: Mapping[str, Any], *, claims: int, serp: int, graded_keys: Sequence[str]
) -> dict[str, Any]:
    """Advance the day's spend and remember what was graded (refill's dedup).

    ``graded_keys`` is bounded to the day's own claims and reset by the day
    rollover, so it cannot grow without limit.
    """
    out = dict(state)
    spent = dict(out.get("spent") or {})
    out["spent"] = {
        "claims": int(spent.get("claims") or 0) + int(claims),
        "serp": int(spent.get("serp") or 0) + int(serp),
    }
    seen = list(out.get("graded_keys") or [])
    known = set(seen)
    for key in graded_keys:
        if key not in known:
            seen.append(str(key))
            known.add(key)
    out["graded_keys"] = seen
    return out


def requeue_failed_writes(
    state: Mapping[str, Any],
    failed: Sequence[tuple[WidthClaim, str]],
    *,
    max_write_attempts: int = DEFAULT_MAX_WRITE_ATTEMPTS,
) -> tuple[dict[str, Any], int, int]:
    """A ledger write failing must not cost the claim its place in the day.

    ``failed`` is ``(claim, error_class)`` for every PRIMARY claim whose
    ``external_grades`` row did not land this tick (see
    ``external_grades.write_grades``' per-row outcomes). This is the mirror
    image of :func:`plan_drain` removing a claim from ``entries`` at
    SELECTION time: a claim ``plan_drain`` drained but whose write then failed
    is put BACK, so it is eligible for the very next tick's drain rather than
    waiting on ``refill`` to rediscover a read whose watermark has already
    advanced past it — which, for the rest of the UTC day, it never will.

    Bounded, not unconditional: a claim's own ``write_attempts`` counter
    survives across ticks (in ``state``, keyed by ``claim_key`` — NOT on the
    day, the same way ``entries`` itself is not), and a claim that fails
    ``max_write_attempts`` times is moved to ``state["dead_letter"]`` instead
    of being requeued again. A permanently-failing row must be visible and
    inert, never silently dropped and never a loop that reburns a fresh SERP +
    grader call every tick to reproduce a write that will fail again.

    Returns ``(state, requeued, dead_lettered)`` — the counts this call itself
    produced, for the tick's own heartbeat.

    Unlike :func:`refill`, no ``max_depth`` truncation runs here: these claims
    were already inside depth when the day queued them; a failed-write retry
    is a return, not a new arrival, and the volume a write-failure incident
    can produce in one tick is bounded by the tick's own claim cap.
    """
    out = dict(state)
    entries = [dict(e) for e in (out.get("entries") or [])]
    attempts = dict(out.get("write_attempts") or {})
    dead_letter = [dict(d) for d in (out.get("dead_letter") or [])]
    requeued = 0
    dead_lettered = 0
    for claim, error_class in failed:
        key = claim.key
        n = int(attempts.get(key) or 0) + 1
        if n >= max(1, int(max_write_attempts)):
            dead_letter.append({
                "claim_key": key,
                "claim_text": claim.claim_text[:400],
                "analyst_id": claim.analyst_id,
                "desk_key": claim.desk_key,
                "write_attempts": n,
                "last_error": error_class,
            })
            attempts.pop(key, None)
            dead_lettered += 1
            logger.error(
                "external_audit.write_dead_lettered claim=%s attempts=%d "
                "last_error=%s — this claim's ledger write has failed %d "
                "times running and will NOT be re-drained; a human needs to "
                "look at it",
                key[:16], n, error_class, n,
            )
        else:
            entries.append(claim.as_dict())
            attempts[key] = n
            requeued += 1
            logger.warning(
                "external_audit.write_requeued claim=%s attempts=%d/%d "
                "error=%s — the claim goes back into the pending queue for "
                "the next tick's drain",
                key[:16], n, max_write_attempts, error_class,
            )
    out["entries"] = entries
    out["write_attempts"] = attempts
    out["dead_letter"] = dead_letter
    return out, requeued, dead_lettered


def serp_provider_order(raw: Any) -> tuple[str, ...]:
    """The SERP ladder, normalised. Empty / malformed → the shipped default.

    A ladder is a list of rungs in the order they are tried, and today it has
    exactly one rung. That is a statement about what exists, not a placeholder:
    SearXNG cannot verify its own emptiness at any volume, so 16–41% of every
    read is ungradable until a rung that can is appended here.
    """
    if isinstance(raw, (list, tuple)):
        rungs = tuple(
            str(r).strip() for r in raw if isinstance(r, str) and r.strip()
        )
        if rungs:
            return rungs
    return DEFAULT_SERP_PROVIDER_ORDER


def utc_day(now: datetime | None = None) -> str:
    """The budget day — UTC, ISO, so a run is replayable from its timestamp."""
    moment = now or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).date().isoformat()


def parse_day(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


__all__ = [
    "DEFAULT_GOVERNOR_INVOCATIONS_PER_HOUR",
    "DEFAULT_MAX_CLAIMS_PER_DAY",
    "DEFAULT_MAX_CLAIMS_PER_TICK",
    "DEFAULT_MAX_QUEUE_DEPTH",
    "DEFAULT_MAX_SERP_PER_DAY",
    "DEFAULT_MAX_WRITE_ATTEMPTS",
    "DEFAULT_SERP_PROVIDER_ORDER",
    "EGRESS_CALLS_PER_CLAIM",
    "GOVERNOR_EGRESS_PER_HOUR_ENV",
    "QUEUE_KEY",
    "SAMPLED_REASON_CLAIM_BUDGET",
    "SAMPLED_REASON_SERP_BUDGET",
    "admitted_by_sample",
    "coerce_state",
    "empty_state",
    "governor_invocations_per_hour",
    "governor_max_claims_per_tick",
    "hash_gate",
    "load_queue",
    "parse_day",
    "plan_drain",
    "priority_key",
    "record_spend",
    "refill",
    "requeue_failed_writes",
    "save_queue",
    "serp_provider_order",
    "utc_day",
]
