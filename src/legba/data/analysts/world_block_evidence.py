# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""WHAT A CARRIED BLOCK SAYS ABOUT ITS OWN EVIDENCE — three deterministic
additions to the assembly payload (N-C, 2026-11-11). No model, no judge.

From the operator's review of world record fd6584eb (WR4, WR5/S2, WR7):

  1. THE EVIDENCE BALANCE on a declared tension (:func:`evidence_balance`).
     Declaring a tension stays the tier's only act about it — it still may not
     say WHY two reads pull apart — but a declared tension is now WEIGHED: for
     each side, the distinct sources its quoted sentence cites, the newest of
     those signals' dates, and which side the count favours, or "balanced". The
     count is over the signals the side's OWN markers name, minus any signal both
     sides cite (a shared item is evidence for neither side).

  2. THE NEW-MATERIAL BULLET (:func:`window_adds_span`). The world read quoted
     each desk head's BLUF and nothing else, so what a body said the window
     ADDED never reached the page. When the head carries such a bullet ("this
     window adds …", "the new material is …"), it is quoted too, verbatim,
     through the same byte-identity gate as the BLUF, and only when the bytes sit
     inside the origin capture the quote arm grades against.

  3. CONTESTED FACTS (:func:`contested_facts`). On Sudan the desks cited both a
     Reuters-text report that NCP officials confirmed Ahmed Haroun's arrest and a
     Dabanga report that the arrest is "in doubt"; the world page showed neither.
     When signals cited inside ONE unit's head give opposite verdicts on one event
     — judged from their headlines: a doubt/denial marker on one side, none on the
     other, two or more shared event words — the block says "reported by X /
     disputed by Y". The existing contention machinery (``data.contested``) is the
     arbiter's facts table driven by the voice's ``[[contested:id]]`` markers and
     needs a model turn; this is the deterministic, headline-grain sibling.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID

from .assembly_spans import (
    SPAN_ROLE_WINDOW_ADDS,
    SpanConstructionError,
    body_sha256,
    build_span,
    context_spans,
    quoted_spans,
    span_markers,
)
from .world_selection_rules import _terms

logger = logging.getLogger(__name__)

__all__ = [
    "BALANCE_BASIS",
    "BALANCE_KEY",
    "CONTESTED_BASIS",
    "CONTESTED_FACTS_KEY",
    "balance_sentence",
    "contested_facts",
    "contested_sentence",
    "enrich_world_block",
    "evidence_balance",
    "stamp_balance_dates",
    "window_adds_span",
]

# ---------------------------------------------------------------------------
# 1. THE EVIDENCE BALANCE
# ---------------------------------------------------------------------------

#: ``tensions[].evidence_balance``.
BALANCE_KEY: str = "evidence_balance"
BALANCE_BASIS: str = "distinct_cited_sources.v1"


def _cited(text: Any, signals: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """``{signal_id: source_id}`` for the signals ``text``'s own markers name."""
    markers = set(span_markers(text))
    out: dict[str, str] = {}
    for s in signals or ():
        if not isinstance(s, Mapping):
            continue
        m = str(s.get("marker") or "")
        if m in markers and s.get("signal_id"):
            out[str(s["signal_id"])] = str(s.get("source_id") or "")
    return out


def _side(cited: Mapping[str, str], shared: set[str]) -> dict[str, Any]:
    own = {sid: src for sid, src in cited.items() if sid not in shared}
    sources = sorted({src for src in own.values() if src})
    return {
        "signal_ids": sorted(own),
        "n_signals": len(own),
        "sources": sources,
        "n_sources": len(sources),
        # Filled by :func:`stamp_balance_dates` where a connection exists; an
        # unread date is stated as unknown, never as today.
        "newest_at": None,
    }


def evidence_balance(
    a_text: Any,
    a_signals: Iterable[Mapping[str, Any]],
    b_text: Any,
    b_signals: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """The deterministic weighing of one declared tension.

    ``favours`` is the side with MORE distinct cited sources, else
    ``"balanced"``. Sources, not signals: three outlets re-running one wire are
    one voice per outlet, and a side resting on one masthead cited five times
    is one source. The newest date is published beside the count and never
    decides it — recency is not weight (the Sudan desks resolved Haroun toward
    "disputed" by recency; the operator's point was that the page should show
    both sides, not the newer one).
    """
    a = _cited(a_text, a_signals)
    b = _cited(b_text, b_signals)
    shared = set(a) & set(b)
    side_a, side_b = _side(a, shared), _side(b, shared)
    if side_a["n_sources"] > side_b["n_sources"]:
        favours = "a"
    elif side_b["n_sources"] > side_a["n_sources"]:
        favours = "b"
    else:
        favours = "balanced"
    return {
        "basis": BALANCE_BASIS,
        "a": side_a,
        "b": side_b,
        "shared_signals": len(shared),
        "favours": favours,
    }


def _when(side: Mapping[str, Any]) -> str:
    at = str(side.get("newest_at") or "")
    return f", newest {at[:10]}" if at else ""


def _sources_phrase(n: int) -> str:
    return "1 distinct source" if n == 1 else f"{n} distinct sources"


def balance_sentence(
    balance: Mapping[str, Any] | None, *, a_label: str, b_label: str,
) -> str | None:
    """The rendered line, or ``None`` when the tension carries no balance."""
    if not isinstance(balance, Mapping):
        return None
    a = balance.get("a") or {}
    b = balance.get("b") or {}
    fav = str(balance.get("favours") or "balanced")
    verdict = (
        f"the count favours {a_label}" if fav == "a"
        else f"the count favours {b_label}" if fav == "b"
        else "balanced"
    )
    shared = int(balance.get("shared_signals") or 0)
    tail = f"; {shared} signal(s) cited by both sides set aside" if shared else ""
    return (
        f"Evidence balance (cited signals only): {a_label} cites "
        f"{_sources_phrase(int(a.get('n_sources') or 0))}{_when(a)}; {b_label} "
        f"cites {_sources_phrase(int(b.get('n_sources') or 0))}{_when(b)}{tail} "
        f"— {verdict}."
    )


_DATES_SQL = (
    "SELECT id::text AS id, fetched_at, payload->>'published_at' AS published_at "
    "FROM signals WHERE id = ANY($1::uuid[])"
)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


async def stamp_balance_dates(payload: dict[str, Any], conn: Any) -> dict[str, Any]:
    """Fill ``newest_at`` on every balance side from the signals table.

    The date is the publisher's ``published_at`` when it parses, else the fetch
    time. ``None`` conn, no balance, or any raised query returns the payload
    with the dates left ``None`` — the balance's counts stand on their own.
    """
    balances = [
        t.get(BALANCE_KEY) for t in payload.get("tensions") or ()
        if isinstance(t, Mapping) and isinstance(t.get(BALANCE_KEY), Mapping)
    ]
    ids: set[str] = set()
    for bal in balances:
        for side in ("a", "b"):
            ids.update(str(s) for s in (bal.get(side) or {}).get("signal_ids") or ())
    valid = []
    for sid in sorted(ids):
        try:
            valid.append(UUID(sid))
        except ValueError:
            continue
    if conn is None or not valid:
        return payload
    try:
        acquire = getattr(conn, "acquire", None)
        if callable(acquire):
            async with conn.acquire() as held:
                rows = await held.fetch(_DATES_SQL, valid)
        else:
            rows = await conn.fetch(_DATES_SQL, valid)
    except Exception as exc:  # degrade-not-break: counts stand without dates
        logger.info("world_block_evidence.dates_unavailable err=%s", exc)
        return payload
    when: dict[str, datetime] = {}
    for r in rows:
        r = dict(r)
        dt = _parse_dt(r.get("published_at")) or _parse_dt(r.get("fetched_at"))
        if dt is not None:
            when[str(r.get("id"))] = dt
    for bal in balances:
        for side in ("a", "b"):
            s = bal.get(side)
            if not isinstance(s, dict):
                continue
            dts = [when[x] for x in s.get("signal_ids") or () if x in when]
            s["newest_at"] = max(dts).isoformat() if dts else None
    return payload


# ---------------------------------------------------------------------------
# 2. THE NEW-MATERIAL BULLET
# ---------------------------------------------------------------------------

#: The desk prompts' own new-material phrasings, measured over the 171 child
#: blocks of fd6584eb's candidates: "this window adds" (14), "new in this window"
#: (5), "this window's new …" (2), "the new material is" (1).
_NEW_MATERIAL_RE = re.compile(
    r"(?:this window adds|new in this window|new this window|the new material is|"
    r"this window['’]s new (?:element|material|signals?|item)s?)"
    r"(?!\s+(?:no|nothing)\b)",
    re.IGNORECASE,
)
_BULLET_RE = re.compile(r"^(\s*[-*]\s+)(.*?)\s*$")


def _full_body(block: Mapping[str, Any]) -> tuple[str, str] | None:
    """``(body, head_id)`` when the block's context span IS its whole head body
    (offsets ``[0:body_len]`` and the digest matching), else ``None``."""
    for ctx in context_spans(block):
        origin = ctx.get("origin") if isinstance(ctx, Mapping) else None
        if not isinstance(origin, Mapping):
            continue
        text = str(ctx.get("text") or "")
        raw = text.encode("utf-8")
        if (
            int(origin.get("start") or 0) == 0
            and int(origin.get("end") or -1) == len(raw)
            and int(origin.get("body_len") or -1) == len(raw)
            and str(origin.get("body_sha256") or "") == body_sha256(text)
        ):
            return text, str(origin.get("head_id") or "")
    return None


def window_adds_span(
    block: Mapping[str, Any],
    *,
    captured: str | None = None,
) -> dict[str, Any] | None:
    """The head's new-material bullet as a quoted span, or ``None``.

    ``block`` is the CHILD's block (the one that still carries its desk head in
    full as a ``context_body`` span); ``captured`` is the origin evidence text
    the record's quote arm will grade against (``carried_origins``). A bullet
    whose bytes fall outside that capture is NOT quoted: the arm would charge it
    ``quote_origin_truncated`` and the reader would refuse the whole block, so an
    unprovable second quotation would cost the block its first.
    """
    full = _full_body(block)
    if full is None:
        return None
    body, head_id = full
    lead = quoted_spans(block)
    lead_text = str(lead[0].get("text") or "") if lead and isinstance(lead[0], Mapping) else ""
    pos = 0
    for line in body.split("\n"):
        m = _BULLET_RE.match(line)
        if m and _NEW_MATERIAL_RE.search(m.group(2)) and m.group(2) not in lead_text:
            start = pos + len(m.group(1))
            end = start + len(m.group(2))
            try:
                span = build_span(
                    role=SPAN_ROLE_WINDOW_ADDS, body=body,
                    start_char=start, end_char=end, head_id=head_id,
                )
            except SpanConstructionError:
                return None
            if captured is not None:
                cap = str(captured).encode("utf-8")
                o = span["origin"]
                if o["end"] > len(cap) or cap[o["start"]:o["end"]].decode(
                    "utf-8", "replace"
                ) != span["text"]:
                    return None
            return span
        pos += len(line) + 1
    return None


# ---------------------------------------------------------------------------
# 3. CONTESTED FACTS
# ---------------------------------------------------------------------------

#: ``blocks[].contested_facts`` (world tier).
CONTESTED_FACTS_KEY: str = "contested_facts"
CONTESTED_BASIS: str = "cited_signal_titles.v1"
CONTESTED_MAX: int = 3

_DOUBT_RE = re.compile(
    r"\b(?:in doubt|not confirmed|unconfirmed|deni(?:es|ed)|deny|disputed|"
    r"staged|fake|refut\w*|reject\w*|questioned|no evidence|baseless|false claims?|"
    r"pure lie|dismiss(?:es|ed)? reports?)\b",
    re.IGNORECASE,
)
_CONFIRM_RE = re.compile(r"\bconfirm(?:s|ed)?\b", re.IGNORECASE)
#: A headline that negates is not the AFFIRMING side ("Araghchi: No plans for
#: meeting …" is a denial without a doubt word).
_NEGATED_RE = re.compile(r"\b(?:no|not|never|nor)\b", re.IGNORECASE)
_CAP_WORD_RE = re.compile(r"\b[A-Z][\w'’-]{3,}")
#: Words that carry the VERDICT, never the event.
_VERDICT_TERMS = frozenset({
    "doubt", "confirm", "confirmed", "unconfirmed", "denie", "denied", "deny",
    "dispute", "disputed", "staged", "fake", "alleged", "allegedly", "claim",
    "reject", "rejected", "baseless", "questioned", "refute", "false", "evidence",
    "allegation", "report", "region", "ministry", "official", "source",
})
_MIN_SHARED_EVENT_TERMS: int = 2


def _event_terms(title: str, unit_terms: set[str]) -> set[str]:
    return {t for t in _terms(title) if t not in _VERDICT_TERMS and t not in unit_terms}


def _named(title: str) -> set[str]:
    """The capitalised words of a headline, through the same term fold."""
    return _terms(" ".join(_CAP_WORD_RE.findall(str(title or ""))))


def contested_facts(
    child: Mapping[str, Any] | None,
    *,
    unit_name: Any = None,
    limit: int = CONTESTED_MAX,
) -> list[dict[str, Any]]:
    """Opposite verdicts on one event among the signals one unit's head cites.

    ``child`` is the candidate's own assembly (every desk block of the unit).
    A DISPUTING signal is one whose headline carries a doubt/denial marker; a
    REPORTING signal is one from a different source whose headline carries none
    and shares at least two event words with it (the unit's own name never
    counts). Pairs that share their event words are one entry. The affirming
    side is labelled ``confirmed`` only when a headline on it says so; otherwise
    ``reported`` — a headline that reports an arrest has not confirmed one.
    """
    sigs: dict[str, dict[str, Any]] = {}
    for block in (child or {}).get("blocks") or ():
        if not isinstance(block, Mapping):
            continue
        for s in block.get("signals") or ():
            if isinstance(s, Mapping) and s.get("signal_id"):
                sigs.setdefault(str(s["signal_id"]), {
                    "signal_id": str(s["signal_id"]),
                    "source_id": str(s.get("source_id") or ""),
                    "title": str(s.get("title") or ""),
                })
    unit_terms = _terms(unit_name)
    doubt = [s for s in sigs.values() if _DOUBT_RE.search(s["title"])]
    plain = [
        s for s in sigs.values()
        if s["title"] and not _DOUBT_RE.search(s["title"])
        and not _NEGATED_RE.search(s["title"])
    ]
    # One entry per connected set of (disputing, affirming) pairs: a pair joins
    # an entry when it shares a signal with it, so ten outlets re-running one
    # accusation against one denial are one contested fact, not ten.
    entries: list[dict[str, Any]] = []
    for d in sorted(doubt, key=lambda s: s["signal_id"]):
        d_terms = _event_terms(d["title"], unit_terms)
        d_named = _named(d["title"])
        for a in sorted(plain, key=lambda s: s["signal_id"]):
            if not a["source_id"] or a["source_id"] == d["source_id"]:
                continue
            shared = d_terms & _event_terms(a["title"], unit_terms)
            # Two shared event words, one of them a NAME both headlines
            # capitalise (Haroun; Iran) — "tanker", "diesel" alone bind nothing.
            if len(shared) < _MIN_SHARED_EVENT_TERMS or not (shared & d_named & _named(a["title"])):
                continue
            hit = next((e for e in entries if d["signal_id"] in e["against"]
                        or a["signal_id"] in e["for"]), None)
            if hit is None:
                hit = {"terms": set(), "for": {}, "against": {}}
                entries.append(hit)
            hit["terms"] |= shared
            hit["for"][a["signal_id"]] = a
            hit["against"][d["signal_id"]] = d
    out = []
    for g in sorted(entries, key=lambda e: (-len(e["for"]) - len(e["against"]), sorted(e["terms"]))):
        g["event_terms"] = sorted(g["terms"])
        affirm = [g["for"][k] for k in sorted(g["for"])]
        out.append({
            "basis": CONTESTED_BASIS,
            "event_terms": g["event_terms"],
            "affirm_label": (
                "confirmed" if any(_CONFIRM_RE.search(s["title"]) for s in affirm)
                else "reported"
            ),
            "affirmed_by": affirm,
            "disputed_by": [g["against"][k] for k in sorted(g["against"])],
        })
        if len(out) >= int(limit):
            break
    return out


def _src(s: Mapping[str, Any]) -> str:
    return str(s.get("source_id") or "").removeprefix("source.") or "unknown source"


def contested_sentence(entry: Mapping[str, Any]) -> str:
    """``Contested in cited reporting — reported by jpost.frontpage ("…") /
    disputed by dabanga.sudan ("…"), allafrica.headlines ("…").``"""
    def side(items: Sequence[Mapping[str, Any]]) -> str:
        shown = ", ".join(f'{_src(s)} ("{s.get("title")}")' for s in items[:3])
        more = len(items) - 3
        return shown + (f" and {more} more" if more > 0 else "")

    return (
        f"Contested in cited reporting — {entry.get('affirm_label') or 'reported'} "
        f"by {side(entry.get('affirmed_by') or [])} / disputed by "
        f"{side(entry.get('disputed_by') or [])}."
    )


def enrich_world_block(
    block: dict[str, Any],
    source_block: Mapping[str, Any],
    child: Mapping[str, Any] | None,
    *,
    captured: str | None,
    unit_name: Any = None,
    unit_head: bool = True,
) -> dict[str, Any]:
    """Mutate and return a WORLD carried block: the new-material quote after the
    BLUF (``captured`` is the origin evidence text the quote arm grades against,
    ``None`` when the child published none), and, on a unit's own head
    (``unit_head``; a thematic lane is not one unit), the contested facts."""
    span = window_adds_span(source_block, captured=captured)
    if span is not None:
        block["spans"] = list(block.get("spans") or []) + [span]
    if unit_head:
        facts = contested_facts(child, unit_name=unit_name)
        if facts:
            block[CONTESTED_FACTS_KEY] = facts
    return block
