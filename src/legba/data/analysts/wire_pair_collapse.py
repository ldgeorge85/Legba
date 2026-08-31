# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wire-pair collapse — one syndicated story reaches a desk as ONE signal.

THE DEFECT THIS CLOSES (measured, seven replay rounds). ``narrative_coordination``
called "coordination" on the AU organic window in **3 of 40** pooled draws
(7.5%), and every one of the three fired on the same two rows:

  * ``[80]`` ``english.aawsat.com`` — "Social Media Firms Urge Caution on Early
    Australia Under-16 Ban Data"
  * ``[91]`` ``www.channelnewsasia.com`` — "Social media firms urge caution on
    early Australia under-16 ban data"

One Reuters dispatch under two mastheads, headlines identical word-for-word,
same day, reaching the desk as **two separately-numbered signals**. Four
successive prompt fences (a one-origin rule, a handout rule, an
identical-HEADLINE rule, a story-identity sentence) were each paraphrased
around: the fences describe a SURFACE cue, and a model can restate the cue —
*"two distinct publishers independently reported ... using virtually identical
phrasing"* — and cross the line in the same sentence. No prompt text makes a
desk reliably un-see two numbered signals it was handed. So the fix is
structural and lives here.

WHY THE SUBSTRATE DEDUP CANNOT REACH THIS CLASS. It is not an oversight in
:mod:`legba.data.analysts.deterministic_handlers.cross_source_dedup` — the pair
is out of its reach by construction, on both tiers:

  * **Exact tier.** ``signals.content_hash`` is
    ``sha256(canonical_url(url) + "\\x1f" + normalize_wire_title(title))``. The
    two rows carry DIFFERENT URLs (different publishers), so the hashes differ
    however identical the headlines are. And ``normalize_wire_title`` strips
    leading agency revision markers — it does not fold case, while this pair
    differs in exactly that: Title Case vs sentence case.
  * **Semantic tier.** Gated at cosine ``0.97`` and structurally restricted to
    rows carrying a real floor-clearing vector, because a FALSE link there sets
    ``signals.canonical_signal_id`` and every desk slice filters on it — one bad
    link makes a real, distinct signal invisible platform-wide. That asymmetry
    is correct and must not be relaxed to catch a rendering problem.

So the collapse belongs where its blast radius matches its confidence: **one
desk, one run, one prompt**. That is a statement about EACH invocation's
blast radius, not about WHICH desks get one — this module carries no
desk/analyst identity anywhere in it (:func:`collapse_wire_pairs` takes a row
list, nothing else), and its one caller, ``inline_target._orient``, runs for
EVERY analyst of kind ``inline_target`` (all nine tradecraft units and every
composition), unconditionally. It was measured and proven against
``narrative_coordination`` because that is where the false-coordination class
was FOUND, not because the fix is scoped to it.

FLEET-WIDE, CONFIRMED (2026-08-27 DQ sweep). The sweep read this module's
narrative-desk motivating example and the code as narrative-desk-only, and
measured live that 78 findings across 11 non-narrative desks (7.1% of
non-narrative findings in a 48h window) were citing two members of one
wire-pair group as separate evidence — worst examples ``escalation`` +
``economic_coercion`` and ``military_posture`` + ``internal_stability``. Read
against the code as it already stood at that commit, the finding does not
hold: :func:`_orient`'s call is unconditional, so if that leak is real in a
live rig it is a STALE DEPLOYED WORKER IMAGE (the collapse landed 2026-08-25,
two days before the sweep) rather than a code-level scoping gap — see
``feedback-worker-image-from-runtime-stale-code``. ``tests/data_pkg/
test_wire_pair_collapse.py`` now pins this explicitly: the [80]/[91] replay
pair and the GDACS same-masthead guard are both exercised (and proven
byte-identical to the narrative-desk render) under ``analyst_id`` values for
every desk the sweep named, so a future change cannot silently re-scope the
collapse to one desk without a test catching it.

Nothing here writes to the substrate — no ``canonical_signal_id``, no
``signal_aliases`` row, no mutation of any input row. Both signals stay
first-class in the corpus and BOTH ids stay in the run's ``derived_from`` (see
:data:`WIRE_COLLAPSE_ROW_KEY`), because the desk did read both; it simply
reads them as the one story they are.

WHAT THE DESK SEES. The surviving row renders with a ``carried_by=`` line
naming the mastheads that ran it. That is the whole point of collapsing rather
than dropping: the duplicate stops being a false coordination surface and
becomes the corroboration datum it always was — *this story travelled*. A desk
that must not read syndication as coordination still gets to read reach as
reach.

THE KEY, and why it is deliberately narrow:

    (normalized headline, publication day)

``_normalize_headline`` strips agency revision markers (reusing the shared
``normalize_wire_title``), NFKC-folds, casefolds, replaces punctuation with
space and collapses whitespace — so "Under-16" and "under-16" and "Under‑16"
(U+2011) all agree. The day comes from the article's own ``published_at`` when
present, else ingest time.

Three precision guards, all load-bearing:

  * **≥2 DISTINCT MASTHEADS, or no collapse.** Syndication IS one story under
    several mastheads; that is also the only shape that produces the
    "two publishers agree" surface this exists to remove. A repeat from ONE
    masthead is a re-poll — the substrate dedup's job, via
    ``canonical_signal_id``, not the renderer's — and it never presents the
    false-coordination surface anyway, because one outlet echoing itself is not
    two outlets agreeing.

    This guard is not theoretical. Measured over the AU replay window, keying on
    headline+day alone would have collapsed **five** groups, and only ONE of
    them was a wire pair. Nine of the ten absorbed rows were GDACS wildfire
    alerts sharing the auto-generated title "Green forest fire notification in
    Australia" while carrying **distinct ``eventid``s** — nine DIFFERENT fires,
    which a headline-only rule would have hidden from the desk. All nine are
    one masthead (``gdacs.org``), so requiring two publishers drops every one of
    them and keeps the aawsat/channelnewsasia pair. Machine-generated
    boilerplate headlines are common and no token-count floor separates them
    from real ones; a publisher count does.
  * :data:`_MIN_KEY_TOKENS` — a headline must carry at least 5 tokens to key at
    all. Identical 5-token-plus headlines on one day, from two publishers, are
    syndication essentially always; short generic ones ("Business", "Morning
    briefing") carry no identity worth trusting.
  * An unresolvable day, or an empty normalized headline, yields NO key and the
    row is never a collapse candidate. Same lesson ``normalize_wire_title``
    encodes by never returning empty: a degenerate key collapses everything it
    touches, which is far worse than the duplicate it was chasing.

A miss costs a little redundancy in one prompt. A false collapse hides a real,
distinct signal from the desk that was supposed to read it — so every guard here
resolves ties toward NOT collapsing.

ORDER. The collapse is order-preserving and first-wins. The survivor of a group
is the member that already stood first in the ORIENTed order (freshest, since
``_orient`` sorts ``produced_at`` descending), and it stays in that slot; no row
is ever reordered. Rows ahead of the first absorbed duplicate keep their exact
ordinals, and the rows after it close up by exactly the number of copies
absorbed — the same shape, and the same safety argument, as the dead-row drop
this sits beside: ``_orient`` returns the ONE list that
``_render_user_prompt``, ``_build_citation_index`` and ``derived_from`` all key
off, so the ``[N]`` space stays contiguous and gap-free by construction.
"""

from __future__ import annotations

import datetime
import logging
import re
import unicodedata
from typing import Any, Mapping
from urllib.parse import urlsplit

from .._url_canon import normalize_wire_title

logger = logging.getLogger(__name__)


#: Row key stamped onto the SURVIVOR of a collapsed wire group. Private to the
#: render path by convention (leading underscore), and read in exactly two
#: places, which must stay in step:
#:
#:   * ``slice_render._render_signal`` — emits the ``carried_by=`` line.
#:   * ``inline_target._orient`` — folds ``absorbed_ids`` into ``derived_from``
#:     so a collapsed sibling keeps its provenance edge.
#:
#: ``_orient`` additionally stamps the absorbed COUNT onto the ORIENT trace step
#: as ``wire_copies_collapsed``. That counter reads off this function's return
#: value, not off the rows — it counts rows that never reached ``sliced``, so,
#: like ``dropped_dead_rows``, it cannot be recovered from the rows that did.
#:
#: Value shape: ``{"copies": int, "mastheads": [str, ...],
#: "absorbed_ids": [Any, ...]}``. ``copies`` counts the WHOLE group (survivor
#: included), so it is the number the ``carried_by`` line states.
WIRE_COLLAPSE_ROW_KEY = "_wire_pair_collapse"

#: Minimum token count for a headline to be eligible as a collapse key. See the
#: module docstring — this is the precision guard that keeps generic headlines
#: ("Business", "News roundup") from merging distinct stories.
_MIN_KEY_TOKENS = 5

#: How many mastheads the ``carried_by`` line names before it summarises the
#: tail. A widely-syndicated story can run under dozens; the line is inside the
#: per-row INPUT-token budget, so it states the COUNT in full and names a
#: bounded sample.
_MAX_NAMED_MASTHEADS = 6

#: Everything that is not a word character or whitespace becomes a space before
#: the key is built, so hyphens, curly quotes and colons cannot split an
#: otherwise identical headline.
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _normalize_headline(title: Any) -> str:
    """Case/punctuation/marker-insensitive headline identity.

    Reuses the shared :func:`normalize_wire_title` for the leading agency
    revision markers ("(LEAD)", "UPDATE 1-") so this agrees with the ingest and
    ``cross_source_dedup`` content-hash keys on that dimension, then goes
    further where those deliberately do not: NFKC normalisation, casefold, and
    punctuation-to-space. Returns ``""`` for anything that normalises away —
    the caller treats an empty key as "not a collapse candidate".
    """
    text = str(title or "").strip()
    if not text:
        return ""
    text = normalize_wire_title(text)
    text = unicodedata.normalize("NFKC", text).casefold()
    text = _PUNCT_RE.sub(" ", text)
    return " ".join(text.split())


def _row_day(row: Mapping[str, Any]) -> str:
    """The row's publication DAY as ``YYYY-MM-DD``, or ``""`` when unresolvable.

    Prefers the article's own ``data.published_at`` (the story's date) over
    ``produced_at`` (our ingest time) — two mastheads pick a syndicated
    dispatch up minutes to hours apart, so ingest time is the noisier of the
    two, but it is a sound fallback when the publisher gave us nothing.
    """
    data = row.get("data")
    candidates = []
    if isinstance(data, Mapping):
        candidates.append(data.get("published_at"))
    candidates.append(row.get("produced_at"))
    for cand in candidates:
        if cand is None:
            continue
        if isinstance(cand, datetime.datetime):
            return cand.date().isoformat()
        if isinstance(cand, datetime.date):
            return cand.isoformat()
        text = str(cand).strip()
        if not text:
            continue
        # ISO-8601 is what both columns carry; take the date head rather than
        # parsing a full timestamp (a "Z" suffix defeats fromisoformat on 3.10).
        head = text[:10]
        try:
            datetime.date.fromisoformat(head)
        except ValueError:
            continue
        return head
    return ""


def wire_story_key(row: Mapping[str, Any]) -> tuple[str, str] | None:
    """``(normalized_headline, day)`` for a row, or ``None`` if it cannot key.

    ``None`` means "never collapse this row" and is returned whenever the
    identity would be weak: no resolvable day, an empty normalized headline, or
    a headline under :data:`_MIN_KEY_TOKENS` tokens.
    """
    headline = _normalize_headline(row.get("title"))
    if not headline or len(headline.split()) < _MIN_KEY_TOKENS:
        return None
    day = _row_day(row)
    if not day:
        return None
    return (headline, day)


def _masthead(row: Mapping[str, Any]) -> str:
    """Human-readable publisher handle — the ``source_url`` host, else source_id.

    ``www.`` is stripped so a masthead reads the way a person would say it. An
    unparseable/absent URL falls back to the row's ``source_id``, and finally to
    a literal so the ``carried_by`` line never renders a bare empty slot.
    """
    url = str(row.get("source_url") or "").strip()
    if url:
        host = (urlsplit(url).hostname or "").strip().lower()
        if host:
            return host[4:] if host.startswith("www.") else host
    sid = str(row.get("source_id") or "").strip()
    return sid or "unattributed"


def carried_by_line(marker: Mapping[str, Any]) -> str:
    """The rendered ``carried_by=`` line for a collapsed survivor.

    States the COUNT first (that is the corroboration datum), names a bounded
    sample of mastheads, and then says the thing the desk must not get wrong:
    this is ONE origin. The wording is deliberately blunt about both halves —
    a desk told only "duplicate" loses the reach signal, and a desk told only
    "carried widely" is being handed the false-positive surface again.
    """
    copies = int(marker.get("copies") or 0)
    mastheads = [str(m) for m in (marker.get("mastheads") or [])]
    named = mastheads[:_MAX_NAMED_MASTHEADS]
    shown = ", ".join(named)
    extra = len(mastheads) - len(named)
    if extra > 0:
        shown = f"{shown}, +{extra} more"
    return (
        f"    carried_by={len(mastheads)} mastheads ({shown}) — ONE syndicated "
        f"story collapsed from {copies} copies in this slice. This is evidence "
        f"of REACH, not {copies} independent reports; do not read it as "
        f"coordination between these outlets."
    )


def collapse_wire_pairs(
    rows: list[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], int]:
    """Collapse same-wire-story rows to one row each. Returns ``(rows, copies)``.

    ``copies`` is the number of rows ABSORBED (0 when nothing collapsed), i.e.
    ``len(rows_in) - len(rows_out)`` — the ``wire_copies_collapsed`` receipt.

    A group collapses only when it holds ≥2 rows sharing a
    :func:`wire_story_key` AND those rows carry ≥2 DISTINCT mastheads. Both
    conditions are checked before anything is absorbed, so a same-publisher
    repeat and a machine-generated-title collision are left strictly alone.

    Order-preserving and first-wins: each group's survivor keeps the slot its
    first member already held, and a slice with no wire pairs comes back as the
    SAME list object, so the overwhelmingly common case is provably a no-op
    rather than a rebuild that merely looks like one.

    The survivor is returned as a shallow copy carrying
    :data:`WIRE_COLLAPSE_ROW_KEY`; the input rows are never mutated (they are
    typed ``Mapping``, and callers upstream share them with the trace/receipt
    paths).
    """
    candidates: dict[tuple[str, str], list[int]] = {}
    keys: list[tuple[str, str] | None] = []
    for i, row in enumerate(rows):
        key = wire_story_key(row)
        keys.append(key)
        if key is not None:
            candidates.setdefault(key, []).append(i)

    # Mastheads in first-seen order, de-duplicated. A group only collapses when
    # at least TWO distinct publishers ran the story — see the module docstring:
    # a same-masthead repeat is a re-poll (substrate dedup's job) and machine
    # -generated alert titles collide across genuinely different events.
    groups: dict[tuple[str, str], tuple[list[int], list[str]]] = {}
    for key, members in candidates.items():
        if len(members) < 2:
            continue
        mastheads: list[str] = []
        for m in members:
            name = _masthead(rows[m])
            if name not in mastheads:
                mastheads.append(name)
        if len(mastheads) < 2:
            continue
        groups[key] = (members, mastheads)

    absorbed: set[int] = set()
    for members, _mastheads in groups.values():
        absorbed.update(members[1:])
    if not absorbed:
        # No wire pair in this slice — hand back the identical list object.
        return rows, 0

    out: list[Mapping[str, Any]] = []
    for i, row in enumerate(rows):
        if i in absorbed:
            continue
        key = keys[i]
        group = groups.get(key) if key is not None else None
        if group is None:
            out.append(row)
            continue
        members, mastheads = group
        survivor = dict(row)
        survivor[WIRE_COLLAPSE_ROW_KEY] = {
            "copies": len(members),
            "mastheads": mastheads,
            "absorbed_ids": [
                rows[m].get("id") for m in members[1:] if rows[m].get("id") is not None
            ],
        }
        out.append(survivor)

    logger.debug(
        "wire_pair_collapse in=%d out=%d absorbed=%d groups=%d",
        len(rows), len(out), len(absorbed), len(groups),
    )
    return out, len(absorbed)


__all__ = [
    "WIRE_COLLAPSE_ROW_KEY",
    "carried_by_line",
    "collapse_wire_pairs",
    "wire_story_key",
]
