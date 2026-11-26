# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ingestion intake's vocabulary and limits — a LEAF (stdlib only).

One queue of ingestion requests (migration 0249). This module holds what
every side of it shares and nothing that does I/O, so the registry's slim
image (the read route), the agent tools, the follow step, the drift batch,
the archiver's route and the drain all read ONE vocabulary:

* the closed lists the table's CHECKs mirror (target kinds, sinks, reasons,
  budget classes, states, refusal reasons);
* :class:`IntakeLimits` — every admission and politeness number, each with a
  default and an environment override (read when the limits are built, so a
  test or an operator can change one without a code edit);
* the ADDRESS ADAPTERS — how an address is handed to a pipeline whose kind can
  fetch one address (``page`` — the production page pipeline —
  plus ``scraper`` and ``rss``): the pipeline's own config with the address
  substituted, so the run is that kind's own poll of one address. A kind
  without an adapter cannot take an address request (refused
  ``pipeline_not_address_capable`` at submit, never a quieter fallback). The
  ``scraper`` kind's only shipped impl, ``example_news``, is an EXAMPLE (it
  writes ``payload.body``, not ``raw_body``): page requests go to a ``page``
  pipeline, :data:`DEFAULT_PAGE_PIPELINE` by default.

Robots/terms posture is NOT here and NOT changed: an address request runs the
pipeline kind's own fetch, so the ``page`` and ``scraper`` kinds'
``respect_robots`` (default on) applies exactly as it does to a poll.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

__all__ = [
    "ADDRESS_ADAPTERS",
    "BUDGET_CLASSES",
    "DEFAULT_CLASS_DAILY",
    "DEFAULT_PAGE_PIPELINE",
    "IntakeLimits",
    "REASONS",
    "REASON_BUDGET_CLASS",
    "REFUSAL_REASONS",
    "SATISFIED_FROM",
    "SINKS",
    "STATES",
    "TARGET_KINDS",
    "TERMINAL_STATES",
    "address_config",
    "dedupe_key",
    "host_of",
]

TARGET_KINDS: tuple[str, ...] = ("address", "source_window", "stored_version")
#: ``document`` / ``observation`` = the pipeline's own family (the run writes
#: records exactly as a poll of it would); ``receipt`` = keep the fetched
#: version (and any drift row it produces), write no records.
SINKS: tuple[str, ...] = ("document", "observation", "receipt")
REASONS: tuple[str, ...] = (
    "scheduled", "followed_reference", "agent_request", "drift_recheck",
    "backfill", "operator_request", "user_request",
)
BUDGET_CLASSES: tuple[str, ...] = ("routine", "follow", "agent", "drift", "backfill", "operator")
#: The budget a request draws on when the submitter names none.
REASON_BUDGET_CLASS: dict[str, str] = {
    "scheduled": "routine",
    "followed_reference": "follow",
    "agent_request": "agent",
    "drift_recheck": "drift",
    "backfill": "backfill",
    "operator_request": "operator",
    "user_request": "operator",
}
STATES: tuple[str, ...] = ("queued", "claimed", "done", "refused", "failed", "expired")
TERMINAL_STATES: frozenset[str] = frozenset({"done", "refused", "failed", "expired"})
SATISFIED_FROM: tuple[str, ...] = ("stored_version", "stored_signal")

#: Every reason a request is refused at submit, in the order admission checks
#: them (the first that applies is recorded).
REFUSAL_REASONS: tuple[str, ...] = (
    "egress_refused",               # not http(s), userinfo, a private/loopback/internal literal host
    "unknown_pipeline",             # no head descriptor by that id
    "pipeline_not_runnable",        # not active/paused, or a push/subscribe source asked to re-poll
    "pipeline_not_address_capable", # its kind has no address adapter
    "sink_mismatch",                # the sink is neither the pipeline's family nor 'receipt'
    "unknown_version",              # stored_version: no such fetched version
    "depth_exceeded",               # depth above the global (or the follow policy's) limit
    "follow_not_allowed",           # the source's follow policy does not allow this reference
    "claim_link_refused",           # C-1: a link out of a CLAIM row to a host no ordinary source covers
    "duplicate_pending",            # the same target/pipeline/sink is already queued or claimed
    "fanout_exceeded",              # the parent already has its cap of children
    "host_budget",                  # the host's daily admissions are spent
    "requester_budget",             # the requester's daily admissions are spent
    "class_budget",                 # the budget class's global daily admissions are spent
)

#: Global daily admissions per budget class (UTC day). Reasons in
#: ``IntakeLimits`` docstring.
DEFAULT_CLASS_DAILY: dict[str, int] = {
    "routine": 2000, "follow": 1000, "agent": 200, "drift": 200, "backfill": 500, "operator": 500,
}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


def _class_daily_from_env() -> dict[str, int]:
    out = dict(DEFAULT_CLASS_DAILY)
    raw = os.environ.get("LEGBA_INTAKE_CLASS_DAILY", "").strip()
    for part in raw.split(","):
        name, _, value = part.partition(":")
        name = name.strip()
        if name in out:
            try:
                out[name] = max(0, int(value))
            except ValueError:
                pass
    return out


@dataclass(frozen=True)
class IntakeLimits:
    """Every admission and politeness number (env overrides in brackets).

    * ``freshness_hours`` (6) [LEGBA_INTAKE_FRESHNESS_HOURS] — an address
      fetched this recently is served from the stored copy, no fetch.
      Six hours is a quarter of a day: long enough that a burst of follows or
      agent asks for one page within a cycle costs one fetch, short enough
      that a page re-checked for drift the next day is fetched again.
    * ``max_depth`` (2) [LEGBA_INTAKE_MAX_DEPTH] — a reference of a reference,
      no further; a follow policy may only tighten it.
    * ``fanout_per_parent`` (10) [LEGBA_INTAKE_FANOUT_PER_PARENT] — children
      one request or one signal may spawn.
    * ``host_daily`` (200) [LEGBA_INTAKE_HOST_DAILY] — admissions per host per
      UTC day (the busiest live feed host writes ~150 signals a day).
    * ``host_concurrency`` (1) [LEGBA_INTAKE_HOST_CONCURRENCY] and
      ``host_min_interval_s`` (10) [LEGBA_INTAKE_HOST_MIN_INTERVAL_S] — at
      claim: one in flight per host, ten seconds between claims on a host
      (service.bund.de asks 30 s in its robots; the scraper kind's own
      ``rate_limit`` still applies inside a run).
    * ``requester_daily`` (100) [LEGBA_INTAKE_REQUESTER_DAILY] — admissions
      per requesting principal per UTC day.
    * ``class_daily`` [LEGBA_INTAKE_CLASS_DAILY, ``class:n,...``] — global
      admissions per budget class per UTC day (:data:`DEFAULT_CLASS_DAILY`).
    * ``expire_hours`` (24) [LEGBA_INTAKE_EXPIRE_HOURS] — a queued request not
      started within a day expires (an agent's ask is stale by then).
    * ``max_attempts`` (3) [LEGBA_INTAKE_MAX_ATTEMPTS], ``retry_base_s`` (60)
      [LEGBA_INTAKE_RETRY_BASE_S], ``retry_cap_s`` (3600) — the retry policy
      for a transient failure: back to queued after base * 2^(attempt-1),
      capped, until max_attempts, then ``failed``.
    * ``lease_s`` (600) [LEGBA_INTAKE_LEASE_S] — a claim's lease; longer
      than the poll ceiling (240 s) plus enrichment, so only a dead worker's
      claim ever expires.
    """

    freshness_hours: float = 6.0
    max_depth: int = 2
    fanout_per_parent: int = 10
    host_daily: int = 200
    host_concurrency: int = 1
    host_min_interval_s: float = 10.0
    requester_daily: int = 100
    class_daily: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_CLASS_DAILY))
    expire_hours: float = 24.0
    max_attempts: int = 3
    retry_base_s: float = 60.0
    retry_cap_s: float = 3600.0
    lease_s: float = 600.0

    @classmethod
    def from_env(cls) -> "IntakeLimits":
        return cls(
            freshness_hours=_env_float("LEGBA_INTAKE_FRESHNESS_HOURS", 6.0),
            max_depth=_env_int("LEGBA_INTAKE_MAX_DEPTH", 2),
            fanout_per_parent=_env_int("LEGBA_INTAKE_FANOUT_PER_PARENT", 10),
            host_daily=_env_int("LEGBA_INTAKE_HOST_DAILY", 200),
            host_concurrency=max(1, _env_int("LEGBA_INTAKE_HOST_CONCURRENCY", 1)),
            host_min_interval_s=_env_float("LEGBA_INTAKE_HOST_MIN_INTERVAL_S", 10.0),
            requester_daily=_env_int("LEGBA_INTAKE_REQUESTER_DAILY", 100),
            class_daily=_class_daily_from_env(),
            expire_hours=_env_float("LEGBA_INTAKE_EXPIRE_HOURS", 24.0),
            max_attempts=min(10, max(1, _env_int("LEGBA_INTAKE_MAX_ATTEMPTS", 3))),
            retry_base_s=_env_float("LEGBA_INTAKE_RETRY_BASE_S", 60.0),
            retry_cap_s=_env_float("LEGBA_INTAKE_RETRY_CAP_S", 3600.0),
            lease_s=_env_float("LEGBA_INTAKE_LEASE_S", 600.0),
        )

    def retry_delay_s(self, attempts: int) -> float:
        return min(self.retry_cap_s, self.retry_base_s * (2 ** max(0, attempts - 1)))


def host_of(url: str | None) -> str | None:
    """Lowercased host of ``url``, or ``None`` when there is none."""
    if not url:
        return None
    try:
        host = urlsplit(str(url).strip()).hostname
    except ValueError:
        return None
    return host.lower().rstrip(".") if host else None


# ---------------------------------------------------------------------------
# Address adapters: a pipeline kind that can fetch ONE address
# ---------------------------------------------------------------------------


def _scraper(config: Mapping[str, Any], address: str) -> dict[str, Any]:
    # The seed is the page; depth 0 = fetch it, discover nothing beyond it.
    return {**dict(config), "seed_urls": [address], "max_depth": 0}


def _rss(config: Mapping[str, Any], address: str) -> dict[str, Any]:
    return {**dict(config), "url": address}


def _page(config: Mapping[str, Any], address: str) -> dict[str, Any]:
    # The page pipeline's descriptor carries no url; the request supplies it.
    return {**dict(config), "url": address}


ADDRESS_ADAPTERS: dict[str, Callable[[Mapping[str, Any], str], dict[str, Any]]] = {
    "page": _page,
    "scraper": _scraper,
    "rss": _rss,
}

#: The instance's default page pipeline (``descriptors/source_page_pipeline_
#: default.yaml``) — what an operator, an agent or a follow policy names when
#: it wants a page fetched and landed as a document.
DEFAULT_PAGE_PIPELINE = "source.page.default"

#: Pipeline states an ADDRESS request may run under (I-2). A ``configured``
#: page pipeline is validated and has no actor and no schedule — exactly the
#: posture of a pipeline that exists only to be run by requests (an ``active``
#: poll source must declare a cadence, so a schedule-less pipeline can never
#: reach ``paused``). A source-window re-poll still needs ``active``/``paused``.
ADDRESS_RUNNABLE_STATES: frozenset[str] = frozenset({"active", "paused", "configured"})
WINDOW_RUNNABLE_STATES: frozenset[str] = frozenset({"active", "paused"})


def address_config(kind: str, config: Mapping[str, Any], address: str) -> dict[str, Any] | None:
    """The pipeline's config with ``address`` substituted, or ``None`` when
    the kind cannot take an address."""
    adapter = ADDRESS_ADAPTERS.get(kind)
    return None if adapter is None else adapter(config or {}, address)


def dedupe_key(
    *, target_kind: str, pipeline_source_id: str, sink: str,
    address_canonical: str | None = None, source_id: str | None = None,
    window_start: datetime | None = None, version_id: int | None = None,
) -> str:
    """The pending-request identity (migration 0249's partial unique index)."""
    if target_kind == "address":
        target = address_canonical or ""
    elif target_kind == "source_window":
        target = f"{source_id}@{window_start.isoformat() if window_start else ''}"
    else:
        target = f"v{version_id}"
    return f"{target_kind}|{target}|{pipeline_source_id}|{sink}"
