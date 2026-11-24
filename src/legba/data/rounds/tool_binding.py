# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pack tools bound to a round run: the as-of cut, and the run's own rows
excluded (plan P4 §3.3 rule 4, lane G-5).

A seat that lists packs under ``tools`` gets the pack's read tools that have a
classified record-time cut (``raw_reader_known_at``: ``cut`` or ``exempt``),
and nothing else. Every call goes through ONE injected dispatcher — in the
hosts, the consult tool router (``consult_tool_routing._dispatch_tool``) with
``known_at`` set to the run's as-of time, which itself refuses an
unclassified tool — so:

* **the cut cannot be widened or dropped**: ``known_at`` comes from the run,
  never from the model's arguments (a ``known_at`` or ``scope_predicate`` in
  the arguments is discarded);
* **unclassified tools are refused here first** (``no_known_at_cut``), and a
  name outside the seat's packs is refused (``not_in_pack``);
* **the current run's rows are invisible**: every result is filtered against
  the run's exclusions (:func:`exclusions_from_receipts`): any entry carrying
  an id the run wrote, or the text of a sentence a seat of this run wrote, is
  dropped before the model sees it, and the drop is counted. This is what
  keeps a blind seat blind through its tools: a sibling's sealed output — even
  if a later host persists it where a reader can reach it — is cut out of the
  result, by id and by text.

Rows a tool returns come back as evidence rows (id, text, source, published
time) so a seat may cite them and the gates can check the quotes.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .rows import sentences_of
from .tables import readable_text

#: A sentence text shorter than this is not used to exclude by text (a short
#: phrase would knock out unrelated corpus rows).
MIN_EXCLUDED_TEXT = 40
#: Characters of a tool row's text kept as an evidence row's ``text``.
ROW_TEXT_CHARS = 4000
#: Seconds a single tool read may take (each call's duration is on the log).
DEFAULT_TOOL_TIMEOUT_S = 25.0

_ID_KEYS = ("id", "signal_id", "doc_id", "finding_id", "ref_id")
_TEXT_KEYS = ("text", "body", "content", "archived_text", "summary", "snippet", "excerpt",
              "title_en", "title")

Dispatch = Callable[[str, Mapping[str, Any], Any], Awaitable[Any]]


@dataclass(frozen=True)
class Exclusions:
    """What the current run wrote: ids (receipt keys, position ids, sentence
    keys) and sentence texts. Nothing in it is a substrate row's id."""

    ids: frozenset[str] = frozenset()
    texts: tuple[str, ...] = ()

    def hits(self, value: str) -> bool:
        v = value.strip().lower()
        if v in self.ids:
            return True
        return any(t in v for t in self.texts)


def exclusions_from_receipts(receipts: Iterable[Any]) -> Exclusions:
    """The exclusions for a run, from its receipts (any status)."""
    ids: set[str] = set()
    texts: set[str] = set()
    for r in receipts:
        ids.add(str(r.key).lower())
        payload = getattr(r, "payload", None)
        if not isinstance(payload, Mapping):
            continue
        for s in sentences_of(payload):
            if s.get("key"):
                ids.add(str(s["key"]).lower())
            t = " ".join(str(s.get("text") or "").split()).lower()
            if len(t) >= MIN_EXCLUDED_TEXT:
                texts.add(t)
        for key in ("working_reading", "summary", "prose", "note"):
            t = " ".join(str(payload.get(key) or "").split()).lower()
            if len(t) >= MIN_EXCLUDED_TEXT:
                texts.add(t)
        for p in payload.get("positions") or ():
            if isinstance(p, Mapping) and p.get("position_id"):
                ids.add(str(p["position_id"]).lower())
    return Exclusions(frozenset(ids), tuple(sorted(texts)))


def _strings(obj: Any) -> Iterable[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, Mapping):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def _norm(s: str) -> str:
    return " ".join(s.split()).lower()


def filter_output(obj: Any, excl: Exclusions) -> tuple[Any, int]:
    """``obj`` with every list entry that touches ``excl`` removed (an entry
    touches it when any string inside it is an excluded id or contains an
    excluded text); returns the filtered value and how many entries dropped.
    A top-level object that itself touches ``excl`` is replaced by an error."""
    if not excl.ids and not excl.texts:
        return obj, 0
    dropped = 0

    def touches(v: Any) -> bool:
        return any(excl.hits(_norm(s)) for s in _strings(v))

    def walk(v: Any) -> Any:
        nonlocal dropped
        if isinstance(v, Mapping):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            kept = []
            for x in v:
                if touches(x):
                    dropped += 1
                    continue
                kept.append(walk(x))
            return kept
        return v

    if isinstance(obj, Mapping) and not any(isinstance(v, (list, tuple)) for v in obj.values()) \
            and touches(obj):
        return {"error": "excluded: this read returns a row the current round run wrote"}, 1
    out = walk(obj)
    return out, dropped


def rows_from_output(out: Any) -> list[dict[str, Any]]:
    """Evidence rows (id + text) from a tool result, for citation and gates."""
    found: list[dict[str, Any]] = []

    def take(m: Mapping[str, Any]) -> None:
        rid = next((m.get(k) for k in _ID_KEYS if isinstance(m.get(k), str) and m.get(k)), None)
        if rid is None:
            return
        parts = []
        for k in _TEXT_KEYS:
            v = m.get(k)
            if isinstance(v, str) and v.strip() and v.strip() not in parts:
                parts.append(v.strip())
        if not parts:
            return
        row: dict[str, Any] = {"id": rid, "kind": "signal",
                               "text": readable_text(" ".join(parts))[:ROW_TEXT_CHARS]}
        if isinstance(m.get("title"), str):
            row["title"] = m["title"]
        if isinstance(m.get("source_id"), str):
            row["source_id"] = m["source_id"]
        pub = m.get("published_at") or m.get("produced_at") or m.get("fetched_at")
        if isinstance(pub, str):
            row["published_at"] = pub
        found.append(row)

    def walk(v: Any, depth: int = 0) -> None:
        if depth > 4:
            return
        if isinstance(v, Mapping):
            take(v)
            for x in v.values():
                if isinstance(x, (list, tuple, Mapping)):
                    walk(x, depth + 1)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x, depth + 1)

    walk(out)
    seen: set[str] = set()
    uniq = []
    for r in found:
        if r["id"] not in seen:
            seen.add(r["id"])
            uniq.append(r)
    return uniq


@dataclass
class RoundTools:
    """The tool binding of one round run.

    * ``dispatch(name, args, known_at)`` — the host's router (the consult
      tool router over a read-only port, in the hosts);
    * ``known_at`` — the run's as-of time (the record-time cut);
    * ``packs`` — pack id → its tool names (the pack descriptors);
    * ``classified`` — tool name → ``cut`` | ``exempt``
      (``raw_reader_known_at.KNOWN_AT_CUTS``); anything else is refused;
    * ``exclusions`` — returns the run's current :class:`Exclusions` (read
      fresh on every call, so a row sealed a moment ago is already excluded).
    """

    dispatch: Dispatch
    known_at: datetime
    packs: Mapping[str, Sequence[str]]
    classified: Mapping[str, str]
    exclusions: Callable[[], Exclusions]
    log: list[dict[str, Any]] = field(default_factory=list)
    #: Seconds one tool read may take before the seat is told so (lane G-6:
    #: the first runs saw reads under the as-of cut take tens of seconds).
    timeout_s: float = DEFAULT_TOOL_TIMEOUT_S

    def tools_for(self, seat: Any) -> tuple[str, ...]:
        out: list[str] = []
        for pack in seat.tools:
            for name in self.packs.get(pack, ()):
                if name in self.classified and name not in out:
                    out.append(name)
        return tuple(out)

    async def call(self, call: Any, name: str, args: Mapping[str, Any]
                   ) -> tuple[Any, list[dict[str, Any]], dict[str, Any]]:
        """Run one tool call for ``call``'s seat: ``(result for the model,
        evidence rows, log entry)``. Never raises."""
        entry: dict[str, Any] = {"seat": call.seat_id, "tool": name}
        args = {k: v for k, v in args.items() if k not in ("known_at", "scope_predicate")}
        if name not in self.tools_for(call.seat):
            reason = "no_known_at_cut" if name not in self.classified else "not_in_pack"
            entry["refused"] = reason
            self.log.append(entry)
            return {"error": f"tool_refused: {reason}: {name!r}"}, [], entry
        t0 = time.monotonic()
        try:
            out = await asyncio.wait_for(self.dispatch(name, args, self.known_at),
                                         timeout=self.timeout_s)
        except asyncio.TimeoutError:
            entry["failed"] = "tool_timeout"
            entry["duration_s"] = round(time.monotonic() - t0, 3)
            self.log.append(entry)
            return {"error": f"tool_timeout: {name!r} took longer than {self.timeout_s:g} s; "
                             "answer from what you have"}, [], entry
        except Exception as exc:  # noqa: BLE001 - a failed read is a result the model sees
            entry["failed"] = type(exc).__name__
            entry["duration_s"] = round(time.monotonic() - t0, 3)
            self.log.append(entry)
            return {"error": f"tool_failed: {type(exc).__name__}"}, [], entry
        entry["duration_s"] = round(time.monotonic() - t0, 3)
        out, dropped = filter_output(out, self.exclusions())
        if dropped:
            entry["dropped_run_rows"] = dropped
        if isinstance(out, Mapping):
            cut = out.get("known_at_cut")
            if isinstance(cut, Mapping) and cut.get("dropped_after_known_at"):
                entry["dropped_after_known_at"] = cut.get("dropped_after_known_at")
            if set(out) == {"error"}:
                entry["failed"] = str(out["error"])[:120]
        rows = rows_from_output(out)
        entry["rows"] = len(rows)
        self.log.append(entry)
        return out, rows, entry


__all__ = [
    "Exclusions",
    "RoundTools",
    "exclusions_from_receipts",
    "filter_output",
    "rows_from_output",
]
