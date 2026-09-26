# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The open-row SUPERSESSION writers for the knowledge-plane tables.

Extracted from ``writes.py`` (2026-09, the module-size gate — the port-file
rule is honoured by SPLITTING, never by raising a ceiling). This module is
the cohesive unit the file's own section banners already named: the
source-tier precedence table (Holes-A A1), the contested-claims coexistence
flag (Holes-B Wave 4), and the four close-the-prior-open-row writers for
``facts`` and ``nexuses`` — ``supersede_prior_facts``,
``_supersede_prior_facts_coexist``, ``supersede_prior_functional_role_facts``,
``collapse_open_triple`` and ``supersede_prior_nexuses``.

``writes.py`` re-exports every public and private name defined here, so
``legba.data.provenance.writes.<name>`` — and the ``provenance.__init__``
re-export built on it — resolve exactly as before. Nothing else changes:
the SQL text, the docstrings and the comments below are verbatim moves.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Sequence
from uuid import UUID

import asyncpg

from .origin import origin_class_clause

#: P7/7g-1 — the origin-class leg on EVERY fact supersession site in this
#: module (SEAMS #57 sweep). Supersession is the live plane asserting that a
#: belief has been replaced. A loaded historical row is not a belief that can
#: be replaced: it is what a provider published about a period that is over,
#: and closing it would rewrite a holding the platform cites as fetched.
#: The nexus sites below take no leg — `nexuses` carries no origin_class.
_LIVE_FACTS = origin_class_clause("")


# ---------------------------------------------------------------------------
# Source-tier precedence (Holes-A A1 — confidence-tier-aware supersession)
# ---------------------------------------------------------------------------
#
# A fact's ``source_type`` is its provenance class. For auto-supersession we
# rank those classes on a TOTAL ORDER of authority: an AUTHORITATIVE fact (a
# human-curated seed) must NOT be closed by a lower-authority MACHINE-extracted
# one (an ingestion-NER hit or an analyst LLM emission). Within the SAME tier
# recency still wins — a NEW leader fact supersedes the OLD leader fact of the
# same tier exactly as before.
#
# Total order (higher int = more authoritative):
#   seed     == curated   -> 2   (AUTHORITATIVE: human/operator-owned ground truth)
#   ingestion == agent    -> 1   (MACHINE-EXTRACTED: NER hit / LLM emission)
#   <anything else / None>-> 1   (unknown class is treated as machine-extracted)
#
# ``seed`` and ``curated`` are deliberately the SAME rank (neither outranks the
# other — both are operator-blessed); likewise ``ingestion`` and ``agent``. The
# guard blocks ONLY a STRICT downgrade (incoming tier < prior row's tier), so
# same-tier and upgrades pass through untouched.
_SOURCE_TIER_RANK: dict[str, int] = {
    "seed": 2,
    "curated": 2,
    "ingestion": 1,
    "agent": 1,
}
_DEFAULT_SOURCE_TIER_RANK = 1


def _source_tier_rank(source_type: str | None) -> int:
    """Map a fact ``source_type`` onto its authority rank (see the table above).

    An unknown / ``None`` class falls back to the MACHINE-extracted rank (1) so
    an unrecognised producer can never masquerade as authoritative.
    """
    if not source_type:
        return _DEFAULT_SOURCE_TIER_RANK
    return _SOURCE_TIER_RANK.get(source_type.strip().lower(), _DEFAULT_SOURCE_TIER_RANK)

# ---------------------------------------------------------------------------
# Contested-claims coexistence (Holes-B Wave 4 — #101, decision #1)
# ---------------------------------------------------------------------------
#
# The ONE behavioral change of the contested-claims feature, gated OFF by
# default behind ``LEGBA_FACT_CONTENTION``. When ON, a SAME-TIER open prior
# whose value is FUZZY-DISTINCT from the incoming value is NOT closed — both
# rows COEXIST open so the detect-only ``fact_contention_arbiter`` opens a
# contention group on its next cadence (decision #1: coexist + surface a
# winner, never destroy the loser). Everything else closes exactly as before.
_FACT_CONTENTION_ENV = "LEGBA_FACT_CONTENTION"


def _fact_contention_enabled() -> bool:
    """Honor ``LEGBA_FACT_CONTENTION`` (default OFF).

    Only "1"/"true"/"yes"/"on" enable the write-path coexistence behavior;
    unset/empty/anything-else keeps it off, so :func:`supersede_prior_facts`
    runs the single blind UPDATE byte-for-byte as before (zero extra queries on
    the hot path).
    """
    raw = os.environ.get(_FACT_CONTENTION_ENV, "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}

async def supersede_prior_facts(
    conn: asyncpg.Connection,
    *,
    subject: str,
    predicate: str,
    value: str,
    new_fact_id: UUID,
    incoming_source_type: str | None = None,
) -> int:
    """Close any open fact(s) for ``(lower(subject), lower(predicate))`` whose
    VALUE differs from the incoming ``value``, pointing them at ``new_fact_id``
    — UNLESS the prior row outranks the incoming fact on source authority
    (Holes-A A1 — confidence-tier-aware supersession).

    This is the altitude-0 auto-supersession the old system had (PIECE B —
    temporal-fact hardening): the canonical "what is true now" for a
    subject+predicate is the single open row (``valid_until IS NULL AND
    superseded_by IS NULL``). When a new fact asserts a DIFFERENT value for the
    same subject+predicate, the prior open row(s) are closed:
    ``valid_until = now()`` + ``superseded_by = <new id>``. The new row is then
    inserted open by the caller (``_insert_fact`` / ``_insert_ingestion_fact``).

    Contract / safety:
      * **source-tier guard (A1)** — an incoming MACHINE-extracted fact
        (``ingestion``/``agent``) does NOT close an open AUTHORITATIVE fact
        (``seed``/``curated``) for the same subject+predicate. Authority ranks
        on the total order in :data:`_SOURCE_TIER_RANK`
        (``seed == curated > ingestion == agent``); the UPDATE skips any prior
        row whose tier is STRICTLY higher than the incoming one. WITHIN the same
        tier recency still wins, so a NEW leader fact supersedes the OLD leader
        fact of the same tier exactly as before — the guard blocks only the
        downgrade direction. When ``incoming_source_type`` is ``None`` (e.g. the
        operator journal-correction caller, which is maximally authoritative) NO
        tier filtering is applied and the historical behavior is preserved.
      * **value-differs only** — a re-assert of the SAME value is NOT a
        supersession; that path stays the ``idx_facts_temporal_triple_open``
        ``ON CONFLICT`` upsert (confidence lift + lineage union). The
        ``lower(value) <> lower($3)`` predicate guarantees the identical-triple
        row is never closed by its own re-ingest.
      * **idempotent** — only rows still open
        (``valid_until IS NULL AND superseded_by IS NULL``) are touched; a
        replay closes nothing new once the prior is already superseded.
      * **same connection** — the caller runs this immediately before the
        insert on the same ``conn`` so the close + open are one logical step
        (the dapr write path acquires one connection per output).

    **Contested-claims coexistence (Holes-B Wave 4, ``LEGBA_FACT_CONTENTION``).**
    When the flag is ON one extra rule joins the close set: a SAME-TIER prior
    open row whose value is FUZZY-DISTINCT from the incoming value is NOT closed
    — both rows COEXIST open so the detect-only ``fact_contention_arbiter`` opens
    a contention group next cadence (decision #1 — coexist + surface a winner,
    never destroy the loser). "Same-tier" is equal ``_source_tier_rank``;
    "fuzzy-distinct" is ``cluster_values([incoming, prior_value])`` yielding more
    than one cluster (so e.g. same-tier "Russian" vs "Russia" is fuzzy-SAME and
    still closes as today). Lower-tier priors (the incoming outranks) and
    higher-tier priors (already A1-skipped) are unaffected. With the flag OFF
    (the default) this function is byte-for-byte the single blind UPDATE below —
    ZERO extra queries on the hot path.

    Returns the number of prior rows closed (0 when this is the first
    assertion of the subject+predicate, a same-value re-assert, every
    differing-value prior row outranks the incoming fact on authority, or — with
    the flag ON — every differing-value same-tier prior is a fuzzy-distinct
    coexistence and nothing is left to close).
    """
    if _fact_contention_enabled():
        # Flag ON — the ONE behavioral change. Fetch the candidate open
        # differing-value priors, decide per-row in Python (A1 tier guard PLUS
        # the same-tier fuzzy-distinct coexistence carve-out), then close only
        # the surviving set in a single UPDATE ... WHERE id = ANY($ids). Same
        # connection, idempotent (only open rows are fetched / closed).
        return await _supersede_prior_facts_coexist(
            conn,
            subject=subject,
            predicate=predicate,
            value=value,
            new_fact_id=new_fact_id,
            incoming_source_type=incoming_source_type,
        )

    # Flag OFF (default) — the single blind UPDATE, unchanged.
    #
    # A1 — source-tier precedence. When the caller declares the incoming fact's
    # source_type we forbid closing any prior row whose authority rank is
    # STRICTLY higher (an ingestion/agent fact must not retire a seed/curated
    # one). A NULL incoming rank disables the filter (historical / operator
    # correction path stays unconditional). The guard is `source_type IS NULL`
    # tolerant — a legacy untyped row is treated as the machine-extracted rank,
    # so it can never silently outrank and block a legitimate supersession.
    incoming_rank = (
        None if incoming_source_type is None
        else _source_tier_rank(incoming_source_type)
    )
    result = await conn.execute(
        f"""
        UPDATE facts
           SET valid_until   = now(),
               superseded_by = $4, superseded_at = now(),
               updated_at    = now()
         WHERE lower(subject)   = lower($1)
           AND lower(predicate) = lower($2)
           AND lower(value)    <> lower($3)
           AND valid_until IS NULL
           AND superseded_by IS NULL
           AND {_LIVE_FACTS}
           AND id <> $4
           AND (
                 $5::int IS NULL
                 OR CASE lower(coalesce(source_type, ''))
                        WHEN 'seed'      THEN 2
                        WHEN 'curated'   THEN 2
                        WHEN 'ingestion' THEN 1
                        WHEN 'agent'     THEN 1
                        ELSE 1
                    END <= $5::int
               )
        """,
        subject,
        predicate,
        value,
        new_fact_id,
        incoming_rank,
    )
    try:
        return int(result.split()[-1]) if result else 0
    except (ValueError, IndexError):                     # pragma: no cover
        return 0


async def _supersede_prior_facts_coexist(
    conn: asyncpg.Connection,
    *,
    subject: str,
    predicate: str,
    value: str,
    new_fact_id: UUID,
    incoming_source_type: str | None,
) -> int:
    """Contention-aware variant of :func:`supersede_prior_facts` (flag ON).

    Replaces the single blind UPDATE with a FETCH → decide-per-row → close-set
    UPDATE so a same-tier fuzzy-distinct prior can COEXIST instead of being
    closed (Holes-B Wave 4, decision #1). The close set is computed in Python:
    a differing-value open prior closes iff it passes the A1 source-tier guard
    AND is NOT (same-tier AND fuzzy-distinct from the incoming value). Lower-tier
    priors still close (the incoming outranks them); higher-tier priors are
    A1-skipped; same-tier fuzzy-SAME priors ("Russian" vs "Russia") still close
    as today; only same-tier fuzzy-DISTINCT priors are spared to coexist.

    Runs on the caller's connection (the close + the subsequent insert are one
    logical step) and is idempotent: only rows still open are fetched, and a
    replay re-fetches an already-closed prior as gone. Returns the number of
    prior rows actually closed.
    """
    # Lazy import — the fuzzy clusterer is a sibling module (Wave 2); importing
    # it only inside the ON branch keeps the OFF hot path and module import free
    # of the dependency, and lets a test substitute it via monkeypatch.
    from .value_clustering import cluster_values

    incoming_rank = (
        None if incoming_source_type is None
        else _source_tier_rank(incoming_source_type)
    )
    # FETCH the candidate open differing-value priors (the SAME selection the
    # OFF UPDATE's WHERE encodes, minus the tier guard — we apply A1 in Python so
    # the fuzzy carve-out can sit alongside it). `id <> new_fact_id` keeps the
    # just-inserting row out (mirrors the UPDATE's `id <> $4`).
    rows = await conn.fetch(
        f"""
        SELECT id, value, source_type
          FROM facts
         WHERE lower(subject)   = lower($1)
           AND lower(predicate) = lower($2)
           AND lower(value)    <> lower($3)
           AND valid_until IS NULL
           AND superseded_by IS NULL
           AND {_LIVE_FACTS}
           AND id <> $4
        """,
        subject,
        predicate,
        value,
        new_fact_id,
    )

    to_close: list[UUID] = []
    for row in rows:
        prior_rank = _source_tier_rank(row["source_type"])
        # A NULL incoming rank is the operator-correction caller — maximally
        # authoritative, NO tier guard and NO coexistence carve-out: it closes
        # every differing-value prior unconditionally, exactly as the OFF path's
        # `$5::int IS NULL` short-circuit does. Only a producer that declared a
        # source_type is subject to A1 + coexistence.
        if incoming_rank is not None:
            # A1 — never close a STRICTLY higher-authority prior (an
            # ingestion/agent fact must not retire a seed/curated one).
            if prior_rank > incoming_rank:
                continue
            # Coexistence carve-out — a SAME-TIER prior whose value is
            # FUZZY-DISTINCT from the incoming one stays OPEN so the detect-only
            # arbiter groups the two next cadence. Fuzzy-SAME ("Russian" vs
            # "Russia" → ONE cluster) is NOT contention; it closes as today.
            if prior_rank == incoming_rank:
                fuzzy_distinct = len(cluster_values([value, row["value"]])) > 1
                if fuzzy_distinct:
                    continue  # COEXIST — leave the prior open.
        to_close.append(row["id"])

    if not to_close:
        return 0

    result = await conn.execute(
        f"""
        UPDATE facts
           SET valid_until   = now(),
               superseded_by = $1, superseded_at = now(),
               updated_at    = now()
         WHERE id = ANY($2::uuid[])
           AND valid_until IS NULL
           AND superseded_by IS NULL
           AND {_LIVE_FACTS}
        """,
        new_fact_id,
        to_close,
    )
    try:
        return int(result.split()[-1]) if result else 0
    except (ValueError, IndexError):                     # pragma: no cover
        return 0


# ---------------------------------------------------------------------------
# FU3 — office-keyed supersession for FUNCTIONAL-ROLE facts (P5 durable
# stale-leader fix)
# ---------------------------------------------------------------------------
#
# supersede_prior_facts keys on (subject, predicate). For a FUNCTIONAL ROLE the
# canonical "current holder" is keyed on the COUNTRY, not the person:
#   * a person-subject 'leader of <country>' fact carries the country in VALUE;
#   * a country-subject 'head of state' / 'head of government' fact carries it
#     in SUBJECT (its office IS the predicate).
# So a re-seed of a NEW office-holder (a DIFFERENT person subject) never closed
# the prior 'leader of <country>' row — the P5 both-open stale-leader
# contradiction migration 0064 had to clean by hand (Biden/Scholz/…). This closes
# every OTHER open row of the same functional role for the SAME country, keyed on
# the country side, regardless of person. The caller scopes it to the
# authoritative seed/curated tier so ingestion contention COEXISTENCE is
# untouched, and — for the person-subject 'leader of' shape — it is role-aware
# (``data->>'role'``) so a dual-office country (Iran supreme leader vs president,
# both 'leader of Iran') is NOT collapsed into one holder.

#: Which column names the COUNTRY for each functional-role predicate (normalized).
_FUNCTIONAL_ROLE_COUNTRY_SIDE: dict[str, str] = {
    "leader of": "value",          # subject=person, value=country
    "head of state": "subject",    # subject=country, value=person
    "head of government": "subject",
}

#: Reused SQL tier-rank CASE (mirrors supersede_prior_facts's A1 guard exactly).
_FUNCTIONAL_ROLE_TIER_CASE = """
        CASE lower(coalesce(source_type, ''))
            WHEN 'seed'      THEN 2
            WHEN 'curated'   THEN 2
            WHEN 'ingestion' THEN 1
            WHEN 'agent'     THEN 1
            ELSE 1
        END
"""


async def supersede_prior_functional_role_facts(
    conn: asyncpg.Connection,
    *,
    subject: str,
    predicate: str,
    value: str,
    role: str | None,
    new_fact_id: UUID,
    incoming_source_type: str | None = None,
) -> int:
    """Office-keyed supersession for a FUNCTIONAL-ROLE fact (FU3 / P5).

    ``predicate`` MUST already be canonical (``normalize_predicate``). Closes
    every OTHER open row of the SAME functional role for the SAME COUNTRY
    (whichever column holds it), pointing them at ``new_fact_id``:

      * 'leader of' — country is VALUE, person is SUBJECT: close prior open
        'leader of <country>' rows with a DIFFERENT person of the SAME office
        (``data->>'role'``, matched CASE-INSENSITIVELY so 'President' vs
        'president' casing drift between re-seeds still closes the prior holder).
        A role-less incoming fact can't safely role-split a dual-office country,
        so it takes NO office-keyed close (the plain (subject, predicate)
        supersession the caller already ran still applies).
      * 'head of state' / 'head of government' — country is SUBJECT, person is
        VALUE, office is the predicate: close prior open rows for the SAME country
        with a DIFFERENT person. (A no-op on the OFF path — supersede_prior_facts
        already closed them — but completes the fold when contention coexistence
        spared a fuzzy-distinct same-tier prior.)

    A1 source-tier guard applies (never closes a STRICTLY higher-authority prior).
    Idempotent (only open rows touched). ``id <> new_fact_id`` plus the person-side
    inequality exclude the just-inserted / collapsed-into row. Returns #closed.
    """
    side = _FUNCTIONAL_ROLE_COUNTRY_SIDE.get(predicate)
    if side is None or not new_fact_id:
        return 0
    incoming_rank = (
        None if incoming_source_type is None
        else _source_tier_rank(incoming_source_type)
    )
    if side == "value":
        # 'leader of' — country=value, person=subject. Role-split guard: only
        # close within the SAME office/role, and only when a role is known.
        if not role:
            return 0
        result = await conn.execute(
            f"""
            UPDATE facts
               SET valid_until   = now(),
                   superseded_by = $1, superseded_at = now(),
                   updated_at    = now()
             WHERE lower(predicate) = $2
               AND lower(value)     = lower($3)
               AND lower(subject)  <> lower($4)
               AND lower(coalesce(data->>'role', '')) = lower($5)
               AND valid_until IS NULL
               AND superseded_by IS NULL
               AND {_LIVE_FACTS}
               AND id <> $1
               AND ($6::int IS NULL OR {_FUNCTIONAL_ROLE_TIER_CASE} <= $6::int)
            """,
            new_fact_id, predicate, value, subject, role, incoming_rank,
        )
    else:
        # 'head of state' / 'head of government' — country=subject, person=value.
        result = await conn.execute(
            f"""
            UPDATE facts
               SET valid_until   = now(),
                   superseded_by = $1, superseded_at = now(),
                   updated_at    = now()
             WHERE lower(predicate) = $2
               AND lower(subject)   = lower($3)
               AND lower(value)    <> lower($4)
               AND valid_until IS NULL
               AND superseded_by IS NULL
               AND {_LIVE_FACTS}
               AND id <> $1
               AND ($5::int IS NULL OR {_FUNCTIONAL_ROLE_TIER_CASE} <= $5::int)
            """,
            new_fact_id, predicate, subject, value, incoming_rank,
        )
    try:
        return int(result.split()[-1]) if result else 0
    except (ValueError, IndexError):                     # pragma: no cover
        return 0


async def collapse_open_triple(
    conn: asyncpg.Connection,
    *,
    subject: str,
    predicate: str,
    value: str,
    new_fact_id: UUID,
    confidence: float,
    derived_from: Sequence[UUID],
    valid_from: datetime | None = None,
    source_credibility: float | None = None,
) -> UUID | None:
    """Collapse a standing fact triple onto ONE open row regardless of
    ``valid_from`` drift (D17 — full-triple supersession leaked open duplicates
    via per-cycle valid_from drift).

    The ``idx_facts_temporal_triple_open`` partial-unique index keys on the FULL
    quad INCLUDING ``COALESCE(valid_from, '1970-01-01')``, so the SAME
    ``(subject, predicate, value)`` re-asserted from N cycles with N distinct
    event-times accumulates N OPEN rows — the live "Russia located in UK" ×8
    noise the ON CONFLICT upsert never catches (its conflict target carries the
    valid_from dimension, so a drifted valid_from is a NEW conflict key, not a
    hit).

    This helper closes that dimension for SAME-value OPEN rows BEFORE the
    insert: if an open row for ``(lower(subject), lower(predicate),
    lower(value))`` already exists (ANY valid_from), it is refreshed in place
    (confidence → noisy-OR combine capped at 0.99 per A2, lineage unioned,
    EARLIEST valid_from kept) and its id is
    returned so the caller SKIPS the insert. Returns ``None`` when no open row
    exists (the caller proceeds to insert the fresh open row).

    Contract / safety:
      * **same-value only** — the match is on ``lower(value) = lower($3)``; a
        DIFFERENT value is NOT collapsed here (that path is
        :func:`supersede_prior_facts`, which the caller runs first).
      * **open-only** — only rows still open (``valid_until IS NULL AND
        superseded_by IS NULL``) are touched; a closed/superseded row is never
        resurrected (matches the partial index's WHERE).
      * **confidence aggregation on agreement (A2)** — a replay of the same
        triple is CORROBORATION from another source, so confidence is combined
        with a bounded noisy-OR (``1 - (1-existing)*(1-incoming)``, clamped to
        ``<= 0.99``) rather than lifted to the plain max. N agreeing sources
        therefore raise confidence ABOVE any single one yet never reach
        certainty. Before this was ``GREATEST`` (max), which never rose above
        the single most-confident source. Lineage is still unioned; the row
        count is unchanged (idempotent in row terms, monotone-increasing in
        confidence toward the 0.99 cap).
      * **deterministic pick** — when (legacy data) more than one open row for
        the triple exists, the EARLIEST (``valid_from ASC, created_at ASC``) is
        refreshed; the caller's :func:`supersede_prior_facts` already collapsed
        differing-value rows, and future writes converge on this one open row.

    Runs on the caller's connection so the collapse + insert are one logical
    step. Shared by BOTH fact producers (the analyst ``_insert_fact`` path and
    the ingest ``fact_extractor._insert_ingestion_fact`` path) so a standing
    triple keeps ONE open row across producers.
    """
    existing_id = await conn.fetchval(
        f"""
        UPDATE facts
           -- A2: bounded noisy-OR combine of agreeing confidences, capped at
           -- 0.99 so corroboration raises belief above any single source but
           -- never reaches certainty (was GREATEST/max).
           SET confidence   = LEAST(
                                 0.99,
                                 1.0 - (1.0 - facts.confidence) * (1.0 - $4)
                               ),
               derived_from = COALESCE((SELECT array_agg(DISTINCT e)
                               FROM unnest(facts.derived_from || $5::uuid[]) e),
                              '{{}}'::uuid[]),
               -- LEAST/GREATEST skip NULL args in Postgres (NULL only if ALL
               -- are NULL), so this keeps the EARLIEST known valid_from and is
               -- a no-op when either side is NULL — matches the ingest path.
               valid_from   = LEAST(facts.valid_from, $6),
               -- Holes-B Wave 0: a corroborating re-assert keeps the MOST
               -- credible backing source. GREATEST skips NULLs, so an unscored
               -- side never lowers a known credibility; NULL only if both NULL.
               source_credibility = GREATEST(facts.source_credibility, $8),
               updated_at   = now()
         WHERE id = (
                 SELECT id FROM facts
                  WHERE lower(subject)   = lower($1)
                    AND lower(predicate) = lower($2)
                    AND lower(value)     = lower($3)
                    AND valid_until IS NULL
                    AND superseded_by IS NULL
                    AND {_LIVE_FACTS}
                    AND id <> $7
                  ORDER BY valid_from ASC, created_at ASC
                  LIMIT 1
               )
        RETURNING id
        """,
        subject,
        predicate,
        value,
        float(confidence),
        list(derived_from),
        valid_from,
        new_fact_id,
        source_credibility,
    )
    return existing_id

async def supersede_prior_nexuses(
    conn: asyncpg.Connection,
    *,
    subject: str,
    intermediary: str | None,
    object_: str,
    rel_type: str,
    polarity: int,
    label: str,
    new_nexus_id: UUID,
) -> int:
    """Close any open nexus(es) for the typed triple
    ``(lower(subject), lower(COALESCE(intermediary,'')), lower(object),
    lower(rel_type))`` whose VALUE (polarity OR label) differs from the
    incoming one, pointing them at ``new_nexus_id`` (PIECE A — mirrors
    :func:`supersede_prior_facts`).

    The canonical "what holds now" for a reified relationship is the single
    open row (``valid_until IS NULL AND superseded_by IS NULL``). When the
    reifier re-types the SAME triple with a DIFFERENT polarity sign or label,
    the prior open row(s) are closed (``valid_until = now()`` +
    ``superseded_by = <new id>``) and the new row is inserted open by the
    caller.

    Contract / safety (identical to facts):
      * **value-differs only** — a re-assert of the SAME polarity AND label is
        NOT a supersession; that path stays the ``idx_nexuses_triple_open``
        ``ON CONFLICT`` upsert (confidence lift + lineage union). The
        ``(polarity <> $5 OR lower(label) <> lower($6))`` predicate guarantees
        the identical row is never closed by its own re-ingest.
      * **idempotent** — only OPEN rows are touched; a replay closes nothing
        new once the prior is already superseded.
      * **same connection** — the caller runs this immediately before the
        insert on the same ``conn`` so close + open are one logical step.

    Returns the number of prior rows closed.
    """
    result = await conn.execute(
        """
        UPDATE nexuses
           SET valid_until   = now(),
               superseded_by = $7,
               updated_at    = now()
         WHERE lower(subject)                  = lower($1)
           AND lower(COALESCE(intermediary,'')) = lower(COALESCE($2, ''))
           AND lower(object)                   = lower($3)
           AND lower(rel_type)                 = lower($4)
           AND (polarity <> $5 OR lower(label) <> lower($6))
           AND valid_until IS NULL
           AND superseded_by IS NULL
           AND id <> $7
        """,
        subject,
        intermediary,
        object_,
        rel_type,
        int(polarity),
        label,
        new_nexus_id,
    )
    try:
        return int(result.split()[-1]) if result else 0
    except (ValueError, IndexError):                     # pragma: no cover
        return 0
