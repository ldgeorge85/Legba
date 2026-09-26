# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The desk brief's TYPED ABSENCE section — composed server-side (lane k5b).

``POST /api/v1/v3/export`` already carries a desk brief: the composition, each
unit's admitted read, and the caller's own situations/tracked-events appendix.
Every one of those is something the platform HAS. The brief said nothing about
what it does not have, which is the half of a desk a reader most needs told:
a quiet desk and a broken one print identically when neither is printed at all.

This module composes that half. It runs on the SAME reader the
``/v3/absence`` route runs on (:func:`absence_api.read_absences`) — not a
second query over the same tables — so the brief, the route and the Morning
Read's Gaps band cannot tell a reader three different stories about one desk's
silence.

WHY SERVER-SIDE. The caller could have composed this into its markdown
``appendix`` the way it composes situations. It must not: the JSON format of
this route would then carry a *string* where the markdown carries a section,
and the two formats of one export would disagree about the desk's absences. A
client that composes a fact into prose has also deleted it from the structured
document. So the request only ASKS (``appendix.absences: true`` plus the desk's
``scope``); the block is built here, published in the JSON document under
``absences``, and rendered from that same block into the markdown.

BYTE-IDENTITY. A caller that does not ask gets exactly the bytes it got
before: the document key is absent, not null, and the markdown section is not
emitted. ``tests/data_pkg/test_export_absences.py`` pins that against the
unchanged golden.

WHAT IS PRINTED. All seven kinds, always, in the route's own vocabulary order,
each in exactly one of the route's three states — items, read-and-clear, or
not measured — because "we checked and found nothing" and "we did not check"
are different facts and a brief that prints only the first is lying by layout.
Every item carries the stamp rule in words (measured when, by which instant,
current until when, or LAST KNOWN AND NOT RE-CHECKED) and the proof rule
(what was checked, when, and the ref that holds the record of that look).

Registry-slim: stdlib + ``.absence_api``. Nothing from ``legba.data.analysts``
or ``legba.runtime``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator

from .absence_api import ABSENCE_KINDS, AbsenceItem, AbsenceOut, read_absences

#: Cap on ``ExportRequest.appendix`` — a client-composed markdown block (the
#: Desk Brief's situations/tracked-events section, A10/7b-iii), never a row
#: the export route reads itself. Bounded so the honest-cap discipline that
#: already governs ``EXPORT_MAX_ITEMS`` covers this free-text field too.
APPENDIX_MAX_CHARS = 20_000


class ExportAppendixIn(BaseModel):
    """The OBJECT form of ``ExportRequest.appendix`` — the same client
    markdown, plus an explicit ASK for the server-composed absence section.

    ``absences`` is a request, not a payload: the caller cannot supply the
    absences, only ask the route to read them for ``scope``. That asymmetry
    is the point — an absence a client composed is a client's claim about
    what the platform does not have, and the export's whole contract is that
    what it prints, it read.

    ``scope`` is REQUIRED when ``absences`` is true and is never inferred
    from the basket: a basket can hold rows from more than one desk, and
    guessing which one the brief is about would stamp another desk's silence
    onto this document.
    """

    markdown: str | None = Field(default=None, max_length=APPENDIX_MAX_CHARS)
    absences: bool = False
    scope: str | None = None

    @model_validator(mode="after")
    def _scope_required_for_absences(self) -> "ExportAppendixIn":
        if self.absences and not (self.scope or "").strip():
            raise ValueError(
                "appendix.absences requires appendix.scope naming the desk "
                "(target_id) the absences are read for — this route never "
                "infers a desk from the basket"
            )
        return self


#: The markdown heading this section opens with. The print document
#: (``legba-ui-v3/src/lib/printDocument.ts``) splits the composed markdown on
#: it to give the absences their OWN printed page rather than letting them run
#: on from the situations appendix. The two live in two languages and cannot
#: share a literal, so ``tests/data_pkg/test_export_absences.py`` parses the
#: TypeScript and fails on drift in either direction.
ABSENCES_HEADING = "Typed absence for this desk"

#: The one sentence both reader surfaces carry to the glossary entry — the
#: printed brief here, and the Morning Read's Gaps band in
#: ``legba-ui-v3/src/lib/absenceModel.ts`` (``TYPED_ABSENCE_GLOSSARY_NOTE``,
#: pinned identical by the same drift test). One definition, one wording,
#: wherever absence is read.
GLOSSARY_NOTE = (
    "Typed absence (docs/GLOSSARY.md): an absence with a scope, a kind, a "
    "proof and a shelf life, never a blank. Seven kinds, one closed "
    "vocabulary; every item says what was looked at, when, and whether "
    "anyone has looked since."
)

#: Items printed per kind before the held-back line. The route caps its own
#: read at ``limit_per_kind`` (25); a brief is a document a person reads, so
#: it carries fewer — and SAYS how many it is holding back, in the JSON block
#: as well as the markdown, so neither format can imply it printed them all.
ABSENCES_PER_KIND = 10

#: The three answers the route distinguishes, named for a reader. A brief that
#: collapsed ``clear`` into ``not_measured`` (or either into a blank) would
#: report an unread desk as a clean one.
STATE_ABSENT = "absent"
STATE_CLEAR = "clear"
STATE_NOT_MEASURED = "not_measured"

_STATE_LINE = {
    STATE_CLEAR: "read for this desk, nothing absent under this kind",
    STATE_NOT_MEASURED: "not measured for this desk",
}


def _not_measured_reason(out: AbsenceOut, kind: str) -> Optional[str]:
    """The ``not_measured`` sentence covering ``kind``, or ``None``.

    The route's wire format is ``"<kinds…>: <why>"`` and ONE entry can cover
    two kinds, each optionally qualified — ``"not_collected / source_stale
    (units): …"``. So the match is on the leading identifier of each
    ``/``-separated part of the head, never a prefix of the whole entry: a
    prefix match finds the first kind named and silently misses the second,
    which would leave a kind the route said it could not read rendering as a
    kind it read and found nothing under.
    """
    for entry in out.not_measured:
        head, _, why = str(entry).partition(":")
        for part in head.split("/"):
            token = part.strip().split(" ", 1)[0].strip()
            if token == kind:
                return why.strip() or head.strip()
    return None


def _item_block(item: AbsenceItem) -> dict[str, Any]:
    """One item, reduced to what a printed brief states — and nothing it
    cannot show. Every field here is copied off the route's item; none is
    computed, re-stamped or rounded."""
    return {
        "subject": item.subject,
        "reason": item.reason,
        "since": item.since,
        "window": item.window,
        "as_of": item.as_of,
        "as_of_basis": item.as_of_basis,
        "expires_at": item.expires_at,
        "review": item.review,
        "stale": item.stale,
        "proof": {
            "what_was_checked": item.proof.what_was_checked,
            "checked_at": item.proof.checked_at,
            "ref": item.proof.ref,
            "ref_kind": item.proof.ref_kind,
        },
    }


def absences_block(out: AbsenceOut, *, per_kind: int = ABSENCES_PER_KIND) -> dict[str, Any]:
    """The structured ``absences`` block — PURE, DB-free, golden-testable.

    All seven kinds are present whatever the desk looks like: a kind with no
    items is the route's own "read and found nothing", which is an answer, and
    dropping it would leave the brief unable to say the desk was read at all.
    """
    by_kind: list[dict[str, Any]] = []
    total = 0
    for kind, meaning in ABSENCE_KINDS.items():
        items = [a for a in out.absences if a.kind == kind]
        why = _not_measured_reason(out, kind)
        shown = items[:per_kind] if per_kind > 0 else items
        total += len(items)
        by_kind.append(
            {
                "kind": kind,
                "meaning": meaning,
                # A kind that raised is NOT measured even if some earlier
                # partial read left items behind: the honest state is the one
                # that says the read did not complete.
                "state": (
                    STATE_NOT_MEASURED
                    if why is not None
                    else STATE_ABSENT
                    if items
                    else STATE_CLEAR
                ),
                "not_measured": why,
                "count": len(items),
                "held_back": max(0, len(items) - len(shown)),
                "items": [_item_block(a) for a in shown],
            }
        )
    return {
        "scope": out.scope,
        "read_at": out.read_at,
        "item_count": total,
        "note": GLOSSARY_NOTE,
        "by_kind": by_kind,
    }


def _kind_label(kind: str) -> str:
    return kind.replace("_", " ")


def _stamp_line(item: dict[str, Any]) -> str:
    """The stamp rule in one sentence. A stale item says LAST KNOWN, NOT
    RE-CHECKED in words — an old absence must never pass for a current one."""
    measured = (
        f"measured {item['as_of']} ({item['as_of_basis']})"
        if item["as_of"]
        else f"not measured — {item['as_of_basis']}"
    )
    if item["stale"] and item["expires_at"]:
        return (
            f"{measured} · last known absence, NOT re-checked "
            f"(was due {item['expires_at']})"
        )
    if item["expires_at"]:
        return f"{measured} · current until {item['expires_at']}"
    if item["review"]:
        return f"{measured} · no schedule — revised by {item['review']}"
    return f"{measured} · no recheck scheduled"


def _extent_line(item: dict[str, Any]) -> str:
    if item["since"]:
        return f"since {item['since']}"
    if item["window"]:
        return str(item["window"])
    return "extent not recorded"


def _md_item(item: dict[str, Any]) -> list[str]:
    proof = item["proof"]
    lines = [f"- **{item['subject']}** — {item['reason']}"]
    lines.append(f"  - {_extent_line(item)}")
    lines.append(f"  - {_stamp_line(item)}")
    checked_at = proof["checked_at"]
    lines.append(
        f"  - checked: {proof['what_was_checked']}"
        + (f" (at {checked_at})" if checked_at else "")
    )
    if proof["ref"]:
        ref_kind = proof["ref_kind"] or "unnamed record"
        lines.append(f"  - record of that look: `{proof['ref']}` ({ref_kind})")
    return lines


def render_absences_markdown(block: dict[str, Any] | None) -> list[str]:
    """The markdown section, as lines. ``None`` renders NOTHING — a caller
    that did not ask for absences gets the bytes it got before."""
    if not block:
        return []
    lines = [
        f"## {ABSENCES_HEADING}",
        "",
        f"- scope: {block['scope']}",
        f"- read_at: {block['read_at']}",
        f"- typed absences recorded: {block['item_count']}",
        "",
        f"*{block['note']}*",
        "",
    ]
    for group in block["by_kind"]:
        lines.append(f"### {_kind_label(group['kind'])}")
        lines.append("")
        lines.append(f"*{group['meaning']}*")
        lines.append("")
        if group["state"] == STATE_NOT_MEASURED:
            lines.append(
                f"{_STATE_LINE[STATE_NOT_MEASURED]}: {group['not_measured']}"
            )
            lines.append("")
            continue
        if group["state"] == STATE_CLEAR:
            lines.append(_STATE_LINE[STATE_CLEAR])
            lines.append("")
            continue
        for item in group["items"]:
            lines.extend(_md_item(item))
        if group["held_back"]:
            lines.append(
                f"- +{group['held_back']} more of this kind recorded and not "
                f"printed here (the whole set is at "
                f"`GET /api/v1/v3/absence?scope={block['scope']}`)"
            )
        lines.append("")
    return lines


async def load_absences_block(
    conn: Any, *, scope: str, now: datetime, per_kind: int = ABSENCES_PER_KIND
) -> dict[str, Any]:
    """Read the desk and compose the block, on the caller's own connection and
    the caller's own ``now`` — the same instant that stamps the document."""
    out = await read_absences(conn, scope=scope, now=now)
    return absences_block(out, per_kind=per_kind)


__all__ = [
    "ABSENCES_HEADING",
    "APPENDIX_MAX_CHARS",
    "ABSENCES_PER_KIND",
    "GLOSSARY_NOTE",
    "STATE_ABSENT",
    "STATE_CLEAR",
    "STATE_NOT_MEASURED",
    "ExportAppendixIn",
    "absences_block",
    "load_absences_block",
    "render_absences_markdown",
]
