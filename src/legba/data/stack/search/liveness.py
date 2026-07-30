# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Engine-set liveness — absence must be MEASURED, never assumed.

WHY THIS MODULE EXISTS
----------------------
Over a multi-engine meta-search, a genuinely empty result set for any real
query is close to impossible. Even a nonsense query returns unrelated noise;
even an obscure one returns *something* from *some* engine. So in practice
**an empty result set means BROKEN** — every engine banned, an encoding bug, a
network fault — not "the web contains nothing about this".

Before this module, :attr:`~.base.SearchStatus.EMPTY` (provider ran, reported
no unresponsive engines, returned zero rows) was treated as a citable TRUE
absence: ``supports_absence_claim`` was True. That default is wrong, and it is
wrong in the most expensive direction available to an intelligence platform —
it lets a broken search manufacture exactly the FALSE-ABSENCE claims a
production correctness review caught in analytical prose ("no reporting on X
exists" when the truth was "we were blocked", or "we never collected it").

Reading ``unresponsive_engines`` closes only half the hole. It catches the case
where the provider KNOWS it served partial results. It cannot catch:

  * an instance whose engine set is empty / misconfigured (nothing to report as
    unresponsive — there are no engines to refuse);
  * a query-encoding bug that reaches every engine as gibberish;
  * an upstream that answers 200 + ``{"results": []}`` with no error field;
  * a provider whose degradation field we do not know how to read.

All four are HTTP 200 + zero results + no degradation signal — byte-identical
to a true absence. The only honest way to tell them apart is to MEASURE: issue
one bounded CONTROL PROBE — a fixed, deliberately high-yield query that must
return results if the engine set is alive — through the SAME provider, and
decide from the outcome:

  * control returns results ⇒ the engine set is demonstrably live, so the empty
    is real FOR THAT QUERY. This licenses a **scoped** absence only ("these
    engines returned nothing for this query"), NEVER "X does not exist".
  * control ALSO returns empty (or the probe itself errors) ⇒ the plane is
    broken ⇒ the original empty is folded into the DEGRADED_EMPTY failure
    class, which is already a loud tool FAILURE, not an empty success.

ONE ORGAN, NOT TWO
------------------
The R-3b handoff separately declared a "low-cadence control-query canary" to be
wired into the watchdog cron, because ``health_check`` is TCP-only and reports
HEALTHY while every upstream engine is banned. That canary and this probe are
the same measurement. A coherence audit found ~40 organs where ~15 archetypes
would do, so this is implemented ONCE, here: :func:`verify_engine_liveness` is
the single code path. A cadence hook calls it with ``force=True`` (bypassing
the freshness cache) and reads the verdict; it does NOT get its own copy of the
logic, its own probe query, or its own threshold.

COST DISCIPLINE
---------------
The probe costs one real upstream query, and upstream-engine goodwill is the
scarce resource that keeps a self-hosted meta-search unbanned. So:

  * it runs ONLY on a zero-result response that reported NO unresponsive engine
    (a response that already admits degradation needs no probe — it is already
    not-absence);
  * the verdict is CACHED per provider for :data:`CONTROL_PROBE_TTL_SECONDS`,
    because "is the engine set answering right now" is a per-MOMENT question,
    not a per-query one. A run that hits several empties therefore costs ONE
    probe, not N;
  * concurrent callers collapse onto one in-flight probe via a per-provider
    lock.

DEFERRED REQUEUE, NEVER AN IMMEDIATE RETRY
------------------------------------------
When search fails, retrying immediately against engines that are already
refusing worsens the ban and double-counts against them — which is why the
no-retry-on-degraded rule exists and is preserved. But a caller draining a
standing-question backlog still needs to know "try this later, not now". That
is :class:`DeferralAdvice`: a bounded, exponentially backing-off, per-provider
signal carried on the tool result. It is ADVICE, not a queue — see
:func:`compute_deferral` for the consumption contract.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .base import (
    HardSearchFailure,
    LivenessVerdict,
    SearchResponse,
    TransientSearchFailure,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------

#: The control query. Chosen to be maximally high-yield and boring: a
#: two-token, globally-indexed proper noun that EVERY general web engine has
#: millions of documents for. If this returns nothing, the engine set is not
#: answering — there is no plausible world in which the live web has no page
#: about the United Nations. Deliberately NOT topical, so it can never be
#: confused with an analytic query in a log or a provenance trail, and
#: deliberately fixed, so the "expected non-zero" property stays stable over
#: time (a trendy query would decay).
CONTROL_PROBE_QUERY = "united nations"

#: Results to ask for. We only need the count>0 bit; asking for fewer is
#: cheaper on the upstream engines and on the merge.
CONTROL_PROBE_LIMIT = 3

#: How long a liveness verdict stays fresh. The question is per-MOMENT, so this
#: is short; it is long enough that a single analyst run with several empty
#: searches pays exactly one probe. SearXNG's own ban windows
#: (``ban_time_on_fail``) are minutes-to-hours, so a 5-minute verdict cannot
#: mask a recovery for long.
CONTROL_PROBE_TTL_SECONDS = 300.0

#: Operator override for the TTL above (seconds). Unset = the default.
CONTROL_PROBE_TTL_ENV = "LEGBA_SEARCH_CONTROL_PROBE_TTL_SECONDS"


def control_probe_ttl_seconds() -> float:
    """The effective TTL, honouring the operator override.

    A non-numeric or non-positive override is IGNORED (logged) rather than
    silently disabling the cache — a zero TTL would mean one real upstream
    query per empty result, which is exactly the ban pressure this cache
    exists to avoid.
    """
    raw = (os.environ.get(CONTROL_PROBE_TTL_ENV) or "").strip()
    if not raw:
        return CONTROL_PROBE_TTL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "search.liveness.bad_ttl_override %s=%r — using the %.0fs default",
            CONTROL_PROBE_TTL_ENV, raw, CONTROL_PROBE_TTL_SECONDS,
        )
        return CONTROL_PROBE_TTL_SECONDS
    if value <= 0:
        logger.warning(
            "search.liveness.non_positive_ttl_override %s=%r — using the %.0fs "
            "default (a zero TTL would burn one upstream query per empty "
            "result, which is the ban pressure the cache prevents)",
            CONTROL_PROBE_TTL_ENV, raw, CONTROL_PROBE_TTL_SECONDS,
        )
        return CONTROL_PROBE_TTL_SECONDS
    return value


@dataclass
class _CachedVerdict:
    verdict: LivenessVerdict
    detail: str
    decided_at: float


@dataclass
class _FailureStreak:
    """Consecutive failures for one provider — the deferral ladder's state.

    Deliberately IN-PROCESS and not a table: the deferral signal is advice for
    the caller's NEXT cadence tick, and "how many times in a row has this
    provider just failed" is a per-moment property of the running runtime, the
    same class of state as the liveness verdict itself. A restart resetting the
    ladder to its base delay is correct — a fresh process has no evidence the
    provider is still refusing.
    """

    count: int = 0
    last_reason: str = ""


class SearchLivenessCache:
    """Per-provider liveness verdicts + deferral streaks, with a short TTL.

    Keyed by PROVIDER, not by query — the liveness question is about the engine
    set, so one probe answers it for every query in the window.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._verdicts: dict[str, _CachedVerdict] = {}
        self._failures: dict[str, _FailureStreak] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_loop_id: int | None = None
        #: Count of REAL upstream probes issued. Observability + the test that
        #: proves N empties cost 1 probe.
        self.probes: int = 0

    @property
    def ttl_seconds(self) -> float:
        return self._ttl if self._ttl is not None else control_probe_ttl_seconds()

    # -- verdicts -----------------------------------------------------------

    def get(self, provider_key: str) -> tuple[LivenessVerdict, str] | None:
        entry = self._verdicts.get(provider_key)
        if entry is None:
            return None
        if (self._clock() - entry.decided_at) >= self.ttl_seconds:
            self._verdicts.pop(provider_key, None)
            return None
        return entry.verdict, entry.detail

    def put(
        self, provider_key: str, verdict: LivenessVerdict, detail: str,
    ) -> None:
        self._verdicts[provider_key] = _CachedVerdict(
            verdict=verdict, detail=detail, decided_at=self._clock(),
        )

    # -- deferral ladder ----------------------------------------------------

    def record_failure(self, provider_key: str, reason: str) -> int:
        streak = self._failures.setdefault(provider_key, _FailureStreak())
        streak.count += 1
        streak.last_reason = reason
        return streak.count

    def record_success(self, provider_key: str) -> None:
        """Clear the streak. A served search is proof the ladder should reset."""
        self._failures.pop(provider_key, None)

    def failure_count(self, provider_key: str) -> int:
        streak = self._failures.get(provider_key)
        return streak.count if streak is not None else 0

    # -- concurrency --------------------------------------------------------

    def lock_for(self, provider_key: str) -> asyncio.Lock:
        """One lock per provider, per event loop.

        A module-level cache outlives any single event loop (the test suite
        builds a fresh loop per test); an :class:`asyncio.Lock` bound to a dead
        loop raises on use. Drop the whole lock map when the running loop
        changes — locks carry no state worth preserving across loops.
        """
        loop_id = id(asyncio.get_running_loop())
        if self._locks_loop_id != loop_id:
            self._locks.clear()
            self._locks_loop_id = loop_id
        lock = self._locks.get(provider_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[provider_key] = lock
        return lock

    # -- lifecycle ----------------------------------------------------------

    def reset(self) -> None:
        self._verdicts.clear()
        self._failures.clear()
        self._locks.clear()
        self._locks_loop_id = None
        self.probes = 0


#: The process-wide cache. One instance so every caller — the ``web_search``
#: tool, a cadence canary, a future agentic provider — shares one verdict and
#: one probe budget.
DEFAULT_LIVENESS_CACHE = SearchLivenessCache()


async def verify_engine_liveness(
    handler: Any,
    *,
    provider_key: str,
    cache: SearchLivenessCache | None = None,
    force: bool = False,
) -> tuple[LivenessVerdict, str]:
    """Measure whether ``handler``'s engine set is answering AT ALL.

    THE single control-probe code path (see the module docstring — this
    subsumes the separately-declared low-cadence canary). Callers:

      * ``web_search`` calls it on a zero-result, non-degraded response, so the
        empty can be classified honestly;
      * a cadence/watchdog hook calls it with ``force=True`` to refresh the
        verdict on a schedule regardless of query traffic.

    Returns ``(verdict, detail)`` and NEVER raises: a probe that itself fails is
    :attr:`~.base.LivenessVerdict.PROBE_FAILED`, which is treated exactly like
    a dead engine set (not-absence), because an unverifiable plane and a broken
    plane license the same conclusion: nothing.
    """
    cache = cache if cache is not None else DEFAULT_LIVENESS_CACHE
    if not force:
        cached = cache.get(provider_key)
        if cached is not None:
            verdict, detail = cached
            return verdict, f"{detail} [cached]"

    async with cache.lock_for(provider_key):
        # Re-check under the lock: a concurrent caller may have just probed,
        # which is the whole point of the lock (N empties ⇒ 1 probe).
        if not force:
            cached = cache.get(provider_key)
            if cached is not None:
                verdict, detail = cached
                return verdict, f"{detail} [cached]"

        cache.probes += 1
        try:
            probe = await handler.search(
                CONTROL_PROBE_QUERY, limit=CONTROL_PROBE_LIMIT,
            )
        except (TransientSearchFailure, HardSearchFailure) as exc:
            verdict = LivenessVerdict.PROBE_FAILED
            detail = (
                f"control probe {CONTROL_PROBE_QUERY!r} could not run "
                f"({type(exc).__name__}: {exc}) — engine-set liveness is "
                "UNVERIFIABLE, so the empty result is not absence"
            )
        except Exception as exc:  # a provider that raises something else
            verdict = LivenessVerdict.PROBE_FAILED
            detail = (
                f"control probe {CONTROL_PROBE_QUERY!r} raised an unexpected "
                f"{type(exc).__name__}: {exc} — engine-set liveness is "
                "UNVERIFIABLE, so the empty result is not absence"
            )
        else:
            count = len(getattr(probe, "results", []) or [])
            if count > 0:
                verdict = LivenessVerdict.LIVE
                detail = (
                    f"control probe {CONTROL_PROBE_QUERY!r} returned {count} "
                    "result(s) — the engine set is answering, so the empty is "
                    "real FOR THIS QUERY (a SCOPED absence, never a claim that "
                    "the subject does not exist)"
                )
            else:
                unresponsive = list(
                    getattr(probe, "unresponsive_engines", []) or []
                )
                verdict = LivenessVerdict.DEAD
                detail = (
                    f"control probe {CONTROL_PROBE_QUERY!r} ALSO returned zero "
                    "results — the engine set is NOT answering (a live web has "
                    "results for this query). The search plane is broken; the "
                    "empty is UNKNOWN, not absence"
                    + (f". unresponsive_engines: {', '.join(unresponsive)}"
                       if unresponsive else "")
                )
        cache.put(provider_key, verdict, detail)
        logger.log(
            logging.INFO if verdict is LivenessVerdict.LIVE else logging.WARNING,
            "search.liveness.probe provider=%s verdict=%s detail=%s",
            provider_key, verdict.value, detail,
        )
        return verdict, detail


def apply_liveness(
    response: SearchResponse,
    verdict: LivenessVerdict,
    detail: str,
) -> SearchResponse:
    """Stamp a liveness verdict onto a zero-result response, in place.

    A DEAD / PROBE_FAILED verdict ALSO sets ``degraded`` — deliberately folding
    the broken plane into the EXISTING degradation channel rather than minting a
    parallel one, so every downstream consumer that already refuses to read a
    DEGRADED_EMPTY as absence (the tool's loud failure, the pack rules, the
    wire output) covers this case for free.
    """
    response.liveness = verdict
    response.liveness_detail = detail
    if verdict in (LivenessVerdict.DEAD, LivenessVerdict.PROBE_FAILED):
        response.degraded = True
        response.degraded_detail = (
            f"{response.degraded_detail} | {detail}"
            if response.degraded_detail else detail
        )
    return response


# ---------------------------------------------------------------------------
# The deferral contract
# ---------------------------------------------------------------------------

#: First deferral delay. Matched to the liveness TTL: retrying sooner than the
#: verdict's own freshness window cannot learn anything new.
DEFER_BASE_SECONDS = 300.0

#: Ceiling on the backoff. An hour is long enough to outlast a typical engine
#: ban window and short enough that a recovered plane is picked up the same day.
DEFER_MAX_SECONDS = 3600.0

#: Consecutive failures after which the advice carries ``escalate=True`` — the
#: signal that this is no longer a transient the caller can wait out.
DEFER_ESCALATE_AFTER = 5


@dataclass(frozen=True)
class DeferralAdvice:
    """"Try this again LATER" — bounded, backing off, per provider.

    THE CONSUMPTION CONTRACT (what a caller does with this)
    ------------------------------------------------------
    This is ADVICE on a tool result, not a queue. There is deliberately NO new
    table: the work item already exists and already has a durable home, and the
    caller already has a cadence.

    For the corpus researcher draining the standing-question backlog, consuming
    it is three rules:

      1. **Do not retry inside this run.** ``defer=True`` means the provider is
         refusing right now; an immediate retry worsens the ban and
         double-counts the query against engines already unhappy with us. This
         preserves the existing no-retry-on-degraded rule rather than
         weakening it.
      2. **Leave the work item OPEN and untouched.** The standing question
         stays an ``open_question`` hypotheses row (never silently closed —
         that rule is unchanged). No status flag is flipped, nothing is
         consumed. The analyst simply produces its finding from the substrate
         evidence it does have, or produces none this tick.
      3. **Let the next cadence tick pick it up**, no earlier than
         ``not_before``. The analyst's own cadence IS the requeue mechanism —
         the backlog is re-read from scratch every tick and priority-ordered, so
         an un-answered question is naturally re-attempted. ``retry_after_seconds``
         tells a caller whose cadence is FASTER than the backoff to skip the
         web leg on the intervening ticks.

    ``escalate=True`` means the ladder has run out: this needs an operator, not
    more waiting (engine set banned for hours, provider unregistered, endpoint
    wrong). A caller surfaces it rather than continuing to defer silently.

    A programmatic consumer reads it back off a ToolResult with
    :func:`deferral_from_tool_output`.
    """

    defer: bool
    #: ``search_degraded_no_results`` | ``search_liveness_unverified`` |
    #: ``search_provider_unresolved`` | ``search_unavailable``
    reason: str
    retry_after_seconds: float
    not_before: datetime
    consecutive_failures: int
    escalate: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "defer": self.defer,
            "reason": self.reason,
            "retry_after_seconds": self.retry_after_seconds,
            "not_before": self.not_before.isoformat(),
            "consecutive_failures": self.consecutive_failures,
            "escalate": self.escalate,
            "detail": self.detail,
            "guidance": (
                "Do NOT retry this search in this run — retrying against "
                "engines that are already refusing worsens the ban. Leave the "
                "work item OPEN; the next cadence tick re-attempts it no "
                "earlier than not_before."
                + (" ESCALATE: the backoff ladder is exhausted — this needs an "
                   "operator, not more waiting." if self.escalate else "")
            ),
        }


def compute_deferral(
    reason: str,
    *,
    provider_key: str,
    cache: SearchLivenessCache | None = None,
    detail: str = "",
    now: datetime | None = None,
) -> DeferralAdvice:
    """Advance the per-provider backoff ladder and return the advice.

    Exponential from :data:`DEFER_BASE_SECONDS`, capped at
    :data:`DEFER_MAX_SECONDS`, escalating after
    :data:`DEFER_ESCALATE_AFTER` consecutive failures. The streak resets on the
    first served search (:meth:`SearchLivenessCache.record_success`).
    """
    cache = cache if cache is not None else DEFAULT_LIVENESS_CACHE
    streak = cache.record_failure(provider_key, reason)
    delay = min(DEFER_BASE_SECONDS * (2 ** (streak - 1)), DEFER_MAX_SECONDS)
    moment = now or datetime.now(tz=timezone.utc)
    return DeferralAdvice(
        defer=True,
        reason=reason,
        retry_after_seconds=delay,
        not_before=moment + timedelta(seconds=delay),
        consecutive_failures=streak,
        escalate=streak >= DEFER_ESCALATE_AFTER,
        detail=detail,
    )


def deferral_from_tool_output(output: Any) -> DeferralAdvice | None:
    """Read a :class:`DeferralAdvice` back off a ``web_search`` tool output.

    The programmatic half of the consumption contract: a caller that drains a
    backlog checks this on every failed search rather than string-matching the
    error text. Returns ``None`` when the output carries no deferral (a served
    search, or a failure class that is NOT deferrable — a hard misconfiguration
    needs an operator, and waiting will not fix it).
    """
    if not isinstance(output, Mapping):
        return None
    block = output.get("deferral")
    if not isinstance(block, Mapping) or not block.get("defer"):
        return None
    raw_not_before = block.get("not_before")
    try:
        not_before = (
            datetime.fromisoformat(str(raw_not_before))
            if raw_not_before else datetime.now(tz=timezone.utc)
        )
    except ValueError:
        not_before = datetime.now(tz=timezone.utc)
    return DeferralAdvice(
        defer=True,
        reason=str(block.get("reason") or ""),
        retry_after_seconds=float(block.get("retry_after_seconds") or 0.0),
        not_before=not_before,
        consecutive_failures=int(block.get("consecutive_failures") or 0),
        escalate=bool(block.get("escalate")),
        detail=str(block.get("detail") or ""),
    )


__all__ = [
    "CONTROL_PROBE_LIMIT",
    "CONTROL_PROBE_QUERY",
    "CONTROL_PROBE_TTL_ENV",
    "CONTROL_PROBE_TTL_SECONDS",
    "DEFAULT_LIVENESS_CACHE",
    "DEFER_BASE_SECONDS",
    "DEFER_ESCALATE_AFTER",
    "DEFER_MAX_SECONDS",
    "DeferralAdvice",
    "SearchLivenessCache",
    "apply_liveness",
    "compute_deferral",
    "control_probe_ttl_seconds",
    "deferral_from_tool_output",
    "verify_engine_liveness",
]
