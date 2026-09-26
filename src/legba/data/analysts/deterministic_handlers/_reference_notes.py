# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R2/D3(c) — the NOTES a failed build leaves for the next one.

WHY THIS EXISTS. The 00:43Z Argentina build spent 927.9 s and 4.5 M core-plane
tokens and wrote NOTHING — not because it learned nothing, but because the only
artefact a build produces is the committed reference, and it never committed
one. Everything the model had NOTED (each ``NOTE: | URL: ... | DATE: ... |``
line it wrote as it read pages) went out with the process. The next tick for
that target would have started from an empty conversation and paid the whole
discovery cost again.

So a build that ends in ``no_commit`` leaves its notes behind, and the next
build for the SAME target starts from them. That is the entire contract.

THREE THINGS IT IS DELIBERATELY NOT.

  * **Not a resume.** No conversation, no tool state, no partial reference is
    carried — only the model's own plain-text notes. A resumed conversation
    would have to re-send the evicted page payloads to mean anything, which is
    the token shape that caused the incident.
  * **Not a migration.** Notes are per-target scratch with a 24-hour life;
    putting them in Postgres would give a throwaway the durability guarantees
    of a reference. They live beside the page archive, on the same filesystem,
    and degrade to "no carry" when that filesystem is not writable — exactly
    how ``ReferenceArchive`` degrades.
  * **Not evidence.** Nothing here reaches a fence. A carried note is a HINT
    about where to look; every URL, date and span it mentions is re-fetched and
    re-checked by the same fences as any other. A carried note that names a
    page this run did not fetch is dropped by the manifest fence like any
    other unfetched URL.

BOUNDED THREE WAYS, because an unbounded carry is the same defect wearing a
different hat: by CHARACTERS (the newest whole NOTE lines that fit), by AGE
(older than ``MAX_CARRY_AGE_HOURS`` is not carried — a reference window moves,
and a note about last week's news is worse than no note), and by TARGET (one
file per target, replaced not appended).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ....data.archive import archive_root

logger = logging.getLogger(__name__)

#: Subdirectory of the archive root the carry files live in.
NOTES_SUBDIR = "reference_notes"

#: Characters of carried notes handed to the next build. 20k is ~5k tokens —
#: under 0.5% of the per-build token ceiling, so the carry can never become the
#: cost it exists to avoid.
MAX_CARRY_CHARS = 20_000

#: A carry older than this is dropped unread. The cadence tick is hourly, so a
#: real retry lands within minutes; a day-old note belongs to a window whose
#: edges have moved and would send the next build after developments the date
#: gate is about to reject.
MAX_CARRY_AGE_HOURS = 24.0

#: A target id is a path segment here, so it is validated as one.
_SAFE_TARGET = re.compile(r"^[A-Za-z0-9._-]{1,120}$")

_NOTE_LINE = re.compile(r"^\s*(?:NOTE:)?\s*\|")


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def notes_root() -> Path | None:
    """The carry directory, or ``None`` when the filesystem will not have it.

    Degrades exactly like ``ReferenceArchive.open_default``: a build whose
    archive root is unwritable still runs, still verifies its spans in memory
    and still writes its reference — it simply carries nothing.
    """
    try:
        root = archive_root() / NOTES_SUBDIR
        root.mkdir(parents=True, exist_ok=True)
        return root
    except Exception as exc:  # noqa: BLE001 — degrade, never lose the run
        logger.warning(
            "reference_builder.notes_root_unwritable err=%s — a failed build "
            "will leave nothing for the next one", exc,
        )
        return None


def _path_for(target_id: str) -> Path | None:
    if not _SAFE_TARGET.match(str(target_id or "")):
        logger.warning(
            "reference_builder.notes_bad_target target=%r — not carried",
            target_id,
        )
        return None
    root = notes_root()
    return None if root is None else root / f"{target_id}.json"


def bound(notes: Sequence[str], *, max_chars: int = MAX_CARRY_CHARS) -> str:
    """The newest whole NOTE lines that fit in ``max_chars``.

    Newest-first is the right end to keep: the model works forward through a
    window, so its later notes are the ones it had not yet turned into
    developments. Whole lines only — half a NOTE line is a URL the next build
    would chase to nowhere.
    """
    lines = [
        line.rstrip()
        for note in notes
        for line in str(note).splitlines()
        if _NOTE_LINE.match(line)
    ]
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        cost = len(line) + 1
        if used + cost > max_chars:
            break
        kept.append(line)
        used += cost
    kept.reverse()
    return "\n".join(kept)


def save(
    target_id: str,
    notes: Sequence[str],
    *,
    window_end: str = "",
    stop_reason: str = "",
    commit_trigger: str = "",
    pages: str = "",
) -> dict[str, Any] | None:
    """Persist a failed build's notes for the next attempt on this target.

    Returns the record written (for the receipt), or ``None`` when there was
    nothing worth carrying or nowhere to put it. Never raises: a carry that
    fails to write must not turn a named ``no_commit`` into a crash.

    D6 — ``pages`` IS THE CARRY THAT DOES NOT DEPEND ON THE MODEL. The 06:43Z
    Australia build ended ``no_notes`` and carried nothing, although it had read
    a page and been shown thirty-odd results; every lead it bought went out with
    the process, and the next attempt would have paid for the discovery again.
    The loop's own manifest lines are carried alongside the model's NOTEs and
    are subject to the same three bounds and the same "leads, never evidence"
    rule — a carried URL is re-fetched and re-fenced like any other.
    """
    text = bound(notes)
    pages = str(pages or "").strip()[:MAX_CARRY_CHARS]
    if not text and not pages:
        return None
    path = _path_for(target_id)
    if path is None:
        return None
    record = {
        "target_id": str(target_id),
        "saved_at": _now().isoformat(),
        "window_end": str(window_end or ""),
        "stop_reason": str(stop_reason or ""),
        "commit_trigger": str(commit_trigger or ""),
        "note_lines": (text.count("\n") + 1) if text else 0,
        "chars": len(text),
        "notes": text,
        "pages": pages,
        "page_lines": (pages.count("\n") + 1) if pages else 0,
    }
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:  # noqa: BLE001 — degrade, never lose the run
        logger.warning(
            "reference_builder.notes_write_failed target=%s err=%s",
            target_id, exc,
        )
        return None
    logger.info(
        "reference_builder.notes_carried target=%s lines=%d chars=%d "
        "stop_reason=%s", target_id, record["note_lines"], len(text),
        stop_reason,
    )
    return record


def load(
    target_id: str, *, max_age_hours: float = MAX_CARRY_AGE_HOURS
) -> dict[str, Any] | None:
    """The carry left by the last failed build, or ``None``.

    A carry past ``max_age_hours`` is DELETED rather than returned: leaving a
    stale file to be re-read and re-rejected every tick is how a scratch
    directory becomes a slow leak.
    """
    path = _path_for(target_id)
    if path is None or not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — degrade, never lose the run
        logger.warning(
            "reference_builder.notes_unreadable target=%s err=%s — discarded",
            target_id, exc,
        )
        clear(target_id)
        return None
    if not isinstance(record, Mapping) or not (
        record.get("notes") or record.get("pages")
    ):
        clear(target_id)
        return None
    try:
        saved_at = datetime.fromisoformat(str(record.get("saved_at")))
    except ValueError:
        clear(target_id)
        return None
    if saved_at.tzinfo is None:
        saved_at = saved_at.replace(tzinfo=timezone.utc)
    if _now() - saved_at > timedelta(hours=float(max_age_hours)):
        logger.info(
            "reference_builder.notes_expired target=%s saved_at=%s — a window "
            "has moved under them", target_id, record.get("saved_at"),
        )
        clear(target_id)
        return None
    return dict(record)


def carried_urls(record: Mapping[str, Any]) -> list[str]:
    """The URLs on a carry's PAGE lines — pages the last attempt really read.

    Parsed rather than stored as a list so the carry file stays one text blob
    under one character bound. Only the ``PAGE: |`` lines are read: a URL the
    MODEL wrote in a NOTE is a claim about a page, and admitting it would let a
    hallucinated URL survive a build by being written down once.
    """
    urls: list[str] = []
    for line in str(record.get("pages") or "").splitlines():
        if not line.lstrip().startswith("PAGE: |"):
            continue
        for part in line.split("|"):
            part = part.strip()
            if part.upper().startswith("URL:"):
                url = part[4:].strip()
                if url.startswith(("http://", "https://")):
                    urls.append(url)
    return urls


def clear(target_id: str) -> bool:
    """Drop the carry for ``target_id``. True when a file was removed.

    Called on a build that COMMITTED: the reference supersedes the notes, and a
    carry that outlives its reason would seed the next window from the last one.
    """
    path = _path_for(target_id)
    if path is None:
        return False
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except Exception as exc:  # noqa: BLE001 — degrade, never lose the run
        logger.warning(
            "reference_builder.notes_clear_failed target=%s err=%s",
            target_id, exc,
        )
        return False


def preamble(record: Mapping[str, Any]) -> str:
    """The carried notes as the block appended to the first user turn.

    It says plainly that these are LEADS and not evidence, because the model
    that reads them has no other way to know they came from a run it does not
    remember, and a model that treats a carried URL as already-verified would
    commit a development the manifest fence then drops for no stated reason.
    """
    block = (
        "\n\nNOTES CARRIED FROM YOUR LAST ATTEMPT ON THIS COUNTRY "
        f"(saved {record.get('saved_at')}; that attempt ended "
        f"'{record.get('stop_reason') or 'unknown'}' and committed nothing).\n"
        "THESE ARE LEADS, NOT EVIDENCE. Nothing below has been verified and "
        "none of these pages is fetched for you. Any development you keep from "
        "them you must FETCH AGAIN in this run and quote from the page you "
        "fetch — a source_url this run did not read is dropped, and a page "
        "with no machine-readable publish date cannot carry a development "
        "however good the quote. Use them to skip the searching you have "
        "already done, then go straight to the pages.\n"
    )
    if record.get("notes"):
        block += f"{record.get('notes')}\n"
    if record.get("pages"):
        block += (
            "PAGES THAT ATTEMPT ACTUALLY READ (recorded by the harness, not by "
            "the model — the URL is exact, copy it character for character):\n"
            f"{record.get('pages')}\n"
        )
    return block


__all__ = [
    "MAX_CARRY_AGE_HOURS",
    "MAX_CARRY_CHARS",
    "NOTES_SUBDIR",
    "bound",
    "carried_urls",
    "clear",
    "load",
    "notes_root",
    "preamble",
    "save",
]
