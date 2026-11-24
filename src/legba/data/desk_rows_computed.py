# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What CODE knows about a desk run's handed slice (lane D-1).

Three things, all computed from the slice object the run already holds — no
read of its own:

* :func:`slice_scope` — the SCOPE every desk finding now stores in
  ``data.slice_scope`` (v1 and v2 alike): the window the slice covered (start,
  end and the basis it came from), the sources it drew on, and the ids of the
  rows handed. S-6 found 10 of 75 sampled findings carried no window at all,
  which left every absence and coverage sentence in them unscopable.
* :func:`computed_block` — contract v2's COLLECTION FACTS: rows handed,
  distinct sources and origin groups, per-source counts, the observed time
  span, the coverage gap. The model never writes these (S-6: collection counts
  in prose were the largest single desk-contract failure class); the renderer
  prints them.
* :func:`slice_evidence` / :func:`offered_subjects` — the cited rows as the
  statement gates read them, and the SUBJECTS list code offers the model
  (the desk's subject, related subjects, then the actors of the slice's rows
  — :mod:`statement_actor_subjects`). D-2: an actor is "of" a row when the
  entity layer links it to the row OR one of its names occurs in the row's
  quotable text (:func:`named_in_rows`, an in-memory alias index over the
  entities linked anywhere in the slice — no alias-table read); each offered
  actor says how it was found (``linked`` / ``named_in_text``).
* the computed block's SOURCE ROSTER fields come from the run's one roster
  read (:mod:`desk_rows_roster`) when the caller hands it; else ``None`` with
  the basis named.

THE TEXT A QUOTE IS CHECKED AGAINST. A slice row's PAGE text is the source's
own words as stored on the signal — ``text_en`` (the stored translation of a
non-Latin body), ``text``, ``archived_text``, ``raw_body`` — and its FEED ITEM
is the title and summary the feed published. ``distilled_body`` (this
platform's own English brief) is deliberately NOT a text a quote may come
from: it is a derivative, and a quote of it is a quote of us.

A LEAF: stdlib + the statement leaves.
"""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple

from .provenance.text_fold import normalize_for_match
from .statement_actor_subjects import ACTOR_CLASSES, LinkedEntity, is_publisher
from .statement_independence import count_groups
from .statement_shapes import HandedSlice, SupportKind, SupportRole, SupportRow
from .statement_subject_names import name_in_text, related_subjects, resolve_subject_names
from .statement_typing import EvidenceRef, SubjectCandidate, evidence_texts

#: The ``data`` key the scope rides on.
SLICE_SCOPE_KEY = "slice_scope"
#: Page-text fields of a signal payload, in order (see the module doc).
PAGE_FIELDS: tuple[str, ...] = ("text_en", "text", "archived_text", "raw_body")
FEED_FIELDS: tuple[str, ...] = ("title", "summary", "description")
#: Actors offered for a whole run (ranked by how many handed rows link them).
MAX_RUN_ACTORS = 60


def _utc(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str) and v.strip():
        try:
            d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def _payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = row.get("data") if row.get("data") is not None else row.get("payload")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    return raw if isinstance(raw, Mapping) else {}


def row_published_at(row: Mapping[str, Any]) -> datetime | None:
    return _utc(_payload(row).get("published_at")) or _utc(row.get("fetched_at")) or _utc(row.get("produced_at"))


# ---------------------------------------------------------------------------
# The scope every finding stores
# ---------------------------------------------------------------------------


def slice_scope(
    sliced: Sequence[Mapping[str, Any]],
    derived_from: Iterable[Any],
    *,
    collection: str | None,
    coverage: Mapping[str, Any] | None,
    window_hours: int | None,
    run_at: datetime | None = None,
) -> dict[str, Any]:
    """The finding's slice scope (``data.slice_scope``).

    The window comes from, in order: the watermark coverage stamp the reader
    cut the slice with (``basis: watermark``); the descriptor window the actor
    cut it with, back from the run (``descriptor_window``); the span of the
    rows themselves (``observed_span`` — a lower bound, and it says so)."""
    start = end = None
    basis = "unknown"
    if isinstance(coverage, Mapping) and coverage.get("window_start") and coverage.get("window_end"):
        start, end, basis = _utc(coverage["window_start"]), _utc(coverage["window_end"]), "watermark"
    elif window_hours:
        end = run_at or datetime.now(timezone.utc)
        start, basis = end - timedelta(hours=int(window_hours)), "descriptor_window"
    else:
        times = [t for t in (row_published_at(r) for r in sliced) if t is not None]
        if times:
            start, end, basis = min(times), max(times), "observed_span"
    return {
        "collection": collection,
        "window_start": start.isoformat() if start else None,
        "window_end": end.isoformat() if end else None,
        "window_basis": basis,
        "source_ids": sorted({str(r.get("source_id")) for r in sliced if r.get("source_id")}),
        "row_ids": [str(x) for x in derived_from],
        "rows_handed": len(sliced),
    }


def stamp_slice_scope(finding: Any, scope: Mapping[str, Any], coverage: Mapping[str, Any] | None) -> Any:
    """``finding`` with ``data.slice_scope`` (and the watermark coverage stamp
    when the run carried one) — additive; no other key moves."""
    data = dict(finding.data) if isinstance(finding.data, dict) else {}
    data[SLICE_SCOPE_KEY] = dict(scope)
    if coverage is not None:
        data["slice_coverage"] = coverage
    return finding.model_copy(update={"data": data})


def handed_slice(scope: Mapping[str, Any], run_id: str | None) -> HandedSlice:
    """The statement gates' :class:`HandedSlice` for a run, from its scope."""
    return HandedSlice.of(scope.get("row_ids") or (), run_id, collection=scope.get("collection"),
                          source_ids=scope.get("source_ids") or (),
                          window_start=_utc(scope.get("window_start")),
                          window_end=_utc(scope.get("window_end")))


# ---------------------------------------------------------------------------
# Cited rows as the gates read them
# ---------------------------------------------------------------------------


def page_text(row: Mapping[str, Any]) -> str:
    p = _payload(row)
    parts: list[str] = []
    for k in PAGE_FIELDS:
        v = p.get(k)
        if isinstance(v, str) and v.strip() and v.strip() not in parts:
            parts.append(v.strip())
    return "\n\n".join(parts)


def feed_text(row: Mapping[str, Any]) -> tuple[str, ...]:
    p = _payload(row)
    out = [row.get("title") if isinstance(row.get("title"), str) else None]
    out += [p.get(k) for k in FEED_FIELDS]
    return tuple(dict.fromkeys(t.strip() for t in out if isinstance(t, str) and t.strip()))


def slice_evidence(sliced: Sequence[Mapping[str, Any]]) -> dict[int, EvidenceRef]:
    """``{ordinal: EvidenceRef}`` for the slice, 1-based exactly as the render
    numbered it (the ORIENTed list). Rows with no id are left out."""
    out: dict[int, EvidenceRef] = {}
    for n, row in enumerate(sliced, start=1):
        rid = row.get("id")
        if not rid:
            continue
        p = _payload(row)
        claim = p.get("claim") if isinstance(p.get("claim"), Mapping) else {}
        feed = feed_text(row)
        out[n] = EvidenceRef(
            n, "signal", str(rid).lower(), page_text(row), published_at=row_published_at(row),
            title=feed[0] if feed else None, source_id=row.get("source_id"),
            access_class=row.get("access_class"), source_class=claim.get("source_class"),
            feed_text=feed, source_name=None,
        )
    return out


# ---------------------------------------------------------------------------
# The computed collection block
# ---------------------------------------------------------------------------


def computed_block(
    sliced: Sequence[Mapping[str, Any]],
    scope: Mapping[str, Any],
    *,
    coverage: Mapping[str, Any] | None = None,
    evidence: Mapping[int, EvidenceRef] | None = None,
    roster: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The collection facts code states about the handed slice. ``roster`` is
    the run's ONE source-roster read (:func:`desk_rows_roster.roster_state`):
    which feeds of the desk's source pool were failing, quiet or delivering
    in the window. Without it the block says so (``not_handed``) and never
    guesses."""
    evidence = slice_evidence(sliced) if evidence is None else evidence
    per_source = Counter(str(r.get("source_id")) for r in sliced if r.get("source_id"))
    rows = [SupportRow(SupportRole.EVIDENCE.value, SupportKind.SIGNAL.value, ev.ref_id,
                       source_id=ev.source_id) for ev in evidence.values()]
    groups = count_groups(rows, evidence_texts(evidence.values())) if rows else None
    times = sorted(t for t in (row_published_at(r) for r in sliced) if t is not None)
    cov = coverage if isinstance(coverage, Mapping) else {}
    return {
        "rows_handed": len(sliced),
        "distinct_sources": len(per_source),
        "per_source": dict(per_source.most_common()),
        "distinct_origin_groups": groups.groups if groups else 0,
        "origin_basis": groups.basis if groups else None,
        "earliest": times[0].isoformat() if times else None,
        "latest": times[-1].isoformat() if times else None,
        "window_start": scope.get("window_start"),
        "window_end": scope.get("window_end"),
        "window_basis": scope.get("window_basis"),
        "in_window": cov.get("in_window"),
        "not_read": cov.get("not_read"),
        **_roster_fields(roster),
    }


def _roster_fields(roster: Mapping[str, Any] | None) -> dict[str, Any]:
    """The roster part of the computed block. With no roster read handed:
    every roster field ``None`` and the basis ``not_handed`` (never guessed)."""
    if not isinstance(roster, Mapping) or roster.get("basis") != "poll_outcomes":
        # not read, read failed, window unknown, empty pool: stated, never [] (= "none")
        return {"sources_silent": None, "sources_failing": None, "sources_polling_clean": None,
                "roster_size": None, "sources_silent_basis": str((roster or {}).get("basis") or "not_handed")}
    return {"sources_silent": list(roster.get("silent") or ()), "sources_failing": list(roster.get("failing") or ()),
            "sources_polling_clean": roster.get("polling_clean"), "roster_size": roster.get("roster_size"),
            "sources_delivered_elsewhere": roster.get("delivered_out_of_scope"),
            "sources_not_polled": list(roster.get("not_polled") or ()),
            "sources_silent_basis": str(roster.get("basis"))}


# ---------------------------------------------------------------------------
# The subjects code offers
# ---------------------------------------------------------------------------


def desk_subjects(target_id: str | None) -> list[SubjectCandidate]:
    if not target_id:
        return []
    out = [SubjectCandidate("target", target_id, resolve_subject_names("target", target_id))]
    for kind, key in related_subjects(target_id):
        out.append(SubjectCandidate(kind, key, resolve_subject_names(kind, key)))
    return out


def quotable_text(ev: EvidenceRef) -> str:
    """Everything a quote of ``ev`` may come from: its page text and its feed
    item (the texts G1 checks a quote against)."""
    return "\n".join(t for t in (ev.text, *ev.feed_text) if t)


def named_in_rows(
    candidates: Iterable[tuple[str, Any]], evidence: Mapping[int, EvidenceRef],
) -> dict[str, frozenset[str]]:
    """``{key: signal ids whose quotable text NAMES it}`` — a name-occurrence
    match of each candidate's usable names (the profile's canonical name and
    aliases, under the G2 names rule, homonym exclusions honoured) against
    every handed row. Bounded and in memory: the candidates are the entities
    linked somewhere in the slice (D-2 §1a); a cheap substring pre-filter on
    the folded text precedes the exact rule."""
    rows = [(ev.ref_id, t, normalize_for_match(t)) for ev in evidence.values() if (t := quotable_text(ev))]
    out: dict[str, frozenset[str]] = {}
    for key, names in candidates:
        usable = tuple(getattr(names, "usable", ()))
        excluded = tuple(getattr(names, "excluded", ()))
        probes = [(n, (normalize_for_match(n.name).split() or [""])[0]) for n in usable]
        hits: set[str] = set()
        for rid, text, folded in rows:
            for n, probe in probes:
                if probe and (probe in folded if not n.case_sensitive else n.name.split()[0] in text) \
                        and name_in_text(n, text, excluded=excluded):
                    hits.add(rid)
                    break
        if hits:
            out[key] = frozenset(hits)
    return out


class OfferedSubjects(NamedTuple):
    """What :func:`offered_subjects` returns. ``links`` is, per offered actor,
    every handed signal id that LINKS it or NAMES it (the gate's rule);
    ``found_by`` says, per actor and row, which (``linked`` /
    ``named_in_text``)."""

    subjects: list[SubjectCandidate]
    links: dict[str, frozenset[str]]
    detail: dict[str, Any]
    found_by: dict[str, dict[str, str]]


def offered_subjects(
    target_id: str | None,
    linked: Iterable[LinkedEntity],
    evidence: Mapping[int, EvidenceRef],
    *,
    limit: int = MAX_RUN_ACTORS,
) -> OfferedSubjects:
    """The desk's own subjects first (S1 is the desk), then the ACTORS of the
    handed rows — ranked by how many rows link or name them, minus non-actors,
    nameless profiles, publishers and the desk itself.

    D-2: an actor is "of" a row when the entity layer LINKS it to the row or
    when one of its names literally OCCURS in the row's quotable text
    (:func:`named_in_rows`, over the entities linked anywhere in the slice).
    D-1 measured 57 of its 60 ``subject_not_linked_to_cited_rows`` failures
    as actors the cited row names but the entity layer did not link to it.
    Each offered actor carries how it was found (``found_by``: ``linked`` when
    some handed row links it, else ``named_in_text``)."""
    from .statement_actor_subjects import _candidate_for  # the S-5 builder, reused as is

    subjects = desk_subjects(target_id)
    desk_forms = [{n.name.lower() for n in getattr(s.names, "usable", ())} for s in subjects]
    keys = {(s.kind, s.key) for s in subjects}
    handed = {ev.ref_id for ev in evidence.values()}
    declined: Counter[str] = Counter()
    evs = list(evidence.values())
    # the bounded alias index: every actor-class entity linked anywhere in the slice
    index: list[tuple[LinkedEntity, SubjectCandidate, frozenset[str]]] = []
    for ent in linked:
        rows = frozenset(s for s in ent.signal_ids if s in handed)
        if not rows:
            continue
        if ent.entity_class not in ACTOR_CLASSES:
            declined["event_not_actor"] += 1
            continue
        cand = _candidate_for(ent, gazetteer=None, metonyms=None, subjects=None)
        if not getattr(cand.names, "usable", ()):
            declined["no_proper_name"] += 1
            continue
        index.append((ent, cand, rows))
    named = named_in_rows(((ent.entity_id, cand.names) for ent, cand, _ in index), evidence)
    ranked: list[tuple[tuple[Any, ...], SubjectCandidate, frozenset[str], frozenset[str]]] = []
    seen: dict[tuple[str, str], int] = {}
    for ent, cand, rows in index:
        forms = {n.name.lower() for n in cand.names.usable}
        if (cand.kind, cand.key) in keys or any(forms & d for d in desk_forms):
            declined["desk_itself"] += 1
            continue
        named_rows = named.get(ent.entity_id, frozenset())
        if is_publisher(cand.names, [e for e in evs if e.ref_id in rows | named_rows]):
            declined["publisher"] += 1
            continue
        k = (cand.kind, cand.key)
        if k in seen:      # two profiles resolving to one place: merge their rows
            i = seen[k]
            key, c, prev, prev_named = ranked[i]
            both, both_named = prev | rows, prev_named | named_rows
            ranked[i] = ((-len(both | both_named),) + key[1:], c, both, both_named)
            continue
        seen[k] = len(ranked)
        ranked.append(((-len(rows | named_rows), ent.canonical_name.lower()), cand, rows, named_rows))
    ranked.sort(key=lambda t: t[0])
    actors = ranked[:limit]
    links = {c.key: rows | nrows for _, c, rows, nrows in actors}
    found_by = {c.key: {**{r: "named_in_text" for r in nrows - rows}, **{r: "linked" for r in rows}}
                for _, c, rows, nrows in actors}
    by = Counter("linked" if rows else "named_in_text" for _, _, rows, _ in actors)
    pairs = Counter(v for m in found_by.values() for v in m.values())
    detail = {"desk_subjects": len(subjects), "actors_eligible": len(ranked), "actors_offered": len(actors),
              "actors_cut_by_limit": max(0, len(ranked) - limit), "declined": dict(declined),
              "alias_index_entities": len(index), "offered_found_by": dict(by),
              "actor_row_pairs": dict(pairs)}
    return OfferedSubjects(subjects + [c for _, c, _, _ in actors], links, detail, found_by)


def subjects_block(subjects: Sequence[SubjectCandidate]) -> str:
    """The SUBJECTS list appended to the desk's user prompt."""
    lines = ["SUBJECTS — every fact row's \"subject\" is ONE id from this list; \"counterpart\", "
             "when the fact names a second party, is another id from it (or null). S1 is this desk's "
             "own subject. An actor may be a fact's subject only when a row the fact cites reports "
             "it, and your quote must name it. An actor your quote names that is NOT on this list: "
             "\"subject_kind\": \"surface\" and its name copied exactly from your quote."]
    for i, s in enumerate(subjects, start=1):
        lines.append(f"  S{i} · {s.label()} · {s.display()}")
    return "\n".join(lines)


__all__ = [
    "FEED_FIELDS",
    "MAX_RUN_ACTORS",
    "PAGE_FIELDS",
    "SLICE_SCOPE_KEY",
    "OfferedSubjects",
    "computed_block",
    "desk_subjects",
    "feed_text",
    "handed_slice",
    "named_in_rows",
    "offered_subjects",
    "page_text",
    "quotable_text",
    "row_published_at",
    "slice_evidence",
    "slice_scope",
    "stamp_slice_scope",
    "subjects_block",
]
