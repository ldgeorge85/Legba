# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE HOMONYM CLASS (B6, W11-F13): a desk never reads another subject's code
as its own.

A per-subject desk is scoped by its subject's CODE (``geo && ['KP']``), and a
code is not a name. Pakistani wire copy writes Khyber Pakhtunkhwa as "KP"; the
ingest geocoder reads the bare token as ISO-3166 ``KP``; the North Korea desk
then wrote 16 of 288 findings in two weeks about a Pakistani province, one at
confidence 0.9, and the judge, the grader and the composition passed every one
of them (verified by SQL 10-08). The same class: "SA" (South Africa in South
African copy, a company's S.A. suffix) on the Saudi desk; "NWS San Diego CA"
on the Canada desk; "New Mexico" on the Mexico desk.

THE DATA is the profile gazetteer's ``subject_homonyms`` section (per subject
code: the ``exclude`` forms, what they ``reads_as``, and ``subject`` forms beyond
the gazetteer's own names for it). THE RULE is one predicate, used three ways:

  **homonym-only** — the text names a homonym form of the subject AND, once
  every homonym form is cut out of it, no longer names the subject. Cutting
  first is what keeps "Niger State" (Nigeria) from being rescued by the word
  "Niger" inside it. A text naming NEITHER is not homonym-only: exclusion needs
  positive evidence, so a Korean-language row or an empty title is never
  dropped by this rule.

  * the desk's SLICE (``signal_slice_scope.HomonymSliceFilter``) drops a row
    whose only match on the desk's code is homonym-only — in SQL, on the slice's
    one statement, with :meth:`HomonymRule.pg_patterns` (the same regex text the
    Python matcher compiles, valid in both engines);
  * the CRITIC (``runtime.actor_critic``) flags each homonym-only claim
    ``off_subject``;
  * the CORRECTNESS GRADER excludes a homonym-only span under a named reason.

A form with no lowercase letter (``KP``, ``S.A.``) matches CASE-SENSITIVELY (a
code is uppercase; "sa" is not "SA"); every other form case-insensitively. Both
on ASCII-letter boundaries — the boundary the geocoder's bare-token regex uses,
so the token that made the mistake is the token this rule sees.

A profile that declares no ``subject_homonyms`` has no rules: the slice and the
grader read it as empty (recorded by name through ``or_empty``), the critic's
check answers ``NotApplicable`` (SEAMS #67).

A leaf: stdlib + ``legba.data.gazetteer`` (itself a leaf).
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Iterable

from .gazetteer import Gazetteer, gazetteer, or_empty

__all__ = [
    "OFF_SUBJECT_REASON",
    "HomonymRule",
    "desk_code",
    "off_subject_claims",
    "rule_for_code",
    "rule_for_target",
    "rules",
]

#: The critique span reason / grader exclusion reason this class is named by.
OFF_SUBJECT_REASON = "off_subject"

_BOUNDARY_L = "(^|[^A-Za-z])"
_BOUNDARY_R = "([^A-Za-z]|$)"


def _case_sensitive(form: str) -> bool:
    return any(c.isupper() for c in form) and not any(c.islower() for c in form)


def _escape(form: str) -> str:
    """A form as regex text valid in BOTH Python ``re`` and Postgres ARE:
    alphanumerics literal, a whitespace run as ``\\s+``, everything else
    backslash-escaped (a backslash before a non-alphanumeric is that character
    literally in both engines)."""
    out: list[str] = []
    for part in re.split(r"(\s+)", form.strip()):
        if not part:
            continue
        if part.isspace():
            out.append(r"\s+")
            continue
        out.extend(c if c.isalnum() else "\\" + c for c in part)
    return "".join(out)


def _alternation(forms: Iterable[str]) -> str | None:
    uniq = sorted({f.strip() for f in forms if f and f.strip()}, key=lambda f: (-len(f), f))
    if not uniq:
        return None
    return _BOUNDARY_L + "(" + "|".join(_escape(f) for f in uniq) + ")" + _BOUNDARY_R


@dataclass(frozen=True)
class HomonymRule:
    """One subject's homonym rule (see the module doc)."""

    #: The subject code, lowercase as the gazetteer keys it (``kp``).
    code: str
    reads_as: tuple[str, ...]
    exclude: tuple[str, ...]
    subject_forms: tuple[str, ...]
    _compiled: dict[str, re.Pattern[str] | None] = field(
        default_factory=dict, repr=False, compare=False,
    )

    @property
    def geo_code(self) -> str:
        """The code as ``signals.geo`` carries it (``KP``)."""
        return self.code.upper()

    def pg_patterns(self) -> tuple[str | None, str | None, str | None, str | None]:
        """``(homonym_cs, homonym_ci, subject_cs, subject_ci)`` regex text;
        ``None`` where a group has no form. ``_cs`` for ``~``, ``_ci`` for
        ``~*`` (Postgres) / ``re.IGNORECASE`` (Python)."""
        return (
            _alternation(f for f in self.exclude if _case_sensitive(f)),
            _alternation(f for f in self.exclude if not _case_sensitive(f)),
            _alternation(f for f in self.subject_forms if _case_sensitive(f)),
            _alternation(f for f in self.subject_forms if not _case_sensitive(f)),
        )

    def _re(self, which: int) -> re.Pattern[str] | None:
        key = str(which)
        if key not in self._compiled:
            text = self.pg_patterns()[which]
            flags = re.IGNORECASE if which in (1, 3) else 0
            self._compiled[key] = None if text is None else re.compile(text, flags)
        return self._compiled[key]

    def homonym_forms_in(self, text: str) -> list[str]:
        """The homonym forms ``text`` names (as written), first-seen order."""
        out: list[str] = []
        for i in (0, 1):
            pat = self._re(i)
            for m in pat.finditer(text or "") if pat is not None else ():
                if m.group(2) not in out:
                    out.append(m.group(2))
        return out

    def names_subject(self, text: str) -> bool:
        """Does ``text`` name the subject once every homonym form is cut out?"""
        stripped = text or ""
        for i in (0, 1):
            pat = self._re(i)
            if pat is not None:
                stripped = pat.sub(" ", stripped)
        return any(
            pat is not None and pat.search(stripped) is not None
            for pat in (self._re(2), self._re(3))
        )

    def homonym_only(self, text: str) -> bool:
        """THE predicate: names a homonym form and, cut, no longer the subject."""
        return bool(self.homonym_forms_in(text)) and not self.names_subject(text)


def _subject_forms(code: str, gz: Gazetteer, extra: Iterable[str]) -> tuple[str, ...]:
    """Every form the gazetteer already knows the subject ``code`` by, plus
    ``extra``: the desk names, slug names, abbreviations, home aliases, and
    the demonyms of any of them."""
    names: list[str] = []
    for section in ("desk_countries", "slug_to_country", "target_slug_names", "slug_to_abbrev"):
        names.extend((gz.get(section) or {}).get(code, ()))
    names.extend((gz.get("iso2_home_aliases") or {}).get(code.upper(), ()))
    folded = {n.casefold() for n in names}
    for name, demonyms in (gz.get("irregular_demonyms") or {}).items():
        if name.casefold() in folded:
            names.extend(demonyms)
    for demonym, canonical in (gz.get("demonym_map") or {}).items():
        if str(canonical).casefold() in folded:
            names.append(demonym)
    names.extend(extra)
    seen: dict[str, None] = {}
    for n in names:
        if n and n.strip():
            seen.setdefault(n.strip(), None)
    return tuple(seen)


_lock = threading.Lock()
_cache: dict[int, tuple[Gazetteer, dict[str, HomonymRule]]] = {}


def rules(gz: Gazetteer | None = None, *, consumer: str = "subject_homonyms") -> dict[str, HomonymRule]:
    """Every declared rule, keyed by lowercase code. Empty (recorded by name)
    when the profile declares no ``subject_homonyms``."""
    gz = gz if gz is not None else gazetteer()
    with _lock:
        hit = _cache.get(id(gz))
        if hit is not None and hit[0] is gz:
            return hit[1]
    declared = or_empty("subject_homonyms", consumer=consumer, gz=gz) or {}
    built = {
        code.lower(): HomonymRule(
            code=code.lower(),
            reads_as=tuple(entry.reads_as),
            exclude=tuple(entry.exclude),
            subject_forms=_subject_forms(code.lower(), gz, entry.subject),
        )
        for code, entry in declared.items()
    }
    with _lock:
        _cache[id(gz)] = (gz, built)
    return built


def rule_for_code(code: str | None, gz: Gazetteer | None = None) -> HomonymRule | None:
    """The rule for one subject code (any case), or ``None``."""
    if not code:
        return None
    return rules(gz).get(str(code).strip().lower())


def desk_code(target_id: str | None, gz: Gazetteer | None = None) -> str | None:
    """The subject code a per-subject desk id carries (the gazetteer's
    ``desk_ids`` convention: ``country_watch_kp`` -> ``kp``), else ``None``."""
    if not target_id or not isinstance(target_id, str):
        return None
    marker = (gz if gz is not None else gazetteer()).get("desk_ids")
    tid = target_id.strip().lower()
    if not marker or marker not in tid:
        return None
    token = tid.rsplit("_", 1)[-1]
    return token if len(token) == 2 and token.isalpha() else None


def rule_for_target(target_id: str | None, gz: Gazetteer | None = None) -> HomonymRule | None:
    """The rule for a per-subject desk id, or ``None``."""
    return rule_for_code(desk_code(target_id, gz), gz)


def off_subject_claims(claims: Iterable[str], rule: HomonymRule) -> list[str]:
    """The claims that are homonym-only under ``rule``, in order. The caller
    segments (the critic passes the title plus the SHIPPED segmenter's spans,
    so this check and the judge see the same claims)."""
    return [c.strip() for c in claims if c and c.strip() and rule.homonym_only(c)]
