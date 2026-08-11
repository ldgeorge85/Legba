# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CW-4 / CW-5 — the two guards the ``fact_contention`` question class needs.

``fact_contention`` questions are the one harvested class ``claim_watch``
deliberately still scores: "which value of 'operates in' for 'israel defense
forces' is correct?" is a question about the WORLD, and new reporting genuinely
can settle it. K-4 round 3 labeled 40 of them and found the class is not one
population but two, cleanly split::

    idf/operates in, israel defense forces/operates in,
    saudi arabia/conflict with, japan/part of, houthis/located in,
    gironde/located in, sumy/located in, sarah morris/located in,
    pakistan/part of                                    18/20 = 0.900

    israel/part of, madrid/capital of, madrid/border with,
    bordeaux/part of, the indian ocean/operates in, texas/located in,
    washington/ally of, yemen/controls, kiev/conflict with,
    kremlin/spokesperson for, kiev/spokesperson for      0/20 = 0.000

Not a weighting problem. The second group is dead relations and junk triples,
and no amount of cosine or entity overlap distinguishes "is the IDF operating
in Nablus" (which the wire reports every day) from "is Madrid the capital of
France" (which nothing will ever report, because it is a metonymy artifact —
see the fact-plane guard in :mod:`legba.data.filters.fact_extractor`).

Two guards, both deterministic, both monotone (they can only ever REMOVE a
candidate), both counted.

CW-5 — THE SUBJECT ANCHOR (the lever that measured)
---------------------------------------------------
The matcher was edging these questions off the CONTESTED VALUE and the desk
furniture, never checking that the signal was about the SUBJECT at all. The
clearest case in the round: the question "which value of 'located in' for
**texas** is correct? (surfaced winner: 'spacex')" matched a SpaceX Starship
recovery story with **no Texas token anywhere in it** — labeled spurious. Four
more rows matched "which value of 'capital of' for **madrid**" against
Ceuta/Morocco migrant-crossing stories that never mention Madrid.

So a contention pair now requires the SUBJECT to be present in the signal:
literally in its text, or as one of its resolved canonical entity names (an
"idf" question matches an "Israel Defense Forces"-linked signal, and "saudi
arabia" matches a story headlined "Saudis"). Alias resolution is what keeps
this from being a cheap substring test — 7 of the round's correct matches
name the subject only through an alias, and a literal-only anchor would have
thrown all 7 away.

CW-4 — THE LIVENESS FILTER, MEASURED AND SHIPPED **OFF**
--------------------------------------------------------
The ranked finding proposed a liveness filter for the second group, on the
theory that dead relations stop being asserted. :data:`LIVENESS_SQL` implements
exactly that against the house signals — ``fact_contention_values``'
``latest_asserted_at`` (falling back to the group's ``opened_at``, since a
freshly opened group is live by construction) plus the ``collapsed`` status the
arbiter sets when a dispute resolves. It is default-DISABLED
(:data:`DEFAULT_CONTENTION_LIVENESS_DAYS` = 0), because it was measured and it
does not work. Two independent reasons, both worth writing down:

**1. The signal does not separate the two groups.** Read-only against the live
substrate on 2026-08-03, over the 12 R3 groups still present: the 0.900 group's
newest assertions span 18-77h and the 0.000 group's span 1-83h — interleaved,
not separated (``sumy`` at 77h scored a correct match; ``bordeaux`` at 74h and
``washington`` at 74.5h scored none). ``decay_state`` read ``fresh`` for all
5,660 contention representative facts, and ``sighting_source`` did not separate
them either. A window drawn anywhere in that range is fitted to 12 points and
would cost real evidence the first time a live relation went quiet over a
weekend.

**2. ``collapsed`` is downstream of SUCCESS, so filtering on it is backwards.**
Replayed over the 120 gold rows (``scripts/replay_k4_r3.py --db``), the
liveness filter removes 15 rows: **7 correct matches and 8 false ones**. That
is a worse-than-random cut — the base rate is 60% false — and it drags the
train's cumulative numbers from 0.664/71.8% down to 0.634/62.9%. The mechanism
is not mysterious: a contention group COLLAPSES when the arbiter resolves the
dispute, and the dispute gets resolved because good evidence arrived. Groups
that collapsed between the 07-31 labels and the 08-03 replay are
disproportionately the ones a correct match helped settle.

(That second reading also exposes a real limit of replaying a TIME-VARYING
filter retrospectively: it judges 07-31 matches against 08-03 group state. But
a filter that cannot be shown to help, on the only evidence that exists, does
not ship armed. ``contention_liveness_days`` is the switch when a signal that
separates these two groups turns up; the finding-4 population is meanwhile
removed by CW-5 above, which measures.)
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, NamedTuple

from ..._entity_canon import canonicalize_entity

__all__ = [
    "DEFAULT_CONTENTION_LIVENESS_DAYS",
    "MAX_ANCHOR_TEXT_CHARS",
    "ContentionKey",
    "LIVENESS_SQL",
    "anchor_text",
    "contention_key",
    "live_contention_ids",
    "subject_anchored",
    "subject_surfaces",
]


#: How long a contested relation may go without a fresh assertion before the
#: watch stops treating it as live. **0 = DISABLED, which is what ships.** The
#: filter is built, tested and one option away, but the replay measured it
#: removing 7 correct matches for 8 false ones against a 60% base false rate —
#: see the module docstring. A guard that cannot be shown to help does not ship
#: armed; arming it is a measurement, not a preference.
DEFAULT_CONTENTION_LIVENESS_DAYS = 0.0

#: Bounded signal text for the anchor test. Larger than the gate's 600-char
#: digest on purpose — the subject frequently appears once, in the body, and
#: a 600-char window would manufacture "unanchored" for real evidence.
MAX_ANCHOR_TEXT_CHARS = 4000

#: The harvested contention thesis, verbatim from
#: ``scripts/harvest_open_questions.py::collect_fact_contentions``. Parsed
#: rather than re-queried because the thesis IS the durable statement of what
#: the question is about — a question whose text and whose group had drifted
#: apart should follow its text.
_CONTENTION_RE = re.compile(
    r'^Contested fact:\s*which value of\s*"(?P<predicate>[^"]+)"\s*'
    r'for\s*"(?P<subject>[^"]+)"\s*is correct\?',
    re.IGNORECASE,
)

#: Tokens stripped from a subject surface before matching. A contention
#: subject_key is already lower-cased and space-normalised by the arbiter
#: ("the prosecutor general 's office"), so the tokenizer's spaced possessive
#: has to be re-joined or nothing will match the article's "Prosecutor
#: General's Office".
_POSSESSIVE_RE = re.compile(r"\s+'s\b")

#: Words too generic to anchor anything on their own. A one-word subject from
#: this set would make the guard inert for that question — better to let the
#: pair through on the other planes than to pretend "today" anchored it.
_UNANCHORABLE: frozenset[str] = frozenset({
    "today", "yesterday", "tomorrow", "us", "we", "they", "it", "he", "she",
    "the", "a", "an", "this", "that", "one", "two", "new", "news",
})


class ContentionKey(NamedTuple):
    """The (subject, predicate) a harvested contention question is about."""

    subject: str
    predicate: str


def contention_key(thesis: Any) -> ContentionKey | None:
    """The contested (subject, predicate) in a harvested thesis, or ``None``.

    ``None`` for every other question shape, which is the whole scoping of
    both guards: they apply to contention questions and to nothing else.
    """
    text = " ".join(str(thesis or "").split())
    m = _CONTENTION_RE.match(text)
    if m is None:
        return None
    subject = m.group("subject").strip().lower()
    predicate = m.group("predicate").strip().lower()
    if not subject or not predicate:
        return None
    return ContentionKey(subject, predicate)


#: Leading words dropped before taking a subject's initials. "the indian
#: ocean" must not acronym to "tio".
_ACRONYM_STOPWORDS: frozenset[str] = frozenset({"the", "a", "an", "of", "and"})

#: Shortest acronym worth trusting. Two letters collide with far too much
#: ordinary text to be an identity claim ("the indian ocean" -> "io"); three
#: is where the real ones live (IDF, IRGC, PMF, SPR).
_MIN_ACRONYM_LEN = 3


def _acronym(raw: str) -> str:
    """The initials of a multi-word subject, or ``''``.

    The entity plane does not merge "idf" with "israel defense forces" (they
    elect as separate keepers), so a question about one cannot reach a signal
    linked to the other through the canon. Initials bridge exactly that gap
    and, at three letters word-bounded, essentially nothing else.
    """
    words = [w for w in raw.split() if w not in _ACRONYM_STOPWORDS and w[:1].isalpha()]
    if len(words) < 2:
        return ""
    letters = "".join(w[0] for w in words)
    return letters if len(letters) >= _MIN_ACRONYM_LEN else ""


def subject_surfaces(subject: str) -> set[str]:
    """Every surface the subject may legitimately appear as in a signal.

    The raw ``subject_key``, its possessive-rejoined form, its canonical
    entity name, and — for a multi-word subject — its initials. The canon is
    what makes "saudi arabia" reachable from a story headlined "Saudis"; the
    initials are what make "israel defense forces" reachable from one linked
    only to "IDF". 7 of R3's correct matches name the subject ONLY through an
    alias, so a literal-only anchor would have discarded all of them along
    with the spurious rows.
    """
    raw = " ".join(str(subject or "").split()).strip().lower()
    if not raw:
        return set()
    out = {raw, _POSSESSIVE_RE.sub("'s", raw)}
    try:
        canon, _cls = canonicalize_entity(raw, "entity")
    except Exception:  # noqa: BLE001 — the canon is an enrichment, not a gate
        canon = ""
    if canon:
        out.add(canon.strip().lower())
    acronym = _acronym(raw)
    if acronym:
        out.add(acronym)
    return {s for s in out if s and s not in _UNANCHORABLE}


def _contains_surface(haystack: str, surface: str) -> bool:
    """Word-bounded containment. Bounded so "us" does not match "thus" and
    "tal" does not match "total" — a substring anchor is not an anchor."""
    if not surface:
        return False
    return re.search(rf"(?<!\w){re.escape(surface)}(?!\w)", haystack) is not None


def subject_anchored(
    subject: str,
    *,
    signal_text: str = "",
    signal_names: Iterable[str] = (),
) -> bool:
    """Is the contested SUBJECT actually present in this signal?

    Two surfaces, either sufficient: the signal's TEXT (word-bounded, so a
    substring is never mistaken for a name) and its resolved canonical entity
    names. Returns True when the subject has no anchorable surface at all —
    an inert guard is correct there; refusing every pair for a subject the
    canon cannot represent would be the guard failing, not the pair.
    """
    surfaces = subject_surfaces(subject)
    if not surfaces:
        return True
    text = (signal_text or "").lower()
    names = {str(n or "").strip().lower() for n in signal_names}
    names.discard("")
    for surface in surfaces:
        if surface in names:
            return True
        if text and _contains_surface(text, surface):
            return True
    return False


def anchor_text(payload: Any, *, max_chars: int = MAX_ANCHOR_TEXT_CHARS) -> str:
    """Lower-cased, whitespace-collapsed signal text for the anchor test.

    Deliberately NOT :func:`bearing_gate.signal_digest`: that is a 600-char
    prompt digest picking ONE body field, and the anchor wants every field it
    can cheaply see, because a contested subject often appears once, deep in
    the body.
    """
    if isinstance(payload, str):
        import json

        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            payload = {}
    if not isinstance(payload, Mapping):
        return ""
    parts: list[str] = []
    for key in (
        "title", "headline", "name",
        "distilled_body", "summary", "description", "body", "text", "content",
    ):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val)
    if not parts:
        return ""
    return re.sub(r"\s+", " ", " ".join(parts)).lower()[:max_chars]


# ---------------------------------------------------------------------------
# CW-4 — liveness
# ---------------------------------------------------------------------------

#: Contention groups that are still LIVE, by group id.
#:
#: "Live" = not ``collapsed`` (the arbiter's own resolved marker) AND carrying
#: a non-junk value asserted inside the window, where a group whose values have
#: no ``latest_asserted_at`` at all falls back to its ``opened_at`` — a freshly
#: opened group is live by construction and must not be muted by a column the
#: arbiter has not filled in yet.
LIVENESS_SQL = """
    SELECT fc.id::text AS id
      FROM fact_contention fc
      LEFT JOIN fact_contention_values fcv
             ON fcv.contention_id = fc.id AND NOT fcv.is_junk
     WHERE fc.id = ANY($1::uuid[])
       AND fc.status <> 'collapsed'
     GROUP BY fc.id, fc.opened_at
    HAVING COALESCE(max(fcv.latest_asserted_at), fc.opened_at) >= $2::timestamptz
"""


async def live_contention_ids(
    conn: Any,
    group_ids: Iterable[Any],
    *,
    now: datetime | None = None,
    liveness_days: float = DEFAULT_CONTENTION_LIVENESS_DAYS,
) -> set[str]:
    """The subset of ``group_ids`` whose dispute is still live.

    ``liveness_days <= 0`` disables the filter (every id is returned without a
    query), and an unreadable substrate returns EVERY id rather than none: a
    filter that cannot read must not silently mute a whole question class.
    That is the same degrade-loud posture the bearing gate takes, for the same
    reason.
    """
    ids = [str(g) for g in group_ids if str(g or "").strip()]
    if not ids or liveness_days <= 0:
        return set(ids)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=liveness_days)
    try:
        rows = await conn.fetch(LIVENESS_SQL, ids, cutoff)
    except Exception:  # noqa: BLE001 — degrade OPEN, never mute the class
        return set(ids)
    return {str(r["id"]) for r in rows}
