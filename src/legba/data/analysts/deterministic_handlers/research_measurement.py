# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``research_measurement`` sub-handler — the outbound-research program's three
counters (RESEARCH_PROGRAM_SPEC_2026-09-05 §4, train R-D).

THE POINT. The research program fetches pages off the open web and lands them
as ordinary ``signals`` rows carrying one distinguishing column,
``retrieval_origin = 'web_search:<component_id>'`` (§1.1). Without a
measurement organ that program is a self-watching loop: it would produce
evidence, cite its own evidence, and call the citation a result. These three
counters are what make it falsifiable.

  * **NOVELTY** — was the fetched evidence absent from the dispatching desk's
    slice at the moment of dispatch? (§4.1)
  * **CORROBORATION** — did a second, **non-research** source later carry the
    same observation? (§4.2)
  * **CONSEQUENCE** — did a *desk* cite it, and did that desk head reach the
    reader? (§4.3)

A GAUGE, NEVER A REPAIR. This handler COUNTS and NAMES. It performs no
``UPDATE``, writes no sidecar, lifts no ceiling and grades nothing with an LLM.
The ceiling-lift the spec's §1.4b describes is deliberately NOT here — see
"WHAT THIS HANDLER DELIBERATELY DOES NOT DO" below.

REFUSES LOUD. Like ``composition_lineage_sweep`` it requires a live
``deps.pg_pool``; a missing relation propagates rather than being swallowed
into a zeroed finding. A counter that reports 0 because it could not read is
strictly worse than no counter, because 0 is also the honest verified baseline
(229,945 signals, 100% NULL ``retrieval_origin`` at 2026-09-05) and the two
would be indistinguishable on the feed.

HONEST-NULL, NEVER ZERO. Every rate is published beside its ``n`` and is
``null`` with a machine-readable ``reason`` whenever it cannot be earned:

  =========================  ==============================================
  ``no_rows``                the window held no research signals
  ``n_below_minimum``        ``n < min_n`` — gate G9 (§6.3); the spec's own
                             power statement (§4, F-10) says regime-1 yield is
                             ~20-60 signals/week, so CORROBORATION and
                             CONSEQUENCE are UNDERPOWERED and must publish a
                             direction, never a verdict
  ``novelty_not_stamped``    rows exist but carry no write-time novelty block
                             — a MISSING field, which is not a 0% novelty rate
  ``window_not_matured``     CORROBORATION's 7-day forward window has not
                             closed for any row yet (§4.2)
  =========================  ==============================================

That table is the whole discipline: a field this handler cannot find reads as
*unknown*, never as *zero*. A rate of 0.0 in this finding always means "we
looked at n≥min_n rows and none qualified".

WHERE THE WRITE-TIME NOVELTY BLOCK LIVES. §4.1 pins it at
``payload.research.novelty`` and §1.2b repeats that shape. The R-D brief
described it as riding in ``raw_provenance``. Rather than guess at a lane
boundary, :data:`_NOVELTY_JSON` resolves THREE paths in order —
``payload.research.novelty``, ``raw_provenance.research.novelty``,
``raw_provenance.novelty`` — taking the first that is present. Whichever home
R-A actually stamps, the counter reads it; if R-A stamps none of them the rows
land in ``unstamped`` and the rate is ``null`` with ``novelty_not_stamped``.
The spec-pinned path is tried FIRST, so the pin is honoured and the fallbacks
are only a safety net.

WHAT "A DESK HEAD" MEANS, MECHANICALLY. Not a hardcoded list. A desk is an
``analyst_descriptors`` head carrying ``method.bounded_question`` — the field
§2.2 already relies on, deliberately absent on the composition tiers. Live at
2026-09-05 that predicate returns exactly the nine desks (disruption_status,
economic_coercion, energy_security, escalation, internal_stability,
leadership_transition, military_posture, narrative_coordination,
proliferation_watch) and excludes ``corpus_researcher`` for free — so §4.3's
"self-citation excluded" is a property of the definition rather than a special
case someone can forget to maintain.

THE CONSEQUENCE LADDER IS REACH, NOT EFFECT. ``c1 → c2 → c3`` ascends:
cited by anything → cited by a desk → that desk head was QUOTED into a
composition (``assembly.blocks[].spans[].origin.head_id``). None of the three
is a counterfactual. Whether a desk would have written something different
without the signal has no home in this substrate, and §4.3 forbids pretending
otherwise. The finding labels the ladder ``reach`` in its own data.

TWO PUBLICATION GRAINS, NEVER POOLED. ``by_target`` is keyed on the
**dispatching** target (``raw_provenance.dispatch.target_id``) and carries
NOVELTY + CORROBORATION; ``by_target_unit`` is keyed on the **citing** head's
``(target_id, unit)`` and carries CONSEQUENCE. They count different things
against different denominators and are published as two tables on purpose —
one merged table would silently divide a citing-desk count by a dispatch
denominator.

WHAT THIS HANDLER DELIBERATELY DOES NOT DO
------------------------------------------
* **No ceiling lift.** §1.4b's "``UPDATE`` the corroborated row's magnitude"
  is a WRITE, and this train is scoped read-only. It is not stubbed here: no
  function pretends to do it. Until it is built, ``corroboration.ceiling_lift``
  publishes ``{"performed": false, "reason": "not_built_read_only_gauge"}`` on
  every row, so a reader can never mistake the counter's silence for a lift
  that happened.
* **No ``retrieval_origin`` onto ``assembly.blocks[].signals[]``.** That is an
  edit to the composition payload, frozen for the R4 window. CONSEQUENCE's
  third rung therefore reads the span origin (``blocks[].spans[].origin.
  head_id``), which the assembly ALREADY writes, and asks whether the citing
  desk head is one a composition quoted. Same question, zero composition edits.
* **No LLM, no repair, no new ``OutputKind``.** §4.4: ``kind='finding'`` +
  ``analyst_id='research_measurement'``, the ``calibration_tracking``
  precedent, served by ``GET /api/v1/findings?analyst_id=research_measurement``
  with no API change (``substrate_reads_api.py`` takes ``analyst_id`` as a
  query parameter).

THE R4 STRATIFICATION (§4.5)
----------------------------
:func:`stratify_heads` is the importable half of this module: it splits desk
heads over a date range into ``has_research_evidence ∈ {true, false}`` so R4
reports its bars per arm and NEVER pools them. The SQL is
:data:`STRATIFY_HEADS_SQL`, reproduced here so the contract is readable
without opening a query planner::

    WITH desks AS (
        SELECT descriptor_id FROM analyst_descriptors
         WHERE is_head = TRUE
           AND COALESCE(state, 'active') <> 'retired'
           AND (body -> 'method' ->> 'bounded_question') IS NOT NULL
    ), research AS (
        SELECT s.id::text AS sid FROM signals s
         WHERE s.retrieval_origin LIKE 'web_search:%'
    ), heads AS (
        SELECT o.id, o.analyst_id, o.target_id, o.produced_at,
               CASE WHEN jsonb_typeof(o.data->'data'->'citations') = 'array'
                    THEN o.data->'data'->'citations' ELSE '[]'::jsonb END
                 AS citations
          FROM analyst_outputs o
          JOIN desks d ON d.descriptor_id = o.analyst_id
         WHERE o.kind = 'finding'
           AND o.produced_at >= $1 AND o.produced_at < $2
    )
    SELECT h.id::text AS head_id, h.analyst_id AS unit, h.target_id,
           h.produced_at, jsonb_array_length(h.citations) AS citation_count,
           EXISTS (SELECT 1 FROM jsonb_array_elements(h.citations) c
                     JOIN research rr ON rr.sid = lower(c ->> 'signal_id'))
             AS has_research_evidence
      FROM heads h ORDER BY h.produced_at LIMIT $3

Two deliberate choices in there. The research id set is materialised as TEXT
and joined, rather than casting each citation to ``uuid`` — a malformed
``signal_id`` in one citation would otherwise abort the whole R4 population
with a cast error, and the set is tiny (the partial index
``signals_retrieval_origin_idx … WHERE retrieval_origin IS NOT NULL`` serves
it). And a head with zero citations stays IN the ``false`` arm rather than
being dropped: it genuinely did not cite research evidence. ``citation_count``
rides along so R4 can see how much of the ``false`` arm cited nothing at all.

Live at 2026-09-05 over a 7-day range this returns 3,280 desk heads, 9 units,
38 targets, **all in the ``false`` arm** — the structurally-verified zero
baseline R-D is required to report before R-A merges (§7 R-D, acceptance 1).

Registered via a descriptor (``descriptors/analyst_research_measurement.yaml``,
``state: draft``) — daily, offset off the maintenance band and after the
midday composition tick so the day's assemblies are visible to c3.
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import timedelta
from typing import Any, Iterable, Mapping, Sequence

from ...provenance.models import FindingPayload
from ...retrieval_origin import WEB_SEARCH_PREFIX
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "research_measurement"

#: ``data.data.research_measurement.schema``. Bumped with any change to the
#: published counter shape — the ``assembly.v1`` / ``absence.v4`` discipline.
SCHEMA = "research_measurement.v1"

#: The program flag (§6.2, F-4). R-A owns writing it; R-D only READS it, so
#: the value is stamped on every counter row and a pooling reader can re-split
#: across the cutover retroactively (§4.5). Unset is reported as ``"unset"``
#: rather than as the documented default ``"off"``: before R-A lands there is
#: no write path at all, and claiming "off" would assert something about a path
#: that does not exist yet.
RESEARCH_EVIDENCE_ENV = "LEGBA_RESEARCH_EVIDENCE"
REGIME_UNSET = "unset"
KNOWN_REGIMES: tuple[str, ...] = ("off", "substrate", "desks")

# ---------------------------------------------------------------------------
# Knobs. Every one is declared in ``handler_options.HANDLER_OPTIONS`` (X-1);
# an undeclared knob is unreachable dead config, which is the defect that
# catalog closes.
# ---------------------------------------------------------------------------

#: Trailing window for NOVELTY + CONSEQUENCE, in days. 7 matches §4's own
#: windows and the ~20-60 signals/week yield the power statement is written
#: against.
DEFAULT_WINDOW_DAYS = 7

#: CORROBORATION's maturation lag (§4.2). A signal fetched today cannot be
#: scored until this many days have passed, so the counter reports on rows aged
#: ``[window+maturation, maturation)`` — 7-14 days at the defaults.
DEFAULT_MATURATION_DAYS = 7

#: How far BEFORE a research signal a non-research row may sit and still count
#: as the same observation. §4.2's own number.
DEFAULT_CORROBORATION_LOOKBACK_HOURS = 48

#: Gate G9 (§6.3): a rate with fewer than this many observations publishes as
#: ``null`` with a ``reason``, never as a number. This is the single knob that
#: keeps an underpowered week from reading as a result.
DEFAULT_MIN_N = 10

#: Distinct shared resolved entities required for the WEAK (entity-overlap)
#: corroboration arm. Never lifts a ceiling, never merged into the strong rate.
DEFAULT_WEAK_ENTITY_OVERLAP = 2

#: Bounded row sets — this handler shares a daily cadence with the maintenance
#: sweeps and must stay cheap. At regime-1 yield the research caps are ~100x
#: the expected population.
DEFAULT_ROW_CAP = 5000
DEFAULT_HEAD_CAP = 20000

#: Named samples in the finding body, so a reader gets ids and not just counts.
_SAMPLE_CAP = 10

#: How many per-day / per-target rows the published tables carry.
_TABLE_CAP = 60

_ORIGIN_LIKE = f"{WEB_SEARCH_PREFIX}%"

# ---------------------------------------------------------------------------
# THE SQL. One statement per counter, kept separate (rather than fused into one
# pass) so each is independently quotable, testable and explainable — §4 asks
# for three counters with three named denominators, not one join.
# ---------------------------------------------------------------------------

#: The write-time novelty block, resolved across its three candidate homes.
#: See "WHERE THE WRITE-TIME NOVELTY BLOCK LIVES" above. ``COALESCE`` over
#: jsonb returns the first NON-NULL, so an absent path costs nothing.
_NOVELTY_JSON = """COALESCE(s.payload -> 'research' -> 'novelty',
                            s.raw_provenance -> 'research' -> 'novelty',
                            s.raw_provenance -> 'novelty')"""

#: NOVELTY (§4.1) — one row per research signal in the window.
#:
#: ``novel`` and ``host_in_slice`` are read as jsonb so the caller can tell a
#: stamped ``false`` from an absent key: ``jsonb_typeof(...) = 'boolean'`` is
#: the presence test, and a missing block yields SQL NULL, which the Python
#: side counts as ``unstamped`` rather than as "not novel".
#:
#: ``folded_onto_curated`` is the F-9 companion: the share of research rows the
#: existing dedup plane later folds onto a PRE-EXISTING non-research signal.
#: novelty.v1 over-reports (same story, different outlet counts as novel); this
#: is the conservative number beside it, and the truth is between the two.
NOVELTY_SQL = f"""
SELECT s.id::text                                            AS signal_id,
       s.retrieval_origin                                    AS retrieval_origin,
       (s.raw_provenance -> 'dispatch' ->> 'target_id')       AS target_id,
       date_trunc('day', s.fetched_at)::date::text            AS day,
       {_NOVELTY_JSON} -> 'novel'                             AS novel,
       {_NOVELTY_JSON} -> 'host_in_slice'                     AS host_in_slice,
       {_NOVELTY_JSON} ->> 'scope'                            AS scope,
       {_NOVELTY_JSON} ->> 'version'                          AS novelty_version,
       EXISTS (SELECT 1 FROM signals c
                WHERE c.id = s.canonical_signal_id
                  AND c.id <> s.id
                  AND c.retrieval_origin IS NULL)             AS folded_onto_curated
FROM signals s
WHERE s.retrieval_origin LIKE $1
  AND s.fetched_at >= $2
  AND s.fetched_at <  $3
ORDER BY s.fetched_at
LIMIT $4
"""

#: CORROBORATION (§4.2) — one row per MATURED research signal, with each
#: anchor reported separately.
#:
#: Four anchors, three of them identity-grade and one deliberately weak:
#:
#: ``fold``   the canonical fold (``COALESCE(canonical_signal_id, id)``). The
#:            PRIMARY anchor, because ``cross_source_dedup`` /
#:            ``cross_source_coalesce`` already decide "these two rows are the
#:            same observation" and the whole substrate trusts that verdict.
#:            Reusing it makes CORROBORATION mean here what it means elsewhere.
#: ``url``    exact ``canonical_url`` equality with a non-research row. Also
#:            identity-grade: a curated feed independently carried the same
#:            page.
#: ``fact``   a ``facts`` row whose ``derived_from`` holds BOTH this signal and
#:            a non-research signal. This anchor is STRUCTURALLY ZERO under
#:            regime 1 and is published anyway, with that reason attached:
#:            §1.4c excludes research signals from ``fact_extractor`` until
#:            corroborated, so no research id can reach ``facts.derived_from``.
#:            A zero that is explained is a measurement; a zero that is merely
#:            absent is a hole.
#: ``entity`` >= N shared resolved entities plus geo overlap, inside the window.
#:            The WEAK companion §4.2 names. Reported as its own rate, NEVER
#:            unioned into the strong rate and NEVER used to lift a ceiling.
CORROBORATION_SQL = """
WITH r AS (
    SELECT s.id,
           COALESCE(s.canonical_signal_id, s.id)             AS fold,
           s.canonical_url,
           s.fetched_at,
           s.geo,
           s.retrieval_origin,
           (s.raw_provenance -> 'dispatch' ->> 'target_id')   AS target_id
    FROM signals s
    WHERE s.retrieval_origin LIKE $1
      AND s.fetched_at >= $2
      AND s.fetched_at <  $3
    ORDER BY s.fetched_at
    LIMIT $4
)
SELECT r.id::text                                            AS signal_id,
       r.retrieval_origin                                    AS retrieval_origin,
       r.target_id                                           AS target_id,
       date_trunc('day', r.fetched_at)::date::text           AS day,
       EXISTS (SELECT 1 FROM signals s2
                WHERE s2.retrieval_origin IS NULL
                  AND COALESCE(s2.canonical_signal_id, s2.id) = r.fold
                  AND s2.id <> r.id
                  AND s2.fetched_at >= r.fetched_at - make_interval(hours => $5)
                  AND s2.fetched_at <= r.fetched_at + make_interval(days => $6))
                                                             AS anchor_fold,
       (r.canonical_url IS NOT NULL AND EXISTS (
            SELECT 1 FROM signals s3
             WHERE s3.retrieval_origin IS NULL
               AND s3.canonical_url = r.canonical_url
               AND s3.id <> r.id
               AND s3.fetched_at >= r.fetched_at - make_interval(hours => $5)
               AND s3.fetched_at <= r.fetched_at + make_interval(days => $6)))
                                                             AS anchor_url,
       EXISTS (SELECT 1 FROM facts f
                WHERE f.derived_from @> ARRAY[r.id]
                  AND EXISTS (SELECT 1 FROM signals s4
                               WHERE s4.id = ANY (f.derived_from)
                                 AND s4.retrieval_origin IS NULL))
                                                             AS anchor_fact,
       EXISTS (SELECT 1 FROM signals s5
                WHERE s5.retrieval_origin IS NULL
                  AND s5.id <> r.id
                  AND s5.geo && r.geo
                  AND s5.fetched_at >= r.fetched_at - make_interval(hours => $5)
                  AND s5.fetched_at <= r.fetched_at + make_interval(days => $6)
                  AND (SELECT count(DISTINCT l2.entity_id)
                         FROM signal_entity_links l2
                        WHERE l2.signal_id = s5.id
                          AND l2.entity_id IN (
                              SELECT l1.entity_id FROM signal_entity_links l1
                               WHERE l1.signal_id = r.id)) >= $7)
                                                             AS anchor_entity
FROM r
"""

#: CONSEQUENCE (§4.3) — one row per research signal, carrying the three rungs.
#:
#: ``desks`` is the mechanical desk definition (``method.bounded_question``);
#: ``quoted`` is the set of head ids a composition QUOTED, read off the
#: assembly spans the composition already writes. Citation ids are compared as
#: lowercase TEXT against ``signals.id::text`` — never cast to ``uuid`` — so a
#: single malformed ``signal_id`` in one citation cannot abort the counter.
CONSEQUENCE_SQL = """
WITH desks AS (
    SELECT descriptor_id FROM analyst_descriptors
     WHERE is_head = TRUE
       AND COALESCE(state, 'active') <> 'retired'
       AND (body -> 'method' ->> 'bounded_question') IS NOT NULL
), r AS (
    SELECT s.id,
           s.retrieval_origin,
           (s.raw_provenance -> 'dispatch' ->> 'target_id') AS target_id,
           date_trunc('day', s.fetched_at)::date::text       AS day
    FROM signals s
    WHERE s.retrieval_origin LIKE $1
      AND s.fetched_at >= $2
      AND s.fetched_at <  $3
    ORDER BY s.fetched_at
    LIMIT $4
), cited AS (
    SELECT DISTINCT lower(c ->> 'signal_id')  AS signal_id,
           o.analyst_id                       AS unit,
           o.id::text                         AS head_id,
           o.target_id                        AS head_target_id
    FROM analyst_outputs o
    CROSS JOIN LATERAL jsonb_array_elements(o.data -> 'data' -> 'citations') c
    WHERE o.kind = 'finding'
      AND o.produced_at >= $2
      AND jsonb_typeof(o.data -> 'data' -> 'citations') = 'array'
      AND (c ->> 'signal_id') IS NOT NULL
), quoted AS (
    SELECT DISTINCT (sp -> 'origin' ->> 'head_id') AS head_id
    FROM analyst_outputs a
    CROSS JOIN LATERAL
        jsonb_array_elements(a.data -> 'data' -> 'assembly' -> 'blocks') b
    CROSS JOIN LATERAL jsonb_array_elements(b -> 'spans') sp
    WHERE a.kind = 'finding'
      AND a.produced_at >= $2
      AND jsonb_typeof(a.data -> 'data' -> 'assembly' -> 'blocks') = 'array'
      AND jsonb_typeof(b -> 'spans') = 'array'
)
SELECT r.id::text                       AS signal_id,
       r.retrieval_origin               AS retrieval_origin,
       r.target_id                      AS target_id,
       r.day                            AS day,
       EXISTS (SELECT 1 FROM cited ct
                WHERE ct.signal_id = r.id::text)              AS c1_cited_any,
       EXISTS (SELECT 1 FROM cited ct
                 JOIN desks d ON d.descriptor_id = ct.unit
                WHERE ct.signal_id = r.id::text)              AS c2_cited_by_desk,
       EXISTS (SELECT 1 FROM cited ct
                 JOIN desks d ON d.descriptor_id = ct.unit
                 JOIN quoted q ON q.head_id = ct.head_id
                WHERE ct.signal_id = r.id::text)              AS c3_quoted_head,
       COALESCE((SELECT array_agg(DISTINCT ct.unit || '␟'
                                  || COALESCE(ct.head_target_id, ''))
                   FROM cited ct
                   JOIN desks d ON d.descriptor_id = ct.unit
                  WHERE ct.signal_id = r.id::text), '{}')     AS desk_pairs
FROM r
"""

#: §4.5 — the R4 stratification. Reproduced in this module's docstring.
STRATIFY_HEADS_SQL = """
WITH desks AS (
    SELECT descriptor_id FROM analyst_descriptors
     WHERE is_head = TRUE
       AND COALESCE(state, 'active') <> 'retired'
       AND (body -> 'method' ->> 'bounded_question') IS NOT NULL
), research AS (
    SELECT s.id::text AS sid
      FROM signals s
     WHERE s.retrieval_origin LIKE $1
), heads AS (
    SELECT o.id, o.analyst_id, o.target_id, o.produced_at,
           CASE WHEN jsonb_typeof(o.data -> 'data' -> 'citations') = 'array'
                THEN o.data -> 'data' -> 'citations' ELSE '[]'::jsonb END
             AS citations
      FROM analyst_outputs o
      JOIN desks d ON d.descriptor_id = o.analyst_id
     WHERE o.kind = 'finding'
       AND o.produced_at >= $2
       AND o.produced_at <  $3
)
SELECT h.id::text                            AS head_id,
       h.analyst_id                          AS unit,
       h.target_id                           AS target_id,
       h.produced_at                         AS produced_at,
       jsonb_array_length(h.citations)       AS citation_count,
       EXISTS (SELECT 1 FROM jsonb_array_elements(h.citations) c
                 JOIN research rr ON rr.sid = lower(c ->> 'signal_id'))
                                             AS has_research_evidence
FROM heads h
ORDER BY h.produced_at
LIMIT $4
"""

# The separator packed into ``desk_pairs`` — U+241F SYMBOL FOR UNIT SEPARATOR.
# A literal control character would not survive a jsonb round-trip cleanly and
# a comma would collide with target ids; this one cannot appear in an
# ``analyst_id`` or a ``target_id``.
_PAIR_SEP = "␟"


# ---------------------------------------------------------------------------
# PURE ARITHMETIC — every function below is DB-free and takes plain mappings,
# which is what makes the counters unit-testable without a Postgres.
# ---------------------------------------------------------------------------


def rate(count: int, n: int, *, min_n: int, empty_reason: str = "no_rows") -> dict:
    """One published rate: the number, its ``n``, and WHY it is null.

    Gate G9 (§6.3) lives here and nowhere else. ``n == 0`` yields
    ``empty_reason`` so a caller can distinguish "no rows at all" from "rows
    exist but the field was never stamped"; ``0 < n < min_n`` yields
    ``n_below_minimum``. A returned ``rate`` is therefore always earned.
    """
    if n <= 0:
        return {"rate": None, "n": 0, "count": 0, "reason": empty_reason}
    if n < min_n:
        return {"rate": None, "n": n, "count": count, "reason": "n_below_minimum"}
    return {"rate": round(count / n, 4), "n": n, "count": count, "reason": None}


def _is_true(value: Any) -> bool:
    """A stamped jsonb boolean read as True. Anything else — including the
    string ``"true"`` from a sloppy writer — is handled by :func:`_tri`."""
    return value is True


def _tri(value: Any) -> bool | None:
    """Three-valued read of a stamped flag: True / False / **unknown**.

    ``None`` (the key was absent) stays ``None``. This is the function that
    keeps a missing write-time stamp out of the denominator instead of letting
    it read as ``False``.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "t"):
            return True
        if low in ("false", "f"):
            return False
    return None


def novelty_counters(rows: Sequence[Mapping[str, Any]], *, min_n: int) -> dict:
    """NOVELTY (§4.1) over write-time-stamped research signals.

    Three arms, published side by side and never merged (F-9):

    ``rate``          novelty.v1 — ``NOT (url_in_slice OR content_hash_in_slice)``
                      as R-A stamped it. **Over-reports**: same story, different
                      outlet counts as novel.
    ``v1b``           the stricter arm — novel AND the publisher's host was not
                      already in the desk's slice window. Free, because
                      ``host_in_slice`` is already one of §4.1's stamped inputs.
    ``near_dup``      the conservative companion — the share the existing dedup
                      plane later folded onto a PRE-EXISTING non-research row.

    The truth about novelty is between ``rate`` and ``1 - near_dup.rate``, and
    this handler says so rather than picking one.
    """
    scoped = [r for r in rows if (r.get("scope") or "dispatching_target")
              == "dispatching_target"]
    stamped = [r for r in scoped if _tri(r.get("novel")) is not None]
    unstamped = len(scoped) - len(stamped)
    novel = [r for r in stamped if _tri(r.get("novel")) is True]
    # v1b's denominator is the rows that stamped BOTH inputs — a row that
    # stamped `novel` but not `host_in_slice` cannot answer the stricter
    # question and is excluded rather than assumed.
    v1b_pool = [r for r in stamped if _tri(r.get("host_in_slice")) is not None]
    v1b_hits = [r for r in v1b_pool
                if _tri(r.get("novel")) is True
                and _tri(r.get("host_in_slice")) is False]
    empty = "novelty_not_stamped" if scoped and not stamped else "no_rows"

    versions = sorted({str(r.get("novelty_version")) for r in stamped
                       if r.get("novelty_version")})
    return {
        **rate(len(novel), len(stamped), min_n=min_n, empty_reason=empty),
        "unstamped": unstamped,
        "scope_filtered_out": len(rows) - len(scoped),
        "novelty_versions": versions,
        "v1b": {
            **rate(len(v1b_hits), len(v1b_pool), min_n=min_n,
                   empty_reason=empty),
            "definition": (
                "novel AND the canonical host was not already in the "
                "dispatching desk's slice window"
            ),
        },
        "near_dup": {
            **rate(sum(1 for r in rows if _is_true(r.get("folded_onto_curated"))),
                   len(rows), min_n=min_n),
            "definition": (
                "later folded by cross_source_dedup onto a PRE-EXISTING "
                "non-research signal"
            ),
        },
        "caveat": (
            "novelty.v1 matches exact canonical_url / content_hash only, so it "
            "OVER-REPORTS (F-9). Read it against near_dup; the truth is "
            "between them. Never merge the two."
        ),
    }


def corroboration_counters(
    rows: Sequence[Mapping[str, Any]],
    *,
    min_n: int,
    matured: bool,
) -> dict:
    """CORROBORATION (§4.2) over research signals whose forward window closed.

    ``matured`` is False when the maturation lag means no row can yet be
    scored; the rate then publishes ``null`` with ``window_not_matured``, which
    is the honest reading and NOT a 0% corroboration rate.
    """
    empty = "no_rows" if matured else "window_not_matured"
    strong = [r for r in rows
              if _is_true(r.get("anchor_fold")) or _is_true(r.get("anchor_url"))]
    weak = [r for r in rows if _is_true(r.get("anchor_entity"))]
    by_anchor = {
        name: sum(1 for r in rows if _is_true(r.get(f"anchor_{name}")))
        for name in ("fold", "url", "fact", "entity")
    }
    return {
        **rate(len(strong), len(rows), min_n=min_n, empty_reason=empty),
        "by_anchor": by_anchor,
        "anchors_strong": ["fold", "url"],
        "matured": matured,
        "weak": {
            **rate(len(weak), len(rows), min_n=min_n, empty_reason=empty),
            "definition": (
                "entity overlap + geo overlap inside the window — the WEAK "
                "companion. Never unioned into the strong rate, never used to "
                "lift a ceiling (§4.2)."
            ),
        },
        "anchor_fact_note": (
            "STRUCTURALLY ZERO under regime 1: §1.4c keeps research signals "
            "out of fact_extractor until corroborated, so no research id can "
            "reach facts.derived_from. Published so the zero is explained."
        ),
        "ceiling_lift": {
            "performed": False,
            "reason": "not_built_read_only_gauge",
            "note": (
                "§1.4b's magnitude UPDATE is a WRITE and is out of scope for "
                "this read-only gauge. Nothing here lifts a ceiling."
            ),
        },
    }


def consequence_counters(rows: Sequence[Mapping[str, Any]], *, min_n: int) -> dict:
    """CONSEQUENCE (§4.3) — the three-rung REACH ladder over research signals."""
    n = len(rows)
    rungs = {
        "c1_cited_any": sum(1 for r in rows if _is_true(r.get("c1_cited_any"))),
        "c2_cited_by_desk": sum(
            1 for r in rows if _is_true(r.get("c2_cited_by_desk"))),
        "c3_quoted_head": sum(
            1 for r in rows if _is_true(r.get("c3_quoted_head"))),
    }
    return {
        "n": n,
        **rungs,
        "c1": rate(rungs["c1_cited_any"], n, min_n=min_n),
        "c2": rate(rungs["c2_cited_by_desk"], n, min_n=min_n),
        "c3": rate(rungs["c3_quoted_head"], n, min_n=min_n),
        "ladder": "reach",
        "ladder_note": (
            "c1 cited by any finding -> c2 cited by a DESK head "
            "(method.bounded_question; excludes corpus_researcher by "
            "construction) -> c3 that desk head was QUOTED into a composition "
            "(assembly.blocks[].spans[].origin.head_id). An ascending ladder "
            "of REACH, never of EFFECT: the counterfactual has no home in "
            "this substrate (§4.3)."
        ),
    }


def _pairs(row: Mapping[str, Any]) -> list[tuple[str, str | None]]:
    """``desk_pairs`` unpacked into ``(unit, target_id)`` tuples."""
    out: list[tuple[str, str | None]] = []
    for packed in row.get("desk_pairs") or ():
        unit, _, target = str(packed).partition(_PAIR_SEP)
        if unit:
            out.append((unit, target or None))
    return out


def per_day(
    novelty_rows: Sequence[Mapping[str, Any]],
    consequence_rows: Sequence[Mapping[str, Any]],
    *,
    min_n: int,
) -> list[dict]:
    """The daily series. One entry per day that actually held a research
    signal — an absent day is absent, not a zero row."""
    days: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"signals": 0, "novel": 0, "novel_n": 0, "cited_by_desk": 0}
    )
    for r in novelty_rows:
        bucket = days[str(r.get("day"))]
        bucket["signals"] += 1
        if _tri(r.get("novel")) is not None:
            bucket["novel_n"] += 1
            if _tri(r.get("novel")) is True:
                bucket["novel"] += 1
    for r in consequence_rows:
        bucket = days[str(r.get("day"))]
        if _is_true(r.get("c2_cited_by_desk")):
            bucket["cited_by_desk"] += 1
    out = [
        {
            "day": day,
            "signals": b["signals"],
            "novelty": rate(b["novel"], b["novel_n"], min_n=min_n),
            "cited_by_desk": b["cited_by_desk"],
        }
        for day, b in sorted(days.items())
    ]
    return out[-_TABLE_CAP:]


def per_target(
    novelty_rows: Sequence[Mapping[str, Any]],
    corroboration_rows: Sequence[Mapping[str, Any]],
    *,
    min_n: int,
    matured: bool,
) -> list[dict]:
    """NOVELTY + CORROBORATION keyed on the **dispatching** target.

    A self-selected run has no dispatching target (§1.3, F-8) and therefore
    reaches no desk; those rows key on ``None`` and are reported under the
    literal ``"(self_selected)"`` rather than being folded into a real target.
    """
    keys: dict[str, dict[str, list]] = defaultdict(
        lambda: {"novelty": [], "corroboration": []}
    )
    for r in novelty_rows:
        keys[str(r.get("target_id") or "(self_selected)")]["novelty"].append(r)
    for r in corroboration_rows:
        keys[str(r.get("target_id") or "(self_selected)")][
            "corroboration"].append(r)
    out = [
        {
            "target_id": key,
            "signals": len(v["novelty"]),
            "novelty": novelty_counters(v["novelty"], min_n=min_n),
            "corroboration": corroboration_counters(
                v["corroboration"], min_n=min_n, matured=matured
            ),
        }
        for key, v in sorted(keys.items())
    ]
    return out[:_TABLE_CAP]


def per_target_unit(
    consequence_rows: Sequence[Mapping[str, Any]], *, min_n: int
) -> list[dict]:
    """CONSEQUENCE keyed on the **citing** head's ``(target_id, unit)``.

    A DIFFERENT grain from :func:`per_target` and published as its own table on
    purpose: the denominator here is "research signals this desk cited on this
    target", which cannot be divided by a dispatch denominator without lying.
    """
    keys: dict[tuple[str, str], set[str]] = defaultdict(set)
    quoted: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in consequence_rows:
        sid = str(r.get("signal_id"))
        for unit, target in _pairs(r):
            key = (str(target or "(none)"), unit)
            keys[key].add(sid)
            if _is_true(r.get("c3_quoted_head")):
                quoted[key].add(sid)
    out = [
        {
            "target_id": target,
            "unit": unit,
            "signals_cited": len(sids),
            "quoted_into_composition": len(quoted.get((target, unit), ())),
            "share_of_window": rate(
                len(sids), len(consequence_rows), min_n=min_n
            ),
        }
        for (target, unit), sids in sorted(keys.items())
    ]
    return out[:_TABLE_CAP]


def stratification(head_rows: Sequence[Mapping[str, Any]]) -> dict:
    """§4.5 — the two R4 arms, aggregated from :func:`stratify_heads`' rows.

    **It does not pool.** Every bar R4 reports must be reported per arm; that
    discipline is D-1 §5.2's ``regime`` split applied to a new axis, and
    without it a research-contaminated read and a curated read sit in one
    number and the program becomes unfalsifiable.
    """
    arms: dict[str, dict[str, Any]] = {
        "true": {"heads": [], "units": set(), "targets": set()},
        "false": {"heads": [], "units": set(), "targets": set()},
    }
    for row in head_rows:
        arm = arms["true" if _is_true(row.get("has_research_evidence"))
                   else "false"]
        arm["heads"].append(row)
        arm["units"].add(str(row.get("unit") or ""))
        if row.get("target_id"):
            arm["targets"].add(str(row.get("target_id")))
    return {
        "axis": "has_research_evidence",
        "pooled": False,
        "arms": {
            name: {
                "n_heads": len(v["heads"]),
                "n_uncited_heads": sum(
                    1 for h in v["heads"] if not int(h.get("citation_count") or 0)
                ),
                "n_units": len(v["units"] - {""}),
                "n_targets": len(v["targets"]),
            }
            for name, v in arms.items()
        },
        "note": (
            "R4 reports every bar per arm and NEVER pools them (§4.5). A head "
            "with zero citations stays in the false arm — it genuinely cited "
            "no research evidence — and n_uncited_heads says how much of that "
            "arm cited nothing at all."
        ),
    }


# ---------------------------------------------------------------------------
# OPTIONS
# ---------------------------------------------------------------------------


def _pos(raw: Any, default: int) -> int:
    """A positive-int knob, degrading to ``default`` on anything else.

    Every CALL SITE spells its key as a double-quoted literal inside the
    ``options`` lookup rather than passing the key through this helper —
    ``test_handler_options_x1`` scrapes the module SOURCE TEXT for exactly that
    literal and holds the X-1 catalog equal to what it finds, so a key resolved
    through a variable would be an undeclarable (and therefore permanently
    unreachable) knob. Which is also why this sentence does not spell one out.
    """
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _regime() -> dict[str, Any]:
    """The program flag as published on every counter row (§4.5, §6.2)."""
    raw = (os.environ.get(RESEARCH_EVIDENCE_ENV) or "").strip()
    value = raw or REGIME_UNSET
    return {
        "regime": value,
        "regime_known": value in KNOWN_REGIMES,
        "regime_env": RESEARCH_EVIDENCE_ENV,
    }


# ---------------------------------------------------------------------------
# THE FINDING
# ---------------------------------------------------------------------------


def _fmt(block: Mapping[str, Any]) -> str:
    """A rate for the body: the number, or the REASON it is null."""
    value = block.get("rate")
    if value is None:
        return f"null (n={block.get('n', 0)}, {block.get('reason') or 'unknown'})"
    return f"{float(value):.3f} (n={block.get('n', 0)})"


def build_finding(payload: Mapping[str, Any]) -> FindingPayload:
    """The published row. ``data.data.meta = true`` per §4.4, so no composition
    tier can ever read a measurement row back in as substrate."""
    signals = int(payload.get("signals_written") or 0)
    novelty = payload.get("novelty") or {}
    corroboration = payload.get("corroboration") or {}
    consequence = payload.get("consequence") or {}
    regime = str(payload.get("regime") or REGIME_UNSET)

    title = (
        f"Research measurement: {signals} research signals, "
        f"novelty {_fmt(novelty)}"
        if signals
        else "Research measurement: no research signals in window"
    )
    lines = [
        f"schema={SCHEMA}",
        f"regime={regime} (flag {RESEARCH_EVIDENCE_ENV})",
        f"window_days={payload.get('window_days')} "
        f"min_n={payload.get('min_n')}",
        f"signals_written={signals}",
        f"retrieval_origins={payload.get('retrieval_origins') or []}",
        "",
        f"NOVELTY        {_fmt(novelty)}"
        f"  v1b={_fmt(novelty.get('v1b') or {})}"
        f"  near_dup={_fmt(novelty.get('near_dup') or {})}"
        f"  unstamped={novelty.get('unstamped', 0)}",
        f"CORROBORATION  {_fmt(corroboration)}"
        f"  weak={_fmt(corroboration.get('weak') or {})}"
        f"  anchors={corroboration.get('by_anchor') or {}}",
        f"CONSEQUENCE    n={consequence.get('n', 0)}"
        f"  c1={_fmt(consequence.get('c1') or {})}"
        f"  c2={_fmt(consequence.get('c2') or {})}"
        f"  c3={_fmt(consequence.get('c3') or {})}",
        "",
        "The consequence ladder measures REACH, not effect (§4.3).",
        "novelty.v1 over-reports; read it against near_dup (F-9).",
    ]
    strat = payload.get("stratification") or {}
    arms = strat.get("arms") or {}
    if arms:
        lines.append("")
        lines.append("R4 arms (§4.5 — never pooled):")
        for name in ("true", "false"):
            arm = arms.get(name) or {}
            lines.append(
                f"  has_research_evidence={name}: heads={arm.get('n_heads', 0)} "
                f"units={arm.get('n_units', 0)} targets={arm.get('n_targets', 0)} "
                f"uncited={arm.get('n_uncited_heads', 0)}"
            )
    for row in (payload.get("per_day") or [])[-7:]:
        lines.append(
            f"  {row['day']}: signals={row['signals']} "
            f"novelty={_fmt(row['novelty'])} "
            f"cited_by_desk={row['cited_by_desk']}"
        )

    tags = ["deterministic", SUB_HANDLER_NAME, f"research_regime:{regime}"]
    tags.append(
        "research_signals_present" if signals else "research_signals_absent"
    )
    return FindingPayload(
        title=title[:2048],
        body="\n".join(lines)[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={
            # §4.4 — a measurement row is META and must never be read back in
            # as substrate by a composition tier (window_ledger.py:320,
            # composition_window.py:985 and meta_findings_synthesizer.py:1880
            # all exclude on exactly this key).
            "meta": True,
            "sub_handler": SUB_HANDLER_NAME,
            "research_measurement": dict(payload),
        },
    )


# ---------------------------------------------------------------------------
# THE READ
# ---------------------------------------------------------------------------


async def stratify_heads(
    conn: Any,
    *,
    since: Any,
    until: Any,
    head_cap: int = DEFAULT_HEAD_CAP,
) -> list[dict[str, Any]]:
    """§4.5 — desk heads over ``[since, until)`` split into the two R4 arms.

    Importable by the round harness: returns ONE dict per desk head with
    ``head_id`` / ``unit`` / ``target_id`` / ``produced_at`` /
    ``citation_count`` / ``has_research_evidence``, so R4 can join its own bars
    onto the arm label rather than re-deriving it. :func:`stratification`
    aggregates the same rows.
    """
    rows = await conn.fetch(
        STRATIFY_HEADS_SQL, _ORIGIN_LIKE, since, until, int(head_cap)
    )
    return [dict(r) for r in rows]


def _origins(*row_sets: Iterable[Mapping[str, Any]]) -> list[str]:
    """The distinct ``retrieval_origin`` values actually SEEN this window —
    the audit trail asking WHICH provider introduced WHICH claims."""
    seen: set[str] = set()
    for rows in row_sets:
        for row in rows:
            value = row.get("retrieval_origin")
            if value:
                seen.add(str(value))
    return sorted(seen)


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see the module docstring.

    REFUSES LOUD: requires a live ``deps.pg_pool``. A missing relation
    propagates rather than being swallowed into a zeroed finding, because zero
    is also the honest baseline and the two must never be confusable.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        raise RuntimeError(
            "research_measurement requires a live deps.pg_pool — refusing to "
            "publish zeroed research counters without reading the substrate; "
            "zero is also the verified baseline and the two must not be "
            "indistinguishable"
        )

    window_days = _pos(options.get("window_days"), DEFAULT_WINDOW_DAYS)
    maturation_days = _pos(
        options.get("maturation_days"), DEFAULT_MATURATION_DAYS
    )
    lookback_hours = _pos(
        options.get("corroboration_lookback_hours"),
        DEFAULT_CORROBORATION_LOOKBACK_HOURS,
    )
    min_n = _pos(options.get("min_n"), DEFAULT_MIN_N)
    overlap = _pos(
        options.get("weak_entity_overlap"), DEFAULT_WEAK_ENTITY_OVERLAP
    )
    row_cap = _pos(options.get("row_cap"), DEFAULT_ROW_CAP)
    head_cap = _pos(options.get("head_cap"), DEFAULT_HEAD_CAP)

    async with pool.acquire() as conn:
        now = await conn.fetchval("SELECT now()")
        since = now - timedelta(days=window_days)
        # CORROBORATION reports on the MATURED band only (§4.2): rows old
        # enough that their 7-day forward window has closed.
        matured_until = now - timedelta(days=maturation_days)
        matured_since = matured_until - timedelta(days=window_days)

        novelty_rows = [
            dict(r) for r in await conn.fetch(
                NOVELTY_SQL, _ORIGIN_LIKE, since, now, row_cap
            )
        ]
        corroboration_rows = [
            dict(r) for r in await conn.fetch(
                CORROBORATION_SQL,
                _ORIGIN_LIKE,
                matured_since,
                matured_until,
                row_cap,
                lookback_hours,
                maturation_days,
                overlap,
            )
        ]
        consequence_rows = [
            dict(r) for r in await conn.fetch(
                CONSEQUENCE_SQL, _ORIGIN_LIKE, since, now, row_cap
            )
        ]
        head_rows = await stratify_heads(
            conn, since=since, until=now, head_cap=head_cap
        )

    matured = bool(corroboration_rows)
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        **_regime(),
        "window_days": window_days,
        "maturation_days": maturation_days,
        "corroboration_lookback_hours": lookback_hours,
        "min_n": min_n,
        "weak_entity_overlap": overlap,
        "window": {"since": since.isoformat(), "until": now.isoformat()},
        "matured_window": {
            "since": matured_since.isoformat(),
            "until": matured_until.isoformat(),
        },
        "signals_written": len(novelty_rows),
        "retrieval_origins": _origins(
            novelty_rows, corroboration_rows, consequence_rows
        ),
        "novelty": novelty_counters(novelty_rows, min_n=min_n),
        "corroboration": corroboration_counters(
            corroboration_rows, min_n=min_n, matured=matured
        ),
        "consequence": consequence_counters(consequence_rows, min_n=min_n),
        "per_day": per_day(novelty_rows, consequence_rows, min_n=min_n),
        "by_target": per_target(
            novelty_rows, corroboration_rows, min_n=min_n, matured=matured
        ),
        "by_target_unit": per_target_unit(consequence_rows, min_n=min_n),
        "stratification": stratification(head_rows),
        "sample_signal_ids": [
            str(r.get("signal_id")) for r in novelty_rows[:_SAMPLE_CAP]
        ],
        "row_caps": {"rows": row_cap, "heads": head_cap},
        "caps_hit": {
            "novelty": len(novelty_rows) >= row_cap,
            "corroboration": len(corroboration_rows) >= row_cap,
            "consequence": len(consequence_rows) >= row_cap,
            "heads": len(head_rows) >= head_cap,
        },
    }

    logger.info(
        "research_measurement.swept signals=%d matured=%d heads=%d regime=%s",
        len(novelty_rows),
        len(corroboration_rows),
        len(head_rows),
        payload.get("regime"),
    )

    # §4.4 + the collection_gap idempotency precedent: an empty window is a
    # trace, not a feed row, so a quiet week never repeats "nothing to report".
    return AnalystMethodResult(
        finding=build_finding(payload),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        force_trace_only=not novelty_rows,
    )


__all__ = [
    "CONSEQUENCE_SQL",
    "CORROBORATION_SQL",
    "NOVELTY_SQL",
    "SCHEMA",
    "STRATIFY_HEADS_SQL",
    "SUB_HANDLER_NAME",
    "build_finding",
    "consequence_counters",
    "corroboration_counters",
    "handle",
    "novelty_counters",
    "per_day",
    "per_target",
    "per_target_unit",
    "rate",
    "stratification",
    "stratify_heads",
]
