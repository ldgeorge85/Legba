# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``reifier_alias_pairs`` — "these two names are one entity" is not an edge (K-G2).

THE OBSERVATION
---------------
The bake-off's floor stratum produced accepted verdicts like
``IRGC AffiliatedWith Revolutionary Guards Corps``,
``Council of the IMO PartOf IMO`` and ``Central Ben Hill County LocatedIn
Georgia`` — **true, and worthless** (``docs/TYPING_BAKEOFF_2026-08-03.md`` §6.5).
No model filters them, because they *are* related. They are the same referent
wearing two surfaces.

The existing defences do not catch this class, and cannot:

  * :func:`~legba.data._entity_canon.same_referent` folds demonyms and
    singular/plural ("Iran"/"Iranian", "Houthi"/"Houthis") — a lexical relation;
  * the N4 keeper gate drops a pair whose endpoints elect the SAME
    ``entity_profiles`` keeper — which requires the merge to have already
    happened.

``IRGC`` and ``Revolutionary Guards Corps`` share no lexical form and no keeper.
They pass both gates and mint an edge that says an organisation is affiliated
with itself. Worse, that edge then makes the graph look denser than it is.

WHAT THIS MODULE DOES
---------------------
The typer is now asked one extra question per candidate — ``same_entity`` — and
when it says yes, **no edge is minted**. The pair is counted and routed to
``entity_judgement``, which is the tree's purpose-built merge-adjudication
surface: an order-independent ``pair_key``, a ``verdict`` vocabulary of
``same``/``not_same``/``unsure``, and a ``decided_by`` lane for rule/llm/human.

THE ROW WE WRITE, AND WHY IT IS SHAPED THIS WAY
----------------------------------------------
``verdict='unsure'``, ``decided_by='rule'``, ``model_id=`` :data:`ALIAS_PAIR_MODEL_ID`.

Writing ``verdict='same'`` would over-claim: the typer was asked to type a
relationship, not to adjudicate an identity, and it answered a side question.
``unsure`` is the honest value — *a rule noticed this pair; nobody has
adjudicated it*.

The ``model_id`` tag is load-bearing, not decoration. ``entity_judgement``
doubles as ``entity_researcher``'s **re-adjudication cache**: a pair with a row
is never re-sent to the LLM. An untagged row here would therefore SUPPRESS the
real adjudication of exactly the pairs it was trying to surface — and if it
carried ``verdict='same'``, ``execute_merges`` would act on a relationship
typer's side answer. So the row is tagged and
:func:`~legba.data.analysts.entity_researcher._load_cached` excludes the tag,
which makes the proposal inert to both paths by construction rather than by
hope. It stays exactly one thing: a queryable merge candidate.

``ON CONFLICT (pair_key) DO NOTHING`` — a real verdict (human or LLM) already on
the pair is never touched.

Both sides must resolve to live ``entity_profiles`` rows, because ``pair_key``
is built from entity IDs (``_entity_candidates.CandidatePair.pair_key``) and a
merge candidate naming something that is not an entity is not a candidate. When
either side does not resolve, the pair is still COUNTED and logged — the count
is the honest floor, and it never silently drops to zero.
"""

from __future__ import annotations

import logging
from typing import Any

from .._entity_resolve import resolve_keeper

logger = logging.getLogger(__name__)

#: Tags every ``entity_judgement`` row this module writes.
#:
#: The discriminator that keeps a proposal from becoming a decision. Nothing
#: else in the tree writes this value, so excluding it from the adjudication
#: cache is provably a no-op for existing behaviour — see the module docstring.
ALIAS_PAIR_MODEL_ID: str = "relationship_reifier:alias_pair"

#: The lane. ``entity_judgement.decided_by`` is CHECK-constrained to
#: rule/llm/human; a deterministic observation is a ``rule``.
ALIAS_PAIR_DECIDED_BY: str = "rule"

#: An observation is not an adjudication. See the module docstring for why this
#: is not ``'same'``.
ALIAS_PAIR_VERDICT: str = "unsure"

#: Resolve an already-keeper-resolved surface to its live ``entity_profiles``
#: id. Mirrors the active-keeper guard used by the E3 candidate generator: a
#: de-fragmentation LOSER (``gc_status`` merged/junk, or a ``merged_into``
#: tombstone) is never a valid side of a merge candidate.
_ENTITY_ID_SQL = """
SELECT id FROM entity_profiles
 WHERE lower(btrim(canonical_name)) = lower(btrim($1))
   AND COALESCE(data->>'gc_status', '') NOT IN ('merged', 'junk')
   AND merged_into IS NULL
 ORDER BY created_at ASC
 LIMIT 1
"""

_INSERT_SQL = """
INSERT INTO entity_judgement
    (pair_key, entity_a, entity_b, verdict, justification, decided_by,
     model_id, confidence)
VALUES ($1, $2::uuid, $3::uuid, $4, $5, $6, $7, $8)
ON CONFLICT (pair_key) DO NOTHING
"""


def pair_key_for(entity_a: str, entity_b: str) -> str:
    """Order-independent key over two entity IDs.

    Byte-identical to ``_entity_candidates.CandidatePair.pair_key`` so a row
    written here collides correctly with one the E3/E4 path would write for the
    same pair — which is what makes ``ON CONFLICT DO NOTHING`` mean "a real
    verdict already exists".
    """
    lo, hi = sorted((str(entity_a), str(entity_b)))
    return f"{lo}::{hi}"


async def record_alias_pair(
    conn: Any,
    subject: str,
    object_: str,
    *,
    confidence: float | None = None,
    keeper_cache: dict[str, str] | None = None,
) -> str:
    """Route one "these are the same entity" observation. NEVER raises.

    Returns the outcome, for the run counters:

    ``"recorded"``   a new merge-candidate row landed;
    ``"duplicate"``  the pair already carries a verdict — left untouched;
    ``"unresolved"`` one or both surfaces are not live entities (counted + logged);
    ``"failed"``     the write errored (counted + logged).
    """
    try:
        keeper_a = (
            await resolve_keeper(
                conn, subject, entity_class="entity", cache=keeper_cache
            )
        ).strip() or subject
        keeper_b = (
            await resolve_keeper(
                conn, object_, entity_class="entity", cache=keeper_cache
            )
        ).strip() or object_

        id_a = await conn.fetchval(_ENTITY_ID_SQL, keeper_a)
        id_b = await conn.fetchval(_ENTITY_ID_SQL, keeper_b)
        if id_a is None or id_b is None or str(id_a) == str(id_b):
            # Not routable as a merge candidate: an endpoint is not a live
            # entity row, or the two already ARE one entity (in which case the
            # keeper gate has it and there is nothing to propose).
            logger.info(
                "reifier_alias_pairs.unresolved a=%r b=%r a_id=%s b_id=%s",
                keeper_a, keeper_b, id_a, id_b,
            )
            return "unresolved"

        key = pair_key_for(id_a, id_b)
        result = await conn.execute(
            _INSERT_SQL,
            key,
            id_a,
            id_b,
            ALIAS_PAIR_VERDICT,
            (
                f"relationship_reifier typed {keeper_a!r} and {keeper_b!r} as the "
                "same entity while typing a co-mention; surfaced as a merge "
                "candidate, NOT adjudicated"
            )[:4000],
            ALIAS_PAIR_DECIDED_BY,
            ALIAS_PAIR_MODEL_ID,
            float(confidence) if confidence is not None else None,
        )
        # asyncpg returns 'INSERT 0 0' when ON CONFLICT DO NOTHING skipped.
        inserted = str(result or "").strip().endswith("1")
        if inserted:
            logger.info(
                "reifier_alias_pairs.recorded pair=%s a=%r b=%r", key, keeper_a,
                keeper_b,
            )
        return "recorded" if inserted else "duplicate"
    except Exception as exc:  # degrade-not-break: never sink a typing run
        logger.warning(
            "reifier_alias_pairs.failed a=%r b=%r err=%s", subject, object_, exc
        )
        return "failed"


__all__ = [
    "ALIAS_PAIR_MODEL_ID",
    "ALIAS_PAIR_DECIDED_BY",
    "ALIAS_PAIR_VERDICT",
    "pair_key_for",
    "record_alias_pair",
]
