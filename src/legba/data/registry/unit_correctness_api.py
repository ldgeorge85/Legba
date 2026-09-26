# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE PER-UNIT CORRECTNESS NUMBER, on the read surface (Program 2, track G2).

`GET /api/v1/units/{target_id}/correctness` — the latest `unit_correctness`
row per `analyst_id` for one target, and, on demand (`?claims=1`), the
per-claim ledger under each of them. SELECT-only, cursor-paginated,
bearer-gated, mounted into the substrate-reads router so the number lives on
the same API surface as the reads it sits beside.

WHY A SIBLING MODULE AND NOT `substrate_reads_api.py`. That file was 1,207
lines; the response models + SQL + hydration here are ~300 more, which would
put it over the 1,500-line entry threshold of `tests/test_module_size_gate.py`
and make it a new pinned monster. The router is `include_router`-ed into the
substrate-reads router, so the mounted path and the auth gate are identical to
having written it inline — the seam is a file boundary, not an API boundary.

── WHAT THIS ROUTE WILL AND WILL NOT SAY ───────────────────────────────────

  * It returns rows that EXIST. A unit with no `unit_correctness` row comes
    back absent from `data[]` — never as a synthesized 1.0, never as a 0.0.
    The read surface renders nothing for an absent unit; "we did not measure
    this" and "we measured this and it was perfect" must not look alike.

  * `correctness_share` and `coverage_share` travel TOGETHER, each with the
    `n` it rests on (`n_decided` and `n_claims`). Migration 0196's own words:
    neither is readable without the other. Both are `null` — never `0.0` —
    when their denominator is empty, and that nullability is preserved
    end-to-end rather than coalesced for the client's convenience.

  * `single_family` is computed and surfaced per row, because a number three
    families agreed on and a number one family produced alone are different
    numbers. It is true when the stored `families` block says so (`<= 1`
    family answered at all) OR when every claim carried one family's label.

  * The REFERENCE is a first-class part of the answer, at page level and per
    row: its window, its builder, its sha, its thin dimensions, and a `state`
    of `current` / `stale` / `none`. That state is the GRADER's, not this
    route's: `reference_age_days()` / `reference_grace_days()` /
    `reference_is_stale()` come from the shared leaf
    `data/correctness_reference_currency`, which the grader imports and
    re-exports — so a reference three days past its window, one the grader will
    still grade against under the 7-day default grace, is not described to a
    reader as stale. A row's age is the grader's STORED
    `unit_correctness.reference_age_days` (migration 0197); only a pre-0197 row
    is aged here. `none` stays a page-level fact: a row always has a reference.

    THAT LEAF IS A LEAF ON PURPOSE. This module may not import
    `analysts.deterministic_handlers`, deferred or otherwise: the registry
    image carries no runtime analyst dependencies, and the handler package's
    import graph reaches `feedparser`. It did, and the deployed
    `GET /units/{target_id}/correctness` answered 500. A subprocess guard test
    poisons `sys.modules["feedparser"]` and imports this module, so the next
    convenient import fails in CI instead of in production.

Reference: `docs/CORRECTNESS_GRADER.md`, migration
`0196_unit_correctness_grading.sql`, `LEDGER_RESET_2026-09-16` §3 Program 2
row G2.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..correctness_reference_currency import (
    reference_age_days,
    reference_grace_days,
    reference_is_stale,
)
from .api import RegistryAPIDeps, require_bearer

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Units per page. A country carries ~9 bounded units (8 desks + the
#: composition), so the default returns a whole country in one call.
DEFAULT_UNIT_LIMIT = 50
MAX_UNIT_LIMIT = 200

#: Per-claim ledger rows returned per REQUEST (not per unit) when
#: ``?claims=1``. The Israel run segmented 54 claims across 9 units; the cap
#: exists so a pathological target cannot hand the browser an unbounded body.
DEFAULT_CLAIM_LIMIT = 500
MAX_CLAIM_LIMIT = 2_000

Grain = Literal["desk", "composition"]
ReferenceState = Literal["current", "stale", "none"]

_SECONDS_PER_DAY = 86_400.0

#: How a NULL share prints. Never "0%", never "100%", never an empty string
#: that a template might swallow: a reader must be able to tell "we measured
#: nothing" from "we measured badly" at a glance, in the badge itself.
UNMEASURED = "unmeasured"


# ---------------------------------------------------------------------------
# Cursor — keyed on analyst_id
# ---------------------------------------------------------------------------
#
# The other substrate-read endpoints paginate on ``(produced_at, id)`` because
# they walk a time series. This one does not: it returns ONE row per unit (the
# latest), so the natural — and only stable — ordering is by `analyst_id`, which
# also happens to line the desks up alphabetically for the panel that renders
# them. The cursor is the last `analyst_id` emitted, base64'd so it stays
# opaque like its siblings.


def _encode_unit_cursor(analyst_id: str) -> str:
    payload = json.dumps({"analyst_id": analyst_id})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_unit_cursor(cursor: str) -> str:
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode("ascii"))
        obj = json.loads(decoded)
        analyst_id = str(obj["analyst_id"])
    except Exception as exc:  # pragma: no cover - validation path
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid cursor: {exc}",
        )
    return analyst_id


def _validate(limit: int, *, maximum: int, name: str) -> int:
    if limit < 1 or limit > maximum:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{name} must be in [1, {maximum}]",
        )
    return limit


# ---------------------------------------------------------------------------
# Response models — column-for-column mirrors of migration 0196.
# ---------------------------------------------------------------------------


class ReferenceRef(BaseModel):
    """The independent reference a number was computed against.

    Carried at page level (the target's newest reference, which is what a
    future grading run would use) and on every row (the reference THAT row was
    actually graded against — not necessarily the same one).
    """

    state: ReferenceState = Field(
        description=(
            "`current` = the as-of stamp falls inside the window; `stale` = it "
            "is past `window_end`; `none` = this target has no reference row "
            "at all. Descriptive arithmetic over the window, not a policy."
        ),
    )
    id: str | None = None
    sha256: str | None = None
    builder: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    span_verified_rate: float | None = Field(
        default=None,
        description=(
            "The BUILDER's number. NULL = the builder did not report one, "
            "which is not the same as zero."
        ),
    )
    thin_dimensions: list[str] = Field(default_factory=list)
    age_days: float | None = Field(
        default=None,
        description=(
            "Days between `window_end` and the as-of stamp, 0.0 while the "
            "stamp is still inside the window. NULL when there is no "
            "reference. On a row this is the grader's own STORED "
            "`unit_correctness.reference_age_days` (migration 0197) wherever "
            "it exists."
        ),
    )
    age_source: Literal["stored", "derived"] | None = Field(
        default=None,
        description=(
            "`stored` = the grader wrote this age on the row at grading time "
            "(migration 0197). `derived` = a pre-0197 row, aged here from the "
            "reference window by the grader's own helper. NULL at page level, "
            "where there is no row to have stored anything."
        ),
    )


class UnitCorrectnessClaimRow(BaseModel):
    """One claim in the ledger under a unit's number — the re-argument surface."""

    id: str
    claim_id: str
    grain: str
    claim_text: str
    label_by_family: dict[str, Any] = Field(default_factory=dict)
    adjudicated: str
    n_families: int
    single_family: bool
    spans: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class UnitCorrectnessRow(BaseModel):
    """One bounded unit's correctness at one as-of stamp."""

    id: str
    analyst_id: str
    target_id: str
    head_id: str
    as_of: datetime
    rubric_sha: str
    grain: str

    n_claims: int
    n_contains: int
    n_contradicts: int
    n_silent: int
    n_split: int
    n_unparseable: int
    n_single_family: int
    n_decided: int = Field(
        description=(
            "contains + contradicts — the denominator `correctness_share` "
            "rests on, materialized so no client has to reconstruct it."
        ),
    )

    correctness_share: float | None = Field(
        description=(
            "contains / (contains + contradicts). NULL — never 0.0 — when the "
            "reference bore on nothing the unit said."
        ),
    )
    coverage_share: float | None = Field(
        description=(
            "(contains + contradicts) / n_claims. Unreadable without "
            "`correctness_share`, and vice versa."
        ),
    )
    single_family: bool = Field(
        description=(
            "True when only ONE grader family stood behind this number — "
            "either because only one answered at all, or because every claim "
            "carried a single family's label. A real number, and a weaker one."
        ),
    )

    badge: str = Field(
        description=(
            "The read surface's line, composed SERVER-side and rendered "
            "verbatim — `correctness 90.9% (10/11) · coverage 24.4% (n=45) · "
            "as of 2026-09-16 · single-family`. See `correctness_badge`."
        ),
    )

    families: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float
    created_at: datetime
    reference: ReferenceRef
    claims: list[UnitCorrectnessClaimRow] | None = Field(
        default=None,
        description="Present only when the request asked for `claims=1`.",
    )


class UnitCorrectnessPage(BaseModel):
    target_id: str
    reference: ReferenceRef = Field(
        description=(
            "The target's NEWEST reference and its state as of the request — "
            "how the surface says `no reference` / `reference stale` for a "
            "target whose units carry no row yet."
        ),
    )
    data: list[UnitCorrectnessRow] = Field(default_factory=list)
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# SQL — SELECT only.
# ---------------------------------------------------------------------------

#: The latest row per unit for one target. ``DISTINCT ON (analyst_id)`` with a
#: descending as-of is the house keyset idiom for "newest per key"; the outer
#: ORDER BY re-sorts the survivors into the cursor's ascending analyst order.
_LATEST_PER_UNIT_SQL = """
SELECT * FROM (
    SELECT DISTINCT ON (uc.analyst_id)
           uc.id, uc.analyst_id, uc.target_id, uc.head_id, uc.as_of,
           uc.reference_id, uc.rubric_sha, uc.grain,
           uc.n_claims, uc.n_contains, uc.n_contradicts, uc.n_silent,
           uc.n_split, uc.n_unparseable, uc.n_single_family,
           uc.correctness_share, uc.coverage_share, uc.families, uc.cost_usd,
           uc.reference_age_days, uc.created_at,
           ur.id                 AS ref_id,
           ur.sha256             AS ref_sha256,
           ur.builder            AS ref_builder,
           ur.window_start       AS ref_window_start,
           ur.window_end         AS ref_window_end,
           ur.span_verified_rate AS ref_span_verified_rate,
           ur.thin_dimensions    AS ref_thin_dimensions
      FROM unit_correctness uc
      JOIN unit_references ur ON ur.id = uc.reference_id
     WHERE {where}
     ORDER BY uc.analyst_id, uc.as_of DESC, uc.created_at DESC
) latest
 ORDER BY analyst_id
 LIMIT {limit_param}
"""

#: The target's newest reference, for the page-level state.
_NEWEST_REFERENCE_SQL = """
SELECT id, sha256, builder, window_start, window_end,
       span_verified_rate, thin_dimensions
  FROM unit_references
 WHERE target_id = $1
 ORDER BY window_end DESC, built_at DESC
 LIMIT 1
"""

#: The ledger under a page of units, in one round trip.
_CLAIMS_SQL = """
SELECT id, correctness_id, claim_id, grain, claim_text, label_by_family,
       adjudicated, n_families, single_family, spans, created_at
  FROM unit_correctness_claims
 WHERE correctness_id = ANY($1::uuid[])
 ORDER BY correctness_id, claim_id
 LIMIT $2
"""


# ---------------------------------------------------------------------------
# Hydration
# ---------------------------------------------------------------------------


def _jsonb(value: Any) -> dict[str, Any]:
    """asyncpg hands jsonb back as `str` on some pools and `dict` on others."""
    if value is None:
        return {}
    if isinstance(value, (str, bytes)):
        try:
            loaded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return dict(value) if isinstance(value, dict) else {}


def _float_opt(value: Any) -> float | None:
    """`numeric` → float, preserving NULL. The NULL is the measurement."""
    return None if value is None else float(value)


def _age_days(window_end: datetime | None, stamp: datetime) -> float | None:
    """``stamp - window_end`` in days, floored at zero — the SAME function the
    grader calls, so a value this route derives for a pre-0197 row is the same
    number the grader would have stored for it."""
    return reference_age_days(stamp, window_end)


def _reference_state(
    window_start: datetime | None,
    window_end: datetime | None,
    stamp: datetime,
    age_days: float | None = None,
) -> ReferenceState:
    """`none` / `stale` / `current`, on the GRADER's grace.

    A reference is current until ``window_end + LEGBA_GRADER_REFERENCE_GRACE_DAYS``
    (default 7) — not merely until ``window_end``. That grace is what the
    grader's own reference lookup uses (migration 0197's train), and reading it
    here from `reference_grace_days()` is what stops the badge and the grader
    disagreeing about the word "stale": a reference three days past its window
    is one the grader will still grade against, so the badge must not call it
    stale.

    ``age_days`` is passed in when the row STORES it (`unit_correctness.
    reference_age_days`, written at grading time) so the state rests on the age
    the grader actually recorded rather than on a re-derivation.
    """
    if window_start is None or window_end is None:
        return "none"
    age = age_days if age_days is not None else _age_days(window_end, stamp)
    if age is None:
        return "none"
    # The grace resolved explicitly at the call site rather than defaulted
    # inside the predicate: the number that decided this is the one an
    # operator set, and a reader of this function should see it being read.
    return "stale" if reference_is_stale(age, reference_grace_days()) else "current"


def _hydrate_reference(row: Any, stamp: datetime) -> ReferenceRef:
    """The page-level reference block (or the `none` state when absent)."""
    if row is None:
        return ReferenceRef(state="none")
    age = _age_days(row["window_end"], stamp)
    return ReferenceRef(
        state=_reference_state(
            row["window_start"], row["window_end"], stamp, age_days=age
        ),
        id=str(row["id"]),
        sha256=row["sha256"],
        builder=row["builder"],
        window_start=row["window_start"],
        window_end=row["window_end"],
        span_verified_rate=_float_opt(row["span_verified_rate"]),
        thin_dimensions=list(row["thin_dimensions"] or []),
        age_days=age,
    )


def _row_reference(row: Any) -> ReferenceRef:
    """The reference THIS row was graded against, aged at the row's own
    `as_of` — the stamp the grader actually stood on.

    THE STORED COLUMN WINS. Migration 0197 put `reference_age_days` on
    `unit_correctness`, written by the grader from the very stamp it graded at;
    that number is the record, and re-deriving it here would let a later edit
    to a reference's window silently change the age a published number is
    described as resting on. The derivation survives only as the fallback for a
    row written before 0197, where the column is NULL.
    """
    as_of = row["as_of"]
    stored = _float_opt(row["reference_age_days"])
    age = stored if stored is not None else _age_days(row["ref_window_end"], as_of)
    return ReferenceRef(
        state=_reference_state(
            row["ref_window_start"], row["ref_window_end"], as_of,
            age_days=age,
        ),
        id=str(row["ref_id"]),
        sha256=row["ref_sha256"],
        builder=row["ref_builder"],
        window_start=row["ref_window_start"],
        window_end=row["ref_window_end"],
        span_verified_rate=_float_opt(row["ref_span_verified_rate"]),
        thin_dimensions=list(row["ref_thin_dimensions"] or []),
        age_days=age,
        age_source="stored" if stored is not None else "derived",
    )


def single_family_row(
    families: Any, n_single_family: Any, n_claims: Any
) -> bool:
    """Did ONE family stand behind this number?

    Two independent ways it can be true, and the surface must say so for
    either: the stored ``families`` block already computed ``single_family``
    (``<= 1`` family answered the unit at all), or every claim in the unit
    carried exactly one family's label (the cost triage at a $0 ceiling
    produces this even when the block disagrees). Shared with the composition
    gate — `composition_correctness_gate.py` imports THIS function rather than
    re-deriving the predicate, so the badge and the gate can never disagree
    about what "single-family" means.
    """
    block = _jsonb(families)
    if bool(block.get("single_family")):
        return True
    try:
        n_single = int(n_single_family or 0)
        n_all = int(n_claims or 0)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return False
    return n_all > 0 and n_single >= n_all


def _pct(share: float | None) -> str:
    """A share as a one-decimal percentage, or the honest absence."""
    return UNMEASURED if share is None else f"{share * 100:.1f}%"


def correctness_badge(
    *,
    correctness_share: float | None,
    coverage_share: float | None,
    n_contains: int,
    n_decided: int,
    n_claims: int,
    as_of: datetime,
    single_family: bool,
    reference_state: ReferenceState,
    reference_age_days: float | None,
) -> str:
    """The badge string, composed HERE and rendered verbatim by the client.

    Same contract as `/eval/scores`' server-composed badge (the UI's
    `UnitEvalBadge` renders that one verbatim too): the "no invented number"
    rule lives in ONE place, so a client cannot quietly decide that a NULL
    share rounds to 0% or that a coverage of 0.244 is "about a quarter".

    The shape, in full::

        correctness 90.9% (10/11) · coverage 24.4% (n=45) · as of 2026-09-16
            · single-family · reference stale (2.1 d)

    The last two segments appear only when they are true. A NULL
    `correctness_share` prints ``unmeasured (0 decided)`` — the reference bore
    on nothing this unit said, which is not a score of zero and must never
    read as one.
    """
    if correctness_share is None:
        head = f"correctness {UNMEASURED} ({n_decided} decided)"
    else:
        head = f"correctness {_pct(correctness_share)} ({n_contains}/{n_decided})"
    parts = [
        head,
        f"coverage {_pct(coverage_share)} (n={n_claims})",
        f"as of {as_of.date().isoformat()}",
    ]
    if single_family:
        parts.append("single-family")
    if reference_state == "stale":
        age = "" if reference_age_days is None else f" ({reference_age_days:.1f} d)"
        parts.append(f"reference stale{age}")
    return " · ".join(parts)


def _hydrate_unit(row: Any) -> UnitCorrectnessRow:
    n_contains = int(row["n_contains"])
    n_contradicts = int(row["n_contradicts"])
    n_decided = n_contains + n_contradicts
    n_claims = int(row["n_claims"])
    correctness_share = _float_opt(row["correctness_share"])
    coverage_share = _float_opt(row["coverage_share"])
    single = single_family_row(
        row["families"], row["n_single_family"], row["n_claims"]
    )
    reference = _row_reference(row)
    return UnitCorrectnessRow(
        id=str(row["id"]),
        analyst_id=row["analyst_id"],
        target_id=row["target_id"],
        head_id=str(row["head_id"]),
        as_of=row["as_of"],
        rubric_sha=row["rubric_sha"],
        grain=row["grain"],
        n_claims=n_claims,
        n_contains=n_contains,
        n_contradicts=n_contradicts,
        n_silent=int(row["n_silent"]),
        n_split=int(row["n_split"]),
        n_unparseable=int(row["n_unparseable"]),
        n_single_family=int(row["n_single_family"]),
        n_decided=n_decided,
        correctness_share=correctness_share,
        coverage_share=coverage_share,
        single_family=single,
        badge=correctness_badge(
            correctness_share=correctness_share,
            coverage_share=coverage_share,
            n_contains=n_contains,
            n_decided=n_decided,
            n_claims=n_claims,
            as_of=row["as_of"],
            single_family=single,
            reference_state=reference.state,
            reference_age_days=reference.age_days,
        ),
        families=_jsonb(row["families"]),
        cost_usd=float(row["cost_usd"] or 0),
        created_at=row["created_at"],
        reference=reference,
    )


def _hydrate_claim(row: Any) -> UnitCorrectnessClaimRow:
    return UnitCorrectnessClaimRow(
        id=str(row["id"]),
        claim_id=row["claim_id"],
        grain=row["grain"],
        claim_text=row["claim_text"],
        label_by_family=_jsonb(row["label_by_family"]),
        adjudicated=row["adjudicated"],
        n_families=int(row["n_families"]),
        single_family=bool(row["single_family"]),
        spans=_jsonb(row["spans"]),
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_unit_correctness_router(deps: RegistryAPIDeps) -> APIRouter:
    """The G2 read route, bound to the registry deps.

    Included into `build_substrate_reads_router`'s router, so it inherits the
    `/api/v1` prefix and the `require_bearer` posture of the rest of the
    substrate-read surface.
    """
    router = APIRouter(tags=["substrate-reads"])

    @router.get(
        "/units/{target_id}/correctness",
        response_model=UnitCorrectnessPage,
    )
    async def unit_correctness(
        target_id: str,
        analyst_id: str | None = Query(
            default=None,
            description="Restrict to one bounded unit (one desk, or the composition).",
        ),
        grain: Grain | None = Query(
            default=None,
            description=(
                "`desk` or `composition`. The two grains are NEVER pooled; "
                "this filter is how a caller keeps them apart."
            ),
        ),
        claims: bool = Query(
            default=False,
            description=(
                "Attach the per-claim ledger to each row. Off by default: the "
                "badge needs the shares, the drawer needs the ledger."
            ),
        ),
        claim_limit: int = Query(default=DEFAULT_CLAIM_LIMIT),
        limit: int = Query(default=DEFAULT_UNIT_LIMIT),
        cursor: str | None = Query(default=None),
        principal: str = Depends(require_bearer),
    ) -> UnitCorrectnessPage:
        """The latest correctness row per unit for one target.

        A unit with no row is ABSENT from `data[]` — the read surface renders
        nothing for it. `reference.state` at page level carries the `none` /
        `stale` case so a target that was never graded can still say why.
        """
        limit = _validate(limit, maximum=MAX_UNIT_LIMIT, name="limit")
        claim_limit = _validate(
            claim_limit, maximum=MAX_CLAIM_LIMIT, name="claim_limit"
        )
        now = datetime.now(timezone.utc)

        where = ["uc.target_id = $1"]
        args: list[Any] = [target_id]
        if analyst_id is not None:
            args.append(analyst_id)
            where.append(f"uc.analyst_id = ${len(args)}")
        if grain is not None:
            args.append(grain)
            where.append(f"uc.grain = ${len(args)}")
        if cursor is not None:
            args.append(_decode_unit_cursor(cursor))
            where.append(f"uc.analyst_id > ${len(args)}")

        args.append(limit + 1)
        sql = _LATEST_PER_UNIT_SQL.format(
            where=" AND ".join(where), limit_param=f"${len(args)}"
        )

        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(sql, *args)
            ref_row = await conn.fetchrow(_NEWEST_REFERENCE_SQL, target_id)

            out = [_hydrate_unit(r) for r in rows[:limit]]
            if claims and out:
                claim_rows = await conn.fetch(
                    _CLAIMS_SQL, [r.id for r in out], claim_limit
                )
                by_unit: dict[str, list[UnitCorrectnessClaimRow]] = {
                    r.id: [] for r in out
                }
                for cr in claim_rows:
                    by_unit[str(cr["correctness_id"])].append(
                        _hydrate_claim(cr)
                    )
                for unit in out:
                    unit.claims = by_unit[unit.id]

        next_cursor: str | None = None
        if len(rows) > limit and out:
            next_cursor = _encode_unit_cursor(out[-1].analyst_id)

        return UnitCorrectnessPage(
            target_id=target_id,
            reference=_hydrate_reference(ref_row, now),
            data=out,
            next_cursor=next_cursor,
        )

    return router
