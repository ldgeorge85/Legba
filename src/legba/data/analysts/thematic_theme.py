# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A THEME-FILTERED thematic composition: one theme across many units and desks.

The S2-T4 thematic composition (``escalation_composition``) fuses ONE unit
dimension across every desk: the escalation unit's head for each desk, all of
them. That shape cannot carry a subject which no single unit owns. A
cross-border campaign that one profile calls "hybrid activity" lands in several
units at once (escalation, internal stability, military posture, energy
security) and in several countries' desks, and it is the lead of none of them
on a busy day. The world tier ranks country reads, so such a campaign is
carried only if one country's read happens to rank high on it.

This module lets a thematic descriptor declare a THEME as data:

    subscription:
      other_analysts: [{id: escalation, ...}, {id: internal_stability, ...}]
      substrate:
        thematic_dimension: <theme id>
        thematic_terms: ["bomb hoax", "sabotag", ...]

Every current head of each listed unit, on every desk, is a candidate; a head
is ON THEME when its LEAD SPAN (the sentence the assembly quotes,
``assembly_spans.extract_lead_span``) contains one of the terms. Matching the
lead, and not the title or the whole body, is deliberate: under the assembly
the composition carries each head's lead sentence verbatim, and a head whose
title or fourth bullet mentions the theme would be carried under a lead about
something else, an off-theme quote under an on-theme heading. Measured on the
2026-11-07 heads: the Italy internal-stability title read "bomb hoaxes hit
elites" over a lead about strikes and furnace closures.

PLATFORM, NOT EXEMPLAR. Nothing here names a theme, a term or a desk. The term
list, the unit list and the theme id live in the descriptor YAML, the way the
escalation composition's unit and the dyad's desk allow-list do.

THE TERM GRAMMAR. A term is lowercase letters, digits, apostrophes, spaces and
hyphens, and starts with a letter or digit. It matches at a WORD START and runs
on, so a stem matches its inflections (``sabotag`` matches sabotage and
sabotaged, but not saboteur: choose the stem accordingly). A space or hyphen in a
term matches any run of spaces and hyphens, so ``bomb hoax`` matches
"bomb-hoax". The same term compiles to a Postgres ARE (the SQL pre-filter that
keeps the read's LIMIT from truncating on-theme heads) and to a Python regex
(the authoritative lead check). A term outside the grammar is refused
at read time with a ValueError: a typo in a descriptor fails the run loudly
rather than matching nothing.

COVERAGE. A desk with no on-theme head is not a GAP of collection, it is a
desk whose current reads do not lead on the theme. Those desks are recorded as
mode ``quiet`` in ``data.desk_coverage`` and are not handed to the prose as
unassessed desks.

THE PROSE PATH IS NOT BUILT (SEAMS #75). The thematic prompt is worded for the
escalation dimension. A theme-filtered run composes only under the
deterministic assembly (``LEGBA_COMPOSITION_ASSEMBLY``), which writes no prose;
on the legacy prose path :func:`refuse_theme_prose` raises before any model
call rather than compose a hybrid-threat read with escalation instructions.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .assembly_spans import SpanConstructionError, extract_lead_span
from .composition_slice import BasisReader, THEMATIC_MODE_GAP

__all__ = [
    "THEMATIC_MODE_QUIET",
    "THEMATIC_TERMS_KEY",
    "THEME_ROW_KEY",
    "ThemeProseNotBuilt",
    "on_theme",
    "pg_theme_pattern",
    "py_theme_pattern",
    "quiet_theme_coverage",
    "refuse_theme_prose",
    "theme_basis_reader",
    "theme_filter_rows",
    "thematic_terms",
]

#: The ``subscription.substrate`` key carrying the theme's term list. Lives in
#: the open substrate dict, so no schema change and no registry rebuild.
THEMATIC_TERMS_KEY: str = "thematic_terms"

#: The per-desk coverage mode for a desk whose current reads do not lead on the
#: theme. Not a gap: nothing is missing, the desk is reading something else.
THEMATIC_MODE_QUIET: str = "quiet"

#: The row annotation naming the terms a row was admitted under. Ephemeral like
#: every ``_``-prefixed slice key; :func:`refuse_theme_prose` keys on it.
THEME_ROW_KEY: str = "_thematic_terms"

_TERM_RE = re.compile(r"^[a-z0-9][a-z0-9' \-]*$")
_SPLIT_RE = re.compile(r"[\s\-]+")

#: What a space or hyphen in a term matches in the text: whitespace, the
#: no-break space, the hyphen-minus and the Unicode hyphens and dashes
#: U+2010-U+2015 and U+2212. Desk bodies carry U+2011 in most heads
#: (``text_fold``'s measurement), so "bomb‑hoax" must match ``bomb hoax``.
_DASHES = "\u00a0\u2010-\u2015\u2212"
_PY_SEP = "[\\s" + _DASHES + "\\-]+"
_PG_SEP = "[[:space:]" + _DASHES + "-]+"


class ThemeProseNotBuilt(RuntimeError):
    """A theme-filtered thematic run reached the legacy prose path (SEAMS #75)."""


def thematic_terms(descriptor: Any) -> tuple[str, ...] | None:
    """The theme's terms from ``subscription.substrate.thematic_terms``.

    ``None`` when the key is absent or empty (every existing thematic, which is
    then byte-for-byte unchanged). Raises ``ValueError`` on a term outside the
    grammar in the module banner, or on a value that is not a list.
    """
    sub = getattr(descriptor, "subscription", None)
    substrate = getattr(sub, "substrate", None) if sub is not None else None
    if not isinstance(substrate, Mapping):
        return None
    raw = substrate.get(THEMATIC_TERMS_KEY)
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        raise ValueError(
            f"subscription.substrate.{THEMATIC_TERMS_KEY} must be a list of "
            f"terms, got {type(raw).__name__}"
        )
    terms: list[str] = []
    for item in raw:
        term = " ".join(str(item).strip().lower().split())
        if not _TERM_RE.match(term):
            raise ValueError(
                f"subscription.substrate.{THEMATIC_TERMS_KEY}: term {item!r} is "
                "outside the grammar (lowercase letters, digits, apostrophes, "
                "spaces and hyphens, starting with a letter or digit)"
            )
        if term not in terms:
            terms.append(term)
    return tuple(terms) or None


def _words(term: str) -> list[str]:
    return [w for w in _SPLIT_RE.split(term) if w]


def py_theme_pattern(terms: Sequence[str]) -> re.Pattern[str]:
    """The Python regex: any term, at a word start, case-insensitive."""
    alts = [_PY_SEP.join(re.escape(w) for w in _words(t)) for t in terms]
    return re.compile(r"(?<!\w)(?:" + "|".join(alts) + ")", re.IGNORECASE)


def pg_theme_pattern(terms: Sequence[str]) -> str:
    """The same alternation as a Postgres ARE, for ``~*``.

    The grammar admits only letters, digits, apostrophes, spaces and hyphens,
    none of which is special in an ARE, so the words go in unescaped; ``\\m`` is
    the ARE word-start anchor (``\\b`` is a backspace there). The separator
    class carries the dash characters themselves, not escapes, so the pattern
    means the same thing whatever the server's escape settings.
    """
    alts = [_PG_SEP.join(_words(t)) for t in terms]
    return r"\m(?:" + "|".join(alts) + ")"


def _lead_text(row: Mapping[str, Any]) -> str:
    """The lead span the assembly would quote, or ``""`` when it cannot build one
    (the assembly would then refuse the head too, so it cannot carry it)."""
    try:
        span = extract_lead_span(row, head_id=str(row.get("id") or ""))
    except SpanConstructionError:
        return ""
    return str(span.get("text") or "")


def on_theme(row: Mapping[str, Any], pattern: re.Pattern[str]) -> bool:
    """Is this head on theme? Its LEAD SPAN, the sentence quoted, carries a term."""
    return bool(pattern.search(_lead_text(row)))


def theme_filter_rows(
    rows: Sequence[dict[str, Any]], terms: Sequence[str]
) -> list[dict[str, Any]]:
    """Keep the on-theme rows, each stamped with the terms it was admitted under."""
    pattern = py_theme_pattern(terms)
    kept: list[dict[str, Any]] = []
    for row in rows:
        if on_theme(row, pattern):
            row[THEME_ROW_KEY] = list(terms)
            kept.append(row)
    return kept


def theme_basis_reader(reader: BasisReader, terms: Sequence[str]) -> BasisReader:
    """Wrap a basis (or periphery) reader: pre-filter in SQL, decide in Python.

    The SQL pattern runs over title + body, a superset of the lead, so it only
    narrows what the read's LIMIT has to hold; the Python check over the lead
    is the one that admits a row.
    """
    sql_pattern = pg_theme_pattern(terms)

    async def _read(conn: Any, **kwargs: Any) -> list[dict[str, Any]]:
        rows = await reader(conn, text_pattern=sql_pattern, **kwargs)
        return theme_filter_rows(rows, terms)

    return _read


def quiet_theme_coverage(rows: Sequence[Mapping[str, Any]]) -> None:
    """Re-mode the slice's ``gap`` desks as ``quiet`` (in place).

    The thematic slice diffs the desk roster against the desks with a head and
    calls every other desk a gap. Under a theme the heads were filtered, so a
    desk without one has reads that do not lead on the theme: ``quiet``. The
    coverage list is one object shared by every row, so the first carries it.
    """
    for row in rows:
        coverage = row.get("_thematic_coverage")
        if isinstance(coverage, list):
            for entry in coverage:
                if isinstance(entry, dict) and entry.get("mode") == THEMATIC_MODE_GAP:
                    entry["mode"] = THEMATIC_MODE_QUIET
            return


def refuse_theme_prose(rows: Sequence[Mapping[str, Any]]) -> None:
    """SEAMS #75 — raise before a model call on a theme-filtered slice."""
    if any(row.get(THEME_ROW_KEY) for row in rows):
        raise ThemeProseNotBuilt(
            "a theme-filtered thematic composition reached the prose path; only "
            "the deterministic assembly composes one (LEGBA_COMPOSITION_ASSEMBLY "
            "must include this analyst). The thematic prompt is worded for the "
            "escalation dimension and no theme prompt is built: SEAMS #75."
        )
