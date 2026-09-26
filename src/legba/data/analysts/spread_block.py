# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lane narrative (Program 7 piece 7e) — the SPREAD BLOCK for
``narrative_coordination``.

THE GAP THIS CLOSES. The unit's descriptor already treats organic coverage as
the null hypothesis and already tells the model it has NO source-class field
to reason from (S1-T8 / Phase-V D8a, held by
``tests/data_pkg/test_source_class_taxonomy.py``). What it lacked was a
DEFINITION of inauthentic spread the faithfulness judge could hold it to — a
model asked to eyeball "coordination" from a numbered signal list will keep
finding it in ordinary syndication and ordinary newsworthiness, because
"near-simultaneous" and "the same framing" are judgment calls with no
anchor. Accounts are never stored (house rule: content, not accounts), so the
definition has to be SPREAD-shaped, never actor-shaped: this module measures
three things ACROSS SOURCES, in code, before the model ever sees the slice:

  * **synchrony** — how many hours separate the earliest and latest signal
    that carries a given reused framing;
  * **verbatim reuse** — near-identical sentences appearing in DISTINCT
    sources' rendered text (title + body);
  * **source-class concentration** — whether the sources carrying a framing
    all sit in one registered class, read from
    ``source_descriptors.body->'scope'->>'source_class'`` (the same join
    ``signal_salience.py``'s ``_SELECT_BATCH_SQL`` uses), never inferred from
    a URL.

Every number here is CITED BY ORDINAL, never asserted by the model — the
descriptor's ``system_prompt`` requires a coordination claim to name the
block's own ``[N]`` markers, so a finding that calls something coordinated
without matching the block's own arithmetic fails the faithfulness floor on
its face.

WHY THIS IS NOT THE SAME HOLE S1-T8 CLOSED. That fix removed a field the
model was GUESSING at from outlet URLs and presenting as observed metadata —
the RU finding that claimed a frame crossed "distinct source classes
(independent reporting, analysis, official statements, and state-media)" with
nothing in its prompt to back that up. This module does not hand the model a
new guess to make; it hands the model a NUMBER IT DID NOT COMPUTE, the same
shape as :mod:`wire_pair_collapse`'s ``carried_by=`` line or the rendered
``Slice window:`` line — code-derived, cited, never model-asserted. The
descriptor's system_prompt is explicit that the class mix is FROM THE BLOCK,
never the model's own read of an outlet name.

VERBATIM REUSE, GENERALIZED FROM THE WIRE-PAIR FIX. :func:`wire_pair_collapse
.wire_story_key` keys on ONE thing (the HEADLINE) and folds a same-story
duplicate away entirely so it never reads as two signals. This module runs
the identical normalize-and-key-equality idea (NFKC, casefold,
punctuation-to-space, a minimum token floor so short generic clauses can't
key) over EVERY SENTENCE of the rendered title+body, and — unlike the
wire-pair collapse — does not remove anything: a sentence repeated across
DISTINCT sources becomes a FRAMING entry, complete with the sources, the
class mix and the time spread that produced it. Because :func:`_orient`
already ran the wire-pair collapse before this module ever sees the slice,
genuine one-story-two-mastheads duplication has ALREADY been folded to one
row by the time a framing group is built here — a framing therefore reports
genuinely distinct rows repeating the same wording, which is exactly the
spread signal the unit needs and the false-positive class the wire-pair fix
already removed.

PRECISION GUARDS (same posture as the wire-pair collapse: a miss costs a
little missed spread, a false framing manufactures a coordination tell out of
nothing):

  * A candidate sentence must carry >= :data:`_MIN_SENTENCE_TOKENS` tokens
    after normalization — short/generic clauses ("officials said", "the
    ministry added") repeat constantly across unrelated stories and carry no
    identity worth keying on.
  * A framing requires >= 2 DISTINCT mastheads (the same hostname identity
    :func:`wire_pair_collapse._masthead` uses) — a source restating its own
    sentence twice inside one window is not spread.
  * Class mix and time spread are BEST-EFFORT: an unresolved ``source_id`` (no
    ``source_descriptors`` row, or the DB lookup was never wired for this
    run) reports ``"unknown"`` rather than a fabricated class, and an
    unresolvable timestamp is simply excluded from the spread arithmetic
    rather than defaulting to zero.

Two-function split, deliberately: :func:`build_spread_block` is a PURE,
synchronous function over the already-ORIENTed slice rows (so a unit test can
hand it a synthetic list and get a deterministic answer with no event loop and
no database); :func:`resolve_source_classes` is the ONLY piece that touches
the database, and it is optional — a caller with no pool, or a caller that
never awaits it, gets the honest ``"unknown"`` class-mix path, never an
exception.
"""

from __future__ import annotations

import datetime
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .wire_pair_collapse import _masthead

logger = logging.getLogger(__name__)


def _it():
    """Late import of ``inline_target`` for the two rendered-text helpers —
    call-time only, mirroring :mod:`slice_render`'s own ``_it()`` so module
    load stays acyclic (``inline_target`` imports this module, not the
    reverse, at call time inside ``run_method``)."""
    from . import inline_target
    return inline_target


#: A candidate sentence must carry at least this many normalized tokens to be
#: eligible as a framing key. Mirrors ``wire_pair_collapse._MIN_KEY_TOKENS``
#: (5, for headlines) but a touch higher: full sentences legitimately run
#: shorter generic clauses ("Reuters could not immediately confirm.") that
#: repeat across unrelated stories for reasons that have nothing to do with
#: one framing spreading.
_MIN_SENTENCE_TOKENS = 6

#: Sentences considered per row, in rendered order. The rendered text is
#: already bounded by the per-row snippet cap (``_MAX_SNIPPET_CHARS``), so
#: this is a defensive ceiling, not the normal case.
_MAX_SENTENCES_PER_ROW = 40

#: Top framings rendered into the header (the "TOP framings" the lane brief
#: asks for) — bounded so a chatty window can't crowd the header out of the
#: input-token budget the way an unbounded structure list once did.
_DEFAULT_MAX_FRAMINGS = 5

#: A short, human-checkable excerpt of the reused wording, capped so one long
#: sentence cannot dominate the header the way an unbounded GDELT dump once
#: dominated a slice (see ``inline_target._MAX_TITLE_CHARS`` for the sibling
#: precedent).
_MAX_EXCERPT_CHARS = 140

#: Sentence boundary: a period/question mark/exclamation point followed by
#: whitespace, or a bare newline (rows sometimes carry title+body joined by
#: ``\n``, which is itself a boundary worth splitting on).
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")

#: Everything that is not a word character or whitespace becomes a space
#: before the key is built — the identical treatment
#: ``wire_pair_collapse._normalize_headline`` applies to a title, generalized
#: to a sentence.
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _normalize_sentence(text: str) -> str:
    """Case/punctuation-insensitive identity for a sentence.

    Same recipe as ``wire_pair_collapse._normalize_headline``: NFKC, casefold,
    punctuation-to-space, whitespace-collapse. Returns ``""`` for anything
    that normalizes away — the caller treats an empty key as unkeyable.
    """
    text = str(text or "").strip()
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = _PUNCT_RE.sub(" ", text)
    return " ".join(text.split())


def _split_sentences(text: str) -> list[str]:
    """Split rendered title+body text into candidate sentences, in order."""
    if not text:
        return []
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


def _row_timestamp(row: Mapping[str, Any]) -> datetime.datetime | None:
    """The row's best-effort timestamp, hour-resolution, for spread arithmetic.

    Same precedence as ``wire_pair_collapse._row_day`` (the article's own
    ``data.published_at`` over our ``produced_at`` ingest time), generalized
    from a bare day to a full timestamp since synchrony is measured in HOURS,
    not days. A naive datetime is treated as UTC (every timestamp this
    substrate stores is UTC); an unresolvable value is ``None`` — excluded
    from the spread, never defaulted to "now".
    """
    data = row.get("data")
    candidates: list[Any] = []
    if isinstance(data, Mapping):
        candidates.append(data.get("published_at"))
    candidates.append(row.get("produced_at"))
    for cand in candidates:
        if cand is None:
            continue
        if isinstance(cand, datetime.datetime):
            return cand if cand.tzinfo else cand.replace(tzinfo=datetime.timezone.utc)
        text = str(cand).strip()
        if not text:
            continue
        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            dt = datetime.datetime.fromisoformat(iso)
        except ValueError:
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
    return None


@dataclass(frozen=True)
class SpreadFraming:
    """One reused-wording group: the code's own coordination-tell candidate.

    ``ordinals`` are the SAME 1-based ``[N]`` indices
    :func:`slice_render._render_signal` stamped on the rows carrying this
    framing — the descriptor's prompt requires every coordination claim to
    cite exactly these.
    """

    ordinals: tuple[int, ...]
    source_count: int
    class_mix: tuple[str, ...]
    hours_spread: float
    excerpt: str


@dataclass(frozen=True)
class SpreadBlockResult:
    """The rendered block plus the structured numbers the run receipt keeps."""

    text: str
    framings: tuple[SpreadFraming, ...]
    max_sources: int
    min_hours: float | None


_SPREAD_BLOCK_HEADER = (
    "SPREAD BLOCK (computed by code from this slice, not by you — cite ONLY "
    "these ordinals when you claim coordination; never infer synchrony, "
    "reuse or a class mix yourself from the numbered signals below):"
)

_SPREAD_BLOCK_EMPTY_LINE = (
    "  (no near-verbatim cross-source reuse detected in this slice)"
)


def _render_spread_block_text(framings: Sequence[SpreadFraming]) -> str:
    lines = [_SPREAD_BLOCK_HEADER]
    if not framings:
        lines.append(_SPREAD_BLOCK_EMPTY_LINE)
        return "\n".join(lines)
    for n, framing in enumerate(framings, start=1):
        ords = "".join(f"[{o}]" for o in framing.ordinals)
        classes = ", ".join(framing.class_mix) if framing.class_mix else "unknown"
        lines.append(
            f"  F{n}: {ords} — {framing.source_count} distinct sources; "
            f"class mix: {classes}; time spread {framing.hours_spread:.1f}h; "
            f"reused wording: \"{framing.excerpt}\""
        )
    return "\n".join(lines)


def build_spread_block(
    sliced: list[Mapping[str, Any]],
    *,
    source_class_by_source_id: Mapping[str, str] | None = None,
    max_framings: int = _DEFAULT_MAX_FRAMINGS,
) -> SpreadBlockResult:
    """The deterministic SPREAD BLOCK for an already-ORIENTed slice.

    ``sliced`` is the SAME list, in the SAME order,
    :func:`slice_render._render_user_prompt` renders — a framing's
    ``ordinals`` are therefore the exact ``[N]`` markers the model reads.

    ``source_class_by_source_id`` is optional: keyed by the row's
    ``source_id`` (the ``source_descriptors.descriptor_id`` join key, see
    :func:`resolve_source_classes`). Absent or missing entries report
    ``"unknown"`` for that source — an honest degrade, never a guess.

    Pure and synchronous — no I/O, no clock reads (every timestamp is read
    off the rows), so a synthetic slice gets the identical answer every call.
    """
    groups: dict[str, list[int]] = {}
    excerpts: dict[str, str] = {}
    for idx, row in enumerate(sliced):
        title = _it()._signal_title(row) or ""
        body = _it()._signal_body(row).text or ""
        text = f"{title}\n{body}" if body else title
        for raw_sentence in _split_sentences(text)[:_MAX_SENTENCES_PER_ROW]:
            key = _normalize_sentence(raw_sentence)
            if not key or len(key.split()) < _MIN_SENTENCE_TOKENS:
                continue
            members = groups.setdefault(key, [])
            if idx not in members:
                members.append(idx)
            excerpts.setdefault(key, raw_sentence[:_MAX_EXCERPT_CHARS])

    framings: list[SpreadFraming] = []
    for key, idxs in groups.items():
        if len(idxs) < 2:
            continue
        mastheads: list[str] = []
        for i in idxs:
            name = _masthead(sliced[i])
            if name not in mastheads:
                mastheads.append(name)
        if len(mastheads) < 2:
            continue
        classes: set[str] = set()
        timestamps: list[datetime.datetime] = []
        for i in idxs:
            row = sliced[i]
            source_id = row.get("source_id")
            cls = None
            if source_class_by_source_id is not None and source_id is not None:
                cls = source_class_by_source_id.get(str(source_id))
            classes.add(cls or "unknown")
            ts = _row_timestamp(row)
            if ts is not None:
                timestamps.append(ts)
        hours = 0.0
        if len(timestamps) >= 2:
            hours = (max(timestamps) - min(timestamps)).total_seconds() / 3600.0
        ordinals = tuple(sorted(i + 1 for i in idxs))
        framings.append(SpreadFraming(
            ordinals=ordinals,
            source_count=len(mastheads),
            class_mix=tuple(sorted(classes)),
            hours_spread=round(hours, 2),
            excerpt=excerpts[key],
        ))

    # Widest-spread, most-sourced framings first; ties broken by first
    # ordinal for a deterministic, reviewable order.
    framings.sort(key=lambda f: (-f.source_count, f.hours_spread, f.ordinals[0]))
    framings = framings[:max_framings]

    text = _render_spread_block_text(framings)
    max_sources = max((f.source_count for f in framings), default=0)
    min_hours = min((f.hours_spread for f in framings), default=None)
    return SpreadBlockResult(
        text=text,
        framings=tuple(framings),
        max_sources=max_sources,
        min_hours=min_hours,
    )


#: The join this mirrors verbatim: ``signal_salience.py``'s
#: ``_SELECT_BATCH_SQL`` (``source_descriptors`` keyed by ``descriptor_id``,
#: ``is_head = TRUE``, the ``scope.source_class`` JSONB path). ``signals
#: .source_id`` and ``source_descriptors.descriptor_id`` are both ``text``
#: columns (migration 0001) — no cast needed.
_SOURCE_CLASS_SQL = (
    "SELECT descriptor_id, body -> 'scope' ->> 'source_class' AS source_class "
    "FROM source_descriptors "
    "WHERE is_head = TRUE AND descriptor_id = ANY($1::text[])"
)


async def resolve_source_classes(
    pg: Any, source_ids: Iterable[Any],
) -> dict[str, str]:
    """Best-effort ``source_id -> source_class`` map for :func:`build_spread_block`.

    ``pg`` is duck-typed (anything with ``.fetch``, matching
    :func:`legba.data.provenance.event_citations.expand_event_citation`'s own
    convention) — an asyncpg ``Pool`` or ``Connection``. Degrade-not-drop: a
    ``None`` pool, an empty id set, or a query failure all return ``{}``
    (every source then reports ``"unknown"`` in the block) rather than
    raising — a substrate blip must not sink a unit's synthesis over a
    class-mix nicety.

    A source with no head ``source_descriptors`` row (an unregistered
    provider, e.g. a research-tool citation) is simply absent from the
    returned map — the same honest gap :func:`build_spread_block` already
    treats as ``"unknown"``.
    """
    ids = sorted({str(sid) for sid in source_ids if sid})
    if not ids or pg is None:
        return {}
    try:
        rows = await pg.fetch(_SOURCE_CLASS_SQL, ids)
    except Exception as exc:  # noqa: BLE001 — degrade, never sink the run
        logger.warning("spread_block.source_class_lookup_failed err=%s", exc)
        return {}
    out: dict[str, str] = {}
    for r in rows:
        cls = r["source_class"]
        if cls:
            out[str(r["descriptor_id"])] = str(cls)
    return out


__all__ = [
    "SpreadBlockResult",
    "SpreadFraming",
    "build_spread_block",
    "resolve_source_classes",
]
