# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A-2 — THE TWO DIFFS: reference-vs-slice, and reference-vs-output.

Both are deterministic, ``$0``, no LLM, and run in the same sweep immediately
after :mod:`desk_reference` writes the day's references. Both follow the
coverage-floor architecture: **SQL fetches, Python folds and decides.**

TWO QUESTIONS, TWO DENOMINATORS, AND THAT SEPARATION IS THE WHOLE DESIGN
------------------------------------------------------------------------
``collection_recall = collected / anchorable`` — *did our collection have it?*
``attention_rate    = engaged  / collected``   — *did the desk engage it?*

You cannot be blind to what you never had. On 2026-09-05 ``country_g20_jp`` ->
North Korea measured **0.3** slice rows per 120-row slice and 0 of 7 desks
engaging; scoring that as an attention failure would misdirect exactly as the
coverage-floor record warned. It is a REFERENCE gap and it belongs to the
research program (A-4, not built here). Meanwhile ``country_watch_il`` -> Iran
measured **9 of 120** slice rows and **0 of 8** desks — the desks HAD it. Those
are different failures and a single ratio cannot tell them apart.

THE ITEM KEY, AND ``unanchorable``
----------------------------------
``folds(item) = { identity_fold(canonicalize_entity(e)) for e in item.entities
                  if not is_junk_entity(e) and not is_home_country(e, home) }``

An item with an EMPTY key set after junk-and-home filtering is
``unanchorable`` and is excluded from BOTH the numerator and the denominator of
BOTH metrics, counted separately in the receipt. This is the SA/Yemen lesson
made structural: a polity-only matcher against a register that names the ACTOR
("Houthi maritime embargo") reported a gap where the desks were in fact engaging
4 of 8, and that detector shipped at outcome precision 2 of 3 saying so. **A
matcher that cannot name the thing does not get to score it.**

THE SLICE SIDE IS ``input_row_refs``, NOT ``prompt_rendered``
------------------------------------------------------------
``analyst_traces.input_row_refs`` is a flat ``uuid[]`` of the signal ids
``_orient`` packed, joined to ``signals``. ``prompt_rendered`` is capped at
``_MAX_PROMPT_RENDERED_CHARS = 32,000`` and a verbose desk's system prompt
consumes the whole cap before a single numbered signal is stored (#83). This is
the same fallback the IL diagnosis was forced into, and it is the right one: it
is what the desk actually CONSUMED, not what the trace happened to keep.

THE OUTPUT SIDE IS ``title`` + ``body`` ONLY
--------------------------------------------
Never ``data.citations[].source_text``. A naive whole-JSON grep over the head
picks up the quoted source text and reports a desk as naming something its own
prose never mentions — it produced the one factual error the IL diagnosis had to
correct in R3's record.

THREE ENGAGEMENT ARMS, RECORDED SEPARATELY, NEVER OR-ED INTO ONE OPAQUE BIT
---------------------------------------------------------------------------
``E1 NAMED`` (the item's prose surface appears in the head) and ``E2 CITED``
(the head cited a signal the collection diff matched to this item) are both
ENGAGEMENT. ``E3 FRAMED`` — an open ``situations`` row for the target names it —
is **NOT** engagement: it is the register's LICENCE, recorded as a covariate,
because it is the CAUSE R-1 will move rather than the effect. Live on 09-05 the
three separate cleanly: TR carries one cited-but-unnamed desk, which is the
whole difference between "the desk saw it" and "the desk said it" and vanishes
the moment E1 and E2 are merged.

A DEGRADED DAY DECLINES
-----------------------
A reference row whose ``status`` is not ``ok`` / ``nothing_material`` publishes
``None`` for BOTH metrics and is excluded from every mean. A search outage must
never read as a quiet world: the 08-12 judge outage moved fleet faithfulness
0.898 -> 0.583 for three days and nothing alerted, because the stamp key split
on code rather than on grader availability.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID

from ..._entity_canon import canonicalize_entity, identity_fold, is_junk_entity
from ..._polity_match import home_prose, is_home_country, represented_by

logger = logging.getLogger(__name__)

#: NER span confidence floor — the coverage floor's own default, so the two
#: instruments read the same slice.
DEFAULT_MIN_ENTITY_CONFIDENCE = 0.5

#: Defensive per-run bounds. Hitting one is REPORTED, never silent.
_MAX_SLICE_ROWS = 60_000
_MAX_FRAMES = 4_000
_MAX_HEADS = 4_000
_MAX_MATCHED_IDS_PER_ITEM = 25

#: Minimum fold length, mirroring ``cluster_entities`` — a 1-2 character fold is
#: a tokeniser artefact, never a polity.
_MIN_FOLD_LEN = 3

# The three collection arms and the three engagement arms, named once.
ARM_C1_URL = "c1_url"
ARM_C2_ENTITY = "c2_entity"
ARM_C3_PROSE = "c3_prose"
ARM_E1_NAMED = "e1_named"
ARM_E2_CITED = "e2_cited"

#: Reference statuses either metric will score. Mirrors
#: ``desk_reference.SCORABLE_STATUSES``; declared as a literal here rather than
#: imported so this module has no import edge back onto the writer (the diff is
#: independently testable, and A-2 must run against rows A-1 wrote yesterday
#: under a pipeline version this code may no longer be).
SCORABLE_STATUSES: frozenset[str] = frozenset({"ok", "nothing_material"})


# ---------------------------------------------------------------------------
# The item key
# ---------------------------------------------------------------------------


def item_folds(
    item: Mapping[str, Any], *, home_blob: str
) -> list[str]:
    """``folds(item)`` — the anchorable key set, or ``[]`` for ``unanchorable``.

    Junk spans, non-polity canonicalizations and anything naming the desk's OWN
    country are dropped HERE rather than in SQL, because the judgment is the
    canon's and the canon is Python. The home-country test is
    :func:`~legba.data._polity_match.is_home_country` — a PROSE match over every
    spelling — and not a fold containment, which is unsafe in both directions
    ("niger" is a substring of "nigeria"; "Russia" does not contain the ISO
    spelling "Russian Federation").
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in (item.get("entities") or []):
        surface = str(raw or "").strip()
        if not surface or is_junk_entity(surface):
            continue
        canonical, canon_class = canonicalize_entity(surface, "")
        if not canonical:
            continue
        fold = identity_fold(canonical)
        if len(fold) < _MIN_FOLD_LEN or fold in seen:
            continue
        if home_blob and is_home_country(canonical, home_blob):
            continue
        seen.add(fold)
        out.append(fold)
    return out


def item_names(
    item: Mapping[str, Any], *, home_blob: str
) -> list[str]:
    """The CANONICAL NAMES behind :func:`item_folds`, in the same order.

    The folds are the set key; the names are what :func:`represented_by` probes
    prose with, because the matcher's alias/demonym expansion is keyed on the
    canonical spelling. Keeping the two in lockstep is what stops the C2 arm's
    two clauses from asking about different entities.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in (item.get("entities") or []):
        surface = str(raw or "").strip()
        if not surface or is_junk_entity(surface):
            continue
        canonical, _cls = canonicalize_entity(surface, "")
        if not canonical:
            continue
        fold = identity_fold(canonical)
        if len(fold) < _MIN_FOLD_LEN or fold in seen:
            continue
        if home_blob and is_home_country(canonical, home_blob):
            continue
        seen.add(fold)
        out.append(canonical)
    return out


def _canonical_url(raw: Any) -> str:
    """The same reduction the URL fence uses — trailing slash and whitespace
    only. The two sides of a URL equality must fold identically or the zero-FP
    arm silently stops being zero-FP."""
    return str(raw or "").strip().rstrip("/")


def surface_folds(surfaces: Iterable[Any]) -> set[str]:
    """Fold a slice row's raw NER surfaces onto canonical identity folds."""
    out: set[str] = set()
    for raw in surfaces or ():
        surface = str(raw or "").strip()
        if not surface or is_junk_entity(surface):
            continue
        canonical, _cls = canonicalize_entity(surface, "")
        if not canonical:
            continue
        fold = identity_fold(canonical)
        if len(fold) >= _MIN_FOLD_LEN:
            out.add(fold)
    return out


# ---------------------------------------------------------------------------
# Diff (a) — REFERENCE vs SLICE
# ---------------------------------------------------------------------------


def collect_item(
    *,
    folds: Sequence[str],
    names: Sequence[str],
    slice_rows: Sequence[Mapping[str, Any]],
    urls: Sequence[str],
) -> tuple[bool, dict[str, bool], list[Any]]:
    """The three collection arms, strongest first. Pure.

    ==========  =========================================================  ======
    arm         rule                                                       FP
    ==========  =========================================================  ======
    C1 URL      ``signals.canonical_url`` EQUALS one of the item's URLs    zero
    C2 ENTITY   a row's folded NER surfaces intersect ``folds(item)``      low
                AND that row's prose is ``represented_by`` an item name
    C3 PROSE    an item name appears in a row's title/summary prose        medium
    ==========  =========================================================  ======

    ``collected = C1 or C2 or C3``. Returns
    ``(collected, {arm: fired}, matched_signal_ids)`` — the matched ids are what
    land in ``unit_reference_labels.canonical_source_ids``, which is exactly the
    column migration 0057 designed for them, and are what the E2 arm intersects
    against the head's citations.

    C1 is kept separate and FIRST because it is the only zero-false-positive
    arm: on the measured IL slice 120 of 120 rows carried a ``canonical_url``,
    so the strongest arm is fully available. C2 leads C3 because the entity arm
    found 2.25x what the prose arm did on the same slice — but C3 is not
    optional, because 18.3% of rows carry no NER array at all.
    """
    wanted_urls = {_canonical_url(u) for u in urls if _canonical_url(u)}
    fold_set = set(folds)
    arms = {ARM_C1_URL: False, ARM_C2_ENTITY: False, ARM_C3_PROSE: False}
    matched: list[Any] = []

    for row in slice_rows:
        hit = False
        row_url = _canonical_url(row.get("canonical_url"))
        prose = str(row.get("prose") or "")
        if row_url and row_url in wanted_urls:
            arms[ARM_C1_URL] = True
            hit = True
        named = any(represented_by(n, prose) is not None for n in names)
        if fold_set & set(row.get("folds") or ()):
            if named:
                arms[ARM_C2_ENTITY] = True
                hit = True
        if named:
            arms[ARM_C3_PROSE] = True
            hit = True
        if hit and len(matched) < _MAX_MATCHED_IDS_PER_ITEM:
            sid = row.get("signal_id")
            if sid is not None and sid not in matched:
                matched.append(sid)

    return any(arms.values()), arms, matched


# ---------------------------------------------------------------------------
# Diff (b) — REFERENCE vs OUTPUT
# ---------------------------------------------------------------------------


def engage_item(
    *,
    names: Sequence[str],
    head_prose: str,
    head_cited: Iterable[Any],
    matched_signal_ids: Sequence[Any],
    frame_names: Sequence[str],
) -> tuple[bool, dict[str, bool], bool]:
    """The three engagement arms. Pure.

    Returns ``(engaged, {E1, E2}, licensed)``.

    ``engaged = E1 or E2``. **E3 is NOT in that disjunction.** An open frame
    naming the item is the register's LICENCE — the standing continuity a desk
    reads out of ``unit_grounding``'s OPEN SITUATION REGISTER block — and it is
    the cause R-1 will move, not the effect. Folding it into engagement would
    make the mechanism test unfalsifiable: if R-1 opens frames and the
    ``licensed`` cells move but ``engaged`` does not, the register was never the
    binding constraint and R-1 should stop. That is only visible with the two
    recorded apart.
    """
    e1 = any(represented_by(n, head_prose) is not None for n in names)
    cited = {str(c) for c in (head_cited or ()) if c is not None}
    e2 = bool(cited & {str(m) for m in matched_signal_ids})
    licensed = any(
        represented_by(n, str(f or "")) is not None
        for f in frame_names
        for n in names
    )
    return (e1 or e2), {ARM_E1_NAMED: e1, ARM_E2_CITED: e2}, licensed


def reference_gap_candidates(
    *,
    reference: Mapping[str, Any],
    record: Mapping[str, Any],
    geo: Sequence[str],
    n_slice_rows: int,
    home_blob: str = "",
) -> list[dict[str, Any]]:
    """A-4's candidates for ONE scored (target, unit) reference. Pure, DB-free.

    One entry per UNCOLLECTED, ANCHORABLE item: the world reference named a
    development on this desk's bounded question and none of the three
    collection arms found it anywhere in the desk's own slice.

    Only ``record['uncollected']`` is walked, so every exclusion the diff
    already makes is inherited rather than re-decided: an ``unanchorable``
    item (no key after junk/home filtering) never reaches the list, a declined
    pair is never passed in at all, and a collected item is by definition not a
    gap. The item's ``sentence`` is recovered from the reference row by
    ``ordinal`` because the gauge's compact ``uncollected`` shape drops it and
    the dispatched question is built from it.

    ``arms_failed`` is stated explicitly and is always all three: an item is
    uncollected exactly when ``collect_item`` returned ``any(arms) == False``.
    It rides on the candidate so the written rationale can name WHAT was tried
    rather than only that nothing matched.

    ``entity_names`` is :func:`item_names` — the CANONICAL spellings behind the
    folds, in lockstep order, which is the guarantee that function exists to
    make. The fold is the KEY (stable, lowercase, what the natural_key and the
    containment probe are built on); the name is what a human-readable
    assignment should say. A dispatched question that asks about "lebanon"
    rather than "Lebanon" is not wrong, only worse, and the machinery to say it
    properly is already here.
    """
    by_ordinal = {
        item.get("ordinal"): item
        for item in (reference.get("items") or [])
        if isinstance(item, Mapping)
    }
    out: list[dict[str, Any]] = []
    for gap in record.get("uncollected") or ():
        if not isinstance(gap, Mapping):
            continue
        folds = [str(f) for f in (gap.get("entity_folds") or []) if str(f).strip()]
        if not folds:
            # Defensive: score_pair never records an uncollected item without a
            # fold (an item with no fold is unanchorable and skipped earlier).
            continue
        item = by_ordinal.get(gap.get("ordinal")) or {}
        names = item_names(item, home_blob=home_blob) if item else []
        out.append({
            "reference_id": reference.get("id"),
            "target_id": str(reference.get("target_id") or ""),
            "unit": str(reference.get("unit") or ""),
            "geo": [str(g) for g in geo if str(g).strip()],
            "ordinal": gap.get("ordinal"),
            "headline": str(gap.get("headline") or ""),
            "sentence": str(item.get("sentence") or ""),
            "entity_folds": folds,
            "entity_names": names,
            "urls": [str(u) for u in (gap.get("urls") or []) if str(u).strip()],
            "materiality": str(gap.get("materiality") or ""),
            "n_slice_rows": int(n_slice_rows),
            "arms_failed": [ARM_C1_URL, ARM_C2_ENTITY, ARM_C3_PROSE],
        })
    return out


def _ratio(numerator: int, denominator: int) -> float | None:
    """``n/d``, or ``None`` when ``d == 0``.

    NEVER ``0.0`` on an empty denominator. *"A gauge that reports a confident
    zero for 'no data' is exactly the failure mode this retires."*
    """
    if denominator <= 0:
        return None
    return numerator / denominator


def score_pair(
    *,
    items: Sequence[Mapping[str, Any]],
    status: str,
    home_blob: str,
    slice_rows: Sequence[Mapping[str, Any]],
    head_prose: str,
    head_cited: Iterable[Any],
    frame_names: Sequence[str],
) -> dict[str, Any]:
    """Both diffs for ONE (target, unit) reference. Pure, DB-free, $0.

    A non-scorable ``status`` (``degraded`` / ``unverified_liveness``) short-
    circuits to ``None`` for BOTH ratios with ``declined=True``: the reference
    itself is unreliable that day, so every number derived from it would be too.
    """
    record: dict[str, Any] = {
        "status": status,
        "declined": status not in SCORABLE_STATUSES,
        "n_items": len(items),
        "n_anchorable": 0,
        "n_unanchorable": 0,
        "n_collected": 0,
        "n_engaged": 0,
        "n_licensed": 0,
        "collection_recall": None,
        "attention_rate": None,
        "attention_gap": None,
        "collection_arms": {ARM_C1_URL: 0, ARM_C2_ENTITY: 0, ARM_C3_PROSE: 0},
        "attention_arms": {ARM_E1_NAMED: 0, ARM_E2_CITED: 0},
        "licensed_engaged_2x2": [[0, 0], [0, 0]],
        "uncollected": [],
        "matched_signal_ids": [],
    }
    if record["declined"]:
        return record

    cited = list(head_cited or ())
    matched_all: list[str] = []
    for item in items:
        folds = list(item.get("entity_folds") or []) or item_folds(
            item, home_blob=home_blob
        )
        names = item_names(item, home_blob=home_blob)
        if not folds or not names:
            # UNANCHORABLE — no key, no score. Excluded from BOTH the numerator
            # and the denominator of BOTH metrics.
            record["n_unanchorable"] += 1
            continue
        record["n_anchorable"] += 1
        collected, c_arms, matched = collect_item(
            folds=folds, names=names, slice_rows=slice_rows,
            urls=list(item.get("urls") or []),
        )
        for arm, fired in c_arms.items():
            if fired:
                record["collection_arms"][arm] += 1
        if not collected:
            record["uncollected"].append({
                "ordinal": item.get("ordinal"),
                "headline": item.get("headline"),
                "entity_folds": folds,
                "urls": list(item.get("urls") or [])[:2],
                "materiality": item.get("materiality"),
            })
            continue
        record["n_collected"] += 1
        for m in matched:
            key = str(m)
            if key not in matched_all:
                matched_all.append(key)
        engaged, e_arms, licensed = engage_item(
            names=names, head_prose=head_prose, head_cited=cited,
            matched_signal_ids=matched, frame_names=frame_names,
        )
        for arm, fired in e_arms.items():
            if fired:
                record["attention_arms"][arm] += 1
        if engaged:
            record["n_engaged"] += 1
        if licensed:
            record["n_licensed"] += 1
        # The 2x2 R-1's mechanism test reads: rows = licensed?, cols = engaged?
        record["licensed_engaged_2x2"][1 if licensed else 0][
            1 if engaged else 0
        ] += 1

    record["collection_recall"] = _ratio(
        record["n_collected"], record["n_anchorable"]
    )
    record["attention_rate"] = _ratio(record["n_engaged"], record["n_collected"])
    if record["attention_rate"] is not None:
        record["attention_gap"] = 1.0 - record["attention_rate"]
    record["matched_signal_ids"] = matched_all
    return record


# ---------------------------------------------------------------------------
# SQL — fetches only; every fold and every decision is Python above
# ---------------------------------------------------------------------------

#: The day's machine-authored references. ``labeled_by LIKE`` is the F-2
#: population split, applied on the READ side too so an operator row can never
#: be scored as if a machine had written it.
_REFERENCES_SQL = """
SELECT id, unit_analyst_id, target_id, items, status, provenance
  FROM unit_reference_labels
 WHERE window_start = $1
   AND labeled_by LIKE $2
 ORDER BY target_id, unit_analyst_id
"""

_TARGET_GEO_SQL = """
SELECT td.descriptor_id AS target_id,
       ARRAY(SELECT jsonb_array_elements_text(td.body -> 'scope' -> 'geo')) AS geo
  FROM target_descriptors td
 WHERE td.is_head = TRUE
   AND td.descriptor_id = ANY($1::text[])
"""

_ISO_NAMES_SQL = """
SELECT iso2, name FROM iso_countries WHERE iso2 = ANY($1::text[])
"""

#: THE SLICE. ``input_row_refs`` for the latest successful run of each
#: (target, unit) inside the window, joined to ``signals``. NOT
#: ``prompt_rendered`` — see the module banner (#83).
#:
#: ``analyst_traces.status`` is ``'success'``, not ``'ok'`` (3,903 rows in 2
#: days, one value). Noted because the obvious guess costs a silent zero-row
#: join.
_SLICE_SQL = """
WITH p AS (
    SELECT * FROM unnest($1::text[], $2::text[]) AS p(target_id, analyst_id)
), t AS (
    SELECT DISTINCT ON (at.analyst_id, at.target_id)
           at.analyst_id, at.target_id, at.input_row_refs
      FROM analyst_traces at
      JOIN p ON p.target_id = at.target_id AND p.analyst_id = at.analyst_id
     WHERE at.status = 'success'
       AND at.run_started_at > $3
     ORDER BY at.analyst_id, at.target_id, at.run_started_at DESC
)
SELECT DISTINCT
       t.target_id,
       t.analyst_id,
       s.id                                              AS signal_id,
       s.canonical_url                                   AS canonical_url,
       lower(coalesce(s.payload ->> 'title', '') || ' ' ||
             coalesce(s.payload ->> 'title_en', '') || ' ' ||
             coalesce(s.payload ->> 'summary', ''))      AS prose,
       ARRAY(
           SELECT lower(e ->> 'text')
             FROM jsonb_array_elements(
                    CASE WHEN jsonb_typeof(s.payload -> 'entities') = 'array'
                         THEN s.payload -> 'entities' ELSE '[]'::jsonb END) e
            WHERE coalesce((e ->> 'confidence')::float, 1.0) >= $4
       )                                                 AS surfaces
  FROM t
  JOIN signals s ON s.id = ANY(t.input_row_refs)
 LIMIT $5
"""

#: THE HEAD. ``title`` + ``body`` prose ONLY — never
#: ``data.citations[].source_text``, which a naive whole-JSON grep picks up and
#: which produced the one factual error the IL diagnosis had to correct.
_HEADS_SQL = """
WITH p AS (
    SELECT * FROM unnest($1::text[], $2::text[]) AS p(target_id, analyst_id)
)
SELECT DISTINCT ON (ao.target_id, ao.analyst_id)
       ao.target_id,
       ao.analyst_id,
       ao.id                                             AS output_id,
       lower(coalesce(ao.title, '') || ' ' || coalesce(ao.body, '')) AS prose,
       ARRAY(
           SELECT c ->> 'signal_id'
             FROM jsonb_array_elements(
                    CASE WHEN jsonb_typeof(ao.data -> 'data' -> 'citations')
                              = 'array'
                         THEN ao.data -> 'data' -> 'citations'
                         ELSE '[]'::jsonb END) c
            WHERE c ? 'signal_id'
       )                                                 AS cited
  FROM analyst_outputs ao
  JOIN p ON p.target_id = ao.target_id AND p.analyst_id = ao.analyst_id
 WHERE ao.kind = 'finding'
   AND ao.superseded_by IS NULL
   AND ao.produced_at > $3
 ORDER BY ao.target_id, ao.analyst_id, ao.produced_at DESC, ao.id DESC
 LIMIT $4
"""

#: The SAME open-frame predicate ``read_open_situations`` / ``_OPEN_FRAMES_SQL``
#: use, because E3 must measure the register a desk actually reads rather than a
#: private definition of "open".
_FRAMES_SQL = """
SELECT target_id, name
  FROM situations
 WHERE superseded_by IS NULL
   AND (valid_until IS NULL OR valid_until > now())
   AND status <> 'closed'
   AND target_id = ANY($1::text[])
 LIMIT $2
"""

#: 0057's ``canonical_source_ids`` used for exactly what its own comment says it
#: is for: "the substrate rows that GROUND the label". The collection diff is
#: what knows them, so it is what fills them.
_UPDATE_MATCHED_SQL = """
UPDATE unit_reference_labels
   SET canonical_source_ids = $2::uuid[]
 WHERE id = $1
"""


def _coerce_uuids(values: Iterable[Any]) -> list[UUID]:
    out: list[UUID] = []
    for v in values or ():
        try:
            out.append(UUID(str(v)))
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def _load_items(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, list):
        return []
    return [dict(i) for i in raw if isinstance(i, Mapping)]


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


async def run_reference_diff(
    conn: Any,
    *,
    window_start: datetime,
    window_end: datetime,
    labeled_by_prefix: str,
    pipeline_version: str,
    now: datetime,
    min_entity_confidence: float = DEFAULT_MIN_ENTITY_CONFIDENCE,
    gap_sink: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Both diffs over the day's references, plus the gauge and the harness.

    Returns the ``data`` keys the sweep publishes:
    ``collection_gauge`` / ``attention_gauge`` / ``instrument``. It NEVER
    alerts, NEVER dispatches and NEVER gates — A-5 (the ``attention_gap``
    trigger class) is deliberately absent, and is post-R4 in any case because
    no page may come from an ungraded instrument.

    ``gap_sink`` IS A-4's leg, and it is an OUT-PARAMETER rather than a
    returned key on purpose. The returned dict is spliced wholesale into the
    finding payload (``desk_reference._build_receipt``'s ``data.update``), so
    a new key here would appear on every gauge row the moment this train lands
    — flag or no flag — and the A-4 contract is that with
    ``LEGBA_REFERENCE_GAP_DISPATCH_ENABLED`` off the payload is byte-identical
    to its pre-A-4 self but for one added ``reference_gap_dispatch`` key. Left
    ``None`` (the default, and what the flag-off caller passes) not one line of
    this costs anything: no extra query, no extra fold, no allocation.

    When a list IS passed it is filled with one FULL-FIDELITY candidate per
    uncollected, anchorable item — the whole item (headline, sentence,
    materiality, folds, urls), its citing ``unit_reference_labels`` row id, its
    unit and its target's ISO geo. The gauge's own ``uncollected`` array cannot
    serve: it is truncated to five per pair and carries neither the reference
    row id (A-4's ``evidence_id``) nor the geo (the researcher's ONLY
    reachability key).
    """
    ref_rows = await conn.fetch(
        _REFERENCES_SQL, window_start, f"{labeled_by_prefix}%"
    )
    if not ref_rows:
        return _empty_payload(pipeline_version, reason="no references in window")

    references = [
        {
            "id": r["id"],
            "unit": str(r["unit_analyst_id"]),
            "target_id": str(r["target_id"] or ""),
            "items": _load_items(r["items"]),
            "status": str(r["status"] or ""),
        }
        for r in ref_rows
        if r["target_id"]
    ]
    targets = sorted({r["target_id"] for r in references})
    pair_targets = [r["target_id"] for r in references]
    pair_units = [r["unit"] for r in references]

    geo_rows = await conn.fetch(_TARGET_GEO_SQL, targets)
    geo_by_target = {
        str(r["target_id"]): [
            str(g) for g in (r["geo"] or []) if isinstance(g, str) and str(g).strip()
        ]
        for r in geo_rows
    }
    iso_codes = sorted({
        str(g) for r in geo_rows for g in (r["geo"] or []) if isinstance(g, str)
    })
    iso_rows = await conn.fetch(_ISO_NAMES_SQL, iso_codes) if iso_codes else []
    iso_names = {str(r["iso2"]): str(r["name"]) for r in iso_rows}
    home_by_target = {
        str(r["target_id"]): home_prose(
            [str(g) for g in (r["geo"] or []) if isinstance(g, str)],
            [
                iso_names[str(g)]
                for g in (r["geo"] or [])
                if isinstance(g, str) and str(g) in iso_names
            ],
        )
        for r in geo_rows
    }

    slice_rows = await conn.fetch(
        _SLICE_SQL, pair_targets, pair_units, window_start,
        float(min_entity_confidence), int(_MAX_SLICE_ROWS),
    )
    head_rows = await conn.fetch(
        _HEADS_SQL, pair_targets, pair_units, window_start, int(_MAX_HEADS)
    )
    frame_rows = await conn.fetch(_FRAMES_SQL, targets, int(_MAX_FRAMES))

    slices: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in slice_rows:
        key = (str(row["target_id"]), str(row["analyst_id"]))
        slices.setdefault(key, []).append({
            "signal_id": row["signal_id"],
            "canonical_url": row["canonical_url"],
            "prose": str(row["prose"] or ""),
            "folds": surface_folds(row["surfaces"] or ()),
        })
    heads = {
        (str(r["target_id"]), str(r["analyst_id"])): {
            "output_id": r["output_id"],
            "prose": str(r["prose"] or ""),
            "cited": [str(c) for c in (r["cited"] or []) if c],
        }
        for r in head_rows
    }
    frames: dict[str, list[str]] = {}
    for row in frame_rows:
        frames.setdefault(str(row["target_id"]), []).append(str(row["name"] or ""))

    collection_gauge: dict[str, Any] = {}
    attention_gauge: dict[str, Any] = {}
    degraded_pairs = 0
    scored_pairs: list[dict[str, Any]] = []

    for ref in references:
        key = (ref["target_id"], ref["unit"])
        gauge_key = f"{ref['target_id']}|{ref['unit']}"
        head = heads.get(key) or {}
        record = score_pair(
            items=ref["items"],
            status=ref["status"],
            home_blob=home_by_target.get(ref["target_id"], ""),
            slice_rows=slices.get(key, []),
            head_prose=str(head.get("prose") or ""),
            head_cited=head.get("cited") or (),
            frame_names=frames.get(ref["target_id"], []),
        )
        if record["declined"]:
            degraded_pairs += 1
        else:
            scored_pairs.append({**record, "target_id": ref["target_id"],
                                 "unit": ref["unit"],
                                 "output_id": head.get("output_id")})
            if gap_sink is not None:
                gap_sink.extend(
                    reference_gap_candidates(
                        reference=ref,
                        record=record,
                        geo=geo_by_target.get(ref["target_id"], []),
                        n_slice_rows=len(slices.get(key, [])),
                        home_blob=home_by_target.get(ref["target_id"], ""),
                    )
                )
            matched = _coerce_uuids(record["matched_signal_ids"])
            if matched:
                try:
                    await conn.execute(_UPDATE_MATCHED_SQL, ref["id"], matched)
                except Exception as exc:  # noqa: BLE001 — never break the sweep
                    logger.warning(
                        "reference_diff.canonical_source_ids_write_failed "
                        "ref=%s err=%s", ref["id"], exc,
                    )

        collection_gauge[gauge_key] = {
            "status": record["status"],
            "declined": record["declined"],
            "n_items": record["n_items"],
            "n_anchorable": record["n_anchorable"],
            "n_unanchorable": record["n_unanchorable"],
            "n_collected": record["n_collected"],
            "n_slice_rows": len(slices.get(key, [])),
            "collection_recall": record["collection_recall"],
            "arms": dict(record["collection_arms"]),
            "uncollected": record["uncollected"][:5],
        }
        attention_gauge[gauge_key] = {
            "status": record["status"],
            "declined": record["declined"],
            "has_head": bool(head),
            "n_collected": record["n_collected"],
            "n_engaged": record["n_engaged"],
            "attention_rate": record["attention_rate"],
            "attention_gap": record["attention_gap"],
            "arms": dict(record["attention_arms"]),
            "n_licensed": record["n_licensed"],
            "licensed_engaged_2x2": record["licensed_engaged_2x2"],
        }

    from ._reference_validity import assess_instrument

    instrument = await assess_instrument(
        conn,
        scored_pairs=scored_pairs,
        references=references,
        home_by_target=home_by_target,
        slices=slices,
        heads=heads,
        frames=frames,
        pipeline_version=pipeline_version,
        degraded_pairs=degraded_pairs,
        now=now,
    )
    return {
        "collection_gauge": collection_gauge,
        "attention_gauge": attention_gauge,
        "instrument": instrument,
    }


def _empty_payload(pipeline_version: str, *, reason: str) -> dict[str, Any]:
    """The honest empty. Every ratio absent rather than zero, and a reason."""
    return {
        "collection_gauge": {},
        "attention_gauge": {},
        "instrument": {
            "status": "unvalidated",
            "reason": reason,
            "permuted_attention_rate": None,
            "v2_precision": None,
            "v3_precision": None,
            "v3_recall": None,
            "degraded_pairs": 0,
            "pipeline_version": pipeline_version,
        },
    }


def pooled_rate(
    records: Sequence[Mapping[str, Any]], *, numerator: str, denominator: str
) -> float | None:
    """Pool a rate over pairs by SUMMING both sides, never by averaging rates.

    A mean of per-pair rates weights a pair with one collected item as heavily
    as a pair with five — the same defect ``correctness_axis`` documents for the
    fleet operator score, and the reason it pools verdicts rather than unit
    means. ``None`` when the pooled denominator is empty.
    """
    num = sum(int(r.get(numerator) or 0) for r in records)
    den = sum(int(r.get(denominator) or 0) for r in records)
    return _ratio(num, den)


__all__ = [
    "ARM_C1_URL",
    "ARM_C2_ENTITY",
    "ARM_C3_PROSE",
    "ARM_E1_NAMED",
    "ARM_E2_CITED",
    "SCORABLE_STATUSES",
    "collect_item",
    "engage_item",
    "item_folds",
    "item_names",
    "pooled_rate",
    "reference_gap_candidates",
    "run_reference_diff",
    "score_pair",
    "surface_folds",
]
