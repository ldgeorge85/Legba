# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SEARCH HEALTH — one rule for "did this search answer?", read by three callers.

THE DEFECT THIS CLOSES (W11-F15, verified 10-08). Every ``searched_found_nothing``
absence on the us / cn / ua desks (70 of 70 sampled through ``/v3/absence``)
carried a proof that said the search plane was DEGRADED. By docs/ANALYSIS.md's
own definitions those are ``search_failed``: "we could not look properly" and
"we looked and saw nothing" are different facts, and the first was being shown
as the second. The kind is computed at READ time (``absence_api.audit_item``),
so the fix is the RULE — applied here — and every live row flips on the next
read with no retrofit.

ONE RULE, THREE READERS
-----------------------
:func:`read_search_health` is the only place the platform decides whether a
recorded search was degraded. The absence route types a grade with it, the
search-health route (``/v3/system/search-health``) aggregates it, and the
watchdog's ``search_degraded`` page counts it. Three implementations of the
same judgement would be three answers.

The rule, over the structured HEALTH BLOCK the ``web_search`` tool stamps on
every result (:func:`build_health_block`, persisted on ``external_grades.
search_health`` by migration 0235):

  * DEGRADED when the number of unresponsive engines is at or above
    :data:`DEGRADED_MIN_UNRESPONSIVE_ENV` (default 1 — any engine lost), or
  * DEGRADED when the search returned ZERO results and any engine errored (or
    the provider admitted partial service with no engine named).

A row written before the health block existed carries only the provider's
boolean ``search_degraded``. Its engine count is UNKNOWN, so the flag is read
as DEGRADED — the conservative direction: an unknown search is never promoted
to "we looked and found nothing".

THE THRESHOLD IS A TUNABLE, AND ITS DEFAULT IS 1. A search that lost any engine
could have lost the result that decided the claim, which is the base layer's own
doctrine (``SearchResponse.supports_absence_claim`` is False for every degraded
response, hits or not). Raising it is an operator act (docs/TUNABLES.md §1)
once the engine set is cleaned up (SW-3): a permanently dead engine that is
dropped from SearXNG stops counting at all, which is the honest fix; raising
the threshold to step around it is the other one, and it is written down.

Registry-slim: stdlib only. Imported by ``absence_api``, ``search_health_api``,
``agency.web_tools`` and ``runtime.search_degraded_watch``.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Sequence

#: The health block's own version, stamped on every block so a reader can tell
#: which shape it holds.
HEALTH_BLOCK_VERSION = "search-health/1"

#: THE TUNABLE (docs/TUNABLES.md §1): unresponsive engines at or above which a
#: search with results is read as DEGRADED. Read on every call, so a container
#: recreate with a new value applies without a code change.
DEGRADED_MIN_UNRESPONSIVE_ENV = "LEGBA_SEARCH_DEGRADED_MIN_UNRESPONSIVE"
DEFAULT_DEGRADED_MIN_UNRESPONSIVE = 1

#: What "engines asked" means: SearXNG does not report the engine set it fanned
#: out to, only the engines whose hits survived the merge and the ones that
#: failed. The union of the two is a LOWER BOUND, and the block says so.
#: Kept short on purpose: the block rides on every tool output a planner reads.
ENGINES_ASKED_BASIS = "observed: answered ∪ errored (fan-out set not reported)"


def degraded_min_unresponsive() -> int:
    """The live threshold. A missing, non-integer or < 1 value is the default:
    a threshold of 0 would call every search degraded, which is a different
    instrument, not a setting of this one."""
    raw = os.getenv(DEGRADED_MIN_UNRESPONSIVE_ENV, "").strip()
    if not raw:
        return DEFAULT_DEGRADED_MIN_UNRESPONSIVE
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_DEGRADED_MIN_UNRESPONSIVE
    return value if value >= 1 else DEFAULT_DEGRADED_MIN_UNRESPONSIVE


# ---------------------------------------------------------------------------
# The block — what the web_search tool stamps
# ---------------------------------------------------------------------------


def split_engine_error(text: Any) -> tuple[str, str]:
    """``"reuters: HTTP error"`` → ``("reuters", "HTTP error")``.

    The provider layer normalises SearXNG's ``[engine, reason]`` pairs to that
    string (``stack.search.base.coerce_engine_names``). A name with no reason
    keeps an empty reason rather than inventing one.
    """
    name, _, reason = str(text or "").partition(":")
    return name.strip(), reason.strip()


def _answered_engines(results: Iterable[Mapping[str, Any]]) -> list[str]:
    names: set[str] = set()
    for item in results:
        if not isinstance(item, Mapping):
            continue
        engine = item.get("engine")
        if engine:
            names.add(str(engine))
        raw = item.get("raw")
        merged = raw.get("engines") if isinstance(raw, Mapping) else None
        if isinstance(merged, (list, tuple)):
            names.update(str(e) for e in merged if e)
    return sorted(names)


def build_health_block(
    *,
    provider: str,
    status: str,
    provider_degraded: bool,
    results: Sequence[Mapping[str, Any]] = (),
    unresponsive: Sequence[str] = (),
    liveness: str = "",
    elapsed_ms: Optional[int] = None,
    error_class: str = "",
) -> dict[str, Any]:
    """The structured health of ONE search, as the tool saw it.

    ``results`` are dicts carrying ``engine`` and optionally ``raw.engines``
    (SearXNG's merged-engine list). ``result_count`` is ``None`` when the
    provider never answered (``error_class`` set) — no count was measured, and
    a ``0`` there would read as "answered, found nothing".
    """
    errored = []
    for text in unresponsive:
        name, reason = split_engine_error(text)
        if name:
            errored.append({"engine": name, "error": reason})
    answered = _answered_engines(results) if not error_class else []
    asked = sorted(set(answered) | {e["engine"] for e in errored})
    return {
        "version": HEALTH_BLOCK_VERSION,
        "provider": provider,
        "status": status,
        "provider_degraded": bool(provider_degraded),
        "result_count": None if error_class else len(results),
        "engines_asked": asked,
        "engines_asked_basis": ENGINES_ASKED_BASIS,
        "engines_answered": answered,
        "engines_errored": errored,
        "n_engines_asked": len(asked),
        "n_engines_answered": len(answered),
        "n_engines_errored": len(errored),
        "liveness": liveness,
        "elapsed_ms": elapsed_ms,
        **({"error_class": error_class} if error_class else {}),
    }


# ---------------------------------------------------------------------------
# The rule — what every reader asks
# ---------------------------------------------------------------------------


def _as_mapping(value: Any) -> dict[str, Any]:
    """A jsonb column arrives as a dict (a codec) or a JSON string (none)."""
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (str, bytes)) and value:
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


@dataclass(frozen=True)
class SearchHealth:
    """One recorded search, judged.

    ``recorded`` is False for a row that carries no health block (written
    before it existed): ``degraded`` is then the provider's own flag, and the
    engine lists are empty because nobody recorded them — not because no engine
    failed.
    """

    degraded: bool
    recorded: bool
    threshold: int
    errored: tuple[tuple[str, str], ...] = ()
    answered: tuple[str, ...] = ()
    result_count: Optional[int] = None
    provider_flag: bool = False
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def partial(self) -> bool:
        """Engines failed but the rule did not call it degraded."""
        return bool(self.errored) and not self.degraded

    def _errored_text(self) -> str:
        return "; ".join(
            f"{name}: {why}" if why else name for name, why in self.errored
        )

    def describe(self) -> str:
        """The proof fragment. ``DEGRADED`` appears in it if and only if the
        rule says degraded — the word a reader audits is the rule's verdict."""
        answered = (
            f"engines answered: {', '.join(self.answered)}" if self.answered
            else "engines answered: none recorded"
        )
        if not self.recorded:
            if self.degraded:
                return (
                    "search plane DEGRADED — the provider flagged partial "
                    "service; this row predates the per-engine health record, "
                    "so which engines failed is not recorded"
                )
            return "per-engine health not recorded on this row"
        if self.degraded:
            why = " and ".join(self.reasons) or "degraded"
            lost = self._errored_text() or "no engine named"
            return (
                f"search plane DEGRADED ({why}): unresponsive — {lost}; "
                f"{answered}"
            )
        if self.partial:
            return (
                f"partial service below the degraded threshold of "
                f"{self.threshold}: unresponsive — {self._errored_text()}; "
                f"{answered}"
            )
        return f"no engine unresponsive; {answered}"


def read_search_health(
    *,
    search_degraded: Any,
    health: Any,
    threshold: Optional[int] = None,
) -> SearchHealth:
    """THE rule (see the module banner). Pure; never raises."""
    n = threshold if threshold is not None else degraded_min_unresponsive()
    flag = bool(search_degraded)
    block = _as_mapping(health)
    if not block or "engines_errored" not in block:
        return SearchHealth(
            degraded=flag, recorded=False, threshold=n, provider_flag=flag,
            reasons=(("provider flag only",) if flag else ()),
        )
    errored: list[tuple[str, str]] = []
    for item in block.get("engines_errored") or []:
        if isinstance(item, Mapping):
            name = str(item.get("engine") or "").strip()
            if name:
                errored.append((name, str(item.get("error") or "").strip()))
        elif item:
            errored.append(split_engine_error(item))
    answered = tuple(str(e) for e in (block.get("engines_answered") or []) if e)
    count_raw = block.get("result_count")
    count = int(count_raw) if isinstance(count_raw, (int, float)) and not isinstance(
        count_raw, bool) else None
    provider_flag = bool(block.get("provider_degraded")) or flag
    reasons: list[str] = []
    if len(errored) >= n:
        reasons.append(f"{len(errored)} engine(s) unresponsive ≥ threshold {n}")
    if count == 0 and (errored or provider_flag):
        reasons.append("zero results with an engine error")
    return SearchHealth(
        degraded=bool(reasons),
        recorded=True,
        threshold=n,
        errored=tuple(errored),
        answered=answered,
        result_count=count,
        provider_flag=provider_flag,
        reasons=tuple(reasons),
    )


# ---------------------------------------------------------------------------
# The measure — the route's 7-day read and the watchdog's 24-hour page
# ---------------------------------------------------------------------------

#: The audit's PRIMARY grade rows inside a window, one row per search that
#: decided a claim (the second rater reuses the primary's envelope, so its row
#: would double-count the same search). ``idx_external_grades_window
#: (graded_at DESC, …)`` drives the range; the live EXPLAIN is in the lane
#: report.
SEARCH_HEALTH_WINDOW_SQL = """
SELECT search_provider, verdict, search_degraded, search_health, graded_at
  FROM public.external_grades
 WHERE graded_at > $1
   AND rater_role = 'primary'
   AND search_provider IS NOT NULL
"""

NOT_FOUND = "NOT_FOUND"


def _share(part: int, whole: int) -> Optional[float]:
    """A share, or ``None`` when there is no denominator — never a 0 standing
    in for "nothing was measured"."""
    return round(part / whole, 4) if whole else None


def summarize(
    rows: Iterable[Mapping[str, Any]], *, threshold: Optional[int] = None,
) -> dict[str, Any]:
    """Per-provider and per-engine degraded shares over ``rows``.

    ``providers[*]``: every primary grade row with its provider, the share the
    rule reads as degraded, and the same share over NOT_FOUND rows alone (the
    rows the absence route would otherwise type as "searched, found nothing").
    ``engines[*]``: over rows that CARRY a health block only — an engine's
    error share has no denominator on a row that never recorded engines, and
    those rows are counted separately (``rows_without_engine_record``).
    """
    n = threshold if threshold is not None else degraded_min_unresponsive()
    providers: dict[str, dict[str, Any]] = {}
    engines: dict[str, dict[str, Any]] = {}
    recorded_rows = 0
    unrecorded_rows = 0
    newest: Optional[datetime] = None
    for row in rows:
        provider = str(row.get("search_provider") or "")
        if not provider:
            continue
        health = read_search_health(
            search_degraded=row.get("search_degraded"),
            health=row.get("search_health"),
            threshold=n,
        )
        at = row.get("graded_at")
        if isinstance(at, datetime) and (newest is None or at > newest):
            newest = at
        p = providers.setdefault(provider, {
            "provider": provider, "searches": 0, "degraded": 0,
            "not_found": 0, "not_found_degraded": 0,
        })
        p["searches"] += 1
        p["degraded"] += int(health.degraded)
        if row.get("verdict") == NOT_FOUND:
            p["not_found"] += 1
            p["not_found_degraded"] += int(health.degraded)
        if not health.recorded:
            unrecorded_rows += 1
            continue
        recorded_rows += 1
        for name, why in health.errored:
            e = engines.setdefault(name, {
                "engine": name, "searches": 0, "errored": 0, "answered": 0,
                "errors": {},
            })
            e["errored"] += 1
            key = why or "unstated"
            e["errors"][key] = e["errors"].get(key, 0) + 1
        for name in health.answered:
            e = engines.setdefault(name, {
                "engine": name, "searches": 0, "errored": 0, "answered": 0,
                "errors": {},
            })
            e["answered"] += 1
    for e in engines.values():
        e["searches"] = recorded_rows
        e["error_share"] = _share(e["errored"], recorded_rows)
    for p in providers.values():
        p["degraded_share"] = _share(p["degraded"], p["searches"])
        p["not_found_degraded_share"] = _share(
            p["not_found_degraded"], p["not_found"]
        )
    return {
        "threshold": n,
        "as_of": newest.isoformat() if newest else None,
        "rows_with_engine_record": recorded_rows,
        "rows_without_engine_record": unrecorded_rows,
        "providers": sorted(providers.values(), key=lambda p: p["provider"]),
        "engines": sorted(
            engines.values(), key=lambda e: (-e["errored"], e["engine"])
        ),
    }


__all__ = [
    "DEFAULT_DEGRADED_MIN_UNRESPONSIVE",
    "DEGRADED_MIN_UNRESPONSIVE_ENV",
    "ENGINES_ASKED_BASIS",
    "HEALTH_BLOCK_VERSION",
    "SEARCH_HEALTH_WINDOW_SQL",
    "SearchHealth",
    "build_health_block",
    "degraded_min_unresponsive",
    "read_search_health",
    "split_engine_error",
    "summarize",
]
