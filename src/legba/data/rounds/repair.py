# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The bounded REPAIR step (lane G-6): what a ``repair: reasons`` seat is
handed on a retry.

A seat whose attempt was refused — its gates failed, or its reply did not
parse — gets ONE kind of help on its retry: the refusal itself, as typed
data, and its own refused output. Never another seat's words, never a free
rewrite by a later seat: the same seat, under the same card and the same
inputs, fixes the sentences the gates named or drops them. How many times is
the seat's ``retries`` (counted in the worst case like any retry).

:func:`repair_note` builds the note from the refused attempt's RECEIPT, so a
replay hands the retry exactly what the first run handed it. Pure.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .rows import sentences_of

#: Failed checks listed per note (a reply with forty failing sentences is
#: told about the first ones; the rule is the same for the rest).
MAX_FAILED_LISTED = 24
#: Characters of a parse error / raw reply kept in a note.
MAX_ERROR_CHARS = 600
MAX_RAW_CHARS = 1500

#: What each gate reason asks the seat to do (plain words; the names stay).
FIXES: dict[str, str] = {
    "number_not_in_evidence": "every figure in the sentence must appear in the quote it cites — "
                              "quote the figure from the row, or drop the figure; never put a row "
                              "id or any other digits in the sentence text",
    "date_not_in_evidence": "a date in the sentence must be in the quote or be the cited row's "
                            "publication day — quote the date, or drop it",
    "relative_date_unresolved": "a relative day word (yesterday, Tuesday) needs a cited row whose "
                                "publication day resolves it — or drop it",
    "quote_absent": "the quote must be copied exactly, character for character, from the cited "
                    "row — no added or dropped words, no backslashes — or the sentence dropped",
    "quote_missing": "an evidence row needs a quote copied exactly from it",
    "text_unavailable": "cite a row whose text you were handed",
    "quote_too_short": "quote at least three consecutive words of the row",
    "not_in_run_slice": "cite only rows you were handed, by their exact id",
    "sentence_without_rows": "every sentence cites at least one handed row",
    "row_not_handed": "cite only rows you were handed, by their exact id",
    "control_text": "no instructions, tags or tool syntax in a sentence",
    "required_row_dropped": "keep every row the scope marks required",
    "probability_not_copied": "copy probabilities from the positions; never compute one",
}


def _sentence_texts(payload: Any) -> dict[str, str]:
    return {str(s.get("key", "")): str(s.get("text", "")) for s in sentences_of(payload)
            if s.get("key")}


def _failed(gates: list[Mapping[str, Any]], texts: Mapping[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for g in gates:
        if g.get("passed"):
            continue
        checks = list(g.get("failed_checks") or ()) or [{"reason": g.get("reason")}]
        for c in checks:
            reason = str(c.get("reason") or g.get("reason") or "failed")
            entry: dict[str, Any] = {"gate": g.get("gate"), "reason": reason}
            key = c.get("sentence")
            if key:
                entry["sentence"] = key
                if key in texts:
                    entry["text"] = texts[key]
            detail = c.get("detail") or {}
            for k in ("missing", "not_handed", "unresolved_relative"):
                if detail.get(k):
                    entry[k] = list(detail[k])[:8]
            if detail.get("closest_passage"):
                entry["closest_passage"] = str(detail["closest_passage"])
            if reason in FIXES:
                entry["fix"] = FIXES[reason]
            out.append(entry)
    return out[:MAX_FAILED_LISTED]


def repair_note(receipt: Any) -> dict[str, Any] | None:
    """The typed failure of a refused attempt, or ``None`` when there is
    nothing a reply could repair (a deadline, a failed call, a refused read:
    the retry is then a clean one)."""
    status = getattr(receipt, "status", None)
    failure = str(getattr(receipt, "failure", None) or "")
    attempt = int(getattr(receipt, "attempt", 0))
    if status == "sealed" and not getattr(receipt, "released", False):
        payload = getattr(receipt, "payload", None)
        failed = _failed(list(getattr(receipt, "gates", None) or ()), _sentence_texts(payload))
        if not failed:
            return None
        return {"attempt": attempt, "kind": "gates", "failed": failed,
                "previous": payload if isinstance(payload, Mapping) else None}
    if status == "failed" and ("unparseable" in failure):
        raw = getattr(receipt, "raw", None)
        note: dict[str, Any] = {"attempt": attempt, "kind": "parse",
                                "error": failure[:MAX_ERROR_CHARS]}
        if isinstance(raw, str) and raw.strip():
            note["raw_head"] = raw[:MAX_RAW_CHARS]
        return note
    return None


__all__ = ["FIXES", "repair_note"]
