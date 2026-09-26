# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The RESEARCH SIGNAL — builder, licence ledger, ceiling, novelty, CAS write.

R-A of the research program (``planning/RESEARCH_PROGRAM_SPEC_2026-09-05.md``
§1). This module is the *builder*; ``analysts/agency/research_tools.py`` is the
*handler* that calls it. They are separate on purpose: everything here is
unit-testable without a pack, a governor or a search provider.

THE ONE IDEA. A research signal is an ORDINARY ``signals`` row. No new table,
no new modality, no new ``schema_uri``. It is distinguished by exactly one
column — ``retrieval_origin = 'web_search:<component_id>'`` — the seam
migration 0112 built and that, until this module, NOTHING had ever written
(229,893 live rows, 100% NULL). The moment it is written, six already-built
things light up with no further code: the archiver's ``web_origin_examined``
counter, the R-3b fail-closed licence gate, the ``skipped_license_unreviewed``
sidecar status, the OpenSearch ``retrieval_origin`` keyword facet, the
finding-title clause and the ``web_origin_license_unreviewed`` tag.

WHAT KEEPS IT HONEST — four properties, each obtained by SUBTRACTION rather
than by assertion, and each pinned by a test:

1. **The source is UNREGISTERED** (:func:`research_source_id`). ``signals``
   carries no foreign keys at all, so ``source.research.<provider>`` is a
   legal value with no ``source_descriptors`` row behind it. The salience
   scorer resolves ``source_class`` by JOINING that table
   (``signal_salience.py:612``); no row ⇒ NULL ⇒ ``_authority_for(None)`` ⇒
   ``"unknown"`` ⇒ ``AUTHORITY_RANK`` **0**. The honest authority floor, free.
   A REGISTERED descriptor would have to declare ``scope.source_class``, whose
   schema default is ``reporting`` = rank 3 — a lie about an unreviewed domain
   set. One id per PROVIDER (never per query) is also what preserves the free
   dilution bound: ``_diversify_by_source`` admits at most
   ``LEGBA_GLOBAL_SLICE_PER_SOURCE_CAP`` rows per ``source_id``.
2. **The ceiling is the arithmetic, not a rule** (:func:`research_salience`).
   ``magnitude`` is capped at :data:`RESEARCH_MAGNITUDE_CAP`, which IS
   ``signal_salience.MASS_FLOOR``. ``cited_mass = Σ max(0, m − MASS_FLOOR)``,
   so an uncorroborated research signal contributes EXACTLY ZERO cited_mass:
   it can be read and cited, and it can never crown the Morning Read. Because
   the salience is stamped AT WRITE, the LLM scorer never sees the row
   (``_SELECT_BATCH_SQL`` selects ``WHERE s.salience IS NULL``) — so no model
   can lift the ceiling by accident.
3. **It never auto-grounds.** Fact extraction is a per-source PIPELINE FILTER
   a source descriptor binds; the research source has no descriptor, so it
   binds none, so no ``facts`` row can ever derive from a research signal
   until R-D's corroboration test lifts the ceiling.
4. **Two archive depths, and the split is the licensing ledger's own rule**
   (:func:`depth_for_license`). Stamping ``retrieval_origin`` makes archiving
   IMPOSSIBLE by default — ``evidence_archiver.WEB_ORIGIN_UNKNOWN_LICENSE_ARCHIVES``
   is ``False``, and not one registered source carries a ``license_class``. So
   a licence-CLEARED host gets full text + content-addressed bytes; an
   UNREVIEWED host gets teaser depth (title/url/snippet, no bytes); a
   FORBIDDEN host is never fetched at all. That is
   ``SOURCE_LICENSING_LEDGER_2026-07-10.md:431-435`` verbatim. This module
   ships the ledger MACHINERY and **zero licence verdicts** — classifying a
   host is a legal reading and an operator item (spec §8 F-2), never a
   model's guess.

WHAT THIS MODULE MUST NOT DO, restated because each is a way to launder:
weaken the archiver's fail-closed gate; pass ``web_origin_license_gate:
"inherit"``; put extracted full text in ``payload.text`` at teaser depth;
invent a licence verdict; register a ``source_descriptors`` row for the
synthetic source; or publish a research signal onto ``legba_signals`` (the
coalescer's severity/accumulation gates must never be woken by research).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from .archive import cas_object_ref  # noqa: F401  (re-exported address format)
from .filters.source_credibility import extract_lookup_hosts
from .nats import SIGNALS_EXCLUDE_BACKFILL_SQL
from .provenance.origin import origin_class_for
from .research_flag import RESEARCH_SLICE_EXCLUSION_SQL, research_reaches_desks
from .retrieval_origin import web_search_origin

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Identity — the deterministic row id and the synthetic source
# ---------------------------------------------------------------------------

#: uuid5 namespace for research signal ids. A FIXED, in-tree constant: the id
#: must be reproducible from ``(provider, canonical_url, run_id)`` alone so a
#: GATHER round that re-fetches the same URL inside one run lands ONE row under
#: ``ON CONFLICT (id) DO NOTHING`` — idempotence by construction, not by a
#: pre-SELECT that races itself.
RESEARCH_NS = UUID("6b6f2f8e-6a4b-5f1e-9d3a-9f1c2b7e4a10")

#: The synthetic, DELIBERATELY UNREGISTERED source id prefix. See the module
#: docstring, property 1.
RESEARCH_SOURCE_PREFIX = "source.research."

#: ``produced_by_kind`` for a research row — the honest statement that this row
#: was not polled. The live column has NO CHECK constraint and the live
#: vocabulary is ``source`` (229,901 rows) plus ``job`` in code.
#:
#: NOTE, and it is a real one: ``data/sources/_contract.py:Signal`` types this
#: field as ``Literal["source","job","analyst","deterministic","system"]``, so a
#: research row canNOT round-trip through the source-handler contract model.
#: Nothing in ``src/`` parses a ``signals`` ROW back into ``Signal`` (only
#: source handlers CONSTRUCT one on the ingest path), which is why this module
#: writes its own INSERT rather than borrowing ``write_canonical_signal`` —
#: that writer takes a ``Signal``, and ``Signal`` has neither a
#: ``retrieval_origin`` nor a ``salience`` field and forbids extras.
RESEARCH_PRODUCED_BY_KIND = "research"

#: ``payload.research.schema``. Bump on ANY change to the provenance, ceiling
#: or novelty blocks — the same discipline as ``assembly.v1`` / ``absence.v4``.
RESEARCH_PAYLOAD_SCHEMA = "research_evidence.v1"

#: ``payload.research.novelty.version`` (spec §4.1). v1 matches on EXACT
#: canonical_url or EXACT content_hash, and therefore OVER-REPORTS novelty: the
#: same story from a different outlet counts as novel. R-D publishes this beside
#: the ``near_dup_rate`` the existing dedup fold computes a tick later and says
#: the truth is between them. Stated, never hidden.
RESEARCH_NOVELTY_VERSION = "novelty.v1"

#: ``tags`` every research row carries, before the dispatching target's own.
RESEARCH_TAG = "research"

#: The stack family every search provider component id is namespaced under.
_SEARCH_FAMILY = "search"


def research_source_id(component_id: str) -> str:
    """``search.searxng.local`` → ``source.research.searxng_local``.

    One id per PROVIDER COMPONENT. Per-QUERY ids would take
    ``count(distinct source_id)`` from 117 to thousands and would dissolve the
    one poisoning bound the slice already has for free (see the module
    docstring, property 1).

    The stack family prefix (``search.``) is dropped: it is already implied by
    ``source.research.``, and carrying it would render as
    ``source.research.search_searxng_local`` on every read surface.
    """
    raw = str(component_id or "").strip().lower()
    if raw.startswith(f"{_SEARCH_FAMILY}."):
        raw = raw[len(_SEARCH_FAMILY) + 1:]
    slug = "".join(
        ch if (ch.isalnum() or ch == "_") else "_" for ch in raw
    ).strip("_")
    return f"{RESEARCH_SOURCE_PREFIX}{slug or 'unknown'}"


def is_research_source_id(source_id: Any) -> bool:
    """True for a synthetic research source id (never for a registered one)."""
    return isinstance(source_id, str) and source_id.startswith(RESEARCH_SOURCE_PREFIX)


def research_source_display_name(source_id: Any) -> str | None:
    """A human label for a synthetic source id, or ``None`` if it isn't one.

    THE OBLIGATION THE UNREGISTERED ID CREATES (spec §1.2, an R-A acceptance
    item). Every read surface that renders a ``source_id`` joins
    ``source_descriptors`` for a name; a research row has no such row, so
    without this it renders as a raw id and looks like a bug to the next
    reader. This is that two-line answer, in ONE place, so the surfaces cannot
    each invent their own.
    """
    if not is_research_source_id(source_id):
        return None
    provider = str(source_id)[len(RESEARCH_SOURCE_PREFIX):].replace("_", ".")
    return f"Web research ({provider})"


def research_signal_id(
    *, provider_component_id: str, canonical_url: str, run_id: Any,
) -> UUID:
    """The DETERMINISTIC row id: ``uuid5(NS, provider ‖ url ‖ run_id)``.

    Scoped to the RUN on purpose. Within one run the same URL is one row (a
    re-fetch in a later GATHER round collapses under ``ON CONFLICT``); across
    runs the same URL fetched a week later is a genuinely new observation with
    its own ``fetched_at``, and the dedup plane — not this id — is what folds
    the two.
    """
    key = "\x1f".join(
        (
            str(provider_component_id or ""),
            str(canonical_url or ""),
            str(run_id or ""),
        )
    )
    return uuid5(RESEARCH_NS, key)


# ---------------------------------------------------------------------------
# The credibility ceiling (spec §1.4)
# ---------------------------------------------------------------------------

def _mass_floor() -> float:
    """``signal_salience.MASS_FLOOR``, imported lazily to keep this module's
    import graph free of the analyst plane for the DB-free unit tests."""
    from .analysts.signal_salience import MASS_FLOOR

    return float(MASS_FLOOR)


#: The ceiling. NOT a new constant — it is ``signal_salience.MASS_FLOOR``, and
#: the coupling is the point: ``cited_mass = Σ max(0, m − MASS_FLOOR)``, so a
#: capped row contributes EXACTLY 0.0000. If MASS_FLOOR ever moves, the
#: ceiling's meaning must move with it; a test pins the two equal WITH that
#: reason in its assertion message.
RESEARCH_MAGNITUDE_CAP: float = _mass_floor()

#: The second half of the ceiling. ``facts.source_credibility`` is
#: ``GREATEST(existing, incoming)`` over backing signals, so a research signal
#: can never raise a fact's credibility above this.
RESEARCH_CREDIBILITY_CAP: float = RESEARCH_MAGNITUDE_CAP

#: Why the cap was applied. Recorded on the row so a later lift is auditable
#: and so the row is self-describing to a reader who finds it in a slice.
RESEARCH_CEILING_REASON = "uncorroborated_research_origin"


def capped_credibility(host_score: float | None) -> float:
    """``min(host_score, 0.50)`` — and 0.50 when the host is unscored.

    An UNKNOWN host does not get the benefit of the doubt: it gets the cap. The
    column would otherwise be NULL, which reads downstream as "no opinion"
    rather than as "unreviewed open web".
    """
    if host_score is None:
        return RESEARCH_CREDIBILITY_CAP
    try:
        return min(float(host_score), RESEARCH_CREDIBILITY_CAP)
    except (TypeError, ValueError):
        return RESEARCH_CREDIBILITY_CAP


def research_salience(*, scored_at: str | None = None) -> dict[str, Any]:
    """The stamped-at-write, capped salience block.

    Deliberately stamped rather than left NULL for the LLM scorer to fill:
    ``signal_salience._SELECT_BATCH_SQL`` selects ``WHERE s.salience IS NULL``,
    so a stamped row is STRUCTURALLY invisible to the scorer and the ceiling
    cannot be lifted by a model — only by R-D's deterministic corroboration
    UPDATE (spec §1.4b).

    ``event_class``/``actor_rank`` carry the scorer's own unbound defaults
    (``other``/``none``) rather than a guess, and ``authority`` carries the
    ``unknown`` the unregistered source would have resolved to anyway — stating
    it here means a reader never has to re-derive it from an absent join.
    """
    return {
        "event_class": "other",
        "actor_rank": "none",
        "magnitude": RESEARCH_MAGNITUDE_CAP,
        "authority": "unknown",
        "confidence": 0.0,
        "model_id": None,
        "scored_at": scored_at or datetime.now(timezone.utc).isoformat(),
        "research_ceiling": {
            "applied": True,
            "magnitude_cap": RESEARCH_MAGNITUDE_CAP,
            "reason": RESEARCH_CEILING_REASON,
        },
    }


def ceiling_block() -> dict[str, Any]:
    """``payload.research.ceiling`` — the self-describing record of the cap."""
    return {
        "applied": True,
        "magnitude_cap": RESEARCH_MAGNITUDE_CAP,
        "credibility_cap": RESEARCH_CREDIBILITY_CAP,
        "reason": RESEARCH_CEILING_REASON,
    }


# ---------------------------------------------------------------------------
# The licence gate — two depths (spec §1.5)
# ---------------------------------------------------------------------------

#: Licence classes that CLEAR a host for full text + CAS bytes. Every value is
#: an AFFIRMATIVE permission an operator recorded; ``unknown`` and unset are
#: deliberately absent, which is what makes the default teaser depth.
CLEARED_LICENSE_CLASSES: frozenset[str] = frozenset(
    {
        "public_domain",
        "open_gov_attribution",
        "cc_by",
        "cc_by_sa",
        "cc_nc",
        "open_data_sharealike",
        "api_terms",
    }
)

DEPTH_FULL_TEXT = "full_text"
DEPTH_TEASER = "teaser"
DEPTH_FORBIDDEN = "forbidden"

REASON_CLEARED = "cleared"
REASON_LICENSE_UNREVIEWED = "license_unreviewed"
REASON_LICENSE_FORBIDS = "license_forbids"
REASON_ROBOTS_DISALLOWED = "robots_disallowed"
REASON_FETCH_FAILED = "fetch_failed"
#: (a′) A publisher's edge REFUSED us — an anti-bot challenge/interstitial body
#: or a 401/403/429. Distinct from ``fetch_failed`` on purpose: a caller that
#: cannot tell a block from a network error reads both as "the web had
#: nothing", which is the false-absence defect FETCH_REVIEW §2 names. The hit
#: still lands at TEASER depth (the licence gate is untouched); what changes is
#: that the reason SAYS it was blocked.
REASON_FETCH_BLOCKED = "fetch_blocked"
REASON_FETCH_NOT_REQUESTED = "fetch_not_requested"


def forbidden_license_classes() -> frozenset[str]:
    """The never-fetch set — ``evidence_archiver.FORBID_RETENTION_CLASSES``,
    REUSED VERBATIM and never forked. Those three classes are a reviewed
    publisher refusal; R-A honours it PRE-CONNECT (the archiver only ever
    honoured it as a retention refusal, after the bytes were already in hand).
    """
    from .analysts.deterministic_handlers.evidence_archiver import (
        FORBID_RETENTION_CLASSES,
    )

    return FORBID_RETENTION_CLASSES


def depth_for_license(license_class: str | None) -> tuple[str, str]:
    """``license_class`` → ``(depth, depth_reason)``. The whole gate, one call.

    * a REVIEWED forbidding class → ``forbidden``: the URL is never fetched and
      **no signals row is written at all**;
    * an AFFIRMATIVE cleared class → ``full_text``: extracted main text +
      content-addressed bytes + a numbered, citable row;
    * anything else — ``unknown``, unset, ``permissive_feed_unreviewed``, or a
      class nobody has taught this gate — → ``teaser``: the provider snippet,
      no bytes, named to the planner in prose but never numbered.

    The unrecognised-class case landing on TEASER (not on full_text) is the
    fail-safe: a new licence value must never widen retention by default.
    """
    cls = (license_class or "").strip().lower() or None
    if cls is not None and cls in forbidden_license_classes():
        return DEPTH_FORBIDDEN, REASON_LICENSE_FORBIDS
    if cls is not None and cls in CLEARED_LICENSE_CLASSES:
        return DEPTH_FULL_TEXT, REASON_CLEARED
    return DEPTH_TEASER, REASON_LICENSE_UNREVIEWED


_HOST_LEDGER_SQL = """
    SELECT source_host, score, license_class
      FROM source_credibility
     WHERE source_host = ANY($1::text[])
"""


@dataclass(frozen=True)
class HostVerdict:
    """One host's row in the ledger: credibility score + licence verdict."""

    host: str
    score: float | None = None
    license_class: str | None = None
    matched_host: str | None = None


async def resolve_host_verdict(conn: Any, url: str) -> HostVerdict:
    """Read the HOST LEDGER for ``url``'s host — credibility AND licence.

    The ledger is the EXISTING ``source_credibility`` table (119 operator-owned
    host rows) plus one additive ``license_class`` column (migration 0190). A
    new table was not built: one row per host, credibility and licence in one
    operator-reviewable place, already served by ``/api/v1/source_credibility``.

    Resolution reuses ``extract_lookup_hosts`` — exact host first, then
    progressively-trimmed subdomains — so this agrees with the credibility
    lookup the canonical ingest write path performs. An unknown host resolves
    to ``(None, None)`` ⇒ the cap for credibility and TEASER depth for the
    licence. Fail-safe in both directions, and never raises: a ledger read that
    errors degrades to the unknown verdict rather than losing the evidence.
    """
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return HostVerdict(host="")
    candidates = extract_lookup_hosts(host)
    if not candidates:
        return HostVerdict(host=host)
    try:
        rows = await conn.fetch(_HOST_LEDGER_SQL, candidates)
    except Exception as exc:  # pragma: no cover — best-effort ledger read
        logger.warning("research.host_ledger.failed host=%s err=%s", host, exc)
        return HostVerdict(host=host)
    by_host = {r["source_host"]: r for r in rows}
    for candidate in candidates:
        row = by_host.get(candidate)
        if row is None:
            continue
        lic = row["license_class"]
        return HostVerdict(
            host=host,
            score=float(row["score"]) if row["score"] is not None else None,
            license_class=str(lic).strip().lower() if lic else None,
            matched_host=candidate,
        )
    return HostVerdict(host=host)


# ---------------------------------------------------------------------------
# NOVELTY — computed at WRITE time (spec §4.1)
# ---------------------------------------------------------------------------

NOVELTY_SCOPE_TARGET = "dispatching_target"
NOVELTY_SCOPE_NONE = "none"


def _slice_clauses(window_hours: int, geo_param: int | None) -> list[str]:
    """The desk slice's predicate, rebuilt so novelty compares against the SAME
    set ``actor_substrate_slice`` would read: the window, the backfill
    exclusion, canonical-only, the geo overlap, and — unless the flag is at
    ``desks`` — the research exclusion itself.
    """
    clauses = [
        f"fetched_at > NOW() - INTERVAL '{int(window_hours)} hours'",
        SIGNALS_EXCLUDE_BACKFILL_SQL,
        "(canonical_signal_id IS NULL OR canonical_signal_id = id)",
    ]
    if geo_param is not None:
        clauses.append(f"geo && ${geo_param}::text[]")
    if not research_reaches_desks():
        clauses.append(RESEARCH_SLICE_EXCLUSION_SQL)
    return clauses


async def probe_novelty(
    conn: Any,
    *,
    canonical_url: str,
    content_hash: str,
    host: str,
    target_id: str | None,
    target_geo: Sequence[str],
    window_hours: int,
    regime: str,
) -> dict[str, Any]:
    """The novelty block, measured against the dispatching desk's slice NOW.

    Computing this LATER is impossible — the slice is a moving window and by
    the next tick the comparison set has changed. That is the whole reason it
    is a write-time stamp and not a nightly sweep.

    A run with NO dispatching target (the corpus researcher self-selecting)
    yields ``scope: "none"`` and no booleans: there is no desk to be novel
    *to*, and inventing a comparison against the global pool would answer a
    question nobody asked. R-D's denominator filters on exactly this key.
    """
    block: dict[str, Any] = {
        "version": RESEARCH_NOVELTY_VERSION,
        "regime": regime,
        "slice_target_id": target_id,
        "slice_window_hours": int(window_hours),
        "scope": NOVELTY_SCOPE_TARGET if target_id else NOVELTY_SCOPE_NONE,
    }
    if not target_id:
        block.update(
            {
                "slice_size": None,
                "url_in_slice": None,
                "host_in_slice": None,
                "content_hash_in_slice": None,
                "novel": None,
            }
        )
        return block

    params: list[Any] = []
    geo_param = None
    if target_geo:
        params.append(list(target_geo))
        geo_param = len(params)
    where = " AND ".join(_slice_clauses(window_hours, geo_param))
    params.append(canonical_url or "")
    url_param = len(params)
    params.append(content_hash or "")
    hash_param = len(params)
    params.append(f"%{host}%" if host else "")
    host_param = len(params)
    sql = f"""
        SELECT count(*)::bigint AS slice_size,
               count(*) FILTER (WHERE canonical_url = ${url_param})::bigint AS url_hits,
               count(*) FILTER (
                   WHERE content_hash <> '' AND content_hash = ${hash_param}
               )::bigint AS hash_hits,
               count(*) FILTER (
                   WHERE ${host_param} <> '' AND canonical_url ILIKE ${host_param}
               )::bigint AS host_hits
          FROM signals
         WHERE {where}
    """
    try:
        row = await conn.fetchrow(sql, *params)
    except Exception as exc:  # pragma: no cover — never lose the row over a probe
        logger.warning("research.novelty.probe_failed target=%s err=%s", target_id, exc)
        block.update(
            {
                "slice_size": None,
                "url_in_slice": None,
                "host_in_slice": None,
                "content_hash_in_slice": None,
                "novel": None,
                "probe_error": type(exc).__name__,
            }
        )
        return block
    url_in = bool(row["url_hits"])
    hash_in = bool(row["hash_hits"])
    block.update(
        {
            "slice_size": int(row["slice_size"]),
            "slice_size_basis": "window_pool",
            "url_in_slice": url_in,
            "host_in_slice": bool(row["host_hits"]),
            "content_hash_in_slice": hash_in,
            "novel": not (url_in or hash_in),
        }
    )
    return block


# ---------------------------------------------------------------------------
# The row
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResearchDispatch:
    """WHO asked, and about what — the provenance ruling 2 requires.

    ``target_id`` is the ONLY thing that gives a research signal desk reach:
    the slice narrows on ``geo`` alone (133 of 134 live targets carry
    ``sources[].source_id: null``), and ``geo`` is inherited from the
    dispatching question's target. A SELF-SELECTED run has no target ⇒ no geo
    ⇒ it reaches no desk. That is a real limit on the program and it is stated
    rather than papered over: the desk hop is R-B's, not R-A's.

    ``geo``/``tags`` are read from the TARGET DESCRIPTOR (operator-owned),
    never from the planner's arguments — a planner-supplied geo would be a
    fabricated reachability claim.
    """

    kind: str = "self_selected"          # open_question | coverage_floor | self_selected
    hypothesis_id: str | None = None
    target_id: str | None = None
    bounded_question: str = ""
    gap: dict[str, Any] | None = None
    geo: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    window_hours: int = 24


@dataclass(frozen=True)
class ProviderContext:
    """The resolved provider, carried onto every row it produced."""

    component_id: str
    subprovider: str = ""
    route_class: str = ""
    version: str = ""
    degraded: bool = False
    degraded_detail: str = ""
    unresponsive_engines: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResearchHit:
    """One provider hit, already de-wrapped and depth-decided."""

    url: str
    title: str = ""
    snippet: str = ""
    text: str = ""
    published_at: str | None = None
    language: str | None = None
    engine: str | None = None
    rank: int = 0
    depth: str = DEPTH_TEASER
    depth_reason: str = REASON_LICENSE_UNREVIEWED
    license_class: str | None = None
    host_score: float | None = None
    http_status: int | None = None
    content_type: str | None = None
    url_chain: tuple[str, ...] = ()
    query: str = ""
    novelty: dict[str, Any] = field(default_factory=dict)


def content_hash_for(text: str) -> str:
    """sha256 of the text we STORE. Empty text ⇒ ``''`` — the schema default,
    which the S-4 dedup path explicitly refuses as a key (``content_hash <>
    ''``). A fabricated hash over a title would make two unrelated teasers look
    like the same content."""
    body = (text or "").strip()
    if not body:
        return ""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def build_research_payload(
    hit: ResearchHit,
    provider: ProviderContext,
    dispatch: ResearchDispatch,
    *,
    regime: str,
) -> dict[str, Any]:
    """``signals.payload`` for one research hit (spec §1.2b).

    ``payload.text`` is WHAT A DESK READS, and its depth is the licence gate's
    verdict: the extracted main text on a cleared host, the provider's own
    snippet otherwise. Putting extracted full text here at teaser depth —
    storing the content while declaring the bytes withheld — is laundering, and
    it is worse than not archiving at all. ``summary`` is ALWAYS the provider
    snippet and is never synthesized.
    """
    return {
        "title": hit.title,
        "text": hit.text,
        "summary": hit.snippet,
        "link": hit.url,
        "published_at": hit.published_at,
        "language": hit.language,
        "license_class": hit.license_class or "unknown",
        "retrieval_origin": web_search_origin(provider.component_id),
        "research": {
            "schema": RESEARCH_PAYLOAD_SCHEMA,
            "regime": regime,
            "depth": hit.depth,
            "depth_reason": hit.depth_reason,
            "provider": provider.component_id,
            "provider_route_class": provider.route_class,
            "subprovider": provider.subprovider,
            "engine": hit.engine,
            "rank": hit.rank,
            "query": hit.query,
            "degraded": provider.degraded,
            "unresponsive_engines": list(provider.unresponsive_engines),
            "ceiling": ceiling_block(),
            "novelty": dict(hit.novelty),
            # WHO DISPATCHED THIS ROW, in the payload a desk actually reads.
            # ``raw_provenance.dispatch`` has carried the same three facts
            # since R-A, but nothing downstream reads raw_provenance, so the
            # 09-08 15:37Z rows (``geo {}``, ``kind self_selected``) could only
            # be diagnosed by joining back to the run. Mirrored here — from the
            # SAME ``ResearchDispatch``, so the two can never disagree — the
            # question a landed row was fetched for is answerable from the row.
            "dispatch": {
                "kind": dispatch.kind,
                "hypothesis_id": dispatch.hypothesis_id,
                "target_id": dispatch.target_id,
            },
            "corroboration": {
                "state": "pending",
                "checked_at": None,
                "by_signal_id": None,
            },
        },
    }


def build_research_provenance(
    hit: ResearchHit,
    provider: ProviderContext,
    dispatch: ResearchDispatch,
    *,
    run_id: Any,
    requested_by: str,
) -> dict[str, Any]:
    """``signals.raw_provenance`` — the question that dispatched it and the run
    that asked (spec §1.2c, ruling 2's requirement made mechanical).

    Additive with a zero blast radius (no comparator reads ``raw_provenance``
    today) and queryable:
    ``raw_provenance->'dispatch'->>'hypothesis_id'``.

    The ``novelty`` mirror here is deliberate and is the R-D lane's contract in
    the task brief: the two boolean facts a novelty counter needs — did this
    exact URL, or this exact content hash, already exist in the dispatching
    desk's slice — are readable WITHOUT parsing the fuller payload block. Both
    are written by ONE function from ONE probe, so they cannot disagree.
    """
    novelty = dict(hit.novelty)
    return {
        "kind": "research",
        "fetch_kind": "web_evidence",
        "source_kind": "search_provider",
        "provider_component_id": provider.component_id,
        "provider_version": provider.version,
        "query": hit.query,
        "requested_by": requested_by,
        "run_id": str(run_id) if run_id else None,
        "dispatch": {
            "kind": dispatch.kind,
            "hypothesis_id": dispatch.hypothesis_id,
            "target_id": dispatch.target_id,
            "bounded_question": dispatch.bounded_question,
            "gap": dispatch.gap,
        },
        "novelty": {
            "version": novelty.get("version", RESEARCH_NOVELTY_VERSION),
            "scope": novelty.get("scope", NOVELTY_SCOPE_NONE),
            "url_in_slice": novelty.get("url_in_slice"),
            "content_hash_in_slice": novelty.get("content_hash_in_slice"),
            "novel": novelty.get("novel"),
        },
        "fetched_url_chain": list(hit.url_chain),
        "http_status": hit.http_status,
        "content_type": hit.content_type,
    }


def build_research_row(
    hit: ResearchHit,
    provider: ProviderContext,
    dispatch: ResearchDispatch,
    *,
    run_id: Any,
    requested_by: str,
    produced_by_id: str | None = None,
    regime: str,
    fetched_at: datetime | None = None,
) -> dict[str, Any]:
    """The complete ``signals`` row for one hit — every NOT NULL satisfied.

    Returns a plain dict (not a pydantic model): ``Signal`` has no
    ``retrieval_origin`` and no ``salience`` field and forbids extras, so a
    research row is not expressible in it. See :data:`RESEARCH_PRODUCED_BY_KIND`.
    """
    when = fetched_at or datetime.now(timezone.utc)
    payload = build_research_payload(hit, provider, dispatch, regime=regime)
    tags = [RESEARCH_TAG] + [t for t in dispatch.tags if t and t != RESEARCH_TAG]
    return {
        "id": research_signal_id(
            provider_component_id=provider.component_id,
            canonical_url=hit.url,
            run_id=run_id,
        ),
        "source_id": research_source_id(provider.component_id),
        "source_version": provider.version or "",
        "produced_by_id": produced_by_id or (str(run_id) if run_id else None),
        "produced_by_kind": RESEARCH_PRODUCED_BY_KIND,
        "fetched_at": when,
        "owner_tenant": "default",
        "modality": "text",
        # An unreviewed TEASER must never claim evidence_hold. The archiver's
        # stamp raises this to evidence_hold only if bytes actually land.
        "retention_class": "reference_only",
        "object_ref": None,
        "payload": payload,
        "canonical_url": hit.url,
        "raw_provenance": build_research_provenance(
            hit, provider, dispatch, run_id=run_id, requested_by=requested_by,
        ),
        "language": hit.language,
        # THE REACHABILITY KEY. Inherited from the dispatching question's
        # target scope; empty for a self-selected run, which therefore reaches
        # no desk (spec §1.3, consequence 3).
        "geo": list(dispatch.geo),
        "tags": tags,
        # Left empty at write; the NER re-enrichment sweep fills it.
        "entity_classes": [],
        "source_credibility": capped_credibility(hit.host_score),
        "content_hash": content_hash_for(hit.text),
        "derived_from": [],
        "schema_uri": "iglu:legba/signal/jsonschema/3-0-0",
        "retrieval_origin": web_search_origin(provider.component_id),
        "salience": research_salience(scored_at=when.isoformat()),
    }


# The write. Mirrors ``source_actor._INSERT_SIGNAL``'s shape and its
# ``ON CONFLICT (id) DO NOTHING`` idempotence, PLUS the two columns that writer
# cannot carry: ``retrieval_origin`` (the label this whole program exists to
# write) and ``salience`` (stamped so the LLM scorer structurally never sees
# the row — see :func:`research_salience`).
#
# ``indexed_at`` is deliberately NOT set: a fresh row with a NULL marker is
# exactly what ``corpus_indexer``'s partial index scans, so the OpenSearch
# projection happens on the next tick like any other signal, with the
# ``retrieval_origin`` keyword facet (``opensearch.py:93``) populated for free.
_INSERT_RESEARCH_SIGNAL = """
INSERT INTO signals (
    id, source_id, source_version, produced_by_id, produced_by_kind,
    fetched_at, owner_tenant, modality, retention_class, object_ref,
    payload, canonical_url, raw_provenance,
    language, geo, tags, entity_classes, source_credibility,
    content_hash, derived_from, schema_uri,
    retrieval_origin, salience, last_seen_at, origin_class
)
VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8, $9, $10,
    $11::jsonb, $12, $13::jsonb,
    $14, $15::text[], $16::text[], $17::text[], $18,
    $19, $20::uuid[], $21,
    $22, $23::jsonb, $6, $24
)
ON CONFLICT (id) DO NOTHING
RETURNING id
"""


async def land_research_row(conn: Any, row: Mapping[str, Any]) -> Any | None:
    """INSERT one research signal. Returns the id, or ``None`` on conflict.

    ``None`` means "this exact (provider, url, run) already landed" — the
    idempotent replay case a multi-round GATHER produces — never an error.
    """
    return await conn.fetchval(
        _INSERT_RESEARCH_SIGNAL,
        row["id"],
        row["source_id"],
        row["source_version"],
        row["produced_by_id"],
        row["produced_by_kind"],
        row["fetched_at"],
        row["owner_tenant"],
        row["modality"],
        row["retention_class"],
        row["object_ref"],
        json.dumps(row["payload"], default=str),
        row["canonical_url"],
        json.dumps(row["raw_provenance"], default=str),
        row["language"],
        list(row["geo"]),
        list(row["tags"]),
        list(row["entity_classes"]),
        row["source_credibility"],
        row["content_hash"],
        list(row["derived_from"]),
        row["schema_uri"],
        row["retrieval_origin"],
        json.dumps(row["salience"], default=str),
        # V3/P7 — the origin class derives from the retrieval origin this
        # write path exists to stamp (web_search:* → 'web_retrieval').
        origin_class_for(row["retrieval_origin"]),
    )


# ---------------------------------------------------------------------------
# The archive leg — at FETCH time, not through the sweep (spec §1.5)
# ---------------------------------------------------------------------------


async def record_archive_status(
    pool: Any,
    *,
    signal_id: Any,
    status: str,
    url: str,
    license_class: str | None,
    retrieval_origin: str | None,
    last_error: str | None = None,
    object_ref: str | None = None,
    sha256: str | None = None,
    size_bytes: int | None = None,
    content_type: str | None = None,
    text_extracted: bool = False,
    attempts: int = 1,
) -> None:
    """Write the ``evidence_archive`` sidecar row for one research hit.

    REUSES ``evidence_archiver._record`` — the same upsert, the same status
    vocabulary the live CHECK constraint enforces, the same columns — so the
    archiver's own sweep and this fetch-time path can never disagree about what
    an archive row means. Every outcome is recorded, including the refusals:
    ``skipped_license`` (a reviewed class forbade it) and
    ``skipped_license_unreviewed`` (we never reviewed this domain) are the two
    statuses that have NEVER fired in 70,836 live rows, and making them fire
    honestly is half of what this program is.
    """
    from .analysts.deterministic_handlers.evidence_archiver import _record

    await _record(
        pool,
        signal_id=signal_id,
        status=status,
        attempts=attempts,
        object_ref=object_ref,
        sha256=sha256,
        size_bytes=size_bytes,
        content_type=content_type,
        fetched_url=url,
        license_class=license_class,
        retrieval_origin=retrieval_origin,
        text_extracted=text_extracted,
        last_error=last_error,
    )


def store_archive_bytes(root: Path, body: bytes) -> tuple[str, str, bool]:
    """Content-address ``body`` under ``root`` → ``(sha256, object_ref, existed)``.

    REUSES ``evidence_archiver._store_bytes`` (temp-file + atomic rename; an
    existing object is never rewritten) so the CAS address format and the write
    discipline live in exactly one place.
    """
    from .analysts.deterministic_handlers.evidence_archiver import _store_bytes

    return _store_bytes(root, body)


async def stamp_archived_signal(
    conn: Any, *, signal_id: Any, object_ref: str, archived_text: str | None,
) -> None:
    """Stamp ``object_ref`` (+ archived_text) onto the landed row.

    REUSES ``evidence_archiver._STAMP_SIGNAL_SQL`` verbatim, which is what
    keeps ARCHIVE-THEN-INDEX true: that one UPDATE sets ``indexed_at = NULL``
    **and** ``updated_at = now()`` together — nulling without bumping lets an
    in-flight indexer batch clobber the re-null and the corpus doc goes stale
    forever (``corpus_indexer``'s DIRTY-MARKER CONTRACT). It also raises
    ``retention_class`` to ``evidence_hold`` — correct here precisely BECAUSE
    bytes landed; a teaser never reaches this function.
    """
    from .analysts.deterministic_handlers.evidence_archiver import _STAMP_SIGNAL_SQL

    chars = len(archived_text) if archived_text is not None else None
    await conn.execute(_STAMP_SIGNAL_SQL, signal_id, object_ref, archived_text, chars)


def extract_archived_text(
    body: bytes, content_type: str | None, encoding: str | None, *, max_chars: int,
) -> str | None:
    """Trafilatura main-text extraction with the archiver's OWN guards.

    Returns ``None`` for a non-textual body, a failed extraction, or a
    JS-wall / bot-check / redirect-interstitial page wearing the shape of a
    successful extraction (``_match_wall_pattern``) — the last one matters
    doubly here: a wall page's boilerplate stored as ``payload.text`` would
    become a numbered [N] citation whose "source" is a cookie banner.
    """
    from .analysts.deterministic_handlers.evidence_archiver import (
        _extract_text,
        _is_textual,
        _match_wall_pattern,
    )

    if not _is_textual(content_type, body):
        return None
    text = _extract_text(body, encoding, max_chars=max_chars)
    if text is None:
        return None
    if _match_wall_pattern(text) is not None:
        return None
    return text


def detect_challenge_page(
    body: bytes | None, content_type: str | None = None,
    encoding: str | None = None,
) -> str | None:
    """The anti-bot CHALLENGE tell in a raw response body, else ``None``.

    REUSES ``_challenge_detect.detect_challenge_page`` so the archiver sweep,
    the ``web_evidence`` tool and the ``fetch_impersonation_probe`` script can
    never disagree about what a challenge page is. See that module for why the
    raw body is scanned and not only the extracted text.
    """
    from .analysts.deterministic_handlers._challenge_detect import (
        detect_challenge_page as _detect,
    )

    return _detect(body, content_type, encoding)


__all__ = [
    "CLEARED_LICENSE_CLASSES",
    "DEPTH_FORBIDDEN",
    "DEPTH_FULL_TEXT",
    "DEPTH_TEASER",
    "HostVerdict",
    "NOVELTY_SCOPE_NONE",
    "NOVELTY_SCOPE_TARGET",
    "ProviderContext",
    "REASON_CLEARED",
    "REASON_FETCH_BLOCKED",
    "REASON_FETCH_FAILED",
    "REASON_FETCH_NOT_REQUESTED",
    "REASON_LICENSE_FORBIDS",
    "REASON_LICENSE_UNREVIEWED",
    "REASON_ROBOTS_DISALLOWED",
    "RESEARCH_CEILING_REASON",
    "RESEARCH_CREDIBILITY_CAP",
    "RESEARCH_MAGNITUDE_CAP",
    "RESEARCH_NOVELTY_VERSION",
    "RESEARCH_NS",
    "RESEARCH_PAYLOAD_SCHEMA",
    "RESEARCH_PRODUCED_BY_KIND",
    "RESEARCH_SOURCE_PREFIX",
    "RESEARCH_TAG",
    "ResearchDispatch",
    "ResearchHit",
    "build_research_payload",
    "build_research_provenance",
    "build_research_row",
    "capped_credibility",
    "ceiling_block",
    "content_hash_for",
    "depth_for_license",
    "detect_challenge_page",
    "extract_archived_text",
    "forbidden_license_classes",
    "is_research_source_id",
    "land_research_row",
    "probe_novelty",
    "record_archive_status",
    "research_salience",
    "research_signal_id",
    "research_source_display_name",
    "research_source_id",
    "stamp_archived_signal",
    "store_archive_bytes",
]
