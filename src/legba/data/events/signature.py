# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The event signature (DATA MODEL V3 / P0 — spec §2.2).

An event is a bounded occurrence keyed by its own identity — not a frame like
``sig:<topic>#dim:<dimension>``. Its signature is minted from the promoted
cluster, not used to form it::

    evt:<topic>|<top-K identity-folded entity tokens>#evt:<anchor_token(primary polity)>

* ``#evt:`` is a RESERVED grammar slot that already exists and nothing else
  writes — ``finding_supersession._SIGNATURE_EVENT_MARKER == "#evt:"`` and the
  existing parsers (``with_dimension`` / ``signature_dimension`` /
  ``strip_dimension``, all ``rsplit(marker, 1)``) already read past it. This
  module's :data:`EVENT_MARKER` must equal that constant; a test pins it.
* ``|`` — the top-K entity segment. K > 0 deliberately: the situations lesson
  is ``finding_supersession._SITUATION_SIGNATURE_ENTITY_K == 0``, where the
  entity half was turned OFF for frames because it fragmented them. An event
  is narrower than a frame and its actor set is the greater part of its
  identity, so K belongs above zero here. :data:`EVENT_SIGNATURE_ENTITY_K` is
  the tunable — P1's acceptance measures fragmentation at K in {0, 2, 3, 4}.
* ``#evt:`` — :func:`anchor_token` over the primary polity. The primary polity
  is the modal ISO2 across the member signals' ``geo`` arrays (the caller's
  derivation — both the SQL backfill and the Python handler take it the same
  way); ``anchor_token`` slugifies whatever it is given, and ``None`` /
  unresolvable mints the ``_domestic`` residue, never an empty slot.

THE TWIN CONTRACT
-----------------
The 0205 backfill mints signatures in SQL and the P1 handler mints them in
Python — :data:`EVENT_SIGNATURE_SQL` is the Postgres twin, asserted
row-for-row against the Python builder by
``test_event_signature_python_and_sql_agree``.

Its declared input domain is CANONICAL entity surfaces —
``entity_profiles.canonical_name`` and surfaces already canonicalized the same
way. On that domain ``identity_fold`` reduces to its last mile — the
leading-article strip (with its never-blank guard), lowercase, and the
non-alphanumeric collapse — because every heavier transform (alias / demonym /
region / plural collapse, residue strip, junk gating) already fired at
canonical-write time. The twin additionally mirrors the junk predicates a
canonical surface could still trip: the ``length > 2`` gate and the four
literal junk sets. Callers must pass canonical names — a raw NER span can
diverge (e.g. a demonym folds to its country only in Python), and the twin
test over the live ``canonical_name`` population is the guard that keeps the
contract honest.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from .._entity_canon import identity_fold, is_junk_entity
from .._frame_anchor import anchor_token

#: The reserved signature-grammar slot — must equal
#: ``finding_supersession._SIGNATURE_EVENT_MARKER`` (the test pins it; the
#: module stays import-light so the write path never pulls the handler chain).
EVENT_MARKER: str = "#evt:"

#: The top-K entity segment width. A tunable, not a guess — P1's acceptance
#: measures fragmentation at K in {0, 2, 3, 4} and re-seeds the default from
#: the sweep.
EVENT_SIGNATURE_ENTITY_K: int = 3


def signature_entity_tokens(
    entity_names: Iterable[Any] | None,
    *,
    entity_k: int = EVENT_SIGNATURE_ENTITY_K,
) -> tuple[str, ...]:
    """The ``|``-segment: top-K identity-folded, junk-free, deduped tokens.

    Each surface is folded through :func:`identity_fold` (the class-agnostic
    key — alias / demonym / article / case / punctuation all collapse) after
    the :func:`is_junk_entity` gate drops non-referents; folds are deduped and
    sorted so the segment is order-insensitive, then capped at ``entity_k``.
    The SQL twin takes the identical last mile over canonical surfaces.
    """
    folds: set[str] = set()
    for name in entity_names or ():
        text = str(name or "").strip()
        if not text or is_junk_entity(text):
            continue
        fold = identity_fold(text)
        if fold:
            folds.add(fold)
    return tuple(sorted(folds)[: max(entity_k, 0)])


def event_signature(
    topic: Any,
    entity_names: Iterable[Any] | None,
    primary_polity: Any,
    *,
    entity_k: int = EVENT_SIGNATURE_ENTITY_K,
) -> Optional[str]:
    """Mint ``evt:<topic>|<tokens>#evt:<anchor>`` — or ``None`` when nothing keys it.

    ``topic`` is lowercased and trimmed like the situation topic; an empty
    topic falls back to the first entity token (the strongest entity anchors
    the signature — the ``derive_signature`` idiom), and when neither exists
    there is no identity to key on, so the caller skips the candidate rather
    than minting an empty signature.
    """
    tokens = signature_entity_tokens(entity_names, entity_k=entity_k)
    t = str(topic or "").strip().lower()
    if not t and tokens:
        t = tokens[0]
    if not t:
        return None
    tail = f"|{','.join(tokens)}" if tokens else ""
    return f"evt:{t}{tail}{EVENT_MARKER}{anchor_token(primary_polity)}"


# ---------------------------------------------------------------------------
# THE POSTGRES TWIN
# ---------------------------------------------------------------------------
#
# An EXPRESSION over ($1::text topic, $2::text[] entity_names, $3::text polity,
# $4::int entity_k), the same form ANCHOR_TOKEN_SQL ships — a migration embeds
# it, and `SELECT <expr>` with the four params bound evaluates it standalone.
# The anchor half is ANCHOR_TOKEN_SQL verbatim with $3 in the $1 slot. The
# entity half takes identity_fold's last mile — article strip (with the
# never-blank guard: the stripped remainder must still be >= 2 chars or the
# name is kept whole), lowercase, non-alnum collapse — plus the length > 2
# gate and the four literal junk sets (_JUNK_ENTITIES / _STOPWORD_ENTITIES /
# _NUMBER_WORD_ENTITIES / _VAGUE_ENDPOINT_TOKENS + _TRUNCATED_INSTITUTION_
# FRAGMENTS + _QUANTIFIER_PLURAL_ENTITIES), which are the only junk rules a
# canonical surface can still trip. The pattern-based junk classes (clock /
# money / quantifier / HTML residue) are unreachable on the canonical domain:
# the write path junk-gates before a name is ever stored.
#
# Token order is COLLATE "C" — byte/codepoint order, exactly Python's sorted()
# over the a-z0-9 alphabet the fold emits — so a locale collation can never
# reorder the segment.

EVENT_SIGNATURE_SQL: str = """
WITH toks AS (
    SELECT DISTINCT
           regexp_replace(
               lower(
                   CASE WHEN btrim(e.n) ~* '^(the|a|an)\\y'
                         AND char_length(btrim(regexp_replace(btrim(e.n),
                               '^(the|a|an)\\y\\s*', '', 'i'))) >= 2
                        THEN btrim(regexp_replace(btrim(e.n),
                               '^(the|a|an)\\y\\s*', '', 'i'))
                        ELSE btrim(e.n)
                   END),
               '[^a-z0-9]+', '', 'g'
           ) AS tok
      FROM unnest(COALESCE($2::text[], '{}'::text[])) AS e(n)
     WHERE char_length(btrim(e.n)) > 2
       AND lower(btrim(e.n)) <> ALL (ARRAY[
           -- _JUNK_ENTITIES
           'tv','radio','online',
           -- _STOPWORD_ENTITIES
           'the','a','an','and','or','but','nor','of','to','in','on','at',
           'by','for','from','with','as','into','onto','off','out','up','down',
           -- _NUMBER_WORD_ENTITIES (cardinals + ordinals)
           'zero','one','two','three','four','five','six','seven','eight',
           'nine','ten','eleven','twelve','thirteen','fourteen','fifteen',
           'sixteen','seventeen','eighteen','nineteen','twenty','thirty',
           'forty','fifty','sixty','seventy','eighty','ninety','hundred',
           'thousand','million','billion','trillion','dozen','couple',
           'first','second','third','fourth','fifth','sixth','seventh',
           'eighth','ninth','tenth','eleventh','twelfth','thirteenth',
           'twentieth','thirtieth',
           -- _VAGUE_ENDPOINT_TOKENS
           'west','western','eastern','northern','southern',
           'islamic','islamist','leader','leaders','leadership',
           'annual','annually','yearly','daily','weekly','monthly',
           'quarterly','biannual','semiannual','biennial','resistance',
           -- _TRUNCATED_INSTITUTION_FRAGMENTS
           'parl','fed',
           -- _QUANTIFIER_PLURAL_ENTITIES
           'hundreds','thousands','millions','billions','trillions','dozens'
       ]::text[])
),
picked AS (
    SELECT tok FROM toks WHERE tok <> '' ORDER BY tok COLLATE "C"
    LIMIT GREATEST($4::int, 0)
),
agg AS (
    SELECT string_agg(tok, ',' ORDER BY tok COLLATE "C") AS tail,
           (array_agg(tok ORDER BY tok COLLATE "C"))[1] AS first_tok
      FROM picked
),
topic AS (
    SELECT NULLIF(lower(btrim(COALESCE($1::text, ''))), '') AS t
)
SELECT CASE
           WHEN COALESCE((SELECT t FROM topic), (SELECT first_tok FROM agg))
                IS NULL THEN NULL
           ELSE 'evt:'
                || COALESCE((SELECT t FROM topic), (SELECT first_tok FROM agg))
                || COALESCE('|' || (SELECT tail FROM agg), '')
                || '#evt:'
                || COALESCE(
                     NULLIF(
                       btrim(
                         left(
                           btrim(
                             regexp_replace(
                               lower(btrim(COALESCE($3::text, ''))),
                               '[^a-z0-9]+', '_', 'g'
                             ),
                             '_'
                           ),
                           64
                         ),
                         '_'
                       ),
                       ''
                     ),
                     '_domestic'
                   )
       END
"""
