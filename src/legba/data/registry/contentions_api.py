# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/contentions`` — the contrary-evidence pass's records (Program 7a).

WHAT THIS SERVES, AND WHAT IT IS NOT. Each row is one RETRIEVAL OUTCOME against
one published claim: the counter-query that was issued, the rung it ran on, the
pages this platform actually fetched, and a stance derived in code from that
text against the claim's own polarity. It is not a verdict on the claim and the
response says so on every row — ``adjudicated`` is absent because there is no
such field to be absent from, and the route's own ``note`` states the contract
so a client cannot read a stance as a truth value by accident.

WHY A SIBLING OF ``GET /api/v1/contention`` RATHER THAN AN EXTENSION OF IT. The
existing route serves ``fact_contention``: a disagreement between VALUES already
extracted for one subject/predicate, with a surfaced winner and a collapse
lifecycle. This one serves a disagreement between a published CLAIM and a page
retrieved on purpose to oppose it, and it has no winner and never will. The two
have different keys (subject/predicate versus a claim id), different
vocabularies (contested/surfaced/collapsed versus the four retrieval stances)
and different lifecycles. Folding them into one route would force a reader to
discover which kind of row it is holding before it could interpret any field —
which is how a union type becomes an outage.

FILTERS. ``scope`` matches EITHER handle — the TARGET (``country_g20_in``) or
the desk key (``energy_security``, which on this table is the analyst id) — and
the response says which one the returned rows actually matched on
(``scope_field``). It accepts both because every other reader surface on this
platform means the TARGET by ``scope`` while this route shipped meaning only
``desk_key``, and a route that quietly means something else hands a reader an
empty list with no way to tell "nothing found" from "wrong handle". On a country
desk the two are often the same string, which is exactly why guessing would have
been worse than matching both. ``finding_id`` is the published read the
Inspector is showing — the chip's join, paired with each row's
``block_ordinal``, because ``|blocks| == |citations| == |distinct markers|``
makes block N the citation ``[[ref:N]]``; ``claim_id`` is the exact single-claim
drill; ``stance`` narrows to one outcome. With none of them the route returns the
most recent records fleet-wide, newest first — the operator's "what did the pass
find last night" read.

THE FENCE COLUMNS (migration 0222) ARE SERVED AS ABSENCE OR AS A NUMBER, NEVER
AS ZERO. ``host_class``, ``page_published_at``, ``subject_overlap`` and
``independent_pages`` are what the four fences measured on the decisive page. A
row written before 0222 measured none of them and serves ``null`` in all four —
"not measured", which is not "measured, and the answer was nothing".

``live`` vs ``expired`` IS RENDERED, NEVER FILTERED BY DEFAULT. A contention has
a shelf life because the web moves; a client that wants only live rows asks for
them, and one that wants history gets it with the flag telling the two apart.
Silently hiding expired rows would make a pass that stopped running look exactly
like a week with nothing to contend.

Registry-slim: stdlib + fastapi + pydantic + ``.api`` only. Nothing from
``legba.data.analysts`` or ``legba.runtime`` — this module ships in the slim
registry image, and a deferred import would not help (deferring moves WHEN the
graph is walked, never HOW FAR).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

CONTENTIONS_ROUTE_VERSION = "2026-09/7a.2"

_ROUTE = "/contentions"

#: The contract, on every response. A client that renders a stance as a verdict
#: is misreading it, and the cheapest place to say so is beside the rows.
CONTRACT_NOTE = (
    "Each row is a RETRIEVAL outcome against one published claim, never a "
    "verdict on it. 'contradicts' means a page this platform fetched states "
    "the opposite; it does not mean the claim is false. Only "
    "derivation='polarity' records reach a composition; 'negation' records are "
    "an uncalibrated fallback shown here for a human to judge."
)

_STANCES = ("contradicts", "qualifies", "none_found", "search_failed")

_SELECT = """
SELECT claim_id,
       claim_text,
       finding_id::text            AS finding_id,
       origin_head_id::text        AS origin_head_id,
       block_ordinal,
       span_role,
       target_id,
       desk_key,
       analyst_id,
       query,
       query_source,
       query_novel_tokens,
       stance,
       derivation,
       reason,
       statement,
       rung,
       refs,
       array_length(ref_signal_ids, 1) AS linked_signals,
       host_class,
       page_published_at::text     AS page_published_at,
       subject_overlap,
       independent_pages,
       pipeline_version,
       retrieved_at::text          AS retrieved_at,
       as_of::text                 AS as_of,
       expires_at::text            AS expires_at,
       (expires_at > now())        AS live,
       (desk_key = $1)             AS scope_is_desk,
       (target_id = $1)            AS scope_is_target
  FROM claim_contentions
 WHERE ($1::text IS NULL OR desk_key = $1 OR target_id = $1)
   AND ($2::text IS NULL OR claim_id = $2)
   AND ($3::text IS NULL OR stance = $3)
   AND ($4::bool IS NOT TRUE OR expires_at > now())
   AND ($5::uuid IS NULL OR finding_id = $5)
 ORDER BY as_of DESC, claim_id
 LIMIT $6
"""

#: ``scope_field`` values. Which COLUMN the returned rows actually matched on —
#: derived from the rows themselves rather than guessed from the string's shape,
#: because nothing about ``energy_security`` or ``country_g20_in`` tells a route
#: which kind of handle it is holding.
SCOPE_FIELD_DESK = "desk_key"
SCOPE_FIELD_TARGET = "target_id"
SCOPE_FIELD_BOTH = "both"


class ContentionRef(BaseModel):
    """One page the pass FETCHED. A search snippet never becomes a ref.

    The last four fields are the FOUR FENCES' record of THIS page (0222):
    ``host_class`` is what kind of host answered, ``page_published_at`` the date
    the date gate actually parsed (``published_at`` stays the raw discovery, so a
    stamp the gate could not read is visible as the difference between the two),
    ``subject_overlap`` how much of the claim's subject the matched sentence
    carried, and ``fence`` the rule that refused or capped the page — empty on
    an admissible one.
    """

    url: str = ""
    sha256: str = ""
    chars: int = 0
    published_at: Optional[str] = None
    extracted: bool = True
    fetched_at: str = ""
    status_code: Optional[int] = None
    stance: str = ""
    quote: str = ""
    host_class: str = ""
    page_published_at: Optional[str] = None
    subject_overlap: int = 0
    fence: str = ""


class ContentionRow(BaseModel):
    """One contention record.

    ``statement`` is the hedged line a composition would render — present on
    stance-bearing rows only, and empty rather than invented everywhere else.
    ``linked_signals`` counts corpus rows whose content hash matched a fetched
    page; the pass writes no signals of its own, so zero is the normal value and
    is not a gap.

    ``host_class`` / ``page_published_at`` / ``subject_overlap`` /
    ``independent_pages`` are the four fences' numbers for the DECISIVE page, and
    every one of them is ``None`` rather than a default when it was not measured
    — a row written before migration 0222 measured none of them, and "not
    measured" is not zero. A client rendering these must print absence as
    absence.
    """

    claim_id: str
    claim_text: str
    finding_id: Optional[str] = None
    origin_head_id: Optional[str] = None
    block_ordinal: Optional[int] = None
    span_role: Optional[str] = None
    target_id: Optional[str] = None
    desk_key: str = ""
    analyst_id: str = ""
    query: str = ""
    query_source: str = ""
    query_novel_tokens: int = 0
    stance: str
    derivation: str
    reason: str = ""
    statement: str = ""
    rung: str = ""
    refs: list[ContentionRef] = Field(default_factory=list)
    linked_signals: int = 0
    host_class: Optional[str] = None
    page_published_at: Optional[str] = None
    subject_overlap: Optional[int] = None
    independent_pages: Optional[int] = None
    pipeline_version: str = ""
    retrieved_at: str = ""
    as_of: str = ""
    expires_at: str = ""
    live: bool = False


class ContentionsOut(BaseModel):
    """The listing — newest first.

    ``scope`` echoes the filter that produced it so a cached response can never
    be mistaken for a different desk's, and ``scope_field`` says WHICH HANDLE the
    returned rows matched on — ``target_id``, ``desk_key``, or ``both`` when the
    two are the same string (the common case on a country desk). It is ``None``
    when no scope was asked for, and also when a scope matched nothing: a route
    that named a column it never actually filtered on would be asserting
    something it did not do.
    """

    version: str = CONTENTIONS_ROUTE_VERSION
    note: str = CONTRACT_NOTE
    scope: Optional[str] = None
    scope_field: Optional[str] = None
    stances: list[str] = Field(default_factory=lambda: list(_STANCES))
    contentions: list[ContentionRow] = Field(default_factory=list)


def _refs_of(raw: Any) -> list[ContentionRef]:
    """Parse the ``refs`` JSONB into models, dropping anything malformed.

    A ref that cannot be parsed is DROPPED rather than rendered half-built: the
    whole claim this column makes is "here is a page we hold", and a ref missing
    its url or its hash cannot make it.
    """
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    out: list[ContentionRef] = []
    for entry in raw or ():
        if not isinstance(entry, dict):
            continue
        try:
            out.append(ContentionRef(**entry))
        except Exception:  # noqa: BLE001 — a bad ref is dropped, not fatal
            continue
    return out


def _scope_field(
    scope: Optional[str], matched_desk: bool, matched_target: bool,
) -> Optional[str]:
    """Which column the RETURNED ROWS matched ``scope`` on, or ``None``.

    Read off the rows rather than inferred from the string, because nothing
    about ``energy_security`` or ``country_g20_in`` says which kind of handle it
    is — the live table has rows whose ``desk_key`` is an analyst id and whose
    ``target_id`` is a country, and country desks where the two are equal.
    ``None`` when no scope was asked for, and ``None`` when a scope matched
    nothing: naming a column the route did not actually filter on would be
    asserting something it did not do.
    """
    if not scope:
        return None
    if matched_desk and matched_target:
        return SCOPE_FIELD_BOTH
    if matched_target:
        return SCOPE_FIELD_TARGET
    if matched_desk:
        return SCOPE_FIELD_DESK
    return None


def build_contentions_router(deps: RegistryAPIDeps) -> APIRouter:
    """Build the read-only router (mounted at ``/api/v1/v3``)."""
    router = APIRouter(tags=["contentions"])

    def _get_deps(request: Request) -> RegistryAPIDeps:
        return getattr(request.app.state, "registry_deps", deps)

    @router.get(_ROUTE, response_model=ContentionsOut)
    async def list_contentions(
        request: Request,
        scope: Optional[str] = Query(
            default=None,
            description="The TARGET (country_g20_in) or the desk key "
                        "(energy_security) — both are matched, and "
                        "scope_field on the response says which the returned "
                        "rows used. Omit for fleet-wide.",
        ),
        claim_id: Optional[str] = Query(
            default=None,
            description="The audit's claim key — an exact single-claim drill.",
        ),
        finding_id: Optional[str] = Query(
            default=None,
            description="The published read the records were written against. "
                        "With block_ordinal on each row this is the Inspector "
                        "chip's join: block N IS citation [[ref:N]].",
        ),
        stance: Optional[str] = Query(
            default=None,
            description="One of contradicts | qualifies | none_found | "
                        "search_failed.",
        ),
        live_only: bool = Query(
            default=False,
            description="Only records whose shelf life has not run out. Off by "
                        "default: hiding expired rows would make a stopped pass "
                        "look like a quiet week.",
        ),
        limit: int = Query(default=100, ge=1, le=500),
        principal: str = Depends(require_bearer),
    ) -> ContentionsOut:
        """Contrary-evidence records, newest first.

        Degrades to an empty list at HTTP 200 on ANY query failure, including
        migrations 0221/0222 not yet applied — the ``system_escalations``
        posture. A reader surface must not 500 because an optional sidecar is
        missing, and "no records" is the same honest answer a genuinely empty
        table gives.
        """
        wanted = stance if stance in _STANCES else None
        d = _get_deps(request)
        try:
            async with d.descriptor_registry.pg.acquire() as conn:
                rows = await conn.fetch(
                    _SELECT, scope or None, claim_id or None, wanted,
                    bool(live_only), finding_id or None, int(limit),
                )
        except Exception as exc:  # noqa: BLE001 — degrade to [], HTTP 200
            logger.info(
                "v3.contentions.unavailable err_class=%s", type(exc).__name__,
            )
            return ContentionsOut(scope=scope)
        out: list[ContentionRow] = []
        matched_desk = matched_target = False
        for row in rows:
            record = dict(row)
            # The two scope probes are per-row booleans the query computed; they
            # are not fields of a contention and must not reach the model.
            matched_desk = matched_desk or bool(record.pop("scope_is_desk", False))
            matched_target = matched_target or bool(
                record.pop("scope_is_target", False)
            )
            record["refs"] = _refs_of(record.get("refs"))
            record["linked_signals"] = int(record.get("linked_signals") or 0)
            out.append(ContentionRow(**record))
        return ContentionsOut(
            scope=scope,
            scope_field=_scope_field(scope, matched_desk, matched_target),
            contentions=out,
        )

    return router


__all__ = [
    "CONTENTIONS_ROUTE_VERSION",
    "CONTRACT_NOTE",
    "SCOPE_FIELD_BOTH",
    "SCOPE_FIELD_DESK",
    "SCOPE_FIELD_TARGET",
    "ContentionRef",
    "ContentionRow",
    "ContentionsOut",
    "build_contentions_router",
]
