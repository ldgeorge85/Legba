# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The FROZEN bake-off design, read and verified — the harness's only source of truth.

The design file (``BAKEOFF_DESIGN.md``) is pre-registered: line 1 carries the
sha256 of every byte after it. The harness never retypes anything the design
fixes. It reads the design file, refuses unless the computed hash equals both
the stamped one and :data:`PINNED_SHA256` (the version this harness was built
to), and then takes from the HASHED bytes:

  * the 36 questions (§3 tables: ID, class, desk, window, gold, text) and the
    LONG-MEMORY anchor signal ids inside their gold cells;
  * the per-question hard stops (§7 ceilings table);
  * the fixed prompts (§2: A-r's instruction, D's prompt, E's instruction).

The census is enforced (§9: HEADLINE 9, LONG-MEMORY 10, SLOW-CHANGE 9,
INSUFFICIENT 8): a design that parses to anything else is refused, never
"mostly" run. The design file is not in the repo, so the path is always an argument.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

__all__ = [
    "ARMS",
    "CENSUS",
    "Design",
    "DesignError",
    "PINNED_SHA256",
    "Question",
    "load_design",
]

#: The design version this harness implements: v1.2, re-stamped 2026-11-11
#: (normaliser revised under §5.4 after run 1's first tell test failed: N3
#: absence phrases, the N3b voice-and-apparatus rule, the N5 section labels;
#: everything else unchanged). v1.1 (2026-11-05) was
#: ``3cd216967cb421b20ee9a354fb70475d091d7383603415244b544f74e0d3b338``; v1.0
#: (frozen 2026-10-09) was ``8268f147c7ef35a837d4eb285c246c0ef54599494a98675ad1dfa470c4fc2955``.
PINNED_SHA256 = "47e364ce845701597b65fa10f648cd2451eb07c7490ea6f45431abfc2a3fa6ff"

#: §9's expected census.
CENSUS = {"HEADLINE": 9, "LONG-MEMORY": 10, "SLOW-CHANGE": 9, "INSUFFICIENT": 8}

#: The arms, in the design's order. A-r is a pre-registered secondary sub-arm.
ARMS = ("A", "A-r", "B", "C", "D", "E")

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ROW = re.compile(
    r"^\|\s*([A-Z]{2}\d{2})\s*\|\s*([A-Z-]+)\s*\|\s*([a-z0-9_]+)\s*\|"
    r"\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|\s*$"
)


class DesignError(RuntimeError):
    """The design file is not the pinned version the harness was built to."""


@dataclass(frozen=True)
class Question:
    qid: str
    klass: str
    desk: str
    window: str
    gold: str
    text: str
    anchors: tuple[str, ...] = field(default_factory=tuple)

    def window_bounds(self, as_of: datetime) -> tuple[datetime, datetime]:
        """``[start, end]`` of the question's window for run as-of ``as_of``.

        HEADLINE / INSUFFICIENT ``T-7d..T``; SLOW-CHANGE ``T-42d..T`` (the
        comparison half ``T-84d..T-42d`` is :meth:`prior_window`); LONG-MEMORY
        an absolute ``YYYY-MM-DD..T`` start; ``as of T`` is the instant.
        """
        w = self.window.strip()
        m = re.match(r"T-(\d+)d\.\.T", w)
        if m:
            return as_of - timedelta(days=int(m.group(1))), as_of
        m = re.match(r"(\d{4}-\d{2}-\d{2})\.\.T", w)
        if m:
            start = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
            return start, as_of
        if w == "as of T":
            return as_of, as_of
        raise DesignError(f"{self.qid}: unparseable window {self.window!r}")

    def prior_window(self, as_of: datetime) -> tuple[datetime, datetime] | None:
        """The SLOW-CHANGE comparison window (``vs prior 42d``), else None."""
        if "vs prior" not in self.window:
            return None
        start, _ = self.window_bounds(as_of)
        span = as_of - start
        return start - span, start


@dataclass(frozen=True)
class Design:
    path: Path
    sha256: str
    version: str
    questions: tuple[Question, ...]
    hard_stops: dict[str, float]
    prompt_ar: str
    prompt_d: str
    prompt_e: str
    arm_titles: dict[str, str] = field(default_factory=dict)

    def question(self, qid: str) -> Question:
        for q in self.questions:
            if q.qid == qid:
                return q
        raise KeyError(f"{qid} is not one of the design's 36 questions")


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start) + len(start)
    return text[i:text.index(end, i)]


def _unwrap(s: str) -> str:
    return " ".join(s.split())


def load_design(path: str | Path, *, pinned: str = PINNED_SHA256) -> Design:
    """Read + verify the frozen design. Raises :class:`DesignError` on any drift."""
    p = Path(path)
    raw = p.read_bytes()
    first, _, body = raw.partition(b"\n")
    m = re.match(rb"PREREG-SHA256:\s*([0-9a-f]{64})\s*$", first)
    if not m:
        raise DesignError(f"{p}: line 1 is not a PREREG-SHA256 stamp")
    stamped = m.group(1).decode()
    computed = _sha(body)
    if computed != stamped:
        raise DesignError(f"{p}: body hash {computed} != stamped {stamped} (edited after freeze)")
    if computed != pinned:
        raise DesignError(
            f"{p}: design {computed[:12]} is not the version this harness was "
            f"built to ({pinned[:12]}); a new design version needs a harness review"
        )
    text = body.decode("utf-8")
    vm = re.search(r"\*\*Version ([0-9.]+)", text)
    # The version is the LAST row of the §10 version log when there is one:
    # v1.1 re-stamped the file through §10 and left the header line reading
    # "Version 1.0" (the header is inside the hashed bytes, so the harness
    # cannot correct it — it reads the log instead).
    log_versions = re.findall(r"^\|\s*(\d+\.\d+)\s*\|\s*\d{4}-\d{2}-\d{2}\s*\|", text, re.M)
    version = log_versions[-1] if log_versions else (vm.group(1) if vm else "?")

    questions: list[Question] = []
    for line in text.splitlines():
        rm = _ROW.match(line)
        if not rm or rm.group(1) == "ID":
            continue
        qid, klass, desk, window, gold, qtext = rm.groups()
        anchors = tuple(_UUID.findall(gold)) if klass == "LONG-MEMORY" else ()
        questions.append(Question(qid, klass, desk, window, gold, qtext, anchors))
    census: dict[str, int] = {}
    for q in questions:
        census[q.klass] = census.get(q.klass, 0) + 1
    if census != CENSUS or len({q.qid for q in questions}) != sum(CENSUS.values()):
        raise DesignError(f"question census {census} != the pre-registered {CENSUS}")
    for q in questions:
        if q.klass == "LONG-MEMORY" and not q.anchors:
            raise DesignError(f"{q.qid}: LONG-MEMORY row carries no anchor signal id")

    stops: dict[str, float] = {}
    ceil = _between(text, "| Arm | Hard stop per question (Opus) |", "Each stop delivers")
    for arm, usd in re.findall(r"\|\s*([A-E])\s*\|\s*\$(\d+(?:\.\d+)?)\s*\|", ceil):
        stops[arm] = float(usd)
    if set(stops) != {"B", "C", "D", "E"}:
        raise DesignError(f"hard-stop table parsed to {stops}")

    flat = _unwrap(text)  # the prompts wrap across lines in the markdown
    prompt_ar = _between(
        flat, 'with one fixed instruction: "', '" This is the tower plus a free reader.',
    )
    prompt_d = _between(flat, '- **D:** "', '" - **E:**')
    prompt_e = _between(flat, 'no tools and one fixed prompt: "', '" - **Generalisation:**')
    for name, val in (("A-r", prompt_ar), ("D", prompt_d), ("E", prompt_e)):
        if len(val) < 60:
            raise DesignError(f"fixed prompt {name} did not parse: {val!r}")
    titles = dict(re.findall(r"^### Arm ([A-E]) — (.+)$", text, re.M))
    if set(titles) != {"A", "B", "C", "D", "E"}:
        raise DesignError(f"arm headings parsed to {sorted(titles)}")
    return Design(
        path=p, sha256=computed, version=version,
        questions=tuple(questions), hard_stops=stops,
        prompt_ar=prompt_ar, prompt_d=prompt_d, prompt_e=prompt_e,
        arm_titles=titles,
    )
