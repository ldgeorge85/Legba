# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W-5 — the ``external_grades`` ledger: the writer, and the aggregations.

THE ONE NUMBER THIS PLATFORM LACKS is how TRUE what the reader reads this week
is. Everything else it measures is a consistency check against its own
citations. Migration 0190 gives external truth a DB home for the first time (the
only table that looked like one, ``correctness_labels``, holds eight rows
all-time from a single 2026-07-28 backfill batch); this module is the code side
of it — the append-only writer, and the aggregation that turns rows into a
number nobody can read wrong.

THE ARITHMETIC, AND WHY IT IS NOT A MEAN OVER ROWS::

    accuracy      = supported / (supported + contradicted)   -- None, never 0.0
    decided_rate  = (supported + contradicted) / searched     -- ALWAYS beside it

Three of the five verdicts score nothing and are excluded from numerator AND
denominator: ``NOT_FOUND`` (a statement about the SEARCH, never about the
world), ``UNCHECKED`` (the search plane did not answer), ``UNCHECKABLE``
(deterministically decided before any query — the claim has no world
truth-maker). That is exactly ``correctness_axis.WEIGHTS``' treatment of
``unresolvable``: the dict simply has no entry for it, and :func:`score` returns
``None`` rather than ``0.0`` when nothing is scorable. Reused, not re-derived.

**Why ``decided_rate`` is not optional.** Today's live 6-claim sweep decided TWO
of six — 0.33. An accuracy of 1.00 over two decided claims and an accuracy of
0.71 over four hundred are not the same object, and a dashboard showing only the
first will be read as the second. The decided rate is the instrument's own
coverage and it travels on every surface the accuracy travels on.

THREE POPULATIONS, NEVER POOLED. ``assembly_span`` (the record — grading it
grades the DESKS), ``assessment_sentence`` (the voice), ``legacy_prose`` (the
flag-off arm). Each carries its own n, its own decided rate, its own instrument
stamp. The world-facing headline is stated as a PAIR — *record 0.712 (n=1,148) ·
voice 0.68 (n=56)* — because they are two different acts of authorship, and
:func:`assert_not_pooled` is the enforcement point the way
``correctness_axis.assert_not_pooled`` is for the operator axis.

WHAT THIS INSTRUMENT CANNOT CLAIM (lifted verbatim into the API's honesty note):
it is web-dependent and the web is not a constant; ``NOT_FOUND`` is a statement
about the search; n per stratum will be small for a long time and any cell below
the floor publishes ``None`` and its n; it does not measure SALIENCE (the
"the world had a major development and we did not have it" failure is invisible
to a loop that only grades claims we made); it does not measure reader harm (a
false BLUF and a false footnote score identically — which is why ``span_role``
and ``block_ordinal`` ride every row so a position-weighted number can be
computed later against a REGISTERED rule rather than headlined now on an
invented one); it produces a RATE, not a mechanism; and a CONTRADICTED verdict
is one grader's reading of one span on one page, which is why two families must
agree before it pages.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, NamedTuple, Sequence

from .external_span_check import as_datetime as _as_iso_datetime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The axis
# ---------------------------------------------------------------------------

#: The axis' machine-readable name, used as a key prefix and in receipts.
AXIS_NAME = "external_truth"

#: Keys this axis owns. Any aggregate that is NOT the external-truth axis must
#: not carry them — see :func:`assert_not_pooled`. Mirrors
#: ``correctness_axis.AXIS_KEYS`` deliberately: the operator gold-set axis
#: learned this lesson first, and the reason is identical. An external-truth
#: number that leaks into a faithfulness or calibration aggregate describes a
#: population that never existed, and it does it silently.
AXIS_KEYS: tuple[str, ...] = (
    "external_truth",
    "external_accuracy",
    "n_external_decided",
    "n_external_searched",
    "external_decided_rate",
    "external_population",
    "external_instrument_limited",
)

#: Floor for a POPULATION's headline number. Below it the accuracy publishes
#: ``None`` and its n — never a number. Chosen to match
#: ``correctness_axis.MIN_FLEET_LABELS``; the point is that it is well above the
#: n a single day produces for the small tiers, not that 30 is magic.
MIN_DECIDED_POPULATION = 30

#: The same floor for one STRATUM cell. The world tier makes ~16 claims/day and
#: the Assessment ~8 fact claims/day, so sub-strata (regime x retrieval_origin x
#: severity) are honest-null for weeks and are SUPPOSED to be.
MIN_DECIDED_STRATUM = 10

VERDICT_SUPPORTED = "SUPPORTED"
VERDICT_CONTRADICTED = "CONTRADICTED"
VERDICT_NOT_FOUND = "NOT_FOUND"
VERDICT_UNCHECKED = "UNCHECKED"
VERDICT_UNCHECKABLE = "UNCHECKABLE"

VERDICTS: tuple[str, ...] = (
    VERDICT_SUPPORTED, VERDICT_CONTRADICTED, VERDICT_NOT_FOUND,
    VERDICT_UNCHECKED, VERDICT_UNCHECKABLE,
)

#: The two that score. Everything else is excluded from BOTH sides of the ratio.
DECISIVE_VERDICTS: frozenset[str] = frozenset(
    {VERDICT_SUPPORTED, VERDICT_CONTRADICTED}
)
#: "The search answered" — the decided-rate denominator. UNCHECKED and
#: UNCHECKABLE are not in it: nothing was measured in the first case, and
#: nothing COULD be measured in the second.
SEARCHED_VERDICTS: frozenset[str] = DECISIVE_VERDICTS | {VERDICT_NOT_FOUND}

POPULATION_ASSEMBLY_SPAN = "assembly_span"
POPULATION_ASSESSMENT_SENTENCE = "assessment_sentence"
POPULATION_LEGACY_PROSE = "legacy_prose"
POPULATIONS: tuple[str, ...] = (
    POPULATION_ASSEMBLY_SPAN,
    POPULATION_ASSESSMENT_SENTENCE,
    POPULATION_LEGACY_PROSE,
)

HONESTY_NOTE = (
    "This is an EXTERNAL check, not a faithfulness verdict. NOT_FOUND is a "
    "statement about the search, never about the world, and is excluded from "
    "both sides of the ratio — read `decided_rate` beside every accuracy. The "
    "web is not a constant: `search` carries the provider mix, the degraded "
    "share and the liveness-verified share, because a week when the engine set "
    "shrank is not a week the world got quieter. The `assembly_span` number is "
    "DESK-GRAIN truth: the assembly quotes its desks verbatim and authors "
    "nothing, so this measures the desks, not the composition tier. It does not "
    "measure salience (a development we never wrote about is invisible here), "
    "and it does not weight by position — a false BLUF and a false footnote "
    "score identically."
)


def assert_not_pooled(payload: Mapping[str, Any], *, what: str) -> None:
    """Raise if a NON-external-truth aggregate carries an external-truth key.

    The standing rule (design §0.4, §3.3): the three populations are never
    summed into one headline and the external number is never pooled into
    faithfulness, calibration, the Brier plane or the operator gold set. Prose
    said so for the operator axis and nothing enforced it until
    ``correctness_axis.assert_not_pooled`` existed; this is the same enforcement
    point for the same reason, called from the tests that guard the boundary and
    cheap enough for a writer that wants to be sure.
    """
    leaked = sorted(k for k in AXIS_KEYS if k in payload)
    if leaked:
        raise AssertionError(
            f"{what} carries external-truth key(s) {leaked}: the external "
            "truth axis is web-grounded and judge-independent and must never be "
            "pooled into a faithfulness / calibration / operator aggregate "
            "(EXTERNAL_GRADING_WIDTH_DESIGN §0.4)."
        )


def assert_populations_not_pooled(records: Sequence[Mapping[str, Any]]) -> None:
    """Raise if a published list carries two records for one population.

    The failure this catches is a summed headline wearing a population label —
    the exact shape §0.4 forbids, and the one a well-meaning "just give me one
    number" edit produces.
    """
    seen: set[str] = set()
    for record in records:
        population = str(record.get("population") or "")
        if population in seen:
            raise AssertionError(
                f"population {population!r} appears twice in one external-truth "
                "block: the three populations are three different acts of "
                "authorship and are never merged"
            )
        seen.add(population)


# ---------------------------------------------------------------------------
# THE PUBLISHED-DATE BOUNDARY — a search result's raw string, never a str at
# the INSERT (2026-09-05 23:07Z sweep: 14/40 writes rejected by asyncpg for
# `decisive_published_at`, precisely the dated — most informative — rows).
# ---------------------------------------------------------------------------

#: A search engine returns a publish date in whatever the SITE stated it in,
#: and this loop has now observed four distinct shapes on one sweep: a full
#: ISO date, a bare year-month, a long human form, and an "N units ago"
#: relative phrase. `migration 0190` types the column `timestamptz`; asyncpg
#: enforces that at the wire, not just at the DB, so a str reaching
#: :data:`INSERT_GRADE_SQL` is a guaranteed write failure — and the failure
#: mode is silent (``write_grade`` never raises), so the whole population of
#: dated rows was disappearing without a trace until the heartbeat's own
#: counts were read by hand.
PUBLISHED_PRECISION_DAY = "day"
PUBLISHED_PRECISION_MONTH = "month"
PUBLISHED_PRECISION_RELATIVE = "relative"
PUBLISHED_PRECISION_UNPARSED = "unparsed"

#: 'YYYY-MM' — a search snippet that only committed to a month. Resolved to
#: the first of the month; the precision tag is what tells a reader later that
#: the day-of-month is fabricated, not observed.
_MONTH_PRECISION_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")

#: 'N unit(s) ago' — resolved against the tick's own `graded_at`, never
#: against wall-clock `now()`, so a replay of an old tick's rows is
#: reproducible. Units beyond day/week are calendar-approximate on purpose
#: (30/365-day months/years): a search snippet saying "2 months ago" is
#: itself an approximation, and inventing calendar precision it never claimed
#: would be a second fabrication on top of the first.
_RELATIVE_AGO_RE = re.compile(
    r"^(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago$",
    re.IGNORECASE,
)
_RELATIVE_UNIT_SECONDS: dict[str, int] = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 7 * 86400,
    "month": 30 * 86400,
    "year": 365 * 86400,
}

#: Long human forms observed on the sweep ('Aug 20, 2026', 'Jan 1, 2026').
#: Both abbreviated and full month names, tried in order.
_LONG_FORM_STRPTIME: tuple[str, ...] = ("%b %d, %Y", "%B %d, %Y")


def parse_decisive_published_at(
    value: Any, *, reference: datetime | None = None
) -> tuple[datetime | None, str]:
    """Coerce a search result's raw published-date value to ``(dt, precision)``.

    Never raises — an unparseable or malformed value returns
    ``(None, "unparsed")`` rather than costing the row its write, exactly the
    contract :func:`write_grade` already holds for every other field.

    Handles, in order:
      * a real ``datetime`` (passed straight through, UTC-stamped if naive) —
        precision ``"day"``;
      * ``'YYYY-MM'`` month precision — resolved to the 1st, precision
        ``"month"``;
      * ``'N unit(s) ago'`` relative phrases — resolved against ``reference``
        (the tick's own ``graded_at`` when the caller has one, else
        wall-clock UTC), precision ``"relative"``;
      * full ISO-8601 (delegates to :func:`.external_span_check.as_datetime`,
        the same parser the evidence-window time gate already uses — one
        parser for "is this timestamp real", not two) — precision ``"day"``;
      * long human forms ('Aug 20, 2026', 'January 1, 2026') — precision
        ``"day"``;
      * anything else, including ``None``/empty — ``(None, "unparsed")``.
    """
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt, PUBLISHED_PRECISION_DAY
    if not isinstance(value, str):
        return None, PUBLISHED_PRECISION_UNPARSED
    raw = value.strip()
    if not raw:
        return None, PUBLISHED_PRECISION_UNPARSED

    month_match = _MONTH_PRECISION_RE.match(raw)
    if month_match:
        year, month = int(month_match.group(1)), int(month_match.group(2))
        return (
            datetime(year, month, 1, tzinfo=timezone.utc),
            PUBLISHED_PRECISION_MONTH,
        )

    relative_match = _RELATIVE_AGO_RE.match(raw)
    if relative_match:
        anchor = reference if reference is not None else datetime.now(timezone.utc)
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        amount = int(relative_match.group(1))
        unit = relative_match.group(2).lower()
        delta = timedelta(seconds=amount * _RELATIVE_UNIT_SECONDS[unit])
        return anchor - delta, PUBLISHED_PRECISION_RELATIVE

    parsed = _as_iso_datetime(raw)
    if parsed is not None:
        return parsed, PUBLISHED_PRECISION_DAY

    for fmt in _LONG_FORM_STRPTIME:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone.utc), PUBLISHED_PRECISION_DAY

    return None, PUBLISHED_PRECISION_UNPARSED


def _decisive_published_and_window(
    row: Mapping[str, Any], *, reference: datetime | None
) -> tuple[datetime | None, dict[str, Any]]:
    """The ``$22``/``$37`` pair: a real datetime|None, and the honest record.

    ``read_evidence_window`` already carries the READ's own admissible window
    (``{"earliest": ..., "latest": ...}`` — G-3's own stamp); the raw
    published-date string is nested under a ``decisive_published`` key rather
    than written at the top level, so this writer can never clobber that
    field even if a future window shape adds keys of its own.
    """
    window = dict(row.get("read_evidence_window") or {})
    raw = row.get("decisive_published_at")
    published_dt, precision = parse_decisive_published_at(raw, reference=reference)
    if raw not in (None, ""):
        window["decisive_published"] = {
            "published_raw": str(raw),
            "published_precision": precision,
        }
    return published_dt, window


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------

#: INSERT ... ON CONFLICT DO NOTHING on the (claim, pipeline, family) UNIQUE.
#: Re-running a tick is therefore idempotent AT THE DATABASE rather than in a
#: caller's memory, which matters because the drain's own progress row and the
#: ledger write are not one transaction: a tick that wrote rows and then failed
#: to save the queue re-grades those claims next tick, and this is what makes
#: that harmless instead of a double-count.
INSERT_GRADE_SQL = """
INSERT INTO external_grades (
    claim_key, population, graded_output_id, origin_head_id, block_ordinal,
    span_role, analyst_id, target_id, desk_key,
    claim_text, claim_severity, assembly_regime, scope_bounded, absence_shaped,
    verdict, uncheckable_class, unchecked_reason,
    decisive_url, decisive_span, decisive_span_sha256, decisive_source_tier,
    decisive_published_at, archive_ref, source_urls,
    search_provider, search_status, search_liveness, search_degraded,
    grader_family, grader_component_id, grader_model_name, grader_served_by,
    grader_pipeline_version, rubric_version, rater_role,
    retrieval_origin_mix, read_evidence_window, sample_fraction,
    graded_at, derived_from
) VALUES (
    $1, $2, $3::uuid, $4::uuid, $5,
    $6, $7, $8, $9,
    $10, $11, $12, $13, $14,
    $15, $16, $17,
    $18, $19, $20, $21,
    $22::timestamptz, $23, $24::text[],
    $25, $26, $27, $28,
    $29, $30, $31, $32,
    $33, $34, $35,
    $36::jsonb, $37::jsonb, $38,
    $39::timestamptz, $40::uuid[]
)
ON CONFLICT (claim_key, grader_pipeline_version, grader_family) DO NOTHING
"""

WINDOW_SQL = """
SELECT claim_key, population, graded_output_id, origin_head_id, block_ordinal,
       span_role, analyst_id, target_id, desk_key, claim_text, claim_severity,
       assembly_regime, scope_bounded, absence_shaped, verdict,
       uncheckable_class, unchecked_reason, decisive_url, decisive_span,
       decisive_source_tier, source_urls, search_provider, search_status,
       search_liveness, search_degraded, grader_family, grader_component_id,
       grader_model_name, grader_served_by, grader_pipeline_version,
       rubric_version, rater_role, retrieval_origin_mix, sample_fraction,
       graded_at
FROM external_grades
WHERE graded_at > $1
ORDER BY graded_at DESC
LIMIT $2
"""

#: A window read is bounded like every other polled surface in this tree.
WINDOW_FETCH_CAP = 20000


def _uuid_or_none(value: Any) -> Any:
    """Pass a real uuid straight through; anything unparsable becomes NULL.

    ``origin_head_id`` is genuinely nullable (a legacy-prose claim has no desk
    head), and a malformed id must not take the whole tick's write down — it
    must land as an honest NULL on a row that still records the verdict.
    """
    from uuid import UUID

    if value is None or isinstance(value, UUID):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return UUID(text)
    except (TypeError, ValueError):
        return None


#: :func:`_execute_write`'s three-way outcome. Only the first two mean a
#: matching row exists in the ledger after the call; ``_WRITE_FAILED`` means it
#: does not, and the caller needs to know WHY (``error_class``) to decide
#: whether the claim is worth retrying.
_WRITE_INSERTED = "inserted"
_WRITE_CONFLICT = "conflict"
_WRITE_FAILED = "failed"

#: The pre-INSERT validation refusal's ``error_class`` (no exception is
#: raised for it, so it needs a name of its own — see :func:`_execute_write`).
ERROR_CLASS_MISSING_GRADED_OUTPUT_ID = "missing_graded_output_id"


class WriteOutcome(NamedTuple):
    """One ledger row's write outcome, in :func:`write_grades`' input order.

    ``landed`` is True whenever the ledger holds a matching row after the
    call — a fresh INSERT or an idempotent ``ON CONFLICT DO NOTHING`` re-run of
    the same ``(claim_key, grader_pipeline_version, grader_family)`` triple.
    Only those two are NOT failures: a conflict means the claim is already
    graded, which is exactly as good as landing it here. ``error_class`` is
    ``""`` on a landed row; on a failure it names WHY the row does not exist —
    the INSERT exception's class name, or
    :data:`ERROR_CLASS_MISSING_GRADED_OUTPUT_ID` for the one pre-INSERT
    validation refusal — so a caller can requeue the claim with a reason
    instead of silently losing it.
    """

    claim_key: str
    landed: bool
    error_class: str = ""


async def _execute_write(
    conn: Any,
    row: Mapping[str, Any],
    *,
    pipeline_version: str,
    graded_at: Any = None,
    derived_from: Sequence[Any] = (),
) -> tuple[str, str]:
    """Execute ONE grade's INSERT. Returns ``(outcome, error_class)``.

    The single place both :func:`write_grade` and :func:`write_grades` execute
    the INSERT and classify what happened, so their notions of "did this row
    land" can never drift apart. Never raises: one malformed grade must not
    cost the tick its other rows or its heartbeat, which is the row that
    proves the auditor is alive at all.
    """
    graded_output_id = _uuid_or_none(row.get("graded_output_id"))
    if graded_output_id is None:
        logger.warning(
            "external_grades.write_skipped claim=%s — no graded_output_id; a "
            "grade that cannot name the read it graded is not a grade",
            str(row.get("claim_key") or "")[:16],
        )
        return _WRITE_FAILED, ERROR_CLASS_MISSING_GRADED_OUTPUT_ID
    reference = graded_at if isinstance(graded_at, datetime) else None
    decisive_published_dt, read_evidence_window = _decisive_published_and_window(
        row, reference=reference,
    )
    try:
        result = await conn.execute(
            INSERT_GRADE_SQL,
            str(row.get("claim_key") or ""),
            str(row.get("population") or POPULATION_LEGACY_PROSE),
            graded_output_id,
            _uuid_or_none(row.get("origin_head_id")),
            row.get("block_ordinal"),
            row.get("span_role"),
            str(row.get("analyst_id") or ""),
            row.get("target_id"),
            row.get("desk_key"),
            str(row.get("claim_text") or ""),
            row.get("claim_severity"),
            str(row.get("assembly_regime") or "legacy"),
            bool(row.get("scope_bounded")),
            bool(row.get("absence_shaped")),
            str(row.get("verdict") or ""),
            row.get("uncheckable_class"),
            row.get("unchecked_reason"),
            row.get("decisive_url"),
            row.get("decisive_span"),
            row.get("decisive_span_sha256"),
            row.get("decisive_source_tier"),
            decisive_published_dt,
            row.get("archive_ref"),
            [str(u) for u in (row.get("source_urls") or [])],
            row.get("search_provider"),
            row.get("search_status"),
            row.get("search_liveness"),
            bool(row.get("search_degraded")),
            str(row.get("grader_family") or ""),
            str(row.get("grader_component_id") or ""),
            row.get("grader_model_name"),
            row.get("grader_served_by"),
            str(pipeline_version),
            str(row.get("rubric_version") or ""),
            str(row.get("rater_role") or "primary"),
            json.dumps(dict(row.get("retrieval_origin_mix") or {})),
            json.dumps(read_evidence_window),
            float(row.get("sample_fraction") or 1.0),
            graded_at,
            [_uuid_or_none(d) for d in derived_from if _uuid_or_none(d)],
        )
    except Exception as exc:
        logger.warning(
            "external_grades.write_failed claim=%s err=%s",
            str(row.get("claim_key") or "")[:16], exc,
        )
        return _WRITE_FAILED, type(exc).__name__
    # asyncpg returns 'INSERT 0 1' on a landed row and 'INSERT 0 0' on a
    # DO NOTHING conflict. The distinction is the idempotency receipt.
    if str(result).strip().endswith(" 1"):
        return _WRITE_INSERTED, ""
    return _WRITE_CONFLICT, ""


async def write_grade(
    conn: Any,
    row: Mapping[str, Any],
    *,
    pipeline_version: str,
    graded_at: Any = None,
    derived_from: Sequence[Any] = (),
) -> bool:
    """Append ONE grade. Returns True when a FRESH row landed.

    False covers both an idempotent conflict (the row was already there) and a
    genuine failure — this function's contract predates the per-row
    landed/failed distinction :func:`write_grades` now reports, and is left
    unchanged so existing callers keep their exact boolean meaning. Never
    raises: see :func:`_execute_write`.
    """
    outcome, _ = await _execute_write(
        conn, row, pipeline_version=pipeline_version,
        graded_at=graded_at, derived_from=derived_from,
    )
    return outcome == _WRITE_INSERTED


async def write_grades(
    conn: Any,
    rows: Iterable[Mapping[str, Any]],
    *,
    pipeline_version: str,
    graded_at: Any = None,
    derived_from: Sequence[Any] = (),
) -> tuple[int, int, list[WriteOutcome]]:
    """Append many grades. Returns ``(written, skipped, outcomes)``.

    ``written``/``skipped`` are byte-identical in meaning to before this
    function grew a third return value: ``skipped`` counts BOTH conflicts and
    failures on purpose, because from a pure counter's point of view they are
    the same fact — this tick did not add that row — and splitting them there
    would invite a heartbeat that reports a healthy write rate while every row
    conflicts.

    ``outcomes`` is the per-row detail that counter collapses: one
    :class:`WriteOutcome` per input row, in the SAME order the rows were
    given, so a caller can ``zip()`` it back against whatever it built those
    rows from (e.g. the ``WidthGrade`` objects a row dict was rendered from)
    to tell a landed claim from a failed one — which is what lets a failed
    write be requeued instead of silently dropped from the day's population.
    """
    written = skipped = 0
    outcomes: list[WriteOutcome] = []
    for row in rows:
        key = str(row.get("claim_key") or "")
        outcome, error_class = await _execute_write(
            conn, row, pipeline_version=pipeline_version,
            graded_at=graded_at, derived_from=derived_from,
        )
        if outcome == _WRITE_INSERTED:
            written += 1
            outcomes.append(WriteOutcome(claim_key=key, landed=True))
        elif outcome == _WRITE_CONFLICT:
            skipped += 1
            outcomes.append(WriteOutcome(claim_key=key, landed=True))
        else:
            skipped += 1
            outcomes.append(
                WriteOutcome(claim_key=key, landed=False, error_class=error_class)
            )
    return written, skipped, outcomes


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


def score(
    verdicts: Iterable[str], *, min_decided: int = MIN_DECIDED_POPULATION
) -> dict[str, Any]:
    """Score one population of external verdicts.

    Returns the axis record::

        accuracy        supported / (supported + contradicted), or None when
                        nothing is decided or the n floor is not cleared. NEVER
                        0.0 — a real 0.0 means every decided claim was
                        CONTRADICTED, which is a very different sentence.
        n_decided       supported + contradicted
        n_searched      + not_found: what the search actually reached
        n_unchecked     the search plane did not answer
        n_uncheckable   deterministically excluded before any query
        decided_rate    n_decided / n_searched, or None over zero searched
        mix             full per-verdict counts (the tiny-n display)
        sufficient      n_decided >= min_decided
        status          a sentence naming the n — a number is never bare
    """
    mix = {v: 0 for v in VERDICTS}
    unknown = 0
    for raw in verdicts:
        verdict = str(raw)
        if verdict in mix:
            mix[verdict] += 1
        else:
            # Only reachable if the DB CHECK is ever loosened. Counted so it is
            # visible, never scored: an unrecognised verdict is not a verdict.
            unknown += 1
    supported = mix[VERDICT_SUPPORTED]
    contradicted = mix[VERDICT_CONTRADICTED]
    n_decided = supported + contradicted
    n_searched = n_decided + mix[VERDICT_NOT_FOUND]
    sufficient = n_decided >= int(min_decided)
    accuracy = (
        supported / n_decided if (n_decided and sufficient) else None
    )
    decided_rate = (n_decided / n_searched) if n_searched else None

    if not any(mix.values()):
        status = "no external verdicts"
    elif n_searched == 0:
        status = "nothing reached the world — every claim was unchecked or uncheckable"
    elif n_decided == 0:
        status = f"searched (n={n_searched}) and nothing was decided"
    elif sufficient:
        status = f"decided (n={n_decided} of {n_searched} searched)"
    else:
        status = (
            f"indicative only — n={n_decided} decided claim"
            f"{'' if n_decided == 1 else 's'}, below the {int(min_decided)} floor"
        )

    return {
        "accuracy": accuracy,
        "supported": supported,
        "contradicted": contradicted,
        "n_decided": n_decided,
        "n_searched": n_searched,
        "n_unchecked": mix[VERDICT_UNCHECKED],
        "n_uncheckable": mix[VERDICT_UNCHECKABLE],
        "n_unknown_verdict": unknown,
        "decided_rate": decided_rate,
        "mix": mix,
        "sufficient": sufficient,
        "min_decided": int(min_decided),
        "status": status,
    }


def describe(record: Mapping[str, Any]) -> str:
    """One human line for a :func:`score` record — never a bare ratio."""
    value = record.get("accuracy")
    n_decided = int(record.get("n_decided") or 0)
    rate = record.get("decided_rate")
    rate_text = "decided_rate unmeasured" if rate is None else f"decided_rate {rate:.2f}"
    if value is None:
        return f"external accuracy unmeasured — {record.get('status')} · {rate_text}"
    return (
        f"external accuracy {float(value):.3f} (n={n_decided} decided: "
        f"{int(record.get('supported') or 0)} supported / "
        f"{int(record.get('contradicted') or 0)} contradicted) · {rate_text} — "
        f"{record.get('status')}"
    )


def as_payload(record: Mapping[str, Any], *, population: str) -> dict[str, Any]:
    """Project a :func:`score` record onto the axis' PUBLIC key names.

    One place decides what the external-truth axis is called on the wire, so the
    ops route, the v3 eval block and the badge cannot drift into three spellings
    of the same number.
    """
    return {
        "external_truth": True,
        "external_population": population,
        "external_accuracy": record.get("accuracy"),
        "n_external_decided": int(record.get("n_decided") or 0),
        "n_external_searched": int(record.get("n_searched") or 0),
        "external_decided_rate": record.get("decided_rate"),
    }


# ---------------------------------------------------------------------------
# Strata
# ---------------------------------------------------------------------------

#: The stratum axes, in the order they are published. Each is a FUNCTION of a
#: row, so a new axis is a one-line addition and never a second aggregation.
STRATUM_AXES: tuple[str, ...] = (
    "regime", "retrieval_origin", "severity", "claim_shape", "tier",
    "grader_family", "tier_class",
)


def _stratum_value(axis: str, row: Mapping[str, Any]) -> str:
    if axis == "regime":
        return str(row.get("assembly_regime") or "legacy")
    if axis == "retrieval_origin":
        mix = row.get("retrieval_origin_mix")
        if isinstance(mix, str):
            try:
                mix = json.loads(mix)
            except Exception:
                mix = {}
        if isinstance(mix, Mapping) and mix:
            # The dominant origin. Honest-null today: `retrieval_origin` is NULL
            # on every signal fetched in the last 7 days, so this stratum ships
            # EMPTY and fills itself the day R-A's write path lands.
            return max(mix.items(), key=lambda kv: (float(kv[1] or 0), kv[0]))[0]
        return "unlabelled"
    if axis == "severity":
        return str(row.get("claim_severity") or "unstated")
    if axis == "claim_shape":
        return "absence" if row.get("absence_shaped") else "positive"
    if axis == "tier":
        return str(row.get("analyst_id") or "unknown")
    if axis == "grader_family":
        return str(row.get("grader_family") or "unknown")
    if axis == "tier_class":
        # F-3 AS RULED: a VISIBLE class, counted separately. A decisive verdict
        # whose decisive domain is not in the source register is NOT silently
        # suppressed — it is published here, where somebody can argue about it.
        verdict = str(row.get("verdict") or "")
        if verdict not in DECISIVE_VERDICTS:
            return "not_decisive"
        tier = row.get("decisive_source_tier")
        if tier is None:
            return "tier_unknown"
        return f"tier_{int(tier)}"
    return "unknown"


def strata(
    rows: Sequence[Mapping[str, Any]], *, min_decided: int = MIN_DECIDED_STRATUM
) -> dict[str, dict[str, dict[str, Any]]]:
    """Every stratum axis, each cell scored with its own n and its own floor.

    A cell below the floor publishes ``None`` and its n. That is not a
    limitation to apologise for: the world tier makes ~110 claims a week and the
    Assessment ~56, so most sub-strata are honest-null for weeks and a number
    there would be noise wearing a decimal point.
    """
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for axis in STRATUM_AXES:
        buckets: dict[str, list[str]] = {}
        for row in rows:
            buckets.setdefault(_stratum_value(axis, row), []).append(
                str(row.get("verdict") or "")
            )
        out[axis] = {
            key: score(verdicts, min_decided=min_decided)
            for key, verdicts in sorted(buckets.items())
        }
    return out


# ---------------------------------------------------------------------------
# The instrument's own health
# ---------------------------------------------------------------------------


def instrument(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The rolling raw overlap between the primary grader and the audit rater.

    R3 died at raw overlap 0.7451 < 0.75 on 51 shared items, after R2 called its
    0.804 "the round's methodological result". It did not replicate. A standing
    instrument that cannot report its own reliability every week has learned
    nothing from that — so this is computed on the same three-verdict decisive
    vocabulary the rounds published (0.774 / 0.804 / 0.7451), which is what makes
    the standing number directly comparable to them.

    ``instrument_limited`` is TRUE below 0.75, and when it is true every number
    published that week renders as a SENTENCE rather than a number.
    """
    primary: dict[str, str] = {}
    audit: dict[str, str] = {}
    families: set[str] = set()
    versions: set[str] = set()
    for row in rows:
        key = str(row.get("claim_key") or "")
        verdict = str(row.get("verdict") or "")
        families.add(str(row.get("grader_family") or ""))
        versions.add(str(row.get("grader_pipeline_version") or ""))
        if str(row.get("rater_role") or "") == "audit":
            audit[key] = verdict
        else:
            primary[key] = verdict
    pairs = [
        (primary[key], audit[key]) for key in sorted(set(primary) & set(audit))
    ]
    scored = [
        (a, b) for a, b in pairs
        if a in SEARCHED_VERDICTS and b in SEARCHED_VERDICTS
    ]
    overlap = (
        sum(1 for a, b in scored if a == b) / len(scored) if scored else None
    )
    if overlap is None:
        band = "unmeasured"
    elif overlap >= 0.80:
        band = "stands"
    elif overlap >= 0.75:
        band = "contingent"
    else:
        band = "instrument_limited"
    return {
        "overlap_raw": overlap,
        "overlap_n": len(scored),
        "band": band,
        # Never True on an UNMEASURED overlap: "the graders did not agree" and
        # "we did not ask a second grader" are different failures, and folding
        # them together would let an unwired audit rater mute the whole surface.
        "instrument_limited": band == "instrument_limited",
        "graders": sorted(f for f in families if f),
        "pipeline_versions": sorted(v for v in versions if v),
    }


def search_health(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The search plane's own condition, published beside every number.

    The web is not a constant. A week when SearXNG lost eight engines is not a
    week the world got quieter, and a headline that moves with engine health
    while claiming to be about the world is the pooling the D5 stamp doctrine
    forbids — a key that splits on code rather than on the condition that
    changed.
    """
    total = 0
    degraded = 0
    verified = 0
    providers: dict[str, int] = {}
    for row in rows:
        if str(row.get("verdict") or "") not in SEARCHED_VERDICTS:
            continue
        total += 1
        if row.get("search_degraded"):
            degraded += 1
        if str(row.get("search_liveness") or "") == "verified":
            verified += 1
        provider = str(row.get("search_provider") or "unknown")
        providers[provider] = providers.get(provider, 0) + 1
    return {
        "n": total,
        "provider_mix": (
            {k: round(v / total, 4) for k, v in sorted(providers.items())}
            if total else {}
        ),
        "degraded_share": (degraded / total) if total else None,
        "liveness_verified_share": (verified / total) if total else None,
    }


def sampling(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """How much of the population these rows actually are.

    A PARTIAL DAY IS NEVER PUBLISHED AS A WHOLE ONE. When the day's budget could
    not cover the population the loop degraded to a hash-gated sample and every
    row carries the fraction; this reports the minimum fraction seen and how many
    rows came from a sampled day, so the reader is told the denominator is
    smaller than the population rather than left to assume it is not.
    """
    fractions = [float(r.get("sample_fraction") or 1.0) for r in rows]
    sampled = [f for f in fractions if f < 1.0]
    return {
        "sample_fraction": min(fractions) if fractions else 1.0,
        "sampled_rows": len(sampled),
        "complete": not sampled,
        "reason": (
            None if not sampled
            else "the day's claim or SERP budget could not cover the population"
        ),
    }


def aggregate(
    rows: Sequence[Mapping[str, Any]],
    *,
    window_days: int,
    min_decided: int = MIN_DECIDED_POPULATION,
) -> dict[str, Any]:
    """The ``external_truth`` block — one record per population, never pooled.

    Only ``rater_role='primary'`` rows enter a population's numbers. The audit
    rater's rows exist to measure the INSTRUMENT, and letting them into the
    accuracy would mean the 10% double-graded claims counted twice while the
    other 90% counted once — a weighting nobody chose.
    """
    primary_rows = [
        r for r in rows if str(r.get("rater_role") or "primary") != "audit"
    ]
    records: list[dict[str, Any]] = []
    for population in POPULATIONS:
        subset = [
            r for r in primary_rows
            if str(r.get("population") or "") == population
        ]
        if not subset:
            continue
        record = score(
            (str(r.get("verdict") or "") for r in subset),
            min_decided=min_decided,
        )
        pop_rows = [r for r in rows if str(r.get("population") or "") == population]
        record["population"] = population
        record["strata"] = strata(subset)
        record["instrument"] = instrument(pop_rows)
        record["search"] = search_health(subset)
        record["sampled"] = sampling(subset)
        records.append(record)
    assert_populations_not_pooled(records)
    return {
        "window": {"days": int(window_days)},
        "populations": records,
        "honesty_note": HONESTY_NOTE,
    }


def coerce_rows(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """Normalise DB records (or dicts) into plain mappings for the pure code."""
    return [dict(r) for r in rows]


__all__ = [
    "AXIS_KEYS",
    "AXIS_NAME",
    "DECISIVE_VERDICTS",
    "ERROR_CLASS_MISSING_GRADED_OUTPUT_ID",
    "HONESTY_NOTE",
    "INSERT_GRADE_SQL",
    "MIN_DECIDED_POPULATION",
    "MIN_DECIDED_STRATUM",
    "POPULATIONS",
    "POPULATION_ASSEMBLY_SPAN",
    "POPULATION_ASSESSMENT_SENTENCE",
    "POPULATION_LEGACY_PROSE",
    "PUBLISHED_PRECISION_DAY",
    "PUBLISHED_PRECISION_MONTH",
    "PUBLISHED_PRECISION_RELATIVE",
    "PUBLISHED_PRECISION_UNPARSED",
    "SEARCHED_VERDICTS",
    "STRATUM_AXES",
    "VERDICTS",
    "WINDOW_FETCH_CAP",
    "WINDOW_SQL",
    "WriteOutcome",
    "aggregate",
    "as_payload",
    "assert_not_pooled",
    "assert_populations_not_pooled",
    "coerce_rows",
    "describe",
    "instrument",
    "parse_decisive_published_at",
    "sampling",
    "score",
    "search_health",
    "strata",
    "write_grade",
    "write_grades",
]
