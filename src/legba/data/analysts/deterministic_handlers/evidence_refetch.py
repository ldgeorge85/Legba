# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``evidence_refetch`` sub-handler — re-fetch the textless backlog on
fetch-policy hosts (W11-F138).

THE DEFECT it drains. Every Times of Israel and Jerusalem Post signal of the
last 30 days was ingested with a summary and no ``payload.text``: both hosts
challenge the evidence archiver on every fetch (http_403), so no body ever
landed. W11-F138's per-host fetch policy
(:mod:`legba.data.sources._fetch_policy`) now gives those hosts the
impersonated transport, but the ~2,454 ALREADY-INGESTED textless signals sit
outside the archiver's selection predicate — most are not cited by verified
findings, and none would be picked anyway until a citation arrives. This
sweep is the backlog drain: per tick it re-runs THE ARCHIVER'S OWN fetch leg
on the newest ``refetch_max_per_tick`` (default 50) textless signals whose
canonical host carries a non-default policy — the registry's
``impersonate=on`` set — inside ``refetch_window_days`` (default 30),
processed OLDEST FIRST within that batch.

"Re-runs the archiver fetch" is literal: the row is handed to
:func:`evidence_archiver._archive_one`, so the licence gates, the
per-host-policy two-leg client, the CAS write, the challenge/boilerplate
detectors, the sidecar upsert, the signal stamp and the per-feed counters
are the SAME code path a cited archival takes. A re-fetched body lands as
``payload.archived_text`` — the corpus's ``best_body`` upgrade — which is
what a consult reaches for; this sweep deliberately does not rewrite the
ingest-time ``payload.text``.

Selection notes:

  * ``payload.text`` absent or empty is the defect signature. Rows already
    carrying ``payload.archived_text`` are skipped — there is nothing left
    to fix on them.
  * ``object_ref``/sidecar status are NOT gates: a ``blocked_challenge`` or
    ``failed`` row is exactly the population this exists for, and the
    sidecar's ``attempts`` counter keeps its meaning across the re-fetch.
  * The host filter runs twice on purpose: a coarse ILIKE prefilter keeps
    the SQL bounded, then :func:`fetch_policy_for` on the parsed host makes
    the authoritative call (a URL that merely names a policy host in its
    path cannot slip through).
  * The newest N are selected (DESC), then drained ASC — a steadily-
    refilled backlog still makes forward progress on its oldest members.

Ships DRAFT — do not run it against anything; the VM applies it. Idempotent:
a successful re-fetch stamps ``object_ref`` + ``archived_text`` and the row
leaves the predicate; a failure is attempt-capped in the sidecar.

Output ``data`` keys are the archiver's own vocabulary (``_zero_counters``
is inherited), plus ``refetched`` — rows handed to the archiver leg this
run.
"""
from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Any, Mapping

from ...sources._fetch_policy import IMPERSONATE_ON, fetch_policy_for, policy_hosts
from ....runtime.analyst_method import AnalystMethodResult
from ...provenance.models import FindingPayload
from . import evidence_archiver

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "evidence_refetch"

#: Descriptor-declared options (handler_options catalog): the per-tick
#: re-fetch budget and the look-back window.
_DEFAULT_MAX_PER_TICK = 50
_DEFAULT_WINDOW_DAYS = 30

#: Candidate rows: textless, canonical-url-bearing, inside the window, on a
#: host the ILIKE prefilter can see. ORDER DESC = the newest N; the sweep
#: drains the batch oldest-first (reversed below).
_SELECT_CANDIDATES_SQL = """
    SELECT s.id, s.source_id, s.canonical_url, s.media_ref, s.payload,
           s.raw_provenance, s.retention_class, s.retrieval_origin,
           COALESCE(ea.attempts, 0) AS prior_attempts
      FROM signals s
      LEFT JOIN evidence_archive ea ON ea.signal_id = s.id
     WHERE s.canonical_url IS NOT NULL
       AND s.canonical_url <> ''
       AND (s.payload->>'text' IS NULL OR s.payload->>'text' = '')
       AND s.payload->>'archived_text' IS NULL
       AND s.fetched_at > now() - make_interval(days => $1)
       AND s.canonical_url ILIKE ANY ($2::text[])
     ORDER BY s.fetched_at DESC
     LIMIT $3
"""

#: The archiver leg the sweep delegates to, bound at module level so a test
#: patches the name it actually calls (the neighbouring-suite idiom).
_archive_one = evidence_archiver._archive_one


def _policy_refetched(url: str) -> bool:
    """Whether ``url``'s host carries a non-default impersonation policy —
    the "hosts whose policy changed" set, authoritative (the SQL prefilter is
    deliberately coarser)."""
    return fetch_policy_for(url).impersonate == IMPERSONATE_ON


def _zero_counters() -> dict[str, Any]:
    """The archiver's counter vocabulary (incl. ``per_source``), plus the
    sweep's own ``refetched`` attempt count."""
    counters = evidence_archiver._zero_counters()
    counters["refetched"] = 0
    return counters


async def _sweep(pool: Any, options: Mapping[str, Any]) -> dict[str, Any]:
    """One tick: select the textless policy-host backlog, re-fetch a batch."""
    counters = _zero_counters()
    root = evidence_archiver._archive_root()
    if root is None:
        counters["skipped_no_root"] = 1
        return counters

    # The two knobs this sweep owns; every other fetch shaping rides the
    # archiver's own module defaults — an option this handler does not
    # declare in the catalog is an option it must not read (the X-1 rule).
    max_per_tick = max(1, int(options.get("refetch_max_per_tick", _DEFAULT_MAX_PER_TICK)))
    window_days = max(1, int(options.get("refetch_window_days", _DEFAULT_WINDOW_DAYS)))

    patterns = [f"%{host}%" for host in policy_hosts()]
    if not patterns:
        return counters
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            _SELECT_CANDIDATES_SQL, window_days, patterns, max_per_tick,
        )
    rows = [r for r in rows if _policy_refetched(str(r["canonical_url"]))]
    if not rows:
        return counters
    counters["examined"] = len(rows)

    # The newest N came back DESC; drain them OLDEST FIRST — the stated
    # order, and the one that keeps a steadily-refilled backlog moving.
    rows = list(reversed(rows))

    politeness = evidence_archiver._HostPoliteness(
        evidence_archiver._DEFAULT_PER_HOST_DELAY_S,
    )
    async with AsyncExitStack() as stack:
        clients = evidence_archiver._FetchClients(
            stack, timeout=evidence_archiver._DEFAULT_TIMEOUT_S,
        )
        for row in rows:
            counters["refetched"] += 1
            await _archive_one(
                pool, clients, row,
                root=root, politeness=politeness, counters=counters,
                forbid=evidence_archiver.FORBID_RETENTION_CLASSES,
                max_bytes=evidence_archiver._DEFAULT_MAX_OBJECT_BYTES,
                max_text_chars=evidence_archiver._DEFAULT_MAX_TEXT_CHARS,
                max_attempts=evidence_archiver._DEFAULT_MAX_ATTEMPTS,
                web_origin_fail_closed=(
                    not evidence_archiver.WEB_ORIGIN_UNKNOWN_LICENSE_ARCHIVES
                ),
                unknown_fail_closed=(
                    not evidence_archiver.UNKNOWN_LICENSE_ARCHIVES
                ),
            )
    return counters


def _build_finding(counters: Mapping[str, Any]) -> FindingPayload:
    """The cadence receipt — same receipt discipline as the archiver's."""
    title = (
        f"Evidence re-fetch: re-fetched {counters.get('refetched', 0)} of "
        f"{counters.get('examined', 0)} textless policy-host signal(s), "
        f"{counters.get('archived', 0)} archived, "
        f"{counters.get('fetch_failed', 0)} failed"
    )
    if counters.get("blocked_by_challenge", 0):
        title += f", {counters['blocked_by_challenge']} challenge-blocked"
    body = "\n".join(f"{k}={v}" for k, v in counters.items())
    tags = ["deterministic", SUB_HANDLER_NAME]
    if counters.get("archived", 0):
        tags.append("archived")
    if counters.get("blocked_by_challenge", 0):
        tags.append("blocked_by_challenge")
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={"sub_handler": SUB_HANDLER_NAME, **dict(counters)},
    )


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    Sweeps the substrate directly via ``deps.pg_pool`` (the ``inputs`` slice
    is ignored — the unit of work is "the next bounded batch of textless
    policy-host signals"). ``deps is None`` (unit-test path) yields a zeroed
    run. Usage is always zeroed (deterministic kind, no LLM)."""
    counters = _zero_counters()
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None:
        try:
            counters = await _sweep(pool, options)
        except Exception as exc:
            logger.warning("evidence_refetch.failed err=%s", exc)

    return AnalystMethodResult(
        finding=_build_finding(counters),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle", "SUB_HANDLER_NAME"]
