# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE READS AND WRITES of ``claim_contentions`` (migrations 0221 + 0222).

Three reads and one write, and the reason they live together is that the claim
SELECTION and the contention WRITE have to agree about what a claim is. They do,
by construction: the selection is the standing external auditor's own
enumeration, imported and not re-implemented, and the row's ``claim_id`` is the
auditor's own ``claim_key``. A second spelling of "which claims are material"
would be a second population, and the platform has been explicit since the
08-12 lesson that two instruments describing the same thing under two
definitions is worse than one instrument.

SELECTION — THE AUDIT'S BAR, VERBATIM
-------------------------------------
* the same TOP LAYER: ``_external_audit_width.WIDTH_READ_ANALYST_IDS``, the
  reads that assert things about the world. The region tier is absent there
  because it became a deterministic rollup (D-5) with no claims of its own, and
  it stays absent here for the same reason.
* the same CLAIM ENUMERATION: ``_external_audit_claims.claims_from_read`` — the
  assembled record's spans and the Assessment channel's sentences, straight off
  the payload, zero model cost, a ``claim_key`` stable across a replay.
* the same MATERIALITY BAR: a claim is material when the audit calls it
  CHECKABLE — ``uncheckable_class is None``, i.e. it survived the deterministic
  pre-filter that removes scope-bounded, provenance and perspective claims
  (statements with no world truth-maker, which a search can only ever return
  nothing about).
* the same PRIORITY: ``_external_audit_queue.priority_key`` — severity, then
  lead position, then tier, then key. When the per-run cap binds, it drops the
  least material claims rather than the last ones by name.

ONE ADDITION, AND IT IS A SUBTRACTION. Claims longer than R2's
``MAX_CLAIM_CHARS`` are NOT selected. A 400-character sentence enumerating five
domains has no single negation; asking for its counter-evidence would produce
either a paraphrase or a counter to one clause reported as a stance on all of
them. R2 skips them for the same reason. The count is on the receipt, so the
exclusion is visible rather than silent.

THE FENCE COLUMNS (0222) ARE NULLABLE AND UNBACKFILLED. ``host_class``,
``page_published_at``, ``subject_overlap`` and ``independent_pages`` are what
the four fences measured on the row's decisive page. A row written before 0222
measured none of them and carries NULL in all four — which is "not measured",
not zero, and the route renders it as absence. The three false ``contradicts``
rows the first live run left behind stay exactly as they are, expired.

WHY A WATERMARK AND NOT A QUEUE. The auditor drains a durable queue because it
grades every claim and must not lose one. This pass does something different: it
asks a question of the open web ONCE per claim, at publication time, and the
answer's shelf life is the row's ``expires_at``. A watermark over
``produced_at`` is the whole state that needs: it rides the auditor's existing
``alert_trigger_watermarks`` partition under this pass's own
``trigger_class``, so no new table, and a re-run inside a day is a no-op by the
unique index rather than by bookkeeping.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID

from ..contrary_tension import (
    MAX_TENSION_RECORDS,
    composition_admits,
    contradictions_for_claims,
    tension_entries,
)
from ._contrary_fences import HOST_CLASSES
from ._contrary_stance import DERIVATION_POLARITY, STANCE_SEARCH_FAILED
from ._external_audit_claims import WidthClaim, claims_from_read, iter_unique
from ._external_audit_queue import priority_key
from ._external_audit_width import WIDTH_READ_ANALYST_IDS
from ._reference_page import host_of, parse_iso_date

logger = logging.getLogger(__name__)

#: This instrument's OWN population stamp. It is not the audit's and it is not
#: the judge's: pooling a contention record into either population would
#: describe a population that never existed (the ``structural_claims`` /
#: ``EXTERNAL_AUDIT_PIPELINE_VERSION`` precedent). Bump it when the counter-query
#: rules, the stance rules or the selection bar move — never for a comment.
#:
#: ``7a.2`` (2026-09-25) — THE FOUR FENCES. The stance rules moved, so the stamp
#: moves with them: a fenced row and one of the first live run's three false
#: ``contradicts`` rows describe two different instruments and must never be
#: averaged together. The unique key carries this column, so the bump also means
#: a claim contended under 7a.1 can be contended again today under 7a.2 rather
#: than being silently skipped as already-done.
CONTRARY_PIPELINE_VERSION = "2026-09/7a.2"

#: ``alert_trigger_watermarks`` partition. The claim_watch / standing_auditor
#: precedent — durable state on an existing table, never a new one.
TRIGGER_CLASS = "contrary_evidence"
HEARTBEAT_KEY = "_heartbeat"
WATERMARK_KEY = "_watermark"

#: The top layer. Imported, never re-listed — see the module docstring.
MATERIAL_ANALYST_IDS: tuple[str, ...] = WIDTH_READ_ANALYST_IDS

#: Cold-start reach when no watermark exists yet. Matches the auditor's own
#: refill cold start, so the first tick of the two instruments sees the same
#: reads rather than two different fortnights.
COLD_START_HOURS = 48

#: Safety valve on the pre-sort, not a window. Same shape and the same warning
#: as the auditor's ``_REFILL_FETCH_CAP``: if this were ever REACHED the
#: truncation would drop the OLDEST reads inside the window, which is the
#: correct direction here (a stale read's counter-evidence is the least useful)
#: — but it would be silent, so the handler counts it onto the receipt.
READ_FETCH_CAP = 200


_READS_SQL = """
SELECT ao.id, ao.analyst_id, ao.target_id, ao.title, ao.body, ao.data,
       ao.produced_at
FROM analyst_outputs ao
WHERE ao.kind = 'finding'
  AND ao.analyst_id = ANY($1::text[])
  AND ao.superseded_by IS NULL
  AND ao.produced_at > $2
ORDER BY ao.produced_at DESC, ao.id DESC
LIMIT $3
"""

_WATERMARK_READ_SQL = """
SELECT state FROM alert_trigger_watermarks
WHERE trigger_class = $1 AND watermark_key = $2
"""

_WATERMARK_WRITE_SQL = """
INSERT INTO alert_trigger_watermarks (trigger_class, watermark_key, state,
                                      fired_at, updated_at)
VALUES ($1, $2, $3::jsonb, $4, now())
ON CONFLICT (trigger_class, watermark_key) DO UPDATE
   SET state = EXCLUDED.state,
       fired_at = COALESCE(EXCLUDED.fired_at,
                           alert_trigger_watermarks.fired_at),
       updated_at = now()
"""

_INSERT_SQL = """
INSERT INTO claim_contentions (
    claim_id, claim_text, finding_id, origin_head_id, block_ordinal,
    span_role, target_id, desk_key, analyst_id,
    query, query_source, query_novel_tokens, polarity_group, polarity_sign,
    rung, stance, derivation, reason, statement, refs, ref_signal_ids,
    pipeline_version, retrieved_at, as_of, as_of_day, expires_at, receipt_id,
    host_class, page_published_at, subject_overlap, independent_pages
) VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8, $9,
    $10, $11, $12, $13, $14,
    $15, $16, $17, $18, $19, $20::jsonb, $21::uuid[],
    $22, $23, $24, $25, $26, $27,
    $28, $29, $30, $31
)
ON CONFLICT (claim_id, pipeline_version, as_of_day) DO NOTHING
RETURNING id
"""

#: The Inspector chip's drill. Served by ``claim_contentions_claim_idx``.
_BY_CLAIM_SQL = """
SELECT claim_id, claim_text, finding_id, target_id, desk_key, analyst_id,
       query, query_source, stance, derivation, reason, statement, refs,
       rung, retrieved_at, as_of, expires_at,
       host_class, page_published_at, subject_overlap, independent_pages
FROM claim_contentions
WHERE claim_id = ANY($1::text[])
ORDER BY claim_id, as_of DESC
LIMIT $2
"""

#: F1's LABEL source — every registered source head's host and the class it was
#: REGISTERED under. One query per run, not per page: the map is small (a few
#: hundred rows), it moves only when an operator registers a source, and a
#: per-page lookup would put a round trip inside the fence.
#:
#: The feed URL is the descriptor's own ``config.url.raw``; a descriptor whose
#: config shapes its URL differently simply contributes no host, which is the
#: honest answer — an unknown host PASSES F1 and meets the other three.
_HOST_CLASS_SQL = """
SELECT body->'config'->'url'->>'raw'      AS url,
       body->'scope'->>'source_class'     AS source_class
FROM source_descriptors
WHERE is_head
  AND body->'config'->'url'->>'raw' IS NOT NULL
  AND body->'scope'->>'source_class' IS NOT NULL
LIMIT $1
"""

#: Bound on the catalog read. Well above the live head count; present so a
#: pathological registry cannot turn one fence into an unbounded fetch.
HOST_CLASS_CATALOG_CAP = 5000

#: Link-not-create: a fetched page whose content hash already matches a landed
#: signal. Served by the baseline's ``signals_content_hash_idx``.
_SIGNAL_LINK_SQL = """
SELECT id FROM signals
WHERE content_hash = ANY($1::text[])
LIMIT $2
"""

#: How many linked signal ids one row may carry. A page is one page; a hash
#: matching dozens of rows means the corpus has duplicates, not that this
#: contention rests on dozens of sources.
MAX_LINKED_SIGNALS = 8

# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def material_claims(
    rows: Sequence[Mapping[str, Any]], *, cap: int, max_claim_chars: int,
) -> tuple[list[WidthClaim], dict[str, int]]:
    """The audit's checkable claims from these reads, ranked, capped.

    Pure — no database, no clock — so the bar can be argued about in a test
    rather than inferred from a live run. Returns the claims and the counters
    the receipt publishes: how many were enumerated, how many the audit's own
    pre-filter called uncheckable, how many were dropped as compound, and how
    many the cap left behind.
    """
    enumerated: list[WidthClaim] = []
    for row in rows or ():
        enumerated.extend(claims_from_read(row))
    unique = list(iter_unique(enumerated))
    uncheckable = sum(1 for c in unique if not c.checkable)
    checkable = [c for c in unique if c.checkable]
    too_long = sum(1 for c in checkable if len(c.claim_text) > max_claim_chars)
    eligible = [c for c in checkable if len(c.claim_text) <= max_claim_chars]
    eligible.sort(key=priority_key)
    counts = {
        "reads": len(rows or ()),
        "claims_enumerated": len(enumerated),
        "claims_unique": len(unique),
        "claims_uncheckable": uncheckable,
        "claims_compound": too_long,
        "claims_eligible": len(eligible),
        "claims_over_cap": max(0, len(eligible) - cap),
    }
    return eligible[:cap], counts


async def fetch_reads(
    conn: Any, *, watermark: str, now: datetime, fetch_cap: int = READ_FETCH_CAP,
) -> tuple[list[Mapping[str, Any]], str]:
    """Top-layer reads newer than the watermark. Returns ``(rows, new_watermark)``.

    The watermark advances to the newest ``produced_at`` ENUMERATED, and only on
    a successful read — the ``finding_supersession`` lesson: a watermark that
    advances past rows nobody looked at freezes the whole leg silently.
    """
    cutoff = _parse_ts(watermark) or (
        now - timedelta(hours=COLD_START_HOURS)
    )
    rows = await conn.fetch(
        _READS_SQL, list(MATERIAL_ANALYST_IDS), cutoff, int(fetch_cap)
    )
    out = [dict(r) for r in rows]
    # The watermark is compared as a DATETIME and only then serialised. String
    # comparison over ISO stamps looks safe and is not: two rows written under
    # different offset spellings ('+00:00' vs 'Z') order lexically by their
    # suffix, and a watermark that went backwards would re-contend a day of
    # claims while one that went forwards would skip one.
    newest_ts = _parse_ts(watermark)
    for row in out:
        produced = row.get("produced_at")
        if not isinstance(produced, datetime):
            continue
        if produced.tzinfo is None:
            produced = produced.replace(tzinfo=timezone.utc)
        if newest_ts is None or produced > newest_ts:
            newest_ts = produced
    return out, (newest_ts.isoformat() if newest_ts else watermark)


# ---------------------------------------------------------------------------
# Durable state
# ---------------------------------------------------------------------------


def _parse_ts(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def load_state(conn: Any, *, key: str) -> dict[str, Any]:
    """One watermark row's ``state``. ``{}`` when absent or unreadable."""
    try:
        raw = await conn.fetchval(_WATERMARK_READ_SQL, TRIGGER_CLASS, key)
    except Exception as exc:
        logger.warning("contrary_evidence.state_read_failed key=%s err=%s", key, exc)
        return {}
    if raw is None:
        return {}
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


async def save_state(
    conn: Any, state: Mapping[str, Any], *, key: str, fired: bool = False,
) -> bool:
    """Upsert one watermark row. Returns ``False`` on failure; never raises."""
    try:
        await conn.execute(
            _WATERMARK_WRITE_SQL, TRIGGER_CLASS, key, json.dumps(dict(state)),
            datetime.now(timezone.utc) if fired else None,
        )
        return True
    except Exception as exc:
        logger.error(
            "contrary_evidence.state_write_failed key=%s err=%s — the pass ran "
            "but did not record that it ran; the liveness family cannot see "
            "this run", key, exc,
        )
        return False


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def build_row(
    claim: WidthClaim,
    counter: Any,
    outcome: Any,
    *,
    rung: str,
    reason: str,
    as_of: datetime,
    ttl_hours: int,
    receipt_id: UUID | None,
    ref_signal_ids: Sequence[UUID] = (),
) -> dict[str, Any]:
    """The ``claim_contentions`` row for ONE claim. Pure; no clock of its own.

    ``as_of`` is passed in rather than read here so one run stamps one moment
    across every claim it touched — two rows from the same pass must not
    disagree about when the pass was.
    """
    return {
        "claim_id": claim.key,
        "claim_text": claim.claim_text[:4000],
        # THREE DIFFERENT JOINS, and each surface uses the one that is right
        # for it (migration 0221 §1): the read the claim was published IN, the
        # desk head its span was QUOTED FROM, and its ordinal inside that read.
        "finding_id": _as_uuid(claim.graded_output_id),
        "origin_head_id": _as_uuid(getattr(claim, "origin_head_id", None)),
        "block_ordinal": getattr(claim, "block_ordinal", None),
        "span_role": getattr(claim, "span_role", None),
        "target_id": claim.target_id,
        "desk_key": claim.desk_key or "",
        "analyst_id": claim.analyst_id or "",
        "query": (counter.query or "")[:400],
        "query_source": counter.source,
        "query_novel_tokens": int(counter.novel_tokens or 0),
        "polarity_group": counter.polarity_group,
        "polarity_sign": counter.polarity_sign,
        "rung": rung or "",
        "stance": outcome.stance,
        "derivation": outcome.derivation,
        "reason": (reason or counter.reason or "")[:200],
        "statement": outcome.statement[:2000],
        "refs": [r.as_dict() for r in outcome.refs],
        "ref_signal_ids": list(ref_signal_ids)[:MAX_LINKED_SIGNALS],
        "pipeline_version": CONTRARY_PIPELINE_VERSION,
        "retrieved_at": as_of,
        "as_of": as_of,
        "as_of_day": as_of.astimezone(timezone.utc).date(),
        "expires_at": as_of + timedelta(hours=int(ttl_hours)),
        "receipt_id": receipt_id,
        # THE FOUR FENCES' NUMBERS (migration 0222). Each one is what a reader
        # needs to re-argue the row without re-fetching the page: what KIND of
        # host answered, the date the gate actually parsed, how much of the
        # claim's subject the matched sentence carried, and how many independent
        # outlets stood behind a contradiction. Absent is NULL, never zero —
        # a row written before 0222 measured none of them and must not read as
        # "measured, and the answer was nothing".
        "host_class": (
            getattr(outcome, "host_class", "") or None
        ),
        "page_published_at": parse_iso_date(
            getattr(outcome, "page_published_at", None)
        ),
        "subject_overlap": (
            int(getattr(outcome, "subject_overlap", 0) or 0)
            if getattr(outcome, "host_class", "") else None
        ),
        # ...and F4's count is MEASURED only where a retrieval happened. A
        # ``search_failed`` row never reached the derivation, so its zero would
        # be a default wearing a measurement's clothes.
        "independent_pages": (
            None if outcome.stance == STANCE_SEARCH_FAILED
            else int(getattr(outcome, "independent_pages", 0) or 0)
        ),
    }


def _as_uuid(raw: Any) -> UUID | None:
    if isinstance(raw, UUID):
        return raw
    try:
        return UUID(str(raw))
    except (TypeError, ValueError, AttributeError):
        return None


async def write_contention(conn: Any, row: Mapping[str, Any]) -> bool:
    """Insert one row. ``False`` on a conflict (already written today) or a
    rejected write — counted by the caller, never raised: one malformed row must
    not cost the run its heartbeat."""
    try:
        written = await conn.fetchval(
            _INSERT_SQL,
            row["claim_id"], row["claim_text"], row["finding_id"],
            row["origin_head_id"], row["block_ordinal"], row["span_role"],
            row["target_id"], row["desk_key"], row["analyst_id"],
            row["query"], row["query_source"], row["query_novel_tokens"],
            row["polarity_group"], row["polarity_sign"],
            row["rung"], row["stance"], row["derivation"], row["reason"],
            row["statement"], json.dumps(row["refs"], default=str),
            [str(s) for s in row["ref_signal_ids"]],
            row["pipeline_version"], row["retrieved_at"], row["as_of"],
            row["as_of_day"], row["expires_at"], row["receipt_id"],
            row.get("host_class"), row.get("page_published_at"),
            row.get("subject_overlap"), row.get("independent_pages"),
        )
    except Exception as exc:
        logger.warning(
            "contrary_evidence.write_failed claim=%s err=%s",
            str(row.get("claim_id"))[:16], exc,
        )
        return False
    return written is not None


async def host_class_catalog(
    conn: Any, *, cap: int = HOST_CLASS_CATALOG_CAP,
) -> dict[str, str]:
    """``{host: source_class}`` off the registered source heads — F1's labels.

    Returns ``{}`` on ANY failure, and an empty catalog is not a degraded run:
    F1's DECISION turns only on the reference-host list, which is a module
    constant. The catalog exists so a known outlet is LABELLED with the class
    the platform already registered it under rather than with a second
    vocabulary invented for this fence.
    """
    try:
        rows = await conn.fetch(_HOST_CLASS_SQL, int(cap))
    except Exception as exc:
        logger.info("contrary_evidence.host_catalog_unavailable err=%s", exc)
        return {}
    out: dict[str, str] = {}
    for row in rows:
        host = host_of(str(row["url"] or ""))
        klass = str(row["source_class"] or "").strip()
        if host and klass in HOST_CLASSES:
            # First writer wins, deterministically by host: two feeds of one
            # outlet registered under two classes is a registry question, and
            # a fence is not the place to arbitrate it.
            out.setdefault(host, klass)
    return out


async def link_signals(conn: Any, hashes: Sequence[str]) -> list[UUID]:
    """signals ids whose ``content_hash`` matches a page we fetched.

    LINKS, never creates — see the migration header §3. A failure is not an
    error: an unlinked contention is a complete contention, and the array is a
    convenience for a reader that wants the corpus row, not the evidence.
    """
    wanted = [h for h in dict.fromkeys(hashes or ()) if h]
    if not wanted:
        return []
    try:
        rows = await conn.fetch(_SIGNAL_LINK_SQL, wanted, MAX_LINKED_SIGNALS)
    except Exception as exc:
        logger.info("contrary_evidence.signal_link_failed err=%s", exc)
        return []
    out: list[UUID] = []
    for row in rows:
        linked = _as_uuid(row["id"])
        if linked is not None:
            out.append(linked)
    return out


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def contentions_for_claims(
    conn: Any, claim_ids: Sequence[str], *, limit: int = 50,
) -> list[dict[str, Any]]:
    """Every record for these claim ids, newest first per claim."""
    wanted = [str(c) for c in dict.fromkeys(claim_ids or ()) if c]
    if not wanted:
        return []
    try:
        rows = await conn.fetch(_BY_CLAIM_SQL, wanted, int(limit))
    except Exception as exc:
        logger.info("contrary_evidence.claim_read_unavailable err=%s", exc)
        return []
    return [_row_dict(r) for r in rows]


def _row_dict(row: Any) -> dict[str, Any]:
    out = dict(row)
    refs = out.get("refs")
    if isinstance(refs, (str, bytes)):
        try:
            out["refs"] = json.loads(refs)
        except (TypeError, ValueError):
            out["refs"] = []
    return out


__all__ = [
    "COLD_START_HOURS",
    "CONTRARY_PIPELINE_VERSION",
    "DERIVATION_POLARITY",
    "HEARTBEAT_KEY",
    "HOST_CLASS_CATALOG_CAP",
    "MATERIAL_ANALYST_IDS",
    "MAX_LINKED_SIGNALS",
    "MAX_TENSION_RECORDS",
    "READ_FETCH_CAP",
    "TRIGGER_CLASS",
    "WATERMARK_KEY",
    "build_row",
    "composition_admits",
    "contentions_for_claims",
    "contradictions_for_claims",
    "fetch_reads",
    "host_class_catalog",
    "link_signals",
    "load_state",
    "material_claims",
    "save_state",
    "tension_entries",
    "write_contention",
]
