# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE COMPOSITION TENSION RULE'S contrary-evidence leg (Program 7a).

WHAT THE TENSION SECTION IS TODAY. Under the D-5/D-6 assembly regime a
composition's ``## Tension`` section is DETERMINISTIC: ``assembly_payload.
_tensions`` walks the carried blocks (and the shown-not-carried drops) for
direction conflicts, and ``assembly_render`` prints one bullet per declared
tension plus, when none was found, the checked-negative counters. Every side of
every bullet is a read this platform published.

WHAT THIS MODULE ADDS, AND THE ONE THING IT MAY NOT DO. It admits a THIRD kind
of other side: a page retrieved from the open web by the contrary-evidence pass
because it asserts the opposite of a claim in a carried read. The line is
HEDGED, it CITES the page, and it NEVER RESOLVES — the record says what was
retrieved, not which side is right. That restraint is the whole permission: a
composition is allowed to declare THAT two things pull apart; deciding between
them is a judgement this tier does not make and a retrieval certainly does not.

THE ADMISSION RULE, AND WHY IT IS NARROWER THAN THE TABLE
----------------------------------------------------------
Only ``stance='contradicts'`` AND ``derivation='polarity'`` AND
``independent_pages >= 2`` AND a live ``expires_at`` reach a composition.

  * a ``qualifies`` record is not a tension — it narrows a claim, it does not
    pull against it, and rendering it as one would overstate every
    qualification the pass ever finds;
  * the ``negation`` derivation is the UNCALIBRATED fallback (see
    ``_contrary_stance``'s docstring). R2 earned the polarity table's precision
    with a live sweep — 57 candidate pairs across 24 of 32 desks, almost all
    false, pruned to zero — and the lesson it wrote down is that a detector
    wired into a composition's input as a stated fact manufactures
    disagreements for the fleet to write up. The fallback is served on the
    route and on the Inspector chip, where a human reads it with the
    counter-ref one click away; it is not served here;
  * an expired record is history. A contention has a shelf life because the web
    moves, and a composition citing a six-week-old retrieval as a live tension
    would be asserting something nobody checked;
  * a contradiction resting on ONE page is not a disagreement, it is a
    retrieval. The stance rules already cap it at ``qualifies``
    (``_contrary_fences`` F4), and this reader repeats the test so a row that
    reached the table by any other path is refused here too. A row whose
    ``independent_pages`` is NULL — every row written before migration 0222,
    including the three false ``contradicts`` the first live run left behind —
    measured no independence and is excluded by the same comparison.

THE JOIN IS THE CLAIM KEY, NOT THE READ ID. A contention row names the read
it was written against (``finding_id``), and using that here would be the
natural mistake: the composition being built is a NEW row, so nothing would
ever match. What carries across cycles is the CLAIM —
``claim_key(fold(text), origin_head_id, start, end)`` — so a composition
quoting the same desk-head span a day later is carrying the same claim and
inherits its contention. The key is computed here from the payload's own spans
by the AUDITOR's own function, imported rather than re-implemented: a second
spelling of a content hash is a second population wearing one name.

CITATION SHAPE. The counter page is NOT a ``[[ref:N]]`` — the assembly mints
exactly one ``[[ref:N]]`` per carried block, and ``|blocks| == |citations| ==
|distinct markers|`` is true BY CONSTRUCTION rather than by inspection. A new
``[[…]]`` marker would break that by introducing a marker with no block behind
it. So a counter page takes a plain ``[counter:N]`` ordinal into a
``## Counter-evidence`` list at the foot of the body: an ordinal a reader can
follow, invisible to citation resolution, and one the invariant never sees.

EVERYTHING DEGRADES TO TODAY. No records, no pool, migration 0221 or 0222 not
applied,
a query that raises — every one of them returns the payload UNCHANGED, which
renders byte-for-byte the body that ships now. A composition must never fail to
render because an optional sidecar is missing.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID

from .deterministic_handlers._external_audit_claims import claim_key

logger = logging.getLogger(__name__)

#: The ``tensions`` entry kind this module mints. A sibling of the assembler's
#: own ``carried_pair`` / ``carried_vs_dropped``, so the UI, the export and the
#: record's own arithmetic can tell the populations apart without parsing prose.
TENSION_KIND_CONTRARY = "contrary_retrieval"

DETECTOR = "contrary_evidence_pass"

#: The two predicates that gate a record onto a composition. See the docstring.
ADMITTED_STANCE = "contradicts"
ADMITTED_DERIVATION = "polarity"

#: How many counter-refs one composition may render. R2's ``MAX_CONTRADICTIONS``
#: is 5 for the stated reason — a long tension list is noise — and a
#: composition showing ten retrieved counter-refs would be reporting the
#: retrieval rather than the read.
MAX_TENSION_RECORDS = 5

#: F4 AT THE COMPOSITION FLOOR, belt and braces with the stance itself. The
#: derivation already caps a single-page contradiction at ``qualifies`` before
#: the row is written, so this predicate should never exclude a row the stance
#: rules admitted. It is here anyway for two reasons: a row written by an older
#: pipeline version measured no independence at all (NULL, which this comparison
#: excludes — correctly, since "not measured" is not "two"), and a future write
#: path that forgot the fence would be refused by the reader rather than
#: rendered into a composition. A composition is the one surface where a false
#: contradiction manufactures a disagreement for the fleet to write up.
MIN_INDEPENDENT_PAGES = 2

#: The COMPOSITION TENSION read. Served by
#: ``claim_contentions_contradicts_idx``, a partial index on exactly this
#: predicate, so the per-cycle question never walks the ``none_found`` bulk
#: that is — by design — most of the table. ``independent_pages`` is an extra
#: filter on the rows the index already narrowed to, not a new access path.
TENSION_SQL = """
SELECT claim_id, claim_text, finding_id, target_id, desk_key, analyst_id,
       query, statement, refs, as_of, expires_at, stance, derivation,
       pipeline_version, host_class, page_published_at, subject_overlap,
       independent_pages
FROM claim_contentions
WHERE claim_id = ANY($1::text[])
  AND stance = 'contradicts'
  AND derivation = 'polarity'
  AND expires_at > $2
  AND independent_pages >= 2
ORDER BY as_of DESC, claim_id
LIMIT $3
"""


def _as_uuid(raw: Any) -> UUID | None:
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw))
    except (TypeError, ValueError, AttributeError):
        return None


def _row_dict(row: Any) -> dict[str, Any]:
    out = dict(row)
    refs = out.get("refs")
    if isinstance(refs, (str, bytes)):
        try:
            out["refs"] = json.loads(refs)
        except (TypeError, ValueError):
            out["refs"] = []
    return out


def composition_admits(record: Mapping[str, Any]) -> bool:
    """The single place the composition's admission rule is spelled out.

    Three predicates now, and the third is F4 read back off the row: a
    contradiction resting on fewer than two independent pages — or on an
    unmeasured number, which a pre-0222 row carries — does not reach a
    composition. ``expires_at`` is the fourth and lives in the SQL, because
    liveness is a clock question the reader answers once per cycle.
    """
    try:
        independent = int(record.get("independent_pages") or 0)
    except (TypeError, ValueError):
        independent = 0
    return (
        str(record.get("stance") or "") == ADMITTED_STANCE
        and str(record.get("derivation") or "") == ADMITTED_DERIVATION
        and independent >= MIN_INDEPENDENT_PAGES
    )


async def contradictions_for_claims(
    conn: Any,
    claim_ids: Sequence[str],
    *,
    now: datetime,
    limit: int = MAX_TENSION_RECORDS,
) -> list[dict[str, Any]]:
    """Live, polarity-derived contradictions for the claims a composition carries.

    Returns ``[]`` on ANY failure, including migration 0221 not yet applied —
    the ``system_escalations`` posture, and it is right here: an empty list
    renders exactly as today.
    """
    ids = [str(c) for c in dict.fromkeys(claim_ids or ()) if c]
    if not ids:
        return []
    try:
        rows = await conn.fetch(TENSION_SQL, ids, now, int(limit))
    except Exception as exc:
        logger.info("contrary_tension.read_unavailable err=%s", exc)
        return []
    return [_row_dict(r) for r in rows]


def _top_ref(record: Mapping[str, Any]) -> Mapping[str, Any]:
    refs = [r for r in (record.get("refs") or []) if isinstance(r, Mapping)]
    for ref in refs:
        if ref.get("stance") == ADMITTED_STANCE:
            return ref
    return refs[0] if refs else {}


def tension_entries(
    records: Sequence[Mapping[str, Any]],
    ordinal_by_claim: Mapping[str, int],
) -> list[dict[str, Any]]:
    """Contention records → assembly ``tensions`` entries, CARRIED BLOCKS ONLY.

    A record whose CLAIM this composition does not carry is dropped: the
    section cites ordinals, and an ordinal with no block behind it is worse than
    a missing line. ``b`` is ``None`` and ``b_ref`` names the counter PAGE — the
    existing slot for "the other side is not a carried read", so the renderer,
    the export and the record's arithmetic need no new concept.
    """
    out: list[dict[str, Any]] = []
    for record in records or ():
        if not composition_admits(record):
            continue
        ordinal = ordinal_by_claim.get(str(record.get("claim_id") or ""))
        if ordinal is None:
            continue
        ref = _top_ref(record)
        out.append({
            "kind": TENSION_KIND_CONTRARY,
            "a": {"ordinal": int(ordinal), "span_index": 0},
            "b": None,
            "b_ref": {
                "counter_url": str(ref.get("url") or ""),
                # The DATE GATE's date first (F2 parsed it out of the page's own
                # metadata or a dated URL), the raw discovery only as a
                # fallback for a pre-0222 row. Absence stays absence — the
                # renderer prints "no date stated", never today.
                "published_at": (
                    ref.get("page_published_at") or ref.get("published_at")
                ),
                "sha256": str(ref.get("sha256") or ""),
                "retrieved_at": str(record.get("as_of") or ""),
                "query": str(record.get("query") or ""),
                "span": str(ref.get("quote") or ""),
            },
            "b_carried": False,
            "b_why": "retrieved counter-evidence, not a published read",
            "statement": str(record.get("statement") or ""),
            "statement_source": "template",
            "detector": DETECTOR,
            "detector_version": str(record.get("pipeline_version") or ""),
            "same_target": None,
            "same_window_h": None,
            "detail": {
                "claim_id": str(record.get("claim_id") or ""),
                "derivation": str(record.get("derivation") or ""),
                # The fences' own numbers, so a rendered tension line can be
                # re-argued from the payload without re-reading the table.
                "host_class": str(record.get("host_class") or ""),
                "subject_overlap": record.get("subject_overlap"),
                "independent_pages": record.get("independent_pages"),
            },
        })
    return out


def ordinals_by_claim(payload: Mapping[str, Any]) -> dict[str, int]:
    """``{claim_key: ordinal}`` over every QUOTED span of every carried block.

    The key is minted by the auditor's own :func:`claim_key` over the span's
    folded text and its byte-exact origin, which is exactly how the
    contrary-evidence pass minted the ``claim_id`` it stored — so this map and
    the table agree by construction rather than by convention.

    A block whose spans carry no origin offsets yields nothing: without the
    byte range there is no key, and guessing one would join a contention to a
    claim it was never about.
    """
    out: dict[str, int] = {}
    for block in payload.get("blocks") or ():
        ordinal = block.get("ordinal")
        if ordinal is None:
            continue
        for span in block.get("spans") or ():
            if not isinstance(span, Mapping):
                continue
            origin = span.get("origin")
            origin = origin if isinstance(origin, Mapping) else {}
            head = str(origin.get("head_id") or block.get("finding_id") or "")
            if not head:
                continue
            key = claim_key(
                str(span.get("text") or ""), head,
                origin.get("start"), origin.get("end"),
            )
            out.setdefault(key, int(ordinal))
    return out


def merge_entries(
    payload: dict[str, Any], entries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Splice contrary entries into a built assembly payload. Pure and total.

    THE COUNTERS MOVE WITH THE ENTRIES, and that is not bookkeeping — the
    Assessment voice is handed ``tension_checked`` as the record's own
    arithmetic and is graded against it, so a payload whose ``tensions`` list
    grew while its counters did not would put the voice in the position of
    narrating a tension the arithmetic says was never found. The contrary
    entries land in the UNCARRIED population (their other side is a web page,
    not a published read), and a named counter records how many of that
    population came from retrieval rather than from a drop.

    ``pairs_examined`` is deliberately NOT touched: this pass examined claims
    against the open web, not block PAIRS, and adding them to a denominator
    that means "pairs of shown blocks compared to each other" would corrupt the
    one number the checked-negative sentence rests on.
    """
    if not entries:
        return payload
    tensions = list(payload.get("tensions") or [])
    tensions.extend(dict(e) for e in entries)
    payload["tensions"] = tensions
    checked = dict(payload.get("tension_checked") or {})
    n = len(entries)
    checked["pairs_found"] = int(checked.get("pairs_found") or 0) + n
    checked["pairs_found_uncarried"] = (
        int(checked.get("pairs_found_uncarried") or 0) + n
    )
    checked["contrary_retrieval"] = (
        int(checked.get("contrary_retrieval") or 0) + n
    )
    payload["tension_checked"] = checked
    return payload


async def merge_contrary_tension(
    payload: dict[str, Any],
    conn: Any,
    *,
    now: datetime | None = None,
    limit: int = MAX_TENSION_RECORDS,
) -> dict[str, Any]:
    """Read the live contradictions for this payload's blocks and splice them in.

    ``conn`` is a pool OR a connection (the synthesizer's ``pg`` carrier is
    either), and ``None`` — a deps carrier without one — returns the payload
    untouched. So does an empty result, an unapplied migration, or any raised
    query: the composition renders exactly as it does today.
    """
    if conn is None or not payload.get("blocks"):
        return payload
    by_claim = ordinals_by_claim(payload)
    if not by_claim:
        return payload
    moment = now or datetime.now(timezone.utc)
    acquire = getattr(conn, "acquire", None)
    try:
        if callable(acquire):
            async with conn.acquire() as held:
                records = await contradictions_for_claims(
                    held, list(by_claim), now=moment, limit=limit
                )
        else:
            records = await contradictions_for_claims(
                conn, list(by_claim), now=moment, limit=limit
            )
    except Exception as exc:
        logger.info("contrary_tension.merge_unavailable err=%s", exc)
        return payload
    return merge_entries(payload, tension_entries(records, by_claim))


def counter_refs(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ``## Counter-evidence`` list, in the order the tension bullets cite it.

    One entry per contrary tension entry, numbered from 1. The renderer prints
    ``[counter:N]`` beside the bullet and this list at the foot of the body, so
    a reader can follow the ordinal to a URL, a publication date and the hash of
    the bytes the platform holds.
    """
    out: list[dict[str, Any]] = []
    for entry in payload.get("tensions") or ():
        if entry.get("kind") != TENSION_KIND_CONTRARY:
            continue
        ref = dict(entry.get("b_ref") or {})
        ref["ordinal"] = len(out) + 1
        out.append(ref)
    return out


__all__ = [
    "ADMITTED_DERIVATION",
    "ADMITTED_STANCE",
    "DETECTOR",
    "MAX_TENSION_RECORDS",
    "MIN_INDEPENDENT_PAGES",
    "TENSION_KIND_CONTRARY",
    "TENSION_SQL",
    "composition_admits",
    "contradictions_for_claims",
    "counter_refs",
    "merge_contrary_tension",
    "merge_entries",
    "ordinals_by_claim",
    "tension_entries",
]
