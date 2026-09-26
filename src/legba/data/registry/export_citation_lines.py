# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The exported document's ``### Citations`` bullets — one per citation kind.

Extracted from :mod:`legba.data.registry.export_api` (7g-2), verbatim, when
the HISTORICAL OBSERVATION bullet pushed that module past the module-size
gate's 1,500-line entry threshold. The gate is honoured by splitting, never
by pinning a new ceiling, and the citation-line renderers are the cohesive
cut: pure functions of one already-classified citation dict, depending on
nothing in ``export_api`` but the ``resolves_against`` default.

Registry-slim by construction: stdlib only. ``export_api`` imports these
names back ONE WAY and re-exports, so every caller and test is unchanged.

WAVE O / LANE o2 — THE CITATION DATE. Until now a signal endnote resolved to a
title and a ``canonical_url`` and nothing else, so a PRINTED document's
endnotes carried no date at all: a reader holding the paper could not tell a
piece filed this morning from one filed last year without opening the link.
:func:`citation_date` resolves it off the signal row the export already reads,
and the rendered bullet says WHICH date it got — ``published`` when the source
declared one, ``fetched`` when it did not and the platform's own read stamp is
all there is. The two are never conflated and neither is ever invented: a row
that resolves to neither prints no date, and the print document states the
absence in words.
"""

from __future__ import annotations

from typing import Any

#: The default ``resolves_against`` — the ordinal indexes an entry in this
#: row's OWN ``data.citations`` list. Kept beside the renderers that print it
#: and re-exported by ``export_api``, which owns the other two targets.
_RESOLVES_IN_ROW = "data.citations"

#: ``citation_date_label`` values. ``published`` is the SOURCE's own date;
#: ``fetched`` is this platform's read stamp, which is an upper bound on the
#: publication and is labelled as such rather than passed off as one.
CITATION_DATE_PUBLISHED = "published"
CITATION_DATE_FETCHED = "fetched"

__all__ = [
    "CITATION_DATE_FETCHED",
    "CITATION_DATE_PUBLISHED",
    "_RESOLVES_IN_ROW",
    "_md_citation_line",
    "_md_observation_line",
    "citation_date",
]


def _iso_day(value: Any) -> str | None:
    """``YYYY-MM-DD`` out of the three shapes the stream actually stores.

    Measured on the live ``signals`` table (2026-09-26): 200,946 rows hold a
    25-character offset-aware ISO stamp, 46,236 hold a bare ``YYYYMMDD`` (the
    GDELT files source writes the SQLDATE through verbatim), 4 hold a
    19-character naive ISO stamp, and 3,739 hold an empty string. Anything
    else — and an empty string — reads as NO date rather than as a guess.
    """
    if value is None:
        return None
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        stamped = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    elif len(text) >= 10:
        stamped = text[:10]
    else:
        return None
    head, sep, rest = stamped.partition("-")
    month, _, day = rest.partition("-")
    if not (sep and head.isdigit() and month.isdigit() and day.isdigit()):
        return None
    return stamped


def citation_date(
    published_at: Any, published_at_dt: Any, fetched_at: Any,
) -> tuple[str | None, str | None]:
    """``(YYYY-MM-DD, label)`` for one cited signal, or ``(None, None)``.

    Order, and why each leg is where it is:

      1. ``payload['published_at']`` — the date the SOURCE declared. The
         source's own claim about its own document always wins.
      2. ``payload['_published_at_dt']`` — the normalised stamp the RSS and
         json_api sources write beside it when they could parse the feed's
         date. Same claim, normalised, so it carries the same ``published``
         label; it is second only because the raw field is the one the
         publisher actually wrote.
      3. ``signals.fetched_at`` — when THIS PLATFORM read the page. Not a
         publication date and never labelled as one: it is an upper bound, and
         a reader is told ``fetched`` so they can treat it as one.

    A row that answers none of the three gets ``(None, None)`` and the
    endnote prints no date, which is the honest rendering of an absence.
    """
    for value in (published_at, published_at_dt):
        stamped = _iso_day(value)
        if stamped:
            return stamped, CITATION_DATE_PUBLISHED
    stamped = _iso_day(fetched_at.isoformat() if hasattr(fetched_at, "isoformat")
                       else fetched_at)
    if stamped:
        return stamped, CITATION_DATE_FETCHED
    return None, None


def _md_citation_line(c: dict[str, Any]) -> str:
    """One ``### Citations`` bullet, per citation kind.

    A SIGNAL ref renders title — url — DATE — archive hash — the pruned-signal
    note. The date is o2 and it is ADDITIVE on every signal endnote that
    resolves to one: there is no byte-identity to preserve here, because an
    endnote with no date was the defect. The kinds that used to be STRIPPED
    render with their kind and the
    set they resolve against stated in the line, because "[6]" with no target
    named is the illegibility this repair exists to close — and the reader has
    no citation record to join against, unlike the UI.

    ``citation_kind`` absent → signal, so a document composed before
    2026-08-30 renders unchanged through this function.
    """
    kind = str(c.get("citation_kind") or "signal")
    marker = c.get("marker") or ""
    if kind == "signal":
        entry = f"- {marker} {c.get('title') or '(untitled signal)'}"
        url = c.get("canonical_url")
        if url:
            entry += f" — {url}"
        # o2 — the date, with the label that says which one it is. Printed
        # immediately after the URL because it is a property of the SOURCE,
        # like the URL is, and before the archive hash, which is a property of
        # OUR copy. Absent when the signal resolves to neither date.
        stamped, label = c.get("citation_date"), c.get("citation_date_label")
        if stamped and label:
            entry += f" — {label} {stamped}"
        if c.get("archive_sha256"):
            # P2-1: our archived copy of the original bytes exists — the
            # receipt chain terminates in a verifiable hash, not the URL.
            entry += f" — evidence preserved, sha256:{c['archive_sha256']}"
        if not c.get("resolved"):
            entry += " *(signal no longer in substrate; stored citation shown)*"
        return entry
    target = c.get("resolves_against") or _RESOLVES_IN_ROW
    if kind == "finding":
        label = c.get("title") or "(untitled sub-claim finding)"
        ref = c.get("ref_id")
        note = "sub-claim finding" + (f" {ref}" if ref else "")
        line = f"- {marker} {label} — *{note}; resolves against {target}*"
        # W-3 — the hop, spelled out. Absent on an ordinary sub-claim ref, so
        # every pre-Assessment document renders through here unchanged.
        sb = c.get("spine_block")
        if isinstance(sb, dict):
            ordinal = c.get("ordinal")
            where = f"block {ordinal}" if ordinal is not None else "a block"
            who = sb.get("desk") or "(unattributed desk)"
            if sb.get("target_id"):
                who += f" / {sb['target_id']}"
            line += f"\n  - via {where} of that record → {who}"
            link = sb.get("receipt_url") or sb.get("receipt_path")
            if link:
                line += f" — {link}"
            if not sb.get("resolved"):
                line += " *(that read no longer resolves; stored hop shown)*"
            hop = sb.get("first_citation")
            if isinstance(hop, dict):
                leaf = hop.get("title") or "(untitled)"
                cites = " ".join(
                    p for p in (str(hop.get("marker") or ""), str(leaf)) if p
                )
                line += (
                    f"\n    - which cites {cites} — "
                    f"*{hop.get('citation_kind')}; resolves against "
                    f"{hop.get('resolves_against')}*"
                )
        return line
    if kind == "observation":
        # 7g-2 — a cited HISTORICAL OBSERVATION. The endnote prints the row,
        # not a label for it: provider · series · subject · value+unit · the
        # VALID period · when the provider RECORDED it · the URL it was
        # fetched from. A reader of the printed document can check the number
        # against the provider without joining to anything, which is the whole
        # point of citing a holding rather than recalling one. The
        # `stale_tense` marker rides the line for the same reason it rides the
        # prompt: a past figure must read as past on every surface.
        return _md_observation_line(c, marker, target)
    # A desk grounding block. `kind` is the registered ref_kind
    # (prior_read / window_ledger / situation_register / desk_baseline /
    # open_questions); the four synthetic ones carry NO id by design, so no
    # drill target is named for them rather than one being minted.
    pretty = kind.replace("_", " ")
    label = c.get("title") or pretty
    note = f"desk grounding · {pretty}"
    ref = c.get("ref_id")
    if ref:
        note += f" {ref}"
    return f"- {marker} {label} — *{note}; resolves against {target}*"


def _md_observation_line(c: dict[str, Any], marker: Any, target: Any) -> str:
    """The ``### Citations`` bullet for one cited observation.

    Every field is printed ONLY when the citation carries it — an absent
    provider prints nothing rather than "(unknown)", and an absent period
    prints nothing rather than a guessed year. The ONE thing always printed is
    that this is a historical observation and which set the ordinal resolves
    against, because a number with no tense on a printed page is exactly the
    failure the `stale_tense` marker exists to prevent.
    """
    obs = c.get("observation")
    obs = obs if isinstance(obs, dict) else {}
    label = (
        obs.get("indicator_name")
        or obs.get("series_id")
        or c.get("title")
        or "(unnamed series)"
    )
    bits: list[str] = []
    for key in ("provider", "series_id", "subject"):
        value = obs.get(key)
        if value:
            bits.append(str(value))
    value = obs.get("value")
    unit = obs.get("unit")
    if value:
        bits.append(f"{value} {unit}" if unit else str(value))
    stale = obs.get("stale_tense")
    if stale:
        bits.append(str(stale))
    line = f"- {marker} {label}"
    if bits:
        line += " — " + " · ".join(bits)
    url = obs.get("source_url") or c.get("canonical_url")
    if url:
        line += f" — {url}"
    sha = obs.get("sha256")
    if sha:
        # The file the number was read out of. This is what makes a cited
        # observation checkable rather than merely attributed.
        line += f" — file sha256:{sha}"
    return line + f" — *historical observation; resolves against {target}*"
